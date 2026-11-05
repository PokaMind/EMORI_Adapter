#!/usr/bin/env bash
# The ROBOT half of the one-click deploy.
#
# The computer half -- scripts/deploy_to_robot.py, double-clicked as Deploy-to-Robot.cmd
# (Windows) or Deploy-to-Robot.command (macOS/Linux) -- has just streamed `git archive
# HEAD` into /opt/emori and listed what it extracted in FILES. This finishes the job and
# ends on the readycheck verdict:
#
#   bash /opt/emori/deploy/robot/one-click-deploy.sh --files LIST [--quick]
#        [--commit SHA] [--branch NAME] [--from HOST] [--stack v1|v2] [--hub-source HASH]
#
#   full  (default)  install.sh: apt, pip, models, units, services. Needs internet.
#   quick            code only: restart the EMORI units that are already running.
#                    Chosen automatically when the robot cannot reach PyPI, because
#                    install.sh stops at its first apt-get without it.
#
# Either way it then puts the robot on its ROS 2 stack (deploy/robot/ros2_native/
# v2-stack.sh): v2 unless .env says EMORI_ROS2_STACK=v1. --stack records a choice there.
#
# On a robot whose gateway is the hub container (the flag file /opt/emori/HUB_IS_ROS), the
# hub is restarted onto the new code only when its image was built from the code just
# shipped: --hub-source is that code's hash, compared with the image's label by
# deploy/hub/hub-refresh.sh. Without a match the running hub is left alone and a line says
# to deploy again.
#
# Exit: 0 ready | 1 deployed, readycheck FAILed a gate | 3 refused, nothing restarted
#       | 4 install.sh failed
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

FILES=""
MODE=full
COMMIT=unknown
BRANCH=unknown
FROM=unknown
STACK=""
HUB_SOURCE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --files)  FILES="${2:-}"; shift ;;
    --quick)  MODE=quick ;;
    --commit) COMMIT="${2:-unknown}"; shift ;;
    --branch) BRANCH="${2:-unknown}"; shift ;;
    --from)   FROM="${2:-unknown}"; shift ;;
    --stack)
      STACK="${2:-}"; shift
      case "$STACK" in v1|v2) ;; *) echo "--stack must be v1 or v2" >&2; exit 2 ;; esac ;;
    --hub-source) HUB_SOURCE="${2:-}"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
ME="$(id -un)"
# The flag file of deploy/robot/ros2_native/reachy-mini-daemon-v2-fence.conf. Overridable
# only so the tests can point it somewhere that is not /opt.
FENCE="${EMORI_V2_FENCE:-/opt/emori/V2_OWNS_THE_BUS}"
# The flag file of deploy/hub/emori-hub.service: present means the hub, not the host
# application, runs the gateway. Overridable only so the tests can point it elsewhere.
HUB_FLAG="${EMORI_HUB_FLAG:-/opt/emori/HUB_IS_ROS}"

say()    { printf '\n== %s ==\n' "$*"; }
refuse() {
  echo
  printf '!! %s\n' "$@"
  echo "!! The new code is on disk, but nothing was installed or restarted."
  exit 3
}

hostapp_port() {
  local port
  port="$(grep -E '^HOSTAPP_PORT=' .env 2>/dev/null | tail -n1 | cut -d= -f2- | tr -d '\r" ')"
  echo "${port:-8080}"
}

# The gateway's name in messages: the unit that runs it.
gateway_unit() { [ -e "$HUB_FLAG" ] && echo emori-hub || echo emori-hostapp; }

wait_for_hostapp() {
  local port i
  port="$(hostapp_port)"
  for i in $(seq 1 45); do
    curl -fsS -m 3 -o /dev/null "http://127.0.0.1:${port}/healthz" 2>/dev/null && return 0
    sleep 2
  done
  echo "  !! the gateway ($(gateway_unit)) did not answer /healthz on :${port} within 90 s"
  return 1
}

# 1. Guards, before anything is restarted.
say "checks"
sudo -n true 2>/dev/null || refuse \
  "'$ME' has no passwordless sudo on this robot; install.sh needs it." \
  "Fix: echo '$ME ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/010_${ME}-nopasswd"
[ -f .env ] || refuse \
  "There is no $ROOT/.env. The computer half normally creates it; copy .env.example" \
  "to .env (nothing in it has to be filled: the install generates the admin PIN), then" \
  "install this robot's own Pokamind token (docs/deploy-a-new-robot.md)."
