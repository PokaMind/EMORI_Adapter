"""Identity resolution for the screenless robot: a QR staff badge -> Pokamind identity.

Flow (see docs/adr-001-onrobot-screenless.md, "Identity & session start"):
the kiosk page scans the person's QR badge with the camera (jsQR, in-browser), POSTs the
decoded payload to the host app's /api/identify, which resolves it against a staff
**roster** to a Pokamind identity (email + student_id + their assigned module/pathway) and
mints a partner SSO JWT. A non-empty unregistered badge is rejected before it can
preempt add-on media; the explicit empty-badge path is the **anonymous** shared-robot
training flow (frictionless drop-in practice, no per-staff tracking).

The roster keeps the staff PII on the robot/host side only; the QR just carries an opaque
badge id. ``decode_qr`` (optional, lazy OpenCV) exists for non-browser decoding; the
primary path decodes in the browser and sends only the payload string here.

A staff record is ``{student_id, email, name}`` plus the optional ``module_id`` /
``pathway`` (what they practise) and ``lang`` (``ca``/``en``). Session language is
ALWAYS the kiosk's global
``lang_state.current()`` -- ``lang`` is still parsed and normalised here for backward
roster compatibility (an older ``roster.yaml`` with per-badge ``lang:`` lines must
keep loading without a validation error), but the value is no longer consulted to
choose a session's language; it rides on ``Identity`` unread by every caller.

The **anonymous** identity is not a placeholder: it is this robot's own Pokamind
account, and it is the identity almost every drop-in session runs under. It is
therefore derived from the robot's id (see :func:`robot_identity`) rather than
shipped as a constant -- ``deploy/robot/install.sh`` copies
``configs/roster.example.yaml`` onto every robot, so a hard-coded id would give the
whole fleet ONE learner: interleaved session history, a shared weekly session cap and
analysis emails for sessions someone else had.

Which id: when the robot has its own Pokamind token
(``robot_token.py``), the token's ``robot_id`` IS the Pokamind identity -- Pokamind
admits ``robot-<robot_id>@emori.local`` only for the robot whose key signed the login,
so any other id would be refused as ``identity_not_in_scope``. ``EMORI_ROBOT_ID`` may
then be left unset (it is derived from the token); set to a DIFFERENT robot it is
reported (an ERROR at hostapp start, ``robot_id_mismatch`` in readycheck) and the
token still wins. Without a token, ``EMORI_ROBOT_ID`` is the identity, as before.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from emori_adapter.robot_token import configured as configured_token
from emori_adapter.robot_token import robot_slug

log = logging.getLogger("emori.identity")

# Kiosk languages a roster badge may pin (the set Pokamind whitelists for kiosk sessions;
# see pages.UI_STRINGS and driver._SUMMARY_TEXT, which must cover the same two).
KIOSK_LANGS = ("ca", "en")

# What the robot's own account is called in Pokamind (session reports, result emails).
DEFAULT_ROBOT_NAME = "EMORI"
# The SSO course claim when nothing names one: no roster ``course_id``, no
# ``EMORI_COURSE_ID`` (which the site settings layer fills from this robot's Pokamind
# record when it is left unset locally). The claim must never be empty.
DEFAULT_COURSE_ID = "emori"
# The id used when EMORI_ROBOT_ID is unset. Deliberately the pre-existing constant: an
# unconfigured robot keeps working and stays recognisable as unconfigured, instead of
# minting a plausible-looking unique id (a hostname or a random suffix) that would
# silently create a NEW Pokamind learner after every reinstall.
UNCONFIGURED_ROBOT_SLUG = "anonymous"


def _robot_slug(robot_id: str) -> str:
    """``EMORI_ROBOT_ID`` -> a token safe in an email local part and a session id.

    The value ends up in ``student_id`` and in ``<student_id>@emori.local``, both of
    which travel through SSO claims, server-side records and SMTP. A deployment id
    with a space or a slash in it must degrade to an addressable account rather than to
    an unroutable address nobody notices until the first report bounces.

    The one implementation lives in ``robot_token.robot_slug`` so the readiness
    check's robot_id cross-check normalises exactly as this account derivation does.
    """
    return robot_slug(robot_id)


def pokamind_robot_id() -> str:
    """This robot's Pokamind identity, as a slug ("" when nothing configures one).

    The token's ``robot_id`` when this process runs with a per-robot token (bound at
    process start, ``robot_token.configured``), else ``EMORI_ROBOT_ID``. A conflict
    between the two is not decided here -- the token wins -- but reported once by the
    hostapp at start and by readycheck (``robot_id_mismatch``).
    """
    token = configured_token().token
    if token is not None:
        return token.robot_id
    return _robot_slug(os.environ.get("EMORI_ROBOT_ID", "") or "")


def robot_name() -> str:
    """What this robot's own account is called in Pokamind (reports, result emails)."""
    return (os.environ.get("EMORI_ROBOT_NAME", "") or "").strip() or DEFAULT_ROBOT_NAME


