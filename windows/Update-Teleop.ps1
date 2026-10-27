# Run with the old control client closed. Preserve the existing .venv and installed MANUS SDK.
param(
    [string]$ServerAddress = $env:DEX_TELEOP_SSH_HOST,
    [string]$UserName = $env:DEX_TELEOP_SSH_USER,
    [string]$RemoteRoot = $env:DEX_TELEOP_REMOTE_ROOT,
    [ValidateRange(0,65535)][int]$SshPort = 0
)
$ErrorActionPreference='Stop'
# Accept environment settings used by earlier client bundles.
if ([string]::IsNullOrWhiteSpace($ServerAddress)) { $ServerAddress = $env:BENCH2DEX_SSH_HOST }
if ([string]::IsNullOrWhiteSpace($UserName)) { $UserName = $env:BENCH2DEX_SSH_USER }
if ([string]::IsNullOrWhiteSpace($RemoteRoot)) { $RemoteRoot = $env:BENCH2DEX_REMOTE_ROOT }
if ([string]::IsNullOrWhiteSpace($ServerAddress)) {
    throw 'Set -ServerAddress or DEX_TELEOP_SSH_HOST to user@server-ip, a hostname/IP, or an SSH host alias.'
}
if ([string]::IsNullOrWhiteSpace($RemoteRoot) -or -not $RemoteRoot.StartsWith('/')) {
    throw 'Set -RemoteRoot or DEX_TELEOP_REMOTE_ROOT to the absolute Linux workspace path.'
}
$Target = $ServerAddress
# An explicit user@host takes precedence over a separate/default user name.
if (-not $ServerAddress.Contains('@') -and -not [string]::IsNullOrWhiteSpace($UserName)) {
    $Target = "${UserName}@${ServerAddress}"
}
$RemoteRoot = $RemoteRoot.TrimEnd('/')
$ProjectRoot=Split-Path $PSScriptRoot -Parent
$Archive=Join-Path $env:TEMP 'dex_teleop_windows.zip'
$Arguments = @()
if ($SshPort -gt 0) { $Arguments += @('-P', "$SshPort") }
$Arguments += @("${Target}:${RemoteRoot}/dist/dex_teleop_windows.zip", $Archive)
& scp @Arguments
if($LASTEXITCODE -ne 0){throw 'Download failed'}
Expand-Archive -Path $Archive -DestinationPath $ProjectRoot -Force
Write-Host 'Updated. Existing .venv and SDK retained. Refresh the browser and run windows\Run-Teleop.ps1.'
