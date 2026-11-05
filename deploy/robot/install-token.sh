#!/usr/bin/env bash
# THIS robot's Pokamind token: ask Pokamind about a candidate, THEN install it.
#
#   ... | bash deploy/robot/install-token.sh --stdin [--defer] [--allow-robot-id-change]
#         stage the token arriving on stdin (Deploy-to-Robot), then verify it
#   bash deploy/robot/install-token.sh
#         verify the token staged earlier (install.sh once its venv exists, and the
#         quick deploy)
#
# The candidate waits in <token dir>/.pokamind_robot_token.candidate (0600, in the 0700
# token directory) and replaces the robot's token only after the robot's own Python asked
# Pokamind about it -- `python -m emori_adapter.hostapp.token_verify`, the live whoami the
# admin page's Replace token runs, signed with the candidate:
#   verified    installed
#   refused     deleted; the robot keeps the token it had (or none)
#   unverified  Pokamind could not be asked (no internet yet, a timeout, 5xx, 429, ...):
#               on a robot with NO token, installed, loudly -- the first token of a robot
#               that is not online yet must still go in. On a robot that HAS one, never:
#               the working token stays, and the candidate stays staged (0600) for the
#               next deploy (install.sh step 2b and the quick deploy ask again) or the
#               admin page's Replace token -- a Pokamind hiccup must not swap a working
#               credential for one nobody has checked.
# Whenever a token is replaced, the one it replaces is kept beside it as
# <token file>.prev (0600). Rolling back (docs/deploy-a-new-robot.md, "Replace, rotate,
# revoke"): cp secrets/pokamind_robot_token.prev secrets/pokamind_robot_token, then
# sudo systemctl restart emori-hostapp.
# --defer: a robot whose Python cannot ask yet (a first deploy: no venv before install.sh
# step 2) keeps the candidate staged for install.sh. Without it that is "unverified".
# --allow-robot-id-change: a human at the deploy keyboard said "install it anyway" to a
# token whose robot id is not the robot's EMORI_ROBOT_ID (kept with the candidate).
#
# A staged candidate older than the robot's token (a token installed since, on the admin
# page) is dropped unasked: verdict "superseded", exit 4.
#
# A verified token also leaves this robot's Pokamind scope behind (token_verify
# --write-scope): the course, languages, pathways and menus the robot then configures
# itself from (src/emori_adapter/site_settings.py).
#
# Prints key=value lines, never the token: token_verdict=verified|refused|unverified|
# deferred|superseded, token_code=<whoami code>, token_robot=<id>, token_installed=<id>,
# token_staged=<id>, token_kept=<id>, robot_id_set=<id>, token_org=<org>,
# token_reason=<sentence>. The verdict is also kept in artifacts/last-token-verdict.env
# (non-secret; verdict=staged for an unverified token left staged) for the deploy's
# final summary.
# Exit: 0 installed | 3 not a token (deleted) | 4 nothing staged | 5 refused | 6 deferred
#       | 7 unverified and the robot keeps its own token (the candidate stays staged)
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
umask 077

STDIN=0
DEFER=0
ALLOW=0
while [ $# -gt 0 ]; do
  case "$1" in
    --stdin) STDIN=1 ;;
    --defer) DEFER=1 ;;
    --allow-robot-id-change) ALLOW=1 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

TOKEN_FILE="$(grep -E '^EMORI_POKAMIND_TOKEN_FILE=' .env 2>/dev/null | tail -n1 | cut -d= -f2- \
  | tr -d '\r" ' || true)"
case "$TOKEN_FILE" in ''|*..*) TOKEN_FILE=secrets/pokamind_robot_token ;; esac
DIR="$(dirname "$TOKEN_FILE")"
mkdir -p "$DIR"
# Never chmod the install root itself (a token file configured at its top level).
if [ "$DIR" != "." ] && [ "$DIR" != "/" ]; then chmod 700 "$DIR"; fi
CAND="$DIR/.pokamind_robot_token.candidate"
ALLOW_MARK="$CAND.allow-robot-id-change"
LINE='[[:space:]]*pkr1\.[a-z0-9][a-z0-9-]{1,61}[a-z0-9]\.[A-Za-z0-9_-]{43}[[:space:]]*'

