"""A speech model that cannot be downloaded never ends the install, nor stays half there.

``install.sh`` step 3 runs ``scripts/get_voice_models.sh`` under ``set -e``, so one
connection cut mid-download must not end the WHOLE install, nor leave a truncated voice
that a later run takes for "already present". Each file is fetched bounded and retried
into ``<file>.part``, checked, and renamed into place only when whole; a voice is present
only as a whole pair; a failure is a WARNING (the language falls back) and readycheck's
tts gate names it. A further voice given as an argument gets exactly the same treatment,
and these tests use two of them.

The downloaders are RUN here, against a stub ``curl`` (bash) and a stub
``Invoke-WebRequest`` (PowerShell), in a scratch install root.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from emori_adapter.hostapp import readycheck
from emori_adapter.hostapp.readycheck import PASS, WARN, FAIL, HttpResult, evaluate
from tests.test_readycheck import _by_name, _FakeProbe, _healthy

_ROOT = Path(__file__).resolve().parents[1]
_SH = _ROOT / "scripts" / "get_voice_models.sh"
_PS1 = _ROOT / "scripts" / "get_voice_models.ps1"
_INSTALL = _ROOT / "deploy" / "robot" / "install.sh"
_LANGS = ("ca", "en", "yy", "xx")
# Two further voices, as an add-on's install hook would ask for them.
_EXTRA = ("yy=yy/yy_YY/test/medium/yy_YY-test-medium", "xx=xx/xx_XX/test/medium/xx_XX-test-medium")
_ONNX_BYTES = 1_000_001  # just over the downloader's "this is a model, not a page" floor


def _bash():
    found = shutil.which("bash")
    if found is None or "system32" in found.lower():  # the WSL launcher is not a shell here
        return None
    return found


# curl, stubbed: logs its arguments, answers --version (so every option is "known"), and
# for a URL containing $CURL_FAIL writes a few bytes and exits 18 -- a transfer cut short.
_FAKE_CURL = r"""#!/bin/sh
echo "$*" >> "$CURL_LOG"
out=""; url=""; prev=""
for a in "$@"; do
  [ "$prev" = "-o" ] && out="$a"
  case "$a" in --version) echo "curl 8.14.1 (stub)"; exit 0 ;; *://*) url="$a" ;; esac
  prev="$a"
done
if [ -n "$CURL_FAIL" ]; then
  case "$url" in *"$CURL_FAIL"*) printf 'partial' > "$out"; exit 18 ;; esac
fi
case "$url" in
  *.onnx.json*) printf '{"audio": {"sample_rate": 22050}}' > "$out" ;;
  *.onnx*) head -c 1000001 /dev/zero > "$out" ;;
  *) printf 'zip' > "$out" ;;
