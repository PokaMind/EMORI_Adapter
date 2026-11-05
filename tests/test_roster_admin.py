"""The People tab's roster core: who may be added, how the file is edited, what a badge is.

Pinned here, without FastAPI and without a network:

* a name and an e-mail are validated (length, control characters, ``@emori.local``),
  an e-mail outside this robot's Pokamind ``identity_domains`` is refused (allowed
  with a warning only when whoami cannot be asked), and duplicates are refused;
* add / edit / remove change the ``staff:`` block and NOTHING else in the file: the
  banner, the commented ``anonymous:`` block, ``course_id``, unknown keys and the
  comments inside other entries survive byte for byte;
* the running roster picks a change up without a restart (``Roster.reload``);
* a badge's QR carries exactly the string ``identity.Roster.resolve`` looks up, as the
  scanner path hands it over (``server._decoded_qr_payload``) -- never a name or an
  e-mail -- and decodes back to it from the printed image;
* no name, address or badge id reaches a log line.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

from emori_adapter.hostapp import badges, roster_admin
from emori_adapter.hostapp.identity import Roster, decode_qr
from emori_adapter.hostapp.pokamind_whoami import Whoami, WhoamiResult
from emori_adapter.hostapp.roster_admin import (
    FieldError,
    RosterAdmin,
    badge_ref,
    clean_email,
    clean_name,
    edit_roster_text,
)
from emori_adapter.hostapp.server import _decoded_qr_payload

# The roster the repository SHIPS: nobody on it (people are added on the admin page).
EXAMPLE = Path("configs/roster.example.yaml")
# A populated roster in the example's layout (banner, course, a commented anonymous
# block, entries with comments of their own), written per test.
SAMPLE = """\
# EMORI staff roster (copy to roster.yaml; point the host app at it via EMORI_ROSTER).
# Maps a QR-badge payload -> Pokamind identity.
course_id: emori-demo

# --- The anonymous identity ---
#anonymous:
#  student_id: robot-lobby-01
#  email: robot-lobby-01@emori.local
#  name: EMORI

# Identified staff.
staff:
  "EMORI:anna-001":
    student_id: anna-001
    email: anna@example.org
    name: "Anna Example"
    # EXACT category string only; omit to offer every module.
    #pathway: "<exact category string>"
  "EMORI:marc-002":
    student_id: marc-002
    email: marc@example.org
    name: "Marc Example"
    # A real module id: drops straight into one scenario (no picker). A stale or
    # invented id is another silent dead end.
    #module_id: <module id>
