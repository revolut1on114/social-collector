"""HTTP-клієнт, що поважає ліміти джерела.

- пейсинг: мінімальна пауза між запитами до одного інстансу;
- адаптивне гальмування: коли X-RateLimit-Remaining падає нижче порогу, розтягуємо запити до Reset;
- 429: чекаємо Retry-After / X-RateLimit-Reset (а не вгадуємо), або перемикаємось на інший токен;
- 5xx і мережеві збої: експоненційний backoff з jitter;
- інші 4xx (401/403/404/410/422): не повторюємо — це не тимчасова проблема;
- кожна відповідь логуються з кодом і заголовками лімітів (емпіричний аналіз для звіту).
"""

from __future__ import annotations

import hashlib
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable

import requests

from collector.config import Settings

log = logging.getLogger("collector.http")

RETRYABLE_STATUS = {500, 502, 503, 504, 520, 521, 522, 524}


class PermanentError(Exception):
    """Помилка, яку немає сенсу повторювати (доступ заборонено, стрічки немає тощо)."""

    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} {url} {body[:200]}")
        self.status = status


class RetriesExhausted(Exception):
    pass


def _token_tag(token: str | None) -> str:
    return "anon" if not token else "tok-" + hashlib.sha256(token.encode()).hexdigest()[:8]


