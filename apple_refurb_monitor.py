#!/usr/bin/env python3
"""Monitor Apple Taiwan's refurbished store and notify changes via Telegram."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


BASE_URL = "https://www.apple.com"
# Apple Taiwan removed its refurbished AirPods page (404) in November 2026.
DEFAULT_CATEGORIES = ("mac", "ipad", "homepod", "appletv", "accessories")
BOOTSTRAP_MARKER = "window.REFURB_GRID_BOOTSTRAP = "
USER_AGENT = "AppleRefurbMonitor/1.0 (+https://github.com/)"
# Taiwan has no daylight saving time: a fixed UTC+8 avoids needing the tzdata package on Windows
TAIPEI = timezone(timedelta(hours=8), "Asia/Taipei")
# A category that suddenly parses as empty is usually an Apple page glitch, not a sell-out:
# re-read it a few times before reporting every product in it as removed.
EMPTY_CATEGORY_RETRIES = 2
EMPTY_CATEGORY_RETRY_SECONDS = 20


@dataclass(frozen=True)
class Settings:
    categories: tuple[str, ...]
    include_keywords: tuple[str, ...]
    exclude_keywords: tuple[str, ...]
    max_price: int | None
    state_file: Path
    telegram_token: str
    telegram_chat_id: str
    notify_first_run: bool
    dry_run: bool
    health_check: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        def csv(name: str, default: str = "") -> tuple[str, ...]:
            # An unset GitHub variable arrives as an empty string: fall back to the default then too
            raw = os.getenv(name, "").strip() or default
            return tuple(x.strip() for x in raw.split(",") if x.strip())

        return cls(
            categories=csv("APPLE_CATEGORIES", ",".join(DEFAULT_CATEGORIES)),
            include_keywords=tuple(x.casefold() for x in csv("INCLUDE_KEYWORDS")),
            exclude_keywords=tuple(x.casefold() for x in csv("EXCLUDE_KEYWORDS")),
            max_price=parse_max_price(os.getenv("MAX_PRICE_TWD", "")),
            state_file=Path(os.getenv("STATE_FILE", "data/state.json")),
            notify_first_run=env_flag("NOTIFY_ON_FIRST_RUN", "true"),
            health_check=env_flag("HEALTH_CHECK"),
            **telegram_env(),
        )

    @classmethod
    def notifier_only(cls) -> "Settings":
        """Just enough to send a Telegram message when the other settings cannot be read."""
        return cls((), (), (), None, Path("data/state.json"), notify_first_run=False, **telegram_env())


def env_flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes"}


def telegram_env() -> dict[str, Any]:
    return {
        "telegram_token": os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        "dry_run": env_flag("DRY_RUN"),
    }


def parse_max_price(raw: str) -> int | None:
    """MAX_PRICE_TWD as an integer; thousands separators such as 60,000 are allowed."""
    cleaned = raw.strip().replace(",", "").replace("，", "").replace(" ", "")
    if not cleaned:
        return None
    if not cleaned.isdigit():
        raise ValueError(f"MAX_PRICE_TWD 必須是整數（例如 60000），目前是「{raw.strip()}」")
    return int(cleaned)


@dataclass(frozen=True)
class Change:
    kind: str
    product: dict[str, Any]
    old_price: int | None = None


class PageNotFound(RuntimeError):
    """The page answered 404: retrying will not help."""


def fetch(url: str, attempts: int = 3) -> str:
    error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-TW,zh;q=0.9"})
            with urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8")
        except HTTPError as exc:
            if exc.code == 404:
                raise PageNotFound(f"找不到頁面 {url}") from exc
            error = exc
            if attempt + 1 < attempts:
                time.sleep(2**attempt)
        except (URLError, TimeoutError) as exc:
            error = exc
            if attempt + 1 < attempts:
                time.sleep(2**attempt)
    raise RuntimeError(f"無法讀取 {url}: {error}")


def parse_bootstrap(page: str) -> dict[str, Any]:
    start = page.find(BOOTSTRAP_MARKER)
    if start < 0:
        raise ValueError("找不到 REFURB_GRID_BOOTSTRAP，Apple 頁面結構可能已變更")
    start += len(BOOTSTRAP_MARKER)
    payload, _ = json.JSONDecoder().raw_decode(page[start:].lstrip())
    if payload.get("tiles") is not None and not isinstance(payload.get("tiles"), list):
        raise ValueError("Apple 商品資料缺少 tiles")
    return payload


def parse_products(page: str, category: str) -> dict[str, dict[str, Any]]:
    products: dict[str, dict[str, Any]] = {}
    for tile in parse_bootstrap(page).get("tiles") or []:
        sku = str(tile.get("partNumber", "")).strip().upper()
        title = " ".join(str(tile.get("title", "")).split())
        price_data = tile.get("price", {}).get("currentPrice", {})
        raw_price = price_data.get("raw_amount")
        path = str(tile.get("productDetailsUrl", "")).split("?", 1)[0]
        if not sku or not title or raw_price is None or not path:
            continue
        products[sku] = {
            "sku": sku,
            "title": title,
            "price": int(float(raw_price)),
            "url": urljoin(BASE_URL, path),
            "category": category,
        }
    return products


def matches(product: dict[str, Any], settings: Settings) -> bool:
    text = f"{product['title']} {product['sku']}".casefold()
    if settings.include_keywords and not any(word in text for word in settings.include_keywords):
        return False
    if settings.exclude_keywords and any(word in text for word in settings.exclude_keywords):
        return False
    return settings.max_price is None or int(product["price"]) <= settings.max_price


def fetch_category(category: str) -> dict[str, dict[str, Any]] | None:
    """Products of one category page, or None when Apple has no page for it (404)."""
    try:
        page = fetch(f"{BASE_URL}/tw/shop/refurbished/{category}")
    except PageNotFound:
        return None
    return parse_products(page, category)


def collect(
    settings: Settings, previous: dict[str, dict[str, Any]] | None = None
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Read every category and return (matching products, problems found along the way).

    One broken category never fails the whole run:
    - Apple removed the page (404): the category counts as empty, e.g. AirPods in November 2026.
    - Any other error (network, page format): its products from the previous run are kept,
      so nothing is reported as removed or added because of it.
    The run only fails when not a single category could be read.
    """
    previous = previous or {}
    all_products: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    read_any = False
    for category in settings.categories:
        old_products = {sku: p for sku, p in previous.items() if p.get("category") == category}
        try:
            category_products = fetch_category(category)
            # A category that suddenly reads as empty or missing is usually a passing glitch
            for _ in range(EMPTY_CATEGORY_RETRIES):
                if category_products or not old_products:
                    break
                print(f"{category}: 0 項，但上次有商品，{EMPTY_CATEGORY_RETRY_SECONDS} 秒後重新讀取")
                time.sleep(EMPTY_CATEGORY_RETRY_SECONDS)
                category_products = fetch_category(category)
        except Exception as exc:
            problems.append(f"{category}：讀取失敗，沿用上次的 {len(old_products)} 項商品（{str(exc)[:200]}）")
            all_products.update(old_products)
            continue
        if category_products is None:
            problems.append(f"{category}：Apple 沒有這個分類的頁面（404），當作 0 項")
            continue
        read_any = True
        print(f"{category}: {len(category_products)} 項")
        all_products.update(category_products)
    for problem in problems:
        # GitHub Actions shows ::warning:: lines as yellow annotations on a green run
        print(f"::warning::{problem}")
    if not read_any:
        raise ValueError("所有分類都讀取失敗，為避免誤報已停止：\n" + "\n".join(problems))
    if not all_products:
        raise ValueError("所有分類均解析不到商品，為避免誤報已停止")
    return {sku: p for sku, p in all_products.items() if matches(p, settings)}, problems


