# ROS 2 v2 on the Reachy Mini CM4: the robot as a node, with or without Docker

In v1 the robot was a FastAPI daemon and the ROS graph was an observer that asked the
hostapp, over HTTP, to move it. In v2 `reachy_mini_ros2` owns Pollen's `Backend` object
in-process: it drives the 9-servo bus itself, publishes `/joint_states` at 50 Hz and
`base_link -> head` on `/tf`, serves `GoTo` / `PlayRecordedMove` actions, and runs a REST
compatibility shim on `:8000` speaking the daemon's own dialect so the hostapp never
finds out.

That is a bigger change on a robot than it is in the simulator, because **the daemon has
to go**. `reachy-mini-daemon` and the robot node want the same `/dev/ttyAMA3` and the same
`:8000`. Two processes on one serial bus is not a degraded robot; it is a robot where the
motors belong to whichever one won the race, and it looks perfectly healthy from outside.

**The one-click deploy runs Route A for you.** v2 is what Deploy-to-Robot leaves a robot
on unless its `.env` says `EMORI_ROS2_STACK=v1`: `scripts/deploy_to_robot.py` builds and
ships the image below exactly as written here, and `v2-stack.sh` (in this directory) is
"Committed to v2" and "Rolling back to v1", run in order, with the fall back to v1 when the
node does not come up (`docs/deploy-a-new-robot.md`, step 6). The commands below are what
it does, and how to do any of it by hand.

Two routes here. Read "Status and known limits" before choosing either.

| | Docker (`deploy/ros2/docker-compose.v2.robot.yml`) | Native (this directory) |
|---|---|---|
| ROS 2 | Jazzy from the `emori/ros2-jazzy-v2` image | Jazzy from RoboStack (conda-forge) via pixi |
| Disk | ~1.4 GB image, built off the robot and pulled over SSH | ~1.5 GB conda env, resolved on the robot |
| Needs | `docker.io`, `docker-compose` (Debian's packages), device passthrough | `pixi` (user-level, no root), a C toolchain |
| Rollback | `docker compose down`, re-enable the daemon unit | `systemctl disable`, re-enable the daemon unit |
| Risk | lower: nothing outside the container changes | higher: the robot's own Python environment grows |
| Recommended | **yes, for a first robot** | for a robot where the Docker daemon is unwanted |

The Docker route is the zero-risk one and is what `docs/onrobot-runbook.md` points at
first. It is also the one that has run on robot hardware.

---

## Before either route: retire the daemon

```bash
sudo systemctl disable --now reachy-mini-daemon
# EMORI's --no-media drop-in must go too, or a later `systemctl enable` silently
# restores a second owner of the serial bus:
sudo rm -f /etc/systemd/system/reachy-mini-daemon.service.d/override.conf
sudo systemctl daemon-reload
```

Nothing hostapp-side changes. `REACHY_BASE_URL=http://127.0.0.1:8000` stays exactly as it
is; the shim answers there instead. The daemon also did the robot's Wi-Fi (the phone
admin's Network tab, and the hotspot it raises at boot when no known network is in
range) and its speaker/mic levels; the shim does neither, so `emori-admin` notices v2 on
its own and does that work itself (`docs/robot-admin.md`, "v1 and v2 robots"). The one-line check that you are running v2 and not v1:

```bash
curl -s localhost:8000/api/daemon/status | python3 -m json.tool | grep backend
#   "backend": "reachy_mini_ros2"      <- v2
#   (no `backend` field at all)        <- still Pollen's daemon
```

Pollen's daemon has no `backend` field, which is what makes this detectable at all. Both
`scripts/rehearse_motion_owner.py --robot native` and `scripts/ros2_v2_smoke.py` refuse to
start unless that field says `reachy_mini_ros2`.

## Route A — Docker, host networking

Two images, two tags, on purpose. `Dockerfile.v2` (`:latest`) is the **simulator**
build: MuJoCo, Mesa/EGL and the dev headers that compile them. `Dockerfile.v2.robot`
(`:robot`) is what the robot gets — the real backend never imports MuJoCo, and the
CM4's card has under 3 GB free.

