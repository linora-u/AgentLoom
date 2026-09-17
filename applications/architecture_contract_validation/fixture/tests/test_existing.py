from orderdesk.domain.lines import line_total
from orderdesk.service.checkout import quote


def test_regular_checkout():
    assert quote([{"unit_cents": 1200, "quantity": 2}]) == {
        "subtotal_cents": 2400, "discount_cents": 0,
        "shipping_cents": 500, "total_cents": 2900,
    }


def test_expensive_order_shipping():
    assert quote([{"unit_cents": 6000, "quantity": 1}])["shipping_cents"] == 0


def test_valid_line_total():
    assert line_total({"unit_cents": 75, "quantity": 3}) == 225
