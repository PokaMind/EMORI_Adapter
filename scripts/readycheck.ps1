# EMORI readiness self-check (dev box). Exit 0 = ready, 1 = a gate FAILed, 2 = --strict skip.
#   .\scripts\readycheck.ps1
#   .\scripts\readycheck.ps1 --strict
# Point it at a sim/remote stack with the same env the hostapp uses, e.g.
#   $env:HOSTAPP_PORT="8095"; $env:REACHY_BASE_URL="http://<robot-ip>:8000"  # e.g. 192.0.2.10
Set-Location (Split-Path $PSScriptRoot -Parent)
& .\.venv-reachy\Scripts\python.exe -m emori_adapter.hostapp.readycheck @args
exit $LASTEXITCODE
