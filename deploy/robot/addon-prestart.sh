#!/bin/sh
# Run each installed add-on's deploy/pre-start.sh before the hostapp starts. An add-on uses
# it to wait for something it opens once at startup. With no add-on
# under /opt/emori/addons this does nothing. Always exits 0: a hook that cannot finish must
# not keep the kiosk from booting.

ROOT="${EMORI_ROOT:-/opt/emori}"
for hook in "$ROOT"/addons/*/deploy/pre-start.sh; do
    [ -f "$hook" ] || continue
    /bin/sh "$hook" || echo "add-on pre-start hook $hook exited $?; starting anyway"
done
exit 0
