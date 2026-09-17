from orderdesk.config.settings import load_settings
from orderdesk.domain.lines import line_total


def quote(lines, overrides=None):
    settings = load_settings(overrides)
    subtotal = sum(line_total(line) for line in lines)
    shipping = 0 if subtotal > settings["free_shipping_at"] else settings["shipping_cents"]
    discount = (subtotal + shipping) * settings["discount_percent"] // 100
    return {
        "subtotal_cents": subtotal,
        "discount_cents": discount,
        "shipping_cents": shipping,
        "total_cents": subtotal - discount + shipping,
    }
