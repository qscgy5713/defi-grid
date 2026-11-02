import datetime
import json
import os
import tempfile
import unittest
from unittest import mock

from defigrid import paper, sui
from defigrid.sim import Candle, CostModel, GridConfig, simulate

POOL = "0x" + "ab" * 32
OTHER = "0x" + "cd" * 32
USDC = "0xdba34672e30cb065b1f93e3ab55318768fd6fef66c15942c9f7cb846e2f900e7::usdc::USDC"
SUI = "0x0000000000000000000000000000000000000000000000000000000000000002::sui::SUI"
COSTS = CostModel(fee_rate=0.0005, slippage_rate=0.0002, gas_usd=0.01)
CFG = {"grids": 10, "capital": 1000.0, "mode": "arithmetic", "range_pct": 5.0, "lower": None, "upper": None}


def sqrt_for(price):
    """SUI 美元價格 → USDC(coin_a,6 位)/ SUI(coin_b,9 位)池子的 sqrt_price。"""
    sui_per_usdc_raw = (1 / price) * 10 ** (9 - 6)
    return str(int(sui_per_usdc_raw**0.5 * sui.Q64))


def iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class FakeSui:
    """假的 Sui GraphQL:events 依 checkpoint 篩選並分頁(每頁 page 筆)。checkpoint n 的時間 = n 秒。"""

    def __init__(self, head, events, page=2, pool_type=None, coins=("USDC", "SUI")):
        self.head, self.events, self.page, self.queries = head, events, page, 0
        self.pool_type = pool_type or f"{sui.CETUS_PACKAGE}::pool::Pool<{USDC}, {SUI}>"
        self.coins = coins

    def query(self, q, variables=None):
        v = variables or {}
        if "events(" in q:
            self.queries += 1
            f = v["filter"]
            assert f["type"] == sui.SWAP_EVENT
            rows = [e for e in self.events if f["afterCheckpoint"] < e[0] < f["beforeCheckpoint"]]
            start = int(v["after"] or 0)
            chunk = rows[start:start + self.page]
            return {"events": {
                "pageInfo": {"hasNextPage": start + self.page < len(rows), "endCursor": str(start + self.page)},
                "nodes": [{"timestamp": iso(cp), "contents": {"json": {"pool": pool, "after_sqrt_price": sqrt_for(px)}}}
                          for cp, pool, px in chunk]}}
        if "coinMetadata" in q:
            dec = {"USDC": 6, "SUI": 9, "CETUS": 9}
            return {"a": {"symbol": self.coins[0], "decimals": dec[self.coins[0]]},
                    "b": {"symbol": self.coins[1], "decimals": dec[self.coins[1]]}}
        if "object(" in q:
            return {"checkpoint": {"sequenceNumber": str(self.head)},
                    "object": {"asMoveObject": {"contents": {"type": {"repr": self.pool_type},
                                                             "json": {"fee_rate": "500", "current_sqrt_price": sqrt_for(2.0)}}}}}
        # latest_checkpoint 會往回退 LAG_CHECKPOINTS,這裡先加回去,讓測試裡的 head 就是實際處理到的位置
        lag = sui.LAG_CHECKPOINTS
        return {"checkpoint": {"sequenceNumber": str(self.head + lag), "timestamp": iso(self.head + lag // sui.CHECKPOINTS_PER_SEC)}}


class SuiMathTests(unittest.TestCase):
    def test_price_roundtrip_and_invert(self):
        self.assertAlmostEqual(sui.price_from_sqrt(sqrt_for(0.9073), 6, 9, True), 0.9073, places=6)
        self.assertAlmostEqual(sui.price_from_sqrt(sqrt_for(0.5), 6, 9, False), 2.0, places=6)  # 1 USDC = 2 SUI
        with self.assertRaises(ValueError):
            sui.price_from_sqrt("0", 6, 9, True)

    def test_latest_checkpoint_keeps_safety_lag(self):
        c = FakeSui(1000, [])
        self.assertEqual(sui.latest_checkpoint(c), (1000, 1000))
        self.assertEqual(c.query("{ checkpoint { sequenceNumber timestamp } }")["checkpoint"]["sequenceNumber"],
                         str(1000 + sui.LAG_CHECKPOINTS))

    def test_helpers(self):
        self.assertEqual(sui.normalize_coin(SUI), "0x2::sui::SUI")
        self.assertEqual(sui.normalize_coin("0x2::sui::SUI"), "0x2::sui::SUI")
        self.assertEqual(sui.parse_ts("2026-10-29T08:06:25.059Z"), sui.parse_ts("2026-10-29T08:06:25Z"))


class SuiPoolTests(unittest.TestCase):
    def test_read_pool_picks_non_stable_side(self):
        p = sui.read_pool(FakeSui(500, []), POOL)
        self.assertEqual((p.symbol0, p.symbol1, p.invert, p.block), ("SUI", "USDC", True, 500))
        self.assertEqual((p.decimals0, p.decimals1), (6, 9))  # 依池子 coin_a / coin_b 順序
        self.assertAlmostEqual(p.price, 2.0, places=6)
        self.assertAlmostEqual(p.fee_rate, 0.0005)

    def test_read_pool_rejects(self):
        with self.assertRaisesRegex(ValueError, "Cetus"):
            sui.read_pool(FakeSui(1, [], pool_type=f"0x99::pool::Pool<{USDC}, {SUI}>"), POOL)
        with self.assertRaisesRegex(ValueError, "穩定幣"):
            sui.read_pool(FakeSui(1, [], coins=("CETUS", "SUI")), POOL)

    def test_fetch_filters_pool_and_paginates(self):
        evs = [(101, POOL, 2.0), (102, OTHER, 9.0), (103, POOL, 2.1), (104, POOL, 2.2), (106, POOL, 2.3)]
        c = FakeSui(110, evs, page=2)
        ref = sui.read_pool(c, POOL)
        seen = []
        got = sui.fetch_swaps(c, ref, 101, 105, 101, 105, seen.append)  # (101, 105]
        self.assertEqual([round(s.price, 6) for s in got], [2.1, 2.2])
        self.assertEqual([s.ts for s in got], [103, 104])
        self.assertEqual(c.queries, 2)
        self.assertEqual(seen[-1], 1.0)


class SuiPaperTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = mock.patch.object(paper, "STATE_DIR", tmp.name)
        p.start()
        self.addCleanup(p.stop)

    def test_incremental_equals_backtest(self):
        prices = [2.0, 1.96, 1.93, 1.99, 2.04, 2.07, 2.01, 1.95, 2.025]
        evs = [(1001 + i * 10, POOL, x) for i, x in enumerate(prices)] + [(1003, OTHER, 5.0)]
        evs.sort()
        c = FakeSui(1000, evs, page=3)
        p = paper.create("s1", sui.read_pool(c, POOL), CFG, COSTS, "sui")
        p.last_ts = 1000
        for head in (1015, 1015, 1040, 1100):
            c.head = head
            paper.step(p, c)
        p = paper.load("s1")
        self.assertEqual((p.chain, p.invert, p.symbol0), ("sui", True, "SUI"))
        ref = simulate([Candle(0, 2.0, 2.0, 2.0, 2.0)] + [Candle(i, x, x, x, x) for i, x in enumerate(prices)],
                       GridConfig(p.cfg.lower, p.cfg.upper, 10, 1000.0), COSTS)
        self.assertAlmostEqual(p.equity(), ref.final_equity, places=6)
        self.assertEqual(p.sells, ref.cycles)
        self.assertEqual(p.last_block, 1100)
        c.head = 1100
        before = (p.equity(), p.buys, p.sells)
        paper.step(p, c)  # 沒有新 checkpoint:不重複處理
        self.assertEqual((p.equity(), p.buys, p.sells), before)

    def test_long_downtime_only_catches_up_recent_window(self):
        c = FakeSui(1000, [])
        p = paper.create("s2", sui.read_pool(c, POOL), CFG, COSTS, "sui")
        p.last_ts = 1000
        span = sui.MAX_CATCHUP_SECONDS * sui.CHECKPOINTS_PER_SEC
        c.head = 1000 + span + 5000
        c.events = [(1500, POOL, 1.0), (c.head - 10, POOL, 1.99)]
        _, n, gap = paper.step(p, c)
        self.assertTrue(gap)
        self.assertEqual(n, 1)  # 太舊的那筆被跳過
        self.assertAlmostEqual(p.last_price, 1.99, places=6)

    def test_old_save_files_default_to_base(self):
        from tests.test_paper import pool_state
        p = paper.create("b1", pool_state(2000, 1000), CFG, COSTS)
        path = os.path.join(paper.STATE_DIR, "b1.json")
        with open(path) as f:
            d = json.load(f)
        del d["chain"], d["invert"]  # 模擬加入 Sui 之前的存檔
        with open(path, "w") as f:
            json.dump(d, f)
        q = paper.load("b1")
        self.assertEqual((q.chain, q.invert), ("base", False))
        self.assertAlmostEqual(q.equity(), p.equity())


if __name__ == "__main__":
    unittest.main()
