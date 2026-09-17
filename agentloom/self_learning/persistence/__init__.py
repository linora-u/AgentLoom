"""Concrete SQLite persistence for self-learning domain state.

Schema-aware callers import the specific domain store they need.  This package
does not re-export them because a convenience facade would hide ownership and
eagerly load unrelated persistence implementation.
"""
