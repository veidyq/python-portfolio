#!/usr/bin/env python3
"""leadhook — Telegram-бот приёма заявок для малого бизнеса.

Что делает:
  ведёт клиента по короткой анкете (имя → услуга → телефон → удобное время),
  сохраняет заявку в CSV и уведомляет владельца в Telegram.
  Данные не теряются: если отправка уведомления упала, заявка всё равно
  остаётся в файле.

Запуск:
  # проверить анкету без Telegram — сценарий из скрипта:
  python leadhook.py --demo

  # боевой режим:
  set TELEGRAM_TOKEN=123456:AA...
  set TELEGRAM_ADMIN_ID=111222333
  python leadhook.py
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path


# --------------------------------------------------------------------------
# модель заявки
# --------------------------------------------------------------------------

@dataclass
class Lead:
    """Одна заявка. Поля — ровно то, что нужно владельцу бизнеса."""

    user_id: int
    name: str = ""
    service: str = ""
    phone: str = ""
    when: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    def is_complete(self) -> bool:
        return all([self.name, self.service, self.phone, self.when])

    def as_text(self) -> str:
        return (
            f"Новая заявка\n"
            f"Имя: {self.name}\n"
            f"Услуга: {self.service}\n"
            f"Телефон: {self.phone}\n"
            f"Время: {self.when}\n"
            f"Пришла: {self.created_at}"
        )


# Шаги анкеты: (поле, вопрос, проверка)
# Проверка возвращает None если ок, или текст ошибки.
def _check_name(value: str) -> str | None:
    return None if 2 <= len(value.strip()) <= 60 else "Имя слишком короткое или длинное"


def _check_phone(value: str) -> str | None:
    digits = sum(ch.isdigit() for ch in value)
    return None if 10 <= digits <= 15 else "Похоже, это не телефон. Введите номер с кодом"


def format_phone(value: str) -> str:
    """Приводит номер к виду +7 911 555-38-62. Если не похоже на российский — оставляет как есть."""
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) == 11 and digits[0] in "78":
        return f"+7 {digits[1:4]} {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"
    if len(digits) == 10:
        return f"+7 {digits[0:3]} {digits[3:6]}-{digits[6:8]}-{digits[8:10]}"
    return value.strip()


def _check_when(value: str) -> str | None:
    return None if value.strip() else "Напишите, когда удобно прийти"


STEPS: list[tuple[str, str, callable]] = [
    ("name", "Как вас зовут?", _check_name),
    ("service", "Какая услуга интересует?", lambda v: None),
    ("phone", "Оставьте телефон для связи", _check_phone),
    ("when", "Когда удобно прийти? (например: вторник, после 18:00)", _check_when),
]

WELCOME = "Здравствуйте! Отвечу на несколько вопросов и запишу вас."
BYE = "Спасибо! Заявка принята, мы свяжемся с вами."
START_HINT = "Напишите /start, чтобы начать."


@dataclass
class BotConfig:
    """Тексты под конкретный бизнес — меняются без правки логики."""

    business: str = "Студия красоты «Лотос»"
    welcome: str = WELCOME
    services: list[str] = field(default_factory=lambda: [
        "стрижка",
        "окрашивание",
        "маникюр",
        "уход за лицом",
    ])
    attacker_note: str = ""

    def greeting(self) -> str:
        lines = [f"{self.welcome}", "", self.business, "", "Выберите услугу:"]
        for i, service in enumerate(self.services, 1):
            lines.append(f"{i}. {service}")
        return "\n".join(lines)


def _check_choice(choices: list[str]):
    """Принимает номер из меню или название услуги, возвращает её название."""

    def check(value: str) -> str | None:
        text = value.strip().lower()
        if text.isdigit() and 1 <= int(text) <= len(choices):
            return None
        if text in {c.lower() for c in choices}:
            return None
        return f"Не понял. Напишите номер от 1 до {len(choices)}"

    return check


# --------------------------------------------------------------------------
# сценарий (без Telegram) — суть бота, которую показывает заказчику демо
# --------------------------------------------------------------------------

class LeadFlow:
    """Машина состояний анкеты. Не знает про сеть — тестируется отдельно."""

    def __init__(self, config: BotConfig | None = None) -> None:
        self.config = config or BotConfig()
        self._state: dict[int, int] = {}   # user_id -> индекс шага
        self._leads: dict[int, Lead] = {}

    def _steps(self) -> list[tuple[str, str, callable]]:
        """Шаги под текущий бизнес: услуги берутся из меню конфига."""
        return [
            ("service", "Напишите номер услуги — 1, 2, 3 или 4.", _check_choice(self.config.services)),
            ("name", "Как вас зовут?", _check_name),
            ("phone", "Оставьте телефон для связи", _check_phone),
            ("when", "Когда удобно прийти? (например: вторник, после 18:00)", _check_when),
        ]

    def start(self, user_id: int) -> str:
        self._leads[user_id] = Lead(user_id=user_id)
        self._state[user_id] = 0
        return f"{self.config.greeting()}\n\n{self._question(0)}"

    def _question(self, step: int) -> str:
        return self._steps()[step][1]

    def handle(self, user_id: int, text: str) -> tuple[str, Lead | None]:
        """Возвращает (ответ_пользователю, готовая_заявка_или_None)."""
        text = text.strip()
        command = text.lower()

        # /start перезапускает анкету в любой момент
        if command in {"/start", "начать", "start"}:
            return self.start(user_id), None

        if user_id not in self._state:
            return START_HINT, None

        if command in {"/cancel", "отмена", "стоп"}:
            del self._state[user_id]
            self._leads.pop(user_id, None)
            return "Ок, отменил. Напишите /start, чтобы начать заново.", None

        step = self._state[user_id]
        field_name, _question, check = self._steps()[step]

        error = check(text)
        if error:
            return f"{error}. {self._question(step)}", None

        lead = self._leads[user_id]
        # выбор из меню: сохраняем название услуги, а не номер
        if field_name == "service":
            choices = self.config.services
            text = choices[int(text) - 1] if text.isdigit() else text
        if field_name == "phone":
            text = format_phone(text)
        setattr(lead, field_name, text)

        next_step = step + 1
        if next_step < len(self._steps()):
            self._state[user_id] = next_step
            return self._question(next_step), None

        del self._state[user_id]
        self._leads.pop(user_id, None)
        return BYE, lead


# --------------------------------------------------------------------------
# хранение и уведомление
# --------------------------------------------------------------------------

CSV_HEADER = ["created_at", "user_id", "name", "service", "phone", "when"]


def save_lead(lead: Lead, path: Path) -> None:
    """Дописывает заявку в CSV. Файл создаётся с заголовком при первом сохранении.

    Пишем в utf-8 без BOM: BOM ломает чтение сторонними скриптами и не нужен,
    если файл открывают в Excel через «Данные → Из текста».
    """
    new_file = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_HEADER, delimiter=";")
        if new_file:
            writer.writeheader()
        writer.writerow(asdict(lead))


def send_telegram(token: str, chat_id: str, text: str, timeout: float = 15.0) -> bool:
    """Отправляет сообщение через Bot API. Возвращает успех, не бросает исключение."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return bool(body.get("ok"))
    except Exception as exc:  # noqa: BLE001 - бот не должен падать из-за сети
        print(f"[warn] не удалось отправить уведомление: {exc}", file=sys.stderr)
        return False


