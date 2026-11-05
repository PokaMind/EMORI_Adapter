"""Tests for QR-badge identity resolution + the host-app /api/identify endpoint."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from emori_adapter.hostapp import server as server_mod
from emori_adapter.hostapp.extensions import HostExtension
from emori_adapter.hostapp.identity import (
    DEFAULT_COURSE_ID,
    DEFAULT_ROBOT_NAME,
    Roster,
    effective_course_id,
    robot_identity,
    roster_course_id,
)
from emori_adapter.hostapp.server import _build_app

# The roster the repository SHIPS: nobody on it (people are added on the admin page).
ROSTER = "configs/roster.example.yaml"

# A populated roster for the tests that scan a known badge. Written per test (never the
# tracked example, which ships no people).
SAMPLE_ROSTER = """\
# EMORI staff roster: test sample.
course_id: emori-demo

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
    # A real module id: drops straight into one scenario (no picker).
    #module_id: <module id>
"""


def _sample(tmp_path) -> str:
    path = tmp_path / "roster.yaml"
    path.write_text(SAMPLE_ROSTER, encoding="utf-8")
    return str(path)


def test_roster_resolves_known_staff(tmp_path):
    r = Roster.from_yaml(_sample(tmp_path))
    anna = r.resolve("EMORI:anna-001")
    assert anna.anonymous is False
    assert anna.email == "anna@example.org"
    marc = r.resolve("EMORI:marc-002")
    assert marc.student_id == "marc-002"


def test_shipped_roster_example_pins_no_pathway_or_module(tmp_path):
    """install.sh copies this file onto every robot, so a PLACEHOLDER here ships.

    A placeholder such as `pathway: reception-deescalation` matches nothing: Pokamind
    compares the module category with the pathway exactly (accents too), so the module list
    comes back empty, the session never starts, and the robot stands there silent with
    its antennas open. Both fields are deployment data that only the site knows, so the
    example must ship them commented out.
    """
    data = yaml.safe_load(Path(ROSTER).read_text(encoding="utf-8")) or {}

    for badge, record in (data.get("staff") or {}).items():
        assert "pathway" not in record, badge
        assert "module_id" not in record, badge
    assert ("ROSTER NOTES: READ BEFORE ADDING A COURSE OR PATHWAY"
            in Path(ROSTER).read_text(encoding="utf-8"))


def test_install_warns_when_the_roster_was_never_edited():
    """The example is COPIED onto every robot, so "unedited" is a shipping state.

    Byte-identical to the example is the one check that cannot produce a false alarm.
    """
    install = Path("deploy/robot/install.sh").read_text(encoding="utf-8")

    assert "cmp -s configs/roster.yaml configs/roster.example.yaml" in install
    assert "roster-unedited.marker" in install


def test_roster_unknown_and_empty_are_anonymous(monkeypatch):
    monkeypatch.setenv("EMORI_ROBOT_ID", "lobby-01")
    r = Roster.from_yaml(ROSTER)
    assert r.resolve("EMORI:nobody").anonymous is True
    assert r.resolve("").anonymous is True
    assert r.resolve(None).anonymous is True
    # The shipped example carries no anonymous block, so the identity is THIS robot's.
    assert r.resolve("").student_id == "robot-lobby-01"


def test_default_roster_only_anonymous():
    r = Roster.default()
    assert r.resolve("anything").anonymous is True


# --- the anonymous identity is the robot's OWN Pokamind account -----------------------
# One shared drop-in account per ROBOT, not per fleet: two robots answering to the same
# student_id interleave their sessions, weekly caps and analysis emails into one
# Pokamind learner. The robot id (its token's; EMORI_ROBOT_ID on a bench without one) is
# what makes it unique; EMORI_ROBOT_NAME is what the learner is called in the report.

def _roster_without_anonymous(tmp_path) -> str:
    p = tmp_path / "roster.yaml"
    p.write_text(
        "course_id: emori-demo\n"
        "staff:\n"
        '  "EMORI:anna-001": {student_id: anna-001, email: a@example.org, name: Anna}\n',
        encoding="utf-8",
    )
    return str(p)


def test_anonymous_identity_is_derived_from_this_robots_id(monkeypatch, tmp_path):
    monkeypatch.setenv("EMORI_ROBOT_ID", "reachy-frontdesk-01")
    monkeypatch.setenv("EMORI_ROBOT_NAME", "Emori Recepció")

    identity = Roster.from_yaml(_roster_without_anonymous(tmp_path)).resolve("")

    assert identity.anonymous is True
    assert identity.student_id == "robot-reachy-frontdesk-01"
    assert identity.email == "robot-reachy-frontdesk-01@emori.local"
    assert identity.name == "Emori Recepció"
    assert identity.course_id == "emori-demo"


def test_anonymous_display_name_defaults_to_emori(monkeypatch, tmp_path):
    monkeypatch.setenv("EMORI_ROBOT_ID", "reachy-frontdesk-01")
    monkeypatch.delenv("EMORI_ROBOT_NAME", raising=False)

    identity = Roster.from_yaml(_roster_without_anonymous(tmp_path)).resolve("")

    assert identity.name == DEFAULT_ROBOT_NAME == "EMORI"


def test_default_roster_carries_the_same_robot_account(monkeypatch):
    """`Roster.default()` (no roster file at all) must still be THIS robot."""
    monkeypatch.setenv("EMORI_ROBOT_ID", "reachy-frontdesk-02")

    identity = Roster.default().resolve("")

    assert identity.student_id == "robot-reachy-frontdesk-02"
    assert identity.email == "robot-reachy-frontdesk-02@emori.local"


def test_an_explicit_anonymous_roster_entry_still_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("EMORI_ROBOT_ID", "reachy-frontdesk-01")
    monkeypatch.setenv("EMORI_ROBOT_NAME", "Emori")
    p = tmp_path / "roster.yaml"
    p.write_text(
        "course_id: emori-demo\n"
        "anonymous:\n"
        "  student_id: robot-lobby-01\n"
        "  email: reception@example.org\n"
        "  name: Demo reception\n"
        "staff: {}\n",
        encoding="utf-8",
    )

    identity = Roster.from_yaml(str(p)).resolve("")

    assert identity.student_id == "robot-lobby-01"
    assert identity.email == "reception@example.org"
    assert identity.name == "Demo reception"


def test_a_partial_anonymous_entry_keeps_its_id_and_derives_the_rest(monkeypatch, tmp_path):
    """An operator who pins only the id must not get another robot's mailbox."""
    monkeypatch.setenv("EMORI_ROBOT_ID", "reachy-frontdesk-01")
    monkeypatch.setenv("EMORI_ROBOT_NAME", "Emori")
    p = tmp_path / "roster.yaml"
    p.write_text("anonymous: {student_id: robot-lobby-01}\nstaff: {}\n", encoding="utf-8")

    identity = Roster.from_yaml(str(p)).resolve("")

    assert identity.student_id == "robot-lobby-01"
    assert identity.email == "robot-lobby-01@emori.local"   # from ITS id, not the robot's
    assert identity.name == "Emori"


