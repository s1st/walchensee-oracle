"""Command-line entry point."""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, datetime, timedelta

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from oracle.engine import Forecast, run_forecast
from oracle.logger import backfill_run, forecast_to_dict, load_run, write_run

load_dotenv()

app = typer.Typer(help="Walchi Thermic Oracle")
console = Console()


@app.command()
def forecast(
    day: str = typer.Option(None, help="ISO date, defaults to today"),
    horizon: int = typer.Option(
        1, "--horizon",
        help="Number of consecutive days to forecast starting from `day` (or today). "
             "With --horizon 3 the scheduled job writes today, tomorrow and day-after logs.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON to stdout instead of tables."
    ),
    log: bool = typer.Option(
        True, "--log/--no-log", help="Write the run(s) to data/runs/<day>.json."
    ),
) -> None:
    start = date.fromisoformat(day) if day else date.today()
    targets = [start + timedelta(days=i) for i in range(horizon)]

    if horizon == 1:
        target = targets[0]
        result = asyncio.run(run_forecast(target))
        if log:
            location = write_run(result, target)
            if not json_output:
                console.print(f"[dim]logged to {location}[/dim]")
        if json_output:
            sys.stdout.write(json.dumps(forecast_to_dict(result, target), ensure_ascii=False) + "\n")
            return
        _render_tables(result, target)
        return

    # Multi-day mode: terse per-day summary, each day logged independently.
    async def run_all() -> None:
        for target in targets:
            result = await run_forecast(target)
            if log:
                location = write_run(result, target)
                console.print(f"{target.isoformat()}: {result.overall.value:5} → {location}")
            else:
                console.print(f"{target.isoformat()}: {result.overall.value}")

    asyncio.run(run_all())


@app.command()
def backfill(
    day: str = typer.Option(None, help="ISO date, defaults to today"),
) -> None:
    """Merge the day's Urfeld wind curve into the existing run log."""
    target = date.fromisoformat(day) if day else date.today()
    location = asyncio.run(backfill_run(target))
    data = load_run(target)
    machine = data.get("ground_truth", {}).get("machine") or {}
    console.print(f"[bold]Backfilled:[/bold] {location}")
    if not machine:
        console.print("[yellow]no Urfeld samples landed in that day's window[/yellow]")
        return
    console.print(
        f"  peak avg : {machine.get('peak_avg_knots')} kt @ {machine.get('peak_avg_at')}"
    )
    console.print(f"  peak gust: {machine.get('peak_gust_knots')} kt")
    console.print(f"  first ignition (≥8 kt): {machine.get('first_ignition_at') or '—'}")
    console.print(
        f"  samples ≥8 kt: {machine.get('samples_above_8kt')}  "
        f"≥12 kt: {machine.get('samples_above_12kt')}"
    )


def _render_tables(result: Forecast, target: date) -> None:
    verdict_table = Table(title=f"Walchi Oracle — {target.isoformat()}")
    verdict_table.add_column("Rule")
    verdict_table.add_column("Signal")
    verdict_table.add_column("Reason")
    for v in result.verdicts:
        verdict_table.add_row(v.rule, v.signal.value, v.reason)
    console.print(verdict_table)
    console.print(f"[bold]Overall:[/bold] {result.overall.value}")

    if result.chat_messages:
        chat_table = Table(title="windinfo.eu — recent Walchensee mentions")
        chat_table.add_column("When")
        chat_table.add_column("Who")
        chat_table.add_column("Message")
        for m in result.chat_messages:
            chat_table.add_row(
                m.posted_at.strftime("%Y-%m-%d %H:%M"),
                m.author,
                m.text if len(m.text) <= 140 else m.text[:137] + "…",
            )
        console.print(chat_table)


if __name__ == "__main__":
    app()
