#!/usr/bin/env bash
# One-command EMORI on-robot install — the repeatable "skill" deploy.
#
# Idempotent: safe to re-run. Captures the exact steps proven live on the physical
# Reachy Mini (ReachyMiniOS v0.2.3 / daemon v1.9.0, Debian Trixie aarch64).
# Run on the CM4 as user pollen (passwordless sudo assumed):
#
#     cd /opt/emori && bash deploy/robot/install.sh
#
# It needs a .env (Deploy-to-Robot writes one from .env.example; by hand: copy it). It
# generates every value nobody should have to type (step 8: the admin PIN when none is
# set, admin token, cookie salt, hotspot ssid/password, the dashboard password and, on a
# fresh robot, its salt, the ROS skill key and id salt, and the hardware profile), and
# only ever into a key that is absent or empty -- never over a value an operator set,
# in .env or on the admin page (secrets/admin.env). The one value it takes back is one
# an older template shipped: the 45 s session-start deadline on a Reachy Mini Wireless,
# whose hardware profile has its own (step 8). What is specific to the robot's site
# (course, languages, pathways, menus) is not in .env at all: it comes from
# the robot's Pokamind record once its token is installed (src/emori_adapter/site_settings.py).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
echo "== EMORI install from $ROOT =="

# Write KEY=VALUE into .env unless it already holds a non-empty value. Returns 0
# when it wrote something, 1 when the operator's value was left alone.
#
# Two hazards it exists to avoid:
#  * a present-but-EMPTY line (.env.example ships several) must be REPLACED, not
#    joined by a second assignment whose winner depends on the parser;
#  * a .env whose last line lost its trailing newline (an editor that strips it,
#    a printf one-liner, a copy-paste through a tool that trims) would otherwise
#    FUSE with the appended line -- silently mangling the LAST secret in the
#    file (the admin token, the dashboard salt...), i.e. the installer breaking
#    the robot while adding a key.
# Values are generated here and are always [0-9A-Za-z_-], so the sed is safe.
env_ensure() {
  if grep -q "^$1=.\+" .env 2>/dev/null; then
    return 1
  fi
  env_set "$1" "$2"
  return 0
}

# Whether the admin page saved KEY (secrets/admin.env: emori-admin writes the PIN, the
# hotspot and the dashboard password there when they are changed on the phone). That
# value outranks .env (site_settings.apply, and the units' EnvironmentFile order), so
# nothing is generated over it and .env's older copy is never shown as the one in force.
admin_env_has() {
  grep -q "^$1=.\+" secrets/admin.env 2>/dev/null
}

# Set KEY=VALUE on exactly ONE line, whatever .env held: the first KEY= line is
# rewritten in place and every later one deleted, or the line is appended (the same
# trailing-newline care as above). dotenv and systemd keep the LAST assignment, so
# rewriting every KEY= line -- or only the first -- leaves a duplicate that decides.
# Callers only ever drop lines they may: empty ones, or names this installer generated.
env_set() {
  if grep -q "^$1=" .env 2>/dev/null; then
    sed -i -e "0,/^$1=/{" -e "s|^$1=.*|$1=$2|" -e "b" -e "}" -e "/^$1=/d" .env
  else
    if [ -s .env ] && [ -n "$(tail -c1 .env)" ]; then echo >> .env; fi
    printf '%s=%s\n' "$1" "$2" >> .env
  fi
}

# 0. Guard: LF line endings (a CRLF shebang makes systemd fail 203/EXEC).
find "$ROOT" \( -name '*.sh' -o -name '*.conf' -o -name '*.service' \) -not -path '*/.venv/*' \
  -exec sed -i 's/\r$//' {} +
chmod +x deploy/robot/*.sh

# 1. System packages: v4l2loopback + GStreamer/libcamera + Chromium + Xvfb.
#    unzip: step 3 (scripts/get_voice_models.sh) unpacks the Vosk models with it, and a
#    fresh Debian image does not have it -- without it the first full install on a new
#    robot dies there with exit 127, before any unit is installed.
#    --no-upgrade: a missing package is installed, an installed one is left as it is. A
#    plain `install` upgrades whatever the refreshed lists have newer, so every redeploy of
#    a robot in service would swap its kiosk browser and camera stack unannounced; OS
#    upgrades are the owner's call, not a side effect of shipping code.
echo "== [1/10] apt packages =="
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-upgrade \
  v4l2loopback-dkms v4l2loopback-utils \
  gstreamer1.0-libcamera gstreamer1.0-tools \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-base \
  chromium xvfb unzip

# 2. Python venv + EMORI. Each optional add-on shipped under addons/ (Deploy-to-Robot
#    copies them there; a robot deployed from the public tree has none) is installed
#    into the same venv, after the core, so its dependency on emori-adapter is met.
echo "== [2/10] venv + pip install =="
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -U pip -q
.venv/bin/pip install -e ".[hostapp,pokamind,speech,stt]" -q
ADDON_DIRS=()
for addon in addons/*/; do
  [ -f "${addon}pyproject.toml" ] && ADDON_DIRS+=("${addon%/}")
done
for addon in ${ADDON_DIRS[@]+"${ADDON_DIRS[@]}"}; do
  echo "  add-on $(basename "$addon")"
  .venv/bin/pip install -e "$addon" -q
done

# 2b. A Pokamind token Deploy-to-Robot STAGED because this robot could not ask Pokamind
#     about it yet (a first deploy has no venv before step 2). Asked now, with the venv
#     just built, and installed only if Pokamind does not refuse it -- BEFORE step 8
#     names the hotspot after the token's robot and step 8b/9 start the units with it.
#     A refusal does not stop the install: the robot keeps the token it had (or none).
echo "== [2b/10] staged Pokamind token =="
TOKEN_RC=0
bash deploy/robot/install-token.sh || TOKEN_RC=$?
case "$TOKEN_RC" in
  0) ;;
  4) echo "  (none)" ;;
  5) echo "  !! the staged token was REFUSED by Pokamind and NOT installed (reason above)." ;;
  7) echo "  !! Pokamind could not be asked about the staged token: NOT installed; the robot"
     echo "     keeps its working token and the new one stays staged (reason above)." ;;
  *) echo "  !! the staged token could not be installed (exit $TOKEN_RC)." ;;
esac

