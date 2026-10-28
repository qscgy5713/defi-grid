"""即時模擬盤(paper trading):用鏈上每一筆真實成交推進網格,記錄「如果真的下單會怎樣」。

不碰錢包、不送交易。狀態存在 paper/<名稱>.json,成交記錄附加在 paper/<名稱>.trades.jsonl。
每次輪詢只抓上次處理過的區塊之後的 Swap 事件,所以中途關掉再開會從斷點接著跑。
"""
import json
import os
import re
import time
from dataclasses import dataclass

from .sim import CostModel, GridConfig, GridEngine
from .swaps import BLOCK_TIME, MAX_HOURS, fetch_range, latest_head
from .uniswap import check_address, price_token1_per_token0

STATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "paper")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MAX_CATCHUP_BLOCKS = MAX_HOURS * 3600 // BLOCK_TIME  # 停機太久時,最多補抓這麼多區塊
VERSION = 1


def check_name(name):
    if not NAME_RE.match(name or ""):
        raise ValueError("名稱只能用小寫英數字、- 與 _,1~32 字,開頭須為英數字")
    return name


def _paths(name):
    check_name(name)
    return os.path.join(STATE_DIR, f"{name}.json"), os.path.join(STATE_DIR, f"{name}.trades.jsonl")


@dataclass
class Paper:
    name: str
    pool: str
    symbol0: str
    symbol1: str
    decimals0: int
    decimals1: int
    cfg: GridConfig
    costs: CostModel
    engine: GridEngine
    started_ts: int
    start_price: float
    last_block: int
    last_ts: int
    last_price: float
    buys: int = 0
    sells: int = 0
    peak: float = 0.0
    max_drawdown: float = 0.0
    gaps: int = 0  # 停機太久、只能補抓最近一段的次數

    def equity(self):
        return self.engine.equity(self.last_price)

    def exit_equity(self):
        return self.equity() - self.engine.exit_cost(self.last_price)

    def return_pct(self):
        return (self.equity() / self.cfg.capital - 1) * 100

    def exit_return_pct(self):
        return (self.exit_equity() / self.cfg.capital - 1) * 100

    def hodl_pct(self):
        return (self.last_price / self.start_price - 1) * 100

    def to_dict(self):
        c = self.cfg
        return {
            "version": VERSION,
            "name": self.name,
            "pool": self.pool,
            "tokens": [self.symbol0, self.symbol1, self.decimals0, self.decimals1],
            "config": {"lower": c.lower, "upper": c.upper, "grids": c.grids, "capital": c.capital, "mode": c.mode},
            "costs": {"fee_rate": self.costs.fee_rate, "slippage_rate": self.costs.slippage_rate, "gas_usd": self.costs.gas_usd},
            "engine": self.engine.to_dict(),
            "started_ts": self.started_ts,
            "start_price": self.start_price,
            "last_block": self.last_block,
            "last_ts": self.last_ts,
            "last_price": self.last_price,
            "buys": self.buys,
            "sells": self.sells,
            "peak": self.peak,
            "max_drawdown": self.max_drawdown,
            "gaps": self.gaps,
        }

    @classmethod
    def from_dict(cls, d):
        if d.get("version") != VERSION:
            raise ValueError("模擬盤存檔版本不符")
        cfg = GridConfig(**d["config"])
        costs = CostModel(**d["costs"])
        s0, s1, d0, d1 = d["tokens"]
        return cls(
            name=check_name(d["name"]), pool=check_address(d["pool"]), symbol0=s0, symbol1=s1,
            decimals0=int(d0), decimals1=int(d1), cfg=cfg, costs=costs,
            engine=GridEngine.from_dict(cfg, costs, d["engine"]),
            started_ts=int(d["started_ts"]), start_price=float(d["start_price"]),
            last_block=int(d["last_block"]), last_ts=int(d["last_ts"]), last_price=float(d["last_price"]),
            buys=int(d["buys"]), sells=int(d["sells"]), peak=float(d["peak"]),
            max_drawdown=float(d["max_drawdown"]), gaps=int(d.get("gaps", 0)),
        )


def exists(name):
    return os.path.exists(_paths(name)[0])


