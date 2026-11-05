"""Ask Pokamind about a candidate robot token BEFORE it replaces the robot's own.

    python -m emori_adapter.hostapp.token_verify --token-file <candidate>

Run ON the robot, by ``deploy/robot/install-token.sh`` (Deploy-to-Robot, ``install.sh``,
the quick deploy), so the check has the robot's own network, its ``POKAMIND_BASE`` and
PyJWT + cryptography (the deploy computer's script is stdlib-only). It is the live
``GET /api/robot/whoami`` the admin page's Replace token runs
(``pokamind_whoami.fetch_whoami``, signed with the CANDIDATE), with one difference: the
admin page refuses a token it cannot verify, while a deploy must still be able to give
a robot with no internet yet its first token. So the answer has three values:

* ``verified``   -- Pokamind knows this robot, it is active, and holds this key.
* ``refused``    -- a definitive no; the robot keeps the token it had:
  ``robot_unknown`` (no such robot on this backend), ``robot_revoked`` (or whoami
  answering ``status: revoked``), ``whoami_auth_failed`` (a rotated-out key, another
  backend, or a robot clock more than 2 s off -- readycheck's ``clock`` gate),
  ``whoami_key_mismatch``, ``whoami_robot_id_mismatch``, and ``robot_id_mismatch``: the
  robot's ``EMORI_ROBOT_ID`` names another robot (``--allow-robot-id-change`` is what a
  human's "install it anyway" at the deploy keyboard passes).
* ``unverified`` -- Pokamind could not be asked (unreachable, timeout, 5xx, rate limit,
  a backend without whoami, an unusable ``POKAMIND_BASE``, no crypto): ``deploy/robot/
  install-token.sh`` installs it, loudly, only on a robot with no token; a robot with
  one keeps it, and the candidate stays staged.

Output: ``key=value`` lines -- ``verdict``, ``code``, ``robot_id`` and, when Pokamind
answered, ``org``/``status``, plus one ``reason`` sentence. Never the token, never a
response body. Exit 0 verified, 2 refused, 3 unverified, 4 the file is not a token.

``--write-scope`` (what ``install-token.sh`` passes): a VERIFIED token's whoami is also
recorded as this robot's Pokamind scope (``configs/site/pokamind_scope.json`` and the
menus built from it, ``hostapp/pokamind_scope.py``), so the restart that follows the
token install boots configured. One more line says how that went: ``scope=written``, or
``scope=failed`` with ``scope_code=<code>``. Nothing is written for any other verdict,
and the exit code is the verdict's either way. ``--root`` names the install root the
scope is written under (default: this install's, the same one the hostapp reads).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import os
import sys
from typing import Any, Callable, Optional

# Load /opt/emori/.env exactly as the hostapp and readycheck do, so POKAMIND_BASE and
# EMORI_ROBOT_ID are the robot's own.
import emori_adapter.config  # noqa: F401
from emori_adapter import robot_token as _robot_token
from emori_adapter import site_settings
from emori_adapter.hostapp.pokamind_whoami import Whoami, WhoamiResult, fetch_whoami

VERIFIED = "verified"
REFUSED = "refused"
UNVERIFIED = "unverified"
EXIT_CODES = {VERIFIED: 0, REFUSED: 2, UNVERIFIED: 3}
EXIT_NOT_A_TOKEN = 4

# The hostapp's own POKAMIND_BASE default, so an unset value asks the same backend the
# hostapp would.
_DEFAULT_BASE = site_settings.DEFAULT_POKAMIND_BASE

# whoami codes that are Pokamind saying NO about this token. Everything else is "could
# not ask", which must not stop a first install on a robot that is not online yet.
REFUSAL_REASONS = {
    "robot_unknown": "Pokamind has no robot with this token's id (a mistyped id, or "
                     "POKAMIND_BASE points at another backend than the one that issued it)",
    "robot_revoked": "this robot has been REVOKED in Pokamind",
    "robot_not_active": "Pokamind lists this robot, but not as active",
    "whoami_auth_failed": "Pokamind refused the token's signature: it was rotated (use "
                          "the newest one), it belongs to another backend, or the robot's "
                          "clock is more than 2 s off (readycheck's clock gate)",
    "whoami_key_mismatch": "Pokamind holds a newer key for this robot: this token was "
                           "rotated out",
    "whoami_robot_id_mismatch": "Pokamind answered for a different robot",
    "robot_id_mismatch": "the robot's .env says EMORI_ROBOT_ID is another robot",
}


@dataclass(frozen=True)
class Verdict:
    verdict: str
    code: str
    robot_id: str
    org: str = ""
    status: str = ""
    # The verified answer itself (only on ``verified``), for --write-scope. Never printed.
    whoami: Optional[Whoami] = field(default=None, repr=False, compare=False)

    @property
    def reason(self) -> str:
        if self.verdict == VERIFIED:
            return "Pokamind knows this robot, it is active, and holds this key"
        if self.verdict == REFUSED:
            return REFUSAL_REASONS.get(self.code, "Pokamind refused this token")
        return ("Pokamind could not be asked, so the token is UNVERIFIED (installed only on "
                "a robot with no token): run Test connection on the admin page's Pokamind "
                "tab once the robot is online")

    def lines(self) -> list[str]:
        out = [f"verdict={self.verdict}", f"code={self.code}", f"robot_id={self.robot_id}"]
        if self.org:
            out.append(f"org={self.org}")
        if self.status:
            out.append(f"status={self.status}")
        out.append(f"reason={self.reason}")
        return out


def judge(token: Any, *, base_url: str, env_robot_id: str = "",
          allow_robot_id_change: bool = False,
          whoami: Optional[Callable[[str, Any], WhoamiResult]] = None) -> Verdict:
    """The verdict on ``token`` (a parsed :class:`robot_token.RobotToken`)."""

    robot_id = token.robot_id
    if (not allow_robot_id_change
            and _robot_token.robot_id_conflict(token, env_robot_id or "")):
        # Local and definitive: no network call is needed to know this robot's .env
        # names another robot.
        return Verdict(REFUSED, "robot_id_mismatch", robot_id)
    asker = whoami if whoami is not None else fetch_whoami
    try:
        result = asker(base_url, token)
    except Exception:  # noqa: BLE001 - fetch_whoami never raises; an injected one may
        result = WhoamiResult(code="whoami_unreachable")
    if not isinstance(result, WhoamiResult):
        result = WhoamiResult(code="invalid_whoami_response")
    if result.ok and result.whoami is not None:
        answer = result.whoami
        if answer.status != "active":
            return Verdict(REFUSED, "robot_not_active", robot_id, answer.org, answer.status)
        return Verdict(VERIFIED, "ok", robot_id, answer.org, answer.status, answer)
    if result.code in REFUSAL_REASONS:
        return Verdict(REFUSED, result.code, robot_id)
    return Verdict(UNVERIFIED, result.code, robot_id)


def write_scope(token: Any, verdict: Verdict, *, base_url: str,
                root: Optional[str] = None) -> str:
    """Record a VERIFIED verdict's whoami as this robot's scope; the code (``ok``)."""

    if verdict.verdict != VERIFIED or verdict.whoami is None:
        return "not_verified"
    from emori_adapter.hostapp.pokamind_scope import ScopeSync  # noqa: PLC0415

    return ScopeSync(base_url, token, root=root).record(verdict.whoami, token)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m emori_adapter.hostapp.token_verify",
        description="Ask Pokamind about a candidate robot token (prints no secret).")
    parser.add_argument("--token-file", required=True, help="the candidate token file")
    parser.add_argument("--base", default=None,
                        help="Pokamind backend origin (default: POKAMIND_BASE)")
    parser.add_argument("--env-robot-id", default=None,
                        help="the robot's EMORI_ROBOT_ID (default: from .env)")
    parser.add_argument("--allow-robot-id-change", action="store_true",
                        help="a human confirmed installing a token for another robot id")
    parser.add_argument("--write-scope", action="store_true",
                        help="record a verified token's Pokamind scope (and its menus)")
    parser.add_argument("--root", default=None,
                        help="install root the scope is written under (default: this "
                             "install's)")
    args = parser.parse_args(argv)

    load = _robot_token.load_token(
        environ={_robot_token.TOKEN_FILE_ENV: os.path.abspath(args.token_file)})
    if load.token is None or load.source != "file":
        print("verdict=refused")
        print(f"code={load.error or 'token_empty'}")
        print("reason=the candidate file does not hold one pkr1 token line")
        return EXIT_NOT_A_TOKEN
    base = args.base if args.base is not None else os.environ.get("POKAMIND_BASE", "")
    env_robot_id = (args.env_robot_id if args.env_robot_id is not None
                    else os.environ.get("EMORI_ROBOT_ID", ""))
    base_url = base.strip() or _DEFAULT_BASE
    verdict = judge(load.token, base_url=base_url, env_robot_id=env_robot_id,
                    allow_robot_id_change=args.allow_robot_id_change)
    for line in verdict.lines():
        print(line)
    if args.write_scope and verdict.verdict == VERIFIED:
        try:
            code = write_scope(load.token, verdict, base_url=base_url, root=args.root)
        except Exception:  # noqa: BLE001 - the verdict stands whatever the scope did
            code = "scope_record_failed"
        if code == "ok":
            print("scope=written")
        else:
            print("scope=failed")
            print(f"scope_code={code}")
    return EXIT_CODES[verdict.verdict]


if __name__ == "__main__":
    sys.exit(main())
