param(
    [Parameter(Mandatory = $true)]
    [string]$BackupPath,
    [string]$DatabaseName = ('aimp_restore_' + (Get-Date -Format 'yyyyMMddHHmmss'))
)

$ErrorActionPreference = 'Stop'
$resolvedBackupPath = [System.IO.Path]::GetFullPath($BackupPath)
if (-not (Test-Path -LiteralPath $resolvedBackupPath -PathType Leaf)) {
    throw 'Backup file was not found.'
}
if ($DatabaseName -notmatch '^[a-z][a-z0-9_]{2,62}$') {
    throw 'DatabaseName must contain 3-63 lowercase letters, digits, or underscores and start with a letter.'
}

Push-Location (Split-Path -Parent $PSScriptRoot)
$containerDump = $null
try {
    $existingDatabase = docker compose exec -T postgres psql -U trading -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname = '$DatabaseName'"
    if ($LASTEXITCODE -ne 0) { throw 'Could not check whether the destination database exists.' }
    if (($existingDatabase | Out-String).Trim() -eq '1') {
        throw "Database '$DatabaseName' already exists. Choose a new name; existing databases are never overwritten."
    }

    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $containerDump = "/tmp/aimp-restore-$stamp.dump"
    docker compose cp $resolvedBackupPath "postgres:$containerDump"
    if ($LASTEXITCODE -ne 0) { throw 'Could not copy the backup into the PostgreSQL container.' }

    docker compose exec -T postgres createdb -U trading -T template0 $DatabaseName
    if ($LASTEXITCODE -ne 0) { throw 'Could not create a new restore database.' }

    docker compose exec -T postgres pg_restore -U trading -d $DatabaseName $containerDump
    if ($LASTEXITCODE -ne 0) { throw "Restore failed. The separate database '$DatabaseName' was left intact for inspection; the active trading database was not changed." }

    Write-Output "Backup restored into new database '$DatabaseName'. The active trading database was not changed."
}
finally {
    if ($containerDump) { docker compose exec -T postgres rm -f $containerDump 2>$null | Out-Null }
    Pop-Location
}