# 3. Voice + STT models. ALWAYS run the downloader: it is per-model idempotent
#    ("already present" per language) and costs one stat() per file when nothing
#    is missing. Gating the whole step on voices/ca.onnx would mean a robot deployed
#    before a language was added never gets it, and /api/tts for that language would
#    then read it with the first configured voice (PiperTts falls back to it).
#
#    A model that cannot be downloaded is a WARNING, never the end of the install: the
#    robot's internet is whatever the site gives it, and one cut connection during a
#    large voice must not abort here, before a single unit is installed, with
#    Deploy-to-Robot reporting "THE ROBOT'S INSTALLER FAILED". The language falls back
#    to the first voice until the next deploy fetches it; readycheck's tts gate names it
#    (voice_missing).
echo "== [3/10] speech models =="
if ! bash scripts/get_voice_models.sh; then
  echo "  !! WARNING: not every speech model could be downloaded (named above). The install"
  echo "     goes on; those languages fall back until they are installed. Once the robot"
  echo "     is online: cd /opt/emori && bash scripts/get_voice_models.sh, then"
  echo "     sudo systemctl restart emori-hostapp (or simply deploy again)."
fi

# 4. Configs + .env sanity.
echo "== [4/10] configs =="
[ -f configs/roster.yaml ] || cp configs/roster.example.yaml configs/roster.yaml
# Names, e-mails and the badge ids that log people in: the hostapp's user only (the
# admin page's People tab writes it 0600 too; cp would have left it 0644).
chmod 600 configs/roster.yaml 2>/dev/null || true
[ -f configs/voice_topics.yaml ] || cp configs/voice_topics.example.yaml configs/voice_topics.yaml
# The robot-local settings layer's directories (src/emori_adapter/site_settings.py).
# configs/site/ holds what the admin page saved (settings.env, saved menus) and what came
# from Pokamind (pokamind_scope.json, auto/ menus); secrets/ holds the robot's token and
# the secrets the admin page wrote. Created when missing and NEVER emptied: no deploy
# ships a file into either, and nothing here touches what is inside them.
mkdir -p configs/site/auto secrets
sudo chown pollen:pollen configs/site configs/site/auto secrets 2>/dev/null || true
chmod 755 configs/site configs/site/auto 2>/dev/null || true
chmod 700 secrets 2>/dev/null || true
# --- the spoken menu ------------------------------------------------------------------
# Where each language's menu comes from, first match wins (server._menu_candidates):
# EMORI_VOICE_TOPICS_<LANG> in .env; configs/site/voice_topics.<lang>.yaml (saved on the
# admin page's Menu tab); configs/prod/ (only in a fork that ships a fixed menu); the
# menu built from this robot's Pokamind record (configs/site/auto/, rebuilt on every
# token sync); the development menus. Nothing is copied here. Do not reintroduce a copy:
# a file at configs/voice_topics.<lang>.yaml can only shadow, never help.
menu_origin() {  # LANG SITE_FILE FORK_FILE -- where this language's menu will come from
  local upper
  upper="$(printf '%s' "$1" | tr 'a-z' 'A-Z')"
  if grep -Eq "^EMORI_VOICE_TOPICS(_$upper)?=.+" .env 2>/dev/null; then
    echo "the EMORI_VOICE_TOPICS override in .env"
  elif [ -f "$2" ]; then
    echo "$2 (saved on this robot)"
  elif [ -f "$3" ]; then
    echo "$3 (shipped by this fork)"
  else
    echo "this robot's Pokamind record (configs/site/auto/, built once the token is synced)"
  fi
}
echo "  .. menu en: $(menu_origin en configs/site/voice_topics.en.yaml configs/prod/voice_topics.yaml)"
echo "  .. menu ca: $(menu_origin ca configs/site/voice_topics.ca.yaml configs/prod/voice_topics.ca.yaml)"
# Retire the copies older installs left behind. They sit BELOW every real source in the
# search order, so they change nothing about what the robot speaks -- but an operator who
# edits one and sees no effect is an operator debugging the wrong file.
for stale in configs/voice_topics.en.yaml; do
  if [ -f "$stale" ]; then
    mkdir -p artifacts/superseded
    mv -f "$stale" "artifacts/superseded/$(basename "$stale")" 2>/dev/null || true
    echo "  .. retired $stale (never read: the menu sources are listed above) -> artifacts/superseded/"
  fi
done
echo "  .. active menus:"
.venv/bin/python - <<'PY' || true
from emori_adapter.hostapp import server
from emori_adapter.hostapp.lang_state import SUPPORTED_LANGS
for lang in SUPPORTED_LANGS:
    source = server._menu_source(lang) or "(built-in default)"
    origin = getattr(server, "menu_source", None)
    tag = f"  [{origin(lang)}]" if callable(origin) else ""
    note = f"  [{server.menu_override_env(lang)}]" if server.menu_override_env(lang) else ""
    print(f"     {lang}: {source}{tag}{note}")
PY
# A roster still byte-identical to the shipped example has nobody on it yet. That is a
# working robot (the anonymous drop-in needs no roster): people and their QR badges are
# added on the admin page's People tab, which also checks e-mail domains and pathways
# against this robot's Pokamind record.
mkdir -p artifacts
if cmp -s configs/roster.yaml configs/roster.example.yaml; then
  echo "  .. configs/roster.yaml has no people yet: add them (and print their badges) on the"
  echo "     admin page's People tab. The anonymous drop-in works without any."
  date -u +%Y-%m-%dT%H:%M:%SZ > artifacts/roster-unedited.marker 2>/dev/null || true
else
  rm -f artifacts/roster-unedited.marker
fi
if [ ! -f .env ]; then
  echo "  !! no .env — copy .env.example to .env (Deploy-to-Robot writes one for you), then"
  echo "     re-run. Everything specific to this robot comes from its Pokamind token, which"
  echo "     is pasted on the admin page's Pokamind tab (docs/deploy-a-new-robot.md)."; exit 1
fi
mkdir -p artifacts

