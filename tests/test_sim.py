import math
import unittest

from defigrid.sim import Candle, CostModel, GridConfig, candle_path, economics, grid_lines, simulate

ZERO = CostModel(fee_rate=0, slippage_rate=0, gas_usd=0)


def candles(prices, wick=0.0):
    """每個價格一根 K 線(開=收=該價,可加影線)。"""
    return [Candle(i * 3600, p, p * (1 + wick), p * (1 - wick), p) for i, p in enumerate(prices)]


def zigzag(lo, hi, rounds, steps=20):
    out = []
    for _ in range(rounds):
        out += [lo + (hi - lo) * i / steps for i in range(steps)] + [hi - (hi - lo) * i / steps for i in range(steps)]
    return out


class ConfigTests(unittest.TestCase):
    def test_validation(self):
        bad = [
            dict(lower=0, upper=10, grids=5, capital=100),
            dict(lower=10, upper=10, grids=5, capital=100),
            dict(lower=1, upper=10, grids=1, capital=100),
            dict(lower=1, upper=10, grids=501, capital=100),
            dict(lower=1, upper=10, grids=5, capital=0),
            dict(lower=1, upper=10, grids=5, capital=100, mode="foo"),
            dict(lower=1, upper=float("inf"), grids=5, capital=100),
            dict(lower=1, upper=float("nan"), grids=5, capital=100),
        ]
        for kw in bad:
            with self.assertRaises(ValueError, msg=str(kw)):
                GridConfig(**kw)
        with self.assertRaises(ValueError):
            CostModel(gas_usd=-1)
        with self.assertRaises(ValueError):
            CostModel(fee_rate=0.6, slippage_rate=0.4)

    def test_grid_lines(self):
        arith = grid_lines(GridConfig(100, 200, 4, 1000))
        self.assertEqual(arith, [100, 125, 150, 175, 200])
        geo = grid_lines(GridConfig(100, 400, 2, 1000, "geometric"))
        self.assertAlmostEqual(geo[1], 200)
        self.assertAlmostEqual(geo[2] / geo[0], 4)


class SimulationTests(unittest.TestCase):
    cfg = GridConfig(90, 110, 10, 1000)

    def test_flat_price_no_trades(self):
        r = simulate(candles([100.0] * 50), self.cfg, ZERO)
        self.assertEqual(r.cycles, 0)
        self.assertAlmostEqual(r.final_equity, 1000.0)
        self.assertAlmostEqual(r.max_drawdown, 0.0)

    def test_zero_cost_zigzag_profits(self):
        r = simulate(candles(zigzag(90, 110, 5)), self.cfg, ZERO)
        self.assertGreater(r.cycles, 20)
        self.assertGreater(r.final_equity, 1000.0)
        self.assertGreater(r.realized_profit, 0)

    def test_equity_identity(self):
        # 恆等式:期末資產 = 本金 + 已實現 + 未實現 - gas(任何成本設定下都要成立,等於檢查沒有憑空生錢)
        costs = CostModel(0.0005, 0.0002, 0.03)
        for prices in (zigzag(90, 110, 3), [100, 80, 60], [100, 120, 140], zigzag(95, 105, 4)):
            for path in ("auto", "low-first", "high-first"):
                r = simulate(candles(prices, wick=0.01), self.cfg, costs, path)
                expected = 1000 + r.realized_profit + r.unrealized_pnl - r.gas_paid
                self.assertAlmostEqual(r.final_equity, expected, places=6, msg=f"{prices[:3]} {path}")

    def test_costs_reduce_profit(self):
        data = candles(zigzag(90, 110, 5))
        free = simulate(data, self.cfg, ZERO).final_equity
        fees = simulate(data, self.cfg, CostModel(0.003, 0.001, 0)).final_equity
        gas = simulate(data, self.cfg, CostModel(0, 0, 5.0)).final_equity
        self.assertLess(fees, free)
        self.assertLess(gas, free)
        self.assertLess(simulate(data, self.cfg, CostModel(0, 0, 1000.0)).final_equity, 0)  # gas 失控時淨值為負

    def test_price_above_range_ends_in_quote(self):
        r = simulate(candles([100, 105, 110, 115, 130]), self.cfg, ZERO)
        self.assertEqual(len(r.trades) % 2, 0)  # 買進的全部賣出
        self.assertEqual(r.unrealized_pnl, 0)
        self.assertGreater(r.final_equity, 1000.0)

    def test_price_below_range_holds_everything(self):
        r = simulate(candles([100, 95, 90, 80, 60]), self.cfg, ZERO)
        self.assertEqual(r.cycles, 0)
        self.assertLess(r.final_equity, 1000.0)  # 賠錢,但比全程持有賠得少(分批買低)
        self.assertGreater(r.final_equity, 1000.0 * 60 / 100)
        self.assertLess(r.in_range_share, 0.7)

    def test_start_below_range_buys_everything_once(self):
        r = simulate(candles([50, 50, 60]), GridConfig(90, 110, 10, 1000), ZERO)
        # 起始價低於下界:所有格子都已是持倉,價格仍在下界以下 → 沒有賣出
        self.assertEqual(r.cycles, 0)
        self.assertEqual(len(r.trades), 10)

    def test_start_above_range_all_cash(self):
        r = simulate(candles([200, 205, 190]), GridConfig(90, 110, 10, 1000), ZERO)
        self.assertEqual(r.trades, [])
        self.assertAlmostEqual(r.final_equity, 1000.0)

    def test_path_assumption_irrelevant_for_monotone_moves(self):
        data = candles([100, 104, 108])
        counts = {len(simulate(data, self.cfg, ZERO, p).trades) for p in ("auto", "low-first", "high-first")}
        self.assertEqual(len(counts), 1)

    def test_candle_path(self):
        up, down = Candle(0, 10, 15, 5, 12), Candle(0, 10, 15, 5, 8)
        self.assertEqual(candle_path(up), [10, 5, 15, 12])
        self.assertEqual(candle_path(down), [10, 15, 5, 8])
        self.assertEqual(candle_path(up, "high-first"), [10, 15, 5, 12])
        with self.assertRaises(ValueError):
            candle_path(up, "x")

    def test_buys_skipped_when_cash_runs_out(self):
        # 成本高到每個循環都虧,現金逐步耗盡後要略過買入,而不是讓餘額變負
        cfg = GridConfig(90, 110, 10, 1000)
        r = simulate(candles(zigzag(90, 110, 40)), cfg, CostModel(0.05, 0.05, 0))
        self.assertGreater(r.skipped_buys, 0)
        self.assertGreater(min(r.equity), -1e-6)

    def test_float_rounding_never_skips_affordable_buys(self):
        # 回歸測試:本金分格後的浮點誤差曾讓 quote 比每格資金少 1e-13,導致錯誤略過買入
        data = [Candle(0, 111, 111, 111, 111), Candle(1, 89, 89, 89, 89)]
        for capital in (1000, 999.99, 1234.56, 777, 100, 3000.3):
            for grids in range(2, 60):
                r = simulate(data, GridConfig(90, 110, grids, capital), ZERO)
                self.assertEqual(r.skipped_buys, 0, f"本金 {capital}、{grids} 格")
                self.assertEqual(len(r.trades), grids)
                self.assertGreaterEqual(min(r.equity), -1e-6)

    def test_empty_candles(self):
        with self.assertRaises(ValueError):
            simulate([], self.cfg, ZERO)


