# Verification

Version 0.1.0 developer preview, checked 2026-10-27.

- **40 offline Python tests passed.** Includes authentication, CSRF/Host checks, encrypted settings, durable scheduling, concurrent leases, stale-result rejection, one-shot matches, retries, provider evidence validation, signed ID-token verification, OAuth protection/rotation, model pinning, database migration, private Codex credential copy, restricted CLI launch/event parsing, Telegram validation/chunking, and a real worker-to-adapter-to-outbox flow with controlled CLI/HTTP I/O.
- **24 Node assertions passed.** Lens listening/focus/search/error/offline/match states, per-watch model readiness, Enter, Shift+Enter and IME composition.
- **Three live ChatGPT/Codex checks ran through the real app API and worker.** One positive condition matched and produced one durable inbox notice; a negative condition stayed waiting without an alert. A further tick did not duplicate the notice. Both used the official page through the actual web tool. A third test discovered a co-op game listing via search, opened three page variants and returned inconclusive when the overall rating could not be verified, producing no false match. Test watches that could recur were paused. See [harness details](HARNESS.md) and [sanitized live results](live-harness.json).
- **Isolated Chromium verification passed.** Real Enter save exactly once, Shift+Enter/IME, saved model/frequency, optical contraction, edit restoration, rejected Telegram save retaining the draft, setup navigation, notification choices, frequency warning, live research history, server-driven eye states, locking and no uncaught JavaScript errors. Four views had no horizontal overflow at 320, 390, 680, 768, 1024, 1280 and 1440 pixels. Reduced-motion settings disable animation and transitions. [Check list](browser-checks.json).
- New screenshots show the actual simplified local app: [standing by](preview.jpg), [listening](listening.jpg), [focus acquired after a real save](focused.jpg), and [phone](mobile.jpg). These contain no credentials or account identifiers.
- Python compilation, JavaScript syntax and wheel asset inclusion were checked. Docker Compose configuration parses.

The normal browser-control connection was unavailable. Browser tests used an isolated headless Chromium profile, separate from existing browser profiles, as authorized.

Not live-tested: OpenRouter, direct app ChatGPT OAuth consent/refresh/revocation, Google/Gemini, Claude API, SMTP, Telegram and ntfy delivery. Notification transports are covered with controlled I/O; actual external destinations still need a send test. The reused Codex sign-in and live research were tested; CLI session renewal over time was not.

Docker runtime was not tested because no Docker daemon is running. Multi-year operation has not been simulated. AI source interpretation and search freshness remain limitations.

GitHub Actions is not enabled: the publishing connection lacks the `workflow` scope. [The CI template](github-actions-tests.yml) is provided for installation using a suitable connection. Local checks passed independently.