# 5. Camera loopbacks at /dev/video40 (kiosk) and /dev/video41 (ROS robot node).
#    video10 is a bcm2835 codec node on this image; video10-31 are all taken.
#
#    BOTH nodes, in ONE options line, because this script is the REPEATABLE deploy: it
#    runs again on every skill update, and a one-node line here would silently revert
#    the two-node line setup-camera-bridge.sh writes at first install. The next reboot
#    would then load the module with video40 only, and `devices:` in
#    deploy/ros2/docker-compose.v2.robot.yml naming a missing /dev/video41 fails
#    container CREATION -- i.e. emori-ros2-v2, which owns /dev/ttyAMA3 and the 50 Hz
#    control loop, would not come back and the robot would have no motor control at all.
#    Keep this line byte-identical to setup-camera-bridge.sh's (tests/test_ros2_v2_deploy.py
#    asserts they are the same string, so the two can never drift again).
#
#    video_nr, exclusive_caps and card_label are ARRAYS and must be the same length;
#    max_buffers is NOT an array: it is one int for the whole module (v0.15.3
#    modinfo: `max_buffers ... (int)`). `max_buffers=2,2` fails every load with EINVAL.
#    A short exclusive_caps array silently leaves the extra node
#    non-exclusive, which is a node Chromium will list and a second consumer can steal.
ROS_VIDEO_NR="${EMORI_CAM_ROS_VIDEO_NR:-41}"
echo "== [5/10] camera loopbacks (video40 kiosk + video${ROS_VIDEO_NR} ROS) =="
echo v4l2loopback | sudo tee /etc/modules-load.d/v4l2loopback.conf >/dev/null
echo "options v4l2loopback video_nr=40,${ROS_VIDEO_NR} exclusive_caps=1,1 card_label=\"ReachyCam\",\"ReachyCamROS\" max_buffers=2" \
  | sudo tee /etc/modprobe.d/v4l2loopback.conf >/dev/null
sudo modprobe -r v4l2loopback 2>/dev/null || true
sudo modprobe v4l2loopback video_nr="40,${ROS_VIDEO_NR}" exclusive_caps=1,1 \
  card_label="ReachyCam","ReachyCamROS" max_buffers=2

# 5b. The v2 face look-at camera path. Installed on every robot, ARMED on none: the
#     helper and the model are laid down here so that turning look-at on later is one
#     variable and a bridge restart rather than a deploy, and so that a robot that never
#     turns it on is byte-identical to one that never had this step.
#
#     The loopback node itself was already dealt with in step 5: the persisted module
#     options create video41 beside video40 at module LOAD, which is what guarantees it
#     exists before docker.service starts the v2 container -- `devices:` naming a
#     missing node fails container creation. ros-loopback.sh is the no-reboot path for
#     the same node, and needs no copying: the unit runs it out of the checkout at
#     /opt/emori/deploy/robot/, exactly as it runs camera-bridge.sh, and step 0 above
#     already chmod +x'd deploy/robot/*.sh.
echo "== [5b/10] v2 face look-at (helper + detector weights; this step arms nothing) =="
test -x deploy/robot/ros-loopback.sh || { echo "  !! ros-loopback.sh is not executable"; exit 1; }
sudo mkdir -p /opt/emori/models
# The image BAKES this same model at /opt/emori/models/ and that baked copy is what the
# node reads by default, so a fresh container works with no host state at all. This host
# copy is mounted read-only at /opt/emori/models-HOST (deliberately beside, never over,
# the baked path -- a mount on top would shadow the bake and, on a robot where this step
# has not run, Docker would create an empty directory and the detector would find no
# model while the node still reported healthy). Point EMORI_GAZE_MODEL at
# /opt/emori/models-host/<file> to test a model change without a 45-minute arm64 rebuild.
sudo cp deploy/ros2/models/face_detection_yunet_2026may.onnx /opt/emori/models/
sudo chmod 0644 /opt/emori/models/face_detection_yunet_2026may.onnx
echo "  /opt/emori/models: $(sha256sum /opt/emori/models/face_detection_yunet_2026may.onnx | cut -c1-16)…"
# The tee is armed in ONE place, the bridge's own file; readycheck's gaze gate reads the
# device number there too, so .env needs nothing (EMORI_READYCHECK_ROS_VIDEO_DEVICE only
# overrides it). The last assignment wins, as in the bridge's EnvironmentFile. The path
# is overridable only so the tests can point it somewhere that is not /etc.
CAM_DEFAULTS="${EMORI_CAMERA_DEFAULTS:-/etc/default/emori-camera}"
CAM_TEE_NR="$(grep -E '^[[:space:]]*EMORI_CAM_ROS_VIDEO_NR=' "$CAM_DEFAULTS" 2>/dev/null \
  | tail -n1 | cut -d= -f2- | tr -d "\r\"' " || true)"
if [ -n "$CAM_TEE_NR" ]; then
  echo "  look-at camera tee ARMED: EMORI_CAM_ROS_VIDEO_NR=${CAM_TEE_NR} in ${CAM_DEFAULTS}"
  echo "                   (readycheck's gaze gate checks /dev/video${CAM_TEE_NR} from there)"
else
  echo "  to ARM look-at:  EMORI_CAM_ROS_VIDEO_NR=41 in /etc/default/emori-camera, then"
  echo "                   sudo systemctl restart emori-camera-bridge emori-kiosk"
  echo "                   (readycheck's gaze gate reads the number from that file)"
fi

# 6. User-session lingering — WITHOUT THIS THE ROBOT IS DEAF AND MUTE.
#
# PipeWire and WirePlumber are systemd USER services. With lingering off they run
# only while a human is logged in, and /run/user/1000 is not even created at boot.
# Both EMORI units set XDG_RUNTIME_DIR=/run/user/1000, so on a laptop-free boot:
#   - Chromium cannot open PipeWire ("Failed to create secure directory
#     /run/user/1000/pulse"), falls back to raw ALSA, asks the Reachy Mini USB card
#     for 48 kHz, gets 16 kHz, and EVERY TTS playback fails silently;
#   - the `pipewire` PortAudio device never appears.
# The robot then scans a badge, identifies the visitor, "speaks" a menu nobody hears,
# and times out — indistinguishable from the robot ignoring people. An open SSH session
# to the robot hides all of this: it supplies the user session for as long as it lasts.
echo "== [6/10] user-session lingering (PipeWire at boot) =="
sudo loginctl enable-linger pollen
for i in $(seq 1 30); do
  [ -d /run/user/1000 ] && break
  sleep 1
done
[ -d /run/user/1000 ] || echo "  !! /run/user/1000 still absent — audio will not work"

# 7. Add-on install hooks. An add-on may ship deploy/install-hook.sh for what pip cannot do
#    (system packages, files it seeds). Each runs with this directory as its working
#    directory, before any unit starts, and may call env_ensure / env_set (exported here)
#    to add a key to .env that is absent or empty. A hook that fails is reported and does
#    not stop the install. With no add-on installed this step does nothing.
echo "== [7/10] add-on hooks =="
export -f env_ensure env_set
ADDON_HOOKS=0
for addon in ${ADDON_DIRS[@]+"${ADDON_DIRS[@]}"}; do
  if [ -f "$addon/deploy/install-hook.sh" ]; then
    ADDON_HOOKS=1
    echo "  $(basename "$addon")"
    bash "$addon/deploy/install-hook.sh"       || echo "  !! $(basename "$addon") install hook failed (exit $?)"
  fi
