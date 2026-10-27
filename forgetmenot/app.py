import asyncio
from contextlib import asynccontextmanager
import fcntl
import os
from pathlib import Path
import re
import shutil
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
import uvicorn

from .auth import Auth
from . import providers
from .providers import ACCOUNT_LOCK, ProviderError
from .store import INTERVALS, Store
from .worker import Worker, smtp_ready


STATIC = Path(__file__).parent / "static"


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    password: str = Field(min_length=1, max_length=1024)


class ModelChoice(Input):
    provider: Literal["openrouter", "chatgpt", "gemini_cli", "claude", "codex"]
    id: str = Field(min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_./:@+-]*$")


class Watch(Input):
    goal: str = Field(min_length=5, max_length=4000)
    interval: int | None = Field(default=None, strict=True)
    channels: list[Literal["email", "telegram", "push"]] = Field(default_factory=list, max_length=3)
    model: ModelChoice | None = None


class Settings(Input):
    openrouter_key: str | None = Field(default=None, max_length=1000)
    claude_key: str | None = Field(default=None, max_length=1000)
    email: str = Field(default="", max_length=254)
    ntfy_topic: str = Field(default="", max_length=100)
    ntfy_token: str | None = Field(default=None, max_length=1000)
    telegram_token: str | None = Field(default=None, max_length=200)
    telegram_chat: str = Field(default="", max_length=100)


class OAuthStart(Input):
    account_id: str | None = Field(default=None, max_length=200)


class AccountChoice(Input):
    account_id: str = Field(min_length=1, max_length=200)


