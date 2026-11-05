# The robot's admin page (phone)

A phone-first management surface for a single Reachy Mini running EMORI: Wi-Fi
(including AP mode), speaker/mic volume, the EN/CA language toggle, the people who
have a QR badge (and printing those badges), this robot's Pokamind token and what its
Pokamind record sets, the spoken topic menu, the robot-local settings and credentials, service
restarts, a readycheck run, logs, system info, and reboot/poweroff. It runs as
its **own** systemd unit, `emori-admin.service`, deliberately independent of
the hostapp/kiosk/daemon it manages — so it is still reachable when one of
those is broken. This page is the day-to-day operator guide.

A robot configures itself from its Pokamind token: once the token is installed, the
robot reads its own Pokamind record and takes the course, languages, pathways, display
name and spoken menu from it, and generates what only it can know.
On this page a person enters only Wi-Fi, the token, people, and the few optional
overrides; every setting shows where its value came from.

## v1 and v2 robots: who does Wi-Fi and volume

On a **v1** robot the Network and Audio tabs drive Pollen's reachy-mini daemon
(`:8000 /wifi/*`, `/api/volume/*`), and the daemon raises the hotspot at boot
when no known Wi-Fi is in range. On a **v2** robot the ROS 2 node owns `:8000`
and its REST shim has none of those routes, so `emori-admin` does the same work
itself, by the daemon's own rules: Wi-Fi through `nmcli` (scan, join with an
automatic fall-back to the hotspot, forget, start hotspot, last error), the
speaker/mic hardware level through ALSA's `amixer` (same sound card, same
controls), a real test sound through `pw-play`, and the boot-time hotspot
fallback. The page, the Wi-Fi watchdog and the persisted levels behave the same
on both, and there is nothing to configure: which one is in force is decided
automatically (`EMORI_ADMIN_HOST_BACKEND=auto`, see
[configuration.md](configuration.md)) and logged once:

```bash
journalctl -u emori-admin | grep "Wi-Fi/volume backend"
#   ... Wi-Fi/volume backend: host -- emori-admin runs Wi-Fi (nmcli) and volume (amixer) itself (...)
```

On v2 the root-only `nmcli` calls run as `sudo -n nmcli ...`, each one
allowlisted in `deploy/robot/sudoers-emori-admin` (installed by `install.sh`);
the ones that carry a Wi-Fi password are kept out of sudo's own command log.

Four guardrails keep a v2 robot from calling a Wi-Fi or volume route its `:8000`
does not serve (the Network tab would answer "HTTP 404", and the robot would raise no
hotspot at boot):

- **CI**: `tests/test_daemon_contract.py` enumerates every HTTP call the code makes to
  `:8000` and fails unless the v2 REST shim serves it, emori-admin's own backend
  (`netman`/`mixer`) answers it, or it is an allowlisted v1-only call whose absence is
  proven harmless. A call through `network._wifi`/`audio._volume` must be in
  `netman`/`mixer` (those wrappers never ask the shim on v2). It also fails on any way
  to `:8000` it cannot read: a file that reaches the daemon and is not in its `DOORS`,
  a use of the address that is neither a call nor a listed `DOOR_USES` text, a daemon
  URL whose path is not literal, `probes.daemon` passed around as a value, and a
  `:8000` reference under `scripts/`/`deploy/` that is not in `SCRIPT_DOORS` (a
  robot-side shell route must be served on v2 too). A new daemon-only call cannot merge.
- **On the robot**: `emori-readycheck`'s `admin` gate asks the running emori-admin
  (`GET /healthz/deep`, `EMORI_ADMIN_TOKEN` -- sent only to a `/healthz` that says it is
  emori-admin -- no PIN) to read Wi-Fi status and the speaker level through the backend
  the phone would use, and on v2 to ask sudo (`sudo -n -l`, which runs nothing) whether
  its Wi-Fi changes would be allowed. A missing route or tool is
  `admin FAIL admin_wifi_backend_missing`, a refused sudo grant
  `admin FAIL admin_wifi_privileges_missing`, an emori-admin older than
  `DEPLOYED_COMMIT.txt` `admin FAIL admin_stale`; anything transient is a WARN. A full
  deploy restarts emori-admin and the hostapp onto the new code (`install.sh`).
- **Version skew**: CI proves every call against the shim's *source*, but a robot runs the
  node (the `emori/ros2-jazzy-v2:robot` image or the native pixi env) or Pollen daemon it
  was last shipped, which can be older than the code beside it. So the contract test also
  writes `src/emori_adapter/hostapp/daemon_routes.json` -- every route the code needs from
  `:8000`, and on which flavour -- and readycheck's `daemon_routes` gate asks the RUNNING
  `:8000` for each one without calling any: one `GET /openapi.json` (Pollen's daemon and
  the v2 shim both publish it), then `OPTIONS` on anything the table does not list (405 =
  served, 404 = missing; Starlette answers before any handler runs). On v2 it asks for
  the node's routes, not the Wi-Fi/volume ones emori-admin answers itself; on v1 for
  Pollen's, not the node's own `/api/emori/*`. A missing route is
  `daemon_routes FAIL daemon_route_missing`, naming it and the fix.
- **On the phone**: a failing Wi-Fi/volume backend is never shown as a status code. The
  tab says what broke and lists the way back in (the robot's hotspot, the `nmcli`
  command over SSH, `EMORI_ADMIN_HOST_BACKEND=auto` for a missing route, restarting
  emori-admin). Any other unexplained failure is a sentence plus a log id; the same id
  is in `journalctl -u emori-admin` (Logs tab, Admin).

## Quick reference

| Where you are | URL |
|---|---|
| Same LAN as the robot (Wi-Fi/Ethernet) | `http://reachy-mini.local:8070` |
| Joined to the robot's own hotspot | `http://10.42.0.1:8070` |
| Neither (know the IP) | `http://<robot-ip>:8070` |

`reachy-mini.local` is the robot's own mDNS hostname (works from most phones
and laptops without typing an IP). If your device's browser has an mDNS
network-service picker, the service also advertises itself as **"EMORI
Admin"** (`deploy/robot/emori-admin.avahi.service`, `_http._tcp` on 8070) —
that only helps on a real LAN, never on the robot's own AP, since the AP has
no other services to browse.

Login is a 6-digit PIN (`EMORI_ADMIN_PIN`). `install.sh` generates it and shows it
once; **Settings → Credentials → Change** replaces it (the current PIN is required).
There is no username and no "forgot PIN" flow: a forgotten PIN means SSHing in (or
using the physical console), reading or editing `EMORI_ADMIN_PIN` in
`/opt/emori/secrets/admin.env` (a PIN changed on this page lives there and outranks
`.env`) or, when that file has none, in `/opt/emori/.env`, then `sudo systemctl
restart emori-admin`.

