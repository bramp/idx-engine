import { useEffect, useState } from "react";
import {
  apiClient,
  type DatasetMetadata,
  type JobStatusResponse,
  type RunDetailResponse,
  type RunListItem,
  type SimulationRunPayload,
  type SweepDetailResponse,
  type SweepPayload,
} from "../api/client";
import { PairedDeltaChart } from "../components/PairedDeltaChart";
import { ParameterForm } from "../components/ParameterForm";
import { PercentileFanChart } from "../components/PercentileFanChart";
import { SweepComparisonTable } from "../components/SweepComparisonTable";

interface Props {
  datasets: DatasetMetadata[];
  runs: RunListItem[];
  selectedRunId: string | null;
  onSelectRunId: (runId: string) => void;
  onRefreshRuns: () => Promise<void>;
  onOpenLedgerForRun: (runId: string) => void;
}

function fmtUsdStr(v: string | number): string {
  const num = typeof v === "string" ? Number(v) : v;
  const sign = num < 0 ? "-" : "";
  return `${sign}$${Math.round(Math.abs(num)).toLocaleString()}`;
}

function fmtSignedUsdStr(v: string | number): string {
  const num = typeof v === "string" ? Number(v) : v;
  const sign = num >= 0 ? "+" : "-";
  return `${sign}$${Math.round(Math.abs(num)).toLocaleString()}`;
}

