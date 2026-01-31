# =============================================================================
# LinkedIn Job Monitor - On-Demand Runner
# =============================================================================
#
# WHAT THIS SCRIPT DOES:
#   Runs monitor.py once to check for new LinkedIn job postings. It takes a
#   screenshot and uses AI to detect changes from the previous screenshot.
#
# HOW TO USE:
#   Run this script in PowerShell: .\run_monitor_ondemand.ps1
#
# EXIT CODES:
#   0  - Success
#   1  - Configuration error (cannot read monitors.yaml or no monitors defined)
#   2  - Missing required email environment variables
#   3  - Missing ANTHROPIC_API_KEY (required for screenshot comparison)
#   4  - Screenshot capture failed
#   5  - AI comparison call to LLM failed
#   10 - LinkedIn login failed
#
# =============================================================================

Set-Location "C:\Users\raja\Downloads\JobMonitor"

# Exit code descriptions
$exitCodeDescriptions = @{
    1  = "Configuration error (monitors.yaml)"
    2  = "Missing email environment variables"
    3  = "Missing ANTHROPIC_API_KEY"
    4  = "Screenshot capture failed"
    5  = "AI comparison call to LLM failed"
    10 = "LinkedIn login failed"
}

# Function to send email alert
function Send-AlertEmail {
    param (
        [string]$Subject,
        [string]$Body
    )

    $smtpHost = $env:SMTP_HOST
    $smtpPort = [int]$env:SMTP_PORT
    $smtpUsername = $env:SMTP_USERNAME
    $smtpPassword = $env:SMTP_PASSWORD
    $fromAddr = $env:FROM_ADDR
    $toAddrs = $env:TO_ADDRS -split ","

    if (-not $smtpHost -or -not $smtpUsername -or -not $fromAddr -or -not $toAddrs) {
        Write-Output "Cannot send email alert - missing SMTP environment variables"
        return
    }

    try {
        $securePassword = ConvertTo-SecureString $smtpPassword -AsPlainText -Force
        $credential = New-Object System.Management.Automation.PSCredential($smtpUsername, $securePassword)

        $mailParams = @{
            From       = $fromAddr
            To         = $toAddrs
            Subject    = $Subject
            Body       = $Body
            SmtpServer = $smtpHost
            Port       = $smtpPort
            Credential = $credential
            UseSsl     = $true
        }

        Send-MailMessage @mailParams
        Write-Output "Alert email sent: $Subject"
    }
    catch {
        Write-Output "Failed to send email alert: $_"
    }
}

# Run the monitor script
.\JobMonitor.venv\Scripts\python.exe .\monitor.py
$exitCode = $LASTEXITCODE

# If non-zero exit code, send alert email
if ($exitCode -ne 0) {
    $description = if ($exitCodeDescriptions.ContainsKey($exitCode)) {
        $exitCodeDescriptions[$exitCode]
    } else {
        "Unknown error"
    }

    Write-Output "Monitor failed with exit code $exitCode ($description)"

    $etZone = [System.TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
    $etNow = [System.TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow, $etZone)

    $subject = "[JobMonitor] FAILED: $description (exit code $exitCode)"
    $body = @"
The LinkedIn Job Monitor failed during an on-demand run.

Exit Code: $exitCode
Error: $description
Timestamp: $($etNow.ToString('yyyy-MM-dd HH:mm:ss')) ET

To retry:
  .\run_monitor_ondemand.ps1
"@

    Send-AlertEmail -Subject $subject -Body $body
    exit $exitCode
}

Write-Output "Monitor completed successfully"
exit 0
