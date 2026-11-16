#!/usr/bin/env python3
"""pricewatch — мониторинг цен конкурентов с историей.

Что делает:
  - обходит магазины постранично (пагинация), склеивает каталог;
  - находит цену конкретного товара по названию, а не первую со страницы;
  - хранит историю цен в SQLite и показывает динамику за день;
  - умеет ретраи с паузой, чтобы сайт не блокировал за частые запросы;
  - пишет отчёт в консоль и CSV для Excel.

Демо работает на локальных HTML-файлах, поэтому запускается без сети:
локальный путь с {page} разворачивается в demo_data/shop_north_p1.html и т.д.

Запуск:
  python pricewatch.py demo_data/config.json
  python pricewatch.py demo_data/config.json --out report.csv
  python pricewatch.py demo_data/config.json --history      # история за дни
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


# --------------------------------------------------------------------------
# модель
# --------------------------------------------------------------------------

@dataclass
class PriceRow:
    """Одна найденная цена: магазин, товар, цена, ссылка."""

    shop: str
    product: str
    price: float
    url: str = ""


@dataclass
class Comparison:
    """Сравнение цены магазина с вашей ценой на тот же товар."""

    product: str
    my_price: float
    shop: str
    their_price: float
    diff: float
    diff_pct: float
    url: str = ""
    prev_price: float | None = None

    @property
    def verdict(self) -> str:
        # diff = my_price - their_price: плюс означает, что наша цена выше.
        if self.diff > 0:
            return "вы дороже"
        if self.diff < 0:
            return "вы дешевле"
        return "одинаково"

    @property
    def trend(self) -> str:
        """Что сделал конкурент с ценой с прошлого запуска."""
        if self.prev_price is None:
            return "новое"
        if self.their_price > self.prev_price:
            return f"выросла +{self.their_price - self.prev_price:.0f} ₽"
        if self.their_price < self.prev_price:
            return f"упала -{self.prev_price - self.their_price:.0f} ₽"
        return "без изменений"


@dataclass
class ShopSource:
    """Источник цен: имя, шаблон адреса и число страниц."""

    name: str
    target: str
    pages: int = 1


@dataclass
class Report:
    rows: list[Comparison] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    pages_fetched: int = 0

    def by_shop(self) -> dict[str, list[Comparison]]:
        out: dict[str, list[Comparison]] = {}
        for row in self.rows:
            out.setdefault(row.shop, []).append(row)
        return out

    def count_dearer(self) -> int:
        return sum(1 for row in self.rows if row.diff < 0)

    def count_dropped(self) -> int:
        return sum(1 for row in self.rows if row.prev_price and row.their_price < row.prev_price)


# --------------------------------------------------------------------------
# разбор чисел
# --------------------------------------------------------------------------

def parse_number(raw: str) -> float | None:
    """'1 299,00 ₽' -> 1299.0 ; '12 999.90' -> 12999.9 ; мусор -> None."""
    cleaned = re.sub(r"[^\d,.]", "", raw)
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "")
    elif cleaned.count(",") == 1 and len(cleaned.split(",")[-1]) == 2:
        cleaned = cleaned.replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if value > 0 else None


# --------------------------------------------------------------------------
# извлечение товара со страницы
# --------------------------------------------------------------------------

CARD_RE = re.compile(
    r"<[^>]+class=[\"'][^\"']*(?:card|product|item)[^\"']*[\"'][^>]*>(?P<body>.*?)</(?:div|li|article)>",
    re.S | re.I,
)
# Карточка с одной ценой может быть без вложенных заголовков:
# <div class="card" data-price="1590"><h3>...</h3></div> или сразу текст.
ANY_CARD_RE = re.compile(
    r"<(?P<tag>div|li|article)[^>]+class=[\"'][^\"']*(?:card|product|item)[^\"']*[\"'][^>]*>"
    r"(?P<body>.*?)</(?P=tag)>",
    re.S | re.I,
)
TITLE_RE = re.compile(r"<(?:h[1-6]|a|span)[^>]*>(?P<title>[^<]{3,120})</(?:h[1-6]|a|span)>", re.I)
PRICE_ATTR_RE = re.compile(r'data-price=["\']([^"\']+)["\']')
PRICE_CLASS_RE = re.compile(r"class=[\"'][^\"']*\bprice\b[^\"']*[\"'][^>]*>([^<]+)<")
LINK_RE = re.compile(r'href=["\']([^"\']+)["\']')


@dataclass
class Card:
    """Карточка товара: название, цена, ссылка."""

    title: str
    price: float
    url: str = ""


def parse_cards(html: str, page_url: str = "") -> list[Card]:
    """Разбирает страницу каталога в список карточек.

    Три стратегии, от точной к запасной: data-price, класс price, JSON-LD.
    В боевом заказе вместо этого — CSS-селектор из конфига, но принцип тот же.
    """
    cards: list[Card] = []

    for match in ANY_CARD_RE.finditer(html):
        # data-price живёт в атрибутах открывающего тега, а не в теле
        opening = match.group(0)[: match.group(0).find(">") + 1]
        body = match.group("body")
        title_match = TITLE_RE.search(body)
        title = (title_match.group("title").strip() if title_match else "").strip()
        if not title:
            # название может быть просто текстом внутри карточки
            plain = re.sub(r"<[^>]+>", " ", body)
            plain = re.sub(r"\s+", " ", plain).strip()
            title = plain[:120]
        if not title:
            continue

        price: float | None = None
        attr = PRICE_ATTR_RE.search(opening)
        if attr:
            price = parse_number(attr.group(1))
        if price is None:
            cls = PRICE_CLASS_RE.search(body)
            if cls:
                price = parse_number(cls.group(1))
        if price is None:
            continue

        link = LINK_RE.search(body)
        url = link.group(1) if link else page_url
        cards.append(Card(title=title, price=price, url=url))

    if cards:
        return cards

    # запасной путь: JSON-LD на странице товара
    for match in re.finditer(r'"price"\s*:\s*"([^"]+)"', html):
        price = parse_number(match.group(1))
        if price:
            name = re.search(r'"name"\s*:\s*"([^"]+)"', html)
            cards.append(Card(title=name.group(1) if name else "", price=price, url=page_url))
            break

    return cards


def match_card(cards: list[Card], pattern: str) -> Card | None:
    """Ищет карточку по подстроке названия. Точное совпадение важнее частичного."""
    pattern = pattern.lower().strip()
    if not pattern:
        return None
    exact = [c for c in cards if c.title.lower().strip() == pattern]
    if exact:
        return exact[0]
    partial = [c for c in cards if pattern in c.title.lower()]
    if not partial:
        # последняя попытка: все слова шаблона встречаются в названии
        words = [w for w in re.split(r"\s+", pattern) if len(w) > 2]
        partial = [c for c in cards if words and all(w in c.title.lower() for w in words)]
    return partial[0] if partial else None


# --------------------------------------------------------------------------
# загрузка страниц: локально или по HTTP, с ретраями
# --------------------------------------------------------------------------

def load_page(target: str, base_dir: Path | None = None, retries: int = 3) -> str:
    """Читает страницу. Локальный файл в демо, HTTP в бою.

    Ретраи с растущей паузой — сайты режут частые запросы, и падать
    на первом таймауте значит терять данные за весь день.
    """
    path = Path(target)
    candidates = [path]
    if base_dir is not None and not path.is_absolute():
        candidates.append(base_dir / path)

    for candidate in candidates:
        if candidate.exists():
            return candidate.read_text(encoding="utf-8", errors="replace")

    if not target.startswith(("http://", "https://")):
        raise FileNotFoundError(target)

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(target, headers={"User-Agent": "pricewatch/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read().decode(resp.headers.get_content_charset() or "utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < retries:
                time.sleep(attempt * 2)  # 2с, 4с — не долбим сервер
    raise RuntimeError(f"{target}: {last_error}")


def expand_pages(target: str, pages: int) -> list[str]:
    """Разворачивает шаблон адреса в список страниц.

    'https://shop.ru/catalog?page={page}' + 3 -> 3 адреса.
    Без {page} пагинации нет — возвращается один адрес.
    """
    if "{page}" not in target:
        return [target]
    return [target.replace("{page}", str(n)) for n in range(1, pages + 1)]


# --------------------------------------------------------------------------
# история цен (SQLite)
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    day        TEXT NOT NULL,
    shop       TEXT NOT NULL,
    product    TEXT NOT NULL,
    price      REAL NOT NULL,
    url        TEXT,
    PRIMARY KEY (day, shop, product)
);
"""


