import { useState } from "react";
import type {
  DatasetMetadata,
  SimulationRunPayload,
  SweepPayload,
} from "../api/client";

interface Props {
  datasets: DatasetMetadata[];
  busy: boolean;
  onSubmitRun: (payload: SimulationRunPayload) => void;
  onSubmitSweep: (payload: SweepPayload) => void;
}

export function ParameterForm({
  datasets,
  busy,
  onSubmitRun,
  onSubmitSweep,
}: Props) {
  const defaultKey = datasets[0]
    ? `${datasets[0].dataset_id}@${datasets[0].version}`
    : "";

  const [datasetKey, setDatasetKey] = useState(defaultKey);
  const [pathMode, setPathMode] = useState<
    "historical" | "rolling_window" | "block_bootstrap"
  >("block_bootstrap");
  const [horizonDays, setHorizonDays] = useState(504);
  const [blockSizeDays, setBlockSizeDays] = useState(21);
  const [stepDays, setStepDays] = useState(21);
  const [numPaths, setNumPaths] = useState(15);
  const [seed, setSeed] = useState(42);

  const [strategyName, setStrategyName] = useState("direct_index_tlh");
  const [sampleSize, setSampleSize] = useState<string>("");
  const [allowFractional, setAllowFractional] = useState(true);
  const [tlhEnabled, setTlhEnabled] = useState(true);
  const [tlhLossPct, setTlhLossPct] = useState("0.05");
  const [tlhMinDollars, setTlhMinDollars] = useState("250");
  const [tlhCheckFreq, setTlhCheckFreq] = useState(5);
  const [substituteMode, setSubstituteMode] = useState<
    "sector_peer" | "sector_etf" | "cash"
  >("sector_peer");
  const [rebalanceMode, setRebalanceMode] = useState<
    "none" | "monthly" | "quarterly" | "annual" | "drift_band"
  >("quarterly");
  const [directFeeBps, setDirectFeeBps] = useState("10");
  const [etfFeeBps, setEtfFeeBps] = useState("3");
  const [lotPolicy, setLotPolicy] = useState<"HIFO" | "FIFO">("HIFO");

  const [fedStRate, setFedStRate] = useState("0.37");
  const [fedLtRate, setFedLtRate] = useState("0.20");
  const [niitRate, setNiitRate] = useState("0.038");
  const [stateRate, setStateRate] = useState("0.093");
  const [extStGains, setExtStGains] = useState("5000");
  const [extLtGains, setExtLtGains] = useState("15000");
  const [settlementMode, setSettlementMode] = useState<
    "reinvest_and_pay_from_portfolio" | "external_cash"
  >("reinvest_and_pay_from_portfolio");

  const [initialCapital, setInitialCapital] = useState("250000");
  const [periodicContribution, setPeriodicContribution] = useState("0");

  // Sweep settings
  const [sweepThresholds, setSweepThresholds] = useState("0.03, 0.05, 0.08");
  const [sweepSampleSizes, setSweepSampleSizes] = useState("25, 50");
  const [walkForwardSplit, setWalkForwardSplit] = useState("0.70");

  const selectedDs =
    datasets.find((d) => `${d.dataset_id}@${d.version}` === datasetKey) ??
    datasets[0];
  const hasDatasets = Boolean(selectedDs);

  const buildRunPayload = (): SimulationRunPayload => ({
    dataset: {
      dataset_id: selectedDs?.dataset_id ?? "",
      version: selectedDs?.version ?? "latest",
    },
    path: {
      mode: pathMode,
      horizon_days: Math.max(10, Number(horizonDays) || 252),
      step_days: Math.max(1, Number(stepDays) || 21),
      block_size_days: Math.max(2, Number(blockSizeDays) || 21),
      num_paths: pathMode === "historical" ? 1 : Math.max(1, Number(numPaths) || 10),
      seed: Number(seed) || 42,
    },
    strategy: {
      name: strategyName || "direct_index",
      sample_size: sampleSize.trim() ? Number(sampleSize) : null,
      allow_fractional: allowFractional,
      tlh_enabled: tlhEnabled,
      tlh_loss_pct_threshold: tlhLossPct,
      tlh_min_loss_dollars: tlhMinDollars,
      tlh_check_frequency_days: Math.max(1, Number(tlhCheckFreq) || 5),
      substitute_mode: substituteMode,
      rebalance_mode: rebalanceMode,
      drift_band_abs: "0.015",
      direct_expense_ratio_bps: directFeeBps,
      etf_expense_ratio_bps: etfFeeBps,
      lot_policy: lotPolicy,
    },
    tax: {
      fed_st_rate: fedStRate,
      fed_lt_rate: fedLtRate,
      niit_rate: niitRate,
      state_st_rate: stateRate,
      state_lt_rate: stateRate,
      ordinary_income_offset_cap: "3000",
      external_st_gains_annual: extStGains,
      external_lt_gains_annual: extLtGains,
      tax_settlement_mode: settlementMode,
      liquidate_at_end: true,
    },
    cashflow: {
      initial_capital: initialCapital,
      periodic_contribution: periodicContribution,
      contribution_frequency_days: 21,
    },
    save_equity_curves: true,
    save_lot_details: true,
  });

  const handleRunSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    onSubmitRun(buildRunPayload());
  };

  const handleSweepClick = () => {
    const base = buildRunPayload();
    base.save_equity_curves = false;
    base.save_lot_details = false;
    const threshList = sweepThresholds
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    const sampleList = sweepSampleSizes
      .split(",")
      .map((s) => Number(s.trim()))
      .filter((n) => Number.isFinite(n) && n > 0);

    const grid: Record<string, (string | number | boolean)[]> = {};
    if (threshList.length > 0) grid["strategy.tlh_loss_pct_threshold"] = threshList;
    if (sampleList.length > 0) grid["strategy.sample_size"] = sampleList;

    const splitVal = walkForwardSplit.trim() ? Number(walkForwardSplit) : null;
    onSubmitSweep({
      name: `${strategyName}_sweep`,
      walk_forward_split_ratio:
        splitVal && splitVal > 0.1 && splitVal < 0.95 ? splitVal : null,
      base,
      grid,
    });
  };

  return (
    <form className="card" onSubmit={handleRunSubmit}>
      <h3 className="card-title">Simulation & Sweep Configuration</h3>

      <div className="form-section-title">Dataset & Path Spec</div>
      <div className="form-field">
        <label>Dataset</label>
        <select
          value={selectedDs ? `${selectedDs.dataset_id}@${selectedDs.version}` : ""}
          onChange={(e) => setDatasetKey(e.target.value)}
          disabled={!hasDatasets}
        >
          {!hasDatasets && (
            <option value="">No datasets cached — run `idx-fetch` first</option>
          )}
          {datasets.map((d) => (
            <option
              key={`${d.dataset_id}@${d.version}`}
              value={`${d.dataset_id}@${d.version}`}
            >
              {d.dataset_id}@{d.version} ({d.ticker_count} stocks,{" "}
              {d.survivorship_biased ? "Survivorship-Biased" : "PIT"})
            </option>
          ))}
        </select>
      </div>
      {!hasDatasets && (
        <div className="banner-warn" style={{ fontSize: "0.78rem" }}>
          No market datasets found in <code>data/</code>. Fetch or generate one offline via CLI:
          <pre
            style={{
              margin: "0.4rem 0 0",
              padding: "0.4rem 0.55rem",
              background: "#0d1426",
              borderRadius: 6,
              overflowX: "auto",
              fontFamily: "var(--font-mono)",
            }}
          >
            uv run idx-fetch pull --dataset-id sp500_public{"\n"}
            uv run idx-fetch synthetic --dataset-id sp500_synthetic_demo
          </pre>
        </div>
      )}

      <div className="form-row">
        <div className="form-field">
          <label>Path Mode</label>
          <select
            value={pathMode}
            onChange={(e) =>
              setPathMode(
                e.target.value as "historical" | "rolling_window" | "block_bootstrap"
              )
            }
          >
            <option value="block_bootstrap">block_bootstrap</option>
            <option value="rolling_window">rolling_window</option>
            <option value="historical">historical</option>
          </select>
        </div>
        <div className="form-field">
          <label>Horizon (Trading Days)</label>
          <input
            type="number"
            min={20}
            value={horizonDays}
            onChange={(e) => setHorizonDays(Number(e.target.value))}
          />
        </div>
      </div>

      {pathMode !== "historical" && (
        <div className="form-row">
          <div className="form-field">
            <label>
              {pathMode === "block_bootstrap" ? "Block Size (Days)" : "Step Stride (Days)"}
            </label>
            <input
              type="number"
              min={2}
              value={pathMode === "block_bootstrap" ? blockSizeDays : stepDays}
              onChange={(e) =>
                pathMode === "block_bootstrap"
                  ? setBlockSizeDays(Number(e.target.value))
                  : setStepDays(Number(e.target.value))
              }
            />
          </div>
          <div className="form-field">
            <label>Paths / Seed</label>
            <div style={{ display: "flex", gap: 4 }}>
              <input
                type="number"
                min={1}
                max={500}
                style={{ width: "55%" }}
                value={numPaths}
                onChange={(e) => setNumPaths(Number(e.target.value))}
              />
              <input
                type="number"
                style={{ width: "45%" }}
                value={seed}
                onChange={(e) => setSeed(Number(e.target.value))}
              />
            </div>
          </div>
        </div>
      )}

      <div className="form-section-title">Direct Index Strategy & Constraints</div>
      <div className="form-row">
        <div className="form-field">
          <label>Strategy Label</label>
          <input
            value={strategyName}
            onChange={(e) => setStrategyName(e.target.value)}
          />
        </div>
        <div className="form-field">
          <label>Top-K Sample (blank = All)</label>
          <input
            type="number"
            placeholder="All constituents"
            value={sampleSize}
            onChange={(e) => setSampleSize(e.target.value)}
          />
        </div>
      </div>

      <div className="form-row">
        <div className="form-field">
          <label>Share Precision</label>
          <select
            value={allowFractional ? "fractional" : "whole"}
            onChange={(e) => setAllowFractional(e.target.value === "fractional")}
          >
            <option value="fractional">Fractional Shares</option>
            <option value="whole">Whole Shares Only (Floor + Remainder)</option>
          </select>
        </div>
        <div className="form-field">
          <label>Tax-Loss Harvesting</label>
          <select
            value={tlhEnabled ? "on" : "off"}
            onChange={(e) => setTlhEnabled(e.target.value === "on")}
          >
            <option value="on">Enabled</option>
            <option value="off">Disabled (Index Tracking Only)</option>
          </select>
        </div>
      </div>

      {tlhEnabled && (
        <div className="form-row">
          <div className="form-field">
            <label>TLH Loss % Trigger</label>
            <input value={tlhLossPct} onChange={(e) => setTlhLossPct(e.target.value)} />
          </div>
          <div className="form-field">
            <label>Min Loss ($) / Freq (d)</label>
            <div style={{ display: "flex", gap: 4 }}>
              <input
                style={{ width: "60%" }}
                value={tlhMinDollars}
                onChange={(e) => setTlhMinDollars(e.target.value)}
              />
              <input
                type="number"
                min={1}
                style={{ width: "40%" }}
                value={tlhCheckFreq}
                onChange={(e) => setTlhCheckFreq(Number(e.target.value))}
              />
            </div>
          </div>
        </div>
      )}

      <div className="form-row">
        <div className="form-field">
          <label>Substitute Mode</label>
          <select
            value={substituteMode}
            onChange={(e) =>
              setSubstituteMode(e.target.value as "sector_peer" | "sector_etf" | "cash")
            }
          >
            <option value="sector_peer">Same-Sector Peer</option>
            <option value="sector_etf">Sector ETF / Proxy</option>
            <option value="cash">Hold Cash During 30d Lockout</option>
          </select>
        </div>
        <div className="form-field">
          <label>Rebalance / Lot Policy</label>
          <div style={{ display: "flex", gap: 4 }}>
            <select
              style={{ width: "62%" }}
              value={rebalanceMode}
              onChange={(e) =>
                setRebalanceMode(
                  e.target.value as
                    | "none"
                    | "monthly"
                    | "quarterly"
                    | "annual"
                    | "drift_band"
                )
              }
            >
              <option value="quarterly">Quarterly</option>
              <option value="monthly">Monthly</option>
              <option value="annual">Annual</option>
              <option value="drift_band">Drift Band</option>
              <option value="none">None</option>
            </select>
            <select
              style={{ width: "38%" }}
              value={lotPolicy}
              onChange={(e) => setLotPolicy(e.target.value as "HIFO" | "FIFO")}
            >
              <option value="HIFO">HIFO</option>
              <option value="FIFO">FIFO</option>
            </select>
          </div>
        </div>
      </div>

      <div className="form-row">
        <div className="form-field">
          <label>Direct Fee (bps)</label>
          <input value={directFeeBps} onChange={(e) => setDirectFeeBps(e.target.value)} />
        </div>
        <div className="form-field">
          <label>Paired ETF Fee (bps)</label>
          <input value={etfFeeBps} onChange={(e) => setEtfFeeBps(e.target.value)} />
        </div>
      </div>

      <div className="form-section-title">Tax Rates, External Gains & Capital</div>
      <div className="form-row">
        <div className="form-field">
          <label>Fed ST / LT Rates</label>
          <div style={{ display: "flex", gap: 4 }}>
            <input
              style={{ width: "50%" }}
              value={fedStRate}
              onChange={(e) => setFedStRate(e.target.value)}
            />
            <input
              style={{ width: "50%" }}
              value={fedLtRate}
              onChange={(e) => setFedLtRate(e.target.value)}
            />
          </div>
        </div>
        <div className="form-field">
          <label>NIIT / State Rate</label>
          <div style={{ display: "flex", gap: 4 }}>
            <input
              style={{ width: "50%" }}
              value={niitRate}
              onChange={(e) => setNiitRate(e.target.value)}
            />
            <input
              style={{ width: "50%" }}
              value={stateRate}
              onChange={(e) => setStateRate(e.target.value)}
            />
          </div>
        </div>
      </div>

      <div className="form-row">
        <div className="form-field">
          <label>External ST Gains / Yr ($)</label>
          <input value={extStGains} onChange={(e) => setExtStGains(e.target.value)} />
        </div>
        <div className="form-field">
          <label>External LT Gains / Yr ($)</label>
          <input value={extLtGains} onChange={(e) => setExtLtGains(e.target.value)} />
        </div>
      </div>

      <div className="form-field">
        <label>Annual Tax Settlement Mode</label>
        <select
          value={settlementMode}
          onChange={(e) =>
            setSettlementMode(
              e.target.value as "reinvest_and_pay_from_portfolio" | "external_cash"
            )
          }
        >
          <option value="reinvest_and_pay_from_portfolio">
            Reinvest Tax Savings & Pay Taxes from Portfolio
          </option>
          <option value="external_cash">External Tax Account (No Portfolio Reinvestment)</option>
        </select>
      </div>

      <div className="form-row">
        <div className="form-field">
          <label>Initial Capital ($)</label>
          <input
            value={initialCapital}
            onChange={(e) => setInitialCapital(e.target.value)}
          />
        </div>
        <div className="form-field">
          <label>Monthly Deposit ($)</label>
          <input
            value={periodicContribution}
            onChange={(e) => setPeriodicContribution(e.target.value)}
          />
        </div>
      </div>

      <button
        type="submit"
        className="btn"
        style={{ width: "100%", marginTop: 6 }}
        disabled={busy || !hasDatasets}
      >
        {busy ? "Running Job..." : "Run Paired Simulation"}
      </button>

      <div className="form-section-title">Grid Sensitivity Sweep (Plateau Map)</div>
      <div className="form-field">
        <label>TLH Loss % Grid (comma-separated)</label>
        <input
          value={sweepThresholds}
          onChange={(e) => setSweepThresholds(e.target.value)}
        />
      </div>
      <div className="form-row">
        <div className="form-field">
          <label>Sample Sizes Grid</label>
          <input
            value={sweepSampleSizes}
            onChange={(e) => setSweepSampleSizes(e.target.value)}
          />
        </div>
        <div className="form-field">
          <label>Walk-Forward Split</label>
          <input
            value={walkForwardSplit}
            onChange={(e) => setWalkForwardSplit(e.target.value)}
          />
        </div>
      </div>
      <button
        type="button"
        className="btn btn-secondary"
        style={{ width: "100%" }}
        disabled={busy || !hasDatasets}
        onClick={handleSweepClick}
      >
        Launch Sensitivity Sweep
      </button>
    </form>
  );
}
