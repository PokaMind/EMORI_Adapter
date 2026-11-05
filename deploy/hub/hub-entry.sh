#!/bin/bash
# Entrypoint of the hub container.
#
# With a command, runs it in the ROS environment: that is how a throwaway container runs pip
# or a check. With none, runs the hub from its own virtualenv. It is not a colcon console
# script, because those start with the system Python and would miss the pinned packages.
set -e

# Files the hub writes under /opt/emori are private to the robot's user, as the host
# application's unit has them.
umask 0077

# ROS's setup scripts read variables they have not set, so they run before anything is made
# strict.
source /opt/ros/jazzy/setup.bash
source /emori/ros2_ws/install/setup.bash

HUB_PYTHON=/opt/emori-hub/venv/bin/python
EXPECTED_CORE=/opt/emori/src/

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

# The image carries an older copy of the adapter core. The hub must run the checkout, so a
# missing bind mount or a path file that did not load stops here, in plain words, and not as
# an old hub that starts and misbehaves.
"$HUB_PYTHON" -I - "$EXPECTED_CORE" <<'PY'
import importlib.util
import sys

spec = importlib.util.find_spec("emori_adapter")
origin = (spec.origin or "") if spec else ""
if not origin.startswith(sys.argv[1]):
    sys.exit("the adapter core resolves to %r, not under %s: is /opt/emori mounted?"
             % (origin or None, sys.argv[1]))
PY

exec "$HUB_PYTHON" -m emori_hub.main
