# Clean restart for Voice Dictation.
# Kills every process running this project's code (stale copies included),
# relaunches hidden via launch.vbs, confirms the new PID, tails the log.

$ErrorActionPreference = 'Stop'
$proj = Split-Path -Parent $PSScriptRoot   # project root (scripts/ parent)

Write-Host "== Killing stale processes for $proj"
$killed = @()
Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match [regex]::Escape("$proj")) -or
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
    $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match [regex]::Escape("$proj")
}
if (-not $new) {
    Write-Host "FAIL: no new process found. Last 30 log lines:"
    Get-Content "$proj\app.log" -Tail 30 -ErrorAction SilentlyContinue
    exit 1
}
$new | ForEach-Object {
    $started = ([Management.ManagementDateTimeConverter]::ToDateTime($_.CreationDate))
    Write-Host ("OK: PID {0} ({1}) started {2:HH:mm:ss}" -f $_.ProcessId, $_.Name, $started)
}

Write-Host "== Last 15 log lines:"
Get-Content "$proj\app.log" -Tail 15 -ErrorAction SilentlyContinue