# v2 ON TRIAL -- its container holds /dev/ttyAMA3 while Pollen's daemon is merely stopped,
# not fenced -- is the one state this deploy must not touch: install.sh restarts
# reachy-mini-daemon, which would then open the same serial bus as the running v2 node.
# Two owners of one bus is a robot whose motors belong to whoever won the race.
if [ ! -e "$FENCE" ] \
   && [ "$(sudo -n docker inspect -f '{{.State.Running}}' emori-ros2-v2 2>/dev/null)" = "true" ]; then
  refuse \
    "v2 (container emori-ros2-v2) is running, but the daemon fence $FENCE is not up." \
    "Commit to v2 (deploy/robot/ros2_native/README.md, 'Committed to v2') or stop the" \
    "container first; a deploy now would start Pollen's daemon on the same serial bus."
fi
# The hub needs the v2 motor node; v1 would leave it with nothing to talk to.
if [ "$STACK" = v1 ] && [ -e "$HUB_FLAG" ]; then
  refuse \
    "This robot runs the hub, which needs the v2 motor node, so it cannot be put on v1." \
    "Run deploy/robot/hub-stack.sh apply hostapp first, then deploy again."
fi
echo "  sudo ok, .env present, motor bus owner: $([ -e "$FENCE" ] && echo 'v2 (fenced)' || echo 'daemon (v1)')"

# 2. Ownership. With sudo available the extraction ran as root, so it could overwrite files
#    an older `sudo tar -x` left root-owned; give every shipped top-level entry back.
if [ -n "$FILES" ] && [ -s "$FILES" ]; then
  cut -d/ -f1 "$FILES" | sort -u | while IFS= read -r top; do
    case "$top" in ''|.|..) continue ;; esac
    [ -e "$top" ] && sudo -n chown -R "$ME": "$top"
  done
fi

