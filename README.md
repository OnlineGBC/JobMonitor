# JobMonitor

Watches LinkedIn job search pages and emails you when new jobs appear.
Screenshots the page, compares against the baseline using a perceptual hash,
and (only if the visual fingerprint differs) extracts the new job listings and
sends a digest email.

## How it works

1. Loads the LinkedIn search URL in a headless Chromium (Playwright).
2. Takes a full-page screenshot.
3. Compares to the baseline:
   - Skips silently if the page shows "No matching jobs found".
   - Compares perceptual hashes (`imagehash`). `PHASH_THRESHOLD = 0` in
     `monitor.py` — any visual difference is treated as a change.
   - Falls back to Claude AI vision comparison if `imagehash` is not installed.
4. If changed, extracts the job listings, finds URLs not in the previous run,
   and emails a digest (subject + direct links). If no new URLs, no email.
5. Rotates the new screenshot into the baseline only after a successful send.

Session cookies are saved per monitor in `snapshots/<name>_linkedin_state.json`
so subsequent runs stay logged in. A newly added monitor has no session file, so
it seeds from the most recently saved one (all monitors use the same LinkedIn
account) and writes its own copy after the first successful run. This avoids a
full username/password login, which LinkedIn tends to answer with a 2FA/captcha
checkpoint.

## Setup

```bash
python -m venv JobMonitor.venv
JobMonitor.venv\Scripts\activate           # Windows
# source JobMonitor.venv/bin/activate      # Linux / Mac

pip install -r requirements.txt
playwright install chromium
```

Create a `.env` in the project root:

```env
# SMTP (required)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=you@gmail.com
SMTP_PASSWORD=app-password       # Gmail: use an App Password, not the account password
SMTP_USE_TLS=1
FROM_ADDR=you@gmail.com
TO_ADDRS=you@gmail.com,also@you.com

# LinkedIn (recommended — reduces rate limiting)
LINKEDIN_USERNAME=you@example.com
LINKEDIN_PASSWORD=your-linkedin-password

# Anthropic API (only needed if imagehash is unavailable)
# ANTHROPIC_API_KEY=sk-ant-...

# Optional webhook notifications
# SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
# DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

# Optional overrides
SUBJECT_PREFIX=[LinkedIn Jobs]
CONFIG_PATH=monitors.yaml
```

## Configuring monitors

Edit `monitors.yaml`. Only these fields are read:

| Field | Type | Notes |
|-------|------|-------|
| `name` | string | Unique identifier — used in filenames |
| `url` | string | The full LinkedIn job search URL |
| `headless` | bool | Run browser without a window. Set `false` for first login so you can solve captcha / 2FA |
| `enabled` | bool | Default `true`. When `false`, the scheduler and "Run All Once" skip this monitor. The per-card **Run** button and `python monitor.py --monitor NAME` still run it. |
| `to_addrs` | string or list | Optional. Who receives this monitor's emails. Omit to use the global `TO_ADDRS`. |

```yaml
monitors:
  - name: "RemoteUSA"
    url: "https://www.linkedin.com/jobs/search/?keywords=..."
    headless: true
    enabled: true

  - name: "Roopa Chief Strategy Innovation"
    url: "https://www.linkedin.com/jobs/search/?keywords=..."
    headless: true
    enabled: true
    to_addrs: "roopa@example.com, raja@onlinegbc.com"
```

### Who gets each email

Every email for a monitor — change detected, initial baseline, login failure —
goes to that monitor's `to_addrs`, or to the global `TO_ADDRS` when it sets none.
A monitor's recipients can also be set from the web UI on the monitor's edit page.

Two cases are called out explicitly so a misdirected alert is never silent:

- **No `to_addrs` configured** → the email goes to the global address and its
  body says it went there because that monitor has no recipients configured.
- **Delivery to a monitor's own recipients fails** → the global address gets a
  `DELIVERY FAILED` email naming the monitor, the intended recipients, the
  original subject, and the SMTP error. The baseline is not rotated, so the
  alert is retried on the next run until it gets through.

The global `TO_ADDRS` is still required — it is the fallback and the destination
for delivery-failure reports.

To build a URL: run the search on LinkedIn, then copy the address bar. LinkedIn
supports boolean filters in `keywords=` (e.g. `title:"VP" AND title:"AI"`,
`NOT ("Toptal" OR "Crossover")`).

