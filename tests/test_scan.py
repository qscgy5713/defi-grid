import unittest

from defigrid.scan import _chain, scan, spearman, split_candles, summarize, walk_forward, walk_summary
from defigrid.sim import Candle, CostModel

ZERO = CostModel(fee_rate=0, slippage_rate=0, gas_usd=0)


def series(prices):
    return [Candle(i * 3600, p, p, p, p) for i, p in enumerate(prices)]


def zigzag(center, amp, n):
    # 在 center ± amp 之間來回,每 10 根 K 線一個來回
    out = []
    for i in range(n):
        phase = (i % 10) / 10
        out.append(center + amp * (1 - 4 * abs(phase - 0.5)))
    return out


class SplitTests(unittest.TestCase):
    def test_split_sizes(self):
        train, test = split_candles(series([100.0] * 100), 0.7)
        self.assertEqual((len(train), len(test)), (70, 30))
        self.assertEqual(train[-1].ts + 3600, test[0].ts)  # 不重疊、不漏

    def test_no_test_segment(self):
        train, test = split_candles(series([100.0] * 30), 1)
        self.assertEqual((len(train), test), (30, []))

    def test_invalid(self):
        for frac, n in ((0, 100), (1.2, 100), (-0.5, 100), (0.7, 40), (0.99, 100), (1, 10)):
            with self.assertRaises(ValueError, msg=(frac, n)):
                split_candles(series([100.0] * n), frac)


class SpearmanTests(unittest.TestCase):
    def test_values(self):
        self.assertAlmostEqual(spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)
        self.assertAlmostEqual(spearman([1, 2, 3, 4], [4, 3, 2, 1]), -1.0)
        self.assertIsNone(spearman([1, 1, 1], [1, 2, 3]))  # 沒有變異
        self.assertIsNone(spearman([1, 2], [1, 2]))  # 太少
        self.assertAlmostEqual(spearman([1, 2, 2, 3], [1, 2, 2, 3]), 1.0)  # 同分也正確


class ScanTests(unittest.TestCase):
    def test_rows_sorted_and_complete(self):
        data = series(zigzag(100, 8, 200))
        rows, train, test = scan(data, [5, 10, 20], [5, 10], 1000, ZERO)
        self.assertEqual(len(rows), 6)
        returns = [r.train.return_pct for r in rows]
        self.assertEqual(returns, sorted(returns, reverse=True))
        self.assertTrue(all(r.test is not None for r in rows))
        self.assertEqual(len(train) + len(test), 200)

    def test_range_that_contains_oscillation_profits(self):
        # 價格在 ±8% 內來回:±10% 的網格會反覆成交;±5% 的網格大部分時間在區間外
        rows, _, _ = scan(series(zigzag(100, 8, 200)), [10], [5, 10], 1000, ZERO)
        by_range = {r.range_pct: r for r in rows}
        self.assertGreater(by_range[10].train.cycles, 0)
        self.assertGreater(by_range[10].train.return_pct, 0)
        self.assertGreater(by_range[10].train.in_range_share, by_range[5].train.in_range_share)

    def test_duplicates_removed_and_validation(self):
        rows, _, _ = scan(series(zigzag(100, 8, 100)), [10, 10], [5, 5], 1000, ZERO)
        self.assertEqual(len(rows), 1)
        for bad in ([], [0], [100], [-5]):
            with self.assertRaises(ValueError, msg=bad):
                scan(series(zigzag(100, 8, 100)), [10], bad, 1000, ZERO)
        with self.assertRaises(ValueError):
            scan(series(zigzag(100, 8, 100)), [], [5], 1000, ZERO)

    def test_without_test_segment(self):
        rows, _, test = scan(series(zigzag(100, 8, 100)), [5, 10], [5, 10], 1000, ZERO, train_frac=1)
        self.assertEqual(test, [])
        self.assertTrue(all(r.test is None for r in rows))
        self.assertIsNone(summarize(rows)["rank_corr"])

    def test_test_segment_recenters_grid(self):
        # 樣本外的區間以樣本外起始價為中心;價格整體上移後仍應落在新區間內
        prices = zigzag(100, 3, 70) + zigzag(150, 3, 30)
        rows, _, test = scan(series(prices), [10], [5], 1000, ZERO)
        self.assertGreater(rows[0].test.in_range_share, 0.9)


