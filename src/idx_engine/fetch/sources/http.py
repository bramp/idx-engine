"""Shared HTTP caching and ticker-normalization helpers for external data sources."""

from __future__ import annotations

from pathlib import Path

import httpx
import polars as pl

HTTP_HEADERS = {"User-Agent": "idx-engine/0.1 (research direct-indexing simulator)"}


def normalize_yahoo_ticker(symbol: str) -> str:
    """Convert S&P dot tickers (`BRK.B`) to Yahoo Finance hyphen format (`BRK-B`)."""
    return symbol.strip().upper().replace(".", "-")


def normalize_ticker_expr(col: str) -> pl.Expr:
    return (
        pl.col(col)
        .cast(pl.String)
        .str.strip_chars()
        .str.to_uppercase()
        .str.replace_all(".", "-", literal=True)
    )


def download_cached(url: str, dest: Path, timeout_sec: float, refresh: bool = False) -> Path:
    """Atomically download `url` to `dest` once and reuse the cached file thereafter."""
    if dest.exists() and not refresh:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    resp = httpx.get(url, headers=HTTP_HEADERS, timeout=timeout_sec, follow_redirects=True)
    resp.raise_for_status()
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(resp.content)
    tmp.replace(dest)
    return dest