def get_updates(token: str, offset: int | None = None, timeout: int = 30) -> list[dict]:
    """Long polling: забирает новые сообщения."""
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    url = f"https://api.telegram.org/bot{token}/getUpdates?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=timeout + 10) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if not body.get("ok"):
        raise RuntimeError(f"Telegram API: {body.get('description')}")
    return body.get("result", [])


# --------------------------------------------------------------------------
# режимы запуска
# --------------------------------------------------------------------------

def _bubble(text: str, incoming: bool, width: int = 44) -> list[str]:
    """Рисует сообщение как в мессенджере: слева клиент, справа бот."""
    lines: list[str] = []
    for raw in text.splitlines() or [""]:
        # перенос длинных строк по словам, чтобы пузырь не вылезал за экран
        chunk = raw
        while len(chunk) > width:
            cut = chunk.rfind(" ", 0, width + 1)
            cut = cut if cut > 0 else width
            lines.append(chunk[:cut])
            chunk = chunk[cut:].lstrip()
        lines.append(chunk)

    inner = max(len(line) for line in lines) + 2
    corners = ("╭", "╮", "╰", "╯") if incoming else ("╭", "╮", "╰", "╯")
    top, bottom = corners[0] + "─" * inner + corners[1], corners[2] + "─" * inner + corners[3]
    body = [f"│ {line.ljust(inner - 2)} │" for line in lines]
    block = [top, *body, bottom]

    indent = 0 if incoming else 6
    return [" " * indent + line for line in block]


