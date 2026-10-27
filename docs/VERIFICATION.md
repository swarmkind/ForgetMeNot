# Verification

Version: 0.1.0 developer preview. No live account authorization, inference or external notification was performed during creation.

Completed locally:

- 33 offline unittest checks: encrypted settings and durable watches; owner login, rate limiting and logout; CSRF/Host/body-size checks; watch validation and editing; concurrent claim exclusion, expired-lease recovery and stale-result rejection; one-shot matching and atomic outbox creation; persistent delivery retries; repeated-failure attention notices; a search-to-push pipeline with mocked transport; TLS-required SMTP construction; provider failures; required evidence provenance; rejection of malformed and truncated provider responses; OpenAI subscription request shape and terminal stream handling; real RSA-signed ID-token verification against fixture JWKS; OAuth PKCE/state/replay protection, issued-client retry handling and token rotation; Gemini search-only launch configuration and event parsing.
- Python syntax compilation and JavaScript syntax check.
- Local Chrome browser checks of unlocking, watch creation with an hourly interval, pause, history, account settings, and the responsive phone layout.
- The Python wheel builds and includes all four frontend assets. Docker Compose configuration parses successfully.

Not verified with live services: OpenRouter/ChatGPT OAuth consent and refresh/revocation; paid model/search compatibility; real Gemini CLI/Google subscription; SMTP transport; ntfy delivery. These require account setup and a small end-to-end check before using this app unattended.

Docker image/runtime was not verified: Docker CLI is installed, but no Docker daemon is running on this host. Multi-year operation has not been simulated or established by these checks.

GitHub Actions is not enabled: the publishing connection lacks the `workflow` scope, and GitHub rejected a push containing an active workflow. The same CI configuration is supplied as `docs/github-actions-tests.yml` for later installation with suitable permissions. Local checks passed independently.
