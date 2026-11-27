import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from http.client import IncompleteRead
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from apple_refurb_monitor import (
    BOOTSTRAP_MARKER,
    DEFAULT_CATEGORIES,
    Change,
    Filters,
    PageNotFound,
    Settings,
    TelegramConfig,
    chunks,
    collect,
    compare,
    fetch,
    health_message,
    load_state,
    main,
    parse_max_price,
    parse_products,
    run,
    save_state,
    telegram_send,
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


def product(sku="FTESTTA/A", title="MacBook Pro (整修品)", price=39900, category="mac"):
    return {"sku": sku, "title": title, "price": price, "url": "u", "category": category}


def fake_fetcher(pages_by_category, calls=None):
    """Category fetcher reading pages in order; an Exception instance is raised, None means 404."""

    def fetch_category(category):
        if calls is not None:
            calls.append(category)
        page = pages_by_category[category].pop(0)
        if isinstance(page, Exception):
            raise page
        return None if page is None else parse_products(page, category, log=lambda _: None)

    return fetch_category


def http_error(code, body=b"", url="https://example.com"):
    return HTTPError(url, code, "Error", None, io.BytesIO(body))


def response(payload):
    mock = MagicMock()
    mock.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    return mock


class ParseTests(unittest.TestCase):
    def test_parse_products(self):
        products = parse_products(page_with([tile()]), "mac")
        self.assertEqual(products["FTESTTA/A"]["price"], 39900)
        self.assertEqual(products["FTESTTA/A"]["url"], "https://www.apple.com/tw/shop/product/ftestta/a/test")

    def test_legitimate_empty_category(self):
        empty_page = f"<script>{BOOTSTRAP_MARKER}{json.dumps({'tiles': None})};</script>"
        self.assertEqual(parse_products(empty_page, "airpods"), {})

    def test_skipped_tiles_are_logged_through_the_given_logger(self):
        logs = []
        broken = {"partNumber": "BAD", "price": None}
        with patch("apple_refurb_monitor.fetch", return_value=page_with([tile(), broken])):
            collect(("mac",), log=logs.append)
        self.assertIn("mac: 略過 1 個格式不符的商品", logs)

    def test_one_malformed_tile_is_skipped_not_the_whole_category(self):
        broken = {"partNumber": "BAD", "title": "Broken", "productDetailsUrl": "/x", "price": None}
        logs = []
        products = parse_products(page_with([tile(), broken, "not a dict"]), "mac", log=logs.append)
        self.assertEqual(set(products), {"FTESTTA/A"})
        self.assertIn("略過 2 個", logs[0])

    def test_every_tile_malformed_is_a_format_change(self):
        with self.assertRaises(ValueError):
            parse_products(page_with([{"partNumber": "X", "price": {"currentPrice": {"raw_amount": "n/a"}}}]), "mac")


class FilterTests(unittest.TestCase):
    def test_include_and_exclude_keywords_and_price(self):
        filters = Filters(("macbook",), ("air",), 50000)
        self.assertTrue(filters.matches({"title": "MacBook Pro", "sku": "X", "price": 40000}))
        self.assertFalse(filters.matches({"title": "MacBook Air", "sku": "Y", "price": 40000}))
        self.assertFalse(filters.matches({"title": "MacBook Pro", "sku": "Z", "price": 60000}))

    def test_keywords_also_match_sku(self):
        self.assertTrue(Filters(include_keywords=("fde14",)).matches({"title": "Mac", "sku": "FDE14TA/A", "price": 1}))
        self.assertFalse(Filters(exclude_keywords=("fde14",)).matches({"title": "Mac", "sku": "FDE14TA/A", "price": 1}))

    def test_max_price_accepts_plain_and_comma_separated_numbers(self):
        self.assertEqual(parse_max_price("60000"), 60000)
        self.assertEqual(parse_max_price(" 60,000 "), 60000)
        self.assertIsNone(parse_max_price(""))
        with self.assertRaises(ValueError) as raised:
            parse_max_price("6萬")
        self.assertIn("MAX_PRICE_TWD", str(raised.exception))


class SettingsTests(unittest.TestCase):
    def test_unset_categories_variable_uses_defaults(self):
        with patch.dict("os.environ", {"APPLE_CATEGORIES": ""}):
            self.assertEqual(Settings.from_env().categories, DEFAULT_CATEGORIES)
        self.assertNotIn("airpods", DEFAULT_CATEGORIES)
        with patch.dict("os.environ", {"APPLE_CATEGORIES": "mac, ipad"}):
            self.assertEqual(Settings.from_env().categories, ("mac", "ipad"))

    def test_health_setting_comes_from_environment(self):
        with patch.dict("os.environ", {"HEALTH_CHECK": "true"}):
            self.assertTrue(Settings.from_env().health_check)
        with patch.dict("os.environ", {"HEALTH_CHECK": "false"}):
            self.assertFalse(Settings.from_env().health_check)


class CompareAndMessageTests(unittest.TestCase):
    def test_compare_all_change_types(self):
        old = {
            "DOWN": {"sku": "DOWN", "title": "Cheaper", "price": 100, "url": "down"},
            "UP": {"sku": "UP", "title": "Costlier", "price": 100, "url": "up"},
            "GONE": {"sku": "GONE", "title": "Gone", "price": 2000, "url": "u"},
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
        self.assertIn("Gone\n原價 NT$2,000", output)

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

    def test_health_message_converts_to_taipei_time(self):
        text = health_message(datetime(2026, 11, 16, 16, 3, 9, tzinfo=timezone.utc), 43)
        self.assertIn("檢查時間：2026-11-17 00:03:09（台北）", text)
        self.assertIn("目前追蹤：43 項商品", text)
        self.assertIn("本次商品檢查成功", text)


class CollectTests(unittest.TestCase):
    def collect_with_pages(self, pages_by_category, previous=None):
        calls = []
        with patch("apple_refurb_monitor.time.sleep") as sleep:
            result = collect(
                tuple(pages_by_category), previous, fake_fetcher(pages_by_category, calls), log=lambda _: None
            )
        return result, calls, sleep

    def test_empty_category_is_reread_when_it_had_products(self):
        previous = {"OLD": product("OLD", "AirPods", 1, "airpods")}
        result, calls, sleep = self.collect_with_pages(
            {"mac": [page_with([tile()])], "airpods": [page_with([]), page_with([tile(sku="AIR", title="AirPods")])]},
            previous,
        )
        self.assertEqual(calls, ["mac", "airpods", "airpods"])
        self.assertIn("AIR", result.products)
        self.assertEqual(sleep.call_count, 1)

    def test_category_still_empty_after_rereads_is_accepted(self):
        empty = page_with([])
        previous = {"OLD": product("OLD", "AirPods", 1, "airpods")}
        result, calls, _ = self.collect_with_pages(
            {"mac": [page_with([tile()])], "airpods": [empty, empty, empty]}, previous
        )
        self.assertEqual(calls.count("airpods"), 3)
        self.assertEqual(set(result.products), {"FTESTTA/A"})

    def test_missing_page_is_reread_when_it_had_products(self):
        previous = {"OLD": product("OLD", "AirPods", 1, "airpods")}
        result, calls, _ = self.collect_with_pages(
            {"mac": [page_with([tile()])], "airpods": [None, page_with([tile(sku="AIR", title="AirPods")])]},
            previous,
        )
        self.assertEqual(calls, ["mac", "airpods", "airpods"])
        self.assertIn("AIR", result.products)
        self.assertEqual(result.problems, ())

    def test_empty_category_without_previous_products_is_not_reread(self):
        _, calls, sleep = self.collect_with_pages({"mac": [page_with([tile()])], "airpods": [page_with([])]})
        self.assertEqual(calls, ["mac", "airpods"])
        sleep.assert_not_called()

    def test_missing_category_page_is_treated_as_empty(self):
        result, calls, _ = self.collect_with_pages(
            {"mac": [page_with([tile()])], "airpods": [None], "ipad": [page_with([])]}
        )
        self.assertEqual(set(result.products), {"FTESTTA/A"})
        self.assertEqual(calls, ["mac", "airpods", "ipad"])
        self.assertEqual(len(result.problems), 1)
        self.assertIn("airpods", result.problems[0])
        self.assertIn("404", result.problems[0])
        self.assertEqual(result.failed, ())

    def test_every_category_missing_stops_with_error(self):
        with self.assertRaises(ValueError):
            self.collect_with_pages({"mac": [None], "ipad": [None]})

    def test_broken_category_keeps_previous_products_without_failing(self):
        previous = {"OLDIPAD": product("OLDIPAD", "iPad", 1, "ipad"), "FTESTTA/A": product()}
        result, _, _ = self.collect_with_pages(
            {"mac": [page_with([tile()])], "ipad": [RuntimeError("無法讀取 ipad: timed out")]}, previous
        )
        self.assertEqual(set(result.products), {"FTESTTA/A", "OLDIPAD"})
        self.assertEqual(len(result.problems), 1)
        self.assertIn("ipad：讀取失敗，沿用上次的 1 項商品", result.problems[0])
        self.assertEqual(result.failed, ("ipad",))

    def test_category_with_changed_page_format_keeps_previous_products(self):
        previous = {"OLDIPAD": product("OLDIPAD", "iPad", 1, "ipad")}
        result, _, _ = self.collect_with_pages(
            {"mac": [page_with([tile()])], "ipad": ["<html>new design</html>"]}, previous
        )
        self.assertIn("OLDIPAD", result.products)
        self.assertIn("REFURB_GRID_BOOTSTRAP", result.problems[0])

    def test_product_listed_in_two_categories_is_one_product_from_the_first(self):
        result, _, _ = self.collect_with_pages(
            {"homepod": [page_with([tile(sku="DUP", title="Cable")])],
             "accessories": [page_with([tile(sku="DUP", title="Cable")])]}
        )
        self.assertEqual(list(result.products), ["DUP"])
        self.assertEqual(result.products["DUP"]["category"], "homepod")

    def test_freshly_read_product_wins_over_one_kept_from_a_broken_category(self):
        previous = {"DUP": product("DUP", "Cable", 1000, "accessories")}
        result, _, _ = self.collect_with_pages(
            {"accessories": [RuntimeError("down")], "homepod": [page_with([tile(sku="DUP", title="Cable", price="800")])]},
            previous,
        )
        self.assertEqual(result.products["DUP"]["price"], 800)

    def test_every_category_broken_still_fails_even_with_previous_products(self):
        previous = {"OLD": product("OLD", "Mac", 1, "mac")}
        with self.assertRaises(ValueError) as raised:
            self.collect_with_pages({"mac": [RuntimeError("down")], "ipad": [RuntimeError("down")]}, previous)
        self.assertIn("所有分類都讀取失敗", str(raised.exception))


class FetchTests(unittest.TestCase):
    def test_fetch_does_not_retry_404(self):
        with patch("apple_refurb_monitor.urlopen", side_effect=http_error(404)) as opener, \
             patch("apple_refurb_monitor.time.sleep") as sleep:
            with self.assertRaises(PageNotFound):
                fetch("https://www.apple.com/x")
        self.assertEqual(opener.call_count, 1)
        sleep.assert_not_called()

    def test_fetch_retries_server_errors(self):
        with patch("apple_refurb_monitor.urlopen", side_effect=http_error(503)) as opener, \
             patch("apple_refurb_monitor.time.sleep"):
            with self.assertRaises(RuntimeError) as raised:
                fetch("https://www.apple.com/x")
        self.assertNotIsInstance(raised.exception, PageNotFound)
        self.assertEqual(opener.call_count, 3)

    def test_fetch_retries_connection_resets_and_cut_off_responses(self):
        ok = MagicMock()
        ok.__enter__.return_value.read.return_value = b"page"
        with patch("apple_refurb_monitor.urlopen", side_effect=[ConnectionResetError(), IncompleteRead(b""), ok]), \
             patch("apple_refurb_monitor.time.sleep"):
            self.assertEqual(fetch("https://www.apple.com/x"), "page")


class StateTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "nested" / "state.json"

    def test_save_and_load_round_trip(self):
        self.assertIsNone(load_state(self.path))
        products = {"B": product("B"), "A": product("A")}
        save_state(self.path, products)
        self.assertEqual(load_state(self.path), products)
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(document["product_count"], 2)
        self.assertEqual(list(document["products"]), ["A", "B"])
        self.assertIn("整修品", self.path.read_text(encoding="utf-8"))

    def test_broken_state_file_is_reported(self):
        self.path.parent.mkdir(parents=True)
        for content in ("{not json", '{"products": []}', "[]"):
            self.path.write_text(content, encoding="utf-8")
            with self.assertRaises(ValueError):
                load_state(self.path)


class TelegramTests(unittest.TestCase):
    config = TelegramConfig("token", "chat")

    def test_missing_credentials(self):
        with self.assertRaises(RuntimeError) as raised:
            telegram_send("hi", TelegramConfig())
        self.assertIn("TELEGRAM_BOT_TOKEN", str(raised.exception))

    def test_sends_message(self):
        with patch("apple_refurb_monitor.urlopen", return_value=response({"ok": True})) as opener:
            telegram_send("hi", self.config)
        body = json.loads(opener.call_args.args[0].data)
        self.assertEqual((body["chat_id"], body["text"]), ("chat", "hi"))

    def test_not_ok_answer_fails(self):
        with patch("apple_refurb_monitor.urlopen", return_value=response({"ok": False, "description": "nope"})):
            with self.assertRaises(RuntimeError) as raised:
                telegram_send("hi", self.config)
        self.assertIn("nope", str(raised.exception))

    def test_client_error_shows_telegram_reason_without_retrying(self):
        error = http_error(400, b'{"ok":false,"description":"Bad Request: chat not found"}')
        with patch("apple_refurb_monitor.urlopen", side_effect=error) as opener, \
             patch("apple_refurb_monitor.time.sleep"):
            with self.assertRaises(RuntimeError) as raised:
                telegram_send("hi", self.config)
        self.assertIn("chat not found", str(raised.exception))
        self.assertNotIn("token", str(raised.exception))
        self.assertEqual(opener.call_count, 1)

    def test_rate_limit_waits_retry_after_then_succeeds(self):
        limited = http_error(429, b'{"ok":false,"description":"Too Many Requests","parameters":{"retry_after":7}}')
        with patch("apple_refurb_monitor.urlopen", side_effect=[limited, response({"ok": True})]), \
             patch("apple_refurb_monitor.time.sleep") as sleep:
            telegram_send("hi", self.config)
        sleep.assert_called_once_with(7.0)

    def test_odd_retry_after_falls_back_to_normal_backoff(self):
        limited = http_error(429, b'{"ok":false,"parameters":{"retry_after":"soon"}}')
        with patch("apple_refurb_monitor.urlopen", side_effect=[limited, response({"ok": True})]),              patch("apple_refurb_monitor.time.sleep") as sleep:
            telegram_send("hi", self.config)
        sleep.assert_called_once_with(1)

    def test_network_errors_are_retried_then_reported(self):
        with patch("apple_refurb_monitor.urlopen", side_effect=ConnectionResetError()) as opener, \
             patch("apple_refurb_monitor.time.sleep"):
            with self.assertRaises(RuntimeError):
                telegram_send("hi", self.config)
        self.assertEqual(opener.call_count, 3)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.state = Path(self.dir.name) / "state.json"
        self.messages = []

    def run_monitor(self, pages, **overrides):
        overrides.setdefault("notify_first_run", False)
        settings = Settings(categories=tuple(pages), state_file=self.state, **overrides)
        with patch("apple_refurb_monitor.time.sleep"):
            return run(settings, self.messages.append, fake_fetcher(pages))

    def test_first_run_saves_baseline_silently_when_notify_disabled(self):
        self.assertEqual(self.run_monitor({"mac": [page_with([tile()])]}), 0)
        self.assertEqual(set(load_state(self.state)), {"FTESTTA/A"})
        self.assertEqual(self.messages, [])

    def test_first_run_notifies_when_enabled(self):
        self.run_monitor({"mac": [page_with([tile()])]}, notify_first_run=True)
        self.assertIn("已啟動", self.messages[0])

    def test_first_run_with_broken_category_does_not_save_baseline(self):
        with self.assertRaises(RuntimeError) as raised:
            self.run_monitor({"mac": [page_with([tile()])], "ipad": [RuntimeError("down")]})
        self.assertIn("ipad", str(raised.exception))
        self.assertIn("從 APPLE_CATEGORIES 拿掉", str(raised.exception))
        self.assertFalse(self.state.exists())

    def test_changes_are_saved_and_sent(self):
        save_state(self.state, {"GONE": product("GONE", "Old Mac", 1000)})
        self.run_monitor({"mac": [page_with([tile()])]})
        self.assertEqual(set(load_state(self.state)), {"FTESTTA/A"})
        self.assertEqual(len(self.messages), 1)
        self.assertIn("🆕 新上架 1｜❌ 已下架 1", self.messages[0])

    def test_regular_run_stays_silent_without_changes(self):
        save_state(self.state, {"FTESTTA/A": product()})
        before = self.state.read_text(encoding="utf-8")
        self.assertEqual(self.run_monitor({"mac": [page_with([tile()])]}), 0)
        self.assertEqual(self.messages, [])
        self.assertEqual(self.state.read_text(encoding="utf-8"), before)

    def test_filters_apply_to_the_saved_products(self):
        self.run_monitor({"mac": [page_with([tile(), tile(sku="AIR", title="MacBook Air")])]},
                         filters=Filters(exclude_keywords=("air",)))
        self.assertEqual(set(load_state(self.state)), {"FTESTTA/A"})

    def test_dry_run_leaves_state_file_untouched(self):
        save_state(self.state, {"GONE": product("GONE")})
        before = self.state.read_text(encoding="utf-8")
        self.run_monitor({"mac": [page_with([tile()])]}, dry_run=True)
        self.assertEqual(self.state.read_text(encoding="utf-8"), before)
        self.assertEqual(len(self.messages), 1)

    def test_health_sent_without_product_changes(self):
        save_state(self.state, {"FTESTTA/A": product()})
        self.run_monitor({"mac": [page_with([tile()])]}, health_check=True)
        self.assertEqual(len(self.messages), 1)
        self.assertIn("Health", self.messages[0])
        self.assertIn("目前追蹤：1 項商品", self.messages[0])
        self.assertIn("（台北）", self.messages[0])

    def test_first_run_also_sends_requested_health(self):
        self.run_monitor({"mac": [page_with([tile()])]}, health_check=True)
        self.assertEqual(len(self.messages), 1)
        self.assertIn("Health", self.messages[0])

    def test_health_lists_category_problems_without_failing_the_run(self):
        save_state(self.state, {"FTESTTA/A": product()})
        result = self.run_monitor({"mac": [page_with([tile()])], "airpods": [None]}, health_check=True)
        self.assertEqual(result, 0)
        self.assertEqual(len(self.messages), 1)
        self.assertIn("有分類需要留意", self.messages[0])
        self.assertIn("⚠️ airpods：Apple 沒有這個分類的頁面（404）", self.messages[0])
        self.assertNotIn("本次商品檢查成功", self.messages[0])

    def test_category_problems_alone_send_no_hourly_message(self):
        save_state(self.state, {"FTESTTA/A": product()})
        self.assertEqual(self.run_monitor({"mac": [page_with([tile()])], "ipad": [RuntimeError("down")]}), 0)
        self.assertEqual(self.messages, [])


class MainTests(unittest.TestCase):
    def test_invalid_setting_is_reported_on_telegram(self):
        env = {"MAX_PRICE_TWD": "6萬", "TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "chat", "DRY_RUN": "false"}
        with patch.dict("os.environ", env), \
             patch("apple_refurb_monitor.run") as run_mock, \
             patch("apple_refurb_monitor.telegram_send") as send:
            self.assertEqual(main([]), 1)
        run_mock.assert_not_called()
        message, config = send.call_args.args
        self.assertIn("執行失敗", message)
        self.assertIn("設定錯誤：MAX_PRICE_TWD 必須是整數", message)
        self.assertEqual(config, TelegramConfig("token", "chat"))

    def test_failed_check_sends_error_message_and_never_health(self):
        env = {"HEALTH_CHECK": "true", "TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "chat", "DRY_RUN": "false"}
        with patch.dict("os.environ", env), \
             patch("apple_refurb_monitor.run", side_effect=ValueError("store unavailable")), \
             patch("apple_refurb_monitor.telegram_send") as send:
            self.assertEqual(main([]), 1)
        self.assertEqual(send.call_count, 1)
        self.assertIn("執行失敗\nstore unavailable", send.call_args.args[0])
        self.assertNotIn("Health", send.call_args.args[0])

    def test_report_failure_command_for_the_workflow(self):
        env = {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "chat", "DRY_RUN": "false"}
        with patch.dict("os.environ", env), patch("apple_refurb_monitor.telegram_send") as send:
            self.assertEqual(main(["--report-failure", "push failed"]), 1)
        self.assertIn("執行失敗\npush failed", send.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
