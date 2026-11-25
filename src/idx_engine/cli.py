"""Typer CLI entrypoints: `idx-fetch`, `idx-sim`, and `idx-ui`."""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

import typer
import uvicorn
from rich.console import Console
from rich.table import Table

from idx_engine.config import load_run_config, load_sweep_config
from idx_engine.fetch import (
    DataStore,
    fetch_sharadar_dataset,
    fetch_yfinance_dataset,
    generate_synthetic_sp500_dataset,
    import_local_dataset,
)
from idx_engine.sim import execute_and_persist_run, execute_and_persist_sweep

console = Console()

fetch_app = typer.Typer(
    name="idx-fetch",
    help="Pull, import, generate, and inspect versioned market & index datasets.",
    no_args_is_help=True,
)
sim_app = typer.Typer(
    name="idx-sim",
    help="Run paired direct-indexing vs. ETF simulations, bootstrap sweeps, and lot exports.",
    no_args_is_help=True,
)
ui_app = typer.Typer(
    name="idx-ui",
    help="Launch the FastAPI + React Direct Indexing Workbench.",
)


@fetch_app.command("synthetic")
def fetch_synthetic(
    dataset_id: str = typer.Option("sp500_synthetic_demo", help="Dataset identifier."),
    start_date: str = typer.Option("2019-01-02", help="Start date (YYYY-MM-DD)."),
    trading_days: int = typer.Option(1260, help="Number of trading days (~252/yr)."),
    max_constituents: int | None = typer.Option(None, help="Cap constituent count."),
    seed: int = typer.Option(12345, help="Deterministic RNG seed."),
    data_dir: Path = typer.Option(Path("data"), help="Local data store directory."),
) -> None:
    """Generate a deterministic multi-sector point-in-time synthetic S&P 500 dataset."""
    store = DataStore(data_dir=data_dir)
    meta = generate_synthetic_sp500_dataset(
        store=store,
        dataset_id=dataset_id,
        start_date=date.fromisoformat(start_date),
        trading_days=trading_days,
        max_constituents=max_constituents,
        seed=seed,
    )
    console.print(
        f"[green]Saved dataset[/green] [bold]{meta.dataset_id}@{meta.version}[/bold] "
        f"({meta.ticker_count} tickers, {meta.trading_days} days, "
        f"survivorship_biased={meta.survivorship_biased})"
    )


