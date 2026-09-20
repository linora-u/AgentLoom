"""Deterministic Tool used by the real model-protocol acceptance matrix."""

from __future__ import annotations


def protocol_matrix_echo(token: str) -> str:
    """Return the supplied protocol-matrix token unchanged.

    Args:
        token: Exact validation token supplied by the Application.

    Returns:
        The exact input token.
    """

    return token
