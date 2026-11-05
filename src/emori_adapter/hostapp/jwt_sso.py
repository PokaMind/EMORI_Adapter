"""Partner JWT-SSO minting for the EMORI host app.

Implements Pokamind's partner SSO contract: the robot host acts as a partner, signs a
short-lived JWT with THIS robot's own per-robot token (``robot_token.py``), and
redirects the kiosk browser to ``GET /partner/sso/login?token=<JWT>&partner=<name>``.
The learner never types a Pokamind password.

:func:`mint_robot_sso_jwt` signs EdDSA with header ``kid`` = robot_id; the claims are
the partner claims PLUS ``robot_id``, ``aud=pokamind-partner-sso`` and a ``jti``, 600 s.
Pokamind checks the signature against the public key it holds for that robot and
enforces the robot's scope (org, modules, pathways, course, languages, identities)
before any login. A robot without a token signs nothing: the hostapp then serves only
demo sessions (server.py) and readycheck FAILs ``pokamind_token_missing``.

The URL is built by :func:`build_sso_url`.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Optional


# Claims the per-robot token sets itself. ``extra`` may add claims (``emori_lang``,
# ``emori_kiosk``) but never replace one of these: a caller-supplied ``robot_id`` or
# ``aud`` would be a token that says one thing in its header and another in its body.
_ROBOT_SSO_PROTECTED = frozenset({"robot_id", "aud", "jti", "iat", "exp"})


def mint_robot_sso_jwt(
    robot_token: Any,
    *,
    student_id: str,
    email: str,
    course_id: str,
    ttl_s: int = 600,
    now_ts: Optional[int] = None,
    module_id: Optional[str] = None,
    pathway: Optional[str] = None,
    name: Optional[str] = None,
    assessment_type: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> str:
    """Mint the partner SSO token with THIS robot's own key (EdDSA, ``kid`` = robot_id).

    The partner claims (``student_id``, ``email``, ``course_id``, ``iat``, ``exp`` and
    the optional ``module_id``/``pathway``/``name``/``assessment_type``) plus
    ``robot_id`` (== the header ``kid``), ``aud=pokamind-partner-sso`` and a unique
    ``jti``. ``ttl_s`` defaults to 600 s and
    may not exceed the server's 900 s ceiling: a token the
    server is going to refuse must fail here, on the robot, where it is debuggable.
    """

    from emori_adapter.robot_token import (  # noqa: PLC0415 - keeps this module stdlib
        SSO_AUDIENCE,
        SSO_MAX_TTL_S,
    )

    ttl = int(ttl_s)
    if not 0 < ttl <= SSO_MAX_TTL_S:
        raise ValueError("robot SSO ttl must be within (0, %d] seconds" % SSO_MAX_TTL_S)
    if extra and _ROBOT_SSO_PROTECTED & set(extra):
        raise ValueError("extra may not override %s" % sorted(_ROBOT_SSO_PROTECTED & set(extra)))
    iat = int(now_ts if now_ts is not None else time.time())
    claims: dict[str, Any] = {
        "student_id": student_id,
        "email": email,
        "course_id": course_id,
        "iat": iat,
        "exp": iat + ttl,
    }
    for key, val in (("module_id", module_id), ("pathway", pathway),
                     ("name", name), ("assessment_type", assessment_type)):
        if val:
            claims[key] = val
    if extra:
        claims.update(extra)
    claims["robot_id"] = robot_token.robot_id
    claims["aud"] = SSO_AUDIENCE
    claims["jti"] = str(uuid.uuid4())
    return robot_token.sign(claims)


def build_sso_url(pokamind_base: str, partner: str, token: str) -> str:
    """Build the Pokamind partner SSO entry URL."""
    return f"{pokamind_base.rstrip('/')}/partner/sso/login?token={token}&partner={partner}"