class EconomicsTests(unittest.TestCase):
    def test_breakeven_spacing_gives_zero_net(self):
        costs = CostModel(0.0005, 0.0002, 0.05)
        cfg = GridConfig(90, 110, 10, 1000)
        be = economics(cfg, costs, 100)["breakeven_spacing_pct"] / 100
        # 做一個格距正好等於損益兩平的網格,淨損益應為 0
        cfg2 = GridConfig(100, 100 * (1 + be) ** 4, 4, 400, "geometric")
        e = economics(cfg2, costs, 100 * (1 + be) / 2 * 1.0)
        self.assertAlmostEqual(e["net_per_cycle_usd"], 0.0, places=6)

    def test_viability_flag(self):
        costs = CostModel(0.0005, 0.0002, 0.03)
        self.assertTrue(economics(GridConfig(90, 110, 5, 5000), costs, 100)["viable"])
        self.assertFalse(economics(GridConfig(99, 101, 100, 1000), costs, 100)["viable"])  # 格距 0.02% 遠低於成本
        self.assertFalse(economics(GridConfig(90, 110, 20, 10), costs, 100)["viable"])  # 每格資金太小,gas 吃光

    def test_matches_simulation_for_one_cycle(self):
        # 格線 100 / 110 / 120,起始在上界之外(沒有持倉)。
        # 價格 125 → 109 觸發「在 110 買入」,再 → 121 觸發「在 120 賣出」,剛好一個完整循環。
        costs = CostModel(0.0005, 0.0002, 0.03)
        cfg = GridConfig(100, 120, 2, 2000)  # 每格資金 1000
        data = [Candle(0, 125, 125, 125, 125), Candle(1, 109, 109, 109, 109), Candle(2, 121, 121, 121, 121)]
        r = simulate(data, cfg, costs)
        self.assertEqual([(t.side, t.price) for t in r.trades], [("buy", 110), ("sell", 120)])
        predicted = economics(cfg, costs, 115)["net_per_cycle_usd"]  # 115 落在 110~120 這一格
        self.assertAlmostEqual(r.final_equity - cfg.capital, predicted, places=9)
        self.assertAlmostEqual(r.realized_profit - r.gas_paid, predicted, places=9)

    def test_gap_between_candles_still_crosses_lines(self):
        # 兩根 K 線各自內部沒有波動,但收盤到下一根開盤之間跳空穿越了格線
        cfg = GridConfig(100, 120, 2, 2000)
        data = [Candle(0, 125, 125, 125, 125), Candle(1, 105, 105, 105, 105)]
        r = simulate(data, cfg, ZERO)
        self.assertEqual([(t.side, t.price) for t in r.trades], [("buy", 110)])
        self.assertAlmostEqual(r.unrealized_pnl, 1000 * (105 / 110 - 1), places=9)

if __name__ == "__main__":
    unittest.main()
