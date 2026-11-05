"""Generate tests/fixtures/live_session.jsonl — a deterministic recording of one
full session in the EXACT Pokamind contract shape.

This is the replay fixture the e2e test (and scripts/replay_session.py) pushes
through the complete stack. Regenerate after contract changes:

    python scripts/gen_replay_fixture.py

Faithfulness notes (each matches the contract):
* analysis envelopes carry NO session_id/person_id (the hostapp stamps them);
* AI lines are emitted ONCE each (the session page sends the scenario intro/first
  question and the streamed turns in separate, non-overlapping phases);
  the fixture includes ONE deliberate same-seq transport double-delivery to exercise
  the dedup guard, plus a legitimately REPEATED line (different seq) that must speak;
* one frame per ~7 has a null body (body-analysis failure) — here pre-sanitized
  to empty dicts exactly as the wire nulls reduce to;
* the completion message carries results: analysisData.
"""

from __future__ import annotations

import json
from pathlib import Path

from emori_adapter.sources.mock import MockPokamindSource

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "live_session.jsonl"

AI_LINES = [
    "Hello. Thank you for seeing me, I have been waiting a while.",
    "I just want to understand what is happening with my mother.",
    "Sorry, I am a bit on edge today.",
    "No one has told me anything for hours. This is unacceptable!",
    "I am sorry for raising my voice. I am just scared.",
    "Okay. Thank you for explaining it calmly, that helps.",
]


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    src = MockPokamindSource(realtime=False, shape="live", cycles_per_phase=4, seed=7)
    lines: list[dict] = []
    line_i = 0
    for cycle in src.cycles():
        env = {
            "type": "pokamind_analysis_update",
            "communication_styles": cycle.communication_styles,
            "body_language": cycle.body_language,
            "body_language_metrics": cycle.body_language_metrics,
            "seq": cycle.seq,
            "timestamp": cycle.timestamp,
        }
        lines.append(env)
        if cycle.seq > 0 and cycle.seq % 4 == 0:
            text = AI_LINES[line_i % len(AI_LINES)]
            line_i += 1
            utt = {"type": "pokamind_ai_utterance", "text": text,
                   "seq": line_i, "timestamp": cycle.timestamp}
            lines.append(utt)
            if line_i == 2:
                # one transport-level double delivery (identical seq+text) -> must dedup
                lines.append(dict(utt))
            if line_i == 3:
                # a legitimately repeated identical line at a NEW seq -> must speak again
                lines.append({"type": "pokamind_ai_utterance", "text": text,
                              "seq": line_i + 100, "timestamp": cycle.timestamp})
    lines.append({
        "type": "pokamind_session_completed",
        "partner": "emori-robot",
        "session_id": "pokamind-side-session-id",
        "status": "completed",
        "results": {
            "styles": {"mirroring": 14.2, "acknowledging": 11.8, "steady": 9.3},
            "body_language_metrics": {"engagement": 64.0, "confidence": 58.5},
            "duration_minutes": 8,
        },
    })
    with OUT.open("w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(lines)} envelopes)")


if __name__ == "__main__":
    main()
