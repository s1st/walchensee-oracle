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

from oracle.calibration import compile_report, format_text_report, rescore_all
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


@app.command()
def rescore(
    since: str = typer.Option(None, help="ISO date — only re-score days from this date forward."),
    until: str = typer.Option(None, help="ISO date — only re-score days up to this date."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Don't write; just report what would change."),
) -> None:
    """Re-run the rule layer on each logged record under the current aggregator.

    Adds `overall_resimulated` + `verdicts_resimulated` to each record without
    touching the historical `overall` / `verdicts` (kept as evidence of what
    the aggregator said at write time). The dashboard reads the resimulated
    field for the 'Re-scored' strip row when present.
    """
    since_d = date.fromisoformat(since) if since else None
    until_d = date.fromisoformat(until) if until else None
    summary = rescore_all(since=since_d, until=until_d, dry_run=dry_run)
    action = "would rewrite" if dry_run else "rewrote"
    console.print(f"{action} {len(summary['rewritten']) or len(summary.get('flipped', []))} records")
    if summary["skipped"]:
        console.print(f"[yellow]skipped (incomplete inputs): {len(summary['skipped'])}[/yellow]")
        for iso in summary["skipped"][:10]:
            console.print(f"  {iso}")
    if summary["flipped"]:
        console.print(f"[bold]verdict flipped on {len(summary['flipped'])} days:[/bold]")
        for iso, old, new in summary["flipped"]:
            console.print(f"  {iso}: {old} → {new}")
    else:
        console.print("[dim]no overall verdicts changed[/dim]")


@app.command()
def calibrate(
    since: str = typer.Option(None, help="ISO date — only consider days from this date forward."),
    until: str = typer.Option(None, help="ISO date — only consider days up to this date."),
    rule: str = typer.Option(None, help="Restrict per-rule table to a single rule (e.g. post_rain_moisture)."),
) -> None:
    """Score logged forecasts against Urfeld peak ground truth.

    Reports the overall confusion matrix and per-rule false-positive vetos
    (rules that said NO_GO on days the lake actually fired). Reads from the
    same RunStore the forecast/backfill jobs write — local in dev, GCS in
    prod when $RUNS_BUCKET is set.
    """
    since_d = date.fromisoformat(since) if since else None
    until_d = date.fromisoformat(until) if until else None
    report = compile_report(since=since_d, until=until_d)
    console.print(format_text_report(report, rule_filter=rule))


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