done
[ "$ADDON_HOOKS" = 1 ] || echo "  (none)"
# The hub container reads add-ons from its own site directory. Installed here, once the hooks
# have run, when the hub image is on this robot; the step does nothing otherwise.
sh deploy/hub/hub-refresh.sh addons

# 8. Admin secrets in .env — BEFORE any unit is started.
#
# Ordering is the whole point of this step's position. Both the hostapp and
# emori-admin read EMORI_ADMIN_TOKEN from .env once, at start (the hostapp's
# EnvironmentFile=-/opt/emori/.env plus config.py's load_dotenv at import;
# emori-admin's own site_settings.load_admin_environment); nothing re-reads .env
# at runtime, and `enable --now` on an
# already-active unit is a no-op start. Generating the token AFTER step 9 has
# started both units would leave every fresh install with a tokenless hostapp:
# /api/admin/* answers 503, the phone's Language tab says "hostapp admin
# disabled", and restarting emori-admin fixes the wrong side.
echo "== [8/10] admin secrets in .env =="
SECRETS_WROTE=0
# The admin PIN is the lock on the whole phone admin (:8070): without one every route
# answers 503, and a robot provisioned from a phone has no other way in. Generated when
# neither .env nor the admin page (secrets/admin.env) has one: 6 digits from
# /dev/urandom, shown ONCE at the end of this install with the other generated
# credentials. Change it on the admin page (it then lives in secrets/admin.env).
if ! grep -q '^EMORI_ADMIN_PIN=.\+' .env 2>/dev/null && ! admin_env_has EMORI_ADMIN_PIN; then
  PIN_GEN="$(LC_ALL=C tr -dc '0-9' < /dev/urandom 2>/dev/null | head -c 6 || true)"
  if [ "${#PIN_GEN}" != 6 ]; then
    PIN_GEN="$(python3 -c 'import secrets; print("%06d" % secrets.randbelow(10 ** 6))')"
  fi
  env_set EMORI_ADMIN_PIN "$PIN_GEN"
  unset PIN_GEN
  NEW_PIN=1
  SECRETS_WROTE=1
  echo "  generated EMORI_ADMIN_PIN (the phone admin's; shown at the end of this install)"
fi
# What this robot IS (site_settings.HARDWARE_PROFILES): its audio devices come from here
# whenever .env leaves them empty, and it marks an installed robot -- the only place the
# profile's defaults apply. Dev boxes never have it.
if env_ensure EMORI_HARDWARE_PROFILE reachy-mini-wireless; then
  echo "  set EMORI_HARDWARE_PROFILE=reachy-mini-wireless"
  SECRETS_WROTE=1
fi
# That profile supplies the session-start deadline too (120000 / 130), but only to an
# EMPTY line. Every .env (and robot.local.env) written from the template before it did
# holds the old shipped 45000 / 45: an explicit value, which outranks the profile, so a
# redeploy alone would leave the robot on the 45 s a cold CM4 session overruns. Exactly
# those two old values are emptied, judged on the last assignment as dotenv reads it;
# any other number is an operator's and stays. 45 s on purpose on this robot: save it on
# the admin page (configs/site/), which this never touches.
HW_PROFILE="$(grep -E '^EMORI_HARDWARE_PROFILE=' .env 2>/dev/null | tail -n1 | cut -d= -f2- \
  | tr -d "\r\"' " || true)"
if [ "$HW_PROFILE" = reachy-mini-wireless ]; then
  for OLD_START in EMORI_SESSION_START_MS=45000 EMORI_SESSION_START_S=45; do
    OLD_KEY="${OLD_START%%=*}"
    OLD_VALUE="$(grep -E "^${OLD_KEY}=" .env 2>/dev/null | tail -n1 | cut -d= -f2- \
      | tr -d "\r\"' " || true)"
    if [ "$OLD_VALUE" = "${OLD_START#*=}" ]; then
      env_set "$OLD_KEY" ""
      echo "  emptied ${OLD_START} (the old shipped default): the hardware profile supplies it"
    fi
  done
fi
# The ROS skill intake's key, generated whether or not the lane is on: inert while it is
# off (EMORI_SKILL_API=0), and turning it on later then needs nothing pasted. Never shown.
# EMORI_ROS_ID_SALT is deliberately NOT generated: unset, the ROS lane's pseudonyms use
# a fresh salt per process and cannot be linked across restarts; a fixed salt is a
# data-protection decision a person makes (docs/configuration.md).
if env_ensure EMORI_SKILL_KEY "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"; then
  echo "  generated EMORI_SKILL_KEY (the ROS skill intake's key)"
  SECRETS_WROTE=1
fi
if env_ensure EMORI_ADMIN_TOKEN "$(python3 -c 'import secrets; print(secrets.token_hex(16))')"; then
  echo "  generated EMORI_ADMIN_TOKEN (emori-admin <-> hostapp /api/admin/*)"
  SECRETS_WROTE=1
fi
# The cookie signature is HMAC(HMAC(salt, pin), issued) and the PIN is six
# digits by mandate, so the SALT is where the entropy has to come from: with a
# guessable one, anyone who observes a single cookie on the plain-HTTP LAN/AP
# tries all 10^6 PINs offline in about a second and keeps the PIN itself, past
# every rate limit and surviving expiry, logout and reboot.
if env_ensure EMORI_ADMIN_SALT "$(python3 -c 'import secrets; print(secrets.token_hex(16))')"; then
  echo "  generated EMORI_ADMIN_SALT (admin cookie key)"
  SECRETS_WROTE=1
fi
# The Wi-Fi watchdog's only action is "broadcast an access point", and with no
# AP identity of our own the daemon falls back to its published constants
# (reachy-mini-ap / reachy-mini, hardcoded in a pip-installable package). An
# automatic transition onto a network whose passphrase is printed in the
# vendor's source hands anyone in radio range the unauthenticated daemon on
# :8000 and the operator's plaintext admin cookie — so emori-admin now refuses
# to ARM the watchdog until these two are set. Generate them here, and print
# them: this AP is how a human gets back to a robot that lost the site Wi-Fi.
#
# The SSID names THIS robot: the robot id of its Pokamind token when one is installed
# (step 2b may just have put it there), else EMORI_ROBOT_ID, else its hostname. Every
# Reachy Mini ships with the hostname "reachy-mini", so a hostname-only SSID would give
# every robot at a site the same recovery hotspot, "emori-reachy-mini". At most 26
# characters follow "emori-" (the 32-byte SSID limit); a longer id loses its FRONT,
# never its end -- robots are numbered at the end (reception-01, -02), and a cut tail
# would give two of them one name.
AP_TOKEN_FILE="$(grep -E '^EMORI_POKAMIND_TOKEN_FILE=' .env 2>/dev/null | tail -n1 \
  | cut -d= -f2- | tr -d '\r" ' || true)"
