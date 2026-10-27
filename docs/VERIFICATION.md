# Verification

Version: 0.1.0 developer preview. No live account authorization, inference or external notification was performed during creation.

Completed locally:

- 33 offline unittest checks: encrypted settings and durable watches; owner login, rate limiting and logout; CSRF/Host/body-size checks; watch validation and editing; concurrent claim exclusion, expired-lease recovery and stale-result rejection; one-shot matching and atomic outbox creation; persistent delivery retries; repeated-failure attention notices; a search-to-push pipeline with mocked transport; TLS-required SMTP construction; provider failures; required evidence provenance; rejection of malformed and truncated provider responses; OpenAI subscription request shape and terminal stream handling; real RSA-signed ID-token verification against fixture JWKS; OAuth PKCE/state/replay protection, issued-client retry handling and token rotation; Gemini search-only launch configuration and event parsing.
- Python syntax compilation and JavaScript syntax check.
- Local Chrome browser checks of unlocking, watch creation with an hourly interval, pause, history, account settings, and the responsive phone layout.
- The Python wheel builds and includes the frontend assets. Docker Compose configuration parses successfully.

Optical mascot redesign:

- 22 Node assertions cover listening, submission, waiting for a model, active checks, inconclusive checks, worker errors/offline, match notices, paused watches, Enter, Shift+Enter and IME composition.
- 12 isolated Chromium checks cover the complete typed-objective/Enter flow, a single real save, optical contraction during submission, editing/pause/resume, preserved drafts on rejected saves, API fixtures for worker/search/match states, connection loss/recovery, instance locking on phones, and the absence of uncaught JavaScript errors.
- Watchlist, settings and inbox were checked for horizontal overflow at 320, 390, 680, 768, 1024, 1280 and 1440 pixels. Reduced-motion emulation confirmed that animated breathing, scanning and focus transitions stop.
- Screenshots show the real local UI with no provider connected: [standing by](preview.jpg), [listening](listening.jpg), [objective saved](focused.jpg), and [phone layout](mobile.jpg). Searching/error/match branches were exercised with local API response fixtures, not paid provider requests.

The normal browser-control connection was unavailable. Visual and interaction verification used an isolated local headless Chromium profile, separate from any existing browser profile.

Not verified with live services: OpenRouter/ChatGPT OAuth consent and refresh/revocation; paid model/search compatibility; real Gemini CLI/Google subscription; SMTP transport; ntfy delivery. These require account setup and a small end-to-end check before using this app unattended.

Docker image/runtime was not verified: Docker CLI is installed, but no Docker daemon is running on this host. Multi-year operation has not been simulated or established by these checks.

GitHub Actions is not enabled: the publishing connection lacks the `workflow` scope, and GitHub rejected a push containing an active workflow. The same CI configuration is supplied as `docs/github-actions-tests.yml` for later installation with suitable permissions. Local checks passed independently.
