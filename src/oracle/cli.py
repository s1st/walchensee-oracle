"""Command-line entry point: `oracle forecast`."""
from __future__ import annotations

import asyncio
from datetime import date

import typer
from rich.console import Console
from rich.table import Table

from oracle.engine import run_forecast

app = typer.Typer(help="Walchi Thermic Oracle")
console = Console()


@app.command()
def forecast(day: str = typer.Option(None, help="ISO date, defaults to today")) -> None:
    target = date.fromisoformat(day) if day else date.today()
    result = asyncio.run(run_forecast(target))

    table = Table(title=f"Walchi Oracle — {target.isoformat()}")
    table.add_column("Rule")
    table.add_column("Signal")
    table.add_column("Reason")
    for v in result.verdicts:
        table.add_row(v.rule, v.signal.value, v.reason)

    console.print(table)
    console.print(f"[bold]Overall:[/bold] {result.overall.value}")


if __name__ == "__main__":
    app()
