"""Sui 鏈上讀取:Cetus CLMM 池子狀態與每一筆成交(唯讀,用 Sui GraphQL)。

Sui 不是 EVM 鏈,公開節點已停用 JSON-RPC,所以改用 GraphQL。
成交來源是 Cetus 的 SwapEvent:依事件類型查詢(整個 Cetus 每秒約 1~2 筆),再篩出指定池子。
每頁最多 50 筆、約 1 秒,補抓太久的期間很慢,所以停機最多只補抓 MAX_CATCHUP_SECONDS。
"""
import datetime
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

from .rpc import RpcError
from .swaps import Swap

GRAPHQL_URL = "https://graphql.mainnet.sui.io/graphql"
CETUS_PACKAGE = "0x1eabed72c53feb3805120a081dc15963c204dc8d091542592abaf7a35689b2fb"  # Cetus CLMM 最初的套件位址(事件型別用它)
SWAP_EVENT = CETUS_PACKAGE + "::pool::SwapEvent"
Q64 = 2**64
PAGE = 50  # 公開節點每頁上限
CHECKPOINTS_PER_SEC = 4  # 約略值(實測約 4.1),只用來換算補抓範圍
MAX_CATCHUP_SECONDS = 2 * 3600
LAG_CHECKPOINTS = 20  # 約 5 秒,見 latest_checkpoint
_RETRY_HTTP = (429, 500, 502, 503, 504)
_POOL_TYPE_RE = re.compile(r"^(0x[0-9a-fA-F]+)::pool::Pool<(.+),\s*(.+)>$")

_SWAPS_QUERY = """query($filter: EventFilter, $after: String) {
  events(first: %d, after: $after, filter: $filter) {
    pageInfo { hasNextPage endCursor }
    nodes { timestamp contents { json } }
  }
}""" % PAGE


class SuiClient:
    """最小的 GraphQL 客戶端,只做查詢。"""

    def __init__(self, url=None, timeout=20.0, retries=3):
        self.url = url or os.environ.get("SUI_GRAPHQL_URL") or GRAPHQL_URL
        if urlparse(self.url).scheme not in ("http", "https"):
            raise ValueError("GraphQL 網址必須是 http(s)")
        self.timeout = timeout
        self.retries = retries

    @property
    def host(self):
        return urlparse(self.url).netloc.split("@")[-1]  # 網址可能含金鑰,只顯示主機

    def query(self, query, variables=None):
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        last = "未知錯誤"
        for attempt in range(self.retries):
            try:
                req = urllib.request.Request(self.url, data=body, headers={
                    "Content-Type": "application/json", "User-Agent": "defi-grid/0.1"})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.load(resp)
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}"
                if e.code not in _RETRY_HTTP:
                    break
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                last = type(e).__name__
            else:
                if data.get("errors"):
                    raise RpcError(f"GraphQL 回傳錯誤: {data['errors'][0].get('message', '')[:200]}")
                if "data" not in data:
                    raise RpcError("GraphQL 回應缺少 data")
                return data["data"]
            time.sleep(1.5 * (attempt + 1))
        raise RpcError(f"{self.host} 查詢失敗: {last}")


def normalize_coin(t):
    """0x0000…0002::sui::SUI 與 0x2::sui::SUI 是同一個型別:位址去掉前導零再比較。"""
    addr, _, rest = t.strip().partition("::")
    return "0x" + (addr.lower()[2:].lstrip("0") or "0") + "::" + rest


