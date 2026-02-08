# JobMonitor - LinkedIn Job Search Monitor

A sophisticated Python-based monitoring tool that tracks changes on LinkedIn job search pages using **AI-powered screenshot comparison** and sends email notifications when new jobs are posted. Perfect for staying ahead of the competition and never missing important job opportunities.

## 🎯 Overview

JobMonitor uses Claude AI's vision capabilities to intelligently compare screenshots of LinkedIn job search pages, detecting meaningful changes (new or removed jobs) while ignoring irrelevant changes like timestamps, applicant counts, and visual styling. This AI-powered approach is far more reliable than traditional text-based comparison methods.

### Key Highlights

- **AI-Powered Comparison**: Uses Claude Sonnet 4 vision model to intelligently detect meaningful job listing changes
- **Screenshot-Based**: Takes full-page screenshots for accurate visual comparison
- **Smart Detection**: Ignores timestamps, applicant counts, and UI changes - only detects actual job listing changes
- **LinkedIn Authentication**: Automatically handles LinkedIn login with session persistence
- **Multiple Monitors**: Track multiple job searches simultaneously
- **Flexible Scheduling**: Smart timing that checks more frequently during business hours
- **Multiple Notification Channels**: Email, Slack, and Discord webhooks
- **Windows-Friendly**: Designed to work seamlessly with Windows Task Scheduler
- **Interactive Menu**: User-friendly command-line interface for easy management
- **Web Management UI**: Browser-based dashboard for monitoring, configuration, and control

## 🚀 Quick Start

### Prerequisites

- Python 3.9 or higher
- Playwright (for browser automation)
- Anthropic API key (for Claude AI)
- SMTP credentials for email notifications (Gmail, Outlook, Brevo, etc.)
- LinkedIn account (optional but recommended for authenticated access)

### 1. Setup Environment

```bash
# Clone or download the project
cd JobMonitor

# Create virtual environment
python -m venv JobMonitor.venv

# Activate virtual environment
# Windows:
JobMonitor.venv\Scripts\activate
# Linux/Mac:
source JobMonitor.venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers (required for screenshots)
playwright install chromium
```

### 2. Get Your Anthropic API Key

