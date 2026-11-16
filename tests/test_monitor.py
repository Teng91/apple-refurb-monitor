import json
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from apple_refurb_monitor import (
    BOOTSTRAP_MARKER,
    Change,
    Settings,
    chunks,
    collect,
    compare,
    main,
    matches,
    parse_products,
)


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
            "DOWN": {"sku": "DOWN", "title": "Cheaper", "price": 100, "url": "down"},
            "UP": {"sku": "UP", "title": "Costlier", "price": 100, "url": "up"},
            "GONE": {"sku": "GONE", "title": "Gone", "price": 200, "url": "u"},
        }
        new = {
            "DOWN": {"sku": "DOWN", "title": "Cheaper", "price": 80, "url": "down"},
            "UP": {"sku": "UP", "title": "Costlier", "price": 120, "url": "up"},
            "NEW": {"sku": "NEW", "title": "New", "price": 300, "url": "u"},
        }
        changes = compare(old, new)
        self.assertEqual([change.kind for change in changes], ["added", "decreased", "increased", "removed"])
        self.assertEqual(changes[1].old_price, 100)
        self.assertEqual(changes[2].old_price, 100)

        output = "\n".join(chunks(changes))
        self.assertIn("🆕 新上架 1", output)
        self.assertIn("❌ 已下架 1", output)
        self.assertIn("📉 降價 1", output)
        self.assertIn("📈 漲價 1", output)
        self.assertIn("NT$100 → NT$80", output)
        self.assertIn("NT$100 → NT$120", output)

    def test_chunks_returns_no_message_without_changes(self):
        self.assertEqual(chunks([]), [])

    def test_chunks_splits_long_updates_without_header_only_messages(self):
        changes = [
            Change("added", {"sku": f"S{i}", "title": f"MacBook Pro {i}", "price": 1000, "url": "u" * 40})
            for i in range(30)
        ]
        messages = chunks(changes, limit=300)
        self.assertGreater(len(messages), 1)
        self.assertTrue(messages[1].startswith("🍎 Apple 台灣整修品更新（續）"))
        for message in messages:
            self.assertIn("NT$1,000", message)
        joined = "\n".join(messages)
        for i in range(30):
            self.assertIn(f"MacBook Pro {i}\n", joined)

    def test_chunks_keeps_single_oversized_change_whole(self):
        change = Change("added", {"sku": "BIG", "title": "X" * 500, "price": 1, "url": "u"})
        messages = chunks([change], limit=100)
        self.assertEqual(len(messages), 1)
        self.assertIn("X" * 500, messages[0])

    def collect_with_pages(self, pages_by_category, previous=None):
        settings = Settings(tuple(pages_by_category), (), (), None, Path("unused.json"), "", "", True, True)
        calls = []

        def fake_fetch(url):
            category = url.rsplit("/", 1)[-1]
            calls.append(category)
            return pages_by_category[category].pop(0)

        with patch("apple_refurb_monitor.fetch", side_effect=fake_fetch), \
             patch("apple_refurb_monitor.time.sleep") as sleep:
            products = collect(settings, previous)
        return products, calls, sleep

    def test_empty_category_is_reread_when_it_had_products(self):
        empty = page_with([])
        previous = {"OLD": {"sku": "OLD", "title": "AirPods", "price": 1, "url": "u", "category": "airpods"}}
        products, calls, sleep = self.collect_with_pages(
            {"mac": [page_with([tile()])], "airpods": [empty, page_with([tile(sku="AIR", title="AirPods")])]},
            previous,
        )
        self.assertEqual(calls, ["mac", "airpods", "airpods"])
        self.assertIn("AIR", products)
        self.assertEqual(sleep.call_count, 1)

    def test_category_still_empty_after_rereads_is_accepted(self):
        empty = page_with([])
        previous = {"OLD": {"sku": "OLD", "title": "AirPods", "price": 1, "url": "u", "category": "airpods"}}
        products, calls, _ = self.collect_with_pages(
            {"mac": [page_with([tile()])], "airpods": [empty, empty, empty]}, previous
        )
        self.assertEqual(calls.count("airpods"), 3)
        self.assertEqual(set(products), {"FTESTTA/A"})

    def test_empty_category_without_previous_products_is_not_reread(self):
        products, calls, sleep = self.collect_with_pages(
            {"mac": [page_with([tile()])], "airpods": [page_with([])]}, previous=None
        )
        self.assertEqual(calls, ["mac", "airpods"])
        sleep.assert_not_called()

    def test_filters(self):
        settings = Settings(("mac",), ("macbook",), ("air",), 50000, None, "", "", True, True)  # type: ignore[arg-type]
        self.assertTrue(matches({"title": "MacBook Pro", "sku": "X", "price": 40000}, settings))
        self.assertFalse(matches({"title": "MacBook Air", "sku": "Y", "price": 40000}, settings))

    def run_monitor(self, *, health=False, failure=False, previous=True):
        settings = Settings(("mac",), (), (), None, Path("unused.json"), "token", "chat", False, False)
        products = parse_products(page_with([tile()]), "mac")
        with patch("apple_refurb_monitor.Settings.from_env", return_value=replace(settings, health_check=health)), \
             patch("apple_refurb_monitor.collect", return_value=products, side_effect=ValueError("store unavailable") if failure else None), \
             patch("apple_refurb_monitor.load_state", return_value=products if previous else None), \
             patch("apple_refurb_monitor.save_state"), \
             patch("apple_refurb_monitor.telegram_send") as send:
            result = main()
        return result, [call.args[0] for call in send.call_args_list]

    def test_health_sent_without_product_changes(self):
        result, messages = self.run_monitor(health=True)
        self.assertEqual(result, 0)
        self.assertEqual(len(messages), 1)
        self.assertIn("Health", messages[0])
        self.assertIn("目前追蹤：1 項商品", messages[0])
        self.assertIn("（台北）", messages[0])
        self.assertIn("商品有異動時會另外通知", messages[0])

    def test_failed_check_never_sends_healthy_message(self):
        result, messages = self.run_monitor(health=True, failure=True)
        self.assertEqual(result, 1)
        self.assertEqual(len(messages), 1)
        self.assertIn("執行失敗", messages[0])
        self.assertNotIn("Health", messages[0])

    def test_first_run_also_sends_requested_health(self):
        result, messages = self.run_monitor(health=True, previous=False)
        self.assertEqual(result, 0)
        self.assertEqual(len(messages), 1)
        self.assertIn("Health", messages[0])

    def test_health_setting_comes_from_environment(self):
        with patch.dict("os.environ", {"HEALTH_CHECK": "true"}):
            self.assertTrue(Settings.from_env().health_check)
        with patch.dict("os.environ", {"HEALTH_CHECK": "false"}):
            self.assertFalse(Settings.from_env().health_check)

    def test_health_message_uses_taipei_time(self):
        from apple_refurb_monitor import health_message
        text = health_message(datetime(2026, 11, 17, 0, 3, 9), 43)
        self.assertIn("成功檢查時間：2026-11-17 00:03:09（台北）", text)
        self.assertIn("目前追蹤：43 項商品", text)

    def test_regular_run_stays_silent_without_changes(self):
        self.assertEqual(self.run_monitor(), (0, []))

    def test_failed_check_sends_error_message(self):
        result, messages = self.run_monitor(failure=True)
        self.assertEqual(result, 1)
        self.assertEqual(len(messages), 1)
        self.assertIn("執行失敗", messages[0])

    def test_first_run_saves_baseline_silently_when_notify_disabled(self):
        self.assertEqual(self.run_monitor(previous=False), (0, []))


if __name__ == "__main__":
    unittest.main()
