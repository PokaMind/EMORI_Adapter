"""``POST /api/skill`` — the ROS graph's only way to move the robot.

The hostapp is the single robot writer (ADR: hostapp-hub), and that rule is what
keeps the motion serialization traps closed: one ``RobotPlayer`` queue, one
generation guard, one ``busy`` interlock for the gesture detector, one place that
decides whether an add-on or the training lane owns the head. A ROS node that
POSTed the daemon directly would bypass every one of them at once.

So the ros4hri skill facade does not talk to the daemon. It talks to THIS endpoint,
which funnels a bounded skill request through ``skill_to_intents`` (pure, in the
adapter core) into ``driver.submit_legacy_motion`` — the same path the hostapp's own
engagement and cue lanes use.

Four things make that safe enough to expose over HTTP at all, and all four are
deliberately independent:

* **Off by default.** ``EMORI_SKILL_API=1`` mounts the route; without it there is no
  route to find. The shipped robot has no skill intake.
* **A shared secret.** ``EMORI_SKILL_KEY`` must match exactly, and the endpoint
  refuses to mount at all when the flag is on but the key is unset — a secret-less
  "enabled" state is the one configuration that must never exist.
* **Loopback only.** The ROS container is host-networked on the robot, so a genuine
  caller is always 127.0.0.1; anything else on the venue LAN is refused before the
  body is read.
* **A rate limit.** The 12-slot queue is shared with the greeting, the listening cue
  and add-on poses, so a chatty node is refused (429) rather than queued. It is
  per skill KIND, because one ROS event is legitimately a burst of different kinds.
* **An owner.** ``EMORI_ROS_MOTION_OWNER`` must be ``ros``; on the shipped default
  (``hostapp``) an authenticated loopback request is still answered 409 ``not_owner``,
  so the knob that documents which lane may move the robot actually decides it.

Nothing here decides *what* the robot should do — that is ``behavior/cues.py``. This
module only authenticates, range-checks and submits.

Every answer is also a number on ``driver.counters``. The ROS facade keeps its own
ledger, but that one counts what it POSTED, not what the hostapp did with it: a stale
key, a container that is not loopback, an owner knob left on ``hostapp`` are all
"posted, no motion" on the ROS side and total silence on this one. The rehearsal
(``scripts/rehearse_motion_owner.py``) reads both ledgers and compares them, which is
only possible if this side counts its own refusals by error code.
"""

# NOTE: no `from __future__ import annotations` — see server.py (FastAPI handler
# annotations must be evaluated at def time inside mount()'s closure scope).
import hmac
import logging
import os
import threading
import time
from typing import Callable, Optional

from emori_adapter.behavior import CueConfig, SkillError, skill_to_intents

log = logging.getLogger("emori.hostapp.skill")

# One accepted request per this many seconds; the excess is refused, never queued.
# PER SKILL KIND: one ROS event is legitimately several requests of DIFFERENT kinds
# (a recovery cycle is set_expression + replay_motion + look_at, issued microseconds
# apart), and a single shared slot would refuse the actual motion in favour of the
# expression that happened to be published first. A chatty node repeating the SAME
# skill is still refused, which is the starvation this limit exists for.
SKILL_MIN_INTERVAL_S = 0.35
# Which lane may move the robot: hostapp (default) | ros | none. Enforced here and in
# the ROS skill_facade; anything unrecognised reads as the shipped default rather than
# as permission.
MOTION_OWNERS = ("hostapp", "ros", "none")
# The client hosts a loopback request can present. IPv6-mapped IPv4 ("::ffff:127.0.0.1")
# is normalised before the lookup, so the mapped form is accepted without listing it.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def skill_api_enabled() -> bool:
    """Both halves of the switch: the flag AND a non-empty shared secret."""

    return os.environ.get("EMORI_SKILL_API", "0") == "1" and bool(skill_key())


def skill_key() -> str:
    return (os.environ.get("EMORI_SKILL_KEY", "") or "").strip()


def min_interval_s() -> float:
    raw = (os.environ.get("EMORI_SKILL_MIN_INTERVAL_S", "") or "").strip()
    if not raw:
        return SKILL_MIN_INTERVAL_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        log.warning("EMORI_SKILL_MIN_INTERVAL_S=%r is not a number; using %s",
                    raw, SKILL_MIN_INTERVAL_S)
        return SKILL_MIN_INTERVAL_S
    # 0 would remove exactly the protection this exists for.
    return max(0.0, value)