def test_robot_id_becomes_a_usable_email_local_part(monkeypatch):
    """A deployment id with spaces/case/slashes must not mint an unroutable address."""
    monkeypatch.setenv("EMORI_ROBOT_ID", " Reachy Mini/Lobby 01 ")
    monkeypatch.delenv("EMORI_ROBOT_NAME", raising=False)

    account = robot_identity()

    assert account["student_id"] == "robot-reachy-mini-lobby-01"
    assert account["email"] == "robot-reachy-mini-lobby-01@emori.local"


def test_unset_robot_id_keeps_the_legacy_shared_account(monkeypatch, tmp_path):
    """Unconfigured must stay recognisable (and flagged), never a forged unique id."""
    monkeypatch.delenv("EMORI_ROBOT_ID", raising=False)
    monkeypatch.delenv("EMORI_ROBOT_NAME", raising=False)

    identity = Roster.from_yaml(_roster_without_anonymous(tmp_path)).resolve("")

    assert identity.student_id == "robot-anonymous"
    assert identity.email == "robot-anonymous@emori.local"


def test_env_example_carries_exactly_one_robot_id_assignment():
    """`.env` is read top to bottom: a second EMORI_ROBOT_ID= silently wins.

    The variable is also the robot's Pokamind
    account, so a leftover empty assignment further down would put the whole fleet back
    on `robot-anonymous` while the top of the file says otherwise.
    """
    text = Path(".env.example").read_text(encoding="utf-8")
    assigned = [
        line for line in text.splitlines()
        if not line.lstrip().startswith("#") and line.split("=", 1)[0].strip() == "EMORI_ROBOT_ID"
    ]

    assert len(assigned) == 1, assigned
    # The display name is issued with the token now: the template leaves it empty, and
    # empty means the name Pokamind issued, else DEFAULT_ROBOT_NAME.
    names = [line for line in text.splitlines()
             if not line.lstrip().startswith("#")
             and line.split("=", 1)[0].strip() == "EMORI_ROBOT_NAME"]
    assert names == ["EMORI_ROBOT_NAME="], names
    assert "NOWHERE ELSE" in text          # the uniqueness requirement is stated