```bash
# on your workstation: build, then serve it to the robot through the SSH tunnel
docker buildx build --platform linux/arm64 -f deploy/ros2/Dockerfile.v2.robot \
    -t emori/ros2-jazzy-v2:robot --load .
docker run -d -p 5000:5000 --name emori-reg registry:2          # once
docker tag  emori/ros2-jazzy-v2:robot 127.0.0.1:5000/emori/ros2-jazzy-v2:robot
docker push 127.0.0.1:5000/emori/ros2-jazzy-v2:robot
ssh -N -R 5000:localhost:5000 pollen@reachy-mini.local &        # the robot's 127.0.0.1:5000

# on the robot
docker pull 127.0.0.1:5000/emori/ros2-jazzy-v2:robot
docker tag  127.0.0.1:5000/emori/ros2-jazzy-v2:robot emori/ros2-jazzy-v2:robot
docker rmi  127.0.0.1:5000/emori/ros2-jazzy-v2:robot
```

**Not `docker save | ssh docker load`.** That is the obvious way and it does not fit
on this card: `docker load` stages the whole UNCOMPRESSED archive before importing,
so it wants about twice the image size free. On a robot with 2.7 GB free and zero
images, the load of a 1.67 GB image drains `/` to nothing and dies at `no space left on
device` partway through the pip layer. `docker pull` writes compressed blobs and
unpacks once, and re-ships skip every layer the robot already has. No robot
configuration is needed: docker trusts `127.0.0.1` registries by default, and the
reverse tunnel makes your workstation's registry exactly that.

**Stop v1's graph too, not just the daemon.** On a deployed robot the v1 graph is a
systemd unit of its own, and two graphs on one DDS domain is two publishers per topic
and two engagement lanes deciding what the robot does:

```bash
# on the robot
sudo systemctl stop emori-ros2          # v1 graph (docker-compose.robot.yml)
EMORI_V2_FENCE_FILE=/dev/null docker compose -f deploy/ros2/docker-compose.v2.robot.yml up -d
```

The container binds the fence file, so that Docker cannot bring it back once the fence is down.
There is no fence yet on a first try by hand, and `EMORI_V2_FENCE_FILE=/dev/null` says so. Leave
it out once the fence is up.

Compose reads its variables from the shell or from `/opt/emori/deploy/ros2/.env` next to
the compose file, never from `/opt/emori/.env`, and only at `up`. The motion owner stays at
the compose default, `hostapp`, which is also the hostapp's own default, so the two agree
with no line anywhere (why `hostapp`, and how to run `ros` instead: below). Put any
`EMORI_GAZE_*` tuning in `/opt/emori/deploy/ros2/.env`, so every later `up` carries it. An
edit takes effect only when the container is recreated (`docker compose ... up -d`).

Two ways to leave it. **On trial:** leave both units *enabled* and only stopped, and run
`docker compose -f deploy/ros2/docker-compose.v2.robot.yml down` before you reboot, so the
robot comes back on the proven v1 setup: a container made with `/dev/null` for the fence comes
back with Docker, beside the daemon. **Committed to v2**: disable both and fence
the daemon, so a reboot comes back on v2 alone and nothing can hand a second process
the serial bus:

```bash
sudo systemctl disable --now emori-ros2 reachy-mini-daemon
sudo mkdir -p /etc/systemd/system/reachy-mini-daemon.service.d
sudo cp deploy/robot/ros2_native/reachy-mini-daemon-v2-fence.conf \
     /etc/systemd/system/reachy-mini-daemon.service.d/zz-v2-fence.conf
sudo systemctl daemon-reload
sudo touch /opt/emori/V2_OWNS_THE_BUS       # the fence; `rm` hands the bus back
docker compose -f deploy/ros2/docker-compose.v2.robot.yml up -d   # now bound to the fence
```

`disable` alone is not a fence: `emori-camera-bridge.service` carries
`Wants=reachy-mini-daemon`, which starts a disabled unit happily, and `systemctl mask`
fails on the stock robot image because `/etc/systemd/system/reachy-mini-daemon.service` is a real
file rather than a symlink. The drop-in's `ConditionPathExists=!` is what works: a
failed Condition is a SKIPPED start, so the bridge's dependency is satisfied and the
serial port is never opened. Verified by asking systemd to start it anyway.

