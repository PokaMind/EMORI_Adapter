"""Full on-robot session orchestrator (the screenless, audio-first flow) + a self-
contained MockSession to simulate it end-to-end against the Reachy Mini simulator.

On the real robot this is what Chromium-hosted Pokamind + the adapter do together:
topic is pre-picked (JWT module_id), the robot greets the learner, then each ~2 s
Pokamind cycle drives body language while the AI's spoken turns are voiced by the
robot, and a short summary is spoken at the end (full report -> FIWARE dashboard).

In simulation:
* analysis cycles -> Translator -> BehaviorEngine -> RobotClient -> MuJoCo motion (REAL),
* AI utterances -> "talking" animation (REAL motion) + TtsEngine -> robot speaker
  (the daemon call; audible voice is Chromium/CM4-side on hardware),
* metrics + a session summary -> FIWARE (REAL NGSI-LD time-series).
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Iterator
from typing import Optional

from pydantic import BaseModel

from emori_adapter.behavior import BehaviorEngine
from emori_adapter.behavior import repertoire as R
from emori_adapter.fiware import FiwareClient, NullFiwareClient
from emori_adapter.models import AnalysisCycle
from emori_adapter.robot.client import RobotClient
from emori_adapter.sources.mock import MockPokamindSource
from emori_adapter.speech.tts import NullTts, TtsEngine
from emori_adapter.translation import Translator

log = logging.getLogger("emori.session")


class SessionEvent(BaseModel):
    """One step of a session: either an analysis cycle or an AI utterance."""

    kind: str  # "analysis" | "utterance"
    cycle: Optional[AnalysisCycle] = None
    text: str = ""
    lang: str = "en"


# The robot plays the patient/family roleplay partner; lines escalate then settle,
# loosely tracking the mock engagement arc (healthcare de-escalation practice).
_AI_LINES = [
    "Hello. Thank you for seeing me, I have been waiting a while.",
    "I just want to understand what is happening with my mother.",
    "Sorry, I am a bit on edge today.",
    "No one has told me anything for hours. This is unacceptable!",
    "I am sorry for raising my voice. I am just scared.",
    "Okay. Thank you for explaining it calmly, that helps.",
]


class MockSession:
    """Yields a realistic stream of SessionEvents: an opening line, then per-phase
    analysis cycles interleaved with AI roleplay lines."""

    def __init__(
        self,
        *,
        session_id: str = "sim-session",
        person_id: str = "learner_01",
        topic: str = "Reassuring an anxious family member at the front desk",
        cycles_per_phase: int = 3,
        utter_every: int = 3,
        period_s: float = 2.0,
        realtime: bool = True,
        seed: int = 7,
    ) -> None:
        self.topic = topic
        self.utter_every = utter_every
        self.period_s = period_s
        self.realtime = realtime
        self._mock = MockPokamindSource(
            session_id=session_id, person_id=person_id,
            cycles_per_phase=cycles_per_phase, period_s=period_s, realtime=False, seed=seed,
        )

    def events(self) -> Iterator[SessionEvent]:
        yield SessionEvent(kind="utterance", text=_AI_LINES[0])
        if self.realtime:
            time.sleep(self.period_s)
        line_i = 1
        for cycle in self._mock.cycles():
            yield SessionEvent(kind="analysis", cycle=cycle)
            if self.realtime:
                time.sleep(self.period_s)
            if cycle.seq > 0 and cycle.seq % self.utter_every == 0:
                yield SessionEvent(kind="utterance", text=_AI_LINES[line_i % len(_AI_LINES)])
                line_i += 1
                if self.realtime:
                    time.sleep(self.period_s)


class RobotSession:
    """Drives a full session against the robot (sim or real)."""

    def __init__(
        self,
        source: MockSession,
        client: RobotClient,
        *,
        translator: Optional[Translator] = None,
        engine: Optional[BehaviorEngine] = None,
        tts: Optional[TtsEngine] = None,
        fiware=None,
        topic: str = "",
    ) -> None:
        self.source = source
        self.client = client
        self._bcfg = engine.config if engine is not None else None  # to rebuild a fresh engine per run
        self.translator = translator or Translator()
        self.engine = engine or BehaviorEngine()
        self.tts = tts or NullTts()
        self.fiware = fiware or NullFiwareClient()
        self.topic = topic or getattr(source, "topic", "")
        self._eng_scores: list[float] = []
        self._styles: Counter = Counter()
        self._max_escalation = 0.0

    def run(self) -> dict:
        # Fresh per-run state: the Translator and BehaviorEngine are stateful (engagement/
        # context trends, cooldown counters), so rebuild them + the accumulators each run()
        # so a second run() on the same instance is fully independent.
        self.translator = Translator()
        self.engine = BehaviorEngine(self._bcfg) if self._bcfg is not None else BehaviorEngine()
        self._eng_scores = []
        self._styles = Counter()
        self._max_escalation = 0.0
        log.info("=== SESSION START | topic: %s ===", self.topic)
        self.fiware.ensure_session(self.topic)
        if self.client.health():
            self.client.wake_up()
        try:
            for event in self.source.events():
                if event.kind == "analysis" and event.cycle is not None:
                    self._on_analysis(event.cycle)
                elif event.kind == "utterance":
                    self._speak(event.text, event.lang)
            summary = self._finish()
        finally:
            self.fiware.close()
            self.client.close()
        return summary

    # --- handlers ---
    def _on_analysis(self, cycle: AnalysisCycle) -> None:
        sig = self.translator.translate(cycle)
        intents = self.engine.step(sig)
        for intent in intents:
            self.client.apply(intent)
        self.fiware.push(sig)
        self._eng_scores.append(sig.engagement_score)
        self._max_escalation = max(self._max_escalation, sig.context.escalation)
        if sig.top_styles:
            self._styles[sig.top_styles[0][0]] += 1
        gestures = ", ".join(dict.fromkeys(i.reason for i in intents)) or "-"
        log.info("cyc %02d | eng=%.2f %-11s val=%+.2f esc=%.2f | -> %s",
                 sig.seq, sig.engagement_score, sig.engagement_level.name,
                 sig.valence, sig.context.escalation, gestures)

    def _speak(self, text: str, lang: str = "en") -> None:
        # Talking gestures on the robot; the audible voice plays through the kiosk
        # Chromium on the live path. Here (headless orchestrator) a configured TTS
        # still renders the WAV so the audio artifact can be inspected/played.
        for intent in R.talking_gesture():
            self.client.apply(intent)
        path = self.tts.synthesize(text, lang)
        if path:
            log.info("[robot speech wav] %s", path)
        log.info('[robot speaks] "%s"', text)

    def _finish(self) -> dict:
        avg = sum(self._eng_scores) / len(self._eng_scores) if self._eng_scores else 0.0
        dominant = self._styles.most_common(1)[0][0] if self._styles else "neutral"
        if self._max_escalation >= 0.6:
            tip = "You held steady when things heated up - keep that calm, grounded tone."
        elif avg >= 0.6:
            tip = "Strong engagement throughout. Next time, try one more open question."
        else:
            tip = "Attention dipped a few times - slowing down and reflecting back can help."
        summary = (f"That's the end of our practice. Your engagement averaged "
                   f"{round(avg * 100)} percent, and you were most often {dominant}. {tip}")
        self._speak(summary)
        self.fiware.write_summary(summary, avg)
        log.info("=== SESSION END | avg_eng=%.2f dominant=%s max_esc=%.2f ===",
                 avg, dominant, self._max_escalation)
        return {"avg_engagement": avg, "dominant_style": dominant,
                "max_escalation": self._max_escalation, "summary": summary}


def main() -> None:  # `emori-session` entry point
    import typer
    from rich.console import Console

    from emori_adapter.config import load_settings
    from emori_adapter.logging_setup import setup_logging
    from emori_adapter.robot.client import DryRunRobotClient, HttpRobotClient

    app = typer.Typer(add_completion=False, help="Run a full EMORI on-robot session (simulated).")

    @app.command()
    def run(
        robot: str = typer.Option("http", help="http | dry"),
        base_url: Optional[str] = typer.Option(None, help="Reachy daemon base URL"),
        topic: str = typer.Option("Reassuring an anxious family member at the front desk"),
        person_id: Optional[str] = typer.Option(None),
        session_id: Optional[str] = typer.Option(None),
        cycles_per_phase: int = typer.Option(3),
        realtime: bool = typer.Option(True, help="--no-realtime for a fast run"),
        fiware: bool = typer.Option(True, help="--no-fiware to skip NGSI-LD"),
        orion_url: Optional[str] = typer.Option(None),
        log_level: str = typer.Option("INFO"),
    ) -> None:
        setup_logging(log_level)
        s = load_settings()
        person = person_id or s.person_id
        sess = session_id or s.session_id
        src = MockSession(session_id=sess, person_id=person, topic=topic,
                          cycles_per_phase=cycles_per_phase, period_s=s.cycle_period_s, realtime=realtime)
        client = DryRunRobotClient() if robot == "dry" else HttpRobotClient(base_url or s.reachy_base_url)
        fw = (FiwareClient(orion_url=orion_url or s.orion_url, tenant=s.ngsild_tenant,
                           session_id=sess, person_id=person) if fiware else NullFiwareClient())
        if robot == "http" and not client.health():
            Console().print("[yellow]warning:[/] daemon not reachable - start `reachy-mini-daemon --sim`.")
        result = RobotSession(src, client, tts=NullTts(), fiware=fw, topic=topic).run()
        Console().rule("[bold]session summary[/]")
        Console().print(result)

    app()


if __name__ == "__main__":
    main()
