$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot

try {
    Write-Output 'Starting Bechthold Industries services...'
    docker compose up -d --build
    if ($LASTEXITCODE -ne 0) { throw 'Docker Compose could not start the platform.' }

    $deadline = (Get-Date).AddMinutes(2)
    $apiReady = $false
    $n8nReady = $false
    while ((Get-Date) -lt $deadline -and (-not $apiReady -or -not $n8nReady)) {
        try {
            $apiReady = (Invoke-RestMethod -Uri 'http://localhost:8000/health/ready' -TimeoutSec 3).status -eq 'ready'
        }
        catch { $apiReady = $false }
        try {
            $n8nReady = (Invoke-WebRequest -Uri 'http://localhost:5678/healthz' -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200
        }
        catch { $n8nReady = $false }
        if (-not $apiReady -or -not $n8nReady) { Start-Sleep -Seconds 2 }
    }
    if (-not $apiReady) { throw 'API did not become ready within two minutes.' }
    if (-not $n8nReady) { throw 'n8n did not become ready within two minutes.' }

    Write-Output 'API and n8n are ready.'
    Write-Output 'This computer: http://localhost:8000'

    $lanAddresses = @()
    foreach ($adapter in [System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()) {
        if ($adapter.OperationalStatus -ne 'Up' -or $adapter.NetworkInterfaceType -notin @('Ethernet', 'Wireless80211')) { continue }
        if ($adapter.Name -match 'vEthernet|WSL|Docker|Virtual|VPN') { continue }
        foreach ($unicast in $adapter.GetIPProperties().UnicastAddresses) {
            $address = $unicast.Address
            if ($address.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork -or $address.IsLoopback) { continue }
            $bytes = $address.GetAddressBytes()
            $isPrivate = $bytes[0] -eq 10 -or
                ($bytes[0] -eq 192 -and $bytes[1] -eq 168) -or
                ($bytes[0] -eq 172 -and $bytes[1] -ge 16 -and $bytes[1] -le 31)
            if ($isPrivate) { $lanAddresses += $address.ToString() }
        }
    }

    foreach ($address in ($lanAddresses | Sort-Object -Unique)) {
        Write-Output "Same Wi-Fi on iPhone: http://${address}:8000"
    }
    if (-not $lanAddresses) {
        Write-Output 'No private Wi-Fi/Ethernet IPv4 address found. Check ipconfig and connect this computer to your home network.'
    }
    Write-Output 'Keep Docker Desktop running. No router port forwarding is needed.'
}
finally {
    Pop-Location
}