@fetch_app.command("pull")
def fetch_pull(
    source: str = typer.Option(
        "public",
        help="Data provider: 'public' (fja05680 + yfinance) or 'sharadar' (api.sharadar.com).",
    ),
    dataset_id: str | None = typer.Option(
        None,
        help="Dataset identifier (default: 'sp500_public' or 'sp500_sharadar').",
    ),
    start_date: str = typer.Option("2019-01-01", help="Start date (YYYY-MM-DD)."),
    end_date: str = typer.Option("2024-12-31", help="End date (YYYY-MM-DD)."),
    max_constituents: int | None = typer.Option(
        None,
        help="Debug only: cap constituent count (excludes later additions).",
    ),
    benchmark: str | None = typer.Option(
        None,
        help="Paired S&P 500 ETF symbol (default: VOO for public, SPY for sharadar).",
    ),
    spy_weights: bool = typer.Option(
        True, help="[public] Weight by annual SPY holdings snapshots (else equal-weight proxy)."
    ),
    api_key: str | None = typer.Option(
        None,
        envvar="SHARADAR_API_KEY",
        help="[sharadar] API key (or set SHARADAR_API_KEY; 'test-api-key' uses free sample).",
    ),
    tickers: str | None = typer.Option(
        None,
        help="[sharadar] Optional comma-separated ticker subset (e.g. AAPL,MSFT,NVDA for test key).",
    ),
    refresh_reference_data: bool = typer.Option(
        False, help="Re-download cached membership/weight/action files in data/raw/."
    ),
    refresh_prices: bool = typer.Option(
        False, help="Re-download price history even when it is already cached."
    ),
    retry_missing_prices: bool = typer.Option(
        False, help="[public] Retry symbols previously cached with no price data."
    ),
    data_dir: Path = typer.Option(Path("data"), help="Local data store directory."),
) -> None:
    """Build a point-in-time S&P 500 dataset from public sources or Sharadar."""
    store = DataStore(data_dir=data_dir)
    provider = source.strip().lower()
    if provider == "public":
        meta = fetch_yfinance_dataset(
            store=store,
            dataset_id=dataset_id or "sp500_public",
            start_date=date.fromisoformat(start_date),
            end_date=date.fromisoformat(end_date),
            max_constituents=max_constituents,
            benchmark_ticker=benchmark or "VOO",
            use_spy_weights=spy_weights,
            refresh_reference_data=refresh_reference_data,
            refresh_prices=refresh_prices,
            retry_missing_prices=retry_missing_prices,
        )
    elif provider == "sharadar":
        ticker_list = [t.strip() for t in tickers.split(",") if t.strip()] if tickers else None
        meta = fetch_sharadar_dataset(
            store=store,
            dataset_id=dataset_id or "sp500_sharadar",
            start_date=date.fromisoformat(start_date),
            end_date=date.fromisoformat(end_date),
            benchmark_ticker=benchmark or "SPY",
            api_key=api_key,
            tickers=ticker_list,
            max_constituents=max_constituents,
            refresh_reference_data=refresh_reference_data,
            refresh_prices=refresh_prices,
        )
    else:
        raise typer.BadParameter(f"Unknown --source '{source}'. Expected 'public' or 'sharadar'.")

    console.print(
        f"[green]Pulled dataset[/green] [bold]{meta.dataset_id}@{meta.version}[/bold] "
        f"({meta.ticker_count} tickers, {meta.trading_days} days, "
        f"[yellow]survivorship_biased={meta.survivorship_biased}[/yellow])"
    )
    console.print(f"[dim]{meta.notes}[/dim]")
    console.print(f"Data gaps: {store.parquet_dir / meta.dataset_id / meta.version / 'errors.txt'}")


@fetch_app.command("import")
def fetch_import(
    dataset_id: str = typer.Option(..., help="Target dataset ID."),
    prices: Path = typer.Option(..., exists=True, help="CSV/Parquet wide prices file."),
    weights: Path = typer.Option(..., exists=True, help="CSV/Parquet weights file."),
    etf: Path = typer.Option(..., exists=True, help="CSV/Parquet benchmark ETF file."),
    dividends: Path | None = typer.Option(None, help="Optional CSV/Parquet dividends file."),
    survivorship_biased: bool = typer.Option(
        False, help="Mark True if dataset lacks delisted point-in-time constituents."
    ),
    benchmark: str = typer.Option("VOO", help="Benchmark ETF symbol."),
    data_dir: Path = typer.Option(Path("data"), help="Local data store directory."),
) -> None:
    """Import local CSV/Parquet point-in-time market & weight files."""
    store = DataStore(data_dir=data_dir)
    meta = import_local_dataset(
        store=store,
        dataset_id=dataset_id,
        prices_path=prices,
        weights_path=weights,
        etf_path=etf,
        dividends_path=dividends,
        survivorship_biased=survivorship_biased,
        benchmark_ticker=benchmark,
    )
    console.print(
        f"[green]Imported dataset[/green] [bold]{meta.dataset_id}@{meta.version}[/bold] "
        f"({meta.ticker_count} tickers, {meta.trading_days} days)"
    )


@fetch_app.command("list")
def fetch_list(
    data_dir: Path = typer.Option(Path("data"), help="Local data store directory."),
) -> None:
    """List cached dataset snapshots in DuckDB."""
    store = DataStore(data_dir=data_dir)
    datasets = store.list_datasets()
    table = Table(title="Cached Datasets")
    table.add_column("Dataset@Version", style="bold cyan")
    table.add_column("Source")
    table.add_column("Date Range")
    table.add_column("Days", justify="right")
    table.add_column("Tickers", justify="right")
    table.add_column("Survivorship Biased")
    table.add_column("Missing Bars", justify="right")

    for d in datasets:
        bias_label = "[yellow]YES[/yellow]" if d.survivorship_biased else "[green]NO (PIT)[/green]"
        table.add_row(
            f"{d.dataset_id}@{d.version}",
            d.source,
            f"{d.start_date} -> {d.end_date}",
            str(d.trading_days),
            str(d.ticker_count),
            bias_label,
            f"{d.missing_bar_fraction * 100:.2f}%",
        )
    console.print(table)


