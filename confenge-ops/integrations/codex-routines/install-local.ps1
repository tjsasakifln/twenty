param(
    [string]$Destination = "$env:USERPROFILE\.codex\integrations\codex-routines",
    [Parameter(Mandatory = $true)]
    [string]$CatalogPath,
    [Parameter(Mandatory = $true)]
    [string]$SshHost
)

$ErrorActionPreference = 'Stop'
$source = Split-Path -Parent $MyInvocation.MyCommand.Path
$catalog = [System.IO.Path]::GetFullPath($CatalogPath)
if (-not (Test-Path -LiteralPath $catalog -PathType Leaf)) {
    throw "Private catalog not found: $catalog"
}
$parent = Split-Path -Parent $Destination
$staging = Join-Path $parent ('codex-routines.staging.' + [guid]::NewGuid().ToString('N'))

New-Item -ItemType Directory -Force -Path $parent | Out-Null
New-Item -ItemType Directory -Path $staging | Out-Null
try {
    Get-ChildItem -LiteralPath $source -File | Where-Object {
        $_.Extension -in '.py', '.ps1' -or $_.Name -in 'README.md', 'catalog.example.json'
    } | Copy-Item -Destination $staging
    Copy-Item -LiteralPath $catalog -Destination (Join-Path $staging 'catalog.json')
    @{ sshHost = $SshHost } | ConvertTo-Json -Compress | Set-Content -LiteralPath (Join-Path $staging 'transport.json') -Encoding utf8NoBOM
    $python = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        $python = (Get-Command python -ErrorAction Stop).Source
    }
    & $python (Join-Path $staging 'cli.py') catalog-sync | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'Installed client failed its catalog dry-run.'
    }
    if (Test-Path -LiteralPath $Destination) {
        $backup = $Destination + '.previous.' + (Get-Date -Format 'yyyyMMddHHmmss')
        Move-Item -LiteralPath $Destination -Destination $backup
    }
    Move-Item -LiteralPath $staging -Destination $Destination
    Write-Output $Destination
}
finally {
    if (Test-Path -LiteralPath $staging) {
        Remove-Item -LiteralPath $staging -Recurse -Force
    }
}
