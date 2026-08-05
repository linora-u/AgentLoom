"""Self-learning history, curated memory, skill, and review primitives.

Public exports stay lazy so hook/bootstrap imports do not eagerly load the
runtime Agent and tool graph.
"""

_LAZY_EXPORTS = {
    "CanonicalSessionEvent": (".event_schema", "CanonicalSessionEvent"),
    "SelfLearningLedger": (".persistence.ledger", "SelfLearningLedger"),
    "MemoryStore": (".persistence.memory_store", "MemoryStore"),
    "ProposalWriter": (".proposal_writer", "ProposalWriter"),
    "SessionRecorder": (".session_recorder", "SessionRecorder"),
}


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    module_name, attribute = target
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY_EXPORTS})

__all__ = [
    "CanonicalSessionEvent",
    "SelfLearningLedger",
    "MemoryStore",
    "ProposalWriter",
    "SessionRecorder",
]
