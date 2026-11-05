"""Server-side speech for the kiosk: Piper TTS out, Vosk STT in.

Both run on the hostapp (CM4 or dev box) so the **identical code path** serves the
simulator and the deployed robot — the kiosk pages just play/post audio through
Chromium. Both engines are optional: when missing, the endpoints return 503 and the
pages fall back to the browser's speechSynthesis / Web Speech automatically.

Models (ca = production, en = test build):
* Piper voices: ``PIPER_VOICE_{EN,CA}`` (.onnx) or ``./voices/{en,ca}.onnx``; any other
  language by ``PIPER_VOICE_<LANG>`` or ``./voices/<lang>.onnx``.
* Vosk models:  ``VOSK_MODEL_CA`` / ``VOSK_MODEL_EN`` (dirs) or ``./models/vosk-{ca,en}``.

``scripts/get_voice_models.ps1`` / ``.sh`` download both sets.
"""

# NOTE: no `from __future__ import annotations` — see server.py (FastAPI handler
# annotations must be evaluated at def time inside mount()'s closure scope).
import asyncio
import glob
import hashlib
import json
import logging
import os
import re
import threading
from typing import Optional

log = logging.getLogger("emori.hostapp.speech")

TTS_CACHE_DIR = os.environ.get("EMORI_TTS_CACHE", "artifacts/tts_cache")
# Upper bound on ONE synthesis. Piper on the CM4 reads a long course title in well under
# two seconds; anything past this is a wedged engine, and a wedged engine must not hold the
# request open: a kiosk waiting on it stops advancing (no speech, no callback, no error).
_TTS_SYNTH_TIMEOUT_S = 15.0

# Upper bound on ONE recognition. The FIRST call also pays the lazy vosk.Model load
# (2.6 s measured on the CM4, more under load), so this is deliberately generous.
# Recognition stays off the event loop: on it every poller, watchdog and relay would
# stall, and a libvosk abort during a model load poisoned by a wedged audio teardown
# (std::system_error ESRCH) would take the whole hostapp down with no Python trace.  A timed-out worker leaks one thread, which is
# strictly better than a frozen kiosk.
_STT_TIMEOUT_S = 20.0