def effective_course_id() -> str:
    """This robot's course: ``EMORI_COURSE_ID``, else :data:`DEFAULT_COURSE_ID`.

    ``EMORI_COURSE_ID`` is already the effective value when this runs: an admin-saved
    or explicit ``.env`` value, else the course issued with this robot's Pokamind token
    (``site_settings.apply`` put it in the environment at process start).
    """
    return (os.environ.get("EMORI_COURSE_ID", "") or "").strip() or DEFAULT_COURSE_ID


def roster_course_id(data: object) -> Optional[str]:
    """A roster file's own ``course_id``, or None when it names none (absent or empty).

    An explicit roster course wins over the robot's; without one, a roster follows
    :func:`effective_course_id`.
    """
    if not isinstance(data, dict):
        return None
    value = data.get("course_id")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def robot_identity() -> dict[str, str]:
    """This robot's own Pokamind account: ``{student_id, email, name}``.

    Unique per robot by construction (the token's robot_id, else ``EMORI_ROBOT_ID``;
    see :func:`pokamind_robot_id`), because the anonymous drop-in identity IS an
    account: sessions, the weekly cap and the analysis email all accrue to it.
    PRODUCTION requires that this account exist on exactly one robot.
    """
    robot_id = pokamind_robot_id()
    if not robot_id:
        log.warning(
            "EMORI_ROBOT_ID is unset and there is no Pokamind robot token: the anonymous "
            "identity falls back to 'robot-%s', which every unconfigured robot shares - "
            "install this robot's own token (docs/deploy-a-new-robot.md)",
            UNCONFIGURED_ROBOT_SLUG,
        )
        robot_id = UNCONFIGURED_ROBOT_SLUG
    return {"student_id": f"robot-{robot_id}",
            "email": f"robot-{robot_id}@emori.local",
            "name": robot_name()}


def _anonymous_record(record: object) -> dict[str, str]:
    """Merge an explicit roster ``anonymous:`` block over the derived robot account.

    An explicit entry wins field by field: a deployment that pins only ``student_id``
    (a per-room shared account, say) gets ``<student_id>@emori.local`` -- the same rule
    the staff path uses -- and never inherits ANOTHER robot's mailbox. The derived id is
    computed only when it is actually needed, so pinning one does not log a warning
    about an EMORI_ROBOT_ID this deployment deliberately does not use.
    """
    if not isinstance(record, dict) or not record:
        return robot_identity()
    student_id = str(record.get("student_id") or "").strip() or robot_identity()["student_id"]
    email = str(record.get("email") or "").strip() or f"{student_id}@emori.local"
    name = str(record.get("name") or "").strip() or robot_name()
    if configured_token().token is not None and email.lower().endswith("@emori.local"):
        own = robot_identity()
        if email.lower() != own["email"].lower():
            # With a per-robot token, Pokamind refuses every @emori.local learner except
            # this robot's own (identity_not_in_scope): a pinned one would make EVERY
            # drop-in session fail while readycheck, which probes the robot's own
            # account, still passed. The roster may keep its display name.
            log.warning(
                "roster 'anonymous' pins an @emori.local learner that is not this robot's "
                "own account; with a Pokamind robot token drop-in sessions run as %s",
                own["student_id"],
            )
            return {"student_id": own["student_id"], "email": own["email"], "name": name}
    return {"student_id": student_id, "email": email, "name": name}