def parse_retry_after(value: str | None, now: float) -> float | None:
    """Retry-After буває в секундах або як HTTP-дата."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        return max(0.0, dt.timestamp() - now)
    except (TypeError, ValueError):
        return None


def parse_reset(value: str | None) -> float | None:
    """X-RateLimit-Reset у Mastodon — ISO-8601 дата; деякі API віддають epoch-секунди."""
    if not value:
        return None
    try:
        v = float(value)
        return v if v > 1e9 else time.time() + v
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass
class TokenState:
    token: str | None
    remaining: int | None = None
    limit: int | None = None
    reset_at: float | None = None
    cooldown_until: float = 0.0
    used: int = 0

    @property
    def tag(self) -> str:
        return _token_tag(self.token)


@dataclass
class InstanceState:
    tokens: list[TokenState]
    last_request: float = 0.0

    def pick(self, now: float) -> TokenState:
        """Найменш «засвічений» доступний токен: без cooldown, з найбільшим Remaining, далі по колу."""
        ready = [t for t in self.tokens if t.cooldown_until <= now]
        if not ready:
            return min(self.tokens, key=lambda t: t.cooldown_until)
        # невідомий Remaining (ще не використаний токен) вважаємо найкращим; при рівності — менш використаний
        return max(ready, key=lambda t: (t.remaining if t.remaining is not None else 10**6, -t.used))


@dataclass
class HttpStats:
    requests: int = 0
    rate_limited: int = 0
    retries: int = 0
    by_status: dict[int, int] = field(default_factory=dict)


class HttpClient:
    def __init__(
        self,
        cfg: Settings,
        on_response: Callable[..., None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        session: requests.Session | None = None,
    ):
        self.cfg = cfg
        self.sleep = sleep
        self.clock = clock
        self.on_response = on_response
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": cfg.user_agent, "Accept": "application/json"})
        if cfg.proxy:  # бонус: вихід через проксі
            self.session.proxies.update({"http": cfg.proxy, "https": cfg.proxy})
        self.instances: dict[str, InstanceState] = {}
        self.stats = HttpStats()

    def _inst(self, instance: str) -> InstanceState:
        if instance not in self.instances:
            toks = self.cfg.tokens.get(instance) or [None]
            self.instances[instance] = InstanceState([TokenState(t) for t in toks])
        return self.instances[instance]

    def _pace(self, st: InstanceState, tok: TokenState) -> None:
        now = self.clock()
        wait = max(0.0, self.cfg.request_delay_s - (now - st.last_request))
        # Адаптивно: якщо квота майже вичерпана — рівномірно розподіляємо залишок до Reset
        if tok.remaining is not None and tok.reset_at and tok.remaining < self.cfg.low_remaining:
            spread = (tok.reset_at - now) / max(tok.remaining, 1)
            wait = max(wait, min(spread, self.cfg.backoff_max_s))
        if tok.cooldown_until > now:
            wait = max(wait, tok.cooldown_until - now)
        if wait > 0:
            if wait > self.cfg.request_delay_s + 0.01:
                log.info("pacing", extra={"sleep_s": round(wait, 2), "token": tok.tag})
            self.sleep(wait)

    def _backoff(self, attempt: int) -> float:
        base = min(self.cfg.backoff_max_s, self.cfg.backoff_base_s * (2 ** (attempt - 1)))
        return base * (0.5 + random.random() / 2)  # jitter, щоб не бити синхронно

    def get_json(self, instance: str, path: str, params: dict | None = None, run_id: str | None = None
                 ) -> tuple[Any, requests.structures.CaseInsensitiveDict]:
        url = f"https://{instance}{path}"
        st = self._inst(instance)
        last_err = ""
        for attempt in range(1, self.cfg.max_retries + 1):
            tok = st.pick(self.clock())
            self._pace(st, tok)
            headers = {"Authorization": f"Bearer {tok.token}"} if tok.token else {}
            t0 = self.clock()
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=self.cfg.timeout_s)
            except requests.RequestException as e:
                st.last_request = self.clock()
                self.stats.requests += 1
                self.stats.retries += 1
                wait = self._backoff(attempt)
                last_err = f"{type(e).__name__}: {e}"
                log.warning("network error, retry", extra={"url": url, "attempt": attempt, "sleep_s": round(wait, 2),
                                                           "error": last_err})
                self.sleep(wait)
                continue

            st.last_request = self.clock()
            dur = int((st.last_request - t0) * 1000)
            tok.used += 1
            self.stats.requests += 1
            self.stats.by_status[resp.status_code] = self.stats.by_status.get(resp.status_code, 0) + 1
            rl = self._update_limits(tok, resp)
            log.info("response", extra={"url": resp.url, "status": resp.status_code, "duration_ms": dur,
                                        "ratelimit": rl, "token": tok.tag, "attempt": attempt})
            if self.on_response:
                self.on_response(run_id=run_id, instance=instance, status=resp.status_code,
                                 rl_limit=rl.get("limit"), rl_remaining=rl.get("remaining"),
                                 rl_reset=rl.get("reset"), retry_after=rl.get("retry_after"),
                                 duration_ms=dur, token=tok.tag)

            if resp.status_code == 200:
                return resp.json(), resp.headers

            if resp.status_code == 429:
                self.stats.rate_limited += 1
                self.stats.retries += 1
                now = self.clock()
                wait = parse_retry_after(resp.headers.get("Retry-After"), now)
                if wait is None and tok.reset_at:
                    wait = max(0.0, tok.reset_at - now)
                if wait is None:
                    wait = self._backoff(attempt)
                wait = min(wait + 1.0, self.cfg.backoff_max_s)
                tok.cooldown_until = now + wait
                others = [t for t in st.tokens if t is not tok and t.cooldown_until <= now]
                log.warning("429 rate limited", extra={"url": url, "attempt": attempt, "sleep_s": round(wait, 2),
                                                       "token": tok.tag,
                                                       "error": "switch token" if others else "cooldown"})
                # якщо є інший токен — переключимось без сну (pick() його обере), інакше _pace() почекає
                last_err = "429"
                continue

            if resp.status_code in RETRYABLE_STATUS:
                self.stats.retries += 1
                wait = self._backoff(attempt)
                last_err = f"HTTP {resp.status_code}"
                log.warning("server error, retry", extra={"url": url, "status": resp.status_code,
                                                          "attempt": attempt, "sleep_s": round(wait, 2)})
                self.sleep(wait)
                continue

            raise PermanentError(resp.status_code, url, resp.text)

        raise RetriesExhausted(f"{url}: {self.cfg.max_retries} спроб, остання помилка: {last_err}")

    def _update_limits(self, tok: TokenState, resp: requests.Response) -> dict:
        h = resp.headers
        rl: dict[str, Any] = {}
        for name, key in (("X-RateLimit-Limit", "limit"), ("X-RateLimit-Remaining", "remaining")):
            if name in h:
                try:
                    rl[key] = int(h[name])
                except ValueError:
                    pass
        if "X-RateLimit-Reset" in h:
            rl["reset"] = h["X-RateLimit-Reset"]
        if "Retry-After" in h:
            rl["retry_after"] = h["Retry-After"]
        if "remaining" in rl:
            tok.remaining = rl["remaining"]
        if "limit" in rl:
            tok.limit = rl["limit"]
        if "reset" in rl:
            tok.reset_at = parse_reset(rl["reset"])
        return rl
