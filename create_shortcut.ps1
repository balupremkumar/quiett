# Run once to place a Quietype shortcut on the Desktop.
# Right-click this file -> "Run with PowerShell"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$python     = Join-Path $projectDir ".venv\Scripts\python.exe"
$vbsPath    = Join-Path $projectDir "launch.vbs"
$icoPath    = Join-Path $projectDir "icon.ico"
$desktop    = [Environment]::GetFolderPath("Desktop")
$lnkPath    = Join-Path $desktop "Quietype.lnk"

# Generate multi-resolution icon.ico from tray.export_ico (single source of truth)
$tempPy = [System.IO.Path]::GetTempFileName() -replace '\.tmp$', '.py'
@"
import sys
sys.path.insert(0, r'$projectDir')
import tray
tray.export_ico(r'$icoPath')
"@ | Out-File -FilePath $tempPy -Encoding utf8

& $python $tempPy
Remove-Item $tempPy -ErrorAction SilentlyContinue

# Create the shortcut
$wsh = New-Object -ComObject WScript.Shell
$sc  = $wsh.CreateShortcut($lnkPath)
$sc.TargetPath       = "wscript.exe"
$sc.Arguments        = "`"$vbsPath`""
$sc.WorkingDirectory = $projectDir
$sc.Description      = "Quietype - hold Ctrl+Alt to dictate"

if (Test-Path $icoPath) {
    $sc.IconLocation = "$icoPath,0"
} else {
    $sc.IconLocation = "$env:SystemRoot\System32\imageres.dll,109"
}
$sc.Save()

Write-Host "Done! Shortcut created at: $lnkPath"
Read-Host "Press Enter to close"
