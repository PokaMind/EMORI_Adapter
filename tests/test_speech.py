"""Tests for the server-side speech path: /api/tts (Piper) + /api/stt (Vosk).

The engines are faked — these verify the endpoint contract the kiosk pages rely on
(WAV out / transcript in, 503 when an engine is missing so the pages fall back to
browser speech).
"""

from __future__ import annotations

import logging
import re
import wave

import pytest
from starlette.testclient import TestClient

from emori_adapter.speech.tts import NullTts


class _FakeTtsEngine:
    available = True

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.calls: list[tuple[str, str]] = []

    def synth_path(self, text: str, lang: str = "en"):
        self.calls.append((text, lang))
        out = self.tmp_path / f"out_{len(self.calls)}.wav"
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 160)
        return str(out)


class _FakeSttEngine:
    available = True

    def __init__(self):
        self.received: list[tuple[int, str]] = []
        self.grammar_calls: list[tuple[int, list[str], str]] = []

    def transcribe(self, pcm: bytes, lang: str = "en", sample_rate: int = 16000) -> str:
        self.received.append((len(pcm), lang))
        return "number two please"

    def transcribe_grammar(self, pcm: bytes, phrases: list[str], lang: str = "en",
                           sample_rate: int = 16000) -> str:
        self.grammar_calls.append((len(pcm), list(phrases), lang))
        # Echo the wake phrase when it's the one being listened for.
        return phrases[0] if phrases else ""


@pytest.fixture()
def speech_client(tmp_path):
    from fastapi import FastAPI

    from emori_adapter.hostapp import speech_api

    app = FastAPI()
    tts = _FakeTtsEngine(tmp_path)
    stt = _FakeSttEngine()
    speech_api.mount(app, tts=tts, stt=stt)
    client = TestClient(app)
    client.tts = tts
    client.stt = stt
    return client


def test_tts_returns_wav(speech_client):
    r = speech_client.get("/api/tts?text=Hola&lang=ca")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/wav")
    assert r.content[:4] == b"RIFF"
    assert speech_client.tts.calls == [("Hola", "ca")]


def test_tts_empty_text_503(speech_client):
    assert speech_client.get("/api/tts?text=%20&lang=en").status_code == 503


def test_stt_transcribes_pcm(speech_client):
    pcm = b"\x00\x01" * 16000  # 1 s of fake 16 kHz s16le
    r = speech_client.post("/api/stt?lang=ca", content=pcm)
    assert r.status_code == 200
    assert r.json() == {"text": "number two please"}
    assert speech_client.stt.received == [(len(pcm), "ca")]


def test_stt_tiny_payload_returns_empty(speech_client):
    r = speech_client.post("/api/stt?lang=en", content=b"\x00" * 100)
    assert r.json() == {"text": ""}
    assert speech_client.stt.received == []


def test_stt_health(speech_client):
    assert speech_client.get("/api/stt/health").json() == {"available": True}


def test_vosk_engine_has_grammar_transcribe():
    from emori_adapter.hostapp.speech_api import VoskEngine
    assert hasattr(VoskEngine, "transcribe_grammar")


def test_stt_grammar_routes_to_grammar_transcribe(speech_client):
    pcm = b"\x00\x01" * 16000
    r = speech_client.post("/api/stt?lang=en&grammar=hey%20emori", content=pcm)
    assert r.status_code == 200
    assert r.json() == {"text": "hey emori"}
    # routed to transcribe_grammar with the parsed phrase list; NOT plain transcribe
    assert speech_client.stt.grammar_calls == [(len(pcm), ["hey emori"], "en")]
    assert speech_client.stt.received == []


def test_high_rate_telemetry_is_readable_without_flooding_the_journal(speech_client, caplog):
    """QR scan stats are ~10/s and forever: keep them in the ring, out of the log.

    A screenless robot reports the scan ladder only to on-screen text nobody can see, so
    "the badge does not scan" arrives with no evidence at all. The ring makes it readable
    on demand via GET; logging it would add tens of thousands of lines a day.
    """
    caplog.set_level(logging.WARNING, logger="emori_adapter.hostapp.speech_api")
    speech_client.post("/api/emori-debug", json={"type": "emori_qr", "range": 12})
    speech_client.post("/api/emori-debug", json={"type": "emori_qr_badge", "data": "ok"})
    # Count only THIS client's debug lines: under random test order another module's
    # WARNING (or a sibling speech_api instance) can land in the same caplog.
    debug_lines = [m for m in caplog.messages if m.startswith("[EMORI-DEBUG]")]
    logged = "\n".join(debug_lines)
    assert "'emori_qr'" not in logged                 # high-rate scan stats: ring only
    assert "'emori_qr_badge'" in logged               # a real decode is an event
    assert len(debug_lines) == 1
    events = speech_client.get("/api/emori-debug").json()["events"]
    assert [e.get("type") for e in events][-2:] == ["emori_qr", "emori_qr_badge"]


