# Golden recordings

Runs of `scripts/golden_trace.py record`, kept so that CI can tell when what the robot is
asked to do for a scenario changes. `scripts/golden_ci.py check --require-recordings` records
the same scenarios again from the checkout and compares them with these, structure only
(`--exact-only`): a CI machine says nothing about timing. The job fails if none are found.

## What is kept

| Scenario | Runs | What it pins |
|---|---|---|
| `full_session` | 5 | Badge, voice menu, session, a full turn loop with four sentences on a voice (two plain, one `tension`, one `levity`, the cued two each carrying a recorded move), completion with results, the session summary, the pages and the persisted rows |
| `speech_plans` | 5 | Eight sentences on a voice: plans that outlast it, a pause with the reply still going, cuts where the voice stops, a one-word line, the `tension` and `levity` families, then completion |
| `cue_families` | 5 | One sentence in each of the six cue families, on a voice: what each is performed as, then completion |
| `preempt` | 3 | A new session over a live one: the first is finalized, the second is kept and completed |

The voiced scenarios carry five runs because `deploy/hub/golden_tolerances.yaml` asks for that
many before a voiced scenario's runs say anything about each other. `preempt` has no voice and
needs three.

The scenarios that end a session by a timer (`walkaway`, `late_completion`, the re-prompt
pair) are left out: their order is exact but what they pin is a timer's moment.

## Layout

The layout `record` writes, one directory per scenario:

```
tests/fixtures/golden/<scenario>/run-001/meta.json
                                         client.jsonl      what the robot was asked to do
                                         trace/            the plan of each batch and when it applied
                                         http.jsonl  events.jsonl  marks.jsonl  mic.jsonl ...
                                         html/  email/  recordings/
```

`meta.json` says whether the run's time was compressed and which language it ran in, and the
check records again with the same options. A run without it is refused. The recordings come
from the simulator: its own badges, roster, sentences and results, with session ids and times
masked. They name no site and no real person.

Times and the clock-derived fields differ from run to run and are not compared by `--exact-only`.
Everything else is identical between independent recordings. The simulator writes no arbiter
trace (`motion.jsonl`), so the comparison notes that motion is not applicable.

## Adding one

Goldens are never written by hand. Record the scenario from the simulator with the options
you want kept, look at the result, and copy its `run-NNN` directories here:

```
python scripts/golden_trace.py record --label keep --scenario full_session --runs 5 --fast
```

Record it twice under two labels and run
`python scripts/golden_trace.py compare --a one --b two --exact-only` first: a scenario that
does not agree with itself is not a golden. Keep to the scenarios that do not depend on a timer
firing at a particular moment, and to recordings that hold no site data. Re-record a scenario
whenever a change to what the robot does for it is meant, and say so in the commit.
