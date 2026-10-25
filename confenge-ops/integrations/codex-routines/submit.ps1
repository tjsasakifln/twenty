param(
    [Parameter(Mandatory = $true)]
    [string]$InputPath,
    [string]$Outbox = "$env:USERPROFILE\.codex\codex-routines\outbox.sqlite3",
    [string]$SshHost = $env:CODEX_ROUTINES_SSH_HOST
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$transportPath = Join-Path $root 'transport.json'
if ([string]::IsNullOrWhiteSpace($SshHost) -and (Test-Path -LiteralPath $transportPath -PathType Leaf)) {
    $transport = Get-Content -LiteralPath $transportPath -Raw | ConvertFrom-Json
    $SshHost = [string]$transport.sshHost
}
if ([string]::IsNullOrWhiteSpace($SshHost)) {
    throw 'SSH host is required. Set CODEX_ROUTINES_SSH_HOST, pass -SshHost, or install private transport.json.'
}
$python = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    $python = (Get-Command python -ErrorAction Stop).Source
}

& $python (Join-Path $root 'cli.py') catalog-refresh --automations-dir (Join-Path $env:USERPROFILE '.codex\automations')
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
& $python (Join-Path $root 'cli.py') enqueue --input $InputPath --outbox $Outbox
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
& $python (Join-Path $root 'cli.py') flush --outbox $Outbox --apply --transport ssh --ssh-host $SshHost
exit $LASTEXITCODE
