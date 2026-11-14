"""In-process background job runner.

A bounded pool of worker threads consumes document IDs from a queue. Each attempt is recorded in
the `jobs` table; transient failures are retried with exponential backoff up to `max_attempts`,
permanent failures are recorded immediately. Jobs still queued or running when the process
stops are re-enqueued on the next start, so no upload is silently lost.

Production note: the same contract maps onto BullMQ/Redis (or Celery/SQS); only `submit` and the
worker loop would change. See docs/ARCHITECTURE.md.
"""
from __future__ import annotations

import logging
import queue
import threading

from . import db, processing
from .config import settings
from .processing import TransientError

log = logging.getLogger(__name__)
_BACKOFF_BASE_S = 1.0


class JobRunner:
    def __init__(self, workers: int, max_attempts: int):
        self._queue: queue.Queue[str] = queue.Queue()
        self._workers = max(1, workers)
        self._max_attempts = max(1, max_attempts)
        self._threads: list[threading.Thread] = []
        self._started = False
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            for n in range(self._workers):
                t = threading.Thread(target=self._loop, name=f"doc-worker-{n}", daemon=True)
                t.start()
                self._threads.append(t)
        self.recover()

    def recover(self) -> int:
        """Re-enqueue documents that were interrupted by a restart."""
        rows = db.query_all("SELECT id FROM documents WHERE status IN ('queued', 'processing')")
        for row in rows:
            self._enqueue(row["id"], reset_attempts=False)
        return len(rows)

    def submit(self, doc_id: str, reset_attempts: bool = True) -> None:
        self._enqueue(doc_id, reset_attempts=reset_attempts)

    def _enqueue(self, doc_id: str, reset_attempts: bool) -> None:
        now = db.now_iso()
        with db.tx() as conn:
            conn.execute(
                """INSERT INTO jobs(document_id, status, attempts, created_at, updated_at) VALUES (?, 'queued', 0, ?, ?)
                   ON CONFLICT(document_id) DO UPDATE SET status = 'queued', updated_at = excluded.updated_at,
                   attempts = CASE WHEN ? THEN 0 ELSE jobs.attempts END""",
                (doc_id, now, now, 1 if reset_attempts else 0),
            )
            conn.execute("UPDATE documents SET status = 'queued', error = NULL, error_code = NULL, updated_at = ? WHERE id = ?",
                         (now, doc_id))
        self._queue.put(doc_id)

    def queue_depth(self) -> int:
        return self._queue.qsize()

    def _loop(self) -> None:
        while True:
            doc_id = self._queue.get()
            try:
                self._run_once(doc_id)
            except Exception:  # never let a worker thread die
                log.exception("worker crashed on %s", doc_id)
            finally:
                self._queue.task_done()

    def _run_once(self, doc_id: str) -> None:
        with db.tx() as conn:
            conn.execute("UPDATE jobs SET status = 'running', attempts = attempts + 1, updated_at = ? WHERE document_id = ?",
                         (db.now_iso(), doc_id))
            row = conn.execute("SELECT attempts FROM jobs WHERE document_id = ?", (doc_id,)).fetchone()
        attempts = row["attempts"] if row else 1
        try:
            processing.process_document(doc_id)
        except TransientError as exc:
            if attempts < self._max_attempts:
                delay = _BACKOFF_BASE_S * (2 ** (attempts - 1))
                processing.record_event(doc_id, "retry", "error", f"attempt {attempts} failed: {exc}")
                with db.tx() as conn:
                    conn.execute("UPDATE jobs SET status = 'queued', last_error = ?, updated_at = ? WHERE document_id = ?",
                                 (str(exc), db.now_iso(), doc_id))
                threading.Timer(delay, self._queue.put, args=(doc_id,)).start()
            else:
                processing.record_event(doc_id, "failed", "error", f"gave up after {attempts} attempts: {exc}")
                processing.set_status(doc_id, "failed", error=str(exc), error_code="processing_failed")
                processing.mark_job(doc_id, "failed", str(exc))


runner = JobRunner(workers=settings.worker_threads, max_attempts=settings.max_attempts)
