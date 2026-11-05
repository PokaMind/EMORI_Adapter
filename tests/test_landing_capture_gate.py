"""The landing verbal-menu mic must be explicitly constrained and energy-gated.

The kiosk JS lives in Python string templates (pages._STT_JS, substituted by
server._page), so these are string/contract assertions plus one rendered-page test.
"""

from __future__ import annotations

import json

from emori_adapter.hostapp import pages


def test_landing_mic_requests_explicit_constraints():
    js = pages._STT_JS
    assert "getUserMedia({audio:true})" not in js  # the unconstrained-mic defect
    # echoCancellation is REQUIRED, not an ideal hint the browser may drop silently.
    assert "echoCancellation:{exact:true}" in js
    assert "noiseSuppression:true" in js
    assert "autoGainControl:true" in js
    assert "channelCount:{ideal:1}" in js
    assert "sampleRate:{ideal:16000}" in js


def test_overconstrained_is_a_hard_visible_failure():
    js = pages._STT_JS
    assert "OverconstrainedError" in js
    assert "mic_constraints_unsupported" in js
    # Visible three ways: on screen, in the hostapp log, and via GET /api/emori-debug.
    # Assert the CODE, never the mention: `micHardFail`'s own comment block contains the
    # words "S.mic_constrained", "/api/emori-debug" and "__emoriMicError", so the looser
    # `"S.mic_constrained" in js` / `"/api/emori-debug" in js` pins this suite once shipped
    # green with BOTH the on-screen assignment and the whole debug POST deleted (verified
    # by mutation).  A "reduce debug-ring noise" edit would have silently taken the POST
    # with it, and the on-robot check for this -- no `emori_mic_constraints` event in
    # `GET /api/emori-debug?n=20` -- would then read as a PASS while nothing is emitted.
    assert "msg.textContent=S.mic_constrained;" in js
    assert "fetch('/api/emori-debug',{method:'POST'" in js
    assert "type:'emori_mic_constraints'" in js
    assert "window.__emoriMicError=code;" in js
    # ...and NEVER a silent relaxed retry: exactly one getUserMedia call in _STT_JS.
    assert js.count("getUserMedia(") == 1


def test_acquired_track_is_verified_not_trusted():
    js = pages._STT_JS
    # A browser may accept the constraint and still hand back EC-off audio.
    assert "getSettings" in js
    assert "echoCancellation===false" in js
    assert "mic_echo_cancellation_ignored" in js


def test_energy_gate_runs_before_the_post():
    js = pages._STT_JS
    gate = "let gated=(STT_GATE.min_voiced_ms>0 && voicedMs(pcm)<STT_GATE.min_voiced_ms);"
    assert gate in js
    assert "if(gated) return;" in js            # ...and a gated window never reaches Vosk
    assert "function voicedMs(pcm){" in js
    # The menu grammar moved the POST behind a built `url` (it appends the constrained grammar), so the
    # anchor is the URL construction + the POST itself rather than a literal fetch('/api/stt.
    assert js.index(gate) < js.index("'/api/stt?lang='")
    assert js.index(gate) < js.index("fetch(url,{method:'POST'")
    assert js.index("if(gated) return;") < js.index("'/api/stt?lang='")
    assert "const STT_GATE=__STT_GATE__;" in js
    # The length floor stays the first, cheapest reject -- but it no longer DESTROYS the
    # buffer on its way out (that is what made a sub-0.5 s edge flush lose the answer).
    assert "if(pcm.length<8000){ resetCapture(); return; }" in js
    assert "const chunks=pcmBuf; pcmBuf=[];" not in js
    assert js.index("if(pcm.length<8000)") < js.index(gate)


