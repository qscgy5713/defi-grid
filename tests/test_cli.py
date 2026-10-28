import contextlib
import io
import unittest
from unittest import mock

from defigrid import cli


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def test_bad_arguments_fail_before_any_network_call(self):
        with mock.patch.object(cli, "fetch_candles", side_effect=AssertionError("不該連網")):
            for argv in (
                ["backtest", "--grids", "1"],
                ["backtest", "--lower", "100"],  # 只給下界
                ["backtest", "--upper", "100"],
                ["backtest", "--mode", "geometric", "--grids", "501"],
            ):
                code, _, err = run(argv)
                self.assertEqual(code, 2, argv)
                self.assertIn("錯誤", err)

    def test_scan_validates_before_download(self):
        with mock.patch.object(cli, "fetch_candles", side_effect=AssertionError("不該連網")):
            for argv, word in (
                (["scan", "--grids-list", "5,1"], "格數"),
                (["scan", "--train", "1.5"], "--train"),
                (["scan", "--train", "0"], "--train"),
                (["scan", "--ranges", "5,150"], "--ranges"),
                (["scan", "--top", "0"], "--top"),
            ):
                code, _, err = run(argv)
                self.assertEqual(code, 2, argv)
                self.assertIn(word, err)
        for bad in ("5,x", "0,5", "-3", ""):
            with self.assertRaises(SystemExit, msg=bad):
                with contextlib.redirect_stderr(io.StringIO()):
                    cli.main(["scan", "--grids-list", bad])

    def test_walk_validates_before_download(self):
        with mock.patch.object(cli, "fetch_candles", side_effect=AssertionError("不該連網")):
            for argv, word in (
                (["walk", "--days", "30", "--train-days", "30", "--test-days", "7"], "--days"),
                (["walk", "--days", "200"], "--days"),
                (["walk", "--grids-list", "5,1"], "格數"),
                (["walk", "--ranges", "5,150"], "--ranges"),
            ):
                code, _, err = run(argv)
                self.assertEqual(code, 2, argv)
                self.assertIn(word, err)
        for argv in (["walk", "--test-days", "0"], ["walk", "--train-days", "-1"]):
            with self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()):
                    cli.main(argv)

    def test_walk_runs_offline(self):
        from tests.test_scan import series, zigzag

        with mock.patch.object(cli, "fetch_candles", return_value=series(zigzag(100, 8, 24 * 20))):
            code, out, _ = run(["walk", "--days", "20", "--train-days", "5", "--test-days", "3", "--grids-list", "5,10", "--ranges", "10"])
        self.assertEqual(code, 0)
        self.assertIn("滾動式驗證:5 輪", out)
        self.assertIn("不構成投資建議", out)

    def test_lp_and_swaps_validate_before_network(self):
        no_net = AssertionError("不該連網")
        with mock.patch.object(cli, "fetch_candles", side_effect=no_net), mock.patch.object(cli, "read_pool", side_effect=no_net):
            for argv, word in (
                (["lp", "--lower", "100"], "--lower"),
                (["lp", "--range-pct", "150"], "--range-pct"),
                (["lp", "--grids", "1"], "格數"),
                (["swaps", "--hours", "49"], "--hours"),
                (["swaps", "--range-pct", "100"], "--range-pct"),
            ):
                code, _, err = run(argv)
                self.assertEqual(code, 2, argv)
                self.assertIn(word, err)

    def test_paper_validates_before_network(self):
        with mock.patch.object(cli, "read_pool", side_effect=AssertionError("不該連網")), \
                mock.patch.object(cli.paper, "STATE_DIR", "/nonexistent-defigrid-test"):
            for argv, word in (
                (["paper", "run", "--name", "../x"], "名稱"),
                (["paper", "run", "--name", "ok", "--interval", "1"], "--interval"),
                (["paper", "run", "--name", "ok", "--lower", "100"], "--lower"),
                (["paper", "run", "--name", "ok", "--grids", "1"], "格數"),
                (["paper", "status", "--name", "ok"], "找不到"),
            ):
                code, _, err = run(argv)
                self.assertEqual(code, 2, argv)
                self.assertIn(word, err)

    def test_table_aligns_by_display_width(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli._table(["名稱", "值"], [["中文字", "1"], ["ab", "+12.5%"]], left=(0,))
        lines = out.getvalue().splitlines()
        self.assertEqual(len({cli._width(l) for l in lines}), 1)  # 每行顯示寬度相同
        self.assertEqual(cli._width("中a"), 3)

    def test_economics_offline_with_price(self):
        code, out, _ = run(["economics", "--lower", "90", "--upper", "110", "--price", "100", "--grids", "10", "--capital", "5000"])
        self.assertEqual(code, 0)
        self.assertIn("每個完整買賣循環", out)
        self.assertIn("不構成投資建議", out)

    def test_economics_reports_unviable_grid(self):
        code, out, _ = run(["economics", "--lower", "99", "--upper", "101", "--price", "100", "--grids", "100"])
        self.assertEqual(code, 0)
        self.assertIn("是虧的", out)

    def test_invalid_numbers_rejected_by_parser(self):
        for argv in (["economics", "--lower", "-1", "--upper", "2"], ["economics", "--lower", "1", "--upper", "2", "--gas-usd", "-1"]):
            with self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()):
                    cli.main(argv)


if __name__ == "__main__":
    unittest.main()
