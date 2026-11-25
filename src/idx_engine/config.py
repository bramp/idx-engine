"""Pydantic configuration models, TOML loaders, and deterministic run_id hashing."""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import re
import tomllib
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from idx_engine.models import (
    PathMode,
    PathSpec,
    RebalanceMode,
    SubstituteMode,
    TaxSettlementMode,
)

MAX_SWEEP_GRID_POINTS = 500
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class DatasetRefConfig(BaseModel):
    """Reference to an immutable cached dataset version."""

    model_config = ConfigDict(extra="forbid")

    dataset_id: str = "sp500_synthetic_demo"
    version: str = "latest"
    start_date: date | None = None
    end_date: date | None = None

    @field_validator("dataset_id", "version")
    @classmethod
    def _check_safe_id(cls, v: str) -> str:
        if not _SAFE_ID_RE.fullmatch(v):
            raise ValueError(f"Invalid identifier '{v}'")
        return v


class PathSpecConfig(BaseModel):
    """Pydantic config wrapper for PathSpec."""

    model_config = ConfigDict(extra="forbid")

    mode: PathMode = "historical"
    horizon_days: int = Field(default=252 * 5, ge=2)
    step_days: int = Field(default=21, ge=1)
    block_size_days: int = Field(default=21, ge=2)
    num_paths: int = Field(default=100, ge=1)
    seed: int = 42

    def to_path_spec(self) -> PathSpec:
        return PathSpec(
            mode=self.mode,
            horizon_days=self.horizon_days,
            step_days=self.step_days,
            block_size_days=self.block_size_days,
            num_paths=self.num_paths,
            seed=self.seed,
        )


class StrategyConfig(BaseModel):
    """Direct indexing and paired ETF baseline strategy configuration."""

    model_config = ConfigDict(extra="forbid")

    name: str = "direct_index"
    strategy_type: Literal["direct_index", "etf_baseline"] = "direct_index"
    sample_size: int | None = Field(default=None, ge=1)
    allow_fractional: bool = True
    tlh_enabled: bool = True
    tlh_loss_pct_threshold: Decimal = Field(default=Decimal("0.05"), ge=Decimal("0"))
    tlh_min_loss_dollars: Decimal = Field(default=Decimal("250"), ge=Decimal("0"))
    tlh_check_frequency_days: int = Field(default=5, ge=1)
    substitute_mode: SubstituteMode = "sector_peer"
    rebalance_mode: RebalanceMode = "quarterly"
    drift_band_abs: Decimal = Field(default=Decimal("0.01"), ge=Decimal("0"))
    direct_expense_ratio_bps: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    etf_expense_ratio_bps: Decimal = Field(default=Decimal("3"), ge=Decimal("0"))
    lot_policy: Literal["HIFO", "FIFO"] = "HIFO"
    substantially_identical_groups: list[list[str]] = Field(
        default_factory=lambda: [["GOOG", "GOOGL"], ["FOXA", "FOX"], ["NWSA", "NWS"]]
    )


class TaxConfig(BaseModel):
    """Federal + state capital gains, dividend, external gains, and liquidation rules."""

    model_config = ConfigDict(extra="forbid")

    fed_st_rate: Decimal = Field(default=Decimal("0.37"), ge=Decimal("0"), le=Decimal("1"))
    fed_lt_rate: Decimal = Field(default=Decimal("0.20"), ge=Decimal("0"), le=Decimal("1"))
    niit_rate: Decimal = Field(default=Decimal("0.038"), ge=Decimal("0"), le=Decimal("1"))
    state_st_rate: Decimal = Field(default=Decimal("0.093"), ge=Decimal("0"), le=Decimal("1"))
    state_lt_rate: Decimal = Field(default=Decimal("0.093"), ge=Decimal("0"), le=Decimal("1"))
    ordinary_income_offset_cap: Decimal = Field(default=Decimal("3000"), ge=Decimal("0"))
    external_st_gains_annual: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    external_lt_gains_annual: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    qualified_dividend_fraction_default: Decimal = Field(
        default=Decimal("0.95"), ge=Decimal("0"), le=Decimal("1")
    )
    tax_settlement_mode: TaxSettlementMode = "reinvest_and_pay_from_portfolio"
    liquidate_at_end: bool = True
    carryforward_terminal_credit_rate: Decimal = Field(
        default=Decimal("0"), ge=Decimal("0"), le=Decimal("1")
    )

    @property
    def combined_st_rate(self) -> Decimal:
        return min(Decimal("1"), self.fed_st_rate + self.niit_rate + self.state_st_rate)

    @property
    def combined_lt_rate(self) -> Decimal:
        return min(Decimal("1"), self.fed_lt_rate + self.niit_rate + self.state_lt_rate)


class CashFlowConfig(BaseModel):
    """Initial capital and periodic deposit/withdrawal schedule."""

    model_config = ConfigDict(extra="forbid")

    initial_capital: Decimal = Field(default=Decimal("250000"), gt=Decimal("0"))
    periodic_contribution: Decimal = Field(default=Decimal("0"))
    contribution_frequency_days: int = Field(default=21, ge=1)