case "$AP_TOKEN_FILE" in ''|*..*) AP_TOKEN_FILE=secrets/pokamind_robot_token ;; esac
AP_HOST=""
if [ -s "$AP_TOKEN_FILE" ]; then
  # The robot id of a line that passed the token grammar, cut with -s: never the key.
  AP_HOST="$(tr -d '\r' < "$AP_TOKEN_FILE" | grep -Ex \
    '[[:space:]]*pkr1\.[a-z0-9][a-z0-9-]{1,61}[a-z0-9]\.[A-Za-z0-9_-]{43}[[:space:]]*' \
    | cut -s -d. -f2 | head -n1 || true)"
fi
[ -n "$AP_HOST" ] || AP_HOST="$(grep -E '^EMORI_ROBOT_ID=' .env 2>/dev/null | tail -n1 \
  | cut -d= -f2- | tr -d '\r" ' || true)"
[ -n "$AP_HOST" ] || AP_HOST="$(hostname -s 2>/dev/null || true)"
AP_SLUG="$(printf '%s' "$AP_HOST" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9-' '-' \
  | sed 's/^-*//; s/-*$//' || true)"
if [ "${#AP_SLUG}" -gt 26 ]; then AP_SLUG="$(printf '%s' "${AP_SLUG: -26}" | sed 's/^-*//')"; fi
AP_SSID_GEN="emori-${AP_SLUG:-robot}"
# A later install renames only a hotspot it named itself: the name it last generated is
# kept in artifacts/ap-ssid.generated. Installs from before that file recognise their own
# names by the two earlier recipes (hostname, then EMORI_ROBOT_ID, cut at the end). Any
# other name is an operator's, never touched.
AP_MARK=artifacts/ap-ssid.generated
ap_generated() {  # NAME: did this installer generate it?
  if [ -f "$AP_MARK" ]; then
    [ "$1" = "$(cat "$AP_MARK" 2>/dev/null || true)" ]
    return
  fi
  [ "$1" = "emori-robot" ] && return 0
  local old cut
  for old in "$(hostname -s 2>/dev/null || true)" \
      "$(grep -E '^EMORI_ROBOT_ID=' .env 2>/dev/null | tail -n1 | cut -d= -f2- \
         | tr -d '\r" ' || true)"; do
    [ -n "$old" ] || continue
    old="$(printf '%s' "$old" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9-' '-' || true)"
    for cut in 20 26; do
      [ "$1" = "emori-$(printf '%s' "$old" | cut -c1-$cut | sed 's/-*$//')" ] && return 0
    done
  done
  return 1
}
# EVERY EMORI_AP_SSID line, not just the last: a duplicate (an empty one appended from
# .env.example, say) must neither hide an operator's name from this check nor be
# rewritten along with it. One non-empty line this installer did not generate makes the
# name the operator's, and then no line is touched.
AP_NOW=""   # the last non-empty value: the name the hotspot has now
AP_LINES=0
AP_AUTO=1
while IFS= read -r AP_VAL; do
  AP_LINES=$((AP_LINES + 1))
  [ -n "$AP_VAL" ] || continue
  AP_NOW="$AP_VAL"
  ap_generated "$AP_VAL" || AP_AUTO=0
done < <(grep -E '^EMORI_AP_SSID=' .env 2>/dev/null | cut -d= -f2- | tr -d '\r"' || true)
AP_WROTE=0
if admin_env_has EMORI_AP_SSID; then
  AP_AUTO=0   # named on the admin page: the operator's, and .env's copy is not in force
elif env_ensure EMORI_AP_SSID "$AP_SSID_GEN"; then
  AP_WROTE=1
elif [ "$AP_AUTO" = 1 ] && { [ "$AP_NOW" != "$AP_SSID_GEN" ] || [ "$AP_LINES" -gt 1 ]; }; then
  env_set EMORI_AP_SSID "$AP_SSID_GEN"   # one line, whatever duplicates there were
  if [ "$AP_NOW" != "$AP_SSID_GEN" ]; then
    echo "  the recovery hotspot is renamed $AP_SSID_GEN (was $AP_NOW, a name this installer"
    echo "  gave it); its password is unchanged"
    NEW_AP_NAME=1
  fi
  SECRETS_WROTE=1   # emori-admin rebrands the hotspot when it starts
elif [ "$AP_LINES" -gt 1 ]; then
  echo "  !! .env sets EMORI_AP_SSID $AP_LINES times. The hotspot name is the operator's, so"
  echo "     this install changes none of them -- but only the LAST one counts: keep one."
fi
if [ "$AP_AUTO" = 1 ]; then
  printf '%s\n' "$AP_SSID_GEN" > "$AP_MARK"
fi
if ! admin_env_has EMORI_AP_PASSWORD && env_ensure EMORI_AP_PASSWORD \
     "$(python3 -c 'import secrets,string; print("".join(secrets.choice(string.ascii_lowercase+string.digits) for _ in range(12)))')"; then
  AP_WROTE=1
fi
if [ "$AP_WROTE" = 1 ]; then
  SECRETS_WROTE=1
  NEW_AP=1
  echo "  generated the recovery hotspot (SSID + password; shown at the end of this install)"
fi
# The management dashboard (:8090/dashboard) refuses to serve without a shared password,
# and nothing else asks for one: a fresh robot would answer 503 "not configured" while
# the deploy's last screen printed its URL. Generated like the
# admin token -- only into an empty key, never over an operator's -- and shown ONCE, at
# the end. Lowercase letters and digits without 0/o/1/l: a manager types it on a phone.
if ! admin_env_has EMORI_DASH_PASSWORD && env_ensure EMORI_DASH_PASSWORD \
     "$(python3 -c 'import secrets; a="abcdefghijkmnpqrstuvwxyz23456789"; print("-".join("".join(secrets.choice(a) for _ in range(4)) for _ in range(3)))')"; then
  SECRETS_WROTE=1
  NEW_DASH=1
  echo "  generated EMORI_DASH_PASSWORD (the dashboard's; shown at the end of this install)"