def test_an_unconfigured_robot_says_so_in_the_log(monkeypatch, tmp_path, caplog):
    """On a screenless appliance this WARNING is the only signal that the fleet is
    about to share one learner."""
    monkeypatch.delenv("EMORI_ROBOT_ID", raising=False)

    with caplog.at_level(logging.WARNING, logger="emori.identity"):
        Roster.from_yaml(_roster_without_anonymous(tmp_path)).resolve("")

    assert "EMORI_ROBOT_ID is unset" in caplog.text


def test_pinning_an_anonymous_id_does_not_warn_about_an_unused_robot_id(
    monkeypatch, tmp_path, caplog
):
    """A deployment that deliberately shares a per-room account is not misconfigured."""
    monkeypatch.delenv("EMORI_ROBOT_ID", raising=False)
    p = tmp_path / "roster.yaml"
    p.write_text("anonymous: {student_id: robot-room-3, email: r3@example.org}\nstaff: {}\n",
                 encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="emori.identity"):
        identity = Roster.from_yaml(str(p)).resolve("")

    assert identity.student_id == "robot-room-3"
    assert "EMORI_ROBOT_ID" not in caplog.text


def test_shipped_roster_example_ships_no_shared_anonymous_account():
    """deploy/robot/install.sh copies this file to configs/roster.yaml on EVERY robot.

    An anonymous block in it would give the whole fleet one Pokamind account.
    """
    text = Path(ROSTER).read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}

    assert "anonymous" not in data
    assert "EMORI_ROBOT_ID" in text and "EMORI_ROBOT_NAME" in text


def test_malformed_staff_record_never_promotes_raw_badge_to_session_identity():
    r = Roster(
        staff={"opaque-badge": {"email": "staff@example.org"}},
        anonymous={"student_id": "robot-anonymous", "email": "robot@example.org"},
    )

    identity = r.resolve("opaque-badge")

    assert identity.anonymous is True
    assert identity.student_id == "robot-anonymous"
    assert identity.student_id != "opaque-badge"


# --- per-badge language (a bilingual site on ONE robot) -------------------------------

def _roster_with_langs(tmp_path) -> str:
    """A roster where one badge pins English on an otherwise Catalan kiosk."""
    p = tmp_path / "roster.yaml"
    p.write_text(
        "course_id: emori-demo\n"
        "anonymous: {student_id: robot-1, email: robot-1@emori.local}\n"
        "staff:\n"
        '  "EMORI:anna-001": {student_id: anna-001, email: a@example.org, name: Anna}\n'
        '  "EMORI:john-002": {student_id: john-002, email: j@example.org, name: John, lang: en}\n'
        '  "EMORI:zoe-003": {student_id: zoe-003, email: z@example.org, name: Zoe, lang: " EN "}\n'
        '  "EMORI:bad-004": {student_id: bad-004, email: b@example.org, name: Bad, lang: klingon}\n',
        encoding="utf-8",
    )
    return str(p)


