import unittest

from defigrid.rpc import RpcError
from defigrid.swaps import BLOCK_TIME, as_candles, as_ticks, decode_swap, fetch_swaps
from defigrid.uniswap import PoolState, Q96, l1_fee_upper_bound


def word(v):
    return format(v % 2**256, "064x")


def swap_log(block, index, price, amount1=-5):
    # WETH(18 位小數)/ USDC(6 位小數):原始價格 = 人類價格 × 10^(6-18)
    sqrt_p = int((price * 10**-12) ** 0.5 * Q96)
    data = "0x" + word(10**18) + word(amount1) + word(sqrt_p) + word(10**15) + word(-200000)
    return {"blockNumber": hex(block), "logIndex": hex(index), "data": data}


STATE = PoolState("0x" + "11" * 20, 0, "0x0", "0x1", "WETH", "USDC", 18, 6, 500, 0, 0, 0)


class FakeRpc:
    def __init__(self, latest, logs, max_span=None):
        self.latest, self.logs, self.max_span, self.calls = latest, logs, max_span, []

    def call(self, method, params):
        if method == "eth_getBlockByNumber":
            return {"number": hex(self.latest), "timestamp": hex(1_000_000)}
        frm, to = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
        self.calls.append((frm, to))
        if self.max_span and to - frm + 1 > self.max_span:
            raise RpcError("範圍太大")
        return [l for l in self.logs if frm <= int(l["blockNumber"], 16) <= to]

    def eth_call(self, to, data):
        self.last_call = (to, data)
        return "0x" + word(12345)


class SwapTests(unittest.TestCase):
    def test_decode(self):
        s = decode_swap(swap_log(990, 3, 2500.0, amount1=-7), 1000, 1_000_000, 18, 6)
        self.assertAlmostEqual(s.price, 2500.0, places=6)
        self.assertEqual((s.block, s.log_index, s.amount1), (990, 3, -7))
        self.assertEqual(s.ts, 1_000_000 - 10 * BLOCK_TIME)

    def test_fetch_sorts_and_shrinks_chunk(self):
        latest = 100_000
        logs = [swap_log(latest - 10, 2, 2001), swap_log(latest - 10, 1, 2000), swap_log(latest - 1500, 0, 1999)]
        rpc = FakeRpc(latest, logs, max_span=1500)
        swaps = fetch_swaps(rpc, STATE, 1)
        self.assertEqual([round(s.price) for s in swaps], [1999, 2000, 2001])
        covered = sorted((f, t) for f, t in rpc.calls if t - f + 1 <= 1500)
        self.assertEqual(covered[0][0], latest - 3600 // BLOCK_TIME + 1)  # 剛好一小時的區塊
        self.assertEqual(covered[-1][1], latest)
        for (a, b), (c, d) in zip(covered, covered[1:]):
            self.assertEqual(b + 1, c)  # 不重疊、不漏

    def test_fetch_validates_hours(self):
        for h in (0, 49):
            with self.assertRaises(ValueError):
                fetch_swaps(FakeRpc(10_000, []), STATE, h)

    def test_aggregate(self):
        # 區塊 8000 剛好在整點(ts = 360000),前四筆在同一小時,區塊 9900(ts = 363800)在下一小時
        swaps = [decode_swap(swap_log(b, 0, p), 10_000, 364_000, 18, 6)
                 for b, p in ((8000, 100), (8050, 104), (8100, 98), (8150, 101), (9900, 110))]
        candles = as_candles(swaps)
        self.assertEqual(len(candles), 2)
        c = candles[0]
        self.assertEqual([round(v) for v in (c.open, c.high, c.low, c.close)], [100, 104, 98, 101])
        self.assertEqual(c.ts % 3600, 0)
        self.assertEqual(len(as_ticks(swaps)), 5)

    def test_l1_fee_call_encoding(self):
        rpc = FakeRpc(0, [])
        self.assertEqual(l1_fee_upper_bound(rpc, 400), 12345)
        self.assertEqual(rpc.last_call[1], "0xf1c7a58b" + word(400))
        with self.assertRaises(ValueError):
            l1_fee_upper_bound(rpc, 0)


if __name__ == "__main__":
    unittest.main()
