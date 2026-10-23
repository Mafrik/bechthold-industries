$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot

try {
    docker compose stop
    if ($LASTEXITCODE -ne 0) { throw 'Docker Compose could not stop the platform.' }
    Write-Output 'Platform stopped. PostgreSQL and n8n data volumes were preserved.'
}
finally {
    Pop-Location
}
