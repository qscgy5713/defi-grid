"""Uniswap v3 池子的唯讀查詢與價格數學(不依賴 web3 函式庫)。"""
import re
from dataclasses import dataclass

Q96 = 2**96
BASE_CHAIN_ID = 8453
# Base 上 WETH / USDC 0.05% 池。執行時會從鏈上讀 token0 / token1 / fee 再確認,不盲信這個常數
POOL_WETH_USDC_500 = "0xd0b53D9277642d899DF5C87A3966A349A798F224"
WETH_BASE = "0x4200000000000000000000000000000000000006"

# 函式選擇器(函式簽章 keccak256 的前 4 bytes)
SEL_TOKEN0 = "0x0dfe1681"  # token0()
SEL_TOKEN1 = "0xd21220a7"  # token1()
SEL_FEE = "0xddca3f43"  # fee()
SEL_LIQUIDITY = "0x1a686502"  # liquidity()
SEL_SLOT0 = "0x3850c7bd"  # slot0()
SEL_DECIMALS = "0x313ce567"  # decimals()
SEL_SYMBOL = "0x95d89b41"  # symbol()
SEL_L1_FEE_UPPER_BOUND = "0xf1c7a58b"  # getL1FeeUpperBound(uint256)

# OP Stack 預部署合約:回報把交易資料發到 L1 的費用
GAS_PRICE_ORACLE = "0x420000000000000000000000000000000000000F"

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def check_address(addr):
    if not ADDRESS_RE.match(addr or ""):
        raise ValueError(f"不是有效的位址: {addr!r}")
    return addr


def words(hexstr):
    h = hexstr[2:] if hexstr.startswith("0x") else hexstr
    if len(h) % 64:
        raise ValueError("ABI 回傳長度不是 32 bytes 的倍數")
    return [h[i : i + 64] for i in range(0, len(h), 64)]


def to_int(word, signed=False):
    v = int(word, 16)
    if signed and v >= 2**255:
        v -= 2**256
    return v


def to_address(word):
    return "0x" + word[-40:]


def decode_string(hexstr):
    """解 ABI 字串;少數老代幣的 symbol() 回傳 bytes32,也一併處理。"""
    h = hexstr[2:] if hexstr.startswith("0x") else hexstr
    if len(h) == 64:  # bytes32
        return bytes.fromhex(h).rstrip(b"\x00").decode("utf-8", "replace")
    w = words(hexstr)
    offset = to_int(w[0]) // 32
    length = to_int(w[offset])
    data = "".join(w[offset + 1 :])[: length * 2]
    return bytes.fromhex(data).decode("utf-8", "replace")


@dataclass(frozen=True)
class PoolState:
    address: str
    block: int
    token0: str
    token1: str
    symbol0: str
    symbol1: str
    decimals0: int
    decimals1: int
    fee_ppm: int  # 500 = 0.05%
    sqrt_price_x96: int
    tick: int
    liquidity: int  # 目前價格所在 tick 區間內的流動性

    @property
    def fee_rate(self):
        return self.fee_ppm / 1_000_000


def parse_slot0(hexstr):
    w = words(hexstr)
    if len(w) < 7:
        raise ValueError("slot0 回傳的欄位數不足")
    return to_int(w[0]), to_int(w[1], signed=True)


def price_token1_per_token0(s):
    """token0 以 token1 計價的價格(例如 1 WETH = 多少 USDC)。"""
    p = (s.sqrt_price_x96 / Q96) ** 2
    return p * 10 ** (s.decimals0 - s.decimals1)


def price_impact(trade_value, s):
    """用 token1 計價的 `trade_value` 買入 token0 時,價格上漲的比例。

    只考慮「目前價格所在區間」內的流動性(未跨越 tick),所以只適合相對池子深度很小的交易;
    大額交易實際影響會更大。
    """
    if trade_value <= 0:
        return 0.0
    if s.liquidity <= 0:
        return 1.0
    sqrt_p = s.sqrt_price_x96 / Q96
    dy = trade_value * 10**s.decimals1
    return ((sqrt_p + dy / s.liquidity) / sqrt_p) ** 2 - 1


def l1_fee_upper_bound(rpc, tx_bytes):
    """一筆 `tx_bytes` 大小(未簽名)的交易,L1 資料費的上限(wei)。實際費用會因資料可壓縮而更低。"""
    if not 1 <= tx_bytes <= 1_000_000:
        raise ValueError("交易大小必須介於 1 到 1,000,000 bytes")
    return to_int(words(rpc.eth_call(GAS_PRICE_ORACLE, SEL_L1_FEE_UPPER_BOUND + format(tx_bytes, "064x")))[0])


def _token_meta(rpc, token, block):
    decimals = to_int(words(rpc.eth_call(token, SEL_DECIMALS, block))[0])
    try:
        symbol = decode_string(rpc.eth_call(token, SEL_SYMBOL, block))
    except Exception:  # 個別代幣 symbol() 可能 revert 或格式特殊,不影響價格計算
        symbol = token[:8]
    return symbol, decimals


def read_pool(rpc, address=POOL_WETH_USDC_500):
    check_address(address)
    block_n = to_int(rpc.call("eth_blockNumber", []))
    block = hex(block_n)  # 全部在同一個區塊讀,避免狀態前後不一致
    token0 = to_address(words(rpc.eth_call(address, SEL_TOKEN0, block))[0])
    token1 = to_address(words(rpc.eth_call(address, SEL_TOKEN1, block))[0])
    fee = to_int(words(rpc.eth_call(address, SEL_FEE, block))[0])
    liquidity = to_int(words(rpc.eth_call(address, SEL_LIQUIDITY, block))[0])
    sqrt_price, tick = parse_slot0(rpc.eth_call(address, SEL_SLOT0, block))
    sym0, dec0 = _token_meta(rpc, token0, block)
    sym1, dec1 = _token_meta(rpc, token1, block)
    return PoolState(address, block_n, token0, token1, sym0, sym1, dec0, dec1, fee, sqrt_price, tick, liquidity)
