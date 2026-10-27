from email.message import EmailMessage
import json
import logging
import os
import smtplib
import ssl
import threading
import time

from . import providers


log = logging.getLogger("forgetmenot")


def smtp_ready():
    return bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_FROM"))


def send_delivery(store, delivery):
    sources = json.loads(delivery["evidence"])
    body = delivery["body"] + "\n\n" + "\n".join(item["title"] + ": " + item["url"] for item in sources)
    if delivery["channel"] == "email":
        destination = store.setting("email", "")
        if not smtp_ready() or not destination:
            raise providers.ProviderError("Configure SMTP on the server and an email address in Settings.")
        message = EmailMessage()
        message["From"], message["To"] = os.environ["SMTP_FROM"], destination
        message["Subject"] = "ForgetMeNot: " + delivery["title"]
        message["Message-ID"] = f'<forgetmenot-{delivery["id"]}@{store.host_id().split(":")[-1]}.local>'
        message.set_content(body)
        mode = os.getenv("SMTP_SECURITY", "starttls")
        if mode not in ("starttls", "tls"):
            raise providers.ProviderError("SMTP_SECURITY must be starttls or tls.")
        port = int(os.getenv("SMTP_PORT", "465" if mode == "tls" else "587"))
        context = ssl.create_default_context()
        with (smtplib.SMTP_SSL(os.environ["SMTP_HOST"], port, timeout=30, context=context) if mode == "tls"
              else smtplib.SMTP(os.environ["SMTP_HOST"], port, timeout=30)) as client:
            if mode == "starttls":
                client.starttls(context=context)
            if os.getenv("SMTP_USER"):
                client.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
            client.send_message(message)
    elif delivery["channel"] == "push":
        topic, token = store.setting("ntfy_topic", ""), store.setting("ntfy_token", "")
        if not topic:
            raise providers.ProviderError("Set a private ntfy topic in Settings.")
        headers = {"Authorization": "Bearer " + token} if token else {}
        providers.request_json("POST", "https://ntfy.sh", headers=headers, json={
            "topic": topic, "title": "ForgetMeNot: " + delivery["title"], "message": body,
        })
    else:
        raise providers.ProviderError("Unknown notification channel.")


class Worker:
    def __init__(self, store):
        self.store = store
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name="forgetmenot-worker", daemon=True)
        self.last_tick = None
        self.error = ""

    def tick(self):
        self.last_tick = time.time()
        # Deliveries remain independent of model access, including after a provider is disconnected.
        for _ in range(10):
            delivery = self.store.pending_delivery()
            if not delivery:
                break
            try:
                send_delivery(self.store, delivery)
                self.store.delivered(delivery)
            except Exception:
                self.store.delivered(delivery, "Delivery failed. Check channel settings and server connectivity; retry scheduled.")
        if not self.store.setting("model", {}).get("id"):
            return
        watch = self.store.claim()
        if not watch:
            return
        try:
            result, model = providers.check(self.store, watch)
        except Exception as exc:
            message = str(exc) if isinstance(exc, providers.ProviderError) else "An unexpected provider error occurred. The check will be retried."
            result = {"outcome": "inconclusive", "title": watch["title"], "approach": watch["approach"],
                      "summary": message, "interval": watch["interval"], "evidence": []}
            model = self.store.setting("model", {}).get("id", "unconfigured")
        self.store.finish(watch, result, model)

    def run(self):
        while not self.stop.is_set():
            try:
                self.tick()
                self.error = ""
            except Exception:
                # Never log provider responses, goals or credentials.
                log.error("Worker cycle failed; retrying in ten seconds.")
                self.error = "The worker encountered a storage or scheduling error. Check the server."
            self.stop.wait(10)
