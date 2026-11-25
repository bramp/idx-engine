"""Market data fetchers and versioned Parquet + DuckDB storage."""

from idx_engine.fetch.index_weights import (
    SECTOR_ETFS,
    load_weights_file,
)
from idx_engine.fetch.prices import import_local_dataset
from idx_engine.fetch.sources.fja05680 import (
    load_sp500_current_info,
    load_sp500_membership,
)
from idx_engine.fetch.sources.kaggle_spy import load_spy_annual_weights
from idx_engine.fetch.sources.price_cache import YahooPriceCache
from idx_engine.fetch.sources.sharadar import (
    SharadarAccessError,
    SharadarClient,
    fetch_sharadar_dataset,
)
from idx_engine.fetch.sources.synthetic import (
    DEFAULT_SP500_SEED,
    ConstituentInfo,
    generate_synthetic_sp500_dataset,
)
from idx_engine.fetch.sources.yfinance import fetch_yfinance_dataset
from idx_engine.fetch.store import DataStore, MarketDataset

__all__ = [
    "ConstituentInfo",
    "DEFAULT_SP500_SEED",
    "DataStore",
    "MarketDataset",
    "SECTOR_ETFS",
    "SharadarAccessError",
    "SharadarClient",
    "YahooPriceCache",
    "fetch_sharadar_dataset",
    "fetch_yfinance_dataset",
    "generate_synthetic_sp500_dataset",
    "import_local_dataset",
    "load_sp500_current_info",
    "load_sp500_membership",
    "load_spy_annual_weights",
    "load_weights_file",
]