# ----- the /api/emori-debug journal projection -----
#
# That endpoint is unauthenticated, unbounded and reachable by anything on the
# local network, and every non-quiet event it receives is written to a journal that
# is world-readable to whoever holds the box and survives reboots.  The shipped
# kiosk sends only numeric/enum telemetry today -- but the in-iframe endpointer
# whose frames pages.py relays here lives in a SEPARATE codebase, so one
# added text field over there would start writing turn text into journald with
# no change on this side and nothing to notice it.
#
# So the projection is content-free BY CONSTRUCTION, not by convention:
#
# * numbers and booleans pass for ANY key -- an utterance cannot be one, and
#   this is what keeps future counters from that other codebase useful without a
#   round trip through here;
# * STRINGS are the only shape a transcript can take, so a string passes only
#   for a NAMED key and only if the value itself is SLUG-SHAPED;
# * keys must look like identifiers and are capped in number, which is also what
#   stops the endpoint being a log-injection and journal-flooding primitive.
#
# Nothing is lost for live debugging: the FULL body still goes into `_dbg_ring`,
# readable over GET /api/emori-debug, and it dies with the process.
#
# On the value rule, which is the part a length cap could not do.  A cap is not
# a content bound: 40 characters of Catalan is a whole sentence ("Sí, això és el
# que em van dir ahir a la tarda" is 42), so "short enough to be safe" and "long
# enough to be useful" have no overlap.  What the shipped producers actually
# send is a closed vocabulary of single TOKENS:
#
# * pages.py -- `type` ("emori_qr_badge"), `code` (a getUserMedia/DOMException
#   name or a reject slug), `constraint` (a constraint name), `cam` ("1280x720"
#   / "none"), `pass` (a ladder-pass label);
# * the Pokamind endpointer's stage reports -- `stage`
#   ("smartturn-spawn-failed", the longest at 22 characters), `lang` ("ca"),
#   `ctxState` ("running"), `reason` ("disposed-during-load"); everything else
#   it sends is already a number.
#
# Not one of them contains a space, and prose does.  So the rule is the regex
# below -- identifier characters only, no whitespace, no sentence punctuation,
# <= _DEBUG_SLUG_MAX -- and a producer that starts sending a transcript under a
# named key is dropped rather than truncated.  32 is the cap because the longest
# real value measured is 22 and the closed vocabularies grow by hyphenated
# words; it is a backstop on the regex, not the safety property.
#
# `raw` and `error` were in this list and are gone: NEITHER side sends them, so
# they were a standing invitation with no user.  `err` IS sent (four sites, all
# `String(e.message)`, i.e. genuinely free text) -- see _DEBUG_DIGEST_KEYS.
_DEBUG_SLUG_KEYS = frozenset({
    "type", "code", "constraint", "cam", "pass", "stage", "state", "phase",
    "mode", "event", "reason", "lang", "engine", "model", "ctxState",
})
# The free-text exception, handled by not writing the text down. `err` carries a
# JavaScript Error message from the endpointer's start/spawn/VAD failure paths,
# which is the one string here that is authored by an engine rather than chosen
# from a vocabulary -- so it cannot be slug-checked, and truncating it would put
# the projection back where it started. Instead the journal gets `err_id`, a
# short digest: an operator watching `stage=start-failed err_id=<same code>`
# repeat learns it is ONE fault and not forty, and the message itself is one
# `curl localhost:8080/api/emori-debug` away in the ring. Unsalted and truncated
# on purpose -- it groups identical strings, and it is not an identity.
_DEBUG_DIGEST_KEYS = frozenset({"err"})
_DEBUG_SLUG_MAX = 32
_DEBUG_MAX_KEYS = 32
_DEBUG_KEY_RE = re.compile(r"[A-Za-z0-9_.-]{1,32}\Z")
# Deliberately excludes whitespace: that is the whole guarantee. Also excludes
# the quote/brace characters that would let a value forge a second field in a
# journal line.
_DEBUG_SLUG_RE = re.compile(r"[A-Za-z0-9_.:/+-]{1,%d}\Z" % _DEBUG_SLUG_MAX)


def journal_safe_debug(body: dict) -> dict:
    """The part of a debug POST that may be written down.  See above."""

    safe: dict = {}
    if not isinstance(body, dict):
        return safe
    for key, value in body.items():
        if len(safe) >= _DEBUG_MAX_KEYS:
            break
        if type(key) is not str or _DEBUG_KEY_RE.fullmatch(key) is None:
            continue
        if type(value) in (bool, int, float):
            safe[key] = value
        elif type(value) is not str:
            continue
        elif key in _DEBUG_SLUG_KEYS and _DEBUG_SLUG_RE.fullmatch(value) is not None:
            safe[key] = value
        elif key in _DEBUG_DIGEST_KEYS and value:
            safe[key + "_id"] = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return safe


class PiperEngine:
    """Synthesizes WAVs with Piper, cached by (lang, text) content hash."""

    available = True

    def __init__(self, voices: dict[str, str], cache_dir: str = TTS_CACHE_DIR) -> None:
        from emori_adapter.speech.tts import PiperTts

        os.makedirs(cache_dir, exist_ok=True)
        self.cache_dir = cache_dir
        self._tts = PiperTts(voices, out_dir=cache_dir)

    def synth_path(self, text: str, lang: str = "en") -> Optional[str]:
        key = hashlib.sha1(f"{lang}|{text}".encode("utf-8")).hexdigest()[:24]
        cached = os.path.join(self.cache_dir, f"{key}.wav")
        if os.path.exists(cached):
            return cached
        path = self._tts.synthesize(text, lang)
        if path is None:
            return None
        try:
            os.replace(path, cached)
            return cached
        except OSError:
            return path


