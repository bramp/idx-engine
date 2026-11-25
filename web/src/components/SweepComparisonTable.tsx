import type { SweepDetailResponse } from "../api/client";

interface Props {
  sweep: SweepDetailResponse;
}

function fmtUsdStr(val: string | number): string {
  const num = typeof val === "string" ? Number(val) : val;
  const sign = num >= 0 ? "+" : "-";
  return `${sign}$${Math.round(Math.abs(num)).toLocaleString()}`;
}

export function SweepComparisonTable({ sweep }: Props) {
  const sorted = [...sweep.rows].sort(
    (a, b) => (b.plateau_score ?? -Infinity) - (a.plateau_score ?? -Infinity)
  );
  const hasOos = sweep.walk_forward_split_ratio !== null;

  return (
    <div className="card">
      <div className="card-header">
        <div>
          <h3 className="card-title">
            Sensitivity Sweep: {sweep.name} ({sweep.sweep_id})
          </h3>
          <div style={{ fontSize: "0.77rem", color: "var(--muted)", marginTop: "0.15rem" }}>
            Ranked by neighborhood <strong>Plateau Score</strong> (Rule #6: favoring broad stable parameter
            plateaus over isolated in-sample spikes).
          </div>
        </div>
        {hasOos && (
          <span className="badge badge-info">
            Walk-Forward Split: {Math.round((sweep.walk_forward_split_ratio ?? 0.7) * 100)}% IS /{" "}
            {Math.round((1 - (sweep.walk_forward_split_ratio ?? 0.7)) * 100)}% OOS
          </span>
        )}
      </div>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Parameters</th>
              <th className="num">Plateau Score</th>
              <th className="num">IS Win Rate</th>
              <th className="num">IS Median ΔWp</th>
              <th className="num">IS P10 ΔWp</th>
              <th className="num">IS TE (bps)</th>
              <th className="num">Lots/Yr</th>
              {hasOos && <th className="num">OOS Win Rate</th>}
              {hasOos && <th className="num">OOS Median ΔWp</th>}
            </tr>
          </thead>
          <tbody>
            {sorted.map((row, idx) => {
              const isDelta = Number(row.in_sample.median_paired_wealth_delta);
              const oosDelta = row.out_of_sample
                ? Number(row.out_of_sample.median_paired_wealth_delta)
                : null;
              const paramLabel =
                Object.entries(row.params)
                  .map(([k, v]) => `${k.split(".").pop()}=${String(v)}`)
                  .join(", ") || "(base)";

              return (
                <tr key={row.run_id} className={idx === 0 ? "selected-row" : ""}>
                  <td style={{ fontFamily: "var(--font-mono)" }}>
                    {idx === 0 && <span className="badge badge-ok" style={{ marginRight: 6 }}>TOP PLATEAU</span>}
                    {paramLabel}
                  </td>
                  <td className="num">{row.plateau_score?.toLocaleString() ?? "-"}</td>
                  <td className="num">
                    {(row.in_sample.win_rate_vs_benchmark_liquidation * 100).toFixed(1)}%
                  </td>
                  <td className={`num ${isDelta >= 0 ? "pos" : "neg"}`}>{fmtUsdStr(isDelta)}</td>
                  <td className="num">{fmtUsdStr(row.in_sample.p10_paired_wealth_delta)}</td>
                  <td className="num">{row.in_sample.median_tracking_error_bps.toFixed(1)}</td>
                  <td className="num">{row.in_sample.median_closed_lots_per_year.toFixed(1)}</td>
                  {hasOos && (
                    <td className="num">
                      {row.out_of_sample
                        ? `${(row.out_of_sample.win_rate_vs_benchmark_liquidation * 100).toFixed(1)}%`
                        : "-"}
                    </td>
                  )}
                  {hasOos && (
                    <td className={`num ${(oosDelta ?? 0) >= 0 ? "pos" : "neg"}`}>
                      {oosDelta !== null ? fmtUsdStr(oosDelta) : "-"}
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