def list_names():
    if not os.path.isdir(STATE_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(STATE_DIR) if f.endswith(".json") and NAME_RE.match(f[:-5]))


def load(name):
    path, _ = _paths(name)
    if not os.path.exists(path):
        raise ValueError(f"找不到模擬盤「{name}」;用 paper run 建立")
    try:
        with open(path) as f:
            return Paper.from_dict(json.load(f))
    except (OSError, KeyError, TypeError) as e:
        raise ValueError(f"模擬盤「{name}」的存檔損毀: {e}") from e


def save(p):
    path, _ = _paths(p.name)
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(p.to_dict(), f, indent=1)
    os.replace(tmp, path)  # 原子寫入:中途被中斷也不會留下半個檔案


def read_trades(name, limit=None):
    _, log = _paths(name)
    if not os.path.exists(log):
        return []
    with open(log) as f:
        rows = [json.loads(line) for line in f if line.strip()]
    return rows[-limit:] if limit else rows


def create(name, state, cfg_args, costs):
    """以池子目前價格建立新的模擬盤。cfg_args:grids、capital、mode,以及 range_pct 或 lower / upper。"""
    if exists(name):
        raise ValueError(f"模擬盤「{name}」已存在")
    price = price_token1_per_token0(state)
    if cfg_args.get("lower") is not None:
        lower, upper = cfg_args["lower"], cfg_args["upper"]
    else:
        r = cfg_args["range_pct"]
        lower, upper = price * (1 - r / 100), price * (1 + r / 100)
    cfg = GridConfig(lower, upper, cfg_args["grids"], cfg_args["capital"], cfg_args["mode"])
    now = int(time.time())
    eng = GridEngine(cfg, costs, price, now)
    p = Paper(name, state.address, state.symbol0, state.symbol1, state.decimals0, state.decimals1, cfg, costs, eng,
              started_ts=now, start_price=price, last_block=state.block, last_ts=now, last_price=price)
    new = _take_trades(p)
    p.peak = p.equity()
    save(p)
    _append(name, new)
    return p


def _take_trades(p):
    """取出引擎裡新產生的成交並更新計數。"""
    new, p.engine.trades = p.engine.trades, []
    p.buys += sum(1 for t in new if t.side == "buy")
    p.sells += sum(1 for t in new if t.side == "sell")
    return new


def step(p, rpc, progress=None):
    """處理上次之後的新區塊。回傳 (新成交, 處理的 Swap 筆數, 是否因停機太久而跳過一段)。"""
    latest_block, latest_ts = latest_head(rpc)
    if latest_block <= p.last_block:
        return [], 0, False
    start = p.last_block + 1
    gap = latest_block - start + 1 > MAX_CATCHUP_BLOCKS
    if gap:
        start = latest_block - MAX_CATCHUP_BLOCKS + 1
    swaps = fetch_range(rpc, _PoolRef(p), start, latest_block, latest_block, latest_ts, progress)
    if gap:
        p.gaps += 1
    for s in swaps:
        # 跳過的那段只能當作價格直接走過去(期間的來回震盪會漏算)
        p.engine.move(p.last_price, s.price, s.ts)
        p.last_price = s.price
    p.last_block, p.last_ts = latest_block, latest_ts
    eq = p.equity()
    p.peak = max(p.peak, eq)
    if p.peak > 0:
        p.max_drawdown = max(p.max_drawdown, (p.peak - eq) / p.peak)
    # 先存狀態再寫成交記錄:萬一中途被中斷,頂多少一筆記錄,不會重複處理同一段區塊
    new = _take_trades(p)
    save(p)
    _append(p.name, new)
    return new, len(swaps), gap


def _append(name, trades):
    if not trades:
        return
    _, log = _paths(name)
    with open(log, "a") as f:
        for t in trades:
            f.write(json.dumps({"ts": t.ts, "side": t.side, "price": t.price, "qty": t.qty, "cell": t.cell}) + "\n")


class _PoolRef:
    """fetch_range 需要的池子資訊(address、decimals)。"""

    def __init__(self, p):
        self.address, self.decimals0, self.decimals1 = p.pool, p.decimals0, p.decimals1
