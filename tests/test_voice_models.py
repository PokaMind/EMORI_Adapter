"""Every language the robot speaks has a Piper voice, and any language can have one.

``PiperTts`` falls back to its first voice when a language has none, and a voice reading a
language it was not trained on is unintelligible. The downloaders install the kiosk's
languages from rhasspy/piper-voices and take any further voice as an argument; the hostapp
maps whatever voice is on disk, or named by ``PIPER_VOICE_<LANG>``.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys
import types

import pytest

from emori_adapter.hostapp import speech_api
from emori_adapter.speech.tts import PiperTts

_ROOT = Path(__file__).resolve().parents[1]
_SH = _ROOT / "scripts" / "get_voice_models.sh"
_PS1 = _ROOT / "scripts" / "get_voice_models.ps1"
# rhasspy/piper-voices: <family>/<family>_<REGION>/<voice>/<quality>/<file stem>
_VOICE_PATH = re.compile(
    r"(?P<family>[a-z]{2,3})/(?P=family)_(?P<region>[A-Z]{2})/(?P<voice>[a-z0-9_]+)/"
    r"(?P<quality>x_low|low|medium|high)/(?P=family)_(?P=region)-(?P=voice)-(?P=quality)")


def _sh_voices() -> dict[str, str]:
    text = _SH.read_text(encoding="utf-8")
    block = text[text.index("declare -A PIPER=("):]
    block = block[:block.index("\n)")]
    return dict(re.findall(r'\[([a-z]+)\]="([^"]+)"', block))


def _sh_loop() -> list[str]:
    text = _SH.read_text(encoding="utf-8")
    return re.search(r"^VOICES=\(([a-z ]+)\)$", text, re.MULTILINE).group(1).split()


def _ps1_voices() -> dict[str, str]:
    return dict(re.findall(r"@\{ lang = '([a-z]+)'; path = \"([^\"]+)\" \}",
                           _PS1.read_text(encoding="utf-8")))


def _clean_voice_env(monkeypatch) -> None:
    for name in tuple(speech_api.os.environ):
        if name.startswith(("PIPER_VOICE_", "VOSK_MODEL_")):
            monkeypatch.delenv(name)


def test_the_downloaders_install_the_kiosk_languages_from_piper_voices():
    voices = _sh_voices()

    assert sorted(voices) == ["ca", "en"]
    assert sorted(_sh_loop()) == ["ca", "en"]
    for lang, path in voices.items():
        match = _VOICE_PATH.fullmatch(path)
        assert match, path
        assert match["family"] == lang  # a voice of the language it is filed under


def test_the_linux_and_windows_downloaders_install_the_same_voices():
    assert _ps1_voices() == _sh_voices()
    assert sorted(_sh_loop()) == sorted(_sh_voices())


def test_build_tts_maps_a_voice_of_any_language_found_on_disk(tmp_path, monkeypatch):
    voices = tmp_path / "voices"
    voices.mkdir()
    for lang in ("xx", "en", "ca", "yy"):
        (voices / f"{lang}.onnx").write_bytes(b"onnx")
    (voices / "yy.onnx.json").write_text("{}", encoding="utf-8")
    (voices / "not-a-tag.onnx").write_bytes(b"onnx")
    monkeypatch.chdir(tmp_path)
    _clean_voice_env(monkeypatch)
    built = {}
    monkeypatch.setattr(speech_api, "PiperEngine", lambda mapping: built.setdefault("v", mapping))

    speech_api.build_tts()

    assert built["v"] == {"en": "voices/en.onnx", "ca": "voices/ca.onnx",
                          "xx": "voices/xx.onnx", "yy": "voices/yy.onnx"}
    # The fallback for an unknown language stays the FIRST voice, English.
    assert next(iter(built["v"])) == "en"


def test_build_tts_maps_a_voice_a_setting_names(tmp_path, monkeypatch):
    custom = tmp_path / "elsewhere.onnx"
    custom.write_bytes(b"onnx")
    monkeypatch.chdir(tmp_path)
    _clean_voice_env(monkeypatch)
    monkeypatch.setenv("PIPER_VOICE_XX", str(custom))
    monkeypatch.setenv("PIPER_VOICE_YY", "")           # an empty line in .env names nothing
    built = {}
    monkeypatch.setattr(speech_api, "PiperEngine", lambda mapping: built.setdefault("v", mapping))

    speech_api.build_tts()

    assert built["v"] == {"xx": str(custom)}


def test_build_stt_maps_a_model_of_any_language_found_on_disk(tmp_path, monkeypatch):
    for name in ("vosk-ca", "vosk-en", "vosk-xx", "vosk-model-small-ca-0.4"):
        (tmp_path / "models" / name).mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    _clean_voice_env(monkeypatch)
    built = {}
    monkeypatch.setattr(speech_api, "VoskEngine", lambda mapping: built.setdefault("m", mapping))

    speech_api.build_stt()

    assert built["m"] == {"en": "models/vosk-en", "ca": "models/vosk-ca",
                          "xx": "models/vosk-xx"}


@pytest.mark.parametrize("lang, env", [
    ("xx", "PIPER_VOICE_XX"),
    ("ca", "PIPER_VOICE_CA"),
    ("xx-YY", None),
    ("../x", None),
    ("", None),
])
def test_only_a_bare_language_tag_names_a_voice_variable(lang, env):
    assert speech_api.piper_voice_env(lang) == env


def test_a_language_is_read_by_its_own_voice_not_the_fallback(monkeypatch, caplog):
    loaded = []
    fake = types.ModuleType("piper")
    fake.PiperVoice = type("PiperVoice", (), {
        "load": staticmethod(lambda path: loaded.append(path) or path)})
    monkeypatch.setitem(sys.modules, "piper", fake)
    tts = PiperTts({"en": "voices/en.onnx", "ca": "voices/ca.onnx"})

    assert tts._voice("ca") == "voices/ca.onnx"
    assert tts._voice("ca-ES") == "voices/ca.onnx"
    assert "falling back" not in caplog.text
    # ...and without it, the fallback is loud.
    assert PiperTts({"en": "voices/en.onnx"})._voice("ca") == "voices/en.onnx"
    assert "no Piper voice for language 'ca'" in caplog.text


@pytest.mark.parametrize("script", [_SH, _PS1])
def test_the_downloaders_name_the_kiosk_voices_in_their_header(script):
    header = next(line for line in script.read_text(encoding="utf-8").splitlines()
                  if "Piper voices (TTS)" in line)

    assert "{ca,en}.onnx" in header
