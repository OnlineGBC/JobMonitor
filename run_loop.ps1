Start-Job -Name JobMonitorLoop -ScriptBlock {
    Set-Location "C:\Users\raja\Downloads\JobMonitor"
    while ($true) {
        .\JobMonitor.venv\Scripts\python.exe .\monitor.py
        Start-Sleep -Seconds 600
    }
}

