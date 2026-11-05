#!/usr/bin/env bash
# Install every add-on in addons/ for the hub container's interpreter, into hub-site/.
#
# The host's virtualenv runs another Python, so its copies of an add-on's compiled packages
# are no use in the container. Each add-on may ship deploy/hub-constraints.txt (the packages
# the hub's environment does not carry) and deploy/hub-wheels/ (their wheels). Those are
# installed in a throwaway hub container with no network, so nothing but that directory can
# supply them. addon_dist.py then writes the path file and the entry points.
#
# The result is built beside the current hub-site/ and takes its place only when every add-on
# installed, so a failure leaves the hub with what it had. Nothing here runs when hub-site/ is
# newer than what it was built from.
#
#   hub-addons.sh           install what is out of date
#   hub-addons.sh --force   install all of it again
#
# Prints hub_addons=none|current|installed|no_image|failed.
# Exit: 0 fine | 2 usage | 4 an install failed, hub-site/ unchanged | 5 there is no hub image
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
IMAGE=emori/hub:robot
HUB_PYTHON=/opt/emori-hub/venv/bin/python
LIB=lib/python3.12/site-packages
SITE="$ROOT/hub-site"
NEW="$ROOT/hub-site.new"
OLD="$ROOT/hub-site.old"
# One marker per add-on folder, so that a later run can compare it with the add-on's files.
MARKS=.addons
PIP_LIMIT_S="${EMORI_HUB_ADDONS_LIMIT_S:-300}"

force=no
case "${1:-}" in
  '') ;;
  --force) force=yes ;;
  *) echo "usage: $0 [--force]" >&2; exit 2 ;;
esac

addons=()
for pyproject in "$ROOT"/addons/*/pyproject.toml; do
  [ -f "$pyproject" ] && addons+=("$(dirname "$pyproject")")
done

clean() { rm -rf "$NEW" "$OLD"; }

if [ "${#addons[@]}" -eq 0 ]; then
  clean
  rm -rf "$SITE"
  echo "hub_addons=none"
  exit 0
fi

out_of_date() {
  local addon mark file
  [ "$force" = yes ] && return 0
  for addon in "${addons[@]}"; do
    mark="$SITE/$MARKS/$(basename "$addon")"
    [ -e "$mark" ] || return 0
    for file in "$addon/pyproject.toml" "$addon/deploy/hub-constraints.txt"; do
      if [ -e "$file" ] && [ "$file" -nt "$mark" ]; then return 0; fi
    done
  done
  # An add-on that has gone must not stay importable.
  for mark in "$SITE/$MARKS"/*; do
    [ -e "$mark" ] || continue
    [ -d "$ROOT/addons/$(basename "$mark")" ] || return 0
  done
  return 1
}

if ! out_of_date; then
  echo "hub_addons=current"
  exit 0
fi

if ! sudo -n docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "!! there is no $IMAGE to install the add-ons with" >&2
  echo "hub_addons=no_image"
  exit 5
fi

failed() {
  clean
  printf '!! %s; hub-site is unchanged\n' "$1" >&2
  echo "hub_addons=failed"
  exit 4
}

clean
mkdir -p "$NEW/$LIB" "$NEW/$MARKS" || failed "could not create $NEW"

for addon in "${addons[@]}"; do
  name="$(basename "$addon")"
  if [ -f "$addon/deploy/hub-constraints.txt" ]; then
    # --target, not --prefix: where a prefix lands depends on the distribution's pip.
    timeout -k 5 "$PIP_LIMIT_S" sudo -n docker run --rm --network none --user 1000:1000 \
      -e HOME=/tmp -v "$ROOT:$ROOT" "$IMAGE" \
      pip3 --python "$HUB_PYTHON" install --no-index --no-cache-dir --no-deps \
        --target "$NEW/$LIB" --find-links "$addon/deploy/hub-wheels" \
        -r "$addon/deploy/hub-constraints.txt" \
      || failed "the packages of $name did not install"
  fi
  python3 "$ROOT/deploy/hub/addon_dist.py" "$addon" "$NEW/$LIB" \
    || failed "the entry points of $name could not be written"
  : > "$NEW/$MARKS/$name"
done

if [ -e "$SITE" ]; then
  mv "$SITE" "$OLD" || failed "could not move the old hub-site away"
fi
if ! mv "$NEW" "$SITE"; then
  [ -e "$OLD" ] && mv "$OLD" "$SITE"
  failed "could not put the new hub-site in place"
fi
rm -rf "$OLD"
echo "hub_addons=installed"