fi
# The dashboard's person codes are HMAC(EMORI_DASH_SALT, learner); empty, they derive
# from EMORI_ROBOT_ID (metrics_store.resolve_salt), so changing the robot id later would
# turn every returning learner into a new one. Pinned to a random value here ONLY on a
# robot whose dashboard has no data yet: on one that has, a new salt would do exactly
# that to the learners already in it (docs/deploy-a-new-robot.md, "Upgrading a robot").
DASH_DB="$(grep -E '^EMORI_DASH_DB=' .env 2>/dev/null | tail -n1 | cut -d= -f2- \
  | tr -d '\r" ' || true)"
DASH_DB="${DASH_DB:-artifacts/dashboard.db}"
case "$DASH_DB" in /*) ;; *) DASH_DB="$ROOT/$DASH_DB" ;; esac
if [ ! -e "$DASH_DB" ]; then
  if env_ensure EMORI_DASH_SALT "$(python3 -c 'import secrets; print(secrets.token_hex(16))')"; then
    echo "  generated EMORI_DASH_SALT (the dashboard's person codes; set once, never rotate)"
    SECRETS_WROTE=1
  fi
elif ! grep -q '^EMORI_DASH_SALT=.\+' .env 2>/dev/null; then
  echo "  !! EMORI_DASH_SALT is empty and this robot already has dashboard data: its person"
  echo "     codes derive from EMORI_ROBOT_ID. Pin it before that id ever changes"
  echo "     (docs/deploy-a-new-robot.md, 'A robot without a token')."
fi
# Shown once, at the very END of this install (and even if a later step fails), so it is
# the last thing on the screen instead of a line lost in the middle of a long log. Under
# the one-click deploy, which runs readycheck AFTER this install, the same block is also
# written to EMORI_CREDENTIALS_FILE (0600): the deploy prints it again after readycheck,
# as the last thing on the screen, and deletes it.
show_new_credentials() {
  if [ -n "${EMORI_CREDENTIALS_FILE:-}" ]; then
    local out
    out="$(credentials_block)"
    if [ -n "$out" ]; then
      printf '%s\n' "$out"
      (umask 077; printf '%s\n' "$out" > "$EMORI_CREDENTIALS_FILE") 2>/dev/null || true
    fi
    return 0
  fi
  credentials_block
}
credentials_block() {
  if [ "${NEW_AP:-0}" != 1 ] && [ "${NEW_AP_NAME:-0}" = 1 ]; then
    echo
    echo "  !! the recovery hotspot is now called $(grep '^EMORI_AP_SSID=' .env | tail -n1 | cut -d= -f2- \
      || true) (same password, in /opt/emori/.env)"
  fi
  [ "${NEW_AP:-0}" = 1 ] || [ "${NEW_DASH:-0}" = 1 ] || [ "${NEW_PIN:-0}" = 1 ] || return 0
  echo
  echo "  !! WRITE THIS DOWN — generated by this install, shown once (also in /opt/emori/.env):"
  if [ "${NEW_AP:-0}" = 1 ]; then
    echo "     recovery hotspot  SSID     $(grep '^EMORI_AP_SSID=' .env | tail -n1 | cut -d= -f2- || true)"
    echo "                       password $(grep '^EMORI_AP_PASSWORD=' .env | cut -d= -f2- || true)"
    echo "                       (join it and open http://10.42.0.1:8070)"
  fi
  if [ "${NEW_PIN:-0}" = 1 ]; then
    echo "     admin page PIN    $(grep '^EMORI_ADMIN_PIN=' .env | tail -n1 | cut -d= -f2- || true)"
    echo "                       (http://<robot>:8070; change it there, under Settings)"
  fi
  if [ "${NEW_DASH:-0}" = 1 ]; then
    echo "     dashboard         http://<robot>:8090/dashboard"
    echo "                       password $(grep '^EMORI_DASH_PASSWORD=' .env | cut -d= -f2- || true)"
  fi
}
trap show_new_credentials EXIT

# 8b. THIS robot's own Pokamind token (docs/deploy-a-new-robot.md). Never generated here:
#     Pokamind issues it per robot, and Deploy-to-Robot or the admin page's Pokamind tab
#     installs it. This step keeps its directory and file private to the service user
#     (dir 0700, file 0600 -- every unit runs as pollen), says loudly when the robot has
#     no Pokamind credential at all, and restarts the hostapp at the end when the token
#     changed since the last install: the hostapp binds its credential at start, and
#     `enable --now` below is a no-op on a unit that is already running.
echo "== [8b/10] Pokamind robot token =="
TOKEN_FILE="$(grep -E '^EMORI_POKAMIND_TOKEN_FILE=' .env 2>/dev/null | tail -n1 | cut -d= -f2- \
  | tr -d '\r" ' || true)"
case "$TOKEN_FILE" in ''|*..*) TOKEN_FILE=secrets/pokamind_robot_token ;; esac
TOKEN_DIR="$(dirname "$TOKEN_FILE")"
mkdir -p "$TOKEN_DIR"
# Never chmod the install root itself (a token file configured at its top level).
if [ "$TOKEN_DIR" != "." ] && [ "$TOKEN_DIR" != "/" ]; then
  sudo chown pollen:pollen "$TOKEN_DIR" 2>/dev/null || true
  chmod 700 "$TOKEN_DIR"
fi
TOKEN_MARK=artifacts/pokamind-token.sha256
if [ -s "$TOKEN_FILE" ]; then
  sudo chown pollen:pollen "$TOKEN_FILE" 2>/dev/null || true
  chmod 600 "$TOKEN_FILE"
  # Only a line that passed the token grammar is cut, and with -s: a plain cut prints a
  # line with no "." unchanged -- for a hand-edited file holding just the seed, the
  # robot's private key in the install log.
  TOKEN_ROBOT="$(tr -d '\r' < "$TOKEN_FILE" | grep -Ex \
    '[[:space:]]*pkr1\.[a-z0-9][a-z0-9-]{1,61}[a-z0-9]\.[A-Za-z0-9_-]{43}[[:space:]]*' \
    | cut -s -d. -f2 | head -n1 || true)"
  if [ -n "$TOKEN_ROBOT" ] && [ "$(tr -d '\r' < "$TOKEN_FILE" | grep -c .)" = 1 ]; then
    echo "  token for robot '$TOKEN_ROBOT' ($TOKEN_FILE, 0600)"
  else
    echo "  !! $TOKEN_FILE is not one pkr1 token line: replace it (admin page -> Pokamind"
    echo "     -> Replace token, or Deploy-to-Robot --replace-token)"
  fi
  TOKEN_SUM="$(sha256sum "$TOKEN_FILE" | cut -d' ' -f1)"
  if [ "$TOKEN_SUM" != "$(cat "$TOKEN_MARK" 2>/dev/null || true)" ]; then
    (umask 077; printf '%s\n' "$TOKEN_SUM" > "$TOKEN_MARK")
    SECRETS_WROTE=1
  fi
elif grep -q '^POKAMIND_ROBOT_TOKEN=.\+' .env 2>/dev/null; then
  echo "  !! the token is in .env (POKAMIND_ROBOT_TOKEN): that is for dev boxes. Move it to"
  echo "     $TOKEN_FILE (Deploy-to-Robot or the admin page do this) and delete the line."
else
  echo "  !! NO Pokamind token yet: every session is a DEMO until this robot's token is"
  echo "     installed: admin page -> Pokamind -> Replace token, or Deploy-to-Robot. It"
  echo "     then configures itself (docs/deploy-a-new-robot.md)."
fi

# 8c. WHICH robot this is (src/emori_adapter/robot_identity.py): the board's serial and
#     Wi-Fi MAC, with the robot id this install serves, in /opt/emori/ROBOT_IDENTITY (not
#     a secret). Every fresh Reachy Mini is hostname "reachy-mini"; this is what tells
#     them apart on the admin page and in readycheck -- and what shows an SD card that
#     moved into another robot body, which then logs in as the robot the card was for.
echo "== [8c/10] robot identity =="
ID_ROBOT="${TOKEN_ROBOT:-}"
[ -n "$ID_ROBOT" ] || ID_ROBOT="$(grep -E '^EMORI_ROBOT_ID=' .env 2>/dev/null | tail -n1 \
  | cut -d= -f2- | tr -d '\r"' | tr 'A-Z' 'a-z' | tr -c 'a-z0-9._-' '-' \
  | sed 's/^-*//; s/-*$//' || true)"
