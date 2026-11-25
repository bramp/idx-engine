import { useState } from "react";
import type { EquityBandPoint, RunPathEntry } from "../api/client";

interface Props {
  bands: EquityBandPoint[];
  paths: RunPathEntry[];
  pathMode?: string;
}

function fmtSignedUsd(v: number): string {
  const sign = v >= 0 ? "+" : "-";
  return `${sign}$${Math.round(Math.abs(v)).toLocaleString()}`;
}

export function PairedDeltaChart({
  bands,
  paths,
  pathMode = "historical",
}: Props) {
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  if (bands.length < 2) {
    return (
      <div className="card">
        <h3 className="card-title">Paired Outperformance vs. ETF (ΔWp)</h3>
        <p style={{ color: "var(--muted)", fontSize: "0.85rem" }}>
          No paired delta series available.
        </p>
      </div>
    );
  }

  const width = 640;
  const height = 270;
  const pad = { top: 16, right: 16, bottom: 28, left: 74 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;

  let minY = 0;
  let maxY = 0;
  for (const b of bands) {
    minY = Math.min(minY, b.delta_p10);
    maxY = Math.max(maxY, b.delta_p90);
  }
  if (maxY - minY < 10) {
    minY -= 50;
    maxY += 50;
  }

  const xAt = (i: number) => pad.left + (i / (bands.length - 1)) * plotW;
  const yAt = (v: number) => pad.top + plotH - ((v - minY) / (maxY - minY)) * plotH;

  const step = Math.max(1, Math.floor(bands.length / 240));
  const idxs: number[] = [];
  for (let i = 0; i < bands.length; i += step) idxs.push(i);
  if (idxs[idxs.length - 1] !== bands.length - 1) idxs.push(bands.length - 1);

  const areaPath = (loKey: "delta_p10" | "delta_p25", hiKey: "delta_p90" | "delta_p75") => {
    const top = idxs.map((i) => `${xAt(i).toFixed(1)},${yAt(bands[i][hiKey]).toFixed(1)}`);
    const bot = [...idxs]
      .reverse()
      .map((i) => `${xAt(i).toFixed(1)},${yAt(bands[i][loKey]).toFixed(1)}`);
    return `M ${top.join(" L ")} L ${bot.join(" L ")} Z`;
  };

  const medianLine =
    "M " + idxs.map((i) => `${xAt(i).toFixed(1)},${yAt(bands[i].delta_p50).toFixed(1)}`).join(" L ");

  const active = hoverIdx !== null ? bands[hoverIdx] : bands[bands.length - 1];
  const isHistorical = pathMode === "historical";
  const activeXLabel = isHistorical ? active.date : `Day ${active.day_index + 1}`;
  const yZero = yAt(0);
  const wins = paths.filter((p) => p.paired_liquidation_delta > 0).length;
  const sortedPostLiq = [...paths]
    .map((p) => p.paired_liquidation_delta)
    .sort((a, b) => a - b);
  const medianPostLiq =
    sortedPostLiq.length > 0
      ? sortedPostLiq[Math.floor(sortedPostLiq.length / 2)]
      : 0;

  return (
    <div className="card">
      <div className="card-header">
        <h3 className="card-title">Pre-Liquidation Paired Wealth Delta vs. ETF (ΔWp)</h3>
        <span style={{ fontSize: "0.76rem", color: "var(--muted)", fontFamily: "var(--font-mono)" }}>
          {activeXLabel} · Pre-Liq P50 {fmtSignedUsd(active.delta_p50)} (P10{" "}
          {fmtSignedUsd(active.delta_p10)} / P90 {fmtSignedUsd(active.delta_p90)})
        </span>
      </div>

      <svg
        viewBox={`0 0 ${width} ${height}`}
        style={{ width: "100%", height: "auto", display: "block" }}
        onMouseMove={(e) => {
          const rect = e.currentTarget.getBoundingClientRect();
          const relX = ((e.clientX - rect.left) / rect.width) * width - pad.left;
          const ratio = Math.max(0, Math.min(1, relX / plotW));
          setHoverIdx(Math.round(ratio * (bands.length - 1)));
        }}
        onMouseLeave={() => setHoverIdx(null)}
      >
        {[0, 0.25, 0.5, 0.75, 1].map((t, idx) => {
          const tv = minY + t * (maxY - minY);
          return (
            <g key={idx}>
              <line
                x1={pad.left}
                x2={width - pad.right}
                y1={yAt(tv)}
                y2={yAt(tv)}
                stroke="#1f2c4d"
                strokeDasharray="3 3"
              />
              <text x={pad.left - 6} y={yAt(tv) + 4} textAnchor="end" fill="#93a4c3" fontSize="10">
                {fmtSignedUsd(tv)}
              </text>
            </g>
          );
        })}

        <line
          x1={pad.left}
          x2={width - pad.right}
          y1={yZero}
          y2={yZero}
          stroke="#64748b"
          strokeWidth="1.3"
        />

        <path d={areaPath("delta_p10", "delta_p90")} fill="rgba(34, 197, 94, 0.13)" />
        <path d={areaPath("delta_p25", "delta_p75")} fill="rgba(34, 197, 94, 0.25)" />
        <path d={medianLine} fill="none" stroke="#22c55e" strokeWidth="2.2" />

        {hoverIdx !== null && (
          <line
            x1={xAt(hoverIdx)}
            x2={xAt(hoverIdx)}
            y1={pad.top}
            y2={height - pad.bottom}
            stroke="#93a4c3"
            strokeWidth="1"
          />
        )}

        <text x={pad.left} y={height - 8} fill="#93a4c3" fontSize="10">
          {isHistorical ? bands[0].date : "Day 1"}
        </text>
        <text x={width - pad.right} y={height - 8} textAnchor="end" fill="#93a4c3" fontSize="10">
          {isHistorical ? bands[bands.length - 1].date : `Day ${bands.length}`}
        </text>
      </svg>

      <div style={{ fontSize: "0.75rem", color: "var(--muted)", marginTop: "0.4rem" }}>
        Curve shows pre-liquidation ΔWp; post-liquidation P50 ΔWp is{" "}
        <strong>{fmtSignedUsd(medianPostLiq)}</strong> ({wins} / {paths.length} paths win after
        terminal liquidation tax).
      </div>
    </div>
  );
}
