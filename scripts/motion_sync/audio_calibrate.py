"""Measure how far the speaker monitor leads the sound in the room, and the audio helpers.

The motion-sync run takes every sentence's time from the speaker's PipeWire monitor, which
sees samples before the sound card plays them. This plays a few chirps into the sink,
records the monitor and the robot's microphone at once, and reports how much later each
chirp reaches the microphone.

**The result is an upper bound on K**, the monitor-to-audible offset. The microphone path
has a capture latency of its own, and the sound has to cross the air to reach it, so the
true offset is this number or smaller. Only a camera with sound can split the two.

Nobody needs to be present, but the chirps are audible, and this needs the kiosk out of the
way so its landing page does not listen to them. Run it through the harness, which applies
the run's refusals and puts the kiosk back afterwards::

    python scripts/motion_sync_run.py --calibrate

The capture and correlation helpers here are shared with the run. Each capture is one
``pw-record`` writing to a file while a thread stamps each block with ``time.monotonic()``
as it arrives, so a sample index can be turned back into a time. numpy is on the robot.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

CAPTURE_RATE = 16000
CHIRP_S = 0.25
CHIRP_GAP_S = 1.0
CHIRP_COUNT = 8
CHIRP_LOW_HZ = 500.0
CHIRP_HIGH_HZ = 6000.0
CHIRP_AMPLITUDE = 0.25
# How strongly a chirp must correlate to be believed, in each recording.
MIN_MONITOR_STRENGTH = 0.2
MIN_MIC_STRENGTH = 0.1
# A reception stamp is a block's arrival, always a little late, never early. The earliest
# stamps within this window around a sample are the ones with the least scheduling in them.
STAMP_WINDOW_S = 5.0
_READ_BYTES = 8192
_NOTE = ("upper bound on the monitor-to-audible offset: it includes the microphone path's "
         "own capture latency and the sound's flight to the microphone")


def monitor_command(rate: int, sink: str = "") -> list[str]:
    """Record what a sink is playing, the default one unless named: mono s16, to stdout."""

    return (["pw-record", "-P", "{ stream.capture.sink=true }", "--rate", str(rate),
             "--channels", "1", "--format", "s16", "--latency", "20ms", "--raw"]
            + (["--target", sink] if sink else []) + ["-"])


def microphone_command(rate: int) -> list[str]:
    """Record the default source, which is the one the kiosk and the mic profile use."""

    return ["pw-record", "--rate", str(rate), "--channels", "1", "--format", "s16",
            "--latency", "20ms", "--raw", "-"]


def time_of_sample(stamps: list[tuple[int, float]], index: int, rate: int,
                   window_s: float = STAMP_WINDOW_S) -> float:
    """The monotonic time of sample ``index``, from ``(samples so far, arrival time)`` pairs.

    Each pair says the sample just before ``samples so far`` existed by that arrival time,
    so it bounds the wanted time from above. The earliest bound nearby is the tightest, and
    staying nearby keeps the capture's clock drift out of it.
    """

    if not stamps:
        raise ValueError("no capture stamps")
    window = int(window_s * rate)
    near = [pair for pair in stamps if abs(pair[0] - index) <= window] or stamps
    return min(arrival - (count - 1 - index) / rate for count, arrival in near)


class StampedCapture:
    """One running ``pw-record``: raw samples to ``path``, arrival stamps kept in memory."""

    def __init__(self, command: list[str], path: Path, rate: int, *,
                 spawn: Optional[Callable] = None, clock: Callable[[], float] = time.monotonic,
                 env: Optional[dict] = None) -> None:
        self.command = command
        self.path = Path(path)
        self.rate = int(rate)
        self.stamps: list[tuple[int, float]] = []
        self._bytes = 0
        self._spawn = spawn or subprocess.Popen
        self._clock = clock
        self._env = env
        self._proc = None
        self._thread: Optional[threading.Thread] = None
        self._head = bytearray()
        self._head_limit = 0
        self._lock = threading.Lock()

    def start(self, *, keep_head_s: float = 0.0) -> None:
        """Start recording, keeping the first ``keep_head_s`` readable from :meth:`head`."""

        self._head_limit = int(keep_head_s * self.rate) * 2
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Unbuffered: a block has to be stamped when it arrives, not when a buffer fills.
        self._proc = self._spawn(self.command, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, env=self._env, bufsize=0)
        self._thread = threading.Thread(target=self._read, name="capture", daemon=True)
        self._thread.start()

    def _read(self) -> None:
        stream = self._proc.stdout
        with self.path.open("wb") as sink:
            while True:
                try:
                    block = stream.read(_READ_BYTES)
                except (OSError, ValueError):
                    return
                if not block:
                    return
                arrived = self._clock()
                sink.write(block)
                with self._lock:
                    self._bytes += len(block)
                    self.stamps.append((self._bytes // 2, arrived))
                    if len(self._head) < self._head_limit:
                        self._head.extend(block)

    def samples(self) -> int:
        with self._lock:
            return self._bytes // 2

    def head(self) -> bytes:
        with self._lock:
            return bytes(self._head)

    def time_of(self, index: int) -> float:
        with self._lock:
            stamps = list(self.stamps)
        return time_of_sample(stamps, index, self.rate)

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3.0)
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)


def load_samples(path: Path):
    """A raw s16 file as floats in -1..1."""

    import numpy as np

    return np.fromfile(path, dtype="<i2").astype("float64") / 32768.0


def best_offset(haystack, needle) -> tuple[int, float]:
    """Where ``needle`` fits best in ``haystack``: ``(start index, how strongly)``.

    Strength is the peak of the normalized correlation, 0 to 1, with 1 an exact copy up to
    scale. FFT-based, so a minute of audio takes milliseconds.
    """

    import numpy as np

    haystack = np.asarray(haystack, dtype="float64")
    needle = np.asarray(needle, dtype="float64")
    if needle.size == 0 or haystack.size < needle.size:
        return 0, 0.0
    size = int(2 ** math.ceil(math.log2(haystack.size + needle.size)))
    spectrum = np.fft.rfft(haystack, size) * np.conj(np.fft.rfft(needle, size))
    correlation = np.fft.irfft(spectrum, size)[: haystack.size - needle.size + 1]
    # The energy under the needle at each position, for the normalization.
    running = np.cumsum(np.concatenate(([0.0], haystack * haystack)))
    energy = running[needle.size:] - running[:-needle.size]
    floor = max(float(np.max(energy)) * 1e-6, 1e-12)
    needle_norm = float(np.sqrt(np.sum(needle * needle))) or 1.0
    normalized = correlation / (np.sqrt(np.maximum(energy, floor)) * needle_norm)
    index = int(np.argmax(normalized))
    return index, float(max(0.0, min(1.0, normalized[index])))


def make_chirp(rate: int):
    """A linear sweep with short fades, as floats."""

    import numpy as np

    count = int(CHIRP_S * rate)
    times = np.arange(count) / rate
    sweep = (CHIRP_HIGH_HZ - CHIRP_LOW_HZ) / CHIRP_S
    wave = np.sin(2.0 * np.pi * (CHIRP_LOW_HZ * times + 0.5 * sweep * times * times))
    fade = max(1, int(0.005 * rate))
    envelope = np.ones(count)
    envelope[:fade] = np.linspace(0.0, 1.0, fade)
    envelope[-fade:] = np.linspace(1.0, 0.0, fade)
    return wave * envelope


def make_chirp_track(rate: int, count: int = CHIRP_COUNT):
    """Silence, then ``count`` chirps with silence between them: ``(s16 samples, starts)``."""

    import numpy as np

    chirp = make_chirp(rate) * CHIRP_AMPLITUDE
    gap = np.zeros(int(CHIRP_GAP_S * rate))
    pieces = [gap]
    starts = []
    cursor = gap.size
    for _ in range(count):
        starts.append(cursor)
        pieces += [chirp, gap]
        cursor += chirp.size + gap.size
    track = np.concatenate(pieces)
    return (track * 32767.0).astype("<i2"), starts


def write_wav(path: Path, samples, rate: int) -> None:
    """Mono s16 WAV."""

    import wave

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(samples.tobytes())


def monitor_to_mic_ms(monitor, mic, monitor_time: Callable[[int], float],
                      mic_time: Callable[[int], float], rate: int,
                      count: int = CHIRP_COUNT) -> list[float]:
    """Each chirp's microphone arrival minus its monitor arrival, in milliseconds.

    The chirps are found in the monitor first, because it holds them cleanly, and then in
    the part of the microphone recording around each. A chirp the microphone did not
    clearly hear is left out rather than guessed at.
    """

    chirp = make_chirp(rate)
    period = int((CHIRP_S + CHIRP_GAP_S) * rate)
    search = int(0.5 * rate)
    results: list[float] = []
    first, strength = best_offset(monitor[: int(4.0 * rate)], chirp)
    if strength < MIN_MONITOR_STRENGTH:
        return results
    for n in range(count):
        expected = first + n * period
        start = max(0, expected - search)
        at, strength = best_offset(monitor[start: expected + chirp.size + search], chirp)
        if strength < MIN_MONITOR_STRENGTH:
            continue
        in_monitor = start + at
        when = monitor_time(in_monitor)
        # Wherever the two captures started, the same moment is at this mic index.
        mic_index = int(round(in_monitor + (when - mic_time(in_monitor)) * rate))
        low = max(0, mic_index - search // 5)
        region = mic[low: mic_index + chirp.size + search]
        if region.size < chirp.size:
            continue
        at, strength = best_offset(region, chirp)
        if strength < MIN_MIC_STRENGTH:
            continue
        results.append((mic_time(low + at) - when) * 1000.0)
    return results


def median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return 0.5 * (ordered[middle - 1] + ordered[middle])


def calibration_record(offsets_ms: list[float]) -> dict:
    """``calibration.json``'s content from the chirps that were heard."""

    if not offsets_ms:
        raise ValueError("no chirp was heard on both the monitor and the microphone")
    spread = max(offsets_ms) - min(offsets_ms)
    note = (f"{_NOTE}; median of {len(offsets_ms)} chirps, spread {spread:.1f} ms")
    return {"k_ms": round(median(offsets_ms), 1), "note": note}


