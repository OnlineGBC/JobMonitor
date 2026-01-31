# =============================================================================
# LinkedIn Job Monitor - Background Job Runner
# =============================================================================
#
# WHAT THIS SCRIPT DOES:
#   Starts a background job that repeatedly runs monitor.py to check for new
#   LinkedIn job postings. It takes screenshots and uses AI to detect changes.
#
# HOW IT RUNS:
#   - Runs monitor.py in a loop
#   - Waits a random amount of time between runs to avoid detection
#   - Checks more frequently during business hours, less frequently at night
#
# TIMING:
#   - Weekdays (Mon-Fri) 8 AM to 8 PM Eastern: runs every 10-15 minutes
#   - All other times (nights and weekends): runs every 115-125 minutes
#
# STOPPING CONDITIONS:
#   - If monitor.py exits with any non-zero exit code, the job stops automatically
#   - An email alert is sent with a subject describing the specific error
#
# EXIT CODES:
#   1  - Configuration error (cannot read monitors.yaml or no monitors defined)
#   2  - Missing required email environment variables
#   3  - Missing ANTHROPIC_API_KEY (required for screenshot comparison)
#   4  - Screenshot capture failed
#   5  - AI comparison call to LLM failed
#   10 - LinkedIn login failed
#
# HOW TO USE:
#   1. Run this script in PowerShell: .\run_monitor_job.ps1
#   2. Check job status: Get-Job -Name JobMonitorLoop
#   3. View job output: Receive-Job -Name JobMonitorLoop
#   4. Stop the job: Stop-Job -Name JobMonitorLoop
#   5. Remove the job: Remove-Job -Name JobMonitorLoop
#
# =============================================================================

Start-Job -Name JobMonitorLoop -ScriptBlock {
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

    while ($true) {
        # Run the monitor script
        .\JobMonitor.venv\Scripts\python.exe .\monitor.py
        $exitCode = $LASTEXITCODE

        # If non-zero exit code, send alert email and stop the job
        if ($exitCode -ne 0) {
            $description = if ($exitCodeDescriptions.ContainsKey($exitCode)) {
                $exitCodeDescriptions[$exitCode]
            } else {
                "Unknown error"
            }

            Write-Output "Monitor failed with exit code $exitCode ($description) - stopping job"

            $etZone = [System.TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
            $etNow = [System.TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow, $etZone)

            $subject = "[JobMonitor] STOPPED: $description (exit code $exitCode)"
            $body = @"
The LinkedIn Job Monitor has stopped due to an error.

Exit Code: $exitCode
Error: $description
Timestamp: $($etNow.ToString('yyyy-MM-dd HH:mm:ss')) ET

Please check the job output for more details:
  Get-Job -Name JobMonitorLoop | Receive-Job

To restart the monitor:
  Remove-Job -Name JobMonitorLoop
  .\run_monitor_job.ps1
"@

            Send-AlertEmail -Subject $subject -Body $body
            break
        }

        # Figure out the current time in US Eastern Time
        $etZone = [System.TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
        $etNow = [System.TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow, $etZone)
        $hour = $etNow.Hour
        $dayOfWeek = $etNow.DayOfWeek

        # Determine if we're in "business hours" (Mon-Fri, 8 AM to 8 PM Eastern)
        $isWeekday = $dayOfWeek -ge [DayOfWeek]::Monday -and $dayOfWeek -le [DayOfWeek]::Friday
        $isBusinessHours = $hour -ge 8 -and $hour -lt 20

        # Set sleep interval based on time of day
        if ($isWeekday -and $isBusinessHours) {
            # During business hours: check frequently (every 10-15 minutes)
            $sleepSeconds = Get-Random -Minimum 600 -Maximum 901
        } else {
            # Outside business hours: check less often (every 115-125 minutes)
            $sleepSeconds = Get-Random -Minimum 6900 -Maximum 7501
        }

        Write-Output "[$($etNow.ToString('yyyy-MM-dd HH:mm:ss')) ET] Sleeping for $sleepSeconds seconds ($(($sleepSeconds / 60).ToString('F1')) minutes)"
        Start-Sleep -Seconds $sleepSeconds
    }
}

