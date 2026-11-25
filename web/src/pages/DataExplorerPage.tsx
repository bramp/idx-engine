import { useEffect, useState } from "react";
import {
  apiClient,
  type DatasetDetailResponse,
  type DatasetMetadata,
  type TickerSeriesResponse,
} from "../api/client";

interface Props {
  datasets: DatasetMetadata[];
  onRefreshDatasets: () => Promise<void>;
}

export function DataExplorerPage({ datasets, onRefreshDatasets }: Props) {
  const [selectedId, setSelectedId] = useState<string>(
    datasets[0]?.dataset_id ?? ""
  );
  const [selectedVersion, setSelectedVersion] = useState<string>(
    datasets[0]?.version ?? "latest"
  );
  const [detail, setDetail] = useState<DatasetDetailResponse | null>(null);
  const [selectedTicker, setSelectedTicker] = useState<string>("");
  const [series, setSeries] = useState<TickerSeriesResponse | null>(null);
  const [search, setSearch] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!selectedId && datasets.length > 0) {
      setSelectedId(datasets[0].dataset_id);
      setSelectedVersion(datasets[0].version);
    }
  }, [datasets, selectedId]);

  useEffect(() => {
    if (!selectedId) return;
    let cancelled = false;
    setError(null);
    apiClient
      .inspectDataset(selectedId, selectedVersion)
      .then((res) => {
        if (cancelled) return;
        setDetail(res);
        const defaultSym =
          res.constituents[0]?.ticker ?? res.metadata.benchmark_ticker;
        setSelectedTicker(defaultSym);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId, selectedVersion]);

  useEffect(() => {
    if (!selectedId || !selectedTicker) return;
    let cancelled = false;
    apiClient
      .getTickerSeries(selectedId, selectedTicker, selectedVersion)
      .then((res) => {
        if (!cancelled) setSeries(res);
      })
      .catch(() => {
        if (!cancelled) setSeries(null);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId, selectedVersion, selectedTicker]);

  const filteredConstituents =
    detail?.constituents.filter(
      (c) =>
        c.ticker.toLowerCase().includes(search.toLowerCase()) ||
        c.name.toLowerCase().includes(search.toLowerCase()) ||
        c.sector.toLowerCase().includes(search.toLowerCase())
    ) ?? [];

  // Render mini SVG price chart
  const renderSeriesChart = () => {
    if (!series || series.points.length < 2) return null;
    const pts = series.points;
    const w = 680;
    const h = 210;
    const pad = { top: 14, right: 16, bottom: 24, left: 58 };
    const pw = w - pad.left - pad.right;
    const ph = h - pad.top - pad.bottom;

    let minP = Infinity;
    let maxP = -Infinity;
    for (const p of pts) {
      minP = Math.min(minP, p.price);
      maxP = Math.max(maxP, p.price);
    }
    if (maxP <= minP) maxP = minP + 1;
    const step = Math.max(1, Math.floor(pts.length / 260));
    const sampled = pts.filter((_, i) => i % step === 0 || i === pts.length - 1);
    const dPath =
      "M " +
      sampled
        .map((pt, idx) => {
          const origIdx = idx === sampled.length - 1 ? pts.length - 1 : idx * step;
          const x = pad.left + (origIdx / (pts.length - 1)) * pw;
          const y = pad.top + ph - ((pt.price - minP) / (maxP - minP)) * ph;
          return `${x.toFixed(1)},${y.toFixed(1)}`;
        })
        .join(" L ");

    const divDays = pts.filter((p) => p.dividend > 0).length;

    return (
      <div className="card">
        <div className="card-header">
          <h3 className="card-title">
            {series.ticker} ({series.sector}) — Point-in-Time Price Series
          </h3>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <button
              type="button"
              className="btn btn-secondary"
              style={{ padding: "0.28rem 0.6rem", fontSize: "0.75rem" }}
              onClick={() =>
                detail && setSelectedTicker(detail.metadata.benchmark_ticker)
              }
            >
              Inspect Benchmark ({detail?.metadata.benchmark_ticker})
            </button>
            <span className="badge badge-info">{divDays} Dividend Ex-Dates</span>
          </div>
        </div>
        <svg viewBox={`0 0 ${w} ${h}`} style={{ width: "100%", height: "auto" }}>
          {[0, 0.5, 1].map((t, i) => {
            const val = minP + t * (maxP - minP);
            const y = pad.top + ph - t * ph;
            return (
              <g key={i}>
                <line
                  x1={pad.left}
                  x2={w - pad.right}
                  y1={y}
                  y2={y}
                  stroke="#1f2c4d"
                  strokeDasharray="3 3"
                />
                <text x={pad.left - 6} y={y + 4} textAnchor="end" fill="#93a4c3" fontSize="10">
                  ${val.toFixed(0)}
                </text>
              </g>
            );
          })}
          <path d={dPath} fill="none" stroke="#38bdf8" strokeWidth="2" />
          <text x={pad.left} y={h - 6} fill="#93a4c3" fontSize="10">
            {pts[0].date}
          </text>
          <text x={w - pad.right} y={h - 6} textAnchor="end" fill="#93a4c3" fontSize="10">
            {pts[pts.length - 1].date}
          </text>
        </svg>
      </div>
    );
  };

  return (
    <div>
      {error && <div className="banner-error">{error}</div>}

      <div className="grid-2">
        <div>
          <div className="card">
            <div className="card-header" style={{ marginBottom: "0.75rem" }}>
              <h3 className="card-title">Cached Market Datasets ({datasets.length})</h3>
              <button
                type="button"
                className="btn btn-secondary"
                style={{ padding: "0.25rem 0.55rem", fontSize: "0.75rem" }}
                onClick={() => void onRefreshDatasets()}
              >
                Refresh
              </button>
            </div>
            {datasets.length === 0 ? (
              <p style={{ fontSize: "0.82rem", color: "var(--muted)", margin: 0 }}>
                No datasets cached in <code>data/</code> yet.
              </p>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Dataset</th>
                      <th className="num">Days</th>
                      <th>Bias</th>
                    </tr>
                  </thead>
                  <tbody>
                    {datasets.map((d) => {
                      const isSel =
                        d.dataset_id === selectedId && d.version === selectedVersion;
                      return (
                        <tr
                          key={`${d.dataset_id}@${d.version}`}
                          className={`clickable ${isSel ? "selected-row" : ""}`}
                          onClick={() => {
                            setSelectedId(d.dataset_id);
                            setSelectedVersion(d.version);
                          }}
                        >
                          <td style={{ fontFamily: "var(--font-mono)" }}>
                            {d.dataset_id}@{d.version}
                          </td>
                          <td className="num">{d.trading_days}</td>
                          <td>
                            {d.survivorship_biased ? (
                              <span className="badge badge-warn">BIASED</span>
                            ) : (
                              <span className="badge badge-ok">PIT</span>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div className="card">
            <h3 className="card-title">Fetch Datasets via CLI</h3>
            <p style={{ fontSize: "0.78rem", color: "var(--muted)", margin: "0.4rem 0 0.55rem" }}>
              Dataset ingestion runs offline as a one-time CLI step via <code>idx-fetch</code>, then
              click <strong>Refresh</strong> above:
            </p>
            <pre
              style={{
                margin: 0,
                padding: "0.55rem 0.65rem",
                background: "#0d1426",
                border: "1px solid var(--border)",
                borderRadius: 6,
                fontSize: "0.75rem",
                overflowX: "auto",
                fontFamily: "var(--font-mono)",
              }}
            >
              uv run idx-fetch pull --dataset-id sp500_public{"\n"}
              uv run idx-fetch synthetic --dataset-id sp500_synthetic_demo{"\n"}
              uv run idx-fetch import --dataset-id sp500_pit ...
            </pre>
          </div>
        </div>

        <div>
          {!detail && datasets.length === 0 && (
            <div className="card">
              <h3 className="card-title">No Market Dataset Loaded</h3>
              <p style={{ fontSize: "0.85rem", color: "var(--muted)" }}>
                Run one of the <code>idx-fetch</code> commands on the left to populate{" "}
                <code>data/</code>, then click <strong>Refresh</strong> to inspect constituent
                weights, corporate actions, and price series.
              </p>
            </div>
          )}
          {detail && (
            <>
              {detail.metadata.survivorship_biased && (
                <div className="banner-warn">
                  <strong>Survivorship-Bias Warning (Analytical Rule #4):</strong> Dataset{" "}
                  <code>
                    {detail.metadata.dataset_id}@{detail.metadata.version}
                  </code>{" "}
                  was built from current index membership without delisted point-in-time
                  constituents. Simulation runs on this dataset are automatically flagged{" "}
                  <code>exploratory_survivorship_biased</code>.
                </div>
              )}

              <div className="kpi-grid">
                <div className="kpi-card">
                  <div className="kpi-label">Dataset Version</div>
                  <div className="kpi-value">
                    {detail.metadata.dataset_id}@{detail.metadata.version}
                  </div>
                  <div className="kpi-sub">{detail.metadata.source}</div>
                </div>
                <div className="kpi-card">
                  <div className="kpi-label">Date Range</div>
                  <div className="kpi-value" style={{ fontSize: "0.95rem" }}>
                    {detail.metadata.start_date} → {detail.metadata.end_date}
                  </div>
                  <div className="kpi-sub">{detail.metadata.trading_days} trading days</div>
                </div>
                <div className="kpi-card">
                  <div className="kpi-label">Constituents / Benchmark</div>
                  <div className="kpi-value">
                    {detail.metadata.ticker_count} / {detail.metadata.benchmark_ticker}
                  </div>
                  <div className="kpi-sub">
                    Missing bars: {(detail.metadata.missing_bar_fraction * 100).toFixed(2)}%
                  </div>
                </div>
                <div className="kpi-card">
                  <div className="kpi-label">Corporate Actions</div>
                  <div className="kpi-value">{detail.corporate_actions.length}</div>
                  <div className="kpi-sub">Explicit splits / spin-offs / renames</div>
                </div>
              </div>

              {renderSeriesChart()}

              <div className="card">
                <div className="card-header">
                  <h3 className="card-title">GICS Sector Index Weights</h3>
                </div>
                <div style={{ display: "grid", gap: "0.45rem" }}>
                  {detail.sectors.map((s) => (
                    <div
                      key={s.sector}
                      style={{
                        display: "grid",
                        gridTemplateColumns: "200px 1fr 64px",
                        alignItems: "center",
                        gap: "0.75rem",
                        fontSize: "0.8rem",
                      }}
                    >
                      <span>{s.sector}</span>
                      <div className="progress-bar-outer" style={{ marginTop: 0 }}>
                        <div
                          className="progress-bar-inner"
                          style={{ width: `${Math.min(100, s.weight * 100 * 2.5)}%` }}
                        />
                      </div>
                      <span style={{ fontFamily: "var(--font-mono)", textAlign: "right" }}>
                        {(s.weight * 100).toFixed(1)}%
                      </span>
                    </div>
                  ))}
                </div>
              </div>

              <div className="card">
                <div className="card-header">
                  <h3 className="card-title">
                    Constituent Weights, Prices & Dividends (Click row to plot)
                  </h3>
                  <input
                    placeholder="Filter ticker / sector..."
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                  />
                </div>
                <div className="table-wrap" style={{ maxHeight: 380, overflowY: "auto" }}>
                  <table>
                    <thead>
                      <tr>
                        <th>Ticker</th>
                        <th>Company</th>
                        <th>Sector</th>
                        <th className="num">Start Weight</th>
                        <th className="num">End Weight</th>
                        <th className="num">Start Px</th>
                        <th className="num">End Px</th>
                        <th className="num">Divs/Sh</th>
                      </tr>
                    </thead>
                    <tbody>
                      {filteredConstituents.map((c) => (
                        <tr
                          key={c.ticker}
                          className={`clickable ${selectedTicker === c.ticker ? "selected-row" : ""}`}
                          onClick={() => setSelectedTicker(c.ticker)}
                        >
                          <td style={{ fontWeight: 700, fontFamily: "var(--font-mono)" }}>
                            {c.ticker}
                          </td>
                          <td>{c.name}</td>
                          <td>{c.sector}</td>
                          <td className="num">{(c.start_weight * 100).toFixed(2)}%</td>
                          <td className="num">{(c.end_weight * 100).toFixed(2)}%</td>
                          <td className="num">${c.start_price.toFixed(2)}</td>
                          <td className="num">${c.end_price.toFixed(2)}</td>
                          <td className="num">${c.total_dividends_per_share.toFixed(2)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
