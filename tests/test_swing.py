import gzip
import io
import json
import os
import random
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load

sw = load("trader-timemachine", "swing")


def day(d, o, h, l, c):
    return {"d": d, "o": o, "h": h, "l": l, "c": c, "v": 1000}


def walk(n=400, seed=1, p=100.0):
    rng, out = random.Random(seed), []
    for i in range(n):
        o = p
        c = max(1.0, o * (1 + rng.gauss(0, 0.02)))
        out.append(day("2024-%02d-%02d" % (1 + i // 28 % 12, 1 + i % 28) if i < 336 else
                       "2025-%02d-%02d" % (1 + (i - 336) // 28 % 12, 1 + (i - 336) % 28),
                       o, max(o, c) * 1.01, min(o, c) * 0.99, c))
        p = c
    return out


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = sw.main(argv)
    return code, out.getvalue(), err.getvalue()


class Trade(unittest.TestCase):
    def test_next_open_stop_target_and_gaps(self):
        B = [day("d%d" % i, 100, 101, 99, 100) for i in range(5)]
        B[1] = day("d1", 100, 101, 99, 100.5)
        B[2] = day("d2", 101, 105, 100.5, 104.5)                  # reaches 104 = 2R with 2 of risk
        t, end = sw.trade(B, 0, 1, 1.0, {}, target_r=2, stop_atr=2, hold=3)
        self.assertEqual((t["entry"], t["stop"], t["target"], t["outcome"], t["R"], end), (100, 98, 104, "target", 2.0, 2))
        B[2] = day("d2", 97, 99, 96, 97)                          # opens under the stop: fills at the open
        t, _ = sw.trade(B, 0, 1, 1.0, {}, target_r=2, stop_atr=2, hold=3)
        self.assertEqual((t["outcome"], t["R"], t["R_net"], t["gapped"]), ("stop", -1.0, -1.5, True))
        B[2] = day("d2", 100, 104.5, 97.5, 100)                   # both in one day: the stop
        t, _ = sw.trade(B, 0, 1, 1.0, {}, target_r=2, stop_atr=2, hold=3)
        self.assertEqual(t["outcome"], "stop")
        B[2] = day("d2", 100, 101, 99, 100)
        t, end = sw.trade(B, 0, -1, 1.0, {}, target_r=2, stop_atr=2, hold=3)
        self.assertEqual((t["outcome"], t["days"], end), ("close", 3, 3))
        self.assertIsNone(sw.trade(B, 4, 1, 1.0, {}))


class Signals(unittest.TestCase):
    def flat(self, n=30):
        return [day("d%d" % i, 100, 101, 99, 100) for i in range(n)]

    def test_breakout_and_sweep(self):
        B = self.flat()
        B[25] = day("d25", 100, 103, 100, 102.5)
        self.assertEqual(sw.sig_breakout(B, None, 25), 1)
        B[25] = day("d25", 100, 100.5, 97, 100)
        self.assertEqual(sw.sig_sweep(B, None, 25), 1)
        self.assertEqual(sw.sig_breakout(B, None, 25), 0)

    def test_fvg_retest_once(self):
        B = self.flat()
        B[20] = day("d20", 101, 106, 101, 105)
        B[21] = day("d21", 105, 108, 103, 107)                    # low 103 over the high of day 19 (101): gap 101-103
        for k in range(22, 30):
            B[k] = day("d%d" % k, 107, 108, 105, 107)
        B[24] = day("d24", 106, 107, 102, 104)                    # trades into it and closes above
        self.assertEqual(sw.sig_fvg(B, None, 24), 1)
        B[26] = day("d26", 106, 107, 102, 104)
        self.assertEqual(sw.sig_fvg(B, None, 26), 0)               # already used on day 24

    def test_rsi_runs_from_0_to_100(self):
        r = sw.rsi([10, 9, 8, 7, 6, 7, 8])
        self.assertLess(r[4], 10)
        self.assertGreater(r[6], 50)


class Lab(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        for n, sym in enumerate(("AAA", "BBB")):
            with gzip.open(os.path.join(self.d, "%s-daily.json.gz" % sym), "wt", encoding="utf-8") as fh:
                json.dump({"symbol": sym, "bars": walk(seed=n + 1)}, fh)

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_backtest_and_engine(self):
        code, out, err = run(["backtest", "--symbols", "AAA,BBB", "--daily", self.d, "--config", "none.json"])
        self.assertEqual(code, 0, err)
        self.assertIn("Swing lab: 2 stocks", out)
        self.assertIn("random entry", out)
        self.assertIn("pullback in trend", out)
        code, out, err = run(["engine", "--symbols", "AAA,BBB", "--daily", self.d, "--config", "none.json"])
        self.assertEqual(code, 0, err)
        self.assertIn("Swing engine: 2 stocks", out)

    def test_no_overlap_inside_a_variant(self):
        out, _, _ = sw.run_symbol("AAA", walk(), {})
        for trades in out.values():
            for a, b in zip(trades, trades[1:]):
                self.assertLess(a["date"], b["date"])

    def test_errors(self):
        code, _, err = run(["backtest", "--symbols", "ZZZ", "--daily", self.d])
        self.assertEqual(code, 1)
        self.assertIn("no stock had enough history", err)
        env = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith(("TRADER_ALPACA", "ALPACA_", "APCA_"))}
        try:
            code, _, err = run(["download", "--symbols", "AAA", "--out", self.d])
        finally:
            os.environ.update(env)
        self.assertEqual(code, 1)
        self.assertIn("keys", err)

    def test_download_writes_one_file_a_stock(self):
        saved = sw.ad.bars, sw.ad.keys
        sw.ad.keys = lambda: ("k", "s")
        sw.ad.bars = lambda syms, tf, start, end: {"AAA": [{"t": "2026-10-20T04:00:00Z", "o": 1, "h": 2, "l": 0.5,
                                                            "c": 1.5, "v": 9}], "BBB": []}
        try:
            code, out, err = run(["download", "--symbols", "AAA,BBB", "--out", self.d, "--years", "2"])
        finally:
            sw.ad.bars, sw.ad.keys = saved
        self.assertEqual(code, 0, err)
        self.assertEqual(sw.read_daily(self.d, "AAA")[0]["d"], "2026-10-20")
        self.assertIn("BBB    no daily bars", out)


if __name__ == "__main__":
    unittest.main()
