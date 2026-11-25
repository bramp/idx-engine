"""Resumable, incremental on-disk cache of daily yfinance bars (`data/raw/yfinance/`).

- `<SYMBOL>.parquet`: `date, close, dividend, split` as returned by yfinance with
  `auto_adjust=False` (close and dividends are split-adjusted, not dividend-adjusted).
- `manifest.json`: per symbol, the contiguous date range already requested from Yahoo
  (`start`..`end`), the cached row count and when it was fetched. `rows == 0` means Yahoo
  had no data for that range (typically a delisted company).

Each download chunk is persisted immediately, so an interrupted pull resumes where it
stopped, and later pulls only request dates outside the cached range. Yahoo re-bases the
split-adjusted history after every new split, so each extension re-downloads a few
overlapping days; if those disagree with the cache, the symbol's full history is refetched.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl
import yfinance as yf

log = logging.getLogger(__name__)

OVERLAP_DAYS = 10
REBASE_TOLERANCE = 1e-3
_SCHEMA = {"date": pl.Date, "close": pl.Float64, "dividend": pl.Float64, "split": pl.Float64}


@dataclass
class PriceCacheStats:
    from_cache: int = 0
    fetched: int = 0
    rebased: int = 0
    not_cached: int = 0


def _extract_bars(df: pd.DataFrame, sym: str) -> pl.DataFrame | None:
    if sym not in df.columns.get_level_values(0):
        return None
    sub = df[sym]
    if "Close" not in sub.columns:
        return None
    n = len(sub)
    close = sub["Close"].to_numpy(dtype=np.float64)
    div = (
        sub["Dividends"].fillna(0.0).to_numpy(dtype=np.float64)
        if "Dividends" in sub.columns
        else np.zeros(n)
    )
    split = (
        sub["Stock Splits"].fillna(0.0).to_numpy(dtype=np.float64)
        if "Stock Splits" in sub.columns
        else np.zeros(n)
    )
    if np.all(np.isnan(close)):
        return None
    keep = ~np.isnan(close) | (div != 0.0) | (split != 0.0)
    return pl.DataFrame(
        {
            "date": [d.date() for d in sub.index[keep]],
            "close": close[keep],
            "dividend": div[keep],
            "split": split[keep],
        },
        schema=_SCHEMA,
    ).with_columns(pl.col("close").fill_nan(None))


class YahooPriceCache:
    def __init__(
        self,
        cache_dir: str | Path,
        chunk_size: int = 100,
        retry_chunk_size: int = 25,
        max_attempts: int = 4,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.chunk_size = chunk_size
        self.retry_chunk_size = retry_chunk_size
        self.max_attempts = max_attempts
        self._sleep = sleep or time.sleep
        self._manifest_path = self.dir / "manifest.json"
        self.manifest = self._load_manifest()
        self._frames: dict[str, pl.DataFrame | None] = {}
        self._any_ok = False
        self._pending_empty: list[tuple[str, date, date]] = []

    # ---- persistence ----
    def _path(self, sym: str) -> Path:
        return self.dir / f"{sym}.parquet"

    def _load_manifest(self) -> dict[str, dict[str, object]]:
        try:
            data = json.loads(self._manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            log.warning(
                "Ignoring unreadable %s (%s); cached ranges will be re-fetched.",
                self._manifest_path,
                exc,
            )
            return {}
        # Drop entries whose bar file has gone missing so they are fetched again.
        return {s: e for s, e in data.items() if e.get("rows", 0) == 0 or self._path(s).exists()}

    def _save_manifest(self) -> None:
        tmp = self._manifest_path.with_name(self._manifest_path.name + ".part")
        tmp.write_text(json.dumps(self.manifest, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(self._manifest_path)

    def read(self, sym: str) -> pl.DataFrame | None:
        if sym in self._frames:
            return self._frames[sym]
        path = self._path(sym)
        if not path.exists():
            self._frames[sym] = None
            entry = self.manifest.get(sym)
            if entry is not None and entry.get("rows", 0) != 0:
                self.manifest.pop(sym)
                self._save_manifest()
            return None
        try:
            frame = pl.read_parquet(path)
            self._frames[sym] = frame
            return frame
        except Exception as exc:
            log.warning("Ignoring unreadable %s (%s).", path, exc)
            self._frames[sym] = None
            if sym in self.manifest:
                self.manifest.pop(sym)
                self._save_manifest()
            return None

    def _write(self, sym: str, df: pl.DataFrame) -> None:
        tmp = self._path(sym).with_name(f"{sym}.parquet.part")
        df.write_parquet(tmp)
        tmp.replace(self._path(sym))
        self._frames[sym] = df

    def coverage(self, sym: str) -> tuple[date, date, int] | None:
        e = self.manifest.get(sym)
        if e is None:
            return None
        try:
            return (
                date.fromisoformat(str(e["start"])),
                date.fromisoformat(str(e["end"])),
                int(str(e["rows"])),
            )
        except (KeyError, TypeError, ValueError):
            self.manifest.pop(sym, None)
            return None

    # ---- fetching ----
    def ensure(
        self,
        symbols: Sequence[str],
        start: date,
        end: date,
        refresh: bool = False,
        retry_missing: bool = False,
    ) -> PriceCacheStats:
        """Make sure every symbol's bars for `start..end` are cached, downloading only gaps."""
        if start > end:
            raise ValueError(f"Price-cache start date {start} is after end date {end}.")
        stats = PriceCacheStats()
        # Today's bar can still change, so coverage never extends past yesterday.
        complete_end = min(end, date.today() - timedelta(days=1))
        plan: dict[tuple[date, date], list[str]] = {}
        for sym in dict.fromkeys(symbols):
            cached = self.coverage(sym)
            if cached is not None and cached[2] > 0:
                bars = self.read(sym)
                if bars is not None and bars.height != cached[2]:
                    log.warning(
                        "Invalidating %s: manifest has %d rows, Parquet has %d.",
                        sym,
                        cached[2],
                        bars.height,
                    )
                    self.manifest.pop(sym, None)
                    self._frames[sym] = None
                    self._save_manifest()
            segs = self._missing_segments(sym, start, end, refresh, retry_missing)
            if not segs:
                stats.from_cache += 1
            for seg in segs:
                plan.setdefault(seg, []).append(sym)
        stats.fetched = len({s for syms in plan.values() for s in syms})

        self._any_ok = False
        self._pending_empty = []
        rebase: set[str] = set()
        for (s, e), syms in sorted(plan.items()):
            # Refresh means force a request for this range, not discard cached
            # bars outside it. Overlapping values are replaced when merged.
            self._download(syms, s, e, complete_end, rebase, replace=False)

        if rebase:
            stats.rebased = len(rebase)
            groups: dict[tuple[date, date], list[str]] = {}
            for sym in sorted(rebase):
                cov = self.coverage(sym)
                s = min(start, cov[0]) if cov else start
                e = max(end, cov[1]) if cov else end
                groups.setdefault((s, e), []).append(sym)
            for (s, e), syms in sorted(groups.items()):
                self._download(syms, s, e, min(e, complete_end), set(), replace=True)

        if self._pending_empty and self._any_ok:
            for sym, s, e in self._pending_empty:
                self._store(sym, None, s, e, complete_end, set(), replace=False)
            self._save_manifest()
        elif self._pending_empty:
            stats.not_cached = len({p[0] for p in self._pending_empty})
            log.warning(
                "Yahoo returned no data at all for %d symbols; not caching them as empty "
                "(offline or throttled?).",
                stats.not_cached,
            )
        return stats

    def _missing_segments(
        self, sym: str, start: date, end: date, refresh: bool, retry_missing: bool
    ) -> list[tuple[date, date]]:
        cov = None if refresh else self.coverage(sym)
        if cov is None:
            return [(start, end)]
        c0, c1, rows = cov
        if rows == 0 and retry_missing:
            return [(min(start, c0), max(end, c1))]
        overlap_days = min(OVERLAP_DAYS, max(1, (c1 - c0).days // 4))
        overlap = timedelta(days=overlap_days)
        segs: list[tuple[date, date]] = []
        # Segments stay contiguous with the cached range so coverage is a single span.
        if start < c0:
            segs.append((start, min(c1, c0 + overlap)))
        if end > c1:
            segs.append((max(c0, c1 - overlap), end))
        return segs

    def _download(
        self,
        syms: Sequence[str],
        start: date,
        end: date,
        complete_end: date,
        rebase: set[str],
        replace: bool,
    ) -> None:
        # Yahoo throttles and silently drops symbols in large batches, so download in
        # chunks and retry the empty ones a few times with increasing backoff.
        missing = list(syms)
        for attempt in range(self.max_attempts):
            if not missing:
                break
            size = self.chunk_size if attempt == 0 else self.retry_chunk_size
            still_missing: list[str] = []
            for i in range(0, len(missing), size):
                chunk = missing[i : i + size]
                if attempt or i:
                    self._sleep(2.0 * attempt + 0.5)
                try:
                    df = yf.download(
                        tickers=list(chunk),
                        start=start.isoformat(),
                        end=(end + timedelta(days=1)).isoformat(),
                        auto_adjust=False,
                        actions=True,
                        progress=False,
                        group_by="ticker",
                        threads=True,
                    )
                except Exception as exc:
                    log.warning("yfinance download failed for %d symbols: %s", len(chunk), exc)
                    df = None
                found: dict[str, pl.DataFrame] = {}
                if df is not None and not df.empty:
                    for sym in chunk:
                        bars = _extract_bars(df, sym)
                        if bars is not None:
                            found[sym] = bars
                if found:
                    self._any_ok = True
                    for sym, bars in found.items():
                        self._store(sym, bars, start, end, complete_end, rebase, replace)
                    self._save_manifest()
                still_missing.extend(s for s in chunk if s not in found)
            missing = still_missing
        self._pending_empty.extend((s, start, end) for s in missing)

    def _store(
        self,
        sym: str,
        bars: pl.DataFrame | None,
        start: date,
        end: date,
        complete_end: date,
        rebase: set[str],
        replace: bool,
    ) -> None:
        old = None if replace else self.read(sym)
        entry = None if replace else self.coverage(sym)
        if bars is None:
            if old is not None:
                # A failed extension must not advance coverage and hide the gap from
                # the next pull. Keep the existing bars/manifest intact.
                return
            bars = pl.DataFrame(schema=_SCHEMA)
        if old is not None and old.height and bars.height:
            ov = old.join(bars, on="date", suffix="_new").drop_nulls(["close", "close_new"])
            if ov.height:
                drift = ((ov["close_new"] / ov["close"]) - 1.0).abs().max()
                if drift is not None and float(drift) > REBASE_TOLERANCE:  # type: ignore[arg-type]
                    rebase.add(sym)
                    return
        merged = bars
        if old is not None:
            merged = (
                pl.concat(
                    [old.filter(~pl.col("date").is_in(bars.get_column("date").implode())), bars],
                    how="vertical",
                )
                .unique(subset=["date"], keep="last")
                .sort("date")
            )
        if merged.height:
            self._write(sym, merged)
        cov_end = min(end, complete_end)
        c0 = min(start, entry[0]) if entry else start
        c1 = max(cov_end, entry[1]) if entry else cov_end
        if c1 < c0:
            return
        self.manifest[sym] = {
            "start": c0.isoformat(),
            "end": c1.isoformat(),
            "rows": merged.height,
            "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }

    # ---- reading ----
    def load(
        self, symbols: Sequence[str], start: date, end: date
    ) -> tuple[list[date], dict[str, np.ndarray], dict[str, np.ndarray], dict[str, np.ndarray]]:
        """Return `(dates, closes, dividends, splits)` aligned on the union of trading dates.

        Symbols with no close in the window are omitted; closes are NaN where a symbol has no bar.
        """
        frames: dict[str, pl.DataFrame] = {}
        for sym in dict.fromkeys(symbols):
            df = self.read(sym)
            if df is None:
                continue
            df = df.filter(pl.col("date").is_between(start, end))
            if df.height and df["close"].is_not_null().any():
                frames[sym] = df
        if not frames:
            raise RuntimeError("yfinance returned no price data for the requested window")

        idx_dates = sorted(set().union(*(f["date"].to_list() for f in frames.values())))
        pos = {d: i for i, d in enumerate(idx_dates)}
        n = len(idx_dates)
        closes: dict[str, np.ndarray] = {}
        divs: dict[str, np.ndarray] = {}
        splits: dict[str, np.ndarray] = {}
        for sym, f in frames.items():
            rows = np.fromiter(
                (pos[d] for d in f["date"].to_list()), dtype=np.int64, count=f.height
            )
            c = np.full(n, np.nan)
            c[rows] = f["close"].fill_null(np.nan).to_numpy()
            dv = np.zeros(n)
            dv[rows] = f["dividend"].fill_null(0.0).to_numpy()
            sp = np.zeros(n)
            sp[rows] = f["split"].fill_null(0.0).to_numpy()
            closes[sym], divs[sym], splits[sym] = c, dv, sp
        return idx_dates, closes, divs, splits
