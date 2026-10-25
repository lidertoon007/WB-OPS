"""Фото товара Wildberries по WB ID (nm).

Картинки лежат на серверах basket-NN.wbbasket.ru; номер сервера зависит от «тома» (nm // 100000) и
меняется по мере роста каталога, поэтому сервер для тома находим один раз (параллельные HEAD-запросы)
и запоминаем в базе. Браузер получает редирект на картинку WB — сами файлы мы не храним.
"""
from __future__ import annotations

import asyncio
import time

import httpx

MAX_BASKET = 60
SIZES = {"small": "c246x328", "big": "big"}


def image_url(host: str, nm: int, size: str = "small", index: int = 1) -> str:
    vol, part = nm // 100000, nm // 1000
    return f"https://{host}/vol{vol}/part{part}/{nm}/images/{SIZES.get(size, 'c246x328')}/{index}.webp"


class WBImages:
    def __init__(self, store):
        self.store = store
        self._miss: dict[int, float] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._down_until = 0.0

    def _cached(self, vol: int) -> str | None:
        return self.store.get_setting(f"wb_basket:{vol}", "") or None

    def _guess_order(self, vol: int) -> list[int]:
        """Начинаем с серверов соседних уже найденных томов — обычно попадаем с первого раза."""
        known = []
        with self.store._connect() as c:
            for key, value in c.execute("SELECT key, value FROM app_settings WHERE key LIKE 'wb_basket:%'"):
                try:
                    known.append((int(key.split(":")[1]), int(value.split("-")[1].split(".")[0])))
                except (IndexError, ValueError):
                    pass
        lower = max((b for v, b in known if v <= vol), default=1)
        upper = min((b for v, b in known if v >= vol), default=MAX_BASKET)
        if upper < lower:
            upper = MAX_BASKET
        first = list(range(lower, upper + 1))
        return first + [b for b in range(1, MAX_BASKET + 1) if b not in first]

    async def host_for(self, nm: int, client: httpx.AsyncClient | None = None) -> str | None:
        vol = nm // 100000
        cached = self._cached(vol)
        if cached:
            return cached
        now = time.time()
        if now < self._down_until or now - self._miss.get(vol, 0) < 3600:
            return None
        lock = self._locks.setdefault(vol, asyncio.Lock())
        async with lock:
            cached = self._cached(vol)
            if cached:
                return cached
            own = client is None
            client = client or httpx.AsyncClient(timeout=4, follow_redirects=False)
            errors = answered = 0
            try:
                order = self._guess_order(vol)
                for start in range(0, len(order), 12):
                    batch = [f"basket-{n:02d}.wbbasket.ru" for n in order[start:start + 12]]

                    async def probe(host):
                        nonlocal errors, answered
                        try:
                            r = await client.head(image_url(host, nm))
                            answered += 1
                            return host if r.status_code == 200 else None
                        except httpx.HTTPError:
                            errors += 1
                            return None
                    found = [h for h in await asyncio.gather(*(probe(h) for h in batch)) if h]
                    if found:
                        self.store.set_setting(f"wb_basket:{vol}", found[0])
                        return found[0]
                    if not answered:            # нет связи с WB — не мучаем сеть 5 минут
                        self._down_until = time.time() + 300
                        return None
            finally:
                if own:
                    await client.aclose()
            self._miss[vol] = time.time()
            return None

    async def urls(self, nms: list[int], size: str = "small", budget: float = 8.0) -> dict[str, str | None]:
        """Ссылки на фото для списка товаров одним запросом (тома ищутся параллельно, не дольше budget секунд)."""
        nms = [n for n in dict.fromkeys(nms) if 1000 <= n <= 10**11][:100]
        result: dict[str, str | None] = {str(n): None for n in nms}
        async with httpx.AsyncClient(timeout=4, follow_redirects=False, limits=httpx.Limits(max_connections=24)) as client:
            by_vol: dict[int, list[int]] = {}
            for n in nms:
                by_vol.setdefault(n // 100000, []).append(n)

            async def resolve(group):
                host = await self.host_for(group[0], client)
                for n in group:
                    result[str(n)] = image_url(host, n, size) if host else None
            try:
                await asyncio.wait_for(asyncio.gather(*(resolve(g) for g in by_vol.values())), budget)
            except asyncio.TimeoutError:
                pass
        return result
