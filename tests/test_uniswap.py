import unittest

from defigrid import uniswap as u

# 2026-10-28 從 Base 主網 WETH/USDC 0.05% 池(區塊 0x32b97c8)讀到的真實 slot0 回傳值
REAL_SLOT0 = (
    "0x000000000000000000000000000000000000000000032d623132da0782a3ba2b"
    "fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffcf7d7"
    "00000000000000000000000000000000000000000000000000000000000068c0"
    "0000000000000000000000000000000000000000000000000000000000001388"
    "0000000000000000000000000000000000000000000000000000000000001388"
    "0000000000000000000000000000000000000000000000000000000000000044"
    "0000000000000000000000000000000000000000000000000000000000000001"
)


def make_state(sqrt_price_x96, liquidity=10**18, dec0=18, dec1=6):
    return u.PoolState("0x" + "0" * 40, 1, "0x" + "1" * 40, "0x" + "2" * 40, "WETH", "USDC", dec0, dec1, 500, sqrt_price_x96, 0, liquidity)


class UniswapTests(unittest.TestCase):
    def test_parse_real_slot0(self):
        sqrt_price, tick = u.parse_slot0(REAL_SLOT0)
        self.assertEqual(tick, -198697)  # 負的 tick 要正確還原符號
        # 用 sqrtPriceX96 算出的價格,應與 1.0001^tick 一致(誤差 < 0.01%)
        price = u.price_token1_per_token0(make_state(sqrt_price))
        self.assertAlmostEqual(price / (1.0001**tick * 10**12), 1.0, delta=1e-4)
        self.assertTrue(1000 < price < 10000)  # ETH 的合理範圍

    def test_price_roundtrip(self):
        for target in (0.5, 1.0, 2350.0, 100000.0):
            sqrt = int((target * 10 ** (6 - 18)) ** 0.5 * u.Q96)
            self.assertAlmostEqual(u.price_token1_per_token0(make_state(sqrt)) / target, 1.0, delta=1e-9)

    def test_price_impact(self):
        sqrt = int((2350 * 10 ** (6 - 18)) ** 0.5 * u.Q96)
        s = make_state(sqrt, liquidity=9 * 10**17)
        self.assertEqual(u.price_impact(0, s), 0.0)
        small, big = u.price_impact(1_000, s), u.price_impact(100_000, s)
        self.assertGreater(small, 0)
        self.assertGreater(big, small * 50)  # 金額越大衝擊越大
        self.assertEqual(u.price_impact(1_000, make_state(sqrt, liquidity=0)), 1.0)  # 沒流動性 = 100%

    def test_decode_string(self):
        abi = "0x" + "20".rjust(64, "0") + "4".rjust(64, "0") + b"USDC".hex().ljust(64, "0")
        self.assertEqual(u.decode_string(abi), "USDC")
        self.assertEqual(u.decode_string("0x" + b"MKR".hex().ljust(64, "0")), "MKR")  # bytes32 版本

    def test_address_validation(self):
        self.assertEqual(u.check_address(u.POOL_WETH_USDC_500), u.POOL_WETH_USDC_500)
        for bad in ("", "0x123", "0x" + "g" * 40, None, u.POOL_WETH_USDC_500 + "00"):
            with self.assertRaises(ValueError):
                u.check_address(bad)

    def test_bad_abi_length(self):
        with self.assertRaises(ValueError):
            u.words("0x1234")
        with self.assertRaises(ValueError):
            u.parse_slot0("0x" + "0" * 64)  # 欄位不足


if __name__ == "__main__":
    unittest.main()