def load_state(path: Path) -> dict[str, dict[str, Any]] | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    products = data.get("products")
    if not isinstance(products, dict):
        raise ValueError(f"狀態檔格式錯誤: {path}")
    return products


def save_state(path: Path, products: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "product_count": len(products),
        "products": dict(sorted(products.items())),
    }
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def compare(old: dict[str, dict[str, Any]], new: dict[str, dict[str, Any]]) -> list[Change]:
    changes: list[Change] = []
    for sku in sorted(new.keys() - old.keys()):
        changes.append(Change("added", new[sku]))
    for sku in sorted(old.keys() & new.keys()):
        before, after = old[sku], new[sku]
        if int(before["price"]) != int(after["price"]):
            kind = "decreased" if int(after["price"]) < int(before["price"]) else "increased"
            changes.append(Change(kind, after, int(before["price"])))
    for sku in sorted(old.keys() - new.keys()):
        changes.append(Change("removed", old[sku]))
    return changes


def format_change(change: Change) -> str:
    product = change.product
    price = int(product["price"])
    if change.kind == "removed":
        price_line = f"原價 NT${price:,}"
    elif change.kind in {"decreased", "increased"}:
        price_line = f"NT${change.old_price:,} → NT${price:,}"
    else:
        price_line = f"NT${price:,}"
    return f"{product['title']}\n{price_line}\n{product['url']}"


