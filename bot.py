"""Следит за поисковой выдачей Avito и сохраняет ссылки на новые подходящие объявления."""

import argparse
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

import yaml
from playwright.sync_api import BrowserContext, Page, sync_playwright

BASE_URL = "https://www.avito.ru"
SORT_BY_DATE = "104"
SEEN_LIMIT = 3000

EXTRACT_ITEMS_JS = """
() => [...document.querySelectorAll('[data-marker="item"]')].map(el => {
    const a = el.querySelector('[data-marker="item-title"]');
    const price = el.querySelector('meta[itemprop="price"]');
    return {
        id: el.dataset.itemId || el.id,
        title: a ? (a.title || a.textContent.trim()) : '',
        href: a ? a.href : '',
        price: price ? parseInt(price.content, 10) : null,
    };
})
"""


@dataclass
class Search:
    name: str
    url: str
    price_min: int | None = None
    price_max: int | None = None
    include: list[re.Pattern] = field(default_factory=list)
    exclude: list[re.Pattern] = field(default_factory=list)

    @classmethod
    def from_config(cls, raw: dict) -> "Search":
        url = raw.get("url") or build_url(raw)
        compile_all = lambda pats: [re.compile(p, re.IGNORECASE) for p in pats or []]
        return cls(
            name=raw["name"],
            url=url,
            price_min=raw.get("price_min"),
            price_max=raw.get("price_max"),
            include=compile_all(raw.get("include")),
            exclude=compile_all(raw.get("exclude")),
        )

    def matches(self, item: dict) -> bool:
        price = item["price"]
        if self.price_min is not None and (price is None or price < self.price_min):
            return False
        if self.price_max is not None and (price is None or price > self.price_max):
            return False
        title = item["title"]
        if self.include and not any(p.search(title) for p in self.include):
            return False
        return not any(p.search(title) for p in self.exclude)


def build_url(raw: dict) -> str:
    path = "/".join(filter(None, [raw.get("region", "all"), raw.get("category")]))
    params = {"q": raw["query"], "s": SORT_BY_DATE}
    if raw.get("price_min") is not None:
        params["pmin"] = raw["price_min"]
    if raw.get("price_max") is not None:
        params["pmax"] = raw["price_max"]
    return f"{BASE_URL}/{path}?{urlencode(params)}"


def clean_link(href: str) -> str:
    parts = urlsplit(href)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


COLOR = sys.stdout.isatty()
if COLOR and os.name == "nt":
    os.system("")  # включает обработку ANSI-кодов в консоли Windows


def price_text(price: int | None) -> str:
    text = f"{price:,} ₽".replace(",", " ") if price is not None else "цена не указана"
    return f"\033[1;32m{text}\033[0m" if COLOR else text


def hyperlink(url: str, text: str) -> str:
    """Кликабельный текст (OSC 8) — работает в iTerm2 и большинстве современных терминалов."""
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\" if COLOR else text


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


class SeenStore:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, list[str]] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def has_baseline(self, search: str) -> bool:
        return search in self.data

    def add(self, search: str, ids: list[str]) -> list[str]:
        """Запоминает id и возвращает те, что раньше не встречались."""
        known = self.data.setdefault(search, [])
        known_set = set(known)
        new = [i for i in ids if i not in known_set]
        known.extend(new)
        del known[:-SEEN_LIMIT]
        return new

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")


def is_blocked(page: Page) -> bool:
    return "Доступ ограничен" in page.title()


def wait_until_unblocked(page: Page, headless: bool) -> None:
    if headless:
        sys.exit("Avito показывает капчу. Запустите с headless: false и решите её в окне браузера.")
    log("Avito просит пройти капчу — решите её в окне браузера, бот продолжит сам.")
    while not page.is_closed() and is_blocked(page):
        page.wait_for_timeout(2000)
    if page.is_closed():
        return
    log("Капча пройдена.")


def fetch_items(page: Page, url: str, headless: bool) -> list[dict]:
    page.goto(url, wait_until="domcontentloaded")
    if is_blocked(page):
        wait_until_unblocked(page, headless)
        page.goto(url, wait_until="domcontentloaded")
    try:
        page.wait_for_selector('[data-marker="item"]', timeout=15000)
    except Exception:
        log(f"На странице не найдено объявлений: {page.title()!r}")
        return []
    return [i for i in page.evaluate(EXTRACT_ITEMS_JS) if i["id"] and i["href"]]


def process(search: Search, items: list[dict], seen: SeenStore, output: Path) -> None:
    first_run = not seen.has_baseline(search.name)
    new_ids = set(seen.add(search.name, [i["id"] for i in items]))
    if first_run:
        log(f"[{search.name}] первый запуск: запомнил {len(new_ids)} текущих объявлений")
        return
    found = [i for i in items if i["id"] in new_ids and search.matches(i)]
    log(f"[{search.name}] объявлений: {len(items)}, новых: {len(new_ids)}, подходящих: {len(found)}")
    if not found:
        return
    with output.open("a", encoding="utf-8") as f:
        for item in found:
            link = clean_link(item["href"])
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}\t{item['price']}\t{item['title']}\t{link}\n")
            log(f"  НОВОЕ: {price_text(item['price'])} | {hyperlink(link, item['title'])}\n    {link}")


def open_context(p, cfg: dict) -> BrowserContext:
    return p.chromium.launch_persistent_context(
        cfg.get("profile_dir", ".browser_profile"),
        headless=cfg.get("headless", False),
        locale="ru-RU",
        timezone_id="Europe/Moscow",
        viewport={"width": 1280, "height": 900},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.add_argument("--once", action="store_true", help="один цикл и выход")
    args = parser.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    searches = [Search.from_config(s) for s in cfg["searches"]]
    seen = SeenStore(Path(cfg.get("seen_file", "seen.json")))
    output = Path(cfg.get("output_file", "found.txt"))
    headless = cfg.get("headless", False)
    interval = cfg.get("interval_seconds", 10)
    jitter = cfg.get("jitter_seconds", 0)

    with sync_playwright() as p:
        context = open_context(p, cfg)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            while True:
                for search in searches:
                    if page.is_closed():
                        log("Окно браузера закрыто — останавливаюсь.")
                        return
                    try:
                        items = fetch_items(page, search.url, headless)
                    except Exception as e:
                        log(f"[{search.name}] ошибка запроса: {e}")
                        continue
                    process(search, items, seen, output)
                    seen.save()
                if args.once:
                    break
                time.sleep(interval + random.uniform(0, jitter))
        except KeyboardInterrupt:
            log("Остановлено.")
        finally:
            context.close()


if __name__ == "__main__":
    main()
