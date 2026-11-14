import { useState } from "react";
import { api } from "../api.js";
import { ErrorBanner, StatusBadge, formatMoney, usePolling, TERMINAL } from "./common.jsx";

const PAGE = 25;
const STATUSES = ["", "queued", "processing", "completed", "needs_review", "failed"];

export default function DocumentList({ refreshKey, onOpen }) {
  const [filters, setFilters] = useState({ status: "", supplier: "", currency: "", invoice_number: "" });
  const [offset, setOffset] = useState(0);
  const params = { ...filters, limit: PAGE, offset };
  const [page, error, refresh] = usePolling(() => api.list(params), [JSON.stringify(params), refreshKey], 3000);

  const update = (key) => (e) => {
    setOffset(0);
    setFilters((f) => ({ ...f, [key]: e.target.value }));
  };
  const total = page?.total ?? 0;
  const hasActive = (page?.items || []).some((d) => !TERMINAL.has(d.status));

  return (
    <section className="card">
      <div className="card-head">
        <h2>Documents</h2>
        <span className="muted small">{hasActive ? "Updating…" : ""} {total} total</span>
      </div>

      <div className="filters">
        <label>Status
          <select value={filters.status} onChange={update("status")}>
            {STATUSES.map((s) => <option key={s} value={s}>{s ? s.replace("_", " ") : "All"}</option>)}
          </select>
        </label>
        <label>Supplier<input value={filters.supplier} onChange={update("supplier")} placeholder="contains…" /></label>
        <label>Invoice no.<input value={filters.invoice_number} onChange={update("invoice_number")} placeholder="contains…" /></label>
        <label>Currency<input value={filters.currency} onChange={update("currency")} maxLength={3} placeholder="USD" /></label>
        <button className="ghost" onClick={refresh}>Refresh</button>
      </div>

      <ErrorBanner error={error} />

      {!page ? (
        <p className="muted">Loading…</p>
      ) : page.items.length === 0 ? (
        <p className="muted">No documents match these filters.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>File</th><th>Status</th><th>Invoice</th><th>Supplier</th><th>Date</th><th className="num">Total</th><th>Issues</th>
              </tr>
            </thead>
            <tbody>
              {page.items.map((d) => (
                <tr key={d.id} onClick={() => onOpen(d.id)} className="clickable" tabIndex={0}
                    onKeyDown={(e) => e.key === "Enter" && onOpen(d.id)}>
                  <td className="truncate" title={d.filename}>{d.filename}</td>
                  <td><StatusBadge status={d.status} /></td>
                  <td>{d.invoice_number || <span className="muted">—</span>}</td>
                  <td className="truncate">{d.supplier_name || <span className="muted">—</span>}</td>
                  <td>{d.invoice_date || <span className="muted">—</span>}</td>
                  <td className="num">{formatMoney(d.total, d.currency)}</td>
                  <td>
                    {d.error_count > 0 && <span className="pill error">{d.error_count} error</span>}{" "}
                    {d.warning_count > 0 && <span className="pill warning">{d.warning_count} warn</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="pager">
        <button className="ghost" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</button>
        <span className="muted small">{total ? `${offset + 1}–${Math.min(offset + PAGE, total)} of ${total}` : "0"}</span>
        <button className="ghost" disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>Next</button>
      </div>
    </section>
  );
}
