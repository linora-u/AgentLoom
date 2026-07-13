"""Self-learning storage, memory, proposal, and reviewer primitives.

Public exports stay lazy so the detached outbox worker can reach its narrow
entrypoint without importing every memory/tool surface before claiming work.
"""

_LAZY_EXPORTS = {
    "CanonicalSessionEvent": (".event_schema", "CanonicalSessionEvent"),
    "SelfLearningLedger": (".ledger", "SelfLearningLedger"),
    "MemoryStore": (".memory_store", "MemoryStore"),
    "ProposalWriter": (".proposal_writer", "ProposalWriter"),
    "SessionIndex": (".session_index", "SessionIndex"),
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
    "SessionIndex",
]
