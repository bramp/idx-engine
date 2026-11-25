import type { ClosedLotItem, WashSaleItem } from "../api/client";

interface Props {
  lots: ClosedLotItem[];
  washSales: WashSaleItem[];
}

function fmtMoney(v: number): string {
  const sign = v < 0 ? "-" : "";
  return `${sign}$${Math.abs(v).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

export function TaxLotTable({ lots, washSales }: Props) {
  return (
    <>
      <div className="card">
        <div className="card-header">
          <h3 className="card-title">Closed Tax Lots ({lots.length})</h3>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Sold Date</th>
                <th>Acquired</th>
                <th>Ticker</th>
                <th>Sector</th>
                <th>Term</th>
                <th className="num">Qty</th>
                <th className="num">Proceeds</th>
                <th className="num">Cost Basis</th>
                <th className="num">Raw Gain/Loss</th>
                <th className="num">Wash Disallowed</th>
                <th className="num">Adjusted Gain/Loss</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {lots.length === 0 ? (
                <tr>
                  <td colSpan={12} style={{ color: "var(--muted)", textAlign: "center" }}>
                    No closed tax lots match the current filters.
                  </td>
                </tr>
              ) : (
                lots.map((lot) => (
                  <tr key={`${lot.path_id}_${lot.close_id}`}>
                    <td>{lot.sold_date}</td>
                    <td>{lot.acquired_date}</td>
                    <td style={{ fontWeight: 600 }}>{lot.ticker}</td>
                    <td>{lot.sector}</td>
                    <td>
                      <span className={`badge ${lot.term === "LT" ? "badge-ok" : "badge-info"}`}>
                        {lot.term} ({lot.holding_days}d)
                      </span>
                    </td>
                    <td className="num">{lot.quantity.toFixed(4)}</td>
                    <td className="num">{fmtMoney(lot.proceeds_total)}</td>
                    <td className="num">{fmtMoney(lot.cost_basis_total)}</td>
                    <td className={`num ${lot.raw_realized_gain_loss >= 0 ? "pos" : "neg"}`}>
                      {fmtMoney(lot.raw_realized_gain_loss)}
                    </td>
                    <td className={`num ${lot.disallowed_wash_loss > 0 ? "neg" : ""}`}>
                      {fmtMoney(lot.disallowed_wash_loss)}
                    </td>
                    <td className={`num ${lot.adjusted_realized_gain_loss >= 0 ? "pos" : "neg"}`}>
                      {fmtMoney(lot.adjusted_realized_gain_loss)}
                    </td>
                    <td style={{ fontFamily: "var(--font-mono)", fontSize: "0.75rem" }}>
                      {lot.reason}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      <div className="card">
        <div className="card-header">
          <h3 className="card-title">30-Day Wash-Sale Basis Rollovers ({washSales.length})</h3>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Sold Date</th>
                <th>Loss Ticker</th>
                <th>Replacement Date</th>
                <th>Replacement Ticker</th>
                <th>Replacement Lot</th>
                <th className="num">Matched Qty</th>
                <th className="num">Disallowed Loss Rolled</th>
                <th className="num">Tacked Holding Days</th>
              </tr>
            </thead>
            <tbody>
              {washSales.length === 0 ? (
                <tr>
                  <td colSpan={8} style={{ color: "var(--muted)", textAlign: "center" }}>
                    Zero wash-sale disallowances triggered on this selection.
                  </td>
                </tr>
              ) : (
                washSales.map((ws) => (
                  <tr key={`${ws.path_id}_${ws.adjustment_id}`}>
                    <td>{ws.sold_date}</td>
                    <td style={{ fontWeight: 600 }}>{ws.loss_ticker}</td>
                    <td>{ws.replacement_date}</td>
                    <td style={{ fontWeight: 600 }}>{ws.replacement_ticker}</td>
                    <td style={{ fontFamily: "var(--font-mono)", fontSize: "0.75rem" }}>
                      {ws.replacement_lot_id}
                    </td>
                    <td className="num">{ws.matched_quantity.toFixed(4)}</td>
                    <td className="num neg">{fmtMoney(ws.disallowed_loss)}</td>
                    <td className="num">{ws.tacked_holding_days}d</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}
