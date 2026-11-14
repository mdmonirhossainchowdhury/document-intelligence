import { useState } from "react";
import { api } from "../api.js";
import { ErrorBanner, StatusBadge, TERMINAL, usePolling } from "./common.jsx";

const ACCEPT = ".pdf,.png,.jpg,.jpeg,.tif,.tiff,.xlsx,.xls,.csv";

/** Live progress for the documents uploaded in this session. */
function ProgressList({ ids, onOpen }) {
  // One list request per tick (newest first, so this session's uploads are on the first page).
  const [page] = usePolling(
    () => api.list({ limit: 200 }),
    [ids.join(",")],
    1500,
    ids.length > 0,
  );
  if (!ids.length) return null;
  const wanted = new Set(ids);
  const rows = page ? page.items.filter((d) => wanted.has(d.id)) : null;
  const list = rows || [];
  const done = list.filter((d) => TERMINAL.has(d.status)).length;
  return (
    <section className="card">
      <div className="card-head">
        <h2>Processing</h2>
        <span className="muted">{done} of {ids.length} finished</span>
      </div>
      <div className="progress" role="progressbar" aria-valuemin={0} aria-valuemax={ids.length} aria-valuenow={done}>
        <div style={{ width: `${ids.length ? (done / ids.length) * 100 : 0}%` }} />
      </div>
      <ul className="stack">
        {list.map((d) => (
          <li key={d.id} className="row-item">
            <button className="link" onClick={() => onOpen(d.id)}>{d.filename}</button>
            <StatusBadge status={d.status} />
            {d.error && <span className="muted small">{d.error}</span>}
          </li>
        ))}
        {!rows && <li className="muted small">Waiting for status…</li>}
      </ul>
    </section>
  );
}

export default function Upload({ onOpen, onUploaded }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [report, setReport] = useState(null);
  const [ids, setIds] = useState([]);
  const [dragOver, setDragOver] = useState(false);

  const send = async (fileList) => {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    setBusy(true);
    setError(null);
    setReport(null);
    try {
      const result = await api.upload(files);
      setReport(result);
      const newIds = result.accepted.map((a) => a.id);
      setIds((prev) => [...newIds, ...prev]);
      onUploaded?.();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="stack-lg">
      <section className="card">
        <h2>Upload documents</h2>
        <p className="muted">
          PDF (digital or scanned), JPG, PNG, TIFF, XLSX, XLS and CSV. Up to 50 files per batch, 25 MB each.
          Files are processed in the background; watch progress below.
        </p>
        <label
          className={`dropzone ${dragOver ? "over" : ""} ${busy ? "disabled" : ""}`}
          onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => { e.preventDefault(); setDragOver(false); if (!busy) send(e.dataTransfer.files); }}
        >
          <input
            type="file"
            multiple
            accept={ACCEPT}
            disabled={busy}
            onChange={(e) => { send(e.target.files); e.target.value = ""; }}
          />
          <span>{busy ? "Uploading…" : "Drop files here, or click to choose"}</span>
        </label>
        <ErrorBanner error={error} />
        {report && (
          <div className="stack">
            <div className="banner ok">Accepted {report.accepted.length} file(s).</div>
            {report.accepted.filter((a) => a.duplicate_of).map((a) => (
              <div className="banner warn" key={a.id}>
                {a.filename} has identical content to an earlier upload; it will be flagged as a duplicate file.
              </div>
            ))}
            {report.rejected.map((r, i) => (
              <div className="banner error" key={i}>Rejected {r.filename}: {r.reason}</div>
            ))}
          </div>
        )}
      </section>
      <ProgressList ids={ids} onOpen={onOpen} />
    </div>
  );
}
