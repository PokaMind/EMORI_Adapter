"""Robot speech engines (TTS).

All audible robot speech flows through the kiosk Chromium: the hostapp's
``/api/tts`` endpoint (``emori_adapter.hostapp.speech_api``) renders WAVs with
:class:`PiperTts` and the kiosk pages play them on the robot speaker. The engines
live here so the headless orchestrator (``emori-session``) can reuse them.
"""

from __future__ import annotations

from emori_adapter.speech.tts import NullTts, PiperTts, TtsEngine

__all__ = ["TtsEngine", "NullTts", "PiperTts"]