def calibrate(out_path: Path, work_dir: Path, *, spawn: Optional[Callable] = None,
              env: Optional[dict] = None, rate: int = CAPTURE_RATE,
              count: int = CHIRP_COUNT) -> dict:
    """Play the chirps, record both ends, write ``calibration.json``. Deletes its own audio."""

    track, _starts = make_chirp_track(rate, count)
    work_dir.mkdir(parents=True, exist_ok=True)
    wav = work_dir / "chirps.wav"
    monitor = StampedCapture(monitor_command(rate), work_dir / "chirps-monitor.raw", rate,
                             spawn=spawn, env=env)
    mic = StampedCapture(microphone_command(rate), work_dir / "chirps-mic.raw", rate,
                         spawn=spawn, env=env)
    runner = spawn or subprocess.Popen
    player = None
    try:
        write_wav(wav, track, rate)
        monitor.start()
        mic.start()
        time.sleep(0.5)
        player = runner(["pw-play", str(wav)], env=env)
        player.wait(timeout=len(track) / rate + 15.0)
        time.sleep(0.5)
        monitor.stop()
        mic.stop()
        offsets = monitor_to_mic_ms(load_samples(monitor.path), load_samples(mic.path),
                                    monitor.time_of, mic.time_of, rate, count)
        record = calibration_record(offsets)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        return record
    finally:
        if player is not None and player.poll() is None:
            player.terminate()
        monitor.stop()
        mic.stop()
        monitor.delete()
        mic.delete()
        wav.unlink(missing_ok=True)


if __name__ == "__main__":
    # Through the harness, so the same refusals and the same restore apply.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from motion_sync_run import main  # noqa: E402

    raise SystemExit(main(["--calibrate", *sys.argv[1:]]))
