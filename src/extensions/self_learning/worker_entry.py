"""Minimal process entrypoint for the detached durable-learning worker."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

_BATCH_MAX_JOBS = 1000
_COUNT_KEYS = ("succeeded", "retry", "dead", "fenced", "attempted", "processed")


def run_worker(
    db_path: str,
    *,
    max_wait: float = 15.0,
    kick_token: str = "",
) -> dict[str, int]:
    """Drain one isolated outbox and always release its launcher fence.

    The per-worker job cap is a transaction/lease batch boundary, not a cap on
    durable work.  While this entry still owns the launch fence it drains every
    currently-ready follow-up batch in-process.  This avoids both a stranded
    1001st job and a recursive detached-process chain.
    """
    from .learning_jobs import LearningJobQueue, LearningJobWorker

    queue = LearningJobQueue(db_path)
    total = {key: 0 for key in _COUNT_KEYS}
    try:
        while True:
            batch = LearningJobWorker(queue).run_until_idle(
                max_jobs=_BATCH_MAX_JOBS,
                max_wait_seconds=max(0.0, float(max_wait)),
            )
            for key in _COUNT_KEYS:
                total[key] += int(batch.get(key) or 0)

            if kick_token:
                if queue.continue_worker_kick_slot(kick_token):
                    continue
                # The atomic readiness check either released this token or
                # observed a newer fenced owner.  Do not delete that owner's
                # slot in the outer finally block.
                kick_token = ""
            break
        return total
    finally:
        if kick_token:
            queue.release_worker_kick_slot(kick_token)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--db", required=True)
    parser.add_argument("--max-wait", type=float, default=15.0)
    parser.add_argument("--kick-token", default="")
    args = parser.parse_args(argv)
    result = run_worker(
        args.db,
        max_wait=args.max_wait,
        kick_token=args.kick_token,
    )
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
