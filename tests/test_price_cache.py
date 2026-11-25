from __future__ import annotations

from datetime import date

import pandas as pd

from idx_engine.fetch.sources import price_cache
from idx_engine.fetch.sources.price_cache import YahooPriceCache


def _yahoo_frame(
    tickers: list[str], start: str, end: str, price: float = 100.0
) -> pd.DataFrame:
    dates = pd.bdate_range(start, pd.Timestamp(end) - pd.Timedelta(days=1))
    columns = {
        (ticker, field): [price] * len(dates)
        for ticker in tickers
        for field in ("Close", "Dividends", "Stock Splits")
    }
    return pd.DataFrame(columns, index=dates)


def test_price_cache_reuses_ranges_and_fetches_only_extension(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_download(*, tickers: list[str], start: str, end: str, **kwargs):
        calls.append((start, end))
        return _yahoo_frame(tickers, start, end)

    monkeypatch.setattr(price_cache.yf, "download", fake_download)
    cache = YahooPriceCache(tmp_path / "yfinance", sleep=lambda _: None)
    start, first_end, extended_end = date(2024, 1, 2), date(2024, 1, 10), date(2024, 1, 17)

    cache.ensure(["AAA"], start, first_end)
    assert len(calls) == 1

    stats = cache.ensure(["AAA"], start, first_end)
    assert stats.from_cache == 1
    assert len(calls) == 1

    cache.ensure(["AAA"], start, extended_end)
    assert len(calls) == 2
    assert date.fromisoformat(calls[1][0]) > start
    assert cache.coverage("AAA") == (start, extended_end, len(pd.bdate_range(start, extended_end)))

    cache.ensure(["AAA"], date(2024, 1, 3), date(2024, 1, 5), refresh=True)
    assert cache.coverage("AAA") == (start, extended_end, len(pd.bdate_range(start, extended_end)))


def test_price_cache_retries_symbols_with_no_cached_data(tmp_path, monkeypatch) -> None:
    calls: list[str] = []
    return_missing = False

    def fake_download(*, tickers: list[str], start: str, end: str, **kwargs):
        calls.extend(tickers)
        available = [ticker for ticker in tickers if ticker != "BBB" or return_missing]
        if not available:
            return pd.DataFrame()
        return _yahoo_frame(available, start, end)

    monkeypatch.setattr(price_cache.yf, "download", fake_download)
    cache = YahooPriceCache(
        tmp_path / "yfinance",
        chunk_size=1,
        retry_chunk_size=1,
        max_attempts=1,
        sleep=lambda _: None,
    )
    start, end = date(2024, 1, 2), date(2024, 1, 5)

    cache.ensure(["AAA", "BBB"], start, end)
    assert cache.coverage("BBB") == (start, end, 0)
    first_call_count = len(calls)

    cache.ensure(["AAA", "BBB"], start, end)
    assert len(calls) == first_call_count

    return_missing = True
    cache.ensure(["AAA", "BBB"], start, end, retry_missing=True)
    assert calls[-1] == "BBB"
    assert cache.coverage("BBB") == (start, end, len(pd.bdate_range(start, end)))


def test_corrupt_price_cache_is_invalidated_and_refetched(tmp_path, monkeypatch) -> None:
    calls = 0

    def fake_download(*, tickers: list[str], start: str, end: str, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            return pd.DataFrame()
        return _yahoo_frame(tickers, start, end)

    monkeypatch.setattr(price_cache.yf, "download", fake_download)
    cache_dir = tmp_path / "yfinance"
    start, end = date(2024, 1, 2), date(2024, 1, 5)
    cache = YahooPriceCache(cache_dir, max_attempts=1, sleep=lambda _: None)
    cache.ensure(["AAA"], start, end)
    cache._path("AAA").write_bytes(b"not a parquet file")

    recovered_cache = YahooPriceCache(cache_dir, max_attempts=1, sleep=lambda _: None)
    recovered_cache.ensure(["AAA"], start, end)
    assert recovered_cache.coverage("AAA") is None
    assert "AAA" not in recovered_cache.manifest

    recovered_cache.ensure(["AAA"], start, end)
    assert recovered_cache.coverage("AAA") == (start, end, len(pd.bdate_range(start, end)))
    assert recovered_cache.read("AAA") is not None


def test_negative_cache_survives_load_across_fresh_instances(tmp_path, monkeypatch) -> None:
    calls: list[str] = []

    def fake_download(*, tickers: list[str], start: str, end: str, **kwargs):
        calls.extend(tickers)
        available = [t for t in tickers if t != "DELISTED"]
        return _yahoo_frame(available, start, end) if available else pd.DataFrame()

    monkeypatch.setattr(price_cache.yf, "download", fake_download)
    cache_dir = tmp_path / "yfinance"
    start, end = date(2024, 1, 2), date(2024, 1, 5)

    c1 = YahooPriceCache(cache_dir, max_attempts=1, sleep=lambda _: None)
    c1.ensure(["AAA", "DELISTED"], start, end)
    assert c1.coverage("DELISTED") == (start, end, 0)

    # Fresh instance: ensure + load must keep the negative-cache entry on disk.
    calls.clear()
    c2 = YahooPriceCache(cache_dir, max_attempts=1, sleep=lambda _: None)
    c2.ensure(["AAA", "DELISTED"], start, end)
    c2.load(["AAA", "DELISTED"], start, end)
    assert calls == []

    # Third fresh instance must still skip DELISTED.
    c3 = YahooPriceCache(cache_dir, max_attempts=1, sleep=lambda _: None)
    assert c3.coverage("DELISTED") == (start, end, 0)
    c3.ensure(["AAA", "DELISTED"], start, end)
    assert calls == []