class Identity(BaseModel):
    student_id: str
    email: str
    course_id: str
    name: str = ""
    module_id: Optional[str] = None
    pathway: Optional[str] = None
    # Roster-parsed for BACKWARD COMPATIBILITY ONLY. Session language is ALWAYS the
    # kiosk's global lang_state.current() (no per-badge or per-bundle value may
    # override it). This field is still populated by
    # _roster_lang so an older roster.yaml keeps loading, but nothing reads it to
    # choose a session's language anymore.
    lang: Optional[str] = None
    anonymous: bool = False


def _roster_lang(value: object) -> Optional[str]:
    """A staff record's optional ``lang`` -> a normalised kiosk language, or None.

    Parsed and validated for BACKWARD ROSTER COMPATIBILITY ONLY: session language is
    ALWAYS ``lang_state.current()``,
    so this value is no longer forwarded into the ``emori_lang`` SSO claim or consulted
    anywhere else. Kept normalising (and warning on a bad value) all the same, so a
    roster typo still shows up the same way it always did to anyone reading the log,
    even though nothing depends on the result anymore.
    """
    if value is None:
        return None
    lang = str(value).strip().lower()
    if lang in KIOSK_LANGS:
        return lang
    log.warning("staff roster lang %r is not one of %s; using the kiosk default",
                lang, KIOSK_LANGS)
    return None


def _pathway_allowlist() -> tuple[str, ...]:
    """The exact pathway strings this deployment serves (``EMORI_PATHWAYS``).

    Comma-separated, same idiom as every other list knob here.  Empty (the default)
    means "no allowlist configured", which deliberately keeps whatever the roster says:
    a site that has not listed its categories must not have its working badges disabled
    by this check.  An empty value is filled from this robot's Pokamind record when it
    has one (site_settings); ``*`` says "no allowlist" explicitly, so the record's list
    is never derived into it.
    """
    raw = os.environ.get("EMORI_PATHWAYS", "") or ""
    if raw.strip() == "*":
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def pathway_allowlist() -> tuple[str, ...]:
    """``EMORI_PATHWAYS`` as a tuple (empty: no allowlist). See :func:`_pathway_allowlist`."""
    return _pathway_allowlist()


