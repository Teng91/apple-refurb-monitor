#!/usr/bin/env python3
"""Monitor Apple Taiwan's refurbished store and notify changes via Telegram."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


BASE_URL = "https://www.apple.com"
# Apple Taiwan removed its refurbished AirPods page (404) in November 2026.
DEFAULT_CATEGORIES = ("mac", "ipad", "homepod", "appletv", "accessories")
BOOTSTRAP_MARKER = "window.REFURB_GRID_BOOTSTRAP = "
USER_AGENT = "AppleRefurbMonitor/1.0"
# Taiwan has no daylight saving time: a fixed UTC+8 avoids needing the tzdata package on Windows
TAIPEI = timezone(timedelta(hours=8), "Asia/Taipei")
# A category that suddenly parses as empty is usually an Apple page glitch, not a sell-out:
# re-read it a few times before reporting every product in it as removed.
EMPTY_CATEGORY_RETRIES = 2
EMPTY_CATEGORY_RETRY_SECONDS = 20
# Telegram's 429 answer says how long to wait; never wait longer than this per attempt.
TELEGRAM_MAX_RETRY_SECONDS = 30

Product = dict[str, Any]
Products = dict[str, Product]
Notifier = Callable[[str], None]
CategoryFetcher = Callable[[str], "Products | None"]
Logger = Callable[[str], None]


# --------------------------------------------------------------------------- settings


def env_flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes"}


def env_csv(name: str, default: str = "") -> tuple[str, ...]:
    # An unset GitHub variable arrives as an empty string: fall back to the default then too
    raw = os.getenv(name, "").strip() or default
    return tuple(x.strip() for x in raw.split(",") if x.strip())


def parse_max_price(raw: str) -> int | None:
    """MAX_PRICE_TWD as an integer; thousands separators such as 60,000 are allowed."""
    cleaned = raw.strip().replace(",", "").replace("，", "").replace(" ", "")
    if not cleaned:
        return None
    if not cleaned.isdigit():
        raise ValueError(f"MAX_PRICE_TWD 必須是整數（例如 60000），目前是「{raw.strip()}」")
    return int(cleaned)


@dataclass(frozen=True)
class Filters:
    include_keywords: tuple[str, ...] = ()
    exclude_keywords: tuple[str, ...] = ()
    max_price: int | None = None

    @classmethod
    def from_env(cls) -> "Filters":
        return cls(
            include_keywords=tuple(x.casefold() for x in env_csv("INCLUDE_KEYWORDS")),
            exclude_keywords=tuple(x.casefold() for x in env_csv("EXCLUDE_KEYWORDS")),
            max_price=parse_max_price(os.getenv("MAX_PRICE_TWD", "")),
        )

    def matches(self, product: Product) -> bool:
        text = f"{product['title']} {product['sku']}".casefold()
        if self.include_keywords and not any(word in text for word in self.include_keywords):
            return False
        if self.exclude_keywords and any(word in text for word in self.exclude_keywords):
            return False
        return self.max_price is None or int(product["price"]) <= self.max_price

    def apply(self, products: Products) -> Products:
        return {sku: p for sku, p in products.items() if self.matches(p)}


@dataclass(frozen=True)
class TelegramConfig:
    token: str = ""
    chat_id: str = ""

    @classmethod
    def from_env(cls) -> "TelegramConfig":
        return cls(os.getenv("TELEGRAM_BOT_TOKEN", "").strip(), os.getenv("TELEGRAM_CHAT_ID", "").strip())


@dataclass(frozen=True)
class Settings:
    categories: tuple[str, ...] = DEFAULT_CATEGORIES
    filters: Filters = field(default_factory=Filters)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    state_file: Path = Path("data/state.json")
    notify_first_run: bool = True
    health_check: bool = False
    # Print Telegram messages instead of sending them and leave the state file untouched
    dry_run: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            categories=env_csv("APPLE_CATEGORIES", ",".join(DEFAULT_CATEGORIES)),
            filters=Filters.from_env(),
            telegram=TelegramConfig.from_env(),
            state_file=Path(os.getenv("STATE_FILE", "data/state.json")),
            notify_first_run=env_flag("NOTIFY_ON_FIRST_RUN", "true"),
            health_check=env_flag("HEALTH_CHECK"),
            dry_run=env_flag("DRY_RUN"),
        )


# --------------------------------------------------------------------------- Apple store


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
            exc.close()  # release the error response's connection
            if exc.code == 404:
                raise PageNotFound(f"找不到頁面 {url}") from exc
            error = exc
        # OSError covers URLError, timeouts and connection resets; HTTPException covers
        # a response cut off half-way (IncompleteRead) and other protocol hiccups.
        except (OSError, HTTPException) as exc:
            error = exc
        if attempt + 1 < attempts:
            time.sleep(2**attempt)
    raise RuntimeError(f"無法讀取 {url}: {error!r}")


def parse_bootstrap(page: str) -> dict[str, Any]:
    start = page.find(BOOTSTRAP_MARKER)
    if start < 0:
        raise ValueError("找不到 REFURB_GRID_BOOTSTRAP，Apple 頁面結構可能已變更")
    start += len(BOOTSTRAP_MARKER)
    payload, _ = json.JSONDecoder().raw_decode(page[start:].lstrip())
    if not isinstance(payload, dict):
        raise ValueError("Apple 商品資料格式不符")
    if payload.get("tiles") is not None and not isinstance(payload.get("tiles"), list):
        raise ValueError("Apple 商品資料缺少 tiles")
    return payload


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def parse_tile(tile: Any, category: str) -> Product | None:
    """One product, or None when the tile is missing a field we need."""
    tile = _as_dict(tile)
    sku = str(tile.get("partNumber") or "").strip().upper()
    title = " ".join(str(tile.get("title") or "").split())
    raw_price = _as_dict(_as_dict(tile.get("price")).get("currentPrice")).get("raw_amount")
    path = str(tile.get("productDetailsUrl") or "").split("?", 1)[0]
    try:
        price = int(float(raw_price))
    except (TypeError, ValueError):
        return None
    if not sku or not title or not path:
        return None
    return {"sku": sku, "title": title, "price": price, "url": urljoin(BASE_URL, path), "category": category}


def parse_products(page: str, category: str, log: Logger = print) -> Products:
    tiles = parse_bootstrap(page).get("tiles") or []
    products: Products = {}
    for tile in tiles:
        product = parse_tile(tile, category)
        if product is not None:
            products[product["sku"]] = product
    if tiles and not products:
        # Every single tile broken means Apple changed the format, not that the category is empty
        raise ValueError(f"{len(tiles)} 個商品都無法解析，Apple 頁面結構可能已變更")
    skipped = len(tiles) - len(products)
    if skipped:
        log(f"{category}: 略過 {skipped} 個格式不符的商品")
    return products


def fetch_category(category: str, log: Logger = print) -> Products | None:
    """Products of one category page, or None when Apple has no page for it (404)."""
    try:
        page = fetch(f"{BASE_URL}/tw/shop/refurbished/{category}")
    except PageNotFound:
        return None
    return parse_products(page, category, log)


@dataclass(frozen=True)
class CollectResult:
    products: Products
    problems: tuple[str, ...] = ()
    # Categories that could not be read; their products are the previous run's
    failed: tuple[str, ...] = ()


def collect(
    categories: tuple[str, ...],
    previous: Products | None = None,
    fetcher: CategoryFetcher | None = None,
    log: Logger = print,
) -> CollectResult:
    """Read every category; one broken category never fails the whole run.

    - Apple removed the page (404): the category counts as empty, e.g. AirPods in November 2026.
    - Any other error (network, page format): its products from the previous run are kept,
      so nothing is reported as removed or added because of it.
    The run only fails when not a single category could be read.
    """
    fetch_one = fetcher or (lambda category: fetch_category(category, log))
    previous = previous or {}
    fresh: Products = {}
    kept: Products = {}  # previous run's products of categories that could not be read
    problems: list[str] = []
    failed: list[str] = []
    read_any = False
    for category in categories:
        old_products = {sku: p for sku, p in previous.items() if p.get("category") == category}
        try:
            category_products = fetch_one(category)
            # A category that suddenly reads as empty or missing is usually a passing glitch
            for _ in range(EMPTY_CATEGORY_RETRIES):
                if category_products or not old_products:
                    break
                log(f"{category}: 0 項，但上次有商品，{EMPTY_CATEGORY_RETRY_SECONDS} 秒後重新讀取")
                time.sleep(EMPTY_CATEGORY_RETRY_SECONDS)
                category_products = fetch_one(category)
        except Exception as exc:
            problems.append(f"{category}：讀取失敗，沿用上次的 {len(old_products)} 項商品（{str(exc)[:200]}）")
            failed.append(category)
            kept.update(old_products)
            continue
        if category_products is None:
            problems.append(f"{category}：Apple 沒有這個分類的頁面（404），當作 0 項")
            continue
        read_any = True
        log(f"{category}: {len(category_products)} 項")
        # Apple lists some products in two categories (e.g. a HomePod accessory under both
        # homepod and accessories). Keyed by SKU it is still one product, notified once; its
        # stored category is the first one read, so APPLE_CATEGORIES order decides it.
        for sku, product in category_products.items():
            fresh.setdefault(sku, product)
    if not read_any:
        raise ValueError("所有分類都讀取失敗，為避免誤報已停止：\n" + "\n".join(problems))
    # What was read this run always wins over what was kept from the previous run
    all_products = {**kept, **fresh}
    if not all_products:
        raise ValueError("所有分類均解析不到商品，為避免誤報已停止")
    return CollectResult(all_products, tuple(problems), tuple(failed))


# --------------------------------------------------------------------------- state


def load_state(path: Path) -> Products | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"狀態檔不是有效的 JSON: {path}（{exc}）") from exc
    products = data.get("products") if isinstance(data, dict) else None
    if not isinstance(products, dict):
        raise ValueError(f"狀態檔格式錯誤: {path}")
    return products


def save_state(path: Path, products: Products) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "product_count": len(products),
        "products": dict(sorted(products.items())),
    }
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- changes and messages


@dataclass(frozen=True)
class Change:
    kind: str
    product: Product
    old_price: int | None = None


def compare(old: Products, new: Products) -> list[Change]:
    changes: list[Change] = []
    for sku in sorted(new.keys() - old.keys()):
        changes.append(Change("added", new[sku]))
    for sku in sorted(old.keys() & new.keys()):
        before, after = int(old[sku]["price"]), int(new[sku]["price"])
        if before != after:
            changes.append(Change("decreased" if after < before else "increased", new[sku], before))
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


def health_message(checked_at: datetime, product_count: int, problems: tuple[str, ...] | list[str] = ()) -> str:
    if problems:
        status = "本次商品檢查完成，但有分類需要留意："
        status += "".join(f"\n⚠️ {problem}" for problem in problems)
    else:
        status = "本次商品檢查成功"
    return (
        "💚 Apple 整修品監控 Health\n"
        f"{status}\n"
        f"檢查時間：{checked_at.astimezone(TAIPEI):%Y-%m-%d %H:%M:%S}（台北）\n"
        f"目前追蹤：{product_count} 項商品\n"
        "商品有異動時會另外通知。"
    )


# --------------------------------------------------------------------------- Telegram


def _telegram_error(exc: HTTPError) -> tuple[str, Any]:
    """Telegram's own explanation (e.g. "Bad Request: chat not found") and its retry_after."""
    try:
        with exc:
            body = json.loads(exc.read().decode("utf-8"))
    except Exception:
        return str(exc.reason or ""), None
    retry_after = _as_dict(_as_dict(body).get("parameters")).get("retry_after")
    return str(_as_dict(body).get("description") or exc.reason), retry_after


