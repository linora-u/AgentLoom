"""Host-owned behavior oracle, deliberately independent of the fixture code.

Run in a fresh interpreter against a supplied workspace. Expected amounts below
are hand-computed contract examples, never computed with production helpers.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path


def check(workspace: Path) -> dict[str, object]:
    sys.path.insert(0, str(workspace))
    quote = importlib.import_module("orderdesk.service.checkout").quote
    settings = importlib.import_module("orderdesk.config.settings").load_settings
    failures: list[str] = []
    passed: list[str] = []

    def record(name: str, action, expected) -> None:
        try:
            actual = action()
            if actual != expected:
                raise AssertionError(f"expected {expected!r}, got {actual!r}")
        except Exception as exc:
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
        else:
            passed.append(name)

    def totals(subtotal, discount, shipping, total):
        return dict(subtotal_cents=subtotal, discount_cents=discount,
                    shipping_cents=shipping, total_cents=total)

    rows = [
        ("ordinary", [{"unit_cents": 1200, "quantity": 2}], None, totals(2400, 0, 500, 2900)),
        ("threshold_equal", [{"unit_cents": 2500, "quantity": 2}], None, totals(5000, 0, 0, 5000)),
        ("threshold_below", [{"unit_cents": 4999, "quantity": 1}], None, totals(4999, 0, 500, 5499)),
        ("discount_before_shipping", [{"unit_cents": 999, "quantity": 1}], {"discount_percent": 10}, totals(999, 99, 500, 1400)),
        ("free_based_on_subtotal", [{"unit_cents": 5000, "quantity": 1}], {"discount_percent": 50}, totals(5000, 2500, 0, 2500)),
        ("override_precedence", [{"unit_cents": 1000, "quantity": 1}], {"shipping_cents": 123, "free_shipping_at": 2000}, totals(1000, 0, 123, 1123)),
        ("multiple_lines", [{"unit_cents": 333, "quantity": 3}, {"unit_cents": 501, "quantity": 2}], {"discount_percent": 15}, totals(2001, 300, 500, 2201)),
        ("zero_price", [{"unit_cents": 0, "quantity": 1}], None, totals(0, 0, 500, 500)),
        ("empty_cart", [], None, totals(0, 0, 0, 0)),
        ("empty_cart_override", [], {"shipping_cents": 900}, totals(0, 0, 0, 0)),
        ("quantity_upper", [{"unit_cents": 1, "quantity": 100}], {"free_shipping_at": 0}, totals(100, 0, 0, 100)),
    ]
    for name, lines, config, expected in rows:
        record(name, lambda lines=lines, config=config: quote(lines, config), expected)
    record("empty_overrides", lambda: settings({}), {"discount_percent": 0, "shipping_cents": 500, "free_shipping_at": 5000})

    def rejects(action) -> bool:
        try:
            action()
        except ValueError:
            return True
        return False

    for key, invalids in {
        "quantity": [True, False, 0, -1, 101, 1.0, "1", None],
        "unit_cents": [True, False, -1, 1.0, "1", None],
    }.items():
        for value in invalids:
            line = {"unit_cents": 100, "quantity": 1, key: value}
            record(f"invalid_{key}_{value!r}", lambda line=line: rejects(lambda: quote([line])), True)
    for key in ("discount_percent", "shipping_cents", "free_shipping_at"):
        for value in (True, False, -1, "1", 1.0, None):
            record(f"invalid_config_{key}_{value!r}",
                   lambda key=key, value=value: rejects(lambda: settings({key: value})), True)
    for config in ({"discount_percent": 51}, {"unknown": 1}, [], "", 0, False):
        record(f"invalid_config_shape_{config!r}", lambda config=config: rejects(lambda: settings(config)), True)
    return {"passed": not failures, "checks": len(passed) + len(failures), "passed_checks": passed, "failures": failures}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    args = parser.parse_args()
    result = check(args.workspace.resolve())
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
