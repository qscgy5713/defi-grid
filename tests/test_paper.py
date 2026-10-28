import json
import os
import tempfile
import unittest
from unittest import mock

from defigrid import paper
from defigrid.sim import Candle, CostModel, GridConfig, simulate
from defigrid.uniswap import PoolState, Q96
from tests.test_swaps import swap_log

COSTS = CostModel(fee_rate=0.0005, slippage_rate=0.0002, gas_usd=0.03)
CFG = {"grids": 10, "capital": 1000.0, "mode": "arithmetic", "range_pct": 5.0, "lower": None, "upper": None}


def pool_state(price, block):
    sqrt_p = int((price * 10**-12) ** 0.5 * Q96)
    return PoolState("0x" + "11" * 20, block, "0x0", "0x1", "WETH", "USDC", 18, 6, 500, sqrt_p, 0, 10**18)


class ChainRpc:
    """假鏈:logs 依區塊號給定,head 可以往前推。"""

    def __init__(self, head, logs):
        self.head, self.logs = head, logs

    def call(self, method, params):
        if method == "eth_getBlockByNumber":
            return {"number": hex(self.head), "timestamp": hex(1_000_000 + self.head * 2)}
        frm, to = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
        return [l for l in self.logs if frm <= int(l["blockNumber"], 16) <= to]


class PaperTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = mock.patch.object(paper, "STATE_DIR", tmp.name)
        p.start()
        self.addCleanup(p.stop)

    def test_incremental_equals_backtest(self):
        prices = [2000, 1960, 1930, 1990, 2040, 2070, 2010, 1950, 2025]
        logs = [swap_log(1001 + i * 10, 0, pr) for i, pr in enumerate(prices)]
        p = paper.create("t1", pool_state(2000, 1000), CFG, COSTS)
        rpc = ChainRpc(1000, logs)
        for head in (1015, 1015, 1040, 1100):  # 分幾次推進,含一次沒有新區塊
            rpc.head = head
            paper.step(p, rpc)
        p = paper.load("t1")  # 重新從存檔讀回,確認可接續
        ref = simulate([Candle(0, 2000, 2000, 2000, 2000)] + [Candle(i, x, x, x, x) for i, x in enumerate(prices)],
                       GridConfig(p.cfg.lower, p.cfg.upper, 10, 1000.0), COSTS)
        self.assertAlmostEqual(p.equity(), ref.final_equity, places=9)
        self.assertEqual(p.sells, ref.cycles)
        self.assertEqual(p.buys + p.sells, len(ref.trades))
        self.assertEqual(len(paper.read_trades("t1")), len(ref.trades))
        self.assertEqual(p.last_block, 1100)

    def test_resume_after_restart_does_not_double_count(self):
        logs = [swap_log(1005, 0, 1900), swap_log(1010, 0, 2100)]
        p = paper.create("t2", pool_state(2000, 1000), CFG, COSTS)
        rpc = ChainRpc(1020, logs)
        paper.step(p, rpc)
        before = (p.equity(), p.buys, p.sells)
        p2 = paper.load("t2")
        paper.step(p2, rpc)  # 沒有新區塊:不該重複處理
        self.assertEqual((p2.equity(), p2.buys, p2.sells), before)

    def test_long_downtime_only_catches_up_recent_window(self):
        p = paper.create("t3", pool_state(2000, 1000), CFG, COSTS)
        head = 1000 + paper.MAX_CATCHUP_BLOCKS + 500
        rpc = ChainRpc(head, [swap_log(1100, 0, 1500), swap_log(head - 10, 0, 1990)])
        _, n, gap = paper.step(p, rpc)
        self.assertTrue(gap)
        self.assertEqual(n, 1)  # 太舊的那筆被跳過
        self.assertEqual(p.gaps, 1)
        self.assertAlmostEqual(p.last_price, 1990, places=6)

    def test_names_and_errors(self):
        for bad in ("", "../x", "A", "a" * 33, "-x", "a/b"):
            with self.assertRaises(ValueError, msg=bad):
                paper.check_name(bad)
        paper.create("ok", pool_state(2000, 1000), CFG, COSTS)
        with self.assertRaises(ValueError):
            paper.create("ok", pool_state(2000, 1000), CFG, COSTS)
        with self.assertRaises(ValueError):
            paper.load("missing")
        with open(os.path.join(paper.STATE_DIR, "ok.json"), "w") as f:
            json.dump({"version": 1}, f)
        with self.assertRaises(ValueError):
            paper.load("ok")
        self.assertEqual(paper.list_names(), ["ok"])


if __name__ == "__main__":
    unittest.main()