def telegram_send(text: str, config: TelegramConfig, attempts: int = 3) -> None:
    if not config.token or not config.chat_id:
        raise RuntimeError("缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID")
    endpoint = f"https://api.telegram.org/bot{config.token}/sendMessage"
    body = json.dumps({"chat_id": config.chat_id, "text": text, "disable_web_page_preview": True}).encode()
    error = ""
    for attempt in range(attempts):
        wait: float = 2**attempt
        try:
            request = Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(request, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8"))
            if result.get("ok"):
                return
            raise RuntimeError(f"Telegram 傳送失敗: {result.get('description', result)}")
        except HTTPError as exc:
            description, retry_after = _telegram_error(exc)
            error = f"Telegram 傳送失敗（HTTP {exc.code}）：{description}"
            # 4xx other than 429 (wrong chat id, bot blocked, bad token) will not fix itself
            if exc.code != 429 and exc.code < 500:
                raise RuntimeError(error) from None
            if isinstance(retry_after, (int, float)) and retry_after > 0:
                wait = min(float(retry_after), TELEGRAM_MAX_RETRY_SECONDS)
        except (OSError, HTTPException) as exc:
            error = f"Telegram 傳送失敗: {exc!r}"
        if attempt + 1 < attempts:
            time.sleep(wait)
    raise RuntimeError(error)


def make_notifier(config: TelegramConfig, dry_run: bool) -> Notifier:
    if dry_run:
        return lambda text: print(f"[DRY RUN Telegram]\n{text}")
    return lambda text: telegram_send(text, config)


# --------------------------------------------------------------------------- run


def report_failure(message: str, notify: Notifier) -> int:
    print(f"錯誤: {message}", file=sys.stderr)
    try:
        notify(f"⚠️ Apple 整修品監控執行失敗\n{message[:3500]}")
    except Exception as notify_exc:
        print(f"錯誤通知也傳送失敗: {notify_exc}", file=sys.stderr)
    return 1


def run(settings: Settings, notify: Notifier, fetcher: CategoryFetcher | None = None) -> int:
    def persist(products: Products) -> None:
        if settings.dry_run:
            print(f"[DRY RUN] 不寫入 {settings.state_file}")
        else:
            save_state(settings.state_file, products)

    previous = load_state(settings.state_file)
    result = collect(settings.categories, previous, fetcher)
    for problem in result.problems:
        # GitHub Actions shows ::warning:: lines as yellow annotations on a green run
        print(f"::warning::{problem}")
    current = settings.filters.apply(result.products)

    if previous is None:
        if result.failed:
            # A baseline without these categories would report all their products as new next time
            raise RuntimeError(
                "第一次建立基準資料時有分類讀取失敗，這次先不建立，下次執行會再試：\n"
                + "\n".join(result.problems)
                + f"\n一直失敗的話，可以先把 {'、'.join(result.failed)} 從 APPLE_CATEGORIES 拿掉，讓其他分類開始監控。"
            )
        persist(current)
        print(f"建立基準資料，共 {len(current)} 項")
        if settings.notify_first_run:
            notify(f"✅ Apple 整修品監控已啟動\n目前追蹤 {len(current)} 項商品。")
    else:
        changes = compare(previous, current)
        if changes:
            # Saved before sending: if a later message fails, the run fails, the workflow does not
            # commit the snapshot, and the next run sends every message again (repeat, never lose).
            persist(current)
            for message in chunks(changes):
                notify(message)
            print(f"已通知 {len(changes)} 項變化")
        else:
            print(f"沒有變化，目前追蹤 {len(current)} 項商品")

    if settings.health_check:
        # Daily health notice (Apps Script dispatches it around midnight Taipei time).
        # Sent only after a successful store check, so it proves the whole chain still works.
        # Category problems that did not fail the run are listed here once a day.
        notify(health_message(datetime.now(timezone.utc), len(current), result.problems))
        print("已傳送 Health 通知")
    return 0


def notifier_from_env() -> Notifier:
    """A notifier that needs only the Telegram secrets, for when the other settings are unusable."""
    return make_notifier(TelegramConfig.from_env(), env_flag("DRY_RUN"))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--report-failure"]:
        # Used by the workflow when a later step (pushing the snapshot) fails
        return report_failure(" ".join(argv[1:]) or "未知錯誤", notifier_from_env())
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        # A mistyped GitHub variable would otherwise fail every run without telling anyone
        return report_failure(f"設定錯誤：{exc}", notifier_from_env())
    notify = make_notifier(settings.telegram, settings.dry_run)
    try:
        return run(settings, notify)
    except Exception as exc:
        return report_failure(str(exc), notify)


if __name__ == "__main__":
    raise SystemExit(main())
