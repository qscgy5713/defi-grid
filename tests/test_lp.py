import math
import unittest

from defigrid.lp import LpConfig, amounts, human_liquidity, in_range_fraction, position_value, simulate_lp
from defigrid.sim import Candle, CostModel

ZERO = CostModel(fee_rate=0, slippage_rate=0, gas_usd=0)


def flat(prices, volume=0.0):
    return [Candle(i * 3600, p, p, p, p, volume) for i, p in enumerate(prices)]


class MathTests(unittest.TestCase):
    def test_amounts_at_bounds(self):
        x, y = amounts(10.0, 90, 100, 121)  # 低於區間:全部是幣
        self.assertAlmostEqual(y, 0.0)
        self.assertAlmostEqual(x, 10 * (1 / 10 - 1 / 11))
        x, y = amounts(10.0, 200, 100, 121)  # 高於區間:全部是報價幣
        self.assertAlmostEqual(x, 0.0)
        self.assertAlmostEqual(y, 10 * (11 - 10))

    def test_value_is_continuous_and_increasing(self):
        vals = [position_value(5.0, p, 100, 121) for p in (95, 100, 105, 110, 115, 121, 130)]
        self.assertEqual(vals, sorted(vals))
        self.assertAlmostEqual(vals[-1], vals[-2])  # 超過上界後不再變

    def test_in_range_fraction(self):
        self.assertEqual(in_range_fraction(Candle(0, 105, 110, 100, 105), 100, 120), 1.0)
        self.assertAlmostEqual(in_range_fraction(Candle(0, 115, 130, 110, 115), 100, 120), 0.5)
        self.assertEqual(in_range_fraction(Candle(0, 150, 160, 140, 150), 100, 120), 0.0)
        self.assertEqual(in_range_fraction(Candle(0, 110, 110, 110, 110), 100, 120), 1.0)

    def test_human_liquidity(self):
        self.assertAlmostEqual(human_liquidity(10**12 * 5, 18, 6), 5.0)


class SimTests(unittest.TestCase):
    def test_flat_price_no_cost_keeps_capital(self):
        r = simulate_lp(flat([100] * 10), LpConfig(90, 110, 1000, 0.0005, 0), ZERO)
        self.assertAlmostEqual(r.final_equity, 1000)
        self.assertAlmostEqual(r.exit_return_pct, 0)
        self.assertAlmostEqual(r.impermanent_loss, 0)

    def test_impermanent_loss_never_positive(self):
        for end in (60, 90, 100, 105, 130, 200):
            r = simulate_lp(flat([100, end]), LpConfig(90, 110, 1000, 0.0005, 0), ZERO)
            self.assertLessEqual(r.impermanent_loss, 1e-9, end)

    def test_above_range_is_all_quote(self):
        cfg = LpConfig(90, 110, 1000, 0.0005, 0)
        r = simulate_lp(flat([100, 150]), cfg, ZERO)
        self.assertAlmostEqual(r.final_equity, r.liquidity * (math.sqrt(110) - math.sqrt(90)))
        self.assertLess(r.final_equity, 1000 * 1.5)  # 漲出區間後少賺漲幅

    def test_fees_follow_share_and_range(self):
        c = flat([100] * 5, volume=1_000_000)
        r = simulate_lp(c, LpConfig(90, 110, 1000, 0.0005, 0), ZERO)
        self.assertAlmostEqual(r.share, 1.0)  # 池子沒有其他流動性
        self.assertAlmostEqual(r.fees, 5 * 1_000_000 * 0.0005)
        r1 = simulate_lp(c, LpConfig(90, 110, 1000, 0.0005, 1e6), ZERO)
        r2 = simulate_lp(c, LpConfig(90, 110, 1000, 0.0005, 2e6), ZERO)
        self.assertAlmostEqual(r1.fees / r2.fees, (2e6 + r2.liquidity) / (1e6 + r1.liquidity))
        out = simulate_lp(flat([200] * 5, volume=1_000_000), LpConfig(90, 110, 1000, 0.0005, 0), ZERO)
        self.assertEqual(out.fees, 0)  # 區間外不收手續費

    def test_costs(self):
        costs = CostModel(fee_rate=0.001, slippage_rate=0, gas_usd=1.0)
        r = simulate_lp(flat([100] * 3), LpConfig(90, 110, 1000, 0.0005, 0, lp_gas_mult=2), costs)
        self.assertAlmostEqual(r.final_equity, 1000 - r.entry_cost, places=6)
        self.assertGreater(r.entry_cost, 3.0)  # swap gas 1 + 加入流動性 gas 2 + swap 手續費
        self.assertGreater(r.exit_cost, 3.0)
        below = simulate_lp(flat([100, 50]), LpConfig(90, 110, 1000, 0.0005, 0, lp_gas_mult=2), costs)
        above = simulate_lp(flat([100, 150]), LpConfig(90, 110, 1000, 0.0005, 0, lp_gas_mult=2), costs)
        self.assertAlmostEqual(above.exit_cost, 2.0)  # 全是 USDC,只要移除流動性
        self.assertGreater(below.exit_cost, 3.0)  # 全是幣,還要換回 USDC

    def test_validation(self):
        for args in ((110, 90, 1000, 0.0005, 0), (0, 90, 1000, 0.0005, 0), (90, 110, 0, 0.0005, 0),
                     (90, 110, 1000, 1.5, 0), (90, 110, 1000, 0.0005, -1)):
            with self.assertRaises(ValueError, msg=args):
                LpConfig(*args)
        with self.assertRaises(ValueError):
            simulate_lp([], LpConfig(90, 110, 1000, 0.0005, 0), ZERO)


if __name__ == "__main__":
    unittest.main()