one_token_line() {
  [ "$(tr -d '\r' < "$1" | grep -c .)" = 1 ] && tr -d '\r' < "$1" | grep -Eqx "$LINE"
}

record() {  # verdict code robot -- what the deploy's last screen reports
  mkdir -p artifacts
  printf 'verdict=%s\ncode=%s\nrobot=%s\nscope=%s\nat=%s\n' "$1" "$2" "$3" \
    "${SCOPE:-none}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > artifacts/last-token-verdict.env
}

if [ "$STDIN" = 1 ]; then
  tmp="$(mktemp "$DIR/.pokamind_robot_token.XXXXXX")"
  cat > "$tmp"
  if ! one_token_line "$tmp"; then
    rm -f "$tmp"
    echo "the robot refused the token (not one pkr1 line)" >&2
    exit 3
  fi
  chmod 600 "$tmp"
  mv -f "$tmp" "$CAND"
  if [ "$ALLOW" = 1 ]; then : > "$ALLOW_MARK"; else rm -f "$ALLOW_MARK"; fi
  # A new candidate: the last verdict was about another one, and must never be read as
  # this one's (the deploy reports it after install.sh when the token was deferred).
  rm -f artifacts/last-token-verdict.env
fi
if [ ! -s "$CAND" ]; then
  exit 4  # nothing staged: the usual case, and silent
fi
[ -e "$ALLOW_MARK" ] && ALLOW=1
if ! one_token_line "$CAND"; then
  rm -f "$CAND" "$ALLOW_MARK"
  echo "the staged token is not one pkr1 line: deleted" >&2
  exit 3
fi
# Only a line that passed the grammar is cut, and with -s: the robot id, never the key.
RID="$(tr -d '\r' < "$CAND" | grep -Ex "$LINE" | cut -s -d. -f2 | tr -d '[:space:]')"
echo "token_robot=$RID"
# A token installed AFTER this one was staged (the admin page's Replace token, which
# asks Pokamind live) is the newer decision: a staged candidate never overrides it.
if [ "$STDIN" = 0 ] && [ -s "$TOKEN_FILE" ] && [ "$TOKEN_FILE" -nt "$CAND" ]; then
  rm -f "$CAND" "$ALLOW_MARK"
  record superseded none "$RID"
  echo "token_verdict=superseded"
  echo "  .. the token staged for '$RID' was dropped: a newer one was installed since" >&2
  exit 4
fi

PY="${EMORI_TOKEN_VERIFY_PYTHON:-.venv/bin/python}"
# --write-scope: an ACCEPTED token's verified whoami is also written as this robot's
# Pokamind scope (configs/site/pokamind_scope.json) with the menus built from it
# (configs/site/auto/), BEFORE the token is moved into place below -- so the hostapp
# restart that follows boots fully configured. Nothing is written for any other verdict.
ARGS=(-m emori_adapter.hostapp.token_verify --token-file "$CAND" --write-scope)
[ "$ALLOW" = 1 ] && ARGS+=(--allow-robot-id-change)
if [ -x "$PY" ] && "$PY" -c 'import emori_adapter.hostapp.token_verify' >/dev/null 2>&1; then
  OUT="$("$PY" "${ARGS[@]}" 2>/dev/null)"
  rc=$?
elif [ "$DEFER" = 1 ]; then
  echo "token_verdict=deferred"
  echo "token_reason=this robot cannot ask Pokamind yet (no EMORI Python environment):" \
       "the token waits, 0600, until install.sh has built one"
  exit 6
else
  OUT="code=verifier_unavailable"
  rc=3
fi
CODE="$(printf '%s\n' "$OUT" | sed -n 's/^code=\([a-z0-9_]*\)$/\1/p' | head -n1)"
ORG="$(printf '%s\n' "$OUT" | sed -n 's/^org=//p' | head -n1)"
REASON="$(printf '%s\n' "$OUT" | sed -n 's/^reason=//p' | head -n1)"
# Whether the verified scope (and the menus built from it) reached the disk: "written",
# "failed" (the token still goes in; the hostapp's own sync records it ~10 s after start).
SCOPE="$(printf '%s\n' "$OUT" | sed -n 's/^scope=\([a-z]*\)$/\1/p' | head -n1)"
case "$rc" in
  0) VERDICT=verified ;;
  2|4) VERDICT=refused ;;
  *) VERDICT=unverified ;;