def test_voiced_ms_arithmetic_is_pinned():
    """The string assertions above pass for a `voicedMs` that returns 0 for everything.

    That broken body is the WORST outcome this task can produce (a permanently deaf
    verbal menu), and an inverted dBFS->amplitude conversion would be the second worst
    (every window, silence included, passes).  The repo has no JS harness, so pin the
    three lines that carry the arithmetic instead.
    """
    js = pages._STT_JS
    # dBFS -> full-scale s16 amplitude.  `Math.pow(10,-STT_GATE.dbfs/20)` (sign flipped)
    # would let pure silence through; anything but *32768 mis-scales the floor.
    assert "const floor=Math.pow(10,STT_GATE.dbfs/20)*32768;" in js
    # 16 samples per ms at 16 kHz - a wrong factor silently changes the frame length.
    assert "const n=Math.max(1,Math.round(16*STT_GATE.frame_ms));" in js
    # RMS over the frame, and `>=` the floor: not a peak, not an inverted comparison.
    assert "if(Math.sqrt(s/n)>=floor) ms+=STT_GATE.frame_ms;" in js
    # ...and the accumulator is actually returned (a `return 0;` body mutes the menu).
    assert js.index("let ms=0;") < js.index("return ms;")
    # The observed value is readable on the robot, so a too-high floor is distinguishable
    # from "nobody spoke" without editing the deployed page.
    assert "window.__emoriVoicedMs=ms;" in js


def test_mic_hard_fail_hands_the_kiosk_back_to_the_badge_scanner():
    """A hard mic failure must not leave the page wedged holding the last badge.

    ``micHardFail`` -> ``stopListen()`` clears the menu's listen deadline while ``phase`` is
    still ``'listen'``, so ``onMiss()`` -> ``giveUp()`` (the path that re-opens the camera)
    can never run.  ``onBadge`` already released the camera, so without an explicit
    hand-back the kiosk is deaf AND blind.  The hand-back returns to ``'scan'`` and
    re-opens the camera, but KEEPS the identified badge (``qr``): this is the SAME present
    person the mic just failed for, so their next topic-button tap (buttons need no mic)
    must stay attributed to them, not be silently anonymised.  An actual new visitor is
    re-identified authoritatively by the re-opened camera (``onBadge`` overwrites ``qr``)
    or by the explicit anonymous button (``onBadge('')``).
    """
    js = pages._STT_JS
    body = js[js.index("function micHardFail("):js.index("async function initStt()")]
    assert "phase='scan';" in body
    assert "qr='';" not in body       # the identified badge is KEPT, not anonymised
    assert "lastQr='';" in body       # ...but re-showing the same badge still works
    assert "tries=0;" in body
    assert "try{ startCam(); }catch(e){}" in body   # the camera onBadge released re-opens
    # NOT a giveUp() CALL: it speaks a ~4s give-up line and replaces the message outright.
    assert "giveUp();" not in body
    assert "msg.textContent=S.mic_constrained;" in body   # the code, not the comment
    # sttMode=null before the hand-back, so the next listen() takes the
    # voice_unavailable ladder instead of retrying the constraint in a loop.
    assert body.index("sttMode=null;") < body.index("phase='scan';")


def test_mic_hard_fail_yields_to_a_session_that_is_already_starting():
    """The hand-back must not fire once ``beginSession`` has taken the page.

    ``getUserMedia`` can still be pending when a topic button starts a session:
    ``beginSession`` sets ``navigating`` and calls ``stopCam()`` to free the single webcam
    *before* the Pokamind session claims it.  A rejection arriving after that would run
    the hand-back, re-acquiring the webcam mid-navigation -- exactly the device-busy /
    NotReadableError race the ``stopCam()`` comment exists to avoid -- and overwrite
    "Starting <topic>...".  The durable diagnostic POST still happens first: it costs
    nothing and losing it is how a real EC failure would go unnoticed.
    """
    js = pages._STT_JS
    body = js[js.index("function micHardFail("):js.index("async function initStt()")]
    assert "if(navigating) return;" in body
    guard = body.index("if(navigating) return;")
    assert body.index("fetch('/api/emori-debug',{method:'POST'") < guard
    assert guard < body.index("msg.textContent=S.mic_constrained;")
    assert guard < body.index("try{ startCam(); }catch(e){}")
    assert guard < body.index("phase='scan';")


