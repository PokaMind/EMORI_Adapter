# One-stop EMORI launcher: ensures the ROBOT SIMULATION (Reachy daemon + MuJoCo viewer) and
# the EMORI host app are running, then opens a Chromium kiosk browser with camera + mic
# auto-granted and audio autoplay (so the screenless verbal flow works with no prompts).
#
#   .\scripts\launch_kiosk_browser.ps1                 # ensure sim+host app, open http://localhost:8095/
#   .\scripts\launch_kiosk_browser.ps1 -Kiosk          # fullscreen kiosk
#   .\scripts\launch_kiosk_browser.ps1 -FakeCamera     # synthetic camera (isolate webcam issues)
#
# Speech (verbal "say 1/2/3" + robot voice) is server-side (Piper /api/tts, Vosk /api/stt) when
# the models exist (scripts\get_voice_models.ps1); otherwise the pages fall back to browser
# Web Speech, which needs Google Chrome or Edge + internet. Prereq for a LIVE session:
# a robot token (secrets\pokamind_robot_token at the repository root, or
# POKAMIND_ROBOT_TOKEN for development), and POKAMIND_BASE only for a non-default server.
# Without a token the host app serves demo sessions only (docs/guides/02-start-a-session.md).
param(
  [string]$Url = "http://localhost:8095/",
  [switch]$Kiosk,                              # fullscreen, no chrome UI (robot-like)
  [switch]$FakeCamera,                         # synthetic camera instead of the real webcam
  [switch]$OpenOnly                            # skip daemon/hostapp startup; just open the browser
                                               # (skip startup when another launcher already started the stack)
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent

function Test-Up($u) { try { (Invoke-WebRequest -Uri $u -TimeoutSec 2 -UseBasicParsing) | Out-Null; $true } catch { $false } }

# --- 1) Ensure the ROBOT SIMULATION is running (Reachy daemon + MuJoCo viewer) -------------
if (-not $OpenOnly) {
$daemon = Join-Path $root ".venv-reachy\Scripts\reachy-mini-daemon.exe"
if (Test-Up "http://127.0.0.1:8000/api/daemon/status") {
  Write-Host "Robot simulation already running on :8000" -ForegroundColor DarkGray
} else {
  Write-Host "Starting robot simulation (--sim, MuJoCo viewer shown)..." -ForegroundColor Cyan
  # No --headless here so the sim window is visible; no --wireless-version on Windows.
  Start-Process -FilePath $daemon -ArgumentList "--sim","--no-media"
  for ($i=0; $i -lt 40 -and -not (Test-Up "http://127.0.0.1:8000/api/daemon/status"); $i++) { Start-Sleep -Milliseconds 500 }
  if (Test-Up "http://127.0.0.1:8000/api/daemon/status") { Write-Host "  sim up." -ForegroundColor Green }
  else { Write-Host "  WARNING: sim did not report ready on :8000 yet." -ForegroundColor Yellow }
}

# --- 2) Ensure the EMORI host app is running on :8095 -------------------------------------
$hostapp = Join-Path $root ".venv-reachy\Scripts\emori-hostapp.exe"
if (Test-Up "http://localhost:8095/healthz") {
  Write-Host "EMORI host app already running on :8095" -ForegroundColor DarkGray
} else {
  # The Pokamind connection comes from the caller's environment as it is (POKAMIND_BASE,
  # a robot token, EMORI_COURSE_ID): this script sets none of them and holds no secret.
  # Without a token the host app serves demo sessions only.
  if (-not $env:EMORI_LANG) { $env:EMORI_LANG = "en" }   # EMORI language, en by default
  $env:HOSTAPP_DEMO = "0"; $env:HOSTAPP_PORT = "8095"; $env:EMORI_RESULTS_FILE = "artifacts/last_results.json"
  if (-not $env:REACHY_BASE_URL) { $env:REACHY_BASE_URL = "http://127.0.0.1:8000" }  # honour a real-robot URL from the caller
  Write-Host "Starting EMORI host app on :8095 (lang=$($env:EMORI_LANG))..." -ForegroundColor Cyan
  Start-Process -FilePath $hostapp -WorkingDirectory $root -WindowStyle Minimized
  for ($i=0; $i -lt 40 -and -not (Test-Up "http://localhost:8095/healthz"); $i++) { Start-Sleep -Milliseconds 500 }
}
}  # end: if (-not $OpenOnly)

# --- 3) Open the kiosk browser (camera/mic auto-granted, autoplay on) ---------------------
$candidates = @(
  "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
  "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
  "$env:LocalAppData\Google\Chrome\Application\chrome.exe",
  "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
  "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
)
$browser = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $browser) { throw "No Chrome/Edge found. Install Google Chrome (needed for Web Speech)." }

$profileDir = Join-Path $env:TEMP "emori-kiosk-profile"
$flags = @(
  "--use-fake-ui-for-media-stream",            # auto-grant camera+mic (no prompt)
  "--autoplay-policy=no-user-gesture-required",# spoken prompt + AI voice play without a tap
  "--user-data-dir=$profileDir",
  "--new-window",
  "--disable-features=PreloadMediaEngagementData,AutoplayIgnoreWebAudio"
)
if ($FakeCamera) { $flags += "--use-fake-device-for-media-stream" }
if ($Kiosk)      { $flags += @("--kiosk", "--noerrdialogs", "--disable-infobars") }
$flags += $Url

$camDesc = if ($FakeCamera) { "synthetic" } else { "real webcam" }
Write-Host "Kiosk browser: $browser" -ForegroundColor Cyan
Write-Host "  URL : $Url" -ForegroundColor Cyan
Write-Host "  cam : $camDesc (auto-granted, no prompt)" -ForegroundColor Cyan
& $browser @flags
