"""Pluggable text-to-speech engines for robot speech.

* :class:`NullTts`  - logs the would-be utterance, synthesises nothing (default/CI).
* :class:`PiperTts`  - local neural TTS (Piper, free/offline, ARM-OK on the CM4). Lazy
  imports ``piper`` so the package stays importable without it. Voices are selected by
  language (``scripts/get_voice_models.sh`` installs ca_ES / en_US for Catalan and
  English).

A :class:`TtsEngine` returns the path to a synthesised WAV (or ``None`` if it produced
no audio); the speech consumer then plays that file on the robot speaker.
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Optional, Protocol

log = logging.getLogger("emori.speech.tts")


class TtsEngine(Protocol):
    def synthesize(self, text: str, lang: str = "en") -> Optional[str]: ...


class NullTts:
    """No real TTS — logs the line. Lets the whole pipeline run without audio deps."""

    def synthesize(self, text: str, lang: str = "en") -> Optional[str]:
        log.info("[NullTts] (%s) robot would say: %s", lang, text)
        return None


class PiperTts:
    """Local Piper TTS. ``voices`` maps a language prefix -> a .onnx model path.

    Install with the ``speech`` extra and download voices, e.g.::

        uv pip install "emori-adapter[speech]"
        # download voices to ./voices/{en,ca}.onnx (+ .onnx.json) from
        # https://huggingface.co/rhasspy/piper-voices
    """

    def __init__(self, voices: dict[str, str], out_dir: Optional[str] = None) -> None:
        if not voices:
            raise ValueError("PiperTts requires a {lang: model_path} mapping")
        self.voices = voices
        self.out_dir = out_dir or tempfile.gettempdir()
        self._loaded: dict[str, object] = {}

    def _voice(self, lang: str):
        key = lang.split("-")[0].split("_")[0].lower()
        path = self.voices.get(key)
        if path is None:
            # A voice reading a language it was not trained on is worse than
            # noticeable -- an English voice reading Catalan text is
            # what a listener reports as "the robot's pronunciation is broken".
            # The fallback stays (a wrong-voice sentence still beats dead
            # silence for an operator prompt) but it must never be silent in
            # the journal, and installing the missing voice is the real fix.
            path = next(iter(self.voices.values()))
            log.warning(
                "no Piper voice for language %r - falling back to %s; install "
                "voices/%s.onnx for native pronunciation", key, path, key,
            )
        if path not in self._loaded:
            from piper import PiperVoice  # lazy import

            self._loaded[path] = PiperVoice.load(path)
        return self._loaded[path]

    def synthesize(self, text: str, lang: str = "en") -> Optional[str]:
        import wave

        voice = self._voice(lang)
        out = os.path.join(self.out_dir, f"emori_tts_{abs(hash(text)) % 10_000_000}.wav")
        with wave.open(out, "wb") as wav:
            # piper-tts >=1.3 split the file-writing path into ``synthesize_wav`` (which
            # sets the WAV format + writes frames into the passed ``wave.Wave_write``);
            # bare ``synthesize`` now returns an unconsumed AudioChunk iterable and leaves
            # the wave header unwritten ("# channels not specified").
            voice.synthesize_wav(text, wav)
        return out