A robot committed to v2 needs no line in `/opt/emori/.env`. readycheck checks the v2
container whenever the fence is up (`EMORI_WITH_ROS2=1` from an earlier install still
works, and `0` does not switch the check off on a fenced robot), and the hostapp keeps its
default motion owner. A robot **on trial** has no fence, so there readycheck checks the
container only with `EMORI_WITH_ROS2=1` in `/opt/emori/.env`; without it the gate reads
`ros2_lane_off`. readycheck runs as `pollen` and asks docker directly, so `pollen` must be
in the `docker` group (`sudo usermod -aG docker pollen`, then log in again); otherwise the
gate SKIPs `docker_unreachable`:

```bash
bash deploy/robot/readycheck.sh          # expect: ros2 PASS ros2_up_v2
```

On the native route (Route B, below) there is no container: a fenced robot with
`reachy-mini-ros2.service` is judged by the shim on `:8000` (`ros2 PASS ros2_up_native`).

**`EMORI_ROS_MOTION_OWNER`: keep `hostapp` on v2.** Measured on a Reachy Mini
Wireless: with `ros` the robot stands still while it speaks, because the hostapp drops
every expression cue as `not_owner` and the ROS lane makes one gesture per sentence. With
`hostapp` the hostapp plans the speech motion as on v1, and look-at moves to the
`interaction` lane by itself (`EMORI_GAZE_LANE` empty = derived from the owner). `ros`
remains an option for motion that genuinely originates in the ROS graph. Then set it on
BOTH sides, or both lanes author motion into one queue and readycheck FAILs
`ros2_owner_drift`: in `/opt/emori/deploy/ros2/.env` (then `docker compose ... up -d`) and
in the hostapp's `.env`:

```bash
grep -q '^EMORI_ROS_MOTION_OWNER=' /opt/emori/.env \
  && sudo sed -i 's/^EMORI_ROS_MOTION_OWNER=.*/EMORI_ROS_MOTION_OWNER=ros/' /opt/emori/.env \
  || echo 'EMORI_ROS_MOTION_OWNER=ros' | sudo tee -a /opt/emori/.env
sudo systemctl restart emori-hostapp
```

The robot profile passes `/dev/ttyAMA3`, `/dev/video40` and `/dev/video41` through, joins
the host network, adds `dialout` and `video`, and is capped at **1536 MB / 2.0 CPU** —
higher than v1's 512 MB / 1.0, because this container carries the 50 Hz control loop that
the daemon carries under v1.

### Changing the node's Python without rebuilding the image

A one-click deploy keeps the image already on the robot, so a change to `arbiter.py`,
`rest_shim.py`, `state_snapshot.py`, `ros_surface.py` or `robot_node.py` does not reach the
running node on its own. The image takes 45 minutes to rebuild, so these five go in as one
small layer on top, built on the robot:

```bash
# on the robot, after a deploy has put the new files in /opt/emori
bash deploy/robot/ros2_native/motor-overlay.sh --apply     # build it, restart the node
bash deploy/robot/ros2_native/motor-overlay.sh --rollback  # back to the image underneath
```

The first run tags the image it found as `:robot-base`, and every later layer replaces the last
one on that base. Restarting the node parks and wakes the motors, so `--apply` and `--rollback`
refuse during a session.

A change to a message or service is different: it must be compiled against the robot's own
filesystem, which the robot does not do. Build the interface layer on the computer
(`scripts/build_hub_payload.py --layers motor --output motor-payload.tar`, which needs the
imported `emori/robot-buildenv` image and builds no image of its own), unpack the tar on the
robot, and pass its `motor/` folder:

```bash
bash deploy/robot/ros2_native/motor-overlay.sh --delta /path/to/motor --apply
```

The layer then holds that compiled install tree whole (`reachy_mini_msgs`, `reachy_mini_ros2`,
`emori_msgs` and the regenerated prefix scripts) in place of the five files above, and the
image carries a `org.pokamind.emori.v2-interfaces` label with a hash of it. It lands on the same
`:robot-base`, so `--rollback` undoes it too.

