"""網格交易模擬:把池子手續費、滑點、價格衝擊與 gas 都算進成本。純計算,不連網、不碰錢包。

模型(重要假設,結果要看這些前提):
- 資金平均分成 `grids` 格,每格 capital/grids 的報價幣(USDC)。
- 價格往下穿過某條線 → 在該線買入一格;往上穿過上一條線 → 賣出那一格。
- 起始時,價格上方(含所在格)的每一格都先以起始價買好持倉。
- 價格路徑是連續的:上一根收盤 → 這一根開盤 → 盤中。盤中路徑只能猜:預設 陽線 開→低→高→收、陰線 開→高→低→收。
  真實盤中會來回震盪、穿越更多次,所以這個模擬傾向「低估」成交次數。
- 以限價單方式成交在格線價;成本 = 池子手續費 + 滑點 + 價格衝擊,另外每筆交易付一次 gas。
- gas 以美元計、另外記帳(視為用 ETH 另付),不佔用交易本金。
- 最大回撤只看每根 K 線收盤的資產,盤中更深的回撤不會反映(會低估)。
- 報酬有兩種:return_pct 以收盤價計算持倉市值;exit_return_pct 另外扣掉期末全部賣出的成本。
"""
import math
from dataclasses import dataclass, field
from typing import Callable, List, Optional

MODES = ("arithmetic", "geometric")
PATHS = ("auto", "low-first", "high-first")


@dataclass(frozen=True)
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class GridConfig:
    lower: float
    upper: float
    grids: int  # 格數(格線數 = grids + 1)
    capital: float  # 報價幣(USDC)本金
    mode: str = "arithmetic"

    def __post_init__(self):
        if not (math.isfinite(self.lower) and math.isfinite(self.upper) and math.isfinite(self.capital)):
            raise ValueError("價格區間與本金必須是有限數字")
        if self.lower <= 0:
            raise ValueError("下界必須大於 0")
        if self.upper <= self.lower:
            raise ValueError("上界必須大於下界")
        if not 2 <= self.grids <= 500:
            raise ValueError("格數必須介於 2 到 500")
        if self.capital <= 0:
            raise ValueError("本金必須大於 0")
        if self.mode not in MODES:
            raise ValueError(f"mode 必須是 {MODES} 之一")

    @property
    def grid_value(self):
        return self.capital / self.grids


@dataclass(frozen=True)
class CostModel:
    fee_rate: float = 0.0005  # 池子手續費(0.05%)
    slippage_rate: float = 0.0002  # 額外滑點假設
    gas_usd: float = 0.03  # 每筆交易的 gas(美元);請用 `gas` 指令的實測值校準
    impact_fn: Optional[Callable[[float], float]] = None  # 交易金額(USD) → 價格衝擊比例

    def __post_init__(self):
        if min(self.fee_rate, self.slippage_rate, self.gas_usd) < 0:
            raise ValueError("成本不能是負數")
        if self.fee_rate + self.slippage_rate >= 1:
            raise ValueError("手續費加滑點必須小於 100%")

    def rate(self, trade_value):
        impact = self.impact_fn(trade_value) if self.impact_fn else 0.0
        r = self.fee_rate + self.slippage_rate + impact
        return min(r, 0.99)


def grid_lines(cfg):
    n = cfg.grids
    if cfg.mode == "arithmetic":
        step = (cfg.upper - cfg.lower) / n
        return [cfg.lower + i * step for i in range(n + 1)]
    ratio = (cfg.upper / cfg.lower) ** (1 / n)
    return [cfg.lower * ratio**i for i in range(n + 1)]


def economics(cfg, costs, price):
    """一個完整買賣循環(在格線買、在上一條格線賣)的損益,用來判斷這組參數划不划算。"""
    lines = grid_lines(cfg)
    # 取最接近目前價格的一格當代表(幾何網格每格比例相同,等差網格越往上比例越小)
    i = min(range(cfg.grids), key=lambda k: abs((lines[k] + lines[k + 1]) / 2 - price))
    spacing = lines[i + 1] / lines[i] - 1
    gv = cfg.grid_value
    c = costs.rate(gv)
    # 買:拿 gv 換到 gv*(1-c)/p_buy 的幣;賣:全部賣在 p_sell,再扣一次成本
    net = gv * ((1 - c) ** 2 * (1 + spacing) - 1) - 2 * costs.gas_usd
    # 令 net = 0 解出的最小格距:(1-c)^2 (1+s) = 1 + 2·gas/gv
    breakeven = (1 + 2 * costs.gas_usd / gv) / (1 - c) ** 2 - 1
    return {
        "spacing_pct": spacing * 100,
        "grid_value": gv,
        "cost_rate_pct": c * 100,
        "net_per_cycle_usd": net,
        "net_per_cycle_pct": net / gv * 100,
        "breakeven_spacing_pct": breakeven * 100,
        "viable": net > 0,
    }


