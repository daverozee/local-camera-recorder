param([switch]$OpenBrowser)
$ErrorActionPreference = 'Stop'
$address = 'http://127.0.0.1:8765'
$existing = $null
try { $existing = Invoke-RestMethod "$address/api/state" -TimeoutSec 2 } catch {}
if ($existing -and $existing.cameras -and $existing.token) {
    Write-Host "LocalCam is already running: $address"
    if ($OpenBrowser) { Start-Process $address }
    exit
}
$pythonExe = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { throw 'Run setup.ps1 first.' }
$nodeCommand = Get-Command node -ErrorAction SilentlyContinue
if ($nodeCommand) { $nodeExe = $nodeCommand.Source }
else {
    $nodeExe = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'
    if (-not (Test-Path -LiteralPath $nodeExe)) { throw 'Node.js 20 or newer is required.' }
}
$env:LOCALCAM_PYTHON = $pythonExe
$dataFolder = Join-Path $PSScriptRoot 'data'
New-Item -ItemType Directory -Force -Path $dataFolder | Out-Null
$nodeProcess = Start-Process -FilePath $nodeExe -ArgumentList @(('"' + (Join-Path $PSScriptRoot 'server.js') + '"')) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $dataFolder 'server.log') -RedirectStandardError (Join-Path $dataFolder 'server-error.log')
$ready = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Milliseconds 500
    try { $response = Invoke-RestMethod "$address/api/state" -TimeoutSec 2; $ready = $true; break } catch {}
    if ($nodeProcess.HasExited) { break }
}
if (-not $ready) { throw "LocalCam did not start. Check $dataFolder\server-error.log." }
Write-Host "LocalCam is running in the background: $address"
Write-Host 'Use stop.ps1 to stop it cleanly. Cameras start recording when you click Start recording.'
if ($OpenBrowser) { Start-Process $address }
