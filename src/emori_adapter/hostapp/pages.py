"""Kiosk HTML pages served by the hostapp.

Templates use ``__PLACEHOLDER__`` tokens substituted in ``server.py``:
``__LANG__`` (ca | en), ``__STRINGS__`` (localized UI chrome, with the
"say one, two, or three" hint rendered from the live menu), ``__PROMPT__`` /
``__OPTIONS__`` (the voice menu), ``__MENU_TUNING__`` (grammar + confidence gate),
``__STT_GATE__`` (landing capture frame-energy gate), ``__DEMO__``.

Speech I/O strategy (identical on the Windows simulator and the CM4):

* TTS out: try ``GET /api/tts`` (server-side Piper -> WAV played by this page through
  Chromium -> the robot speaker); fall back to browser ``speechSynthesis`` when the
  endpoint 404s/503s (e.g. Piper not installed on the dev box).
* STT in: try ``POST /api/stt`` (server-side Vosk; the page captures mic audio and
  posts 16 kHz PCM); fall back to the browser Web Speech API; fall back to buttons.
* jsQR is vendored at ``/static/jsqr.js`` - the robot has no internet for a CDN.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping


def script_json(value: Any) -> str:
    """``value`` as JSON that is safe INSIDE a ``<script>`` element.

    ``json.dumps`` alone is not: a string holding ``</script>`` ends the element and
    whatever follows runs as markup. ``<``, ``>`` and ``&`` are written as JSON
    unicode escapes (the same string to JavaScript), and so are U+2028 and U+2029,
    which end a line in older JavaScript; every other character stays as it is.
    """

    return (json.dumps(value, ensure_ascii=False)
            .replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def fill(template: str, values: Mapping[str, str]) -> str:
    """``template`` with every ``__TOKEN__`` key of ``values`` replaced in ONE pass.

    A chain of ``str.replace`` calls re-scans what an earlier one inserted, so a menu
    name holding the text ``__OPTIONS__`` would have another value spliced INTO its
    JSON string. Here a substituted value is never searched again. Tokens not in
    ``values`` are left as they are.
    """

    if not values:
        return template
    pattern = re.compile("|".join(re.escape(key) for key in
                                  sorted(values, key=len, reverse=True)))
    return pattern.sub(lambda match: values[match.group(0)], template)


UI_STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "show_badge": "\U0001F44B Show your badge to begin",
        "which_session": "\U0001F399️ Which session?",
        "looking": "looking for a badge…",
        "cam_unavailable": "camera unavailable — tap a topic or practice anonymously",
        "anon_button": "practice anonymously",
        "didnt_catch": "Sorry, I didn't catch that.",
        "choose": "Please choose a topic.",
        "starting": "Starting",
        "heard": "heard",
        "welcome": "Welcome.",
        "hello": "Hello",
        "say_numbers": "Say {numbers}.",
        "hold_badge": "Hold the badge inside the box",
        "too_dark": "Too dark to read the badge — add light, or just say {numbers}",
        "light_label": "light",
        "voice_unavailable": "Voice not available — tap a topic below.",
        "mic_constrained": "Microphone can't be set up safely — tap a topic below.",
        "menu_giving_up": "I didn't catch a topic. Show your badge again, or tap a topic below.",
        "session_over": "Session complete. Thank you!",
        "badge_refused": "That badge did not start a session. Please try again.",
        "session_start_failed": "Sorry, the session did not start. Please show your badge again.",
        "demo_note": "DEMO mode: synthetic session (no real Pokamind).",
    },
    "ca": {
        "show_badge": "\U0001F44B Mostra la teva acreditació per començar",
        "which_session": "\U0001F399️ Quina sessió?",
        "looking": "cercant una acreditació…",
        "cam_unavailable": "càmera no disponible — toca un tema o practica de manera anònima",
        "anon_button": "practica de manera anònima",
        "didnt_catch": "Perdona, no ho he entès.",
        "choose": "Si us plau, tria un tema.",
        "starting": "Comencem amb",
        "heard": "he sentit",
        "welcome": "Et donem la benvinguda.",
        "hello": "Hola",
        "say_numbers": "Digues {numbers}.",
        "hold_badge": "Posa l'acreditació dins del requadre",
        "too_dark": "Massa fosc per llegir l'acreditació — encén el llum, o digues {numbers}",
        "light_label": "llum",
        "voice_unavailable": "Veu no disponible — toca un tema a sota.",
        "mic_constrained": "No es pot configurar el micròfon correctament — toca un tema a sota.",
        "menu_giving_up": "No he entès cap tema. Torna a mostrar l'acreditació o toca un tema a sota.",
        "session_over": "Sessió completada. Gràcies!",
        "badge_refused": "Aquesta acreditació no ha iniciat cap sessió. Torna-ho a provar.",
        "session_start_failed": "Ho sento, la sessió no s'ha iniciat. Torna a mostrar l'acreditació.",
        "demo_note": "Mode DEMO: sessió sintètica (sense Pokamind real).",
    },
}


# ``{numbers}`` in the copy above is the list of number words the CURRENT menu offers.
# It is substituted, not written out, because the menu size is deployment data: a
# course can have six modules, and a hint frozen at "u, dos o tres" makes the other
# three unreachable to anyone who reads (or is read) the screen.
_NUMBERS_TOKEN = "{numbers}"
# The conjunction before the last number, per kiosk language. English also takes the
# serial comma it is already written with ("one, two, or three"); Catalan does not.
_NUMBER_CONJUNCTION = {"en": "or", "ca": "o"}


def _join_numbers(words: list[str], lang: str) -> str:
    if not words:
        return ""
    if len(words) == 1:
        return words[0]
    conjunction = _NUMBER_CONJUNCTION.get(lang, "or")
    lead = ", " if lang == "en" and len(words) > 2 else " "
    return ", ".join(words[:-1]) + lead + conjunction + " " + words[-1]


# The line the /session page speaks while Pokamind is loading behind it.
#
# Spoken by the LANDING page, with navigation waiting for the sentence to finish
# (`say(line, go)`), it would cost about 3 s of dead time at the front of the
# session-start wait. Nor can the landing page simply stop waiting:
# it is a Piper WAV in an <audio> element on a document `location.href` is about to
# destroy, and `GET /session` is answered by this same local process in milliseconds, so
# the new document commits almost immediately and the old page's audio would be cut
# mid-word. Speaking it HERE is the only version that both starts the load at once and
# delivers a whole sentence: /session is the document that survives, it already has the
# same say()/stopSpeech ladder, and the kiosk Chromium already plays its summary and its
# give-up lines with no user gesture (--autoplay-policy=no-user-gesture-required).
#
# Because the line now covers a load instead of delaying one, LENGTH IS FREE: the session's
# own first audio is ~16 s away (measured), so a ~6 s sentence fills dead air it cannot
# collide with. Per kiosk language, and truthful -- it really is setting the session up.
START_COVER: dict[str, str] = {
    "en": "Starting {topic}. Give me a moment while I set everything up, "
          "and we'll begin as soon as it's ready.",
    "ca": "Comencem amb {topic}. Dona'm un moment mentre ho preparo tot, "
          "i començarem tan bon punt estigui a punt.",
}
_TOPIC_TOKEN = "{topic}"


def start_cover_line(lang: str, topic: str) -> str:
    """The covering announcement for a session that is loading, or ``""`` for silence.

    ``topic`` is the module name the visitor actually chose (``SessionInfo.topic``,
    resolved from the LIVE menu server-side). An empty topic returns an empty line: the
    badge-direct and walk-in paths never announced anything, and inventing a sentence
    for a session nobody picked a topic for would be new behaviour, not a latency fix.
    """

    name = (topic or "").strip()
    if not name:
        return ""
    code = lang if lang in START_COVER else "en"
    return START_COVER[code].replace(_TOPIC_TOKEN, name)


def strings_for(lang: str, numbers: list[str] | None = None) -> str:
    """Localized UI chrome as the page's ``S`` object.

    ``numbers`` are the spoken number words the live menu offers
    (``VoiceMenu.spoken_numbers``); omit it and the built-in three-option menu's words are
    used, which is what every non-menu page (``/identify``, ``/manual``, ``/session``)
    would show anyway. Empty falls back the same way rather than rendering "Say ." -- a
    language with no number words is a misconfiguration, not a reason to ship broken copy.
    """
    from emori_adapter.hostapp.voice_topics import VoiceMenu

    code = lang if lang in UI_STRINGS else "en"
    if not numbers:
        numbers = VoiceMenu.default_menu().spoken_numbers(code)
    joined = _join_numbers(numbers, code)
    return script_json({key: value.replace(_NUMBERS_TOKEN, joined)
                        for key, value in UI_STRINGS[code].items()})


# Shared speech helpers injected into pages that speak/listen. say() prefers the
# server-side Piper WAV (the robot-speaker path); listen prefers server-side Vosk.
_SPEECH_JS = """
const LANG=__LANG__, S=__STRINGS__;
const LANG_BCP={en:'en-US',ca:'ca-ES'}[LANG]||'en-US';
// Every Audio say() creates is tracked so a page can SILENCE it. The robot voice is a
// Piper WAV in an <audio> element, and a page that navigates away does not reliably stop
// one that is mid-playback: the landing page's "Starting <topic>" line (~4.4 s) could
// still be talking over Pokamind's own opening line on the robot's SINGLE speaker.
// speechSynthesis.cancel() alone never fixed that - it only stops the browser-TTS fallback branch,
// not the WAV.
let __sayAudio=[];
// Silencing a say() that has not produced sound YET takes more than stopSpeech, which only
// pauses <audio> elements that already exist, and the /api/tts fetch behind an unstarted
// line keeps running -- its .then would then open a NEW <audio> and start talking AFTER
// the page asked for silence. Harmless while every stopSpeech() was immediately followed
// by a navigation (the document died first); NOT harmless for the session page, which
// silences its covering line and STAYS. So stopSpeech bumps a generation and say()
// captures it: a request whose generation has moved on produces no sound at all. The
// caller's callback is unaffected -- it still arrives from say()'s own arm() deadline, so
// every state machine that only advances in that callback advances exactly as before.
let __sayGen=0;
// How many lines are between say() and their callback, so a page can tell it is speaking.
let __sayOpen=0;
function stopSpeech(){
  __sayGen++;
  const live=__sayAudio; __sayAudio=[];
  live.forEach(a=>{ try{ a.onended=null; a.onerror=null; a.onloadedmetadata=null; a.pause();
    const src=a.src; a.removeAttribute('src'); a.load();
    if(src&&src.indexOf('blob:')===0) URL.revokeObjectURL(src); }catch(e){} });
  try{ speechSynthesis.cancel(); }catch(e){}
}
// Upper bound on how long `text` can take to speak. Used ONLY as the "say() never called
// back at all" fail-safe: 95 ms/char is ~1.4x the measured Piper ca/en delivery rate, and
// the 5 s floor covers short lines where fixed synthesis + blob load dominates. Callers
// that must not navigate over an announcement derive THEIR deadline from this same
// function so theirs can never fire first.
function sayMaxMs(text){ return Math.max(5000,(text||'').length*95); }
// `onstart` (optional) fires when this line becomes AUDIBLE -- the <audio> actually
// playing, or the browser-TTS fallback actually starting. A caller that must decide
// whether cutting the line would clip a word cannot use the call time for that: the
// /api/tts round-trip and Piper synthesis sit between the two, and on the CM4 that is
// hundreds of ms. Nothing else changes for callers that pass one or two arguments.
function say(text,cb,onstart){
  // Registered against the CURRENT silence generation: a stopSpeech() between here and
  // the fetch resolving means this line never opens a mouth (see stopSpeech above).
  const gen=__sayGen;
  const began=()=>{ try{ onstart&&onstart(); }catch(e){} };
  // The completion latch is SHARED with the fetch handlers below (fin.done), not
  // private to the timer: whoever gets there first ends this say(), and the losers
  // must produce no sound at all. A .then that opened an <audio> after the caller
  // had already advanced would be talking over whatever state came next - on the
  // landing page, over the microphone listen() just opened.
  __sayOpen++;
  const fin=(function(){const f=()=>{ if(f.done) return; f.done=true; __sayOpen--; cb&&cb(); };
    f.done=false; return f;})();
  let timer=null;
  const arm=(ms)=>{ try{ clearTimeout(timer); }catch(e){} timer=setTimeout(fin,ms); };
  // Armed BEFORE the fetch, and the request itself is bounded. With a deadline INSIDE
  // .then, a /api/tts that never answered (a wedged Piper synth) would mean say() never
  // called back at all -- and every
  // caller here is a state machine that only advances in that callback (listen after
  // the menu, startCam after the give-up line, goHome after the completion line). It is
  // STRICTLY longer than the 8 s abort below: sayMaxMs floors at 5000, so any line under
  // ~53 characters would otherwise pre-arm at exactly 8000 too and -- being registered
  // first -- fire before the abort could route into .catch.
  arm(Math.max(sayMaxMs(text),8000)+3000);
  // AbortSignal.timeout is Chromium 103+; an older engine would throw HERE, before the
  // .catch below exists, and take the whole speech ladder down with it. That engine also
  // leaves the fetch unbounded, which is the other reason both handlers check fin.done.
  const opts=(typeof AbortSignal!=='undefined'&&AbortSignal.timeout)
    ?{signal:AbortSignal.timeout(8000)}:undefined;
  fetch('/api/tts?lang='+LANG+'&text='+encodeURIComponent(text),opts)
    .then(r=>{ if(!r.ok) throw 0; return r.blob(); })
    .then(b=>{ if(fin.done||gen!==__sayGen) return;   // moved on, or silenced: stay silent
      const a=new Audio(URL.createObjectURL(b)); __sayAudio.push(a);
      a.onended=fin; a.onerror=fin; a.onplay=began;
      // The length heuristic is only needed until the decoder knows the REAL duration;
      // re-arm from it so a line longer than the estimate is never cut off mid-word.
      a.onloadedmetadata=()=>{ if(isFinite(a.duration)&&a.duration>0) arm(a.duration*1000+750); };
      a.play().catch(()=>{ throw 0; }); arm(sayMaxMs(text)); })
    .catch(()=>{ if(fin.done||gen!==__sayGen) return;   // ...browser TTS is speech too
      try{ speechSynthesis.cancel();
      const u=new SpeechSynthesisUtterance(text); u.lang=LANG_BCP; u.onend=fin;
      u.onstart=began;
      speechSynthesis.speak(u); arm(Math.max(3000,text.length*70)); }
      catch(e){ fin(); } });
}
"""

_STT_JS = """
// --- listening: server Vosk first, Web Speech fallback, buttons last ---
let sttMode=null, rec=null, audioCtx=null, micStream=null, proc=null, pcmBuf=[], sttTimer=null;
// Landing capture: the landing mic is EXPLICITLY constrained. A bare {audio:true} let the whole room
// in; worse, a bare `echoCancellation:true` is only an *ideal* hint the browser may
// silently drop, so EC is `exact` (required). This is keyword spotting, not prosody, so
// NS/AGC are welcome. STT_GATE is the frame-energy gate config (server _stt_gate_json).
const STT_GATE=__STT_GATE__;
const MIC_CONSTRAINTS={audio:{echoCancellation:{exact:true},noiseSuppression:true,
  autoGainControl:true,channelCount:{ideal:1},sampleRate:{ideal:16000}}};
