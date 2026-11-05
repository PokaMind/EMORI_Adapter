"""Regenerate deploy/fiware/{entities,subscriptions}/*.json from the canonical
schema (emori_adapter.fiware_schema), so the static fixtures can never drift from
what the writers actually produce.

    python scripts/gen_fiware_fixtures.py
"""

from __future__ import annotations

import json
from pathlib import Path

from emori_adapter import fiware_schema as schema

ROOT = Path(__file__).resolve().parent.parent / "deploy" / "fiware"
SEED_TS = "2026-01-01T00:00:00Z"  # fixed stamp: fixtures must be diff-stable


def main() -> None:
    (ROOT / "entities").mkdir(parents=True, exist_ok=True)
    (ROOT / "subscriptions").mkdir(parents=True, exist_ok=True)

    person = schema.person_entity("learner_01")
    session = schema.session_entity("sim-session", "learner_01",
                                    topic="training", observed_at=SEED_TS)
    sub = schema.subscription_body("http://quantumleap:8668/v2/notify", "pokamind")

    for name, payload in (("entities/person.json", person),
                          ("entities/session.json", session),
                          ("subscriptions/quantumleap-subscription.json", sub)):
        path = ROOT / name
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
