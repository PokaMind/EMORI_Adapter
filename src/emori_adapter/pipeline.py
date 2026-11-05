"""The adapter pipeline: SessionSource -> Translator -> BehaviorEngine -> RobotClient.

This is the Slice-1 loop (Windows, no ROS). The same Translator + BehaviorEngine are
reused inside the ROS 2 nodes in Slice 2; only the *transport* differs (there the
signals are published/consumed as ROS4HRI messages between the two halves).

``on_report`` is a generic per-cycle telemetry hook - the ROS publisher and the
FIWARE bridge both attach here without the pipeline knowing about them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Optional

from emori_adapter.behavior import BehaviorEngine
from emori_adapter.models import DerivedSignals, MotionIntent
from emori_adapter.robot.client import RobotClient
from emori_adapter.sources.base import SessionSource
from emori_adapter.translation import Translator

log = logging.getLogger("emori.pipeline")


@dataclass
class CycleReport:
    signals: DerivedSignals
    intents: list[MotionIntent]


ReportHook = Callable[[CycleReport], None]


class Pipeline:
    def __init__(
        self,
        source: SessionSource,
        client: RobotClient,
        *,
        translator: Optional[Translator] = None,
        engine: Optional[BehaviorEngine] = None,
        on_report: Optional[ReportHook] = None,
    ) -> None:
        self.source = source
        self.client = client
        self.translator = translator or Translator()
        self.engine = engine or BehaviorEngine()
        self.on_report = on_report

    def run(self, max_cycles: Optional[int] = None, wake: bool = True) -> list[CycleReport]:
        reports: list[CycleReport] = []
        if wake and self.client.health():
            self.client.wake_up()
        try:
            for i, cycle in enumerate(self.source.cycles()):
                signals = self.translator.translate(cycle)
                intents = self.engine.step(signals)
                for intent in intents:
                    self.client.apply(intent)
                report = CycleReport(signals=signals, intents=intents)
                reports.append(report)
                self._log_cycle(report)
                if self.on_report is not None:
                    self.on_report(report)
                if max_cycles is not None and i + 1 >= max_cycles:
                    break
        finally:
            self.source.close()
            self.client.close()
        return reports

    @staticmethod
    def _log_cycle(report: CycleReport) -> None:
        s = report.signals
        gestures = ", ".join(dict.fromkeys(i.reason for i in report.intents)) or "(none)"
        log.info(
            "cyc %02d | eng=%.2f %-11s | val=%+.2f aro=%+.2f %-9s | %s | -> %s",
            s.seq,
            s.engagement_score,
            s.engagement_level.name,
            s.valence,
            s.arousal,
            s.expression,
            s.context.note or "-",
            gestures,
        )