"""
PAYLOAD_RE = re.compile(r"EMORI:[%s]{10}\Z" % roster_admin.BADGE_ALPHABET)


@pytest.fixture(autouse=True)
def _robot_id(monkeypatch):
    # Roster.reload derives the anonymous account; give it an id so it stays quiet.
    monkeypatch.setenv("EMORI_ROBOT_ID", "bench-01")
    # No allowlist and no course of this test process leak into the roster's answers.
    monkeypatch.delenv("EMORI_PATHWAYS", raising=False)
    monkeypatch.delenv("EMORI_COURSE_ID", raising=False)


def _scope(domains=("example.org",), *, ok=True, code="ok", pathways=()):
    if not ok:
        return lambda: WhoamiResult(code=code)
    who = Whoami(robot_id="bench-01", display_name="Demo reception", status="active",
                 credential_kind="registry", key_fingerprint=None, key_version=1,
                 org="example-org", partner="emori-robot", course_id="", languages=("ca",),
                 pathways=tuple(pathways), identity_domains=tuple(domains), modules=(),
                 features={})
    return lambda: WhoamiResult(ok=True, code="ok", http_status=200, whoami=who)


def _copy_sample(tmp_path) -> Path:
    path = tmp_path / "roster.yaml"
    path.write_text(SAMPLE, encoding="utf-8")
    return path


def _copy_example(tmp_path) -> Path:
    path = tmp_path / "roster.yaml"
    shutil.copyfile(EXAMPLE, path)
    return path


def _admin(path: Path, scope=None, roster: Roster | None = None) -> RosterAdmin:
    live = roster if roster is not None else Roster.from_yaml(path)
    return RosterAdmin(roster=live, path=path, scope=scope or _scope(),
                       robot_name=lambda: "EMORI")


def _staff(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["staff"]


def _key_of(path: Path, email: str) -> str:
    return next(k for k, r in _staff(path).items() if r["email"] == email)


# --------------------------------------------------------------------------- values
@pytest.mark.parametrize("raw, clean", [
    ("Núria Example", "Núria Example"),
    ("  Núria   Example i Example \n", "Núria Example i Example"),
    ("Jo Ann", "Jo Ann"),                       # a pasted no-break space
    ("Aná", "Aná"),                            # NFC
    ("مهسا\u200cرضایی", "مهسا\u200cرضایی"),          # a zero-width non-joiner is part of it
    ("X" * 80, "X" * 80),
])
def test_a_name_is_trimmed_and_normalised(raw, clean):
    assert clean_name(raw) == clean


@pytest.mark.parametrize("raw, code", [
    (None, "name_required"), (7, "name_required"), ("", "name_required"),
    ("   ", "name_required"), ("X" * 81, "name_too_long"),
    ("Anna\nExample", "name_invalid"), ("Anna\tExample", "name_invalid"),
    ("Anna\x00", "name_invalid"), ("Anna\u202eelpmaxE", "name_invalid"),  # bidi override
    ("An\u2028na", "name_invalid"),
])
def test_a_bad_name_is_refused_with_a_code(raw, code):
    with pytest.raises(FieldError) as caught:
        clean_name(raw)
    assert (caught.value.field, caught.value.code) == ("name", code)


@pytest.mark.parametrize("raw, clean", [
    ("anna@example.org", "anna@example.org"),
    ("  Anna.Example@Example.Org ", "Anna.Example@example.org"),
    ("o'brien+robot@site.example.org", "o'brien+robot@site.example.org"),
])
def test_an_email_keeps_its_local_part_and_lowercases_its_domain(raw, clean):
    assert clean_email(raw) == clean


@pytest.mark.parametrize("raw, code", [
    (None, "email_required"), ("", "email_required"),
    ("anna", "email_invalid"), ("anna@", "email_invalid"), ("@example.org", "email_invalid"),
    ("anna@example", "email_invalid"), ("an na@example.org", "email_invalid"),
    ("anna..g@example.org", "email_invalid"), ("anna@example..org", "email_invalid"),
    ("anna@-example.org", "email_invalid"), ("núria@example.org", "email_invalid"),
    ("anna@10.0.0.1", "email_invalid"), ("a" * 65 + "@example.org", "email_invalid"),
    ("robot-bench-01@emori.local", "email_reserved"),
    ("anna@EMORI.local", "email_reserved"), ("anna@x.emori.local", "email_reserved"),
])
def test_a_bad_email_is_refused_with_a_code(raw, code):
    with pytest.raises(FieldError) as caught:
        clean_email(raw)
    assert (caught.value.field, caught.value.code) == ("email", code)


# --------------------------------------------------------------------------- add / edit / remove
def test_add_edit_remove_round_trip_through_the_file_and_the_live_roster(tmp_path):
    path = _copy_sample(tmp_path)
    live = Roster.from_yaml(path)
    admin = _admin(path, roster=live)

    status, body = admin.add({"name": " Núria Example ", "email": "nuria@Example.org"})
    assert status == 200 and body["saved"] is True and body["warning"] is None
    key = _key_of(path, "nuria@example.org")
    assert PAYLOAD_RE.fullmatch(key)
    record = _staff(path)[key]
    assert record["name"] == "Núria Example" and record["student_id"].startswith("s-")
    assert body["ref"] == badge_ref(key) and body["count"] == 3
    assert {p["name"] for p in body["people"]} == {"Anna Example", "Marc Example", "Núria Example"}
    # live, no restart: the SAME roster object now knows the badge
    assert live.resolve(key).email == "nuria@example.org"
    assert live.resolve(key).anonymous is False

    status, body = admin.update({"ref": badge_ref(key), "name": "Núria Example i Example",
                                 "email": "nuria.second@example.org"})
    assert status == 200
    assert _staff(path)[key]["student_id"] == record["student_id"]  # same learner, same badge
    assert live.resolve(key).name == "Núria Example i Example"
    assert live.resolve(key).email == "nuria.second@example.org"

    status, body = admin.remove({"ref": badge_ref(key)})
    assert status == 200 and body["count"] == 2 and body["ref"] is None
    assert key not in _staff(path)
    assert live.resolve(key).anonymous is True


def test_a_change_keeps_everything_outside_the_staff_block_byte_for_byte(tmp_path):
    path = _copy_sample(tmp_path)
    original = path.read_text(encoding="utf-8")
    admin = _admin(path)

    admin.add({"name": "Núria Example", "email": "nuria@example.org"})
    admin.update({"ref": badge_ref("EMORI:marc-002"), "name": "Marc Example i Example",
                  "email": "marc@example.org"})
    admin.remove({"ref": badge_ref("EMORI:anna-001")})
    text = path.read_text(encoding="utf-8")

    head = original[:original.index("\nstaff:")]
    assert text.startswith(head)  # banner, course_id, the commented anonymous block
    # Marc's own comments survive his edit; Anna's went with her entry.
    assert "#module_id: <module id>" in text
    assert "A real module id" in text
    assert "anna@example.org" not in text
    data = yaml.safe_load(text)
    assert data["course_id"] == "emori-demo"
    assert data["staff"]["EMORI:marc-002"] == {"student_id": "marc-002",
                                              "email": "marc@example.org",
                                              "name": "Marc Example i Example"}


def test_other_keys_and_fields_survive_every_change(tmp_path):
    path = tmp_path / "roster.yaml"
    path.write_text(
        "# site roster\n"
        "course_id: example-course\n"
        "anonymous:\n"
        "  student_id: robot-bench-01\n"
        "  name: EMORI   # the robot itself\n"
        "site_note: kept as is\n"
        "staff:\n"
        "  \"EMORI:anna-001\": {student_id: anna-001, email: anna@example.org, name: Anna,"
        " pathway: \"Example pathway\", lang: ca}\n"
        "  EMORI:joan-002:\n"
        "    student_id: joan-002\n"
        "    email: joan@example.org   # work address\n"
        "    name: Joan\n"
        "    module_id: mod_difficult_news\n"
        "\n"
        "# trailing note about staff\n"
        "extra:\n"
        "  - one\n", encoding="utf-8")
    admin = _admin(path)

    assert admin.update({"ref": badge_ref("EMORI:anna-001"), "name": "Anna Example",
                         "email": "anna@example.org"})[0] == 200
    assert admin.update({"ref": badge_ref("EMORI:joan-002"), "name": "Joan Example",
                         "email": "joan@example.org"})[0] == 200
    assert admin.add({"name": "Pau", "email": "pau@example.org"})[0] == 200
    data = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert data["course_id"] == "example-course"
    assert data["anonymous"] == {"student_id": "robot-bench-01", "name": "EMORI"}
    assert data["site_note"] == "kept as is" and data["extra"] == ["one"]
    assert data["staff"]["EMORI:anna-001"] == {
        "student_id": "anna-001", "email": "anna@example.org", "name": "Anna Example",
        "pathway": "Example pathway", "lang": "ca"}
    assert data["staff"]["EMORI:joan-002"]["module_id"] == "mod_difficult_news"
    assert len(data["staff"]) == 3
    text = path.read_text(encoding="utf-8")
    assert "# the robot itself" in text and "# trailing note about staff" in text


def test_a_roster_with_no_staff_block_or_no_file_gets_one(tmp_path):
    bare = tmp_path / "roster.yaml"
    bare.write_text("course_id: emori-demo\n", encoding="utf-8")
    assert _admin(bare).add({"name": "Anna", "email": "anna@example.org"})[0] == 200
    data = yaml.safe_load(bare.read_text(encoding="utf-8"))
    assert data["course_id"] == "emori-demo" and len(data["staff"]) == 1

    missing = tmp_path / "new" / "roster.yaml"
    missing.parent.mkdir()
    admin = RosterAdmin(roster=Roster.default(), path=missing, scope=_scope())
    assert admin.add({"name": "Anna", "email": "anna@example.org"})[0] == 200
    assert missing.read_text(encoding="utf-8").startswith("# EMORI staff roster")


def test_the_first_write_seeds_roster_yaml_from_the_example_the_robot_read(tmp_path):
    """No roster.yaml: the hostapp READ the tracked example; writes never touch it."""

    example = _copy_sample(tmp_path)
    example = example.rename(tmp_path / "roster.example.yaml")
    before = example.read_bytes()
    target = tmp_path / "roster.yaml"
    live = Roster.from_yaml(example)
    admin = RosterAdmin(roster=live, path=target, scope=_scope())

    assert admin.list()[1]["count"] == 2  # what the robot is serving now
    assert admin.add({"name": "Pau", "email": "pau@example.org"})[0] == 200

    assert example.read_bytes() == before
    assert len(_staff(target)) == 3 and live.source == target
    assert yaml.safe_load(target.read_text(encoding="utf-8"))["course_id"] == "emori-demo"


def test_a_crlf_file_stays_crlf_and_keeps_its_comments(tmp_path):
    """A roster saved by a Windows editor: edited in place all the same."""

    path = tmp_path / "roster.yaml"
    path.write_bytes(b"# banner\r\ncourse_id: x\r\nstaff:\r\n  \"EMORI:a\":\r\n"
                     b"    student_id: a\r\n    email: a@example.org   # work\r\n    name: A\r\n")
    admin = _admin(path)
    assert admin.add({"name": "B", "email": "b@example.org"})[0] == 200
    assert admin.update({"ref": badge_ref("EMORI:a"), "name": "A2",
                         "email": "a@example.org"})[0] == 200
    raw = path.read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n")
    assert raw.startswith(b"# banner\r\ncourse_id: x\r\nstaff:\r\n")
    assert b"Rewritten" not in raw and len(_staff(path)) == 2


def test_an_unusual_layout_is_still_written_correctly():
    text = "staff: {\"EMORI:a\": {student_id: a, email: a@example.org, name: A}}\ncourse_id: x\n"

    new_text, how = edit_roster_text(text, "add", "EMORI:B", {"student_id": "b",
                                                              "email": "b@example.org", "name": "B"})

    assert how == "staff_block"
    data = yaml.safe_load(new_text)
    assert data["course_id"] == "x" and set(data["staff"]) == {"EMORI:a", "EMORI:B"}


def test_an_unreadable_roster_is_never_overwritten(tmp_path):
    path = tmp_path / "roster.yaml"
    broken = "staff:\n  \"EMORI:a\": {student_id: a\n"
    path.write_text(broken, encoding="utf-8")
    admin = RosterAdmin(roster=Roster.default(), path=path, scope=_scope())

    assert admin.add({"name": "B", "email": "b@example.org"}) == (409, {"error": "roster_unreadable"})
    assert admin.list() == (409, {"error": "roster_unreadable"})
    assert path.read_text(encoding="utf-8") == broken


def test_a_hand_edit_is_picked_up_when_the_tab_is_opened(tmp_path):
    path = _copy_sample(tmp_path)
    live = Roster.from_yaml(path)
    admin = _admin(path, roster=live)
    with path.open("a", encoding="utf-8") as fh:
        fh.write('  "EMORI:pau-003":\n    student_id: pau-003\n    email: pau@example.org\n'
                 '    name: Pau\n')

    status, body = admin.list()

    assert status == 200 and body["live"] is True and body["count"] == 3
    assert live.resolve("EMORI:pau-003").name == "Pau"


@pytest.mark.parametrize("bad", ["staff: [one, two]\n", "staff: 5\n",
                                 "anonymous: 5\nstaff: {}\n", "anonymous: [x]\nstaff: {}\n"])
def test_a_reload_of_a_misshapen_roster_keeps_the_roster_it_had(tmp_path, caplog, bad):
    """`staff: [..]` going live made every badge scan a 500 (`list.get`)."""

    path = _copy_sample(tmp_path)
    live = Roster.from_yaml(path)
    admin = _admin(path, roster=live)
    path.write_text("course_id: x\n" + bad, encoding="utf-8")

    with pytest.raises(ValueError):
        live.reload(path)
    with caplog.at_level(logging.WARNING):
        assert admin.list() == (409, {"error": "roster_unreadable"})

    assert live.resolve("EMORI:anna-001").name == "Anna Example"  # still in service
    assert live.course_id == "emori-demo" and live.staff_count == 2
    assert any("keeps the roster it had" in r.getMessage() for r in caplog.records)


def test_the_roster_file_is_always_written_owner_only(tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX permission bits")
    path = _copy_sample(tmp_path)
    os.chmod(path, 0o644)  # what install.sh's cp leaves behind

    assert _admin(path).add({"name": "Pau", "email": "pau@example.org"})[0] == 200
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    missing = tmp_path / "fresh.yaml"
    RosterAdmin(roster=Roster.default(), path=missing, scope=_scope()).add(
        {"name": "Pau", "email": "pau@example.org"})
    assert stat.S_IMODE(missing.stat().st_mode) == 0o600


def test_install_sh_tightens_the_roster_it_copies():
    install = Path("deploy/robot/install.sh").read_text(encoding="utf-8")
    copy_line = install.index("cp configs/roster.example.yaml configs/roster.yaml")

    assert "chmod 600 configs/roster.yaml" in install[copy_line:copy_line + 400]


def test_no_temp_file_is_left_behind(tmp_path):
    path = _copy_sample(tmp_path)
    _admin(path).add({"name": "Pau", "email": "pau@example.org"})

    assert sorted(p.name for p in tmp_path.iterdir()) == ["roster.yaml"]


# --------------------------------------------------------------------------- who may be added
def test_an_email_outside_the_robot_s_domains_is_refused_with_the_domains(tmp_path):
    path = _copy_sample(tmp_path)
    before = path.read_bytes()
    admin = _admin(path, scope=_scope(("example.org", "site.example.org")))

    status, body = admin.add({"name": "Pau", "email": "someone@example.net"})

    assert (status, body["error"], body["field"]) == (422, "email_not_in_scope", "email")
    assert body["domains"] == ["example.org", "site.example.org"]
    assert path.read_bytes() == before
    assert admin.add({"name": "Pau", "email": "pau@Site.Example.org"})[0] == 200
    # an edit is checked the same way
    status, body = admin.update({"ref": badge_ref("EMORI:anna-001"), "name": "Anna",
                                 "email": "anna@example.net"})
    assert (status, body["error"]) == (422, "email_not_in_scope")


def test_a_robot_with_no_staff_domain_admits_nobody(tmp_path):
    status, body = _admin(_copy_sample(tmp_path), scope=_scope(())).add(
        {"name": "Pau", "email": "pau@example.org"})

    assert (status, body["error"], body["domains"]) == (422, "email_not_in_scope", [])


@pytest.mark.parametrize("code", ["no_robot_token", "whoami_unreachable", "robot_revoked"])
def test_without_whoami_an_email_is_allowed_with_a_warning(tmp_path, code):
    admin = _admin(_copy_sample(tmp_path), scope=_scope(ok=False, code=code))

    status, body = admin.add({"name": "Pau", "email": "someone@example.com"})

    assert status == 200 and body["warning"] == "scope_unchecked"
    assert body["scope"] == {"checked": False, "code": code, "domains": []}
    pau = next(p for p in body["people"] if p["name"] == "Pau")
    assert pau["in_scope"] is None


def test_the_list_flags_people_outside_the_scope(tmp_path):
    path = _copy_sample(tmp_path)
    body = _admin(path, scope=_scope(("site.example.org",))).list()[1]

    assert {p["name"]: p["in_scope"] for p in body["people"]} == {
        "Anna Example": False, "Marc Example": False}
    assert body["site_name"] == "Demo reception" and body["robot_name"] == "EMORI"


def test_duplicates_are_refused(tmp_path):
    path = _copy_sample(tmp_path)
    admin = _admin(path)

    status, body = admin.add({"name": "Another Anna", "email": "ANNA@example.org"})
    assert (status, body) == (409, {"error": "duplicate_email", "field": "email"})
    status, body = admin.update({"ref": badge_ref("EMORI:marc-002"), "name": "Marc",
                                 "email": "anna@example.org"})
    assert (status, body["error"]) == (409, "duplicate_email")
    # keeping one's own address is not a duplicate
    assert admin.update({"ref": badge_ref("EMORI:anna-001"), "name": "Anna Example",
                         "email": "anna@example.org"})[0] == 200


@pytest.mark.parametrize("payload", [{"name": "", "email": "x@example.org"},
                                     {"name": "X", "email": "nope"},
                                     {"name": "X\x07", "email": "x@example.org"}, None, [1]])
def test_a_bad_field_is_a_400_and_writes_nothing(tmp_path, payload):
    path = _copy_sample(tmp_path)
    before = path.read_bytes()

    status, body = _admin(path).add(payload)

    assert status == 400 and body["field"] in ("name", "email")
    assert path.read_bytes() == before


@pytest.mark.parametrize("ref", [None, "", "0" * 16, "EMORI:anna-001", "x" * 16])
def test_an_unknown_ref_is_a_404(tmp_path, ref):
    admin = _admin(_copy_sample(tmp_path))

    assert admin.remove({"ref": ref}) == (404, {"error": "person_not_found"})
    assert admin.badge({"ref": ref})[:2] == (404, {"error": "person_not_found"})


def test_a_full_roster_refuses_one_more(tmp_path):
    path = _copy_sample(tmp_path)
    admin = RosterAdmin(roster=Roster.from_yaml(path), path=path, scope=_scope(),
                        max_people=2)

    assert admin.add({"name": "Pau", "email": "pau@example.org"}) == (
        409, {"error": "roster_full", "max": 2})


def test_a_removed_and_re_added_person_is_the_same_learner_with_a_new_badge(tmp_path):
    path = _copy_sample(tmp_path)
    admin = _admin(path)
    admin.add({"name": "Pau", "email": "pau@example.org"})
    first_key = _key_of(path, "pau@example.org")
    first_id = _staff(path)[first_key]["student_id"]

    admin.remove({"ref": badge_ref(first_key)})
    admin.add({"name": "Pau", "email": "Pau@example.org"})
    second_key = _key_of(path, "Pau@example.org")

    assert second_key != first_key  # the lost badge stays dead
    assert _staff(path)[second_key]["student_id"] == first_id


def test_no_name_address_or_badge_reaches_the_log(tmp_path, caplog):
    path = _copy_sample(tmp_path)
    admin = _admin(path)
    with caplog.at_level(logging.DEBUG):
        admin.add({"name": "Núria Secret", "email": "nuria.secret@example.org"})
        key = _key_of(path, "nuria.secret@example.org")
        admin.update({"ref": badge_ref(key), "name": "Núria Hidden",
                      "email": "nuria.hidden@example.org"})
        admin.badge({"ref": badge_ref(key)})
        admin.sheet({})
        admin.remove({"ref": badge_ref(key)})
        admin.add({"name": "Pau", "email": "someone@example.net"})  # refused

    text = "\n".join(r.getMessage() for r in caplog.records)
    for secret in ("Núria", "nuria", "Secret", "Hidden", "example.net", key, badge_ref(key),
                   "anna", "marc"):
        assert secret not in text
    assert "on the roster" in text  # ...but the event and the count are


# --------------------------------------------------------------------------- badges
def test_a_badge_qr_carries_exactly_what_the_scanner_resolves(tmp_path):
    """segno encodes; the payload goes through the scanner path's own gate and lookup."""

    path = _copy_sample(tmp_path)
    admin = _admin(path)
    admin.add({"name": "Pau Example", "email": "pau@example.org"})
    key = _key_of(path, "pau@example.org")

    qr = badges.make_qr(key)
    assert (qr.mode, qr.version, qr.is_micro) == ("alphanumeric", 1, False)
    assert qr.error in ("Q", "H")
    # Nothing personal in what is encoded.
    assert "pau" not in key.lower() and "example" not in key.lower()
    # The kiosk hands the decoded text to /api/identify, which gates it like this...
    scanned = _decoded_qr_payload(key + "\n")
    assert scanned == key
    # ...and resolves it against the roster the hostapp loaded.
    ident = Roster.from_yaml(path).resolve(scanned)
    assert (ident.anonymous, ident.name, ident.email) == (False, "Pau Example",
                                                          "pau@example.org")
    # Badges written by hand before this page print the same way.
    assert badges.make_qr("EMORI:anna-001").mode == "byte"


