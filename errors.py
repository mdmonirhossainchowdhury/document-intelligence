"""Shared processing exceptions (kept separate to avoid circular imports between processors)."""
from __future__ import annotations


class DocumentError(Exception):
    """A processing failure.

    `permanent=True` (corrupt, unsupported, mismatched input) is never retried.
    `permanent=False` (infrastructure such as a missing OCR binary) is retried by the job runner.
    """

    def __init__(self, code: str, message: str, permanent: bool = True):
        super().__init__(message)
        self.code = code
        self.message = message
        self.permanent = permanent