def connect_history(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(SCHEMA)
    conn.commit()
    return conn


def previous_price(conn: sqlite3.Connection, shop: str, product: str) -> float | None:
    """Цена конкурента при последнем запуске до сегодняшнего дня."""
    today = datetime.now().strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT price FROM prices WHERE shop=? AND product=? AND day<? ORDER BY day DESC LIMIT 1",
        (shop, product, today),
    ).fetchone()
    return float(row[0]) if row else None


def save_price(conn: sqlite3.Connection, shop: str, product: str, price: float, url: str) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    conn.execute(
        "INSERT OR REPLACE INTO prices (day, shop, product, price, url) VALUES (?,?,?,?,?)",
        (today, shop, product, price, url),
    )
    conn.commit()


def print_history(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT day, shop, product, price FROM prices ORDER BY product, shop, day"
    ).fetchall()
    if not rows:
        print("История пуста — запустите сбор хотя бы раз.")
        return
    print("\nИСТОРИЯ ЦЕН")
    print("-" * 72)
    current = None
    for day, shop, product, price in rows:
        if product != current:
            if current is not None:
                print()
            print(product)
            current = product
        print(f"  {day}  {shop:<22} {price:>9.0f} ₽")
    print()


# --------------------------------------------------------------------------
# сравнение и отчёт
# --------------------------------------------------------------------------