def test_the_printed_code_decodes_back_to_the_roster_key(tmp_path):
    """A real image round trip through identity.decode_qr (OpenCV), where installed."""

    pytest.importorskip("cv2")
    path = _copy_sample(tmp_path)
    admin = _admin(path)
    admin.add({"name": "Pau Example", "email": "pau@example.org"})
    key = _key_of(path, "pau@example.org")

    status, png, media_type = admin.badge({"ref": badge_ref(key), "format": "png"})

    assert (status, media_type) == (200, "image/png") and png[:8] == b"\x89PNG\r\n\x1a\n"
    decoded = decode_qr(png)
    assert decoded == key
    assert Roster.from_yaml(path).resolve(_decoded_qr_payload(decoded)).name == "Pau Example"


def test_zxing_reads_the_printed_code_too(tmp_path):
    """The robot's server-side reader (server._decode_qr_jpeg uses zxing-cpp)."""

    zxingcpp = pytest.importorskip("zxingcpp")
    image = pytest.importorskip("PIL.Image")
    import io

    png = badges.qr_png("EMORI:K7Q2M9XAT3")
    results = zxingcpp.read_barcodes(image.open(io.BytesIO(png)))
    assert [r.text for r in results] == ["EMORI:K7Q2M9XAT3"]


def test_a_badge_is_an_svg_with_the_name_escaped(tmp_path):
    path = _copy_sample(tmp_path)
    admin = _admin(path)
    admin.add({"name": "Anna <b>&amp; Co", "email": "anna.co@example.org"})
    key = _key_of(path, "anna.co@example.org")

    status, svg, media_type = admin.badge({"ref": badge_ref(key)})

    assert (status, media_type) == (200, "image/svg+xml")
    text = svg.decode("utf-8")
    assert text.startswith("<?xml") and "<svg" in text and "<script" not in text
    assert "Anna &lt;b&gt;&amp;amp; Co" in text
    root = ET.fromstring(svg)
    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    assert root.get("width") == "90mm" and root.get("height") == "88mm"
    assert "EMORI · DEMO RECEPTION" in text
    assert key not in text and "anna.co@example.org" not in text  # the code is drawn, not written