class SimulationRunConfig(BaseModel):
    """Complete deterministic specification of a paired simulation run."""

    model_config = ConfigDict(extra="forbid")

    dataset: DatasetRefConfig = Field(default_factory=DatasetRefConfig)
    path: PathSpecConfig = Field(default_factory=PathSpecConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    tax: TaxConfig = Field(default_factory=TaxConfig)
    cashflow: CashFlowConfig = Field(default_factory=CashFlowConfig)
    save_equity_curves: bool = True
    save_lot_details: bool = True

    def compute_run_id(self, resolved_dataset_version: str) -> str:
        """Derive deterministic run_id from (dataset_version, path_spec, strategy, tax, cashflow)."""
        payload = {
            "dataset": {
                "dataset_id": self.dataset.dataset_id,
                "version": resolved_dataset_version,
                "start_date": self.dataset.start_date.isoformat()
                if self.dataset.start_date
                else None,
                "end_date": self.dataset.end_date.isoformat() if self.dataset.end_date else None,
            },
            "path": self.path.model_dump(mode="json"),
            "strategy": self.strategy.model_dump(mode="json"),
            "tax": self.tax.model_dump(mode="json"),
            "cashflow": self.cashflow.model_dump(mode="json"),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        return f"run_{digest}"


class SweepConfig(BaseModel):
    """Parameter grid sweep & optional walk-forward out-of-sample split."""

    model_config = ConfigDict(extra="forbid")

    name: str = "parameter_sweep"
    base: SimulationRunConfig = Field(default_factory=SimulationRunConfig)
    grid: dict[str, list[Any]] = Field(default_factory=dict)
    walk_forward_split_ratio: float | None = Field(default=None, gt=0.1, lt=0.95)

    @model_validator(mode="after")
    def _validate_grid_keys(self) -> SweepConfig:
        valid_prefixes = ("strategy.", "tax.", "cashflow.", "path.")
        base_dump = self.base.model_dump(mode="json")
        normalized_grid: dict[str, list[Any]] = {}
        total_points = 1

        for key, values in self.grid.items():
            if not key.startswith(valid_prefixes):
                raise ValueError(
                    f"Sweep grid key '{key}' must start with one of {valid_prefixes}"
                )
            if not values:
                raise ValueError(f"Sweep grid key '{key}' must have at least one value")

            section, field_name = key.split(".", 1)
            if field_name not in base_dump.get(section, {}):
                raise ValueError(f"Unknown config field '{key}' in sweep grid")

            deduped: list[Any] = []
            seen_canonical: set[str] = set()
            for val in values:
                probe = copy.deepcopy(base_dump)
                probe[section][field_name] = val
                validated = SimulationRunConfig.model_validate(probe)
                canonical_val = validated.model_dump(mode="json")[section][field_name]
                canon_token = json.dumps(canonical_val, sort_keys=True)
                if canon_token not in seen_canonical:
                    seen_canonical.add(canon_token)
                    deduped.append(canonical_val)

            # Sort purely numeric/Decimal axes monotonically so plateau neighbors are ordered
            def _numeric_sort_key(item: Any) -> float | None:
                if isinstance(item, bool) or item is None:
                    return None
                try:
                    return float(item)
                except (TypeError, ValueError):
                    return None

            num_keys = [_numeric_sort_key(v) for v in deduped]
            if deduped and all(nk is not None for nk in num_keys):
                deduped = [
                    v
                    for _, v in sorted(
                        zip(num_keys, deduped, strict=True),
                        key=lambda pair: pair[0],  # type: ignore[arg-type,return-value]
                    )
                ]

            normalized_grid[key] = deduped
            total_points *= len(deduped)
            if total_points > MAX_SWEEP_GRID_POINTS:
                raise ValueError(
                    f"Sweep grid exceeds maximum of {MAX_SWEEP_GRID_POINTS} parameter combinations"
                )

        if self.grid:
            all_keys = sorted(normalized_grid.keys())
            for combo in itertools.product(*(normalized_grid[k] for k in all_keys)):
                probe = copy.deepcopy(base_dump)
                for k, v in zip(all_keys, combo, strict=True):
                    sec, fld = k.split(".", 1)
                    probe[sec][fld] = v
                SimulationRunConfig.model_validate(probe)

        self.grid = normalized_grid
        return self

    def compute_sweep_id(self, resolved_dataset_version: str) -> str:
        payload = {
            "name": self.name,
            "base_run_id": self.base.compute_run_id(resolved_dataset_version),
            "grid": self.grid,
            "walk_forward_split_ratio": self.walk_forward_split_ratio,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        return f"sweep_{digest}"


def load_run_config(path: str | Path) -> SimulationRunConfig:
    """Load a SimulationRunConfig from a TOML file."""
    with Path(path).open("rb") as fp:
        raw = tomllib.load(fp)
    return SimulationRunConfig.model_validate(raw)


def load_sweep_config(path: str | Path) -> SweepConfig:
    """Load a SweepConfig from a TOML file."""
    with Path(path).open("rb") as fp:
        raw = tomllib.load(fp)
    return SweepConfig.model_validate(raw)