### The camera the node actually reads: `/dev/video41`

`/dev/video40` is the kiosk's. v4l2loopback is loaded `exclusive_caps=Y` and hands its
buffers to ONE capture consumer, which on a kiosk robot is Chromium; a second consumer
gets `VIDIOC_REQBUFS: Device or resource busy` (v4l2-ctl and GStreamer alike). So the
camera bridge **tees** a second loopback at 320×240 @ 10 fps and the node reads that.

Three pieces, and they are deliberately separate so a robot can stop at any of them:

```bash
# 1. the device. At BOOT it is created by the persisted module options -- this is what
#    guarantees it exists before docker.service starts, because `devices:` naming a
#    missing node fails container CREATION, not just the node inside it:
#      /etc/modprobe.d/v4l2loopback.conf
#      options v4l2loopback video_nr=40,41 exclusive_caps=1,1 \
#              card_label="ReachyCam","ReachyCamROS" max_buffers=2
#    (deploy/robot/setup-camera-bridge.sh writes it; the arrays must be the same length,
#    and max_buffers is one int, not an array: `max_buffers=2,2` fails every load.)
#    WITHOUT a reboot, create it live -- never `modprobe -r`, which would destroy
#    /dev/video40 under Chromium's open handle:
sudo /opt/emori/deploy/robot/ros-loopback.sh 41
#    camera-bridge.service also runs this, bare, as its second ExecStartPre.

# 2. the tee. EMPTY = no tee at all, and the pipeline is character-for-character the v1
#    one:
sudo sh -c 'echo EMORI_CAM_ROS_VIDEO_NR=41 >> /etc/default/emori-camera'
sudo systemctl restart emori-camera-bridge
journalctl -u emori-kiosk --since -1min     # expect: nothing new

# 3. check it. readycheck's gaze gate reads the tee number from that same file
#    (EMORI_READYCHECK_ROS_VIDEO_DEVICE in /opt/emori/.env still overrides it):
bash deploy/robot/readycheck.sh             # expect: gaze PASS ros_camera_ready
```

Both tee branches carry `queue leaky=downstream max-size-buffers=2`. The ROS branch needs
it so a stalled consumer cannot back-pressure the tee; the **kiosk** branch needs it for
the same reason in the other direction. A leaky queue can drop a frame the badge scanner
would have decoded, which at 30 fps is ~66 ms of slack against a scanner that re-reads
continuously.

### `/opt/emori/models` — the detector weights

```bash
sudo mkdir -p /opt/emori/models
scp deploy/ros2/models/face_detection_yunet_2026may.onnx pollen@reachy-mini.local:/opt/emori/models/
ssh pollen@reachy-mini.local 'sha256sum /opt/emori/models/face_detection_yunet_2026may.onnx'
# ebafce4e3c118d6554634be5c27ab333b4c047a9a8c3faf1d7cf93101c22f0f0
```

The same 229,738-byte file is **baked into the image**, at `/opt/emori/models` — the path
`EMORI_GAZE_MODEL` names — so a container with no bind mount and no network still loads it.
This host copy is mounted read-only at `/opt/emori/models-host`, deliberately **beside** the
baked path and never on top of it: a mount over `/opt/emori/models` would shadow the bake on
every run, and on a robot where this step never ran Docker would create the directory empty
and mount that, leaving a detector with no model on a robot that still reports healthy. Point
`EMORI_GAZE_MODEL` at `/opt/emori/models-host/<file>` when you want to try a model or
detector change as a 229 KB copy instead of a 45-minute arm64 rebuild — and, unlike
`docker cp`, that survives `compose up -d`, which recreates the container and discards the
writable layer. `deploy/robot/install.sh` step 5b lays the directory and the file down on
every robot, armed on none.

## Route B — native, pixi + RoboStack

```bash
curl -fsSL https://pixi.sh/install.sh | bash        # user-level, no root
sudo -u pollen bash deploy/robot/ros2_native/install-native.sh
```

