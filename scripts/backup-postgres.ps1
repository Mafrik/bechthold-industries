param(
    [string]$OutputDirectory = (Join-Path $env:TEMP 'ai-market-platform-backups')
)

$ErrorActionPreference = 'Stop'
$resolvedOutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Path $resolvedOutputDirectory -Force | Out-Null

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$containerDump = "/tmp/aimp-postgres-$stamp.dump"
$backupPath = Join-Path $resolvedOutputDirectory "ai-market-platform-$stamp.dump"
Push-Location (Split-Path -Parent $PSScriptRoot)

try {
    docker compose exec -T postgres pg_dump -U trading -d trading -Fc -f $containerDump
    if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL backup creation failed.' }

    docker compose exec -T postgres pg_restore --list $containerDump *> $null
    if ($LASTEXITCODE -ne 0) { throw 'The generated PostgreSQL backup did not pass archive validation.' }

    docker compose cp "postgres:$containerDump" $backupPath
    if ($LASTEXITCODE -ne 0) { throw 'Could not copy the verified backup to the host.' }
    if (-not (Test-Path -LiteralPath $backupPath) -or (Get-Item -LiteralPath $backupPath).Length -eq 0) {
        throw 'The copied PostgreSQL backup is missing or empty.'
    }

    Write-Output "Verified PostgreSQL backup created: $backupPath"
}
finally {
    docker compose exec -T postgres rm -f $containerDump 2>$null | Out-Null
    Pop-Location
}
