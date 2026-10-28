"""鏈上 Swap 事件:取得池子每一筆成交後的價格,用來檢查「小時 K 線 + 盤中路徑猜測」低估了多少成交。

只讀 eth_getLogs,不需要金鑰。很多公開節點不支援大範圍 eth_getLogs(mainnet.base.org 會一直回 429),
所以預設改用 LOGS_RPC;也可以用 --rpc 指定自己的節點。
"""
from dataclasses import dataclass

from .rpc import RpcError
from .sim import Candle
from .uniswap import Q96, to_int, words

LOGS_RPC = "https://base-rpc.publicnode.com"
SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"  # Swap(address,address,int256,int256,uint160,uint128,int24)
BLOCK_TIME = 2  # Base 固定每 2 秒一個區塊
MAX_HOURS = 48
CHUNK = 5000  # 每次查詢的區塊數;節點拒絕時會自動減半


@dataclass(frozen=True)
class Swap:
    block: int
    log_index: int
    ts: int
    price: float  # 成交後的 token0 價格(以 token1 計)
    amount1: int  # token1 的變動量(原始單位;正 = 流入池子)


def decode_swap(log, latest_block, latest_ts, decimals0, decimals1):
    w = words(log["data"])
    if len(w) < 5:
        raise ValueError("Swap 事件資料欄位不足")
    block = to_int(log["blockNumber"])
    sqrt_p = to_int(w[2]) / Q96
    return Swap(
        block=block,
        log_index=to_int(log["logIndex"]),
        ts=latest_ts - (latest_block - block) * BLOCK_TIME,
        price=sqrt_p**2 * 10 ** (decimals0 - decimals1),
        amount1=to_int(w[1], signed=True),
    )


def fetch_swaps(rpc, state, hours, progress=None):
    """抓最近 `hours` 小時的 Swap 事件(依區塊與事件順序排序)。`state` 是 read_pool 的結果。"""
    if not 1 <= hours <= MAX_HOURS:
        raise ValueError(f"小時數必須介於 1 到 {MAX_HOURS}")
    latest_block, latest_ts = latest_head(rpc)
    start = latest_block - hours * 3600 // BLOCK_TIME + 1
    return fetch_range(rpc, state, start, latest_block, latest_block, latest_ts, progress)


def latest_head(rpc):
    """(最新區塊號, 該區塊時間戳)。"""
    latest = rpc.call("eth_getBlockByNumber", ["latest", False])
    return to_int(latest["number"]), to_int(latest["timestamp"])


def fetch_range(rpc, state, start, end, latest_block, latest_ts, progress=None):
    """抓區塊 start ~ end(含)的 Swap 事件;時間戳以 latest_block / latest_ts 推算。"""
    out, frm, chunk = [], start, CHUNK
    while frm <= end:
        to = min(frm + chunk - 1, end)
        try:
            logs = rpc.call("eth_getLogs", [{"address": state.address, "fromBlock": hex(frm), "toBlock": hex(to), "topics": [SWAP_TOPIC]}])
        except RpcError:
            if chunk <= 100:
                raise
            chunk //= 2  # 範圍太大或結果太多,縮小再試
            continue
        out.extend(decode_swap(l, latest_block, latest_ts, state.decimals0, state.decimals1) for l in logs)
        frm = to + 1
        if progress:
            progress((frm - start) / (end - start + 1))
    out.sort(key=lambda s: (s.block, s.log_index))
    return out


def as_ticks(swaps):
    """每一筆成交當成一根「開高低收都相同」的 K 線,讓模擬沿著真實成交順序走,不需要猜盤中路徑。"""
    return [Candle(s.ts, s.price, s.price, s.price, s.price) for s in swaps]


def as_candles(swaps, seconds=3600):
    """把同一批成交彙整成 K 線,和逐筆結果比較時資料來源完全相同。"""
    out, cur = [], None
    for s in swaps:
        bucket = s.ts - s.ts % seconds
        if cur is None or bucket != cur[0]:
            if cur:
                out.append(Candle(*cur))
            cur = [bucket, s.price, s.price, s.price, s.price]
        else:
            cur[2] = max(cur[2], s.price)
            cur[3] = min(cur[3], s.price)
            cur[4] = s.price
    if cur:
        out.append(Candle(*cur))
    return out
