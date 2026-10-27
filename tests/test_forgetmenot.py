from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import jwt

from forgetmenot.app import create_app
from forgetmenot.auth import Auth, verify_identity
from forgetmenot import providers
from forgetmenot.providers import ProviderError, validated_result
from forgetmenot.store import Store
from forgetmenot.worker import Worker, send_delivery


SOURCE = "https://example.com/product"
RESULT = {"outcome": "matched", "title": "The requested product", "approach": "Check the official product listing.",
          "summary": "The exact requested variant is in stock for delivery to the requested country.", "interval": 3600,
          "evidence": [{"url": SOURCE, "title": "Official listing", "detail": "The requested variant is available today."}]}
PASSWORD = "a-test-password-only"


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)

    def watch(self, interval=60, channels=None):
        return self.store.create("Watch an exact product at its official retailer", interval, channels or [])

    def test_persistence_and_encrypted_credentials(self):
        watch_id = self.watch()
        secret = "a-credential-that-must-never-appear-in-sqlite"
        self.store.save_setting("openrouter_key", secret)
        other = Store(self.temp.name)
        self.assertEqual(other.setting("openrouter_key"), secret)
        self.assertEqual(other.watches()[0]["id"], watch_id)
        with other.connect() as db:
            encoded = db.execute("SELECT value FROM settings WHERE name='openrouter_key'").fetchone()[0]
        self.assertNotIn(secret, encoded)
        self.assertEqual((Path(self.temp.name)/"master.key").stat().st_mode & 0o777, 0o600)
        self.assertEqual(other.host_id(), self.store.host_id())

    def test_missing_key_refuses_to_replace_credentials(self):
        (Path(self.temp.name)/"master.key").unlink()
        with self.assertRaisesRegex(RuntimeError, "Restore"):
            Store(self.temp.name)

    def test_concurrent_claims_and_restart_recovery(self):
        self.watch()
        with ThreadPoolExecutor(max_workers=6) as pool:
            claims = list(pool.map(lambda _: self.store.claim(), range(6)))
        claimed = [claim for claim in claims if claim]
        self.assertEqual(len(claimed), 1)
        other = Store(self.temp.name)
        self.assertIsNone(other.claim())
        recovered = other.claim(time.time() + 601)
        self.assertIsNotNone(recovered)
        self.assertNotEqual(claimed[0]["claim"], recovered["claim"])
        self.assertFalse(self.store.finish(claimed[0], RESULT, "test"))

    def test_match_is_atomic_and_not_repeated(self):
        self.watch(channels=["email", "push"])
        watch = self.store.claim()
        self.assertTrue(self.store.finish(watch, RESULT, "test"))
        self.assertFalse(self.store.finish(watch, RESULT, "test"))
        self.assertEqual(self.store.watches()[0]["status"], "matched")
        self.assertIsNone(self.store.claim())
        self.assertEqual(len(self.store.inbox()["notifications"]), 1)
        self.assertEqual(len(self.store.inbox()["deliveries"]), 2)

    def test_fixed_interval_overrides_model_recommendation(self):
        watch_id = self.watch(interval=60)
        waiting = dict(RESULT, outcome="waiting")
        before = time.time()
        self.store.finish(self.store.claim(), waiting, "test", before)
        watch = self.store.watches()[0]
        self.assertEqual(watch["interval"], 60)
        self.assertEqual(watch["next_check"], before+60)
        self.assertEqual(len(self.store.history(watch_id)), 1)
        self.assertEqual(self.store.inbox()["notifications"], [])

    def test_automatic_interval_uses_model_recommendation(self):
        self.watch(interval=None)
        self.store.finish(self.store.claim(), dict(RESULT, outcome="waiting"), "test")
        self.assertEqual(self.store.watches()[0]["interval"], 3600)

    def test_inconclusive_backs_off_without_recording_a_match(self):
        self.watch()
        self.store.finish(self.store.claim(), dict(RESULT, outcome="inconclusive", summary="Source unavailable"), "test")
        watch = self.store.watches()[0]
        self.assertEqual(watch["status"], "active")
        self.assertEqual(watch["failures"], 1)
        self.assertGreaterEqual(watch["next_check"] - watch["last_check"], 600)
        self.assertEqual(self.store.inbox()["notifications"], [])

    def test_pause_edit_and_delete_discard_inflight_results(self):
        for action in ("pause", "edit", "delete"):
            with self.subTest(action=action):
                watch_id = self.watch()
                watch = self.store.claim()
                if action == "edit":
                    self.store.edit(watch_id, "A different exact product", 86400, [])
                else:
                    self.store.update(watch_id, action)
                self.assertFalse(self.store.finish(watch, RESULT, "test"))
                self.assertEqual(self.store.history(watch_id), [])
                self.store.update(watch_id, "delete")

    def test_delete_cascades_history_and_delivery_queue(self):
        watch_id = self.watch(channels=["email"])
        self.store.finish(self.store.claim(), RESULT, "test")
        self.store.update(watch_id, "delete")
        self.assertEqual(self.store.history(watch_id), [])
        self.assertEqual(self.store.inbox(), {"notifications": [], "deliveries": []})
        self.assertIsNone(self.store.pending_delivery())

    def test_delivery_retry_is_persistent(self):
        self.watch(channels=["push"])
        self.store.finish(self.store.claim(), RESULT, "test")
        delivery = self.store.pending_delivery()
        self.store.delivered(delivery, "Try again")
        self.assertIsNone(self.store.pending_delivery())
        row = Store(self.temp.name).inbox()["deliveries"][0]
        self.assertEqual(row["attempts"], 1)
        self.assertIsNone(row["delivered"])
        self.store.delivered(dict(delivery, attempts=1))
        self.assertIsNotNone(self.store.inbox()["deliveries"][0]["delivered"])

    def test_worker_failure_does_not_become_a_match(self):
        self.watch()
        self.store.save_setting("model", {"provider": "openrouter", "id": "test"})
        with patch("forgetmenot.worker.providers.check", side_effect=ProviderError("Provider offline")):
            Worker(self.store).tick()
        self.assertEqual(self.store.watches()[0]["error"], "Provider offline")
        self.assertEqual(self.store.inbox()["notifications"], [])

    def test_delivery_continues_when_model_is_disconnected(self):
        self.watch(channels=["push"])
        self.store.finish(self.store.claim(), RESULT, "test")
        with patch("forgetmenot.worker.send_delivery") as deliver:
            Worker(self.store).tick()
        deliver.assert_called_once()
        self.assertIsNotNone(self.store.inbox()["deliveries"][0]["delivered"])

    def test_three_failures_send_one_attention_notice_and_keep_watch_active(self):
        watch_id = self.watch(channels=["email"])
        for _ in range(4):
            self.store.update(watch_id, "check")
            self.store.finish(self.store.claim(), dict(RESULT, outcome="inconclusive", summary="Provider unavailable"), "test")
        inbox = self.store.inbox()
        self.assertEqual(len(inbox["notifications"]), 1)
        self.assertEqual(inbox["notifications"][0]["kind"], "attention")
        self.assertTrue(inbox["notifications"][0]["title"].startswith("Needs attention"))
        self.assertEqual(self.store.watches()[0]["status"], "active")
        self.assertEqual(len(inbox["deliveries"]), 1)

    def test_full_search_to_push_pipeline_with_mocked_transport(self):
        self.watch(channels=["push"])
        self.store.save_setting("model", {"provider": "openrouter", "id": "test/model"})
        self.store.save_setting("openrouter_key", "test-key")
        self.store.save_setting("ntfy_topic", "a-private-test-topic")
        completion = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(RESULT),
            "annotations": [{"type": "url_citation", "url_citation": {"url": SOURCE}}]}}]}
        def transport(method, url, **kwargs):
            if "chat/completions" in url:
                self.assertEqual(kwargs["json"]["tools"][0]["type"], "openrouter:web_search")
                return completion
            self.assertEqual(url, "https://ntfy.sh")
            self.assertIn(SOURCE, kwargs["json"]["message"])
            return {"id": "test-notification"}
        with patch("forgetmenot.providers.request_json", side_effect=transport) as request:
            worker = Worker(self.store)
            worker.tick()
            worker.tick()
            worker.tick()
        self.assertEqual(request.call_count, 2)
        self.assertIsNotNone(self.store.inbox()["deliveries"][0]["delivered"])

    def test_email_transport_requires_tls_and_uses_configured_destination(self):
        self.watch(channels=["email"])
        self.store.save_setting("email", "recipient@example.com")
        self.store.finish(self.store.claim(), RESULT, "test")
        environment = {"SMTP_HOST": "smtp.example.com", "SMTP_FROM": "sender@example.com", "SMTP_USER": "test-user", "SMTP_PASSWORD": "test-password"}
        with patch.dict(os.environ, environment, clear=True), patch("smtplib.SMTP") as smtp:
            send_delivery(self.store, self.store.pending_delivery())
            connection = smtp.return_value.__enter__.return_value
            connection.starttls.assert_called_once()
            connection.login.assert_called_once_with("test-user", "test-password")
            message = connection.send_message.call_args.args[0]
            self.assertEqual(message["To"], "recipient@example.com")
            self.assertIn(SOURCE, message.get_content())