def create_app(directory=None, password=None, base_url=None, start_worker=True):
    base_url = (base_url or os.getenv("FORGETMENOT_BASE_URL", "http://127.0.0.1:8787")).rstrip("/")
    parsed = urlsplit(base_url)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
            or parsed.scheme not in ("http", "https")
            or (parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost"))):
        raise RuntimeError("FORGETMENOT_BASE_URL must be an HTTPS origin, or an HTTP loopback origin.")
    store = Store(directory or os.getenv("FORGETMENOT_DATA", ".data"))
    auth = Auth(store, password if password is not None else os.getenv("FORGETMENOT_PASSWORD", ""), base_url)
    worker = Worker(store)

    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            lock = (store.directory / "worker.lock").open("a+")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                lock.close()
                raise RuntimeError("Another ForgetMeNot process already owns this data directory. Use a single server worker.") from exc
            worker.thread.start()
        try:
            yield
        finally:
            if start_worker:
                worker.stop.set()
                await asyncio.to_thread(worker.thread.join)
                lock.close()

    app = FastAPI(title="ForgetMeNot", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store, app.state.auth, app.state.worker = store, auth, worker

    @app.middleware("http")
    async def protection(request, call_next):
        if request.headers.get("host", "").lower() != parsed.netloc.lower():
            return JSONResponse({"error": "Unrecognised host. Use FORGETMENOT_BASE_URL."}, status_code=400)
        protected = request.url.path.startswith("/api/") or request.url.path in ("/auth/callback", "/oauth/openrouter/callback")
        if protected:
            if request.method not in ("GET", "HEAD"):
                if request.headers.get("x-forgetmenot") != "1" or request.headers.get("origin", base_url) != base_url:
                    return JSONResponse({"error": "Request origin was not accepted."}, status_code=403)
                size = request.headers.get("content-length", "0")
                if not size.isdigit() or int(size) > 16384:
                    return JSONResponse({"error": "Request is too large."}, status_code=413)
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 16384:
                        return JSONResponse({"error": "Request is too large."}, status_code=413)
                # Replay the bounded body for FastAPI's downstream validation.
                request._body = bytes(body)
            if request.url.path != "/api/login" and not auth.valid(request.cookies.get("fmn_session")):
                return JSONResponse({"error": "Please unlock ForgetMeNot."}, status_code=401)
        response = await call_next(request)
        response.headers.update({
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        })
        return response

    @app.exception_handler(ProviderError)
    async def provider_error(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.post("/api/login")
    def login(value: Login, request: Request):
        token = auth.login(value.password, request.client.host)
        response = JSONResponse({"ok": True})
        response.set_cookie("fmn_session", token, max_age=86400, httponly=True, secure=parsed.scheme == "https", samesite="lax", path="/")
        return response

    @app.post("/api/logout")
    def logout(request: Request):
        auth.logout(request.cookies.get("fmn_session"))
        response = JSONResponse({"ok": True})
        response.delete_cookie("fmn_session")
        return response

    def safe_settings():
        accounts = store.setting("chatgpt_accounts", {})
        return {
            "openrouter_connected": bool(store.setting("openrouter_key")),
            "claude_connected": bool(store.setting("claude_key")),
            "email": store.setting("email", ""), "smtp_ready": smtp_ready(),
            "ntfy_topic": store.setting("ntfy_topic", ""), "ntfy_token_set": bool(store.setting("ntfy_token")),
            "telegram_chat": store.setting("telegram_chat", ""), "telegram_token_set": bool(store.setting("telegram_token")),
            "codex_connected": bool(shutil.which("codex") and (store.directory / "codex" / "auth.json").is_file()),
            "gemini_available": bool(shutil.which("gemini")),
            "model": store.setting("model", {}), "chatgpt_selected": store.setting("chatgpt_selected"),
            "chatgpt_accounts": [{"id": key, "label": value["email"] + " · " + key[-6:],
                                  "connected": bool(value.get("access_token"))} for key, value in accounts.items()],
            "chatgpt_loopback": parsed.scheme == "http" and parsed.hostname == "127.0.0.1",
        }

    @app.get("/api/state")
    def state():
        return {"watches": store.watches(), "settings": safe_settings(), "inbox": store.inbox(),
                "worker": {"running": worker.thread.is_alive(), "last_tick": worker.last_tick, "error": worker.error}}

    @app.put("/api/settings")
    def settings(value: Settings):
        if value.email and not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", value.email):
            raise ProviderError("Enter one valid email address.")
        if value.ntfy_topic and not re.fullmatch(r"[a-zA-Z0-9_-]{8,100}", value.ntfy_topic):
            raise ProviderError("Use an ntfy topic with 8–100 letters, numbers, hyphens or underscores.")
        if value.claude_key and (not value.claude_key.startswith("sk-ant-api") or "\n" in value.claude_key):
            raise ProviderError("Use a Claude Console API key. Claude subscription OAuth tokens are not accepted.")
        if value.telegram_token and not re.fullmatch(r"\d+:[a-zA-Z0-9_-]{20,180}", value.telegram_token):
            raise ProviderError("Use the Telegram bot token supplied by BotFather.")
        if value.telegram_chat and not re.fullmatch(r"-?\d{1,20}|@[a-zA-Z][a-zA-Z0-9_]{4,32}", value.telegram_chat):
            raise ProviderError("Use a Telegram chat ID or public channel username.")
        for name in ("openrouter_key", "claude_key", "ntfy_token", "telegram_token"):
            credential = getattr(value, name)
            if credential is not None:
                if any(ord(c) < 33 for c in credential):
                    raise ProviderError("Credentials must not contain whitespace or control characters.")
        for name, content in value.model_dump().items():
            if content is not None:
                store.save_setting(name, content)
        return safe_settings()

    @app.get("/api/models/{provider}")
    def list_models(provider: str):
        return {"models": providers.models(store, provider)}

    @app.post("/api/codex/connect")
    def connect_codex():
        from . import codex_runtime
        codex_runtime.connect(store)
        return {"ok": True}

    @app.put("/api/model")
    def select_model(value: ModelChoice):
        available = providers.models(store, value.provider)
        if value.provider != "gemini_cli" and value.id not in {model["id"] for model in available}:
            raise ProviderError("Select a model from this account's current catalog.")
        store.save_setting("model", value.model_dump())
        return {"ok": True}

    def validate_watch(value):
        goal = value.goal.strip()
        if len(goal) < 5:
            raise ProviderError("Describe what you want to watch in at least five characters.")
        if value.interval is not None and value.interval not in INTERVALS:
            raise ProviderError("Select one of the supported check intervals.")
        if len(set(value.channels)) != len(value.channels):
            raise ProviderError("Notification channels must be unique.")
        if "email" in value.channels and (not store.setting("email") or not smtp_ready()):
            raise ProviderError("Configure an email address and server SMTP before enabling email.")
        if "push" in value.channels and not store.setting("ntfy_topic"):
            raise ProviderError("Configure an ntfy topic before enabling push.")
        if "telegram" in value.channels and (not store.setting("telegram_token") or not store.setting("telegram_chat")):
            raise ProviderError("Connect a Telegram bot and chat in Settings before enabling Telegram.")
        if value.model:
            available = providers.models(store, value.model.provider)
            if value.model.provider != "gemini_cli" and value.model.id not in {item["id"] for item in available}:
                raise ProviderError("Select a model available to this account.")
        return goal

    @app.post("/api/watches", status_code=201)
    def add_watch(value: Watch):
        return {"id": store.create(validate_watch(value), value.interval, value.channels, value.model.model_dump() if value.model else None)}

    @app.put("/api/watches/{watch_id}")
    def edit_watch(watch_id: str, value: Watch):
        if not store.edit(watch_id, validate_watch(value), value.interval, value.channels, value.model.model_dump() if value.model else None):
            raise HTTPException(404, "Watch not found.")
        return {"ok": True}

    @app.post("/api/watches/{watch_id}/{action}")
    def watch_action(watch_id: str, action: Literal["pause", "resume", "check", "delete"]):
        if not store.update(watch_id, action):
            raise HTTPException(404, "Watch not found.")
        return {"ok": True}

    @app.get("/api/watches/{watch_id}/history")
    def history(watch_id: str):
        return {"checks": store.history(watch_id)}

    @app.post("/api/inbox/read")
    def read_inbox():
        with store.connect() as db:
            db.execute("UPDATE notifications SET seen=1")
        return {"ok": True}

    @app.post("/api/oauth/{provider}/start")
    def start_oauth(provider: str, value: OAuthStart, request: Request):
        return {"url": auth.start_oauth(provider, request.cookies["fmn_session"], value.account_id)}

    def callback(provider, request):
        try:
            auth.complete_oauth(provider, dict(request.query_params), request.cookies["fmn_session"])
            return RedirectResponse("/?connected=" + provider, status_code=303)
        except ProviderError as exc:
            # Only a local, generic error is retained; authorization codes never enter logs or HTML.
            store.save_setting("connection_error", str(exc))
            return RedirectResponse("/?connection_error=1", status_code=303)

    @app.get("/oauth/openrouter/callback")
    def openrouter_callback(request: Request):
        return callback("openrouter", request)

    @app.get("/auth/callback")
    def chatgpt_callback(request: Request):
        return callback("chatgpt", request)

    @app.get("/api/connection-error")
    def connection_error():
        error = store.setting("connection_error", "Sign-in could not be completed.")
        store.save_setting("connection_error", "")
        return {"error": error}

    @app.put("/api/chatgpt/account")
    def choose_account(value: AccountChoice):
        with ACCOUNT_LOCK:
            account = store.setting("chatgpt_accounts", {}).get(value.account_id)
            if not account or not account.get("access_token"):
                raise ProviderError("Reconnect that account first.")
            store.save_setting("chatgpt_selected", value.account_id)
            store.save_setting("model", {})
        return {"ok": True}

    @app.post("/api/chatgpt/disconnect")
    def disconnect(value: AccountChoice):
        auth.disconnect_chatgpt(value.account_id)
        return {"ok": True}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def main():
    os.umask(0o077)
    # Access logs would otherwise retain OAuth callback query strings.
    uvicorn.run(create_app(), host=os.getenv("FORGETMENOT_HOST", "127.0.0.1"),
                port=int(os.getenv("FORGETMENOT_PORT", "8787")), access_log=False)


if __name__ == "__main__":
    main()
