"""Preserve invocation identity and accumulated native cleanup diagnostics."""
from __future__ import annotations

import sys


def close_preserving_error(close):
    original = sys.exception()
    try:
        close()
    except BaseException as cleanup:
        errors = getattr(cleanup, "managed_cleanup_errors", (cleanup,))
        if original is None:
            cleanup.managed_cleanup_errors = errors
            cleanup.category = "infrastructure"
            raise
        accumulated = (*getattr(original, "managed_cleanup_errors", ()), *errors)
        if not isinstance(original, Exception):
            original.managed_cleanup_errors = accumulated
            return  # The caller's finally keeps the original control exception.
        error = BaseExceptionGroup("Invocation and native cleanup failed", [original, cleanup])
        error.category = "infrastructure"
        error.managed_cleanup_errors = accumulated
        raise error from None
