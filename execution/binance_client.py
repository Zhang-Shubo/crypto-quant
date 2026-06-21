#!/usr/bin/env python3
"""Binance USDⓈ-M 合约 REST 客户端 (签名版, 仅标准库)。

支持 testnet (默认) 与实盘切换。鉴权: API Key 放 X-MBX-APIKEY 头,
参数用 HMAC-SHA256(secret) 签名 (Binance 标准)。

安全:
- Key/Secret 从环境变量或显式传入读取, 永不写入仓库 (见 .env.example, .env 已 gitignore)。
- 建议给 API Key 仅开"合约交易"权限, 关闭提现, 并绑定服务器 IP 白名单。

参考: https://developers.binance.com/docs/derivatives/usds-margined-futures
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

LIVE_BASE = "https://fapi.binance.com"
TEST_BASE = "https://testnet.binancefuture.com"


class BinanceFutures:
    def __init__(self, key=None, secret=None, testnet=True, recv_window=5000, timeout=15):
        self.key = key if key is not None else os.environ.get("BINANCE_KEY", "")
        sec = secret if secret is not None else os.environ.get("BINANCE_SECRET", "")
        self.secret = sec.encode()
        self.testnet = testnet
        self.base = TEST_BASE if testnet else LIVE_BASE
        self.recv = recv_window
        self.timeout = timeout

    # ---------- 底层 ----------
    def _sign(self, params: dict) -> str:
        qs = urllib.parse.urlencode(params)
        sig = hmac.new(self.secret, qs.encode(), hashlib.sha256).hexdigest()
        return f"{qs}&signature={sig}"

    def _request(self, method: str, path: str, params=None, signed=False):
        params = dict(params or {})
        headers = {}
        if self.key:
            headers["X-MBX-APIKEY"] = self.key
        url = self.base + path
        data = None
        if signed:
            if not self.key or not self.secret:
                raise RuntimeError("需要 API Key/Secret (设 BINANCE_KEY / BINANCE_SECRET)")
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = self.recv
            qs = self._sign(params)
            if method in ("GET", "DELETE"):
                url += "?" + qs
            else:
                data = qs.encode()
                headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {e.code} {path}: {body}")

    # ---------- 公共 (免签) ----------
    def ping(self):
        return self._request("GET", "/fapi/v1/ping")

    def server_time(self):
        return self._request("GET", "/fapi/v1/time")

    def ticker_24hr(self):
        return self._request("GET", "/fapi/v1/ticker/24hr")

    def exchange_info(self):
        return self._request("GET", "/fapi/v1/exchangeInfo")

    def klines(self, symbol, interval="1h", limit=200):
        """K线, 返回 Binance 原始数组 [[openTime,O,H,L,C,vol,closeTime,quoteVol,...]]。"""
        return self._request("GET", "/fapi/v1/klines",
                             {"symbol": symbol, "interval": interval, "limit": limit})

    def premium_index(self):
        """全市场标记价 / 资金费, 返回 [{symbol, markPrice, lastFundingRate, ...}]。"""
        return self._request("GET", "/fapi/v1/premiumIndex")

    def precision_map(self) -> dict:
        """{symbol: {qty_prec, price_prec, min_notional}}。"""
        info = self.exchange_info()
        out = {}
        for s in info["symbols"]:
            min_notional = 5.0
            for f in s.get("filters", []):
                if f.get("filterType") in ("MIN_NOTIONAL", "NOTIONAL"):
                    min_notional = float(f.get("notional", f.get("minNotional", 5.0)))
            out[s["symbol"]] = {
                "qty_prec": s.get("quantityPrecision", 3),
                "price_prec": s.get("pricePrecision", 2),
                "min_notional": min_notional,
            }
        return out

    # ---------- 私有 (签名) ----------
    def balance(self):
        return self._request("GET", "/fapi/v2/balance", signed=True)

    def usdt_balance(self) -> float:
        for b in self.balance():
            if b.get("asset") == "USDT":
                return float(b.get("availableBalance", b.get("balance", 0)))
        return 0.0

    def positions(self):
        return self._request("GET", "/fapi/v2/positionRisk", signed=True)

    def open_positions(self):
        return [p for p in self.positions() if abs(float(p.get("positionAmt", 0))) > 0]

    def set_leverage(self, symbol, leverage):
        return self._request("POST", "/fapi/v1/leverage",
                             {"symbol": symbol, "leverage": int(leverage)}, signed=True)

    def new_order(self, **params):
        return self._request("POST", "/fapi/v1/order", params, signed=True)

    def cancel_all_orders(self, symbol):
        return self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}, signed=True)

    # ---------- 便捷下单 ----------
    def market_entry(self, symbol, side, quantity):
        """市价开仓 (side: BUY/SELL)。"""
        return self.new_order(symbol=symbol, side=side, type="MARKET", quantity=quantity)

    def stop_market_close(self, symbol, close_side, stop_price):
        """reduceOnly 止损 (整仓平)。close_side 为平仓方向 (空头止损=BUY)。"""
        return self.new_order(symbol=symbol, side=close_side, type="STOP_MARKET",
                              stopPrice=stop_price, closePosition="true", workingType="MARK_PRICE")

    def take_profit_close(self, symbol, close_side, stop_price):
        return self.new_order(symbol=symbol, side=close_side, type="TAKE_PROFIT_MARKET",
                              stopPrice=stop_price, closePosition="true", workingType="MARK_PRICE")


def _selftest_signature():
    """双重校验签名实现:
    1) RFC 标准 HMAC-SHA256 向量 (验证 HMAC 本身)。
    2) Binance 文档示例查询串 (验证参数串拼接顺序 + 签名)。
    """
    import hmac as _h, hashlib as _hh
    rfc = _h.new(b"key", b"The quick brown fox jumps over the lazy dog", _hh.sha256).hexdigest()
    rfc_ok = rfc == "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8"

    c = BinanceFutures(key="x", secret="NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0",
                       testnet=True)
    params = {"symbol": "LTCBTC", "side": "BUY", "type": "LIMIT", "timeInForce": "GTC",
              "quantity": 1, "price": "0.1", "recvWindow": 5000, "timestamp": 1499827319559}
    qs, sig = c._sign(params).rsplit("&signature=", 1)
    canon = "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559"
    bin_ok = (qs == canon
              and sig == "b89008e7051ffbf2242be7dc5ae67fd146e6430688627b802c0cbec146e46aef")
    ok = rfc_ok and bin_ok
    print(("✅" if ok else "❌") + f" 签名校验: RFC向量={'通过' if rfc_ok else '失败'}, "
          f"Binance示例={'通过' if bin_ok else '失败'}")
    return ok


if __name__ == "__main__":
    _selftest_signature()