def test_roster_lang_is_optional_and_normalised(tmp_path):
    r = Roster.from_yaml(_roster_with_langs(tmp_path))
    assert r.resolve("EMORI:anna-001").lang is None        # absent -> the kiosk default
    assert r.resolve("EMORI:john-002").lang == "en"
    assert r.resolve("EMORI:zoe-003").lang == "en"         # " EN " -> en
    # Still parsed and normalised onto Identity.lang for backward roster
    # compatibility -- but no longer consulted anywhere (session language is
    # ALWAYS lang_state.current()), so an unknown value has nothing
    # left to disable either way; it just falls back to None like an absent one.
    assert r.resolve("EMORI:bad-004").lang is None
    assert r.resolve("").lang is None                      # anonymous drop-in


def _roster_pinning_both_langs(tmp_path) -> str:
    """Two badges pinning OPPOSITE languages -- so a test can prove neither value
    reaches the SSO claim whichever way it disagrees with the current kiosk switch."""
    p = tmp_path / "roster.yaml"
    p.write_text(
        "course_id: emori-demo\n"
        "anonymous: {student_id: robot-1, email: robot-1@emori.local}\n"
        "staff:\n"
        '  "EMORI:john-002": {student_id: john-002, email: j@example.org, name: John, lang: en}\n'
        '  "EMORI:marta-003": {student_id: marta-003, email: m@example.org, name: Marta, lang: ca}\n',
        encoding="utf-8",
    )
    return str(p)


def test_global_lang_state_always_wins_over_the_badge_roster_language(monkeypatch, tmp_path):
    """The admin-set GLOBAL LangState is the ONLY language
    authority on the robot. A per-badge roster ``lang`` is still parsed (see
    identity._roster_lang -- kept for backward roster compatibility) but must NEVER
    reach the ``emori_lang`` SSO claim or the /session page it redirects to, in
    EITHER direction, whether the kiosk is at its boot default or has been switched
    at runtime.
    """

    from tests.test_robot_token_hostapp import sso_claims, use_golden_token

    monkeypatch.setenv("EMORI_ROSTER", _roster_pinning_both_langs(tmp_path))
    monkeypatch.setattr(server_mod, "EMORI_LANG", "ca")
    use_golden_token(monkeypatch, server_mod)
    monkeypatch.setattr(server_mod, "DEMO", False)
    app = server_mod._build_app(
        {"extension": HostExtension(),
         "idle": False, "watchdog": False, "gesture": False})
    client = TestClient(app)

    def _session(badge: str) -> tuple[dict, str]:
        # One scan per badge: the QR handoff refuses a second claim of the same card.
        body = client.post("/api/identify", json={"qr": badge}).json()
        return sso_claims(body["session_url"]), body["session_url"]

    # Kiosk boots ca; john's roster pins "en" -- the OLD contract gave "en" here.
    john, john_url = _session("EMORI:john-002")
    assert john["emori_lang"] == "ca"
    assert 'const LANG="ca"' in client.get(john_url).text
    # One badge at a time: john's session ends before marta's card is welcome.
    with app.state.driver._lock:
        app.state.driver._finalize_locked(None)

    # Switch the kiosk at runtime; marta's roster pins "ca" -- the OLD contract
    # would give "ca" regardless of the switch. The claim must follow the switch,
    # not the badge, so the spoken end-of-session summary matches the voice the
    # kiosk is actually using.
    app.state.lang_state.set("en")
    marta, marta_url = _session("EMORI:marta-003")
    assert marta["emori_lang"] == "en"
    assert 'const LANG="en"' in client.get(marta_url).text


# --- the pathway allowlist (a badge must fail OPEN, never silently out of service) ----

def _roster_with_pathway(tmp_path, pathway: str) -> str:
    p = tmp_path / "roster.yaml"
    p.write_text(
        "course_id: emori-demo\n"
        "staff:\n"
        f'  "EMORI:anna-001": {{student_id: anna-001, email: a@example.org, name: Anna, '
        f'pathway: "{pathway}"}}\n',
        encoding="utf-8",
    )
    return str(p)


def test_pathway_is_forwarded_when_no_allowlist_is_configured(monkeypatch, tmp_path):
    """A site that has not listed its categories keeps whatever the roster says."""
    monkeypatch.delenv("EMORI_PATHWAYS", raising=False)

    identity = Roster.from_yaml(
        _roster_with_pathway(tmp_path, "Example pathway")).resolve("EMORI:anna-001")

    assert identity.pathway == "Example pathway"


