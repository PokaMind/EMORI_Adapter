"""``emori-run`` - one-command Slice-1 demo and dev driver.

Examples::

    # Watch the mock session drive the simulated robot (daemon must be running):
    emori-run --source mock --robot http --base-url http://127.0.0.1:8000

    # Logic-only (no robot, no network) - prints the engagement/behaviour trace:
    emori-run --source mock --robot dry --no-realtime

    # Replay a captured Pokamind data.csv:
    emori-run --source csv --csv path/to/data.csv --robot dry --no-realtime
"""

from __future__ import annotations

from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from emori_adapter.behavior import BehaviorConfig, BehaviorEngine
from emori_adapter.config import load_settings
from emori_adapter.logging_setup import setup_logging
from emori_adapter.pipeline import Pipeline
from emori_adapter.robot.client import DryRunRobotClient, HttpRobotClient
from emori_adapter.sources.csv_replay import CsvReplaySource
from emori_adapter.sources.mock import MockPokamindSource
from emori_adapter.translation import TranslationConfig, Translator

app = typer.Typer(add_completion=False, help="EMORI Pokamind -> Reachy Mini adapter")
console = Console()


@app.command()
def run(
    source: str = typer.Option("mock", help="Session source: mock | csv | pokamind"),
    robot: str = typer.Option("http", help="Robot client: http | dry"),
    base_url: Optional[str] = typer.Option(None, help="Reachy daemon base URL"),
    csv: Optional[str] = typer.Option(None, help="Path to a Pokamind data.csv (source=csv)"),
    ws_url: Optional[str] = typer.Option(None, help="Pokamind relay WebSocket URL (source=pokamind)"),
    person_id: Optional[str] = typer.Option(None, help="ROS4HRI person id"),
    session_id: Optional[str] = typer.Option(None, help="Session id"),
    cycles: Optional[int] = typer.Option(None, help="Max cycles (default: full mock arc)"),
    cycles_per_phase: int = typer.Option(3, help="Mock: cycles per scenario phase"),
    period: Optional[float] = typer.Option(None, help="Cycle period seconds"),
    realtime: bool = typer.Option(True, help="Sleep between cycles (use --no-realtime for fast logic runs)"),
    backchannel: str = typer.Option("", help="Daemon sound filename to enable audio backchannels"),
    config: Optional[str] = typer.Option(None, help="Path to a behaviour YAML (configs/behavior.yaml)"),
    fiware: bool = typer.Option(False, help="Historize each cycle to Orion-LD (ORION_URL)"),
    orion_url: Optional[str] = typer.Option(None, help="Orion-LD base URL (with --fiware)"),
    log_level: str = typer.Option("INFO", help="DEBUG|INFO|WARNING|ERROR"),
) -> None:
    setup_logging(log_level)
    settings = load_settings()
    person = person_id or settings.person_id
    sess = session_id or settings.session_id
    p = period if period is not None else settings.cycle_period_s

    # --- source ---
    if source == "mock":
        src = MockPokamindSource(
            session_id=sess, person_id=person, cycles_per_phase=cycles_per_phase,
            period_s=p, realtime=realtime,
        )
    elif source == "csv":
        if not csv:
            raise typer.BadParameter("--csv is required when --source csv")
        src = CsvReplaySource(csv, session_id=sess, person_id=person, period_s=p, realtime=realtime)
    elif source == "pokamind":
        from emori_adapter.sources.websocket import PokamindBridgeSource

        url = ws_url or settings.pokamind_ws_url
        if not url:
            raise typer.BadParameter("--ws-url (or POKAMIND_WS_URL) is required when --source pokamind")
        src = PokamindBridgeSource(url, session_id=sess, person_id=person)
    else:
        raise typer.BadParameter(f"unknown source: {source}")

    # --- robot client ---
    if robot == "http":
        client = HttpRobotClient(base_url or settings.reachy_base_url, timeout=settings.reachy_timeout_s)
    elif robot == "dry":
        client = DryRunRobotClient()
    else:
        raise typer.BadParameter(f"unknown robot client: {robot}")

    if robot == "http" and not client.health():
        console.print(
            f"[yellow]warning:[/] daemon not reachable at {base_url or settings.reachy_base_url}. "
            "Start it with `.venv-reachy\\Scripts\\reachy-mini-daemon.exe --sim`."
        )

    bcfg = BehaviorConfig.from_yaml(config) if config else BehaviorConfig()
    if backchannel:
        bcfg.backchannel_sound = backchannel
    engine = BehaviorEngine(bcfg)
    translator = Translator(TranslationConfig.from_yaml(config)) if config else None

    # FIWARE historization on ANY source (incl. the live relay), via the on_report hook.
    fw = None
    on_report = None
    if fiware:
        from emori_adapter.fiware import FiwareClient

        fw = FiwareClient(orion_url=orion_url or settings.orion_url,
                          tenant=settings.ngsild_tenant, session_id=sess, person_id=person)
        fw.ensure_session()
        on_report = lambda report: fw.push(report.signals)  # noqa: E731

    pipeline = Pipeline(src, client, engine=engine, translator=translator, on_report=on_report)

    console.rule(f"[bold]EMORI[/] | source={source} robot={robot} person={person}")
    try:
        reports = pipeline.run(max_cycles=cycles)
    finally:
        if fw is not None:
            fw.close()
    _summary(reports)


def _summary(reports) -> None:
    table = Table(title="Session summary", show_lines=False)
    for col in ("cyc", "engagement", "level", "expression", "note", "gestures"):
        table.add_column(col)
    for r in reports:
        s = r.signals
        gestures = ", ".join(dict.fromkeys(i.kind for i in r.intents)) or "-"
        table.add_row(
            str(s.seq), f"{s.engagement_score:.2f}", s.engagement_level.name,
            s.expression, s.context.note or "-", gestures,
        )
    Console().print(table)


def main() -> None:  # console-script entry point
    app()


if __name__ == "__main__":
    main()