esac
# The token this robot runs with now, if it is one: a damaged file is no credential to
# protect, and the same token again is no replacement.
KEPT=""
if [ -s "$TOKEN_FILE" ] && one_token_line "$TOKEN_FILE" && ! cmp -s "$CAND" "$TOKEN_FILE"; then
  KEPT="$(tr -d '\r' < "$TOKEN_FILE" | grep -Ex "$LINE" | cut -s -d. -f2 | tr -d '[:space:]')"
fi
if [ "$VERDICT" = unverified ] && [ -n "$KEPT" ]; then
  REASON="Pokamind could not be asked, so this robot keeps its working token for '$KEPT'"
  REASON="$REASON; the new one stays staged until Pokamind confirms it"
fi
echo "token_verdict=$VERDICT"
echo "token_code=${CODE:-unknown}"
[ -n "$SCOPE" ] && echo "token_scope=$SCOPE"
[ -n "$ORG" ] && echo "token_org=$ORG"
[ -n "$REASON" ] && echo "token_reason=$REASON"

if [ "$VERDICT" = refused ]; then
  record refused "${CODE:-unknown}" "$RID"
  rm -f "$CAND" "$ALLOW_MARK"
  echo "  !! Pokamind REFUSED the token for '$RID' (${CODE:-unknown}): it was NOT installed;" >&2
  echo "     the robot keeps the token it had. ${REASON}" >&2
  exit 5
fi

if [ "$VERDICT" = unverified ] && [ -n "$KEPT" ]; then
  # Never an unchecked token over a working one. The candidate (and a human's "install
  # it anyway") stays where it is, 0600, for the next deploy to ask about again.
  record staged "${CODE:-unknown}" "$RID"
  echo "token_staged=$RID"
  echo "token_kept=$KEPT"
  echo "  !! Pokamind could not be asked about the new token for '$RID' (${CODE:-unknown})," >&2
  echo "     so it was NOT installed: this robot KEEPS its working token for '$KEPT'. The" >&2
  echo "     new one waits, staged (0600), on the robot. Once Pokamind is reachable, deploy" >&2
  echo "     again (it is asked about first), or paste it on the admin page: Pokamind ->" >&2
  echo "     Replace token." >&2
  exit 7
fi
record "$VERDICT" "${CODE:-unknown}" "$RID"

# The token being replaced is kept beside it (0600), so a wrong one can be rolled back.
if [ -s "$TOKEN_FILE" ] && ! cmp -s "$CAND" "$TOKEN_FILE"; then
  cp -f "$TOKEN_FILE" "$TOKEN_FILE.prev.tmp" && chmod 600 "$TOKEN_FILE.prev.tmp" \
    && mv -f "$TOKEN_FILE.prev.tmp" "$TOKEN_FILE.prev"
  rm -f "$TOKEN_FILE.prev.tmp"
  echo "token_previous=$TOKEN_FILE.prev"
fi
mv -f "$CAND" "$TOKEN_FILE"
chmod 600 "$TOKEN_FILE"
rm -f "$ALLOW_MARK"
# EMORI_ROBOT_ID is derived from the token only when .env has none; an operator's value
# is never overwritten (a mismatch stays visible: readycheck robot_id_mismatch).
if [ -f .env ] && ! grep -q '^EMORI_ROBOT_ID=.' .env; then
  if grep -q '^EMORI_ROBOT_ID=' .env; then
    sed -i "s|^EMORI_ROBOT_ID=.*|EMORI_ROBOT_ID=$RID|" .env
  else
    if [ -s .env ] && [ -n "$(tail -c1 .env)" ]; then echo >> .env; fi
    printf 'EMORI_ROBOT_ID=%s\n' "$RID" >> .env
  fi
  echo "robot_id_set=$RID"
fi
echo "token_installed=$RID"
if [ "$VERDICT" = unverified ]; then
  echo "  !! the token for '$RID' is installed WITHOUT Pokamind's confirmation" \
       "(${CODE:-unknown}). Once the robot is online: admin page -> Pokamind -> Test" \
       "connection, or readycheck's pokamind_whoami gate." >&2
fi
exit 0