function micHardFail(code,detail){
  // A constraint the browser cannot honour is a HARD, VISIBLE failure - never a silent
  // relaxed retry. The DURABLE channels are the __emoriMicError global and the debug POST
  // below (speech_api logs every event whose type != 'pokamind_emori_vad' at WARNING and
  // serves it back on GET /api/emori-debug?n=). The on-screen string is BEST-EFFORT ONLY:
  // the hand-back re-opens the scanner, so a badge still held in frame re-enters onBadge
  // ~1s later and overwrites it with the greeting. Diagnose from the ring, not the screen.
  window.__emoriMicError=code; sttMode=null; stopListen();
  try{ fetch('/api/emori-debug',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({type:'emori_mic_constraints',code:code,constraint:detail||''})}); }catch(e){}
  // getUserMedia can reject AFTER a topic button already started a session: beginSession
  // sets `navigating` and releases the webcam so the Pokamind session can claim it. The
  // hand-back below would then re-acquire that webcam mid-navigation - the device-busy /
  // NotReadableError race that release exists to avoid - and would replace the
  // "Starting <topic>..." line. The durable diagnostic is already recorded, so stop here.
  if(navigating) return;
  try{ msg.textContent=S.mic_constrained; }catch(e){}
  // ...otherwise HAND THE KIOSK BACK to the badge scanner. stopListen() has just cleared
  // the menu's listen deadline while phase is still 'listen', so onMiss() -> giveUp() - the path
  // that re-opens the camera - can never fire; reset the miss counter, return to 'scan'
  // and re-open the camera here instead. The already-identified badge (qr) is KEPT: this
  // is the SAME present person the mic just failed for, so their next topic-button tap
  // (buttons need no mic) must stay attributed to them, not silently anonymised. An ACTUAL
  // new visitor is still re-identified authoritatively - the re-opened camera's onBadge
  // overwrites qr with a different badge, and the explicit anonymous button is onBadge('')
  // - exactly as the normal identified state already behaves. lastQr is dropped so
  // re-showing the SAME badge is not swallowed by the 3s debounce. NOT giveUp(): it would
  // speak a ~4s give-up line nobody earned. sttMode is null now, so a later listen() takes
  // the voice_unavailable ladder rather than re-entering getUserMedia - a permanently
  // unsupported constraint cannot loop.
  tries=0; phase='scan'; lastQr=''; lastQrAt=0;
  try{ startCam(); }catch(e){}
}
async function initStt(){
  try{ const r=await fetch('/api/stt/health'); if(r.ok && (await r.json()).available){ sttMode='server'; return; } }catch(e){}
  if(window.SpeechRecognition||window.webkitSpeechRecognition){ sttMode='webspeech'; return; }
  sttMode=null;
}
// listenGen identifies the CURRENT listen window. Every open (listen) and every teardown
// (stopListen) bumps it, so any /api/stt or /api/voice-select round-trip that resolves
// after its window closed can recognise itself as stale and drop out. Without it a slow
// Vosk decode on the CM4 (a 3.5s chunk can take ~1s) resolves AFTER the listen deadline
// already fired -> a second onMiss() that tears down the mic opened 200ms earlier, or a
// select() from audio captured before the re-prompt.
let listenTimer=null, listenGen=0;
function listen(){ if(navigating||phase==='listen') return; phase='listen'; listenGen++;
  armListenDeadline();
  if(sttMode==='server'){ startServerListen(); }
  else if(sttMode==='webspeech'){ startRec(); }
  else { msg.textContent=S.voice_unavailable; } }
// Menu grammar gate: with a constrained grammar, unrecognised speech comes back as EMPTY text (Vosk
// resolves it all to [unk]), so nothing would ever advance the miss counter and a
// screenless kiosk would listen forever. This deadline funnels silence into the same
// re-prompt path as a low-confidence hit.
function armListenDeadline(){ try{ if(listenTimer) clearTimeout(listenTimer); }catch(e){}
  listenTimer=setTimeout(()=>{ listenTimer=null;
    if(!navigating&&phase==='listen') onMiss(); }, MENU.listenMs); }