## First-time provisioning

`deploy/robot/install.sh` installs the systemd unit, the mDNS advert, and a
narrow sudoers grant (`deploy/robot/sudoers-emori-admin`, validated with
`visudo -c` before it ever touches `/etc/sudoers.d/`).

**Step 8 generates the machine secrets into `.env` *before* step 9 starts any
unit** — that ordering is load-bearing, not cosmetic: both services read `.env`
once at start (the hostapp through its unit's `EnvironmentFile` and its own import;
`emori-admin` loads it itself) and `enable --now` is a no-op on an
already-running unit, so a secret written after the hostapp started is a secret
the hostapp does not have until something restarts it. Generated for you (each
only when it is not set already; the ones marked *shown once* appear at the end of
the install's output, so write them down):

1. **`EMORI_ADMIN_PIN`** (*shown once*) — six random digits. Until a PIN is set every
   `/api/*` route on port 8070 answers `503 "not configured"` (fail closed), and
   readycheck WARNs `admin_pin_unset`. Change it later on this page.
2. **`EMORI_ADMIN_TOKEN`** — the shared secret between `emori-admin` and the
   hostapp's `/api/admin/*` routes. You never type or remember this one; it
   only moves between two processes on the same box.
3. **`EMORI_ADMIN_SALT`** — the admin cookie's HMAC salt. It must be
   high-entropy: the cookie is signed with a key derived from the salt **and
   the 6-digit PIN**, so a *guessable* salt would let anyone who captures one
   cookie off the plain-HTTP LAN/AP try all 10^6 PINs offline and keep the PIN
   itself. (If it is ever unset, `emori-admin` mints and persists a random one
   in `artifacts/admin_cookie_salt` and warns; the env value is still the
   right answer.)
4. **`EMORI_AP_SSID` / `EMORI_AP_PASSWORD`** (*shown once*) — the robot's own recovery
   hotspot. See "The Wi-Fi watchdog" below for why the robot refuses to raise an AP
   automatically without them. Revealed or changed on this page later.
5. **`EMORI_DASH_PASSWORD`** (*shown once*) — the management dashboard's password;
   revealed or reset on this page later. Its salt is never shown.
6. **`EMORI_HARDWARE_PROFILE=reachy-mini-wireless`** — marks an installed robot: its
   tuning measured on that robot (and whatever an add-on contributes) comes from it.
7. **`EMORI_SKILL_KEY`** — inert until the ROS skill intake is on. (`EMORI_ROS_ID_SALT`
   is never generated: unset, the ROS lane's pseudonyms change at every restart, and a
   fixed salt is a data-protection decision a person makes.)

A value changed on this page (the PIN, the hotspot, the dashboard password) is
written to `/opt/emori/secrets/admin.env` (0600, directory 0700), which both units read
after `.env` and which therefore wins; install.sh never regenerates or prints a value
that file holds. The hostapp's own settings saved on this page live in
`configs/site/settings.env` and `secrets/site.env`
([configuration.md](configuration.md#where-each-setting-comes-from)).

After editing `.env` by hand: **`sudo systemctl restart emori-admin
emori-hub`** — both, not just the admin service (`emori-hostapp` on a robot that
is on the way back). `EMORI_ADMIN_TOKEN` is
read by *both* processes at start, and restarting only one of them is what
makes the Language tab answer "hostapp admin disabled". (Restarting either program
also restarts the kiosk, `PartOf=`.)

Also optional in `.env`: `EMORI_ADMIN_WIFI_WATCHDOG` /
`EMORI_ADMIN_WIFI_GRACE_S` (mid-day Wi-Fi-loss auto-recovery, on by default) and
`EMORI_SITE_AUTO_APPLY_IDLE_S` (the automatic apply, below).

Then, on site, from a phone: **hotspot → PIN → Wi-Fi → token → automatic**, the
on-site flow below.

## On-site flow (a deployed robot, nothing but a phone)

This assumes the robot has had its first deploy
([deploy-a-new-robot.md](deploy-a-new-robot.md)): the hotspot and this page are
installed by it.

1. **Power on.** If the robot has no Wi-Fi it already knows, it brings up its
   own hotspot automatically at boot (`ensure_wifi_on_startup()`: the
   reachy-mini daemon's on v1, with no admin service involved; `emori-admin`'s
   on v2, where there is no daemon — allow up to two minutes after power-on).
2. **Hotspot.** Join it from your phone: the SSID and password are the
   `EMORI_AP_SSID` / `EMORI_AP_PASSWORD` pair install.sh printed once. On a robot where
   those were never set it is instead the daemon's stock `reachy-mini-ap` /
   `reachy-mini` — which also means the Wi-Fi watchdog on that robot is not armed;
   see below.
3. **PIN.** Open `http://10.42.0.1:8070` and enter the admin PIN install.sh printed.
4. **Wi-Fi.** Network tab → scan → join the site Wi-Fi. Before it fires, the UI
   shows an interstitial explaining what happens next: the robot switches
   networks, so *your phone will drop off the hotspot mid-flow* — that is
   expected, not a failure. Rejoin your normal Wi-Fi/data and reload the
   page once the robot has had a few seconds to connect. The connection card
   then shows the site SSID, an IP, and internet reachable. If it instead reverts
   to hotspot mode, check **Network → last error** — the robot reverts to its
   hotspot by itself on a failed connect (the daemon on v1, `emori-admin` on v2),
   by design, so a bad password never strands the robot off-network.
5. **Token.** Pokamind tab. When the deploy installed the token already, **Test
   connection**: Pokamind must answer with this robot's id, its organisation and a
   green module list. Otherwise **Replace token** (the PIN again): the robot checks the
   token with Pokamind, records its Pokamind record, saves the token and restarts the
   host app, which comes back configured.
6. **Automatic.** Nothing to type: the Pokamind tab's *Applied from Pokamind* card lists
   what the record set (course, pathways, name, start-up language), the Menu
   tab shows the spoken menu built from the record's modules, and the Language tab
   offers the record's languages (less any it has no module in). A change Pokamind made
   that needs a host app restart is applied by itself once the robot has been idle for
   10 minutes, or at once with **Apply now**.
7. **Optional.** People tab: add people and print badges. An installed add-on may add a
   tab of its own (see its documentation).
8. **Health tab → Run readycheck.** Fix anything red before leaving site. **Audio tab →
   Play test sound**, and set the speaker for the room.

The Home tab's **Site setup** checklist follows the same steps, read live from the robot
(Wi-Fi, token verified, record synced, the menu's source per language, people, any
add-on's step, the last readycheck -- one run from Health, or else the one the last
Deploy-to-Robot ran on the robot); nothing is remembered on the phone.

## What the tabs do

| Tab | Purpose |
|---|---|
| Home | One glance: network/services/system/Pokamind/hostapp status, refreshed every 5s; the **Site setup** checklist (every 30 s, from live state only) and a banner when changes wait for a host app restart. |
| Network | Current connection, scan + join, forget saved networks, force hotspot mode. |
| Audio | Speaker/mic volume (0–100, applied immediately — through the daemon on v1, to the sound card's mixer on v2 — and **persisted**: a reboot re-applies them, so the speaker can never come back at 0), plus the advanced session/idle mic-gain multipliers (`EMORI_MIC_SESSION_VOL`/`EMORI_MIC_IDLE_VOL`, see `.env.example`). |
| Language | EN/CA toggle. Proxies the hostapp's gesture-equivalent sequence (`lang_state.set` → `driver.default_lang` → kiosk reload → spoken confirmation) — never just flips a flag. Offers only the languages this robot's Pokamind record allows (a session in another would be refused). Returns "busy" while a session is active; try again once idle. |
| People | The badge roster: add a person (name + work email, and a pathway from this robot's Pokamind pathways when it has some), edit, remove, and print their QR badge (A4 sheet, or one badge as SVG/PNG). Live at once, no restart. See "Add people and print badges" below. |
| Pokamind | This robot's own Pokamind token: robot id, org, status, fingerprint, masked token, last verified; its scope (course, languages, pathways, modules with availability); **Applied from Pokamind** (what the record sets on the robot) with **Sync now**. **Test connection**, **Replace token** (kiosk restarts), **Reveal** (PIN again, 30 s). See "The Pokamind tab" below. |
| Menu | The spoken topic menu per language: where it comes from, its options (name, keywords, order), this robot's modules and why each is or is not offered. **Save**, **Reset to Pokamind**, **Preview**. See "The spoken topic menu" below. |
| Settings | Robot-local settings, each with where its value comes from: results e-mail (with a test), analytics, data retention, the Pokamind server (advanced, PIN), and the credentials (PIN, hotspot, dashboard password). **Apply now**. See "Settings and credentials" below. |
| Services | Restart/start/stop the 4 allowlisted units (hostapp, kiosk, camera-bridge, daemon) plus a self-restart of emori-admin. Restarting the hostapp restarts the kiosk with it (`PartOf=`) — the UI notes this. On a robot where the hub container runs the gateway (the flag file `/opt/emori/HUB_IS_ROS` exists) the list shows **Hub** in place of **Host app**, and every restart action in the admin targets it. Its restart is sent with `--no-block` and the page waits up to 90 s for `/healthz`, because the hub unit holds "started" until the gateway answers. While the hub is not running a **Back to the host app** button appears: it runs `deploy/robot/hub-stack.sh apply hostapp` (with `--with-motor`, which parks and wakes the robot, when the motor container runs the hub's graph), so a site with no ssh has a way out. It is refused while the hub answers. |
| Health | Runs `emori-readycheck` and shows the same gate list you'd get on the console, PASS/WARN/FAIL/SKIP with hints. |
| Logs | `journalctl -u <allowlisted unit> -n <lines>` — no sudo needed; the unit has `SupplementaryGroups=systemd-journal`. |
| System | Uptime, CPU/mem/disk, thermal, deployed commit; reboot/poweroff behind a double confirm. |

## Add people and print badges

A person with a badge starts training as **themselves** (their own progress and
assigned training in Pokamind) instead of as the robot's anonymous drop-in learner.
The **People** tab manages who that is:

1. **Add a person** — name and work email, then **Add and make a badge**. The badge
   opens straight away.
2. **Print** — **Print** on one badge, or **Print all badges** for an A4 sheet (six
   badges a page, cut along the dashed lines). Print at **100 %** ("fit to page" off)
   on white paper, and keep the white margin around each code: the camera needs it.
   **SVG** saves the whole badge, **PNG** the code alone (for a badge template of your
   own: keep at least 4 code-squares of white around it, and print the code 4 cm wide
   or more).
3. **Use it** — the person shows the badge to the robot's camera on the start
   screen. It works as soon as it is added: nothing restarts.

**Edit** changes the name or email; the code stays the same, so reprint only if the
name changed. **Remove** stops the badge at once; the person's past sessions stay in
Pokamind. A lost badge: remove the person and add them again. The old code stays dead,
the new one is printed, and Pokamind still sees the same learner.

Who may be added:

* The email's domain must be one Pokamind allows **this robot** to log in
  (`identity_domains`, from the robot's whoami: for example `example.org`). Any other
  address is refused here, with the allowed domains, because Pokamind would refuse it
  at the badge scan (`identity_not_in_scope`) and a screenless robot would just look
  like it ignored the badge. When Pokamind cannot be asked (no internet, no robot
  token), the person is added with a warning: check the address yourself. A robot
  whose scope lists no domain admits nobody: ask Pokamind to add yours
  ([`deploy-a-new-robot.md`](deploy-a-new-robot.md)).
* `@emori.local` addresses are the robots' own accounts, never a person's.
* One person per email address. Names are 1–80 printable characters.
* A **pathway** is picked from this robot's Pokamind pathways (the picker is hidden when
  the record lists none); any other is refused (`pathway_not_in_scope`), because
  Pokamind would refuse the person's session. Editing a person without touching the
  picker keeps the pathway they have. The tab also shows the course badges sign in to.

What the badge carries: only an opaque id (`EMORI:` + 10 random letters and digits,
e.g. `EMORI:K7Q2M9XAT3`). It holds no name and no email. The roster on the robot maps it
to the person. The id is what logs the person in, so treat a badge like a door key.
Badges written by hand in `configs/roster.yaml` (`EMORI:example-001`) appear on the tab
and print the same way.

Under the hood: the hostapp edits **`configs/roster.yaml`** (or whatever `EMORI_ROSTER`
names; never `roster.example.yaml`, which the first change copies). The edit is
atomic and touches only the `staff:` block. Comments, `course_id` and an `anonymous:`
block are kept, and the running hostapp reloads the roster in place. A hand edit of the
file is picked up the next time the tab is opened. If the file no longer parses, the
tab says so and changes nothing until it is fixed over SSH. New people get a
`student_id` derived from their email (`s-<hash>`). Every route is proxied to the
hostapp's loopback-only `/api/admin/roster*`, like the Pokamind tab, so the PIN cannot
be skipped from the Wi-Fi. The journal records only that a person was
added/edited/removed and the new count, never a name, an address or a code.

The QR codes are drawn on the robot by `segno` (pure Python, works on the hotspot with
no internet). It arrives with a **full** deploy. A robot last updated with a quick
deploy shows *Badges cannot be drawn yet*: deploy again (full) once it is online.
Adding and removing people works either way.

## The Pokamind tab (this robot's own token)

Every robot logs its learners into Pokamind with its **own** token
(`pkr1.<robot-id>.<key>`), issued for that robot in Pokamind and scoped to one
org, one course, its languages and its modules. Provisioning is in
[`docs/deploy-a-new-robot.md`](deploy-a-new-robot.md); this tab is where you
look at it and change it afterwards.

* **Status card** — which credential the hostapp runs with (*this robot's own
  token*, or *none*: demo sessions only), the robot id, the org and status
  Pokamind reports, the key fingerprint (not a secret: it is what Pokamind shows
  for the same key), the masked token and when it was last verified.
* **Scope card** — what Pokamind lets this robot start, as of the last **Test
  connection**: course, languages, pathways and every module with a green/red
  dot. A red module is one the robot may not start (`not_found`,
  `not_visible_to_robot`, `language_mismatch`).
* **Test connection** — a live `GET /api/robot/whoami`, signed with the token. A good
  answer is also recorded as this robot's Pokamind record (the same as Sync now).
* **Replace token** — paste the new token, read the warning, type the admin PIN
  **again** and confirm (the token decides which Pokamind record configures the whole
  robot, so a phone left signed in is not enough). The robot
  first asks Pokamind whether the **new** token is valid and active; only then does
  it record the new robot's Pokamind record and spoken menus, save the token
  (`/opt/emori/secrets/pokamind_robot_token`, 0600) and restart the host app — the
  kiosk screen goes blank for a few seconds, and the robot comes back configured.
  Refused while a visitor session or an add-on mode is running ("busy", same rule
  as the language switch).
* **Applied from Pokamind** — what this robot's Pokamind record sets on the robot,
  one row per value: the course, the pathways, the display name, the start-up
  language. Each row shows the record's value and whether
  it is the one in force (*Pokamind*), or overridden by `.env` or by a value saved on
  this page (a local value always wins). Below them: the contract the record came in,
  when it was last synced, the last sync's result, and any value of the record the
  robot ignored because it was malformed. A row marked *restart pending* takes effect
  at the host app's next restart.
* **Sync now** — reads the record again (the robot also does so 10 s after the host
  app starts and every 6 hours, retrying after 10 and 30 minutes when Pokamind cannot
  be reached; the last good copy is kept, so an offline robot keeps working). The
  answer says what changed. A new spoken menu is live at once; a change to another
  value waits for the host app's next restart (see "Applying changes" below). At most
  once every 30 seconds: a quicker press answers *Synced a moment ago* and asks nobody.
  When the record drops the language the kiosk is in, the kiosk moves to another
  offered language at once if nothing is running (otherwise at the next restart); an
  `EMORI_LANG` set on this robot is kept.
  `EMORI_SITE_AUTO=0` in `.env` switches every value derived from Pokamind off on
  this robot.
* **Reveal** — asks for the admin PIN **again**, then shows the full token for 30
  seconds and hides it (also when you switch tabs or lock the phone). The robot's
  journal records *that* it was revealed, never the value.

How it stays safe: `emori-admin` never stores the token. Every action is
proxied to the hostapp — the process that already holds it — through the
token-gated `/api/admin/pokamind*` routes, exactly like the language switch.
Unlike the language routes, those answer only callers on the robot itself
(loopback; anyone else gets `403 local_only`), so the PIN re-entry and the
wrong-PIN limit in `emori-admin` cannot be skipped from the Wi-Fi.

## Settings and credentials

The **Settings** tab holds the few robot-local settings a site may want to change.
Every setting shows where its value comes from, as a badge:

| Badge | Meaning |
|---|---|
| *saved here* | Saved on this page. Wins over everything below |
| *.env* | An explicit value in `/opt/emori/.env` |
| *Pokamind* | Taken from this robot's Pokamind record |
| *hardware* | The installed robot's hardware profile (`EMORI_HARDWARE_PROFILE`) |
| *default* | A shipped default |
| *restart pending* | Saved, but the running host app still has the old value |

A saved value needs a host app restart before it is used (**Apply now**, below).
Emptying a box removes the value saved here; the `.env`, Pokamind or default value then
applies again. A value the robot refused at its start (malformed, or saved by hand in the
wrong file) is named in a red banner and is not used.

* **Results e-mail** — the SMTP server, port, user, password, sender (`From`), and the
  report recipients. Only for a site that wants the robot to e-mail its own engagement
  report; there is no built-in sender. The password is **write-only**: the page shows only
  whether it is set, and **Clear** removes it. The server, port and user need the **PIN
  typed again**, and changing the server or the user removes the saved password unless
  the same save sets a new one: a write-only password never follows a new server.
  **Send test** sends a fixed message with no learner data to one address, using the
  settings as saved (before Apply), and says why it failed (server unreachable, login
  refused, sender refused, ...); at most 5 tests per 10 minutes.
* **Analytics** — `EMORI_FIWARE`, the context broker and notify addresses, the tenant.
  Stored data is kept per tenant: change the tenant only for a new installation.
* **Data** — recording on the robot, and how many days records are kept (0 = always).
* **Hardware tuning (advanced)** — the values a Reachy Mini Wireless gets from its hardware
  profile (*hardware*): the session-start deadlines, expression-cue motion and the kiosk turn-taking patch, plus any values an add-on registers ([configuration.md](configuration.md#emori_hardware_profile)
  has each value and why). Change one only to correct this robot or this room; emptying the
  box goes back to the profile. A save that puts the deadlines out of order is refused (the
  robot's below the screen's).
* **Server (advanced)** — the Pokamind address (`POKAMIND_BASE`). It needs the **PIN typed
  again**, must be an address only (`https://host`, no path), and is saved only if Pokamind
  answers there, signed with this robot's current token (`base_not_verified` otherwise).
  Never auto-detected.

These settings need the PIN typed again wherever they are changed: the Pokamind address,
any setting an add-on marks as PIN-protected and the mail server, port and user. So does **Replace token** (Pokamind tab). A key emori-admin owns
(the PIN, the hotspot, the dashboard password) is never accepted by the generic save; it
has its own action:

### Credentials

Generated at install, revealed or changed here; each action asks for the **PIN again**,
counts a wrong PIN against the login limit (five a minute per phone, sixty in all), answers
`Cache-Control: no-store` when it carries a value, and leaves the event in
`journalctl -u emori-admin` (never a value). A revealed value is on screen for 30 seconds,
and is wiped when you switch tabs or lock the phone.

* **Admin PIN → Change** — the current PIN and the new one (six digits, different).
  Written to `secrets/admin.env`; emori-admin restarts. This phone stays signed in;
  every other phone signs in again.
* **Recovery hotspot → Reveal / Change** — the hotspot's name and password. A change
  (name 1–32 bytes, password 8–63 printable characters; an empty box keeps that half)
  is written to `secrets/admin.env`, and emori-admin restarts and renames the hotspot at
  its start. It never rewrites the hotspot your phone is standing on: changed while you
  are on it, the new name applies at the robot's next start on the site Wi-Fi. Write the
  new one down.
* **Dashboard password → Reveal / Reset** — Reset generates a new random password (16
  characters) and shows it **once**. The host app reads it at its start, so the change
  is applied by the next restart (automatically once the robot is idle, below), which
  signs every dashboard session out. The dashboard's salt is never shown or changed.

### Applying changes

The host app reads its settings when it starts, and never restarts itself. A change
that needs a restart shows a **Changes waiting** banner on Home and Settings, with the
reasons, and readycheck WARNs `site_restart_pending`.

* **Apply now** restarts the host app at once, only when the robot is idle (the host
  app's own answer: no session, no QR training lease, no add-on mode running; `busy` otherwise). The kiosk screen goes blank for a few
  seconds.
* **Automatically**: emori-admin checks every minute and restarts the host app by itself
  once the robot has been idle for 10 consecutive minutes, at most once every 30 minutes,
  and only for changes nobody typed: a value from Pokamind changed (`scope_changed`),
  a reason an add-on adds, or the dashboard password was
  reset (`dashboard_password`). Anything saved by a person on this page
  (`settings_saved`) waits for **Apply now**, and so does every
  reason pending with it. The journal line is `auto-applied <reasons>`.
* To switch the automatic restart off on one robot, set
  `EMORI_SITE_AUTO_APPLY_IDLE_S=0` in `/opt/emori/.env` and restart emori-admin; any
  other number is the idle time in seconds. **Settings → Applying changes** shows which
  is in force and what it is waiting for.

## Routes

Every route is behind the PIN cookie and the same-origin check. Those marked *proxy* are
answered by the hostapp's loopback-only `/api/admin/*` route of the same purpose, with the
shared admin token; emori-admin keeps no robot credential.

| Route | What it does |
|---|---|
| `GET /api/settings` | proxy of `GET /api/admin/site`; the admin-owned keys answered here (set and source, never a value); plus `auto_apply` (the watcher's state) |
| `POST /api/settings` | `{KEY: value \| null, "pin"?}`: proxy of `POST /api/admin/site`. `POKAMIND_BASE`, `EMORI_SMTP_HOST`, `EMORI_SMTP_PORT`, `EMORI_SMTP_USER` need `pin`; admin-owned keys answer `key_not_editable` |
| `POST /api/settings/apply` | restart the host app when `GET /api/admin/summary` answers `idle: true`; `409 busy` otherwise |
| `POST /api/scope/sync` | proxy: sync this robot's Pokamind record now (`sync_throttled` within 30 s of the last ask) |
| `GET /api/menu?lang=` | proxy: the served menu, its source, this robot's modules |
| `POST /api/menu/save` / `reset` / `preview` | proxy: `{lang, menu}` / `{lang}` / `{lang, menu?}` |
| `GET /api/audio/devices` | proxy: the audio devices, listed in a child process |
| `POST /api/results-email/test` | `{to}`: proxy, one test message |
| `POST /api/pin/change` | `{current, new}`; then emori-admin restarts |
| `POST /api/credentials/reveal` | `{which: "ap" \| "dashboard", pin}`; no-store, `visible_s` 30 |
| `POST /api/credentials/ap` | `{pin, ssid?, password?}`; then emori-admin restarts |
| `POST /api/credentials/dashboard/reset` | `{pin}`; the new password once, no-store; queues `dashboard_password` |
| `GET /api/setup` | the Home checklist, assembled from the routes above (cached 15 s) |
| `GET /healthz` | unauthenticated: `{status, service, pin_configured}` (readycheck's `admin` gate) |

## The Wi-Fi watchdog

`EMORI_ADMIN_WIFI_WATCHDOG=1` (default) starts a background loop in
`emori-admin` that polls the Wi-Fi backend's `GET /wifi/status` (the daemon's
on v1, `emori-admin`'s own on v2). If it reports `"disconnected"` continuously
for `EMORI_ADMIN_WIFI_GRACE_S` seconds (default 180), the watchdog asks that
same backend's `setup_hotspot` — never a separate `nmcli` call of its own, so
one busy lock still serialises every Wi-Fi change. This covers a Wi-Fi drop
**mid-day**, after the robot has been up and connected for a while; a drop at
boot time is handled by the boot fallback (see "v1 and v2 robots" above).
Set `EMORI_ADMIN_WIFI_WATCHDOG=0` to disable it (e.g. a site where a
disconnect should be investigated, not auto-recovered into a hotspot).

**It only arms when `EMORI_AP_SSID` *and* an ≥8-character `EMORI_AP_PASSWORD`
are set** (install.sh generates both). Without them the AP the robot would
raise is the vendor's: `reachy-mini-ap` / `reachy-mini`, hardcoded constants in
a pip-installable package. An *unattended* transition onto a network whose
passphrase is published gives anyone in radio range the daemon on `:8000`
(unauthenticated), the hostapp on `:8090`, this service, and — WPA2-PSK being
decryptable by anyone holding the passphrase — the operator's plain-HTTP admin
cookie. Somebody who can make the site Wi-Fi drop for three minutes could ask
for it on demand. When it refuses to arm it says so at WARNING in
`journalctl -u emori-admin`. The **manual** "Start hotspot now" button is
unaffected: that is a human standing in front of the robot choosing it.

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| `http://reachy-mini.local:8070` doesn't load, but the robot is on and clearly working (kiosk visible/audible) | Not on the same LAN, or mDNS didn't resolve from your device | Try `http://10.42.0.1:8070` if you can join the robot's hotspot, or find the LAN IP another way (site router, `arp`) |
| Every page answers 503 *not configured*; `curl http://localhost:8070/healthz` (SSH'd into the robot) says `"pin_configured": false`; readycheck WARNs `admin_pin_unset` | No usable `EMORI_ADMIN_PIN` (unset, or not exactly 6 digits) | Deploy again (install.sh generates one and shows it once), or set it in `.env`, then `sudo systemctl restart emori-admin` |
| Login always 401 with the right PIN | The PIN was changed on this page (it lives in `secrets/admin.env`, which outranks `.env`), or `.env` was edited and the service not restarted | Use the PIN set on this page; forgotten: read `EMORI_ADMIN_PIN` in `/opt/emori/secrets/admin.env` over SSH. After a hand edit, `sudo systemctl restart emori-admin` |
| **Apply now** answers *a session is running* | A visitor session or an add-on mode is on | Wait for the robot to go idle; the automatic apply also waits for that |
| The *Changes waiting* banner never goes away by itself | A change saved on this page (or a measurement) is pending: those wait for a person; or `EMORI_SITE_AUTO_APPLY_IDLE_S=0`; or the robot is never idle for 10 minutes | **Apply now** when the robot is idle. Settings → *Applying changes* says what the automatic apply is waiting for |
| Saving a new Pokamind address answers *Pokamind did not answer at that address with this robot's token* | The address is wrong, the robot cannot reach it, or that Pokamind does not know this robot (`base_not_verified` + the whoami code) | Nothing was changed. Check the address; the robot keeps the one it has |
| The hotspot kept its old name after **Change** | emori-admin never rewrites the hotspot your phone is on (it would cut you off) | It applies at the robot's next start on the site Wi-Fi, or restart emori-admin while the robot is on the site Wi-Fi |
| The old dashboard password still works after **Reset** | The host app reads the password at its start, and it has not restarted yet | It restarts by itself once the robot has been idle for 10 minutes; or **Apply now** |
| Language tab always returns "busy" | An add-on session or a training session is active | Wait for it to end, or check the Home tab's session status |
| Language tab 502s with `hostapp_down` | The hostapp is crashed/restarting | Services tab → restart `emori-hostapp.service`, wait, retry |
| Language tab says `hostapp admin disabled` (503) | The **hostapp** process has no `EMORI_ADMIN_TOKEN` — it started before the value reached `.env`, or `.env` was edited and only `emori-admin` was restarted | Services tab → restart **Host app** (or `sudo systemctl restart emori-hostapp`). It is reachable from the phone itself; no SSH needed |
| Language tab says `admin token unset` (503) | The **admin** process has no token: `EMORI_ADMIN_TOKEN` is empty in `.env` | Re-run `install.sh` (step 8 generates one), or set it by hand, then restart **both** units |
| Network → connect spins forever / status stuck on `busy` | Wi-Fi changes are fire-and-forget by design (NetworkManager itself waits up to 90 s for a join) | Keep polling (the UI does); if it never clears after ~2 min, check Network → last error, then Services → daemon status (v1) or Logs → emori-admin (v2) |
| Robot doesn't come back after a failed Wi-Fi join | It shouldn't — the robot reverts to its own hotspot on a failed connect. If it's still gone, whatever does that may be down: the daemon on v1, `emori-admin` on v2 | Services tab (if reachable) or SSH: `systemctl status reachy-mini-daemon` (v1), `systemctl status emori-admin` + `journalctl -u emori-admin -n 100` (v2) |
| Network tab: **scan** (or any Wi-Fi action) answers `http 404`; the Audio tab shows no levels | A v2 robot (the ROS node owns `:8000`; its shim has no `/wifi` or `/api/volume` routes) running an outdated `emori-admin` without the v2 host backend. readycheck FAILs `admin_wifi_backend_missing` on the robot, and the page says *"…asked the robot for a Wi-Fi or volume function the robot does not have…"* with the steps below | Deploy current code. `journalctl -u emori-admin \| grep "Wi-Fi/volume backend"` must then say `host`; if it says `daemon` on a v2 robot, set `EMORI_ADMIN_HOST_BACKEND=auto` (or `host`) in `/opt/emori/.env` and `sudo systemctl restart emori-admin`. No network in the meantime: join the robot's hotspot (`http://10.42.0.1:8070`), or over Ethernet/SSH `sudo nmcli device wifi connect "<SSID>" password "<password>"` |
| readycheck says `admin FAIL admin_wifi_backend_missing` | emori-admin's own Wi-Fi status or volume read, through the backend it would use for the phone, hit a route this robot does not serve (404), or the tool its host backend needs is missing (`nmcli_missing`, `alsa_missing`), or the running emori-admin is older than the checkout (no `/healthz/deep`; on v1 that is only `admin_deep_missing`) | Do what the hint says: `sudo systemctl restart emori-admin`; `EMORI_ADMIN_HOST_BACKEND=daemon` forced on a v2 robot → set it to `auto`; install `network-manager` / `alsa-utils`. Re-run readycheck until the gate passes |
| readycheck says `admin FAIL admin_stale` | The running emori-admin started before the last deploy: it reports the `DEPLOYED_COMMIT.txt` it read at start, and the file on disk is newer (or it is a build that does not report one). The phone is talking to the previous deploy's code | `sudo systemctl restart emori-admin`, re-run readycheck. Both a full and a quick deploy restart it themselves |
| readycheck says `admin FAIL admin_wifi_privileges_missing` | v2: sudo refuses the `nmcli` commands that change the Wi-Fi (`/etc/sudoers.d/emori-admin` missing or stale, no blanket passwordless sudo). Status reads still work; every join, the hotspot and the boot hotspot fallback fail | Re-run a full install (it validates and installs the fragment), or `sudo visudo -c -f /opt/emori/deploy/robot/sudoers-emori-admin && sudo install -m 0440 /opt/emori/deploy/robot/sudoers-emori-admin /etc/sudoers.d/emori-admin`. Check with `sudo -n -l nmcli device wifi rescan` as pollen |
| readycheck says `daemon_routes FAIL daemon_route_missing` | Version skew: what answers `:8000` does not serve a route the deployed code calls, so every such call answers 404 (the hint names them). CI cannot see this: it checks the shim's source, not the node the robot runs. **v2**: the robot node was not re-shipped with the code. **v1**: Pollen's reachy-mini daemon is a version without that route | **v2**, ship the node again: Docker route, rebuild and ship `emori/ros2-jazzy-v2:robot` and recreate `emori-ros2-v2` ([`ros2_native/README.md`](../deploy/robot/ros2_native/README.md), Route A); native route, re-run `deploy/robot/ros2_native/install-native.sh` (Route B), then `sudo systemctl restart reachy-mini-ros2`. **v1**: update reachy-mini in the daemon's venv (`/venvs/mini_daemon`), `sudo systemctl restart reachy-mini-daemon`. Re-run readycheck. `daemon_routes FAIL route_manifest_missing`/`route_manifest_invalid`: the route list that ships with the code (`daemon_routes.json`) is gone or damaged, so deploy again |
| readycheck says `daemon_routes WARN daemon_route_fallback` | v1: Pollen's daemon does not serve some of its own `/wifi/*` or `/api/volume/*` routes. emori-admin answers them itself (netman/mixer), so the Network and Audio tabs still work, but the daemon is not the version the code expects | Update reachy-mini when convenient (as above) |
| readycheck says `daemon_routes WARN daemon_routes_unverified` | `:8000` publishes no `/openapi.json`, and an `OPTIONS` probe could not settle a route: a path parameter the probe's sample value did not fit (404), or no answer | Ask it by hand: `curl -si -X OPTIONS http://127.0.0.1:8000<path>` gives 405 if it is served and 404 if it is missing |
| readycheck says `admin WARN admin_deep_missing` | v1 (Pollen's daemon on `:8000`): the running emori-admin predates `/healthz/deep`. The phone still works (the daemon serves Wi-Fi and volume); readycheck just cannot check it | `sudo systemctl restart emori-admin` |
| readycheck says `admin WARN admin_backend_unavailable` / `admin_token_unset` / `admin_token_rejected` | Not a deploy defect: the daemon (v1) is restarting, NetworkManager or the sound card is slow, or `EMORI_ADMIN_TOKEN` is unset / was changed without restarting emori-admin, so readycheck could not ask | Re-run in a minute; for the token codes, `install.sh` generates `EMORI_ADMIN_TOKEN`, then restart **both** emori-admin and emori-hostapp |
| Network tab says *NetworkManager (nmcli) is not installed…* / *NetworkManager refused the request* (older builds: `nmcli missing` / `nmcli failed`), or Network → last error reads `… detail:sudo: a password is required` (v2) | `emori-admin` cannot run the `nmcli` call it needs as root: the sudoers fragment is not installed and this robot has no blanket sudo | Re-run `install.sh` and read its `sudoers-emori-admin` line; `sudo -l -U pollen` must list `/usr/bin/nmcli device wifi rescan` |
| Audio tab says *The sound tools (ALSA) are not installed…* / *The sound card did not answer…* (older builds: `alsa missing` / `mixer failed`) (v2) | No `aplay`/`amixer` (alsa-utils), or the USB sound card is not there | `aplay -l` must list `Reachy Mini Audio`; `amixer -c <card> scontents` must show a `pvolume` control |
| Speaker slider moves but nothing audible | Wrong output device selected upstream, or PipeWire not up yet | The Audio tab shows the output devices; check `loginctl show-user pollen -p Linger` (must be `yes` — see install.sh step 6) |
| Mic-related trouble on this page specifically | The admin service never opens a live mic meter on purpose (opening PortAudio a second time next to the hostapp's own capture stream can abort the process) | Use the Health tab's readycheck audio-duplex gate, or the daemon's own `/api/volume/microphone/current` (v1) / `amixer -c <card> sget Mic,0` (v2), as the source of truth instead |
| Logs tab empty/errors for a valid unit | `systemd-journal` group didn't take effect (unit not restarted after a fresh install) | `sudo systemctl restart emori-admin` once after first install |
| A topic option starts nothing — the robot goes quiet and the journal says `session start failed (session_never_started)` | The menu offers a `module_id` Pokamind does not have, or will not start for this robot | Menu tab: the modules card says why each module is or is not offered. Health tab → readycheck; read the `voice_menu` and `voice_menu_in_scope` gates (see "The spoken topic menu"). With the robot's token, `voice_menu_in_scope` names the refused ids |
| readycheck says `voice_menu FAIL voice_menu_placeholder_ids` (or `voice_menu_built_in_default`, `voice_menu_example_file`) | The robot is speaking a development, built-in or example menu, whose module ids are placeholders: no Pokamind record has been synced yet, or a saved/override menu names placeholder ids | Pokamind tab → **Sync now** (the menu is then built from the record); Menu tab → **Reset to Pokamind** for a saved menu; remove an `EMORI_VOICE_TOPICS_<LANG>` override from `.env` |
| readycheck says `voice_menu WARN voice_menu_auto_draft` | The menu was built from the record, but some names were drafted from module titles (Pokamind has no spoken names for those modules yet) | It works by number. Check the names on the Menu tab (**Preview**), rename them there if they are hard to say, or ask Pokamind for spoken names and keywords |
| The Menu tab says *No verified Pokamind record yet* | The robot has no token, or has never synced its record | Install the token (Pokamind tab), then **Sync now**; check the robot reaches Pokamind |
| Menu **Save** answers *An option offers a module this robot may not start* | The menu names a module that is not in this robot's Pokamind record, or not available | Use the modules the modules card marks as offered; ask Pokamind to add the module |
| The smoke test (`deploy/robot/rig-smoke.sh`, over SSH) says `roleplay.start SKIP a session is already active` | Someone is using the robot, or a session is stuck | Wait, or end it by pushing the head back; the Home tab shows the session. The smoke test never interrupts a session ([`deploy-a-new-robot.md`](deploy-a-new-robot.md), "Smoke test") |
| The smoke test says `roleplay.start FAIL no kiosk page on the DevTools port` | The kiosk is down (it drives the kiosk browser through Chromium's local DevTools port, `EMORI_KIOSK_CDP_PORT`, default 9222) | Services -> restart Kiosk, then rerun `rig-smoke.sh --only roleplay` |
| Pokamind tab: *No Pokamind token* (readycheck `pokamind_token_missing`) | The robot has no token installed: it serves demo sessions only and asks Pokamind for nothing | Install this robot's Pokamind token: **Replace token** on this tab, or Deploy-to-Robot — [`deploy-a-new-robot.md`](deploy-a-new-robot.md) |
| Pokamind tab: *Robot id mismatch* | `EMORI_ROBOT_ID` in `.env` names another robot than the token | If the token is right, set `EMORI_ROBOT_ID` to the token's robot id and restart the host app (do not delete the line, and pin `EMORI_DASH_SALT` first: [`deploy-a-new-robot.md`](deploy-a-new-robot.md), "A robot without a token"); if the token is wrong, Replace token |
| Replace token answers *Pokamind did not accept that token* | The new token is revoked, rotated since, for another Pokamind server, or the robot has no internet; or Pokamind's record of this robot's credential does not match the token | Read the reason shown after it; nothing was changed on the robot. For a current token Pokamind still refuses, ask Pokamind to check this robot's credential |
| Pokamind tab: *A new token is saved but not in use yet* | The host app restart after Replace did not happen | Services → Host app → Restart |
| People tab refuses an email: *Pokamind does not let this robot log in people with that email domain* | The domain is not in the robot's `identity_domains` | Use the person's address at an allowed domain (shown under the form), or ask Pokamind to add the domain to the robot |
| People tab: *Badges cannot be drawn yet* | `segno` is not in the robot's venv (last update was a quick deploy) | Deploy again (full); or over SSH: `/opt/emori/.venv/bin/pip install -e "/opt/emori[hostapp]"`, then restart the host app |
| A printed badge is not recognised | The person was removed, the badge was printed before a re-add, or the code is too small or creased | Check the person is on the People tab; reprint at 100 % with the white margin intact |
| Reboot/poweroff button does nothing | Sudoers fragment failed `visudo -c` at install time and wasn't installed | Check the install.sh output for `sudoers-emori-admin failed 'visudo -c'` (or `visudo not found` — it lives in `/usr/sbin`, which a non-root Debian `PATH` omits); fix the file and re-run install.sh — or rely on the robot's existing blanket NOPASSWD if it has one |
| Any button answers `bad origin` (403) | The page was opened through something that rewrites the `Host` header, or a *different* page tried to drive the admin API | Open the admin UI directly at one of the URLs in the quick-reference table. The check is deliberate: `SameSite=lax` ignores the port, so without it a page on the robot's own `:8000` daemon could power the robot off using your session |

## The spoken topic menu

The robot's menu ("say one for …, two for …") maps a spoken number to a
Pokamind `module_id`. If that id is not a module Pokamind will start for this
robot, the option is a **silent** dead end: the robot speaks it, hears the
number, opens the session page, and the frame never comes up. Every watchdog
reports that as `session_never_started`, which several unrelated faults also
produce, so the robot builds the menu from what Pokamind says it may start, and
readycheck checks it before a learner finds a dead option.

A menu holds the opening sentence (the prompt), up to six options, and per option one
module id, a short spoken name and optional keywords (other words a visitor may say
instead of the number; a number always works).

### Where the menu comes from

Per kiosk language, the first of these that exists is served. The **Menu** tab shows
which one, as a badge:

| # | Source | Badge | Notes |
|---|---|---|---|
| 1 | `EMORI_VOICE_TOPICS_<LANG>`, then `EMORI_VOICE_TOPICS` (every language), in `.env` | *.env override* | wins over everything below; the page cannot change it |
| 2 | `configs/site/voice_topics.<lang>.yaml` | *saved here* | this robot's own menu, saved on the Menu tab (`en` or `ca`) |
| 3 | `configs/prod/voice_topics.<lang>.yaml` (`…/voice_topics.yaml` for `en`) | *fork menu* | a menu your fork of this repository ships; the public tree ships none |
| 4 | `configs/site/auto/voice_topics.<lang>.yaml` | *from Pokamind* / *title draft* | built from this robot's Pokamind record; only while derivation is on (`EMORI_SITE_AUTO` not `0`) and the record on disk is this token's |
| 5 | `configs/voice_topics.<lang>.yaml` → `configs/voice_topics.yaml` → `…example.yaml` | *development menu* | the repository's development and example menus: placeholder ids |
| 6 | the built-in default menu | *built-in placeholder* | placeholder ids |

Every box ranks the rows the same way. The simulator has no token, no saved menu and
no record, so it speaks row 5; its sessions are demo sessions and start nothing on
Pokamind.

`configs/site/` is gitignored and no deploy ships or overwrites it, so a saved menu and
the built one survive every deploy. The first deploy of this tree onto a robot whose
previous deploy shipped `configs/prod` menus adopts them as this robot's saved menu
(row 2, `deploy/robot/one-click-deploy.sh` step 2b); a fork that still ships
`configs/prod` keeps row 3 and adopts nothing. An adopted menu is a saved menu: it
outranks the menu built from Pokamind until you press **Reset to Pokamind**.

### The menu built from Pokamind

On every sync of this robot's Pokamind record (10 s after the host app starts, every 6
hours, **Sync now**, Test connection, a token install), the robot rebuilds row 4 for
each language it speaks, live, with no restart:

* the record's modules that are available, with no problem, and whose language is the
  menu's, **in the record's order**, six at most (a seventh and later are listed as
  *not offered*);
* the modules already on the menu keep their order and a new module is added after
  them; when a module leaves the scope, every later option moves up a number. The robot
  speaks the whole menu every time, so nobody has to remember a number;
* the spoken name and keywords are the record's own when Pokamind has them; otherwise
  the robot drafts a name from the module's title (cut at the first `,` `:` `;` `(` or
  ` - `, 40 characters at most) and keywords from the name's words. A menu with drafted
  names is badged *title draft* (or *Pokamind + title draft*) and readycheck WARNs
  `voice_menu_auto_draft`: it works by number, but check the names are easy to say;
* the prompt is written from the names in the menu's language.

The **modules** card lists every module of the record for the language, with its
number when it is offered, or why it is not (*not available*, *not visible to this
robot*, *its language does not match*, *more than six modules*, …).

### Saving and resetting

On the Menu tab, pick the language, then:

* rename an option, edit its keywords (comma-separated), move it up or down, remove it,
  or **Offer another module** from the record's available ones; **Write it from the
  names** redrafts the opening sentence;
* **Preview** — the robot speaks the opening sentence of the menu as it is on the page
  (refused while a session is running);
* **Save** — checks the menu (one to six options, names and keywords the speech
  recogniser can tell apart) and that every module is in this robot's Pokamind record
  and available, then writes `configs/site/voice_topics.<lang>.yaml` and serves it at
  once. A problem is listed per option and nothing is written. A robot without a synced
  record cannot save (it could not check the modules);
* **Reset to Pokamind** — removes the saved menu; the built one is served again at once.

A saved menu outranks the built one until it is reset, so a later change in Pokamind
(a new module, a new spoken name) does not reach a robot with a saved menu: reset it to
pick those up.

### Checking the menu

`emori-readycheck` checks the menu in two ways:

1. **Provenance** — from the filesystem alone: which source is served, and whether it
   can work. On a robot with a token, a menu naming placeholder modules (`example-…`,
   `demo-module-…`, `dev-…`) FAILs `voice_menu_placeholder_ids`; a built menu with
   drafted names WARNs `voice_menu_auto_draft`; otherwise the gate PASSes and says
   whether the menu came from Pokamind or was saved on this robot.
2. **Scope** — with the robot's token, the `voice_menu_in_scope` gate asks
   Pokamind (the same signed whoami as Test connection) whether every offered
   module is in the robot's scope and startable. `menu_in_scope` PASSes;
   otherwise `voice_menu_module_not_in_scope` or
   `voice_menu_module_unavailable` FAIL and name the ids.

Re-run readycheck whenever the menu changes.

## Rollback

The admin service has no dependents — nothing else on the robot requires it
to be running. To remove it entirely:

```bash
sudo systemctl disable --now emori-admin
sudo rm -f /etc/systemd/system/emori-admin.service
sudo rm -f /etc/avahi/services/emori-admin.service
sudo rm -f /etc/sudoers.d/emori-admin
sudo systemctl daemon-reload
```

The hostapp's `/api/admin/*` routes stay mounted but unreachable without the
shared token, and fail closed the same way if `EMORI_ADMIN_TOKEN` is ever
unset.