def _print_message(text: str, incoming: bool, pause: float) -> None:
    """Печатает пузырь с паузой — так GIF читается как живой диалог."""
    for line in _bubble(text, incoming):
        print(line)
    print()
    if pause:
        time.sleep(pause)


def run_demo(leads_path: Path, config: BotConfig | None = None, pause: float = 0.0) -> int:
    """Показывает диалог целиком и сохраняет заявку — то, что видит заказчик."""
    flow = LeadFlow(config)
    user = 42
    script = [
        "/start",
        "2",               # номер из меню — бот подставит «окрашивание»
        "Анна",
        "72",              # специально неверно: проверка должна отсеять
        "+7 900 123-45-67",
        "суббота, до 14:00",
    ]

    title = flow.config.business
    print(f"\n  {title}")
    print(f"  {'─' * (len(title) + 20)}")
    print("  бот · онлайн\n")

    for message in script:
        reply, lead = flow.handle(user, message)
        _print_message(message, incoming=True, pause=pause)
        _print_message(reply, incoming=False, pause=pause)
        if lead:
            save_lead(lead, leads_path)
            print("  ── владельцу пришло в Telegram ──\n")
            _print_message(lead.as_text(), incoming=True, pause=pause)
            print(f"  ── заявка сохранена в {leads_path.name} ──\n")

    print("=== СОДЕРЖИМОЕ leads.csv ===")
    print(leads_path.read_text(encoding="utf-8-sig"))
    return 0


def run_bot(token: str, admin_id: str, leads_path: Path,
            config: BotConfig | None = None) -> int:
    """Боевой режим: long polling + сохранение + уведомление владельцу."""
    flow = LeadFlow(config)
    offset: int | None = None
    print("Бот запущен. Ctrl+C для остановки.", flush=True)

    while True:
        try:
            updates = get_updates(token, offset)
        except KeyboardInterrupt:
            print("\nОстановлен.")
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] getUpdates: {exc}", file=sys.stderr)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            message = update.get("message") or {}
            user_id = message.get("from", {}).get("id")
            text = message.get("text", "")
            if not user_id or not text:
                continue

            reply, lead = flow.handle(user_id, text)
            send_telegram(token, str(user_id), reply)
            if lead:
                save_lead(lead, leads_path)
                send_telegram(token, admin_id, lead.as_text())
                print(f"[заявка] {lead.name} · {lead.service} · {lead.phone}", flush=True)


def load_env_file(path: Path) -> None:
    """Читает KEY=VALUE из файла рядом со скриптом. Уже заданные переменные не трогает."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    parser = argparse.ArgumentParser(description="Telegram-бот приёма заявок")
    parser.add_argument("--demo", action="store_true", help="прогнать сценарий без Telegram")
    parser.add_argument("--export", default="leads.csv", help="путь к CSV с заявками")
    parser.add_argument("--slow", type=float, default=0.0,
                        help="пауза между сообщениями, сек — для записи видео")
    parser.add_argument("--env", default=str(Path(__file__).with_name(".env")),
                        help="файл с TELEGRAM_TOKEN и TELEGRAM_ADMIN_ID")
    args = parser.parse_args(argv)

    leads_path = Path(args.export)

    if args.demo:
        return run_demo(leads_path, pause=args.slow)

    load_env_file(Path(args.env))

    token = os.environ.get("TELEGRAM_TOKEN")
    admin_id = os.environ.get("TELEGRAM_ADMIN_ID")
    if not token or not admin_id:
        print("Нужны TELEGRAM_TOKEN и TELEGRAM_ADMIN_ID.", file=sys.stderr)
        print(f"Положите их в файл {args.env} строками:", file=sys.stderr)
        print("  TELEGRAM_TOKEN=123456:AA...", file=sys.stderr)
        print("  TELEGRAM_ADMIN_ID=111222333", file=sys.stderr)
        print("Для проверки без сети: python leadhook.py --demo", file=sys.stderr)
        return 2
    return run_bot(token, admin_id, leads_path)


if __name__ == "__main__":
    sys.exit(main())