def test_a_sheet_is_a4_six_badges_a_page(tmp_path):
    path = _copy_sample(tmp_path)
    admin = _admin(path)
    for n in range(5):
        admin.add({"name": "Person %d" % n, "email": "p%d@example.org" % n})

    status, html, media_type = admin.sheet({})
    text = html.decode("utf-8")

    assert (status, media_type) == (200, "text/html; charset=utf-8")
    assert "size:A4" in text and text.count('class="page"') == 2
    assert text.count('role="img"') == 7  # one badge each (its code is a nested <svg>)
    assert "http://" not in text.replace("http://www.w3.org/2000/svg", "")
    assert "https://" not in text  # offline: no CDN, no font, nothing to fetch

    one = admin.sheet({"refs": [badge_ref("EMORI:anna-001")]})[1].decode("utf-8")
    assert one.count('role="img"') == 1 and "Anna Example" in one
    assert admin.sheet({"refs": "all"})[:2] == (400, {"error": "bad_refs"})


def test_an_entry_whose_badge_cannot_work_is_never_printed(tmp_path):
    """The list marks it valid: False and does not count it; print never draws it."""

    path = _copy_sample(tmp_path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write('  "EMORI:broken-1":\n    email: broken@example.org\n    name: No Student Id\n'
                 '  "EMORI:broken-2":\n    student_id: " padded "\n    email: b2@example.org\n'
                 '    name: Padded Id\n')
    admin = _admin(path)
    people = admin.list()[1]["people"]
    broken = [p for p in people if not p["valid"]]

    assert {p["name"] for p in broken} == {"No Student Id", "Padded Id"}
    every = admin.sheet({})[1].decode("utf-8")
    assert every.count('role="img"') == 2  # Anna and Marc, the "Print all (2)" count
    assert "No Student Id" not in every and "Padded Id" not in every
    chosen = admin.sheet({"refs": [p["ref"] for p in broken]})[1].decode("utf-8")
    assert 'role="img"' not in chosen
    for p in broken:
        assert admin.badge({"ref": p["ref"]})[:2] == (404, {"error": "person_not_found"})


def test_without_segno_badges_say_so_and_the_roster_still_works(tmp_path, monkeypatch):
    monkeypatch.setattr(badges, "available", lambda: False)
    path = _copy_sample(tmp_path)
    admin = _admin(path)

    assert admin.badge({"ref": badge_ref("EMORI:anna-001")})[:2] == (
        409, {"error": "qr_unavailable"})
    assert admin.sheet({})[:2] == (409, {"error": "qr_unavailable"})
    status, body = admin.add({"name": "Pau", "email": "pau@example.org"})
    assert status == 200 and body["qr_available"] is False


def test_badge_payloads_are_random_and_never_reused():
    taken = {"EMORI:AAAAAAAAAA"}
    payloads = {roster_admin.new_badge_payload(taken) for _ in range(200)}

    assert len(payloads) == 200 and not payloads & taken
    assert all(PAYLOAD_RE.fullmatch(p) for p in payloads)


# --------------------------------------------------------------------------- the shipped example
def test_the_shipped_example_lists_nobody_and_the_first_person_keeps_its_comments(tmp_path):
    """install.sh copies the example onto every robot: it serves nobody until people are
    added, and the first add edits it in place (its explanations stay)."""

    path = _copy_example(tmp_path)
    original = path.read_text(encoding="utf-8")
    live = Roster.from_yaml(path)
    admin = _admin(path, roster=live)

    status, body = admin.list()
    assert (status, body["count"], body["people"]) == (200, 0, [])

    status, body = admin.add({"name": "Pau Example", "email": "pau@example.org"})

    assert status == 200 and body["count"] == 1
    text = path.read_text(encoding="utf-8")
    assert text.startswith(original[:original.index("\nstaff:")])
    assert "ROSTER NOTES: READ BEFORE ADDING A COURSE OR PATHWAY" in text
    assert '#  "EMORI:anna-001":' in text
    assert "Rewritten" not in text
    key = _key_of(path, "pau@example.org")
    assert live.resolve(key).name == "Pau Example"


# --------------------------------------------------------------------------- pathway + course
def test_the_listing_carries_the_pathways_and_the_course(tmp_path):
    body = _admin(_copy_sample(tmp_path),
                  scope=_scope(pathways=("Example pathway", "Second pathway"))).list()[1]

    assert body["pathways"] == ["Example pathway", "Second pathway"]
    assert body["course_id"] == "emori-demo"           # the roster's own wins
    assert {p["name"]: p["pathway"] for p in body["people"]} == {
        "Anna Example": None, "Marc Example": None}


def test_a_roster_without_a_course_reports_the_robots(tmp_path, monkeypatch):
    path = tmp_path / "roster.yaml"
    path.write_text("staff: {}\n", encoding="utf-8")

    assert _admin(path).list()[1]["course_id"] == "emori"
    monkeypatch.setenv("EMORI_COURSE_ID", "course-from-the-token")
    assert _admin(path).list()[1]["course_id"] == "course-from-the-token"


def test_a_pathway_in_scope_is_saved_and_reaches_the_badge(tmp_path):
    path = _copy_sample(tmp_path)
    live = Roster.from_yaml(path)
    admin = _admin(path, scope=_scope(pathways=("Example pathway", "Second pathway")),
                   roster=live)

    status, body = admin.add({"name": "Pau", "email": "pau@example.org",
                              "pathway": " Second pathway "})

    assert status == 200
    key = _key_of(path, "pau@example.org")
    assert _staff(path)[key]["pathway"] == "Second pathway"
    assert next(p for p in body["people"] if p["name"] == "Pau")["pathway"] == "Second pathway"
    assert live.resolve(key).pathway == "Second pathway"


def test_a_pathway_outside_the_scope_is_refused_and_writes_nothing(tmp_path):
    path = _copy_sample(tmp_path)
    before = path.read_bytes()
    admin = _admin(path, scope=_scope(pathways=("Example pathway",)))

    status, body = admin.add({"name": "Pau", "email": "pau@example.org",
                              "pathway": "example pathway"})      # exact match only

    assert (status, body["error"], body["field"]) == (422, "pathway_not_in_scope", "pathway")
    assert body["pathways"] == ["Example pathway"]
    assert path.read_bytes() == before
    status, body = admin.update({"ref": badge_ref("EMORI:anna-001"), "name": "Anna Example",
                                 "email": "anna@example.org", "pathway": "Elsewhere"})
    assert (status, body["error"]) == (422, "pathway_not_in_scope")
    assert path.read_bytes() == before


def test_an_edit_sets_keeps_and_removes_the_pathway_in_place(tmp_path):
    path = _copy_sample(tmp_path)
    admin = _admin(path, scope=_scope(pathways=("Example pathway",)))
    ref = badge_ref("EMORI:marc-002")

    assert admin.update({"ref": ref, "name": "Marc Example", "email": "marc@example.org",
                         "pathway": "Example pathway"})[0] == 200
    assert _staff(path)["EMORI:marc-002"]["pathway"] == "Example pathway"
    # No pathway in the edit: the one it has is kept.
    assert admin.update({"ref": ref, "name": "Marc E.", "email": "marc@example.org"})[0] == 200
    assert _staff(path)["EMORI:marc-002"]["pathway"] == "Example pathway"
    # An empty one removes it.
    assert admin.update({"ref": ref, "name": "Marc E.", "email": "marc@example.org",
                         "pathway": ""})[0] == 200
    assert "pathway" not in _staff(path)["EMORI:marc-002"]
    text = path.read_text(encoding="utf-8")
    # Every change was a line edit: Marc's own comments are still there.
    assert "#module_id: <module id>" in text and "A real module id" in text
    assert "Rewritten" not in text
    assert _staff(path)["EMORI:marc-002"] == {"student_id": "marc-002", "email":
                                              "marc@example.org", "name": "Marc E."}


def test_the_allowlist_narrows_the_pathways_on_offer(tmp_path, monkeypatch):
    """The badge scanner drops a pathway outside EMORI_PATHWAYS: never offer one."""

    monkeypatch.setenv("EMORI_PATHWAYS", "Second pathway")
    path = _copy_sample(tmp_path)
    admin = _admin(path, scope=_scope(pathways=("Example pathway", "Second pathway")))

    assert admin.list()[1]["pathways"] == ["Second pathway"]
    assert admin.add({"name": "Pau", "email": "pau@example.org",
                      "pathway": "Example pathway"})[1]["error"] == "pathway_not_in_scope"


def test_without_whoami_the_allowlist_is_the_pathways_on_offer(tmp_path, monkeypatch):
    monkeypatch.setenv("EMORI_PATHWAYS", "Example pathway")
    admin = _admin(_copy_sample(tmp_path), scope=_scope(ok=False, code="whoami_unreachable"))

    assert admin.list()[1]["pathways"] == ["Example pathway"]
    status, body = admin.add({"name": "Pau", "email": "pau@example.org",
                              "pathway": "Another"})
    assert (status, body["error"]) == (422, "pathway_not_in_scope")
    status, body = admin.add({"name": "Pau", "email": "pau@example.org",
                              "pathway": "Example pathway"})
    assert status == 200 and body["warning"] == "scope_unchecked"


@pytest.mark.parametrize("scope", [_scope(), _scope(ok=False, code="no_robot_token")],
                         ids=["record-lists-none", "no-whoami"])
def test_with_no_pathway_known_any_pathway_is_accepted(tmp_path, scope):
    path = _copy_sample(tmp_path)
    admin = _admin(path, scope=scope)

    assert admin.list()[1]["pathways"] == []
    assert admin.add({"name": "Pau", "email": "pau@example.org",
                      "pathway": "Whatever pathway"})[0] == 200


@pytest.mark.parametrize("pathway", [7, ["Example pathway"], "Example\npathway",
                                     "Example\u202epathway", "x" * 501])
def test_a_bad_pathway_is_a_400_and_writes_nothing(tmp_path, pathway):
    path = _copy_sample(tmp_path)
    before = path.read_bytes()

    status, body = _admin(path).add({"name": "Pau", "email": "pau@example.org",
                                     "pathway": pathway})

    assert (status, body) == (400, {"error": "pathway_invalid", "field": "pathway"})
    assert path.read_bytes() == before


def test_a_null_or_empty_pathway_on_add_is_no_pathway(tmp_path):
    path = _copy_sample(tmp_path)
    admin = _admin(path, scope=_scope(pathways=("Example pathway",)))

    assert admin.add({"name": "Pau", "email": "pau@example.org", "pathway": None})[0] == 200
    assert admin.add({"name": "Pol", "email": "pol@example.org", "pathway": "  "})[0] == 200
    assert "pathway" not in _staff(path)[_key_of(path, "pau@example.org")]
    assert "pathway" not in _staff(path)[_key_of(path, "pol@example.org")]


def test_the_pathway_never_reaches_the_log(tmp_path, caplog):
    path = _copy_sample(tmp_path)
    admin = _admin(path, scope=_scope(pathways=("Zebra pathway",)))
    with caplog.at_level(logging.DEBUG):
        admin.add({"name": "Pau", "email": "pau@example.org", "pathway": "Zebra pathway"})
        admin.add({"name": "Pol", "email": "pol@example.org", "pathway": "Other zebra"})

    assert "ebra" not in "\n".join(r.getMessage() for r in caplog.records)


def test_edit_roster_text_removes_a_field_named_with_none():
    text = ("staff:\n  \"EMORI:a\":\n    student_id: a\n    email: a@example.org\n"
            "    pathway: \"Old\"   # set by hand\n    name: A\n")

    new_text, how = edit_roster_text(text, "update", "EMORI:a",
                                      {"name": "A", "email": "a@example.org",
                                       "pathway": None})

    assert how == "in_place"
    assert yaml.safe_load(new_text)["staff"]["EMORI:a"] == {
        "student_id": "a", "email": "a@example.org", "name": "A"}
