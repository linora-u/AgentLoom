def line_total(line):
    price = line["unit_cents"]
    quantity = line["quantity"]
    if not isinstance(price, int) or price < 0:
        raise ValueError("unit_cents must be a nonnegative integer")
    if not isinstance(quantity, int) or not 0 <= quantity <= 100:
        raise ValueError("quantity must be 1..100")
    return price * quantity
