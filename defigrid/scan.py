"""參數掃描:一次試多組(格數 × 區間),並用「樣本內排名、樣本外驗證」降低過度擬合的誤判。

在同一段歷史上挑出報酬最高的參數,往往只是剛好貼合那段行情。所以把資料切兩段:
前段(樣本內)用來排名,後段(樣本外)檢查排名靠前的參數換一段行情後是否仍然有效。

單一切點仍可能剛好切在某段行情;walk_forward 把「挑參數 → 驗證」沿時間反覆做很多輪,結果比較不靠運氣。
"""
from dataclasses import dataclass
from statistics import median
from typing import List, Optional

from .sim import GridConfig, economics, simulate

MIN_SEGMENT = 24  # 每一段至少一天的小時 K 線


@dataclass(frozen=True)
class SegmentResult:
    return_pct: float  # 持倉以收盤價計值
    exit_return_pct: float  # 期末全部賣出、扣掉出場成本
    cycles: int
    max_drawdown: float
    in_range_share: float


@dataclass(frozen=True)
class ScanRow:
    grids: int
    range_pct: float
    spacing_pct: float
    viable: bool  # 以樣本內起始價估算,每個買賣循環扣完成本是否有賺
    train: SegmentResult
    test: Optional[SegmentResult]


def split_candles(candles, train_frac):
    if not 0 < train_frac <= 1:
        raise ValueError("樣本內比例必須介於 0 與 1 之間(1 = 不切樣本外)")
    if train_frac == 1:
        if len(candles) < MIN_SEGMENT:
            raise ValueError(f"至少需要 {MIN_SEGMENT} 根 K 線")
        return list(candles), []
    k = int(len(candles) * train_frac)
    if k < MIN_SEGMENT or len(candles) - k < MIN_SEGMENT:
        raise ValueError(f"樣本內與樣本外各至少需要 {MIN_SEGMENT} 根 K 線,請增加天數或調整比例")
    return list(candles[:k]), list(candles[k:])


def _config(candles, grids, range_pct, capital, mode):
    # 區間以「該段起始價」為中心,樣本外會重新置中,模擬「到時候再依當下價格設網格」
    p0 = candles[0].open
    return GridConfig(p0 * (1 - range_pct / 100), p0 * (1 + range_pct / 100), grids, capital, mode)


def _segment(candles, cfg, costs, path):
    r = simulate(candles, cfg, costs, path)
    return SegmentResult(r.return_pct, r.exit_return_pct, r.cycles, r.max_drawdown, r.in_range_share)


def hodl_pct(candles):
    return (candles[-1].close / candles[0].open - 1) * 100 if candles else 0.0


def _combos(grids_list, range_list):
    if not grids_list or not range_list:
        raise ValueError("格數與區間清單都不能是空的")
    for r in range_list:
        if not 0 < r < 100:
            raise ValueError(f"區間百分比必須介於 0 與 100 之間: {r}")
    return [(g, r) for g in sorted(set(grids_list)) for r in sorted(set(range_list))]


def _evaluate(train, test, combos, capital, costs, mode, path):
    """每組參數各跑一次樣本內與樣本外,依樣本內報酬由高到低排序(同分時格數少的優先)。"""
    rows = []
    for g, r in combos:
        cfg = _config(train, g, r, capital, mode)
        e = economics(cfg, costs, train[0].open)
        rows.append(
            ScanRow(
                grids=g,
                range_pct=r,
                spacing_pct=e["spacing_pct"],
                viable=e["viable"],
                train=_segment(train, cfg, costs, path),
                test=_segment(test, _config(test, g, r, capital, mode), costs, path) if test else None,
            )
        )
    rows.sort(key=lambda x: (x.train.return_pct, -x.grids), reverse=True)
    return rows


def scan(candles, grids_list, range_list, capital, costs, mode="arithmetic", path="auto", train_frac=0.7):
    """回傳 (依樣本內報酬由高到低排序的結果, 樣本內 K 線, 樣本外 K 線)。"""
    combos = _combos(grids_list, range_list)
    train, test = split_candles(candles, train_frac)
    return _evaluate(train, test, combos, capital, costs, mode, path), train, test


@dataclass(frozen=True)
class Fold:
    """滾動式驗證的一輪:用 train 期間挑出最佳參數,拿到緊接在後的 test 期間實際跑。"""
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    rows: List[ScanRow]  # 依樣本內報酬排序;rows[0] 就是這一輪「事先」選中的參數
    hodl_pct: float  # 樣本外期間持有幣的報酬

    @property
    def chosen(self):
        return self.rows[0]

    @property
    def chosen_rank(self):
        """選中的參數在這一輪樣本外排第幾名(1 = 最好,以出場後報酬比較)。"""
        t = self.chosen.test.exit_return_pct
        return 1 + sum(1 for r in self.rows if r.test.exit_return_pct > t)