export function SimulatorPage({
  datasets,
  runs,
  selectedRunId,
  onSelectRunId,
  onRefreshRuns,
  onOpenLedgerForRun,
}: Props) {
  const [activeJob, setActiveJob] = useState<JobStatusResponse | null>(null);
  const [runDetail, setRunDetail] = useState<RunDetailResponse | null>(null);
  const [sweeps, setSweeps] = useState<
    { sweep_id: string; name: string }[]
  >([]);
  const [selectedSweepId, setSelectedSweepId] = useState<string | null>(null);
  const [sweepDetail, setSweepDetail] = useState<SweepDetailResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiClient
      .listSweeps()
      .then((list) => {
        setSweeps(list);
        if (list.length > 0) {
          setSelectedSweepId((prev) => prev ?? list[0].sweep_id);
        }
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (!selectedSweepId) return;
    let cancelled = false;
    apiClient
      .getSweep(selectedSweepId)
      .then((sw) => {
        if (!cancelled) setSweepDetail(sw);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [selectedSweepId]);

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
        if (!cancelled) setRunDetail(res);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [selectedRunId]);

  // Poll active async job until completed/failed
  useEffect(() => {
    if (!activeJob || activeJob.status === "completed" || activeJob.status === "failed") {
      return;
    }
    const timer = window.setInterval(async () => {
      try {
        const updated = await apiClient.getJob(activeJob.job_id);
        setActiveJob(updated);
        if (updated.status === "completed" && updated.result_id) {
          if (updated.job_type === "run") {
            await onRefreshRuns();
            onSelectRunId(updated.result_id);
            const detail = await apiClient.getRun(updated.result_id);
            setRunDetail(detail);
          } else {
            const list = await apiClient.listSweeps();
            setSweeps(list);
            setSelectedSweepId(updated.result_id);
            const sw = await apiClient.getSweep(updated.result_id);
            setSweepDetail(sw);
          }
        } else if (updated.status === "failed") {
          setError(updated.error ?? "Background job failed");
        }
      } catch (err: unknown) {
        setError(String(err));
      }
    }, 350);
    return () => window.clearInterval(timer);
  }, [activeJob, onRefreshRuns, onSelectRunId]);

  const handleSubmitRun = async (payload: SimulationRunPayload) => {
    setError(null);
    try {
      const job = await apiClient.submitRun(payload);
      setActiveJob(job);
    } catch (err: unknown) {
      setError(String(err));
    }
  };

  const handleSubmitSweep = async (payload: SweepPayload) => {
    setError(null);
    try {
      const job = await apiClient.submitSweep(payload);
      setActiveJob(job);
    } catch (err: unknown) {
      setError(String(err));
    }
  };

  const isJobRunning =
    activeJob !== null &&
    (activeJob.status === "queued" || activeJob.status === "running");

  const s = runDetail?.summary;
  const medianDelta = s ? Number(s.median_paired_wealth_delta) : 0;

  return (
    <div className="grid-2">
      <div>
        <ParameterForm
          datasets={datasets}
          busy={isJobRunning}
          onSubmitRun={handleSubmitRun}
          onSubmitSweep={handleSubmitSweep}
        />
      </div>

      <div>
        {error && <div className="banner-error">{error}</div>}

        {isJobRunning && activeJob && (
          <div className="card">
            <div className="card-header" style={{ marginBottom: 4 }}>
              <span className="card-title">
                Executing {activeJob.job_type === "run" ? "Paired Simulation" : "Parameter Sweep"} (
                {activeJob.job_id})
              </span>
              <span className="badge badge-info">
                {activeJob.progress_completed} / {activeJob.progress_total}
              </span>
            </div>
            <div className="progress-bar-outer">
              <div
                className="progress-bar-inner"
                style={{
                  width: `${
                    (activeJob.progress_completed / Math.max(1, activeJob.progress_total)) * 100
                  }%`,
                }}
              />
            </div>
          </div>
        )}

        <div className="card">
          <div className="card-header" style={{ marginBottom: 0 }}>
            <div style={{ display: "flex", alignItems: "center", gap: "0.65rem", flexWrap: "wrap" }}>
              <span className="card-title">Completed Runs</span>
              <select
                value={selectedRunId ?? ""}
                onChange={(e) => onSelectRunId(e.target.value)}
              >
                {runs.length === 0 && <option value="">No runs yet — launch one on the left</option>}
                {runs.map((r) => (
                  <option key={r.run_id} value={r.run_id}>
                    {r.run_id} · {r.strategy_name} ({r.path_mode}, {r.path_count}p)
                  </option>
                ))}
              </select>
            </div>
            {runDetail && (
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => onOpenLedgerForRun(runDetail.run_id)}
              >
                Inspect Closed Tax Lots & Wash Sales →
              </button>
            )}
          </div>
        </div>

        {s && runDetail && (
          <>
            {s.survivorship_biased && (
              <div className="banner-warn">
                <strong>Exploratory Only (Survivorship-Biased Dataset):</strong> Validation claims
                are blocked until evaluated on a complete point-in-time dataset.
              </div>
            )}

            <div className="kpi-grid">
              <div className="kpi-card">
                <div className="kpi-label">Liquidation Win Rate vs ETF</div>
                <div
                  className={`kpi-value ${
                    s.win_rate_vs_benchmark_liquidation >= 0.5 ? "pos" : "neg"
                  }`}
                >
                  {(s.win_rate_vs_benchmark_liquidation * 100).toFixed(1)}%
                </div>
                <div className="kpi-sub">
                  Status:{" "}
                  <span
                    className={`badge ${
                      s.validation_status === "validated" ? "badge-ok" : "badge-warn"
                    }`}
                  >
                    {s.validation_status}
                  </span>
                </div>
              </div>

              <div className="kpi-card">
                <div className="kpi-label">Median Liquidation ΔWp</div>
                <div className={`kpi-value ${medianDelta >= 0 ? "pos" : "neg"}`}>
                  {fmtSignedUsdStr(medianDelta)}
                </div>
                <div className="kpi-sub">
                  IQR: {fmtSignedUsdStr(s.p25_paired_wealth_delta)} to{" "}
                  {fmtSignedUsdStr(s.p75_paired_wealth_delta)}
                </div>
              </div>

              <div className="kpi-card">
                <div className="kpi-label">Downside P10 / Upside P90 ΔWp</div>
                <div className="kpi-value" style={{ fontSize: "1rem" }}>
                  {fmtSignedUsdStr(s.p10_paired_wealth_delta)} /{" "}
                  {fmtSignedUsdStr(s.p90_paired_wealth_delta)}
                </div>
                <div className="kpi-sub">Full terminal liquidation basis</div>
              </div>

              <div className="kpi-card">
                <div className="kpi-label">After-Tax CAGR (Strat vs ETF)</div>
                <div className="kpi-value" style={{ fontSize: "1.05rem" }}>
                  {(s.median_after_tax_cagr * 100).toFixed(2)}% vs{" "}
                  {(s.benchmark_median_after_tax_cagr * 100).toFixed(2)}%
                </div>
                <div className="kpi-sub">
                  Terminal: {fmtUsdStr(s.median_strategy_liquidation_wealth)} vs{" "}
                  {fmtUsdStr(s.median_benchmark_liquidation_wealth)}
                </div>
              </div>

              <div className="kpi-card">
                <div className="kpi-label">Tracking Error (Median / P90)</div>
                <div className="kpi-value">
                  {s.median_tracking_error_bps.toFixed(0)} / {s.p90_tracking_error_bps.toFixed(0)} bps
                </div>
                <div className="kpi-sub">
                  Annual turnover: {(s.median_annual_turnover * 100).toFixed(1)}%
                </div>
              </div>

              <div className="kpi-card">
                <div className="kpi-label">Harvested Losses / Wash Disallowed</div>
                <div className="kpi-value" style={{ fontSize: "1rem" }}>
                  {fmtUsdStr(s.median_harvested_losses)} /{" "}
                  {fmtUsdStr(s.median_disallowed_wash_losses)}
                </div>
                <div className="kpi-sub">
                  {s.median_closed_lots_per_year.toFixed(1)} closed lots / year
                </div>
              </div>
            </div>

            <div className="grid-charts">
              <PercentileFanChart
                bands={runDetail.equity_bands}
                strategyName={s.strategy_name}
                benchmarkName={s.benchmark_name}
                pathMode={s.path_mode}
              />
              <PairedDeltaChart
                bands={runDetail.equity_bands}
                paths={runDetail.paths}
                pathMode={s.path_mode}
              />
            </div>
          </>
        )}

        {sweeps.length > 1 && (
          <div className="card" style={{ padding: "0.65rem 1rem" }}>
            <div style={{ display: "flex", alignItems: "center", gap: "0.65rem" }}>
              <span className="card-title">Completed Sweeps</span>
              <select
                value={selectedSweepId ?? ""}
                onChange={(e) => setSelectedSweepId(e.target.value)}
              >
                {sweeps.map((sw) => (
                  <option key={sw.sweep_id} value={sw.sweep_id}>
                    {sw.sweep_id} · {sw.name}
                  </option>
                ))}
              </select>
            </div>
          </div>
        )}
        {sweepDetail && <SweepComparisonTable sweep={sweepDetail} />}
      </div>
    </div>
  );
}
