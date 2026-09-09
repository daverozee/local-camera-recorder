$ErrorActionPreference = 'Stop'
$address = 'http://127.0.0.1:8765'
$state = Invoke-RestMethod "$address/api/state" -TimeoutSec 5
Invoke-RestMethod "$address/api/shutdown" -Method Post -Headers @{'X-LocalCam-Token'=$state.token} -ContentType 'application/json' -Body '{}' | Out-Null
Write-Host 'Stopping LocalCam, stopping camera sweeps and finalizing clips. Allow up to 70 seconds.'