def test_an_allowlisted_pathway_survives_exactly_as_written(monkeypatch, tmp_path):
    """Accents and case are load-bearing: Pokamind compares the category with the pathway exactly."""
    monkeypatch.setenv("EMORI_PATHWAYS",
                       "Example pathway, Second pathway")

    identity = Roster.from_yaml(
        _roster_with_pathway(tmp_path, "Example pathway")).resolve("EMORI:anna-001")

    assert identity.pathway == "Example pathway"


def test_an_unlisted_pathway_is_dropped_and_warned_about(monkeypatch, tmp_path, caplog):
    """The wedge: an unmatched pathway filters the module list to ZERO.

    Dropping the claim puts the badge back on the anonymous path (every module offered)
    instead of taking it out of service, and the WARNING is the only place a screenless
    robot can say which roster line did it.
    """
    monkeypatch.setenv("EMORI_PATHWAYS", "Example pathway")

    with caplog.at_level(logging.WARNING, logger="emori.identity"):
        identity = Roster.from_yaml(
            _roster_with_pathway(tmp_path, "reception-deescalation")).resolve("EMORI:anna-001")

    assert identity.pathway is None
    assert identity.anonymous is False          # still THIS person's session
    # It names the FIELD and the allowlist it was checked against -- which is what
    # the operator, roster file in hand, needs to find the line -- and not the
    # value, which they wrote.
    line = next(m for m in caplog.messages if "staff roster field 'pathway'" in m)
    assert "reception-deescalation" not in line
    assert "1 EMORI_PATHWAYS" in line
    # The allowlist is a count, not a dump: the operator has EMORI_PATHWAYS in
    # the same .env, and the tuple must not push the value it is about off the end.
    assert "Example pathway" not in line


def _pathway_warning(monkeypatch, tmp_path, caplog, pathway: str) -> str:
    monkeypatch.setenv("EMORI_PATHWAYS", "Example pathway")
    with caplog.at_level(logging.WARNING, logger="emori.identity"):
        Roster.from_yaml(
            _roster_with_pathway(tmp_path, pathway)).resolve("EMORI:anna-001")
    return next(m for m in caplog.messages if "staff roster field 'pathway'" in m)


def test_the_dropped_pathway_value_never_reaches_the_journal_at_all(
        monkeypatch, tmp_path, caplog):
    """A length cap is not a privacy bound, which is why there is no longer one.

    This line fires on a MISCONFIGURED roster, beside a badge scan, into a journal
    that is world-readable and survives reboots -- and the misconfiguration it
    fires on is a roster that put something unexpected in `pathway`. The
    unexpected thing that costs the most is an identity, and identities are
    SHORT: `Anna Example <anna@example.org>` is 31 characters, so the old 48-char
    bound let a whole one through while claiming it could not.
    """

    monkeypatch.setenv("EMORI_DASH_SALT", "test-salt")
    stray = "Anna Example <anna@example.org>"

    line = _pathway_warning(monkeypatch, tmp_path, caplog, stray)

    assert stray not in line
    assert "Anna" not in line and "anna@" not in line and "example.org" not in line
    # ...nor any leading fragment of it: truncation is not the mechanism here.
    assert not any(stray[:n] in line for n in range(4, len(stray) + 1))


def test_identical_roster_mistakes_group_and_different_ones_do_not(
        monkeypatch, tmp_path, caplog):
    """What the code buys: a hundred badges carrying ONE bad pathway read as one
    problem in the journal, and two bad pathways read as two -- without the
    journal holding either value."""

    monkeypatch.setenv("EMORI_DASH_SALT", "test-salt")

    first = _pathway_warning(monkeypatch, tmp_path, caplog, "reception-deescalation")
    caplog.clear()
    again = _pathway_warning(monkeypatch, tmp_path, caplog, "reception-deescalation")
    caplog.clear()
    other = _pathway_warning(monkeypatch, tmp_path, caplog, "Anna Example")

    code = re.search(r"code (V-[0-9a-f]{6})", first)
    assert code is not None, first
    assert code.group(1) in again
    assert code.group(1) not in other
    # A person code and a value code must never be mistaken for one another.
    assert not code.group(1).startswith("S-")


