"""Uniswap v3 集中流動性(LP)部位的粗略模擬,用來和「自己下 swap 的網格」比較。

LP 部位在區間內等於一張很密的網格:價格往上時自動把幣換成 USDC,往下時反過來,並收取池子手續費。
差別是:不必付每筆交易的 gas 與滑點,但會承受無常損失,手續費收入取決於自己占池子流動性的比例。

重要假設(手續費收入是粗估,誤差可能很大):
- 池子流動性固定為「現在」鏈上讀到的區間流動性;歷史上實際會變動,且不同價位的流動性不同。
- 每根 K 線的成交量(USD)乘上「價格落在區間內的比例」計入手續費;比例以 K 線高低點和區間的重疊程度估算。
- 自己的部位不影響價格,也不考慮 JIT 流動性、MEV 搶先等行為。
- 建倉:先把約一部分 USDC 換成幣(付 swap 成本),再加入流動性;出場:移除流動性,再把幣全部換回 USDC。
  加入 / 移除流動性的 gas 各以 `lp_gas_mult` 倍的 swap gas 估算。
"""
import math
from dataclasses import dataclass, field
from typing import List


@dataclass(frozen=True)
class LpConfig:
    lower: float
    upper: float
    capital: float
    pool_fee: float  # 例如 0.0005
    pool_liquidity: float  # 池子區間內流動性(人類單位,見 human_liquidity)
    lp_gas_mult: float = 2.0

    def __post_init__(self):
        if not (0 < self.lower < self.upper) or not all(map(math.isfinite, (self.lower, self.upper, self.capital))):
            raise ValueError("區間必須是 0 < 下界 < 上界")
        if self.capital <= 0:
            raise ValueError("本金必須大於 0")
        if not 0 <= self.pool_fee < 1:
            raise ValueError("池子手續費必須介於 0 與 1 之間")
        if self.pool_liquidity < 0 or self.lp_gas_mult < 0:
            raise ValueError("流動性與 gas 倍數不能是負數")


def human_liquidity(raw_liquidity, decimals0, decimals1):
    """鏈上的 L(原始單位)換成以人類價格計算時的 L:L_raw = L_human × 10^((d0+d1)/2)。"""
    return raw_liquidity / 10 ** ((decimals0 + decimals1) / 2)


def amounts(liq, price, lower, upper):
    """流動性 liq 在價格 price 時持有的 (幣數量, 報價幣數量)。"""
    sp, sa, sb = math.sqrt(min(max(price, lower), upper)), math.sqrt(lower), math.sqrt(upper)
    return liq * (1 / sp - 1 / sb), liq * (sp - sa)


def position_value(liq, price, lower, upper):
    x, y = amounts(liq, price, lower, upper)
    return x * price + y


def in_range_fraction(c, lower, upper):
    """這根 K 線有多少比例的價格區間落在 LP 區間內(用來分攤成交量)。"""
    if c.high == c.low:
        return 1.0 if lower <= c.close <= upper else 0.0
    overlap = min(c.high, upper) - max(c.low, lower)
    return max(0.0, overlap) / (c.high - c.low)


@dataclass
class LpResult:
    cfg: LpConfig
    start_price: float
    end_price: float
    liquidity: float = 0.0
    share: float = 0.0  # 占池子區間流動性的比例
    fees: float = 0.0
    entry_cost: float = 0.0  # swap 成本 + gas
    exit_cost: float = 0.0
    equity: List[float] = field(default_factory=list)  # 每根收盤:部位市值 + 累積手續費 - 建倉成本
    hold_value: float = 0.0  # 建倉後不加入流動性、單純拿著同樣的幣與 USDC 的期末價值
    in_range_share: float = 0.0

    @property
    def final_equity(self):
        return self.equity[-1] if self.equity else self.cfg.capital

    @property
    def exit_return_pct(self):
        return ((self.final_equity - self.exit_cost) / self.cfg.capital - 1) * 100

    @property
    def impermanent_loss(self):
        """部位市值比「拿著同樣的幣不動」少了多少(不含手續費)。"""
        return position_value(self.liquidity, self.end_price, self.cfg.lower, self.cfg.upper) - self.hold_value


def simulate_lp(candles, cfg, costs):
    """`costs` 用 sim.CostModel:建倉 / 出場的 swap 付手續費與滑點,加入 / 移除流動性各付 lp_gas_mult 倍 gas。"""
    if not candles:
        raise ValueError("沒有 K 線可以模擬")
    p0 = candles[0].open
    res = LpResult(cfg, p0, candles[-1].close)
    unit = position_value(1.0, p0, cfg.lower, cfg.upper)  # 每 1 單位流動性的價值
    x_unit, _ = amounts(1.0, p0, cfg.lower, cfg.upper)
    # 要換成幣的部分得先付 swap 成本,所以可投入的金額略少於本金(一次修正就足夠精確)
    liq = cfg.capital / unit
    for _ in range(2):
        swap_value = liq * x_unit * p0
        swap_cost = swap_value * costs.rate(swap_value) if swap_value > 0 else 0.0
        liq = (cfg.capital - swap_cost) / unit
    gas_in = (costs.gas_usd if swap_value > 0 else 0.0) + costs.gas_usd * cfg.lp_gas_mult
    res.liquidity = liq
    res.entry_cost = swap_cost + gas_in
    res.share = liq / (cfg.pool_liquidity + liq) if cfg.pool_liquidity + liq > 0 else 0.0
    x0, y0 = amounts(liq, p0, cfg.lower, cfg.upper)

    in_range = 0
    for c in candles:
        res.fees += c.volume * in_range_fraction(c, cfg.lower, cfg.upper) * cfg.pool_fee * res.share
        res.equity.append(position_value(liq, c.close, cfg.lower, cfg.upper) + res.fees - gas_in)
        if cfg.lower <= c.close <= cfg.upper:
            in_range += 1
    last = candles[-1].close
    res.in_range_share = in_range / len(candles)
    res.hold_value = x0 * last + y0
    x_end, _ = amounts(liq, last, cfg.lower, cfg.upper)
    held = x_end * last
    res.exit_cost = costs.gas_usd * cfg.lp_gas_mult + (held * costs.rate(held) + costs.gas_usd if held > 0 else 0.0)
    return res
