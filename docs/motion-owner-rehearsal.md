# The motion-owner rehearsal

`EMORI_ROS_MOTION_OWNER` decides who moves the robot: the hostapp's own expressive lane
(`hostapp`, the default), the ROS graph (`ros`), or nobody (`none`). This page covers the
harness that rehearses each mode end to end in the simulator, the assertions it makes, and
how to read the two ledgers it collects.

The thing worth saying up front: **reading a counter on one side of the boundary is not
proof.** The facade's ledger counts what it POSTed, the hostapp's counters count what it
did with those posts, and neither of them is evidence that a head moved. So the harness
samples the robot's pose too, at 4 Hz, straight off the daemon — the only witness that is
not written by the code under test.

---

## Running it

```bash
# Build once: this retags emori/ros2-humble:latest for THIS host's architecture.
# The shipped :latest is an arm64 CM4 image and will run under emulation, slowly.
docker compose -f deploy/ros2/docker-compose.yml build

python scripts/rehearse_motion_owner.py --owner ros --no-build
python scripts/rehearse_motion_owner.py --owner hostapp --no-build
python scripts/rehearse_motion_owner.py --owner none --no-build
```

Run `ros` first: it is the only mode that exercises the whole chain, and the other two are
mostly "nothing happened" assertions. Exit 0 means every assertion held. The JSON ledger
lands in `artifacts/rehearsals/<utc>-<owner>.json` **either way** — a failed rehearsal is
the one whose numbers are worth keeping.

Useful flags: `--fixture`, `--host-port 8095`, `--proxy-port 8096`, `--image`,
`--build/--no-build`, `--keep-up` (leave the stack running to poke at it), `--json <path>`,
and `--fiware [--orion-url http://localhost:1026]` (see "Analytics" below).

### What one run does

1. **Daemon.** Reuses `http://127.0.0.1:8000` if it answers; otherwise starts
   `reachy-mini-daemon --sim --headless --no-media` and waits up to 40 s. It is killed on
   the way out only if this run started it.
2. **Loopback proxy — `owner=ros` only.** `scripts/dev/loopback_proxy.py` listens on
   `0.0.0.0:8096` and forwards to `127.0.0.1:8095`. Docker Desktop cannot host-network, and
   `/api/skill` refuses any caller that is not loopback. Rather than weaken the production
   gate for the dev box, the request takes one extra hop and still has to pass it. On the
   robot the container *is* host-networked and there is no proxy.
3. **Hostapp** on `:8095` with the skill intake open and a fresh random key per run.
4. **The ROS container** (`deploy/ros2/docker-compose.yml`), pointed at the hostapp's
   `/ws/relay` and at the proxy's `/api/skill`, waiting for the four nodes to appear.
5. **A 4 Hz pose sampler** on `GET /api/state/present_head_pose`.
6. **The replay**: `scripts/replay_session.py` over `tests/fixtures/rehearsal_session.jsonl`
   in real time (24 analysis cycles, 7 utterances rotating through all six cue families,
   7 `pokamind_tts_ended` events, the TTS-ended marker, ~46 s), then an 8 s tail so the
   last sentence's motion
   and both 5 s ledger ticks land.
7. **Collect**: `GET /api/status` counters, `ros2 topic echo /diagnostics` for 12 s parsed
   into the latest status per name, and the last 400 lines of the container log.
8. **Tear down** (unless `--keep-up`) and assert.

### The environment it pins, and why

The harness pins these variables, because each one would otherwise make the rehearsal
lie:

| Variable | Value | Why it is not optional |
|---|---|---|
| `EMORI_ROS_OWNER_FALLBACK_S` | `0` | Otherwise an `owner=ros` run could silently become a hostapp run halfway through and still pass every assertion |
| `EMORI_IDLE_AFTER_S` | `0` | The idle ticker moves the head every 5 s on its own, which would satisfy `pose_changes` with a robot nobody drove — the one thing the pose sampler exists to rule out |
| `EMORI_GESTURE` | `0` | The MuJoCo head drifts under gravity compensation and the tilt detector reads the drift as taps |
| `EMORI_VOICE_LINK`, `EMORI_RECORD` | `0` | No `pw-record` on Windows; and a rehearsal is not a session, so it should not litter `artifacts/recordings/` |
| `EMORI_FIWARE` | `0` unless `--fiware` | No broker in the loop; a missing one adds retry latency to every cycle |

---

## The assertion table

Every threshold is a named constant at the top of the script with the reason it has that
value. They are **floors well under a healthy run**, because this is a pass/fail gate on a
timing-sensitive stack, not a benchmark: it has to fail on "the lane never ran" and never
on "the box was busy for a second".