`install-native.sh` creates the environment under `/opt/emori/ros2`, pip-installs the
robot SDK and the EMORI core into it, clones `hri_msgs`, builds `ros2_ws` with colcon
(after deleting any top-level `ros2_ws/src/<pkg>` copy that a deploy of an older layout
left beside `ros2_ws/src/open/` and `ros2_ws/src/emori/`, since colcon refuses duplicate package names),
adds `pollen` to `dialout`/`video`, retires the daemon and installs
`reachy-mini-ros2.service`. It prints the rollback commands at the end, because a
changeover you cannot reverse is not a trial.

### What Route B does not set for you

The unit is not a copy of the Docker robot profile. It reads `/opt/emori/.env`, which
does not carry the ROS container's own settings, so before the first start add these to
`/opt/emori/.env` (or to a drop-in from `sudo systemctl edit reachy-mini-ros2`):

```bash
EMORI_SOURCE=pokamind
EMORI_WS_URL=ws://127.0.0.1:8090/ws/relay     # the robot's HOSTAPP_PORT
EMORI_WITH_FIWARE=false
FIWARE_BRIDGE_ENABLED=0
```

Without them the adapter publishes the mock engagement arc instead of the live session
(and with the owner set to `ros`, that arc moves the robot), cannot reach the hub
(`host.docker.internal` does not exist outside Docker), and a second FIWARE writer starts.
As shipped, the unit reads the camera through the separate `v4l2_camera` node
(`WITH_CAMERA_V4L2=1`, device `REACHY_VIDEO_DEVICE`, default `/dev/video40`, which the
kiosk already holds), so face look-at is not available on Route B: there are no
`/humans/faces/*` topics and `POST /api/emori/gaze` answers 503. The expected node list is
Route A's plus `/reachy_camera`.

### Two things about this route that are not obvious

**`hri_msgs` is not in RoboStack.** `pixi lock` answers `No candidates were found for
ros-jazzy-hri-msgs` for `linux-aarch64` *and* `linux-64` (checked with pixi 0.81.0).
It is a pure `rosidl` interface package, so the installer clones it into
`ros2_ws/src` and colcon builds it alongside `emori_msgs`. The apt route (the Docker
image) has a binary; the conda route does not. Do not "fix" it back into `pixi.toml` — the
environment becomes unsolvable.

**The PyPI half is not in the lock file, and cannot be.** `reachy-mini` belongs in
`[pypi-dependencies]` on paper. Two measured reasons it is a `pip install` in the
installer instead:

1. pixi cannot resolve PyPI requirements for a platform the resolving machine cannot
   execute (`The workspace does not support 'win-64' on this machine`). A manifest
   carrying it could only ever be locked *on the robot*, which defeats shipping a lock.
2. Even resolving from a `linux-64` container, the `linux-aarch64` half fails:
   `reachy-mini` requires PyGObject, PyGObject publishes **no aarch64 wheel**, and
   building its `pycairo` dependency from sdist ends in
   `ERROR: Unknown compiler(s): [['cc'], ['gcc'], ...]`.

So the CM4 compiles PyGObject and pycairo itself, and needs `build-essential pkg-config
libcairo2-dev libgirepository1.0-dev` first. The installer apt-installs them; the Docker
image carries the same four for the same reason.

---

## Status and known limits

