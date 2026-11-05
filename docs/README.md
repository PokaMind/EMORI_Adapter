# EMORI documentation index

Every document in the repository, by who needs it, one line each. The figures in
[`architecture/`](architecture/) are generated and are not listed separately.

## Operators: deploying and running robots

| Document | What it is for |
|---|---|
| [deploy-a-new-robot.md](deploy-a-new-robot.md) | Provisioning a robot: its Pokamind token and per-robot settings, v1 or v2, deploying from another network, readycheck then rig-smoke |
| [onrobot-runbook.md](onrobot-runbook.md) | First bring-up of new hardware by hand, and debugging a robot: the daemon, camera bridge, kiosk, ROS 2 lane, presence |
| [robot-admin.md](robot-admin.md) | The phone admin page on `:8070`: first-time provisioning (hotspot, PIN, Wi-Fi, token, automatic), Wi-Fi, volume, language, people and QR badges, the Pokamind token and what its record sets, the spoken topic menu, settings and credentials, automatic apply, restarts, logs, troubleshooting |
| [addons.md](addons.md) | Writing and deploying an optional add-on: the `emori_adapter.extensions` entry point, the `HostExtension` hooks, the `contributed` classmethods and the `addons/<name>/` deploy convention |
| [configuration.md](configuration.md) | Every `.env` setting with its full rationale (the template is [`.env.example`](../.env.example)) |
| [../deploy/robot/ros2_native/README.md](../deploy/robot/ros2_native/README.md) | v2 on the robot without Docker (pixi + RoboStack), and its status and known limits |
| [../deploy/fiware/README.md](../deploy/fiware/README.md) | Running the FIWARE analytics stack on a LAN server |

## Developers: working on this repository

| Document | What it is for |
|---|---|
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | Setting up, running the checks, the repository rules, pull requests |
| [dev-setup.md](dev-setup.md) | Dev box setup, the simulator topologies, tests, ports, where to find things, known issues |
| [guides/01-start-the-simulation.md](guides/01-start-the-simulation.md) | Guide 1: start the MuJoCo simulator, alone or as the full topology |
| [guides/02-start-a-session.md](guides/02-start-a-session.md) | Guide 2: run a training session through the hub, mock or live |
| [guides/03-how-the-robot-reacts.md](guides/03-how-the-robot-reacts.md) | Guide 3: from an analysis cycle to a movement, and the behaviour policy |
| [architecture.md](architecture.md) | The architecture: the hostapp hub, the two slices, the data model, how to verify it |
| [architecture/emori-system-architecture.md](architecture/emori-system-architecture.md) | The one-page generated system figure and how to regenerate it |
| [ros4hri-mapping.md](ros4hri-mapping.md) | The realized ROS graph: every topic, message, the skill contract, the privacy gate |
| [ros2-v2-native-robot.md](ros2-v2-native-robot.md) | v2: the robot as a native ROS 2 node: design, deployment routes, verification, risks |
| [analytics-interface.md](analytics-interface.md) | The FIWARE analytics contract and how an external consumer verifies it |
| [motion-owner-rehearsal.md](motion-owner-rehearsal.md) | `EMORI_ROS_MOTION_OWNER`: the rehearsal harness and how to read its assertions |
| [hardware-notes.md](hardware-notes.md) | Reachy Mini hardware facts and the verified REST surface |
| [adr-001-onrobot-screenless.md](adr-001-onrobot-screenless.md) | ADR-001 (accepted): the session runs on the robot, screenless and audio-first, with the robot's own camera |

## Pokamind integration

| Document | What it is for |
|---|---|
| [pokamind-integration.md](pokamind-integration.md) | Pokamind session contract (robot side): what the session emits, the iframe `postMessage` transport, partner SSO |

## Open source and licensing

| Document | What it is for |
|---|---|
| [../LICENSING.md](../LICENSING.md) | The licence (Apache-2.0) and the third-party components |
| [../SECURITY.md](../SECURITY.md) | Reporting a vulnerability, and the robot token model |
| [../CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md) | How we expect contributors to behave |
| [../ros2_ws/src/open/README.md](../ros2_ws/src/open/README.md) | The standalone ROS 2 packages: what is in them and how to build them alone |
| [../ros2_ws/src/open/CONTRIBUTING.md](../ros2_ws/src/open/CONTRIBUTING.md) | The rules that keep those packages standalone |
| [ros4hri-release.md](ros4hri-release.md) | Releasing the ROS 2 packages |
| The package `README.md` files under `ros2_ws/src/` | Topics, parameters and build instructions per package |