function stopListen(){ listenGen++; resetWindow();
  try{ if(listenTimer){ clearTimeout(listenTimer); listenTimer=null; } }catch(e){}
  try{ if(rec){ rec.onend=null; rec.stop(); rec=null; } }catch(e){}
  try{ if(sttTimer){ clearInterval(sttTimer); sttTimer=null; } }catch(e){}
  // onaudioprocess is DETACHED before the disconnect, not just disconnected: an event the
  // audio thread already queued is still delivered afterwards, and this teardown is about
  // to null audioCtx underneath it. One of three independent guards; see startServerListen.
  try{ if(proc){ proc.onaudioprocess=null; proc.disconnect(); proc=null; } }catch(e){}
  try{ if(micStream){ micStream.getTracks().forEach(t=>t.stop()); micStream=null; } }catch(e){}
  try{ if(audioCtx){ audioCtx.close(); audioCtx=null; } }catch(e){}
}
async function startServerListen(){
  let stream=null;
  try{ stream=await navigator.mediaDevices.getUserMedia(MIC_CONSTRAINTS); }
  catch(e){
    const n=(e&&e.name)||'';
    if(n==='OverconstrainedError'||n==='NotSupportedError'){
      micHardFail('mic_constraints_unsupported',(e&&e.constraint)||''); return; }
    // permission/no-device: the pre-existing ladder (Web Speech -> buttons)
    sttMode=(window.SpeechRecognition||window.webkitSpeechRecognition)?'webspeech':null;
    if(sttMode==='webspeech') startRec(); else msg.textContent=S.voice_unavailable;
    return;
  }
  // Accepting `exact` is not proof it was applied: verify what we actually got.
  const trk=stream.getAudioTracks()[0];
  const cfg=(trk&&trk.getSettings)?trk.getSettings():{};
  if(cfg.echoCancellation===false){ micStream=stream;
    micHardFail('mic_echo_cancellation_ignored','echoCancellation'); return; }
  micStream=stream;
  try{
    audioCtx=new (window.AudioContext||window.webkitAudioContext)();
    const srcN=audioCtx.createMediaStreamSource(micStream);
    proc=audioCtx.createScriptProcessor(4096,1,1);
    srcN.connect(proc); proc.connect(audioCtx.destination);
    pcmBuf=[]; resetWindow();
    // The capture rate is read ONCE, HERE, into the closure -- NEVER off `audioCtx` from
    // inside the callback. stopListen() sets audioCtx=null while this handler can still be
    // installed (the audio thread delivers events queued before the disconnect), and
    // `audioCtx.sampleRate` at the CALL SITE sits OUTSIDE noteCapture's own try/catch: it
    // threw an uncaught TypeError inside the audio callback on every teardown. Three
    // independent guards now, so no single future edit can put it back:
    //   1. capRate  - the handler dereferences nothing that teardown nulls,
    //   2. capGen   - every listen()/stopListen() bumps listenGen, so a late event is
    //                 inert and cannot contaminate the NEXT window's buffer either,
    //   3. stopListen() detaches proc.onaudioprocess before disconnecting it.
    const capRate=audioCtx.sampleRate, capGen=listenGen;
    proc.onaudioprocess=e=>{ if(capGen!==listenGen) return;
      const ch=e.inputBuffer.getChannelData(0);
      pcmBuf.push(new Float32Array(ch)); noteCapture(ch, capRate); };
    // The BACKSTOP, not the trigger (B). As the only flush it would make every answer
    // wait out the rest of a 3.5 s window, ~1,750 ms of pure dead time on average.
    // noteCapture() flushes
    // on the voiced->silence EDGE; this interval stays exactly as it was so a room that
    // never falls silent (or an energy floor that reports nothing voiced at all) still
    // gets its audio to Vosk on the old schedule instead of wedging the menu.
    sttTimer=setInterval(flushStt, MENU.flushMs);
  }catch(e){ stopListen(); armListenDeadline();  // stopListen releases the half-built
    // capture chain but also clears the listen deadline; re-arm it or the Web Speech
    // fallback below would listen forever on silence (the very bug the deadline prevents).
    sttMode=(window.SpeechRecognition||window.webkitSpeechRecognition)?'webspeech':null;
    if(sttMode==='webspeech') startRec(); else msg.textContent=S.voice_unavailable; }
}
function toPcm16k(chunks, fromRate){
  let n=0; for(const c of chunks) n+=c.length;
  const all=new Float32Array(n); let o=0; for(const c of chunks){ all.set(c,o); o+=c.length; }
  const ratio=fromRate/16000, outN=Math.floor(all.length/ratio), out=new Int16Array(outN);
  for(let i=0;i<outN;i++){ const v=all[Math.floor(i*ratio)];
    out[i]=Math.max(-32768,Math.min(32767,Math.round(v*32767))); }
  return out;
}
function voicedMs16k(pcm){
  // Energy gate: how many ms of this window clear the noise floor, measured per 20 ms frame on
  // the already-downsampled 16 kHz s16le buffer. Peak-insensitive (RMS), so one door
  // slam does not open the gate and a quiet steady hum never does.
  const n=Math.max(1,Math.round(16*STT_GATE.frame_ms));   // 16 samples per ms @ 16 kHz
  const floor=Math.pow(10,STT_GATE.dbfs/20)*32768;
  let ms=0;
  for(let i=0;i+n<=pcm.length;i+=n){ let s=0;
    for(let j=i;j<i+n;j++){ s+=pcm[j]*pcm[j]; }
    if(Math.sqrt(s/n)>=floor) ms+=STT_GATE.frame_ms; }
  return ms;
}
function voicedMs(pcm){
  const ms=voicedMs16k(pcm);
  // Breadcrumb: a floor set too high and "nobody spoke" are indistinguishable on a
  // screenless robot - both just yield didnt_catch. The last observed value is readable
  // over CDP as window.__emoriVoicedMs, so the on-robot floor can be derived from the
  // deployed page without a POST per dropped window (which would spam the debug ring).
  // It stays a WINDOW measurement: the per-chunk edge detector below deliberately calls
  // voicedMs16k, not this, so the number an operator reads still means what it meant.
  window.__emoriVoicedMs=ms;
  return ms;
}
// B: END-OF-SPEECH FLUSH. The same energy gate, the same dBFS floor and the same 20 ms RMS
// frames as the window gate above -- run per captured chunk (~85 ms at 48 kHz/4096) so
// the page can see the moment the visitor STOPS talking instead of waiting out a fixed
// 3.5 s window. Two counters, both in ms of real audio:
//   capVoicedMs   voiced ms accumulated since the last flush,
//   capSilenceMs  UNVOICED ms since the last voiced frame -- and it only starts counting
//                 once something was voiced, so leading room tone never trips the edge.
// The edge needs BOTH: eosMinVoicedMs of speech (a cough or a door bang is ~150-300 ms
// and must not flush a near-empty buffer) and eosSilenceMs of trailing silence (a visitor
// who hesitates mid-phrase -- "uh... three" -- must not have their answer cut in half).
// Declining to fire is always safe: the backstop interval still flushes the same buffer
// on the pre-existing 3.5 s schedule, and flushStt applies the energy window gate after that.
// eosSilenceMs=0 disables the edge entirely (backstop-only, the old page).
//
// AND AN EDGE FLUSH MAY ONLY EVER ACCEPT (see flushStt). It is a GUESS that the visitor
// has finished; a guess must not be able to spend something the visitor owns. Measured:
// intermittent room chatter (0.43 s bursts, 0.77 s gaps) clears eosMinVoicedMs on every
// burst, so the edge posted ~3x per second-and-a-bit where the old page posted once per
// 3.5 s -- and each sub-confidence answer was an onMiss(), which tears down the mic and
// burns one of only 3 tries. All three went in 3.6 s and the kiosk gave up on a visitor
// who had not spoken yet. Confidence cannot separate 430 ms of babble from 430 ms of
// "three", so no threshold fixes this; what fixes it is that the edge never judges. Every
// NEGATIVE judgement (low confidence, a non-exact match) stays exactly where it was
// before B: on the backstop window and the listen deadline, at the old rate. So the edge
// can still only make things EARLIER -- never more often, and never worse.
let capVoicedMs=0, capSilenceMs=0;
// Per-mic-session state, distinct from the per-flush counters above:
//   edgeSpent   one speculative post per backstop period. Without it a chattering room
//               re-posts a growing buffer every ~1 s; a 3.5 s Vosk decode costs ~1 s of
//               CM4 CPU, so the cap keeps the load at most 2x the old page's.
//   gateJudged  how many backstop windows the energy gate has already judged in this
//               mic session -- the escape hatch in flushStt reads it.
let edgeSpent=false, gateJudged=0;
function resetCapture(){ capVoicedMs=0; capSilenceMs=0; }
function resetWindow(){ resetCapture(); edgeSpent=false; gateJudged=0; }
function noteCapture(chunk, rate){
  // Anything thrown here runs inside onaudioprocess, i.e. on EVERY capture callback; a
  // throw must never be able to take the buffer with it. The chunk is already in pcmBuf.
  try{
    if(!(MENU.eosSilenceMs>0) || !rate) return;
    const ms=(chunk.length/rate)*1000;
    const voiced=voicedMs16k(toPcm16k([chunk], rate));
    if(voiced>0){ capVoicedMs+=voiced; capSilenceMs=0; }
    else if(capVoicedMs>0){ capSilenceMs+=ms; }
    // Same diagnosis story as __emoriVoicedMs: on a screenless robot "the edge never fires"
    // and "nobody spoke" look identical, so the two counters are readable over CDP.
    window.__emoriCapture={voiced:Math.round(capVoicedMs),silence:Math.round(capSilenceMs)};
    if(capVoicedMs>=MENU.eosMinVoicedMs && capSilenceMs>=MENU.eosSilenceMs) flushStt(true);
  }catch(e){}
}
// `edge` = this flush is the end-of-speech GUESS, not the backstop window. setInterval
// calls flushStt with no arguments, so the backstop is `undefined` -> falsy by
// construction. The two differ in exactly two ways, both above:
//   * an edge may only ACCEPT (never onMiss, never a non-exact select),
//   * an edge does not CONSUME the window -- the audio stays in pcmBuf so the backstop
//     still judges the whole thing on the old schedule. Only the backstop drains it.
async function flushStt(edge){
  if(navigating||!pcmBuf.length||!audioCtx) return;
  if(edge&&edgeSpent) return;         // one speculative post per backstop period
  const pcm=toPcm16k(pcmBuf, audioCtx.sampleRate);
  // MEASURED, NOT DESTROYED. This check runs BEFORE `pcmBuf=[]`: after it, a flush shorter
  // than 0.5 s would throw the audio away and leave the backstop nothing to carry. The
  // edge makes such a flush reachable
  // (EMORI_MENU_EOS_SILENCE_MS=200 / _MIN_VOICED_MS=120 flushes at ~0.3 s), and a
  // screenless visitor's answer would simply vanish. The buffer is kept and keeps
  // growing; only the edge counters reset, so the edge re-arms on the next silence.
  if(pcm.length<8000){ resetCapture(); return; } // <0.5s of audio: nothing meaningful said
  // Energy window gate: is any of this above the noise floor? ...
  let gated=(STT_GATE.min_voiced_ms>0 && voicedMs(pcm)<STT_GATE.min_voiced_ms);
  // ...AND ITS ESCAPE HATCH. The gate reads an ABSOLUTE dBFS floor, so one bad
  // EMORI_STT_ENERGY_DBFS (-5 instead of -50) makes voicedMs return 0 for everything --
  // including someone shouting at the robot. The edge cannot fire at all then (it needs a
  // voiced frame), the backstop fires but every window is dropped here, and nothing ever
  // reaches Vosk: a screenless menu deafened by one typo, which is the failure this
  // project keeps hitting. So the gate may skip windows, but never the FIRST window of a
  // mic session -- the one the answer to the prompt just spoken is overwhelmingly in. Cost
  // when the floor is fine: nothing (a real answer passes the gate, so no bypass happens).
  // Cost in a genuinely silent room: one Vosk decode of room tone per listen window, which
  // a constrained grammar resolves to [unk] -> empty text -> no action. Counted for CDP.
  if(gated&&!edge&&!gateJudged){ gated=false;
    window.__emoriGateBypass=(window.__emoriGateBypass||0)+1; }
  if(edge){ edgeSpent=true; }
  else { gateJudged++; edgeSpent=false; pcmBuf=[]; }   // the backstop OWNS its window
  resetCapture();
  if(gated) return;                                   // room tone
  let url='/api/stt?lang='+LANG;
  if(MENU.grammar) url+='&grammar='+encodeURIComponent(MENU.grammar);
  const gen=listenGen;
  try{ const r=await fetch(url,{method:'POST',
      headers:{'content-type':'application/octet-stream'},body:pcm.buffer});
    if(!r.ok) return; const d=await r.json();
    // STALE GUARD: the listen deadline (or a button/badge/session start) can fire while
    // this POST is in flight. Acting on the answer then would either double-count a miss
    // AND kill the freshly opened mic, or start a session from pre-re-prompt audio.
    if(navigating||phase!=='listen'||gen!==listenGen) return;
    if(!d.text) return;  // all [unk]: the listen deadline handles it, not a miss yet
    // A constrained grammar FORCES stray speech onto a menu word, so an UNSCORED
    // accept is more dangerous than open recognition. Below the gate is a miss, never a
    // selection. Open recognition sends no conf -> treated as 1 (no confidence gate).
    const conf=(d.conf===undefined)?1:d.conf;
    // ...unless this was the EDGE guessing. Then it is not a miss, it is a guess that did
    // not land: the audio is still in pcmBuf and the backstop will judge the whole window
    // on the old schedule, so the visitor keeps their try and their open mic.
    if(conf<MENU.confMin){ if(!edge) onMiss(); return; }
    msg.textContent=S.heard+': “'+d.text+'”'; select(d.text, edge); }catch(e){}
}
function startRec(){ const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
  if(!SR){ msg.textContent=S.voice_unavailable; return; }
  try{ rec=new SR(); rec.lang=LANG_BCP; rec.interimResults=false; rec.maxAlternatives=4;
    rec.onresult=e=>{ const t=Array.from(e.results[0]).map(a=>a.transcript).join(' ');
      msg.textContent=S.heard+': “'+t+'”'; select(t); };
    rec.onerror=e=>{ if(e.error!=='no-speech') msg.textContent='mic: '+e.error; };
    rec.onend=()=>{ if(!navigating&&phase==='listen') setTimeout(startRec,300); };
    rec.start(); }
  catch(e){ /* already started */ } }
