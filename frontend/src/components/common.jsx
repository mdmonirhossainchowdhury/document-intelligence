import { useEffect, useRef, useState } from "react";

export const STATUS_LABEL = {
  queued: "Queued",
  processing: "Processing",
  completed: "Completed",
  needs_review: "Needs review",
  failed: "Failed",
};

export function StatusBadge({ status }) {
  return <span className={`badge status-${status}`}>{STATUS_LABEL[status] || status}</span>;
}

export const TERMINAL = new Set(["completed", "needs_review", "failed"]);

export function formatMoney(value, currency) {
  if (value == null) return "—";
  const n = Number(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return currency ? `${currency} ${n}` : n;
}

export function formatDateTime(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

/** Polls `fetcher` every `interval` ms while `active` is true. Returns [data, error, refresh]. */
export function usePolling(fetcher, deps, interval, active = true) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [tick, setTick] = useState(0);
  const fetchRef = useRef(fetcher);
  fetchRef.current = fetcher;

  useEffect(() => {
    let cancelled = false;
    let timer;
    const run = async () => {
      try {
        const result = await fetchRef.current();
        if (!cancelled) {
          setData(result);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) setError(e);
      }
      if (!cancelled && active) timer = setTimeout(run, interval);
    };
    run();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick, active, interval]);

  return [data, error, () => setTick((t) => t + 1)];
}

/** Renders FTS snippets: only <mark>…</mark> is interpreted; everything else is plain text (React escapes it). */
export function Snippet({ text }) {
  if (!text) return null;
  const parts = text.split(/(<mark>.*?<\/mark>)/g);
  return (
    <p className="snippet">
      {parts.map((part, i) =>
        part.startsWith("<mark>") ? <mark key={i}>{part.slice(6, -7)}</mark> : <span key={i}>{part}</span>,
      )}
    </p>
  );
}

export function ErrorBanner({ error }) {
  if (!error) return null;
  return <div className="banner error" role="alert">{error.message || String(error)}</div>;
}
