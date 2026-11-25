"""Public point-in-time S&P 500 dataset builder (`fja05680` + Kaggle SPY weights + `yfinance`)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

import numpy as np
import polars as pl

from idx_engine.fetch.index_weights import SECTOR_ETFS
from idx_engine.fetch.sources.fja05680 import (
    load_sp500_current_info,
    load_sp500_membership,
    resolve_ticker_rename,
)
from idx_engine.fetch.sources.kaggle_spy import load_spy_annual_weights
from idx_engine.fetch.sources.price_cache import YahooPriceCache
from idx_engine.fetch.store import DataStore
from idx_engine.models import CorporateAction, DatasetMetadata


def _fmt_spans(days: Sequence[date]) -> str:
    """Collapse a sorted list of trading dates into `first..last` (single span)."""
    return f"{days[0]}..{days[-1]}" if days else "-"


def fetch_yfinance_dataset(
    store: DataStore,
    dataset_id: str = "sp500_public",
    start_date: date = date(2019, 1, 1),
    end_date: date | None = None,
    max_constituents: int | None = None,
    benchmark_ticker: str = "VOO",
    use_spy_weights: bool = True,
    refresh_reference_data: bool = False,
    refresh_prices: bool = False,
    retry_missing_prices: bool = False,
) -> DatasetMetadata:
    """Build a point-in-time S&P 500 dataset from fja05680 membership + yfinance prices.

    - Membership: fja05680/sp500 daily constituents (a ticker's weight is 0 on dates it
      is not an index member).
    - Weights: annual SPY holdings snapshots (Sept 30) drifted daily with each stock's
      split-adjusted price, renormalized over members that have price data. Falls back
      to equal weights at the first date if the SPY snapshots are unavailable.
    - Prices/dividends/splits: yfinance. Members yfinance has no data for are dropped.
      Bars are cached per ticker in `data/raw/yfinance/` (see `YahooPriceCache`), so an
      interrupted pull resumes and a later pull only downloads dates it has not seen.

    Every data gap is written to `errors.txt` in the dataset directory.
    """
    end_date = end_date or date.today()
    ref_dir = store.raw_dir
    issues: list[str] = []

    membership = load_sp500_membership(ref_dir / "fja05680", refresh=refresh_reference_data)
    renamed = sorted(
        {(t, resolve_ticker_rename(t)) for t in membership.get_column("ticker").unique().to_list()}
        - {(t, t) for t in membership.get_column("ticker").unique().to_list()}
    )
    rename_map = dict(renamed)
    membership = membership.with_columns(pl.col("ticker").replace(rename_map)).unique()
    try:
        current_info = load_sp500_current_info(ref_dir / "fja05680", refresh=refresh_reference_data)
    except Exception as exc:  # sector metadata is optional
        current_info = {}
        issues.append(f"Could not load fja05680 sp500.csv sectors ({exc}); sectors marked Unknown.")

    spy_weights: pl.DataFrame | None = None
    if use_spy_weights:
        try:
            spy_weights = (
                load_spy_annual_weights(ref_dir / "kaggle_spy", refresh=refresh_reference_data)
                .with_columns(pl.col("ticker").replace(rename_map))
                .group_by(["as_of", "ticker"])
                .agg(pl.col("weight").max())
            )
        except Exception as exc:
            issues.append(
                f"Could not load annual SPY weights ({exc}); falling back to equal weights "
                "at the first date drifted by price."
            )

    snap_dates: list[date] = sorted(membership.get_column("date").unique().to_list())
    first_eff = max((d for d in snap_dates if d <= start_date), default=snap_dates[0])
    window_snaps = [d for d in snap_dates if first_eff <= d <= end_date]
    members_by_snap: dict[date, set[str]] = {
        d: set(g.get_column("ticker").to_list())
        for (d,), g in membership.filter(pl.col("date").is_in(window_snaps)).group_by(["date"])
    }
    universe = sorted(set().union(*members_by_snap.values()))

    spy_snaps: list[date] = []
    spy_by_snap: dict[date, dict[str, float]] = {}
    if spy_weights is not None:
        spy_snaps = sorted(spy_weights.get_column("as_of").unique().to_list())
        spy_by_snap = {
            d: dict(g.select(["ticker", "weight"]).iter_rows())
            for (d,), g in spy_weights.group_by(["as_of"])
        }

    def _anchor_snap(d: date) -> date | None:
        if not spy_snaps:
            return None
        return max((s for s in spy_snaps if s <= d), default=spy_snaps[0])

    if max_constituents is not None and max_constituents < len(members_by_snap[first_eff]):
        first_members = members_by_snap[first_eff]
        anchor = _anchor_snap(start_date)
        w0 = spy_by_snap.get(anchor, {}) if anchor else {}
        keep = set(sorted(first_members, key=lambda t: (-w0.get(t, 0.0), t))[:max_constituents])
        universe = sorted(keep)
        issues.append(
            f"--max-constituents={max_constituents}: kept only the {len(keep)} largest members "
            f"on {first_eff}. Companies added to the index later are EXCLUDED, so this "
            "dataset is survivorship-limited."
        )

    all_symbols = sorted({*universe, benchmark_ticker, *SECTOR_ETFS.values()})
    price_cache = YahooPriceCache(ref_dir / "yfinance")
    cache_stats = price_cache.ensure(
        all_symbols,
        start_date,
        end_date,
        refresh=refresh_prices,
        retry_missing=retry_missing_prices,
    )
    if cache_stats.not_cached:
        issues.append(
            f"Yahoo returned nothing for {cache_stats.not_cached} symbols in this run (offline or "
            "throttled?); they were not cached and will be retried on the next pull."
        )
    idx_dates, close_cols, div_cols, split_cols = price_cache.load(
        all_symbols, start_date, end_date
    )
    if benchmark_ticker not in close_cols:
        raise RuntimeError(f"Benchmark ticker '{benchmark_ticker}' missing from yfinance download")

    etf_c = close_cols[benchmark_ticker].copy()
    bench_valid = np.flatnonzero(~np.isnan(etf_c))
    if bench_valid[0] > 5:
        raise RuntimeError(
            f"Benchmark '{benchmark_ticker}' has no prices until {idx_dates[bench_valid[0]]}; "
            "use --start-date on/after that date or a longer-lived benchmark such as SPY "
            "(VOO launched 2010-09-09)."
        )

    # Snapshot in force on each trading date; drop tickers never a member on any of them.
    snap_for_day = [
        max((s for s in window_snaps if s <= d), default=window_snaps[0]) for d in idx_dates
    ]
    member_days = {
        t: [idx_dates[i] for i, s in enumerate(snap_for_day) if t in members_by_snap[s]]
        for t in universe
    }
    universe = [t for t in universe if member_days[t]]

    no_data = [t for t in universe if t not in close_cols]
    valid_tickers = [t for t in universe if t in close_cols]
    if not valid_tickers:
        raise RuntimeError("No constituent tickers had valid price data from yfinance")

    n_days = len(idx_dates)
    n_tok = len(valid_tickers)
    close_mat = np.column_stack([close_cols[t] for t in valid_tickers])
    has_bar = ~np.isnan(close_mat)
    first_bar = np.argmax(has_bar, axis=0)
    last_bar = n_days - 1 - np.argmax(has_bar[::-1, :], axis=0)
    day_idx = np.arange(n_days)[:, None]
    covered = (day_idx >= first_bar[None, :]) & (day_idx <= last_bar[None, :])

    tok_pos = {t: j for j, t in enumerate(valid_tickers)}
    snap_rows: dict[date, np.ndarray] = {}
    for s, members in members_by_snap.items():
        row = np.zeros(n_tok, dtype=bool)
        for t in members:
            j = tok_pos.get(t)
            if j is not None:
                row[j] = True
        snap_rows[s] = row
    member_mask = np.vstack([snap_rows[s] for s in snap_for_day])
    missing_fraction = float((member_mask & ~has_bar).sum() / max(1, member_mask.sum()))

    for j in range(n_tok):
        col = close_mat[:, j]
        for i in range(1, n_days):
            if np.isnan(col[i]) and not np.isnan(col[i - 1]):
                col[i] = col[i - 1]
        for i in range(n_days - 2, -1, -1):
            if np.isnan(col[i]) and not np.isnan(col[i + 1]):
                col[i] = col[i + 1]
        close_mat[:, j] = col

    ret_mat = np.zeros_like(close_mat)
    ret_mat[1:, :] = (close_mat[1:, :] / np.maximum(close_mat[:-1, :], 1e-8)) - 1.0

    raw_price_mat = close_mat.copy()
    raw_div_mat = np.column_stack([div_cols[t] for t in valid_tickers])
    corporate_actions: list[CorporateAction] = []
    for j, t in enumerate(valid_tickers):
        splits = split_cols[t]
        cum_future_split = 1.0
        for i in range(n_days - 1, -1, -1):
            raw_price_mat[i, j] *= cum_future_split
            raw_div_mat[i, j] *= cum_future_split
            if splits[i] > 0.0 and abs(splits[i] - 1.0) > 1e-6:
                factor = float(splits[i])
                cum_future_split *= factor
                corporate_actions.append(
                    CorporateAction(
                        action_id=f"yf_split_{t}_{idx_dates[i].isoformat()}",
                        effective_date=idx_dates[i],
                        action_type="split",
                        ticker=t,
                        ratio_numerator=Decimal(str(round(factor, 6))),
                        ratio_denominator=Decimal("1"),
                    )
                )
    corporate_actions.sort(key=lambda ca: (ca.effective_date, ca.ticker))

    # Anchor weights: latest SPY snapshot on/before each date, drifted by price since then.
    # Members absent from a snapshot (added mid-year / ticker mismatch) get its median weight.
    anchor_for_day = [_anchor_snap(d) for d in idx_dates]
    anchor_keys = sorted({a for a in anchor_for_day if a is not None})
    no_spy_weight: dict[str, list[int]] = {}
    anchor_w: dict[date | None, np.ndarray] = {}
    anchor_px_idx: dict[date | None, int] = {}
    universe_anchor_w: dict[date | None, dict[str, float]] = {}
    anchor_candidates: list[date | None] = list(anchor_keys) if anchor_keys else [None]
    for a in anchor_candidates:
        snap = spy_by_snap.get(a, {}) if a else {}
        median_w = float(np.median(list(snap.values()))) if snap else 1.0
        universe_anchor_w[a] = {t: snap.get(t, median_w) for t in universe}
        anchor_w[a] = np.array([universe_anchor_w[a][t] for t in valid_tickers])
        anchor_px_idx[a] = next((i for i, d in enumerate(idx_dates) if a is None or d >= a), 0)

    universe_set = set(universe)
    for s_date, a in sorted({(s, a) for s, a in zip(snap_for_day, anchor_for_day, strict=True)}):
        if a is None:
            continue
        snap = spy_by_snap.get(a, {})
        if not snap:
            continue
        for t in sorted((members_by_snap[s_date] & universe_set) - snap.keys()):
            years = no_spy_weight.setdefault(t, [])
            if a.year not in years:
                years.append(a.year)

    weights_mat = np.zeros((n_days, n_tok))
    for i, a in enumerate(anchor_for_day):
        p0 = close_mat[anchor_px_idx[a], :]
        weights_mat[i, :] = anchor_w[a] * close_mat[i, :] / np.maximum(p0, 1e-8)
    weights_mat *= member_mask & covered
    row_sums = weights_mat.sum(axis=1, keepdims=True)
    weights_mat = np.where(row_sums > 0, weights_mat / np.maximum(row_sums, 1e-12), 0.0)

    # Share of index weight (undrifted anchor weights) with no usable price on each day.
    lost_share = np.zeros(n_days)
    for i, (s, a) in enumerate(zip(snap_for_day, anchor_for_day, strict=True)):
        w = universe_anchor_w[a]
        members = members_by_snap[s] & set(universe)
        tot = sum(w[t] for t in members)
        lost = sum(w[t] for t in members if t not in tok_pos or not covered[i, tok_pos[t]])
        lost_share[i] = lost / tot if tot > 0 else 0.0

    etf_c = etf_c.copy()
    for i in range(1, n_days):
        if np.isnan(etf_c[i]):
            etf_c[i] = etf_c[i - 1]
    etf_c = np.where(np.isnan(etf_c), etf_c[bench_valid[0]], etf_c)
    etf_ret = np.zeros_like(etf_c)
    etf_ret[1:] = (etf_c[1:] / np.maximum(etf_c[:-1], 1e-8)) - 1.0
    etf_div = div_cols[benchmark_ticker]

    sectors: dict[str, str] = {}
    names: dict[str, str] = {}
    unknown_sector: list[str] = []
    for t in valid_tickers:
        if t in current_info:
            names[t], sectors[t] = current_info[t]
        else:
            names[t], sectors[t] = t, "Unknown"
            unknown_sector.append(t)

    # ---- errors.txt ----
    partial: list[str] = []
    for t in valid_tickers:
        j = tok_pos[t]
        mdays = member_days[t]
        if not mdays:
            continue
        mi = [i for i, s in enumerate(snap_for_day) if t in members_by_snap[s]]
        gaps = int((~has_bar[mi, j]).sum())
        if gaps > 0:
            partial.append(
                f"  {t:<8} member {_fmt_spans(mdays)}  yfinance bars "
                f"{idx_dates[first_bar[j]]}..{idx_dates[last_bar[j]]}  "
                f"({gaps} member-days without a bar)"
            )

    renames_used = [(a, b) for a, b in renamed if b in member_days]
    lines = [
        f"Data-quality report for {dataset_id}",
        f"Generated {datetime.now(UTC).isoformat(timespec='seconds')}",
        f"Window {idx_dates[0]}..{idx_dates[-1]} ({n_days} trading days), benchmark {benchmark_ticker}",
        "Sources: membership fja05680/sp500; prices/dividends/splits yfinance; weights "
        + (
            "annual SPY holdings (Kaggle, Sept 30 snapshots)"
            if spy_weights is not None
            else "equal-weight proxy"
        ),
        f"Price cache {price_cache.dir}: {cache_stats.from_cache} symbols served from cache, "
        f"{cache_stats.fetched} (partly) downloaded, {cache_stats.rebased} re-downloaded after a split",
        "",
        "SUMMARY",
        f"  Index members during window ........ {len(universe)}",
        f"  With full price coverage ........... {len(valid_tickers) - len(partial)}",
        f"  With partial price coverage ........ {len(partial)}",
        f"  With NO price data (dropped) ....... {len(no_data)}",
        f"  Member-days missing a price bar .... {missing_fraction * 100:.2f}%",
        f"  Index weight with no price (avg) ... {lost_share.mean() * 100:.2f}%",
        f"  Index weight with no price (max) ... {lost_share.max() * 100:.2f}% on {idx_dates[int(lost_share.argmax())]}",
        "",
        "  Missing index weight by year (avg):",
    ]
    years = sorted({d.year for d in idx_dates})
    for y in years:
        sel = [i for i, d in enumerate(idx_dates) if d.year == y]
        lines.append(f"    {y}: {lost_share[sel].mean() * 100:6.2f}%")
    if issues:
        lines += ["", "WARNINGS", *[f"  {m}" for m in issues]]
    lines += [
        "",
        f"TICKER RENAMES APPLIED ({len(renames_used)}) - historical symbol mapped to the current",
        "one so the company is a single continuous holding (KNOWN_TICKER_RENAMES).",
        *[f"  {a:<8} -> {b}" for a, b in renames_used],
        "",
        f"NO PRICE DATA ({len(no_data)}) - index members yfinance has no history for (usually",
        "delisted / acquired / bankrupt). Their weight is redistributed to the other members.",
        "They are cached as empty; pass --retry-missing-prices to ask Yahoo again.",
        *[f"  {t:<8} member {_fmt_spans(member_days[t])}" for t in no_data],
        "",
        f"PARTIAL PRICE COVERAGE ({len(partial)}) - weight is set to 0 outside the yfinance bar",
        "range, so the strategy exits at the last available close. A bar range that starts",
        "after membership began can also mean the ticker was reused by a different company.",
        *partial,
        "",
        f"NO SPY WEIGHT ({len(no_spy_weight)}) - member not matched in that year's SPY snapshot",
        "(added mid-year, or since acquired: the Kaggle source relabels acquired companies",
        "with the acquirer's ticker). Assigned the snapshot's median weight.",
        *[f"  {t:<8} {','.join(str(y) for y in yrs)}" for t, yrs in sorted(no_spy_weight.items())],
        "",
        f"UNKNOWN SECTOR ({len(unknown_sector)}) - not a current member; GICS sector unknown.",
        *[f"  {t}" for t in unknown_sector],
        "",
        "NOT MODELED",
        "  - Mergers, spin-offs, ticker changes and cash takeovers (only splits are applied).",
        "  - Weights are SPY weights drifted by price; share-count changes between annual",
        "    snapshots (buybacks, issuance, float adjustments) are not captured.",
        "  - Sectors are static (current GICS classification).",
        "",
    ]
    report = "\n".join(lines)

    # Also append downloaded zero-weight GICS sector ETFs (XLK, XLF, ...) for sector_etf substitutes
    sec_etf_prices: dict[str, np.ndarray] = {}
    sec_etf_rets: dict[str, np.ndarray] = {}
    sec_etf_divs: dict[str, np.ndarray] = {}
    for sec_name, sec_sym in sorted(SECTOR_ETFS.items()):
        if sec_sym in valid_tickers or sec_sym == benchmark_ticker or sec_sym not in close_cols:
            continue
        sc = close_cols[sec_sym].copy()
        if np.all(np.isnan(sc)):
            continue
        for i in range(1, n_days):
            if np.isnan(sc[i]) and not np.isnan(sc[i - 1]):
                sc[i] = sc[i - 1]
        for i in range(n_days - 2, -1, -1):
            if np.isnan(sc[i]) and not np.isnan(sc[i + 1]):
                sc[i] = sc[i + 1]
        sr = np.zeros_like(sc)
        sr[1:] = (sc[1:] / np.maximum(sc[:-1], 1e-8)) - 1.0
        sd = div_cols[sec_sym].copy()
        ss = split_cols[sec_sym]
        cum_sp = 1.0
        for i in range(n_days - 1, -1, -1):
            sc[i] *= cum_sp
            sd[i] *= cum_sp
            if ss[i] > 0.0 and abs(ss[i] - 1.0) > 1e-6:
                factor = float(ss[i])
                cum_sp *= factor
                corporate_actions.append(
                    CorporateAction(
                        action_id=f"yf_split_{sec_sym}_{idx_dates[i].isoformat()}",
                        effective_date=idx_dates[i],
                        action_type="split",
                        ticker=sec_sym,
                        ratio_numerator=Decimal(str(round(factor, 6))),
                        ratio_denominator=Decimal("1"),
                    )
                )
        sec_etf_prices[sec_sym] = sc
        sec_etf_rets[sec_sym] = sr
        sec_etf_divs[sec_sym] = sd
        sectors[sec_sym] = sec_name
        names[sec_sym] = f"{sec_name} Select Sector ETF"
    corporate_actions.sort(key=lambda ca: (ca.effective_date, ca.ticker))

    prices_df = pl.DataFrame(
        {
            "date": idx_dates,
            **{valid_tickers[j]: raw_price_mat[:, j] for j in range(n_tok)},
            **sec_etf_prices,
        }
    ).with_columns(pl.col("date").cast(pl.Date))
    returns_df = pl.DataFrame(
        {
            "date": idx_dates,
            **{valid_tickers[j]: ret_mat[:, j] for j in range(n_tok)},
            **sec_etf_rets,
        }
    ).with_columns(pl.col("date").cast(pl.Date))
    dividends_df = pl.DataFrame(
        {
            "date": idx_dates,
            **{valid_tickers[j]: raw_div_mat[:, j] for j in range(n_tok)},
            **sec_etf_divs,
        }
    ).with_columns(pl.col("date").cast(pl.Date))
    weights_df = pl.DataFrame(
        {
            "date": idx_dates,
            **{valid_tickers[j]: weights_mat[:, j] for j in range(n_tok)},
            **{sym: np.zeros(n_days, dtype=np.float64) for sym in sec_etf_prices},
        }
    ).with_columns(pl.col("date").cast(pl.Date))
    etf_df = pl.DataFrame(
        {"date": idx_dates, "close": etf_c, "return": etf_ret, "dividend": etf_div}
    ).with_columns(pl.col("date").cast(pl.Date))

    survivorship_biased = bool(no_data or partial or max_constituents is not None)
    source_label = "yfinance+fja05680_pit" + (
        "+spy_annual_weights" if spy_weights is not None else ""
    )
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
        benchmark_ticker=benchmark_ticker,
        survivorship_biased=survivorship_biased,
        missing_bar_fraction=missing_fraction,
        notes=(
            f"Point-in-time S&P 500 (fja05680) via yfinance; {len(no_data)} of {len(universe)} "
            f"members lack prices (avg {lost_share.mean() * 100:.2f}% of index weight). "
            "See errors.txt."
        ),
        extra_files={"errors.txt": report},
    )
