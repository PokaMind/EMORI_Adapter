"""People and badges: the roster behind the admin page's People tab.

A site admin adds a person (name + e-mail) on the phone admin (``emori-admin``,
:8070), sees the list, edits or removes people, and prints a QR badge for each. The
robot's camera then reads that badge to log the person into a Pokamind roleplay.

**Where it runs.** ``emori-admin`` holds no PII and no robot credential; like the
Pokamind token, the roster is read and written HERE, in the hostapp, which already
holds it, behind the loopback-only, token-gated ``/api/admin/roster*`` routes
(``admin_api.py``). emori-admin proxies them behind its PIN.

**What a badge is.** The QR carries the roster KEY and nothing else: an opaque
``EMORI:`` + ten random characters (:func:`new_badge_payload`), looked up verbatim by
``identity.Roster.resolve``. No name, no e-mail. Badges issued by hand before this page
(``EMORI:anna-001``) keep working and print the same way.

**Who may be added.** Pokamind admits a robot's learner only if the e-mail's domain is
in that robot's ``identity_domains`` (whoami); anything else fails at login as
``identity_not_in_scope``, which on a screenless robot looks like a badge being
ignored. So an out-of-scope e-mail is refused here, with the domains that would work.
When whoami cannot be asked (no token, no internet) it is allowed with a warning.
``@emori.local`` is never a person: it is the robots' own accounts.

**Which pathway.** A person may carry a ``pathway`` (it filters their module list by
EXACT category). Pokamind refuses a pathway outside this robot's record, and the badge
scanner drops one outside ``EMORI_PATHWAYS``, so the listing offers exactly the
pathways that would work (``pathways``: this robot's whoami pathways, kept to
``EMORI_PATHWAYS`` when that is set; else ``EMORI_PATHWAYS``) and an add or edit naming
another is refused as ``pathway_not_in_scope``. With neither known, any pathway is
accepted. The listing also carries the ``course_id`` badges are signed in with (the
roster's own, else this robot's).

**The file.** ``configs/roster.yaml`` is hand-edited too, and carries a banner, an
optional ``anonymous:`` block and the ``course_id``. A change is therefore applied as a
small TEXT edit of the ``staff:`` block -- everything else, comments included, is kept
byte for byte -- and the result is accepted only if it parses back to exactly the data
intended. If the layout cannot be edited in place, the ``staff:`` block alone is
re-emitted; only if even that does not verify is the whole file re-dumped (and that is
logged). Writes are atomic (temp file in the same directory + ``os.replace``), and the
live roster is reloaded in place (``Roster.reload``): no restart.

**Privacy.** Names, e-mails and badge ids never reach a log line: counts only.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Optional

from emori_adapter.hostapp import badges
from emori_adapter.hostapp.identity import (
    Roster,
    effective_course_id,
    file_stamp,
    pathway_allowlist,
    robot_name,
    roster_course_id,
)
from emori_adapter.hostapp.pokamind_whoami import WhoamiResult

log = logging.getLogger("emori.hostapp.roster_admin")

ROSTER_FILE_MODE = 0o600  # personal data: owner (the hostapp's user) only
NAME_MAX = 80
EMAIL_MAX = 254
PATHWAY_MAX = 500  # = the whoami contract's longest string
MAX_PEOPLE = 1000
MAX_SHEET_REFS = MAX_PEOPLE
RESERVED_DOMAIN = "emori.local"
BADGE_PREFIX = "EMORI:"
# Upper case + digits only (QR alphanumeric mode -> a version-1 symbol), minus the
# look-alikes a person might have to read back off a damaged card (0/O, 1/I/L, U/V).
BADGE_ALPHABET = "ABCDEFGHJKMNPQRSTWXYZ23456789"
BADGE_RANDOM_CHARS = 10  # 29**10 ~ 2**48: not guessable from the outside

# Zero-width (non-)joiners are part of how some scripts write names; every other
# format/control character is refused (bidi overrides can make a list lie).
_NAME_ALLOWED_FORMAT = frozenset({"\u200c", "\u200d"})
_EMAIL_LOCAL_RE = re.compile(
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*\Z")
_DOMAIN_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_STAFF_HEADER_RE = re.compile(r"""(?:staff|"staff"|'staff')[ \t]*:(?:[ \t\r]|$)""")
# What a roster file created by the admin page starts with (no file and no example).
NEW_FILE_HEADER = (
    "# EMORI staff roster: QR badge id -> Pokamind identity. Managed on the robot admin\n"
    "# page (People tab); every field is explained in configs/roster.example.yaml.\n")


class FieldError(ValueError):
    def __init__(self, field: str, code: str) -> None:
        super().__init__(code)
        self.field = field
        self.code = code


class RosterError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --------------------------------------------------------------------------- values
def clean_name(raw: object) -> str:
    """A display name: NFC, trimmed, runs of spaces collapsed, 1..NAME_MAX characters."""

    if type(raw) is not str:
        raise FieldError("name", "name_required")
    value = unicodedata.normalize("NFC", raw).strip()
    if not value:
        raise FieldError("name", "name_required")
    for ch in value:
        if ch.isprintable() or ch in _NAME_ALLOWED_FORMAT:
            continue
        if unicodedata.category(ch) == "Zs":  # a no-break space pasted from a document
            continue
        raise FieldError("name", "name_invalid")
    value = " ".join(value.split())
    if len(value) > NAME_MAX:
        raise FieldError("name", "name_too_long")
    return value


def clean_email(raw: object) -> str:
    """A plain ASCII address; the domain lower-cased, the local part as typed."""

    if type(raw) is not str:
        raise FieldError("email", "email_required")
    value = raw.strip()
    if not value:
        raise FieldError("email", "email_required")
    if len(value) > EMAIL_MAX or not value.isascii():
        raise FieldError("email", "email_invalid")
    local, at, domain = value.rpartition("@")
    domain = domain.lower()
    labels = domain.split(".")
    if (not at or not local or len(local) > 64 or _EMAIL_LOCAL_RE.fullmatch(local) is None
            or len(labels) < 2 or labels[-1].isdigit()
            or not all(_DOMAIN_LABEL_RE.fullmatch(label) for label in labels)):
        raise FieldError("email", "email_invalid")
    if domain == RESERVED_DOMAIN or domain.endswith("." + RESERVED_DOMAIN):
        # The robots' own learner accounts (robot-<id>@emori.local): never a person.
        raise FieldError("email", "email_reserved")
    return "%s@%s" % (local, domain)


def clean_pathway(raw: object) -> Optional[str]:
    """A person's optional ``pathway``: None when absent or empty, else the exact string.

    Pokamind compares it with the module category EXACTLY, so nothing inside it is
    changed (accents, case, inner spacing); only surrounding whitespace goes, as
    ``identity.Roster.resolve`` strips it too.
    """

    if raw is None:
        return None
    if type(raw) is not str:
        raise FieldError("pathway", "pathway_invalid")
    value = raw.strip()
    if not value:
        return None
    if len(value) > PATHWAY_MAX or any(not ch.isprintable() for ch in value):
        raise FieldError("pathway", "pathway_invalid")
    return value


def email_domain(email: str) -> str:
    return email.rpartition("@")[2].strip().lower()


def badge_ref(key: object) -> str:
    """A stable, non-reversible handle for one roster entry.

    What the phone and the URLs carry instead of the badge payload itself: a payload is
    a bearer credential for that person's sessions, a ref is not.
    """

    return hashlib.sha256(("emori-badge-ref|%s" % (key,)).encode("utf-8")).hexdigest()[:16]


def badge_works(key: object, record: object) -> bool:
    """Would ``identity.Roster.resolve`` log this badge in as this person?

    The same test resolve() applies (a dict with a non-empty, unpadded string
    ``student_id``), plus a string key: the scanner only ever hands over strings. One
    rule for the list's "valid" flag, its printable count and what is printed.
    """

    if not isinstance(key, str) or not isinstance(record, dict):
        return False
    student_id = record.get("student_id")
    return type(student_id) is str and bool(student_id) and student_id == student_id.strip()


def new_badge_payload(existing: set) -> str:
    while True:
        payload = BADGE_PREFIX + "".join(
            secrets.choice(BADGE_ALPHABET) for _ in range(BADGE_RANDOM_CHARS))
        if payload not in existing:
            return payload


def student_id_for(email: str, taken: set) -> str:
    """The Pokamind ``student_id`` for a person added here.

    Derived from the e-mail, so removing someone and adding them again (a lost badge)
    keeps the same learner; a hash rather than the address, because the id also
    travels in session ids and analytics envelopes.
    """

    digest = hashlib.sha256(("emori-roster-student|" + email.lower()).encode("utf-8"))
    base = "s-" + digest.hexdigest()[:12]
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = "%s-%d" % (base, n), n + 1
    return candidate


# --------------------------------------------------------------------------- the file
def _scalar(value: Any) -> str:
    # JSON is YAML flow syntax: a double-quoted string survives any name or address.
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        import yaml  # a hand-written date or the like: YAML's own flow form

        dumped = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True)
        return dumped.replace("\n...\n", "").strip()


def _norm(data: dict) -> dict:
    out = dict(data)
    if out.get("staff") is None:
        out["staff"] = {}
    return out


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_blank_or_comment(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


_PLAIN_FIELD_RE = re.compile(r"[a-z_][a-z0-9_]*\Z")
# YAML 1.1 reads these bare words as booleans/null, so a field named one is quoted.
_YAML11_WORDS = frozenset({"y", "n", "yes", "no", "true", "false", "on", "off", "null"})


def _field(name: Any) -> str:
    text = str(name)
    if _PLAIN_FIELD_RE.fullmatch(text) and text not in _YAML11_WORDS:
        return text
    return _scalar(text)


def _render_entry(key: Any, record: Any, indent: int, field_indent: int, nl: str) -> list[str]:
    head = " " * indent + _scalar(key if isinstance(key, (str, int)) else str(key)) + ":"
    if not isinstance(record, dict) or not record:
        return [head + " " + _scalar(record) + nl]
    lines = [head + nl]
    for field, value in record.items():
        lines.append(" " * field_indent + _field(field) + ": " + _scalar(value) + nl)
    return lines


class _Layout:
    """Where the ``staff:`` block and its entries are in the file, line by line."""

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.header: Optional[int] = None
        self.body_start = self.body_end = len(lines)
        self.indent = 2
        self.field_indent = 4
        self.entries: list[tuple[Any, int, int]] = []  # (key, first line, end line)
        self.editable = False
        self._scan()

    def _scan(self) -> None:
        import yaml

        lines = self.lines
        for i, line in enumerate(lines):
            if line[:1] not in (" ", "\t") and _STAFF_HEADER_RE.match(line):
                self.header = i
                break
        if self.header is None:
            return
        end = len(lines)
        for j in range(self.header + 1, len(lines)):
            line = lines[j]
            if not line.strip() or line[:1] in (" ", "\t") or line.startswith("#"):
                continue
            end = j
            break
        while end > self.header + 1 and (not lines[end - 1].strip()
                                         or lines[end - 1].startswith("#")):
            end -= 1
        self.body_start, self.body_end = self.header + 1, end
        rest = lines[self.header].split(":", 1)[1].split("#", 1)[0].strip()
        if rest not in ("", "{}", "null", "~"):
            return  # a flow mapping on the header line: not edited in place
        starts: list[int] = []
        entry_indent: Optional[int] = None
        for k in range(self.body_start, self.body_end):
            line = lines[k]
            if _is_blank_or_comment(line):
                continue
            if "\t" in line[:_indent_of(line) + 1]:
                return  # tab indentation: not edited in place
            ind = _indent_of(line)
            if entry_indent is None:
                entry_indent = ind
            if ind == entry_indent:
                starts.append(k)
            elif ind < entry_indent or ind == 0:
                return
        if entry_indent is not None:
            if entry_indent == 0:
                return
            self.indent = entry_indent
        field_indent: Optional[int] = None
        for n, start in enumerate(starts):
            stop = starts[n + 1] if n + 1 < len(starts) else self.body_end
            try:
                parsed = yaml.safe_load("".join(self._dedent(start, stop)))
            except yaml.YAMLError:
                return
            if not isinstance(parsed, dict) or len(parsed) != 1:
                return
            key = next(iter(parsed))
            self.entries.append((key, start, stop))
            if field_indent is None:
                for k in range(start + 1, stop):
                    if not _is_blank_or_comment(lines[k]):
                        field_indent = _indent_of(lines[k])
                        break
        if field_indent is not None and field_indent > self.indent:
            self.field_indent = field_indent
        else:
            self.field_indent = self.indent + 2
        self.editable = True

    def _dedent(self, start: int, stop: int) -> list[str]:
        out = []
        for line in self.lines[start:stop]:
            if line[:self.indent].strip() == "":
                out.append(line[self.indent:])
            else:
                out.append(line.lstrip(" "))
        return out

    def find(self, key: Any) -> Optional[tuple[int, int]]:
        for entry_key, start, stop in self.entries:
            if entry_key == key:
                return start, stop
        return None


def _surgery(text: str, op: str, key: Any, record: Optional[dict], nl: str,
             touched: tuple[str, ...] = ("name", "email"),
             removed: tuple[str, ...] = ()) -> Optional[str]:
    """The text with one entry added/updated/removed in place, or None if not possible.

    An update rewrites (or inserts) the ``touched`` fields' lines from ``record`` and
    deletes the lines of the ``removed`` fields; every other line is kept.
    """

    import yaml

    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += nl
    layout = _Layout(lines)
    if layout.header is None:
        if op != "add":
            return None
        tail = [] if not lines or not lines[-1].strip() else [nl]
        new_lines = lines + tail + ["staff:" + nl] + _render_entry(key, record, 2, 4, nl)
        return "".join(new_lines)
    if not layout.editable:
        return None
    if op == "add":
        insert_at = layout.entries[-1][2] if layout.entries else layout.body_end
        # Behind the last entry's own lines, but not behind its trailing blank lines.
        while insert_at > layout.body_start and not lines[insert_at - 1].strip():
            insert_at -= 1
        header = lines[layout.header]
        prefix = lines[:layout.header]
        if header.split(":", 1)[1].split("#", 1)[0].strip():  # `staff: {}` -> `staff:`
            header = header.split(":", 1)[0] + ":" + nl
        entry = _render_entry(key, record, layout.indent, layout.field_indent, nl)
        return "".join(prefix + [header] + lines[layout.header + 1:insert_at] + entry
                       + lines[insert_at:])
    span = layout.find(key)
    if span is None:
        return None
    start, stop = span
    if op == "remove":
        return "".join(lines[:start] + lines[stop:])
    # update: rewrite the name/email lines of a block-style entry, keep the rest
    try:
        head = yaml.safe_load(lines[start].strip())
    except yaml.YAMLError:
        return None
    segment = lines[start:stop]
    if not (isinstance(head, dict) and len(head) == 1 and next(iter(head.values())) is None):
        new_segment = _render_entry(key, record, layout.indent, layout.field_indent, nl)
        return "".join(lines[:start] + new_segment + lines[stop:])
    for field in touched:
        pattern = re.compile(r" {%d}(?:%s|\"%s\"|'%s')[ \t]*:(?:[ \t\r]|$)"
                             % (layout.field_indent, field, field, field))
        if field in removed:
            segment = [line for n, line in enumerate(segment)
                       if n == 0 or not pattern.match(line)]
            continue
        if record is None or field not in record:
            continue
        rendered = " " * layout.field_indent + field + ": " + _scalar(record[field]) + nl
        for k in range(1, len(segment)):
            if pattern.match(segment[k]):
                segment[k] = rendered
                break
        else:
            segment.insert(1, rendered)
    return "".join(lines[:start] + segment + lines[stop:])


def edit_roster_text(text: str, op: str, key: Any, record: Optional[dict] = None
                     ) -> tuple[str, str]:
    """``(new text, how)`` for one change to the roster file's ``staff:`` block.

    ``how`` is ``in_place`` (comments everywhere kept), ``staff_block`` (only the staff
    block re-emitted) or ``rewritten`` (whole file re-dumped). Raises
    :class:`RosterError` ``roster_unreadable`` for a file that does not parse to a
    mapping: a file nobody can read is never overwritten. In an ``update``, a field
    whose value is None is REMOVED from the record; fields not named are kept.
    """

    import yaml

    try:
        data = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError:
        raise RosterError("roster_unreadable") from None
    data = data or {}
    staff = data.get("staff") if isinstance(data, dict) else None
    if not isinstance(data, dict) or not (staff is None or isinstance(staff, dict)):
        raise RosterError("roster_unreadable")
    new_staff = dict(staff or {})
    touched: tuple[str, ...] = ("name", "email")
    removed: tuple[str, ...] = ()
    if op == "add":
        new_staff[key] = record
    elif op == "update":
        merged = dict(new_staff[key]) if isinstance(new_staff.get(key), dict) else {}
        changes = dict(record or {})
        touched = tuple(dict.fromkeys(("name", "email", *changes)))
        removed = tuple(field for field, value in changes.items() if value is None)
        for field, value in changes.items():
            if value is None:
                merged.pop(field, None)
            else:
                merged[field] = value
        new_staff[key] = record = merged
    elif op == "remove":
        new_staff.pop(key, None)
    else:  # pragma: no cover - programming error
        raise ValueError(op)
    expected = copy.deepcopy(data)
    expected["staff"] = new_staff
    expected = _norm(expected)
    nl = "\r\n" if "\r\n" in text else "\n"

    def verified(candidate: Optional[str]) -> bool:
        if candidate is None:
            return False
        try:
            parsed = yaml.safe_load(candidate) or {}
        except yaml.YAMLError:
            return False
        return isinstance(parsed, dict) and _norm(parsed) == expected

    candidate = _surgery(text, op, key, record, nl, touched, removed)
    if verified(candidate):
        return candidate, "in_place"
    # Re-emit the staff block alone; everything around it stays as it was.
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += nl
    layout = _Layout(lines)
    if layout.header is not None:
        block = ["staff:" + nl]
        for entry_key, entry in new_staff.items():
            block += _render_entry(entry_key, entry, 2, 4, nl)
        candidate = "".join(lines[:layout.header] + block + lines[layout.body_end:])
        if verified(candidate):
            return candidate, "staff_block"
    candidate = (
        "# EMORI staff roster. Rewritten by the robot admin page (People tab) on %s:%s"
        "# the previous layout could not be edited in place.%s"
        % (time.strftime("%Y-%m-%d", time.gmtime()), nl, nl)
        + yaml.safe_dump(expected, allow_unicode=True, sort_keys=False,
                         default_flow_style=False).replace("\n", nl))
    if verified(candidate):
        return candidate, "rewritten"
    raise RosterError("roster_write_failed")


def atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` so a reader sees the old file or the new one, never half.

    Same directory (``os.replace`` is atomic only within one filesystem), fsync'ed, and
    ALWAYS mode 0600, whatever the file had before: it holds names and e-mail addresses,
    and the badge ids that log people in. Only its owner reads it (the hostapp runs as
    ``pollen``; install.sh tightens a roster it copies the same way).
    """

    target = Path(os.path.realpath(path))
    fd, tmp = tempfile.mkstemp(prefix=".roster-", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, ROSTER_FILE_MODE)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:  # make the rename itself durable (POSIX; a no-op where unsupported)
        dir_fd = os.open(str(target.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


# --------------------------------------------------------------------------- the admin
class RosterAdmin:
    """The hostapp half of the People tab. No route wiring, no FastAPI.

    ``roster`` is the hostapp's ONE live roster; ``path`` is where writes go
    (``EMORI_ROSTER``, else ``configs/roster.yaml`` -- never the tracked example);
    ``scope`` answers this robot's whoami (cached; see
    ``admin_api.PokamindAdmin.current_whoami``). Every answer is ``(status, body)``.
    """

    def __init__(self, *, roster: Roster, path: str | Path,
                 scope: Optional[Callable[[], WhoamiResult]] = None,
                 robot_name: Callable[[], str] = robot_name,
                 max_people: int = MAX_PEOPLE) -> None:
        self._roster = roster
        self._path = Path(path)
        self._scope_source = scope
        self._robot_name = robot_name
        self._max = max_people
        self._lock = threading.Lock()

    # ----------------------------------------------------------------- reading
    def _read(self) -> tuple[str, dict]:
        """The roster text and data. Falls back to the file the live roster came from."""

        import yaml

        source = self._path
        if not source.exists():
            fallback = self._roster.source
            if fallback is None or not Path(fallback).exists():
                return "", {}
            source = Path(fallback)
        try:
            text = source.read_bytes().decode("utf-8-sig")
        except (OSError, UnicodeDecodeError):
            raise RosterError("roster_unreadable") from None
        try:
            data = yaml.safe_load(text) if text.strip() else {}
        except yaml.YAMLError:
            raise RosterError("roster_unreadable") from None
        data = data or {}
        # The same shape rule Roster.reload enforces before going live.
        if not isinstance(data, dict) or not all(
                data.get(block) is None or isinstance(data.get(block), dict)
                for block in ("staff", "anonymous")):
            raise RosterError("roster_unreadable")
        return text, data

    def _sync_live(self) -> bool:
        """Reload the live roster if the file changed under it (a hand edit). Never raises."""

        if not self._path.exists():
            return True
        same_file = (self._roster.source is not None
                     and os.path.realpath(self._roster.source) == os.path.realpath(self._path))
        if same_file and self._roster.loaded_stamp == file_stamp(self._path):
            return True
        try:
            self._roster.reload(self._path)
        except Exception as e:  # noqa: BLE001 - the running roster stays in service
            log.warning("roster file could not be reloaded (%s); the robot keeps the "
                        "roster it had", type(e).__name__)
            return False
        return True

    def _scope(self) -> dict:
        result: Optional[WhoamiResult] = None
        if callable(self._scope_source):
            try:
                result = self._scope_source()
            except Exception:  # noqa: BLE001 - an unanswerable scope is "unchecked"
                result = None
        who = result.whoami if result is not None and result.ok else None
        if who is None:
            return {"checked": False,
                    "code": result.code if result is not None else "no_robot_token",
                    "domains": [], "pathways": [], "site_name": None}
        domains = sorted({d.strip().lower() for d in who.identity_domains if d.strip()})
        # Exact strings, in the record's order: Pokamind compares them exactly.
        pathways = list(dict.fromkeys(
            p for p in who.pathways if isinstance(p, str) and p.strip() and p == p.strip()))
        return {"checked": True, "code": "ok", "domains": domains, "pathways": pathways,
                "site_name": (who.display_name or "").strip() or None}

    @staticmethod
    def _pathway_choices(scope: dict) -> tuple[list[str], bool]:
        """``(the pathways a person may carry, whether that list is binding)``.

        This robot's whoami pathways, kept to ``EMORI_PATHWAYS`` when that is set (the
        scanner drops any other); without a whoami answer, ``EMORI_PATHWAYS``; with
        neither, nothing is known and any pathway is accepted.
        """

        allow = pathway_allowlist()
        record = (scope.get("pathways") or []) if scope.get("checked") else []
        if record:
            return [p for p in record if not allow or p in allow], True
        if allow:
            return list(allow), True
        return [], False

    @staticmethod
    def _in_scope(email: str, scope: dict) -> Optional[bool]:
        domain = email_domain(email)
        if domain == RESERVED_DOMAIN or domain.endswith("." + RESERVED_DOMAIN):
            return False
        if not scope["checked"]:
            return None
        return domain in scope["domains"]

    def _people(self, data: dict, scope: dict) -> list[dict]:
        people = []
        for key, record in (data.get("staff") or {}).items():
            rec = record if isinstance(record, dict) else {}
            name = str(rec.get("name") or "").strip()
            email = str(rec.get("email") or "").strip()
            pathway = rec.get("pathway")
            people.append({
                "ref": badge_ref(key),
                "name": name,
                "email": email,
                "pathway": (pathway.strip() or None) if isinstance(pathway, str) else None,
                "in_scope": self._in_scope(email, scope) if email else None,
                # resolve() would ignore this record (anonymous session): show it so it
                # can be removed, but do not offer a badge that cannot work.
                "valid": badge_works(key, record),
            })
        people.sort(key=lambda p: (p["name"].casefold(), p["email"].casefold()))
        return people

    def _listing(self, data: dict, scope: dict) -> dict:
        people = self._people(data, scope)
        pathways, _binding = self._pathway_choices(scope)
        return {
            "people": people,
            "count": len(people),
            "max": self._max,
            "scope": {"checked": scope["checked"], "code": scope["code"],
                      "domains": scope["domains"]},
            # The People tab's pathway picker, and the course badges are signed in with.
            "pathways": pathways,
            "course_id": roster_course_id(data) or effective_course_id(),
            "robot_name": self._robot_name(),
            "site_name": scope["site_name"],
            "qr_available": badges.available(),
        }

    def _key_for(self, data: dict, ref: object) -> Any:
        if type(ref) is not str or not re.fullmatch(r"[0-9a-f]{16}", ref):
            raise FieldError("ref", "person_not_found")
        for key in (data.get("staff") or {}):
            if badge_ref(key) == ref:
                return key
        raise FieldError("ref", "person_not_found")

    # ----------------------------------------------------------------- answers
    def list(self) -> tuple[int, dict]:
        with self._lock:
            try:
                live = self._sync_live()
                _, data = self._read()
            except RosterError as e:
                return 409, {"error": e.code}
        body = self._listing(data, self._scope())
        body["live"] = live
        return 200, body

    def add(self, payload: object) -> tuple[int, dict]:
        body = payload if isinstance(payload, dict) else {}
        try:
            name = clean_name(body.get("name"))
            email = clean_email(body.get("email"))
            pathway = clean_pathway(body.get("pathway"))
        except FieldError as e:
            return 400, {"error": e.code, "field": e.field}
        return self._write("add", None, name, email, pathway, "pathway" in body)

    def update(self, payload: object) -> tuple[int, dict]:
        """Edit a person. ``pathway`` absent: kept; empty or null: removed."""

        body = payload if isinstance(payload, dict) else {}
        try:
            name = clean_name(body.get("name"))
            email = clean_email(body.get("email"))
            pathway = clean_pathway(body.get("pathway"))
        except FieldError as e:
            return 400, {"error": e.code, "field": e.field}
        return self._write("update", body.get("ref"), name, email, pathway, "pathway" in body)

    def remove(self, payload: object) -> tuple[int, dict]:
        body = payload if isinstance(payload, dict) else {}
        return self._write("remove", body.get("ref"), None, None)

    def _write(self, op: str, ref: object, name: Optional[str], email: Optional[str],
               pathway: Optional[str] = None, set_pathway: bool = False
               ) -> tuple[int, dict]:
        scope = self._scope() if op != "remove" else None
        warning = None
        if scope is not None and email is not None:
            if scope["checked"] and email_domain(email) not in scope["domains"]:
                return 422, {"error": "email_not_in_scope", "field": "email",
                             "domains": scope["domains"]}
            if not scope["checked"]:
                warning = "scope_unchecked"
        if scope is not None and pathway is not None:
            choices, binding = self._pathway_choices(scope)
            if binding and pathway not in choices:
                return 422, {"error": "pathway_not_in_scope", "field": "pathway",
                             "pathways": choices}
        with self._lock:
            try:
                text, data = self._read()
                staff = data.get("staff") or {}
                key: Any = None
                record: Optional[dict] = None
                if op != "add":
                    key = self._key_for(data, ref)
                if email is not None:
                    for other_key, other in staff.items():
                        if other_key == key or not isinstance(other, dict):
                            continue
                        if str(other.get("email") or "").strip().lower() == email.lower():
                            return 409, {"error": "duplicate_email", "field": "email"}
                if op == "add":
                    if len(staff) >= self._max:
                        return 409, {"error": "roster_full", "max": self._max}
                    key = new_badge_payload(set(staff))
                    taken = {str(r.get("student_id")) for r in staff.values()
                             if isinstance(r, dict)}
                    record = {"student_id": student_id_for(email or "", taken),
                              "email": email, "name": name}
                    if pathway is not None:
                        record["pathway"] = pathway
                elif op == "update":
                    record = {"email": email, "name": name}
                    if set_pathway:
                        record["pathway"] = pathway  # None removes it
                if not text.strip():
                    text = NEW_FILE_HEADER
                new_text, how = edit_roster_text(text, op, key, record)
                atomic_write(self._path, new_text)
            except FieldError as e:
                return 404, {"error": e.code}
            except RosterError as e:
                return (409 if e.code == "roster_unreadable" else 500), {"error": e.code}
            except OSError as e:
                log.warning("roster write failed (%s)", type(e).__name__)
                return 500, {"error": "roster_write_failed"}
            count = len(staff) + (1 if op == "add" else -1 if op == "remove" else 0)
            if how != "in_place":
                log.warning("roster: the file's layout could not be edited in place; "
                            "%s re-emitted (comments there were not kept)",
                            "the staff block was" if how == "staff_block" else "it was")
            live = self._sync_live()
            # Counts only: never a name, an address or a badge id.
            log.warning("roster: a person was %s on the admin page (%d on the roster)",
                        {"add": "added", "update": "edited", "remove": "removed"}[op], count)
            _, data = self._read()
        listing = self._listing(data, scope if scope is not None else self._scope())
        return 200, {"saved": True, "ref": badge_ref(key) if op != "remove" else None,
                     "warning": warning, "live": live, **listing}

    # ----------------------------------------------------------------- badges
    def _people_for_badges(self, refs: Optional[list]) -> list[tuple[str, str]]:
        _, data = self._read()
        wanted = None if refs is None else set(refs)
        out = []
        for key, record in (data.get("staff") or {}).items():
            # The list's own rule ("valid"): a badge resolve() would turn into an
            # anonymous session is never printed, not even by "Print all".
            if not badge_works(key, record):
                continue
            if wanted is not None and badge_ref(key) not in wanted:
                continue
            out.append((str(record.get("name") or "").strip(), key))
        out.sort(key=lambda item: item[0].casefold())
        return [(key, name) for name, key in out]

    def _labels(self) -> tuple[str, Optional[str]]:
        return self._robot_name(), self._scope()["site_name"]

    def badge(self, payload: object) -> tuple[int, Any, str]:
        """``(status, bytes | error body, media type)`` for one badge download."""

        body = payload if isinstance(payload, dict) else {}
        fmt = body.get("format", "svg")
        if fmt not in ("svg", "png"):
            return 400, {"error": "bad_format"}, "application/json"
        if not badges.available():
            return 409, {"error": "qr_unavailable"}, "application/json"
        try:
            with self._lock:
                _, data = self._read()
                key = self._key_for(data, body.get("ref"))
                record = (data.get("staff") or {}).get(key)
            if not badge_works(key, record):
                return 404, {"error": "person_not_found"}, "application/json"
            if fmt == "png":
                return 200, badges.qr_png(key), "image/png"
            robot, site = self._labels()
            svg = badges.badge_svg(key, str(record.get("name") or ""), robot_name=robot,
                                   site_name=site)
            return 200, ('<?xml version="1.0" encoding="UTF-8"?>\n' + svg).encode("utf-8"), \
                "image/svg+xml"
        except FieldError as e:
            return 404, {"error": e.code}, "application/json"
        except RosterError as e:
            return 409, {"error": e.code}, "application/json"

    def sheet(self, payload: object) -> tuple[int, Any, str]:
        """``(status, html bytes | error body, media type)``: the A4 print view."""

        body = payload if isinstance(payload, dict) else {}
        refs = body.get("refs")
        if refs is not None and (not isinstance(refs, list) or len(refs) > MAX_SHEET_REFS
                                 or not all(type(r) is str for r in refs)):
            return 400, {"error": "bad_refs"}, "application/json"
        if not badges.available():
            return 409, {"error": "qr_unavailable"}, "application/json"
        try:
            with self._lock:
                people = self._people_for_badges(refs)
        except RosterError as e:
            return 409, {"error": e.code}, "application/json"
        robot, site = self._labels()
        log.info("roster: a badge sheet was printed (%d badges)", len(people))
        return 200, badges.sheet_html(people, robot_name=robot, site_name=site).encode(
            "utf-8"), "text/html; charset=utf-8"
