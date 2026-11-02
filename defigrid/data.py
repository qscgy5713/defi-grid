"""歷史 K 線:從 GeckoTerminal 公開 API 取得池子的每小時 OHLCV(免金鑰),並快取到 cache/。"""
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .sim import Candle
from .uniswap import POOL_WETH_USDC_500, WETH_BASE, check_address

API = "https://api.geckoterminal.com/api/v2/networks/{network}/pools/{pool}/ohlcv/hour"
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache")
CACHE_TTL = 3600
PAGE = 1000
PAGE_DELAY = 2.5  # 公開 API 約每分鐘 30 次,分頁之間要等
SUI_OBJECT_RE = re.compile(r"^0x[0-9a-fA-F]{1,64}$")
SUI_COIN_RE = re.compile(r"^0x[0-9a-fA-F]{1,64}::[A-Za-z_][A-Za-z0-9_]*::[A-Za-z_][A-Za-z0-9_]*$")

# 各鏈的預設池子與代幣。Sui 不是 EVM 鏈:支援 K 線回測與即時模擬盤,不支援 pool / gas / swaps / lp
CHAINS = {
    "base": {
        "name": "Base",
        "network": "base",
        "pool": POOL_WETH_USDC_500,
        "token": WETH_BASE,
        "gas_usd": 0.03,
        "label": "Base Uniswap v3 WETH/USDC 0.05%",
    },
    "sui": {
        "name": "Sui",
        "network": "sui-network",
        "pool": "0x51e883ba7c0b566a26cbc8a94cd33eb0abd418a77cc1e60ad22fd9b1f29cd2ab",
        "token": "0x2::sui::SUI",
        "gas_usd": 0.01,  # Sui 一次 swap 約 0.003~0.01 SUI,取寬鬆值
        "label": "Sui Cetus USDC/SUI 0.05%",
    },
}


def check_pool(chain, pool):
    """依鏈驗證池子識別碼:Base 是 20 bytes 位址,Sui 是物件 ID。"""
    if chain not in CHAINS:
        raise ValueError(f"不支援的鏈: {chain}")
    if chain == "base":
        return check_address(pool)
    if not SUI_OBJECT_RE.match(pool or ""):
        raise ValueError(f"不是有效的 Sui 物件 ID: {pool!r}")
    return pool


def check_ids(chain, pool, token):
    """驗證池子與代幣:Base 代幣是位址,Sui 代幣是 coin type(0x2::sui::SUI)。"""
    check_pool(chain, pool)
    if chain == "base":
        return pool, check_address(token)
    if not SUI_COIN_RE.match(token or ""):
        raise ValueError(f"不是有效的 Sui coin type(例如 0x2::sui::SUI): {token!r}")
    return pool, token


class DataError(Exception):
    pass


def parse_ohlcv(rows):
    """把 API 的 [ts, o, h, l, c, v] 轉成 Candle:排序、去重,並修正不合理的影線。"""
    seen = {}
    for r in rows:
        try:
            ts, o, h, l, c = int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])
            v = float(r[5]) if len(r) > 5 else 0.0
        except (TypeError, ValueError, IndexError) as e:
            raise DataError(f"K 線格式不正確: {r!r}") from e
        if not all(math.isfinite(x) and x > 0 for x in (o, h, l, c)):
            raise DataError(f"K 線價格必須是正數: {r!r}")
        # 影線必須包住開收盤價,否則視為資料瑕疵並修正
        seen[ts] = Candle(ts, o, max(h, o, c), min(l, o, c), c, v)
    return [seen[t] for t in sorted(seen)]


def suspicious(candles, threshold=0.15):
    """單根 K 線振幅超過 threshold 的數量。池子偶爾有異常價格,會嚴重扭曲回測。"""
    return [c for c in candles if c.high / c.low - 1 > threshold]


def _get(url):
    last = "未知錯誤"
    for attempt in range(3):
        wait = 3
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "defi-grid/0.1", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code not in (429, 500, 502, 503, 504):
                break
            if e.code == 429:  # 被限流:每分鐘額度要等一陣子才會恢復
                wait = 20
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            last = type(e).__name__
        if attempt < 2:
            time.sleep(wait * (attempt + 1))
    if last == "HTTP 429":
        last += "(公開 API 限流,請過一分鐘再試)"
    raise DataError(f"取得 K 線失敗: {last}")


def fetch_candles(pool, token, days, use_cache=True, chain="base"):
    """取得最近 `days` 天的每小時 K 線。`token` 是要看價格的代幣(例如 WETH),以 USD 計價。"""
    check_ids(chain, pool, token)
    if not 1 <= days <= 120:
        raise ValueError("days 必須介於 1 到 120")
    need = days * 24
    key = f"{pool.lower()}_{token.lower()}"
    if chain != "base":  # Base 沿用舊的快取檔名
        key = f"{chain}_{pool.lower()}_{token.lower().replace(':', '-')}"
    cache_file = os.path.join(CACHE_DIR, f"candles_{key}.json")
    cached = []
    if os.path.exists(cache_file) and time.time() - os.path.getmtime(cache_file) < CACHE_TTL:
        try:
            with open(cache_file) as f:
                cached = parse_ohlcv(json.load(f))
        except (OSError, ValueError, DataError):
            cached = []  # 快取損毀就當作沒有
        if use_cache and len(cached) >= need:
            return cached[-need:]

    # 多抓一根:最新那根還在形成中會被丟掉,這樣才湊得滿 need 根
    want = need + 1
    rows, before = [], None
    while len(rows) < want:
        limit = min(PAGE, want - len(rows))
        url = (API.format(network=CHAINS[chain]["network"], pool=pool)
               + f"?aggregate=1&limit={limit}&currency=usd&token={urllib.parse.quote(token, safe='')}")
        if before:
            url += f"&before_timestamp={before}"
        page = _get(url).get("data", {}).get("attributes", {}).get("ohlcv_list") or []
        if not page:
            break
        rows.extend(page)
        before = min(int(r[0]) for r in page)
        if len(page) < limit:  # 拿到的比要求的少 = 已經沒有更舊的資料
            break
        time.sleep(PAGE_DELAY)
    # 最新一根通常還在形成中(收盤價會變),不納入回測
    now = time.time()
    candles = [c for c in parse_ohlcv(rows) if c.ts + 3600 <= now]
    if not candles:
        raise DataError("沒有取得任何 K 線")
    # 和一小時內的快取合併(新資料優先),避免抓短天數時把較長的快取蓋掉
    merged = {c.ts: c for c in cached}
    merged.update((c.ts, c) for c in candles)
    stored = [merged[t] for t in sorted(merged)]
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = cache_file + ".tmp"
    with open(tmp, "w") as f:
        json.dump([[c.ts, c.open, c.high, c.low, c.close, c.volume] for c in stored], f)
    os.replace(tmp, cache_file)
    return candles[-need:]
