param(
    [string]$Config = '',
    [ValidateSet('manus','mock','none')][string]$FingerSource,
    [ValidateSet('on','off','mock')][string]$Haptics,
    [string]$BrowserUrl = '',
    [string]$Endpoint = '',
    [switch]$ShowRtt
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path $PSScriptRoot -Parent
Set-Location $ProjectRoot
$ClientArgs = @('-m', 'dex_teleop.windows_client')
if ($Config) { $ClientArgs += @('--config', $Config) }
if ($FingerSource) { $ClientArgs += @('--finger-source', $FingerSource) }
if ($Endpoint) { $ClientArgs += @('--endpoint', $Endpoint) }
if ($BrowserUrl) { $ClientArgs += @('--browser-url', $BrowserUrl) }
if ($Haptics) { $ClientArgs += @('--haptics', $Haptics) }
if ($ShowRtt) { $ClientArgs += '--show-rtt' }
& '.\.venv\Scripts\python.exe' @ClientArgs
exit $LASTEXITCODE