.venv/bin/python -m emori_adapter.robot_identity record --robot-id "$ID_ROBOT" \
  || echo "  !! could not record the robot identity (ROBOT_IDENTITY)"

# 9. systemd units: daemon --no-media override + the 3 EMORI units.
echo "== [9/10] systemd units =="
sudo mkdir -p /etc/systemd/system/reachy-mini-daemon.service.d
sudo cp deploy/robot/reachy-mini-daemon-override.conf \
     /etc/systemd/system/reachy-mini-daemon.service.d/override.conf
sudo cp deploy/robot/emori-hostapp.service    /etc/systemd/system/emori-hostapp.service
sudo cp deploy/robot/camera-bridge.service    /etc/systemd/system/emori-camera-bridge.service
sudo cp deploy/robot/kiosk.service            /etc/systemd/system/emori-kiosk.service
sudo cp deploy/robot/emori-voice-delay.service /etc/systemd/system/emori-voice-delay.service
# The hub's units and the fence that keeps exactly one of the two gateways running. Only
# copied here: the flag file /opt/emori/HUB_IS_ROS, which deploy/robot/hub-stack.sh raises,
# decides which of the two starts, so the host application stays the default.
sudo mkdir -p /etc/systemd/system/emori-hostapp.service.d
sudo cp deploy/robot/emori-hostapp-hub-fence.conf \
     /etc/systemd/system/emori-hostapp.service.d/zz-hub-fence.conf
for hub_unit in emori-hub.service emori-hub-resolv.path emori-hub-resolv.service; do
  sudo cp "deploy/hub/$hub_unit" "/etc/systemd/system/$hub_unit"
done
sudo systemctl daemon-reload
sudo systemctl restart reachy-mini-daemon
sudo systemctl enable emori-voice-delay
sudo systemctl enable --now emori-hostapp emori-camera-bridge emori-kiosk

# mDNS advert for the management dashboard, so a manager finds it on the LAN
# without being told the robot's DHCP address. Skipped where avahi isn't present.
if [ -d /etc/avahi ]; then
  sudo mkdir -p /etc/avahi/services
  sudo cp deploy/robot/emori-dashboard.avahi.service /etc/avahi/services/emori-dashboard.service
  sudo systemctl reload avahi-daemon 2>/dev/null || true
fi

# 10. Superadmin service (:8070) — Wi-Fi incl. AP mode, speaker/mic volume, the
#    EN/CA language toggle, service restarts, readycheck, logs, reboot, all
#    from a phone. Deliberately independent of the other 3 units (see
#    emori-admin.service: no After=/PartOf= on any of them) so it survives a
#    dead hostapp or a bad kiosk deploy and stays the one way back in.
echo "== [10/10] superadmin service =="
sudo cp deploy/robot/emori-admin.service /etc/systemd/system/emori-admin.service
sudo systemctl daemon-reload
sudo systemctl enable --now emori-admin
if [ -d /etc/avahi ]; then
  sudo cp deploy/robot/emori-admin.avahi.service /etc/avahi/services/emori-admin.service
  sudo systemctl reload avahi-daemon 2>/dev/null || true
fi
# Validate the sudoers fragment BEFORE it ever reaches /etc/sudoers.d/ — a
# syntax error there can lock sudo for the WHOLE robot, so only a clean
# `visudo -c` gets installed; a failure leaves the file out and says so
# instead of guessing. Resolved by PATH *and* /usr/sbin: Debian builds a
# non-root PATH without /usr/sbin, so a bare `visudo` here exits 127 and the
# hardening file would be silently skipped on every robot, with the message
# blaming a syntax error in a file that is perfectly valid.
VISUDO="$(command -v visudo || echo /usr/sbin/visudo)"
if [ ! -x "$VISUDO" ]; then
  echo "  !! visudo not found — deploy/robot/sudoers-emori-admin NOT installed."
  echo "     (install the sudo package, or copy it to /etc/sudoers.d/ by hand)"
elif "$VISUDO" -c -f deploy/robot/sudoers-emori-admin >/dev/null 2>&1; then
  sudo install -m 0440 deploy/robot/sudoers-emori-admin /etc/sudoers.d/emori-admin
  echo "  sudoers-emori-admin installed (validated)"
else
  echo "  !! deploy/robot/sudoers-emori-admin failed 'visudo -c' — NOT installed."
  echo "     emori-admin still works if this robot has pollen's usual blanket"
  echo "     NOPASSWD; fix the file and re-run install.sh to harden it."
