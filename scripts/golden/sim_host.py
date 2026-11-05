"""The hostapp built in this process, with a simulated robot and every outside world stubbed.

It is the real application: the same ``_build_app``, on a real uvicorn, reached over HTTP the
way the kiosk reaches it. What is replaced is what a simulator has no equivalent of: the
robot client, the voice tap, the capture gain, the speech engines and the FIWARE broker.
Each run gets a process of its own, because the hostapp reads its settings at import.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable, Optional
import wave

from .fake_voice import FakeVoice
from .stub_orion import StubOrion

ROOT = Path(__file__).resolve().parents[2]
ADMIN_TOKEN = "golden-harness-admin-token-not-a-secret"
BADGE = "EMORI:anna-001"
SECOND_BADGE = "EMORI:marc-002"
ROSTER_FIXTURE = ROOT / "tests" / "fixtures" / "roster.sample.yaml"

# Settings that would make a simulated run depend on the machine it runs on.
_CLEARED_ENV = ("POKAMIND_ROBOT_TOKEN", "EMORI_SKILL_API", "EMORI_TT_OVERRIDES")
_CLEARED_PREFIXES = ("EMORI_SMTP_",)
_START_WAIT_S = 20.0


def sim_environment(workdir: Path, orion_url: str, extra: dict[str, str]) -> dict[str, str]:
    """What a simulated run's process must have set before it imports the hostapp."""

    settings = {
        "EMORI_POKAMIND_TOKEN_FILE": str(workdir / "secrets" / "no-token"),
        "EMORI_ROSTER": str(workdir / "configs" / "roster.yaml"),
        "EMORI_LANG_STATE_FILE": str(workdir / "artifacts" / "kiosk_lang.json"),
        "EMORI_ADMIN_TOKEN": ADMIN_TOKEN,
        "EMORI_ADDONS": "off",
        "EMORI_GESTURE": "0",
        "EMORI_VOICE_LINK": "0",
        "EMORI_RECORD": "1",
        "EMORI_MOTION_TRACE": "1",
        "EMORI_FIWARE": "1",
        "EMORI_LANG": "en",
        "ORION_URL": orion_url,
        "HOSTAPP_DEMO": "1",
    }
    settings.update(extra)
    return settings


def apply_environment(settings: dict[str, str]) -> None:
    for name in list(os.environ):
        if name in _CLEARED_ENV or name.startswith(_CLEARED_PREFIXES):
            del os.environ[name]
    os.environ.update(settings)


def prepare_workdir(workdir: Path) -> None:
    """A working directory with the tracked configuration and nothing a run left behind."""

    workdir.mkdir(parents=True, exist_ok=True)
    configs = workdir / "configs"
    if not configs.exists():
        shutil.copytree(ROOT / "configs", configs, ignore=shutil.ignore_patterns("site", "prod"))
    shutil.copyfile(ROSTER_FIXTURE, configs / "roster.yaml")


class _CallLog:
    """``client.jsonl``, in the shape the tracing client writes on a real host."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("a", encoding="utf-8", newline="\n")
        self._lock = threading.Lock()

    def write(self, method: str, t_start: float, args: dict, ret: Any) -> None:
        record = {"method": method, "t_start": t_start, "dur_s": time.monotonic() - t_start,
                  "args": args, "ret": ret}
        with self._lock:
            self._handle.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
            self._handle.flush()

    def close(self) -> None:
        with self._lock:
            self._handle.close()


def _simulated_robot_class():
    from emori_adapter.robot.client import DryRunRobotClient

    class SimRobot(DryRunRobotClient):
        """A robot that accepts everything and writes each call down."""

        def __init__(self, log: _CallLog) -> None:
            super().__init__()
            self._log = log
            self._moves = 0
            self._head_script: list[tuple[float, float]] = []
            self._script_start = 0.0

        # A head pushed by a hand, for a scenario that needs one: (seconds, yaw radians).
        def script_head(self, samples: list[tuple[float, float]]) -> None:
            self._head_script = samples
            self._script_start = time.monotonic()

        def _yaw(self) -> float:
            elapsed = time.monotonic() - self._script_start
            yaw = 0.0
            for at, value in self._head_script:
                if at > elapsed:
                    break
                yaw = value
            return yaw

        def health(self) -> bool:
            return True

        def wake_up(self) -> None:
            start = time.monotonic()
            super().wake_up()
            self._log.write("wake_up", start, {}, None)

        def set_motor_mode(self, mode: str) -> None:
            start = time.monotonic()
            super().set_motor_mode(mode)
            self._log.write("set_motor_mode", start, {"mode": mode}, None)

        def apply(self, intent) -> None:
            start = time.monotonic()
            super().apply(intent)
            self._log.write("apply", start, {"intent": intent.model_dump(mode="json")}, None)

        def read_state(self) -> Optional[dict]:
            start = time.monotonic()
            state = {"head_pose": {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": 0.0,
                                   "yaw": self._yaw()},
                     "body_yaw": 0.0, "antennas_position": [0.0, 0.0]}
            self._log.write("read_state", start, {}, "state")
            return state

        def set_gaze(self, enabled: bool) -> bool:
            start = time.monotonic()
            result = super().set_gaze(enabled)
            self._log.write("set_gaze", start, {"enabled": enabled}, result)
            return result

        def set_head_reserved(self, reserved: bool) -> bool:
            start = time.monotonic()
            result = super().set_head_reserved(reserved)
            self._log.write("set_head_reserved", start, {"reserved": reserved}, result)
            return result

        def play_recorded_move(self, name: str, *, supersede: bool = False) -> Optional[str]:
            start = time.monotonic()
            self._moves += 1
            move_id = f"sim-move-{self._moves}"
            args: dict[str, Any] = {"name": name}
            if supersede:
                args["supersede"] = True
            self._log.write("play_recorded_move", start, args, move_id)
            return move_id

        def stop_recorded_move(self, uuid: str) -> None:
            start = time.monotonic()
            self._log.write("stop_recorded_move", start, {"uuid": uuid}, None)

    return SimRobot


class _StubTts:
    """Speech out with the same bytes for the same words, in a fraction of Piper's time."""

    available = True

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def synth_path(self, text: str, lang: str = "en") -> Optional[str]:
        import hashlib

        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._directory / (hashlib.sha256(f"{lang}:{text}".encode()).hexdigest() + ".wav")
        if not path.exists():
            with wave.open(str(path), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(16000)
                out.writeframes(bytes(1600) + text.encode("utf-8"))
        return str(path)


class _StubStt:
    """Recognition that answers from the input's length, so a fixed input has a fixed answer."""

    available = True

    def transcribe(self, pcm: bytes, lang: str = "en", sample_rate: int = 16000) -> str:
        return f"heard {len(pcm)} bytes in {lang}"

    def transcribe_grammar(self, pcm: bytes, phrases: list[str], lang: str = "en",
                           sample_rate: int = 16000) -> str:
        return phrases[len(pcm) % len(phrases)] if phrases else ""

    def transcribe_grammar_conf(self, pcm: bytes, phrases: list[str], lang: str = "en",
                                sample_rate: int = 16000) -> tuple[str, float]:
        return self.transcribe_grammar(pcm, phrases, lang, sample_rate), 0.9


def _mic_recorder_class():
    from emori_adapter.hostapp.mic_profile import MicProfile

    class MicRecorder(MicProfile):
        """The real profile logic with no ``wpctl`` behind it, writing each change down."""

        def __init__(self, path: Path) -> None:
            super().__init__(run=lambda *a, **k: SimpleNamespace(returncode=0))
            self._path = path

        def set_profile(self, profile: str) -> bool:
            changed = super().set_profile(profile)
            with self._path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps({"t": time.monotonic(), "profile": profile,
                                         "applied": changed}) + "\n")
            return changed

    return MicRecorder


