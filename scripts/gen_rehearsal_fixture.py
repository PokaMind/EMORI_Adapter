"""Generate tests/fixtures/rehearsal_session.jsonl from the live-session recording.

``tests/fixtures/live_session.jsonl`` is the faithful replay fixture: it is what the
staged Pokamind contract actually put on the wire, double delivery and all. It is
deliberately NOT enough to rehearse the motion-owner flip, because two things the ROS
behaviour lane keys on are missing from it:

* ``cue_category`` -- the sentence's emotional family. The live fixture predates the
  field, so every utterance replays as untagged, and the ROS engagement node's cue
  branch (``CUE_MOTIONS``: tension -> calm_settle, levity -> antenna_wiggle, ...) is
  never exercised at all. The rehearsal asserts ``cue >= 5`` on that node's ledger.
* ``pokamind_tts_ended`` -- the end-of-sentence marker. Without it the speaking window
  is only ever opened, never closed, so the "settle exactly once when a cue was open"
  path never runs and every later cycle looks yielded.

So this derives a rehearsal fixture from the live one rather than inventing a second
recording: same 24 analysis cycles, same seven utterances, same completion, with the
family rotated through the six wire families and an end marker three seconds after
each sentence. Everything stays inside one ~46 s span, which is what keeps a rehearsal
under a minute of replay.

Deterministic by construction -- no clock, no randomness, no dict ordering that is not
the source file's -- because ``tests/test_rehearsal_tooling.py`` asserts the file on
disk equals this module's output. Regenerate after changing either:

    .venv-reachy\\Scripts\\python.exe scripts/gen_rehearsal_fixture.py
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
SOURCE = _ROOT / "tests" / "fixtures" / "live_session.jsonl"
OUT = _ROOT / "tests" / "fixtures" / "rehearsal_session.jsonl"

# The six families Pokamind actually authors, in the order the cue lexicon lists them.
# Rotated per utterance so one replay covers every branch of the ROS node's cue table
# (and, at seven utterances, wraps once -- which is also worth exercising).
CUE_FAMILIES = ("tension", "levity", "energy", "intimacy", "softening", "release")
# How long after a sentence its end marker arrives. Long enough to be a real speaking
# window (the node's budget is clamped at cue_max_s = 4 s), short enough that the
# marker still lands between two 2 s analysis cycles rather than after the next
# sentence -- which is what an end marker means on the wire.
TTS_ENDED_AFTER_S = 3.0

ANALYSIS_EVENT = "pokamind_analysis_update"
UTTERANCE_EVENT = "pokamind_ai_utterance"
TTS_ENDED_EVENT = "pokamind_tts_ended"
COMPLETED_EVENT = "pokamind_session_completed"


def parse_timestamp(raw: str) -> dt.datetime:
    """Read the wire's ``...Z`` ISO stamp. Naive UTC is enough: we only add seconds."""

    return dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))


def format_timestamp(when: dt.datetime) -> str:
    """Back to the exact shape the source file uses: milliseconds and a literal Z."""

    return when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") \
        + "%03dZ" % (when.microsecond // 1000)


def build(envelopes: list[dict]) -> list[dict]:
    """Derive the rehearsal envelopes from the live-session ones.

    Order: everything that carries a timestamp is sorted by it with a STABLE sort, so
    an utterance that shares its cycle's stamp still follows that cycle exactly as it
    does on the wire, and a 3 s end marker slots between the two cycles it belongs
    between. The completion envelope has no timestamp and stays last.
    """

    timed: list[dict] = []
    completion: list[dict] = []
    utterance_i = 0
    for env in envelopes:
        kind = env.get("type")
        if kind == COMPLETED_EVENT:
            completion.append(dict(env))
            continue
        item = dict(env)
        if kind == UTTERANCE_EVENT:
            # Every sentence gets a family, including the deliberate double delivery:
            # the deduper keys on (text, seq), so the copy is still deduped and the
            # fixture keeps testing that guard.
            item["cue_category"] = CUE_FAMILIES[utterance_i % len(CUE_FAMILIES)]
            utterance_i += 1
            timed.append(item)
            ended = parse_timestamp(item["timestamp"]) \
                + dt.timedelta(seconds=TTS_ENDED_AFTER_S)
            timed.append({"type": TTS_ENDED_EVENT, "seq": item.get("seq"),
                          "timestamp": format_timestamp(ended)})
            continue
        timed.append(item)
    timed.sort(key=lambda e: str(e.get("timestamp", "")))
    return timed + completion


def render(envelopes: list[dict]) -> str:
    return "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in envelopes)


def load_source(path: Path = SOURCE) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def generate(path: Path = SOURCE) -> str:
    """The whole pipeline as one pure-ish function, so the test can compare text."""

    return render(build(load_source(path)))


def main() -> None:
    text = generate()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print("wrote %s (%d envelopes)" % (OUT, text.count("\n")))


if __name__ == "__main__":
    main()