class ProviderTests(unittest.TestCase):
    def test_real_citation_required_for_match_and_waiting(self):
        self.assertEqual(validated_result(json.dumps(RESULT), [SOURCE])["outcome"], "matched")
        for outcome in ("matched", "waiting"):
            with self.subTest(outcome=outcome):
                result = validated_result(json.dumps(dict(RESULT, outcome=outcome)), ["https://invented.example/"])
                self.assertEqual(result["outcome"], "inconclusive")
                self.assertEqual(result["evidence"], [])

    def test_partial_and_unsafe_citations_cannot_confirm_a_match(self):
        partial = deepcopy(RESULT)
        partial["evidence"].append(dict(RESULT["evidence"][0], url="javascript:alert(1)"))
        self.assertEqual(validated_result(json.dumps(partial), [SOURCE])["outcome"], "inconclusive")
        for url in ("javascript:alert(1)", "file:///etc/passwd", "https://user:pass@example.com", "http://localhost/", "https://example.com/\n"):
            self.assertIsNone(providers.public_source(url))

    def test_malformed_results_and_intervals_are_rejected(self):
        for item in (None, "not JSON", "[]", json.dumps(dict(RESULT, interval=True)), json.dumps(dict(RESULT, interval=1)), json.dumps(dict(RESULT, evidence=[None]))):
            with self.subTest(item=item), self.assertRaises(ProviderError):
                validated_result(item, [SOURCE])

    def test_openai_stream_requires_terminal_completion(self):
        with self.assertRaisesRegex(ProviderError, "without a completed"):
            providers.parse_responses_events(['data: {"type":"response.output_text.delta","delta":"hello"}'])
        for event_type in ("response.failed", "response.incomplete", "error"):
            with self.assertRaises(ProviderError):
                providers.parse_responses_events(["data: " + json.dumps({"type": event_type})])
        event = {"type": "response.completed", "response": {"status": "completed", "output": [{"type": "message", "content": [{
            "type": "output_text", "text": json.dumps(RESULT), "annotations": [{"type": "url_citation", "url": SOURCE}]}]}]}}
        self.assertEqual(providers.parse_responses_events(["data: " + json.dumps(event)])["outcome"], "matched")

    def test_openrouter_rejects_truncated_answer(self):
        store = unittest.mock.Mock()
        store.setting.return_value = "test-key"
        answer = {"choices": [{"finish_reason": "length", "message": {"content": json.dumps(RESULT), "annotations": []}}]}
        with patch("forgetmenot.providers.request_json", return_value=answer), self.assertRaises(ProviderError):
            providers.openrouter_check(store, "test-model", "task")

    def test_chatgpt_request_uses_supported_subscription_fields(self):
        event = {"type": "response.completed", "response": {"status": "completed", "output": [{"type": "message", "content": [{
            "type": "output_text", "text": json.dumps(RESULT), "annotations": [{"type": "url_citation", "url": SOURCE}]}]}]}}
        with patch("forgetmenot.providers.chatgpt_account", return_value={"access_token": "test-access"}), patch("httpx.Client") as client:
            response = client.return_value.__enter__.return_value.stream.return_value.__enter__.return_value
            response.is_success = True
            response.iter_lines.return_value = ["data: " + json.dumps(event)]
            self.assertEqual(providers.chatgpt_check(unittest.mock.Mock(), "test-model", "task")["outcome"], "matched")
            body = client.return_value.__enter__.return_value.stream.call_args.kwargs["json"]
            self.assertIsInstance(body["input"], list)
            self.assertFalse(body["store"])
            self.assertTrue(body["stream"])
            self.assertNotIn("max_output_tokens", body)

    def test_gemini_uses_only_successful_search_tool_sources(self):
        events = [
            {"type": "tool_use", "tool_id": "1", "tool_name": "google_web_search"},
            {"type": "tool_result", "tool_id": "1", "status": "success", "output": "Official listing: " + SOURCE},
            {"type": "message", "role": "assistant", "content": json.dumps(RESULT)},
            {"type": "result", "status": "success"},
        ]
        self.assertEqual(providers.parse_gemini_events(map(json.dumps, events))["outcome"], "matched")
        events[0]["tool_name"] = "read_file"
        self.assertEqual(providers.parse_gemini_events(map(json.dumps, events))["outcome"], "inconclusive")

    def test_gemini_launch_has_no_shell_or_secret_inheritance(self):
        store = unittest.mock.Mock()
        def launch(command, **kwargs):
            self.assertNotIn("--model", command)
            self.assertNotIn("FORGETMENOT_PASSWORD", kwargs["env"])
            self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
            self.assertFalse(kwargs.get("shell", False))
            settings = json.loads(Path(kwargs["env"]["GEMINI_CLI_SYSTEM_SETTINGS_PATH"]).read_text())
            self.assertEqual(settings["tools"]["core"], ["google_web_search"])
            self.assertFalse(settings["hooksConfig"]["enabled"])
            self.assertFalse(settings["admin"]["mcp"]["enabled"])
            return unittest.mock.Mock(returncode=0, stdout="")
        with patch("shutil.which", return_value="/usr/bin/gemini"), patch("subprocess.run", side_effect=launch), self.assertRaises(ProviderError):
            providers.gemini_cli_check(store, "auto", "task")


class ApiAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app(self.temp.name, PASSWORD, "http://127.0.0.1:8787", start_worker=False)
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8787", headers={"X-ForgetMeNot": "1"})
        self.addCleanup(self.client.close)

    def login(self):
        response = self.client.post("/api/login", json={"password": PASSWORD})
        self.assertEqual(response.status_code, 200)
        return response

    def test_authentication_csrf_host_and_logout(self):
        self.assertEqual(self.client.get("/api/state").status_code, 401)
        response = self.login()
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertEqual(self.client.get("/api/state").status_code, 200)
        self.assertEqual(self.client.post("/api/watches", json={"goal": "a useful goal"}, headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.get("/api/state", headers={"Host": "evil.example"}).status_code, 400)
        self.assertEqual(self.client.post("/api/watches", content=b"x" * 17000).status_code, 413)
        self.client.post("/api/logout", json={})
        self.assertEqual(self.client.get("/api/state").status_code, 401)

    def test_login_rate_limit(self):
        for _ in range(10):
            self.client.post("/api/login", json={"password": "incorrect"})
        response = self.client.post("/api/login", json={"password": PASSWORD})
        self.assertIn("Too many", response.json()["error"])

    def test_api_validates_watch_and_persists_edits(self):
        self.login()
        for body in ({"goal": "a useful goal", "interval": 1}, {"goal": "a useful goal", "interval": True},
                     {"goal": "a useful goal", "channels": ["push"]}, {"goal": "     "}):
            self.assertGreaterEqual(self.client.post("/api/watches", json=body).status_code, 400)
        watch_id = self.client.post("/api/watches", json={"goal": "a useful goal", "interval": 60}).json()["id"]
        self.assertEqual(self.client.put("/api/watches/" + watch_id, json={"goal": "a revised useful goal", "interval": None}).status_code, 200)
        watch = self.client.get("/api/state").json()["watches"][0]
        self.assertEqual(watch["goal"], "a revised useful goal")
        self.assertTrue(watch["automatic"])

    def test_api_never_returns_credentials(self):
        self.login()
        secret = "a-test-openrouter-secret"
        self.client.put("/api/settings", json={"openrouter_key": secret, "ntfy_token": "private-topic-token"})
        body = self.client.get("/api/state").text
        self.assertNotIn(secret, body)
        self.assertNotIn("private-topic-token", body)
        self.assertIn('"openrouter_connected":true', body)
        self.assertEqual(self.client.put("/api/settings", json={"claude_key": "sk-ant-oat01-test"}).status_code, 400)

    def test_oauth_pkce_state_expiry_and_replay(self):
        self.login()
        result = self.client.post("/api/oauth/openrouter/start", json={}).json()
        query = parse_qs(urlsplit(result["url"]).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertNotIn("code_verifier", query)
        pending = self.app.state.store.setting("oauth_openrouter")
        session = self.client.cookies.get("fmn_session")
        with self.assertRaises(ProviderError):
            self.app.state.auth.complete_oauth("openrouter", {"state": "wrong", "code": "test"}, session)
        with patch("forgetmenot.auth.request_json", return_value={"key": "test-connected-key"}):
            self.app.state.auth.complete_oauth("openrouter", {"state": pending["state"], "code": "test"}, session)
        with self.assertRaises(ProviderError):
            self.app.state.auth.complete_oauth("openrouter", {"state": pending["state"], "code": "test"}, session)
        self.assertEqual(self.app.state.store.setting("openrouter_key"), "test-connected-key")

    def test_chatgpt_signin_preserves_host_id_and_requires_loopback(self):
        self.login()
        first = self.client.post("/api/oauth/chatgpt/start", json={}).json()["url"]
        second = self.client.post("/api/oauth/chatgpt/start", json={}).json()["url"]
        a, b = parse_qs(urlsplit(first).query), parse_qs(urlsplit(second).query)
        self.assertEqual(a["ext_agent_host_id"], b["ext_agent_host_id"])
        self.assertNotEqual(a["state"], b["state"])
        self.assertEqual(a["redirect_uri"], ["http://127.0.0.1:8787/auth/callback"])
        with self.assertRaises(ProviderError):
            Auth(self.app.state.store, PASSWORD, "https://example.com").start_oauth("chatgpt", "session")

    def test_failed_chatgpt_exchange_retains_issued_registration_for_retry(self):
        self.login()
        self.client.post("/api/oauth/chatgpt/start", json={})
        pending = self.app.state.store.setting("oauth_chatgpt")
        with patch("forgetmenot.auth.request_json", side_effect=ProviderError("Code expired")), self.assertRaises(ProviderError):
            self.app.state.auth.complete_oauth("chatgpt", {"state": pending["state"], "code": "test-code", "client_id": "oaiapp_retry"}, self.client.cookies.get("fmn_session"))
        retry = self.client.post("/api/oauth/chatgpt/start", json={"account_id": "oaiapp_retry"}).json()["url"]
        query = parse_qs(urlsplit(retry).query)
        self.assertEqual(query["client_id"], ["oaiapp_retry"])
        self.assertNotIn("agent_name_hint", query)
        self.assertIsNone(self.app.state.store.setting("chatgpt_selected"))

    def test_frontend_and_security_headers(self):
        html = self.client.get("/")
        self.assertEqual(html.status_code, 200)
        self.assertIn("You can forget.", html.text)
        self.assertIn('id="mascot"', html.text)
        self.assertIn("frame-ancestors 'none'", html.headers["content-security-policy"])
        self.assertEqual(html.headers["referrer-policy"], "no-referrer")
        self.assertEqual(self.client.get("/static/app.js").status_code, 200)
        self.assertEqual(self.client.get("/static/mascot.js").status_code, 200)
        self.assertEqual(self.client.get("/static/lens.svg").status_code, 200)


class IdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.private.public_key()))
        cls.jwk.update(kid="test-key", alg="RS256", use="sig")

    def test_identity_requires_real_signature_issuer_audience_and_nonce(self):
        claims = {"iss": "https://auth.openai.com", "aud": "oaiapp_test", "sub": "subject", "exp": time.time()+300, "nonce": "right-nonce"}
        token = jwt.encode(claims, self.private, algorithm="RS256", headers={"kid": "test-key"})
        def discovery(method, url, **kwargs):
            return {"jwks_uri": "https://auth.openai.com/keys"} if "openid-configuration" in url else {"keys": [self.jwk]}
        with patch("forgetmenot.auth.request_json", side_effect=discovery):
            self.assertEqual(verify_identity(token, "oaiapp_test", "right-nonce")["sub"], "subject")
            for audience, nonce in (("wrong-client", "right-nonce"), ("oaiapp_test", "wrong-nonce")):
                with self.assertRaises(ProviderError):
                    verify_identity(token, audience, nonce)
            tampered = token[:-6] + "abcdef"
            with self.assertRaises(ProviderError):
                verify_identity(tampered, "oaiapp_test", "right-nonce")

    def test_refresh_rotates_and_persists_tokens_without_losing_account(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.save_setting("chatgpt_selected", "oaiapp_test")
            store.save_setting("chatgpt_accounts", {"oaiapp_test": {"client_id": "oaiapp_test", "subject": "subject",
                "access_token": "old-access", "refresh_token": "old-refresh", "expires_at": 0,
                "scopes": ["chatgpt.tokens.use.direct"]}})
            with patch("forgetmenot.providers.request_json", return_value={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}) as request:
                self.assertEqual(providers.chatgpt_account(store)["access_token"], "new-access")
                self.assertEqual(providers.chatgpt_account(store)["refresh_token"], "new-refresh")
            request.assert_called_once()
            saved = Store(directory).setting("chatgpt_accounts")["oaiapp_test"]
            self.assertEqual(saved["subject"], "subject")
            self.assertEqual(saved["refresh_token"], "new-refresh")


if __name__ == "__main__":
    unittest.main()