def test_badge_claim_sample_carries_claims_and_no_identity(monkeypatch, tmp_path):
    """The readycheck badge probe reads this; a name or e-mail here would leak."""
    monkeypatch.delenv("EMORI_PATHWAYS", raising=False)
    monkeypatch.delenv("EMORI_COURSE_ID", raising=False)

    course_id, pathway = Roster.from_yaml(
        _roster_with_pathway(tmp_path, "Example pathway")).badge_claim_sample()

    assert (course_id, pathway) == ("emori-demo", "Example pathway")
    assert Roster.default().badge_claim_sample() == ("emori", None)


def test_badge_claim_sample_finds_a_pathway_the_first_record_does_not_carry(tmp_path,
                                                                            monkeypatch):
    """Otherwise the gate SKIPs on a roster that still pins a dead pathway.

    Reading only the first record made the probe report ``no_roster_pathway`` -- the
    same blind spot ("14/14 PASS says nothing about badges") the gate exists to close.
    """
    monkeypatch.delenv("EMORI_PATHWAYS", raising=False)
    path = tmp_path / "roster.yaml"
    path.write_text(
        "course_id: emori-demo\n"
        "staff:\n"
        '  "EMORI:bea-002": {student_id: bea-002, email: b@example.org, name: Bea}\n'
        '  "EMORI:anna-001": {student_id: anna-001, email: a@example.org, name: Anna, '
        'pathway: "Example pathway"}\n',
        encoding="utf-8",
    )

    assert Roster.from_yaml(str(path)).badge_claim_sample() == (
        "emori-demo", "Example pathway")


def test_identify_endpoint_known_and_anonymous(monkeypatch, tmp_path):
    monkeypatch.setenv("EMORI_ROSTER", _sample(tmp_path))
    client = TestClient(_build_app())
    known = client.post("/api/identify", json={"qr": "EMORI:anna-001"}).json()
    assert known["anonymous"] is False
    assert "session_url" in known
    anon = client.post("/api/identify", json={"qr": ""}).json()
    assert anon["anonymous"] is True
    assert "session_url" in anon


def test_kiosk_pages_render():
    client = TestClient(_build_app())
    landing = client.get("/").text                            # QR-triggered verbal landing
    assert "Show your badge to begin" in landing
    # The verbal menu is injected. The course itself is deployment data (the seeder
    # rewrites configs/voice_topics*.yaml), so assert against the menu that is loaded.
    menu = server_mod._load_menu(server_mod.EMORI_LANG)
    assert menu.options["1"].name in landing
    assert "practice anonymously" in landing
    assert "Show your staff badge" in client.get("/identify").text  # legacy QR badge page
    assert client.get("/manual").status_code == 200           # typed fallback


# --- Regression: qr_handoff.release() must never run bare on the ---------------------
# request's event-loop thread. _QrPriorityHandoff.reserve/claim hold a plain
# threading.RLock across an add-on's claim_badge, which can now block the
# calling thread for up to _QR_DISPATCH_WAIT_S (2s) waiting on controller
# quiescence. release() takes that same lock. If a handler ever calls
# release() bare (not via asyncio.to_thread), a to-thread worker parked
# inside that wait would freeze the whole FastAPI event loop -- every other
# in-flight request, the feature poll, the SSE relay, /api/pending-cmd --
# for up to two seconds.
#
# The spy below asserts release() never observes a *running* event loop in
# its own call thread. A bare call executes inline inside the async handler,
# on the loop's own thread, where asyncio.get_running_loop() succeeds. A
# correctly to-thread-wrapped call executes on a plain executor thread that
# never has a running loop, where asyncio.get_running_loop() raises
# RuntimeError. This distinguishes "on-loop" from "off-loop" execution
# directly, without relying on timing.

class _RefusingExtension(HostExtension):
    """A double whose scan always wins the QR preemption but whose training
    bind always fails, forcing the handler down its ``qr_handoff.release()``
    path deterministically."""

    def bind_session(self, session_id: str) -> bool:
        return False


