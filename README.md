# ire-uptime — backup uptime monitor (IRE Digital Media LLC)

This repo is a **backup** to the main IRE monitor that runs on the box. If the box
(or its network) goes down, this GitHub Actions job keeps checking the public
sites and alerts via Telegram. It is not the primary monitor; the box monitor has
more checks (DNS, GA4, latency trends, weekly reports, etc.).

## What it checks (public URLs only)

| Check | Expectation |
|---|---|
| https://ireclaw.com, /pricing, /login, https://iredigitalmedia.com, https://irevideo.com | HTTP 200 + key text on the page, latency logged |
| ireclaw.com/api/health, irevideo.com/api/health | HTTP 200 |
| Stripe webhooks (unsigned POST) on ireclaw, iredigitalmedia, irevideo | 4xx (400/401/403), never 5xx |
| `/api/checkout/session` GET on the three sites | 405 (POST only) |
| TLS certificate of the three domains | alert when < 14 days left |
| agent.ireclaw.com | pending DNS: no alert until it resolves |

The ireclaw WhatsApp webhook check is intentionally left out for now (fix pending).

## How it works

- `.github/workflows/uptime.yml` runs every 5 minutes (`*/5 * * * *`; GitHub may
  delay scheduled runs) and on manual `workflow_dispatch`.
- `check.py` is Python stdlib only.
- State (`state/state.json`) is carried between runs with `actions/cache`
  (key `state-<run_id>`, restore-keys `state-`).
- Alerts (short, in Spanish) are sent only on a state change:
  - 🔴 down after 2 consecutive failures, 🟢 recovered when it comes back,
  - 🔐 TLS < 14 days (once), and when it's renewed.
- Unexpected-but-not-5xx codes on API endpoints are logged as `warn` (no alert),
  same as the box monitor.
- The job always passes; results are in the job log and the run summary.

## Telegram

Configure two repository **Actions secrets**: `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID`. If they are missing, the job still passes and logs
`telegram not configured`.

To test: *Actions → uptime → Run workflow* with `test_alert` checked; it sends
"Backup monitor IRE (GitHub Actions) activo ✅".
