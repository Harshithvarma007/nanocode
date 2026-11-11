import unittest

from cart import Cart


class CartTests(unittest.TestCase):
    def test_subtotal_counts_quantity(self):
        cart = Cart()
        cart.add_item("apple", 0.5, qty=4)
        cart.add_item("bread", 3.0)
        self.assertEqual(cart.subtotal(), 5.0)

    def test_adding_same_item_increases_quantity(self):
        cart = Cart()
        cart.add_item("apple", 0.5)
        cart.add_item("apple", 0.5, qty=2)
        self.assertEqual(cart.items["apple"]["qty"], 3)

    def test_total_includes_tax(self):
        cart = Cart()
        cart.add_item("book", 20.0, qty=2)
        self.assertEqual(cart.total(), 43.2)

    def test_coupon_takes_a_percentage_off(self):
        cart = Cart()
        cart.add_item("shoes", 100.0)
        cart.apply_coupon("SAVE10")
        self.assertEqual(cart.total(), 97.2)  # 100 - 10% = 90, plus 8% tax

    def test_unknown_coupon_is_rejected(self):
        with self.assertRaises(ValueError):
            Cart().apply_coupon("FREE")

    def test_removing_a_missing_item_raises_value_error(self):
        with self.assertRaises(ValueError):
            Cart().remove_item("ghost")


if __name__ == "__main__":
    unittest.main()