| owner | hostapp counters | facade ledger | behaviours ledger | pose |
|---|---|---|---|---|
| `ros` | `engagement_suppressed_by_owner ≥ 5`; `skill_accepted ≥ 8`; `skill_refused_not_owner`, `skill_refused_bad_key`, `skill_refused_not_loopback` all `== 0` | `posted ≥ 8`, `accepted ≥ 5`, `suppressed == 0`, `unreachable == 0` | `cycles ≥ 20`, `cue ≥ 5` | `pose_changes ≥ 5` |
| `hostapp` | `engagement_suppressed_by_owner == 0`; no `skill_*` counter above 0 | `suppressed ≥ 8`, `posted == 0` | `cycles ≥ 20` | `pose_changes ≥ 5` |
| `none` | `engagement_suppressed_by_owner ≥ 5`; no `skill_*` counter above 0 | `suppressed ≥ 8`, `posted == 0` | `cycles ≥ 20` | informational |

Notes on three of them:

* The three refusal counters are the ones that can only mean **misconfiguration** — the
  owner knob disagreeing across the boundary, a key mismatch, or a request that did not
  arrive over loopback. `rate_limited`, `head_reserved` and `dropped` are healthy and are
  not asserted to be zero.
* `pose_changes` counts samples whose maximum absolute change against the previous sample
  exceeds `POSE_EPSILON = 0.01` (rad or m) — well above the sim's idle numerical drift,
  well below any authored gesture. A shape change is not motion.
* `none` leaves pose informational on purpose: a still robot is the correct outcome, but
  the sim's own settling makes `0` an unreliable assertion.

---

## How to read the two ledgers

Both come off `/diagnostics` (`diagnostic_msgs/DiagnosticArray`, every 5 s; the same line
goes to INFO every 30 s). You can read them live:

```bash
docker exec emori-ros2 bash -lc 'source /opt/ros/humble/setup.bash \
  && source /emori/ros2_ws/install/setup.bash && timeout 12 ros2 topic echo /diagnostics'
```

**`emori_skill_facade`** (`hardware_id` = the transport name):
`posted accepted dropped refused suppressed unreachable`, plus `motion_owner`,
`transport` and `url_host`.

* `posted` is counted **by the node, right after the owner gate and before the transport
  runs**, so `posted + suppressed` is always the number of requests the graph made,
  whichever transport is plugged in.
* `suppressed` is the owner gate refusing its own node. On the shipped default that is the
  correct, healthy state, and the diagnostic level stays OK.
* `unreachable` growing means the hostapp is not answering at all (wrong port, no proxy,
  intake not mounted) — the level goes WARN. An empty key under `owner=ros` with the HTTP
  transport is ERROR.

**`hri_engagement_behaviors`** (`hardware_id` = the `person_id`):
the policy's counters `cycles engagement cue yielded_to_speech cooldown
expression_changes settles`, the node's `sent expressions dropped result_<code|ok>`, plus
`motion_owner` and `config_source`.

* `config_source=yaml:/emori/configs/behavior.yaml` is the proof the tuning file was read.
  `defaults` means it was not, and the node will also have logged an ERROR naming the path.
* `result_ok` vs `result_not_owner` tells you which side of the gate you are on without
  reading a single log line: under `owner=hostapp` the node still *sends* every request and
  the facade's action server answers `not_owner` to all of them.
* A `-> yielded` cycle carries **no** request — not even an expression. That is the whole
  point of the yield rule, and the behaviour log line for such a cycle ends in exactly one
  outcome with nothing after it.

The two sides should reconcile request for request. For the bundled fixture under
`owner=ros`, the arithmetic works out like this:

```
24 cycles − 4 yielded = 20 cycle requests
   (nod 7 + calm_settle 4 + antenna_wiggle 3 + expression 3 + look_around 2 + head_tilt 1)
 + 7 cue requests + 4 settles
 = 31 = facade posted = facade accepted = hostapp skill_accepted
```

Nothing lost across the boundary, nothing invented.

---

## What the three modes look like

An illustrative result over the bundled fixture (exact numbers vary a little from run to
run because the stack is timing-sensitive):

| | `--owner ros` | `--owner hostapp` | `--owner none` |
|---|---|---|---|
| facade | `posted 31`, `accepted 31`, `suppressed 0` | `suppressed 31`, `posted 0` | `suppressed 31`, `posted 0` |
| behaviours | `cycles 24`, `cue 7`, `sent 28`, `result_ok 28` | `cycles 24`, `cue 7`, `sent 28`, `result_not_owner 28` | as `hostapp` |
| hostapp | `engagement_suppressed_by_owner 24`, `skill_accepted 31`, no `skill_refused_*` | `engagement_suppressed_by_owner 0`, no `skill_*` key | `engagement_suppressed_by_owner 24`, `cue_suppressed 6`, no `skill_*` key |
| `pose_changes` (of ~217 samples) | about 95 | about 86 | about 11 |

Three things to read from it:

* **`hostapp` versus `ros`.** The behaviours ledger is identical in both modes except that
  `result_not_owner` replaces `result_ok`: the graph computes exactly the same decisions
  and the facade refuses to post them. Turning the graph on is a non-event.
* **`ros`.** Every `skill_refused_*` counter is absent, i.e. zero — not one refusal of any
  kind, including the ones the table does not assert (`head_reserved`, `rate_limited`,
  `out_of_range`, `bad_skill`, `skill_api_disabled`). The facade's `url_host` is
  `host.docker.internal:8096`, the loopback hop, as designed.
* **`none`.** An order of magnitude less head motion is the correct signature of "nobody
  owns expression". What remains are the ungated interaction mechanics — greeting/farewell,
  idle recenter — plus sim settling. Those are interaction *mechanics*, not expression, and
  they are deliberately outside the owner gate.

---

## Analytics: the same rehearsal with a broker

`--fiware` points the hostapp at Orion-LD and, importantly, also opens the session page
once. Without that second part the rehearsal writes **no entity at all**: the NGSI-LD
writer is built in `LiveDriver.start_session()`, which is called from `GET /session` (the
kiosk page load *is* the start of a session), and the fixture is a stream of `/api/cycle`
envelopes and nothing else. All 24 cycles would land as `cycles_outside_session`, and the
consumer check would then pick up a stale entity from a previous run and pass trivially.

```bash
docker compose -f deploy/fiware/docker-compose.yml up -d
python scripts/rehearse_motion_owner.py --owner ros --no-build --fiware
python scripts/arise_consumer_check.py \
  --orion-url http://localhost:1026 --ql-url http://localhost:8668 \
  --tenant <tenant> --session latest
