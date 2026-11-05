# Guide 1 — Start the Reachy Mini simulation

This starts the **Reachy Mini MuJoCo simulator** on Windows — on its own, or as part
of the complete simulated robot topology. No Hugging Face download is needed — the
MuJoCo robot model ships inside the `reachy-mini` pip package.

## 0. One-time setup

> **Python 3.12 is mandatory.** The simulator pins `mujoco==3.3.0`, which has **no
> wheel for Python 3.14** (the system `python`). Always create the venv with 3.12.

```powershell
cd <repo root>
uv venv .venv-reachy --python 3.12
uv pip install --python .venv-reachy "reachy-mini[mujoco]"
uv pip install --python .venv-reachy -e ".[dev,hostapp,pokamind,speech,stt]"
.\scripts\get_voice_models.ps1          # Piper voices + Vosk models
```

## 1. One command, the whole robot (recommended)

```powershell
.\scripts\sim_full_up.ps1
```

This boots exactly what the deployed robot runs, plus the dev-only MuJoCo viewer:

1. the **Reachy daemon** (`--sim --no-media`, container-reachable) — the MuJoCo window;
2. the **FIWARE stack** (Orion-LD + QuantumLeap + TimescaleDB + Grafana, Docker);
3. the **ROS 2 container** in live mode (it reads the hostapp's `/ws/relay` as an
   observer — the hostapp stays the single robot driver and FIWARE writer);
4. the **EMORI hostapp** — the kiosk hub, on **:8095** on this dev box (the app's
   default is :8080, but 8080/8090 are often taken by Docker/WSL).

Options:

| Option | Effect |
|---|---|
| `-Replay tests\fixtures\live_session.jsonl` | replay a recorded session through the whole stack (deterministic, no Pokamind needed) |
| `-SkipRos` | skip the ROS 2 container |
| `-SkipFiware` | skip the FIWARE stack |
| `-HostPort <n>` | hostapp port (default 8095) |

When it's up: kiosk at `http://localhost:8095/`, live counters at
`http://localhost:8095/api/status`, Grafana at `http://localhost:3003/d/emori-session`.

## 2. Just the simulator

```powershell
.\scripts\sim_up.ps1                  # local only
.\scripts\sim_up.ps1 -ForContainer    # reachable from the ROS 2 container
.\scripts\sim_up.ps1 -NoMedia         # also disable camera/audio
```

(equivalent to calling `.\.venv-reachy\Scripts\reachy-mini-daemon.exe --sim` directly,
plus `--fastapi-host 0.0.0.0 --no-localhost-only` / `--no-media`)

A **MuJoCo viewer window** opens showing the robot. The daemon serves a REST +
WebSocket API at **http://127.0.0.1:8000** (interactive docs at
`http://127.0.0.1:8000/docs`, dashboard at `http://127.0.0.1:8000/`).

Useful daemon flags:
| Flag | Effect |
|---|---|
| `--sim` | MuJoCo physics + 3D viewer window |
| `--mockup-sim` | no-physics mock backend (no MuJoCo, no window) — fastest logic loop |
| `--headless` | run the sim without the GUI window |
| `--no-media` | disable camera/audio (avoids the "circular buffer overrun" warning; **disables backchannel sounds**) |
| `--scene minimal` | load a scene with a table + props instead of the empty scene |

## 3. Confirm it's up

```powershell
# REST status (200 + "state":"running"). The daemon has NO /health endpoint —
# the probe is /api/daemon/status.
Invoke-RestMethod http://127.0.0.1:8000/api/daemon/status

# Move the robot once from the SDK (a visible nod + antenna perk):
.\.venv-reachy\Scripts\python.exe scripts\smoke_daemon.py    # prints SMOKE_OK
```

> If `smoke_daemon.py` raises `HTTP 403` on `ws://localhost:8000/ws/sdk`, you
> launched with `--no-localhost-only` (which gates the SDK WebSocket; `sim_full_up.ps1`
> does this). That's fine — EMORI drives the robot over the **REST** API, which stays
> open. Use plain `--sim` (no extra flags) if you want the SDK script itself to connect.

## Notes
- Reachy Mini's expressive outputs are **6-DOF head + body yaw + 2 antennas +
  speaker** (and audio-reactive "wobbling"). There are **no LED eyes / eye-screen**
  — see [hardware-notes.md](../hardware-notes.md).
- Next: **[Guide 2 — Start a session](02-start-a-session.md)**.