def walk_forward(candles, grids_list, range_list, capital, costs, mode="arithmetic", path="auto",
                 train_len=720, test_len=168, anchored=False):
    """滾動式驗證(walk-forward):反覆「用過去 train_len 根挑參數 → 用接下來 test_len 根驗證」,再往後推 test_len。

    各輪的樣本外期間首尾相接、不重疊。多出來湊不滿一輪的 K 線從最舊的那端捨棄,讓最後一輪結束在最新資料。
    anchored=True 時樣本內從資料開頭一路累積(擴張視窗),否則固定長度(滾動視窗)。
    每輪結束都要出場才能依新價格重設網格,所以樣本外一律用「扣掉出場成本」的報酬。
    """
    combos = _combos(grids_list, range_list)
    if train_len < MIN_SEGMENT or test_len < MIN_SEGMENT:
        raise ValueError(f"樣本內與樣本外每段至少需要 {MIN_SEGMENT} 根 K 線")
    n = len(candles)
    if train_len + test_len > n:
        raise ValueError(f"只有 {n} 根 K 線,不夠一輪(樣本內 {train_len} + 樣本外 {test_len});請增加天數或縮短視窗")
    offset = (n - train_len) % test_len
    folds = []
    for s in range(offset + train_len, n - test_len + 1, test_len):
        train = list(candles[offset if anchored else s - train_len : s])
        test = list(candles[s : s + test_len])
        rows = _evaluate(train, test, combos, capital, costs, mode, path)
        folds.append(Fold(train[0].ts, train[-1].ts, test[0].ts, test[-1].ts, rows, hodl_pct(test)))
    return folds


def _chain(pcts):
    """把各期報酬(%)連乘成總報酬(%),相當於每期結束把全部資產帶到下一期。"""
    x = 1.0
    for p in pcts:
        x *= 1 + p / 100
    return (x - 1) * 100


def walk_summary(folds: List[Fold]):
    if not folds:
        raise ValueError("沒有任何一輪可以彙總")
    keys = [(r.grids, r.range_pct) for r in folds[0].rows]
    per_fold = [{(r.grids, r.range_pct): r.test.exit_return_pct for r in f.rows} for f in folds]
    # 對照組:整段都固定用同一組參數(每輪一樣重新以當下價格置中)
    fixed = {k: _chain(d[k] for d in per_fold) for k in keys}
    best_key = max(keys, key=lambda k: fixed[k])
    chosen = [f.chosen.test.exit_return_pct for f in folds]
    return {
        "folds": len(folds),
        "wf_pct": _chain(chosen),
        "hodl_pct": _chain(f.hodl_pct for f in folds),
        "profitable": sum(1 for x in chosen if x > 0),
        "beat_hodl": sum(1 for x, f in zip(chosen, folds) if x > f.hodl_pct),
        # 選中的參數在該輪是否優於所有組合的中位數:衡量「依過去挑參數」有沒有比隨便挑好
        "beat_median": sum(1 for x, d in zip(chosen, per_fold) if x > median(d.values())),
        "avg_rank": sum(f.chosen_rank for f in folds) / len(folds),
        "combos": len(keys),
        "changes": sum(1 for a, b in zip(folds, folds[1:]) if (a.chosen.grids, a.chosen.range_pct) != (b.chosen.grids, b.chosen.range_pct)),
        "fixed_median_pct": median(fixed.values()),
        "fixed_best": (best_key, fixed[best_key]),
        "worst_fold_pct": min(chosen),
    }


def _ranks(values):
    """平均名次(同分取平均),用於 Spearman 相關。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2
        i = j + 1
    return ranks


def spearman(xs, ys) -> Optional[float]:
    """排名相關:1 = 樣本內排名完全預測樣本外,0 = 沒有關係,-1 = 完全相反。資料不足或沒有變異時回傳 None。"""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    rx, ry = _ranks(list(xs)), _ranks(list(ys))
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx == 0 or vy == 0:
        return None
    return cov / (vx * vy) ** 0.5


def summarize(rows: List[ScanRow]):
    tested = [r for r in rows if r.test is not None]
    if not tested:
        return {"rank_corr": None, "test_profitable": None, "best_train": rows[0] if rows else None}
    return {
        "rank_corr": spearman([r.train.return_pct for r in tested], [r.test.return_pct for r in tested]),
        "test_profitable": sum(1 for r in tested if r.test.return_pct > 0),
        "best_train": rows[0],
        "best_test": max(tested, key=lambda r: r.test.return_pct),
    }