def chunks(changes: list[Change], limit: int = 3900) -> list[str]:
    groups = (
        ("added", "🆕 新上架"),
        ("removed", "❌ 已下架"),
        ("decreased", "📉 降價"),
        ("increased", "📈 漲價"),
    )
    grouped = {kind: [change for change in changes if change.kind == kind] for kind, _ in groups}
    counts = "｜".join(f"{label} {len(grouped[kind])}" for kind, label in groups)
    header = f"🍎 Apple 台灣整修品更新\n{counts}\n\n"

    result: list[str] = []
    current = header
    for kind, label in groups:
        if not grouped[kind]:
            continue
        section_heading = f"{label}（{len(grouped[kind])}）\n"
        for index, change in enumerate(grouped[kind]):
            addition = (section_heading if index == 0 else "") + format_change(change) + "\n\n"
            # Never split off a message that only has the header: a single oversized
            # change (unusual, titles and URLs are short) is kept whole instead.
            if len(current) + len(addition) > limit and current != header:
                result.append(current.rstrip())
                current = f"🍎 Apple 台灣整修品更新（續）\n{counts}\n\n{section_heading}"
                addition = format_change(change) + "\n\n"
            current += addition
    if current != header:
        result.append(current.rstrip())
    return result


def telegram_send(text: str, settings: Settings) -> None:
    if settings.dry_run:
        print(f"[DRY RUN Telegram]\n{text}")
        return
    if not settings.telegram_token or not settings.telegram_chat_id:
        raise RuntimeError("缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID")
    endpoint = f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage"
    body = json.dumps({"chat_id": settings.telegram_chat_id, "text": text, "disable_web_page_preview": True}).encode()
    request = Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(f"Telegram 傳送失敗: {result}")


def health_message(checked_at: datetime, product_count: int, problems: list[str] | tuple[str, ...] = ()) -> str:
    if problems:
        status = "本次商品檢查完成，但有分類需要留意："
        status += "".join(f"\n⚠️ {problem}" for problem in problems)
    else:
        status = "本次商品檢查成功"
    return (
        "💚 Apple 整修品監控 Health\n"
        f"{status}\n"
        f"檢查時間：{checked_at:%Y-%m-%d %H:%M:%S}（台北）\n"
        f"目前追蹤：{product_count} 項商品\n"
        "商品有異動時會另外通知。"
    )


def report_failure(message: str, settings: Settings) -> int:
    print(f"錯誤: {message}", file=sys.stderr)
    try:
        if settings.dry_run or (settings.telegram_token and settings.telegram_chat_id):
            telegram_send(f"⚠️ Apple 整修品監控執行失敗\n{message[:3500]}", settings)
    except Exception as notify_exc:
        print(f"錯誤通知也傳送失敗: {notify_exc}", file=sys.stderr)
    return 1


def main() -> int:
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        # A mistyped GitHub variable would otherwise fail every run without telling anyone
        return report_failure(f"設定錯誤：{exc}", Settings.notifier_only())
    try:
        previous = load_state(settings.state_file)
        current, problems = collect(settings, previous)
        checked_at = datetime.now(TAIPEI)
        if previous is None:
            save_state(settings.state_file, current)
            print(f"建立基準資料，共 {len(current)} 項")
            if settings.notify_first_run:
                telegram_send(f"✅ Apple 整修品監控已啟動\n目前追蹤 {len(current)} 項商品。", settings)
        else:
            changes = compare(previous, current)
            if changes:
                save_state(settings.state_file, current)
                for message in chunks(changes):
                    telegram_send(message, settings)
                print(f"已通知 {len(changes)} 項變化")
            else:
                print(f"沒有變化，目前追蹤 {len(current)} 項商品")
        if settings.health_check:
            # Daily health notice (Apps Script dispatches it around midnight Taipei time).
            # Sent only after a successful store check, so it proves the whole chain still works.
            # Category problems that did not fail the run are listed here once a day.
            telegram_send(health_message(checked_at, len(current), problems), settings)
            print("已傳送 Health 通知")
        return 0
    except Exception as exc:
        return report_failure(str(exc), settings)


if __name__ == "__main__":
    raise SystemExit(main())