def parse_ts(s):
    """GraphQL 的 ISO 時間(例如 2026-10-29T08:06:25.059Z)→ Unix 秒。"""
    return int(datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def price_from_sqrt(sqrt_price, decimals_a, decimals_b, invert):
    """Cetus 的 sqrt_price 是 Q64.64 的 √(coin_b / coin_a)(原始單位)。invert=True 時回傳 coin_b 以 coin_a 計的價格。"""
    p = (int(sqrt_price) / Q64) ** 2 * 10 ** (decimals_a - decimals_b)  # 1 coin_a = p coin_b
    if p <= 0:
        raise ValueError("sqrt_price 必須大於 0")
    return 1 / p if invert else p


@dataclass(frozen=True)
class SuiPool:
    """模擬盤需要的池子資訊。symbol0 是網格買賣的幣(例如 SUI),symbol1 是計價的穩定幣。
    decimals0 / decimals1 依池子的 coin_a / coin_b 順序(解析成交價格用);invert 表示要買賣的幣是 coin_b。"""

    address: str
    block: int  # checkpoint
    symbol0: str
    symbol1: str
    decimals0: int
    decimals1: int
    invert: bool
    fee_rate: float
    price: float


def latest_checkpoint(client):
    """(可安全處理到的 checkpoint, 時間戳)。

    往回退 LAG_CHECKPOINTS:萬一事件索引比 checkpoint 慢一點,剛發生的成交還沒查得到,
    處理到最新會讓下一輪從更後面開始,那幾筆就永遠漏掉了。
    """
    c = client.query("{ checkpoint { sequenceNumber timestamp } }")["checkpoint"]
    lag_secs = LAG_CHECKPOINTS // CHECKPOINTS_PER_SEC
    return int(c["sequenceNumber"]) - LAG_CHECKPOINTS, parse_ts(c["timestamp"]) - lag_secs


def read_pool(client, pool_id):
    """讀 Cetus 池子:代幣、精度、手續費、目前價格。另一邊必須是 USD 穩定幣(網格以美元計本金)。"""
    d = client.query("""query($id: SuiAddress!) {
      checkpoint { sequenceNumber }
      object(address: $id) { asMoveObject { contents { type { repr } json } } }
    }""", {"id": pool_id})
    obj = (d.get("object") or {}).get("asMoveObject") or {}
    contents = obj.get("contents")
    if not contents:
        raise ValueError(f"找不到 Sui 物件 {pool_id}")
    m = _POOL_TYPE_RE.match(contents["type"]["repr"])
    if not m or normalize_coin(m.group(1) + "::x") != normalize_coin(CETUS_PACKAGE + "::x"):
        raise ValueError("這個物件不是 Cetus CLMM 池子")
    coin_a, coin_b = m.group(2), m.group(3)
    meta = client.query("""query($a: String!, $b: String!) {
      a: coinMetadata(coinType: $a) { decimals symbol }
      b: coinMetadata(coinType: $b) { decimals symbol }
    }""", {"a": coin_a, "b": coin_b})
    if not meta.get("a") or not meta.get("b"):
        raise ValueError("讀不到池子代幣的精度資訊")
    sym_a, sym_b = meta["a"]["symbol"], meta["b"]["symbol"]
    usd_a, usd_b = sym_a.upper().startswith("USD"), sym_b.upper().startswith("USD")
    if usd_a == usd_b:
        raise ValueError(f"池子必須是一邊 USD 穩定幣、一邊要交易的幣(這個池子是 {sym_a}/{sym_b})")
    invert = usd_a  # 穩定幣是 coin_a → 要買賣的是 coin_b,價格要取倒數
    j = contents["json"]
    da, db = int(meta["a"]["decimals"]), int(meta["b"]["decimals"])
    return SuiPool(
        address=pool_id,
        block=int(d["checkpoint"]["sequenceNumber"]),
        symbol0=sym_b if invert else sym_a,
        symbol1=sym_a if invert else sym_b,
        decimals0=da,
        decimals1=db,
        invert=invert,
        fee_rate=int(j["fee_rate"]) / 1_000_000,
        price=price_from_sqrt(j["current_sqrt_price"], da, db, invert),
    )


def fetch_swaps(client, pool, after, upto, since_ts=None, until_ts=None, progress=None):
    """抓 checkpoint (after, upto] 之間這個池子的成交,依時間排序。

    `pool` 需要 address、decimals0、decimals1、invert(SuiPool 或模擬盤)。
    """
    flt = {"type": SWAP_EVENT, "afterCheckpoint": after, "beforeCheckpoint": upto + 1}
    want = pool.address.lower()
    out, cursor, reported = [], None, False
    while True:
        page = client.query(_SWAPS_QUERY, {"filter": flt, "after": cursor})["events"]
        for i, ev in enumerate(page["nodes"]):
            j = ev["contents"]["json"]
            if j.get("pool", "").lower() != want:
                continue
            ts = parse_ts(ev["timestamp"])
            out.append(Swap(block=upto, log_index=len(out), ts=ts,
                            price=price_from_sqrt(j["after_sqrt_price"], pool.decimals0, pool.decimals1, pool.invert),
                            amount1=0))
        if not page["pageInfo"]["hasNextPage"]:
            if reported:
                progress(1.0)
            return out
        # 只有要翻頁(補抓一大段)時才回報進度,一般輪詢一頁就抓完,不用顯示
        if progress and since_ts and until_ts and page["nodes"] and until_ts > since_ts:
            last = parse_ts(page["nodes"][-1]["timestamp"])
            progress(min(0.999, max(0.0, (last - since_ts) / (until_ts - since_ts))))
            reported = True
        cursor = page["pageInfo"]["endCursor"]