def _ensure_demo_dataset_if_needed(store: DataStore, dataset_id: str) -> None:
    if dataset_id != "sp500_synthetic_demo":
        return
    existing = {d.dataset_id for d in store.list_datasets()}
    if "sp500_synthetic_demo" not in existing:
        console.print("[dim]Auto-generating default 'sp500_synthetic_demo' dataset...[/dim]")
        generate_synthetic_sp500_dataset(store=store, dataset_id="sp500_synthetic_demo")


@sim_app.command("run")
def sim_run(
    config: Path = typer.Argument(..., exists=True, help="Path to SimulationRunConfig TOML file."),
    data_dir: Path = typer.Option(Path("data"), help="Local data store directory."),
) -> None:
    """Execute a paired simulation run from a TOML configuration and print distributional metrics."""
    store = DataStore(data_dir=data_dir)
    cfg = load_run_config(config)
    _ensure_demo_dataset_if_needed(store, cfg.dataset.dataset_id)

    res = execute_and_persist_run(store=store, cfg=cfg, persist=True)
    s = res.summary

    table = Table(title=f"Paired Simulation Summary — {s.run_id}")
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")

    table.add_row("Dataset", f"{s.dataset_id}@{s.dataset_version}")
    table.add_row("Validation Status", s.validation_status)
    table.add_row("Path Mode / Count", f"{s.path_mode} ({s.path_count} paths)")
    table.add_row("Strategy vs Benchmark", f"{s.strategy_name} vs {s.benchmark_name}")
    table.add_row(
        "Win Rate (After-Tax Liquidation)", f"{s.win_rate_vs_benchmark_liquidation * 100:.1f}%"
    )
    table.add_row(
        "Median Strategy Liquidation Wealth", f"${s.median_strategy_liquidation_wealth:,.2f}"
    )
    table.add_row(
        "Median Benchmark Liquidation Wealth", f"${s.median_benchmark_liquidation_wealth:,.2f}"
    )
    table.add_row("Paired Wealth Delta P10", f"${s.p10_paired_wealth_delta:,.2f}")
    table.add_row("Paired Wealth Delta P25", f"${s.p25_paired_wealth_delta:,.2f}")
    table.add_row("Paired Wealth Delta Median (P50)", f"${s.median_paired_wealth_delta:,.2f}")
    table.add_row("Paired Wealth Delta P75", f"${s.p75_paired_wealth_delta:,.2f}")
    table.add_row("Paired Wealth Delta P90", f"${s.p90_paired_wealth_delta:,.2f}")
    table.add_row("Median After-Tax CAGR (Strategy)", f"{s.median_after_tax_cagr * 100:.2f}%")
    table.add_row(
        "Median After-Tax CAGR (Benchmark)", f"{s.benchmark_median_after_tax_cagr * 100:.2f}%"
    )
    table.add_row(
        "Tracking Error Median / P90 (bps)",
        f"{s.median_tracking_error_bps:.1f} / {s.p90_tracking_error_bps:.1f}",
    )
    table.add_row("Median Annual Turnover", f"{s.median_annual_turnover * 100:.1f}%")
    table.add_row("Median Harvested Gross Losses", f"${s.median_harvested_losses:,.2f}")
    table.add_row("Median Disallowed Wash Losses", f"${s.median_disallowed_wash_losses:,.2f}")
    table.add_row("Median Closed Lots / Year", f"{s.median_closed_lots_per_year:.1f}")
    console.print(table)


