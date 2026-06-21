"""统一的 GET-JSON + 重试封装 (仅标准库)。

原先 data/backtest/execution 三处各写了一遍带/不带重试的 urlopen, 行为不一。
这里集中一份: 指数退避重试, 对 Binance 限频 (429) 加长退避, 对封禁
(418 IP-ban / 451 法务限制) 直接抛出不重试。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from ..config import USER_AGENT


def get_json(base: str, path: str, params: dict | None = None,
             timeout: int = 20, retries: int = 4):
    """GET {base}{path}?{params} 并解析 JSON。失败按退避重试。

    429 限频 → 长退避后重试; 418/451 封禁 → 立即抛出 (重试无意义)。
    """
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:                      # 限频: 退避更久
                time.sleep(2 * (attempt + 1))
                continue
            if e.code in (418, 451):               # IP 封禁 / 法务限制: 别重试
                raise
            time.sleep(0.5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"请求失败 {base}{path}: {last_err}")
