param(
    [string]$ServerAddress = $env:DEX_TELEOP_SSH_HOST,
    [string]$UserName = $env:DEX_TELEOP_SSH_USER,
    [ValidateRange(0,65535)][int]$SshPort = 0,
    [int]$VideoPort = 18080,
    [int]$InputPort = 15557,
    [switch]$InputOnly
)
$ErrorActionPreference = 'Stop'
# Accept environment settings used by earlier client bundles.
if ([string]::IsNullOrWhiteSpace($ServerAddress)) { $ServerAddress = $env:BENCH2DEX_SSH_HOST }
if ([string]::IsNullOrWhiteSpace($UserName)) { $UserName = $env:BENCH2DEX_SSH_USER }
if ([string]::IsNullOrWhiteSpace($ServerAddress)) {
    throw 'Set -ServerAddress or DEX_TELEOP_SSH_HOST to user@server-ip, a hostname/IP, or an SSH host alias.'
}
$Target = $ServerAddress
# An explicit user@host takes precedence over a separate/default user name.
if (-not $ServerAddress.Contains('@') -and -not [string]::IsNullOrWhiteSpace($UserName)) {
    $Target = "${UserName}@${ServerAddress}"
}
$Arguments = @('-N','-o','ExitOnForwardFailure=yes','-L',"127.0.0.1:${InputPort}:127.0.0.1:5557")
if (-not $InputOnly) { $Arguments += @('-L',"127.0.0.1:${VideoPort}:127.0.0.1:8080") }
if ($SshPort -gt 0) { $Arguments += @('-p', "$SshPort") }
$Arguments += $Target
Write-Host "Keep this window open. Input endpoint: tcp://127.0.0.1:$InputPort"
if (-not $InputOnly) { Write-Host "Browser: http://127.0.0.1:$VideoPort" }
& ssh @Arguments
