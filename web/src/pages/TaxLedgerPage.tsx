import { useEffect, useState } from "react";
import {
  apiClient,
  type ClosedLotItem,
  type RunDetailResponse,
  type RunListItem,
  type WashSaleItem,
} from "../api/client";
import { TaxLotTable } from "../components/TaxLotTable";

interface Props {
  runs: RunListItem[];
  selectedRunId: string | null;
  onSelectRunId: (runId: string) => void;
}

function fmtUsd(v: string | number): string {
  const num = typeof v === "string" ? Number(v) : v;
  const sign = num < 0 ? "-" : "";
  return `${sign}$${Math.abs(num).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

export function TaxLedgerPage({ runs, selectedRunId, onSelectRunId }: Props) {
  const [runDetail, setRunDetail] = useState<RunDetailResponse | null>(null);
  const [pathId, setPathId] = useState<string>("");
  const [tickerFilter, setTickerFilter] = useState<string>("");
  const [yearFilter, setYearFilter] = useState<string>("");
  const [termFilter, setTermFilter] = useState<string>("");
  const [lots, setLots] = useState<ClosedLotItem[]>([]);
  const [washSales, setWashSales] = useState<WashSaleItem[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!selectedRunId && runs.length > 0) {
      onSelectRunId(runs[0].run_id);
    }
  }, [runs, selectedRunId, onSelectRunId]);

  useEffect(() => {
    if (!selectedRunId) return;
    let cancelled = false;
    apiClient
      .getRun(selectedRunId)
      .then((res) => {
        if (cancelled) return;
        setRunDetail(res);
        const defaultPath =
          res.notable_paths.median_path_id ?? res.paths[0]?.path_id ?? "";
        setPathId(defaultPath);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [selectedRunId]);

  useEffect(() => {
    if (!selectedRunId) return;
    let cancelled = false;
    const taxYearNum = yearFilter.trim() ? Number(yearFilter) : undefined;
    Promise.all([
      apiClient.getClosedLots({
        run_id: selectedRunId,
        path_id: pathId || undefined,
        ticker: tickerFilter.trim() || undefined,
        tax_year: taxYearNum,
        term: termFilter || undefined,
      }),
      apiClient.getWashSales({
        run_id: selectedRunId,
        path_id: pathId || undefined,
        ticker: tickerFilter.trim() || undefined,
      }),
    ])
      .then(([lotRows, wsRows]) => {
        if (cancelled) return;
        setLots(lotRows);
        setWashSales(wsRows);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [selectedRunId, pathId, tickerFilter, yearFilter, termFilter]);

  const activePathEntry = runDetail?.paths.find((p) => p.path_id === pathId);

  return (
    <div>
      {error && <div className="banner-error">{error}</div>}

      <div className="card">
        <div className="card-header">
          <div style={{ display: "flex", gap: "0.75rem", alignItems: "center", flexWrap: "wrap" }}>
            <h3 className="card-title">Tax-Lot & Wash-Sale Audit Drill-Down</h3>
            <select
              value={selectedRunId ?? ""}
              onChange={(e) => onSelectRunId(e.target.value)}
            >
              {runs.length === 0 && <option value="">No completed runs available</option>}
              {runs.map((r) => (
                <option key={r.run_id} value={r.run_id}>
                  {r.run_id} · {r.strategy_name}
                </option>
              ))}
            </select>
          </div>

          {selectedRunId && (
            <div style={{ display: "flex", gap: "0.5rem" }}>
              <a
                className="btn btn-secondary"
                style={{ textDecoration: "none" }}
                href={apiClient.exportLotsUrl({
                  run_id: selectedRunId,
                  format: "csv",
                  path_id: pathId || undefined,
                  ticker: tickerFilter.trim() || undefined,
                  tax_year: yearFilter.trim() ? Number(yearFilter) : undefined,
                  term: termFilter || undefined,
                })}
              >
                Export CSV
              </a>
              <a
                className="btn btn-secondary"
                style={{ textDecoration: "none" }}
                href={apiClient.exportLotsUrl({
                  run_id: selectedRunId,
                  format: "json",
                  path_id: pathId || undefined,
                  ticker: tickerFilter.trim() || undefined,
                  tax_year: yearFilter.trim() ? Number(yearFilter) : undefined,
                  term: termFilter || undefined,
                })}
              >
                Export JSON
              </a>
            </div>
          )}
        </div>

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))",
            gap: "0.65rem",
            alignItems: "end",
          }}
        >
          <div className="form-field" style={{ marginBottom: 0 }}>
            <label>Simulation Path</label>
            <select value={pathId} onChange={(e) => setPathId(e.target.value)}>
              <option value="">All Paths</option>
              {runDetail?.paths.map((p) => (
                <option key={p.path_id} value={p.path_id}>
                  {p.path_id} (ΔLiq: {fmtUsd(p.paired_liquidation_delta)}, TE:{" "}
                  {p.tracking_error_bps.toFixed(0)} bps)
                </option>
              ))}
            </select>
          </div>

          <div className="form-field" style={{ marginBottom: 0 }}>
            <label>Ticker Filter</label>
            <input
              placeholder="e.g. AAPL, NVDA"
              value={tickerFilter}
              onChange={(e) => setTickerFilter(e.target.value)}
            />
          </div>

          <div className="form-field" style={{ marginBottom: 0 }}>
            <label>Tax Year</label>
            <input
              type="number"
              placeholder="All Years"
              value={yearFilter}
              onChange={(e) => setYearFilter(e.target.value)}
            />
          </div>

          <div className="form-field" style={{ marginBottom: 0 }}>
            <label>Holding Term</label>
            <select value={termFilter} onChange={(e) => setTermFilter(e.target.value)}>
              <option value="">All (ST + LT)</option>
              <option value="ST">Short-Term (≤ 365d)</option>
              <option value="LT">Long-Term (&gt; 365d)</option>
            </select>
          </div>
        </div>

        {runDetail?.notable_paths && (
          <div style={{ display: "flex", gap: "0.5rem", marginTop: "0.75rem", flexWrap: "wrap" }}>
            {runDetail.notable_paths.median_path_id && (
              <button
                type="button"
                className="btn btn-secondary"
                style={{ fontSize: "0.76rem", padding: "0.3rem 0.65rem" }}
                onClick={() => setPathId(runDetail.notable_paths.median_path_id!)}
              >
                Jump to Median Path ({runDetail.notable_paths.median_path_id})
              </button>
            )}
            {runDetail.notable_paths.p10_path_id && (
              <button
                type="button"
                className="btn btn-secondary"
                style={{ fontSize: "0.76rem", padding: "0.3rem 0.65rem" }}
                onClick={() => setPathId(runDetail.notable_paths.p10_path_id!)}
              >
                Jump to Downside P10 Path ({runDetail.notable_paths.p10_path_id})
              </button>
            )}
            {runDetail.notable_paths.max_te_path_id && (
              <button
                type="button"
                className="btn btn-secondary"
                style={{ fontSize: "0.76rem", padding: "0.3rem 0.65rem" }}
                onClick={() => setPathId(runDetail.notable_paths.max_te_path_id!)}
              >
                Jump to Highest Tracking Error Path ({runDetail.notable_paths.max_te_path_id})
              </button>
            )}
          </div>
        )}
      </div>

      {activePathEntry && (
        <div className="kpi-grid">
          <div className="kpi-card">
            <div className="kpi-label">Pre-Liquidation After-Tax Wealth</div>
            <div className="kpi-value">
              {fmtUsd(activePathEntry.direct.after_tax_realized_wealth)}
            </div>
            <div className="kpi-sub">
              ETF Baseline: {fmtUsd(activePathEntry.benchmark.after_tax_realized_wealth)}
            </div>
          </div>

          <div className="kpi-card">
            <div className="kpi-label">Terminal Liquidation Tax Due</div>
            <div className="kpi-value neg">
              {fmtUsd(activePathEntry.direct.liquidation_tax_due)}
            </div>
            <div className="kpi-sub">
              ETF Liquidation Tax: {fmtUsd(activePathEntry.benchmark.liquidation_tax_due)}
            </div>
          </div>

          <div className="kpi-card">
            <div className="kpi-label">Post-Liquidation True Wealth</div>
            <div className="kpi-value">
              {fmtUsd(activePathEntry.direct.after_tax_liquidation_wealth)}
            </div>
            <div className="kpi-sub">
              Paired Δ:{" "}
              <span className={activePathEntry.paired_liquidation_delta >= 0 ? "pos" : "neg"}>
                {fmtUsd(activePathEntry.paired_liquidation_delta)}
              </span>
            </div>
          </div>

          <div className="kpi-card">
            <div className="kpi-label">Unused Loss Carryforward</div>
            <div className="kpi-value">
              {fmtUsd(activePathEntry.direct.remaining_loss_carryforward)}
            </div>
            <div className="kpi-sub">
              Avg Cash Drag: {activePathEntry.direct.avg_cash_drag_bps.toFixed(1)} bps
            </div>
          </div>
        </div>
      )}

      <TaxLotTable lots={lots} washSales={washSales} />
    </div>
  );
}
