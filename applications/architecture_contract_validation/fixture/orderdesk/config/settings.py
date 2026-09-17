DEFAULTS = {"discount_percent": 0, "shipping_cents": 500, "free_shipping_at": 5000}


def load_settings(overrides=None):
    values = dict(overrides or {})
    values.update(DEFAULTS)
    for key, value in values.items():
        if key not in DEFAULTS or not isinstance(value, int) or value < 0:
            raise ValueError("invalid configuration")
    if values["discount_percent"] > 50:
        raise ValueError("discount_percent must be 0..50")
    return values