# 2b. Adopt this robot's production menus BEFORE the prune below retires them.
#    A robot deployed from a tree that shipped configs/prod/voice_topics*.yaml speaks
#    those menus. This tree ships none (a fork may), so step 3 would move them aside and
#    the robot would fall back to another menu. A menu that was really this robot's is
#    copied to the robot-local place a saved menu lives (configs/site/, never shipped by
#    any deploy, and read before configs/prod) -- only when ALL of these hold:
#      the file is there; the LAST deploy shipped it and this one does not; it names no
#      placeholder module (example-, demo-module-, dev-); there is no robot-local menu for
#      that language yet; and .env does not point the language elsewhere
#      (EMORI_VOICE_TOPICS_<LANG> or EMORI_VOICE_TOPICS).
#    Never over an existing file. Step 3 then retires the source exactly as before --
#    EXCEPT a file a .env override names: the hostapp reads an override and nothing
#    else, so that file stays where it is (the override still names it).
env_value() {  # NAME: its last .env assignment, as the hostapp reads it ("" when unset)
  grep -E "^$1=" .env 2>/dev/null | tail -n1 | cut -d= -f2- | tr -d '\r' \
    | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
          -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/" || true
}
rel_path() {  # PATH: relative to ROOT when it is inside it
  local p="$1"
  case "$p" in "$ROOT"/*) p="${p#"$ROOT"/}" ;; esac
  printf '%s' "${p#./}"
}
menu_override() {  # LANG: the menu file .env points that language at ("" for none)
  local upper value
  upper="$(printf '%s' "$1" | tr 'a-z' 'A-Z')"
  value="$(env_value "EMORI_VOICE_TOPICS_$upper")"
  [ -n "$value" ] || value="$(env_value EMORI_VOICE_TOPICS)"
  printf '%s' "$value"
}
adopt_menu() {  # SOURCE TARGET LANG
  local src="$1" dst="$2" override
  [ -f "$src" ] || return 0
  [ -n "$FILES" ] && [ -s "$FILES" ] && [ -f "$LAST" ] || return 0
  grep -qxF "$src" "$LAST" || return 0
  if grep -qxF "$src" "$FILES"; then return 0; fi
  if [ -e "$dst" ]; then return 0; fi
  override="$(menu_override "$3")"
  if [ -n "$override" ] && [ "$(rel_path "$override")" != "$src" ]; then
    echo "  .. $src not adopted: .env points the $3 menu elsewhere"
    return 0
  fi
  if grep -Eq "^[[:space:]]*module_id:[[:space:]]*[\"']?(example-|demo-module-|dev-)" "$src"; then
    echo "  .. $src not adopted: it names placeholder modules"
    return 0
  fi
  mkdir -p "$(dirname "$dst")"
  if cp -p "$src" "$dst"; then
    sudo -n chown "$ME": "$dst" 2>/dev/null || true
    echo "  adopted $src as this robot's menu ($dst)"
    if [ -n "$override" ]; then
      echo "     .env still names $src for the $3 menu, so it is kept in place; remove that"
      echo "     line to serve $dst"
    fi
  fi
}
menu_kept() {  # FILE: does a .env menu override name it?
  local name value
  for name in $(grep -oE '^EMORI_VOICE_TOPICS(_[A-Z]+)?=' .env 2>/dev/null | tr -d '=' \
                | sort -u); do
    value="$(env_value "$name")"
    [ -n "$value" ] && [ "$(rel_path "$value")" = "$1" ] && return 0
  done
  return 1
}
mkdir -p artifacts
LAST=artifacts/last-deploy.files
adopt_menu configs/prod/voice_topics.yaml configs/site/voice_topics.en.yaml en
adopt_menu configs/prod/voice_topics.ca.yaml configs/site/voice_topics.ca.yaml ca

# 3. Prune. A file the previous deploy shipped and this one did not was deleted or renamed
#    upstream; leaving it lets a stale module or config outlive its removal. Moved aside,
#    never deleted, so a wrong call is one `mv` back.
#    Only what the archive listed is ever considered: addons/ (shipped by the computer half
#    as separate trees, not named in FILES) is never pruned.
if [ -n "$FILES" ] && [ -s "$FILES" ]; then
  if [ -f "$LAST" ]; then
    STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
    comm -23 <(grep -v '/$' "$LAST" | sort -u) <(grep -v '/$' "$FILES" | sort -u) \
      | while IFS= read -r gone; do
          case "$gone" in ''|/*|*..*) continue ;; esac
          [ -f "$gone" ] || continue
          if menu_kept "$gone"; then
            echo "  kept $gone: a .env EMORI_VOICE_TOPICS* line names it"
            continue
          fi
          mkdir -p "artifacts/superseded/$STAMP/$(dirname "$gone")"
          mv -f "$gone" "artifacts/superseded/$STAMP/$gone" \
            && echo "  retired $gone -> artifacts/superseded/$STAMP/"
        done
  fi
  cp -f "$FILES" "$LAST"
fi

# 3b. The hostapp's unit, on both paths. An installed unit can name a file the prune just
#     retired, and a quick deploy reinstalls no unit at all: its restart would then fail
#     with 203/EXEC, and a full install that stops before its own step 9 leaves the same
#     unit for the next boot. A robot with no unit yet gets it from install.sh.
#     Overridable only so the tests can point it somewhere that is not /etc.
HOSTAPP_UNIT="${EMORI_SYSTEMD_DIR:-/etc/systemd/system}/emori-hostapp.service"
if [ -f "$HOSTAPP_UNIT" ] && [ -f deploy/robot/emori-hostapp.service ] \
   && ! cmp -s deploy/robot/emori-hostapp.service "$HOSTAPP_UNIT"; then
  if sudo -n cp deploy/robot/emori-hostapp.service "$HOSTAPP_UNIT" \
     && sudo -n systemctl daemon-reload; then
    echo "  installed the shipped emori-hostapp unit"
  else
    echo "  !! the emori-hostapp unit could not be replaced: the hostapp may not restart"
  fi
fi

# The hub's units, the same way: an installed copy can name a file the prune just retired.
# Only where they are installed already (hub-stack.sh installs and enables them).
UNIT_DIR="${EMORI_SYSTEMD_DIR:-/etc/systemd/system}"
HUB_UNITS_CHANGED=0
for unit in emori-hub.service emori-hub-resolv.path emori-hub-resolv.service; do
  if [ -f "$UNIT_DIR/$unit" ] && [ -f "deploy/hub/$unit" ] \
     && ! cmp -s "deploy/hub/$unit" "$UNIT_DIR/$unit"; then
    sudo -n cp "deploy/hub/$unit" "$UNIT_DIR/$unit" && HUB_UNITS_CHANGED=1 \
      && echo "  installed the shipped $unit"
  fi
done
if [ "$HUB_UNITS_CHANGED" = 1 ]; then sudo -n systemctl daemon-reload; fi

# 4. Internet decides the mode: install.sh needs apt and PyPI.
if [ "$MODE" = full ] && ! curl -fsS -m 8 -o /dev/null https://pypi.org/simple/pip/ 2>/dev/null; then
  echo "  !! this robot cannot reach the internet (pypi.org): doing a QUICK deploy instead."
  echo "     Code changes go live; new packages, unit files and models need a full deploy"
  echo "     once the robot is online."
  MODE=quick
fi

# What the code on disk IS: admin/system.py's "Deployed commit", and what readycheck
# holds the running emori-admin to (admin_stale). The code arrives as `git archive`, so
# /opt/emori has no .git and `git describe` fails there. Written BEFORE step 5 restarts
# anything: emori-admin reads it once, at process start, to say which commit it runs,
# and a restart that came first would report the previous one.
if [ "$COMMIT" != unknown ]; then
  printf '%s (%s)\n' "$COMMIT" "$BRANCH" > DEPLOYED_COMMIT.txt
fi

# What the hub's image must have been built from for this deploy to restart it.
if [ -n "$HUB_SOURCE" ]; then
  printf '%s\n' "$HUB_SOURCE" > artifacts/hub-source.expected
else
  rm -f artifacts/hub-source.expected
fi

# 5. Install.
PYPROJECT_MARK=artifacts/last-full-install.pyproject.sha256
# The core's pyproject and each shipped add-on's: what the last full install resolved.
pyprojects_sha() {
  cat pyproject.toml addons/*/pyproject.toml 2>/dev/null | sha256sum | cut -d' ' -f1
}
# install.sh also writes the credentials it generated here (0600), so step 6 can show
# them again AFTER readycheck, as the last thing on the screen.
CREDS=artifacts/.new-credentials
rm -f "$CREDS"
# Written by step 5b; an install that stops before it must not leave the last deploy's.
rm -f artifacts/last-ros2-stack.env
if [ "$MODE" = full ]; then
  say "full install (deploy/robot/install.sh)"
  EMORI_CREDENTIALS_FILE="$ROOT/$CREDS" bash deploy/robot/install.sh || {
    rc=$?
    rm -f "$CREDS"   # install.sh printed them itself as it ended
    printf '\n!! install.sh failed (exit %s). Scroll up to its first error.\n' "$rc"
    exit 4
  }
  pyprojects_sha > "$PYPROJECT_MARK"
