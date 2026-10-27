import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit

import jwt

from .providers import ACCOUNT_LOCK, ProviderError, request_json


def challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


class Auth:
    def __init__(self, store, password, base_url):
        if len(password) < 12:
            raise RuntimeError("Set FORGETMENOT_PASSWORD to at least 12 characters.")
        self.store, self.base_url = store, base_url.rstrip("/")
        self.password_hash = hashlib.scrypt(password.encode(), salt=store.key, n=16384, r=8, p=1)
        self.sessions = {}
        self.failures = {}
        self.lock = threading.Lock()

    def login(self, password, address):
        now = time.time()
        with self.lock:
            self.failures = {key: value for key, value in self.failures.items() if value[1] > now - 300}
            count, started = self.failures.get(address, (0, now))
            if count >= 10:
                raise ProviderError("Too many login attempts. Please wait five minutes.")
            candidate = hashlib.scrypt(password.encode(), salt=self.store.key, n=16384, r=8, p=1)
            if not hmac.compare_digest(candidate, self.password_hash):
                self.failures[address] = (count + 1, started)
                raise ProviderError("That password was not accepted.")
            self.failures.pop(address, None)
            self.sessions = {key: expiry for key, expiry in self.sessions.items() if expiry > now}
            token = secrets.token_urlsafe(32)
            self.sessions[token] = now + 86400
            return token

    def valid(self, token):
        with self.lock:
            return self.sessions.get(token, 0) > time.time()

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token, None)

    def start_oauth(self, provider, session, account_id=None):
        state, verifier, nonce = (secrets.token_urlsafe(32) for _ in range(3))
        pending = {"state": state, "verifier": verifier, "nonce": nonce,
                   "session": session, "expires": time.time() + 600}
        if provider == "openrouter":
            pending["callback"] = self.base_url + "/oauth/openrouter/callback"
            url = "https://openrouter.ai/auth?" + urlencode({
                "callback_url": pending["callback"], "state": state, "code_challenge": challenge(verifier),
                "code_challenge_method": "S256", "key_label": "ForgetMeNot",
            })
        elif provider == "chatgpt":
            parsed = urlsplit(self.base_url)
            if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
                raise ProviderError("ChatGPT requires a 127.0.0.1 loopback callback. Use the local app or an SSH tunnel; see the README.")
            account = self.store.setting("chatgpt_accounts", {}).get(account_id, {}) if account_id else {}
            if account_id and not account:
                raise ProviderError("That saved ChatGPT connection was not found.")
            pending.update(callback=self.base_url + "/auth/callback", account_id=account_id,
                           client_id=account.get("client_id", "dynamic_agent_client"))
            parameters = {
                "client_id": pending["client_id"], "ext_agent_host_id": self.store.host_id(),
                "response_type": "code", "redirect_uri": pending["callback"],
                "scope": "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct",
                "resource": "https://api.openai.com/v1", "state": state, "nonce": nonce,
                "code_challenge": challenge(verifier), "code_challenge_method": "S256",
            }
            if account:
                if account.get("id_token"):
                    parameters["id_token_hint"] = account["id_token"]
            else:
                parameters["agent_name_hint"] = "ForgetMeNot"
            url = "https://auth.openai.com/api/accounts/authorize?" + urlencode(parameters)
        else:
            raise ProviderError("That provider does not offer a supported OAuth flow here.")
        self.store.save_setting("oauth_" + provider, pending)
        return url

    def complete_oauth(self, provider, query, session):
        pending = self.store.setting("oauth_" + provider, {})
        if (not pending or pending["expires"] < time.time() or pending["session"] != session
                or not hmac.compare_digest(pending["state"], query.get("state", ""))):
            raise ProviderError("The sign-in state is invalid or expired. Start sign-in again in Settings.")
        self.store.save_setting("oauth_" + provider, {})
        if query.get("error"):
            raise ProviderError("Account authorization was declined. Your existing connection has been kept.")
        code = query.get("code", "")
        if not code or len(code) > 4096:
            raise ProviderError("No valid authorization code was returned.")
        if provider == "openrouter":
            result = request_json("POST", "https://openrouter.ai/api/v1/auth/keys", json={
                "code": code, "code_verifier": pending["verifier"], "code_challenge_method": "S256",
            })
            if not isinstance(result.get("key"), str) or not result["key"]:
                raise ProviderError("OpenRouter did not return an API key.")
            self.store.save_setting("openrouter_key", result["key"])
            return
        client_id = query.get("client_id") or pending["client_id"]
        if client_id == "dynamic_agent_client" or not re.fullmatch(r"[a-zA-Z0-9_-]{3,200}", client_id):
            raise ProviderError("OpenAI did not return an issued client ID.")
        if pending["client_id"] != "dynamic_agent_client" and client_id != pending["client_id"]:
            raise ProviderError("OpenAI returned a different client registration. Your saved account has been kept.")
        # Retain issued registration even if code exchange fails; retries must reuse its client ID.
        with ACCOUNT_LOCK:
            accounts = self.store.setting("chatgpt_accounts", {})
            if client_id not in accounts:
                accounts[client_id] = {"client_id": client_id, "email": "Incomplete registration", "subject": None}
                self.store.save_setting("chatgpt_accounts", accounts)
        result = request_json("POST", "https://auth.openai.com/api/accounts/oauth/token", data={
            "grant_type": "authorization_code", "client_id": client_id, "code": code,
            "code_verifier": pending["verifier"], "redirect_uri": pending["callback"],
            "resource": "https://api.openai.com/v1",
        })
        claims = verify_identity(result.get("id_token", ""), client_id, pending["nonce"])
        scopes = result.get("scope", "").split()
        if "chatgpt.tokens.use.direct" not in scopes or not result.get("access_token") or not result.get("refresh_token"):
            raise ProviderError("ChatGPT plan usage was not enabled. Authorize plan usage and offline access when reconnecting.")
        with ACCOUNT_LOCK:
            accounts = self.store.setting("chatgpt_accounts", {})
            old = accounts.get(client_id)
            if old and old.get("subject") and old["subject"] != claims["sub"]:
                raise ProviderError("The signed-in identity differs from the saved connection. Existing credentials have been kept.")
            accounts[client_id] = {
                "client_id": client_id, "subject": claims["sub"], "email": claims.get("email", "ChatGPT account"),
                "access_token": result["access_token"], "refresh_token": result["refresh_token"],
                "id_token": result["id_token"], "scopes": scopes,
                "expires_at": time.time() + int(result.get("expires_in", 3600)),
            }
            self.store.save_setting("chatgpt_accounts", accounts)
            self.store.save_setting("chatgpt_selected", client_id)
            self.store.save_setting("model", {})

    def disconnect_chatgpt(self, account_id):
        with ACCOUNT_LOCK:
            accounts = self.store.setting("chatgpt_accounts", {})
            account = accounts.get(account_id)
            if not account:
                raise ProviderError("That ChatGPT account was not found.")
            if account.get("refresh_token"):
                discovery = request_json("GET", "https://auth.openai.com/.well-known/openid-configuration")
                endpoint = discovery.get("revocation_endpoint", "")
                if urlsplit(endpoint).scheme != "https" or urlsplit(endpoint).hostname != "auth.openai.com":
                    raise ProviderError("OpenAI did not provide a trusted revocation endpoint.")
                # Revocation succeeds with an empty HTTP 200 response.
                import httpx
                try:
                    response = httpx.post(endpoint, data={"token": account["refresh_token"],
                        "token_type_hint": "refresh_token", "client_id": account_id}, timeout=30, trust_env=False)
                    if response.status_code != 200:
                        raise ProviderError("Remote revocation failed. Retry, or disconnect ForgetMeNot in ChatGPT Settings.")
                except httpx.HTTPError as exc:
                    raise ProviderError("Remote revocation could not be confirmed. Retry, or disconnect in ChatGPT Settings.") from exc
            for name in ("access_token", "refresh_token", "id_token"):
                account.pop(name, None)
            accounts[account_id] = account
            self.store.save_setting("chatgpt_accounts", accounts)
            if self.store.setting("chatgpt_selected") == account_id:
                self.store.save_setting("chatgpt_selected", None)
                if self.store.setting("model", {}).get("provider") == "chatgpt":
                    self.store.save_setting("model", {})


def verify_identity(token, client_id, nonce):
    try:
        discovery = request_json("GET", "https://auth.openai.com/.well-known/openid-configuration")
        endpoint = discovery["jwks_uri"]
        if urlsplit(endpoint).scheme != "https" or urlsplit(endpoint).hostname != "auth.openai.com":
            raise ProviderError("OpenAI did not provide a trusted signing-key endpoint.")
        header = jwt.get_unverified_header(token)
        keys = jwt.PyJWKSet.from_dict(request_json("GET", endpoint))
        key = next(key for key in keys.keys if key.key_id == header.get("kid"))
        claims = jwt.decode(token, key.key, algorithms=["RS256", "ES256"], audience=client_id,
                            issuer="https://auth.openai.com", options={"require": ["exp", "iss", "aud", "sub", "nonce"]})
        if not isinstance(claims["nonce"], str) or not hmac.compare_digest(claims["nonce"], nonce):
            raise ProviderError("The OpenAI identity nonce did not match.")
        return claims
    except (jwt.PyJWTError, KeyError, StopIteration, TypeError, ValueError) as exc:
        raise ProviderError("The OpenAI identity token could not be verified. Existing credentials have been kept.") from exc