esac
exit 0
"""


class _Root:
    """A scratch /opt/emori with the downloader in it and the Vosk models in place."""

    def __init__(self, tmp_path):
        bash = _bash()
        if bash is None:
            pytest.skip("no bash")
        self.bash = bash
        self.root = tmp_path / "emori"
        (self.root / "scripts").mkdir(parents=True)
        shutil.copy2(_SH, self.root / "scripts" / "get_voice_models.sh")
        self.voices = self.root / "voices"
        self.voices.mkdir()
        for lang in ("ca", "en"):
            (self.root / "models" / f"vosk-{lang}").mkdir(parents=True)
        self.stubs = tmp_path / "stubs"
        self.stubs.mkdir()
        self._stub("curl", _FAKE_CURL)
        self._stub("python3", f'exec "{Path(sys.executable).as_posix()}" "$@"\n')
        self.log = tmp_path / "curl.log"

    def _stub(self, name: str, body: str) -> None:
        path = self.stubs / name
        text = body if body.startswith("#!") else "#!/bin/sh\n" + body
        path.write_text(text, encoding="ascii", newline="\n")
        path.chmod(0o755)

    def install_voice(self, lang: str) -> None:
        (self.voices / f"{lang}.onnx").write_bytes(b"\0" * _ONNX_BYTES)
        (self.voices / f"{lang}.onnx.json").write_text('{"audio": {}}', encoding="utf-8")

    def run(self, *, fail: str = "", extra=_EXTRA) -> subprocess.CompletedProcess:
        env = {**os.environ, "PATH": f"{self.stubs}{os.pathsep}{os.environ['PATH']}",
               "CURL_LOG": self.log.as_posix(), "CURL_FAIL": fail}
        return subprocess.run(
            [self.bash, (self.root / "scripts" / "get_voice_models.sh").as_posix(), *extra],
            capture_output=True, encoding="utf-8", errors="replace", env=env, timeout=120,
            check=False)

    def fetched(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def files(self) -> list[str]:
        return sorted(p.name for p in self.voices.iterdir())


def _whole_pairs(root: _Root, langs) -> bool:
    return all((root.voices / f"{lang}.onnx").stat().st_size == _ONNX_BYTES
               and (root.voices / f"{lang}.onnx.json").stat().st_size > 0 for lang in langs)


# --- get_voice_models.sh ------------------------------------------------------------------


def test_with_no_argument_only_the_kiosk_voices_are_fetched(tmp_path):
    root = _Root(tmp_path)

    result = root.run(extra=())

    assert result.returncode == 0, result.stderr
    assert _whole_pairs(root, ("ca", "en"))
    assert root.files() == ["ca.onnx", "ca.onnx.json", "en.onnx", "en.onnx.json"]


def test_an_argument_that_names_no_language_is_a_warning_not_a_download(tmp_path):
    root = _Root(tmp_path)

    result = root.run(extra=("../x=some/path",))

    assert result.returncode == 1
    assert "is not <lang>=<piper-voices path>" in result.stderr
    assert root.files() == ["ca.onnx", "ca.onnx.json", "en.onnx", "en.onnx.json"]


def test_a_further_voice_already_on_a_robot_is_left_alone(tmp_path):
    root = _Root(tmp_path)
    for lang in _LANGS:
        root.install_voice(lang)

    assert root.run(extra=()).returncode == 0
    assert root.run().returncode == 0
    assert _whole_pairs(root, _LANGS)
    assert "-medium" not in root.fetched()


def test_a_download_cut_short_is_a_warning_and_leaves_no_half_voice(tmp_path):
    root = _Root(tmp_path)

    result = root.run(fail="xx_XX-test-medium.onnx?")

    # Every other voice is installed; the script says what is missing and exits 1 (a
    # caller can tell) instead of dying mid-loop under set -e with curl's 18.
    assert result.returncode == 1, result.stderr
    assert _whole_pairs(root, ("ca", "en", "yy"))
    assert "WARNING: the Piper voice 'xx' could not be downloaded" in result.stderr
    assert "NOT installed: voice xx" in result.stderr
    # Nothing of that voice is left for the hostapp to map: no truncated model,
    # no orphan config, no .part.
    assert not [name for name in root.files() if name.startswith("xx.")]
    assert not list(root.root.rglob("*.part"))


def test_a_vosk_model_that_cannot_be_unpacked_is_a_warning_too(tmp_path):
    root = _Root(tmp_path)
    shutil.rmtree(root.root / "models" / "vosk-en")

    result = root.run()  # the stub's "zip" is no zip

    assert result.returncode == 1
    assert "WARNING: the Vosk model 'en' could not be downloaded or unpacked" in result.stderr
    assert _whole_pairs(root, _LANGS)
    assert sorted(p.name for p in (root.root / "models").iterdir()) == ["vosk-ca"]


def test_a_truncated_voice_or_a_stale_part_is_fetched_again(tmp_path):
    root = _Root(tmp_path)
    root.install_voice("ca")
    root.install_voice("en")
    # What the old downloader left after a cut: the model half there, its config never.
    (root.voices / "xx.onnx").write_bytes(b"partial")
    # A whole-looking pair beside a download of it that never finished.
    root.install_voice("yy")
    (root.voices / "yy.onnx.part").write_bytes(b"part")

    result = root.run()

    assert result.returncode == 0, result.stderr
    assert "Piper voice ca already present" in result.stdout
    fetched = root.fetched()
    assert "xx_XX-test-medium.onnx?" in fetched and "yy_YY-test-medium.onnx?" in fetched
    assert "ca_ES" not in fetched and "en_US" not in fetched
    assert _whole_pairs(root, _LANGS)
    assert not list(root.root.rglob("*.part"))


def test_a_whole_voice_beside_a_stale_part_survives_a_failed_refetch(tmp_path):
    root = _Root(tmp_path)
    for lang in _LANGS:
        root.install_voice(lang)
    (root.voices / "xx.onnx.part").write_bytes(b"part")

    result = root.run(fail="xx_XX-test-medium")

    assert result.returncode == 0, result.stderr
    assert _whole_pairs(root, _LANGS)
    assert "WARNING" not in result.stderr
    assert not list(root.root.rglob("*.part"))


def test_an_error_page_or_an_unparsable_config_is_not_a_voice(tmp_path):
    root = _Root(tmp_path)
    for lang in ("ca", "en", "yy"):
        root.install_voice(lang)
    (root.voices / "xx.onnx").write_bytes(b"<html>captive portal</html>")
    (root.voices / "xx.onnx.json").write_text("<html>", encoding="utf-8")

    assert root.run().returncode == 0
    assert _whole_pairs(root, ("xx",))
    assert (root.voices / "xx.onnx.json").read_text(encoding="utf-8").startswith("{")


def test_every_download_is_bounded_retried_and_lands_in_a_part_file(tmp_path):
    root = _Root(tmp_path)

    assert root.run().returncode == 0
    downloads = [line for line in root.fetched().splitlines() if "://" in line]
    assert len(downloads) == 2 * len(_LANGS)
    for line in downloads:
        for flag in ("--connect-timeout 20", "--max-time 900", "--retry 3",
                     "--retry-connrefused", "--retry-all-errors"):
            assert flag in line, (flag, line)
        assert line.split(" -o ", 1)[1].endswith(".part")


def test_a_curl_without_the_newer_retry_options_still_downloads(tmp_path):
    root = _Root(tmp_path)
    root._stub("curl", _FAKE_CURL.replace(
        'case "$a" in --version)',
        'case "$a" in --retry-connrefused|--retry-all-errors) [ -z "$url" ] && '
        '[ -z "$out" ] && exit 2 ;; esac\n  case "$a" in --version)'))

    assert root.run().returncode == 0
    assert _whole_pairs(root, _LANGS)
    assert "--retry-connrefused" not in "".join(
        line for line in root.fetched().splitlines() if "://" in line)


# --- install.sh step 3 --------------------------------------------------------------------


def _step3() -> str:
    text = _INSTALL.read_text(encoding="utf-8")
    return text[text.index('echo "== [3/10] speech models =="'):text.index("# 4. Configs")]


@pytest.mark.parametrize("rc", [1, 18])
def test_install_goes_on_when_a_speech_model_could_not_be_downloaded(tmp_path, rc):
    bash = _bash()
    if bash is None:
        pytest.skip("no bash")
    root = tmp_path / "emori"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "get_voice_models.sh").write_text(
        f"echo 'WARNING: voice xx'; exit {rc}\n", encoding="ascii", newline="\n")
    script = tmp_path / "step3.sh"
    script.write_text("\n".join(["set -euo pipefail", f"cd '{root.as_posix()}'", _step3(),
                                 "echo STEP-4-REACHED"]), encoding="utf-8", newline="\n")

    result = subprocess.run([bash, script.as_posix()], capture_output=True, encoding="utf-8",
                            errors="replace", timeout=60, check=False)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "STEP-4-REACHED" in result.stdout
    assert "!! WARNING: not every speech model could be downloaded" in result.stdout
    assert "bash scripts/get_voice_models.sh" in result.stdout


# --- get_voice_models.ps1 -----------------------------------------------------------------

# Invoke-WebRequest and Start-Sleep, stubbed as functions (a function outranks a cmdlet):
# a URL containing $env:IWR_FAIL writes a few bytes and throws.
_PS_STUBS = r"""
function Invoke-WebRequest { param($Uri, $OutFile, $TimeoutSec, [switch]$UseBasicParsing)
  Add-Content -Path $env:IWR_LOG -Value "$Uri -> $OutFile timeout=$TimeoutSec"
  if ($env:IWR_FAIL -and $Uri.Contains($env:IWR_FAIL)) {
    [IO.File]::WriteAllText($OutFile, 'partial'); throw 'connection reset' }
  if ($Uri.Contains('.onnx.json')) { [IO.File]::WriteAllText($OutFile, '{"audio": {}}') }
  else { [IO.File]::WriteAllBytes($OutFile, (New-Object byte[] 1000001)) }
}
function Start-Sleep { param($Seconds) }
"""


def _powershell():
    return shutil.which("powershell") or shutil.which("pwsh")


def _run_ps1(tmp_path, *, fail: str = ""):
    shell = _powershell()
    if shell is None:
        pytest.skip("no PowerShell")
    root = tmp_path / "emori"
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy2(_PS1, root / "scripts" / "get_voice_models.ps1")
    for lang in ("ca", "en"):
        (root / "models" / f"vosk-{lang}").mkdir(parents=True, exist_ok=True)
    log = tmp_path / "iwr.log"
    wrapper = tmp_path / "run.ps1"
    extra = " ".join(f"'{voice}'" for voice in _EXTRA)
    wrapper.write_text(_PS_STUBS + f"\n& '{root / 'scripts' / 'get_voice_models.ps1'}' {extra}\n"
                       "exit $LASTEXITCODE\n", encoding="utf-8")
    env = {**os.environ, "IWR_LOG": str(log), "IWR_FAIL": fail}
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                             "Bypass", "-File", str(wrapper)], capture_output=True,
                            encoding="utf-8", errors="replace", env=env, timeout=180,
                            check=False)
    return root, log, result


def test_the_windows_downloader_also_warns_and_leaves_no_half_voice(tmp_path):
    root, log, result = _run_ps1(tmp_path, fail="xx_XX-test-medium.onnx?")

    assert result.returncode == 1, result.stdout + result.stderr
    voices = root / "voices"
    for lang in ("ca", "en", "yy"):
        assert (voices / f"{lang}.onnx").stat().st_size == _ONNX_BYTES
        assert (voices / f"{lang}.onnx.json").exists()
    assert not [p.name for p in voices.iterdir() if p.name.startswith("xx.")]
    assert "could not be downloaded" in result.stdout + result.stderr
    # Fetched into .part, with a timeout, and retried before giving up.
    lines = log.read_text(encoding="utf-8").splitlines()
    assert all(".part timeout=" in line for line in lines)
    assert sum("xx_XX-test-medium.onnx?" in line for line in lines) == 4

    # ...and the next run fetches only what is missing.
    log.unlink()
    _root, log, again = _run_ps1(tmp_path)
    assert again.returncode == 0, again.stdout + again.stderr
    fetched = log.read_text(encoding="utf-8")
    assert "xx_XX" in fetched and "ca_ES" not in fetched
    assert (voices / "xx.onnx").stat().st_size == _ONNX_BYTES

    # A whole voice that only a leftover .part made look unfinished survives a failed
    # re-download.
    (voices / "xx.onnx.part").write_bytes(b"part")
    _root, log, stale = _run_ps1(tmp_path, fail="xx_XX-test-medium")
    assert stale.returncode == 0, stale.stdout + stale.stderr
    assert (voices / "xx.onnx").stat().st_size == _ONNX_BYTES
    assert not (voices / "xx.onnx.part").exists()


# --- readycheck names the missing voice ---------------------------------------------------


def test_a_missing_voice_is_a_tts_warning_naming_it():
    gate = _by_name(evaluate(_healthy(voices_missing=("xx",))))["tts"]

    assert (gate.status, gate.code) == (WARN, "voice_missing")
    assert "voices/xx.onnx" in gate.hint and "get_voice_models.sh" in gate.hint
    assert readycheck.exit_code(evaluate(_healthy(voices_missing=("xx",)))) == 0
    assert _by_name(evaluate(_healthy()))["tts"].status == PASS


def test_a_dead_tts_still_fails_and_names_the_missing_voice_too():
    gate = _by_name(evaluate(_healthy(tts=HttpResult(True, 503),
                                      voices_missing=("ca",))))["tts"]

    assert (gate.status, gate.code) == (FAIL, "tts_unavailable")
    assert "voices/ca.onnx" in gate.hint


class _VoiceProbe(_FakeProbe):
    def __init__(self, environ, *, missing=(), **kwargs):
        super().__init__(environ, **kwargs)
        self._missing = missing
        self.asked: list[tuple[str, ...]] = []

    def piper_voices_missing(self, languages):
        self.asked.append(tuple(languages))
        return tuple(lang for lang in languages if lang in self._missing)


def test_collect_asks_for_the_kiosk_voices_only_with_no_add_on():
    plain = _VoiceProbe({"EMORI_LANG": "en"}, missing=("xx", "yy"))
    assert readycheck.collect(plain).voices_missing == ()
    assert plain.asked == [("ca", "en")]


def test_the_real_probe_wants_the_model_and_its_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "voices").mkdir()
    for lang in ("ca", "en"):
        (tmp_path / "voices" / f"{lang}.onnx").write_bytes(b"onnx")
        (tmp_path / "voices" / f"{lang}.onnx.json").write_text("{}", encoding="utf-8")
    (tmp_path / "voices" / "yy.onnx").write_bytes(b"onnx")  # config never arrived
    custom = tmp_path / "elsewhere.onnx"
    custom.write_bytes(b"onnx")
    (tmp_path / "elsewhere.onnx.json").write_text("{}", encoding="utf-8")
    for name in ("PIPER_VOICE_CA", "PIPER_VOICE_EN", "PIPER_VOICE_YY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PIPER_VOICE_XX", str(custom))

    missing = readycheck.SystemProbe().piper_voices_missing(("xx", "ca", "en", "yy", "not a tag"))

    assert missing == ("yy",)
