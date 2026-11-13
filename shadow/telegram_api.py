"""Minimal Telegram Bot API client (PHASE 8): getUpdates long polling, sendMessage, sendDocument.

- Base URL from TELEGRAM_API_BASE (default https://api.telegram.org): a mirror works the same way.
- Retries with exponential backoff (+ jitter) on ConnectTimeout / ConnectError / ReadTimeout / 5xx and
  honours 429 `retry_after`. Errors never contain the token: every message passes through `redact()`.
- `reachable` / `fail_streak` / `last_error` let /status and the outbox show the link state; nothing
  here blocks scanning or paper trading (callers run it in their own tasks).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time

import httpx

PHASE = 8
IMPLEMENTED = True
log = logging.getLogger("shadow.telegram")
logging.getLogger("httpx").setLevel(logging.WARNING)      # httpx logs full URLs (with the token) at INFO
logging.getLogger("httpcore").setLevel(logging.WARNING)

MAX_TEXT = 4096
RETRYABLE = (httpx.ConnectTimeout, httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout,
             httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError)


class TelegramError(RuntimeError):
    def __init__(self, msg: str, code: int | None = None, retry_after: float | None = None):
        super().__init__(msg)
        self.code, self.retry_after = code, retry_after


class TelegramAPI:
    def __init__(self, token: str, base: str = "https://api.telegram.org", client: httpx.AsyncClient | None = None,
                 timeout: float = 15.0, max_retries: int = 5, backoff_base: float = 2.0, backoff_max: float = 120.0,
                 sleep=asyncio.sleep, rand=random.random):
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is empty")
        self._token = token
        self.base = base.rstrip("/")
        self._client = client
        self._own = client is None
        self.timeout, self.max_retries = timeout, max_retries
        self.backoff_base, self.backoff_max = backoff_base, backoff_max
        self._sleep, self._rand = sleep, rand
        self.reachable: bool | None = None
        self.fail_streak = 0
        self.last_error = ""
        self.last_ok_ts = 0.0
        self.calls = 0
        self.errors = 0
        self.retries = 0
        self.rate_limited = 0

    # -- helpers
    def redact(self, text: str) -> str:
        return str(text).replace(self._token, "<token>") if self._token else str(text)

    def _url(self, method: str) -> str:
        return f"{self.base}/bot{self._token}/{method}"

    def backoff(self, attempt: int) -> float:
        d = min(self.backoff_base * (2 ** attempt), self.backoff_max)
        return d * (0.75 + 0.5 * self._rand())

    async def _client_get(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def close(self) -> None:
        if self._own and self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- core
    async def call(self, method: str, params: dict | None = None, files: dict | None = None,
                   timeout: float | None = None, retries: int | None = None) -> dict | list | bool:
        retries = self.max_retries if retries is None else retries
        attempt = 0
        while True:
            self.calls += 1
            try:
                c = await self._client_get()
                to = httpx.Timeout(timeout or self.timeout, connect=min(10.0, timeout or self.timeout))
                if files:
                    r = await c.post(self._url(method), data={k: (json.dumps(v) if isinstance(v, (dict, list)) else str(v))
                                                             for k, v in (params or {}).items()},
                                     files=files, timeout=to)
                else:
                    r = await c.post(self._url(method), json=params or {}, timeout=to)
                try:
                    body = r.json()
                except ValueError:
                    body = {"ok": False, "description": r.text[:200]}
                if r.status_code == 200 and body.get("ok"):
                    self._ok()
                    return body.get("result")
                desc = self.redact(body.get("description") or f"HTTP {r.status_code}")
                if r.status_code == 429:
                    self.rate_limited += 1
                    ra = float((body.get("parameters") or {}).get("retry_after") or 5)
                    if attempt < retries:
                        attempt += 1
                        self.retries += 1
                        await self._sleep(min(ra + 0.5, self.backoff_max))
                        continue
                    raise TelegramError(f"{method}: 429 {desc}", 429, ra)
                if r.status_code >= 500 and attempt < retries:
                    self._fail(f"{method}: HTTP {r.status_code}")
                    attempt += 1
                    self.retries += 1
                    await self._sleep(self.backoff(attempt))
                    continue
                # 4xx = our request is wrong (bad chat id, 409 conflict, ...): no retry
                self.reachable = True
                self.errors += 1
                self.last_error = f"{method}: {r.status_code} {desc}"
                raise TelegramError(self.last_error, r.status_code)
            except RETRYABLE as e:
                self._fail(f"{method}: {type(e).__name__}")
                if attempt >= retries:
                    raise TelegramError(self.last_error) from None
                attempt += 1
                self.retries += 1
                await self._sleep(self.backoff(attempt))

    def _ok(self) -> None:
        self.reachable = True
        self.fail_streak = 0
        self.last_ok_ts = time.time()

    def _fail(self, msg: str) -> None:
        self.reachable = False
        self.fail_streak += 1
        self.errors += 1
        self.last_error = self.redact(msg)
        log.warning("telegram unreachable (%s), streak %d", self.last_error, self.fail_streak)

    # -- methods
    async def get_me(self) -> dict:
        return await self.call("getMe", retries=2)

    async def get_updates(self, offset: int | None = None, timeout: int = 25) -> list:
        p = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            p["offset"] = offset
        return await self.call("getUpdates", p, timeout=timeout + 10, retries=0)

    async def send_message(self, chat_id: str | int, text: str, retries: int | None = None,
                           reply_markup: dict | None = None) -> list[dict]:
        out = []
        parts = split_text(text)
        for i, part in enumerate(parts):
            p = {"chat_id": chat_id, "text": part, "disable_web_page_preview": True}
            if reply_markup and i == len(parts) - 1:
                p["reply_markup"] = reply_markup
            out.append(await self.call("sendMessage", p, retries=retries))
        return out

    async def set_menu_button(self, chat_id: str | int, text: str, url: str) -> bool:
        """Chat menu button that opens a Mini App (https URL)."""
        return await self.call("setChatMenuButton", {"chat_id": int(chat_id), "menu_button": {
            "type": "web_app", "text": text[:64], "web_app": {"url": url}}}, retries=1)

    async def send_document(self, chat_id: str | int, filename: str, content: bytes, caption: str = "") -> dict:
        return await self.call("sendDocument", {"chat_id": chat_id, "caption": caption[:1000]},
                               files={"document": (filename, content, "text/csv")}, timeout=60)


class LogOnlyAPI:
    """TELEGRAM_LOG_ONLY=true: same interface as TelegramAPI but never touches the network. Sends are written to the
    log (first line), getUpdates returns updates injected with `inject()` (tests / long runs). Nothing is sent and the
    real bot token is not polled, so it can not conflict with the bot running on the phone."""

    def __init__(self, sleep=asyncio.sleep):
        self._sleep = sleep
        self.reachable, self.fail_streak, self.last_error, self.last_ok_ts = True, 0, "", time.time()
        self.calls = self.errors = self.retries = self.rate_limited = 0
        self.pending: list[dict] = []
        self.sent: list[tuple[str, str]] = []
        self._uid = 1000

    def redact(self, text: str) -> str:
        return str(text)

    def backoff(self, attempt: int) -> float:
        return min(2.0 * (2 ** attempt), 120.0)

    def inject(self, chat_id, text: str) -> None:
        self._uid += 1
        self.pending.append({"update_id": self._uid, "message": {"chat": {"id": int(chat_id)}, "text": text,
                                                                  "date": int(time.time())}})

    def _log(self, method: str, text: str) -> dict:
        self.calls += 1
        first = (text or "").splitlines()[0][:120] if text else ""
        self.sent.append((method, first))
        del self.sent[:-2000]
        log.info("[log-only %s] %s", method, first)
        return {"message_id": self.calls}

    async def get_me(self) -> dict:
        return {"username": "log-only"}

    async def get_updates(self, offset: int | None = None, timeout: int = 25) -> list:
        self.calls += 1
        if offset is not None and offset < 0:          # backlog skip at start: nothing old
            return []
        if not self.pending:
            await self._sleep(min(timeout, 5) or 0.2)
        ups, self.pending = [u for u in self.pending if offset is None or u["update_id"] >= offset], []
        return ups

    async def send_message(self, chat_id, text: str, retries=None, reply_markup=None) -> list[dict]:
        return [self._log("sendMessage" + ("+button" if reply_markup else ""), text)]

    async def send_document(self, chat_id, filename: str, content: bytes, caption: str = "") -> dict:
        return self._log("sendDocument", f"{filename} ({len(content)} bytes)")

    async def set_menu_button(self, chat_id, text: str, url: str) -> bool:
        self._log("setChatMenuButton", f"{text} -> {url[:60]}… ({len(url)} chars)")
        return True

    async def close(self) -> None:
        return None


def split_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    """Split on line breaks into Telegram-sized parts (4096 chars)."""
    text = text or "-"
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts


__all__ = ["TelegramAPI", "LogOnlyAPI", "TelegramError", "split_text", "MAX_TEXT"]