def test_ui_strings_have_mic_constrained_in_both_kiosk_languages():
    for lang in ("en", "ca"):
        assert pages.UI_STRINGS[lang]["mic_constrained"].strip()
    # test_catalan_kiosk.py pins this too; keep it local so a one-sided edit fails here.
    assert set(pages.UI_STRINGS["ca"]) == set(pages.UI_STRINGS["en"])


def test_stt_gate_defaults(monkeypatch):
    from emori_adapter.hostapp import server

    for name in ("EMORI_STT_ENERGY_DBFS", "EMORI_STT_MIN_VOICED_MS"):
        monkeypatch.delenv(name, raising=False)
    assert json.loads(server._stt_gate_json()) == {
        "dbfs": -50.0,
        "min_voiced_ms": 120,
        "frame_ms": 20,
    }


def test_stt_gate_is_env_tunable_and_disablable(monkeypatch):
    from emori_adapter.hostapp import server

    monkeypatch.setenv("EMORI_STT_ENERGY_DBFS", "-38")
    monkeypatch.setenv("EMORI_STT_MIN_VOICED_MS", "0")
    gate = json.loads(server._stt_gate_json())
    assert gate["dbfs"] == -38.0
    assert gate["min_voiced_ms"] == 0  # 0 disables the gate entirely


def test_stt_gate_survives_a_bad_env(monkeypatch):
    from emori_adapter.hostapp import server

    monkeypatch.setenv("EMORI_STT_ENERGY_DBFS", "loud")
    monkeypatch.setenv("EMORI_STT_MIN_VOICED_MS", "-5")
    gate = json.loads(server._stt_gate_json())
    assert gate["dbfs"] == -50.0   # unparseable -> default, a bad .env must not mute the kiosk
    assert gate["min_voiced_ms"] == 0
    monkeypatch.setenv("EMORI_STT_ENERGY_DBFS", "-999")
    assert json.loads(server._stt_gate_json())["dbfs"] == -120.0  # clamped


def test_a_bad_stt_gate_env_is_logged_not_swallowed(monkeypatch, caplog):
    """A silent revert is the worst failure for the exact workflow these knobs exist for.

    Matches the sibling ``_menu_tuning._num`` idiom (same module, 20 lines above).
    """
    from emori_adapter.hostapp import server

    monkeypatch.setenv("EMORI_STT_ENERGY_DBFS", "-42dB")   # a plausible operator typo
    monkeypatch.setenv("EMORI_STT_MIN_VOICED_MS", "-5")    # clamped, not honoured
    with caplog.at_level("WARNING", logger="emori.hostapp"):
        gate = json.loads(server._stt_gate_json())
    assert gate == {"dbfs": -50.0, "min_voiced_ms": 0, "frame_ms": 20}
    text = caplog.text
    assert "EMORI_STT_ENERGY_DBFS" in text and "not a number" in text
    assert "EMORI_STT_MIN_VOICED_MS" in text and "outside" in text


def test_landing_page_renders_the_gate_config(monkeypatch):
    from starlette.testclient import TestClient

    from emori_adapter.hostapp import server
    from emori_adapter.robot.client import DryRunRobotClient

    monkeypatch.setattr(server, "DEMO", True)
    monkeypatch.setenv("EMORI_STT_ENERGY_DBFS", "-42")
    monkeypatch.delenv("EMORI_STT_MIN_VOICED_MS", raising=False)
    app = server._build_app(
        overrides={
            "robot_client": DryRunRobotClient(),
            "fiware_factory": None,
            "idle": False,
            "watchdog": False,
            "gesture": False,
            "tts": None,
            "stt": None,
        }
    )
    with TestClient(app) as client:
        page = client.get("/").text
    assert "__STT_GATE__" not in page  # the token was substituted
    assert '"dbfs": -42.0' in page
    assert '"min_voiced_ms": 120' in page