"""

# Badge decoding. One jsQR call per tick, passes rotating, so the per-tick cost
# stays bounded on the CM4 while covering the regimes a single pass misses:
#   0 centre crop, native pixels        - good light, cheapest
#   1 crop -> 2x box average -> percentile stretch - very dim + defocused (noise-safe)
#   2 crop -> min/max stretch           - dim + small badge
#   3 whole frame                       - badge off-centre (never worse than before)
# Passes 1 and 2 are skipped outright once the crop is well lit. The vendored jsQR
# binarizer thresholds per 8x8 region and gives up when max-min <= 24
# (MIN_DYNAMIC_RANGE in static/jsqr.js) - that is precisely what an under-exposed
# frame is, and what the stretch passes repair.
_QR_SCAN_JS = """
const QRCFG=(function(){
  const q=(typeof location!=='undefined'&&location.search)?location.search:'';
  const p=new URLSearchParams(q);
  const num=(k,d,lo,hi)=>{const v=parseFloat(p.get(k));return (isFinite(v)&&v>=lo&&v<=hi)?v:d;};
  return {crop:num('qrcrop',0.55,0.2,1.0), dark:num('qrdark',32,0,255),
          lit:num('qrlit',64,0,255), ms:num('qrms',100,0,2000)};
})();
function qrLuma(d,i){ return (d[i]*77+d[i+1]*150+d[i+2]*29)>>8; }
function qrStats(d){ let min=255,max=0,sum=0,n=0;
  for(let i=0;i<d.length;i+=16){ const l=qrLuma(d,i);
    if(l<min)min=l; if(l>max)max=l; sum+=l; n++; }
  return {min:min,max:max,mean:n?sum/n:0,range:max-min}; }
function qrCrop(d,w,h,frac){ const cw=Math.max(1,Math.round(w*frac)),ch=Math.max(1,Math.round(h*frac));
  const x0=(w-cw)>>1, y0=(h-ch)>>1, out=new Uint8ClampedArray(cw*ch*4);
  for(let y=0;y<ch;y++){ const s=((y+y0)*w+x0)*4; out.set(d.subarray(s,s+cw*4), y*cw*4); }
  return {data:out,width:cw,height:ch}; }
function qrHalf(d,w,h){ const w2=w>>1,h2=h>>1,out=new Uint8ClampedArray(w2*h2*4);
  for(let y=0;y<h2;y++) for(let x=0;x<w2;x++){ let a=0;
    for(let dy=0;dy<2;dy++) for(let dx=0;dx<2;dx++) a+=qrLuma(d,((((y<<1)+dy)*w)+((x<<1)+dx))*4);
    const o=(y*w2+x)*4, v=a>>2; out[o]=out[o+1]=out[o+2]=v; out[o+3]=255; }
  return {data:out,width:w2,height:h2}; }
function qrLevels(d,lo,hi){ const hist=new Uint32Array(256); let n=0;
  for(let i=0;i<d.length;i+=4){ hist[qrLuma(d,i)]++; n++; }
  const at=p=>{ let c=0; for(let v=0;v<256;v++){ c+=hist[v]; if(c>=p*n) return v; } return 255; };
  return {min:at(lo),max:at(hi)}; }
function qrStretch(d,min,max){ const out=new Uint8ClampedArray(d.length), k=255/Math.max(1,max-min);
  for(let i=0;i<d.length;i+=4){ const v=(qrLuma(d,i)-min)*k; out[i]=out[i+1]=out[i+2]=v; out[i+3]=255; }
  return out; }
// Screenless observability: the ladder reports itself to on-screen text nobody can read on
// a headless robot, so "the badge does not scan" arrives with no evidence. Summarise each
// second into the hostapp debug ring (type 'emori_qr' is ring-only, never journalled) and
// send a decode immediately as its own event. `tried` is what the ladder actually ran, so
// a pass that never gets a turn is visible; mean/range say whether the frame is even usable.
// Publish the frame the DECODER was handed (same canvas, after drawImage), so "what does
// it see" can be answered without stopping the kiosk to free the camera. Gated server-side
// (403 -> stop trying), memory-only there, and rate-limited to one frame every 3 s.
let qrShotAt=0, qrShotOff=false;
function qrShot(cv,st){
  if(qrShotOff) return;
  const now=(typeof performance!=='undefined')?performance.now():0;
  // 1.2s while scanning: this channel is now the SECOND DECODER, not just debug.
  // zxing-cpp server-side reads hand-held phone badges (small, angled, daylight) that
  // jsQR in this page cannot.
  if(now-qrShotAt<1200) return;
  qrShotAt=now;
  try{ cv.toBlob(b=>{ if(!b) return;
    fetch('/api/qr-frame',{method:'POST',body:b})
      .then(r=>{ if(r.status===403){ qrShotOff=true; return null; } return r.json(); })
      .then(d=>{ if(d&&d.decoded&&phase==='scan'&&!navigating){
        // The SAME badge telemetry the in-page ladder emits. Without it a server-side
        // decode left no trace at all, so "the robot reacted but nothing scanned" had
        // no evidence anywhere - and which of the two readers actually read the badge
        // is the first thing worth knowing when a scan goes wrong. The frame stats are
        // the ones THIS page measured on the frame it uploaded.
        qrTel({pass:'zxing',mean:(st&&st.mean)||0,range:(st&&st.range)||0,ms:0},true);
        onBadge(d.decoded); } })
      .catch(()=>{}); },
    'image/jpeg', 0.7); }catch(e){ qrShotOff=true; }
}
// A scan the SERVER refused (unregistered badge, a lease another card already holds, a
// add-on that cannot hand over) is otherwise completely invisible: the ladder simply
// keeps scanning while the person keeps holding a badge at a robot that has already said
// no. Never the payload - only the server's own content-free error code, which is what
// the opaque 409 body carries (roster-enumeration stance, server.py).
let msgHold=0;
function qrReject(code,text){
  // The scan loop rewrites `msg` on EVERY tick (~10/s), so a bare assignment here is
  // gone before anyone reads it. Hold the line for four seconds; both scan loops and
  // the /identify hand-back honour the latch.
  msgHold=Date.now()+4000;
  try{ msg.textContent=text; }catch(e){}
  try{ fetch('/api/emori-debug',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({type:'emori_qr_reject',code:code||'unknown'})}); }catch(e){}
  // The reviewed cue is a short content-free tone and 404s unless a deployment turned
  // it on (EMORI_UNREGISTERED_BADGE_FEEDBACK); where it IS on, it is the only channel a
  // screenless kiosk has for "that badge did nothing".
  try{ const cue=new Audio('/api/badge-cue'); cueOpen=true;
    cue.onended=cue.onerror=()=>{ cueOpen=false; };
    cue.play().catch(()=>{ cueOpen=false; }); }catch(e){ cueOpen=false; }
}
let qrAcc=null;
function qrTel(r,decoded){
  const now=(typeof performance!=='undefined')?performance.now():0;
  if(decoded){ try{ navigator.sendBeacon('/api/emori-debug', new Blob([JSON.stringify(
      {type:'emori_qr_badge',pass:r.pass,mean:Math.round(r.mean),range:r.range,ms:Math.round(r.ms)})],
      {type:'application/json'})); }catch(e){} return; }
  if(!qrAcc) qrAcc={t0:now,ticks:0,mean:0,range:0,ms:0,tried:{}};
  qrAcc.ticks++; qrAcc.mean+=r.mean; qrAcc.range+=r.range; qrAcc.ms+=r.ms;
  qrAcc.tried[r.pass]=(qrAcc.tried[r.pass]||0)+1;
  if(now-qrAcc.t0<1000) return;
  const n=Math.max(1,qrAcc.ticks);
  const body={type:'emori_qr',ticks:qrAcc.ticks,mean:Math.round(qrAcc.mean/n),
              range:Math.round(qrAcc.range/n),ms:Math.round(qrAcc.ms/n),tried:qrAcc.tried,
              cam:(v&&v.videoWidth)?(v.videoWidth+'x'+v.videoHeight):'none'};
  qrAcc=null;
  try{ navigator.sendBeacon('/api/emori-debug', new Blob([JSON.stringify(body)],
      {type:'application/json'})); }catch(e){}
}
function qrLadder(data,w,h,pass){
  const t0=(typeof performance!=='undefined')?performance.now():0;
  const c=qrCrop(data,w,h,QRCFG.crop), st=qrStats(c.data);
  const opt={inversionAttempts:'attemptBoth'};
  const lit=st.range>=QRCFG.lit;
  let code=null, name='';
  // Cost, measured on the CM4 from the scan telemetry: a FULL-RES whole-frame
  // decode ~1.5 s, the centre crop ~650 ms. The old ladder spent three ticks in four on the
  // full-res whole frame, so the loop managed ~2 looks a second and the centre crop -- the
  // pass that catches a badge held up close -- got a turn only every ~2 s.
  //
  // The two coverage gaps are different: the crop is full-res but only the middle 55%, and
  // a presented badge is often outside it (on the robot one sat entirely above the crop). So
  // keep both, and pay for the whole frame at HALF resolution -- a quarter of the pixels,
  // the same field of view. Small or distant codes still have the full-res crop; off-centre
  // codes now get looked at every cycle instead of every fourth one.
  const whole=()=>{ const hf=qrHalf(data,w,h); return jsQR(hf.data,hf.width,hf.height,opt); };
  if(pass===0){ name='crop'; code=jsQR(c.data,c.width,c.height,opt); }
  else if(pass===1){ if(lit){ name='full/2'; code=whole(); }
    else { name='crop/2+levels'; const hf=qrHalf(c.data,c.width,c.height);
      const lv=qrLevels(hf.data,0.02,0.98);
      code=jsQR(qrStretch(hf.data,lv.min,lv.max),hf.width,hf.height,opt); } }
  else if(pass===2){ if(lit){ name='crop'; code=jsQR(c.data,c.width,c.height,opt); }
    else { name='crop+stretch';
      code=jsQR(qrStretch(c.data,st.min,st.max),c.width,c.height,opt); } }
  else { name='full/2'; code=whole(); }
  const t1=(typeof performance!=='undefined')?performance.now():0;
  return {data:(code&&code.data)?code.data:'', pass:name,
          mean:st.mean, range:st.range, dark:st.range<QRCFG.dark, ms:t1-t0}; }
