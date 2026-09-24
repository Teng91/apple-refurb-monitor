import json
import unittest

from apple_refurb_monitor import BOOTSTRAP_MARKER, Settings, compare, matches, parse_products


def page_with(tiles):
    return f"<html><script>{BOOTSTRAP_MARKER}{json.dumps({'tiles': tiles})};</script></html>"


def tile(sku="FTESTTA/A", title="MacBook Pro (整修品)", price="39900.00"):
    return {
        "partNumber": sku,
        "title": title,
        "productDetailsUrl": "/tw/shop/product/ftestta/a/test?fnode=abc",
        "price": {"currentPrice": {"raw_amount": price}},
    }


class MonitorTests(unittest.TestCase):
    def test_parse_products(self):
        products = parse_products(page_with([tile()]), "mac")
        self.assertEqual(products["FTESTTA/A"]["price"], 39900)
        self.assertEqual(products["FTESTTA/A"]["url"], "https://www.apple.com/tw/shop/product/ftestta/a/test")

    def test_legitimate_empty_category(self):
        empty_page = f"<script>{BOOTSTRAP_MARKER}{json.dumps({'tiles': None})};</script>"
        self.assertEqual(parse_products(empty_page, "airpods"), {})

    def test_compare_all_change_types(self):
        old = {
            "SAME": {"sku": "SAME", "title": "Same", "price": 100, "url": "u"},
            "GONE": {"sku": "GONE", "title": "Gone", "price": 200, "url": "u"},
        }
        new = {
            "SAME": {"sku": "SAME", "title": "Same", "price": 80, "url": "u"},
            "NEW": {"sku": "NEW", "title": "New", "price": 300, "url": "u"},
        }
        output = "\n".join(compare(old, new))
        self.assertIn("新上架", output)
        self.assertIn("降價", output)
        self.assertIn("已下架", output)

    def test_filters(self):
        settings = Settings(("mac",), ("macbook",), ("air",), 50000, None, "", "", True, True)  # type: ignore[arg-type]
        self.assertTrue(matches({"title": "MacBook Pro", "sku": "X", "price": 40000}, settings))
        self.assertFalse(matches({"title": "MacBook Air", "sku": "Y", "price": 40000}, settings))


if __name__ == "__main__":
    unittest.main()
