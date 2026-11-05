# Deploy a new robot

Every EMORI robot has its **own Pokamind token**: one line,
`pkr1.<robot-id>.<key>` (the key is 43 characters), issued by Pokamind for that one robot. The robot
signs its learner logins and its control-plane calls with it; Pokamind keeps only the
public half, scoped to one organisation, its modules, languages and features. One token
= one robot: it never goes in `robot.local.env`, in a chat, or on a second robot.

The token is also how the robot configures itself. Once it is installed, the robot reads
its own Pokamind record over the same signed channel and takes from it everything a
site would otherwise have to type: the course, the languages, the pathways, its display
name and the spoken topic menu. What only the robot can know it generates itself (the
admin PIN, the hotspot and dashboard credentials). A person enters Wi-Fi and the token.

You need a robot token issued by Pokamind, and this repository on a computer that can
reach the robot over SSH: on its Wi-Fi, or from another network
([below](#deploying-from-another-network)).

The order, start to finish:

- [ ] [Before you start: a brand-new robot's first network](#before-you-start-a-brand-new-robots-first-network)
- [ ] [0. v1 or v2](#0-v1-or-v2) - a new robot is v2 unless you pass `--v1`
- [ ] [1. Get the robot's token](#1-get-the-robots-token-from-pokamind) from Pokamind
- [ ] [2. Deploy](#2-deploy-to-the-robot) with Deploy-to-Robot
- [ ] [3. Connect it on the admin page](#3-connect-it-on-the-admin-page-wi-fi-token-automatic-sync): Wi-Fi, token, automatic sync
- [ ] [4. readycheck](#4-run-the-readycheck) green
- [ ] [5. rig-smoke](#5-smoke-test): the control plane, then roleplay
- [ ] [6. v2](#6-v2-what-the-deploy-does) - done by step 2; read it for a robot that stayed on v1

## Per-robot settings

Everything that differs from one robot to the next, grouped by where it comes from. A
setting that is not here is the same on every robot (`.env.example`). Any of them can
still be set by hand, and then the first of these wins: a value saved on the admin page,
a value in `/opt/emori/.env`, the robot's Pokamind record, its hardware profile, the code
default ([configuration.md](configuration.md#where-each-setting-comes-from)). The admin
page's Settings tab and readycheck's `site_config` gate show where each value came from.

**A person enters it**

| Setting | Where it lives | How |
|---|---|---|
| Pokamind token | `/opt/emori/secrets/pokamind_robot_token` (0600, dir 0700) | Deploy-to-Robot (`--token-file`, `--replace-token`) or the admin page's Pokamind tab. Both ask Pokamind first |
| Site Wi-Fi networks | NetworkManager | The very first network: Pollen's own setup ([before you start](#before-you-start-a-brand-new-robots-first-network)). Later ones: admin page, Network tab |
| People and their QR badges | `configs/roster.yaml` | Admin page, People tab. Optional: drop-in sessions need no roster |
| Optional overrides: results e-mail (SMTP), analytics (FIWARE), the menu's wording, the Pokamind server | `configs/site/settings.env`, `secrets/site.env` | Admin page: Settings and Menu tabs |
| Clock (NTP) | `timedatectl set-ntp true`; NTP (UDP 123) allowed out of the site network | Operator; the site's network admin for UDP 123 |
| v1 or v2 | `EMORI_ROS2_STACK` in `.env` (unset = v2); `/opt/emori/V2_OWNS_THE_BUS` is the fence it raises | Deploy-to-Robot (`--v1` / `--v2`, [step 6](#6-v2-what-the-deploy-does)) |

**Pokamind issues it with the token** (the robot's Pokamind record, read at host app
start, every 6 hours, on the Pokamind tab's Sync now and whenever a token is installed;
the last good copy is kept in `configs/site/pokamind_scope.json`). The items marked
*contract 2* come only from a Pokamind service that serves contract 2 of
`GET /api/robot/whoami` ([pokamind-integration.md](pokamind-integration.md)); until it
does, the robot uses the fallback the table names, and the admin page's Settings and
Menu tabs can set them.

| Setting | What the robot does with it |
|---|---|
| Robot id | The token's. `EMORI_ROBOT_ID` is filled from it when `.env` has none |
| Organisation, modules, staff badge domains | Checked live; the People tab refuses an e-mail outside the badge domains |
| Course | The course claim for drop-ins and for badges whose roster sets none (`EMORI_COURSE_ID` when `.env` leaves it empty; else `emori`) |
| Languages, in order, and the default language (*contract 2*) | The kiosk's boot language (`EMORI_LANG` when empty): the default language, else the record's first language that has modules; the Language tab offers only these, less any the record has no module in |
| Pathways | `EMORI_PATHWAYS` when empty (not when a record pathway holds a comma: then no allowlist, as without a record; `EMORI_PATHWAYS=*` says "no allowlist" explicitly); the People tab offers only these |
| Display name | `EMORI_ROBOT_NAME` when empty (a name that is just the robot id is not used: the robot stays "EMORI") |
| Modules, in menu order, with their spoken names (*contract 2*) | The spoken topic menu, per language: up to 6 modules, numbered in the record's order, rebuilt on every sync and live at once (`configs/site/auto/`). A module without a spoken name gets one drafted from its title |

**The robot generates it** (`install.sh`; the ones marked *shown once* appear at the end of
the install's output, and again after readycheck at the end of a Deploy-to-Robot run)

| Setting | Where it lives | Notes |
|---|---|---|
| Admin PIN (*shown once*) | `EMORI_ADMIN_PIN` in `.env`; once changed on the admin page, `secrets/admin.env` | Change it on the admin page (Settings). Deploy-to-Robot generates one too when its PIN question is left empty |
| Recovery hotspot SSID + password (*shown once*) | `EMORI_AP_SSID`, `EMORI_AP_PASSWORD` in `.env`; once changed, `secrets/admin.env` | SSID `emori-<robot id>`. Revealed or changed on the admin page with the PIN; an operator's own name is never touched |
| Dashboard password (*shown once*) + salt | `EMORI_DASH_PASSWORD`, `EMORI_DASH_SALT` in `.env` | The password is revealed or reset on the admin page; the salt (only on a robot with no dashboard data yet) is never shown or changed |
| Admin token + cookie salt | `EMORI_ADMIN_TOKEN`, `EMORI_ADMIN_SALT` in `.env` | |
| ROS skill key | `EMORI_SKILL_KEY` in `.env` | Inert until the ROS skill intake is turned on. The ROS id salt is NOT generated: unset, ROS pseudonyms change at every restart, and a fixed `EMORI_ROS_ID_SALT` is a data-protection decision ([configuration.md](configuration.md#emori_ros_deidentify-emori_ros_speech_text-emori_ros_id_salt)) |
| Hardware profile | `EMORI_HARDWARE_PROFILE=reachy-mini-wireless` in `.env` | Supplies the tuning measured on that robot (session start, expression cues, turn-taking), and what an installed add-on contributes for it |
| Hardware identity (board serial, Wi-Fi MAC) | `/opt/emori/ROBOT_IDENTITY` (not secret) | `install.sh` step 8c records it; label the robot with its id and the serial's last 6 characters |

## Before you start: a brand-new robot's first network

Deploy-to-Robot needs the robot on a network this computer can reach, with internet for
the full install. The robot's own recovery hotspot (`emori-<robot id>`) and the admin page
exist only AFTER the first install, so a robot out of the box joins its first network the
way Pollen's own setup does it (the Reachy Mini's onboarding: its setup hotspot and
dashboard, see Pollen's documentation), or this computer's own hotspot (Windows: Settings
-> Mobile hotspot), which the deploy also searches.

- [ ] The robot is on a network this computer is on, and that network reaches the internet.
- [ ] You know the `pollen` user's password (Pollen's documentation gives the factory
      one; change it if the robot will ever be reachable from outside the site). The
      first deploy asks for it once, to install its own SSH key.

## 0. v1 or v2

- [ ] **v2 is the default for a new robot**, and Deploy-to-Robot puts it in place after
      the install: the robot is a native ROS 2 node. The container `emori-ros2-v2` owns
      the motors and answers Pollen's daemon API on `:8000`, Pollen's daemon is fenced off
      the motor bus, and face look-at is armed. What the deploy does:
      [6. v2: what the deploy does](#6-v2-what-the-deploy-does); design and measurements:
      [ros2-v2-native-robot.md](ros2-v2-native-robot.md).
- [ ] **The first v2 deploy needs Docker with buildx on the deploy computer** (Docker
      Desktop on Windows and macOS). It builds the robot's image for arm64 from the
      checkout, about 45 minutes, and asks first; later robots reuse that image. Without
      Docker there, the robot is installed on v1 and works, and the last screen says so;
      deploy again from a computer with Docker to move it to v2.
- [ ] **v1** - Pollen's `reachy-mini-daemon` owns the motors (`install.sh` step 9 gives it
      EMORI's `--no-media` override) and the hostapp drives it over `:8000` - is for a
      robot that must run Pollen's own apps, SDK, dashboard or WebRTC, none of which run
      on v2. Deploy with `--v1`; the robot's `.env` keeps it (`EMORI_ROS2_STACK=v1`). The
      optional v1 Humble graph is `install.sh` step 11
      ([onrobot-runbook.md](onrobot-runbook.md)).

## 1. Get the robot's token from Pokamind

Pokamind creates the robot's record and its token in its robot registry; nobody outside
Pokamind can. Ask your Pokamind contact, or write to dev@pokamind.com, with the details
below.

- [ ] Pick the robot id: lowercase letters, digits and hyphens, 3-63 characters,
      unique among your robots, numbered at the END (`reception-01`, `-02`): the hotspot
      is named after it and keeps its end when it has to be cut. It becomes the robot's
      Pokamind learner `robot-<robot-id>@emori.local`.
- [ ] Give Pokamind what the robot will configure itself from (the table above):
      the robot id and a display name, your organisation, the course (if sessions must
      be tied to one), the languages in order of preference (and which is the kiosk's
      default), the pathways, and the modules in the order the spoken menu should offer
      them -- at most 6 per language, with a short spoken name and a few keywords for
      each (*contract 2*; without them the robot drafts a name from the module's title,
      which works by number but reads less well). Staff badge domains (for example `example.org`) go in
      the same request.
- [ ] Whatever the record leaves out, the robot takes from its defaults, and the admin
      page's Settings tab can set it afterwards: with no course, drop-in sessions run
      under the default course `emori`; with no default language, the kiosk boots in the
      record's first language that has modules. While Pokamind does not yet serve the
      default language or the spoken names, set the language on the Settings tab and save
      the menu on the Menu tab.
- [ ] You receive one line, `pkr1.<robot-id>.<key>`. Keep it secret: it is the robot's
      credential. Deploy-to-Robot asks for it in step 2. If it is lost or leaked, ask
      Pokamind to rotate it ([below](#replace-rotate-revoke)).

## 2. Deploy to the robot

- [ ] Double-click `Deploy-to-Robot.cmd` (Windows) or `Deploy-to-Robot.command`
      (macOS/Linux). It finds the robot, and asks for **"this robot's Pokamind token
      (pkr1....)"** - paste it (the input is hidden). Without a keyboard (scripts):
      `python scripts/deploy_to_robot.py --token-file token.txt`, and delete `token.txt`
      afterwards.
- [ ] **Check which robot answered.** Every fresh Reachy Mini is called `reachy-mini`, so
      the preflight prints the board's serial and Wi-Fi MAC, the robot's `EMORI_ROBOT_ID`
      and the robot of the token it holds, and its last question names them: compare with
      the robot's label before you say yes. (A token on the wrong unit logs that unit in
      as another robot.) First deploy of a robot: label it with its robot id and the
      serial's last 6 characters.
- [ ] Read the **robot clock** line. Pokamind refuses what a robot signs when its clock is
      more than 2 s ahead (learner logins: 60 s); a CM4 has no RTC battery. More than 2 s
      off, or NTP not synchronized: `sudo timedatectl set-ntp true` on the robot, and the
      site must let NTP (UDP 123) out.
- [ ] The token is checked locally, shipped to the robot, and **asked about there**
      (`deploy/robot/install-token.sh`: the same Pokamind whoami as the admin page)
      before it is installed:
      * Pokamind knows the robot, active, this key: installed as
        `/opt/emori/secrets/pokamind_robot_token`; `EMORI_ROBOT_ID` set from it if
        `.env` had none.
      * Pokamind refuses it (`robot_unknown`, `robot_revoked`, `whoami_auth_failed`,
        `whoami_key_mismatch`), or `EMORI_ROBOT_ID` names another robot: **DEPLOY
        STOPPED**, nothing installed or restarted, the robot keeps the token it had.
      * Pokamind cannot be asked (no internet yet, a timeout, 5xx, rate limit): on a robot
        with **no** token, installed with a loud warning; run Test connection (step 3) once
        the robot is online. On a robot that **has** a token, never: it keeps its working
        token, the new one waits staged (0600) on the robot, and the deploy says so
        loudly. The installer asks again; the next deploy (or a quick one) does too, or
        paste the token on the admin page (Pokamind -> Replace token). A token installed
        on the admin page after that wins: the staged one is then dropped.
      * Whenever a token replaces another, the old one is kept as
        `/opt/emori/secrets/pokamind_robot_token.prev` (0600): see "Roll back a token"
        below.
      * A first deploy has no Python environment on the robot yet: the token waits
        (0600) and the installer asks about it right after building one; the last screen
        says what became of it.
- [ ] **WRITE THIS DOWN** - the admin PIN, the recovery hotspot (`emori-<robot id>`)
      and the dashboard password, generated by the install and shown once: at the end of
      the deploy's output, after readycheck (an admin PIN Deploy-to-Robot generated is in
      its final summary). They stay in `/opt/emori/.env`. The admin page changes all
      three with the PIN, and reveals the hotspot and the dashboard password; a lost PIN
      is read from `/opt/emori/.env` over SSH.
- [ ] The course follows the robot's Pokamind record: leave `EMORI_COURSE_ID` empty in
      `/opt/emori/.env` and leave `course_id:` out of `configs/roster.yaml`. A value set
      by hand wins, and must then equal the robot's course, or those sessions are
      refused (`course_not_in_scope`); readycheck names the wrong one.
- [ ] Drop-in sessions run as the robot's own `robot-<robot-id>@emori.local`. A roster
      `anonymous:` block that pins another `@emori.local` account is ignored once the
      robot has a token (logged as a WARNING); one that pins a staff-domain account is
      used only if that domain is in the robot's `identity_domains`.

A robot that already has a token keeps it; `--replace-token` asks for a new one.

### Deploying from another network

Deploy-to-Robot needs SSH to the robot (port 22, user `pollen`); its search only covers
this computer's own networks. From anywhere else, give the route:

- [ ] **Recommended: Tailscale on the robot.** Once, on site (or over the robot's own
      hotspot): `curl -fsSL https://tailscale.com/install.sh | sh`, then
      `sudo tailscale up --hostname reachy-<robot-id>`. From then on:
      `python scripts/deploy_to_robot.py --host reachy-<robot-id>` from any computer on
      the tailnet. Restrict it with tailnet ACLs: only the deploy team's devices, only
      port 22 (and 8070/8090 if they need the admin page and dashboard). Do not make the
      robot a subnet router or an exit node, and leave Tailscale SSH off: Deploy-to-Robot
      uses its own key.
- [ ] **Or a jump host** on the robot's network (a machine you can SSH to):
      `python scripts/deploy_to_robot.py --host <robot's address as the jump host sees it> --jump you@jump-host`
      (`ssh -J`; `[user@]host[:port]`, comma-separated hops). The jump hop uses your own
      SSH keys and config; the deploy's key and its pinned robot host key
      (`~/.ssh/emori_known_hosts`) apply to the robot hop. When ssh reports a changed
      host key, the deploy offers to forget the robot's pinned key only if ssh names the
      robot; the jump host's key (or one ssh does not name) stops the deploy, and
      nothing is removed.
- [ ] **Or a forwarded SSH port**: `--host <public name> --port <n>`. Only with key-only
      SSH on the robot (`PasswordAuthentication no` in its sshd config) and `pollen`'s
      default password changed: a port 22 open to the internet is scanned within minutes.
- [ ] `--port` and `--jump` need `--host`, go on every SSH the deploy runs (key install,
      preflight, token, code, installer), and end with an SSH tunnel command for the
      admin page and the dashboard instead of plain-HTTP links: leave it running and open
      `http://localhost:8070`.
- [ ] Never open the robot's `:8000` (unauthenticated daemon), `:8070` or `:8090` (plain
      HTTP) to the internet. The first deploy of a robot still asks for `pollen`'s
      password once (to install the deploy key); that works over any of the routes.

## 3. Connect it on the admin page: Wi-Fi, token, automatic sync

- [ ] **Reach the admin page.** Not on the site Wi-Fi yet: join the robot's recovery
      hotspot (`emori-<robot id>`, the password from the install's last screen) and open
      `http://10.42.0.1:8070`; otherwise `http://<robot>:8070`. Sign in with the admin PIN.
- [ ] **Wi-Fi**: Network tab, add the site network.
- [ ] **The token**, if the deploy did not install it: Pokamind tab -> Replace token. The
      robot asks Pokamind first, writes its scope (course, languages, pathways, menu)
      and only then saves the token and restarts the host app, so it comes back fully
      configured.
- [ ] **The automatic sync**: the Pokamind tab's *Applied from Pokamind* card lists every
      value the record supplied, with its source; *Sync now* runs a sync at once. A new
      menu is live at once. Any other change waits for the host app's next restart,
      which emori-admin performs by itself after 10 idle minutes (never during a
      session); Settings -> Apply now does it at once.
- [ ] **Test connection.** Expect: credential *This robot's own token*, the robot id, its
      organisation, status `active`, and every module in the Scope card green.
- [ ] Menu tab: each language shows where its menu comes from (*Pokamind*, or *title
      draft* when Pokamind has no spoken names yet) and Preview speaks it.
- [ ] System tab: the robot serial and Wi-Fi MAC match the robot's label.

## 4. Run the readycheck

- [ ] Admin page -> **Health** -> **Run readycheck** (or `bash deploy/robot/readycheck.sh`
      on the robot). The text report starts with the robot's serial, Wi-Fi MAC and token.
- [ ] These gates must PASS: `pokamind_token` (`robot_token`), `pokamind_whoami`
      (`whoami_active`), `site_config` (`site_config_ok`, which lists where each value
      came from), `voice_menu_in_scope` (`menu_in_scope`), `session_chain`, and `clock`
      (`clock_ntp_synchronized`). `voice_menu` PASSes, or WARNs `voice_menu_auto_draft`
      while Pokamind has no spoken names for the modules (the menu works by number).
- [ ] `identity` PASSes once install.sh has recorded the board (`identity_recorded`).
- [ ] What a brand-new robot shows before anyone has used it, and is not a fault:
      `badge_session` SKIP `no_roster_pathway` until people are added on the People tab;
      `dashboard_data` SKIP `no_finished_session_yet`, then FAIL `dash_no_cycles_ever`
      after smoke-test sessions with nobody in front of the camera (analysis cycles come
      from a person in view), until the first real session. `audio_duplex` FAIL `mic_16k_unsupported`
      can come and go between runs on a Reachy Mini Wireless: a known code task, not a
      setting.

## 5. Smoke test

The readycheck proves the robot is configured. The smoke test proves the experiences
work end to end with this robot's own token: it calls Pokamind for real. Run it on the
robot once the readycheck is green, with nobody using the robot:

- [ ] the control plane and roleplay, in the kiosk's language (the default `--lang` is the
      kiosk's live one):

  ```bash
  ssh pollen@<robot> 'bash /opt/emori/deploy/robot/rig-smoke.sh --only control,roleplay'
  ```

  Without `--only`, the smoke test of each installed add-on runs after these
  (`addons/<name>/scripts/addon_smoke.py`; `rig-smoke.sh --addons` runs only those).

Other options: `--json`, `--strict` (exit 2 on a SKIP), `--start-timeout`/`--end-timeout`
(roleplay, 150/180 s), `--verbose`. Exit 1 means a check FAILed. Every line is
content-free: no token, SSO login, transcript or answer is ever printed.

**It is not read-only.** *roleplay*: the robot speaks and moves (greeting, the scenario
intro, the farewell and the spoken summary), one anonymous session lands on the
dashboard, and if Pokamind returns results the report is emailed to `EMORI_RESULTS_TO`.
It refuses to start while a session or an add-on mode is active.

| Line | PASS proves |
|---|---|
| `control.whoami` | Pokamind accepts this robot's control-plane assertion, signed with its token, and names its course and modules |
| `roleplay.start` | The first voice-menu module started as the anonymous learner: SSO login with the token, the Pokamind frame up, the learner's first turn armed |
| `roleplay.end` | The session page's end control (its `E` key, the same `end` a push of the head back sends) ended it, the kiosk is back at `/`, no session is active |

What it does not prove: the head gestures, a person's real speech, any menu option but
the first, and the staff badge (QR) path.

| You see | Likely cause | Do |
|---|---|---|
| `control.whoami FAIL ... control_auth_failed` | The token is not registered or revoked, the robot clock is off, or Pokamind's record of this robot's credential does not match the token | Admin page -> Pokamind -> Test connection; readycheck `clock`; ask Pokamind to check this robot's credential |
| `roleplay.start FAIL ... session_start_failed` | Pokamind refused the SSO login or never came up | readycheck `session_chain`; the host app journal |
| `roleplay.end FAIL ... still active` | Pokamind's end chain is slower than `--end-timeout` | The watchdog ends it; rerun with `--end-timeout 300` |

## 6. v2: what the deploy does

Deploy-to-Robot does Route A of
[deploy/robot/ros2_native/README.md](../deploy/robot/ros2_native/README.md), which has
the commands and their reasons: `scripts/deploy_to_robot.py` gives the robot its image,
then `deploy/robot/ros2_native/v2-stack.sh` switches it after `install.sh`. In order:

- [ ] **The image**, when the robot has none. It is built on the deploy computer from the
      committed checkout
      (`docker buildx build --platform linux/arm64 -f deploy/ros2/Dockerfile.v2.robot`),
      labelled with a hash of its sources, and pulled by the robot from a `registry:2` on
      that computer through an SSH reverse tunnel - never `docker save | docker load`,
      which does not fit on the card. A robot that has an image keeps it; the deploy says
      when that image was built from older sources, step 7 puts a layer on top of it, and
      `--rebuild-v2-image` builds and ships a new one.
- [ ] **Docker on the robot** when it has none (`docker.io`, `docker-compose`, `pollen`
      in the `docker` group): readycheck runs as `pollen` and asks docker about the v2
      container (without it: `ros2 SKIP docker_unreachable`).
- [ ] **Committed to v2**: `reachy-mini-daemon` (and a v1 `emori-ros2`) disabled, the
      fence drop-in `zz-v2-fence.conf` installed, `/opt/emori/V2_OWNS_THE_BUS` raised - the
      fence every deploy checks first - then
      `docker compose -p ros2 -f deploy/ros2/docker-compose.v2.robot.yml up -d`. Compose
      does not read `/opt/emori/.env`; any `EMORI_GAZE_*` tuning goes in
      `/opt/emori/deploy/ros2/.env`.
- [ ] **The check**: `:8000` must answer as `reachy_mini_ros2` within 180 s. If it does
      not, the deploy lowers the fence and starts Pollen's daemon again, so the motors
      always have an owner, and its last screen says the robot went back to v1.
- [ ] **Face look-at**: `EMORI_CAM_ROS_VIDEO_NR=41` in `/etc/default/emori-camera`, then
      the camera bridge and the kiosk restart. readycheck's `gaze` gate reads the number
      there (`EMORI_READYCHECK_ROS_VIDEO_DEVICE` only overrides it).
- [ ] Nothing to add to `/opt/emori/.env`. The motion owner stays `hostapp`, the default on
      both sides (the hostapp and the compose file), and readycheck checks the v2 container
      because the fence is up. `EMORI_WITH_ROS2=1` is no longer needed (it still works;
      only a robot left on trial, without the fence, needs it for that check).
      `EMORI_ROS_MOTION_OWNER=ros` is an option for motion that originates in the ROS
      graph, set on both sides (the README); measured on a Reachy Mini Wireless, with `ros`
      the robot stands still while it speaks (the hostapp drops every expression cue as
      `not_owner`, and the ROS lane makes one gesture per sentence).
- [ ] readycheck: `ros2 PASS ros2_up_v2`, and every gate of step 4 still PASSes. The
      stack step's own verdict is in `artifacts/last-ros2-stack.env` (`stack`, `changed`,
      `reason`) and on the deploy's last screen.
- [ ] Rolling back: deploy with `--v1`, which runs the README's "Rolling back to v1" and
      records `EMORI_ROS2_STACK=v1`; `--v2` moves the robot back to v2.

## 7. The ROS 2 hub

After the install and the v2 switch, the same deploy puts the robot on the hub
(`scripts/deploy_to_robot.py`, `finish_on_hub`). It needs Docker on the deploy computer. In order:

- [ ] **The motor layer**, when the motor image was not built from this checkout: one small
      layer on top of it (`scripts/build_hub_payload.py --layers motor`, then
      `deploy/robot/ros2_native/motor-overlay.sh --delta <dir> --apply --source <hash>`), which
      parks and wakes the motors. With nothing new for the motor, it is left alone. The first
      time, the deploy copies a build environment from the robot (`scripts/capture_robot_base.py`,
      about 1.7 GB over the network, some minutes), so the motor container must be running.
- [ ] **The hub image**, built on the robot from a payload made here
      (`deploy/hub/hub-build.sh`). The hub's add-ons are installed into it, offline, from each
      add-on's `deploy/hub-wheels/`.
- [ ] **The voice delay**: `EMORI_VOICE_DELAY_MS=100` in `/opt/emori/.env`, only if that file
      names no value.
- [ ] **The switch**: `deploy/robot/hub-stack.sh apply ros`, which raises `/opt/emori/HUB_IS_ROS`,
      stops the host app and starts the hub. Anything that goes wrong puts the host app back.
      It waits for no session: while one runs it refuses and changes nothing, so deploy again
      when the robot is idle.
- [ ] The last screen says how it ended, and `artifacts/last-hub-stack.env` keeps it. readycheck
      judges the hub.
- [ ] A robot already on the hub stays on it, and a deploy restarts it only onto an image built
      from the same code.
- [ ] **The way back**: `bash deploy/robot/hub-stack.sh apply hostapp` on the robot, or the
      admin page. The host app stays installed for this, fenced off while the flag is up.
      `--no-hub` keeps a robot on the host app for a deploy, and is refused on a robot already
      on the hub, whose code would change with no image built to match it.
- [ ] The last screen ends `DEPLOYED, BUT NOT ON THE ROS 2 HUB` when any of this did not finish,
      and the deploy exits non-zero. Deploying again picks up where it stopped.

## Replace, rotate, revoke

| You want to | Do |
|---|---|
| Install a new token on a robot | Admin page -> Pokamind -> **Replace token**. The robot verifies it with Pokamind first, saves it, restarts the host app (kiosk blank for a few seconds). Refused while a visitor session runs. Or: `Deploy-to-Robot --replace-token`, which also asks Pokamind first and keeps the old token on a refusal. |
| Rotate (lost or leaked token) | Ask Pokamind to rotate the robot's token. You receive a new token; the old one stops working within ~30 s. Install the new one right away (Replace token). |
| Take a robot out of service | Ask Pokamind to revoke the robot: its logins are refused (`robot_revoked`) within ~30 s. Pokamind can reactivate it later. |
| Change its modules / languages / organisation / features | Ask Pokamind to change the robot's scope. No change on the robot; the token stays the same. |
| See the token on the robot | Admin page -> Pokamind -> **Reveal** (PIN again; shown 30 s; the event is logged, the value is not). |
| Roll back a token | Every replacement (Deploy-to-Robot, the installer, the admin page) keeps the token it replaced as `secrets/pokamind_robot_token.prev` (0600, one generation). On the robot: `cd /opt/emori && cp secrets/pokamind_robot_token.prev secrets/pokamind_robot_token && sudo systemctl restart emori-hostapp`, then Test connection. Or paste the old token on the admin page (Replace token asks Pokamind first). |

## Upgrading a robot

### Onto a newer release

Every value a robot's `.env` sets explicitly wins over anything a release derives, so an
upgrade keeps the robot's own settings.

- [ ] Keep `/opt/emori/artifacts/superseded/` (where a deploy moves the files the new
      tree no longer ships) until the upgrade is proven.
- [ ] Review `/opt/emori/.env` by key **names** only, never values:
      `ssh pollen@<robot> "grep -o '^[A-Z_]*=' /opt/emori/.env | sort"`. Compare with
      `.env.example`. A key the robot can fill by itself (course, language, pathways,
      display name, the control URL, and on a Reachy Mini Wireless the tuning its hardware
      profile carries: session start, cues, turn-taking, plus any an add-on contributes)
      may stay -- an explicit value keeps winning -- or be emptied one at a time to let
      Pokamind or the hardware profile supply it. The robot signs only with its own
      Pokamind token: a credential line it no longer reads is ignored, and the host app
      names it (never its value) in a WARNING at start; delete it. Check these names in
      particular:
      * `EMORI_PATHWAYS`: an EMPTY line takes the record's pathways as the badge
        allowlist (a badge pathway outside it loses its claim instead of being refused
        by Pokamind). Set `EMORI_PATHWAYS=*` for "no allowlist".
      * `EMORI_COURSE_ID`: when it is empty and the record names no course, the course
        is `emori`.
      * `EMORI_SMTP_FROM`: there is no built-in sender. A robot with `EMORI_SMTP_HOST`
        set and neither `EMORI_SMTP_FROM` nor an e-mail address as `EMORI_SMTP_USER`
        sends nothing (each report waits in `artifacts/outbox/`; readycheck WARNs
        `smtp_from_unset`).
      * `EMORI_SESSION_START_MS` / `EMORI_SESSION_START_S`: a `.env` or
        `robot.local.env` from an older template sets `45000` / `45`, which would outrank
        the Reachy Mini Wireless profile's `120000` / `130`. `install.sh` empties exactly
        those two values on such a robot (also when they came with a `robot.local.env`)
        and prints `emptied ...`; any other value is kept.
- [ ] Deploy with Deploy-to-Robot (full or `--quick`). A deploy never overwrites a menu
      already in `configs/site/`. A menu an older tree kept in `configs/prod/` is moved
      to `configs/site/` first (never over an existing file, never one that names
      placeholder modules), and from then on it is a SAVED menu: it outranks the menu
      built from Pokamind (Menu tab -> Reset to Pokamind to follow the record instead).
      Do not deploy by hand with `tar`: it skips that step.
- [ ] readycheck: no worse than before; `site_config` shows `env` as the source of every
      key `.env` sets.
- [ ] Smoke test (step 5): control and roleplay PASS.
- [ ] **Rolling back.** Without a code change: `EMORI_SITE_AUTO=0` in `/opt/emori/.env`
      and restart the host app; nothing is then derived from Pokamind (values or menu),
      and the menu falls back to `configs/site/`, `configs/prod/`, the development menus,
      the built-in one. Completely: deploy the previous tree again.

### A robot without a token

This release signs every learner login and every control-plane call with the robot's
own Pokamind token, and with nothing else. A robot without one (none installed, or a
token file that does not parse) asks Pokamind for nothing: the kiosk serves demo
sessions only, and readycheck FAILs `pokamind_token_missing`
(or `pokamind_token_invalid`).

- [ ] Install the token: admin page -> Pokamind -> **Replace token** (no SSH needed),
      or `Deploy-to-Robot --replace-token`.
- [ ] Test connection, then readycheck: `pokamind_token PASS robot_token`.
- [ ] If `EMORI_ROBOT_ID` in `/opt/emori/.env` names another robot than the token
      (readycheck `robot_id_mismatch`) and the robot already has dashboard data: when
      `EMORI_DASH_SALT` is empty, first set it to `emori-dashboard-fallback:` followed by
      the OLD id exactly as written. The dashboard derives its person codes from it;
      without this every returning learner looks new. Then set `EMORI_ROBOT_ID` to the
      token's robot id. Never delete the line of a robot that has been in use.

## Troubleshooting by code

Pokamind refusal codes appear in the readycheck `session_chain` / `badge_session`
hints ("Pokamind said ..."); whoami codes in `pokamind_whoami`, on the Pokamind tab, and
in Deploy-to-Robot's token check.

| Code | Means | Fix |
|---|---|---|
| `robot_unknown` | Pokamind has no robot with this id | Wrong token, or `POKAMIND_BASE` points at another Pokamind service than the one that issued it |
| `robot_revoked` | The robot was revoked | Ask Pokamind to reactivate it, or to issue a new robot |
| `invalid_assertion` | Signature, audience or lifetime refused (learner login) | The token was rotated (install the current one), or the robot clock is wrong (readycheck `clock`) |
| `whoami_auth_failed` | Pokamind refused the robot's signature (control plane, Test connection, Replace token, Deploy-to-Robot) | As `invalid_assertion`; or Pokamind's record of this robot's credential does not match the token: ask Pokamind to check this robot's credential |
| `whoami_unreachable` | No answer from Pokamind itself: no connection, or a captive portal, site proxy or firewall answered (a 401/403 without Pokamind's JSON is never read as a refusal) | The robot's network path to `POKAMIND_BASE`; the token is not in question. Deploy-to-Robot keeps a robot's working token and stages the new one |
| `whoami_key_mismatch` | Pokamind holds a newer key | Install the current token (Replace token) |
| `module_not_in_scope` | A menu or badge module is not in the robot's scope | Fix the menu, or ask Pokamind to add the module to the robot's scope; readycheck `voice_menu_in_scope` names the ids |
| `pathway_not_in_scope` | A roster badge pathway is not in scope (also the People tab's refusal) | Pick a pathway from the People tab's list, or ask Pokamind to add it |
| `course_not_in_scope` | `EMORI_COURSE_ID` or the roster `course_id` is not the robot's course | Empty `EMORI_COURSE_ID` and drop the roster `course_id` (the course then follows the token), or set them to the robot's course (readycheck `course_id_not_in_scope` names which) |
| `language_not_in_scope` | The kiosk language is not allowed for this robot | Switch the kiosk language (admin Language tab), or ask Pokamind to add the language |
| `identity_not_in_scope` | A badge (or a roster `anonymous:` staff-domain account) whose domain is not in `identity_domains`, or an account of another organisation | Ask Pokamind to add the staff email domain; never use another organisation's account |
| `robot_id_mismatch` (readycheck, token check) | `EMORI_ROBOT_ID` names another robot than the token | Install the right token, or set `EMORI_ROBOT_ID` to the token's robot id - pinning `EMORI_DASH_SALT` first ("A robot without a token" above). Do not delete the line |
| `pokamind_token_missing` (readycheck) | The robot has no Pokamind token: the kiosk serves demo sessions only | Install this robot's Pokamind token: admin page -> Pokamind -> Replace token, or Deploy-to-Robot |
| `pokamind_token_invalid` (readycheck) | The token file is damaged | Replace token |
| `anonymous_identity_not_this_robot` (readycheck WARN) | A roster `anonymous:` block makes drop-ins log in as a staff-domain account | Fine if that domain is in `identity_domains`; otherwise delete the block, restart the host app |
| `token_not_in_force` (readycheck) | A new token is saved but the host app was not restarted | Services -> Host app -> Restart |
| `whoami_not_supported` | The Pokamind service this robot talks to predates per-robot tokens | Check `POKAMIND_BASE`; ask Pokamind |
| `clock_ahead_of_pokamind` / `clock_behind_pokamind` (readycheck FAIL) | The robot clock is outside what Pokamind accepts for signed calls (2 s ahead / 47 s behind; an add-on may tighten this) | `sudo timedatectl set-ntp true`; let NTP (UDP 123) out of the site network; `timedatectl timesync-status` |
| `ntp_unsynchronized` / `clock_skew` (readycheck WARN) | NTP has not synchronized the clock, or the skew cannot be ruled out | As above, before the next power-off makes it a FAIL |
| `clock_unmeasurable` (readycheck WARN/SKIP) | Pokamind's answer came from a cache (`Age` > 0) or through a redirect, so its `Date` is not Pokamind's clock now and the skew was not measured; NTP does not say synchronized either (with NTP synchronized the gate PASSes) | Get NTP synchronized (as above); if a site proxy caches or redirects `POKAMIND_BASE`, have it pass Pokamind through |
| `voice_missing` (readycheck WARN, `tts` gate) | A Piper voice this robot speaks could not be downloaded (the installer warned and went on): that language is read by the first voice | Once online: `cd /opt/emori && bash scripts/get_voice_models.sh`, then restart the host app (or deploy again) |
| `identity_card_moved` (readycheck WARN) | The card runs in another robot body than the one install.sh recorded; that body logs in as the card's robot | Intended: deploy again (records the new board). Not: put the card back, give this body its own token |
| `site_config_ok` (readycheck PASS) | Every robot-local setting has a usable value; the detail lists each one as `KEY=source` (names, never values) | Nothing |
| `pokamind_base_invalid` (readycheck FAIL) | `POKAMIND_BASE` is not an https origin (http only for a loopback address) | Fix it in `.env`, or on the admin page (Settings -> Server) |
| `site_settings_invalid` (readycheck FAIL) | A value saved on this robot fails its check and is NOT applied; the key is named, never the value | Save it again on the admin page, or remove the line from `configs/site/settings.env` |
| `site_restart_pending` (readycheck WARN) | A change waits for the host app's next restart; the hint names the reasons | `scope_changed` (from Pokamind; an add-on may add reasons): nothing, emori-admin restarts the host app after 10 idle minutes. `settings_saved` (entered on the admin page): Settings -> Apply now. A reset dashboard password shows on the admin page's *Changes waiting* banner, not in readycheck |
| `scope_not_synced` (readycheck WARN) | The robot has a token but has never synced its Pokamind scope | Pokamind tab -> Sync now; check the robot reaches `POKAMIND_BASE` |
| `scope_stale` (readycheck WARN) | The last good scope is older than 7 days; the robot keeps working from it | As above |
| `scope_robot_mismatch` (readycheck WARN) | The scope on disk belongs to another robot or an older token, and is ignored | Sync now: the first successful sync replaces it |
| `smtp_from_unset` (readycheck WARN, `site_config`) | `EMORI_SMTP_HOST` is set but there is no sender (`EMORI_SMTP_FROM`, or an `EMORI_SMTP_USER` that is an e-mail address): no results e-mail is sent, each report waits in `artifacts/outbox/` | Set the sender on the admin page (Settings -> Results e-mail), then Send test e-mail |
| `partner_mismatch` (readycheck WARN) | `.env` sets `EMORI_PARTNER_NAME` to something other than the robot's record | Delete the line: the partner is a protocol constant, not a setting |
| `voice_menu_placeholder_ids` (readycheck FAIL) | The spoken menu names `example-`, `demo-module-` or `dev-` modules on a robot that talks to Pokamind: every pick would fail | Menu tab -> Reset to Pokamind, or remove the file the gate names |
| `voice_menu_site` / `voice_menu_from_pokamind` (readycheck PASS) | The spoken menu is the one saved on this robot (Menu tab), or the one built from its Pokamind record | Nothing |
| `voice_menu_auto_draft` (readycheck WARN) | The menu was drafted from module titles: Pokamind has no spoken names for those modules yet | Works by number. Ask Pokamind for spoken names and keywords, or edit them on the Menu tab |
| `admin_pin_unset` (readycheck WARN) | No admin PIN: every admin page route answers 503 | Deploy again (the installer generates one and shows it once), or set `EMORI_ADMIN_PIN` in `.env` and restart emori-admin |
| `voice_menu_modules_not_offered` (readycheck WARN, `voice_menu_in_scope`) | The menu built from the record leaves modules out: more than six in the kiosk language, or modules with no language | Ask Pokamind to trim or fix the record, or save a menu of this robot's own on the Menu tab |