def _value_code(value: str) -> str:
    """A rejected roster value -> a stable, non-reversible ``V-xxxxxx`` label.

    Same HMAC and the same salt as :func:`metrics_store.person_code` (imported
    lazily: this is a cold, misconfiguration-only path and ``identity`` is
    otherwise free of the store).  Different prefix, because a value code and a
    person code must never be mistaken for one another in a journal.
    """

    from emori_adapter.hostapp.metrics_store import resolve_salt

    digest = hmac.new(
        resolve_salt().encode("utf-8"), value.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return "V-" + digest[:6]


def _roster_pathway(value: object) -> Optional[str]:
    """A staff record's optional ``pathway`` -> one this deployment serves, or None.

    The value reaches Pokamind as an SSO claim and is matched against a module's
    ``category`` EXACTLY -- accents, case and spacing included ("Example pathway"). A
    value that matches nothing filters the module list to zero, the session never
    starts, and on a screenless robot that is indistinguishable from the robot ignoring
    the badge: it is what a placeholder such as ``pathway: reception-deescalation``
    does in production.

    So an unlisted value is DROPPED with a WARNING rather than forwarded. The badge then
    behaves exactly like the known-working anonymous path (all modules offered) instead
    of being silently out of service -- fail open, and say so.
    """
    if value is None:
        return None
    pathway = str(value).strip()
    if not pathway:
        return None
    allowed = _pathway_allowlist()
    if not allowed or pathway in allowed:
        return pathway
    # The FIELD, never its value. A length bound is not a privacy bound: this
    # line fires precisely when a roster put something unexpected in `pathway`,
    # and the unexpected thing that costs the most is an identity -- "Anna Example
    # <anna@example.org>" is 31 characters, so any cap generous enough to show a
    # real pathway ("Example pathway") is generous enough to print a
    # whole person, into a journal that is world-readable and survives reboots.
    #
    # Nothing is lost, because of who reads this. The diagnostic exists so an
    # operator can fix a roster typo, and they are holding the roster file: they
    # need to know WHICH field was rejected and that EMORI_PATHWAYS is the list
    # it was checked against -- both of which are in the .env and the YAML in
    # front of them -- not to be told back a string they wrote. `len(allowed)`
    # rather than the tuple for the same reason, and because the tuple made the
    # line wrap.
    #
    # The code is what makes it operable at scale: it is stable for a given
    # value under the robot's salt, so a hundred badges carrying ONE bad pathway
    # group into one repeated code, and two different bad pathways are visibly
    # two problems. Same construction and same salt as the dashboard's person
    # codes (metrics_store.person_code), so the "never the value, always a code
    # for it" discipline is one mechanism here, not two.
    log.warning("staff roster field 'pathway' is not one of the %d EMORI_PATHWAYS "
                "(value withheld; code %s); dropping the claim so this badge sees "
                "every module", len(allowed), _value_code(pathway))
    return None


def file_stamp(path: str | Path) -> Optional[tuple[int, int]]:
    """``(mtime_ns, size)`` of a roster file, or None when it cannot be stat'ed.

    What "the roster on disk changed" is measured with: the admin page's People tab
    compares it with the stamp the live roster was loaded at (:meth:`Roster.reload`).
    """

    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


class Roster:
    """Maps QR badge payloads to staff identities, with an anonymous fallback."""

    def __init__(self, staff: dict[str, dict], anonymous: Optional[dict] = None,
                 course_id: Optional[str] = None) -> None:
        # None or empty: the roster names no course, so it follows this robot's
        # (effective_course_id: EMORI_COURSE_ID, the course issued with its token, 'emori').
        self.course_id = (course_id or "").strip() or effective_course_id()
        self._staff = staff
        # None (no roster block) = this robot's own account, derived from EMORI_ROBOT_ID.
        self._anon = _anonymous_record(anonymous)
        # Where this roster was read from, and that file's stamp at the time: None for
        # Roster.default() / a roster built in memory.
        self.source: Optional[Path] = None
        self.loaded_stamp: Optional[tuple[int, int]] = None

    @staticmethod
    def _parse(path: str | Path) -> dict:
        import yaml

        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError("roster is not a mapping")
        return data

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Roster":
        stamp = file_stamp(path)
        data = cls._parse(path)
        roster = cls(staff=data.get("staff", {}) or {}, anonymous=data.get("anonymous"),
                     course_id=roster_course_id(data))
        roster.source, roster.loaded_stamp = Path(path), stamp
        return roster

    def reload(self, path: str | Path | None = None) -> None:
        """Re-read the roster file IN PLACE: the explicit live-reload hook.

        The hostapp builds ONE Roster at start and every route (``/api/identify``,
        ``/api/badge/claim``, ``/api/voice-select``, ``/healthz``, the dashboard's
        participation denominator) holds that same object, so swapping its state here
        is what lets a person added on the admin page's People tab badge in without a
        restart. The new file is parsed completely BEFORE anything is swapped: a file
        that does not parse raises and the roster keeps serving what it had.
        """

        target = Path(path) if path is not None else self.source
        if target is None:
            raise ValueError("this roster was not loaded from a file")
        stamp = file_stamp(target)
        data = self._parse(target)
        # Shape-checked BEFORE the swap (the same rule roster_admin._read applies): a
        # `staff: [..]` or `staff: 5` would otherwise go live and turn every badge scan
        # into a 500 (`list.get`). Refused, the robot keeps the roster it had.
        for block in ("staff", "anonymous"):
            if not (data.get(block) is None or isinstance(data.get(block), dict)):
                raise ValueError("roster %r is not a mapping" % block)
        fresh = Roster(staff=data.get("staff", {}) or {}, anonymous=data.get("anonymous"),
                       course_id=roster_course_id(data))
        # Plain attribute stores: a resolve() running concurrently sees the old staff
        # dict or the new one, never a half-built one.
        self._staff = fresh._staff
        self._anon = fresh._anon
        self.course_id = fresh.course_id
        self.source, self.loaded_stamp = target, stamp
        log.info("roster reloaded: %d staff", len(self._staff))

    @classmethod
    def default(cls) -> "Roster":
        """A roster with only the anonymous identity (no staff) - used when none is configured.

        Its course is this robot's (:func:`effective_course_id`)."""
        return cls(staff={})

    @property
    def staff_count(self) -> int:
        """How many staff are on the roster - and nothing about who they are.

        The management dashboard's participation KPI needs a denominator ("of the
        site's staff, how many practised this month"). It is given this number rather than
        the roster itself, so no name, e-mail or badge id can reach a reporting
        surface even by accident.
        """
        return len(self._staff)

    def badge_claim_sample(self) -> tuple[str, Optional[str]]:
        """``(course_id, a pathway this roster actually pins)`` -- claims, never an identity.

        The readiness self-check mints its OWN probe SSO login carrying these two
        claims, so "a badge session starts" becomes a gate instead of a guess (the
        existing probe deliberately sends neither, which is why 14/14 PASS said nothing
        about badges).  It therefore scans for the FIRST record that carries a pathway
        rather than reading the first record: a roster whose first badge pins none but
        whose second pins a dead one would otherwise SKIP the gate and leave exactly the
        blind spot it was added to close.  Deliberately returns no student_id, e-mail,
        name or badge value: the probe brings its own identity, and keeping staff PII
        inside this class is this class's whole job.
        """
        for rec in self._staff.values():
            if isinstance(rec, dict):
                pathway = _roster_pathway(rec.get("pathway"))
                if pathway:
                    return self.course_id, pathway
        return self.course_id, None

    def anonymous_identity(self) -> Identity:
        """The robot's own account: every drop-in session that is not a known badge."""
        return Identity(student_id=self._anon["student_id"], email=self._anon["email"],
                        course_id=self.course_id, name=self._anon["name"], anonymous=True)

    def resolve(self, qr_payload: Optional[str]) -> Identity:
        staff, course_id = self._staff, self.course_id  # one consistent view (reload)
        rec = staff.get((qr_payload or "").strip()) if qr_payload else None
        if not rec:
            if qr_payload:
                # QR values are opaque identifiers and must never enter logs.
                log.info("unknown badge -> anonymous session")
            return self.anonymous_identity()
        if not isinstance(rec, dict):
            log.warning("invalid staff roster record -> anonymous session")
            return self.anonymous_identity()
        student_id = rec.get("student_id")
        if type(student_id) is not str or not student_id or student_id != student_id.strip():
            # Never use the raw badge as an identity fallback: session IDs,
            # SSO claims, recordings, and FIWARE envelopes may retain a staff
            # identity while the opaque QR itself must remain transient.
            log.warning("invalid staff roster record -> anonymous session")
            return self.anonymous_identity()
        return Identity(
            student_id=student_id,
            email=str(rec.get("email", f"{student_id}@emori.local")),
            course_id=course_id,
            name=str(rec.get("name", "")),
            module_id=rec.get("module_id"),
            pathway=_roster_pathway(rec.get("pathway")),
            lang=_roster_lang(rec.get("lang")),
            anonymous=False,
        )


def decode_qr(image_bytes: bytes) -> Optional[str]:
    """Decode a QR from an image (optional, lazy OpenCV). Primary path decodes in-browser."""
    try:
        import cv2  # lazy
        import numpy as np
    except ImportError:
        log.info("OpenCV not installed; QR image decode unavailable (use the browser jsQR path)")
        return None
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None
    data, _, _ = cv2.QRCodeDetector().detectAndDecode(img)
    return data or None
