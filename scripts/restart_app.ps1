# Clean restart for Voice Dictation.
# Kills every process running this project's code (stale copies included),
# relaunches hidden via launch.vbs, confirms the new PID, tails the log.

$ErrorActionPreference = 'Stop'
$proj = Split-Path -Parent $PSScriptRoot   # project root (scripts/ parent)

Write-Host "== Killing stale processes for $proj"
$killed = @()
Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -match '^pythonw?[\d.]*\.exe$' -and (
        $_.CommandLine -match [regex]::Escape("$proj") -or
        $_.ExecutablePath -like "$proj*" -or
        # Instances launched from a shell cd'd into the project have a fully
        # relative command line (".venv\Scripts\pythonw.exe" main.py) that no
        # path match can see — they hold the single-instance mutex and make
        # every restart a silent no-op. Slight cross-project risk accepted.
        $_.CommandLine -match '(^|[\s"])\.venv\\Scripts\\pythonw?\.exe.*(main|dashboard)\.py'
    )) -or
    ($_.Name -eq 'whisper-server.exe' -and $_.ExecutablePath -like "$proj*")
} | ForEach-Object {
    $killed += "$($_.ProcessId) $($_.Name)"
    Stop-Process -Id $_.ProcessId -Force -Confirm:$false -ErrorAction SilentlyContinue
}
if ($killed.Count) { $killed | ForEach-Object { Write-Host "   killed $_" } }
else { Write-Host "   none found" }

Start-Sleep -Seconds 2

Write-Host "== Launching via launch.vbs"
Start-Process -FilePath 'wscript.exe' -ArgumentList "`"$proj\launch.vbs`"" -WorkingDirectory $proj

Start-Sleep -Seconds 4

$new = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^pythonw?[\d.]*\.exe$' -and $_.CommandLine -match [regex]::Escape("$proj")
}
if (-not $new) {
    Write-Host "FAIL: no new process found. Last 30 log lines:"
    Get-Content "$proj\app.log" -Tail 30 -ErrorAction SilentlyContinue
    exit 1
}
$new | ForEach-Object {
    # Get-CimInstance already deserialises CreationDate to DateTime (unlike Get-WmiObject)
    Write-Host ("OK: PID {0} ({1}) started {2:HH:mm:ss}" -f $_.ProcessId, $_.Name, $_.CreationDate)
}

Write-Host "== Last 15 log lines:"
Get-Content "$proj\app.log" -Tail 15 -ErrorAction SilentlyContinue