class SimHost:
    """One hostapp on a free loopback port, writing into ``run_dir`` and ``workdir``."""

    mode = "sim"

    def __init__(self, *, run_dir: Path, workdir: Path,
                 extension_factory: Optional[Callable[[], Any]] = None,
                 overrides: Optional[dict] = None, orion: Optional[StubOrion] = None) -> None:
        self.run_dir = run_dir
        self.artifacts = workdir / "artifacts"
        self._workdir = workdir
        self._extension_factory = extension_factory
        self._overrides = overrides or {}
        self._orion = orion
        self.voice: Optional[FakeVoice] = None
        self.robot: Any = None
        self._log: Optional[_CallLog] = None
        self._server: Any = None
        self._thread: Optional[threading.Thread] = None
        self._port = 0
        self.base_url = ""

    @property
    def admin_headers(self) -> dict[str, str]:
        from emori_adapter.hostapp import admin_api

        return {admin_api.TOKEN_HEADER: ADMIN_TOKEN}

    def _speak(self, text: str, lang: str) -> None:
        with (self.run_dir / "speech.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps({"t": time.monotonic(), "text": text, "lang": lang},
                                    ensure_ascii=False) + "\n")

    def _build(self):
        from emori_adapter.hostapp import server

        self._log = self._log or _CallLog(self.run_dir / "client.jsonl")
        self.robot = _simulated_robot_class()(self._log)
        self.voice = FakeVoice()
        overrides: dict[str, Any] = {
            "robot_client": self.robot,
            "voice": self.voice,
            "mic_profile": _mic_recorder_class()(self.run_dir / "mic.jsonl"),
            "tts": _StubTts(self._workdir / "artifacts" / "stub_tts"),
            "stt": _StubStt(),
            "speak": self._speak,
            "scope_background": False,
        }
        if self._extension_factory is not None:
            overrides["extension"] = self._extension_factory()
        overrides.update(self._overrides)
        return server._build_app(overrides)

    def _serve(self) -> None:
        import uvicorn

        app = self._build()
        config = uvicorn.Config(app, host="127.0.0.1", port=self._port, log_level="warning",
                                ws="wsproto", loop="asyncio", http="h11")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, name="sim-host", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + _START_WAIT_S
        while not self._server.started:
            if time.monotonic() >= deadline or not self._thread.is_alive():
                raise RuntimeError("the simulated hostapp did not start")
            time.sleep(0.02)
        sockets = self._server.servers[0].sockets
        self._port = sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self._port}"

    def start(self) -> None:
        self._serve()

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=15.0)
        if self.voice is not None:
            self.voice.stop()
        if self._log is not None:
            self._log.close()
            self._log = None
        if self._orion is not None:
            self._orion.stop()

    def restart(self) -> None:
        """Stop and build again on the same port, as a restart of the service would."""

        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=15.0)
        if self.voice is not None:
            self.voice.stop()
        self._serve()

    def collect(self, run_dir: Path) -> None:
        """Everything the host wrote elsewhere has already gone straight into ``run_dir``."""