def test_the_debug_sink_is_content_free_by_construction(speech_client, caplog):
    """`POST /api/emori-debug` is unauthenticated, unvalidated and reachable from
    anything on the site LAN, and what it logs lands in a journal that survives
    reboots.  The shipped kiosk sends numeric telemetry only -- but the in-iframe
    turn-taking telemetry pages.py relays here comes from the Pokamind web app, not
    from this repository, so one text field added over there would start writing
    turn text to disk with no change on this side.  Numbers and booleans pass; a
    string passes only for a named key, capped; nothing else is rendered."""

    caplog.set_level(logging.WARNING, logger="emori_adapter.hostapp.speech_api")
    speech_client.post("/api/emori-debug", json={
        "type": "pokamind_emori_dbg",
        "stage": "endpointing",          # a named enum key: kept
        "silenceMs": 820, "eouProb": 0.93, "speaking": False,   # numbers: kept
        "transcript": "Hola, tinc mal de cap des de dilluns",   # the leak: dropped
        "note": "x" * 500,               # unnamed + unbounded: dropped
        "Hola tinc mal de cap": 1,       # a key is not a smuggling channel either
    })

    logged = "\n".join(m for m in caplog.messages if m.startswith("[EMORI-DEBUG]"))
    assert "mal de cap" not in logged
    assert "'stage': 'endpointing'" in logged
    assert "'silenceMs': 820" in logged and "'eouProb': 0.93" in logged
    assert "'speaking': False" in logged
    # ...and nothing is lost for live debugging: the ring still holds it whole,
    # in memory, GET-readable, and dead with the process.
    assert speech_client.get("/api/emori-debug").json()["events"][-1][
        "transcript"] == "Hola, tinc mal de cap des de dilluns"


def test_a_named_key_still_only_admits_a_slug(speech_client, caplog):
    """`code`/`constraint` ARE logged -- they are how a mic-constraint failure is
    diagnosed -- but being NAMED is not enough on its own.

    A length cap cannot separate a slug from a sentence: 40 characters of Catalan
    is a whole utterance.  What separates them is that every value the two
    shipped producers send is a single TOKEN and prose is not, so the value must
    look like one or it is dropped rather than truncated.
    """

    caplog.set_level(logging.WARNING, logger="emori_adapter.hostapp.speech_api")
    speech_client.post("/api/emori-debug", json={
        "type": "emori_mic_constraints", "code": "OverconstrainedError",
        # A named key carrying prose: dropped whole, not shortened into a
        # readable fragment of what somebody said.
        "constraint": "Sí, això és el que em van dir ahir a la tarda"})

    logged = "\n".join(m for m in caplog.messages if m.startswith("[EMORI-DEBUG]"))
    assert "OverconstrainedError" in logged      # the real value survives intact
    assert "'constraint':" not in logged         # ...the prose one is not there at all
    assert "això" not in logged and "van dir" not in logged
    assert "Sí" not in logged


def test_the_one_free_text_field_becomes_a_grouping_code(speech_client, caplog):
    """`err` is the exception: the web app sends `String(e.message)`, engine
    prose that no vocabulary check can pass.

    Truncating it would put the projection back where it started, so the journal
    gets a digest instead -- enough to see that forty `start-failed` lines are ONE
    fault, with the message itself one GET away in the ring.
    """

    caplog.set_level(logging.WARNING, logger="emori_adapter.hostapp.speech_api")
    message = "no available backend found. ERR: [wasm] failed to load"
    for _ in range(2):
        speech_client.post("/api/emori-debug", json={
            "type": "pokamind_emori_dbg", "stage": "start-failed", "err": message})
    speech_client.post("/api/emori-debug", json={
        "type": "pokamind_emori_dbg", "stage": "start-failed", "err": "Failed to fetch"})

    lines = [m for m in caplog.messages if m.startswith("[EMORI-DEBUG]")]
    logged = "\n".join(lines)
    assert message not in logged and "wasm" not in logged
    assert "'err':" not in logged                # never the text, under any key
    codes = re.findall(r"'err_id': '([0-9a-f]{8})'", logged)
    assert len(codes) == 3
    assert codes[0] == codes[1] != codes[2]      # same fault groups, a different one does not
    # ...and the message itself is still recoverable, in memory, over GET.
    assert speech_client.get("/api/emori-debug").json()["events"][-1]["err"] == (
        "Failed to fetch")