1. Sign up for an account at [https://console.anthropic.com/](https://console.anthropic.com/)
2. Go to API Keys section
3. Create a new API key
4. Copy the key (it starts with `sk-ant-...`)

**Note**: Claude API usage is pay-as-you-go. Typical costs:
- Each screenshot comparison costs approximately $0.02-0.05
- Running every 15 minutes = ~4 comparisons/hour = ~$0.15-0.30 per day
- Most users spend $5-15 per month depending on monitoring frequency

### 3. Configure Environment Variables

Create a `.env` file in the project root:

```env
# ==============================================================================
# ANTHROPIC API KEY (REQUIRED)
# ==============================================================================
# Get your API key from: https://console.anthropic.com/
ANTHROPIC_API_KEY=sk-ant-api03-your-key-here

# ==============================================================================
# SMTP EMAIL CONFIGURATION (REQUIRED)
# ==============================================================================
# Choose one of the providers below and uncomment the relevant section

# --- Gmail ---
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your-email@gmail.com
SMTP_PASSWORD=your-app-password  # Use App Password, not regular password
SMTP_USE_TLS=1

# --- Outlook/Hotmail ---
#SMTP_HOST=smtp-mail.outlook.com
#SMTP_PORT=587
#SMTP_USERNAME=your-email@outlook.com
#SMTP_PASSWORD=your-password
#SMTP_USE_TLS=1

# --- Brevo (formerly Sendinblue) ---
#SMTP_HOST=smtp-relay.brevo.com
#SMTP_PORT=587
#SMTP_USERNAME=your-email@domain.com
#SMTP_PASSWORD=your-smtp-key
#SMTP_USE_TLS=1

# Email addresses
FROM_ADDR=your-email@gmail.com
TO_ADDRS=your-email@gmail.com,another@domain.com  # Comma-separated

# ==============================================================================
# LINKEDIN AUTHENTICATION (RECOMMENDED)
# ==============================================================================
# Provides access to more complete job listings and reduces rate limiting
# If not provided, the monitor will access LinkedIn as an unauthenticated user
LINKEDIN_USERNAME=your-linkedin-email@example.com
LINKEDIN_PASSWORD=your-linkedin-password

# ==============================================================================
# OPTIONAL WEBHOOK NOTIFICATIONS
# ==============================================================================
# Slack incoming webhook URL
#SLACK_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/WEBHOOK/URL

# Discord webhook URL
#DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR/WEBHOOK/URL

# ==============================================================================
# OPTIONAL SETTINGS
# ==============================================================================
# Email subject prefix
SUBJECT_PREFIX=[LinkedIn Jobs]

# Path to monitors configuration file
CONFIG_PATH=monitors.yaml
```

### 4. Configure Job Searches

Edit `monitors.yaml` to define what job searches to monitor:

```yaml
defaults:
  headless: true                     # Run browser in background (no window)
  wait_until: "domcontentloaded"     # Wait for page to load
  timeout_seconds: 180               # Page load timeout

monitors:
  - name: "RemoteUSA"
    url: "https://www.linkedin.com/jobs/search/?keywords=..."
    wait_selector: "ul.jobs-search__results-list"  # Wait for results to load
    css_selector: "ul.jobs-search__results-list"   # Focus on job results
    email_on_first_snapshot: false   # Don't email on first run
```

See [Configuration Guide](#-configuration-guide) below for detailed options.

### 5. Run the Monitor

**Web UI (Recommended):**

```bash
python web_monitor_menu.py
```

Open `http://localhost:5000` in your browser. The web dashboard lets you:
- View all monitors with status and screenshot thumbnails
- Start/stop the scheduler, run monitors on demand
- Edit monitors and settings directly in the browser
- View logs and screenshots
- Clear baselines and trigger fresh captures

See [Web Management UI](#-web-management-ui) below for details including background mode.

**Interactive CLI Menu (Alternative):**

```bash
python monitor_menu.py
```

This provides an interactive menu with options:
1. **Run once** - Execute the monitor one time (good for testing)
2. **Run as scheduled job** - Run continuously with smart timing
3. **Test custom schedule** - Run with a custom interval (5-59 minutes)
4. **Exit** - Exit the program

**Direct Execution:**

```bash
# Run once
python monitor.py

# Run with scheduling loop
python run_monitor.py

# Dry run (test without sending notifications)
python monitor.py --dry-run

# Force refresh (clear all baselines and start fresh)
python monitor.py --force-refresh

# Run only specific monitor
python monitor.py --monitor "RemoteUSA"
```

## 📋 How It Works

### First Run (Baseline Creation)

1. Opens LinkedIn job search page in browser
2. Logs in with your credentials (if provided)
3. Takes a full-page screenshot
4. Saves screenshot as baseline (`snapshots/<name>_screenshot1.png`)
5. Sends email notification with the initial screenshot

### Subsequent Runs (Change Detection)

1. Takes a new screenshot (`snapshots/<name>_screenshot2.png`)
2. **Fast Pre-Check**: Uses perceptual hashing to quickly compare images
   - If images are identical, skips expensive AI comparison
3. **AI Comparison**: If pre-check shows differences, uses Claude AI to analyze:
   - Are there NEW job listings?
   - Are there REMOVED job listings?
   - Has job content changed significantly?
   - Ignores: timestamps, applicant counts, visual styling, order changes
4. **If NO meaningful changes**: Discards new screenshot, keeps baseline
5. **If changes detected**: 
   - Sends email notification with new screenshot
   - Replaces baseline with new screenshot
   - Continues monitoring

### LinkedIn Authentication

- **First login**: Visible browser window opens (allows you to handle 2FA/captcha)
- **Session persistence**: Login cookies saved to `<name>_linkedin_state.json`
- **Subsequent runs**: Uses saved session in headless mode (no browser window)
- **Session expiration**: Automatically re-authenticates when needed
- **Fallback**: Continues with unauthenticated access if login fails

## 🔧 Configuration Guide

### monitors.yaml Structure

```yaml
defaults:
  # Browser settings
  headless: true                     # Run browser in background
  wait_until: "domcontentloaded"     # When to consider page loaded
                                     # Options: "load", "domcontentloaded", "networkidle"
  timeout_seconds: 180               # Request timeout in seconds

monitors:
  - name: "JobSearchName"            # Unique identifier (used in filenames)
    url: "https://..."               # LinkedIn job search URL
    
    # Selector options (optional but recommended for LinkedIn)
    wait_selector: "ul.jobs-search__results-list"  # Wait for this element
    css_selector: "ul.jobs-search__results-list"   # Focus comparison on this element
    
    # Behavior options
    email_on_first_snapshot: false   # Send email on first run (baseline creation)
    headless: true                   # Override default headless setting
    
    # Additional options (currently unused by screenshot comparison, kept for reference)
    # skip_if_page_text_matches:     # Skip notification if page text matches regex
    #   - '(?i)\bno\s+matching\s+jobs\s+found\b'
```

### Creating LinkedIn Job Search URLs

1. Go to [LinkedIn Jobs](https://www.linkedin.com/jobs/)
2. Enter your search criteria:
   - Keywords (e.g., "Director AI", "VP Technology")
   - Location (e.g., "United States", "New York, NY")
   - Date posted (e.g., "Past 24 hours")
   - Work type (Remote, Hybrid, On-site)
   - Experience level
   - Company size, industry, etc.
3. Click Search
4. Copy the full URL from your browser's address bar
5. Paste into `monitors.yaml`

**Pro Tip**: Use LinkedIn's advanced search operators in keywords:
- `title:"VP" AND title:"AI"` - Must have both in title
- `title:"Director" OR title:"VP"` - Either in title
- `NOT ("Crossover" OR "Toptal")` - Exclude specific companies

### Monitor Options Reference

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `name` | string | Required | Unique identifier for the monitor |
| `url` | string | Required | LinkedIn job search URL to monitor |
| `headless` | boolean | true | Run browser in background (no visible window) |
| `wait_until` | string | "domcontentloaded" | When to consider page loaded: "load", "domcontentloaded", "networkidle" |
| `wait_selector` | string | null | CSS selector to wait for before taking screenshot |
| `css_selector` | string | null | Focus comparison on this element only |
| `timeout_seconds` | integer | 180 | Maximum time to wait for page load |
| `email_on_first_snapshot` | boolean | true | Send email notification on first run |

## 📧 Email Provider Setup

### Gmail

1. Enable 2-Factor Authentication on your Google account
2. Go to [Google App Passwords](https://myaccount.google.com/apppasswords)
3. Create an app password for "Mail"
4. Use the 16-character password in `.env`:

```env
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your-email@gmail.com
SMTP_PASSWORD=abcd efgh ijkl mnop  # App password (spaces optional)
SMTP_USE_TLS=1
```

### Outlook/Hotmail

```env
SMTP_HOST=smtp-mail.outlook.com
SMTP_PORT=587
SMTP_USERNAME=your-email@outlook.com
SMTP_PASSWORD=your-password
SMTP_USE_TLS=1
```

### Brevo (formerly Sendinblue)

1. Sign up at [Brevo](https://www.brevo.com/)
2. Go to SMTP & API → SMTP
3. Create SMTP key
4. Use in `.env`:

```env
SMTP_HOST=smtp-relay.brevo.com
SMTP_PORT=587
SMTP_USERNAME=your-email@domain.com
SMTP_PASSWORD=your-smtp-key
SMTP_USE_TLS=1
```

### Testing Email Configuration

```bash
# Test with dry run (doesn't send emails)
python monitor.py --dry-run

# Test with real email send
python monitor.py --force-refresh
```

## 🔔 Webhook Notifications

### Slack

1. Create a Slack app and enable Incoming Webhooks
2. Add webhook URL to `.env`:

```env
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/WEBHOOK/URL
```

### Discord

1. Go to Server Settings → Integrations → Webhooks
2. Create a webhook and copy the URL
3. Add to `.env`:

```env
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR/WEBHOOK/URL
```

## 🌐 Web Management UI

The web UI provides a browser-based dashboard for managing all aspects of JobMonitor.

### Starting the Web UI

```bash
# Start on default port 5000
python web_monitor_menu.py

# Start on a custom port
python web_monitor_menu.py --port 8080
```

Then open `http://localhost:5000` in your browser.

### Running in the Background (PowerShell)

```powershell
# Start web_monitor_menu.py in the background (saves PID for later)
$proc = Start-Process python -ArgumentList "web_monitor_menu.py" -WindowStyle Hidden -RedirectStandardOutput "logs\web.log" -RedirectStandardError "logs\web_error.log" -PassThru
$proc.Id | Out-File "data\web.pid"
Write-Host "Started with PID: $($proc.Id)"
```

```powershell
# Check if it's still running
$pid = Get-Content "data\web.pid"
Get-Process -Id $pid -ErrorAction SilentlyContinue
```

```powershell
# Stop it using the saved PID
$pid = Get-Content "data\web.pid"
Stop-Process -Id $pid
Write-Host "Stopped PID: $pid"
```

### Running in the Background (Git Bash / Linux / Mac)

```bash
# Start in the background with output redirected to a log file
nohup python web_monitor_menu.py > logs/web.log 2>&1 &

# Check if it's running
ps aux | grep web_monitor_menu

# Stop it
kill $(ps aux | grep web_monitor_menu | grep -v grep | awk '{print $2}')
```

### Web UI Features

| Page | Description |
|------|-------------|
| **Dashboard** | Monitor cards with screenshot thumbnails, scheduler control panel, recent run history |
| **Monitors** | List all monitors, add/edit/delete with form-based YAML editing (preserves comments) |
| **Settings** | Edit `.env` variables grouped by category, passwords masked with toggle |
| **Logs** | View `screen_compare.log` with configurable line count, auto-refresh, download |
| **Screenshots** | Browse screenshots per monitor with file size and timestamp |

### Web UI Routes

| Action | How |
|--------|-----|
| Start scheduler | Dashboard → "Start" button |
| Stop scheduler | Dashboard → "Stop" button |
| Run a single monitor now | Dashboard → "Run" button on monitor card |
| Run all monitors now | Dashboard → "Run All Now" button |
| Clear a monitor's baseline | Dashboard → reset button on monitor card |
| Add a new monitor | Monitors → "Add Monitor" |
| Edit a monitor | Monitors → pencil icon |
| Delete a monitor | Monitors → trash icon (with confirmation) |
| Edit settings | Settings page → edit fields → "Save Settings" |

## ⏰ Automation & Scheduling

### Intelligent Scheduling (Recommended)

The `run_monitor.py` script includes smart scheduling that checks more frequently during business hours:

- **Weekdays 8 AM - 8 PM ET**: Every 10-15 minutes (randomized)
- **Nights and weekends**: Every 115-125 minutes (randomized)

This reduces API costs while ensuring you catch new jobs during peak posting times.

```bash
python run_monitor.py
```

Or use the interactive menu:

```bash
python monitor_menu.py
# Select option 2: "Run as scheduled job"
```

### Windows Task Scheduler

For running as a background service on Windows:

1. Open Task Scheduler (`taskschd.msc`)
2. Create Basic Task
3. **Name**: LinkedIn Job Monitor
4. **Trigger**: At startup (or at specific time)
5. **Action**: Start a program
   - **Program**: `C:\Users\YourName\Downloads\JobMonitor\JobMonitor.venv\Scripts\python.exe`
   - **Arguments**: `run_monitor.py`
   - **Start in**: `C:\Users\YourName\Downloads\JobMonitor`
6. **Settings**:
   - ✅ Allow task to be run on demand
   - ✅ Run task as soon as possible after a scheduled start is missed
   - ✅ If task fails, restart every 10 minutes
   - ✅ Attempt restart up to 3 times

**Run from Command Line:**

```bat
:: Run task immediately
schtasks /Run /TN "LinkedIn Job Monitor"

:: Check task status
schtasks /Query /TN "LinkedIn Job Monitor" /V /FO LIST
```

```powershell
# Run task immediately
Start-ScheduledTask -TaskName "LinkedIn Job Monitor"

# Check task status
Get-ScheduledTaskInfo -TaskName "LinkedIn Job Monitor"
```

### Linux/Mac Cron

```bash
# Edit crontab
crontab -e

# Run every 15 minutes
*/15 * * * * cd /path/to/JobMonitor && /path/to/JobMonitor/JobMonitor.venv/bin/python monitor.py

# Or use the scheduling loop (recommended)
@reboot cd /path/to/JobMonitor && /path/to/JobMonitor/JobMonitor.venv/bin/python run_monitor.py
```

## 📂 File Structure

```
JobMonitor/
├── monitor.py                 # Core monitoring engine (screenshot + AI comparison)
├── monitor_menu.py            # Interactive CLI menu interface
├── run_monitor.py             # CLI scheduling loop with smart timing
├── web_monitor_menu.py        # Web UI (Flask app — browser-based management)
├── web_run_monitor.py         # Web scheduler (background thread for the web UI)
├── monitors.yaml              # Monitor configurations
├── requirements.txt           # Python dependencies
├── .env                       # Environment variables (create this - see example above)
├── README.md                  # This file
├── templates/                 # Jinja2 HTML templates for web UI
│   ├── base.html              # Shared layout (navbar, Bootstrap CDN, flash messages)
│   ├── dashboard.html         # Dashboard with monitor cards and scheduler panel
│   ├── monitors.html          # Monitor list with CRUD actions
│   ├── monitor_edit.html      # Add/edit monitor form
│   ├── settings.html          # Environment variable editor
│   ├── logs.html              # Log viewer
│   └── screenshots.html       # Screenshot gallery per monitor
├── static/
│   └── style.css              # Custom styles for web UI
├── data/
│   ├── run_history.json       # Structured run history (auto-created, capped at 500)
│   └── web.pid                # Web UI process ID (when running in background)
├── snapshots/                 # Screenshot storage
│   ├── RemoteUSA_screenshot1.png       # Baseline screenshot
│   ├── RemoteUSA_screenshot1.txt       # Baseline page text
│   ├── RemoteUSA_screenshot2.png       # Temporary screenshot (deleted after comparison)
│   ├── RemoteUSA_screenshot2.txt       # Temporary page text (deleted after comparison)
│   ├── RemoteUSA_linkedin_state.json   # Saved LinkedIn session
│   ├── HybridNYC_screenshot1.png
│   ├── HybridNYC_screenshot1.txt
│   └── HybridNYC_linkedin_state.json
├── logs/                      # Log files
│   ├── screen_compare.log     # Main log file (rotated at 5MB, keeps 3 backups)
│   ├── web.log                # Web UI stdout (when running in background)
│   └── web_error.log          # Web UI stderr (when running in background)
└── JobMonitor.venv/           # Virtual environment
```

## 🎯 Command-Line Interface

### monitor.py (Main Script)

```bash
# Normal run
python monitor.py

# Dry run (test without sending notifications)
python monitor.py --dry-run

# Force refresh (clear all baselines and restart)
python monitor.py --force-refresh

# Run only specific monitor
python monitor.py --monitor "RemoteUSA"

# Combine options
python monitor.py --dry-run --monitor "RemoteUSA"
```

**Exit Codes:**
- `0` - Success
- `1` - Configuration error (cannot read monitors.yaml)
- `2` - Missing email/notification configuration
- `3` - Missing ANTHROPIC_API_KEY
- `4` - Screenshot capture failed
- `5` - AI comparison failed
- `10` - LinkedIn login failed (triggers job stop in automation)

### monitor_menu.py (Interactive Menu)

```bash
python monitor_menu.py
```

**Menu Options:**
1. **Run once** - Execute monitor one time, return to menu
2. **Run as scheduled job** - Run with intelligent scheduling (Ctrl+C to stop)
3. **Test custom schedule** - Run with custom interval (5-59 minutes)
4. **Exit** - Exit the program

### web_monitor_menu.py (Web UI)

```bash
# Start web UI on default port
python web_monitor_menu.py

# Start on custom port
python web_monitor_menu.py --port 8080
```

The web UI provides a browser-based alternative to the CLI with dashboard, monitor management, settings editor, log viewer, and scheduler control. See [Web Management UI](#-web-management-ui) for full details.

### run_monitor.py (Scheduling Loop)

```bash
# Run with default intelligent scheduling
python run_monitor.py

# Or call from Python with custom interval
from run_monitor import run_monitor_loop
run_monitor_loop(custom_interval_minutes=30)  # Every 30 minutes
```

**Features:**
- Intelligent timing (10-15 min during business hours, 115-125 min off-hours)
- Automatic error detection and email alerts
- Retry logic for transient failures
- Stops on persistent errors (with notification)

## 🔍 Troubleshooting

### Common Issues

#### 1. Playwright Not Installed

**Error**: `RuntimeError: Playwright not installed`

**Solution**:
```bash
pip install playwright
playwright install chromium
```

#### 2. Missing Anthropic API Key

**Error**: `ANTHROPIC_API_KEY environment variable not set`

**Solution**:
- Add `ANTHROPIC_API_KEY=sk-ant-...` to your `.env` file
- Get API key from [https://console.anthropic.com/](https://console.anthropic.com/)

#### 3. SMTP Authentication Failed

**Error**: `Error sending email: Authentication failed`

**Solutions**:
- **Gmail**: Use an App Password, not your regular password
- **Outlook**: Enable "Less secure app access" or use app password
- **Check credentials**: Verify username/password in `.env`
- **Check TLS setting**: Try toggling `SMTP_USE_TLS` between 0 and 1

#### 4. LinkedIn Blocking Requests

**Symptoms**: Pages not loading, timeouts, or "challenge" pages

**Solutions**:
- Add LinkedIn credentials to `.env`
- Wait longer between checks (LinkedIn rate limits)
- Check `logs/screen_compare.log` for details

#### 5. LinkedIn Login Fails

**Error**: `LinkedIn login failed`

**Solutions**:
- **Verify credentials**: Double-check username and password in `.env`
- **2FA enabled**: Temporarily disable 2FA or use session cookies
- **Security challenge**: Delete `<name>_linkedin_state.json` to force visible browser login
- **Captcha**: On first login with no saved cookies, a visible browser opens to let you solve captcha
- **Session expired**: Delete state file and rerun to re-authenticate

**Note**: After successful login, the session is saved and subsequent runs use headless mode.

#### 6. Too Many False Positives

**Symptoms**: Getting notifications even when jobs haven't changed

**Solutions**:
- Check `logs/screen_compare.log` for AI comparison details
- LinkedIn UI changes can sometimes trigger false positives
- Wait a few runs - AI learns patterns over time
- Use `--dry-run` to test without sending notifications

#### 7. No Notifications Received

**Checklist**:
- [ ] Check spam/junk folder
- [ ] Verify `TO_ADDRS` in `.env` is correct
- [ ] Run with `--dry-run` and check logs for errors
- [ ] Test SMTP credentials with a simple email send
- [ ] Check firewall/antivirus isn't blocking SMTP port 587

#### 8. High API Costs

**Symptoms**: Claude API bills higher than expected

**Solutions**:
- Reduce check frequency (use run_monitor.py's intelligent scheduling)
- Disable `email_on_first_snapshot: false` to avoid baseline emails
- Use perceptual hashing (automatically enabled) to skip AI calls when possible
- Monitor only essential job searches
- Check logs to see how often AI comparisons are triggered

### Debugging Steps

1. **Check Logs**: Always start with `logs/screen_compare.log`
   ```bash
   # View recent logs
   tail -50 logs/screen_compare.log
   
   # Watch logs in real-time
   tail -f logs/screen_compare.log
   ```

2. **Test with Dry Run**:
   ```bash
   python monitor.py --dry-run
   ```

3. **Test Single Monitor**:
   ```bash
   python monitor.py --monitor "RemoteUSA" --dry-run
   ```

4. **Force Refresh** (clear state and start fresh):
   ```bash
   python monitor.py --force-refresh
   ```

5. **Check Screenshots**: Look at saved screenshots in `snapshots/` folder

6. **Verify Dependencies**:
   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```

## 🔐 Security Best Practices

1. **Protect `.env` file**:
   - Never commit `.env` to version control
   - Set restrictive file permissions: `chmod 600 .env` (Linux/Mac)
   - Keep backups in secure location

2. **Use App Passwords**:
   - Gmail: Use App Passwords instead of account password
   - Enable 2FA on your accounts

3. **API Key Security**:
   - Keep Anthropic API key confidential
   - Rotate keys periodically
   - Monitor usage at [console.anthropic.com](https://console.anthropic.com/)

4. **LinkedIn Credentials**:
   - Consider using a dedicated LinkedIn account for automation
   - Monitor for suspicious login notifications
   - Saved sessions are stored in `*_linkedin_state.json` - protect these files

5. **Regular Updates**:
   ```bash
   pip install --upgrade -r requirements.txt
   playwright install chromium  # Update browser
   ```

## 📊 Understanding AI Comparison

### How Claude AI Analyzes Screenshots

The monitor sends both screenshots to Claude with specific instructions:

**What Claude Looks For:**
- ✅ New job listings that weren't present before
- ✅ Removed job listings that disappeared
- ✅ Significant content changes (job title, company, description)

**What Claude Ignores:**
- ❌ Timestamps ("posted 2 hours ago" → "posted 3 hours ago")
- ❌ Applicant counts (23 applicants → 25 applicants)
- ❌ View counts
- ❌ Order/position changes of the same jobs
- ❌ Visual differences (colors, fonts, spacing)
- ❌ UI elements (buttons, menus, ads)

### Perceptual Hash Pre-Check

Before calling Claude AI (which costs money), the monitor uses a fast perceptual hash comparison:

1. Generates a "fingerprint" of each image
2. Compares fingerprints (difference score)
3. If difference ≤ 10: Images are identical, skip AI call
4. If difference > 10: Images differ, use AI for detailed analysis

This can reduce AI calls by 50-70%, saving costs while maintaining accuracy.

### Cost Optimization

- **Perceptual hash** is free and instant
- **Claude API** only used when needed
- **Smart scheduling** reduces unnecessary checks
- **Session persistence** reduces page load time

## 🆘 Getting Help

1. **Check logs first**: `logs/screen_compare.log`
2. **Review this README**: Most issues are covered above
3. **Test configuration**: Use `--dry-run` flag
4. **Check dependencies**: Ensure all packages installed correctly

## 📝 Tips & Best Practices

### Optimizing Job Searches

1. **Use specific keywords**: More specific = fewer false positives
2. **Filter by date posted**: "Past 24 hours" ensures new jobs only
3. **Exclude spammy recruiters**: Use LinkedIn's NOT operator
4. **Multiple monitors**: Create separate monitors for different criteria

### Reducing Costs

1. **Smart scheduling**: Use `run_monitor.py` for intelligent timing
2. **Consolidate searches**: Fewer, broader searches vs. many narrow ones
3. **Business hours only**: Monitor when jobs are most likely posted
4. **Disable first snapshot emails**: Set `email_on_first_snapshot: false`

### Improving Reliability

1. **LinkedIn authentication**: Provides better access and fewer blocks
2. **Reasonable delays**: Don't check more than every 10 minutes
3. **Monitor logs**: Regular log reviews catch issues early
4. **Session persistence**: Lets saved cookies handle authentication

### Managing Multiple Job Searches

```yaml
monitors:
  - name: "Remote-Senior"
    url: "..."
    email_on_first_snapshot: false
    
  - name: "Remote-Director"
    url: "..."
    email_on_first_snapshot: false
    
  - name: "NYC-Hybrid"
    url: "..."
    email_on_first_snapshot: false
```

Each monitor runs independently and maintains its own baseline.

## 🚀 Advanced Usage

### Custom Scheduling

```python
from run_monitor import run_monitor_loop

# Check every 30 minutes
run_monitor_loop(custom_interval_minutes=30)
```

### Programmatic Integration

```python
from monitor import process_monitor, configure_logging, load_yaml
import os

configure_logging()

# Load config
cfg = load_yaml("monitors.yaml")
monitors = cfg.get("monitors", [])
defaults = cfg.get("defaults", {})

# Build email config
email_cfg = {
    "smtp_host": os.getenv("SMTP_HOST"),
    # ... other settings
}

# Process single monitor
exit_code = process_monitor(
    monitor=monitors[0],
    defaults=defaults,
    email_cfg=email_cfg,
    subject_prefix="[Jobs]",
    linkedin_username=os.getenv("LINKEDIN_USERNAME"),
    linkedin_password=os.getenv("LINKEDIN_PASSWORD"),
    dry_run=False
)
```

### Monitoring Non-LinkedIn Sites

While optimized for LinkedIn, the monitor can track any webpage:

```yaml
monitors:
  - name: "CompanyCareerPage"
    url: "https://company.com/careers"
    css_selector: ".job-listings"
    email_on_first_snapshot: true
```

The AI comparison works on any website with visual changes.

## 📈 System Requirements

- **Operating System**: Windows 10/11, Linux, macOS
- **Python**: 3.9 or higher
- **RAM**: 2GB minimum, 4GB recommended
- **Disk Space**: 500MB (for dependencies and screenshots)
- **Internet**: Stable connection required
- **Browser**: Chromium (installed via Playwright)

## 📦 Dependencies

- **anthropic**: Claude AI API client
- **playwright**: Browser automation for screenshots
- **pyyaml**: YAML configuration parsing
- **python-dotenv**: Environment variable management
- **PIL/Pillow**: Image processing (for resizing and perceptual hashing)
- **imagehash**: Perceptual hashing (for cost optimization)
- **tzdata**: Timezone support (Windows)
- **flask**: Web UI framework
- **ruamel.yaml**: YAML editing that preserves comments (used by web UI)

Install all dependencies with:
```bash
pip install -r requirements.txt
playwright install chromium
```

## 🔄 Version History & Updates

This tool is actively maintained. Check the repository for updates:

```bash
# Update dependencies
pip install --upgrade -r requirements.txt

# Update Playwright browser
playwright install chromium
```

## ⚖️ License

This project is provided as-is for educational and personal use. Use responsibly and in accordance with LinkedIn's Terms of Service.

**Important**: Automated scraping may violate LinkedIn's Terms of Service. This tool is intended for personal job search monitoring with your own account. Use at your own risk.

## 🤝 Contributing

Found a bug or have a feature request? Contributions are welcome:

1. Check logs and troubleshooting section first
2. Create detailed issue reports
3. Test changes thoroughly before submitting
4. Follow existing code style

## 💡 Use Cases

- **Active job seekers**: Never miss new opportunities in your field
- **Passive candidates**: Monitor dream jobs without daily manual checking
- **Recruiters**: Track competitor job postings and hiring trends
- **Career coaches**: Monitor job market trends for clients
- **Researchers**: Study job market dynamics and trends

## 🎓 Learning Resources

### LinkedIn Search Tips
- [LinkedIn Jobs Advanced Search](https://www.linkedin.com/help/linkedin/answer/a524335)
- [Boolean Search on LinkedIn](https://www.linkedin.com/help/linkedin/answer/a524047)

### Python Automation
- [Playwright Documentation](https://playwright.dev/python/)
- [Anthropic Claude API Docs](https://docs.anthropic.com/)

### SMTP & Email
- [Gmail App Passwords](https://support.google.com/accounts/answer/185833)
- [Python SMTP Tutorial](https://docs.python.org/3/library/smtplib.html)

---

**Happy Job Hunting! 🎯**

For issues, questions, or feedback, check the troubleshooting section or review the logs at `logs/screen_compare.log`.