class VoskEngine:
    """Transcribes 16 kHz mono s16le PCM with Vosk (offline, en/es)."""

    available = True
    # Recognition runs on worker threads (/api/stt to_thread); two concurrent
    # first calls must not race the multi-second lazy model load into loading
    # it twice.  Class-level so engines built without __init__ (test doubles)
    # still lock.
    _load_lock = threading.Lock()

    def __init__(self, models: dict[str, str]) -> None:
        import vosk

        vosk.SetLogLevel(-1)
        self._vosk = vosk
        self._paths = models
        self._loaded: dict[str, object] = {}

    def _model(self, lang: str):
        key = lang.split("-")[0].split("_")[0].lower()
        path = self._paths.get(key) or next(iter(self._paths.values()))
        with self._load_lock:
            if path not in self._loaded:
                self._loaded[path] = self._vosk.Model(path)
            return self._loaded[path]

    def transcribe(self, pcm: bytes, lang: str = "en", sample_rate: int = 16000) -> str:
        rec = self._vosk.KaldiRecognizer(self._model(lang), sample_rate)
        rec.AcceptWaveform(pcm)
        try:
            return str(json.loads(rec.FinalResult()).get("text", "")).strip()
        except (TypeError, ValueError):
            return ""

    # Vosk's garbage token. It is appended to every grammar and, on the way back, it
    # arrives INSIDE ``text`` and always scores conf 1.000 - see transcribe_grammar_conf.
    _UNK = "[unk]"

    def transcribe_grammar(self, pcm: bytes, phrases: list[str], lang: str = "en",
                           sample_rate: int = 16000) -> str:
        """Transcribe constrained to a small phrase grammar (+ '[unk]' garbage).

        Used for wake/keyword spotting: Vosk only emits one of ``phrases`` (or the
        unknown token), which sharply reduces false accepts versus open recognition.

        UNSCORED, and therefore no longer what ``/api/stt?grammar=`` reaches for: that
        endpoint prefers ``transcribe_grammar_conf`` whenever the engine exposes it (this
        one does), and only engines without a scorer fall back here. Kept as the stable
        minimal interface those engines implement.
        """
        grammar = json.dumps(list(phrases) + [self._UNK])
        rec = self._vosk.KaldiRecognizer(self._model(lang), sample_rate, grammar)
        rec.AcceptWaveform(pcm)
        try:
            return str(json.loads(rec.FinalResult()).get("text", "")).strip()
        except (TypeError, ValueError):
            return ""

    def transcribe_grammar_conf(self, pcm: bytes, phrases: list[str], lang: str = "en",
                                sample_rate: int = 16000) -> tuple[str, float]:
        """Constrained transcribe that also reports how much to TRUST the result.

        Returns ``(text, conf)``:

        * ``text`` - the recognised words with Vosk's ``[unk]`` garbage tokens removed
          (Vosk puts ``[unk]`` *inside* ``text``, e.g. ``"[unk] two"``).
        * ``conf`` - the MINIMUM per-word confidence over the non-``[unk]`` words
          (``SetWords(True)``). ``[unk]`` is excluded because Vosk always scores it 1.000,
          which would mask a weak real word.

        Measured on this tree (vosk 0.3.45, models/vosk-en, Piper voices/en.onnx, 16 kHz):
        "Say two." -> ``"[unk] two"`` conf 0.832; "Number three, please." ->
        ``"[unk] three [unk]"`` conf 1.000; two unrelated sentences -> ``"[unk]"`` with no
        scored word at all. A phrase grammar FORCES every segment onto a menu word or
        ``[unk]``, so it makes false accepts MORE likely on its own - this score is what
        makes it safe. Callers must gate on it.
        """
        grammar = json.dumps(list(phrases) + [self._UNK])
        rec = self._vosk.KaldiRecognizer(self._model(lang), sample_rate, grammar)
        rec.SetWords(True)
        rec.AcceptWaveform(pcm)
        try:
            payload = json.loads(rec.FinalResult())
        except (TypeError, ValueError):
            return "", 0.0
        text = " ".join(w for w in str(payload.get("text", "")).split()
                        if w != self._UNK).strip()
        words = payload.get("result")
        if not isinstance(words, list) or not words:
            # No per-word detail (a build that ignores SetWords): the grammar is then the
            # only constraint, so report a neutral 1.0 rather than bricking selection.
            return text, (1.0 if text else 0.0)
        confs = [float(w.get("conf", 0.0)) for w in words
                 if isinstance(w, dict) and str(w.get("word", "")) != self._UNK]
        if not confs:
            return "", 0.0
        return text, min(confs)


