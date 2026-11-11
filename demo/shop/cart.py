"""A tiny shopping cart."""

TAX_RATE = 0.08
COUPONS = {"SAVE10": 10, "HALF": 50}


class Cart:
    def __init__(self):
        self.items = {}  # name -> {"price": float, "qty": int}
        self.discount_percent = 0

    def add_item(self, name, price, qty=1):
        if price < 0 or qty <= 0:
            raise ValueError("price must be >= 0 and qty must be > 0")
        if name in self.items:
            self.items[name]["qty"] += qty
        else:
            self.items[name] = {"price": price, "qty": qty}

    def remove_item(self, name):
        del self.items[name]

    def subtotal(self):
        return sum(item["price"] for item in self.items.values())

    def apply_coupon(self, code):
        if code not in COUPONS:
            raise ValueError(f"unknown coupon: {code}")
        self.discount_percent = COUPONS[code]

    def total(self):
        discounted = self.subtotal() * (1 - self.discount_percent)
        return round(discounted * (1 + TAX_RATE), 2)