def test_the_keys_the_shipped_producers_send_all_survive(speech_client, caplog):
    """The projection is only worth having if it does not blind the two real
    producers.  These are the exact string fields pages.py and the Pokamind web
    app send, as the kiosk receives them."""

    caplog.set_level(logging.WARNING, logger="emori_adapter.hostapp.speech_api")
    speech_client.post("/api/emori-debug", json={
        "type": "pokamind_emori_dbg", "stage": "smartturn-spawn-failed",
        "lang": "ca", "ctxState": "running", "reason": "disposed-during-load",
        "cam": "1280x720", "pass": "inv", "code": "NotAllowedError",
        "sampleRate": 48000, "loadS": 0.02})

    logged = "\n".join(m for m in caplog.messages if m.startswith("[EMORI-DEBUG]"))
    for value in ("pokamind_emori_dbg", "smartturn-spawn-failed", "ca", "running",
                  "disposed-during-load", "1280x720", "inv", "NotAllowedError"):
        assert value in logged, value
    assert "48000" in logged and "0.02" in logged


def test_a_key_no_shipped_producer_sends_is_not_a_standing_invitation(
        speech_client, caplog):
    """`raw` and `error` were named keys with no user in either repo.  A sink that
    accepts text for a key nobody sends is only ever a way in."""

    caplog.set_level(logging.WARNING, logger="emori_adapter.hostapp.speech_api")
    speech_client.post("/api/emori-debug", json={
        "type": "pokamind_emori_dbg",
        "raw": "Hola, tinc mal de cap des de dilluns",
        "error": "Anna Example em va dir que se sent insegura"})

    logged = "\n".join(m for m in caplog.messages if m.startswith("[EMORI-DEBUG]"))
    assert "mal de cap" not in logged and "Anna Example" not in logged
    assert "raw" not in logged and "error" not in logged


def test_a_malformed_post_still_says_so_without_rendering_what_it_sent(
        speech_client, caplog):
    """`raw` is the sink's OWN fallback key for a body it could not parse, and it
    holds whatever an unauthenticated LAN caller chose to send -- so the journal
    gets a type it can act on and the payload stays in the ring."""

    caplog.set_level(logging.WARNING, logger="emori_adapter.hostapp.speech_api")
    speech_client.post("/api/emori-debug", content=b"Hola, tinc mal de cap",
                       headers={"content-type": "application/json"})
    speech_client.post("/api/emori-debug", json=["tinc mal de cap"])

    logged = "\n".join(m for m in caplog.messages if m.startswith("[EMORI-DEBUG]"))
    assert "emori_debug_unparseable" in logged
    assert "emori_debug_non_object" in logged
    assert "mal de cap" not in logged
    assert speech_client.get("/api/emori-debug").json()["events"][-1]["raw"] == [
        "tinc mal de cap"]


@pytest.fixture()
def scored_speech_client(tmp_path):
    """A client whose engine SCORES, like the deployed VoskEngine (the menu path)."""
    from fastapi import FastAPI

    from emori_adapter.hostapp import speech_api

    class _ScoringSttEngine(_FakeSttEngine):
        def transcribe_grammar_conf(self, pcm, phrases, lang="en", sample_rate=16000):
            self.grammar_calls.append((len(pcm), list(phrases), lang))
            return (phrases[0] if phrases else ""), 0.5

    app = FastAPI()
    stt = _ScoringSttEngine()
    speech_api.mount(app, tts=_FakeTtsEngine(tmp_path), stt=stt)
    client = TestClient(app)
    client.stt = stt
    return client


def test_tts_is_refused_while_a_live_session_owns_the_speaker(tmp_path):
    """During a roleplay, the ONLY voice allowed is the session's own TTS.

    Requirement: the on-robot voice must never operate mid-session -
    enforced server-side (409), not by trusting callers. End-of-session feedback
    still works because finalize flips the flag before finish()/goHome speak.
    """
    from fastapi import FastAPI

    from emori_adapter.hostapp import speech_api

    live = {"active": True}
    app = FastAPI()
    speech_api.mount(app, tts=_FakeTtsEngine(tmp_path), stt=None,
                     session_active=lambda: live["active"])
    client = TestClient(app)
    assert client.get("/api/tts?text=hi&lang=en").status_code == 409
    live["active"] = False   # finalize ran: menus/feedback own the speaker again
    assert client.get("/api/tts?text=hi&lang=en").status_code == 200


