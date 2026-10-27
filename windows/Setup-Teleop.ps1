param([string]$ManusPythonPath = '')
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path $PSScriptRoot -Parent
Set-Location $ProjectRoot
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    py -3.11 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 x64 is required.' }
}
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
& $Python -m pip install -r windows\requirements-teleop.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
if ($ManusPythonPath) {
    & $Python -m pip install --editable $ManusPythonPath
    if ($LASTEXITCODE -ne 0) { throw 'MANUS SDK Python installation failed.' }
    & $Python -c "from manus_sdk import ManusSDK, lib, ffi; sdk = ManusSDK(); print('MANUS Python binding load OK')"
    if ($LASTEXITCODE -ne 0) { throw 'MANUS SDK import failed; run the official minimal example before teleoperation.' }
}
Write-Host 'Client dependencies ready. See windows/README.md for SSH and official SDK verification.'
