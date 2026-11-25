/** Typed API client for the FastAPI Direct Indexing Workbench backend. */

export interface DatasetMetadata {
  dataset_id: string;
  version: string;
  source: string;
  created_at_iso: string;
  start_date: string;
  end_date: string;
  trading_days: number;
  ticker_count: number;
  benchmark_ticker: string;
  survivorship_biased: boolean;
  missing_bar_fraction: number;
  notes: string;
}

export interface ConstituentRow {
  ticker: string;
  name: string;
  sector: string;
  start_weight: number;
  end_weight: number;
  start_price: number;
  end_price: number;
  total_dividends_per_share: number;
}

export interface DatasetDetailResponse {
  metadata: DatasetMetadata;
  sectors: { sector: string; weight: number }[];
  constituents: ConstituentRow[];
  corporate_actions: {
    action_id: string;
    effective_date: string;
    action_type: string;
    ticker: string;
    ratio_numerator: string;
    ratio_denominator: string;
    target_ticker: string | null;
  }[];
}

export interface TickerSeriesPoint {
  date: string;
  price: number;
  return: number;
  dividend: number;
  weight: number;
}

export interface TickerSeriesResponse {
  dataset_id: string;
  version: string;
  ticker: string;
  sector: string;
  points: TickerSeriesPoint[];
}

export interface JobStatusResponse {
  job_id: string;
  job_type: "run" | "sweep";
  status: "queued" | "running" | "completed" | "failed";
  progress_completed: number;
  progress_total: number;
  result_id: string | null;
  error: string | null;
  created_at_iso: string;
  updated_at_iso: string;
}

export interface DistributionSummary {
  run_id: string;
  dataset_id: string;
  dataset_version: string;
  survivorship_biased: boolean;
  validation_status: "validated" | "exploratory_survivorship_biased" | "single_path";
  strategy_name: string;
  benchmark_name: string;
  path_mode: "historical" | "rolling_window" | "block_bootstrap";
  path_count: number;
  win_rate_vs_benchmark_liquidation: number;
  median_paired_wealth_delta: string;
  p10_paired_wealth_delta: string;
  p25_paired_wealth_delta: string;
  p75_paired_wealth_delta: string;
  p90_paired_wealth_delta: string;
  median_strategy_liquidation_wealth: string;
  median_benchmark_liquidation_wealth: string;
  median_after_tax_cagr: number;
  benchmark_median_after_tax_cagr: number;
  median_tracking_error_bps: number;
  p90_tracking_error_bps: number;
  median_annual_turnover: number;
  median_harvested_losses: string;
  median_disallowed_wash_losses: string;
  median_closed_lots_per_year: number;
}

export interface EquityBandPoint {
  day_index: number;
  date: string;
  strat_p10: number;
  strat_p25: number;
  strat_p50: number;
  strat_p75: number;
  strat_p90: number;
  bench_p50: number;
  delta_p10: number;
  delta_p25: number;
  delta_p50: number;
  delta_p75: number;
  delta_p90: number;
}

export interface PathOutcomeJson {
  path_id: string;
  strategy_name: string;
  initial_capital: string;
  net_contributions: string;
  ending_market_value: string;
  ending_cash: string;
  cumulative_tax_paid: string;
  liquidation_tax_due: string;
  remaining_loss_carryforward: string;
  after_tax_realized_wealth: string;
  after_tax_liquidation_wealth: string;
  pre_tax_cagr: number;
  after_tax_cagr: number;
  annualized_tracking_error_bps: number;
  annual_turnover: number;
  harvested_gross_losses: string;
  disallowed_wash_sale_losses: string;
  closed_lot_count: number;
  wash_sale_count: number;
  trade_count: number;
  avg_cash_drag_bps: number;
}

export interface RunPathEntry {
  path_id: string;
  direct: PathOutcomeJson;
  benchmark: PathOutcomeJson;
  paired_realized_delta: number;
  paired_liquidation_delta: number;
  paired_cagr_delta_bps: number;
  tracking_error_bps: number;
}

