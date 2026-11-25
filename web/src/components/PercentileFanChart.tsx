import { useState } from "react";
import type { EquityBandPoint } from "../api/client";

interface Props {
  bands: EquityBandPoint[];
  strategyName: string;
  benchmarkName: string;
  pathMode?: string;
}

function fmtUsd(v: number): string {
  return `$${Math.round(v).toLocaleString()}`;
}

export function PercentileFanChart({
  bands,
  strategyName,
  benchmarkName,
  pathMode = "historical",
}: Props) {
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  if (bands.length < 2) {
    return (
      <div className="card">
        <h3 className="card-title">After-Tax Wealth Percentile Fan Band</h3>
        <p style={{ color: "var(--muted)", fontSize: "0.85rem" }}>
          No equity band points recorded for this run.
        </p>
      </div>
    );
  }

  const width = 640;
  const height = 270;
  const pad = { top: 16, right: 16, bottom: 28, left: 68 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;

  let minY = Infinity;
  let maxY = -Infinity;
  for (const b of bands) {
    minY = Math.min(minY, b.strat_p10, b.bench_p50);
    maxY = Math.max(maxY, b.strat_p90, b.bench_p50);
  }
  if (maxY <= minY) {
    maxY = minY + 1;
  }

  const xAt = (i: number) => pad.left + (i / (bands.length - 1)) * plotW;
  const yAt = (v: number) => pad.top + plotH - ((v - minY) / (maxY - minY)) * plotH;

  // Downsample up to ~250 SVG vertices for crisp fast rendering
  const step = Math.max(1, Math.floor(bands.length / 240));
  const idxs: number[] = [];
  for (let i = 0; i < bands.length; i += step) idxs.push(i);
  if (idxs[idxs.length - 1] !== bands.length - 1) idxs.push(bands.length - 1);

  const areaPath = (loKey: "strat_p10" | "strat_p25", hiKey: "strat_p90" | "strat_p75") => {
    const top = idxs.map((i) => `${xAt(i).toFixed(1)},${yAt(bands[i][hiKey]).toFixed(1)}`);
    const bot = [...idxs]
      .reverse()
      .map((i) => `${xAt(i).toFixed(1)},${yAt(bands[i][loKey]).toFixed(1)}`);
    return `M ${top.join(" L ")} L ${bot.join(" L ")} Z`;
  };

  const linePath = (key: "strat_p50" | "bench_p50") =>
    "M " + idxs.map((i) => `${xAt(i).toFixed(1)},${yAt(bands[i][key]).toFixed(1)}`).join(" L ");

  const yTicks = [0, 0.25, 0.5, 0.75, 1].map((t) => minY + t * (maxY - minY));
  const active = hoverIdx !== null ? bands[hoverIdx] : bands[bands.length - 1];
  const isHistorical = pathMode === "historical";
  const activeXLabel = isHistorical ? active.date : `Day ${active.day_index + 1}`;

  return (
    <div className="card">
      <div className="card-header">
        <h3 className="card-title">Pre-Liquidation After-Tax Wealth Fan Band</h3>
        <span style={{ fontSize: "0.76rem", color: "var(--muted)", fontFamily: "var(--font-mono)" }}>
          {activeXLabel} · P50 {fmtUsd(active.strat_p50)} vs ETF {fmtUsd(active.bench_p50)}
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
        {yTicks.map((tv, idx) => (
          <g key={idx}>
            <line
              x1={pad.left}
              x2={width - pad.right}
              y1={yAt(tv)}
              y2={yAt(tv)}
              stroke="#1f2c4d"
              strokeDasharray="3 3"
            />
            <text
              x={pad.left - 6}
              y={yAt(tv) + 4}
              textAnchor="end"
              fill="#93a4c3"
              fontSize="10"
            >
              {fmtUsd(tv)}
            </text>
          </g>
        ))}

        <path d={areaPath("strat_p10", "strat_p90")} fill="rgba(56, 189, 248, 0.14)" />
        <path d={areaPath("strat_p25", "strat_p75")} fill="rgba(56, 189, 248, 0.26)" />
        <path
          d={linePath("bench_p50")}
          fill="none"
          stroke="#f59e0b"
          strokeWidth="1.8"
          strokeDasharray="5 3"
        />
        <path d={linePath("strat_p50")} fill="none" stroke="#38bdf8" strokeWidth="2.2" />

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

      <div
        style={{
          display: "flex",
          gap: "1rem",
          fontSize: "0.75rem",
          color: "var(--muted)",
          marginTop: "0.4rem",
          flexWrap: "wrap",
        }}
      >
        <span>
          <strong style={{ color: "#38bdf8" }}>━━</strong> {strategyName} Median (P50) + IQR/P10-P90
        </span>
        <span>
          <strong style={{ color: "#f59e0b" }}>╌╌</strong> {benchmarkName} Median (P50)
        </span>
      </div>
    </div>
  );
}
