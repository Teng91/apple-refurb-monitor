import json
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from apple_refurb_monitor import (
    BOOTSTRAP_MARKER,
    DEFAULT_CATEGORIES,
    Change,
    PageNotFound,
    Settings,
    chunks,
    collect,
    compare,
    fetch,
    main,
    matches,
    parse_max_price,
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
            page = pages_by_category[category].pop(0)
            if isinstance(page, Exception):
                raise page
            return page

        with patch("apple_refurb_monitor.fetch", side_effect=fake_fetch), \
             patch("apple_refurb_monitor.time.sleep") as sleep:
            products, self.problems = collect(settings, previous)
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

    def test_missing_category_page_is_treated_as_empty(self):
        products, calls, _ = self.collect_with_pages(
            {"mac": [page_with([tile()])], "airpods": [PageNotFound("404")], "ipad": [page_with([])]}
        )
        self.assertEqual(set(products), {"FTESTTA/A"})
        self.assertEqual(calls, ["mac", "airpods", "ipad"])
        self.assertEqual(len(self.problems), 1)
        self.assertIn("airpods", self.problems[0])
        self.assertIn("404", self.problems[0])

    def test_every_category_missing_stops_with_error(self):
        with self.assertRaises(ValueError):
            self.collect_with_pages({"mac": [PageNotFound("404")], "ipad": [PageNotFound("404")]})

    def test_broken_category_keeps_previous_products_without_failing(self):
        previous = {
            "OLDIPAD": {"sku": "OLDIPAD", "title": "iPad", "price": 1, "url": "u", "category": "ipad"},
            "FTESTTA/A": {"sku": "FTESTTA/A", "title": "MacBook Pro (整修品)", "price": 39900, "url": "u", "category": "mac"},
        }
        products, _, _ = self.collect_with_pages(
            {"mac": [page_with([tile()])], "ipad": [RuntimeError("無法讀取 ipad: timed out")]}, previous
        )
        self.assertEqual(set(products), {"FTESTTA/A", "OLDIPAD"})
        self.assertEqual(len(self.problems), 1)
        self.assertIn("ipad：讀取失敗，沿用上次的 1 項商品", self.problems[0])

    def test_category_with_changed_page_format_keeps_previous_products(self):
        previous = {"OLDIPAD": {"sku": "OLDIPAD", "title": "iPad", "price": 1, "url": "u", "category": "ipad"}}
        products, _, _ = self.collect_with_pages(
            {"mac": [page_with([tile()])], "ipad": ["<html>new design</html>"]}, previous
        )
        self.assertIn("OLDIPAD", products)
        self.assertIn("REFURB_GRID_BOOTSTRAP", self.problems[0])

    def test_every_category_broken_still_fails_even_with_previous_products(self):
        previous = {"OLD": {"sku": "OLD", "title": "Mac", "price": 1, "url": "u", "category": "mac"}}
        with self.assertRaises(ValueError) as raised:
            self.collect_with_pages(
                {"mac": [RuntimeError("down")], "ipad": [RuntimeError("down")]}, previous
            )
        self.assertIn("所有分類都讀取失敗", str(raised.exception))

    def test_fetch_does_not_retry_404(self):
        not_found = HTTPError("https://www.apple.com/x", 404, "Not Found", None, None)
        with patch("apple_refurb_monitor.urlopen", side_effect=not_found) as opener, \
             patch("apple_refurb_monitor.time.sleep") as sleep:
            with self.assertRaises(PageNotFound):
                fetch("https://www.apple.com/x")
        self.assertEqual(opener.call_count, 1)
        sleep.assert_not_called()

    def test_fetch_retries_server_errors(self):
        server_error = HTTPError("https://www.apple.com/x", 503, "Unavailable", None, None)
        with patch("apple_refurb_monitor.urlopen", side_effect=server_error) as opener, \
             patch("apple_refurb_monitor.time.sleep"):
            with self.assertRaises(RuntimeError) as raised:
                fetch("https://www.apple.com/x")
        self.assertNotIsInstance(raised.exception, PageNotFound)
        self.assertEqual(opener.call_count, 3)

    def test_max_price_accepts_plain_and_comma_separated_numbers(self):
        self.assertEqual(parse_max_price("60000"), 60000)
        self.assertEqual(parse_max_price(" 60,000 "), 60000)
        self.assertIsNone(parse_max_price(""))
        with self.assertRaises(ValueError) as raised:
            parse_max_price("6萬")
        self.assertIn("MAX_PRICE_TWD", str(raised.exception))

    def test_invalid_setting_is_reported_on_telegram(self):
        env = {"MAX_PRICE_TWD": "6萬", "TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "chat", "DRY_RUN": "false"}
        with patch.dict("os.environ", env), \
             patch("apple_refurb_monitor.collect") as collect_mock, \
             patch("apple_refurb_monitor.telegram_send") as send:
            result = main()
        self.assertEqual(result, 1)
        collect_mock.assert_not_called()
        self.assertEqual(send.call_count, 1)
        message, settings = send.call_args.args
        self.assertIn("執行失敗", message)
        self.assertIn("設定錯誤：MAX_PRICE_TWD 必須是整數", message)
        self.assertEqual((settings.telegram_token, settings.telegram_chat_id), ("token", "chat"))

    def test_unset_categories_variable_uses_defaults(self):
        with patch.dict("os.environ", {"APPLE_CATEGORIES": ""}):
            self.assertEqual(Settings.from_env().categories, DEFAULT_CATEGORIES)
        self.assertNotIn("airpods", DEFAULT_CATEGORIES)
        with patch.dict("os.environ", {"APPLE_CATEGORIES": "mac, ipad"}):
            self.assertEqual(Settings.from_env().categories, ("mac", "ipad"))

    def test_filters(self):
        settings = Settings(("mac",), ("macbook",), ("air",), 50000, None, "", "", True, True)  # type: ignore[arg-type]
        self.assertTrue(matches({"title": "MacBook Pro", "sku": "X", "price": 40000}, settings))
        self.assertFalse(matches({"title": "MacBook Air", "sku": "Y", "price": 40000}, settings))

    def run_monitor(self, *, health=False, failure=False, previous=True, problems=()):
        settings = Settings(("mac",), (), (), None, Path("unused.json"), "token", "chat", False, False)
        products = parse_products(page_with([tile()]), "mac")
        with patch("apple_refurb_monitor.Settings.from_env", return_value=replace(settings, health_check=health)), \
             patch("apple_refurb_monitor.collect", return_value=(products, list(problems)), side_effect=ValueError("store unavailable") if failure else None), \
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
        self.assertIn("檢查時間：2026-11-17 00:03:09（台北）", text)
        self.assertIn("目前追蹤：43 項商品", text)
        self.assertIn("本次商品檢查成功", text)

    def test_health_lists_category_problems_without_failing_the_run(self):
        result, messages = self.run_monitor(health=True, problems=["airpods：Apple 沒有這個分類的頁面（404），當作 0 項"])
        self.assertEqual(result, 0)
        self.assertEqual(len(messages), 1)
        self.assertIn("有分類需要留意", messages[0])
        self.assertIn("⚠️ airpods：Apple 沒有這個分類的頁面（404）", messages[0])
        self.assertNotIn("本次商品檢查成功", messages[0])

    def test_category_problems_alone_send_no_hourly_message(self):
        self.assertEqual(self.run_monitor(problems=["ipad：讀取失敗"]), (0, []))

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
