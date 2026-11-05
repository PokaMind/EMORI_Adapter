"""Sentences with a voice under them, on the same running order in the simulator and on a robot.

Recordings hold event times and no audio offsets, so an old session cannot be replayed under
a voice. ``scripts/motion_sync_run.py`` already solves the same problem: it lays a run of
sentences out in one WAV, with each event placed by the offset the fixture sets, and posts
them the way the page does. This module reuses that layout, player and scheduler.

:class:`SimTrack` plays the layout on a :class:`~golden.fake_voice.FakeVoice`, with sentence
lengths estimated from the text. :class:`RobotTrack` renders and plays the WAV through
``pw-play``, hears it back on the speaker monitor, and times the events from that.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable, Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import motion_sync_run as sync_run  # noqa: E402
from motion_sync import audio_calibrate, fixture  # noqa: E402

SIM_RATE = 16000
_AUDIO_CACHE = Path("artifacts/golden/audio")
_CHARS_PER_S = 15.0
_FINISH_MARGIN_S = 10.0
_CAPTURE_START_S = 5.0


def sentences_for(lang: str, cases: Iterable[str],
                  continuing: Iterable[str] = ()) -> tuple[fixture.Sentence, ...]:
    """Those cases of the fixture, in the order asked for. A name it lacks is a mistake.

    ``continuing`` names the ones a reply goes on after, which then post no end of turn.
    """

    by_case = {sentence.case: sentence for sentence in fixture.build(lang)}
    goes_on = frozenset(continuing)
    return tuple(replace(by_case[case], turn_end=False) if case in goes_on else by_case[case]
                 for case in cases)


def estimated_lengths(sentences: Iterable[fixture.Sentence], *, rate: int = SIM_RATE,
                      speed: float = 1.0) -> dict[str, tuple[int, ...]]:
    """How long each part would take to say. Enough for a voice nobody hears."""

    return {sentence.case: tuple(round(len(part) / (_CHARS_PER_S * speed) * rate)
                                 for part in sentence.parts)
            for sentence in sentences if sentence.audio}


def voice_spans(placements: list[sync_run.Placement], rate: int) -> list[tuple[float, float]]:
    """When the voice is audible, in seconds from sample 0: one span per spoken part."""

    spans = []
    for place in placements:
        sentence = place.sentence
        if not sentence.audio:
            continue
        ends = [start - round(sentence.pause_s * rate) for start in place.part_starts[1:]]
        ends.append(place.end)
        spans.extend((start / rate, end / rate) for start, end in zip(place.part_starts, ends))
    return spans


def mark_voice(plan: "TrackPlan", origin: float, mark: Callable[..., None]) -> None:
    """Write down the voice's plan: how long it spoke, and where each sentence's voice begins."""

    for start_s, end_s in plan.spans():
        mark("voice_span", t=origin + start_s, dur=end_s - start_s)
    for place in plan.placements:
        if place.part_starts and place.sentence.posted:
            mark("voice_onset", t=origin + place.part_starts[0] / plan.rate)


@dataclass
class TrackPlan:
    placements: list[sync_run.Placement]
    schedule: list[sync_run.Post]
    rate: int

    @property
    def seconds(self) -> float:
        return sync_run.track_seconds(self.placements, self.rate)

    def spans(self) -> list[tuple[float, float]]:
        return voice_spans(self.placements, self.rate)


def plan_track(sentences: tuple[fixture.Sentence, ...], lengths: dict[str, tuple[int, ...]],
               rate: int) -> TrackPlan:
    placements = sync_run.lay_out(sentences, lengths, rate)
    return TrackPlan(placements, sync_run.build_schedule(placements, rate), rate)


class SimTrack:
    """The layout on a fake voice. The events go out over HTTP as they do on a robot."""

    def __init__(self, voice: Any, *, speed: float = 1.0) -> None:
        self._voice = voice
        self._speed = speed

    def play(self, cases: Iterable[str], lang: str, client_factory: Callable[[], Any],
             mark: Callable[..., None], *, continuing: Iterable[str] = ()) -> int:
        sentences = sentences_for(lang, cases, continuing)
        plan = plan_track(sentences, estimated_lengths(sentences, speed=self._speed), SIM_RATE)
        origin = time.monotonic()
        self._voice.play(plan.spans(), at=origin)
        mark_voice(plan, origin, mark)
        scheduler = sync_run.Scheduler(plan.schedule, origin, client_factory, lang)
        scheduler.start()
        scheduler.join(plan.seconds + _FINISH_MARGIN_S)
        scheduler.stop()
        self._voice.wait_done(plan.seconds + _FINISH_MARGIN_S)
        return scheduler.failed


class RobotTrack:
    """The layout as one WAV on the robot's speaker.

    The time the WAV started comes from hearing its pilot sweep on the monitor, as a
    motion-sync run does, so the events line up with what really came out.
    """

    def __init__(self, *, root: Path, run_dir: Path, sink: str = "",
                 voice_file: Optional[Path] = None, machine: Any = None,
                 capture_factory: Optional[Callable[..., Any]] = None) -> None:
        self._root = root
        self._run_dir = run_dir
        self._sink = sink
        self._voice_file = voice_file
        self._machine = machine or sync_run.Machine(root)
        self._capture_factory = capture_factory or audio_calibrate.StampedCapture

    def _render(self, sentences: tuple[fixture.Sentence, ...], lang: str) -> sync_run.Rendered:
        voice = self._voice_file or sync_run.voice_path(self._machine, lang, None)
        if not Path(voice).exists():
            raise sync_run.Refused(f"no Piper voice for {lang}")
        # A directory per running order: the renderer drops whatever else its directory holds
        # for this language, and rendering costs a good fraction of the track's length.
        order = hashlib.sha256("\n".join(s.case for s in sentences).encode("utf-8")).hexdigest()
        return sync_run.render(sentences, lang, self._root / _AUDIO_CACHE / order[:12],
                               lambda: sync_run.piper_synth(str(voice)),
                               voice_id=Path(voice).name)

    def _capture(self, rate: int, name: str, sink: str = "") -> Any:
        capture = self._capture_factory(audio_calibrate.monitor_command(rate, sink),
                                        self._run_dir / name, rate, env=self._machine.env())
        capture.start(keep_head_s=sync_run.PILOT_TIMEOUT_S + 1.0)
        deadline = time.monotonic() + _CAPTURE_START_S
        while capture.samples() == 0:
            if time.monotonic() >= deadline:
                capture.stop()
                raise sync_run.RunAborted("the speaker monitor delivered nothing")
            time.sleep(0.02)
        return capture

    def play(self, cases: Iterable[str], lang: str, client_factory: Callable[[], Any],
             mark: Callable[..., None], *, continuing: Iterable[str] = ()) -> int:
        sentences = sentences_for(lang, cases, continuing)
        rendered = self._render(sentences, lang)
        plan = TrackPlan(rendered.placements,
                         sync_run.build_schedule(rendered.placements, rendered.rate),
                         rendered.rate)
        capture = self._capture(rendered.rate, "track-capture.raw")
        ahead = self._capture(rendered.rate, "ahead-capture.raw", self._sink) if self._sink else None
        player = self._machine.spawn(sync_run.play_command(rendered.wav, self._sink),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            _, t_wav0 = sync_run.find_pilot(capture, rendered.rate)
            t_events = t_wav0 if ahead is None else sync_run.find_pilot(ahead, rendered.rate)[1]
            mark_voice(plan, t_wav0, mark)
            scheduler = sync_run.Scheduler(plan.schedule, t_events, client_factory, lang)
            scheduler.start()
            scheduler.join(plan.seconds + _FINISH_MARGIN_S)
            scheduler.stop()
            player.wait(timeout=_FINISH_MARGIN_S)
            return scheduler.failed
        finally:
            if player.poll() is None:
                player.terminate()
            for stream in (capture, ahead):
                if stream is not None:
                    stream.stop()
                    stream.delete()
