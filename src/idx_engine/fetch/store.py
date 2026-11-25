"""Versioned Parquet + DuckDB persistence layer for market datasets, runs, lots, and sweeps."""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import polars as pl

from idx_engine.models import (
    ClosedLotRecord,
    CorporateAction,
    DailySnapshot,
    DatasetMetadata,
    DistributionSummary,
    PairedPathComparison,
    SweepRow,
    WashSaleAdjustment,
)


class _DecimalJsonEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, date):
            return obj.isoformat()
        return super().default(obj)


def dumps_canonical(obj: Any) -> str:
    return json.dumps(obj, cls=_DecimalJsonEncoder, sort_keys=True)


@dataclass(frozen=True)
class MarketDataset:
    """Loaded immutable market dataset snapshot ready for path generation."""

    metadata: DatasetMetadata
    trading_dates: list[date]
    prices_df: pl.DataFrame
    returns_df: pl.DataFrame
    dividends_df: pl.DataFrame
    weights_df: pl.DataFrame
    etf_df: pl.DataFrame
    sectors: dict[str, str]
    names: dict[str, str]
    corporate_actions: tuple[CorporateAction, ...]

    @property
    def tickers(self) -> list[str]:
        return [c for c in self.prices_df.columns if c != "date"]

    def slice_dates(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> MarketDataset:
        """Return a date-bounded view of this dataset without mutating stored files."""
        if start_date is None and end_date is None:
            return self
        s = start_date or self.metadata.start_date
        e = end_date or self.metadata.end_date
        mask = (pl.col("date") >= pl.lit(s)) & (pl.col("date") <= pl.lit(e))

        prices = self.prices_df.filter(mask)
        returns = self.returns_df.filter(mask)
        dividends = self.dividends_df.filter(mask)
        weights = self.weights_df.filter(mask)
        etf = self.etf_df.filter(mask)
        dates = prices.get_column("date").to_list()
        if not dates:
            raise ValueError(f"Date slice [{s}, {e}] produced 0 trading days")

        actions = tuple(ca for ca in self.corporate_actions if s <= ca.effective_date <= e)
        sliced_meta = DatasetMetadata(
            dataset_id=self.metadata.dataset_id,
            version=self.metadata.version,
            source=self.metadata.source,
            created_at_iso=self.metadata.created_at_iso,
            start_date=dates[0],
            end_date=dates[-1],
            trading_days=len(dates),
            ticker_count=self.metadata.ticker_count,
            benchmark_ticker=self.metadata.benchmark_ticker,
            survivorship_biased=self.metadata.survivorship_biased,
            missing_bar_fraction=self.metadata.missing_bar_fraction,
            notes=self.metadata.notes,
        )
        return MarketDataset(
            metadata=sliced_meta,
            trading_dates=dates,
            prices_df=prices,
            returns_df=returns,
            dividends_df=dividends,
            weights_df=weights,
            etf_df=etf,
            sectors=self.sectors,
            names=self.names,
            corporate_actions=actions,
        )


_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def validate_safe_identifier(value: str, field_name: str = "identifier") -> str:
    if not _SAFE_ID_RE.fullmatch(value):
        raise ValueError(
            f"Invalid {field_name} '{value}': must match ^[A-Za-z0-9][A-Za-z0-9._-]{{0,63}}$"
        )
    return value


class DataStore:
    """Thread-safe, transactional Parquet + DuckDB catalog for idx_engine."""

    def __init__(self, data_dir: str | Path = "data") -> None:
        self.data_dir = Path(data_dir)
        self.raw_dir = self.data_dir / "raw"
        self.parquet_dir = self.data_dir / "parquet"
        self.db_path = self.data_dir / "app.duckdb"
        self._lock = threading.RLock()
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.parquet_dir.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(
        self,
        read_only: bool = False,
        transactional: bool = False,
        timeout_sec: float = 5.0,
    ) -> Iterator[duckdb.DuckDBPyConnection]:
        """Acquire the store lock and open a uniform-mode DuckDB connection.

        All in-process operations use a uniform connection configuration under `self._lock`
        so concurrent FastAPI reads and background job writes never trigger DuckDB's
        mixed-`read_only` `ConnectionException`. Transient cross-process file-lock
        `IOException`s (e.g. running `idx-fetch` in a separate terminal) are retried until
        `timeout_sec` elapses.
        """
        del read_only
        with self._lock:
            deadline = time.monotonic() + timeout_sec
            delay = 0.05
            while True:
                try:
                    con = duckdb.connect(str(self.db_path))
                    break
                except duckdb.IOException:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise
                    time.sleep(min(delay, remaining))
                    delay = min(0.2, delay + 0.05)
            try:
                if transactional:
                    con.begin()
                yield con
                if transactional:
                    con.commit()
            except Exception:
                if transactional:
                    with suppress(Exception):
                        con.rollback()
                raise
            finally:
                con.close()

    def _init_schema(self) -> None:
        with self._connect(read_only=False) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id VARCHAR PRIMARY KEY,
                    created_at_iso VARCHAR NOT NULL,
                    dataset_id VARCHAR NOT NULL,
                    dataset_version VARCHAR NOT NULL,
                    survivorship_biased BOOLEAN NOT NULL,
                    validation_status VARCHAR NOT NULL,
                    strategy_name VARCHAR NOT NULL,
                    benchmark_name VARCHAR NOT NULL,
                    path_mode VARCHAR NOT NULL,
                    path_count INTEGER NOT NULL,
                    config_json VARCHAR NOT NULL,
                    summary_json VARCHAR NOT NULL
                );

                CREATE TABLE IF NOT EXISTS path_outcomes (
                    run_id VARCHAR NOT NULL,
                    path_id VARCHAR NOT NULL,
                    direct_json VARCHAR NOT NULL,
                    benchmark_json VARCHAR NOT NULL,
                    paired_realized_delta DOUBLE NOT NULL,
                    paired_liquidation_delta DOUBLE NOT NULL,
                    paired_cagr_delta_bps DOUBLE NOT NULL,
                    tracking_error_bps DOUBLE NOT NULL,
                    PRIMARY KEY (run_id, path_id)
                );

                CREATE TABLE IF NOT EXISTS equity_band_points (
                    run_id VARCHAR NOT NULL,
                    day_index INTEGER NOT NULL,
                    trading_date DATE NOT NULL,
                    strat_p10 DOUBLE NOT NULL,
                    strat_p25 DOUBLE NOT NULL,
                    strat_p50 DOUBLE NOT NULL,
                    strat_p75 DOUBLE NOT NULL,
                    strat_p90 DOUBLE NOT NULL,
                    bench_p50 DOUBLE NOT NULL,
                    delta_p10 DOUBLE NOT NULL,
                    delta_p25 DOUBLE NOT NULL,
                    delta_p50 DOUBLE NOT NULL,
                    delta_p75 DOUBLE NOT NULL,
                    delta_p90 DOUBLE NOT NULL,
                    PRIMARY KEY (run_id, day_index)
                );

                CREATE TABLE IF NOT EXISTS closed_lots (
                    run_id VARCHAR NOT NULL,
                    path_id VARCHAR NOT NULL,
                    strategy_role VARCHAR NOT NULL,
                    close_id VARCHAR NOT NULL,
                    source_lot_id VARCHAR NOT NULL,
                    ticker VARCHAR NOT NULL,
                    sector VARCHAR NOT NULL,
                    acquired_date DATE NOT NULL,
                    sold_date DATE NOT NULL,
                    tax_year INTEGER NOT NULL,
                    holding_days INTEGER NOT NULL,
                    term VARCHAR NOT NULL,
                    quantity DOUBLE NOT NULL,
                    proceeds_total DOUBLE NOT NULL,
                    cost_basis_total DOUBLE NOT NULL,
                    raw_realized_gain_loss DOUBLE NOT NULL,
                    disallowed_wash_loss DOUBLE NOT NULL,
                    adjusted_realized_gain_loss DOUBLE NOT NULL,
                    reason VARCHAR NOT NULL
                );

                CREATE TABLE IF NOT EXISTS wash_sales (
                    run_id VARCHAR NOT NULL,
                    path_id VARCHAR NOT NULL,
                    adjustment_id VARCHAR NOT NULL,
                    closed_lot_id VARCHAR NOT NULL,
                    loss_ticker VARCHAR NOT NULL,
                    replacement_ticker VARCHAR NOT NULL,
                    sold_date DATE NOT NULL,
                    replacement_date DATE NOT NULL,
                    replacement_lot_id VARCHAR NOT NULL,
                    matched_quantity DOUBLE NOT NULL,
                    disallowed_loss DOUBLE NOT NULL,
                    tacked_holding_days INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sweeps (
                    sweep_id VARCHAR PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    created_at_iso VARCHAR NOT NULL,
                    dataset_id VARCHAR NOT NULL,
                    dataset_version VARCHAR NOT NULL,
                    walk_forward_split_ratio DOUBLE,
                    config_json VARCHAR NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sweep_rows (
                    sweep_id VARCHAR NOT NULL,
                    run_id VARCHAR NOT NULL,
                    params_json VARCHAR NOT NULL,
                    in_sample_json VARCHAR NOT NULL,
                    out_of_sample_json VARCHAR,
                    plateau_score DOUBLE,
                    PRIMARY KEY (sweep_id, run_id)
                );
                """
            )

    @staticmethod
    def _version_sort_key(version: str) -> tuple[int, str]:
        if version.startswith("v") and version[1:].isdigit():
            return (int(version[1:]), version)
        return (-1, version)

    @staticmethod
    def _read_dataset_metadata(version_dir: Path) -> DatasetMetadata | None:
        meta_path = version_dir / "metadata.json"
        if not meta_path.is_file():
            return None
        try:
            raw = json.loads(meta_path.read_text(encoding="utf-8"))
            return DatasetMetadata(
                dataset_id=version_dir.parent.name,
                version=version_dir.name,
                source=str(raw["source"]),
                created_at_iso=str(raw["created_at_iso"]),
                start_date=date.fromisoformat(str(raw["start_date"])),
                end_date=date.fromisoformat(str(raw["end_date"])),
                trading_days=int(raw["trading_days"]),
                ticker_count=int(raw["ticker_count"]),
                benchmark_ticker=str(raw["benchmark_ticker"]),
                survivorship_biased=bool(raw["survivorship_biased"]),
                missing_bar_fraction=float(raw["missing_bar_fraction"]),
                notes=str(raw.get("notes", "")),
            )
        except (OSError, KeyError, TypeError, ValueError):
            return None

    def next_dataset_version(self, dataset_id: str) -> str:
        validate_safe_identifier(dataset_id, "dataset_id")
        ds_dir = self.parquet_dir / dataset_id
        if not ds_dir.is_dir():
            return "v1"
        versions = [p.name for p in ds_dir.iterdir() if p.is_dir()]
        if not versions:
            return "v1"
        nums = [int(v[1:]) for v in versions if v.startswith("v") and v[1:].isdigit()]
        next_num = (max(nums) + 1) if nums else (len(versions) + 1)
        return f"v{next_num}"

    def resolve_dataset_version(self, dataset_id: str, version: str = "latest") -> str:
        validate_safe_identifier(dataset_id, "dataset_id")
        ds_dir = self.parquet_dir / dataset_id
        if version == "latest":
            candidates: list[DatasetMetadata] = []
            if ds_dir.is_dir():
                for vdir in ds_dir.iterdir():
                    if not vdir.is_dir():
                        continue
                    meta = self._read_dataset_metadata(vdir)
                    if meta is not None:
                        candidates.append(meta)
            if not candidates:
                raise KeyError(f"Dataset '{dataset_id}' not found in store")
            candidates.sort(
                key=lambda m: (m.created_at_iso, self._version_sort_key(m.version)),
                reverse=True,
            )
            return candidates[0].version

        validate_safe_identifier(version, "version")
        if self._read_dataset_metadata(ds_dir / version) is None:
            raise KeyError(f"Dataset '{dataset_id}@{version}' not found in store")
        return version

    def run_exists(self, run_id: str) -> bool:
        with self._connect(read_only=True) as con:
            row = con.execute("SELECT 1 FROM runs WHERE run_id = ? LIMIT 1", [run_id]).fetchone()
        return row is not None

    def save_dataset(
        self,
        dataset_id: str,
        source: str,
        prices_df: pl.DataFrame,
        returns_df: pl.DataFrame,
        dividends_df: pl.DataFrame,
        weights_df: pl.DataFrame,
        etf_df: pl.DataFrame,
        sectors: dict[str, str],
        names: dict[str, str] | None = None,
        corporate_actions: tuple[CorporateAction, ...] = (),
        benchmark_ticker: str = "VOO",
        survivorship_biased: bool = True,
        missing_bar_fraction: float = 0.0,
        notes: str = "",
        version: str | None = None,
        created_at_iso: str | None = None,
        extra_files: dict[str, str] | None = None,
    ) -> DatasetMetadata:
        """Persist a new immutable dataset version to Parquet + DuckDB.

        `extra_files` maps file names to text written alongside the Parquet files
        (e.g. an `errors.txt` data-quality report).
        """
        validate_safe_identifier(dataset_id, "dataset_id")
        ver = validate_safe_identifier(version or self.next_dataset_version(dataset_id), "version")
        parquet_root = self.parquet_dir.resolve()
        target_dir = (parquet_root / dataset_id / ver).resolve()
        if not target_dir.is_relative_to(parquet_root):
            raise ValueError(f"Unsafe target directory for {dataset_id}@{ver}")
        if target_dir.exists():
            raise FileExistsError(
                f"Dataset {dataset_id}@{ver} already exists; mint a new version instead of mutating."
            )
        target_dir.mkdir(parents=True, exist_ok=False)

        try:
            prices_df.write_parquet(target_dir / "prices.parquet")
            returns_df.write_parquet(target_dir / "returns.parquet")
            dividends_df.write_parquet(target_dir / "dividends.parquet")
            weights_df.write_parquet(target_dir / "weights.parquet")
            etf_df.write_parquet(target_dir / "etf.parquet")

            tickers = [c for c in prices_df.columns if c != "date"]
            names_map = names or {}
            sectors_df = pl.DataFrame(
                {
                    "ticker": tickers,
                    "sector": [sectors.get(t, "Unknown") for t in tickers],
                    "name": [names_map.get(t, t) for t in tickers],
                }
            )
            sectors_df.write_parquet(target_dir / "sectors.parquet")

            ca_rows = [
                {
                    "action_id": ca.action_id,
                    "effective_date": ca.effective_date,
                    "action_type": ca.action_type,
                    "ticker": ca.ticker,
                    "ratio_numerator": str(ca.ratio_numerator),
                    "ratio_denominator": str(ca.ratio_denominator),
                    "cash_amount_per_share": str(ca.cash_amount_per_share),
                    "qualified_fraction": str(ca.qualified_fraction),
                    "target_ticker": ca.target_ticker,
                    "target_sector": ca.target_sector,
                    "basis_allocation_fraction": str(ca.basis_allocation_fraction),
                }
                for ca in corporate_actions
            ]
            ca_schema = {
                "action_id": pl.String,
                "effective_date": pl.Date,
                "action_type": pl.String,
                "ticker": pl.String,
                "ratio_numerator": pl.String,
                "ratio_denominator": pl.String,
                "cash_amount_per_share": pl.String,
                "qualified_fraction": pl.String,
                "target_ticker": pl.String,
                "target_sector": pl.String,
                "basis_allocation_fraction": pl.String,
            }
            pl.DataFrame(ca_rows, schema=ca_schema).write_parquet(
                target_dir / "corporate_actions.parquet"
            )

            dates = prices_df.get_column("date").to_list()
            created_ts = created_at_iso or datetime.now(UTC).isoformat()
            meta = DatasetMetadata(
                dataset_id=dataset_id,
                version=ver,
                source=source,
                created_at_iso=created_ts,
                start_date=dates[0],
                end_date=dates[-1],
                trading_days=len(dates),
                ticker_count=len(tickers),
                benchmark_ticker=benchmark_ticker,
                survivorship_biased=survivorship_biased,
                missing_bar_fraction=float(missing_bar_fraction),
                notes=notes,
            )
            for fname, text in (extra_files or {}).items():
                validate_safe_identifier(fname, "extra_file_name")
                (target_dir / fname).write_text(text, encoding="utf-8")

            # Write metadata.json atomically last so incomplete directories are never discovered.
            meta_tmp = target_dir / "metadata.json.part"
            meta_tmp.write_text(dumps_canonical(asdict(meta)), encoding="utf-8")
            meta_tmp.replace(target_dir / "metadata.json")
            return meta
        except Exception:
            shutil.rmtree(target_dir, ignore_errors=True)
            raise

    def list_datasets(self) -> list[DatasetMetadata]:
        datasets: list[DatasetMetadata] = []
        if not self.parquet_dir.is_dir():
            return datasets
        for ds_dir in self.parquet_dir.iterdir():
            if not ds_dir.is_dir():
                continue
            for vdir in ds_dir.iterdir():
                if not vdir.is_dir():
                    continue
                meta = self._read_dataset_metadata(vdir)
                if meta is not None:
                    datasets.append(meta)
        datasets.sort(
            key=lambda m: (m.created_at_iso, self._version_sort_key(m.version), m.dataset_id),
            reverse=True,
        )
        return datasets

    def load_dataset(
        self,
        dataset_id: str,
        version: str = "latest",
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> MarketDataset:
        resolved = self.resolve_dataset_version(dataset_id, version)
        pdir = self.parquet_dir / dataset_id / resolved
        meta = self._read_dataset_metadata(pdir)
        if meta is None:
            raise KeyError(f"Dataset '{dataset_id}@{resolved}' not found")

        prices_df = pl.read_parquet(pdir / "prices.parquet")
        returns_df = pl.read_parquet(pdir / "returns.parquet")
        dividends_df = pl.read_parquet(pdir / "dividends.parquet")
        weights_df = pl.read_parquet(pdir / "weights.parquet")
        etf_df = pl.read_parquet(pdir / "etf.parquet")
        sectors_df = pl.read_parquet(pdir / "sectors.parquet")
        ca_df = pl.read_parquet(pdir / "corporate_actions.parquet")

        sectors = {str(r["ticker"]): str(r["sector"]) for r in sectors_df.iter_rows(named=True)}
        names = {
            str(r["ticker"]): str(r.get("name") or r["ticker"])
            for r in sectors_df.iter_rows(named=True)
        }
        actions = tuple(
            CorporateAction(
                action_id=str(r["action_id"]),
                effective_date=r["effective_date"],
                action_type=r["action_type"],
                ticker=str(r["ticker"]),
                ratio_numerator=Decimal(str(r["ratio_numerator"])),
                ratio_denominator=Decimal(str(r["ratio_denominator"])),
                cash_amount_per_share=Decimal(str(r["cash_amount_per_share"])),
                qualified_fraction=Decimal(str(r["qualified_fraction"])),
                target_ticker=r["target_ticker"],
                target_sector=r["target_sector"],
                basis_allocation_fraction=Decimal(str(r["basis_allocation_fraction"])),
            )
            for r in ca_df.iter_rows(named=True)
        )

        ds = MarketDataset(
            metadata=meta,
            trading_dates=prices_df.get_column("date").to_list(),
            prices_df=prices_df,
            returns_df=returns_df,
            dividends_df=dividends_df,
            weights_df=weights_df,
            etf_df=etf_df,
            sectors=sectors,
            names=names,
            corporate_actions=actions,
        )
        return ds.slice_dates(start_date=start_date, end_date=end_date)

    def save_run_results(
        self,
        summary: DistributionSummary,
        config_dict: dict[str, Any],
        comparisons: list[PairedPathComparison],
        direct_snapshots_by_path: dict[str, list[DailySnapshot]] | None = None,
        bench_snapshots_by_path: dict[str, list[DailySnapshot]] | None = None,
        closed_lots_by_path: dict[str, tuple[list[ClosedLotRecord], list[ClosedLotRecord]]]
        | None = None,
        wash_sales_by_path: dict[str, list[WashSaleAdjustment]] | None = None,
        created_at_iso: str | None = None,
    ) -> None:
        """Persist a completed paired simulation run, percentile equity bands, and lot records."""
        ts = created_at_iso or datetime.now(UTC).isoformat()

        with self._connect(read_only=False, transactional=True) as con:
            con.execute("DELETE FROM runs WHERE run_id = ?", [summary.run_id])
            con.execute("DELETE FROM path_outcomes WHERE run_id = ?", [summary.run_id])
            con.execute("DELETE FROM equity_band_points WHERE run_id = ?", [summary.run_id])
            con.execute("DELETE FROM closed_lots WHERE run_id = ?", [summary.run_id])
            con.execute("DELETE FROM wash_sales WHERE run_id = ?", [summary.run_id])

            con.execute(
                """
                INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    summary.run_id,
                    ts,
                    summary.dataset_id,
                    summary.dataset_version,
                    summary.survivorship_biased,
                    summary.validation_status,
                    summary.strategy_name,
                    summary.benchmark_name,
                    summary.path_mode,
                    summary.path_count,
                    dumps_canonical(config_dict),
                    dumps_canonical(asdict(summary)),
                ],
            )

            if comparisons:
                outcomes_df = pl.DataFrame(
                    {
                        "run_id": [summary.run_id] * len(comparisons),
                        "path_id": [comp.path_id for comp in comparisons],
                        "direct_json": [
                            dumps_canonical(asdict(comp.direct_outcome)) for comp in comparisons
                        ],
                        "benchmark_json": [
                            dumps_canonical(asdict(comp.benchmark_outcome)) for comp in comparisons
                        ],
                        "paired_realized_delta": [
                            float(comp.paired_realized_wealth_delta) for comp in comparisons
                        ],
                        "paired_liquidation_delta": [
                            float(comp.paired_liquidation_wealth_delta) for comp in comparisons
                        ],
                        "paired_cagr_delta_bps": [
                            float(comp.paired_after_tax_cagr_delta_bps) for comp in comparisons
                        ],
                        "tracking_error_bps": [
                            float(comp.annualized_tracking_error_bps) for comp in comparisons
                        ],
                    }
                )
                con.register("tmp_outcomes_df", outcomes_df)
                con.execute("INSERT INTO path_outcomes SELECT * FROM tmp_outcomes_df")
                con.unregister("tmp_outcomes_df")

            if direct_snapshots_by_path and bench_snapshots_by_path:
                path_ids = sorted(direct_snapshots_by_path.keys())
                if path_ids:
                    min_len = min(len(direct_snapshots_by_path[pid]) for pid in path_ids)
                    strat_mat = np.array(
                        [
                            [
                                float(s.after_tax_wealth_pre_liquidation)
                                for s in direct_snapshots_by_path[pid][:min_len]
                            ]
                            for pid in path_ids
                        ],
                        dtype=np.float64,
                    )
                    bench_mat = np.array(
                        [
                            [
                                float(s.after_tax_wealth_pre_liquidation)
                                for s in bench_snapshots_by_path[pid][:min_len]
                            ]
                            for pid in path_ids
                        ],
                        dtype=np.float64,
                    )
                    delta_mat = strat_mat - bench_mat
                    sp = np.percentile(strat_mat, [10, 25, 50, 75, 90], axis=0)
                    bp50 = np.percentile(bench_mat, 50, axis=0)
                    dp = np.percentile(delta_mat, [10, 25, 50, 75, 90], axis=0)

                    ref_snaps = direct_snapshots_by_path[path_ids[0]][:min_len]
                    bands_df = pl.DataFrame(
                        {
                            "run_id": [summary.run_id] * min_len,
                            "day_index": list(range(min_len)),
                            "trading_date": [ref_snaps[idx].date for idx in range(min_len)],
                            "strat_p10": sp[0, :].tolist(),
                            "strat_p25": sp[1, :].tolist(),
                            "strat_p50": sp[2, :].tolist(),
                            "strat_p75": sp[3, :].tolist(),
                            "strat_p90": sp[4, :].tolist(),
                            "bench_p50": bp50.tolist(),
                            "delta_p10": dp[0, :].tolist(),
                            "delta_p25": dp[1, :].tolist(),
                            "delta_p50": dp[2, :].tolist(),
                            "delta_p75": dp[3, :].tolist(),
                            "delta_p90": dp[4, :].tolist(),
                        }
                    )
                    con.register("tmp_bands_df", bands_df)
                    con.execute("INSERT INTO equity_band_points SELECT * FROM tmp_bands_df")
                    con.unregister("tmp_bands_df")

            if closed_lots_by_path:
                lot_rows: list[dict[str, Any]] = []
                for pid, (direct_lots, bench_lots) in closed_lots_by_path.items():
                    for role, lots in (("direct", direct_lots), ("benchmark", bench_lots)):
                        for cl in lots:
                            lot_rows.append(
                                {
                                    "run_id": summary.run_id,
                                    "path_id": pid,
                                    "strategy_role": role,
                                    "close_id": cl.close_id,
                                    "source_lot_id": cl.source_lot_id,
                                    "ticker": cl.ticker,
                                    "sector": cl.sector,
                                    "acquired_date": cl.acquired_date,
                                    "sold_date": cl.sold_date,
                                    "tax_year": cl.sold_date.year,
                                    "holding_days": cl.holding_days,
                                    "term": cl.term,
                                    "quantity": float(cl.quantity),
                                    "proceeds_total": float(cl.proceeds_total),
                                    "cost_basis_total": float(cl.cost_basis_total),
                                    "raw_realized_gain_loss": float(cl.raw_realized_gain_loss),
                                    "disallowed_wash_loss": float(cl.disallowed_wash_loss),
                                    "adjusted_realized_gain_loss": float(
                                        cl.adjusted_realized_gain_loss
                                    ),
                                    "reason": cl.reason,
                                }
                            )
                if lot_rows:
                    lots_df = pl.DataFrame(lot_rows)
                    con.register("tmp_lots_df", lots_df)
                    con.execute("INSERT INTO closed_lots SELECT * FROM tmp_lots_df")
                    con.unregister("tmp_lots_df")

            if wash_sales_by_path:
                ws_rows: list[dict[str, Any]] = []
                for pid, adjs in wash_sales_by_path.items():
                    for adj in adjs:
                        ws_rows.append(
                            {
                                "run_id": summary.run_id,
                                "path_id": pid,
                                "adjustment_id": adj.adjustment_id,
                                "closed_lot_id": adj.closed_lot_id,
                                "loss_ticker": adj.loss_ticker,
                                "replacement_ticker": adj.replacement_ticker,
                                "sold_date": adj.sold_date,
                                "replacement_date": adj.replacement_date,
                                "replacement_lot_id": adj.replacement_lot_id,
                                "matched_quantity": float(adj.matched_quantity),
                                "disallowed_loss": float(adj.disallowed_loss),
                                "tacked_holding_days": adj.tacked_holding_days,
                            }
                        )
                if ws_rows:
                    ws_df = pl.DataFrame(ws_rows)
                    con.register("tmp_ws_df", ws_df)
                    con.execute("INSERT INTO wash_sales SELECT * FROM tmp_ws_df")
                    con.unregister("tmp_ws_df")

    def list_runs(self) -> list[dict[str, Any]]:
        with self._connect(read_only=True) as con:
            rows = con.execute(
                """
                SELECT run_id, created_at_iso, dataset_id, dataset_version,
                       survivorship_biased, validation_status, strategy_name,
                       benchmark_name, path_mode, path_count, summary_json
                FROM runs
                ORDER BY created_at_iso DESC
                """
            ).fetchall()
        return [
            {
                "run_id": r[0],
                "created_at_iso": r[1],
                "dataset_id": r[2],
                "dataset_version": r[3],
                "survivorship_biased": bool(r[4]),
                "validation_status": r[5],
                "strategy_name": r[6],
                "benchmark_name": r[7],
                "path_mode": r[8],
                "path_count": int(r[9]),
                "summary": json.loads(r[10]),
            }
            for r in rows
        ]

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._connect(read_only=True) as con:
            row = con.execute(
                """
                SELECT run_id, created_at_iso, dataset_id, dataset_version,
                       survivorship_biased, validation_status, strategy_name,
                       benchmark_name, path_mode, path_count, config_json, summary_json
                FROM runs WHERE run_id = ?
                """,
                [run_id],
            ).fetchone()
            if row is None:
                raise KeyError(f"Run '{run_id}' not found")
            outcomes = con.execute(
                """
                SELECT path_id, direct_json, benchmark_json, paired_realized_delta,
                       paired_liquidation_delta, paired_cagr_delta_bps, tracking_error_bps
                FROM path_outcomes WHERE run_id = ? ORDER BY path_id
                """,
                [run_id],
            ).fetchall()
            bands = con.execute(
                """
                SELECT day_index, trading_date, strat_p10, strat_p25, strat_p50,
                       strat_p75, strat_p90, bench_p50, delta_p10, delta_p25,
                       delta_p50, delta_p75, delta_p90
                FROM equity_band_points WHERE run_id = ? ORDER BY day_index
                """,
                [run_id],
            ).fetchall()

        return {
            "run_id": row[0],
            "created_at_iso": row[1],
            "dataset_id": row[2],
            "dataset_version": row[3],
            "survivorship_biased": bool(row[4]),
            "validation_status": row[5],
            "strategy_name": row[6],
            "benchmark_name": row[7],
            "path_mode": row[8],
            "path_count": int(row[9]),
            "config": json.loads(row[10]),
            "summary": json.loads(row[11]),
            "paths": [
                {
                    "path_id": p[0],
                    "direct": json.loads(p[1]),
                    "benchmark": json.loads(p[2]),
                    "paired_realized_delta": p[3],
                    "paired_liquidation_delta": p[4],
                    "paired_cagr_delta_bps": p[5],
                    "tracking_error_bps": p[6],
                }
                for p in outcomes
            ],
            "equity_bands": [
                {
                    "day_index": b[0],
                    "date": b[1].isoformat(),
                    "strat_p10": b[2],
                    "strat_p25": b[3],
                    "strat_p50": b[4],
                    "strat_p75": b[5],
                    "strat_p90": b[6],
                    "bench_p50": b[7],
                    "delta_p10": b[8],
                    "delta_p25": b[9],
                    "delta_p50": b[10],
                    "delta_p75": b[11],
                    "delta_p90": b[12],
                }
                for b in bands
            ],
        }

    def query_closed_lots(
        self,
        run_id: str,
        path_id: str | None = None,
        ticker: str | None = None,
        tax_year: int | None = None,
        term: str | None = None,
        strategy_role: str = "direct",
        limit: int | None = 1000,
    ) -> list[dict[str, Any]]:
        clauses = ["run_id = ?", "strategy_role = ?"]
        params: list[Any] = [run_id, strategy_role]
        if path_id:
            clauses.append("path_id = ?")
            params.append(path_id)
        if ticker:
            clauses.append("UPPER(ticker) = UPPER(?)")
            params.append(ticker)
        if tax_year is not None:
            clauses.append("tax_year = ?")
            params.append(tax_year)
        if term:
            clauses.append("UPPER(term) = UPPER(?)")
            params.append(term)
        limit_clause = ""
        if limit is not None:
            limit_clause = "LIMIT ?"
            params.append(limit)

        sql = f"""
            SELECT path_id, close_id, source_lot_id, ticker, sector,
                   acquired_date, sold_date, tax_year, holding_days, term,
                   quantity, proceeds_total, cost_basis_total,
                   raw_realized_gain_loss, disallowed_wash_loss,
                   adjusted_realized_gain_loss, reason
            FROM closed_lots
            WHERE {" AND ".join(clauses)}
            ORDER BY sold_date ASC, close_id ASC
            {limit_clause}
        """
        with self._connect(read_only=True) as con:
            rows = con.execute(sql, params).fetchall()
        return [
            {
                "path_id": r[0],
                "close_id": r[1],
                "source_lot_id": r[2],
                "ticker": r[3],
                "sector": r[4],
                "acquired_date": r[5].isoformat(),
                "sold_date": r[6].isoformat(),
                "tax_year": r[7],
                "holding_days": r[8],
                "term": r[9],
                "quantity": r[10],
                "proceeds_total": r[11],
                "cost_basis_total": r[12],
                "raw_realized_gain_loss": r[13],
                "disallowed_wash_loss": r[14],
                "adjusted_realized_gain_loss": r[15],
                "reason": r[16],
            }
            for r in rows
        ]

    def query_wash_sales(
        self,
        run_id: str,
        path_id: str | None = None,
        ticker: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        clauses = ["run_id = ?"]
        params: list[Any] = [run_id]
        if path_id:
            clauses.append("path_id = ?")
            params.append(path_id)
        if ticker:
            clauses.append(
                "(UPPER(loss_ticker) = UPPER(?) OR UPPER(replacement_ticker) = UPPER(?))"
            )
            params.extend([ticker, ticker])
        params.append(limit)

        sql = f"""
            SELECT path_id, adjustment_id, closed_lot_id, loss_ticker,
                   replacement_ticker, sold_date, replacement_date,
                   replacement_lot_id, matched_quantity, disallowed_loss,
                   tacked_holding_days
            FROM wash_sales
            WHERE {" AND ".join(clauses)}
            ORDER BY sold_date ASC, adjustment_id ASC
            LIMIT ?
        """
        with self._connect(read_only=True) as con:
            rows = con.execute(sql, params).fetchall()
        return [
            {
                "path_id": r[0],
                "adjustment_id": r[1],
                "closed_lot_id": r[2],
                "loss_ticker": r[3],
                "replacement_ticker": r[4],
                "sold_date": r[5].isoformat(),
                "replacement_date": r[6].isoformat(),
                "replacement_lot_id": r[7],
                "matched_quantity": r[8],
                "disallowed_loss": r[9],
                "tacked_holding_days": r[10],
            }
            for r in rows
        ]

    def save_sweep(
        self,
        sweep_id: str,
        name: str,
        dataset_id: str,
        dataset_version: str,
        walk_forward_split_ratio: float | None,
        config_dict: dict[str, Any],
        rows: list[SweepRow],
        created_at_iso: str | None = None,
    ) -> None:
        ts = created_at_iso or datetime.now(UTC).isoformat()
        with self._connect(read_only=False, transactional=True) as con:
            con.execute("DELETE FROM sweeps WHERE sweep_id = ?", [sweep_id])
            con.execute("DELETE FROM sweep_rows WHERE sweep_id = ?", [sweep_id])
            con.execute(
                "INSERT INTO sweeps VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    sweep_id,
                    name,
                    ts,
                    dataset_id,
                    dataset_version,
                    walk_forward_split_ratio,
                    dumps_canonical(config_dict),
                ],
            )
            seen_run_ids: set[str] = set()
            for row in rows:
                if row.run_id in seen_run_ids:
                    continue
                seen_run_ids.add(row.run_id)
                con.execute(
                    "INSERT INTO sweep_rows VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        sweep_id,
                        row.run_id,
                        dumps_canonical(row.params),
                        dumps_canonical(asdict(row.in_sample_summary))
                        if row.in_sample_summary
                        else "{}",
                        dumps_canonical(asdict(row.out_of_sample_summary))
                        if row.out_of_sample_summary
                        else None,
                        row.plateau_score,
                    ],
                )

    def list_sweeps(self) -> list[dict[str, Any]]:
        with self._connect(read_only=True) as con:
            rows = con.execute(
                """
                SELECT sweep_id, name, created_at_iso, dataset_id,
                       dataset_version, walk_forward_split_ratio
                FROM sweeps ORDER BY created_at_iso DESC
                """
            ).fetchall()
        return [
            {
                "sweep_id": r[0],
                "name": r[1],
                "created_at_iso": r[2],
                "dataset_id": r[3],
                "dataset_version": r[4],
                "walk_forward_split_ratio": r[5],
            }
            for r in rows
        ]

    def get_sweep(self, sweep_id: str) -> dict[str, Any]:
        with self._connect(read_only=True) as con:
            header = con.execute(
                """
                SELECT sweep_id, name, created_at_iso, dataset_id,
                       dataset_version, walk_forward_split_ratio, config_json
                FROM sweeps WHERE sweep_id = ?
                """,
                [sweep_id],
            ).fetchone()
            if header is None:
                raise KeyError(f"Sweep '{sweep_id}' not found")
            rows = con.execute(
                """
                SELECT run_id, params_json, in_sample_json, out_of_sample_json, plateau_score
                FROM sweep_rows WHERE sweep_id = ? ORDER BY run_id
                """,
                [sweep_id],
            ).fetchall()
        return {
            "sweep_id": header[0],
            "name": header[1],
            "created_at_iso": header[2],
            "dataset_id": header[3],
            "dataset_version": header[4],
            "walk_forward_split_ratio": header[5],
            "config": json.loads(header[6]),
            "rows": [
                {
                    "run_id": r[0],
                    "params": json.loads(r[1]),
                    "in_sample": json.loads(r[2]),
                    "out_of_sample": json.loads(r[3]) if r[3] else None,
                    "plateau_score": r[4],
                }
                for r in rows
            ],
        }