| Item | Status |
|---|---|
| Route A: the `:robot` image with the serial backend (`REACHY_BACKEND=robot`) | Runs on robot hardware: real bus and servos, `/joint_states` at 48-49 Hz, the motion-owner rehearsal passing at `owner=ros`, park in about 4.5 s. |
| Route B: `install-native.sh` and `reachy-mini-ros2.service` | **Not yet run on robot hardware.** Reviewed, not executed: treat it as a recipe to trial, with the rollback below at hand. |
| `pixi.lock` | Resolves for `linux-aarch64` and `linux-64` with pixi 0.81.0, generated in the `ghcr.io/prefix-dev/pixi` container (see below). `ros-jazzy-ros-base`, `ros-jazzy-tf2-ros`, `ros-jazzy-v4l2-camera`, `ros-jazzy-diagnostic-msgs` and Python 3.12 are all present for aarch64. |
| `ros-jazzy-hri-msgs` | Not in RoboStack, which is why it is not a dependency: the installer builds it from source. |
| `reachy-mini==1.10.0` on aarch64 | Not resolved through pixi: blocked by the PyGObject sdist above. Its compiled dependencies publish `manylinux_2_17_aarch64` cp312 wheels, so it is expected to install on the robot once the four apt packages are present. |
| Cross-building the `:robot` image | Under qemu, `colcon` must run **sequentially** (parallel `cc1plus` under qemu-user dies with `internal compiler error: Segmentation fault` in the fastrtps type support), and the colcon layer needs its own compiler because the pip layer purges the one it borrowed. |
| Container memory | The graph uses about 1156 MB PSS; how the compose limit becomes real is explained in `deploy/ros2/docker-compose.v2.robot.yml`. |
| The camera | The node reads `/dev/video41`, the second loopback teed by the camera bridge, because v4l2loopback with `exclusive_caps=Y` serves one capture consumer (Chromium on `/dev/video40`). It captures through GStreamer (63 MB of plugins, against 640 MB for `ros-jazzy-v4l2-camera` + `ros-jazzy-audio-common` and their OpenCV/VTK/GDAL/LLVM chain). With the tee off the topic is simply empty, which is where v1's graph always was, so no session loses anything. |
| Firmware `gravity_compensation` | Not yet tested under v2. |

The lock file was produced with:

```bash
docker run --rm -v "$PWD/deploy/robot/ros2_native:/work" -w /work \
    ghcr.io/prefix-dev/pixi:latest pixi lock
```

not with a bare `pixi lock` on Windows — that fails at the first PyPI resolution for the
reason in "Two things" above. Regenerate it the same way.

## What the unit sources, and why it is a login shell

`reachy-mini-ros2.service` does not call the environment's `ros2` binary directly. It
runs

```
ExecStart=/bin/bash -lc 'source /opt/emori/ros2/.pixi/envs/default/setup.bash &&                          source /opt/emori/ros2_ws/install/setup.bash &&                          exec ros2 launch emori_adapter_bringup v2.launch.py'
```

because `ros2 launch` finds packages through `AMENT_PREFIX_PATH`, `ROS_PACKAGE_PATH` and
`PYTHONPATH`, and systemd provides none of them: there is no login shell under a unit, so
nothing sources anything. Running the binary alone produces
`Package 'emori_adapter_bringup' not found`, or — once the environment's own packages
happen to resolve — a graph running without the colcon overlay.

The two files are the documented ones:

| File | Comes from |
|---|---|
| `/opt/emori/ros2/.pixi/envs/default/setup.bash` | RoboStack. Its `ros-<distro>-ros-workspace` conda package installs `setup.bash` at the **environment prefix root**, which is the activation RoboStack's own "Getting started" documents for use outside `pixi shell`. `install-native.sh` sources the same file for its `colcon build`, so the name is proven by the install that produced it. |
| `/opt/emori/ros2_ws/install/setup.bash` | `colcon build`, step 6 of `install-native.sh`. |

`install-native.sh` asserts both exist before it installs the unit, and `exec` is what
makes the launch process replace the shell — without it systemd's `SIGTERM` on stop goes
to `bash`, the launch never hears it, and the robot is `SIGKILL`ed without parking. The
unit's `TimeoutStopSec=25` exists for the same stop path: the node runs
`backend.reset_to_sleep()` on the way out (bounded at 8 s), which lifts the head to the
init pose, plays the sleep trajectory and ends **de-torqued at the sleep pose**. A stop
that kills it mid-trajectory leaves nine servos torqued at an arbitrary pose until the
power goes and the head drops.

## When it will not start

`ROS_LOCALHOST_ONLY=1` and `ROS_DOMAIN_ID=42` apply to Route A and Route B alike: a shell
outside the container or the unit has to export them to see anything.

Route A:

```bash
docker ps --filter name=emori-ros2-v2
docker logs --tail 100 emori-ros2-v2      # the node's first INFO line: "reachy_mini up: backend=... motors=... shim=... camera=on|off gaze=built|off"
docker exec emori-ros2-v2 /entrypoint.sh ros2 node list
curl -s localhost:8000/api/daemon/status
```

Route B:

