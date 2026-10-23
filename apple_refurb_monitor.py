#!/usr/bin/env python3
"""Monitor Apple Taiwan's refurbished store and notify changes via Telegram."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


BASE_URL = "https://www.apple.com"
DEFAULT_CATEGORIES = ("mac", "ipad", "airpods", "homepod", "appletv", "accessories")
BOOTSTRAP_MARKER = "window.REFURB_GRID_BOOTSTRAP = "
USER_AGENT = "AppleRefurbMonitor/1.0 (+https://github.com/)"


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
            return tuple(x.strip() for x in os.getenv(name, default).split(",") if x.strip())

        raw_max = os.getenv("MAX_PRICE_TWD", "").strip()
        return cls(
            categories=csv("APPLE_CATEGORIES", ",".join(DEFAULT_CATEGORIES)),
            include_keywords=tuple(x.casefold() for x in csv("INCLUDE_KEYWORDS")),
            exclude_keywords=tuple(x.casefold() for x in csv("EXCLUDE_KEYWORDS")),
            max_price=int(raw_max) if raw_max else None,
            state_file=Path(os.getenv("STATE_FILE", "data/state.json")),
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            notify_first_run=os.getenv("NOTIFY_ON_FIRST_RUN", "true").lower() in {"1", "true", "yes"},
            dry_run=os.getenv("DRY_RUN", "false").lower() in {"1", "true", "yes"},
            health_check=os.getenv("HEALTH_CHECK", "false").lower() in {"1", "true", "yes"},
        )


@dataclass(frozen=True)
class Change:
    kind: str
    product: dict[str, Any]
    old_price: int | None = None


def fetch(url: str, attempts: int = 3) -> str:
    error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-TW,zh;q=0.9"})
            with urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8")
        except (HTTPError, URLError, TimeoutError) as exc:
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


def collect(settings: Settings) -> dict[str, dict[str, Any]]:
    all_products: dict[str, dict[str, Any]] = {}
    for category in settings.categories:
        url = f"{BASE_URL}/tw/shop/refurbished/{category}"
        category_products = parse_products(fetch(url), category)
        print(f"{category}: {len(category_products)} 項")
        all_products.update(category_products)
    if not all_products:
        raise ValueError("所有分類均解析不到商品，為避免誤報已停止")
    return {sku: p for sku, p in all_products.items() if matches(p, settings)}


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
            if len(current) + len(addition) > limit and current != header:
                result.append(current.rstrip())
                current = f"🍎 Apple 台灣整修品更新（續）\n{counts}\n\n{section_heading}"
                addition = format_change(change) + "\n\n"
            elif len(current) + len(addition) > limit:
                # Apple product titles and URLs are normally short; retain the
                # complete change even if one exceptional item exceeds limit.
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


def main() -> int:
    settings = Settings.from_env()
    try:
        current = collect(settings)
        checked_at = datetime.now(ZoneInfo("Asia/Taipei"))
        previous = load_state(settings.state_file)
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
            telegram_send(
                "💚 Apple 整修品監控 Health\n"
                "本次商品檢查成功\n"
                f"成功檢查時間：{checked_at:%Y-%m-%d %H:%M:%S}（台北）\n"
                f"目前追蹤：{len(current)} 項商品\n"
                "每小時檢查，商品異動另行通知。",
                settings,
            )
            print("已傳送 Health 通知")
        return 0
    except Exception as exc:
        print(f"錯誤: {exc}", file=sys.stderr)
        try:
            if settings.telegram_token and settings.telegram_chat_id:
                telegram_send(f"⚠️ Apple 整修品監控執行失敗\n{str(exc)[:3500]}", settings)
        except Exception as notify_exc:
            print(f"錯誤通知也傳送失敗: {notify_exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