"""

# Screenless QR-TRIGGERED VERBAL landing (the kiosk page). The robot watches via the
# camera; when a staff QR badge is shown (vendored jsQR), it GREETS the person and speaks
# the topic menu (server Piper -> robot speaker, speechSynthesis fallback), then LISTENS
# (server Vosk -> robot mic, Web Speech fallback) for "one / two / three". The spoken
# choice + badge go to /api/voice-select, which maps the words to a module and returns the
# session URL (Pokamind starts that module, no topic screen). Number buttons remain as a
# quiet-room/accessibility fallback. On the robot the kiosk Chromium is launched with
# --use-fake-ui-for-media-stream (camera+mic auto-granted) and
# --autoplay-policy=no-user-gesture-required (the spoken prompt plays without a tap).
VOICE_HTML = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>EMORI</title>
__PRECONNECT__<style>
body{font-family:system-ui,sans-serif;margin:0;display:grid;place-items:center;height:100vh;background:#0f1220;color:#eef}
.card{background:#171a2b;padding:24px 28px;border-radius:18px;box-shadow:0 10px 40px #0008;width:min(94vw,520px);text-align:center}
h1{font-size:22px;margin:0 0 6px}p{color:#9aa;font-size:14px;margin:0 0 14px}
video{width:100%;aspect-ratio:16/9;max-height:40vh;object-fit:contain;display:block;border-radius:12px;background:#000;transform:scaleX(-1)}
.cam{position:relative;width:100%}
#qrbox{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);border:2px dashed #9fd;border-radius:10px;pointer-events:none;opacity:.75}
#qrlight{color:#778;font-size:12px;font-variant-numeric:tabular-nums;min-height:1.2em}
#msg{margin:12px 0;color:#9fd;font-size:15px;min-height:1.4em}
.opts{display:grid;gap:10px;margin-top:8px}
.opt{display:flex;align-items:center;gap:14px;padding:13px 16px;border:0;border-radius:14px;background:#2b3050;color:#fff;font-size:16px;cursor:pointer;text-align:left}
.opt b{display:inline-grid;place-items:center;width:32px;height:32px;border-radius:50%;background:#5b6cff;flex:0 0 auto}
#anon{margin-top:12px;width:100%;padding:12px;border:0;border-radius:12px;background:#23263a;color:#9fd;font-size:13px;cursor:pointer}
.hint{margin:6px 0 0;color:#9aa;font-size:13px;min-height:1.2em}
.demo{margin-top:10px;font-size:11px;color:#778}
</style>
<script src="/static/jsqr.js"></script></head>
<body><div class=card>
<h1 id=title></h1><p id=prompt></p>
<div id="addon-hint" class="hint"></div>
<div class=cam><video id=v autoplay muted playsinline></video><div id=qrbox></div></div>
<div id=msg></div>
<div id=qrlight></div>
<div class=opts id=opts></div>
<button id=anon></button>
<div class=demo id=demo></div></div>
<canvas id=c hidden></canvas>
<script>
""" + _SPEECH_JS + _QR_SCAN_JS + """
const PROMPT=__PROMPT__, OPTIONS=__OPTIONS__, demo=__DEMO__;
const MENU=__MENU_TUNING__;  // grammar + confidence gate + listen deadline
const v=document.getElementById('v'),c=document.getElementById('c'),ctx=c.getContext('2d');
const qrbox=document.getElementById('qrbox'), qrlight=document.getElementById('qrlight');
qrbox.style.width=qrbox.style.height=(QRCFG.crop*100).toFixed(0)+'%';
let qrPass=0;
const msg=document.getElementById('msg'),title=document.getElementById('title');
title.textContent=S.show_badge; msg.textContent=S.looking;
document.getElementById('prompt').textContent=PROMPT;
document.getElementById('anon').textContent=S.anon_button;
async function addonHintPoll(){
  const el=document.getElementById('addon-hint');
  try{ const r=await fetch('/api/addon/hints'); if(!r.ok){ el.textContent=''; return; }
    const d=await r.json(); el.textContent=(d.lines||[]).join('  ·  ');
  }catch(e){ el.textContent=''; }
}
setInterval(addonHintPoll,5000); addonHintPoll();
if(demo){document.getElementById('demo').textContent=S.demo_note;}
let phase='scan', qr='', tries=0, navigating=false, lastQr='', lastQrAt=0, cueOpen=false;
// The kiosk language is switched by a hidden idle gesture (a double head turn), which
// re-languages the menu, this page and the SSO claim server-side. THIS page is already
// rendered in the old language and has no other way to find out, so it polls for that
// one page-level verb. Cheap on purpose (2s, one tiny JSON) and filtered to `reload`:
// the pending-cmd slot is shared with /session, and an unfiltered drain here would
// swallow the begin/end/home a live session is waiting for. Skipped while navigating --
// the session that is starting was already minted in whichever language was current.
// `phase` tells the robot whether anybody is using this page: a line being spoken means
// somebody is, whatever the phase, because the give-up line and a refused badge's cue play
// to a person still standing there while the camera looks for the next badge.
const pageId=Math.random().toString(36).slice(2,12);
setInterval(async ()=>{ if(navigating) return;
  const doing=(__sayOpen>0||cueOpen)?'speaking':phase;
  try{ const r=await fetch('/api/pending-cmd?verbs=reload&phase='+encodeURIComponent(doing)+'&page='+pageId); if(!r.ok) return;
    const d=await r.json();
    if(d.cmd==='reload'){ stopSpeech(); stopListen(); stopCam(); location.reload(); }
  }catch(e){} }, 2000);
""" + _STT_JS + """
async function startCam(){ try{ v.srcObject=await navigator.mediaDevices.getUserMedia({video:{facingMode:'user',width:{ideal:1280},height:{ideal:720}},audio:false}); requestAnimationFrame(scan); }
  catch(e){ msg.textContent=S.cam_unavailable; } }
function scan(){ if(phase!=='scan') return;
  if(v.readyState===v.HAVE_ENOUGH_DATA && v.videoWidth){
    c.width=v.videoWidth; c.height=v.videoHeight; ctx.drawImage(v,0,0,c.width,c.height);
    const r=qrLadder(ctx.getImageData(0,0,c.width,c.height).data,c.width,c.height,qrPass);
    qrPass=(qrPass+1)%4;
    qrlight.textContent=S.light_label+' '+Math.round(r.mean*100/255)+'% · '+r.range+' · '+r.pass+' '+Math.round(r.ms)+'ms';
    if(Date.now()>msgHold) msg.textContent=r.dark?S.too_dark:S.hold_badge;
    qrTel(r); qrShot(c,r);
    if(r.data){ qrTel(r,true); onBadge(r.data); return; } }
  setTimeout(()=>requestAnimationFrame(scan), QRCFG.ms); }
async function priorityQr(data){ const now=Date.now();
  if(data===lastQr && now-lastQrAt<3000) return false;
  lastQr=data; lastQrAt=now;
  try{ const r=await fetch('/api/badge/claim',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({payload:data})});
    if(r.status===202) return true;
    let code=''; try{ code=((await r.json())||{}).error_code||''; }catch(_e){}
    qrReject(code||('http_'+r.status), S.badge_refused);
    return false; }
  catch(e){ qrReject('unreachable', S.badge_refused); return false; } }
// QR shown -> priority preflight -> greet by name -> speak the menu -> listen.
async function onBadge(data){ if(phase!=='scan') return; const candidate=data||''; phase='preflight';
  if(candidate && !(await priorityQr(candidate))){ phase='scan'; requestAnimationFrame(scan); return; }
  phase='greet'; qr=candidate;
  // Release the webcam IMMEDIATELY once we have a badge (the verbal menu needs no camera).
  // This frees the single webcam well before the Pokamind session acquires it, avoiding a
  // NotReadableError/device-busy race on Windows where the just-released device lingers.
  stopCam();
  let name='', anon=true;
  try{ const r=await fetch('/api/identify',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({qr,lookup_only:true})});
    if(!r.ok) throw new Error('qr lookup'); const d=await r.json(); name=d.name||''; anon=!!d.anonymous; }
  catch(e){ phase='scan'; startCam(); return; }
  title.textContent=S.which_session;
  const greet=(anon||!name)?(S.welcome+' '):(S.hello+' '+name+'. ');
  msg.textContent=greet+S.say_numbers;
  say(greet+PROMPT, listen); }
// `speculative` marks an answer the END-OF-SPEECH EDGE guessed at (flushStt above), as
// opposed to one a backstop window, a button or the Web Speech API produced.
async function select(text,speculative){ if(navigating) return; const gen=listenGen;
  const r=await fetch('/api/voice-select',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({text, qr, preview:true})});
  const d=await r.json();
  // Same stale guard as flushStt: a newer listen window (or give-up) began mid-flight, so
  // this answer describes audio nobody is waiting on any more - never re-enter onMiss().
  if(navigating||gen!==listenGen) return;
  if(d.matched && d.exact){ beginSession(d,text); return; }
  // A guess that did not land costs nothing: the audio is still buffered and the backstop
  // window judges it. The edge may only ever ACCEPT -- one rule, both of its exits.
  if(speculative) return;
  // A non-exact match is the menu's DEFAULT topic, not something the person said.
  // Screenless, auto-committing it after 3 tries silently started the wrong roleplay.
  onMiss(); }
function onMiss(){ if(navigating) return;
  // stopListen() first: listen() early-returns while phase==='listen', so the re-prompt
  // callback would otherwise be a no-op riding the previous (still-open) mic stream.
  stopListen(); phase='miss'; tries++;
  if(tries>=MENU.maxTries){ giveUp(); return; }
  msg.textContent=S.didnt_catch; say(S.didnt_catch+' '+PROMPT, listen); }
function giveUp(){ stopListen(); tries=0; phase='scan'; lastQr=''; lastQrAt=0;
  // Hand back to the two paths that cannot be misheard: the badge and the buttons.
  // The identified badge (qr) is KEPT - the SAME person who could not be understood by
  // voice is still here, so their topic-button tap must stay attributed to them, not be
  // downgraded to anonymous. A real new visitor re-scans (onBadge overwrites qr) or taps
  // the anonymous button (onBadge('')). lastQr is cleared so re-showing the SAME badge is
  // not swallowed by the 3s debounce.
  title.textContent=S.show_badge; msg.textContent=S.menu_giving_up;
  // startCam() is the say() CALLBACK, not a sibling statement: the person is normally
  // still holding the badge in frame, so opening the camera immediately let scan() ->
  // onBadge() fire a SECOND utterance (a second Audio element) over the ~4s give-up line
  // on the robot's single speaker, and then open the mic over both.
  say(S.menu_giving_up, startCam); }
async function beginSession(d,text){ if(navigating) return; navigating=true; stopListen(); stopCam();
  try{ const r=await fetch('/api/voice-select',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({text, qr})});
    if(!r.ok) throw new Error('voice select'); const started=await r.json();
    msg.textContent=S.starting+' '+started.topic+'...';
    // A: NAVIGATE FIRST -- the confirmation is spoken by /session, over the load.
    //
    // Not `say(line, go)`: with navigation as the speech-completion callback, the session
    // URL would only start loading when the sentence finished: about 3 s at the
    // front of the session-start wait, while /api/voice-select itself answers in 15 ms.
    //
    // Two ways to overlap it, and only one of them works.  (ii) "navigate and let this
    // page keep talking" does not: the announcement is a Piper WAV in an <audio> element
    // owned by THIS document, `GET /session` is answered by the same local process in
    // milliseconds, and the moment that document commits this one is destroyed -- the
    // sentence would be cut a word or two in, which is worse than the delay it saves.
    // (i) is what ships: /session is the document that SURVIVES, it already carries the
    // identical say()/stopSpeech ladder, it already plays audio with no user gesture on
    // the kiosk (the summary and the give-up lines do it today), and the session's own
    // first audio is ~16 s behind this point -- so a longer, calmer sentence there fills
    // dead air instead of adding to it. The wording lives in pages.START_COVER and is
    // rendered into that page per kiosk language as START_LINE.
    //
    // stopSpeech() still runs, and still matters: the menu prompt (or a didnt_catch
    // re-prompt) can be mid-WAV when a topic BUTTON is tapped, and that <audio> would
    // otherwise carry into the covering line on the robot's single speaker.
    stopSpeech();
    location.href=started.session_url; }
  catch(e){ navigating=false; phase='scan'; msg.textContent=S.looking; startCam(); } }
function stopCam(){ try{ ((v.srcObject&&v.srcObject.getTracks())||[]).forEach(t=>t.stop()); }catch(e){} }
// Fallback: number buttons (also work before a badge -> anonymous). Each is an exact match.
OPTIONS.forEach(o=>{ const b=document.createElement('button'); b.className='opt';
  const k=document.createElement('b'); k.textContent=o.key; const n=document.createElement('span');
  n.textContent=o.name; b.appendChild(k); b.appendChild(n); b.onclick=()=>{ stopListen(); phase='listen'; select(o.key); }; document.getElementById('opts').appendChild(b); });
document.getElementById('anon').onclick=()=>onBadge('');
initStt(); startCam();
</script></body></html>"""

