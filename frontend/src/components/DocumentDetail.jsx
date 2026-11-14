import { useMemo, useState } from "react";
import { api } from "../api.js";
import { ErrorBanner, StatusBadge, TERMINAL, formatDateTime, formatMoney, usePolling } from "./common.jsx";

const NUMERIC = new Set(["subtotal", "tax", "total"]);
const TEXT_FIELDS = [
  ["invoice_number", "Invoice number"],
  ["invoice_date", "Invoice date", "date"],
  ["supplier_name", "Supplier"],
  ["customer_name", "Customer"],
  ["subtotal", "Subtotal", "number"],
  ["tax", "Tax", "number"],
  ["total", "Total", "number"],
  ["currency", "Currency (ISO code)"],
];

const blankItem = () => ({ description: "", quantity: "", unit_price: "", amount: "" });
const toNumberOrNull = (v) => (v === "" || v == null ? null : Number(v));

function Editor({ extraction, onSaved }) {
  const initial = useMemo(() => ({
    ...Object.fromEntries(TEXT_FIELDS.map(([k]) => [k, extraction[k] ?? ""])),
    line_items: (extraction.line_items || []).map((li) => ({
      description: li.description ?? "",
      quantity: li.quantity ?? "",
      unit_price: li.unit_price ?? "",
      amount: li.amount ?? "",
    })),
  }), [extraction]);
  const [form, setForm] = useState(initial);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [message, setMessage] = useState(null);

  const setField = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));
  const setItem = (i, k) => (e) => setForm((f) => ({
    ...f,
    line_items: f.line_items.map((li, idx) => (idx === i ? { ...li, [k]: e.target.value } : li)),
  }));

  const dirty = JSON.stringify(form) !== JSON.stringify(initial);

  const save = async () => {
    // Send only what changed, so the audit trail and corrected_fields stay accurate.
    const body = {};
    for (const [k] of TEXT_FIELDS) {
      if (form[k] !== initial[k]) {
        body[k] = NUMERIC.has(k) ? toNumberOrNull(form[k]) : (form[k] === "" ? null : form[k]);
      }
    }
    if (JSON.stringify(form.line_items) !== JSON.stringify(initial.line_items)) {
      body.line_items = form.line_items
        .filter((li) => li.amount !== "" || li.description !== "")
        .map((li) => ({
          description: li.description || "",
          quantity: toNumberOrNull(li.quantity),
          unit_price: toNumberOrNull(li.unit_price),
          amount: toNumberOrNull(li.amount) ?? 0,
        }));
    }
    setSaving(true);
    setError(null);
    setMessage(null);
    try {
      const result = await api.save(extraction.document_id, body);
      setMessage(result.status === "completed" ? "Saved. All checks pass." : `Saved. ${result.issues.filter((i) => i.severity === "error").length} blocking issue(s) remain.`);
      onSaved?.();
    } catch (e) {
      setError(e);
    } finally {
      setSaving(false);
    }
  };

  const confidence = extraction.field_confidence || {};
  const corrected = new Set(extraction.corrected_fields || []);

  return (
    <div className="stack">
      <div className="grid-fields">
        {TEXT_FIELDS.map(([key, label, type]) => (
          <label key={key} className={`field ${confidence[key] !== undefined && confidence[key] < 0.7 ? "low-conf" : ""}`}>
            <span>
              {label}
              {corrected.has(key) && <em className="tag">corrected</em>}
              {confidence[key] !== undefined && confidence[key] < 0.7 && <em className="tag warn">low confidence</em>}
              {confidence[key] === undefined && form[key] === "" && <em className="tag muted-tag">not found</em>}
            </span>
            <input
              type={type || "text"}
              step={type === "number" ? "0.01" : undefined}
              value={form[key]}
              onChange={setField(key)}
              maxLength={key === "currency" ? 3 : undefined}
            />
          </label>
        ))}
      </div>

      <div className="card-head">
        <h3>Line items</h3>
        <button className="ghost" onClick={() => setForm((f) => ({ ...f, line_items: [...f.line_items, blankItem()] }))}>Add row</button>
      </div>
      {form.line_items.length === 0 ? (
        <p className="muted small">No line items were detected. Add rows manually if the document has them.</p>
      ) : (
        <div className="table-wrap">
          <table className="items">
            <thead><tr><th>Description</th><th className="num">Qty</th><th className="num">Unit price</th><th className="num">Amount</th><th /></tr></thead>
            <tbody>
              {form.line_items.map((li, i) => (
                <tr key={i}>
                  <td><input value={li.description} onChange={setItem(i, "description")} aria-label={`Line ${i + 1} description`} /></td>
                  <td><input className="num" type="number" step="any" value={li.quantity} onChange={setItem(i, "quantity")} aria-label={`Line ${i + 1} quantity`} /></td>
                  <td><input className="num" type="number" step="0.01" value={li.unit_price} onChange={setItem(i, "unit_price")} aria-label={`Line ${i + 1} unit price`} /></td>
                  <td><input className="num" type="number" step="0.01" value={li.amount} onChange={setItem(i, "amount")} aria-label={`Line ${i + 1} amount`} /></td>
                  <td>
                    <button className="ghost small-btn" aria-label={`Remove line ${i + 1}`}
                      onClick={() => setForm((f) => ({ ...f, line_items: f.line_items.filter((_, idx) => idx !== i) }))}>✕</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <ErrorBanner error={error} />
      {message && <div className="banner ok">{message}</div>}
      <div className="actions">
        <button className="primary" disabled={!dirty || saving} onClick={save}>{saving ? "Saving…" : "Save corrections"}</button>
        {dirty && <button className="ghost" onClick={() => setForm(initial)}>Discard changes</button>}
      </div>
    </div>
  );
}

export default function DocumentDetail({ id, onBack, onChanged }) {
  const [doc, error, refresh] = usePolling(() => api.get(id), [id], 2500, true);
  const [actionError, setActionError] = useState(null);
  const [busy, setBusy] = useState(false);

  if (error && !doc) return <ErrorBanner error={error} />;
  if (!doc) return <p className="muted">Loading…</p>;

  const finished = TERMINAL.has(doc.status);
  const errors = doc.issues.filter((i) => i.severity === "error");
  const warnings = doc.issues.filter((i) => i.severity === "warning");

  const run = async (fn) => {
    setBusy(true);
    setActionError(null);
    try {
      await fn();
      refresh();
      onChanged?.();
    } catch (e) {
      setActionError(e);
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!window.confirm(`Delete ${doc.filename}? This removes the file and its extracted data.`)) return;
    await run(async () => { await api.remove(id); onBack(); });
  };

  return (
    <div className="stack-lg">
      <div className="card-head">
        <button className="ghost" onClick={onBack}>← Back to documents</button>
        <div className="actions">
          <a className="ghost button-like" href={api.fileUrl(id)}>Download original</a>
          {finished && (
            <button className="ghost" disabled={busy} onClick={() => run(() => api.reprocess(id))}>Reprocess</button>
          )}
          <button className="ghost danger" disabled={busy || !finished} onClick={remove}>Delete</button>
        </div>
      </div>

      <section className="card">
        <div className="card-head">
          <div>
            <h2 className="filename">{doc.filename}</h2>
            <div className="muted small">
              Uploaded {formatDateTime(doc.created_at)} · {doc.file_type}
              {doc.source_method ? ` · ${doc.source_method.replace("_", " ")}` : ""}
              {doc.page_count ? ` · ${doc.page_count} page(s)` : ""}
              {doc.ocr_confidence != null ? ` · OCR confidence ${Math.round(doc.ocr_confidence)}/100` : ""}
            </div>
          </div>
          <StatusBadge status={doc.status} />
        </div>
        {doc.error && <div className="banner error">{doc.error}</div>}
        {!finished && <p className="muted">Processing… this page refreshes automatically.</p>}
        <div className="summary">
          <div><span className="muted small">Total</span><strong>{formatMoney(doc.extraction?.total, doc.extraction?.currency)}</strong></div>
          <div><span className="muted small">Blocking issues</span><strong>{errors.length}</strong></div>
          <div><span className="muted small">Warnings</span><strong>{warnings.length}</strong></div>
        </div>
        <ErrorBanner error={actionError} />
      </section>

      {doc.extraction?.available ? (
        <section className="card">
          <h2>Extracted information</h2>
          <p className="muted small">
            Method: {doc.extraction.extraction_method}. Empty fields were not found in the document and are left blank rather than guessed.
          </p>
          {/* Remount only when the document itself changed on the server, so unsaved edits survive polling. */}
          <Editor key={doc.updated_at} extraction={doc.extraction} onSaved={() => { refresh(); onChanged?.(); }} />
        </section>
      ) : finished ? (
        <section className="card"><p className="muted">No structured data was extracted from this document.</p></section>
      ) : null}

      <section className="card">
        <h2>Validation</h2>
        {doc.issues.length === 0 && finished && <div className="banner ok">No issues found.</div>}
        <ul className="stack">
          {doc.issues.map((i) => (
            <li key={i.id} className={`issue ${i.severity}`}>
              <span className={`pill ${i.severity}`}>{i.severity}</span>
              <div>
                <div>{i.message}</div>
                <div className="muted small">{i.rule}{i.field ? ` · ${i.field}` : ""}</div>
              </div>
            </li>
          ))}
        </ul>
      </section>

      <section className="card">
        <h2>Processing history</h2>
        <ol className="timeline">
          {doc.events.map((e, i) => (
            <li key={i} className={e.status}>
              <span className="muted small">{formatDateTime(e.created_at)}</span> <strong>{e.stage}</strong>
              {e.duration_ms != null && <span className="muted small"> · {e.duration_ms} ms</span>}
              {e.message && <div className="small">{e.message}</div>}
            </li>
          ))}
        </ol>
      </section>

      {doc.text_preview && (
        <details className="card">
          <summary>Extracted text (first 4,000 characters)</summary>
          <pre className="text-preview">{doc.text_preview}</pre>
        </details>
      )}
    </div>
  );
}
