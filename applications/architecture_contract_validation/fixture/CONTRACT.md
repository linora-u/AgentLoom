# Checkout contract

Repair this small order service without changing its public interfaces. The
repository contains defects in validation, configuration precedence and checkout
arithmetic. Investigate the implementation; do not assume existing tests cover
the contract. Preserve every existing test and configuration fixture byte for byte.

`orderdesk.service.checkout.quote(lines, overrides=None)` loads
`orderdesk.config.settings.DEFAULTS`, applies explicit overrides, and returns
exactly `subtotal_cents`, `discount_cents`, `shipping_cents`, `total_cents`.
Each line is a mapping with `unit_cents` (integer >= 0) and `quantity` (integer
1..100). Booleans, strings, floats and invalid integers raise `ValueError`.
An empty cart returns zero in every field. More than one line must be supported.
Empty means there are no lines, not that the subtotal is zero. A nonempty cart
containing only zero-priced lines still follows the normal shipping rule: charge
the configured shipping when the free-shipping threshold is positive; shipping
is free when that threshold is zero. Preserve this distinction with overrides too.

Configuration keys are `discount_percent` (integer 0..50), `shipping_cents`
(integer >= 0), and `free_shipping_at` (integer >= 0). Reject unknown keys and
non-mappings with `ValueError`. An explicit empty mapping is valid. Booleans,
strings and floats are invalid field values. Explicit overrides replace defaults.

Discount is floor(subtotal * discount_percent / 100). Shipping is free when the
**undiscounted** subtotal is greater than or equal to `free_shipping_at`;
otherwise charge `shipping_cents`. Discount never applies to shipping.
Total is subtotal - discount + shipping. Never use floating-point money.

Add regression tests under `tests/generated/`. Include test names containing all
of these scenario identifiers so independent collection can audit coverage:
`threshold_equal`, `discount_before_shipping`, `override_precedence`,
`invalid_quantity`, `invalid_price`, `invalid_config`, `empty_cart`, `zero_price`,
`multiple_lines`. The `zero_price` family must distinguish a nonempty zero-valued
cart from an empty cart under both positive and zero free-shipping thresholds.
Tests must assert concrete results or exception behavior, pass on your repair,
and fail on the original defective source. Do not delete or relax old tests,
disable pytest collection, add skip/xfail, or modify this contract.

Run real pytest using `run_workspace_tests`, retaining its JSON and JUnit files.
The independent verifier must read the actual changed source and generated tests
and run pytest again before returning its report. Worker results must carry the
provided case nonce and workspace forward without copying data from another run.
