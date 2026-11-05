# Configuration reference (`.env`)

Every EMORI process reads its settings from `.env` in its working directory
(`/opt/emori/.env` on a robot), loaded by `emori_adapter.config` with python-dotenv; the
host app's systemd unit also passes it as `EnvironmentFile`. emori-admin, the phone
admin service, loads it itself, without the credentials it does not hold (see below). [`.env.example`](../.env.example) is the
template: every variable, its shipped value and a one- or two-line comment. **This page
is the long form**: the rationale and the measurements behind each value. The sections
have the same headings in the same order as the template, and each template section
links to its section here.

Three rules hold for the whole file:

* **One assignment per name.** python-dotenv and systemd's `EnvironmentFile` both keep the
  LAST assignment, so a second `NAME=` line silently wins over the first
  (`tests/test_env_example.py` enforces this for the template).
* **A commented-out line (`#NAME=value`) documents the code default**; it changes nothing
  until you uncomment it.
* **Never commit a real `.env`.** Its secrets (`EMORI_SKILL_KEY`, `EMORI_ROS_ID_SALT`,
  `EMORI_ADMIN_PIN` / `_SALT` / `_TOKEN`, `EMORI_DASH_PASSWORD` / `_SALT`,
  `EMORI_AP_PASSWORD`, `EMORI_SMTP_PASS`) live only on the robot, as do the files the
  admin page writes (`secrets/site.env`, `secrets/admin.env`). The robot's Pokamind
  token is not in `.env` at all: it lives in its own file (see the token section).

Some variables are read elsewhere than the hostapp — inside the ROS 2 container, by the
camera bridge from `/etc/default/emori-camera`, or by the robot compose file from
`deploy/ros2/.env`. Their notes say so; a knob set in the wrong file silently does nothing.

## Where each setting comes from

A robot installed from this tree configures itself: nothing site-specific has to be
typed into `.env`. Each setting the robot-local layer manages
(`src/emori_adapter/site_settings.py`) takes the FIRST of:

| # | Source (`source` label) | What it is |
|---|---|---|
| 1 | `admin` (and `calibration`, `calibration_fallback`) | A value saved on the admin page (`:8070`); the other two labels mark a value an add-on measured and wrote |
| 2 | `env` | An explicit value in `/opt/emori/.env` (or the process environment) |
| 3 | `pokamind` | Derived from this robot's Pokamind record: the course, the pathways, the display name, the boot language (an add-on may add its own switch) |
| 4 | `hardware` | The hardware profile (`EMORI_HARDWARE_PROFILE`): the tuning measured on that hardware (on a Reachy Mini Wireless: the session-start deadlines, expression-cue motion, kiosk turn-taking), plus whatever an add-on contributes for it |
| 5 | `default` | A shipped default an add-on injects on an installed robot; otherwise the code default of whatever reads the variable |

Two rules govern the derived layers:

* **An empty value means unset**, so the next source fills it. A setting an add-on registers
  with the layer follows the same rule. A value set in `.env` is never replaced by anything derived, so a
  robot whose `.env` sets its course, language, name and devices keeps them.
* **Robot-only layers need `EMORI_HARDWARE_PROFILE`.** The hardware values and
  any values an add-on pins apply only on a robot `install.sh` installed. The Pokamind layer applies wherever a token and a synced record exist, a
  development box included; set `EMORI_SITE_AUTO=0` there to keep the environment
  exactly as `.env` has it.

The files (all under `/opt/emori`, mode 0600, none ever shipped by a deploy):

| File | Written by | Holds |
|---|---|---|
| `configs/site/settings.env` | the host app (admin page saves, values an add-on measured) | the non-secret settings saved on this robot |
| `secrets/site.env` | the host app | the SMTP password and any secret setting an add-on registers |
| `secrets/admin.env` | emori-admin | a changed admin PIN, hotspot name/password, dashboard password |
| `configs/site/sources.json` | the host app | which saved keys an add-on measured (labels only) |
| `configs/site/pokamind_scope.json` | the host app, `install-token.sh` | the last verified copy of this robot's Pokamind record (`whoami`), with the menu numbering |

Every process applies the layers once, at start, when `emori_adapter.config` is
imported, and writes the result into its environment, so every reader sees the effective
value. The host app's systemd unit also loads the three settings files after `.env`
(`EnvironmentFile=`), in the same order. emori-admin holds no robot credential: it
loads `.env` without the robot's Pokamind token and the host app's secrets
(`EMORI_SKILL_KEY`, `EMORI_ROS_ID_SALT`, `EMORI_DASH_SALT`, `EMORI_SMTP_PASS`, an add-on's
secret settings), then `configs/site/settings.env` and its own `secrets/admin.env`; it
never reads `secrets/site.env` or the token file, so it derives nothing from Pokamind. A process records which source each value came
from in `EMORI_SITE_SOURCES` (key names and source labels, never a value); a child
process (readycheck spawned by emori-admin, a script spawned by the host app) removes
every inherited value that did not come from `.env` and derives it again, so it reports
the same sources as the host app. Where to see them: the admin page's Settings tab (and
the Pokamind tab's *Applied from Pokamind* card), and readycheck's `site_config` gate
(`KEY=source` pairs). The process log carries one INFO line with the same pairs.

What applies when:

* **Live**: the spoken menu built from Pokamind (`configs/site/auto/`) and a menu saved on
  the Menu tab; the People tab.
* **At the next host app restart**: every other derived or saved value. A change that came
  from Pokamind (or a regenerated credential) is applied by emori-admin itself after 10
  minutes of continuous idle, at most once per 30 minutes, never during a session
  (`EMORI_SITE_AUTO_APPLY_IDLE_S`, 0 disables it). A value an operator saved waits for
  the Settings tab's **Apply now**.
* **At emori-admin's restart**: the admin PIN and the hotspot (the admin page restarts
  itself after saving them).

