"""Read-only research through the existing open-source Codex agent runtime."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading

from .providers import HTTP_TIMEOUT, ProviderError, public_source, validated_result


LOCK = threading.RLock()
DISABLED = ("shell_tool", "plugins", "apps", "hooks", "multi_agent", "memories",
            "browser_use", "computer_use", "image_generation", "view_image", "code_mode", "shell_snapshot")


def connect(store):
    if not shutil.which("codex"):
        raise ProviderError("Install the official Codex CLI and sign in with ChatGPT on this server first.")
    source = Path.home() / ".codex" / "auth.json"
    try:
        raw = source.read_bytes()
        data = json.loads(raw)
        if data.get("auth_mode") != "chatgpt" or not data.get("tokens", {}).get("access_token"):
            raise ValueError("not a ChatGPT session")
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise ProviderError("No file-based ChatGPT sign-in was found. Sign in with the official Codex CLI on this server.") from exc
    directory = store.directory / "codex"
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    with LOCK:
        temporary = directory / "auth.pending"
        try:
            with temporary.open("wb") as handle:
                os.chmod(temporary, 0o600)
                handle.write(raw)
            temporary.replace(directory / "auth.json")
        finally:
            temporary.unlink(missing_ok=True)


def models(store):
    directory = store.directory / "codex"
    if not shutil.which("codex") or not (directory / "auth.json").is_file():
        raise ProviderError("Connect the local Codex ChatGPT sign-in in Settings.")
    try:
        catalog = json.loads((directory / "models_cache.json").read_text())
        choices = [{"id": item["slug"], "name": item.get("display_name", item["slug"])}
                   for item in catalog.get("models", []) if item.get("visibility") == "list"]
        if choices:
            return choices
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return [{"id": "auto", "name": "Codex account default"}]


def command(store, model, work):
    executable = shutil.which("codex")
    if not executable or not (store.directory / "codex" / "auth.json").is_file():
        raise ProviderError("Connect the official Codex runtime in Settings.")
    arguments = [executable, "-a", "never", "exec", "--ignore-user-config", "--ephemeral", "--json",
                 "--sandbox", "read-only", "--skip-git-repo-check", "-C", work,
                 "-c", 'web_search="live"', "-c", "project_doc_max_bytes=0", "-c", 'model_reasoning_effort="low"',
                 "-c", 'cli_auth_credentials_store="file"', "-c", "mcp_servers={}"]
    if model != "auto":
        arguments += ["--model", model]
    for feature in DISABLED:
        arguments += ["--disable", feature]
    return arguments + ["--enable", "skip_host_skill_discovery", "-"]


def parse_events(output):
    text, opened, completed, activity = "", [], False, []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") in ("turn.failed", "error"):
            raise ProviderError("The Codex research turn failed. Check its ChatGPT sign-in and account limits.")
        if event.get("type") == "turn.completed":
            completed = True
        if event.get("type") != "item.completed":
            continue
        item = event.get("item", {})
        if item.get("type") in ("command_execution", "file_change", "mcp_tool_call"):
            raise ProviderError("The runtime attempted a tool outside the research-only configuration.")
        if item.get("type") == "agent_message":
            text = item.get("text", "")
        if item.get("type") == "web_search":
            action = item.get("action", {})
            url = public_source(action.get("url"))
            activity.append({"tool": "web_search", "action": action.get("type", "search"), **({"url": url} if url else {})})
            if url:
                opened.append(url)
    if not completed or not activity:
        raise ProviderError("Codex did not complete a live research turn. No match has been recorded.")
    result = validated_result(text, opened)
    result["activity"] = activity
    return result


def check(store, model, task):
    task += """\nUse the live web tool for research. Before including ANY evidence URL, open its full
absolute URL directly with the web tool; do not use an opaque result ID or a click reference.
Only include URLs you opened this way in this turn. Prefer 1–3 primary sources.
The only allowed work is web research. Do not use files, commands, apps, plugins or subagents.
Return the requested JSON as your final message, without citation markers inside the JSON.
"""
    environment = {key: value for key, value in os.environ.items() if key in {
        "PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS",
    }}
    environment["CODEX_HOME"] = str((store.directory / "codex").resolve())
    with LOCK, tempfile.TemporaryDirectory(prefix="forgetmenot-research-") as work:
        try:
            run = subprocess.run(command(store, model, work), input=task, text=True, capture_output=True,
                                 env=environment, cwd=work, timeout=HTTP_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProviderError("The Codex research turn could not finish within its time limit. It will be retried.") from exc
    if run.returncode or len(run.stdout) > 8_000_000:
        raise ProviderError("The Codex runtime failed. Check its private sign-in, CLI version and account limits.")
    return parse_events(run.stdout)
