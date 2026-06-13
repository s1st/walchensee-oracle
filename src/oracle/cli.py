"""Command-line entry point."""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, timedelta

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from oracle import config
from oracle.engine import Forecast, run_forecast, run_replay
from oracle.logger import backfill_run, forecast_to_dict, load_run, write_run

load_dotenv()

app = typer.Typer(help="Walchi Thermic Oracle")
console = Console()


def _resolve_months(season: bool, months: str | None) -> frozenset[int] | None:
    """Turn the --season/--all-year flag and optional --months spec into a month
    set. Explicit --months wins; otherwise --season → Apr–Oct, --all-year → None."""
    from oracle.calibration import parse_months

    if months:
        try:
            return parse_months(months)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    return config.ACTIVE_SEASON_MONTHS if season else None


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
def replay(
    day: str = typer.Option(None, help="ISO date to replay against the archive (e.g. 2017-06-15)."),
    from_: str = typer.Option(None, "--from", help="Batch mode: first ISO date of the range (inclusive). Requires --to."),
    to: str = typer.Option(None, "--to", help="Batch mode: last ISO date of the range (inclusive). Requires --from."),
    source: str = typer.Option(
        "historical-forecast",
        help="Which Open-Meteo archive to use: 'historical-forecast' (IFS HRES 2017+, DWD ICON 2022+) or 'reanalysis' (ERA5 1940+).",
    ),
    models: str = typer.Option(
        None,
        help="Batch mode: pin Open-Meteo model(s) (e.g. 'ecmwf_ifs') instead of Best Match. "
             "Recommended for scoring runs spanning model-coverage eras.",
    ),
    log: bool = typer.Option(True, "--log/--no-log", help="Write the replay to runs/replay/<day>.json."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON to stdout instead of tables."),
) -> None:
    """Re-run the rules against the historical forecast (or reanalysis).

    Single-day mode (--day): pairs the Open-Meteo archive pillars with the
    historical Urfeld buoy day-curve, scraped live.

    Batch mode (--from/--to): replays every stored ground-truth day in the
    range. Archive data is fetched once per year (two requests) instead of
    per day, and the buoy curve is reconstructed from the stored ground
    truth — no re-scraping. This is the path for calibration passes over
    the historical backfill.

    Replay records land in `runs/replay/<day>.json` so the calibrate loop
    and the dashboard don't mistake them for live forecasts. See
    docs/historical_forecasts.md for model coverage and caveats.
    """
    if source not in ("historical-forecast", "reanalysis"):
        raise typer.BadParameter("--source must be 'historical-forecast' or 'reanalysis'")
    batch = from_ is not None or to is not None
    if batch and (day is not None or from_ is None or to is None):
        raise typer.BadParameter("batch mode needs both --from and --to, and no --day")
    if not batch and day is None:
        raise typer.BadParameter("pass --day for a single replay, or --from/--to for a batch")

    if batch:
        if not log:
            raise typer.BadParameter("--no-log makes no sense in batch mode — its whole point is the written records")
        from oracle.replay import run_replay_batch

        summary = asyncio.run(run_replay_batch(
            date.fromisoformat(from_), date.fromisoformat(to),
            source=source, models=models,  # type: ignore[arg-type]
            progress=lambda msg: console.print(f"[dim]{msg}[/dim]"),
        ))
        if json_output:
            sys.stdout.write(json.dumps(
                {"replayed": summary.replayed, "skipped": summary.skipped},
                ensure_ascii=False,
            ) + "\n")
            return
        console.print(f"[bold]replayed {len(summary.replayed)} days[/bold]")
        if summary.skipped:
            console.print(f"[yellow]skipped {len(summary.skipped)}:[/yellow]")
            for iso, reason in summary.skipped[:10]:
                console.print(f"  {iso}: {reason}")
            if len(summary.skipped) > 10:
                console.print(f"  … and {len(summary.skipped) - 10} more")
        return

    target = date.fromisoformat(day)
    result = asyncio.run(run_replay(target, source=source))  # type: ignore[arg-type]
    if log:
        location = write_run(result, target)
        if not json_output:
            console.print(f"[dim]replay logged to {location}[/dim]")
    if json_output:
        sys.stdout.write(json.dumps(forecast_to_dict(result, target), ensure_ascii=False) + "\n")
        return
    _render_tables(result, target)


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
    replayed: bool = typer.Option(False, "--replayed", help="Re-score the replay records (runs/replay/) instead of the live forecasts — the no-API inner loop of historical calibration."),
    season: bool = typer.Option(False, "--season/--all-year", help="Restrict to the active thermal season (Apr–Oct). Default re-scores all months."),
    months: str = typer.Option(None, help="Explicit months, e.g. '4-10' or '4,5,9'. Overrides --season/--all-year."),
) -> None:
    """Re-run the rule layer on each logged record under the current aggregator.

    Adds `overall_resimulated` + `verdicts_resimulated` to each record without
    touching the historical `overall` / `verdicts` (kept as evidence of what
    the aggregator said at write time). The dashboard reads the resimulated
    field for the 'Re-scored' strip row when present.

    With --replayed: same, but over the replay records, from their stored
    inputs — no Open-Meteo traffic. After tuning a threshold, run this and
    then `oracle calibrate --replayed --resimulated` to see the effect.
    """
    from oracle.calibration import rescore_all

    since_d = date.fromisoformat(since) if since else None
    until_d = date.fromisoformat(until) if until else None
    months_set = _resolve_months(season, months)
    summary = rescore_all(
        since=since_d, until=until_d, dry_run=dry_run, replayed=replayed, months=months_set,
    )
    if dry_run:
        console.print(f"would rewrite {len(summary['unchanged'])} records")
    else:
        console.print(f"rewrote {len(summary['rewritten'])} records")
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
    label: str = typer.Option("peak", help="Ground-truth scale: 'peak' (max avg knots), 'duration' (sustained samples), or 'thermal' (duration + thermal-character gates: mid-day onset & coherent gusts)."),
    resimulated: bool = typer.Option(False, "--resimulated/--historical", help="Score the current rule layer (`verdicts_resimulated`) instead of the historical verdicts. Requires `oracle rescore` to have populated those fields."),
    replayed: bool = typer.Option(False, "--replayed", help="Score the replay records (runs/replay/) against the ground truth in the matching main records. Combine with --resimulated after a threshold tune + `oracle rescore --replayed`."),
    season: bool = typer.Option(True, "--season/--all-year", help="Score only the active thermal season (Apr–Oct, the default — the window the product serves). --all-year includes Nov–Mar, which over-weights the winter negative class."),
    months: str = typer.Option(None, help="Explicit months, e.g. '4-10' or '4,5,9'. Overrides --season/--all-year."),
    split: str = typer.Option("none", help="Also print per-partition skill: 'year' (year-wise CV) or 'era' (IFS vs ICON). A tune whose optimum doesn't hold across splits is overfit."),
    mcnemar: bool = typer.Option(False, "--mcnemar", help="Run McNemar's paired significance test comparing the as-written verdict (`overall`) to the current rule layer (`overall_resimulated`). Needs `oracle rescore` first."),
    csv: str = typer.Option(None, help="Path to write a flat one-row-per-day CSV (features + ground truth) for offline ML."),
) -> None:
    """Score logged forecasts against Urfeld ground truth.

    Reports the overall confusion matrix and per-rule false-positive vetos
    (rules that said NO_GO on days the lake actually fired). Reads from the
    same RunStore the forecast/backfill jobs write — local in dev, GCS in
    prod when $RUNS_BUCKET is set.

    Since 2026-06-12 the bucket also contains ~3,600 historical buoy
    stub records (2016-2026, no forecast). They contribute to the
    `actual` (ground truth) side of the confusion matrix — useful for
    hypothesis testing at scale — but have no forecast to score. A
    no-arg run walks all 3,700 files (~10-20 min over GCS); pass
    `--since 2026-04-22` to restrict to the project's own days.

    Scoring defaults to the active thermal season (Apr–Oct) — the window
    the product actually serves. Pass `--all-year` to include Nov–Mar, but
    note winter dominates the negative class and turns univariate
    thresholds into season detectors (see docs/fable_findings.md §2).

    With --replayed: score the archive-replayed verdicts (written by
    `oracle replay --from/--to`) against the same stored ground truth.
    Reads two records per day over GCS — for repeated runs, mirror the
    bucket locally (`gcloud storage cp -r gs://<bucket>/runs data/`) and
    run without $RUNS_BUCKET.
    """
    from oracle.calibration import (
        compile_report,
        export_csv,
        format_mcnemar,
        format_skill_line,
        format_text_report,
        mcnemar_keys,
        reports_by_era,
        reports_by_year,
    )

    if label not in ("peak", "duration", "thermal"):
        raise typer.BadParameter("--label must be 'peak', 'duration', or 'thermal'")
    if split not in ("none", "year", "era"):
        raise typer.BadParameter("--split must be 'none', 'year', or 'era'")
    since_d = date.fromisoformat(since) if since else None
    until_d = date.fromisoformat(until) if until else None
    months_set = _resolve_months(season, months)
    report = compile_report(
        since=since_d, until=until_d, label=label,
        resimulated=resimulated, replayed=replayed, months=months_set,
    )
    console.print(format_text_report(report, rule_filter=rule))

    if split != "none":
        if split == "era":
            parts: dict = reports_by_era(
                label=label, resimulated=resimulated, replayed=replayed, months=months_set,
            )
        else:
            parts = reports_by_year(
                label=label, resimulated=resimulated, replayed=replayed, months=months_set,
            )
        console.print(f"\nPer-{split} skill (a real tune holds across splits):")
        for name, part in parts.items():
            console.print(format_skill_line(str(name), part))

    if mcnemar:
        result = mcnemar_keys(
            since=since_d, until=until_d, label=label, replayed=replayed, months=months_set,
        )
        console.print("\n" + format_mcnemar(result, old="overall", new="overall_resimulated"))

    if csv:
        n = export_csv(csv, since=since_d, until=until_d, replayed=replayed, months=months_set)
        console.print(f"\n[dim]Wrote {n} rows to {csv}[/dim]")


def _render_tables(result: Forecast, target: date) -> None:
    verdict_table = Table(title=f"Walchi Oracle — {target.isoformat()}")
    verdict_table.add_column("Rule")
    verdict_table.add_column("Signal")
    verdict_table.add_column("Reason")
    for v in result.verdicts:
        verdict_table.add_row(v.rule, v.signal.value, v.reason)
    console.print(verdict_table)
    console.print(f"[bold]Overall:[/bold] {result.overall.value}")


if __name__ == "__main__":
    app()