def build_report(config: dict, base_dir: Path | None = None, conn: sqlite3.Connection | None = None) -> Report:
    report = Report()
    products = config.get("products", [])
    pause = float(config.get("pause_seconds", 0))

    for shop_cfg in config.get("shops", []):
        shop = ShopSource(
            name=shop_cfg["name"],
            target=shop_cfg["target"],
            pages=int(shop_cfg.get("pages", 1)),
        )

        cards: list[Card] = []
        for page_url in expand_pages(shop.target, shop.pages):
            try:
                html = load_page(page_url, base_dir)
            except Exception as exc:  # noqa: BLE001 - в отчёт, а не в падение
                report.errors.append(f"{shop.name}: {exc}")
                continue
            report.pages_fetched += 1
            cards.extend(parse_cards(html, page_url))
            if pause:
                time.sleep(pause)

        if not cards:
            report.errors.append(f"{shop.name}: на страницах нет карточек товаров")
            continue

        for product in products:
            card = match_card(cards, product.get("title_pattern", product["name"]))
            if card is None:
                report.errors.append(f"{shop.name}: «{product['name']}» не найден в каталоге")
                continue

            my_price = float(product["my_price"])
            diff = my_price - card.price
            pct = (diff / my_price * 100.0) if my_price else 0.0
            prev = previous_price(conn, shop.name, product["name"]) if conn else None

            report.rows.append(
                Comparison(
                    product=product["name"],
                    my_price=my_price,
                    shop=shop.name,
                    their_price=card.price,
                    diff=diff,
                    diff_pct=pct,
                    url=card.url,
                    prev_price=prev,
                )
            )
            if conn:
                save_price(conn, shop.name, product["name"], card.price, card.url)

    return report


