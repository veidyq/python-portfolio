"""Управление Яндекс Браузером через CDP-порт 9333.

Профиль: .freelance-profile (отдельный, без личных сессий).
Использование:
  python -I cdp.py nav <url>      открыть адрес в первой вкладке
  python -I cdp.py shot <file>    сделать скриншот
  python -I cdp.py text           текстовое содержимое страницы
  python -I cdp.py js "<код>"     выполнить JS и вернуть результат
"""
import json
import sys
import base64
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PORT = 9333


def _pages():
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=10) as r:
        return json.load(r)


def _target_url():
    for t in _pages():
        if t["type"] == "page":
            return t["webSocketDebuggerUrl"]
    raise RuntimeError("нет открытых вкладок")


class Session:
    def __init__(self):
        import websocket

        self.ws = websocket.create_connection(_target_url(), timeout=45)
        self.n = 0

    def call(self, method, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def close(self):
        self.ws.close()


def nav(url):
    s = Session()
    try:
        s.call("Page.enable")
        s.call("Page.navigate", url=url)
    finally:
        s.close()


def shot(path):
    s = Session()
    try:
        res = s.call("Page.captureScreenshot", format="png", captureBeyondViewport=False)
        with open(path, "wb") as f:
            f.write(base64.b64decode(res["data"]))
        print("saved", path)
    finally:
        s.close()


def text():
    s = Session()
    try:
        res = s.call(
            "Runtime.evaluate",
            expression="document.body ? document.body.innerText : '(пусто)'",
            returnByValue=True,
        )
        print(res["result"].get("value", ""))
    finally:
        s.close()


def js(code):
    s = Session()
    try:
        res = s.call("Runtime.evaluate", expression=code, returnByValue=True, awaitPromise=True)
        print(json.dumps(res["result"].get("value"), ensure_ascii=False, indent=2))
    finally:
        s.close()


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "nav":
        nav(sys.argv[2])
    elif cmd == "shot":
        shot(sys.argv[2])
    elif cmd == "text":
        text()
    elif cmd == "js":
        js(sys.argv[2])
    else:
        raise SystemExit(f"неизвестная команда: {cmd}")
