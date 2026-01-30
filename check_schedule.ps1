# PowerShell script to check if monitor runs every 10 minutes
# Extracts timestamps from monitor.log and calculates intervals

$logFile = "logs\monitor.log"

if (-not (Test-Path $logFile)) {
    Write-Host "Error: Log file not found at $logFile" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Analyzing monitor execution times from log file..." -ForegroundColor Cyan
Write-Host ""

# Extract all timestamps from "Fetching" lines (first monitor in each run)
$timestamps = Get-Content $logFile | 
Select-String -Pattern '\[INFO\] \[(RemoteUSA|HybridNYC)\] Fetching' | 
ForEach-Object {
    if ($_.Line -match '(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})') {
        [DateTime]::Parse($matches[1])
    }
}

if ($timestamps.Count -eq 0) {
    Write-Host "No execution timestamps found in log file." -ForegroundColor Yellow
    exit 0
}

Write-Host "Found $($timestamps.Count) execution timestamps" -ForegroundColor Green
Write-Host ""

# Show last 10 executions
Write-Host "Last 10 executions:" -ForegroundColor Yellow
$last10 = $timestamps | Select-Object -Last 10
$last10 | ForEach-Object {
    Write-Host "  $_" -ForegroundColor White
}

Write-Host ""
Write-Host "Intervals between executions:" -ForegroundColor Yellow

# Calculate intervals
$intervals = @()
for ($i = 1; $i -lt $timestamps.Count; $i++) {
    $interval = ($timestamps[$i] - $timestamps[$i - 1]).TotalMinutes
    $intervals += $interval
    $status = if ([math]::Abs($interval - 10) -lt 1) { "OK" } else { "X" }
    $color = if ([math]::Abs($interval - 10) -lt 1) { "Green" } else { "Red" }
    Write-Host "  $status $([math]::Round($interval, 1)) minutes between $($timestamps[$i-1].ToString('HH:mm:ss')) and $($timestamps[$i].ToString('HH:mm:ss'))" -ForegroundColor $color
}

# Statistics
Write-Host ""
Write-Host "Statistics:" -ForegroundColor Yellow
$avgInterval = ($intervals | Measure-Object -Average).Average
$minInterval = ($intervals | Measure-Object -Minimum).Minimum
$maxInterval = ($intervals | Measure-Object -Maximum).Maximum

Write-Host "  Average interval: $([math]::Round($avgInterval, 1)) minutes" -ForegroundColor White
Write-Host "  Minimum interval: $([math]::Round($minInterval, 1)) minutes" -ForegroundColor White
Write-Host "  Maximum interval: $([math]::Round($maxInterval, 1)) minutes" -ForegroundColor White

# Check if running every ~10 minutes
$withinRange = ($intervals | Where-Object { [math]::Abs($_ - 10) -lt 2 }).Count
$percentage = [math]::Round(($withinRange / $intervals.Count) * 100, 1)

Write-Host ""
Write-Host "  $withinRange out of $($intervals.Count) intervals are within 8-12 minutes ($($percentage)%)" -ForegroundColor $(if ($percentage -gt 80) { "Green" } else { "Yellow" })

if ($percentage -gt 80) {
    Write-Host ""
    Write-Host "[OK] Task appears to be running approximately every 10 minutes" -ForegroundColor Green
}
else {
    Write-Host ""
    Write-Host "[WARNING] Task may not be running consistently every 10 minutes" -ForegroundColor Red
    Write-Host "  Check Task Scheduler trigger settings" -ForegroundColor Yellow
}

Write-Host ""