def motion_owner() -> str:
    """Which lane is allowed to submit motion, per ``EMORI_ROS_MOTION_OWNER``.

    An unknown value is NOT permission: it degrades to ``hostapp``, the shipped robot,
    because the one thing this knob must never do is open the robot to a lane on a typo.
    """

    raw = (os.environ.get("EMORI_ROS_MOTION_OWNER", "") or "").strip().lower()
    if raw and raw not in MOTION_OWNERS:
        log.warning("EMORI_ROS_MOTION_OWNER=%r is not one of %s; using 'hostapp'",
                    raw, "|".join(MOTION_OWNERS))
        return "hostapp"
    return raw or "hostapp"


def bump(driver, name: str, amount: int = 1) -> None:
    """Add to ``driver.counters`` (a ``collections.Counter``), best-effort.

    Best-effort on purpose: an injected test double, or a driver mid-teardown, must
    never be able to turn a bookkeeping miss into a refused motion request.
    """

    counters = getattr(driver, "counters", None)
    if counters is None:
        return
    try:
        counters[name] += amount
    except Exception:  # noqa: BLE001 - a ledger must never brick the intake
        pass


def is_loopback(host: Optional[str]) -> bool:
    """Did this request come from the robot itself?"""

    if not host:
        return False
    name = host.strip().lower()
    if name.startswith("::ffff:"):
        name = name[len("::ffff:"):]
    return name in _LOOPBACK_HOSTS


