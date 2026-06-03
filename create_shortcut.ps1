# Run once to place a VoiceDictate shortcut on the Desktop.
# Right-click this file -> "Run with PowerShell"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$python     = Join-Path $projectDir "venv\Scripts\python.exe"
$vbsPath    = Join-Path $projectDir "launch.vbs"
$icoPath    = Join-Path $projectDir "icon.ico"
$desktop    = [Environment]::GetFolderPath("Desktop")
$lnkPath    = Join-Path $desktop "VoiceDictate.lnk"

# Generate icon.ico using the venv's Pillow
$tempPy = [System.IO.Path]::GetTempFileName() -replace '\.tmp$', '.py'
@"
from PIL import Image, ImageDraw
size = 64
img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.ellipse([2, 2, 61, 61], fill=(30, 140, 30))
fg = (255, 255, 255)
cx = 22
d.rounded_rectangle([cx - 7, 13, cx + 7, 34], radius=6, fill=fg)
d.arc([cx - 12, 26, cx + 12, 44], start=0, end=180, fill=fg, width=3)
d.line([cx, 44, cx, 52], fill=fg, width=3)
d.line([cx - 7, 52, cx + 7, 52], fill=fg, width=3)
for bx, half_h in ((42, 8), (48, 13), (54, 9)):
    d.line([bx, 32 - half_h, bx, 32 + half_h], fill=fg, width=3)
img.save(r'$icoPath')
"@ | Out-File -FilePath $tempPy -Encoding utf8

& $python $tempPy
Remove-Item $tempPy -ErrorAction SilentlyContinue

# Create the shortcut
$wsh = New-Object -ComObject WScript.Shell
$sc  = $wsh.CreateShortcut($lnkPath)
$sc.TargetPath       = "wscript.exe"
$sc.Arguments        = "`"$vbsPath`""
$sc.WorkingDirectory = $projectDir
$sc.Description      = "VoiceDictate - hold Ctrl+Alt to dictate"

if (Test-Path $icoPath) {
    $sc.IconLocation = "$icoPath,0"
} else {
    $sc.IconLocation = "$env:SystemRoot\System32\imageres.dll,109"
}
$sc.Save()

Write-Host "Done! Shortcut created at: $lnkPath"
Read-Host "Press Enter to close"
