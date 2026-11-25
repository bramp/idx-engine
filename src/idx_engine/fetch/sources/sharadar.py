"""Sharadar direct API (`https://api.sharadar.com/v1.0`) client and dataset builder.

Builds a point-in-time, survivorship-free S&P 500 dataset with zero hardcoded ticker
renames or constituent lists:
- `sp500`    -> daily point-in-time membership (`historical` + `current` + `added`/`removed`)
- `stocks`   -> `closeunadj` (`prices.parquet`) and split-adjusted `close` (`returns.parquet`)
- `funds`    -> paired benchmark ETF (`SPY`, `VOO`, `IVV`) (`etf.parquet`)
- `actions`  -> `split` (`corporate_actions.parquet`) and `dividend` (`dividends.parquet`)
- `tickers`  -> company names and sectors (`sectors.parquet`)
- `daily`    -> `marketcap` for daily cap weights (`weights.parquet`); falls back to
                price-drifted equal weights if the subscription plan excludes `daily`.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import zipfile
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import numpy as np
import polars as pl

from idx_engine.fetch.store import DataStore
from idx_engine.models import CorporateAction, DatasetMetadata

log = logging.getLogger(__name__)

SHARADAR_API_BASE = "https://api.sharadar.com/v1.0"
SHARADAR_API_KEY_ENV = "SHARADAR_API_KEY"
_HTTP_HEADERS = {"User-Agent": "idx-engine/0.1 (research direct-indexing simulator)"}

# Sharadar's `tickers.sector` uses Morningstar-style labels; map to GICS names so
# `SECTOR_ETFS` and sector-peer wash-sale substitution match cleanly.
_SHARADAR_TO_GICS_SECTOR: dict[str, str] = {
    "Technology": "Information Technology",
    "Healthcare": "Health Care",
    "Financial Services": "Financials",
    "Consumer Cyclical": "Consumer Discretionary",
    "Consumer Defensive": "Consumer Staples",
    "Communication Services": "Communication Services",
    "Industrials": "Industrials",
    "Energy": "Energy",
    "Utilities": "Utilities",
    "Real Estate": "Real Estate",
    "Basic Materials": "Materials",
}


class SharadarAccessError(RuntimeError):
    """Raised when a Sharadar table or tier is not accessible with the supplied API key."""


def _map_sector(raw: str | None) -> str:
    if not raw:
        return "Unknown"
    cleaned = raw.strip()
    return _SHARADAR_TO_GICS_SECTOR.get(cleaned, cleaned) or "Unknown"


def _parse_csv_bytes(content: bytes) -> pl.DataFrame:
    text = content.strip()
    if not text:
        return pl.DataFrame()
    return pl.read_csv(io.BytesIO(text), try_parse_dates=False, infer_schema_length=1000)


class SharadarClient:
    """HTTP client for `https://api.sharadar.com/v1.0` with local Parquet caching."""

    def __init__(
        self,
        api_key: str,
        cache_dir: str | Path,
        base_url: str = SHARADAR_API_BASE,
        timeout_sec: float = 120.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise ValueError(
                f"Sharadar API key is required (pass --api-key or set {SHARADAR_API_KEY_ENV})."
            )
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout_sec = timeout_sec
        self._client = http_client

    def _headers(self) -> dict[str, str]:
        return {**_HTTP_HEADERS, "x-api-key": self.api_key}

    def _get(self, path: str, params: dict[str, str]) -> httpx.Response:
        url = f"{self.base_url}{path}"
        if self._client is not None:
            resp = self._client.get(
                url, params=params, headers=self._headers(), follow_redirects=True
            )
        else:
            with httpx.Client(timeout=self.timeout_sec, follow_redirects=True) as client:
                resp = client.get(url, params=params, headers=self._headers())
        if resp.status_code == 401:
            raise SharadarAccessError(
                "Sharadar returned 401 Unauthorized. Check your API key or subscribe at "
                "https://sharadar.com/subscribe."
            )
        if resp.status_code == 403:
            raise SharadarAccessError(f"Sharadar returned 403 Forbidden for {path}: {resp.text[:200]}")
        resp.raise_for_status()
        return resp

    def _manifest_path(self) -> Path:
        return self.cache_dir / "manifest.json"

    def _load_manifest(self) -> dict[str, dict[str, str]]:
        p = self._manifest_path()
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_manifest(self, manifest: dict[str, dict[str, str]]) -> None:
        p = self._manifest_path()
        tmp = p.with_name(p.name + ".part")
        tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(p)

    def fetch_bulk_table(
        self,
        table: str,
        years: str = "full",
        refresh: bool = False,
    ) -> pl.DataFrame:
        """Download (or reuse cached) bulk zip for `table` (`years` in `5|10|full`)."""
        bulk_dir = self.cache_dir / "bulk"
        bulk_dir.mkdir(parents=True, exist_ok=True)
        cache_key = f"bulk:{table}:{years}"
        pq_path = bulk_dir / f"{table}_{years}.parquet"
        manifest = self._load_manifest()

        remote_modified: str | None = None
        try:
            status_resp = self._get(f"/data/{table}", {"status": "True"})
            remote_modified = str(status_resp.json().get("modified", "")) or None
        except SharadarAccessError:
            raise
        except Exception as exc:
            log.debug("Bulk status check failed for %s: %s", table, exc)

        cached_meta = manifest.get(cache_key, {})
        if (
            not refresh
            and pq_path.exists()
            and (remote_modified is None or cached_meta.get("modified") == remote_modified)
        ):
            return pl.read_parquet(pq_path)

        resp = self._get(f"/data/{table}", {"years": years})
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not csv_names:
                raise RuntimeError(f"Sharadar bulk zip for {table} contained no CSV file")
            with zf.open(csv_names[0]) as fh:
                df = _parse_csv_bytes(fh.read())

        tmp = pq_path.with_name(pq_path.name + ".part")
        df.write_parquet(tmp)
        tmp.replace(pq_path)
        manifest[cache_key] = {
            "modified": remote_modified or "",
            "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "rows": str(df.height),
        }
        self._save_manifest(manifest)
        return df

    def query_table(
        self,
        table: str,
        *,
        tickers: Sequence[str] | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        fields: Sequence[str] | None = None,
        extra_params: dict[str, str] | None = None,
        page_size: int = 10000,
    ) -> pl.DataFrame:
        """Query a Sharadar table slice via REST pagination (`format=csv`)."""
        base_params: dict[str, str] = {"format": "csv", "limit": str(page_size)}
        if tickers:
            base_params["ticker"] = ",".join(tickers)
        if start_date is not None:
            base_params["from"] = start_date.isoformat()
        if end_date is not None:
            base_params["to"] = end_date.isoformat()
        if fields:
            base_params["fields"] = ",".join(fields)
        if extra_params:
            base_params.update(extra_params)

        frames: list[pl.DataFrame] = []
        offset = 0
        while True:
            params = {**base_params, "offset": str(offset)}
            resp = self._get(f"/data/{table}", params)
            page = _parse_csv_bytes(resp.content)
            if page.height == 0:
                break
            frames.append(page)
            if page.height < page_size:
                break
            offset += page.height

        if not frames:
            return pl.DataFrame()
        return pl.concat(frames, how="diagonal_relaxed")

    def load_table(
        self,
        table: str,
        *,
        years: str = "full",
        tickers: Sequence[str] | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        fields: Sequence[str] | None = None,
        extra_params: dict[str, str] | None = None,
        refresh: bool = False,
        prefer_bulk: bool = True,
    ) -> tuple[pl.DataFrame, str]:
        """Load a Sharadar table, preferring cached bulk zips and falling back to sliced REST.

        Returns `(dataframe, mode)` where `mode` is `"bulk"` or `"sliced"`.
        """
        if prefer_bulk:
            try:
                df = self.fetch_bulk_table(table, years=years, refresh=refresh)
                return df, "bulk"
            except SharadarAccessError:
                log.info("Bulk download unavailable for %s; falling back to sliced REST queries.", table)

        # Sliced query with local cache keyed by request parameters.
        slice_dir = self.cache_dir / "slices" / table
        slice_dir.mkdir(parents=True, exist_ok=True)
        ticker_tag = ",".join(sorted(set(tickers))) if tickers else "all"
        span_tag = f"{start_date or 'min'}_{end_date or 'max'}"
        key_payload = json.dumps(
            {
                "table": table,
                "tickers": ticker_tag,
                "span": span_tag,
                "fields": list(fields or ()),
                "extra": sorted((extra_params or {}).items()),
            },
            sort_keys=True,
        )
        safe_key = hashlib.sha256(key_payload.encode("utf-8")).hexdigest()[:16]
        pq_path = slice_dir / f"{safe_key}.parquet"
        if not refresh and pq_path.exists():
            return pl.read_parquet(pq_path), "sliced"

        frames: list[pl.DataFrame] = []
        if tickers and len(tickers) > 25:
            unique_tickers = sorted(set(tickers))
            for i in range(0, len(unique_tickers), 25):
                chunk = unique_tickers[i : i + 25]
                part = self.query_table(
                    table,
                    tickers=chunk,
                    start_date=start_date,
                    end_date=end_date,
                    fields=fields,
                    extra_params=extra_params,
                )
                if part.height:
                    frames.append(part)
            df = pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()
        else:
            df = self.query_table(
                table,
                tickers=tickers,
                start_date=start_date,
                end_date=end_date,
                fields=fields,
                extra_params=extra_params,
            )

        if df.height:
            tmp = pq_path.with_name(pq_path.name + ".part")
            df.write_parquet(tmp)
            tmp.replace(pq_path)
        return df, "sliced"


def build_daily_membership(
    sp500_df: pl.DataFrame, trading_dates: Sequence[date]
) -> dict[date, set[str]]:
    """Reconstruct exact daily S&P 500 constituent sets from Sharadar's `sp500` table.

    Uses quarterly `historical` snapshots as anchors and applies `added` / `removed`
    effective-date changes between snapshots. Falls back to `current` if no earlier
    `historical` snapshot exists.
    """
    if sp500_df.height == 0 or not trading_dates:
        return {d: set() for d in trading_dates}

    df = sp500_df.select(
        pl.col("date").cast(pl.String).str.slice(0, 10).str.to_date("%Y-%m-%d").alias("date"),
        pl.col("action").cast(pl.String).str.strip_chars().str.to_lowercase().alias("action"),
        pl.col("ticker").cast(pl.String).str.strip_chars().str.to_uppercase().alias("ticker"),
    ).drop_nulls(["date", "action", "ticker"])

    hist_snaps: dict[date, set[str]] = {}
    for (snap_date,), grp in df.filter(pl.col("action") == "historical").group_by(["date"]):
        hist_snaps[snap_date] = set(grp.get_column("ticker").to_list())

    current_rows = df.filter(pl.col("action") == "current")
    if current_rows.height:
        cur_date = current_rows.get_column("date").max()
        if isinstance(cur_date, date) and cur_date not in hist_snaps:
            hist_snaps[cur_date] = set(current_rows.get_column("ticker").to_list())

    added_by_date: dict[date, set[str]] = {}
    for (ev_date,), grp in df.filter(pl.col("action") == "added").group_by(["date"]):
        added_by_date[ev_date] = set(grp.get_column("ticker").to_list())

    removed_by_date: dict[date, set[str]] = {}
    for (ev_date,), grp in df.filter(pl.col("action") == "removed").group_by(["date"]):
        removed_by_date[ev_date] = set(grp.get_column("ticker").to_list())

    snap_dates = sorted(hist_snaps)
    if not snap_dates:
        return {d: set() for d in trading_dates}

    all_events = sorted(set(added_by_date) | set(removed_by_date))
    first_td = trading_dates[0]

    # Anchor at the latest snapshot <= first_td; if none exists, roll back from the earliest snapshot.
    prior_snaps = [s for s in snap_dates if s <= first_td]
    if prior_snaps:
        anchor_date = prior_snaps[-1]
        active = set(hist_snaps[anchor_date])
        for ev_d in all_events:
            if anchor_date < ev_d <= first_td:
                active |= added_by_date.get(ev_d, set())
                active -= removed_by_date.get(ev_d, set())
    else:
        anchor_date = snap_dates[0]
        active = set(hist_snaps[anchor_date])
        for ev_d in reversed(all_events):
            if first_td < ev_d <= anchor_date:
                active -= added_by_date.get(ev_d, set())
                active |= removed_by_date.get(ev_d, set())

    result: dict[date, set[str]] = {}
    snap_set = set(snap_dates)
    prev_day = first_td
    for idx, td in enumerate(trading_dates):
        if idx > 0:
            # Apply any snapshot resets or add/remove events strictly after prev_day up to td.
            step_dates = sorted(
                {d for d in snap_set if prev_day < d <= td}
                | {d for d in all_events if prev_day < d <= td}
            )
            for step_d in step_dates:
                if step_d in snap_set:
                    active = set(hist_snaps[step_d])
                else:
                    active |= added_by_date.get(step_d, set())
                    active -= removed_by_date.get(step_d, set())
        result[td] = set(active)
        prev_day = td
    return result


def _infer_dividends_from_adj(close: np.ndarray, closeadj: np.ndarray) -> np.ndarray:
    """Infer ex-date cash dividends from jumps in `closeadj / close` when `actions` is absent."""
    n = len(close)
    divs = np.zeros(n, dtype=np.float64)
    if n < 2:
        return divs
    valid = (close > 0) & (closeadj > 0) & ~np.isnan(close) & ~np.isnan(closeadj)
    ratio = np.where(valid, closeadj / np.maximum(close, 1e-12), np.nan)
    for i in range(1, n):
        if np.isnan(ratio[i]) or np.isnan(ratio[i - 1]) or ratio[i] <= 0:
            continue
        # On an ex-dividend date, close drops relative to closeadj so ratio[i] > ratio[i-1].
        rel_jump = 1.0 - (ratio[i - 1] / ratio[i])
        if rel_jump > 1e-4:
            divs[i] = max(0.0, float(close[i] * rel_jump))
    return divs


def _choose_bulk_years(start_date: date, end_date: date) -> str:
    span_years = (end_date - start_date).days / 365.25
    if span_years <= 4.5 and (date.today() - start_date).days <= 365 * 5 - 30:
        return "5"
    if span_years <= 9.5 and (date.today() - start_date).days <= 365 * 10 - 30:
        return "10"
    return "full"


def fetch_sharadar_dataset(
    store: DataStore,
    dataset_id: str = "sp500_sharadar",
    start_date: date = date(2019, 1, 1),
    end_date: date = date(2024, 12, 31),
    benchmark_ticker: str = "SPY",
    api_key: str | None = None,
    tickers: Sequence[str] | None = None,
    max_constituents: int | None = None,
    refresh_reference_data: bool = False,
    refresh_prices: bool = False,
    client: SharadarClient | None = None,
) -> DatasetMetadata:
    """Build a point-in-time S&P 500 dataset from the Sharadar direct API."""
    resolved_key = api_key or os.environ.get(SHARADAR_API_KEY_ENV, "")
    sharadar = client or SharadarClient(
        api_key=resolved_key,
        cache_dir=store.raw_dir / "sharadar",
    )
    bulk_years = _choose_bulk_years(start_date, end_date)
    prefer_bulk = tickers is None
    issues: list[str] = []
    explicit_tickers = [t.strip().upper() for t in tickers if t.strip()] if tickers else None

    # 1. Benchmark ETF (`funds`) — establishes the exact trading calendar for the window.
    funds_df, funds_mode = sharadar.load_table(
        "funds",
        years=bulk_years,
        tickers=[benchmark_ticker],
        start_date=start_date,
        end_date=end_date,
        fields=["ticker", "date", "close", "closeadj", "closeunadj"],
        refresh=refresh_prices,
        prefer_bulk=prefer_bulk,
    )
    if funds_df.height == 0:
        raise RuntimeError(
            f"Sharadar `funds` returned no rows for benchmark '{benchmark_ticker}' "
            f"in {start_date}..{end_date}."
        )
    funds_df = (
        funds_df.with_columns(
            pl.col("ticker").cast(pl.String).str.strip_chars().str.to_uppercase(),
            pl.col("date").cast(pl.String).str.slice(0, 10).str.to_date("%Y-%m-%d"),
            pl.col("close").cast(pl.Float64),
            pl.col("closeadj").cast(pl.Float64),
            pl.col("closeunadj").cast(pl.Float64),
        )
        .filter(
            (pl.col("ticker") == benchmark_ticker.upper())
            & pl.col("date").is_between(start_date, end_date)
        )
        .sort("date")
        .unique(subset=["date"], keep="last")
    )
    if funds_df.height == 0:
        raise RuntimeError(
            f"Benchmark '{benchmark_ticker}' has no prices in {start_date}..{end_date}."
        )
    idx_dates: list[date] = funds_df.get_column("date").to_list()
    date_pos = {d: i for i, d in enumerate(idx_dates)}
    n_days = len(idx_dates)

    # 2. Point-in-time S&P 500 membership (`sp500`).
    sp500_df, sp500_mode = sharadar.load_table(
        "sp500",
        years="full",
        tickers=explicit_tickers,
        refresh=refresh_reference_data,
        prefer_bulk=prefer_bulk,
    )
    if sp500_df.height == 0:
        raise RuntimeError("Sharadar `sp500` table returned no membership records.")

    members_by_day = build_daily_membership(sp500_df, idx_dates)
    if explicit_tickers is not None:
        allowed = set(explicit_tickers)
        members_by_day = {d: (m & allowed) for d, m in members_by_day.items()}
        issues.append(
            f"Explicit --tickers filter applied ({len(allowed)} tickers); dataset is a subset, "
            "not the full S&P 500."
        )

    universe = sorted(set().union(*members_by_day.values()))
    if not universe:
        raise RuntimeError(
            "No S&P 500 constituents found for the requested window / ticker filter."
        )

    if max_constituents is not None and max_constituents < len(universe):
        keep = set(universe[:max_constituents])
        universe = sorted(keep)
        members_by_day = {d: (m & keep) for d, m in members_by_day.items()}
        issues.append(
            f"--max-constituents={max_constituents}: truncated universe to {len(keep)} tickers."
        )

    # 3. Stock prices (`stocks`).
    stocks_df, stocks_mode = sharadar.load_table(
        "stocks",
        years=bulk_years,
        tickers=universe,
        start_date=start_date,
        end_date=end_date,
        fields=["ticker", "date", "close", "closeunadj"],
        refresh=refresh_prices,
        prefer_bulk=prefer_bulk,
    )
    if stocks_df.height == 0:
        raise RuntimeError("Sharadar `stocks` returned no price rows for the requested universe.")

    universe_set = set(universe)
    stocks_df = (
        stocks_df.with_columns(
            pl.col("ticker").cast(pl.String).str.strip_chars().str.to_uppercase(),
            pl.col("date").cast(pl.String).str.slice(0, 10).str.to_date("%Y-%m-%d"),
            pl.col("close").cast(pl.Float64),
            pl.col("closeunadj").cast(pl.Float64),
        )
        .filter(
            pl.col("ticker").is_in(list(universe_set))
            & pl.col("date").is_in(idx_dates)
            & pl.col("close").is_not_null()
        )
    )

    present_tickers = set(stocks_df.get_column("ticker").unique().to_list())
    no_data = [t for t in universe if t not in present_tickers]
    valid_tickers = [t for t in universe if t in present_tickers]
    if not valid_tickers:
        raise RuntimeError("No constituent tickers had valid price data from Sharadar.")

    n_tok = len(valid_tickers)
    tok_pos = {t: j for j, t in enumerate(valid_tickers)}

    adj_close_mat = np.full((n_days, n_tok), np.nan, dtype=np.float64)
    unadj_close_mat = np.full((n_days, n_tok), np.nan, dtype=np.float64)
    for t, d, c_adj, c_unadj in stocks_df.select(
        ["ticker", "date", "close", "closeunadj"]
    ).iter_rows():
        i = date_pos.get(d)
        j = tok_pos.get(t)
        if i is None or j is None:
            continue
        if c_adj is not None:
            adj_close_mat[i, j] = float(c_adj)
        if c_unadj is not None:
            unadj_close_mat[i, j] = float(c_unadj)
        elif c_adj is not None:
            unadj_close_mat[i, j] = float(c_adj)

    has_bar = ~np.isnan(adj_close_mat)
    first_bar = np.argmax(has_bar, axis=0)
    last_bar = n_days - 1 - np.argmax(has_bar[::-1, :], axis=0)
    day_idx = np.arange(n_days)[:, None]
    covered = (day_idx >= first_bar[None, :]) & (day_idx <= last_bar[None, :])

    member_mask = np.zeros((n_days, n_tok), dtype=bool)
    for i, d in enumerate(idx_dates):
        for t in members_by_day[d]:
            j = tok_pos.get(t)
            if j is not None:
                member_mask[i, j] = True

    missing_fraction = float((member_mask & ~has_bar).sum() / max(1, member_mask.sum()))

    # Forward-fill then back-fill prices outside active spans so Parquet tables have no NaNs.
    for mat in (adj_close_mat, unadj_close_mat):
        for j in range(n_tok):
            col = mat[:, j]
            for i in range(1, n_days):
                if np.isnan(col[i]) and not np.isnan(col[i - 1]):
                    col[i] = col[i - 1]
            for i in range(n_days - 2, -1, -1):
                if np.isnan(col[i]) and not np.isnan(col[i + 1]):
                    col[i] = col[i + 1]
            mat[:, j] = col

    ret_mat = np.zeros_like(adj_close_mat)
    ret_mat[1:, :] = (adj_close_mat[1:, :] / np.maximum(adj_close_mat[:-1, :], 1e-8)) - 1.0

    # 4. Corporate actions & dividends (`actions`).
    actions_df = pl.DataFrame()
    try:
        actions_df, _ = sharadar.load_table(
            "actions",
            years="full",
            tickers=valid_tickers,
            start_date=start_date,
            end_date=end_date,
            refresh=refresh_reference_data,
            prefer_bulk=prefer_bulk,
        )
    except SharadarAccessError as exc:
        issues.append(f"Sharadar `actions` unavailable ({exc}); dividends/splits omitted.")

    raw_div_mat = np.zeros((n_days, n_tok), dtype=np.float64)
    corporate_actions: list[CorporateAction] = []
    if actions_df.height:
        act = (
            actions_df.select(
                pl.col("date").cast(pl.String).str.slice(0, 10).str.to_date("%Y-%m-%d").alias("date"),
                pl.col("action").cast(pl.String).str.strip_chars().str.to_lowercase().alias("action"),
                pl.col("ticker").cast(pl.String).str.strip_chars().str.to_uppercase().alias("ticker"),
                pl.col("value").cast(pl.Float64, strict=False).alias("value"),
            )
            .filter(
                pl.col("ticker").is_in(valid_tickers)
                & pl.col("date").is_between(start_date, end_date)
            )
        )
        for d, action_kind, t, val in act.iter_rows():
            i = date_pos.get(d)
            j = tok_pos.get(t)
            if i is None or j is None or val is None or val <= 0:
                continue
            if action_kind == "dividend":
                # Sharadar `actions` dividends are split-adjusted; scale by closeunadj / close
                # to recover the as-traded cash dividend per share on `d`.
                split_scale = (
                    unadj_close_mat[i, j] / adj_close_mat[i, j]
                    if adj_close_mat[i, j] > 1e-8
                    else 1.0
                )
                raw_div_mat[i, j] += float(val) * float(split_scale)
            elif action_kind == "split" and abs(float(val) - 1.0) > 1e-6:
                corporate_actions.append(
                    CorporateAction(
                        action_id=f"sharadar_split_{t}_{d.isoformat()}",
                        effective_date=d,
                        action_type="split",
                        ticker=t,
                        ratio_numerator=Decimal(str(round(float(val), 6))),
                        ratio_denominator=Decimal("1"),
                    )
                )
    corporate_actions.sort(key=lambda ca: (ca.effective_date, ca.ticker))

    # 5. Daily market-cap weights (`daily`), falling back to price-drifted equal weights.
    weights_source = "sharadar_daily_marketcap"
    mcap_mat = np.full((n_days, n_tok), np.nan, dtype=np.float64)
    try:
        daily_df, _ = sharadar.load_table(
            "daily",
            years=bulk_years,
            tickers=valid_tickers,
            start_date=start_date,
            end_date=end_date,
            fields=["ticker", "date", "marketcap"],
            refresh=refresh_prices,
            prefer_bulk=prefer_bulk,
        )
        if daily_df.height:
            daily_clean = (
                daily_df.select(
                    pl.col("ticker").cast(pl.String).str.strip_chars().str.to_uppercase(),
                    pl.col("date").cast(pl.String).str.slice(0, 10).str.to_date("%Y-%m-%d"),
                    pl.col("marketcap").cast(pl.Float64, strict=False),
                )
                .filter(
                    pl.col("ticker").is_in(valid_tickers)
                    & pl.col("date").is_between(start_date, end_date)
                )
            )
            for t, d, mcap in daily_clean.iter_rows():
                i = date_pos.get(d)
                j = tok_pos.get(t)
                if i is not None and j is not None and mcap is not None and mcap > 0:
                    mcap_mat[i, j] = float(mcap)
    except SharadarAccessError:
        weights_source = "equal_weight_price_drifted"
        issues.append(
            "Sharadar `daily` table (marketcap) is not included in this API plan; "
            "falling back to price-drifted equal weights. Upgrade to Sharadar Bundle for "
            "daily market-cap weights."
        )

    if weights_source == "sharadar_daily_marketcap" and np.any(~np.isnan(mcap_mat)):
        # Forward-fill then back-fill occasional missing marketcap days per ticker.
        for j in range(n_tok):
            col = mcap_mat[:, j]
            for i in range(1, n_days):
                if np.isnan(col[i]) and not np.isnan(col[i - 1]):
                    col[i] = col[i - 1]
            for i in range(n_days - 2, -1, -1):
                if np.isnan(col[i]) and not np.isnan(col[i + 1]):
                    col[i] = col[i + 1]
            mcap_mat[:, j] = col
        # If a ticker has no marketcap at all, assign the cross-sectional median on each day.
        day_medians = np.nanmedian(mcap_mat, axis=1, keepdims=True)
        day_medians = np.where(np.isnan(day_medians), 1.0, day_medians)
        weights_mat = np.where(np.isnan(mcap_mat), day_medians, mcap_mat)
    else:
        weights_source = "equal_weight_price_drifted"
        p0 = np.maximum(adj_close_mat[0, :], 1e-8)
        weights_mat = adj_close_mat / p0[None, :]

    weights_mat = weights_mat * (member_mask & covered)
    row_sums = weights_mat.sum(axis=1, keepdims=True)
    weights_mat = np.where(row_sums > 0, weights_mat / np.maximum(row_sums, 1e-12), 0.0)

    # 6. Benchmark ETF (`etf.parquet`).
    # `bench_ledger` in `engine.py` does not apply corporate actions to the benchmark ETF,
    # so `etf.parquet` stores split-adjusted close and split-adjusted cash dividends.
    etf_close_adj = funds_df.get_column("close").to_numpy().astype(np.float64)
    etf_close_tot = (
        funds_df.get_column("closeadj")
        .fill_null(funds_df.get_column("close"))
        .to_numpy()
        .astype(np.float64)
    )
    etf_ret = np.zeros(n_days, dtype=np.float64)
    etf_ret[1:] = (etf_close_adj[1:] / np.maximum(etf_close_adj[:-1], 1e-8)) - 1.0

    etf_div = np.zeros(n_days, dtype=np.float64)
    etf_div_from_actions = False
    try:
        etf_act_df, _ = sharadar.load_table(
            "actions",
            years="full",
            tickers=[benchmark_ticker.upper()],
            start_date=start_date,
            end_date=end_date,
            refresh=refresh_reference_data,
            prefer_bulk=prefer_bulk,
        )
        if etf_act_df.height:
            etf_act = (
                etf_act_df.select(
                    pl.col("date").cast(pl.String).str.slice(0, 10).str.to_date("%Y-%m-%d"),
                    pl.col("action").cast(pl.String).str.strip_chars().str.to_lowercase(),
                    pl.col("ticker").cast(pl.String).str.strip_chars().str.to_uppercase(),
                    pl.col("value").cast(pl.Float64, strict=False),
                )
                .filter(
                    (pl.col("ticker") == benchmark_ticker.upper())
                    & (pl.col("action") == "dividend")
                    & pl.col("date").is_between(start_date, end_date)
                )
            )
            for d, _a, _t, val in etf_act.iter_rows():
                i = date_pos.get(d)
                if i is not None and val is not None and val > 0:
                    etf_div[i] += float(val)
                    etf_div_from_actions = True
    except SharadarAccessError:
        pass

    if not etf_div_from_actions:
        etf_div = _infer_dividends_from_adj(etf_close_adj, etf_close_tot)

    # 7. Security master (`tickers`) for names and sectors.
    sectors: dict[str, str] = {}
    names: dict[str, str] = {}
    unknown_sector: list[str] = []
    try:
        tickers_df, _ = sharadar.load_table(
            "tickers",
            years="full",
            tickers=valid_tickers,
            extra_params={"table": "stocks"},
            refresh=refresh_reference_data,
            prefer_bulk=prefer_bulk,
        )
        if tickers_df.height:
            t_filtered = tickers_df
            if "table" in t_filtered.columns:
                t_filtered = t_filtered.filter(
                    pl.col("table").cast(pl.String).str.strip_chars().str.to_lowercase() == "stocks"
                )
            t_clean = (
                t_filtered.select(
                    pl.col("ticker").cast(pl.String).str.strip_chars().str.to_uppercase(),
                    pl.col("name").cast(pl.String),
                    pl.col("sector").cast(pl.String),
                )
                .filter(pl.col("ticker").is_in(valid_tickers))
                .unique(subset=["ticker"], keep="first")
            )
            for t, nm, sec in t_clean.iter_rows():
                names[t] = nm or t
                sectors[t] = _map_sector(sec)
    except SharadarAccessError as exc:
        issues.append(f"Sharadar `tickers` metadata unavailable ({exc}); sectors marked Unknown.")

    for t in valid_tickers:
        names.setdefault(t, t)
        if t not in sectors or sectors[t] == "Unknown":
            sectors[t] = "Unknown"
            unknown_sector.append(t)

    # 8. Build `errors.txt` data-quality report.
    partial: list[str] = []
    for t in valid_tickers:
        j = tok_pos[t]
        mi = [i for i, d in enumerate(idx_dates) if t in members_by_day[d]]
        if not mi:
            continue
        gaps = int((~has_bar[mi, j]).sum())
        if gaps > 0:
            partial.append(
                f"  {t:<8} member {idx_dates[mi[0]]}..{idx_dates[mi[-1]]}  "
                f"sharadar bars {idx_dates[first_bar[j]]}..{idx_dates[last_bar[j]]}  "
                f"({gaps} member-days without a bar)"
            )

    lines = [
        f"Data-quality report for {dataset_id}",
        f"Generated {datetime.now(UTC).isoformat(timespec='seconds')}",
        f"Window {idx_dates[0]}..{idx_dates[-1]} ({n_days} trading days), benchmark {benchmark_ticker}",
        f"Sources: Sharadar direct API ({SHARADAR_API_BASE}); "
        f"sp500={sp500_mode}, stocks={stocks_mode}, funds={funds_mode}, weights={weights_source}",
        "",
        "SUMMARY",
        f"  Index members during window ........ {len(universe)}",
        f"  With full price coverage ........... {len(valid_tickers) - len(partial)}",
        f"  With partial price coverage ........ {len(partial)}",
        f"  With NO price data (dropped) ....... {len(no_data)}",
        f"  Member-days missing a price bar .... {missing_fraction * 100:.2f}%",
        f"  Corporate split actions recorded ... {len(corporate_actions)}",
    ]
    if issues:
        lines += ["", "WARNINGS", *[f"  {m}" for m in issues]]
    if no_data:
        lines += ["", f"NO PRICE DATA ({len(no_data)})", *[f"  {t}" for t in no_data]]
    if partial:
        lines += ["", f"PARTIAL PRICE COVERAGE ({len(partial)})", *partial]
    if unknown_sector:
        lines += ["", f"UNKNOWN SECTOR ({len(unknown_sector)})", *[f"  {t}" for t in unknown_sector]]
    lines.append("")
    report = "\n".join(lines)

    prices_df = pl.DataFrame(
        {"date": idx_dates, **{valid_tickers[j]: unadj_close_mat[:, j] for j in range(n_tok)}}
    ).with_columns(pl.col("date").cast(pl.Date))
    returns_df = pl.DataFrame(
        {"date": idx_dates, **{valid_tickers[j]: ret_mat[:, j] for j in range(n_tok)}}
    ).with_columns(pl.col("date").cast(pl.Date))
    dividends_df = pl.DataFrame(
        {"date": idx_dates, **{valid_tickers[j]: raw_div_mat[:, j] for j in range(n_tok)}}
    ).with_columns(pl.col("date").cast(pl.Date))
    weights_df = pl.DataFrame(
        {"date": idx_dates, **{valid_tickers[j]: weights_mat[:, j] for j in range(n_tok)}}
    ).with_columns(pl.col("date").cast(pl.Date))
    etf_df = pl.DataFrame(
        {"date": idx_dates, "close": etf_close_adj, "return": etf_ret, "dividend": etf_div}
    ).with_columns(pl.col("date").cast(pl.Date))

    survivorship_biased = bool(
        no_data
        or partial
        or explicit_tickers is not None
        or max_constituents is not None
        or sp500_mode != "bulk"
    )
    source_label = f"sharadar+{weights_source}"
    return store.save_dataset(
        dataset_id=dataset_id,
        source=source_label,
        prices_df=prices_df,
        returns_df=returns_df,
        dividends_df=dividends_df,
        weights_df=weights_df,
        etf_df=etf_df,
        sectors=sectors,
        names=names,
        corporate_actions=tuple(corporate_actions),
        benchmark_ticker=benchmark_ticker.upper(),
        survivorship_biased=survivorship_biased,
        missing_bar_fraction=missing_fraction,
        notes=(
            f"Sharadar PIT S&P 500 ({weights_source}); {len(valid_tickers)}/{len(universe)} "
            f"members covered, missing_bars={missing_fraction * 100:.2f}%. See errors.txt."
        ),
        extra_files={"errors.txt": report},
    )