class WalkForwardTests(unittest.TestCase):
    def test_folds_contiguous_and_end_at_latest(self):
        data = series(zigzag(100, 8, 200))
        folds = walk_forward(data, [5, 10], [5, 10], 1000, ZERO, train_len=48, test_len=24)
        self.assertEqual(len(folds), (200 - 48) // 24)
        self.assertEqual(folds[-1].test_end, data[-1].ts)  # 最後一輪用到最新資料
        for f in folds:
            self.assertEqual(f.train_end + 3600, f.test_start)  # 樣本內緊接樣本外
            self.assertEqual((f.train_end - f.train_start) // 3600 + 1, 48)  # 滾動視窗固定長度
            self.assertEqual((f.test_end - f.test_start) // 3600 + 1, 24)
        for a, b in zip(folds, folds[1:]):
            self.assertEqual(a.test_end + 3600, b.test_start)  # 樣本外首尾相接、不重疊

    def test_anchored_window_grows(self):
        data = series(zigzag(100, 8, 200))
        folds = walk_forward(data, [5], [10], 1000, ZERO, train_len=48, test_len=24, anchored=True)
        self.assertEqual(len({f.train_start for f in folds}), 1)
        self.assertEqual(folds[-1].train_end + 3600, folds[-1].test_start)

    def test_chosen_is_best_in_sample(self):
        folds = walk_forward(series(zigzag(100, 8, 200)), [5, 10, 20], [5, 10], 1000, ZERO, train_len=48, test_len=24)
        for f in folds:
            self.assertEqual(f.chosen.train.return_pct, max(r.train.return_pct for r in f.rows))
            self.assertTrue(1 <= f.chosen_rank <= len(f.rows))

    def test_summary(self):
        self.assertAlmostEqual(_chain([10, -10]), -1.0)
        self.assertAlmostEqual(_chain([]), 0.0)
        folds = walk_forward(series(zigzag(100, 8, 200)), [5, 10, 20], [5, 10], 1000, ZERO, train_len=48, test_len=24)
        s = walk_summary(folds)
        self.assertEqual((s["folds"], s["combos"]), (len(folds), 6))
        self.assertAlmostEqual(s["wf_pct"], _chain(f.chosen.test.exit_return_pct for f in folds))
        for f in folds:  # 出場成本只會讓報酬變差
            self.assertLessEqual(f.chosen.test.exit_return_pct, f.chosen.test.return_pct)
        self.assertLessEqual(s["fixed_median_pct"], s["fixed_best"][1])
        self.assertLessEqual(s["changes"], len(folds) - 1)
        # 價格在 ±8% 內來回、成本為 0,且每輪長度是來回週期的整數倍(起訖相位相同,不會因收在低點而帳面虧損)
        # → 挑出來的網格每輪都應該賺錢
        aligned = walk_forward(series(zigzag(100, 8, 200)), [5, 10], [10], 1000, ZERO, train_len=50, test_len=30)
        self.assertEqual(walk_summary(aligned)["profitable"], len(aligned))

    def test_invalid(self):
        data = series(zigzag(100, 8, 100))
        for kw in ({"train_len": 10, "test_len": 24}, {"train_len": 48, "test_len": 10}, {"train_len": 80, "test_len": 24}):
            with self.assertRaises(ValueError, msg=kw):
                walk_forward(data, [5], [10], 1000, ZERO, **kw)
        with self.assertRaises(ValueError):
            walk_summary([])


if __name__ == "__main__":
    unittest.main()
