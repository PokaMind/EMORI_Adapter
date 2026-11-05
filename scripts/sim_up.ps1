# Start the Reachy Mini MuJoCo simulator from the project venv.
#   .\scripts\sim_up.ps1            # Slice 1 (local only)
#   .\scripts\sim_up.ps1 -ForContainer   # Slice 2 (reachable from the ROS 2 container)
param([switch]$ForContainer, [switch]$NoMedia)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$daemon = ".\.venv-reachy\Scripts\reachy-mini-daemon.exe"
if (-not (Test-Path $daemon)) { throw "venv missing - see docs/dev-setup.md" }

$args = @("--sim")
if ($ForContainer) { $args += @("--fastapi-host", "0.0.0.0", "--no-localhost-only") }
if ($NoMedia)      { $args += "--no-media" }

Write-Host "Launching: reachy-mini-daemon $($args -join ' ')" -ForegroundColor Cyan
& $daemon @args
