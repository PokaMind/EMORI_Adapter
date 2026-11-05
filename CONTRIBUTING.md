# Contributing to EMORI Adapter

Thank you for helping. This page covers setting up, running the checks, the rules the
repository depends on, and how to send a change. Everyone taking part is expected to
follow the [Code of Conduct](CODE_OF_CONDUCT.md). Security problems are reported
privately, never in a public issue: see [SECURITY.md](SECURITY.md).

## Getting set up

The test suite runs on Python 3.10 to 3.13. The MuJoCo simulator needs **Python 3.12**
(`reachy-mini[mujoco]` pins `mujoco==3.3.0`, which has no wheel for 3.14), so use a 3.12
virtual environment if you want the simulator:

```bash
python3.12 -m venv .venv-reachy            # or: uv venv .venv-reachy --python 3.12
. .venv-reachy/bin/activate                # Windows: .\.venv-reachy\Scripts\Activate.ps1
python -m pip install -e ".[dev,hostapp,pokamind]"
python -m pip install -e ".[dev,hostapp,pokamind,sim]"     # with the simulator
```

The other extras (`speech`, `stt`) are what the robot installs; add them when
you work on those parts. The full development setup, the simulated topologies, ports and
known gotchas are in [docs/dev-setup.md](docs/dev-setup.md), and the walkthroughs start at
[docs/guides/01-start-the-simulation.md](docs/guides/01-start-the-simulation.md).

None of the checks below need a robot, ROS or a Pokamind account.

## Running checks

From the repository root:

```bash
python -m pytest tests -p no:cacheprovider    # pyproject already adds -q; do not add another
python -m ruff check src tests
```

Both are clean on the default branch, so a new failure or a new ruff finding comes from
your change. Keep the simulator's daemon and any local Orion-LD broker **down** while the
suite runs.

Two tests are timing-sensitive under full-suite load and pass when run alone. If one of
them fails, re-run it by itself before investigating:

- `tests/test_hostapp.py::test_robot_side_walkaway_finalize_sends_the_session_page_home`
- `tests/test_robot_player_voice.py::test_a_speech_plan_waits_for_the_voice_then_moves`

With Docker, `python scripts/ros_standalone_check.py` builds the standalone ROS 2 packages
with no EMORI core installed and starts their nodes, as CI does.

## Rules

These hold across the whole repository, and most of them are enforced by tests.

1. **The ROS 2 import boundary.** Nothing under `ros2_ws/src/open/` imports
   `emori_adapter` or depends on a package in `ros2_ws/src/emori/`. Every ROS package
   lives in exactly one of the two directories. `tests/test_ros_standalone.py` and
   `tests/test_ros_publishing.py` check it.
2. **One writer moves the robot**: the `RobotPlayer` queue of the host application, in the hub on
   a robot. No ROS node holds a daemon URL, and `EMORI_ROS_MOTION_OWNER` (`hostapp` | `ros` |
   `none`) decides which lane may submit motion.
3. **Every robot call works on both transports**: over ROS 2, as a robot runs it in the hub, and
   over HTTP, as the simulator and the way back run it. On a v2 robot `:8000` is the ROS node's
   REST shim, not Pollen's daemon. Any new `:8000` call must pass `tests/test_daemon_contract.py`,
   whose failure messages say what to add. The same test writes the committed manifest
   `src/emori_adapter/hostapp/daemon_routes.json`; regenerate it with
   `python tests/test_daemon_contract.py --write-manifest` and commit it with your change.
4. **`.env.example` assigns each name once** (the last assignment wins in dotenv and
   systemd). Its long-form notes live in [docs/configuration.md](docs/configuration.md),
   under the same headings.
5. **Figures are generated.** The figures in `docs/architecture/` come from
   `scripts/build_*_graph.py`. Change the script and regenerate; never edit an SVG or PNG
   by hand.
6. **No secrets.** Never commit a real `.env`, `robot.local.env`, a token, a key or any
   other credential, and never paste them into an issue, a log or a test. A robot token is
   only ever shown masked (`pkr1.<id>.…abcd`). The one full token allowed in the
   repository is the golden test vector `pkr1.test-robot-01.…`.
7. **Docs and configs are tested.** Several tests read docs and configs by path (headings,
   refusal codes, menu files, `.env.example` sections). When you rename or reword one,
   update the tests and the text together.
8. **The Pokamind contract changes in a fixed order.** The client rejects unknown response
   keys. The robot sends `X-EMORI-Contract: 2` on `GET /api/robot/whoami`, and Pokamind
   sends the keys that contract adds only to a caller that sent the header. So a new
   response key behind a contract header can ship server-first; a response key added
   without a header gate ships EMORI-first (every robot must accept it before any server
   sends it); a new request key ships Pokamind-first. The contract is in
   [docs/pokamind-integration.md](docs/pokamind-integration.md).
9. **Never make Pollen's daemon reacquire media** (no `POST /api/media/acquire`): it kills
   the camera bridge and the kiosk's microphone and speaker with it.

[docs/dev-setup.md](docs/dev-setup.md#where-to-find-things) maps where each part of the
code lives.

## Commit style

```
feat(scope): what the change adds
fix(scope): what was wrong, now fixed
docs: …
test(scope): …
chore: …
```

The subject says what changes for a user or a maintainer; the body says why.

## Pull requests

Before you open one:

- [ ] `python -m pytest tests -p no:cacheprovider` and `python -m ruff check src tests`
      pass.
- [ ] New behaviour has a test; a bug fix has a test that failed before the fix.
- [ ] Any new `:8000` call passes `tests/test_daemon_contract.py`, and
      `daemon_routes.json` is regenerated if it changed.
- [ ] A new setting is in `.env.example` (once) and in `docs/configuration.md`.
- [ ] Docs that describe the changed behaviour are updated, and a new document is listed
      in [docs/README.md](docs/README.md).
- [ ] No secret, token, real `.env`, personal data or site-specific data is in the diff.

Keep a pull request to one concern. Say in the description how you tested it, and
whether it needs a robot (v1 or v2) to verify.

## Licence of contributions

This project is licensed under the [Apache License 2.0](LICENSE). Unless you say
otherwise, any contribution you intentionally submit for inclusion is licensed under the
same terms, as section 5 of the licence states, without any additional terms or
conditions.