export interface RunListItem {
  run_id: string;
  created_at_iso: string;
  dataset_id: string;
  dataset_version: string;
  survivorship_biased: boolean;
  validation_status: string;
  strategy_name: string;
  benchmark_name: string;
  path_mode: string;
  path_count: number;
  summary: DistributionSummary;
}

export interface RunDetailResponse extends RunListItem {
  config: Record<string, unknown>;
  paths: RunPathEntry[];
  equity_bands: EquityBandPoint[];
  notable_paths: {
    median_path_id: string | null;
    p10_path_id: string | null;
    max_te_path_id: string | null;
  };
}

export interface SimulationRunPayload {
  dataset: {
    dataset_id: string;
    version: string;
  };
  path: {
    mode: "historical" | "rolling_window" | "block_bootstrap";
    horizon_days: number;
    step_days: number;
    block_size_days: number;
    num_paths: number;
    seed: number;
  };
  strategy: {
    name: string;
    sample_size: number | null;
    allow_fractional: boolean;
    tlh_enabled: boolean;
    tlh_loss_pct_threshold: string;
    tlh_min_loss_dollars: string;
    tlh_check_frequency_days: number;
    substitute_mode: "sector_peer" | "sector_etf" | "cash";
    rebalance_mode: "none" | "monthly" | "quarterly" | "annual" | "drift_band";
    drift_band_abs: string;
    direct_expense_ratio_bps: string;
    etf_expense_ratio_bps: string;
    lot_policy: "HIFO" | "FIFO";
  };
  tax: {
    fed_st_rate: string;
    fed_lt_rate: string;
    niit_rate: string;
    state_st_rate: string;
    state_lt_rate: string;
    ordinary_income_offset_cap: string;
    external_st_gains_annual: string;
    external_lt_gains_annual: string;
    tax_settlement_mode: "reinvest_and_pay_from_portfolio" | "external_cash";
    liquidate_at_end: boolean;
  };
  cashflow: {
    initial_capital: string;
    periodic_contribution: string;
    contribution_frequency_days: number;
  };
  save_equity_curves?: boolean;
  save_lot_details?: boolean;
}

export interface SweepPayload {
  name: string;
  walk_forward_split_ratio: number | null;
  base: SimulationRunPayload;
  grid: Record<string, (string | number | boolean)[]>;
}

export interface SweepRowResponse {
  run_id: string;
  params: Record<string, string | number | boolean | null>;
  in_sample: DistributionSummary;
  out_of_sample: DistributionSummary | null;
  plateau_score: number | null;
}

export interface SweepDetailResponse {
  sweep_id: string;
  name: string;
  created_at_iso: string;
  dataset_id: string;
  dataset_version: string;
  walk_forward_split_ratio: number | null;
  rows: SweepRowResponse[];
}

export interface ClosedLotItem {
  path_id: string;
  close_id: string;
  source_lot_id: string;
  ticker: string;
  sector: string;
  acquired_date: string;
  sold_date: string;
  tax_year: number;
  holding_days: number;
  term: "ST" | "LT";
  quantity: number;
  proceeds_total: number;
  cost_basis_total: number;
  raw_realized_gain_loss: number;
  disallowed_wash_loss: number;
  adjusted_realized_gain_loss: number;
  reason: string;
}

export interface WashSaleItem {
  path_id: string;
  adjustment_id: string;
  closed_lot_id: string;
  loss_ticker: string;
  replacement_ticker: string;
  sold_date: string;
  replacement_date: string;
  replacement_lot_id: string;
  matched_quantity: number;
  disallowed_loss: number;
  tacked_holding_days: number;
}

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const resp = await fetch(url, {
    ...init,
    headers,
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`API ${resp.status}: ${text}`);
  }
  return (await resp.json()) as T;
}