def mount(
    app,
    *,
    driver,
    head_reserved: Optional[Callable[[], bool]] = None,
    enabled: Optional[bool] = None,
    key: Optional[str] = None,
    interval_s: Optional[float] = None,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Attach ``POST /api/skill`` when it is BOTH enabled and keyed.

    Returns whether the route was mounted, so the caller can log the posture. The
    overrides exist for tests; production passes none and the environment decides.
    """

    from fastapi import Request
    from fastapi.responses import JSONResponse

    secret = skill_key() if key is None else str(key or "")
    flag = (os.environ.get("EMORI_SKILL_API", "0") == "1") if enabled is None else bool(enabled)
    if not flag or not secret:
        # Deliberately silent about WHY at the HTTP layer: an unmounted route is a
        # 404, which tells a scanner nothing about whether this robot has an intake.
        log.info("/api/skill not mounted (EMORI_SKILL_API=%s, key=%s)",
                 os.environ.get("EMORI_SKILL_API", "0"), "set" if secret else "unset")
        return False
    interval = min_interval_s() if interval_s is None else max(0.0, float(interval_s))
    reserved = head_reserved if callable(head_reserved) else getattr(driver, "head_reserved", None)
    # The SAME limits the hostapp's own cue lane planned against, read once at startup
    # exactly like LiveDriver does: a robot that bisected EMORI_CUE_Z_MM down to what its
    # Stewart platform survives must not be overridden by the one lane driven from
    # outside the hostapp.
    config = getattr(driver, "cue_config", None)
    if not isinstance(config, CueConfig):
        config = CueConfig.from_env()

    state: dict[str, Optional[float]] = {}
    lock = threading.Lock()

    def _rate_limited(kind: str) -> bool:
        """One ACCEPTED request per interval PER KIND. A refused one spends nothing."""

        now = clock()
        with lock:
            last = state.get(kind)
            if last is not None and (now - last) < interval:
                return True
            state[kind] = now
            return False

    def _live_enabled() -> bool:
        """The switch as it is NOW; a caller-pinned flag (tests) is not re-read."""

        return True if enabled is not None else skill_api_enabled()

    def _head_reserved() -> bool:
        if reserved is None:
            return False
        try:
            return bool(reserved())
        except Exception:  # noqa: BLE001 - a broken predicate must not brick the intake
            return False

    def _refuse(error_code: str, status_code: int, *, with_ok: bool = False):
        """The single exit for every refusal, so a new one cannot forget its counter.

        The response bodies are byte-identical to what each path answered before this
        ledger existed -- ``with_ok`` reproduces the ``{"ok": false, ...}`` shape the
        three "the robot is not yours right now" answers have always used, and the
        status codes and their order are untouched.
        """

        bump(driver, "skill_refused_%s" % error_code)
        body = {"ok": False, "error_code": error_code} if with_ok \
            else {"error_code": error_code}
        return JSONResponse(body, status_code=status_code)

    # The body is deliberately NOT a typed FastAPI parameter: a validated parameter is
    # parsed BEFORE the handler runs, so a scanner posting junk from the venue LAN would
    # get a 422 -- an answer that says "this robot has a motion intake" to a caller that
    # passed neither the loopback check nor the key. Auth first, parse second.
    @app.post("/api/skill")
    async def api_skill(request: Request):
        # The flag and the key are re-read per request: an operator who clears the key
        # on a running robot has revoked the intake, not merely stopped mounting it.
        if not _live_enabled():
            return _refuse("skill_api_disabled", 403)
        client = getattr(request, "client", None)
        if not is_loopback(getattr(client, "host", None)):
            return _refuse("not_loopback", 403)
        presented = request.headers.get("X-Emori-Skill-Key", "") or ""
        if not hmac.compare_digest(presented, secret):
            return _refuse("bad_key", 403)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - any unparseable body is the same 400
            return _refuse("bad_skill", 400)
        body = payload if isinstance(payload, dict) else {}
        try:
            intents = skill_to_intents(str(body.get("skill", "")), body, config=config)
        except SkillError as e:
            return _refuse(e.code, 400)
        # The knob that says which lane may move the robot is a control, not a comment:
        # unless the ROS graph OWNS motion, a request through this intake is refused the
        # same way an add-on-owned robot refuses one.
        if motion_owner() != "ros":
            return _refuse("not_owner", 409, with_ok=True)
        # Ownership BEFORE the rate limit: a request the robot could never have
        # performed must not spend the caller's budget, and the two answers say
        # different things to the ROS node (retry later vs. you are not the writer).
        if _head_reserved():
            # Push-turn selection reads the human's turn out of the head's own pose;
            # any motion here would wipe the detector's rolling baseline.
            return _refuse("head_reserved", 423, with_ok=True)
        if not getattr(driver, "legacy_motion_enabled", True):
            return _refuse("not_owner", 409, with_ok=True)
        # Normalised ONCE, the same way `skill_to_intents` normalises before it builds
        # the intents: a request that parsed as a de-escalation settle must be treated
        # as one here too, and a stray space or capital must not buy a second rate-limit
        # budget under a name of its own.
        kind = str(body.get("skill", "")).strip()
        motion = str(body.get("name", "")).strip().lower()
        # The ONE exception to both gates below is the de-escalation settle. Safety
        # outranks expression -- exactly the rule the hostapp's own lane applies in
        # `_submit_engagement`, where the escalating cycle preempts a live cue and
        # submits with drop_if_busy=False. Dropped, or refused 429 because a cue replay
        # spent this kind's budget 200 ms earlier, it would leave the head parked
        # wherever the aborted plan put it while both ledgers claimed the de-escalation
        # happened, and with owner=ros the ROS graph is the ONLY thing that can produce
        # that settle at all.
        de_escalation = kind == "replay_motion" and motion == "calm_settle"
        if not de_escalation and _rate_limited(kind):
            return _refuse("rate_limited", 429)
        # drop_if_busy: a skill request is never more important than the motion already
        # playing, and a dropped one is reported honestly rather than queued behind it.
        submitted = bool(driver.submit_legacy_motion(intents,
                                                     drop_if_busy=not de_escalation))
        if not submitted and not getattr(driver, "legacy_motion_enabled", True):
            # Ownership flipped between the check above and the submit.
            return _refuse("not_owner", 409, with_ok=True)
        if submitted:
            bump(driver, "skill_accepted")
            bump(driver, "skill_intents_submitted", len(intents))
            # The liveness beat behind EMORI_ROS_OWNER_FALLBACK_S: this is the one
            # moment the hostapp KNOWS the ROS lane is still moving the robot.
            note = getattr(driver, "note_skill_accepted", None)
            if callable(note):
                try:
                    note()
                except Exception:  # noqa: BLE001 - liveness bookkeeping is never fatal
                    pass
        else:
            bump(driver, "skill_dropped")
        return {"ok": True, "submitted": len(intents) if submitted else 0,
                "dropped": not submitted}

    log.info("/api/skill mounted (loopback only, min interval %.2fs per skill, owner=%s)",
             interval, motion_owner())
    return True
