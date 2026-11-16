# browser-control — управление браузером через CDP

Обёртка над Chrome DevTools Protocol: навигация, скриншоты, текст страницы и
выполнение JS в уже запущенном браузере. Без Selenium, без WebDriver, без
скачивания драйверов.

## Зачем

Selenium тянет драйвер под версию браузера и ломается при каждом обновлении.
Chrome DevTools Protocol — это тот же движок, только по HTTP + WebSocket, и
никаких посредников. Скрипт цепляется к браузеру, запущенному с
`--remote-debugging-port`, и работает с ним напрямую.

## Запуск

Сначала браузер с открытым отладочным портом (пример для Windows):

```bash
chrome.exe --remote-debugging-port=9333 --user-data-dir=%TEMP%\profile
```

Затем:

```bash
python cdp.py nav https://example.com
python cdp.py text            # текстовое содержимое страницы
python cdp.py shot page.png   # скриншот
python cdp.py js "document.title"
```

## Технические решения

| Задача | Решение |
|---|---|
| Выбор вкладки | список вкладок через `/json/list`, берётся первая с `type=page` |
| Ожидание загрузки | опрос готовности документа вместо фиксированного `sleep` |
| Кодировка вывода | `reconfigure` stdout в UTF-8, иначе кириллица в Windows-консоли ломается |
| Скриншот | `Page.captureScreenshot`, base64 декодируется и пишется в файл |
| Зависимости | только стандартная библиотека: `urllib`, `json`, `base64` |

## Структура

```
cdp.py    CLI: nav / text / shot / js
```