def _watch_release_thread(monkeypatch) -> list[bool]:
    """Patch _QrPriorityHandoff.release to record, per call, whether it ran
    with a running event loop in its own thread (True = bare/on-loop,
    False = off-loop, e.g. via asyncio.to_thread)."""

    saw_running_loop: list[bool] = []
    original_release = server_mod._QrPriorityHandoff.release

    def spy_release(self, payload):
        try:
            asyncio.get_running_loop()
            saw_running_loop.append(True)
        except RuntimeError:
            saw_running_loop.append(False)
        return original_release(self, payload)

    monkeypatch.setattr(server_mod._QrPriorityHandoff, "release", spy_release)
    return saw_running_loop


def test_identify_release_after_failed_bind_runs_off_the_event_loop(monkeypatch, tmp_path):
    monkeypatch.setenv("EMORI_ROSTER", _sample(tmp_path))
    saw_running_loop = _watch_release_thread(monkeypatch)

    client = TestClient(server_mod._build_app({"extension": _RefusingExtension()}))
    resp = client.post("/api/identify", json={"qr": "EMORI:anna-001"})

    assert resp.status_code == 409
    assert resp.json() == {"error_code": "training_unavailable"}
    assert saw_running_loop == [False], (
        "qr_handoff.release() observed a running event loop in its own call "
        "thread -- it was invoked bare on the event loop instead of via "
        "asyncio.to_thread (server.py identify handler)"
    )


def test_voice_select_release_after_failed_bind_runs_off_the_event_loop(monkeypatch,
                                                                          tmp_path):
    monkeypatch.setenv("EMORI_ROSTER", _sample(tmp_path))
    saw_running_loop = _watch_release_thread(monkeypatch)

    client = TestClient(server_mod._build_app({"extension": _RefusingExtension()}))
    resp = client.post(
        "/api/voice-select",
        json={"text": "number two please", "qr": "EMORI:anna-001"},
    )

    assert resp.status_code == 409
    assert resp.json() == {"error_code": "training_unavailable"}
    assert saw_running_loop == [False], (
        "qr_handoff.release() observed a running event loop in its own call "
        "thread -- it was invoked bare on the event loop instead of via "
        "asyncio.to_thread (server.py voice_select handler)"
    )


_GOLDEN_TOKEN = "pkr1.test-robot-01.AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"


def _with_token(monkeypatch):
    from emori_adapter import robot_token
    monkeypatch.setenv("EMORI_POKAMIND_TOKEN_FILE", "does-not-exist/pokamind_robot_token")
    monkeypatch.setenv("POKAMIND_ROBOT_TOKEN", _GOLDEN_TOKEN)
    robot_token.reset_configured()
    monkeypatch.setattr(robot_token, "_configured", None, raising=False)
    return robot_token


def test_with_a_token_a_pinned_emori_local_drop_in_becomes_the_robots_own(monkeypatch, tmp_path, caplog):
    """A roster can pin another @emori.local drop-in, e.g. robot-lobby-01@emori.local.
    Under its token Pokamind refuses any @emori.local learner but the robot's own, so every
    drop-in would fail while readycheck (which probes the own account) passes."""
    robot_token = _with_token(monkeypatch)
    try:
        monkeypatch.setenv("EMORI_ROBOT_ID", "test-robot-01")
        p = tmp_path / "roster.yaml"
        p.write_text("anonymous:\n  student_id: robot-lobby-01\n  name: Demo reception\n"
                     "staff: {}\n", encoding="utf-8")
        with caplog.at_level("WARNING"):
            identity = Roster.from_yaml(str(p)).resolve("")
        assert identity.student_id == "robot-test-robot-01"
        assert identity.email == "robot-test-robot-01@emori.local"
        assert identity.name == "Demo reception"           # the display name still applies
        assert "pins an @emori.local learner" in caplog.text
        assert "lobby" not in caplog.text
    finally:
        monkeypatch.delenv("POKAMIND_ROBOT_TOKEN", raising=False)
        robot_token.reset_configured()