def _find(env_map: dict[str, str], fallback_pattern: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for lang, env in env_map.items():
        path = os.environ.get(env, "") or fallback_pattern.format(lang=lang)
        if path and os.path.exists(path):
            found[lang] = path
    return found


# A bare language tag: what may name a voice file or a PIPER_VOICE_<LANG> variable.
_LANG_TAG = re.compile(r"[a-z]{2,3}\Z")
# The Piper voice of each kiosk language: an env override, else voices/<lang>.onnx
# (scripts/get_voice_models.sh installs them). A voice of any other language is found the
# same way, so an add-on's install hook only has to put it on disk.
PIPER_VOICE_ENV = {
    "en": "PIPER_VOICE_EN",
    "ca": "PIPER_VOICE_CA",
}
PIPER_VOICE_DEFAULT = "voices/{lang}.onnx"


def piper_voice_env(lang: str) -> Optional[str]:
    """``PIPER_VOICE_<LANG>`` for a bare language tag, None for anything else."""

    return f"PIPER_VOICE_{lang.upper()}" if _LANG_TAG.match(lang) else None


def _with_installed(core: dict[str, str], prefix: str, pattern: str) -> dict[str, str]:
    """``core``, then every other language a ``<prefix><LANG>`` variable or a file names.

    ``core`` stays in front: PiperTts falls back to the FIRST voice for an unknown tag.
    """

    found = {name[len(prefix):].lower() for name, value in os.environ.items()
             if name.startswith(prefix) and value}
    head, tail = pattern.split("{lang}")
    for path in glob.glob(f"{head}*{tail}"):
        name = path.replace(os.sep, "/")
        found.add(name[len(head):len(name) - len(tail)])
    others = sorted(lang for lang in found if _LANG_TAG.match(lang) and lang not in core)
    return {**core, **{lang: f"{prefix}{lang.upper()}" for lang in others}}


def build_tts() -> Optional[PiperEngine]:
    # Every language with an installed voice must be in this map: a language
    # missing here synthesizes through PiperTts' first-voice fallback -- an
    # English voice reading the text -- which a listener hears as "the robot's
    # pronunciation is broken in <language>".
    voices = _find(_with_installed(PIPER_VOICE_ENV, "PIPER_VOICE_", PIPER_VOICE_DEFAULT),
                   PIPER_VOICE_DEFAULT)
    if not voices:
        log.info("no Piper voices found; /api/tts will 503 (pages fall back to speechSynthesis)")
        return None
    try:
        return PiperEngine(voices)
    except Exception as e:  # noqa: BLE001 - piper not installed / model unloadable
        log.warning("Piper unavailable (%s); /api/tts will 503", e)
        return None


def build_stt() -> Optional[VoskEngine]:
    pattern = "models/vosk-{lang}"
    models = _find(
        _with_installed({"en": "VOSK_MODEL_EN", "ca": "VOSK_MODEL_CA"}, "VOSK_MODEL_", pattern),
        pattern,
    )
    if not models:
        log.info("no Vosk models found; /api/stt will 503 (pages fall back to Web Speech)")
        return None
    try:
        return VoskEngine(models)
    except Exception as e:  # noqa: BLE001 - vosk not installed / model unloadable
        log.warning("Vosk unavailable (%s); /api/stt will 503", e)
        return None


def mount(app, *, tts, stt, session_active=None, on_boot_progress=None,
          on_reprompt_stage=None) -> None:
    """Attach /api/tts, /api/stt, /api/stt/health to the app.

    ``session_active``: optional zero-arg callable; while it returns True, /api/tts
    answers 409 (the roleplay session owns the speaker - see the interlock below).

    ``on_boot_progress``: optional zero-arg callable, invoked when the kiosk
    relays a ONE-SHOT ``pokamind_emori_dbg`` opening/warmup lifecycle stage.
    The server wires it to the driver's activity clock: a slow Pokamind boot
    (warmup can take 30 s or more) emits these stages while nothing has
    spoken yet, and without them the blind walk-away timer reads a visibly
    progressing boot as an absent learner. Per-frame types (VAD, QR ticks)
    never reach this callback, so a live conversation's walk-away detection
    is untouched.
    """
    from fastapi import Request
    from fastapi.responses import FileResponse, JSONResponse

    @app.get("/api/tts")
    async def api_tts(text: str = "", lang: str = "en"):
        # HARD interlock, not a convention: during a live roleplay session the only voice
        # allowed out of the robot is the session's own TTS. Every on-robot line
        # (menus, greetings, completion feedback) happens strictly BEFORE session start or
        # AFTER finalize, so a synth request mid-session is by definition a bug - refuse
        # it here rather than trust every current and future caller (requirement:
        # Piper must never operate during the roleplay).
        if callable(session_active) and session_active():
            return JSONResponse({"error": "tts blocked during live session"},
                                status_code=409)
        if tts is None or not text.strip():
            return JSONResponse({"error": "tts unavailable"}, status_code=503)
        try:
            # to_thread: synthesis is blocking CPU/disk work and this is the event
            # loop. wait_for bounds it -- the worker itself cannot be cancelled, so a
            # genuinely wedged engine leaks one thread, which is strictly better than
            # a kiosk that never gets an answer.
            path = await asyncio.wait_for(
                asyncio.to_thread(tts.synth_path, text, lang), _TTS_SYNTH_TIMEOUT_S)
        except asyncio.TimeoutError:
            log.warning("TTS timed out after %.0fs; answering 503 so the page falls back",
                        _TTS_SYNTH_TIMEOUT_S)
            path = None
        except Exception as e:  # noqa: BLE001 - engine failure must not 500 the kiosk
            log.warning("TTS failed: %s", e)
            path = None
        if not path:
            return JSONResponse({"error": "tts failed"}, status_code=503)
        return FileResponse(path, media_type="audio/wav")

    @app.get("/api/stt/health")
    def api_stt_health():
        return {"available": stt is not None}

    # In-memory ring buffer of recent endpointer telemetry so it can be READ back over HTTP
    # (GET) for live debugging, not just logged. Bounded; thread-safe enough for diagnostics.
    import collections as _collections
    import time as _time
    _dbg_ring = _collections.deque(maxlen=2000)
    # Types that repeat forever at frame rate: ring only, never the journal.
    _QUIET_DEBUG_TYPES = {"pokamind_emori_vad", "emori_qr"}

    @app.post("/api/emori-debug")
    async def api_emori_debug(request: Request):
        # Diagnostic sink: the in-iframe endpointer POSTs its state (lifecycle + per-frame VAD)
        # here so it is visible in the hostapp log AND readable via GET for live debugging.
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {"type": "emori_debug_unparseable"}
        if isinstance(body, dict):
            body = dict(body)
        else:
            # A `type` so the journal still SAYS a malformed post arrived (`raw`
            # is ring-only -- journal_safe_debug does not render it, precisely
            # because an unknown sender chose its contents).
            body = {"type": "emori_debug_non_object", "raw": body}
        body["_t"] = round(_time.monotonic(), 3)
        _dbg_ring.append(body)
        # Per-frame VAD and per-tick QR stats are high-rate and never stop, so they stay in
        # the ring (readable on demand via GET) and out of the journal. Everything else --
        # lifecycle stages, an actual badge decode -- is an event worth a line.
        if body.get("type") not in _QUIET_DEBUG_TYPES:
            log.warning("[EMORI-DEBUG] %s", journal_safe_debug(body))
        # A one-shot opening/warmup stage is PROOF the session boot is progressing:
        # refresh the walk-away clock so a slow Pokamind boot is not misread as an
        # absent learner. Bounded by construction -- each stage fires once per boot,
        # so a genuinely wedged boot stops emitting them and the timer still ends it
        # 45s after the last stage. High-rate types were filtered out above.
        if (callable(on_boot_progress) and body.get("type") == "pokamind_emori_dbg"
                and str(body.get("stage", "")).startswith(("opening-", "warmup-"))):
            try:
                on_boot_progress()
            except Exception:  # noqa: BLE001 - telemetry must never 500 the kiosk
                pass
        # The iframe's spoken re-prompt lifecycle (emori_cmd:reprompt): 'reprompt-start'
        # means its mic is already hard-muted, so the robot may move without its servo
        # noise landing on an open microphone; skipped/failed means no voice is coming.
        stage = str(body.get("stage", ""))
        if (callable(on_reprompt_stage) and body.get("type") == "pokamind_emori_dbg"
                and stage in ("reprompt-start", "reprompt-skipped", "reprompt-failed")):
            try:
                on_reprompt_stage(stage)
            except Exception:  # noqa: BLE001 - telemetry must never 500 the kiosk
                pass
        return {"ok": True}

    @app.get("/api/emori-debug")
    async def api_emori_debug_read(n: int = 80):
        # Read the last n telemetry events (newest last). For live endpointer diagnosis.
        items = list(_dbg_ring)
        return {"count": len(items), "events": items[-max(1, min(n, 2000)):]}

    @app.post("/api/stt")
    async def api_stt(request: Request, lang: str = "en", grammar: str = ""):
        if stt is None:
            return JSONResponse({"error": "stt unavailable"}, status_code=503)
        pcm = await request.body()
        if len(pcm) < 3200:  # <0.1 s of 16 kHz s16le: ignore
            return {"text": ""}
        dur_ms = len(pcm) // (16000 * 2 // 1000)
        try:
            if grammar.strip():
                phrases = [p.strip() for p in grammar.split(",") if p.strip()]
                scorer = getattr(stt, "transcribe_grammar_conf", None)
                if scorer is None:
                    # Engines without scoring (the wake/keyword path) keep the old shape.
                    text = await asyncio.wait_for(
                        asyncio.to_thread(stt.transcribe_grammar, pcm, phrases, lang),
                        _STT_TIMEOUT_S)
                    return {"text": text}
                text, conf = await asyncio.wait_for(
                    asyncio.to_thread(scorer, pcm, phrases, lang), _STT_TIMEOUT_S)
                conf = round(float(conf), 3)
                # The whole menu interaction in one line. A miss is otherwise invisible:
                # the caller gates on conf page-side, so the journal showed only "200 OK"
                # followed by the robot saying it did not catch that -- with no way to
                # tell silence from a word scored below the gate.
                #
                # The SLOT and not the string. The grammar is a closed menu set, so free
                # speech cannot appear -- but a matched word is still something a named
                # staff member said out loud in front of the robot, and this journal is
                # world-readable and outlives the session. An index separates exactly the
                # three cases this line exists for (no match / which option / how well it
                # scored) and writes down nothing anybody uttered.
                slot = phrases.index(text) if text in phrases else -1
                log.warning("menu stt: lang=%s dur_ms=%d slot=%d/%d conf=%.3f", lang,
                            dur_ms, slot, len(phrases), conf)
                return {"text": text, "conf": conf}
            # Open recognition is unconstrained: shape only, never what was said.
            text = await asyncio.wait_for(
                asyncio.to_thread(stt.transcribe, pcm, lang), _STT_TIMEOUT_S)
            log.info("open stt: lang=%s dur_ms=%d chars=%d", lang, dur_ms, len(text))
            return {"text": text}
        except asyncio.TimeoutError:
            log.warning("STT timed out after %.0fs; answering 503 so the page falls back",
                        _STT_TIMEOUT_S)
            return JSONResponse({"error": "stt timeout"}, status_code=503)
        except Exception as e:  # noqa: BLE001
            log.warning("STT failed: %s", e)
            return JSONResponse({"error": "stt failed"}, status_code=503)
