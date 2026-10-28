import tempfile
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

from defigrid import data
from defigrid.data import DataError, fetch_candles, parse_ohlcv, suspicious


class DataTests(unittest.TestCase):
    def test_parse_sorts_and_dedupes(self):
        rows = [[200, 2, 3, 1, 2, 5], [100, 1, 2, 1, 1.5, 4], [200, 2, 3, 1, 2.5, 9]]
        c = parse_ohlcv(rows)
        self.assertEqual([x.ts for x in c], [100, 200])
        self.assertEqual(c[1].close, 2.5)  # 重複時以後出現的為準

    def test_wicks_are_repaired(self):
        c = parse_ohlcv([[1, 10, 9, 11, 12, 0]])[0]  # 高點 9 低於開盤、低點 11 高於開盤
        self.assertEqual((c.high, c.low), (12, 10))

    def test_rejects_bad_rows(self):
        for bad in ([[1, 0, 1, 1, 1, 0]], [[1, -1, 1, 1, 1, 0]], [[1, "x", 1, 1, 1, 0]], [[1, 1]], [[1, float("nan"), 1, 1, 1, 0]]):
            with self.assertRaises(DataError, msg=str(bad)):
                parse_ohlcv(bad)

    def test_suspicious(self):
        c = parse_ohlcv([[1, 100, 101, 99, 100, 0], [2, 100, 130, 100, 100, 0]])
        self.assertEqual(len(suspicious(c)), 1)

    def test_fetch_validates_arguments(self):
        good = "0xd0b53D9277642d899DF5C87A3966A349A798F224"
        for pool, token, days in (("../etc", good, 5), (good, "0x1", 5), (good, good, 0), (good, good, 121)):
            with self.assertRaises(ValueError):
                fetch_candles(pool, token, days)


POOL = "0xd0b53D9277642d899DF5C87A3966A349A798F224"


def fake_api(now):
    """模擬 API:回傳 before_timestamp 之前、最多 limit 根的每小時 K 線(由新到舊),最新一根尚未收完。"""
    latest = int(now) - int(now) % 3600

    def get(url):
        q = parse_qs(urlparse(url).query)
        before = int(q["before_timestamp"][0]) if "before_timestamp" in q else latest + 3600
        rows = [[t, 100, 101, 99, 100, 1] for t in range(before - 3600, before - 3600 * (int(q["limit"][0]) + 1), -3600)]
        return {"data": {"attributes": {"ohlcv_list": rows}}}

    return get


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for target, value in (("CACHE_DIR", self.tmp.name), ("PAGE_DELAY", 0)):
            p = mock.patch.object(data, target, value)
            p.start()
            self.addCleanup(p.stop)

    def test_full_days_despite_dropping_open_candle(self):
        with mock.patch.object(data, "_get", side_effect=fake_api(time.time())):
            c = fetch_candles(POOL, POOL, 2)
        self.assertEqual(len(c), 48)
        self.assertLessEqual(c[-1].ts + 3600, time.time())  # 不含還在形成中的 K 線

    def test_short_fetch_does_not_shrink_cache(self):
        with mock.patch.object(data, "_get", side_effect=fake_api(time.time())):
            fetch_candles(POOL, POOL, 5)
            fetch_candles(POOL, POOL, 1, use_cache=False)
        with mock.patch.object(data, "_get", side_effect=AssertionError("應該讀快取")):
            self.assertEqual(len(fetch_candles(POOL, POOL, 5)), 120)


if __name__ == "__main__":
    unittest.main()
