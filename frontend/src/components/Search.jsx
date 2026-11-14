import { useState } from "react";
import { api } from "../api.js";
import { ErrorBanner, Snippet, StatusBadge, formatMoney } from "./common.jsx";

const MODES = [
  { id: "hybrid", label: "Hybrid (recommended)" },
  { id: "keyword", label: "Keyword" },
  { id: "semantic", label: "Similarity (vector)" },
];

export default function Search({ onOpen }) {
  const [q, setQ] = useState("");
  const [mode, setMode] = useState("hybrid");
  const [results, setResults] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    if (!q.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const body = await api.search(q.trim(), mode);
      setResults(body.items);
    } catch (err) {
      setError(err);
      setResults(null);
    } finally {
      setLoading(false);
    }
  };

  return (
    <section className="card">
      <h2>Search processed documents</h2>
      <form className="search-form" onSubmit={submit}>
        <input
          type="search"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="e.g. INV-2025, Apex Office Supplies, consulting services"
          aria-label="Search query"
          maxLength={300}
        />
        <select value={mode} onChange={(e) => setMode(e.target.value)} aria-label="Search mode">
          {MODES.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
        </select>
        <button className="primary" disabled={loading || !q.trim()}>{loading ? "Searching…" : "Search"}</button>
      </form>
      <p className="muted small">
        Keyword matches exact words (with stemming). Similarity ranks by shared, rare vocabulary (TF-IDF style), so it finds related documents even when the wording differs slightly, but it does not understand synonyms. Hybrid combines both.
      </p>
      <ErrorBanner error={error} />
      {results && results.length === 0 && <p className="muted">No documents matched.</p>}
      {results && results.length > 0 && (
        <ul className="stack results">
          {results.map((d) => (
            <li key={d.id} className="result">
              <div className="card-head">
                <button className="link" onClick={() => onOpen(d.id)}>{d.filename}</button>
                <StatusBadge status={d.status} />
              </div>
              <div className="muted small">
                {[d.invoice_number, d.supplier_name, d.total != null ? formatMoney(d.total, d.currency) : null, d.invoice_date]
                  .filter(Boolean).join(" · ") || "No structured fields"}
              </div>
              <Snippet text={d.snippet} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