def test_grammar_stt_logs_the_interaction_without_the_words(scored_speech_client, caplog):
    """A menu miss must be diagnosable from the journal alone -- as a SLOT.

    Without this the log showed only `POST /api/stt 200` and the robot saying "Sorry, I
    didn't catch that" -- indistinguishable between "heard nothing", "heard a word but
    scored it below the gate" and "heard the wrong word". Diagnosing it needed a live
    probe on the robot.

    It logged the recognised string to separate those three, and a closed menu grammar
    does mean free speech cannot appear in it -- but a matched word is still a thing an
    identified staff member said out loud, and this journal is world-readable and
    outlives the session. The INDEX separates the same three cases and records nothing
    anybody uttered.
    """
    caplog.set_level(logging.WARNING, logger="emori_adapter.hostapp.speech_api")
    r = scored_speech_client.post("/api/stt?lang=en&grammar=one,two,three",
                                  content=b"\x00\x01" * 16000)
    assert r.json() == {"text": "one", "conf": 0.5}   # the CALLER still gets the word
    line = "\n".join(caplog.messages)
    assert "menu stt" in line
    assert "slot=0/3" in line            # which option, out of how many
    assert "conf=0.500" in line          # and how it scored against the gate
    assert "dur_ms=1000" in line         # how much audio it was heard in
    assert "text=" not in line and "'one'" not in line


def test_a_grammar_miss_is_still_told_apart_from_a_match(tmp_path, caplog):
    """slot=-1 is the "heard nothing in the menu" case the line exists for."""

    from fastapi import FastAPI

    from emori_adapter.hostapp import speech_api

    class _NeverMatches(_FakeSttEngine):
        def transcribe_grammar_conf(self, pcm, phrases, lang="en", sample_rate=16000):
            return "", 0.0

    app = FastAPI()
    speech_api.mount(app, tts=_FakeTtsEngine(tmp_path), stt=_NeverMatches())
    caplog.set_level(logging.WARNING, logger="emori_adapter.hostapp.speech_api")

    TestClient(app).post("/api/stt?lang=en&grammar=two,three",
                         content=b"\x00\x01" * 16000)

    assert "slot=-1/2" in "\n".join(caplog.messages)


def test_open_stt_never_logs_the_transcript(speech_client, caplog):
    # Open recognition is unconstrained free speech: shape only, never a sample.
    caplog.set_level(logging.WARNING, logger="emori_adapter.hostapp.speech_api")
    speech_client.post("/api/stt?lang=en", content=b"\x00\x01" * 16000)
    assert "number two please" not in "\n".join(caplog.messages)


def test_stt_grammar_splits_comma_phrases(speech_client):
    pcm = b"\x00\x01" * 16000
    r = speech_client.post("/api/stt?lang=en&grammar=hey%20emori,%20hola%20emori", content=pcm)
    assert r.status_code == 200
    assert speech_client.stt.grammar_calls[-1][1] == ["hey emori", "hola emori"]


def test_null_tts_produces_no_audio():
    assert NullTts().synthesize("Hello", "en") is None


def test_tts_engine_failure_returns_503(tmp_path):
    from fastapi import FastAPI

    from emori_adapter.hostapp import speech_api

    class _Broken:
        def synth_path(self, text, lang="en"):
            raise RuntimeError("voice model corrupt")

    app = FastAPI()
    speech_api.mount(app, tts=_Broken(), stt=None)
    c = TestClient(app)
    assert c.get("/api/tts?text=hi&lang=en").status_code == 503


def test_tts_that_never_returns_answers_503_instead_of_hanging(monkeypatch):
    """A wedged synth must not hold the request open forever.

    The kiosk's say() only advances in its callback, so a /api/tts that never answers
    stopped the page dead: no speech, no fallback, no next step (the landing menu never
    listened, the session page never went home).
    """
    import threading

    from fastapi import FastAPI

    from emori_adapter.hostapp import speech_api

    monkeypatch.setattr(speech_api, "_TTS_SYNTH_TIMEOUT_S", 0.05)
    release = threading.Event()

    class _Wedged:
        def synth_path(self, text, lang="en"):
            release.wait(10.0)          # never within the timeout above
            return None

    app = FastAPI()
    speech_api.mount(app, tts=_Wedged(), stt=None)
    try:
        with TestClient(app) as c:
            assert c.get("/api/tts?text=hi&lang=en").status_code == 503
    finally:
        release.set()                   # let the leaked worker thread retire


def test_real_piper_synthesizes_valid_wav(tmp_path):
    """Exercise the REAL Piper wrapper against the installed piper-tts.

    Guards the API the wrapper depends on: piper-tts >=1.3 moved file writing to
    ``synthesize_wav`` (bare ``synthesize`` now returns an unconsumed AudioChunk
    iterable, leaving the wave header unwritten -> "# channels not specified").
    Skipped where piper or the voice models aren't installed (e.g. CI).
    """
    import os
    import wave

    pytest.importorskip("piper", reason="piper-tts not installed")
    from emori_adapter.speech.tts import PiperTts

    en = os.path.join("voices", "en.onnx")
    if not os.path.exists(en):
        pytest.skip("voices/en.onnx not present")

    out = PiperTts({"en": en}, out_dir=str(tmp_path)).synthesize("Hello there.", "en")
    assert out and os.path.getsize(out) > 1000
    with wave.open(out, "rb") as w:
        assert w.getnchannels() >= 1
        assert w.getnframes() > 0
