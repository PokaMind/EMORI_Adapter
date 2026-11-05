#!/bin/bash
# The hub's compose client, as the main process of emori-hub.service.
#
# The client's output is how the hub's log reaches the journal. Compose begins each log line it
# prints with an erase-line escape, even with --ansi never. journalctl shows such a line as
# "[105B blob data]", and `journalctl -u emori-hub | grep ...`, which is what readycheck's remedies
# say to run, finds nothing in it. So the escapes are taken out on the way.
#
# exec twice: the client stays the unit's main process, so its exit code is the unit's.
exec > >(exec sed -u -E 's/\x1b\[[0-9;]*[A-Za-z]//g') 2>&1
# --ansi is a flag of `docker compose`, not of `up`.
exec /usr/bin/docker compose --ansi never -p emori-hub \
  -f /opt/emori/deploy/hub/docker-compose.hub.robot.yml \
  up --no-build --no-log-prefix --abort-on-container-exit --exit-code-from hub
