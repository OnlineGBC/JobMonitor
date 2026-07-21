# JobMonitor

Watches LinkedIn job search pages and emails you when new jobs appear.
Screenshots the page, compares against the baseline using a perceptual hash,
and (only if the visual fingerprint differs) extracts the new job listings and
sends a digest email.

Runs for one person out of the box. It also serves several: each gets their own
login, their own monitors, their own LinkedIn account, and their own schedule —
see [Setting it up for more than one person](#setting-it-up-for-more-than-one-person).

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

Session cookies are saved so subsequent runs stay logged in — see
[Whose LinkedIn account](#whose-linkedin-account-a-monitor-uses) for which
account a given monitor uses.

A monitor on the shared account keeps its cookies in
`snapshots/<name>_linkedin_state.json`. A newly added one has no session file,
so it seeds from the most recently saved shared file and writes its own copy
after the first successful run. This avoids a full username/password login,
which LinkedIn tends to answer with a 2FA/captcha checkpoint.

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

# Only when served through a tunnel or reverse proxy - see "Exposing the UI"
# PUBLIC_URL=https://jobmonitor.onlinegbc.com

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
| `owner` | string | Optional. Email of the account that can see and edit this monitor in the web UI. Omit and it is admin-only. |
| `interval_minutes` | int | Optional. How often this monitor runs. Omit for the shared business-hours / off-hours schedule. Floor is `SCHED_MIN_INTERVAL` (default 30), ceiling 1440. |

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

Each monitor carries its own next-due time, so one person's cadence does not
dictate anyone else's.

- A monitor with `interval_minutes` runs on that cadence, jittered ±10% so it is
  not machine-perfect
- A monitor without one uses the shared schedule: weekdays 8 AM – 8 PM ET every
  10–15 minutes, nights and weekends every 115–125 minutes
- Every monitor card and the dashboard's schedule summary show the current
  cadence. Click it to change it — the summary and the cards are editable in
  place, and the monitor's edit page carries the same field alongside everything
  else. "Default" is shown with the figure actually in force, so nobody has to
  remember what it stands for
- Users set their own interval on the monitor's edit page. The floor is
  `SCHED_MIN_INTERVAL` (default 30 minutes) — an admin setting, so nobody can
  schedule the shared LinkedIn account into a rate limit
- Starting the scheduler with a custom interval overrides every monitor's own

Runs stay **serialized and spaced at least 2 minutes apart** no matter how many
monitors come due at once. Users choose how often their monitor runs, not how
hard LinkedIn gets hit.

On login failure (exit 10) a monitor is rescheduled to retry in 10 minutes
rather than the loop blocking, so nobody's monitors wait out someone else's
retry. Transient email/webhook failures retry with exponential backoff
(2s / 4s / 8s).

Pausing a monitor clears its schedule, so re-enabling it makes it due
immediately rather than resuming an old countdown.

The scheduler starts automatically with the web UI. Set `SCHEDULER_AUTOSTART=0`
to leave it stopped until an admin starts it by hand. Because each monitor has
its own interval and its own enabled flag, whether the engine is running is not
something individual users need to think about — they pause their own monitors,
not the scheduler.

**Pause my monitors** on the dashboard stops automatic runs for everything the
signed-in user owns, leaving everyone else's alone. For an admin it also covers
monitors with no owner, which are admin-only anyway; it never touches a monitor
belonging to somebody else. Only an admin can stop the scheduler itself, since
that would halt everyone's monitors at once.

`run_monitor.py` (the CLI loop, unused if you drive things from the web UI) keeps
the original run-everything-then-sleep behaviour.

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
| 11 | Network unavailable — run skipped, no alert sent, retried at the monitor's next turn |

## File layout

```
monitor.py                Core: screenshot, phash compare, extract, notify
run_monitor.py            CLI scheduling loop with smart timing
monitor_menu.py           Interactive CLI menu
web_monitor_menu.py       Flask web UI (localhost:5000)
background_scheduler.py   Per-monitor scheduler used by the web UI
auth.py                   Accounts, one-time login codes, ownership rules
manage_users.py           CLI for creating and managing accounts
monitors.yaml             Monitor definitions
users.yaml                Accounts (gitignored). See users.example.yaml
requirements.txt          Python dependencies
templates/                Jinja2 templates for the web UI
static/                   CSS + logo for the web UI
data/run_history.json     Structured run history (capped at 500 entries)
snapshots/                Baselines, page text, and LinkedIn session state
  <name>_screenshot1.png    Per-monitor baseline
  <name>_linkedin_state.json    Shared-account session
  owner_<email>_linkedin_state.json    A user's own session
logs/screen_compare.log   Rotated at 5MB, keeps 3 backups
```

## Setting it up for more than one person

JobMonitor can serve several people from one machine: each has their own login,
sees only their own monitors, gets their own emails, runs on their own LinkedIn
account, and picks their own schedule. The sections below cover each piece in
detail — this is the order to do them in.

### Once, as the administrator

**1. Create the accounts.** There is no sign-up page.

```bash
python manage_users.py add you@example.com --role admin   # yourself, first
python manage_users.py add colleague@example.com          # everyone else
```

**2. Set the interval floor** in Settings → Scheduler, or in `.env`:

```
SCHED_MIN_INTERVAL=30
```

This is the fastest cadence any user may choose. It exists to protect the
LinkedIn account from rate limiting, so it is deliberately not user-editable.

**3. Give each monitor an owner.** Open the monitor's edit page and set **Owner**
to the person's email. The Owner field is visible to admins only. A monitor with
no owner stays admin-only, which is why existing monitors are not exposed to a
newly created account by accident.

**4. Make it reachable**, if people are not sitting at this machine — see
[Exposing the UI beyond localhost](#exposing-the-ui-beyond-localhost).

**5. Nothing to start.** The scheduler comes up with the web UI. If you do start
it by hand, leave the custom-interval box empty so each monitor uses its own
schedule — filling it in overrides everyone's.

### Then, each user

Send them the URL. They:

**1. Sign in.** Enter your email, receive a 6-digit code, enter it. No password.

**2. Add a LinkedIn session** at **LinkedIn** in the nav — paste the `li_at`
cookie from their own browser. Until they do, their searches run on the shared
account. Instructions are on that page.

**3. Set their schedule** on their monitor's edit page — **Check every N
minutes**, or blank for the default. They can also edit the URL, the recipients,
and pause the monitor.

They cannot see anyone else's monitors, Settings, the logs, or the scheduler
controls.

### What each person controls

| | Admin | User |
|---|---|---|
| Their own monitors: URL, recipients, schedule, pause, run | ✅ | ✅ |
| Pause/resume all of their own monitors at once | ✅ | ✅ |
| Their own LinkedIn session | ✅ | ✅ |
| Other people's monitors | ✅ | ❌ |
| Monitor ownership | ✅ | ❌ |
| Interval floor, Settings, Logs, start/stop scheduler | ✅ | ❌ |

## Whose LinkedIn account a monitor uses

By default every monitor signs in with the shared credentials in `.env`
(`LINKEDIN_USERNAME` / `LINKEDIN_PASSWORD`). That means other people's searches
run as you, appear in your activity, and any rate limiting lands on your account.

Each user can supply their own LinkedIn session instead, from **LinkedIn** in the
web UI nav. They paste their `li_at` cookie — copied from their own browser — and
from then on their monitors search as them.

| Monitor | Session used |
|---|---|
| Owner has supplied a session | `snapshots/owner_<email>_linkedin_state.json` — searches as them |
| Owner has supplied none | The shared account, exactly as before |
| No `owner` set | The shared account, exactly as before |

Nothing changes until someone adds a cookie, so there is no migration: each user
becomes independent the moment they supply one, and existing monitors keep
working untouched. A user's monitors all share one session, since a person has
one LinkedIn account.

**Passwords are never collected for other users** — only the session cookie. The
first login is the hard part of automating LinkedIn (it answers fresh logins with
a captcha or 2FA challenge, in a browser window on the server), and letting the
user log in normally in their own browser sidesteps it entirely.

Two things to know:

- The cookie is as powerful as a password — whoever holds it can act as that
  person on LinkedIn. It is stored on the server, never displayed back, and can
  be removed from the same page. Signing out of LinkedIn everywhere invalidates it.
- LinkedIn expires sessions, so this needs redoing occasionally. When a session
  goes stale that user's monitors fail to log in and alert them as usual.

Seeding never crosses users: a file named `owner_*` is excluded from the seed
search, so one person's cookies can never be handed to another person's monitor.

## Accounts

The web UI requires a login. There is no sign-up page — accounts are created
from the command line, so having shell access is the only way to grant one.

```bash
python manage_users.py add you@example.com --role admin   # do this first
python manage_users.py add colleague@example.com          # a regular user
python manage_users.py list
python manage_users.py delete colleague@example.com
```

**There are no passwords.** To sign in you enter your email and get a one-time
code sent to it, using the same SMTP settings the monitors use. Proving you can
read that inbox is the whole login — so the address must be a real one the
person can access.

Codes are 6 digits, expire in 10 minutes, and work once. Five wrong guesses
burns the code; five requests in 15 minutes locks the address out; and a second
code cannot be requested within 60 seconds of the first. Requesting a code for
an address with no account looks identical to requesting one for an address that
has one, so the form cannot be used to discover who has accounts.

Accounts live in `users.yaml` (gitignored) and hold only an email and a role.
See `users.example.yaml` for the shape.

| | `admin` | `user` |
|---|---|---|
| Monitors visible | all | only those whose `owner` is their email |
| Create / edit / delete monitors | any | only their own |
| Run a single monitor | any | only their own |
| Run All Once, start/stop scheduler, intervals | yes | no |
| Settings, Logs | yes | no |
| Reassign a monitor's `owner` | yes | no |

A monitor with no `owner` is **admin-only**, so monitors that predate accounts
are never exposed to a newly created user. Set the owner from the monitor's edit
page — the Owner field is shown to admins only.

Sessions are checked against `users.yaml` on every request, so deleting an
account or changing its role takes effect immediately rather than at next login.

`FLASK_SECRET_KEY` is generated into `.env` on first run. It signs session
cookies — if you delete it, everyone is logged out.

### Request forgery protection

Every state-changing request (anything that is not a GET) must carry a CSRF
token tied to the session. Without it, any page on the internet could make a
logged-in browser POST here — deleting monitors or rewriting settings — purely
because the session cookie rides along automatically.

Forms carry the token in a hidden field. JavaScript gets it from the
`<meta name="csrf-token">` tag in `base.html`, which wraps `fetch` once so every
call sends the `X-CSRFToken` header — a new `fetch` cannot be written without it.
The check runs in `before_request` ahead of the login check, so it applies to
every route including login, and a newly added POST route is protected without
anyone remembering to opt in.

### Exposing the UI beyond localhost

The app binds to `127.0.0.1` and is reachable only from the machine it runs on.
To let someone else reach it, put a tunnel in front rather than opening a port —
the app keeps listening only on localhost, and the tunnel makes the outbound
connection.

**1. Install cloudflared**

```powershell
winget install --id Cloudflare.cloudflared
```

**2. Try it with a throwaway URL first**

```powershell
cloudflared tunnel --url http://localhost:5000
```

This prints a random `https://<something>.trycloudflare.com` address that works
immediately, with no account. It changes every restart, so it is for testing —
but it proves the path works before you commit to a name.

**3. Tell the app it is behind a proxy**

Add the public address to `.env` and restart the web UI:

```
PUBLIC_URL=https://your-name.trycloudflare.com
```

This matters. Without it Flask sees a plain HTTP request from 127.0.0.1 and
will not mark the session cookie `Secure`, and every visitor is logged as
127.0.0.1. With it set, the app reads the `X-Forwarded-*` headers to recover the
real scheme and client address.

> Only set `PUBLIC_URL` when something really is proxying to the app, and never
> bind the app to `0.0.0.0` while it is set. Those headers are trivially forged
> by anyone who can reach the port directly.

**4. For a permanent address** — this deployment uses
`https://jobmonitor.onlinegbc.com`. Requires `onlinegbc.com` to be an active
zone in your Cloudflare account.

```powershell
# Opens a browser; pick the onlinegbc.com zone. Writes a cert to ~/.cloudflared
cloudflared tunnel login

# Creates the tunnel and its credentials file. Note the tunnel ID it prints
cloudflared tunnel create jobmonitor

# Points the hostname at the tunnel (creates the DNS record for you)
cloudflared tunnel route dns jobmonitor jobmonitor.onlinegbc.com
```

Then create `C:\Users\<you>\.cloudflared\config.yml`:

```yaml
tunnel: jobmonitor
credentials-file: C:\Users\<you>\.cloudflared\<TUNNEL-ID>.json

ingress:
  - hostname: jobmonitor.onlinegbc.com
    service: http://localhost:5000
  - service: http_status:404
```

Run it in the foreground to check it works:

```powershell
cloudflared tunnel run jobmonitor
```

Once it does, install it as a Windows service so it survives reboots and does
not need a terminal window open:

```powershell
# Run this in an elevated (Administrator) PowerShell
cloudflared service install
```

Finally set the address in `.env` and restart the web UI:

```
PUBLIC_URL=https://jobmonitor.onlinegbc.com
```

On restart the log shows either `Public mode: behind a proxy at ...` or
`Local mode: no PUBLIC_URL set`, so it is always clear which one is in force.

The other confirmation is in the login lines: with the proxy configured they
record the visitor's real address. If every login says `127.0.0.1` while the app
is being reached through the tunnel, `PUBLIC_URL` is not taking effect.

Local access on `http://127.0.0.1:5000` keeps working after this: browsers treat
localhost as a secure context, so `Secure` cookies are still sent.

**What protects it once it is public**

Sign-in is by emailed one-time code, every state-changing request needs a CSRF
token, and code requests are capped both per email address and per source
address (20 per 15 minutes) so someone cycling through addresses cannot keep the
form working. The scheduler, settings, and logs stay admin-only.

Cloudflare Access can be layered in front for a second gate, if you want the
tunnel to refuse strangers before a request ever reaches the app.

## Troubleshooting

**Playwright errors** → `pip install playwright && playwright install chromium`

**SMTP auth failed** → Use an App Password (Gmail requires this). Verify
`SMTP_USE_TLS`. Check firewall for port 587.

**LinkedIn login fails** → If the monitor's owner supplied their own session, it
has probably expired: they should paste a fresh `li_at` at **LinkedIn** in the
nav. Otherwise it is the shared account — set `headless: false` on the monitor
and rerun so you can see the login page and solve any captcha. To force a fresh
login on the shared account you must delete *every* shared
`snapshots/*_linkedin_state.json`, not just this monitor's, since a monitor with
no session seeds from the newest remaining one. `owner_*` files are never used
for seeding.

**Runs logged as "Network unavailable"** → This machine could not reach the
internet at that moment. The run is skipped and retried at the monitor's next
turn, no cookies are cleared and no login is attempted — a passing outage must
not provoke a LinkedIn captcha. No alert is sent either, since the mail server
is unreachable for the same reason. Repeated occurrences mean a real
connectivity problem, not a JobMonitor one.

**No sign-in code arrives** → The code goes out over the same SMTP config the
monitors use, so if email is broken nobody can log in. Check
`logs/screen_compare.log` for `Login code sent to ...` — if it is there, the send
succeeded and the problem is at the recipient end. Requesting a code for an
address with no account looks identical to one that has an account and sends
nothing, so confirm the account exists with `python manage_users.py list`.
Codes expire in 10 minutes, work once, and are refused within 60 seconds of the
previous one.

**A monitor is not running when expected** → Check the scheduler is started on
the dashboard. Runs are serialized and spaced at least 2 minutes apart, so a
monitor due at the same moment as others waits its turn. If the scheduler was
started with a custom interval, that overrides every monitor's own setting —
restart it with the box empty. An interval below `SCHED_MIN_INTERVAL` is
rejected when saving, not silently applied.

**A user cannot see their monitor** → Its `owner` is probably unset or set to a
different address. Owner is matched case-insensitively against the account
email. A monitor with no owner is visible to admins only.

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