else
  say "quick deploy (code only)"
  # install.sh step 0, which this mode skips with the rest of it.
  find deploy/robot \( -name '*.sh' -o -name '*.conf' -o -name '*.service' \) -exec sed -i 's/\r$//' {} +
  chmod +x deploy/robot/*.sh
  if [ -f "$PYPROJECT_MARK" ] \
     && [ "$(pyprojects_sha)" != "$(cat "$PYPROJECT_MARK")" ]; then
    echo "  !! pyproject.toml changed (the core's or an add-on's) since the last full install: any new dependency is"
    echo "     NOT installed. Deploy again (full) once the robot has internet."
  fi
  { systemctl is-enabled --quiet emori-hostapp 2>/dev/null \
    || systemctl is-enabled --quiet emori-hub 2>/dev/null; } || refuse \
    "neither emori-hostapp nor emori-hub is enabled: EMORI was never installed here, or the robot is in" \
    "stock daemon mode ('robot-mode status'). A first deploy has to be a full one, which" \
    "needs the robot online; to leave daemon mode run: robot-mode emori"
  # A Pokamind token the computer half staged but this robot's Python could not ask
  # Pokamind about (install.sh step 2b does this on a full deploy): asked again now,
  # before the restart that makes the hostapp sign with it. Nothing staged: silent.
  if [ -f deploy/robot/install-token.sh ]; then
    bash deploy/robot/install-token.sh || true
  fi
  # An add-on shipped since the last full install is on disk but not in the venv, so the
  # hostapp would run without it and nothing would say so. Installed here without the
  # network when the venv can build it; its dependencies and install hook wait for a full
  # deploy.
  for addon in addons/*/; do
    [ -f "${addon}pyproject.toml" ] || continue
    addon="${addon%/}"
    name="$(sed -n "s/^name *= *[\"']\([^\"']*\).*/\1/p" "$addon/pyproject.toml" | head -n1)"
    if [ -n "$name" ] && .venv/bin/pip show -q "$name" >/dev/null 2>&1; then
      continue
    fi
    if .venv/bin/pip install --no-index --no-deps --no-build-isolation -q -e "$addon"; then
      echo "  installed add-on $(basename "$addon"), without its dependencies or install hook:"
      echo "     deploy again (full) once the robot has internet."
    else
      echo "  !! ADD-ON $(basename "$addon") IS NOT INSTALLED: it is on disk, but the hostapp runs"
      echo "  !! WITHOUT it. Deploy again (full) once the robot has internet."
    fi
  done
  # The add-ons for the hub container, before the hub restarts onto them.
  sh deploy/hub/hub-refresh.sh addons
  # try-restart only touches units that are running, so this never undoes a deliberate
  # stop. The kiosk is PartOf=emori-hostapp and follows it. With the hub running the
  # gateway, emori-hostapp is a skipped start and this restarts the admin only.
  sudo -n systemctl try-restart emori-hostapp emori-admin
  if [ -e "$HUB_FLAG" ]; then
    echo "  restarted emori-admin"
  else
    echo "  restarted emori-hostapp (+ kiosk) and emori-admin"
  fi
  # The hub is restarted only onto an image built from this code (see the header).
  sh deploy/hub/hub-refresh.sh restart
