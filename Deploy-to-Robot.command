#!/usr/bin/env bash
# One-click EMORI deploy (macOS: double-click it; Linux: run it). The robot must be on
# the same Wi-Fi as this computer. All the work is in scripts/deploy_to_robot.py; this
# file only finds a Python for it.
cd "$(dirname "$0")" || exit 1
PY=""
for candidate in .venv-reachy/bin/python .venv/bin/python python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 \
     && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 8))' 2>/dev/null; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3 was not found. Install it from https://www.python.org/downloads/ and run this again."
  [ -t 0 ] && read -r -p "Press Enter to close. " _
  exit 1
fi
"$PY" scripts/deploy_to_robot.py "$@"
rc=$?
# A double-clicked Terminal window would otherwise close before anyone reads the verdict.
if [ $# -eq 0 ] && [ -t 0 ]; then
  echo
  read -r -p "Press Enter to close. " _
fi
exit $rc