```

`<tenant>` is the hostapp's `NGSILD_TENANT` (`pokamind` by default). A passing run reports
`sessions_started: 1`, and the consumer check prints something like

```json
{
  "session": "urn:ngsi-ld:Session:<session-id>",
  "recoveryAttempts": 4, "recoverySuccesses": 4, "figure": 1.0,
  "series_attempts": 4, "series_successes": 4, "series_figure": 1.0,
  "agree": true
}
```

Exit 0, and **`recoveryAttempts` is not 0** — the fixture's DISENGAGING/DISENGAGED
cycles drive real attention drops, all of them recovered inside the 90 s window
(`metrics_store.RECOVERY_WINDOW_S`), and Orion's summary counters agree with an
independent recomputation from QuantumLeap's `engagementLevel` series on **both counters**,
not merely on the ratio. See [analytics-interface.md](analytics-interface.md).

Caveat worth knowing: this fixture recovers from every drop, so the "attempt without
success" arm of the rule is only covered by unit tests.

---

## Gotchas

* **Build for this host first.** The shipped `emori/ros2-humble:latest` is the arm64 CM4
  image. `docker compose -f deploy/ros2/docker-compose.yml build` retags it amd64; the
  harness cannot tell the two apart and an emulated run will time out.
* **Git Bash mangles container paths.** `docker run -e VAR=/absolute/container/path` typed
  into Git Bash on Windows arrives as `C:/Program Files/Git/absolute/...`. Prefix with
  `MSYS_NO_PATHCONV=1`, or use PowerShell.
* **`pytest -q` hides the count.** `pyproject.toml` already sets `addopts = "-q"`, so a
  second `-q` suppresses the final summary line.
* **Do not run the full test suite with the FIWARE stack up.** Orion-LD 1.12.0 hits an
  internal `delayedFreeVec` limit after a few hundred "entity already exists" POSTs from
  the hostapp/FIWARE tests and exits. Nothing is lost (Mongo and Timescale persist) but
  the stack needs bringing back up.
* **A second container on the graph needs `FASTDDS_BUILTIN_TRANSPORTS=UDPv4`.** Containers
  sharing only a network namespace look like one host to Fast DDS, which prefers shared
  memory — but `/dev/shm` is per-container. Discovery succeeds, delivery silently does not.
  This is a deployment fact, not a test artefact; it applies on the robot too.
* **On a v2 robot with face look-at running, `accepted` and `completed` stop reading as
  "moves".** Look-at writes one arbiter `set_target` per accepted tick, on the expression
  lane as client `ros:gaze`, at 5 Hz — roughly **18,000 an hour**. Those land in the same
  global `accepted` / `completed` counters as every goto, because excluding them would
  break the ledger identity the arbiter's own docstring promises
  (`accepted + Σ refused_* == submitted`). So a rehearsal transcript from a robot where a
  person was standing in front of it will show numbers that look wildly wrong against the
  ones on this page, and they are not.
  Two things make it readable anyway: the harness's own checks are all `_ge` bounds on
  *named* counters (`accepted_ros`, `skill_accepted`, …) and are unaffected; and the
  per-client counters separate the streams, so `accepted_ros:gaze` is the look-at traffic
  and `accepted_ros` is the graph's actual gestures. Read the per-client keys, never the
  global pair, on a robot where anyone was present.

## Related

* [`ros4hri-mapping.md`](ros4hri-mapping.md) — the realized graph, the `/api/skill`
  contract, the `Context.msg` constants, the privacy gate.
* [`onrobot-runbook.md`](onrobot-runbook.md) Step 5b — flipping the owner on the robot and
  the `ros2` readycheck gate.
* [`analytics-interface.md`](analytics-interface.md) — the off-robot broker topology and
  the consumer contract.