export const apiClient = {
  listDatasets: () => fetchJson<DatasetMetadata[]>("/api/datasets"),
  inspectDataset: (datasetId: string, version = "latest") =>
    fetchJson<DatasetDetailResponse>(
      `/api/datasets/${encodeURIComponent(datasetId)}?version=${encodeURIComponent(version)}`
    ),
  getTickerSeries: (datasetId: string, ticker: string, version = "latest") =>
    fetchJson<TickerSeriesResponse>(
      `/api/datasets/${encodeURIComponent(datasetId)}/series/${encodeURIComponent(ticker)}?version=${encodeURIComponent(version)}`
    ),
  listRuns: () => fetchJson<RunListItem[]>("/api/runs"),
  submitRun: (payload: SimulationRunPayload) =>
    fetchJson<JobStatusResponse>("/api/runs", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  getRun: (runId: string) =>
    fetchJson<RunDetailResponse>(`/api/runs/${encodeURIComponent(runId)}`),
  getJob: (jobId: string) =>
    fetchJson<JobStatusResponse>(`/api/jobs/${encodeURIComponent(jobId)}`),
  listSweeps: () =>
    fetchJson<
      {
        sweep_id: string;
        name: string;
        created_at_iso: string;
        dataset_id: string;
        dataset_version: string;
        walk_forward_split_ratio: number | null;
      }[]
    >("/api/sweeps"),
  submitSweep: (payload: SweepPayload) =>
    fetchJson<JobStatusResponse>("/api/sweeps", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  getSweep: (sweepId: string) =>
    fetchJson<SweepDetailResponse>(`/api/sweeps/${encodeURIComponent(sweepId)}`),
  getClosedLots: (params: {
    run_id: string;
    path_id?: string;
    ticker?: string;
    tax_year?: number;
    term?: string;
    strategy_role?: "direct" | "benchmark";
    limit?: number;
  }) => {
    const q = new URLSearchParams();
    if (params.path_id) q.set("path_id", params.path_id);
    if (params.ticker) q.set("ticker", params.ticker);
    if (params.tax_year !== undefined && Number.isInteger(params.tax_year)) {
      q.set("tax_year", String(params.tax_year));
    }
    if (params.term) q.set("term", params.term);
    if (params.strategy_role) q.set("strategy_role", params.strategy_role);
    if (params.limit !== undefined && Number.isInteger(params.limit)) {
      q.set("limit", String(params.limit));
    }
    return fetchJson<ClosedLotItem[]>(
      `/api/ledger/${encodeURIComponent(params.run_id)}/closed-lots?${q.toString()}`
    );
  },
  getWashSales: (params: {
    run_id: string;
    path_id?: string;
    ticker?: string;
    limit?: number;
  }) => {
    const q = new URLSearchParams();
    if (params.path_id) q.set("path_id", params.path_id);
    if (params.ticker) q.set("ticker", params.ticker);
    if (params.limit !== undefined && Number.isInteger(params.limit)) {
      q.set("limit", String(params.limit));
    }
    return fetchJson<WashSaleItem[]>(
      `/api/ledger/${encodeURIComponent(params.run_id)}/wash-sales?${q.toString()}`
    );
  },
  exportLotsUrl: (params: {
    run_id: string;
    format: "csv" | "json";
    path_id?: string;
    ticker?: string;
    tax_year?: number;
    term?: string;
    strategy_role?: "direct" | "benchmark";
  }) => {
    const q = new URLSearchParams({ format: params.format });
    if (params.path_id) q.set("path_id", params.path_id);
    if (params.ticker) q.set("ticker", params.ticker);
    if (params.tax_year !== undefined && Number.isInteger(params.tax_year)) {
      q.set("tax_year", String(params.tax_year));
    }
    if (params.term) q.set("term", params.term);
    if (params.strategy_role) q.set("strategy_role", params.strategy_role);
    return `/api/ledger/${encodeURIComponent(params.run_id)}/export?${q.toString()}`;
  },
};
