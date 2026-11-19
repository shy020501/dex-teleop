param([ValidateRange(1,300)][int]$Seconds = 15)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path $PSScriptRoot -Parent
Set-Location $ProjectRoot
& '.\.venv\Scripts\python.exe' -m dex_teleop.devices.vive_probe --seconds $Seconds
exit $LASTEXITCODE
