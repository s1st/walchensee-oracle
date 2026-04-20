"""Command-line entry point."""
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict
from datetime import date, datetime

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from oracle.engine import Forecast, run_forecast

load_dotenv()

app = typer.Typer(help="Walchi Thermic Oracle")
console = Console()


@app.command()
def forecast(
    day: str = typer.Option(None, help="ISO date, defaults to today"),
    json_output: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON to stdout instead of tables."
    ),
) -> None:
    target = date.fromisoformat(day) if day else date.today()
    result = asyncio.run(run_forecast(target))

    if json_output:
        sys.stdout.write(_to_json(result, target) + "\n")
        return

    _render_tables(result, target)


def _to_json(result: Forecast, target: date) -> str:
    return json.dumps(
        {
            "day": target.isoformat(),
            "overall": result.overall.value,
            "verdicts": [
                {"rule": v.rule, "signal": v.signal.value, "reason": v.reason}
                for v in result.verdicts
            ],
            "chat_messages": [
                {
                    "posted_at": m.posted_at.isoformat(),
                    "author": m.author,
                    "channel": m.channel,
                    "text": m.text,
                }
                for m in result.chat_messages
            ],
        },
        ensure_ascii=False,
        default=_json_default,
    )


def _json_default(obj: object) -> str:
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"not JSON-serialisable: {type(obj).__name__}")


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