def test_with_a_token_a_pinned_staff_domain_drop_in_is_left_to_pokamind(monkeypatch, tmp_path):
    """A shared account in a real staff domain is decided by the robot's identity_domains."""
    robot_token = _with_token(monkeypatch)
    try:
        p = tmp_path / "roster.yaml"
        p.write_text("anonymous:\n  student_id: reception-1\n  email: reception@example.org\nstaff: {}\n",
                     encoding="utf-8")
        identity = Roster.from_yaml(str(p)).resolve("")
        assert (identity.student_id, identity.email) == ("reception-1", "reception@example.org")
    finally:
        monkeypatch.delenv("POKAMIND_ROBOT_TOKEN", raising=False)
        robot_token.reset_configured()


# --- the course: the roster's own, else this robot's -----------------------------------
# A roster that names no course follows this robot's: EMORI_COURSE_ID, which the site
# settings layer fills from the course issued with the robot's Pokamind token when it
# is left unset locally, else 'emori'. An explicit roster course_id still wins.

def _roster_text(tmp_path, text: str) -> str:
    path = tmp_path / "roster.yaml"
    path.write_text(text, encoding="utf-8")
    return str(path)


_NO_COURSE = (
    "staff:\n"
    '  "EMORI:anna-001": {student_id: anna-001, email: a@example.org, name: Anna}\n')


def test_a_roster_without_a_course_uses_the_robots(monkeypatch, tmp_path):
    monkeypatch.setenv("EMORI_COURSE_ID", "course-from-the-token")

    roster = Roster.from_yaml(_roster_text(tmp_path, _NO_COURSE))

    assert roster.course_id == "course-from-the-token"
    assert roster.resolve("EMORI:anna-001").course_id == "course-from-the-token"
    assert roster.resolve("").course_id == "course-from-the-token"   # drop-in too
    assert roster.badge_claim_sample()[0] == "course-from-the-token"


def test_with_no_course_anywhere_it_is_emori(monkeypatch, tmp_path):
    monkeypatch.delenv("EMORI_COURSE_ID", raising=False)

    assert DEFAULT_COURSE_ID == "emori" == effective_course_id()
    assert Roster.from_yaml(_roster_text(tmp_path, _NO_COURSE)).course_id == "emori"
    assert Roster.default().course_id == "emori"
    # Empty and blank count as unset, so the claim is never empty.
    monkeypatch.setenv("EMORI_COURSE_ID", "   ")
    assert effective_course_id() == "emori"


def test_an_explicit_roster_course_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("EMORI_COURSE_ID", "course-from-the-token")

    roster = Roster.from_yaml(_roster_text(tmp_path, "course_id: pinned-course\n" + _NO_COURSE))

    assert roster.course_id == "pinned-course"
    assert roster.resolve("EMORI:anna-001").course_id == "pinned-course"


@pytest.mark.parametrize("line", ["course_id:\n", 'course_id: ""\n', "course_id: '  '\n"])
def test_an_empty_roster_course_is_no_course(monkeypatch, tmp_path, line):
    monkeypatch.setenv("EMORI_COURSE_ID", "course-from-the-token")

    roster = Roster.from_yaml(_roster_text(tmp_path, line + _NO_COURSE))

    assert roster.course_id == "course-from-the-token"


def test_the_default_roster_follows_the_robots_course(monkeypatch):
    monkeypatch.setenv("EMORI_COURSE_ID", "course-from-the-token")

    assert Roster.default().course_id == "course-from-the-token"
    assert Roster.default().badge_claim_sample() == ("course-from-the-token", None)


def test_a_reload_re_reads_the_course(monkeypatch, tmp_path):
    monkeypatch.setenv("EMORI_COURSE_ID", "course-a")
    path = _roster_text(tmp_path, "course_id: pinned\n" + _NO_COURSE)
    roster = Roster.from_yaml(path)
    assert roster.course_id == "pinned"

    Path(path).write_text(_NO_COURSE, encoding="utf-8")
    roster.reload()

    assert roster.course_id == "course-a"


@pytest.mark.parametrize("data, expected", [
    ({"course_id": "x"}, "x"), ({"course_id": " x "}, "x"), ({"course_id": 42}, "42"),
    ({"course_id": None}, None), ({"course_id": ""}, None), ({}, None), (None, None),
    ([1], None),
])
def test_roster_course_id_reads_only_a_real_value(data, expected):
    assert roster_course_id(data) == expected