fi

cat > artifacts/last-deploy.env <<EOF
commit=$COMMIT
branch=$BRANCH
from=$FROM
mode=$MODE
at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF

# 5b. The ROS 2 stack, after the install so readycheck below judges the robot as it will
#     run. A --stack from the computer half is this robot's choice from now on; without
#     one, .env's EMORI_ROS2_STACK decides, and an unset one means v2.
if [ -n "$STACK" ]; then
  if grep -qE '^EMORI_ROS2_STACK=' .env; then
    sed -i "s/^EMORI_ROS2_STACK=.*/EMORI_ROS2_STACK=$STACK/" .env
  else
    if [ -s .env ] && [ -n "$(tail -c1 .env)" ]; then echo >> .env; fi
    printf 'EMORI_ROS2_STACK=%s\n' "$STACK" >> .env
  fi
fi
say "ROS 2 stack (deploy/robot/ros2_native/v2-stack.sh)"
bash deploy/robot/ros2_native/v2-stack.sh apply
case $? in
  0|5) ;;  # 5: not possible yet, and nothing changed; it says why above
  *) echo "  !! the ROS 2 stack step did not finish as asked (see above); readycheck follows" ;;
esac

# 6. Verdict.
say "waiting for the hostapp"
wait_for_hostapp || true
say "readycheck"
REPORT=artifacts/.last-deploy-readycheck.txt
readycheck_once() {  # its own exit code; the matrix on screen and in REPORT
  bash deploy/robot/readycheck.sh --no-speak | tee "$REPORT"
  return "${PIPESTATUS[0]}"
}
readycheck_once
rc=$?
if [ "$rc" -ne 0 ]; then
  echo
  echo "  (right after a restart a gate can still be warming up: checking once more in 20 s)"
  sleep 20
  readycheck_once
  rc=$?
fi
# What the computer half's last screen summarises: the WARN count and gate names
# (a WARN never changes the exit code, so "ready" alone would hide them).
WARNS="$(grep -E '^  ~ [a-z0-9_]+:' "$REPORT" 2>/dev/null | sed -E 's/^  ~ ([a-z0-9_]+):.*/\1/' \
  | sort -u | paste -sd, - || true)"
COUNTS="$(grep -E '^PASS [0-9]+  FAIL [0-9]+  SKIP [0-9]+  WARN [0-9]+' "$REPORT" 2>/dev/null \
  | tail -n1 || true)"
FAILS="$(printf '%s' "$COUNTS" | sed -nE 's/.*FAIL ([0-9]+).*/\1/p')"
WARN_N="$(printf '%s' "$COUNTS" | sed -nE 's/.*WARN ([0-9]+).*/\1/p')"
printf 'rc=%s\nfail=%s\nwarn=%s\nwarn_gates=%s\nat=%s\n' "$rc" "${FAILS:-}" "${WARN_N:-}" "$WARNS" \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > artifacts/last-deploy-readycheck.env
rm -f "$REPORT"
if [ -s "$CREDS" ]; then
  echo
  cat "$CREDS"
fi
rm -f "$CREDS"
exit "$rc"
