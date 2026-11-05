# ONE COMMAND = the complete deployed-robot topology, simulated on this machine.
#
#   .\scripts\sim_full_up.ps1                 # boot everything (live kiosk mode)
#   .\scripts\sim_full_up.ps1 -Replay tests\fixtures\live_session.jsonl
#   .\scripts\sim_full_up.ps1 -SkipRos        # without the ROS 2 container
#   .\scripts\sim_full_up.ps1 -SkipFiware     # without the FIWARE stack
#   .\scripts\sim_full_up.ps1 -MotionOwner ros   # the ROS graph moves the robot
#   .\scripts\sim_full_up.ps1 -Hub            # the hub container instead of daemon + hostapp
#
# Boots exactly what the CM4's systemd units run, plus the dev-only MuJoCo sim:
#   1. Reachy daemon       --sim --no-media (the robot's motion stack; visible MuJoCo)
#   2. FIWARE stack        Orion-LD + QuantumLeap + TimescaleDB + Grafana (docker)
#   3. ROS 2 container     slice2 graph in LIVE mode (source:=pokamind reads the relay;
#                          observer behaviours + bridge writer off: hostapp is the
#                          single robot driver and FIWARE writer, as on the robot)
#   4. emori-hostapp       the kiosk hub on :$HostPort
# then optionally replays a recorded session through the whole stack (-Replay),
# or leaves the kiosk at http://localhost:$HostPort/ for a live Pokamind session (for a
# session against a Pokamind server you have a token for, see
# docs/guides/02-start-a-session.md).
param(
    [string]$Replay = "",
    [switch]$SkipRos,
    [switch]$SkipFiware,
    # Run the hub (the robot's host application as a ROS 2 node) in a container beside the
    # simulated robot node, in place of the Windows daemon and the Python hostapp. Needs
    # the hub image (emori/hub:robot); see deploy/hub/docker-compose.hub.sim.yml.
    [switch]$Hub,
    [int]$HostPort = 8095,  # 8080/8090 are often taken by Docker/WSL
    # Which lane may move the robot (EMORI_ROS_MOTION_OWNER). `hostapp` is what a robot
    # ships with and the default here too: the graph computes and publishes everything and
    # POSTs nothing. `ros` is the ROS-driven demonstration mode and needs the skill intake
    # open on the hostapp, a shared key on both sides, and the loopback hop below.
    [ValidateSet("hostapp", "ros", "none")]
    [string]$MotionOwner = "hostapp",
    # The /api/skill shared secret. Left empty a random one is generated ONCE per run
    # and printed, so a dev box never has a committed key and both sides always agree.
    [string]$SkillKey = "",
    [int]$ProxyPort = 8096  # the loopback hop for owner=ros; see the note in step 3a
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$py = Join-Path $root ".venv-reachy\Scripts\python.exe"
$daemonExe = Join-Path $root ".venv-reachy\Scripts\reachy-mini-daemon.exe"
if (-not (Test-Path $py)) { throw "venv missing - see docs/dev-setup.md" }

function Wait-Http($url, $name, $tries = 30) {
    for ($i = 0; $i -lt $tries; $i++) {
        try { Invoke-RestMethod -Uri $url -TimeoutSec 2 | Out-Null; Write-Host "  $name is up" -ForegroundColor Green; return $true }
        catch { Start-Sleep -Seconds 2 }
    }
    Write-Host "  WARN: $name not responding at $url" -ForegroundColor Yellow
    return $false
}

# --- 1. Reachy daemon (MuJoCo sim, motion-only like the CM4 daemon) ------------
$daemonUp = $false
try { Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/daemon/status" -TimeoutSec 2 | Out-Null; $daemonUp = $true } catch {}
if ($Hub) {
    # The robot node answers on :8000 in its container, so a daemon here would take the port.
    if ($daemonUp) { throw "-Hub: stop the Reachy daemon on :8000 first; the simulated robot node serves that port" }
    Write-Host "-Hub: the simulated robot node (not the Windows daemon) moves the robot" -ForegroundColor Cyan
}
elseif ($daemonUp) { Write-Host "Reachy daemon already running" -ForegroundColor Green }
else {
    Write-Host "Starting Reachy daemon (--sim --no-media, container-reachable)..." -ForegroundColor Cyan
    Start-Process -FilePath $daemonExe -ArgumentList @("--sim", "--no-media", "--fastapi-host", "0.0.0.0", "--no-localhost-only")
    Wait-Http "http://127.0.0.1:8000/api/daemon/status" "Reachy daemon" | Out-Null
}

# --- 2. FIWARE stack -------------------------------------------------------------
if (-not $SkipFiware) {
    Write-Host "Starting FIWARE stack (Orion-LD/QuantumLeap/TimescaleDB/Grafana)..." -ForegroundColor Cyan
    docker compose -f deploy/fiware/docker-compose.yml up -d
    Wait-Http "http://localhost:1026/version" "Orion-LD" | Out-Null
    Wait-Http "http://localhost:8668/version" "QuantumLeap" | Out-Null
}

# --- 3a. Who owns motion, and the loopback hop owner=ros needs --------------------
# ONE process env feeds both sides: docker compose substitutes ${EMORI_*} from it, and
# the hostapp below is started as a child of this shell. That is the whole point -- the
# two enforcement points (skill_facade only POSTs when it is `ros`, the hostapp answers
# 409 unless it is `ros`) must never be able to disagree about the same run.
$env:EMORI_ROS_MOTION_OWNER = $MotionOwner
$proxy = $null
if ($MotionOwner -eq "ros") {
    if (-not $SkillKey) {
        $bytes = New-Object byte[] 16
        [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        $SkillKey = -join ($bytes | ForEach-Object { $_.ToString("x2") })
    }
    $env:EMORI_SKILL_API = "1"
    $env:EMORI_SKILL_KEY = $SkillKey
    # /api/skill refuses anything that is not loopback, and Docker Desktop cannot
    # host-network: a container reaches the host through host.docker.internal, which is
    # exactly what the intake is built to turn away. Rather than widen the production
    # gate for the dev box, insert a hop -- the proxy listens off-box and forwards to
    # 127.0.0.1, so the hostapp genuinely sees a loopback peer. On the robot the
    # container IS host-networked and none of this exists.
    $env:EMORI_SKILL_URL = "http://host.docker.internal:$ProxyPort/api/skill"
    Write-Host "Starting loopback proxy :$ProxyPort -> 127.0.0.1:$HostPort..." -ForegroundColor Cyan
    $proxy = Start-Process -FilePath $py -PassThru -WorkingDirectory $root `
        -ArgumentList @("scripts/dev/loopback_proxy.py", "--listen", "$ProxyPort",
                        "--target", "$HostPort")
    # The proxy is a hop that makes private-network peers look like loopback to the
    # intake. It stops itself after 20 min idle, but a script that starts it must say
    # how to stop it NOW, and leave the pid where a teardown can find it.
    New-Item -ItemType Directory -Force (Join-Path $root "artifacts") | Out-Null
    Set-Content -Path (Join-Path $root "artifacts\sim_proxy.pid") -Value "$($proxy.Id)" -Encoding ascii
    Write-Host "  EMORI_SKILL_KEY=$SkillKey" -ForegroundColor Yellow
    Write-Host "  WARNING: dev loopback proxy running (pid $($proxy.Id)); it forwards any" -ForegroundColor Yellow
    Write-Host "  private-network peer to the hostapp as loopback. Stop it when done:" -ForegroundColor Yellow
    Write-Host "    Stop-Process -Id $($proxy.Id)      (pid also in artifacts\sim_proxy.pid)" -ForegroundColor Yellow
}
else {
    # `hostapp` and `none` never POST, so the intake stays shut: a robot that did not ask
    # for a motion intake must not grow one because a sim script was run on it.
    $env:EMORI_SKILL_API = "0"
    $env:EMORI_SKILL_KEY = ""
}

# --- 3. ROS 2 container in LIVE mode ----------------------------------------------
if ($Hub) {
    docker image inspect emori/hub:robot *> $null
    if ($LASTEXITCODE -ne 0) { throw "-Hub: no emori/hub:robot image here; build it first (deploy/hub/Dockerfile.hub-base, then Dockerfile.hub)" }
    Write-Host "Starting the simulated robot node and the hub (one network namespace)..." -ForegroundColor Cyan
    $env:HOSTAPP_PORT = "$HostPort"
    $env:EMORI_SOURCE = "pokamind"
    $env:EMORI_WS_URL = "ws://127.0.0.1:$HostPort/ws/relay"
    docker compose -f deploy/ros2/docker-compose.v2.yml -f deploy/hub/docker-compose.hub.sim.yml up -d --build
}
elseif (-not $SkipRos) {
    Write-Host "Starting ROS 2 slice (LIVE source from the hostapp relay)..." -ForegroundColor Cyan
    $env:EMORI_SOURCE = "pokamind"
    $env:EMORI_WS_URL = "ws://host.docker.internal:$HostPort/ws/relay"
    $env:FIWARE_BRIDGE_ENABLED = "0"    # hostapp is the FIWARE writer on the live stack
    docker compose -f deploy/ros2/docker-compose.yml up -d --build
}

# --- 4. hostapp (the hub: driver + relay + FIWARE + recorder + summary/email) -----
$hostUp = $false
try { Invoke-RestMethod -Uri "http://localhost:$HostPort/healthz" -TimeoutSec 2 | Out-Null; $hostUp = $true } catch {}
if ($Hub) { Wait-Http "http://localhost:$HostPort/healthz" "hub" 60 | Out-Null }
elseif ($hostUp) { Write-Host "hostapp already running on :$HostPort" -ForegroundColor Green }
else {
    Write-Host "Starting emori-hostapp on :$HostPort..." -ForegroundColor Cyan
    $env:HOSTAPP_PORT = "$HostPort"
    # Disable the physical head-tilt gesture sensor on the sim: the back-drivable MuJoCo
    # head drifts under gravity_compensation and the detector reads that as spurious
    # begin/end taps (ends live sessions the instant they open). On the real robot, omit this.
    $env:EMORI_GESTURE = "0"
    Start-Process -FilePath $py -ArgumentList @("-m", "emori_adapter.hostapp.server") -WorkingDirectory $root
    Wait-Http "http://localhost:$HostPort/healthz" "hostapp" | Out-Null
}

Write-Host ""
Write-Host "=== Full topology up ===" -ForegroundColor Green
Write-Host "  Motion owner: $MotionOwner  (EMORI_ROS_MOTION_OWNER, both sides)"
if ($MotionOwner -eq "ros") {
    Write-Host "                skill intake OPEN on the hostapp; the ROS graph moves the robot"
    Write-Host "  Loopback hop: http://host.docker.internal:$ProxyPort/api/skill -> 127.0.0.1:$HostPort"
    if ($proxy) { Write-Host "                proxy pid $($proxy.Id) (Stop-Process -Id $($proxy.Id) when done)" }
}
else {
    Write-Host "                the hostapp's own cue + engagement lanes drive; /api/skill is shut"
}
Write-Host "  Robot sim   : MuJoCo window (daemon http://127.0.0.1:8000)"
Write-Host "  Kiosk       : http://localhost:$HostPort/        (QR badge + voice menu)"
Write-Host "  Status      : http://localhost:$HostPort/api/status"
if (-not $SkipFiware) { Write-Host "  Grafana     : http://localhost:3003/d/emori-session (anonymous viewer)" }
if (-not $SkipRos)    { Write-Host "  ROS topics  : docker exec emori-ros2 bash -lc 'source ros2_ws/install/setup.bash && ros2 topic echo /humans/persons/learner_01/context'" }
Write-Host ""

# --- optional: deterministic replay through the whole stack ----------------------
if ($Replay) {
    Write-Host "Replaying $Replay through the full stack..." -ForegroundColor Cyan
    & $py scripts/replay_session.py $Replay --base-url "http://localhost:$HostPort"
    Write-Host ""
    Write-Host "Replay done. Check: the MuJoCo robot moved; /api/status counters;"
    Write-Host "artifacts/outbox/ has the results email; Grafana shows the session."
}
else {
    Write-Host "For a LIVE Pokamind session see docs/guides/02-start-a-session.md (then .\scripts\launch_kiosk_browser.ps1 -OpenOnly)"
    Write-Host "For a deterministic run    : .\scripts\sim_full_up.ps1 -Replay tests\fixtures\live_session.jsonl"
}