LAUNCHER_HTML = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>EMORI training</title><style>
body{font-family:system-ui,sans-serif;margin:0;display:grid;place-items:center;height:100vh;background:#0f1220;color:#eef}
.card{background:#171a2b;padding:32px 36px;border-radius:18px;box-shadow:0 10px 40px #0008;width:min(92vw,420px)}
h1{font-size:20px;margin:0 0 4px}p{color:#9aa;margin:0 0 20px;font-size:13px}
label{display:block;font-size:12px;color:#9aa;margin:12px 0 4px}
input{width:100%;padding:10px 12px;border-radius:10px;border:1px solid #333;background:#0f1220;color:#eef;box-sizing:border-box}
button{margin-top:22px;width:100%;padding:14px;border:0;border-radius:12px;background:#5b6cff;color:#fff;font-size:16px;cursor:pointer}
.demo{margin-top:14px;font-size:11px;color:#778}
</style></head><body><div class=card>
<h1>Start a training session</h1><p>Identify yourself to begin. The robot will join you.</p>
<label>Staff ID / badge</label><input id=sid value=learner_01>
<label>Email</label><input id=email value=learner@example.org>
<label>Module (optional)</label><input id=module placeholder="leave blank to choose on Pokamind">
<button onclick=start()>Start training</button>
<div class=demo id=demo></div></div>
<script>
if(__DEMO__){document.getElementById('demo').textContent='DEMO mode: no real Pokamind configured; synthetic analysis will drive the robot.'}
async function start(){
  const body={student_id:sid.value,email:email.value,module_id:module.value||null};
  const r=await fetch('/api/start',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json(); if(typeof stopSpeech==='function') stopSpeech();
  location.href=d.session_url;
}
</script></body></html>"""

# Screenless kiosk landing alternative: scan a QR staff badge with the camera (vendored
# jsQR), or practice anonymously. The browser owns the camera (so no contention with the
# host app) and POSTs only the decoded payload to /api/identify.
IDENTIFY_HTML = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>EMORI</title>
<style>
body{font-family:system-ui,sans-serif;margin:0;display:grid;place-items:center;height:100vh;background:#0f1220;color:#eef}
.card{background:#171a2b;padding:28px 32px;border-radius:18px;box-shadow:0 10px 40px #0008;width:min(92vw,460px);text-align:center}
h1{font-size:22px;margin:0 0 4px}p{color:#9aa;font-size:13px;margin:0 0 16px}
video{width:100%;aspect-ratio:16/9;max-height:46vh;object-fit:contain;display:block;border-radius:12px;background:#000;transform:scaleX(-1)}
.cam{position:relative;width:100%}
#qrbox{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);border:2px dashed #9fd;border-radius:10px;pointer-events:none;opacity:.75}
#qrlight{color:#778;font-size:12px;font-variant-numeric:tabular-nums;min-height:1.2em}
#msg{margin:14px 0;color:#9fd;font-size:14px}
button{width:100%;padding:14px;border:0;border-radius:12px;background:#2b3050;color:#fff;font-size:15px;cursor:pointer}
.demo{margin-top:10px;font-size:11px;color:#778}
</style>
<script src="/static/jsqr.js"></script></head>
<body><div class=card>
<h1>Welcome</h1><p>Show your staff badge to the robot to start your session — or practice anonymously.</p>
<div class=cam><video id=v autoplay muted playsinline></video><div id=qrbox></div></div>
<div id=msg>looking for a badge…</div>
<div id=qrlight></div>
<button onclick=anon()>Just practice (anonymous)</button>
<div class=demo id=demo></div></div>
<canvas id=c hidden></canvas>
<script>
""" + _QR_SCAN_JS + """
const demo=__DEMO__; const v=document.getElementById('v'),c=document.getElementById('c'),ctx=c.getContext('2d'),msg=document.getElementById('msg');
let done=false,lastQr='',lastQrAt=0;
if(demo){document.getElementById('demo').textContent='DEMO mode: synthetic session (no real Pokamind).'}
async function start(){
  try{ v.srcObject=await navigator.mediaDevices.getUserMedia({video:{facingMode:'user',width:{ideal:1280},height:{ideal:720}},audio:false}); requestAnimationFrame(scan); }
  catch(e){ msg.textContent='camera unavailable — tap Just practice'; }
}
const qrbox=document.getElementById('qrbox'), qrlight=document.getElementById('qrlight');
qrbox.style.width=qrbox.style.height=(QRCFG.crop*100).toFixed(0)+'%';
let qrPass=0;
function scan(){ if(done) return;
  if(v.readyState===v.HAVE_ENOUGH_DATA && v.videoWidth){
    c.width=v.videoWidth; c.height=v.videoHeight; ctx.drawImage(v,0,0,c.width,c.height);
    const r=qrLadder(ctx.getImageData(0,0,c.width,c.height).data,c.width,c.height,qrPass);
    qrPass=(qrPass+1)%4;
    qrlight.textContent='light '+Math.round(r.mean*100/255)+'% · '+r.range+' · '+r.pass+' '+Math.round(r.ms)+'ms';
    if(Date.now()>msgHold) msg.textContent=r.dark?'too dark — add light, or tap Just practice':'hold the badge in the box';
    if(r.data){ identify(r.data); return; } }
  setTimeout(()=>requestAnimationFrame(scan), QRCFG.ms); }
async function priorityQr(qr){ const now=Date.now();
  if(qr===lastQr && now-lastQrAt<3000) return false;
  lastQr=qr; lastQrAt=now;
  try{ const r=await fetch('/api/badge/claim',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({payload:qr})});
    if(r.status===202) return true;
    let code=''; try{ code=((await r.json())||{}).error_code||''; }catch(_e){}
    qrReject(code||('http_'+r.status), 'badge not accepted — try again, or tap Just practice');
    return false; }
  catch(e){ qrReject('unreachable','badge not accepted — try again, or tap Just practice');
    return false; } }
async function identify(qr){ if(done) return; done=true;
  if(qr && !(await priorityQr(qr))){ done=false;
    if(Date.now()>msgHold) msg.textContent='looking for a badge';
    requestAnimationFrame(scan); return; }
  msg.textContent='starting';
  try{ const r=await fetch('/api/identify',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({qr})});
    if(!r.ok) throw new Error('identify'); const d=await r.json(); msg.textContent=d.anonymous?'anonymous session - starting':('hello '+(d.name||'')+' - starting');
    // Badge-direct start: any greeting/announcement still playing would carry into the
    // session's own opening TTS (the overlap beginSession's go() already prevents on the
    // menu path). Same rule here: silence this page before handing the speaker over.
    if(typeof stopSpeech==='function') stopSpeech();
    location.href=d.session_url; }
  catch(e){ done=false; msg.textContent='looking for a badge'; requestAnimationFrame(scan); } }
function anon(){ identify(''); }
start();
</script></body></html>"""

# The session page: embeds Pokamind in an iframe and relays each pokamind_* postMessage
# to POST /api/cycle (the hostapp drives the robot and fans the envelope out to
# /ws/relay for the ROS 2 publisher + recorder). On completion the /api/cycle response
# carries the spoken summary; this page plays it through the robot speaker, then
# navigates home so the kiosk is ready for the next learner.
SESSION_HTML = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>EMORI session</title>
<style>html,body{margin:0;height:100%;background:#000}iframe{border:0;width:100vw;height:100vh}
#status{position:fixed;bottom:8px;left:8px;font:12px system-ui;color:#6f6;background:#0008;padding:4px 8px;border-radius:8px}</style>
</head><body><div id=status>connecting…</div><div id=stage></div>
<script>
""" + _SPEECH_JS + """
const q=new URLSearchParams(location.search), demo=q.get('demo')==='1', sso=q.get('sso');
// How long the iframe has to say ANYTHING before this page gives up on it (0 = never).
// A session that never starts is not a rare failure: the whole flow after `GET /session`
// lives inside a cross-origin frame, and without a deadline a badge scan can leave the
// robot with its antennas open, silent and unresponsive.
const SESSION_START_MS=__SESSION_START_MS__;
// A: the confirmation the LANDING page does not hold itself back to speak (3,158 ms of
// measured dead time if it did). Empty for every path that never announced anything (a badge-direct
// start, a walk-in, a session whose module is not on the live menu). See pages.START_COVER
// and the beginSession comment on why this document -- not the one being navigated away
// from -- is the only one that can deliver a whole sentence.
const START_LINE=__START_LINE__;
// ...and the ONE thing that ends it early. The line covers a load, so the moment the load
// finishes -- i.e. the moment Pokamind itself starts talking -- this page must get off the
// robot's single speaker. The session frame reports its opening on the EXISTING
// pokamind_emori_dbg channel, and 'opening-intro-first-play' fires when the
// scenario's first audio ACTUALLY starts playing. Every other stage (mount,
// intro-requested, warmup-trigger, endpointer-ready, the rolled-up latency) happens while
// the frame is still silent, so first-play is the only honest hand-over signal here.
//
// If the trace never arrives -- an older app version, a module whose intro never plays, a
// frame that dies -- nothing calls this and the line simply finishes, exactly as it does
// today. The interlock can only ever SHORTEN the announcement.
//
// GRACE. A first-play that arrives in the first second of the line being audible is not
// honoured, and the line is allowed to finish. Reasoning, because this is the one place a
// deliberate choice is being made:
//   * the two mistakes are not symmetric. Ignoring a genuine early first-play costs the
//     remainder of one ~6 s sentence overlapping the scenario's opening -- audibly two
//     voices, bounded, and exactly what happens today. Honouring a bogus one produces
//     "Star--" and then silence, on EVERY session it affects, which on a screenless robot
//     is indistinguishable from a crash and has no recovery;
//   * under a second there is nothing to preserve by cutting: Piper has emitted a syllable
//     or two, so the "announcement" the visitor gets is a noise, not information;
//   * and it is not physically the signal it claims to be. The measured load is ~16 s
//     (SSO 302, app boot, session mint, intro synthesis); a first-play a few hundred ms
//     into this page's own line is a duplicated or replayed trace, not Pokamind speaking.
// Not-yet-audible is a THIRD case and is handled differently: there the line is cancelled
// outright, which is neither a clipped word nor an overlap (stopSpeech now also latches
// say()'s generation, so a WAV that arrives afterwards is never played).
const COVER_HUSH_GRACE_MS=1000;
let coverAudibleAt=0, coverSettled=false;
// The robot moves on what it hears from the speaker, and it hears this line too. It is told
// when the line starts and stops, so the first sentence performs on its own sound.
let coverHeld=false;
function coverHold(on){
  if(coverHeld===on) return;
  coverHeld=on;
  try{ fetch('/api/voice-cover',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({on:on}),keepalive:true}).catch(()=>{}); }catch(e){}
}
function coverAudible(){ if(!coverAudibleAt) coverAudibleAt=Date.now(); coverHold(true); }
function hushCover(){
  // `over` first: once finish()/goHome() owns the speaker, the covering line is long
  // gone and stopSpeech() here would cut the SUMMARY or the ending line instead. A
  // late or duplicated first-play (a frame that re-mounts, a retried intro) is exactly
  // how that arrives, so this is not hypothetical.
  if(over||coverSettled||!START_LINE) return;
  coverSettled=true;   // decided once: a duplicate trace must not re-open the question
  if(coverAudibleAt && Date.now()-coverAudibleAt<COVER_HUSH_GRACE_MS) return;  // let it finish
  stopSpeech();
  coverHold(false);
}
// Read behind a guard, like authFailCode below: this listener also carries the relay, the
// auto-begin and the VAD telemetry, so one throwing getter on a junk message must not be
// able to kill the rest of the conversation. An EXACT stage match, never a prefix -- the
// other opening-* stages are debug, and this branch stays a pure relay for all of them.
function isOpeningFirstPlay(m){
  try{ return !!m && m.stage==='opening-intro-first-play'; }catch(e){ return false; } }
// Same shape, same reason: property access on a hostile payload may throw.
function isTtAck(m){
  try{ return !!m && (m.stage==='tt-patch-applied'||m.stage==='tt-patch-rejected'); }
  catch(e){ return false; } }
status.textContent='ready';
// ONE latch for "this page is done", shared by both exits (finish below, goHome further
// down): whichever fires first wins, so the kiosk navigates exactly once.
let over=false;
function finish(d){ if(over) return; over=true;
  status.textContent=S.session_over;
  // stopSpeech() before navigating: the 30 s cap can fire while the summary is still
  // being read, and an unstopped WAV keeps playing over the next learner's greeting on
  // the landing page (same single-speaker overlap as the landing announcement).
  // next() is reachable from BOTH the say() callback and the 30s cap; latch it so the
  // second arrival cannot re-navigate mid-navigation (the beginSession go() treatment).
  let went=false;
  const next=()=>{ if(went) return; went=true; stopSpeech(); location.href=(d&&d.next_url)||'/'; };
  if(d&&d.summary){ say(d.summary, ()=>setTimeout(next,800)); setTimeout(next,30000); }
  else setTimeout(next,2500); }
// A session can also end ROBOT-side: the walk-away fail-safe finalizes inside the hostapp
// when Pokamind never answers `emori_cmd:end` (a dead iframe). Nothing in this page can
// observe that - it sat on a dead session forever, renderer spinning, and the kiosk never
// returned to the badge screen. `home` is the hostapp
// saying "leave this page"; it is a PAGE verb, so it is acted on HERE and never relayed
// into the frame. The server keeps it set until a new /session opens, so the guard above
// - not the delivery count - is what makes it navigate once.
function goHome(why,line){ if(over) return; over=true; status.textContent=why;
  // A robot-side end (walk-away, watchdog) previously navigated in silence - the ONLY
  // session ending with no user feedback at all. Speak the same completion line the
  // normal finish() shows, THEN go home. This is post-finalize, so it cannot overlap
  // the roleplay's own TTS (the session is already over); a hung say() is capped.
  // `line` overrides the completion wording for an ending that is NOT a completion (a
  // session that never started); a mute reset there is the one thing this must never be.
  stopSpeech();
  const spoken=line||S.session_over;
  let gone=false; const go=()=>{ if(gone) return; gone=true; stopSpeech(); location.href='/'; };
  say(spoken, ()=>setTimeout(go,300)); setTimeout(go, sayMaxMs(spoken)+1500); }
// The session-start deadline. It is disarmed by the FIRST pokamind_* message of any
// kind - that message is the only proof the frame is alive, because the frame is
// cross-origin (its load event fires for a blank error document just as happily) and the
// greeting gesture that precedes it ends with the antennas open and no settle pose.
let frameSpoke=false, startTimer=null;
function noteFrameAlive(){ if(frameSpoke) return; frameSpoke=true;
  try{ if(startTimer){ clearTimeout(startTimer); startTimer=null; } }catch(e){}
  // Mirror the disarm to the adapter-side watchdog: its own disarm (first
  // analysis/AI line) lags a live frame by design, and it killed a healthy
  // boot at +57s. Fire-and-forget, once.
  try{ fetch('/api/frame-alive',{method:'POST'}); }catch(e){} }
function sessionStartFailed(why){
  if(over||frameSpoke) return;
  try{ if(startTimer){ clearTimeout(startTimer); startTimer=null; } }catch(e){}
  // Tell the hostapp FIRST: it owns the session the robot is holding open (training
  // lease, mic profile, the driver's tracker), and this page is about to navigate away
  // from it. Fire-and-forget - the belt-and-braces adapter-side watchdog covers a kiosk
  // that cannot even reach its own origin.
  try{ fetch('/api/session-start-failed',{method:'POST',
    headers:{'content-type':'application/json'},body:JSON.stringify({reason:why})}); }catch(e){}
  goHome(why, S.session_start_failed); }
// Squeeze the `code` of an auth-failure report into the hostapp's OWN status-code grammar
// (server.py `_SAFE_STATUS_CODE_RE` = /^[a-z][a-z0-9_]{0,63}$/, the same one readycheck
// shares).  It arrives by postMessage, so in principle any frame can author it, and it
// then reaches a request body, a WARNING log line and the on-screen status -- the repo's
// slug discipline applies.  Two properties matter: the result is ALWAYS accepted by that
// validator (never silently degraded to `session_never_started`, which would misreport an
// auth failure as a timeout), and nothing the message carried survives except [a-z0-9_].
// A non-string, a hostile object, an empty or fully-stripped code all fall back to the
// bare `auth_failed`, which is still a true and specific description of what happened.
// Reading `.code` happens INSIDE the guard, not at the call site: this listener also
// carries the relay, the auto-begin and the VAD telemetry, so one throw on a junk message
// would silently kill the rest of the conversation.
function authFailCode(msg){ let c='';
  try{ const raw=msg&&msg.code;
    if(typeof raw==='string') c=raw.toLowerCase().replace(/[^a-z0-9_]/g,''); }catch(e){ c=''; }
  return ('auth_failed'+(c?('_'+c):'')).slice(0,64); }
function armSessionStartDeadline(){
  if(!(SESSION_START_MS>0)) return;
  startTimer=setTimeout(()=>{ startTimer=null;
    sessionStartFailed('session_never_started'); }, SESSION_START_MS); }
// Relay each cycle to the host app over plain HTTP (drives the robot). Same-origin POST.
function relay(payload){ fetch('/api/cycle',{method:'POST',headers:{'content-type':'application/json'},
  body:JSON.stringify(payload)}).then(r=>r.json()).then(d=>{
    if(d&&d.completed){ finish(d); return; }
    if(d&&d.level) status.textContent='robot: '+d.level+
      (d.reasons&&d.reasons.length?(' ('+d.reasons[0]+')'):''); })
  .catch(()=>{ status.textContent='relay error (see /api/status)'; }); }

// Real path: embed Pokamind, forward only the pokamind_* postMessages.
let emoriFrame=null;
function emoriCmd(cmd){ try{ emoriFrame&&emoriFrame.contentWindow&&
  emoriFrame.contentWindow.postMessage({type:'emori_cmd',cmd:cmd},'*'); }catch(e){} }
// Turn-taking override channel: push turn-taking/VAD overrides (window.__EMORI_TT) into the frame.
// It reuses the EXISTING command envelope (type:'emori_cmd') on purpose -- Pokamind's
// listener already switches on that one type, so no NEW inbound message type is
// introduced anywhere. Unlike wake/endpoint/end this command carries DATA,
// so it is posted with EXPLICIT targetOrigins and never '*': the browser drops it unless
// the frame document is on an allowed origin, so a redirected or hijacked frame can never
// be handed the kiosk's tuning. Strictly OUTBOUND -- the only way to change the patch is a
// same-origin POST /api/tt-overrides, which is env-gated OFF by default.
// Delivery is ACK-DRIVEN, and that is the whole point. A single push on the
// revision edge is a RACE it loses: the patch is posted ~250 ms after the frame
// document loads, but the listener that applies it only exists once the app has
// mounted the session UI seconds later -- and postMessage has no buffer, so the
// patch would be silently dropped and the kiosk would quietly run profile defaults.
// So: keep the patch, re-push it every poll tick until the frame ACKs it, and
// stop on the ACK (or after a bounded number of tries, so a frame that never
// applies it cannot cost anything for the rest of the session).
const TT_ORIGINS=__TT_ORIGINS__; let ttRev=-1, ttPatch=null, ttAcked=false, ttTries=0;
const TT_MAX_TRIES=120;   // 120 x 250 ms = 30 s, which outlasts a slow React mount
function ttPush(patch){ if(!emoriFrame||!emoriFrame.contentWindow) return;
  for(let i=0;i<TT_ORIGINS.length;i++){ try{ emoriFrame.contentWindow.postMessage(
    {type:'emori_cmd',cmd:'tt_patch',patch:patch},TT_ORIGINS[i]); }catch(e){} } }
// Called every poll tick: re-deliver until the frame says it applied it.
function ttRetry(){ if(!ttPatch||ttAcked||ttTries>=TT_MAX_TRIES) return;
  ttTries++; ttPush(ttPatch); }
async function ttSync(rev){ if(rev===ttRev) return; ttRev=rev; if(!rev) return;
  try{ const r=await fetch('/api/tt-overrides'); if(!r.ok) return; const d=await r.json();
    ttRev=d.rev; if(d.patch&&Object.keys(d.patch).length){
      ttPatch=d.patch; ttAcked=false; ttTries=0; ttPush(d.patch);
      if(!over) status.textContent='tt overrides r'+d.rev; } }catch(e){} }
if(sso){
  const f=document.createElement('iframe'); f.src=sso; emoriFrame=f;
  // A (re)navigated frame is a NEW document with no patch and no listener yet:
  // re-arm both the revision edge and the retry loop.
  f.onload=()=>{ ttRev=-1; ttAcked=false; ttTries=0; };
  // A frame that cannot even load has nothing to wait for; end on the spot rather than
  // stand silent for the whole deadline. (Cross-origin, this rarely fires -- which is
  // exactly why the timer below, not this handler, is the load-bearing half.)
  f.onerror=()=>{ sessionStartFailed('session_frame_error'); };
  // Delegate camera/mic to ALL origins inside the frame (note the `*`). `allow='camera'`
  // (no allowlist) defaults to the frame's *src* origin only (the SSO origin); but after
  // the SSO redirect the session document runs on the app origin, so getUserMedia
  // there is Permissions-Policy-denied -> NotAllowedError "Permission denied". `camera *`
  // grants the post-redirect origin too. (On the real robot the kiosk Chromium also
  // auto-grants via --use-fake-ui-for-media-stream; see onrobot runbook.)
  f.allow='camera *;microphone *;autoplay *'; stage.appendChild(f);
  armSessionStartDeadline();
  // The app posts from its own origin, not the SSO origin, so forward by message type.
  // Screenless auto-begin: a fullscreen kiosk has NO keyboard/mouse/touch, so it can never
  // click "Begin Conversation". The scenario intro finishing (the FIRST pokamind_tts_ended)
  // is the cue to start the roleplay -- we send the same 'wake' the head-tilt 'begin' would
  // (the frame handles it exactly like the button). Once only;
  // every later tts_ended is a conversation turn owned by the endpointer. It fires AFTER the
  // intro audio fully ends (never during), and Pokamind hard-mutes the mic while the AI then
  // speaks its first line, so the first-turn restate bug stays structurally impossible.
  let autoBegun=false;
  addEventListener('message',e=>{ const d=e.data||{};
    // FIRST, ahead of the prefix match below: the ONE pokamind_* message that is a report
    // of DEATH, not proof of life. Pokamind's partner-SSO callback posts
    // {type:'pokamind_auth_failed',code} when it cannot sign this badge in (no token, an
    // invalid token, a sign-in that never completed) - the frame is up, but it will never
    // have a user, a socket, or a session. Falling through to noteFrameAlive() would clear
    // the start deadline AND disarm the adapter-side watchdog, and latch frameSpoke, after
    // which sessionStartFailed() early-returns forever: the failure would SUPPRESS itself
    // and the robot would stand there until the walk-away path mislabels it a minute later.
    // So route it into the existing failure path instead, with its own code. That is
    // deliberately independent of SESSION_START_MS: this is a definitive report, not a
    // deadline, and it is still safe when the frame is already live - sessionStartFailed()
    // stands down once frameSpoke, and `abort_unstarted_session` refuses a session that has
    // exchanged anything.
    if(d.type==='pokamind_auth_failed'){ sessionStartFailed(authFailCode(d)); return; }
    // ANY OTHER pokamind_* message proves the frame came up, including the endpointer's own
    // VAD/debug chatter below - so the deadline is disarmed here, before the two early
    // returns, and not only on a real cycle.
    if(typeof d.type==='string' && d.type.indexOf('pokamind_')===0) noteFrameAlive();
    // VAD telemetry from the in-iframe endpointer: SHOW it (headless/fullscreen debug),
    // do NOT relay it to /api/cycle (it is not a real Pokamind cycle).
    if(d.type==='pokamind_emori_vad'){ if(!over) status.textContent=
      'mic '+(d.speaking?'● SPEAKING':(d.hasSpoken?'○ silence '+d.silence+'s':'○ (say something)'));
      try{ fetch('/api/emori-debug',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(d)}); }catch(_e){}
      // Throttled speaking relay: feeds the adapter watchdog's stall clock so a
      // long, continuously-spoken answer is never force-ended mid-turn. Every VAD frame
      // with speaking:true is a candidate, but rate-limited to >=1s apart -- while the
      // user keeps talking this fires ~1/s (refreshing the clock every second), and while
      // silent it stops firing entirely, so a real stall is still detected.
      if(d.speaking && (!window.__emSpkT || Date.now()-window.__emSpkT>1000)){ window.__emSpkT=Date.now();
        try{ fetch('/api/cycle',{method:'POST',headers:{'content-type':'application/json'},
          body:JSON.stringify({type:'pokamind_user_speaking'})}); }catch(_e){} }
      return; }
    // Endpointer lifecycle/VAD debug: relay to the same-origin hostapp sink so it shows in
    // the server log (the iframe cannot POST :8095 cross-origin; the parent can).
    if(d.type==='pokamind_emori_dbg'){
      // The frame's answer to a tt_patch push. Either verdict ends the retry loop:
      // 'applied' got what we wanted, 'rejected' means re-sending the same patch
      // would be rejected the same way -- and both are already logged below.
      // Read through isTtAck(), never `d.stage` inline: a hostile frame can make
      // any property THROW on access, and this listener also carries the opening
      // trace and the frame-alive proof, so one throw here would silence both.
      if(isTtAck(d)){ ttAcked=true;
        if(!over) status.textContent='tt overrides applied'; }
      // The opening trace carries the covering line's interlock (see hushCover above).
      // Deliberately INSIDE the debug branch and behind an exact stage match: the two
      // rules above -- the auth-failure intercept and the pokamind_* frame-alive prefix --
      // are untouched, and every other stage still relays and nothing else.
      if(isOpeningFirstPlay(d)) hushCover();
      try{ fetch('/api/emori-debug',{method:'POST',
      headers:{'content-type':'application/json'},body:JSON.stringify(d)}); }catch(_e){} return; }
    if(typeof d.type==='string' && d.type.indexOf('pokamind_')===0){ if(!over) status.textContent='relayed '+d.type; relay(d);
      if(d.type==='pokamind_tts_ended' && !autoBegun){ autoBegun=true;
        if(!over) status.textContent='intro read → begin'; emoriCmd('wake'); } } });

  // EMORI screenless session control = PHYSICAL head-tilt gesture (no voice wake-word).
  // The adapter's gesture sensor watches the back-drivable head pose and, on a deliberate
  // nudge, queues a command (begin when idle / end when active). This page polls for it
  // (same-origin) and relays it into the Pokamind iframe. The endpointer still auto-submits
  // mid-conversation turns; the head-tilt is only for begin + intentional end.
  status.textContent='starting when the intro finishes…';
  setInterval(async ()=>{ if(over) return;
    try{ const r=await fetch('/api/pending-cmd'); if(!r.ok) return; const d=await r.json();
      if(typeof d.tt_rev==='number') ttSync(d.tt_rev);
      ttRetry();   // until the frame ACKs it (see ttPush)
      if(d.cmd==='begin'){ status.textContent='head-tilt -> begin'; emoriCmd('wake'); }
      else if(d.cmd==='end'){ status.textContent='head-tilt -> end'; emoriCmd('end'); }
      else if(d.cmd==='reprompt'){ status.textContent='re-prompt'; emoriCmd('reprompt'); }
      else if(d.cmd==='home'){ goHome('session ended -> kiosk'); }
    }catch(e){} }, 250);

  // Keyboard fallback so the session is testable on a fullscreen kiosk with no DevTools
  // and even when the dev-box mic/STT is flaky:  B=begin(wake)  S=submit(endpoint)  E=end.
  addEventListener('keydown',ev=>{ const k=(ev.key||'').toLowerCase();
    if(k==='b'){ status.textContent='[key] begin'; emoriCmd('wake'); }
    else if(k==='s'){ status.textContent='[key] submit'; emoriCmd('endpoint'); }
    else if(k==='e'){ status.textContent='[key] end'; emoriCmd('end'); } });
}
// Demo path: synthesise a believable engagement arc so the robot reacts without Pokamind.
// The demo emits the EXACT live contract shape (dense 31-style dict, 6 body fields with
// Pokamind's real vocabulary, engagement/confidence metrics only) so demo behaviour never
// overstates what the live feed will produce.
if(demo){
  let seq=0; const N=20;
  const STYLES=__DEMO_STYLES__;
  const arc=[0.7,0.8,0.85,0.4,0.35,0.75,0.55,0.5,0.62,0.8];
  const t=setInterval(()=>{ const eng=arc[seq%arc.length];
    const agitated=(seq%10>=3&&seq%10<=4);
    const styles={}; const boost=agitated?['rapid','abrupt','energetic','sudden']:
      (eng>0.6?['mirroring','acknowledging','investigative','lively']:['withdrawing','quiet','reserved','muted']);
    let total=0; STYLES.forEach(s=>{ let v=100.0/STYLES.length;
      if(boost.indexOf(s)>=0) v*=3.2; styles[s]=v; total+=v; });
    Object.keys(styles).forEach(k=>{ styles[k]=Math.round(styles[k]/total*1000)/10; });
    relay({type:'pokamind_analysis_update',seq:seq,timestamp:new Date().toISOString(),
      communication_styles:styles,
      body_language:{posture:eng>0.6?'leaning_forward':'leaning_back',
        head_position:eng>0.6?'nodding':(agitated?'shaking':'tilted_left'),
        gesture_type:agitated?'pointing':'open_palm',
        overall_energy:agitated?'high':(eng>0.6?'medium':'low'),
        engagement_score:Math.round(eng*100),confidence_score:Math.round(eng*90)},
      body_language_metrics:{engagement:Math.round(eng*100),confidence:Math.round(eng*90)}});
    // every other cycle, emit a (demo) AI line so the ROBOT performs its talking gesture
    if(seq%2===0){const lines=['I understand this is difficult for you.','Let me see what I can do to help.',
      'Take your time, there is no rush.','I hear your concern, let us look at the options.'];
      relay({type:'pokamind_ai_utterance',seq:seq,timestamp:new Date().toISOString(),
        text:lines[(seq/2)%lines.length|0]});}
    seq++;
    if(seq>=N){ clearInterval(t);
      relay({type:'pokamind_session_completed',status:'completed',session_id:'demo',
        partner:'demo',results:{demo:true}}); }
  }, 2000);
}
// LAST, deliberately: everything above is synchronous, so by the time this runs the
// Pokamind iframe is already appended (the load is in flight) and the 45 s start deadline
// is already armed. Speaking can therefore only ever be a passenger on the load, never a
// gate in front of it -- which is the whole point of moving the line here.
//
// Nothing in this page waits on the covering line, and its callback only says the line has
// ended. Every exit that
// follows (finish, goHome, sessionStartFailed -> goHome) already calls stopSpeech() before
// it speaks or navigates, so a line still playing when a session ends early is silenced by
// the paths that exist, and say()'s own deadlines cap a wedged /api/tts.
//
// The third argument is the START hook, and it is only used to decide whether cutting the
// line would clip a word: the covering line is the one utterance on this kiosk that a
// message from a cross-origin frame is allowed to interrupt (hushCover above).
if(START_LINE) say(START_LINE,()=>coverHold(false),coverAudible);
</script></body></html>"""