## Running

### Web UI (recommended)

```bash
python web_monitor_menu.py            # default port 5000
python web_monitor_menu.py --port 8080
```

Open `http://localhost:5000`. Binds to localhost only. The UI covers monitor
CRUD, scheduler start/stop, per-monitor run-now, log viewer, and screenshot
gallery.

Background on Windows:

```powershell
$proc = Start-Process python -ArgumentList "web_monitor_menu.py" `
    -WindowStyle Hidden -RedirectStandardOutput "logs\web.log" `
    -RedirectStandardError "logs\web_error.log" -PassThru
$proc.Id | Out-File "data\web.pid"

# Stop
Stop-Process -Id (Get-Content "data\web.pid")
```

Background on Linux / Mac:

```bash
nohup python web_monitor_menu.py > logs/web.log 2>&1 &
```

### CLI

```bash
python monitor.py                          # one-shot run
python monitor.py --dry-run                # don't send notifications
python monitor.py --force-refresh          # clear baselines, rebaseline
python monitor.py --monitor "RemoteUSA"    # just one monitor

python run_monitor.py                      # scheduled loop (see below)
python monitor_menu.py                     # interactive CLI menu
```

### Scheduling

`run_monitor.py` runs a smart loop:

- Weekdays 8 AM – 8 PM ET → every 10–15 minutes
- Nights and weekends → every 115–125 minutes
- On login failure (exit 10) it retries once after ~10 minutes, then alerts and stops
- Transient email/webhook failures retry with exponential backoff (2s / 4s / 8s)

For persistent background operation, register it with Windows Task Scheduler
(Program: `JobMonitor.venv\Scripts\python.exe`, Arguments: `run_monitor.py`,
Start in: project root) or a `@reboot` cron entry.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Config error (couldn't read `monitors.yaml`) |
| 2 | Missing email/notification configuration |
| 3 | Missing `ANTHROPIC_API_KEY` (only when AI fallback is needed) |
| 4 | Screenshot capture failed |
| 5 | AI comparison failed |
| 10 | LinkedIn login failed (scheduling loop retries once, then stops) |

## File layout

```
monitor.py                Core: screenshot, phash compare, extract, notify
run_monitor.py            CLI scheduling loop with smart timing
monitor_menu.py           Interactive CLI menu
web_monitor_menu.py       Flask web UI (localhost:5000)
background_scheduler.py   Background thread scheduler used by the web UI
monitors.yaml             Monitor definitions
requirements.txt          Python dependencies
templates/                Jinja2 templates for the web UI
static/                   CSS + logo for the web UI
data/run_history.json     Structured run history (capped at 500 entries)
snapshots/                Per-monitor baseline PNGs, page text, and session state
logs/screen_compare.log   Rotated at 5MB, keeps 3 backups
```

## Troubleshooting

**Playwright errors** → `pip install playwright && playwright install chromium`

**SMTP auth failed** → Use an App Password (Gmail requires this). Verify
`SMTP_USE_TLS`. Check firewall for port 587.

**LinkedIn login fails** → Set `headless: false` on the monitor and rerun so
you can see the login page and solve any captcha. To force a fresh login you
must delete *every* `snapshots/*_linkedin_state.json`, not just this monitor's
— a monitor with no session of its own seeds from the newest remaining one.

**Too many false-positive emails** → Raise `PHASH_THRESHOLD` in `monitor.py`
(default `0` triggers on any visual difference). Values 2–3 absorb minor
pixel noise.

**No notifications** → Check `logs/screen_compare.log`. Verify `TO_ADDRS`.
If the LinkedIn page shows "No matching jobs found", the run is deliberately
silent.

**Debugging a run**:

```bash
python monitor.py --dry-run --monitor "RemoteUSA"
tail -f logs/screen_compare.log
```

## Notes

- Automated scraping may violate LinkedIn's Terms of Service. Use your own
  account for personal job monitoring at your own risk.
- Legacy YAML fields (`wait_selector`, `css_selector`, `structured_mode`,
  `remove_selectors`, `ignore_regexes`, `compare_mode`,
  `skip_if_page_text_matches`, etc.) are no longer accepted — LinkedIn
  selectors and the "no matching jobs" text check are hardcoded in
  `monitor.py`.
