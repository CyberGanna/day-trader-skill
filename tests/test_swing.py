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


def ibkr_file(path, B):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"time": [b["d"] + "T13:30:00Z" for b in B], "open": [b["o"] for b in B],
                   "high": [b["h"] for b in B], "low": [b["l"] for b in B], "close": [b["c"] for b in B],
                   "volume": [b["v"] for b in B]}, fh)


class Live(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.B = walk(150, seed=3)
        ibkr_file(os.path.join(self.d, "AAA-daily.json"), self.B)
        self.j = os.path.join(self.d, "diario-swing.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_live_proposes_once_and_review_closes_it(self):
        day = self.B[103]["d"]                                    # 2 buy votes against 1 here
        code, out, err = run(["live", "--ibkr", self.d, "--symbols", "AAA", "--date", day, "--journal", self.j,
                              "--level", "0"])
        self.assertEqual(code, 0, err)
        self.assertIn("Shadow only: nothing was ordered.", out)
        self.assertGreater(*sw.live_votes(self.B[:104])[:2])
        j = sw.cf.read_journal(self.j)
        self.assertEqual((len(j), j[0]["status"], j[0]["signal_date"]), (1, "open", day))
        self.assertIn("PROPOSAL buy", out)
        run(["live", "--ibkr", self.d, "--symbols", "AAA", "--date", day, "--journal", self.j, "--level", "0"])
        self.assertEqual(len(sw.cf.read_journal(self.j)), 1)              # one open trade a stock
        code, out, err = run(["review", "--ibkr", self.d, "--journal", self.j, "--config", "none.json"])
        self.assertEqual(code, 0, err)
        self.assertEqual(sw.cf.read_journal(self.j)[0]["status"], "closed")       # 46 more days: it ended
        self.assertIn("1 closed, 0 still open", out)
        code, out, _ = run(["live", "--ibkr", self.d, "--symbols", "AAA", "--date", "2030-01-01", "--journal", self.j])
        self.assertIn("no daily bar for 2030-01-01", out)

    def test_review_keeps_a_young_trade_open(self):
        B = [day("2026-01-%02d" % (k + 1), 100, 101, 99, 100) for k in range(5)]
        j = {"symbol": "AAA", "signal_date": "2026-01-03", "atr": 1.0, "status": "open"}
        out = sw.review_entry(j, B, {})
        self.assertEqual((out["status"], out["entry"], out["stop"], out["days"]), ("open", 100, 98, 2))
        B[4] = day("2026-01-05", 100, 101, 97, 98)
        out = sw.review_entry(j, B, {})
        self.assertEqual((out["status"], out["outcome"], out["R_net"]), ("closed", "stop", -1.0))

    def test_side_filter(self):
        out, _, _ = sw.run_symbol("AAA", self.B, {}, sides=(1,))
        self.assertTrue(all(t["side"] == "buy" for v in out.values() for t in v))


class Universe(unittest.TestCase):
    def test_picks_by_that_years_dollar_volume_and_keeps_delisted(self):
        gm = sw.tm.sys.modules.get("gamma") or __import__("gamma")
        assets = {"active": [{"symbol": "BIG", "exchange": "NASDAQ", "name": "Big Inc", "status": "active"},
                             {"symbol": "SPYX", "exchange": "NYSE", "name": "Some ETF Trust", "status": "active"},
                             {"symbol": "PENNY", "exchange": "NYSE", "name": "Penny Co", "status": "active"}],
                  "inactive": [{"symbol": "GONE", "exchange": "NYSE", "name": "Gone Corp", "status": "inactive"}]}
        def bars(syms, tf, start, end, feed):
            rows = lambda c, v: [{"c": c, "v": v}] * 250  # noqa: E731
            return {"BIG": rows(100, 1e6), "GONE": rows(50, 3e6), "PENNY": rows(2, 1e9), "SPYX": rows(400, 1e9)}
        saved = gm._get_trading, sw.ad._bars
        gm._get_trading = lambda path, params: assets[params["status"]]
        sw.ad._bars = bars
        try:
            doc = sw.universe(2016, 10)
        finally:
            gm._get_trading, sw.ad._bars = saved
        self.assertEqual(doc["hits"], ["GONE", "BIG"])          # funds and penny stocks left out
        self.assertEqual(doc["inactive_now"], ["GONE"])


if __name__ == "__main__":
    unittest.main()