def candle_path(c, path="auto"):
    if path not in PATHS:
        raise ValueError(f"path 必須是 {PATHS} 之一")
    low_first = c.close >= c.open if path == "auto" else path == "low-first"
    return [c.open, c.low, c.high, c.close] if low_first else [c.open, c.high, c.low, c.close]


@dataclass
class Lot:
    qty: float  # 持有的幣數量
    cost: float  # 買入花掉的報價幣(含手續費與滑點,不含 gas)


@dataclass(frozen=True)
class Trade:
    ts: int
    side: str  # "buy" | "sell"
    price: float
    qty: float
    cell: int


@dataclass
class Result:
    cfg: GridConfig
    start_price: float
    end_price: float
    equity: List[float] = field(default_factory=list)  # 每根 K 線收盤時的總資產(已扣 gas)
    trades: List[Trade] = field(default_factory=list)
    realized_profit: float = 0.0  # 已完成循環的獲利(含交易成本,不含 gas)
    unrealized_pnl: float = 0.0
    trading_cost: float = 0.0  # 手續費 + 滑點 + 價格衝擊
    gas_paid: float = 0.0
    skipped_buys: int = 0  # 因報價幣不足而放棄的買入
    in_range_share: float = 0.0
    max_drawdown: float = 0.0
    exit_cost: float = 0.0  # 期末把剩下的幣全部賣回報價幣要付的成本(含一次 gas);沒有持幣則為 0

    @property
    def final_equity(self):
        return self.equity[-1] if self.equity else self.cfg.capital

    @property
    def return_pct(self):
        """以收盤價計算持倉市值(不賣出)的報酬。"""
        return (self.final_equity / self.cfg.capital - 1) * 100

    @property
    def exit_return_pct(self):
        """期末全部賣回報價幣、扣掉出場成本後的報酬。"""
        return ((self.final_equity - self.exit_cost) / self.cfg.capital - 1) * 100

    @property
    def hodl_pct(self):
        """全程持有(不做網格)的報酬,不含任何成本,當作比較基準。"""
        return (self.end_price / self.start_price - 1) * 100

    @property
    def cycles(self):
        return sum(1 for t in self.trades if t.side == "sell")


