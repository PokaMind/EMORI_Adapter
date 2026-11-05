#!/usr/bin/env bash
# EMORI post-deploy smoke test ON the robot: the Pokamind control plane and roleplay, end to
# end, with this robot's own Pokamind token, then the smoke test of each installed add-on
# (addons/<name>/scripts/addon_smoke.py, if it ships one). Exit 0 = no check FAILed,
# 1 = one did in any run, 2 = --strict and one SKIPped (or a run refused its arguments).
#   ssh pollen@reachy-mini.local 'bash /opt/emori/deploy/robot/rig-smoke.sh'
#   ssh pollen@reachy-mini.local 'bash /opt/emori/deploy/robot/rig-smoke.sh --only roleplay --json'
#   ssh pollen@reachy-mini.local 'bash /opt/emori/deploy/robot/rig-smoke.sh --addons --json'
# --only control,roleplay are the core's checks; any other name goes to the add-ons' smoke
# tests, and only the runs with a name to do happen. --addons runs the add-ons' alone. Each
# run prints its own report, so --json needs a selection that makes it one run.
# NOT read-only: roleplay makes the robot speak and move (one anonymous session), and an
# add-on's checks may write records on Pokamind. Run readycheck.sh first.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"   # so load_settings() reads /opt/emori/.env and the token file resolves
PY="$ROOT/.venv/bin/python"

addons_only=0
only=""
json=0
strict=0
args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --addons) addons_only=1 ;;
    --only)
      # With no value it stays in the arguments, for the core's parser to refuse.
      if [ $# -ge 2 ]; then only="$2"; shift; else args+=("$1"); fi ;;
    --only=*) only="${1#--only=}" ;;
    --json) json=1; args+=("$1") ;;
    --strict) strict=1; args+=("$1") ;;
    *) args+=("$1") ;;
  esac
  shift
done

smokes=()
for smoke in "$ROOT"/addons/*/scripts/addon_smoke.py; do
  [ -f "$smoke" ] && smokes+=("$smoke")
done

# The core's check names are scripts/rig_smoke.py's CHECKS.
core_only=""
addon_only=""
IFS=, read -r -a names <<< "$only"
for name in ${names[@]+"${names[@]}"}; do
  name="${name//[[:space:]]/}"
  case "$name" in
    "") ;;
    control|roleplay) core_only="${core_only:+$core_only,}$name" ;;
    *) addon_only="${addon_only:+$addon_only,}$name" ;;
  esac
done
# No add-on to ask: the core's parser refuses the name and says which ones it takes.
if [ "${#smokes[@]}" -eq 0 ] && [ -n "$addon_only" ]; then
  core_only="${core_only:+$core_only,}$addon_only"
  addon_only=""
fi

run_core=1
run_addons=1
core_args=()
addon_args=()
if [ "$addons_only" = 1 ]; then
  run_core=0
  [ -n "$only" ] && addon_args=(--only "$only")
elif [ -n "$core_only$addon_only" ]; then
  [ -n "$core_only" ] && core_args=(--only "$core_only") || run_core=0
  [ -n "$addon_only" ] && addon_args=(--only "$addon_only") || run_addons=0
fi
[ "${#smokes[@]}" -gt 0 ] || run_addons=0
# Asked for the add-ons' checks alone and there are none: say so, as a skipped check.
if [ "$addons_only" = 1 ] && [ "$run_addons" = 0 ]; then
  code=0
  [ "$strict" = 1 ] && code=2
  if [ "$json" = 1 ]; then
    printf '{"exit_code": %s, "notes": [], "results": [{"check": "addons", "status": "SKIP", "detail": "no add-on smoke test"}]}\n' "$code"
  else
    echo "SKIP  addons  no add-on smoke test"
  fi
  exit "$code"
fi

if [ "$json" = 1 ] && [ "$run_core" = 1 ] && [ "$run_addons" = 1 ]; then
  echo "rig-smoke: --json needs one run; choose with --only or --addons" >&2
  exit 2
fi

rc=0
record() {  # EXIT_CODE of a run: a FAIL in any run outranks everything else
  if [ "$1" = 1 ] || [ "$rc" = 1 ]; then rc=1
  elif [ "$1" -gt "$rc" ]; then rc=$1
  fi
}
if [ "$run_core" = 1 ]; then
  "$PY" "$ROOT/scripts/rig_smoke.py" ${core_args[@]+"${core_args[@]}"} ${args[@]+"${args[@]}"}
  record $?
fi
if [ "$run_addons" = 1 ]; then
  for smoke in "${smokes[@]}"; do
    "$PY" "$smoke" ${addon_args[@]+"${addon_args[@]}"} ${args[@]+"${args[@]}"}
    record $?
  done
fi
exit "$rc"