```bash
systemctl status reachy-mini-ros2 reachy-mini-daemon
journalctl -u reachy-mini-ros2 -n 200 --no-pager
/opt/emori/ros2/.pixi/envs/default/bin/ros2 node list
curl -s localhost:8000/api/daemon/status
```

| Symptom | Almost certainly |
|---|---|
| `/api/daemon/status` has no `backend` field | the daemon is still up and holding `:8000` |
| the node starts, motors never enable | `pollen` is not in `dialout` yet — group membership is read at login, so restart the unit (or the robot) after `usermod` |
| `find_serial_port` finds nothing | the daemon still has `/dev/ttyAMA3` open, or this is a *lite* (CH340 on `/dev/ttyUSB0`, USB `1a86:55d3`) and `REACHY_WIRELESS=1` is wrong |
| `ros2 node list` is empty from another shell | `ROS_LOCALHOST_ONLY=1` + `ROS_DOMAIN_ID=42` are set in the unit and have to be set in your shell too |
| `/joint_states` is there but slow | the 50 Hz loop is not keeping up; `MemoryMax=768M` in the unit is the first thing to check against actual RSS |
| a second container on the robot sees every topic and no data | Fast DDS prefers shared memory and `/dev/shm` is per-container. `FASTDDS_BUILTIN_TRANSPORTS=UDPv4` on the *consumer*, never on the graph under test |
| the log says the camera is unavailable, or `/reachy_mini/camera/image_raw` is silent | the tee is off (`EMORI_CAM_ROS_VIDEO_NR`) or another process holds the device; the node keeps retrying |
| the log says gaze is unavailable | `hri_msgs` or `onnxruntime` is missing; see the readycheck `gaze` gate |
| the process exits with code 70 | the control loop died after having been ready; the supervisor restarts it (see "Status and known limits") |

## Rolling back to v1

Two of the v2 changes revert on their own, without touching the container, and they come
**first** because each is one line and each isolates one behaviour:

```bash
# 0. the camera tee. Removes the ROS branch from the bridge pipeline entirely; the kiosk
#    pipeline goes back to character-for-character what it was. /dev/video41 is left in
#    place and simply unfed, which is harmless.
sudo sed -i 's/^EMORI_CAM_ROS_VIDEO_NR=.*/EMORI_CAM_ROS_VIDEO_NR=/' /etc/default/emori-camera
sudo systemctl restart emori-camera-bridge

# 0b. residual gesture sensing off: push_turn reads the ABSOLUTE head pose again, and
#     head_reserved_for_sensing() takes its wide meaning (reserved whenever push-turn
#     sensing is armed), which changes three consumers together: per-sentence cues stop
#     planning while the robot speaks, the skill intake refuses 423 during a session, and
#     the idle scan stands down.
sudo sh -c 'echo EMORI_GESTURE_RESIDUAL=0 >> /opt/emori/.env'
sudo systemctl restart emori-hostapp
```

Then the full v1 rollback:

```bash
sudo rm -f /opt/emori/V2_OWNS_THE_BUS     # lower the fence first, or the daemon start below is skipped
sudo systemctl disable --now reachy-mini-ros2          # route B
docker compose -f deploy/ros2/docker-compose.v2.robot.yml down   # route A
sudo mkdir -p /etc/systemd/system/reachy-mini-daemon.service.d
sudo cp /opt/emori/deploy/robot/reachy-mini-daemon-override.conf \
     /etc/systemd/system/reachy-mini-daemon.service.d/override.conf
sudo systemctl daemon-reload
sudo systemctl enable --now reachy-mini-daemon
curl -s localhost:8000/api/daemon/status    # no `backend` field = v1 is back
# hand expressive motion back to the hostapp (and set EMORI_WITH_ROS2=0 unless the v1 graph runs):
sudo sed -i 's/^EMORI_ROS_MOTION_OWNER=.*/EMORI_ROS_MOTION_OWNER=hostapp/' /opt/emori/.env
sudo systemctl restart emori-hostapp
# the v1 graph, if this robot had one:
# sudo systemctl enable --now emori-ros2
```

v1's image, compose files and units are untouched by anything in v2, so the rollback is a
unit swap and not a redeploy.