def render_text(report: Report) -> str:
    lines: list[str] = ["ОТЧЁТ pricewatch", "=" * 78]

    for shop, rows in report.by_shop().items():
        lines.append(f"\n{shop}")
        for row in rows:
            sign = "+" if row.diff >= 0 else "-"
            # одна позиция — одна строка: так отчёт целиком влезает в кадр записи
            lines.append(
                f"  {row.product:<26} у вас {row.my_price:>8.0f} ₽ | "
                f"у них {row.their_price:>8.0f} ₽ | {sign}{abs(row.diff_pct):>5.1f}%  "
                f"[{row.verdict}]  ({row.trend})"
            )

    lines.append("\n" + "=" * 78)
    lines.append(
        f"Позиций: {len(report.rows)}. Страниц обойдено: {report.pages_fetched}. "
        f"Вы дороже в {report.count_dearer()} из них."
    )
    if report.count_dropped():
        lines.append(f"Конкуренты снизили цену: {report.count_dropped()} позиций.")
    if report.errors:
        lines.append(f"Предупреждений: {len(report.errors)}")
        for err in report.errors:
            lines.append(f"  ! {err}")
    return "\n".join(lines)


def write_csv(report: Report, path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(
            ["Магазин", "Товар", "Ваша цена", "Их цена", "Разница ₽", "Разница %",
             "Динамика", "Вердикт", "Ссылка"]
        )
        for row in report.rows:
            writer.writerow(
                [
                    row.shop,
                    row.product,
                    f"{row.my_price:.0f}",
                    f"{row.their_price:.0f}",
                    f"{row.diff:.0f}",
                    f"{row.diff_pct:.1f}",
                    row.trend,
                    row.verdict,
                    row.url,
                ]
            )


def write_xlsx(report: Report, path: Path) -> None:
    """Отчёт в Excel: заголовок, ширина колонок и подсветка «вы дороже».

    Клиенту это понятнее консоли — открыл файл и сразу видно, где проигрываешь.
    openpyxl может отсутствовать: тогда просто говорим, что писать некуда.
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError:
        print("[warn] для .xlsx нужен openpyxl: python -m pip install openpyxl",
              file=sys.stderr)
        return

    headers = ["Магазин", "Товар", "Ваша цена", "Их цена", "Разница ₽",
               "Разница %", "Динамика", "Вердикт", "Ссылка"]
    widths = [22, 34, 13, 13, 13, 12, 14, 16, 30]

    wb = Workbook()
    ws = wb.active
    ws.title = "Цены конкурентов"

    ws.append(headers)
    bold = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="2F5597")
    for cell in ws[1]:
        cell.font = bold
        cell.fill = head_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")

    red = PatternFill("solid", fgColor="FFC7CE")
    green = PatternFill("solid", fgColor="C6EFCE")

    for row in report.rows:
        ws.append([
            row.shop,
            row.product,
            round(row.my_price),
            round(row.their_price),
            round(row.diff),
            round(row.diff_pct, 1),
            row.trend,
            row.verdict,
            row.url,
        ])
        line = ws.max_row
        # diff_pct > 0 — наша цена выше: красным. Ниже — зелёным.
        if row.diff_pct > 0:
            ws.cell(line, 6).fill = red
            ws.cell(line, 8).fill = red
        elif row.diff_pct < 0:
            ws.cell(line, 6).fill = green
            ws.cell(line, 8).fill = green

    for i, width in enumerate(widths, 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = width
    ws.freeze_panes = "A2"

    wb.save(path)
    print(f"[xlsx] отчёт сохранён: {path}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _force_utf8_stdout() -> None:
    """Windows-консоль по умолчанию cp1251 — без этого падает на ₽."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdout()
    parser = argparse.ArgumentParser(description="Мониторинг цен конкурентов")
    parser.add_argument("config", help="путь к config.json")
    parser.add_argument("--out", help="записать CSV-отчёт в файл")
    parser.add_argument("--xlsx", help="записать отчёт в Excel (.xlsx) для владельца")
    parser.add_argument("--db", default="prices.db", help="файл истории цен (SQLite)")
    parser.add_argument("--history", action="store_true", help="показать историю цен")
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    conn = connect_history(Path(args.db))

    if args.history:
        print_history(conn)
        return 0

    report = build_report(config, base_dir=config_path.parent, conn=conn)

    print(render_text(report))
    if args.out:
        write_csv(report, Path(args.out))
        print(f"\nCSV записан: {args.out}")
    if args.xlsx:
        write_xlsx(report, Path(args.xlsx))

    return 0 if report.rows else 1


if __name__ == "__main__":
    sys.exit(main())