fi
# The code step 2 installed, and any secret step 8 minted, reach a unit that was
# ALREADY running only when it restarts: `enable --now` is a no-op start, and systemd
# reads EnvironmentFile at unit start. Restarting only when a secret was minted would
# leave the OLD emori-admin serving the phone after a full deploy (readycheck FAILs
# admin_stale on it). try-restart touches only running units, so
# a deliberate stop stays stopped; the kiosk is PartOf=emori-hostapp and follows it.
if [ "${SECRETS_WROTE:-0}" = 1 ]; then
  echo "  new secrets in .env — restarting the units that read them"
else
  echo "  restarting emori-hostapp (+ kiosk) and emori-admin onto the code just installed"
fi
sudo systemctl try-restart emori-hostapp emori-admin
# A running hub is restarted too, but only onto an image built from this code (the hub's
# deploy hash is EMORI_HUB_SOURCE_EXPECTED, or artifacts/hub-source.expected).
sh deploy/hub/hub-refresh.sh restart

# 11. OPTIONAL: the containerised ROS 2 (ros4hri) graph. Off unless the operator
#    opted in, so a normal install is byte-identical to before this step existed —
#    the robot has no native ROS2 and never will (Trixie/py3.13/aarch64 vs Humble's
#    Ubuntu 22.04/py3.10), so Humble runs in ONE host-networked container reading
#    ws://127.0.0.1:${HOSTAPP_PORT}/ws/relay.
#
#    The image is built OFF the robot for linux/arm64 and loaded here: there is no
#    compiler on the CM4 and no build time on an SD card. On your workstation:
#      docker buildx build --platform linux/arm64 -f deploy/ros2/Dockerfile \
#        -t emori/ros2-humble:latest --load .
#      docker save emori/ros2-humble:latest | gzip > emori-ros2-humble-arm64.tgz
#      scp emori-ros2-humble-arm64.tgz pollen@reachy-mini.local:/opt/emori/
#
#    NEVER on a v2 robot. Its fence (deploy/robot/ros2_native/) says the v2 container
#    owns /dev/ttyAMA3 and the 50 Hz loop; a v1 graph beside it is a second ROS graph on
#    one DDS domain and a second process deciding what the motors do. So the fence wins
#    over EMORI_WITH_ROS2=1, whatever image happens to be on the card. Overridable only
#    so the tests can point it somewhere that is not /opt (as in one-click-deploy.sh).
V2_FENCE="${EMORI_V2_FENCE:-/opt/emori/V2_OWNS_THE_BUS}"
if [ -e "$V2_FENCE" ]; then
  echo "== [11] ROS 2 graph: v2 robot ($V2_FENCE is up), so the v1 graph (emori-ros2) is NOT installed =="
  # ...and one this robot ran in its v1 days goes too: still enabled, it would start at
  # the next boot beside the v2 graph (the README's "Committed to v2" disables it the
  # same way). Asked without sudo: is-enabled/is-active only read.
  if systemctl is-enabled --quiet emori-ros2 2>/dev/null \
     || systemctl is-active --quiet emori-ros2 2>/dev/null; then
    sudo systemctl disable --now emori-ros2
    echo "  disabled and stopped emori-ros2, the v1 graph left from this robot's v1 install"
  fi
elif [ "${EMORI_WITH_ROS2:-0}" = "1" ]; then
  echo "== [11] ROS 2 graph (EMORI_WITH_ROS2=1) =="
  if ! command -v docker >/dev/null 2>&1; then
    echo "  !! docker is not installed — the ROS 2 unit is NOT enabled."
    echo "     sudo apt-get install -y docker.io docker-compose-plugin && sudo usermod -aG docker pollen"
  else
    if [ -f /opt/emori/emori-ros2-humble-arm64.tgz ] \
       && ! docker image inspect emori/ros2-humble:latest >/dev/null 2>&1; then
      echo "  loading emori-ros2-humble-arm64.tgz"
      gunzip -c /opt/emori/emori-ros2-humble-arm64.tgz | docker load
    fi
    if docker image inspect emori/ros2-humble:latest >/dev/null 2>&1; then
      sudo cp deploy/robot/emori-ros2.service /etc/systemd/system/emori-ros2.service
      sudo systemctl daemon-reload
      sudo systemctl enable --now emori-ros2
      echo "  emori-ros2 enabled (motion owner: ${EMORI_ROS_MOTION_OWNER:-hostapp})"
    else
      echo "  !! no emori/ros2-humble:latest image on this robot — unit NOT enabled."
      echo "     build it for linux/arm64 off the robot and scp the tarball (see above)."
    fi
  fi
else
  echo "== [11] ROS 2 graph skipped (EMORI_WITH_ROS2=0) =="
fi

echo
echo "== waiting for the daemon --no-media + hostapp =="
for i in $(seq 1 40); do
  nm=$(curl -s -m 3 http://localhost:8000/api/daemon/status 2>/dev/null \
       | python3 -c 'import sys,json;print(json.load(sys.stdin).get("no_media"))' 2>/dev/null || true)
  hz=$(curl -s -m 3 http://localhost:8090/healthz 2>/dev/null || true)
  [ "$nm" = "True" ] && [ -n "$hz" ] && break
  sleep 2
done
echo "daemon status : $(curl -s -m 5 http://localhost:8000/api/daemon/status | python3 -c 'import sys,json;d=json.load(sys.stdin);print("state",d.get("state"),"no_media",d.get("no_media"))')"
echo "motors        : $(curl -s -m 5 http://localhost:8000/api/motors/status)"
echo "camera video40: $(v4l2-ctl -d /dev/video40 --list-formats-ext 2>/dev/null | grep -m1 YUYV || echo MISSING)"
echo "hostapp       : $(curl -s -m 5 http://localhost:8090/healthz)"
echo "admin service : $(curl -s -m 5 http://localhost:8070/healthz)"
echo "audio session : linger=$(loginctl show-user pollen -p Linger --value 2>/dev/null) runtime-dir=$([ -d /run/user/1000 ] && echo present || echo MISSING)"
if [ "${#ADDON_DIRS[@]}" -gt 0 ]; then
  echo "add-on        : $(curl -s -m 5 http://localhost:8090/api/addon/status)"
fi
echo
echo "== EMORI installed. Kiosk is on the local Xvfb display; screenshot with:"
echo "   DISPLAY=:99 gst-launch-1.0 -q ximagesrc num-buffers=1 ! videoconvert ! pngenc ! filesink location=/tmp/k.png"