`EMORI_SITE_AUTO=0` switches the Pokamind layer off for one robot without a code change;
see [Robot-local settings layer](#robot-local-settings-layer).

## Sections

* [Journal verbosity (the deployed services)](#journal-verbosity-the-deployed-services)
* [Reachy Mini daemon](#reachy-mini-daemon)
* [Identity defaults (used when no session is active)](#identity-defaults-used-when-no-session-is-active)
* [THIS robot's own Pokamind account (unique per robot)](#this-robots-own-pokamind-account-unique-per-robot)
* [Live source (emori-run --source pokamind / the ROS publisher)](#live-source-emori-run---source-pokamind--the-ros-publisher)
* [Hostapp: Pokamind partner SSO](#hostapp-pokamind-partner-sso)
* [Hostapp: kiosk behaviour](#hostapp-kiosk-behaviour)
* [Optional add-ons](#optional-add-ons)
* [Hostapp: server-side speech (scripts/get_voice_models.*)](#hostapp-server-side-speech-scriptsget_voice_models)
* [Results email (unset SMTP host = drop to artifacts/outbox/)](#results-email-unset-smtp-host--drop-to-artifactsoutbox)
* [FIWARE / NGSI-LD](#fiware--ngsi-ld)
* [Management dashboard (on-robot, port 8090)](#management-dashboard-on-robot-port-8090)
* [Superadmin service (on-robot, port 8070; phone-first ops)](#superadmin-service-on-robot-port-8070-phone-first-ops)
* [Badge, head-gesture and language-toggle UX](#badge-head-gesture-and-language-toggle-ux)
* [Per-sentence expression cues (behavior/cues.py)](#per-sentence-expression-cues-behaviorcuespy)
* [ROS skill intake (POST /api/skill)](#ros-skill-intake-post-apiskill)
* [Hub (the hostapp hosted on the ROS 2 graph)](#hub-the-hostapp-hosted-on-the-ros-2-graph)
* [Readiness self-check (emori-readycheck; read-only)](#readiness-self-check-emori-readycheck-read-only)
* [Robot-local settings layer](#robot-local-settings-layer)
* [THIS robot's Pokamind token (per robot)](#this-robots-pokamind-token-per-robot)

## Journal verbosity (the deployed services)

### `EMORI_LOG_LEVEL`

```text
Root log level for `emori-hostapp` AND `emori-admin`: DEBUG | INFO (default) |
WARNING | ERROR.
INFO is the operating level, not a debug setting -- the lines an appliance is
diagnosed from are INFO (the session/mode transitions). Without a configured root logger ALL of them would be
dropped and only WARNING and above would reach journalctl.
DEBUG means "EMORI's own loggers at DEBUG" and nothing more. Below WARNING the
journal takes records from `emori.*` / `emori_adapter.*` and from NOTHING else,
because third-party loggers write down their input: `websockets` prints URLs and the
leading bytes of every frame, `httpx` prints full outbound URLs (in emori-admin the
site's Wi-Fi password), and `piper.voice`
prints every sentence the robot is about to speak. WARNING and above still
passes from any library, so their errors and tracebacks are never hidden.
An unusable value logs one WARNING naming itself and keeps INFO.
```

Shipped in `.env.example` as `#EMORI_LOG_LEVEL=INFO` (commented out: the code default applies).

### `EMORI_LOG_WIRE`

```text
Third-party WIRE logging, separately and deliberately. =1 lets those library
loggers through, so the journal then holds full URLs, tokens, frame contents
and synthesized speech -- and it also re-enables uvicorn's HTTP
access log, which prints full query strings (`GET /api/tts?lang=ca&text=<the
line the robot is about to say>`) and, on its own, buries the journal under the
kiosk's status polling. NEVER set this on a deployed robot: journald is
world-readable to anyone with the box and survives reboots. Dev triage only,
and unset it after.
```

Shipped in `.env.example` as `#EMORI_LOG_WIRE=0` (commented out: the code default applies).

## Reachy Mini daemon

### `REACHY_BASE_URL`, `REACHY_TIMEOUT_S`

```text
Windows dev sim:                    http://127.0.0.1:8000
From the ROS 2 container:           http://host.docker.internal:8000
On the CM4:                         http://localhost:8000
```

Shipped in `.env.example` as `REACHY_BASE_URL=http://127.0.0.1:8000`, `REACHY_TIMEOUT_S=5.0`.

## Identity defaults (used when no session is active)

### `EMORI_PERSON_ID`, `EMORI_SESSION_ID`, `EMORI_CYCLE_PERIOD_S`

Shipped in `.env.example` as `EMORI_PERSON_ID=learner_01`, `EMORI_SESSION_ID=sim-session`, `EMORI_CYCLE_PERIOD_S=2.0`.

## THIS robot's own Pokamind account (unique per robot)

### `EMORI_ROBOT_ID`, `EMORI_ROBOT_NAME`

```text
Every drop-in session that is not a known staff badge runs as the robot itself, so the
anonymous roster identity is a real learner. It is derived from these two:
    EMORI_ROBOT_ID=lobby-01  ->  student_id  robot-lobby-01
                                 email       robot-lobby-01@emori.local
    EMORI_ROBOT_NAME=EMORI   ->  the name on the session report / results email
PRODUCTION: this account must exist NOWHERE ELSE — each robot must have a unique id.
EMORI_ROBOT_ID comes from the token when it is empty (Deploy-to-Robot fills the line).
EMORI_ROBOT_NAME empty = the display name Pokamind issued with the token; a name that
is just the robot id is not used, and the name stays the default "EMORI".
Unset falls back to the shared `robot-anonymous` (bench only; logged at WARNING).
An explicit `anonymous:` block in configs/roster.yaml overrides this per field.
EMORI_ROBOT_ID is not a credential: with its token (the robot's only Pokamind
credential) the learner and the control-plane id are the TOKEN's robot id, and an
EMORI_ROBOT_ID naming another robot is logged as an ERROR (readycheck
robot_id_mismatch). Keep the line once the robot is in use: while EMORI_DASH_SALT is
empty the dashboard's person codes derive from it.
```

Shipped in `.env.example` as `EMORI_ROBOT_ID=`, `EMORI_ROBOT_NAME=`.

## Live source (emori-run --source pokamind / the ROS publisher)

### `POKAMIND_WS_URL`

```text
The hostapp's /ws/relay — /api/cycle broadcasts every stamped envelope here.
```

Shipped in `.env.example` as `POKAMIND_WS_URL=ws://localhost:8090/ws/relay`.

## Hostapp: Pokamind partner SSO

### `POKAMIND_BASE`, `EMORI_COURSE_ID`

```text
POKAMIND_BASE is the Pokamind API origin; empty = https://api.pokamind.com, the
public one. The robot authenticates with its per-robot token. It is never
discovered automatically. The admin page can change it (Settings > Server, the PIN
again), and saves the new origin only after a whoami signed with the robot's
CURRENT token answers ok and active from it; https only (http only for loopback).
EMORI_COURSE_ID is the SSO course claim for drop-in sessions and for badges whose
roster sets no course_id. Empty = the course issued with this robot's token (its
Pokamind record), else 'emori'. A value set here wins, and must then be the course
in the token's scope, or Pokamind refuses those sessions (course_not_in_scope).
The partner the robot signs in as is a protocol constant ('emori-robot'); see
EMORI_PARTNER_NAME below.
```

Shipped in `.env.example` as `POKAMIND_BASE=https://api.pokamind.com`, `EMORI_COURSE_ID=`.

### `EMORI_PARTNER_NAME`

```text
Not in the template. The partner the robot signs in as is a protocol constant
('emori-robot', the code default): Pokamind refuses any other for a robot. An
explicit value is still honoured, and readycheck WARNs partner_mismatch when it
differs from the partner in the robot's Pokamind record. Leave it unset.
```

### `EMORI_LANG`, `EMORI_LANG_STATE_FILE`

```text
Session language; supported languages: ca, en.
This is the BOOT DEFAULT. Empty = this robot's default language in Pokamind, else
the first of its Pokamind languages the robot supports, else en. The admin page's
Language tab offers only the languages in the robot's scope, and a persisted
language outside it is ignored at start.
A hidden idle gesture (double head turn) switches the live
language with no restart and PERSISTS it to EMORI_LANG_STATE_FILE, which then outranks
this value on every later boot (the hostapp logs a WARNING naming the file when it
does). Delete that file to come back to the value below.
```

Shipped in `.env.example` as `EMORI_LANG=`, `#EMORI_LANG_STATE_FILE=artifacts/kiosk_lang.json`.

### `HOSTAPP_DEMO`, `HOSTAPP_HOST`, `HOSTAPP_PORT`

```text
1 = synthesize a demo session instead of embedding a real Pokamind session.
A robot without its Pokamind token ALWAYS runs demo sessions, whatever this says:
it has nothing to sign a login with (readycheck FAILs pokamind_token_missing).
```

Shipped in `.env.example` as `HOSTAPP_DEMO=`, `HOSTAPP_HOST=0.0.0.0`, `HOSTAPP_PORT=8090`.

## Hostapp: kiosk behaviour

### `EMORI_ROSTER`

```text
Staff roster (QR badge -> identity) and the verbal topic menu:
```

Shipped in `.env.example` as `EMORI_ROSTER=configs/roster.yaml`.

It is also the file the phone admin's **People** tab writes (in place, atomically, then
reloaded live: [robot-admin.md](robot-admin.md), "Add people and print badges"). Unset,
the hostapp reads `configs/roster.yaml`, else the shipped example, and the People tab
writes `configs/roster.yaml`; it never writes the example.

### `EMORI_PATHWAYS`

```text
The EXACT module-category strings this site serves, comma-separated. A roster badge's
`pathway` is matched against a module's `category` letter for letter (accents and all);
a value that matches nothing leaves the learner with ZERO modules and the session never
starts -- on a screenless robot that looks exactly like the robot ignoring the badge.
Listing them here makes the hostapp DROP an unlisted roster pathway with a WARNING, so
the badge behaves like the working anonymous path instead. Empty (the default) = this
robot's pathways in Pokamind -- unless one of them itself holds a comma: it could not
be listed, and an allowlist without it would drop every badge carrying it, so then
nothing is derived. With none there either, no allowlist: whatever the roster says is
forwarded. '*' says "no allowlist" explicitly (the record's pathways are then never
used here). The admin page's People tab offers only the robot's Pokamind pathways.
  Example: EMORI_PATHWAYS=Example pathway,Another example pathway
```

Shipped in `.env.example` as `EMORI_PATHWAYS=`.

**— the spoken topic menu: where it comes from —**

### `EMORI_VOICE_TOPICS_CA`, `EMORI_VOICE_TOPICS_EN`

```text
Unset (the default), each language's menu is the first of:
  1. configs/site/voice_topics.<lang>.yaml   saved on the admin page's Menu tab
  2. configs/prod/voice_topics.<lang>.yaml   (en: configs/prod/voice_topics.yaml)
                                             only in a fork that ships a fixed menu
  3. configs/site/auto/voice_topics.<lang>.yaml  built from this robot's Pokamind
                                             modules, rebuilt on every sync, live
  4. the repository's configs/voice_topics*.yaml  development menus
  5. the built-in menu
configs/site/ is robot-local: no deploy ships or touches it, so a file-level deploy
(sudo tar -x / git archive / rsync) can never change the robot's menu, and nothing
has to be copied after a deploy.

The menu built from Pokamind takes, per language, the robot's modules that are
available, in its scope and in that language, at most 6 (numbers one to six): the
modules already on the menu first, then new ones in the record's order. When a module
leaves the scope, every later option moves up a number; the robot speaks the whole
menu every time, so nobody has to remember a number. Each
option's spoken name and keywords are Pokamind's when its record has them, else
drafted from the module's title (readycheck WARNs voice_menu_auto_draft; number
selection always works). EMORI_SITE_AUTO=0 switches it off.

A per-language override pins one file and outranks all of the above, e.g.
    EMORI_VOICE_TOPICS_CA=configs/site/voice_topics.ca.yaml
It is the ONE menu decision a deploy cannot overwrite (.env is not tracked).
readycheck FAILs voice_menu_placeholder_ids when the served menu names example-,
demo-module- or dev- modules, and voice_menu_in_scope checks every offered module
against what Pokamind says this robot may start. The development menus (4) are the
simulator's: its demo sessions start nothing on Pokamind.
```

Shipped in `.env.example` as `#EMORI_VOICE_TOPICS_CA=configs/site/voice_topics.ca.yaml`, `#EMORI_VOICE_TOPICS_EN=configs/site/voice_topics.en.yaml` (commented out: the code default applies).

### `EMORI_VOICE_TOPICS`

```text
The older GLOBAL override. It applies to every language at once, so on a robot that
may be re-languaged it freezes the spoken menu in the old language after a switch.
Prefer the per-language variables above, which outrank it.
```

Shipped in `.env.example` as `#EMORI_VOICE_TOPICS=configs/voice_topics.yaml` (commented out: the code default applies).

### `EMORI_BEHAVIOR_CONFIG`

```text
Behaviour + signal-translation tuning (BehaviorConfig/TranslationConfig):
```

Shipped in `.env.example` as `EMORI_BEHAVIOR_CONFIG=configs/behavior.yaml`.

### `EMORI_IDLE_AFTER_S`

```text
Idle ticker: gentle alive-motion when no cycle arrives for N seconds (0 = off)
```

Shipped in `.env.example` as `EMORI_IDLE_AFTER_S=8`.

### `EMORI_LISTEN_STILL`

```text
Hold the body still while the learner has the floor (turn armed/open). Every servo move
is 4-8 kHz noise at the head's mic, as loud as a learner at 80 cm; gaze still keeps eye
contact. 0 = the old attentive breath while listening.
```

Shipped in `.env.example` as `EMORI_LISTEN_STILL=1`.

### `EMORI_MENU_GRAMMAR`

```text
Screenless verbal menu. The mic is transcribed against a CONSTRAINED Vosk grammar
built from the menu's number words + keywords in EMORI_LANG, and only a hit at or above
EMORI_MENU_CONF_MIN starts a session. A grammar without the gate is WORSE than open
recognition (it forces stray speech onto a menu word), so do not raise one without the
other. EMORI_MENU_GRAMMAR=0 is the field kill switch back to open recognition, for a robot
whose Vosk lexicon does not cover the menu (models/vosk-<lang> must exist).
```

Shipped in `.env.example` as `EMORI_MENU_GRAMMAR=1`.

### `EMORI_MENU_CONF_MIN`

```text
Starting value: clean speech scores 0.83-1.00 on a quiet bench. Tune it for your room.
```

Shipped in `.env.example` as `EMORI_MENU_CONF_MIN=0.6`.

### `EMORI_MENU_MAX_TRIES`, `EMORI_MENU_LISTEN_MS`

```text
Misses (low confidence, default-topic fallback, or a silent listen window) before the
kiosk stops guessing and hands back to the badge + number buttons. It NEVER auto-starts
a topic nobody chose.
```

Shipped in `.env.example` as `EMORI_MENU_MAX_TRIES=3`, `EMORI_MENU_LISTEN_MS=12000`.

### `EMORI_MENU_EOS_SILENCE_MS`, `EMORI_MENU_EOS_MIN_VOICED_MS`, `EMORI_MENU_FLUSH_MS`

```text
END-OF-SPEECH FLUSH for the menu mic. On a FIXED 3.5 s interval alone, a one-word
answer sits in the buffer until that window expires: ~1,750 ms of average dead time
before the robot has even heard the choice. So the page also flushes on the
voiced->silence EDGE, reusing the SAME 20 ms RMS frames and the same
EMORI_STT_ENERGY_DBFS floor as the capture gate further down this file.
  EMORI_MENU_EOS_SILENCE_MS     trailing silence that ends an utterance. Raise it if
                                answers are being cut in half (a hesitant "uh... three"
                                splits and only half of it reaches Vosk). 0 disables the
                                edge -> backstop only (the fixed interval alone).
  EMORI_MENU_EOS_MIN_VOICED_MS  speech required before an edge is believed at all, so a
                                cough or a door bang (~150-300 ms above the floor) cannot
                                flush a near-empty buffer. Keep it under the shortest
                                real answer: a lone digit measures ~300-500 ms.
  EMORI_MENU_FLUSH_MS           the BACKSTOP interval (the old fixed timer). It is what
                                makes a room that never falls silent unable to wedge the
                                menu -- the edge can only ever make a flush EARLIER.
                                Do not raise it above EMORI_MENU_LISTEN_MS.
An edge flush may only ever ACCEPT an answer. Low confidence or a non-exact match from an
edge is a guess that did not land: the audio stays buffered and the BACKSTOP window
judges it. So re-prompts and the maxTries countdown keep running at the backstop rate
however noisy the room is; otherwise a few seconds of chatter could burn all 3 tries.
Both counters are readable on the robot while someone talks: window.__emoriCapture over CDP.
```

Shipped in `.env.example` as `EMORI_MENU_EOS_SILENCE_MS=700`, `EMORI_MENU_EOS_MIN_VOICED_MS=250`, `EMORI_MENU_FLUSH_MS=3500`.

### `EMORI_TURN_REPROMPT_S`, `EMORI_TURN_FIRST_REPROMPT_S`, `EMORI_TURN_REPROMPT_EVERY_S`, `EMORI_TURN_MAX_REPROMPTS`, `EMORI_TURN_MAX_REPROMPTS_PER_TURN`, `EMORI_TURN_CEILING_S`, `EMORI_TURN_FIRST_CEILING_S`, `EMORI_TURN_ABS_CEILING_S`, `EMORI_TURN_PRESENT_CEILING_S`, `EMORI_TURN_PRESENT_SILENCE_S`, `EMORI_TURN_PRESENT_QUIET_S`

```text
Turn-taking watchdog (training). A turn opens when the in-iframe
endpointer is listening. "quiet" = seconds since the turn opened or since the last
sound evidence (VAD activity / a rejected attempt); "seen" = a body in Pokamind's
camera analysis within EMORI_TURN_BODY_WINDOW_S or a face on the robot node's own
tracker within EMORI_TURN_FACE_WINDOW_S.
  re-prompt     at quiet >= REPROMPT_S (FIRST_REPROMPT_S on the session's first turn),
                then every REPROMPT_EVERY_S; MAX_REPROMPTS per quiet stretch and
                MAX_REPROMPTS_PER_TURN per turn. Sound within SELF_NOISE_S after a
                re-prompt is the robot's own and refreshes nothing.
  silent stall  quiet >= CEILING_S (FIRST_CEILING_S on the first turn) ends the turn
                ONLY while nobody is seen.
  unseen cap    ABS_CEILING_S from turn-open ends a turn that sound keeps alive while
                nobody is seen (the noisy empty room). 0 disables (and re-arms the
                walk-away inside open turns).
  present silence
                while somebody is seen, the turn ends once it has been open for
                PRESENT_SILENCE_S (never before its silent ceiling) and PRESENT_QUIET_S
                have passed with nothing heard. 0 = off.
  present cap   PRESENT_CEILING_S from turn-open is the last end while somebody is
                seen, for sound that never stops. 0 = presence extends nothing.
The walk-away (EMORI_WALKAWAY_S) stands down while a turn is open; it still owns the
rest of the session (armed-but-never-listening, the AI's own turn, a wedged frame).
```

Shipped in `.env.example` as `EMORI_TURN_REPROMPT_S=12`, `EMORI_TURN_FIRST_REPROMPT_S=20`, `EMORI_TURN_REPROMPT_EVERY_S=15`, `EMORI_TURN_MAX_REPROMPTS=2`, `EMORI_TURN_MAX_REPROMPTS_PER_TURN=3`, `EMORI_TURN_CEILING_S=35`, `EMORI_TURN_FIRST_CEILING_S=50`, `EMORI_TURN_ABS_CEILING_S=90`, `EMORI_TURN_PRESENT_CEILING_S=180`, `EMORI_TURN_PRESENT_SILENCE_S=45`, `EMORI_TURN_PRESENT_QUIET_S=10`.

### `EMORI_TURN_ARMED_CEILING_S`, `EMORI_TURN_BODY_WINDOW_S`, `EMORI_TURN_FACE_WINDOW_S`, `EMORI_TURN_SELF_NOISE_S`, `EMORI_TURN_REJECT_EXTENSIONS`

```text
An ARMED turn whose endpointer never opens (hung or failed load) ends after this long.
0 = ARMED never ends on its own.
```

Shipped in `.env.example` as `EMORI_TURN_ARMED_CEILING_S=90`, `EMORI_TURN_BODY_WINDOW_S=30`, `EMORI_TURN_FACE_WINDOW_S=8`, `EMORI_TURN_SELF_NOISE_S=3`, `EMORI_TURN_REJECT_EXTENSIONS=3`.

### `EMORI_REPROMPT_SPOKEN`, `EMORI_WALKAWAY_S`

```text
1 = the iframe also SPEAKS the re-prompt in the session voice (needs a Pokamind web app
that supports the reprompt command; an older one ignores it).
```

Shipped in `.env.example` as `EMORI_REPROMPT_SPOKEN=0`, `EMORI_WALKAWAY_S=45`.

### `EMORI_END_GRACE_S`, `EMORI_END_GRACE_TALKED_S`

```text
Grace for Pokamind to answer a robot-initiated `end` WITH its analysis before the
local fallback finalizes and sends the page home (which destroys the iframe):
_S for a session that never exchanged anything, _TALKED_S once it has (analysing a
real conversation can take Pokamind a couple of minutes before it posts the completion).
```

Shipped in `.env.example` as `EMORI_END_GRACE_S=12`, `EMORI_END_GRACE_TALKED_S=150`.

### `EMORI_LATE_COMPLETION_S`

```text
A completion that still arrives after the local finalize (the grace ran out first) is
salvaged onto the closed row - status completed, scores stored, end_reason kept - if it
carries results and lands within this many seconds of the finalize. 0 = never salvage.
```

Shipped in `.env.example` as `EMORI_LATE_COMPLETION_S=300`.

### `EMORI_SESSION_START_MS`, `EMORI_SESSION_START_S`

```text
The session-start deadline, in both halves. Everything after `GET /session` happens
inside a cross-origin Pokamind iframe, so without these a session that never comes up
leaves the robot posed, silent and unresponsive with nothing watching.
  _MS  the /session PAGE's own timer: no pokamind_* message in this long -> speak the
       "the session did not start" line, tell the hostapp, and return to the badge
       screen. 0 = off.
  _S   the adapter-side belt-and-braces in WatchdogTicker, for a kiosk that cannot even
       POST back to its own origin. 0 = off. NEITHER can fire once a cycle has arrived.
```

Shipped in `.env.example` as `EMORI_SESSION_START_MS=`, `EMORI_SESSION_START_S=` (empty: the
hardware profile fills them, else the code defaults 45000 / 45 apply).

On a Reachy Mini Wireless the [hardware profile](#emori_hardware_profile) supplies `120000` / `130` (source
`hardware`). Measured on a Reachy Mini Wireless: the CM4 loads the Pokamind session page slowly,
and a cold session's first AI line arrived 48.7 s after the page opened, so the 45 s code
default ended a session that was about to start. `_S` stays above `_MS / 1000` so the
page's own deadline, which speaks its line and goes home cleanly, fires first. The code
defaults stay 45 s for the simulator and any other hardware. Set either here, or on the
admin page's Settings tab, to override the profile. The admin page refuses a save that
puts `_S` below `_MS / 1000` (`value_breaks_deadline_order`), judged on the values the next
start runs with, wherever each comes from.

A `.env` (or `robot.local.env`) written from an older template carries the old shipped
`EMORI_SESSION_START_MS=45000` / `EMORI_SESSION_START_S=45`, an explicit value that would
outrank the profile, and a redeploy keeps the robot's own `.env`. So on a Reachy Mini
Wireless `install.sh` (step 8) empties exactly those two values, judged on the last
assignment, and says so (`emptied EMORI_SESSION_START_MS=45000 ...`); any other number is
an operator's and stays. To keep 45 s on such a robot on purpose, save it on the admin
page, which the installer never touches.

### `EMORI_ABSENCE_S`

```text
Presence-based walk-away: end (WITH analysis, via Pokamind) after this many
seconds with no body in the camera analysis, once a body was seen at least
once this session (CAMERA evidence only arms it; an accepted turn refreshes the
clock but can never arm it). 0 disables; audio-only sessions -- camera off,
unavailable or black -- fall through to
EMORI_WALKAWAY_S. DEFAULT 0 (off): this ends live sessions, so it stays inert
until a human enables it. Suggested starting value once enabled: 20 -- tune
on the robot against a learner looking down at notes mid-answer.
```

Shipped in `.env.example` as `EMORI_ABSENCE_S=0`.

**— Turn-taking tuning passed to the embedded session page —**

### `EMORI_TT_OVERRIDES`

```text
Turn-taking tuning passed to the embedded session page: the kiosk hands it to the
page as window.__EMORI_TT[lang], and the page's endpointer deep-merges it over its
own defaults, so a noisy site can be retuned without changing the page. JSON keyed
by kiosk language (en|ca only); leaves must be numbers/bools/short words. Empty (the
default) = channel inert. Example: {"ca":{"hysteresis":{"pos":0.62,"neg":0.45}}}
```

Shipped in `.env.example` as `EMORI_TT_OVERRIDES=`.

On a Reachy Mini Wireless the [hardware profile](#emori_hardware_profile) supplies the patch a robot in service
runs in both languages, `{"en":{"filler":{"enabled":false},"guard":{"maxFires":30,"minAnswerMs":1000}},"ca":{...the same}}`
(source `hardware`). The admin page's Settings tab saves only a patch the host app takes:
it applies the same rules (`en`/`ca` keys, key and word grammar, no `__proto__` /
`constructor` / `prototype`, leaves that are true/false, numbers or one short word, at most
four levels and 64 leaves, 4096 bytes). A saved patch outranks the profile's, so one the host
app would drop as inert would leave the robot with no turn-taking tuning at all. A patch set
in `.env` is still judged only by the host app, which logs and ignores one it refuses.

### `EMORI_TT_INJECT_HTTP`

```text
1 enables POST /api/tt-overrides so an operator can retune a RUNNING kiosk over the
LAN. Default 0: a shipped robot is not remotely reconfigurable. Reads (GET) are always
allowed and carry only what was explicitly set.
```

Shipped in `.env.example` as `EMORI_TT_INJECT_HTTP=0`.

### `EMORI_TT_TARGET_ORIGINS`

```text
Comma-separated postMessage targetOrigins for the patch. POKAMIND_BASE is always
included; add the session page's origin when it differs from POKAMIND_BASE. The
patch is NEVER posted to '*': an unlisted origin means the browser drops it, which
is the safe failure.
```

Shipped in `.env.example` as `EMORI_TT_TARGET_ORIGINS=`.

### `EMORI_RECORD`

```text
Record every stamped envelope to artifacts/recordings/<session>.jsonl (0 = off)
```

Shipped in `.env.example` as `EMORI_RECORD=1`.

### `EMORI_RESULTS_FILE`

```text
Where the last completion payload is persisted:
```

Shipped in `.env.example` as `EMORI_RESULTS_FILE=artifacts/last_results.json`.

## Optional add-ons

### `EMORI_ADDONS`

```text
Add-ons are separately installed packages that publish an `emori_adapter.extensions`
entry point (docs/addons.md). The hostapp, the admin service and the readiness check
discover the first one by name at start. `off` (also `0` or `false`) skips discovery, so
the kiosk runs exactly as it does with no add-on installed. Unset = discover.
```

Shipped in `.env.example` as `#EMORI_ADDONS=` (commented out: discovery is on).

## Hostapp: server-side speech (scripts/get_voice_models.*)

### `PIPER_VOICE_CA`, `PIPER_VOICE_EN`, `VOSK_MODEL_CA`, `VOSK_MODEL_EN`

```text
Defaults: voices/{ca,en}.onnx and models/vosk-{ca,en} relative to the workdir.
ca = Piper upc_ona/medium + vosk-model-small-ca; en = the English session voice.
scripts/get_voice_models.sh installs ca and en, plus any voice named as an argument
(<lang>=<its path under rhasspy/piper-voices>). The hostapp maps any other voice at
voices/<lang>.onnx (with its .onnx.json) or named by PIPER_VOICE_<LANG>; without one, Piper
reads that language in the first voice.
Each file is fetched with a timeout and retries into <file>.part and renamed into
place only when whole; a voice counts as installed only as a whole pair (.onnx and
.onnx.json, no .part beside it), so a cut download is fetched again next time. A
model that cannot be downloaded is a WARNING: install.sh goes on, the language falls
back to the first voice, and readycheck's tts gate WARNs voice_missing naming it.
```

Shipped in `.env.example` as `PIPER_VOICE_CA=`, `PIPER_VOICE_EN=`, `VOSK_MODEL_CA=`, `VOSK_MODEL_EN=`.

### `EMORI_STT_ENERGY_DBFS`, `EMORI_STT_MIN_VOICED_MS`

```text
Landing verbal-menu capture. The mic is opened with echoCancellation REQUIRED
(exact): if Chromium cannot honour it, the page fails LOUDLY (on-screen message +
mic_constraints_unsupported in GET /api/emori-debug) instead of quietly recording the
room. These two tune the frame-energy gate that decides whether a 3.5 s window is
worth sending to Vosk at all:
  EMORI_STT_ENERGY_DBFS    per-20ms-frame RMS floor in dBFS (clamped -120..0).
                           Lower = more permissive; -120 effectively passes everything.
  EMORI_STT_MIN_VOICED_MS  voiced ms required per window; 0 disables the gate.
Raise the floor (e.g. -42) in a busy corridor; lower it if soft speakers are dropped.
A gate set too high and "nobody spoke" both end in didnt_catch, so the kiosk page keeps
the last measured value in window.__emoriVoicedMs: read it over CDP while someone talks
to the robot and compare with EMORI_STT_MIN_VOICED_MS to derive the floor on the robot.
A rejected or clamped value here is logged at WARNING by the hostapp (it never mutes
the kiosk - it falls back to the default below).
ONE TYPO CANNOT DEAFEN THE MENU. -5 instead of -50 is inside the accepted range and makes
every frame unvoiced, which would drop every window before it reached Vosk (the
end-of-speech edge cannot fire either, since it needs a voiced frame): a screenless kiosk
that hears nothing and can only be answered by button. So the gate may skip windows but
never the FIRST window after the mic opens -- the one holding the answer to the prompt
just spoken. A bad floor therefore costs latency (the backstop interval), not the menu. It is
still a bug to fix: each bypass increments window.__emoriGateBypass (CDP), and a nonzero
value there with __emoriVoicedMs stuck at 0 while someone is talking IS the diagnosis.
```

Shipped in `.env.example` as `EMORI_STT_ENERGY_DBFS=-50`, `EMORI_STT_MIN_VOICED_MS=120`.

## Results email (unset SMTP host = drop to artifacts/outbox/)

### `EMORI_SMTP_HOST`, `EMORI_SMTP_PORT`, `EMORI_SMTP_USER`, `EMORI_SMTP_PASS`, `EMORI_SMTP_FROM`

```text
Optional. Pokamind e-mails the session results itself (server-side), so most sites
leave SMTP empty; these are only for the robot's own engagement report. Set them on
the admin page (Settings > Results e-mail, with a "Send test e-mail" action; the
password there is write-only, kept in secrets/site.env and shown only as set/unset)
or here. EMORI_SMTP_FROM is required when EMORI_SMTP_HOST is set -- or an
EMORI_SMTP_USER that is itself an e-mail address, which is then used as the sender.
There is no built-in sender: without one the report goes to artifacts/outbox/ and
readycheck warns. On the admin page the server, port and user need the admin PIN
again, and a change of the server or the user removes the saved password unless the
same save sets a new one (the password is write-only: it must never follow a new
server). "Send test e-mail" is limited to 5 per 10 minutes.
Port 465 is TLS from the first byte; every other port upgrades with STARTTLS.
```

Shipped in `.env.example` as `EMORI_SMTP_HOST=`, `EMORI_SMTP_PORT=587`, `EMORI_SMTP_USER=`, `EMORI_SMTP_PASS=`, `EMORI_SMTP_FROM=`.

### `EMORI_SMTP_TLS_VERIFY`

```text
The mail server's TLS certificate is CHECKED by default (the system's trust store and
the host name, Python's ssl.create_default_context()), on STARTTLS and on port 465
alike. 0 = do not check it: only for a server with a self-signed certificate on a
network you trust, because the SMTP password then goes to whoever answers (the host
app logs a WARNING on every send). Any value other than exactly 0 checks. On the admin
page (Settings > Results e-mail) changing it needs the admin PIN again.
```

Shipped in `.env.example` as `#EMORI_SMTP_TLS_VERIFY=1` (commented out: the code default applies).

### `EMORI_RESULTS_TO`

```text
Trainer/team inbox(es), comma-separated; roster emails are added automatically:
```

Shipped in `.env.example` as `EMORI_RESULTS_TO=`.

## FIWARE / NGSI-LD

### `EMORI_FIWARE`, `ORION_URL`, `NGSILD_TENANT`

```text
0 (shipped) disables FIWARE on the hostapp live path entirely; the on-robot
dashboard works without it. 1 plus the endpoints turns it on; the admin page's
Settings > Analytics sets the switch, the endpoint and the tenant. The code
defaults (EMORI_FIWARE=1 when the line is absent, ORION_URL, NGSILD_TENANT) are
unchanged, and the tenant must not move: stored data is keyed by it.
```

Shipped in `.env.example` as `EMORI_FIWARE=0`, `ORION_URL=http://localhost:1026`, `NGSILD_TENANT=pokamind`.

### `QL_NOTIFY_URL`

```text
Where Orion notifies QuantumLeap (resolvable from ORION's network, not ours):
```

Shipped in `.env.example` as `QL_NOTIFY_URL=http://quantumleap:8668/v2/notify`.

```text
EMORI_FIWARE=1 is safe to leave on with NO broker present: the hostapp probes
GET $ORION_URL/version once per session (1 s) and silently degrades to a no-op
writer, recording a `fiware_skipped` event for the dashboard instead.
```

## Management dashboard (on-robot, port 8090)

### `EMORI_DASH_DB`

```text
Open http://<robot>:8090/dashboard from a phone on the same LAN (advertised over
mDNS as "EMORI Dashboard", deploy/robot/emori-dashboard.avahi.service).
A local SQLite mirror of the same per-cycle metrics that go to FIWARE, so the
dashboard works on a LAN with no Orion-LD. Pull-only, cheap aggregates.
```

Shipped in `.env.example` as `EMORI_DASH_DB=artifacts/dashboard.db`.

### `EMORI_DASH_SALT`

```text
HMAC key for the pseudonymous person codes (S-xxxxxx). NO name or email is ever
written to this database. Set it ONCE per robot and never rotate it casually:
changing the salt makes every returning learner look like a brand-new person.
Unset -> a deterministic fallback derived from EMORI_ROBOT_ID (warned at boot).
```

`deploy/robot/install.sh` (step 8) sets it to a random value when it is empty AND the
robot's dashboard database does not exist yet, i.e. on a fresh robot. On a robot that
already has dashboard data it never does: a new salt would turn every learner already
in it into a new person. There it only warns (pin the fallback value before the robot
id changes: [deploy-a-new-robot.md](deploy-a-new-robot.md), "Upgrading a robot").

Shipped in `.env.example` as `EMORI_DASH_SALT=`.

### `EMORI_DASH_PASSWORD`

```text
Shared management password for the dashboard pages. Empty = dashboard refuses
to serve (there is no anonymous mode); set it before exposing port 8090.
Login is rate-limited to 5 failures/minute per client IP AND 60 failures/minute
across all clients together -- the global ceiling is what makes the limit real
on an unfirewalled LAN, where one attacker can otherwise add hundreds of source
addresses and multiply the per-IP budget. While an attack is in progress a
manager who mistypes may be told to wait too; the block lifts after a minute.
A successful sign-in sets an HttpOnly cookie signed with this password +
EMORI_DASH_SALT, valid 12 h.
Changing either value signs every outstanding cookie out.
```

`deploy/robot/install.sh` (step 8) generates it when it is empty (never over a value
already set): three groups of four lowercase letters and digits, without the
look-alikes 0/o/1/l, because a manager types it on a phone. The install shows it ONCE,
at the very end of its output, beside the recovery hotspot it generated; after that it
is in `/opt/emori/.env`, and the admin page reveals it (the PIN again, 30 s) or resets
it (a new one, shown once, kept in `secrets/admin.env`; the host app restarts when idle,
which signs dashboard sessions out). The salt is never shown, rotated or derived there.

Shipped in `.env.example` as `EMORI_DASH_PASSWORD=`.

### `EMORI_HEARTBEAT_S`

```text
The dashboard header shows EMORI_ROBOT_NAME (set once, near the top of this
file, beside EMORI_ROBOT_ID); unset -> EMORI_ROBOT_ID -> "EMORI". Deliberately
NOT re-assigned here: python-dotenv and systemd's EnvironmentFile both take the
LAST assignment, so a second `EMORI_ROBOT_NAME=` line silently blanks whatever
the operator configured in the block above, with nothing anywhere reporting it.
Uptime evidence cadence, seconds. Uptime KPI = present beats / expected beats.
0 disables the heartbeat writer entirely.
```

Shipped in `.env.example` as `EMORI_HEARTBEAT_S=60`.

## Superadmin service (on-robot, port 8070; phone-first ops)

Operator guide: [robot-admin.md](robot-admin.md).

### `EMORI_ADMIN_PIN`

```text
Standalone process (emori-admin.service) that survives the hostapp and
kiosk it manages: Wi-Fi incl. AP mode, speaker/mic volume, the EN/CA
language toggle, service restarts, readycheck, logs, reboot -- all from a
phone, on the site LAN or on the robot's own hotspot. See
docs/robot-admin.md for the ops guide.

6-digit PIN gate for the whole admin surface. Empty -> every /api/* route
answers 503 "not configured" (fail closed -- same posture as
EMORI_DASH_PASSWORD above; there is no anonymous admin mode).
deploy/robot/install.sh generates one (6 digits from /dev/urandom) when neither
this line nor secrets/admin.env has one, and shows it ONCE at the end of its
output; Deploy-to-Robot generates one when its PIN question is left empty and
shows it in its final summary. Change it on the admin page (the current PIN is
asked; the new one is kept in secrets/admin.env, which outranks this line, and
emori-admin restarts itself). readycheck WARNs admin_pin_unset.
```

Shipped in `.env.example` as `EMORI_ADMIN_PIN=`.

### `EMORI_ADMIN_PORT`, `EMORI_ADMIN_HOST`

```text
Port/bind for the admin HTTP server. 0.0.0.0 so it answers on BOTH the site
LAN and the robot's own AP (10.42.0.1) -- a loopback-only bind would strand
an operator who has nothing but the robot's hotspot to reach it with.
```

Shipped in `.env.example` as `EMORI_ADMIN_PORT=8070`, `EMORI_ADMIN_HOST=0.0.0.0`.

### `EMORI_ADMIN_SALT`

```text
HMAC salt for the admin login cookie -- and it MUST be high-entropy. The
cookie is signed with a key derived from this salt AND the 6-digit PIN, so a
salt an outsider can guess turns one captured cookie (plain HTTP, on a LAN or
on the robot's own AP) into an offline PIN cracker: 10^6 candidates is about
a second of laptop time, and the recovered PIN outlives cookie expiry,
logout and reboot. deploy/robot/install.sh generates one; unset, emori-admin
mints and persists a random salt beside EMORI_ADMIN_STATE_FILE and warns.
Changing it signs every outstanding admin session out.
```

Shipped in `.env.example` as `EMORI_ADMIN_SALT=`.

### `EMORI_ADMIN_TOKEN`

```text
Shared secret between emori-admin and the hostapp's token-gated
/api/admin/* routes (the language-toggle proxy). Empty -> the hostapp
refuses those routes with 503, fail closed if EITHER side is missing it.
deploy/robot/install.sh generates and appends a random one to .env if this
line is still empty when it runs -- it is a machine secret shared between two LOCAL
processes (each reads it from .env at start), never meant to be typed by a person.
```

Shipped in `.env.example` as `EMORI_ADMIN_TOKEN=`.

### `EMORI_ADMIN_STATE_FILE`

```text
Where the admin service persists operator-set audio levels (speaker/mic
volume, mic session/idle multiplier overrides -- see EMORI_MIC_SESSION_VOL/
EMORI_MIC_IDLE_VOL above) so a reboot re-applies them (through the daemon on
v1, straight to the ALSA mixer on v2 -- see EMORI_ADMIN_HOST_BACKEND) instead
of leaving the speaker at 0. Owning module: src/emori_adapter/admin/
state.py. Relative paths resolve against the working directory
(WorkingDirectory=/opt/emori in emori-admin.service).
```

Shipped in `.env.example` as `EMORI_ADMIN_STATE_FILE=artifacts/admin_state.json`.

### `EMORI_AP_SSID`, `EMORI_AP_PASSWORD`

```text
Rebrand the robot's Wi-Fi hotspot from the daemon's stock "reachy-mini-ap"
to your own SSID/password, applied idempotently at emori-admin startup: it
checks the current SSID first, and NEVER touches the NetworkManager
"Hotspot" profile while it is the ACTIVE connection -- doing that would
drop the operator's own phone mid-session if they happen to be joined to
it at the time. Set both together; EMORI_AP_PASSWORD needs >=8 characters
(WPA2 minimum). Empty -> the stock daemon-created hotspot ships unchanged.

REQUIRED COMPANIONS OF EMORI_ADMIN_WIFI_WATCHDOG=1 (below): the watchdog
refuses to arm without them, because the AP it would otherwise raise
UNATTENDED is the vendor's -- "reachy-mini-ap"/"reachy-mini", hardcoded in a
pip-installable package -- and anyone in radio range could then reach the
unauthenticated daemon on :8000, the hostapp on :8090 and this service, plus
decrypt the operator's plain-HTTP admin cookie. deploy/robot/install.sh
generates a pair and prints it once; the admin page reveals it again (the
PIN again, 30 s) and can change it (kept in secrets/admin.env; emori-admin
restarts and rebrands the hotspot); the manual "Start hotspot now" button
stays available either way. The generated SSID is "emori-<robot id>": the
robot id of its installed Pokamind token, else EMORI_ROBOT_ID, else the
hostname. Every Reachy Mini ships as hostname "reachy-mini", so the id is
what keeps two robots at one site from raising the same hotspot.
```

The SSID is at most 32 bytes, so at most 26 characters follow `emori-`; a longer id
loses its **front**, never its end (`...-reception-desk-unit-01` and `-02` stay apart).
A later install renames the hotspot only while it still carries a name the installer
gave it (recorded in `artifacts/ap-ssid.generated`; installs from before that file are
recognised by the earlier recipes, `emori-<hostname>` and `emori-<EMORI_ROBOT_ID>`):
a robot that got its token after its first install moves from `emori-reachy-mini` to
`emori-<robot id>`, its password unchanged, and the install says so at its end. A name
an operator chose is never touched.

Shipped in `.env.example` as `EMORI_AP_SSID=`, `EMORI_AP_PASSWORD=`.

### `EMORI_ADMIN_WIFI_WATCHDOG`, `EMORI_ADMIN_WIFI_GRACE_S`

```text
Background watchdog: if the Wi-Fi backend's GET /wifi/status reports
"disconnected" continuously for EMORI_ADMIN_WIFI_GRACE_S seconds,
emori-admin asks that backend's OWN setup_hotspot (the daemon on v1,
emori-admin's nmcli backend on v2 -- see EMORI_ADMIN_HOST_BACKEND below;
never a separate nmcli call of its own) so a mid-day Wi-Fi drop still leaves
a way back onto the robot. Boot-time loss is covered by
ensure_wifi_on_startup() (the daemon's on v1, emori-admin's on v2); this
only covers a drop while the robot has been up and connected for a while.
0 disables the watchdog entirely. NOTE: 1 alone is not enough -- it only
arms with EMORI_AP_SSID + EMORI_AP_PASSWORD set (see above); when it refuses
it says so at WARNING in journalctl -u emori-admin.
```

Shipped in `.env.example` as `EMORI_ADMIN_WIFI_WATCHDOG=1`, `EMORI_ADMIN_WIFI_GRACE_S=180`.

### `EMORI_ADMIN_HOST_BACKEND`

```text
Who performs the Network and Audio tabs' work (and the Wi-Fi watchdog's, and
the boot-time hotspot fallback): auto (default) | daemon | host.

daemon = Pollen's reachy-mini daemon API on :8000 (/wifi/*, /api/volume/*):
v1, where emori-admin is a proxy in front of it.
host   = emori-admin itself, with the daemon's own semantics and response
shapes: Wi-Fi through nmcli (src/emori_adapter/admin/netman.py; root calls
as `sudo -n nmcli`, allowlisted in deploy/robot/sudoers-emori-admin) and the
speaker/mic hardware level through ALSA's amixer, the daemon's own
card/control choice (admin/mixer.py). Needed on v2, where the ROS 2 node's
REST shim owns :8000 and has no /wifi or /api/volume routes (every such
call answers 404), and where nothing else raises the hotspot at boot any more:
on host, emori-admin runs the daemon's ensure_wifi_on_startup() itself
(no known Wi-Fi in range -> hotspot, 5 tries 3 s apart), on every
emori-admin start, and only once "disconnected" has held for 40 s, so a
restart during a short Wi-Fi drop does not leave the robot on its hotspot.

auto picks host when GET :8000/api/daemon/status reports backend
"reachy_mini_ros2" (the v2 shim's tell), when a /wifi or /api/volume call
answers a route-missing 404, or when :8000 is silent and the v2 fence flag
/opt/emori/V2_OWNS_THE_BUS exists; daemon otherwise. The choice is re-checked
every 60 s (so a v1 rollback heals by itself) and logged at INFO, once and
on every change: journalctl -u emori-admin | grep "Wi-Fi/volume backend".
Force it only to override a wrong guess; an unknown value reads as auto.
```

Shipped in `.env.example` as `EMORI_ADMIN_HOST_BACKEND=auto`.

## Badge, head-gesture and language-toggle UX

### `EMORI_SCAN_GREETING`

```text
Physical greeting at a successful badge scan. Default 0 (off). Auto-suppressed
while an add-on mode is active.
```

Shipped in `.env.example` as `EMORI_SCAN_GREETING=0`.

### `EMORI_MIC_GATE`, `EMORI_MIC_GATE_HALF_WIDTH_DEG`, `EMORI_MIC_GATE_FACE_HOLD_S`, `EMORI_MIC_GATE_CLOSE_AFTER_S`, `EMORI_MIC_GATE_REOPEN_AFTER_S`

```text
Shuts the microphone to what is not the person in front of the robot, while a mode holds it, by
muting the shared capture source. It closes on speech from outside HALF_WIDTH_DEG of straight
ahead (from the mic chip's direction of arrival) that lasts CLOSE_AFTER_S, reopening
REOPEN_AFTER_S after it stops, and when a face seen this session has been gone FACE_HOLD_S.
off, observe (decide and log, touch nothing) or on. Every doubt leaves the microphone open: no
direction reading, the robot speaking, no face seen yet. It cannot part two people speaking from
the same direction. `/api/status` reports it under `mic_gate`. Takes a restart.
```

Shipped in `.env.example` as `#EMORI_MIC_GATE=off`, `#EMORI_MIC_GATE_HALF_WIDTH_DEG=50`, `#EMORI_MIC_GATE_FACE_HOLD_S=8`, `#EMORI_MIC_GATE_CLOSE_AFTER_S=0.3`, `#EMORI_MIC_GATE_REOPEN_AFTER_S=1.5`.

### `EMORI_MIC_SESSION_VOL`, `EMORI_MIC_IDLE_VOL`

```text
Per-mode wpctl capture-gain MULTIPLIER for the one shared microphone (NOT a
device selector -- mic_profile.py shells to `wpctl set-volume
@DEFAULT_AUDIO_SOURCE@ <value>` on every session<->idle transition). TRAINING
hears a person standing a metre or two away and wants much louder gain than
an idle robot can carry before room noise
starts reading as speech -- see mic_profile.py's module docstring for the
measured numbers. Clamped server-side to 0..4.0
(a fat-fingered "140" -- percent mistaken for a multiplier -- must not
deafen the mic for a whole demo). The emori-admin service's Audio tab can
override these live (persisted to EMORI_ADMIN_STATE_FILE below, which then
outranks the values here); leave both unset to keep the measured defaults.
```

Shipped in `.env.example` as `#EMORI_MIC_SESSION_VOL=1.4`, `#EMORI_MIC_IDLE_VOL=0.30` (commented out: the code default applies).


### `EMORI_DIRECTIONAL_YAW_THRESHOLD_RAD`, `EMORI_DIRECTIONAL_ROLL_THRESHOLD_RAD`, `EMORI_DIRECTIONAL_AMBIGUITY_RATIO`

```text
Head-turn detection thresholds for the signed left/right gesture. Tune only after
calibrating on the robot.
```

Shipped in `.env.example` as `EMORI_DIRECTIONAL_YAW_THRESHOLD_RAD=0.20`, `EMORI_DIRECTIONAL_ROLL_THRESHOLD_RAD=0.20`, `EMORI_DIRECTIONAL_AMBIGUITY_RATIO=1.40`.

### `EMORI_UNREGISTERED_BADGE_FEEDBACK`, `EMORI_UNREGISTERED_BADGE_CUE_WAV`, `EMORI_UNREGISTERED_BADGE_CUE_SHA256`

```text
Spoken cue + antenna ack for unregistered badges. Default 0. Enabling this
REVEALS roster membership to whoever scans a badge (a scan that gets no cue is
then known to be a registered badge, and vice versa), so enable it only where
that disclosure is acceptable for the site.
```

Shipped in `.env.example` as `EMORI_UNREGISTERED_BADGE_FEEDBACK=0`, `EMORI_UNREGISTERED_BADGE_CUE_WAV=`, `EMORI_UNREGISTERED_BADGE_CUE_SHA256=`.


### `EMORI_TRAINING_NAV_TIMEOUT_S`

```text
QR scan-to-/session lease deadline (seconds, 10-600; default 60). Raise it if
Piper TTS + Vosk retries + Pokamind SSO take longer than 60 s on your robot.
```

Shipped in `.env.example` as `#EMORI_TRAINING_NAV_TIMEOUT_S=60` (commented out: the code default applies).

### `EMORI_GESTURE_BEGIN`

```text
Idle head-tilt 'begin': a head tilt at the idle kiosk starts an anonymous
training session. Default 0: training starts only from a badge scan, the
on-screen button or the verbal menu. 1 routes idle head movement back to the
binary tilt detector and disables the directional head-turn selector entirely,
so it is for robots that do NOT run an add-on that reads those turns.
```

Never set `EMORI_GESTURE_BEGIN=1` together with an add-on that reads directional head turns.

Shipped in `.env.example` as `EMORI_GESTURE_BEGIN=0`.

### `EMORI_GESTURE`, `EMORI_GESTURE_MODE`

```text
Head-gesture subsystem. EMORI_GESTURE=0 disables it entirely (REQUIRED on the
MuJoCo sim - gravity drift reads as taps; keep 1 on the real robot).
EMORI_GESTURE_MODE=push_turn is REQUIRED on daemon v1.9.0 (gravity_compensation
returns HTTP 500; push_turn detects a firm turn against stiff motors and
recenters afterwards). back_drive is the firmware-default mode.
```

Shipped in `.env.example` as `EMORI_GESTURE=1`, `EMORI_GESTURE_MODE=push_turn`.

### `EMORI_GESTURE_INVERT`

```text
REQUIRED WITH push_turn ON THIS FIRMWARE. With gravity_compensation refused
(HTTP 500 on daemon v1.9.0) the head is stiff, so a human "turn" does not move
the yaw joint at all - it shows up as ROLL through the neck platform, and the
SIGN of that coupling is a property of the mechanism. Measured on the robot: a
turn the operator calls LEFT reports d_roll=+0.236, i.e. RIGHT. Without this,
every directional selection acts as the OTHER direction.
readycheck fails the gesture gate if push_turn is set and this is not.
```

Shipped in `.env.example` as `EMORI_GESTURE_INVERT=1`.

### `EMORI_GESTURE_SETTLE_EPSILON_RAD`, `EMORI_GESTURE_SETTLE_TICKS`, `EMORI_GESTURE_QUIET_EPSILON_RAD`, `EMORI_GESTURE_QUIET_TICKS`

```text
Trajectory-shape guards for the directional detector, so head motion that is
DECAYING, not rising, cannot fire a selection. A selection needs a settle-gated
baseline AND an observed rest on it before the rise. Raise the ticks if the
robot still self-selects while settling;
raise the epsilons only if a real turn is being missed. Ticks are 10 Hz.
```

Shipped in `.env.example` as `#EMORI_GESTURE_SETTLE_EPSILON_RAD=0.03`, `#EMORI_GESTURE_SETTLE_TICKS=3`, `#EMORI_GESTURE_QUIET_EPSILON_RAD=0.05`, `#EMORI_GESTURE_QUIET_TICKS=3` (commented out: the code default applies).

### `EMORI_GESTURE_BACK_EXIT`, `EMORI_GESTURE_BACK_THRESHOLD_RAD`, `EMORI_GESTURE_BACK_HOLD_TICKS`

```text
A firm push of the head BACK, held a little over half a second, leaves whatever is running: a session
ends through Pokamind, so the analysis still lands, and an add-on mode goes back to idle. It
works while the robot is moving, because it reads the residual pitch, with the command already
subtracted. It needs EMORI_GESTURE_MODE=push_turn, a robot that reports the residual, and motors
that are enabled. At idle it does nothing. During a session's opening, the end waits for the
first turn, because the page refuses `end` before then. When it is felt, the antennas flick
twice, and in a session the current sentence's movement stops.
EMORI_GESTURE_BACK_EXIT=0 turns it off and takes a restart. Every other gesture is unchanged.
```

Shipped in `.env.example` as `#EMORI_GESTURE_BACK_EXIT=1`, `#EMORI_GESTURE_BACK_THRESHOLD_RAD=0.15`, `#EMORI_GESTURE_BACK_HOLD_TICKS=7`.

### `EMORI_GESTURE_DEBUG`

```text
Per-tick head-pose trace (roll/pitch/yaw, baseline, latch, rest-armed) for
diagnosing "I turned the head and nothing happened". OFF on a deployed robot,
and assigned rather than commented out so a copied .env cannot inherit a 1 from
an older file: it logs at WARNING, ~400 lines a day, on an SD card that can
already be 94% full. Turn it on for one debugging session, then off.
```

Shipped in `.env.example` as `EMORI_GESTURE_DEBUG=0`.

### `EMORI_GESTURE_RESIDUAL`

```text
Residual sensing: push_turn reads `measured - commanded` (the v2 shim's
motion.residual on /api/state/full) instead of the absolute head pose, so a
human's hand on the head is told apart from the robot's own motion instead of
the sensor going deaf every time the robot moves. 1 (default) on a v2 robot;
a v1 robot or Pollen's daemon reports no residual and falls back to the absolute
pose automatically, so this changes nothing there.

THE ROLLBACK. Setting it to 0 and restarting the hostapp reverts three shipped
behaviours together: per-sentence cues stop planning while the robot speaks,
the skill intake goes back to refusing 423 during a session, and the idle scan
stands down again -- because all three hang off head_reserved_for_sensing(),
which widens back to its old meaning whenever the residual is unavailable.
```

Shipped in `.env.example` as `#EMORI_GESTURE_RESIDUAL=1` (commented out: the code default applies).

### `EMORI_LANG_TOGGLE`, `EMORI_LANG_TOGGLE_WINDOW_S`

```text
Hidden idle language toggle: a full head turn one way then the OTHER way inside the
window below switches the kiosk between ca and en (spoken confirmation + the landing
page reloads itself). Only while the robot is idle - never during a session, a QR
training lease or an add-on mode. The cost, and the reason it is a knob: while the
toggle is armed a LONE directional turn is held for up to this many seconds before an
add-on acts on it, because a turn is not known to be lone until the window closes.
Default (and the shipped value here) is 0, so a head turn reaches an add-on without
that delay. Set 1 only on a robot that has
to be re-languaged in the field with no screen and no shell.
```

Shipped in `.env.example` as `EMORI_LANG_TOGGLE=0`, `#EMORI_LANG_TOGGLE_WINDOW_S=2.5`.

### `EMORI_PLAY_TARGET`

```text
pw-play --target for the robot's own spoken confirmation (empty = the default sink).
```

Shipped in `.env.example` as `#EMORI_PLAY_TARGET=` (commented out: the code default applies).

## Per-sentence expression cues (behavior/cues.py)

### `EMORI_CUE_MOTION`

```text
The AI's reply text arrives from the session page one sentence at a time, and a
sentence may carry a cue family (tension | levity | energy | intimacy | softening |
release). The robot plans a bounded motion for that
family and plays it WHILE the sentence is spoken; the plan is duration-capped, always
ends in a settle, and a newer sentence preempts an older plan's tail mid-flight.

Every knob here is read once, in LiveDriver.__init__ -- changing one needs a hostapp
restart, not just a new session.

0 is a true kill-switch: every sentence falls back to the exact talking_gesture
behaviour that shipped before cues existed. Set it if the head is doing anything the
room reads as wrong and you need the robot calm in one restart.
```

Shipped in `.env.example` as `#EMORI_CUE_MOTION=1` (commented out: the code default applies).

### `EMORI_CUE_MAX_S`

```text
Hard cap on ONE sentence's motion budget, seconds. RobotPlayer sleeps up to 1.2 s per
intent and the next sentence's cue is ~0.3 s away, so a long plan is a long occupancy
of the 12-slot queue. Raise only if sentences on this robot are genuinely longer.
```

Shipped in `.env.example` as `#EMORI_CUE_MAX_S=4.0` (commented out: the code default applies).

### `EMORI_CUE_Z_MM`

```text
Clamp on head Z travel (mm, negative = sink toward the shell; at most 20). Measured on
a Reachy Mini Wireless; lower elsewhere: 8.0 is the deliberately timid code
default. On other hardware tune it by bisection until the sink is visible and the
platform never hits a limit.
```

Shipped in `.env.example` as `#EMORI_CUE_Z_MM=8.0` (commented out: the code default applies).

On a Reachy Mini Wireless the [hardware profile](#emori_hardware_profile) supplies `18`: a robot in service has run
it for months without a motor error.

### `EMORI_CUE_BODY_YAW`

```text
Body-yaw terms in the cue recipes. Measured on a Reachy Mini Wireless; off
elsewhere until measured on that robot, for the same reason as z.
```

Shipped in `.env.example` as `#EMORI_CUE_BODY_YAW=0` (commented out: the code default applies).

On a Reachy Mini Wireless the [hardware profile](#emori_hardware_profile) supplies `1`: a robot in service has run
body yaw for months without a motor error.

### `EMORI_CUE_DEESCALATE_AT`

```text
Escalation above which every family down-maps to `softening` and the intensity is
quartered. Rule: never escalate back. An agitated learner gets slow,
low-amplitude, drooped-antenna motion regardless of what the AI's voice is doing.
```

Shipped in `.env.example` as `#EMORI_CUE_DEESCALATE_AT=0.6` (commented out: the code default applies).

### `EMORI_CUE_AMP`

```text
Amplitude multiplier over every cue and talking-flow angle (0.5..2.0). The TOTAL
scalar (affect k x lexicon x this) is capped at 1.6 inside the planner, so even 2.0
cannot push an authored angle past the platform's ~25 deg envelope. Raise when the
room reads the motion as too subtle; 1.0 is the authored baseline.
```

Shipped in `.env.example` as `#EMORI_CUE_AMP=1.0` (commented out: the code default applies).

On a Reachy Mini Wireless the [hardware profile](#emori_hardware_profile) supplies `1.6`, the amplitude the reference
robot runs (the planner's 1.6 cap on the total still holds). 1.0 elsewhere.

### `EMORI_CUE_TALK`

```text
A sentence with NO cue family performs the seeded full-DOF talking flow (head
yaw/pitch/roll/z + antennas + body sway) instead of the older antenna-flick loop --
the server usually tags one cue per reply, so untagged sentences are most of every
reply. 0 restores the older antenna-flick fallback exactly.
```

Shipped in `.env.example` as `#EMORI_CUE_TALK=1` (commented out: the code default applies).

### `EMORI_CUE_CARRY`

```text
The turn's authored family keeps colouring the reply's untagged sentences at reduced
amplitude until tts_ended (bounded by a 20 s TTL if that marker is lost). 0 makes
every untagged sentence neutral talking flow instead.
```

Shipped in `.env.example` as `#EMORI_CUE_CARRY=1` (commented out: the code default applies).

### `EMORI_MOTION_TRACE`

```text
1 writes one file per session to artifacts/motion-trace/: when each sentence arrived,
which family it was planned to perform, when its voice was seen, and when each motion
was commanded. scripts/motion_sync_report.py turns those files into per-sentence delay
and match figures.

Files hold ids, times, families, slugs and motion reasons, never a sentence's words, a
cue direction or a person's identity. A file stops at 2 MB, the directory at 50 MB with
the oldest removed first, and writing stops under 500 MB of free disk. The motion path
only appends to an in-memory buffer, and one thread writes once per second.

Unset or 0 records nothing, which is the default on a dev box and under pytest.
```

Shipped in `.env.example` as `EMORI_MOTION_TRACE=1`, so a new robot records from its first session.

### `EMORI_VOICE_DELAY_MS`

```text
Milliseconds the kiosk's voice is held back in a sink in front of the speaker, so the head,
which takes longer to answer than the voice takes to reach the speaker, moves with the word
instead of after it. Unset or 0 is no sink and no delay. The real delay comes out about 27 ms
under the value asked for, so report it from a motion-sync run rather than assuming it.

The page believes the voice has ended this much sooner than the room hears it, which can
open the microphone on the end of the voice. Try it with real sessions before leaving it on.

After changing it: sudo systemctl restart emori-voice-delay emori-hostapp.
```

Shipped in `.env.example` as `#EMORI_VOICE_DELAY_MS=0` (commented out: the code default is off).

### `EMORI_MOTION_SYNC`

```text
On by default. A sentence's first move replaces whatever the robot is still doing instead of
waiting behind it, only one head move is in flight at a time, the settle goes out as soon as the
voice has been silent for 0.40 s, and the voice counts as a sentence's from when its event arrived.

0 puts back the older timing, for comparing the two or backing the change out without a deploy.
A sentence that is only a cue is then dropped again.
```

Shipped in `.env.example` as `#EMORI_MOTION_SYNC=1` (commented out: the code default is the same).

## ROS skill intake (POST /api/skill)

### `EMORI_SKILL_API`

```text
The ros4hri graph's ONLY way to move the robot. It does not POST the daemon -- it
POSTs this endpoint, which funnels into the same submit_legacy_motion -> RobotPlayer
queue the hostapp's own lanes use, so the single-writer rule (and every motion
serialization trap it closes) holds with ROS in the picture.

Off by default: without the flag there is no route to find, and a scanner cannot tell
whether this robot has an intake at all.
```

Shipped in `.env.example` as `#EMORI_SKILL_API=0` (commented out: the code default applies).

### `EMORI_SKILL_KEY`

```text
Shared secret, presented as `X-Emori-Skill-Key`. The endpoint REFUSES TO MOUNT when
the flag is 1 and this is empty -- a secret-less "enabled" state is the one
configuration that must never exist. Loopback-only is enforced separately, so this is
defence in depth, not the only door. Generate per robot; never commit a real one.
```

Shipped in `.env.example` as `#EMORI_SKILL_KEY=` (commented out: the code default applies).

### `EMORI_SKILL_MIN_INTERVAL_S`

```text
One ACCEPTED request per this many seconds; the excess is refused (429), never queued.
The 12-slot queue is shared with the greeting and the listening cue, so
a chatty node must not be able to starve them.
```

Shipped in `.env.example` as `#EMORI_SKILL_MIN_INTERVAL_S=0.35` (commented out: the code default applies).

### `EMORI_SKILL_TRANSPORT`

```text
Which TRANSPORT the ROS-side facade posts a skill request through. Read inside the
container by emori_skill_facade (not by the hostapp), and the plug point a future
skill interface slots into as one more class rather than a rewrite.
  hostapp_http (default) - the real one: POST /api/skill with X-Emori-Skill-Key.
  log                    - accepts and records every body, posts nothing. The bench
                           and the standalone image smoke, where no hostapp exists.
```

Shipped in `.env.example` as `#EMORI_SKILL_TRANSPORT=hostapp_http` (commented out: the code default applies).

### `EMORI_ROS_MOTION_OWNER`

```text
Which lane actually submits motion: hostapp | ros | none. Enforced in exactly two
places, one per side: the ROS `skill_facade` only POSTs when it is `ros`, and the
hostapp refuses /api/skill (409 not_owner) unless it is `ros` while standing its OWN
expressive lanes down when it is not `hostapp`. An unrecognised value reads as
`hostapp` -- a typo must never be permission. Read once at hostapp start.
  hostapp (default) - the hostapp drives expression. ROS computes and publishes skill
                      requests, the facade does NOT POST. Zero behaviour change.
  ros               - movement genuinely originates in the ROS graph, still through
                      the one queue; the hostapp's own
                      cue + per-cycle engagement motion stands down (no double-drive).
  none              - pure observation / benchmarking: neither lane submits. Session
                      motion that is not expressive (greeting, farewell,
                      push-turn feedback) is the robot doing its job and is untouched.
```

Shipped in `.env.example` as `#EMORI_ROS_MOTION_OWNER=hostapp` (commented out: the code default applies).

### `EMORI_ROS_OWNER_FALLBACK_S`

```text
Dead-man's handle on the line above, and OFF by default (0 = off). With owner=ros,
if no /api/skill request has been ACCEPTED for this many seconds while a session is
running and cycles are arriving, the hostapp takes its own expressive lanes back and
logs one WARNING (counter: owner_fallback_episodes on /api/status). The next accepted
request hands them straight back, with an INFO line.
Why off by default: a wedged ROS graph should fail VISIBLY -- a robot that stops
being expressive is a symptom an operator can read, whereas a hostapp that quietly
resumed driving makes "movement originates in the ROS graph" false without saying so.
Turn it on for an unattended venue run, where a still robot in front of a person is
the worse of the two failures. Never applies to `none`: that one is a deliberate
"nothing moves", not a delegation that can fail. Read once at hostapp start.
```

Shipped in `.env.example` as `#EMORI_ROS_OWNER_FALLBACK_S=0` (commented out: the code default applies).

**— read inside the ROS 2 CONTAINER, not by the hostapp —**

This setting and the three after it live in `deploy/ros2/.env`, beside the motor's compose
file, and stay there with [`EMORI_HUB_GRAPH=1`](#emori_hub_graph): the hub reads them from that
file when its unit starts. A different value in `/opt/emori/.env` has no effect on the graph in
either case, and the hub logs a warning naming the key.

### `EMORI_ROS_BEHAVIOR_CONFIG_HOST`

```text
Host path bind-mounted read-only over /emori/configs/behavior.yaml, which is what
EMORI_BEHAVIOR_CONFIG points at inside the container. Defaults per compose file:
../../configs/behavior.yaml for the sim one (compose resolves a relative bind against
the compose FILE's directory) and /opt/emori/configs/behavior.yaml on the robot. Set
it when a robot's tuned thresholds live somewhere else; the node logs the effective
numbers and their source at startup, and errors if the path is missing. With the graph
in the hub there is no mount: the node reads this path itself as its behavior_yaml
parameter, so it must be under /opt/emori, the one tree the hub shares with the host.
The session code's own EMORI_BEHAVIOR_CONFIG stays as it is.
```

Shipped in `.env.example` as `#EMORI_ROS_BEHAVIOR_CONFIG_HOST=/opt/emori/configs/behavior.yaml` (commented out: the code default applies).

### `EMORI_ROS_DEIDENTIFY`, `EMORI_ROS_SPEECH_TEXT`, `EMORI_ROS_ID_SALT`

```text
What leaves the graph on DDS. ROS_LOCALHOST_ONLY=1 is the ONLY access control on the
robot, so anything published is readable by any node in that container's domain:
  EMORI_ROS_DEIDENTIFY=1 (default) replaces person_id/session_id with `p-<16 hex>`
    pseudonyms on Context and SpeechCue. The topic namespace (learner_01) is
    unchanged -- it is a slot, not a person.
  EMORI_ROS_SPEECH_TEXT=0 (default) keeps the robot's spoken TEXT off the wire
    entirely. cue_category / est_duration / lang / ended / seq are never withheld:
    the behaviour lane needs exactly those and nothing else.
  EMORI_ROS_ID_SALT empty (default) = a random salt per process, so pseudonyms are
    NOT linkable across restarts and the node says so in its startup line. Set a
    fixed salt only when a study needs the same person to be the same pseudonym
    across sessions -- that is a re-identification key, so treat it as a secret.
    install.sh never sets it: a fixed salt is always a person's decision.
```

Shipped in `.env.example` as `#EMORI_ROS_DEIDENTIFY=1`, `#EMORI_ROS_SPEECH_TEXT=0`, `#EMORI_ROS_ID_SALT=` (commented out: the code default applies).

### `EMORI_ROS_RAW_ENVELOPES`

```text
Whether the hub's session-event topic (/emori/session/events) carries the relayed envelope
itself. 0 (default): every event is typed fields only, and `payload_json` is empty. 1: the
envelope goes into `payload_json` as JSON, after the same gate as every other message:
person_id and session_id become pseudonyms unless EMORI_ROS_DEIDENTIFY=0, and `text`,
`cue_body` and `transcript_delta` are removed unless EMORI_ROS_SPEECH_TEXT=1. What remains
travels as it is, including the `results` of a completed session (scores and summary), so
this is a decision to take for a capture, not for a robot in a room. An envelope longer than
64 K characters is withheld and counted. Only 1, true, yes or on turn it on.
```

Read from `/opt/emori/.env` like the hub's other settings, unlike `EMORI_ROS_DEIDENTIFY`,
`EMORI_ROS_SPEECH_TEXT` and `EMORI_ROS_ID_SALT`, which come from `deploy/ros2/.env` because they
predate the hub and a value left in the host's file must not start to count. Shipped in
`.env.example` as `#EMORI_ROS_RAW_ENVELOPES=0` (commented out: the code default applies).

### `EMORI_HUB_GESTURE_INJECT`

```text
Whether the hub's /emori/gesture/inject_turn service (a std_srvs/SetBool: true is left) may
hand the add-on's head-turn hook a signed turn as if the head had made it. 0 (default): the
service answers success=false with the message inject_disabled. 1: the turn reaches the hook
through the same function that publishes a head turn on /emori/gesture/events, so the event is
published too; the head is not recentred. Refused with session_active while a training session
runs, because a turn then means the end nudge. Only 1, true, yes or on turn it on.
```

Shipped in `.env.example` as `#EMORI_HUB_GESTURE_INJECT=0` (commented out: the code default applies).

**— v2 face look-at (the robot node's own camera; read inside the container) —**

### `EMORI_GAZE`

```text
A CAPABILITY switch, not a run switch. 1 (default) builds the YuNet detector; the
detector does not RUN until the hostapp posts /api/emori/gaze {"enabled":true},
which it does only during a training session or an add-on mode that has asked for it
(see `GET /api/addon/status`). Fail-closed on purpose: an idle kiosk in a
public corridor runs no face detector on nobody's behalf. Set 0 to not build it.

Nothing about a face leaves the process: only a normalised bounding box and a
confidence are published (/humans/faces/*), no image, crop, embedding or identity
is stored, and the face id is a random 4-hex handle with a 10 s life -- a tracking
handle, deliberately not a re-identifier. See docs/ros2-v2-native-robot.md.
```

Shipped in `.env.example` as `#EMORI_GAZE=1` (commented out: the code default applies).

### `EMORI_GAZE_DETECT_HZ`

```text
Detections per second (5.0). The CM4 measured 71 ms per frame, and 5 Hz held /joint_states
at 45.4 Hz on a robot running the kiosk. Lower it first when /joint_states dips below 45 Hz.
```

Shipped in `.env.example` as `#EMORI_GAZE_DETECT_HZ=5.0` (commented out: the code default applies).

`deploy/ros2/docker-compose.v2.robot.yml` defaults it to `3.0`, so a robot whose
`deploy/ros2/.env` does not set it detects at the measured CM4 rate; the launch file's own
default (5.0) is what the simulator and a bench run. Like every container setting, it is
read at `docker compose up`, not from `/opt/emori/.env` (the hardware profile cannot reach it).

### `EMORI_GAZE_SERVO_HZ`

```text
How the head moves towards the face, and how every OTHER move is aimed at it. Like
EMORI_GAZE_DETECT_HZ, these reach the node only through
deploy/ros2/docker-compose.v2.robot.yml, which substitutes them from deploy/ros2/.env
beside it (/opt/emori/deploy/ros2/.env on the robot) -- NOT from this file; setting them
here does nothing. The values below are the defaults; unset changes nothing.
The follower: detection only moves the goal and this loop glides the head there (one
write per 3 Hz detection reads as a head following in small bursts). 0 = the old
one-write-per-detection behaviour.
```

Shipped in `.env.example` as `#EMORI_GAZE_SERVO_HZ=25.0` (commented out: the code default applies).

### `EMORI_GAZE_ANCHOR`

```text
1 = speech gotos and streamed targets are composed onto the face aim while gaze holds
a face (a person to one side otherwise gets a robot talking to the wall). 0 = off.
```

Shipped in `.env.example` as `#EMORI_GAZE_ANCHOR=1` (commented out: the code default applies).

### `EMORI_GAZE_LANE`

```text
Empty = derived from EMORI_ROS_MOTION_OWNER (interaction while the hostapp owns
expression, where an expression-lane gaze write is refused not_owner); a value wins.
```

Shipped in `.env.example` as `#EMORI_GAZE_LANE=` (commented out: the code default applies).

### `EMORI_GAZE_ANCHOR_RECORDED`

```text
How much of the face aim a Pollen RECORDED move (a rec-* cue) gets: yaw | full | off.
yaw because the emotions library already spends its full pitch: with the pitch
composed too, 16% of its frames are out of reach (freeze-then-snap) against 6.1-6.6%
for yaw only (measured over 73 moves). Unknown values play them as authored.
```

Shipped in `.env.example` as `#EMORI_GAZE_ANCHOR_RECORDED=yaw` (commented out: the code default applies).

### `EMORI_RECORDED_BLEND_S`

```text
Seconds over which a recorded move eases in from where the head is commanded to be,
instead of snapping onto its first frame (up to 46 deg measured). 0 = snap.
```

Shipped in `.env.example` as `#EMORI_RECORDED_BLEND_S=0.3` (commented out: the code default applies).

### `EMORI_RECORDED_SKIP_LEADIN`

```text
0 plays a recorded move from its first frame, including the stillness some of them open with,
instead of from just before its first motion.
```

Shipped in `.env.example` as `#EMORI_RECORDED_SKIP_LEADIN=1` (commented out: the code default applies).

### `EMORI_V2_FENCE_FILE`

```text
The file the motor container binds, so that it cannot start once the fence is down. /dev/null
is for a trial by hand, with no fence (deploy/robot/ros2_native/README.md). Compose reads this
from the shell or from deploy/ros2/.env.
```

Shipped in `.env.example` as `#EMORI_V2_FENCE_FILE=/opt/emori/V2_OWNS_THE_BUS` (commented out: the code default applies).

### `EMORI_RECORDED_FIT`

```text
0 sends a recorded move's head to the motors as authored. Pollen's IK refuses a target it cannot
reach and the head then stands still, and a servo does not follow a goal past its limit; 1 draws
the head's translation in until the motors can reach it, and keeps the rotation that makes the
gesture.
```

Shipped in `.env.example` as `#EMORI_RECORDED_FIT=1` (commented out: the code default applies).

### `EMORI_GAZE_MODEL`

```text
The BAKED path inside the image. Leave it here: a fresh container then works with no
host state at all. The host's /opt/emori/models is mounted read-only BESIDE it at
/opt/emori/models-host (never over it -- a mount on the baked path would shadow the
bake, and on a robot where install.sh step 5b has not run Docker would create the host
directory empty and mount that, leaving a detector with no model on a robot that still
reports healthy). To try a model or detector change without a 45-minute arm64 rebuild,
scp the file to /opt/emori/models and point this at /opt/emori/models-host/<file>.
```

Shipped in `.env.example` as `#EMORI_GAZE_MODEL=/opt/emori/models/face_detection_yunet_2026may.onnx` (commented out: the code default applies).

### `EMORI_MODELS_HOST`

```text
Where that read-only host copy comes from; compose mounts it at /opt/emori/models-host.
```

Shipped in `.env.example` as `#EMORI_MODELS_HOST=/opt/emori/models` (commented out: the code default applies).

**— the camera bridge's ROS tee (read by deploy/robot/camera-bridge.sh) —**

### `EMORI_WITH_ROS2`

```text
These live in /etc/default/emori-camera on the robot, NOT in this file -- the bridge
unit reads that EnvironmentFile and nothing else. They are documented here because
an operator looking for "why is the face topic empty" reads .env first, and because
EMORI_CAM_ROS_VIDEO_NR is the one-line rollback for the whole camera tee:
empty it, `sudo systemctl restart emori-camera-bridge`, and the pipeline is
character-for-character the one without the tee.
  EMORI_CAM_ROS_VIDEO_NR=41   the second v4l2loopback node; EMPTY = no tee at all
  EMORI_CAM_ROS_WIDTH=320 / _HEIGHT=240 / _FPS=10   what the tee branch produces
Opt-in for the containerised ROS 2 graph: install.sh step 11 (which loads the
linux/arm64 image built off the robot and enables deploy/robot/emori-ros2.service).
While this is 0 the install is byte-identical to a robot that never had the step.
On v1 the robot has no native ROS 2 -- Humble runs in one host-networked container
pointed at ws://127.0.0.1:8090/ws/relay (v2 runs natively: docs/ros2-v2-native-robot.md).
```

Shipped in `.env.example` as `#EMORI_WITH_ROS2=0` (commented out: the code default applies).

readycheck's `ros2` gate checks the ROS container when this is `1`, **and on every robot
whose v2 fence is up** (`/opt/emori/V2_OWNS_THE_BUS`), with no line here: with the fence
up Pollen's daemon cannot start, so the `emori-ros2-v2` container is the only thing that
moves the robot, and it is checked as `ros2_up_v2`. An explicit `EMORI_WITH_ROS2=0` does
not switch that check off on a fenced robot, because "no ROS graph here" is false there by
construction; lowering the fence (the v2 README's "Rolling back to v1") is what takes a
robot off v2, and the gate back to `ros2_lane_off`. `EMORI_WITH_ROS2=1` keeps working on a
v2 robot that has it, and a v2 robot on trial (no fence) needs it for the check. On a fenced
robot a running v1 graph is `v1_graph_on_v2_robot` (FAIL), the native route
(`reachy-mini-ros2.service`, no container) is judged by the shim on `:8000`
(`ros2_up_native`), and a docker that does not answer readycheck's user is
`docker_unreachable` (SKIP), never an absent container. On a fenced robot `install.sh` step
11 never installs the v1 graph (`emori-ros2.service`), whatever this says, and disables one
it finds enabled or active: one owner of the motor bus.

### `EMORI_ROS2_STACK`

```text
The ROS 2 stack every deploy leaves this robot on (deploy/robot/ros2_native/v2-stack.sh):
v2 (default) = the robot is a ROS 2 node, Pollen's daemon fenced off the motor bus;
v1 = Pollen's daemon owns it (Pollen's apps, SDK and dashboard need v1).
Deploy-to-Robot --v1 / --v2 writes this line.
```

Shipped in `.env.example` as `#EMORI_ROS2_STACK=v2` (commented out: unset means `v2`).

Read by the robot half of the one-click deploy after `install.sh`, on a full and a quick
deploy alike. `v2` is the v2 README's "Committed to v2": the v1 graph and Pollen's daemon
disabled, the daemon fenced (`zz-v2-fence.conf` and `/opt/emori/V2_OWNS_THE_BUS`), the
`emori-ros2-v2` container up, and the look-at camera tee armed (`EMORI_CAM_ROS_VIDEO_NR=41`
in `/etc/default/emori-camera`). It needs Docker and the image `emori/ros2-jazzy-v2:robot`
on the robot; the deploy computer installs the first and ships the second
(`scripts/deploy_to_robot.py`). Without them the robot stays on v1 and the deploy says why
(`no_docker`, `no_image`); the next deploy tries again. When the container does not answer
as `reachy_mini_ros2` on `:8000` within 180 s, the robot goes straight back to v1, so its
motors always have an owner. `v1` is the README's "Rolling back to v1": the tee off, the
fence lowered, the container down, Pollen's daemon back; the v1 graph comes back where its
image is still on the robot, and otherwise `EMORI_WITH_ROS2=1` becomes `0`; an
`EMORI_READYCHECK_ROS_VIDEO_DEVICE` naming the tee it switched off is commented out. An
unknown value stops the stack step and leaves the robot as it was.

The verdict is in `artifacts/last-ros2-stack.env` (`stack`, `changed`, `reason`), and the
deploy's last screen repeats it.

## Hub (the hostapp hosted on the ROS 2 graph)

The hub is `python -m emori_hub.main`: the same FastAPI app on the same port, with rclpy
beside it. `HOSTAPP_HOST`, `HOSTAPP_PORT` and `HOSTAPP_WS` mean what they mean for the
hostapp. Its container sets `EMORI_HUB_ENV_JSON`, the file the unit's environment was written
to; the hub reads that before anything else, leaving out the names that describe the host's
own process (`PATH`, `HOME`, `ROS_*` and the like), and does not start if the file cannot be
read.

### `EMORI_ROBOT_TRANSPORT`

```text
ros (default)   the robot client goes over the robot node's services, on node /emori/robot_link
http            the robot client is the REST one, at REACHY_BASE_URL; no link node is started
```

`GET /healthz` reports which one is running as `runtime`. Any other value stops the hub at
start, because a typo must not pick a transport. Read only by the hub: the hostapp's own
entry point always uses REST.

### `EMORI_HUB_FACADES`

```text
1 (default)   the hub hosts /emori/session, /emori/speech, /emori/gesture, /emori/motion,
              /emori/gateway and /emori/addon
0             it hosts none of them, and is the host app over ROS and nothing else on the graph
```

Read when the unit starts, so a change takes a restart of the hub. A facade that cannot be
built costs what it publishes: the kiosk and the sessions run on, the journal and
`/api/robot/status` name it, `/diagnostics` does too while the gateway is hosted, and readycheck
fails, so a switch to the hub rolls back. At `0` nothing publishes `/diagnostics`, and an add-on's
`ExtensionContext.ros` is `None`.

### `EMORI_HUB_MOTION_SUBMIT`

```text
Whether the hub answers /emori/motion/submit_batch, which hands a batch of moves to the
motion player. 0 (default): every call is refused with `disabled`, before anything of the
player is read. 1: a batch is validated (at most 32 moves; goto, look_at, antennas,
set_target, wake_up or play_recorded; finite numbers; a duration of at most 30 s) and goes in
through the same entry the session's own motion uses, with the same ownership guard and the
same voice mark. It is refused with `session_active` for as long as a training session is
open, so a batch from the graph can never queue ahead of a sentence's plan, and it gives way if
one opens while it plays. Like /api/skill it is refused with `not_motion_owner` unless the host
application's lane owns motion, and with `head_reserved` while the head is an input device: on a
push-turn robot that is all the time it is idle, so this service is for a robot on another
gesture mode. `busy` means a badge's lease, a running mode or a person at the landing page. Only
1, true, yes or on turn it on.
```

Shipped in `.env.example` as `#EMORI_HUB_MOTION_SUBMIT=0` (commented out: the code default applies).

The hub's status and the player's state are on the graph whether or not this is set:
`/emori/motion/player` (latched, on change), `/emori/motion/plan_events` (one message per step of
a sentence's motion plan), `/emori/addon/status` (latched, on change) and
`/emori/gateway/status` (a service), and `/diagnostics` carries one entry per part of the hub,
including the counters of whatever a topic lost to keep the session's motion on time.

### `EMORI_HUB_GRAPH`

```text
0 (default)   the graph's four nodes run in the motor container, on the relay's WebSocket
1             /emori_adapter, /hri_context_analyzer, /hri_engagement_behaviors and /skill_facade
              run in the hub, on an executor of their own (ros-graph), and the adapter node
              is fed the relayed envelopes in-process
```

Only `1` turns it on. It goes with `EMORI_MOTOR_LAUNCH=robot.launch.py`: on any other pairing
each node is missing or runs twice, so `deploy/robot/hub-stack.sh` sets the two together
(`--with-motor`), and refuses a robot where they disagree. With it on, the hub sets
`EMORI_SKILL_TRANSPORT=ros_native` for its whole process, which also makes the session code
ignore `EMORI_ROS_OWNER_FALLBACK_S`. `EMORI_ROS_MOTION_OWNER` means what it always meant: on
`hostapp`, `/skill_facade` counts each request and sends nothing, so the graph in the hub adds
no source of motion. The FIWARE bridge does not run in the hub. Read when the unit starts.

### `EMORI_MOTOR_LAUNCH`

```text
v2.launch.py (default)   the motor container runs the robot node and the graph's four nodes
robot.launch.py          the motor container runs the robot node alone
```

Set in `deploy/ros2/.env`, where compose reads it for the motor container, and not in
`/opt/emori/.env`. A changed value recreates the container on the next `up`, which parks and
wakes the robot, so change it through `deploy/robot/hub-stack.sh apply ros --with-motor` and
`apply hostapp --with-motor`.

## Readiness self-check (emori-readycheck; read-only)

### `EMORI_READYCHECK_HOSTAPP_URL`, `EMORI_READYCHECK_VIDEO_DEVICE`

```text
Both default to the deployed values, so leaving them unset changes nothing.
Override only when checking a stack that is not on this box's defaults.
```

Shipped in `.env.example` as `#EMORI_READYCHECK_HOSTAPP_URL=http://127.0.0.1:8090`, `#EMORI_READYCHECK_VIDEO_DEVICE=/dev/video40` (commented out: the code default applies).

The matrix prints a PASS gate's detail on its own line under a `  = <gate>: ` marker; today
only `site_config` has one: the `KEY=source` pairs that show where each setting came from.
The `--json` output carries the same detail in that gate's `hint`.

### `EMORI_READYCHECK_ROS_VIDEO_DEVICE`

```text
The v2 face look-at tee device, as an OVERRIDE. Unset, readycheck reads the device
number where the tee is armed: EMORI_CAM_ROS_VIDEO_NR in /etc/default/emori-camera
(the camera bridge's own EnvironmentFile; the last assignment wins, as in systemd).
So arming the tee there is the whole opt-in, and the gaze gate checks /dev/video<N>
with no .env line. No tee there and nothing here = PASS gaze_lane_off, which is right
for a v1 robot and for a v2 robot that has not turned look-at on.
```

Shipped in `.env.example` as `#EMORI_READYCHECK_ROS_VIDEO_DEVICE=/dev/video41` (commented out: the code default applies).

Before this was read from the camera file, a robot deployed with only its token reported
`gaze_lane_off` while its tee was running, because nobody had copied the number into
`.env`. An `EMORI_READYCHECK_ROS_VIDEO_DEVICE` line an older robot carries keeps working
and keeps winning.

## Robot-local settings layer

### `EMORI_HARDWARE_PROFILE`

```text
What this robot is. install.sh writes reachy-mini-wireless; a dev box leaves it
empty. It supplies the tuning in the table below when those lines are empty, and
whatever hardware values an add-on contributes for it.
It also marks an installed robot: only there may an add-on pin shipped values.
An unknown name supplies no hardware values (logged) but still marks the robot as
installed.
```

Shipped in `.env.example` as `EMORI_HARDWARE_PROFILE=`.

`reachy-mini-wireless` also supplies the tuning measured on a Reachy Mini
Wireless, so a robot deployed with only its token runs as that robot does. Each value is
the source `hardware` on the admin page's Settings tab and in readycheck's `site_config`
gate; a value in `.env` or saved on the admin page wins, and every other robot (the
simulator included) keeps the code default:

| Setting | Profile | Code default | Why (measured on a Reachy Mini Wireless) |
|---|---|---|---|
| `EMORI_SESSION_START_MS` / `_S` | `120000` / `130` | `45000` / `45` | the CM4 loads the session page slowly: a cold session's first AI line came at 48.7 s |
| `EMORI_CUE_BODY_YAW` | `1` | off | expression motion run since August without a motor error |
| `EMORI_CUE_Z_MM` | `18` | `8.0` | as above |
| `EMORI_CUE_AMP` | `1.6` | `1.0` | the amplitude the robot runs |
| `EMORI_TT_OVERRIDES` | the kiosk patch it runs (see its entry) | inert | kiosk turn-taking on this robot |

A key the profile supplies must not be assigned a value in `.env.example`: a shipped
non-empty line is an explicit `.env` value on every robot copied from it, and it outranks
the profile. That is why the session-start pair ships empty. A robot whose `.env` predates
this keeps its explicit values, profile or not; the one exception is the old shipped
session-start pair `45000` / `45`, which `install.sh` empties on a Reachy Mini Wireless
([its entry](#emori_session_start_ms-emori_session_start_s)). Any other key an older
`.env` sets keeps winning until its line is emptied
([Upgrading a robot](deploy-a-new-robot.md#upgrading-a-robot)).

What no Python process reads is out of this profile's reach and carries its robot value
where it is read: the v2 container's face-detection rate is the robot compose file's own
default (`EMORI_GAZE_DETECT_HZ`, `5.0`), and the host app's stop timeout is its unit's
(`TimeoutStopSec=12` in `deploy/robot/emori-hostapp.service`: the shutdown joins its
workers within about 9 s and finalizes nothing, so systemd's default 90 s only ever waited
on a wedged audio stream).

### `EMORI_SITE_AUTO`

```text
1 (default): settings left empty are filled from this robot's Pokamind record
(course, pathways, display name, boot language), and its spoken
menu is built from its Pokamind modules (configs/site/auto/).
0: switch all of that off, for one robot, without a code change: nothing is derived
from Pokamind, and the menu falls back to configs/site, configs/prod, the
development menus, the built-in one. The sync itself still runs (Test connection
and readycheck keep working). Only the exact value 0 switches it off.
```

Shipped in `.env.example` as `EMORI_SITE_AUTO=1`.

### `EMORI_SITE_AUTO_APPLY_IDLE_S`

```text
Read by emori-admin. Seconds of CONTINUOUS idle (the host app's own answer: no
session, no QR training lease, no add-on mode running)
before emori-admin restarts the host app by itself to put a pending change in
force, for these reasons only: scope_changed and any reason an add-on adds (a change
that came from this robot's Pokamind record) and dashboard_password (a dashboard
password regenerated on the Settings tab). At most once per 30 minutes, never
during a session. A value an operator saved (settings_saved)
always waits for the Settings tab's Apply now. Default 600. 0 switches the
automatic restart off (Apply now still works); an empty, negative or non-numeric
value means 600.
```

Shipped in `.env.example` as `#EMORI_SITE_AUTO_APPLY_IDLE_S=600` (commented out: the code default applies).

## THIS robot's Pokamind token (per robot)

### `EMORI_POKAMIND_TOKEN_FILE`, `POKAMIND_ROBOT_TOKEN`

```text
Every robot has its OWN Pokamind credential: a per-robot Ed25519 token that only
Pokamind issues, for this one robot. The robot signs its learner logins and its
control-plane calls with it. It is ONE line:
    pkr1.<robot-id>.<key>
Pokamind keeps only the public half, scoped to one organisation, one course, its
languages and its module list, so this robot can open exactly those sessions and
nothing else.

It lives in a FILE, not in .env: /opt/emori/secrets/pokamind_robot_token (directory
0700, file 0600, owner pollen). Deploy-to-Robot asks for it and installs it there
over SSH; the admin page's Pokamind tab (:8070) tests it, replaces it (the kiosk
restarts) and, after the PIN is typed again, reveals it for 30 seconds.

Its Pokamind identity is the token's robot_id, and Deploy-to-Robot fills an empty
EMORI_ROBOT_ID from it; set to a DIFFERENT robot, EMORI_ROBOT_ID is logged as an
ERROR and readycheck fails robot_id_mismatch (the token still wins).
The token also brings this robot's scope: the robot reads its Pokamind record with it
(GET /api/robot/whoami, at start, every 6 h, from the admin page and whenever a
token is installed), keeps the last good copy in configs/site/pokamind_scope.json,
and fills the course, languages, pathways, display name and menu
from it unless they are set above. A course set by hand must be the token's course.

EMORI_POKAMIND_TOKEN_FILE: where the token file lives, relative to /opt/emori. Leave
it unset.
POKAMIND_ROBOT_TOKEN: DEV AND TESTS ONLY, the token itself, read only when the token
file is absent or empty. On a robot use the file: an .env gets copied around far too
easily, and one token is one robot.
```

Request, install, replace and revoke, step by step: [deploy-a-new-robot.md](deploy-a-new-robot.md).

Shipped in `.env.example` as `#EMORI_POKAMIND_TOKEN_FILE=secrets/pokamind_robot_token`, `#POKAMIND_ROBOT_TOKEN=` (commented out: the code default applies).
