# JobMonitor - Web Change Monitor

A Python-based web monitoring tool designed to track changes on LinkedIn job search pages and send email notifications when changes are detected. Perfect for monitoring job postings and staying updated on new opportunities.

## Features

- **Dual Fetch Modes**: Fast requests-based fetching or full JavaScript rendering with Playwright
- **Smart Filtering**: Remove noisy elements and ignore dynamic content before comparison
- **Email Notifications**: SMTP-based email alerts with detailed diffs
- **Flexible Configuration**: YAML-based monitor configuration
- **Windows-Friendly**: Designed to work with Windows Task Scheduler
- **Snapshot Storage**: Maintains historical snapshots for comparison

## Quick Start

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

# Install Playwright browsers (required for JS rendering)
playwright install chromium
```

### 2. Configure Email Settings

Create a `.env` file in the project root:

```env
# SMTP Configuration (use your email provider)
SMTP_HOST=smtp-relay.brevo.com
SMTP_PORT=587
SMTP_USERNAME=your-email@domain.com
SMTP_PASSWORD=your-app-password
SMTP_USE_TLS=1

# Email addresses
FROM_ADDR=your-email@domain.com
TO_ADDRS=your-email@domain.com,another@domain.com

# LinkedIn Authentication (optional - for authenticated access)
# If not provided, the monitor will access LinkedIn as an unauthenticated user
LINKEDIN_USERNAME=your-linkedin-email@example.com
LINKEDIN_PASSWORD=your-linkedin-password

# Optional settings
SUBJECT_PREFIX=[WebChange in LinkedIn]
CONFIG_PATH=monitors.yaml
```

**Note on LinkedIn Authentication:**
- **Visible browser is used only when cookies are not available** - this allows you to see and interact with security challenges during initial login
- Once cookies are saved, the monitor uses **headless mode** (hidden browser) for subsequent runs
- **Cookie/session persistence** is automatically enabled - after successful login, your session is saved to `linkedin_state.json`
- On subsequent runs, the monitor will reuse the saved session, avoiding frequent logins and reducing security challenges
- If the session expires and re-login is needed, it will attempt in headless mode; if you encounter challenges, delete `linkedin_state.json` and rerun to get visible browser mode

### 3. Run the Monitor

```bash
python monitor.py
```

## Configuration

### monitors.yaml

The `monitors.yaml` file defines what to monitor. Here's the structure:

```yaml
defaults:
  render_js: true                    # Use Playwright for JS rendering
  wait_until: "networkidle"          # Wait for network to settle
  compare_mode: "text"               # Compare visible text vs HTML
  normalize_whitespace: true         # Clean up whitespace
  timeout_seconds: 60                # Request timeout
  headers:                          # HTTP headers
    User-Agent: "Mozilla/5.0..."

monitors:
  - name: "RemoteUSA"
    url: "https://linkedin.com/jobs/search/..."
    css_selector: "ul.jobs-search__results-list"  # Focus on job results
    remove_selectors:               # Remove noisy elements
      - "time"                      # Timestamps
      - ".job-card-container__footer"
    ignore_regexes:                 # Ignore dynamic text patterns
      - "\\bJust now\\b|\\bminutes? ago\\b"
      - "\\b(views?|applicants?)\\b.*"
    email_on_first_snapshot: true
```

### Monitor Options

- **name**: Unique identifier for the monitor
- **url**: Target URL to monitor
- **css_selector**: CSS selector to focus on specific page elements
- **remove_selectors**: List of CSS selectors to remove before comparison
- **ignore_regexes**: Regex patterns to ignore in text comparison
- **render_js**: Enable JavaScript rendering (requires Playwright)
- **wait_until**: When to consider page loaded ("load", "domcontentloaded", "networkidle")
- **wait_selector**: Wait for specific element to appear
- **compare_mode**: "text" (visible text) or "html" (raw HTML)
- **normalize_whitespace**: Clean up whitespace differences
- **timeout_seconds**: Request timeout
- **email_on_first_snapshot**: Send email on first run

## Email Providers

### Gmail
```env
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your-email@gmail.com
SMTP_PASSWORD=your-app-password  # Use App Password, not regular password
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
```env
SMTP_HOST=smtp-relay.brevo.com
SMTP_PORT=587
SMTP_USERNAME=your-email@domain.com
SMTP_PASSWORD=your-smtp-key
SMTP_USE_TLS=1
```

## Automation

### Windows Task Scheduler

1. Open Task Scheduler
2. Create Basic Task
3. Set trigger (e.g., every 15 minutes)
4. Set action to start program: `python.exe`
5. Set arguments: `C:\path\to\JobMonitor\monitor.py`
6. Set start in: `C:\path\to\JobMonitor`
   #### Run or manage the task from the command line

You can run an existing Task Scheduler task on demand with `schtasks` or PowerShell:

```bat
:: Run an existing task immediately
schtasks /Run /TN "LinkedIn Job Monitor"

:: Query last run results
schtasks /Query /TN "LinkedIn Job Monitor" /V /FO LIST
```

```powershell
# Run an existing task immediately
Start-ScheduledTask -TaskName "LinkedIn Job Monitor"

# Check last run results
Get-ScheduledTaskInfo -TaskName "LinkedIn Job Monitor"
```


### Linux/Mac Cron

```bash
# Edit crontab
crontab -e

# Run every 15 minutes
*/15 * * * * cd /path/to/JobMonitor && python monitor.py
```

## File Structure

```
JobMonitor/
├── monitor.py              # Main monitoring script
├── monitors.yaml           # Monitor configurations
├── requirements.txt        # Python dependencies
├── .env                    # Email configuration (create this)
├── snapshots/              # Stored page snapshots
│   ├── RemoteUSA.txt
│   ├── RemoteUSA.sha256
│   ├── HybridNYC.txt
│   └── HybridNYC.sha256
├── logs/                   # Log files
│   └── monitor.log
└── JobMonitor.venv/        # Virtual environment
```

## Troubleshooting

### Common Issues

1. **Playwright not installed**: Run `playwright install chromium`
2. **SMTP authentication failed**: Check email credentials and app passwords
3. **LinkedIn blocking requests**: Use `render_js: true` and proper User-Agent
4. **LinkedIn login fails**: 
   - Ensure credentials are correct in `.env` file
   - If you have 2FA enabled, you may need to create an app-specific password or temporarily disable 2FA
   - LinkedIn may show captchas or security challenges - **visible browser is used only when cookies don't exist** (first login)
   - After successful login, your session is saved to `linkedin_state.json` and subsequent runs use headless mode
   - If session expires and re-login encounters challenges: delete `linkedin_state.json` and rerun to get visible browser
   - The monitor will continue with unauthenticated access if login fails
5. **Too many false positives**: Adjust `remove_selectors` and `ignore_regexes`

### Logs

Check `logs/monitor.log` for detailed information about monitoring activity and errors.

### Dependencies

- **requests**: HTTP requests
- **beautifulsoup4**: HTML parsing
- **pyyaml**: YAML configuration
- **python-dotenv**: Environment variables
- **playwright**: JavaScript rendering (optional)

## Security Notes

- Store sensitive information in `.env` file, not in code
- Use app passwords for email accounts when possible
- Keep your virtual environment secure
- Regularly update dependencies

## License

This project is provided as-is for educational and personal use.