@sim_app.command("sweep")
def sim_sweep(
    config: Path = typer.Argument(..., exists=True, help="Path to SweepConfig TOML file."),
    data_dir: Path = typer.Option(Path("data"), help="Local data store directory."),
) -> None:
    """Execute a multi-parameter sensitivity sweep (and optional walk-forward OOS check)."""
    store = DataStore(data_dir=data_dir)
    sweep_cfg = load_sweep_config(config)
    _ensure_demo_dataset_if_needed(store, sweep_cfg.base.dataset.dataset_id)

    res = execute_and_persist_sweep(store=store, sweep_cfg=sweep_cfg, persist=True)
    table = Table(title=f"Parameter Sweep — {res.name} ({res.sweep_id})")
    table.add_column("Parameters", style="cyan")
    table.add_column("IS Win Rate", justify="right")
    table.add_column("IS Median ΔWealth", justify="right")
    table.add_column("IS TE (bps)", justify="right")
    table.add_column("Plateau Score", justify="right")
    if res.walk_forward_split_ratio is not None:
        table.add_column("OOS Win Rate", justify="right")
        table.add_column("OOS Median ΔWealth", justify="right")

    sorted_rows = sorted(
        res.rows,
        key=lambda r: r.plateau_score if r.plateau_score is not None else float("-inf"),
        reverse=True,
    )
    for row in sorted_rows:
        is_s = row.in_sample_summary
        assert is_s is not None
        p_str = ", ".join(f"{k.split('.')[-1]}={v}" for k, v in row.params.items())
        cols = [
            p_str or "(base)",
            f"{is_s.win_rate_vs_benchmark_liquidation * 100:.1f}%",
            f"${is_s.median_paired_wealth_delta:,.2f}",
            f"{is_s.median_tracking_error_bps:.1f}",
            f"{row.plateau_score:,.2f}" if row.plateau_score is not None else "-",
        ]
        if res.walk_forward_split_ratio is not None:
            oos_s = row.out_of_sample_summary
            cols.append(
                f"{oos_s.win_rate_vs_benchmark_liquidation * 100:.1f}%" if oos_s else "-"
            )
            cols.append(f"${oos_s.median_paired_wealth_delta:,.2f}" if oos_s else "-")
        table.add_row(*cols)
    console.print(table)


@sim_app.command("export-lots")
def sim_export_lots(
    run_id: str = typer.Argument(..., help="Run ID to export closed lots from."),
    output: Path = typer.Option(..., help="Destination .csv or .json file."),
    path_id: str | None = typer.Option(None, help="Optional path_id filter."),
    data_dir: Path = typer.Option(Path("data"), help="Local data store directory."),
) -> None:
    """Export closed tax lots for a simulation run to CSV or JSON."""
    store = DataStore(data_dir=data_dir)
    lots = store.query_closed_lots(run_id=run_id, path_id=path_id, limit=100_000)
    if output.suffix.lower() == ".json":
        output.write_text(json.dumps(lots, indent=2), encoding="utf-8")
    else:
        with output.open("w", newline="", encoding="utf-8") as fp:
            if lots:
                writer = csv.DictWriter(fp, fieldnames=list(lots[0].keys()))
                writer.writeheader()
                writer.writerows(lots)
    console.print(f"[green]Exported {len(lots)} closed lot records to {output}[/green]")


@ui_app.callback(invoke_without_command=True)
def launch_ui(
    host: str = typer.Option("127.0.0.1", help="Bind host."),
    port: int = typer.Option(8000, help="Bind port."),
    data_dir: Path = typer.Option(Path("data"), help="Local data store directory."),
    reload: bool = typer.Option(False, help="Enable uvicorn auto-reload."),
) -> None:
    """Start the FastAPI backend (and serve built React Web UI if present)."""
    import os

    os.environ["IDX_DATA_DIR"] = str(data_dir.resolve())
    store = DataStore(data_dir=data_dir)
    if not store.list_datasets():
        console.print(
            "[yellow]No cached datasets found in DataStore.[/yellow] "
            "Fetch or generate one with `uv run idx-fetch pull` or `uv run idx-fetch synthetic`."
        )
    uvicorn.run("idx_engine.api.main:create_app", factory=True, host=host, port=port, reload=reload)


def run_fetch_cli() -> None:
    fetch_app()


def run_sim_cli() -> None:
    sim_app()


def run_ui_cli() -> None:
    ui_app()