class GridEngine:
    """網格的逐步狀態機:回測(simulate)與即時模擬盤(paper)共用,確保兩邊規則一致。"""

    def __init__(self, cfg, costs, p0, ts):
        self.cfg, self.costs = cfg, costs
        self.lines = grid_lines(cfg)
        self.quote = cfg.capital
        self.lots = {}  # 格子編號 → Lot;格子 i 介於 lines[i] 與 lines[i+1]
        self.trades = []
        self.realized_profit = 0.0
        self.trading_cost = 0.0
        self.gas_paid = 0.0
        self.skipped_buys = 0
        # 起始建倉:價格上方(含所在格)的每一格都用起始價買好,視為一筆交易
        initial = [i for i in range(cfg.grids) if self.lines[i + 1] > p0]
        if initial:
            rate = costs.rate(len(initial) * cfg.grid_value)
            for i in initial:
                self._buy(ts, i, p0, cfg.grid_value, rate)
            self.gas_paid += costs.gas_usd

    def _buy(self, ts, cell, price, value, rate):
        qty = value * (1 - rate) / price
        self.quote -= value
        self.trading_cost += value * rate
        self.lots[cell] = Lot(qty, value)
        self.trades.append(Trade(ts, "buy", price, qty, cell))

    def move(self, a, b, ts):
        """價格從 a 連續走到 b,依序處理穿越的格線。"""
        lines, n, gv, costs = self.lines, self.cfg.grids, self.cfg.grid_value, self.costs
        if b < a:  # 往下:依序穿過 (b ≤ line < a) 的格線,由高到低
            for k in range(n - 1, -1, -1):
                if not (b <= lines[k] < a) or k in self.lots:  # 在格線 k 買入格子 k
                    continue
                # 容許浮點誤差:本金分格後反覆加減,quote 可能只比 gv 少 1e-13,不能因此略過買入
                if self.quote < gv * (1 - 1e-9):
                    self.skipped_buys += 1
                    continue
                value = min(gv, self.quote)
                self._buy(ts, k, lines[k], value, costs.rate(value))
                self.gas_paid += costs.gas_usd
        elif b > a:  # 往上:依序穿過 (a < line ≤ b) 的格線,由低到高
            for j in range(1, n + 1):
                if not (a < lines[j] <= b):
                    continue
                lot = self.lots.pop(j - 1, None)
                if lot is None:
                    continue
                value = lot.qty * lines[j]
                rate = costs.rate(value)
                proceeds = value * (1 - rate)
                self.quote += proceeds
                self.trading_cost += value * rate
                self.realized_profit += proceeds - lot.cost
                self.gas_paid += costs.gas_usd
                self.trades.append(Trade(ts, "sell", lines[j], lot.qty, j - 1))

    @property
    def base(self):
        return sum(l.qty for l in self.lots.values())

    def equity(self, price):
        return self.quote + self.base * price - self.gas_paid

    def unrealized_pnl(self, price):
        return sum(l.qty * price - l.cost for l in self.lots.values())

    def exit_cost(self, price):
        """把剩下的幣全部賣回報價幣要付的成本(含一次 gas);沒有持幣則為 0。"""
        held = self.base * price
        return held * self.costs.rate(held) + self.costs.gas_usd if held > 0 else 0.0

    def to_dict(self):
        """存檔用(不含 cfg / costs / trades,由呼叫端另外保存)。"""
        return {
            "quote": self.quote,
            "lots": {str(k): [l.qty, l.cost] for k, l in self.lots.items()},
            "realized_profit": self.realized_profit,
            "trading_cost": self.trading_cost,
            "gas_paid": self.gas_paid,
            "skipped_buys": self.skipped_buys,
        }

    @classmethod
    def from_dict(cls, cfg, costs, d):
        e = cls.__new__(cls)
        e.cfg, e.costs, e.lines, e.trades = cfg, costs, grid_lines(cfg), []
        e.quote = float(d["quote"])
        e.lots = {int(k): Lot(float(q), float(c)) for k, (q, c) in d["lots"].items()}
        for k in e.lots:
            if not 0 <= k < cfg.grids:
                raise ValueError(f"存檔中的格子編號不合理: {k}")
        e.realized_profit = float(d["realized_profit"])
        e.trading_cost = float(d["trading_cost"])
        e.gas_paid = float(d["gas_paid"])
        e.skipped_buys = int(d["skipped_buys"])
        return e


def simulate(candles, cfg, costs, path="auto"):
    if not candles:
        raise ValueError("沒有 K 線可以模擬")
    p0 = candles[0].open
    eng = GridEngine(cfg, costs, p0, candles[0].ts)
    res = Result(cfg, p0, candles[-1].close)
    peak = 0.0
    in_range = 0
    prev_close = p0
    for c in candles:
        # 路徑要連續:從上一根收盤走到這一根開盤(跳空)也可能穿越格線
        pts = [prev_close] + candle_path(c, path)
        prev_close = c.close
        for a, b in zip(pts, pts[1:]):
            eng.move(a, b, c.ts)
        equity = eng.equity(c.close)
        res.equity.append(equity)
        peak = max(peak, equity)
        if peak > 0:
            res.max_drawdown = max(res.max_drawdown, (peak - equity) / peak)
        if cfg.lower <= c.close <= cfg.upper:
            in_range += 1

    last = candles[-1].close
    res.trades = eng.trades
    res.realized_profit = eng.realized_profit
    res.trading_cost = eng.trading_cost
    res.gas_paid = eng.gas_paid
    res.skipped_buys = eng.skipped_buys
    res.unrealized_pnl = eng.unrealized_pnl(last)
    res.in_range_share = in_range / len(candles)
    res.exit_cost = eng.exit_cost(last)
    return res
