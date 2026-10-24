import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

of = load("trader-timemachine", "orderflow")
DIR = os.path.join(FIX, "trader-timemachine")
AFTER = "2026-10-19 16:30"


def bar(o, h, l, c):
    return {"o": o, "h": h, "l": l, "c": c, "v": 1000}


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = of.main(argv)
    return code, out.getvalue(), err.getvalue()


class Aggregate(unittest.TestCase):
    def test_the_tick_rule_and_the_footprint(self):
        o = 1000
        st = of.aggregate([(o + 1, 10.00, 5), (o + 2, 10.01, 100), (o + 3, 10.01, 50), (o + 4, 10.00, 30),
                           (o + 301, 10.02, 7)], o)
        b0, b1 = st["bars"][0], st["bars"][1]
        self.assertEqual((b0["buy"], b0["sell"]), (150, 30))          # the first trade has no side yet
        self.assertEqual(b0["fp"], {1001: [150, 0], 1000: [0, 30]})
        self.assertEqual((b1["buy"], b1["sell"]), (7, 0))
        st = of.aggregate([(o + 302, 10.02, 4)], o, st)                # the next page keeps the last side
        self.assertEqual(st["bars"][1]["buy"], 11)

    def test_fetch_day_pages_and_skips_holidays(self):
        day = datetime.date(2026, 10, 20)
        t0 = datetime.datetime(2026, 10, 20, 13, 30, 1, tzinfo=datetime.timezone.utc)
        iso = lambda s: (t0 + datetime.timedelta(seconds=s)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")  # noqa: E731
        pages = [{"trades": {"TSLA": [{"t": iso(0), "p": 400.0, "s": 10}, {"t": iso(1), "p": 400.5, "s": 20}]},
                  "next_page_token": "x"},
                 {"trades": {"TSLA": [{"t": iso(2), "p": 400.4, "s": 5}]}, "next_page_token": None}]
        seen = []
        saved = of.ad._get
        of.ad._get = lambda path, params: seen.append(params.get("page_token")) or pages[len(seen) - 1]
        try:
            doc = of.fetch_day("TSLA", day)
            self.assertEqual(seen, [None, "x"])
            self.assertEqual((doc["trades"], doc["bars"]["0"]["buy"], doc["bars"]["0"]["sell"]), (3, 20, 5))
            of.ad._get = lambda path, params: {"trades": {}, "next_page_token": None}
            self.assertIsNone(of.fetch_day("TSLA", day))
        finally:
            of.ad._get = saved


class Signals(unittest.TestCase):
    def flat(self, p=100.0):
        return [bar(p, p + 0.1, p - 0.1, p) for _ in range(78)]

    def test_cvd_divergence_at_a_new_high(self):
        D = self.flat()
        D[4] = bar(100, 101, 100, 101)
        D[8] = bar(101, 101.5, 100.5, 101)                              # a new high
        F = {4: {"buy": 500, "sell": 0}, 8: {"buy": 0, "sell": 300}}  # but the delta falls
        self.assertEqual(next(iter(of.sig_cvd_div(D, F))), (8, -1))

    def test_delta_push_needs_the_breakout_and_the_delta(self):
        D = self.flat()
        D[20] = bar(100, 101, 100, 100.9)
        F = {k: {"buy": 100, "sell": 50} for k in range(78)}
        F[20] = {"buy": 400, "sell": 50}
        self.assertEqual(next(iter(of.sig_delta_push(D, F))), (20, 1))
        F[20] = {"buy": 100, "sell": 50}
        self.assertEqual(list(of.sig_delta_push(D, F)), [])

    def test_absorption_at_the_low(self):
        D = self.flat()
        D[5] = bar(100, 101, 99, 100.8)
        F = {5: {"buy": 10, "sell": 300, "fp": [[9900, 10, 300], [10080, 50, 0]]}}
        self.assertEqual(list(of.sig_absorption(D, F)), [(5, 1)])


class Lab(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_lab_on_the_fixture_with_made_up_flow(self):
        text = of.tm.read_offline(DIR, "TSLA")
        sessions = of.cf.sessions_of(text, "TSLA", of.tm.parse_now(AFTER))
        for d, D in sessions[-20:]:
            bars = {str(k): {"buy": 100 + k, "sell": 100, "fp": [[int(D[k]["l"] * 100), 50, 60],
                                                                [int(D[k]["h"] * 100), 60, 50]]} for k in range(78)}
            with gzip.open(os.path.join(self.d, "TSLA-flow-%s.json.gz" % d), "wt", encoding="utf-8") as fh:
                json.dump({"symbol": "TSLA", "date": d, "trades": 1, "bars": bars}, fh)
        code, out, err = run(["backtest", "--symbols", "TSLA", "--flow", self.d, "--offline", DIR, "--now", AFTER,
                              "--config", "none.json"])
        self.assertEqual(code, 0, err)
        self.assertIn("Order flow lab (phase 3): 1 stocks, 20 days with flow", out)
        self.assertIn("random entry", out)

    def test_download_needs_keys_and_backtest_needs_flow(self):
        env = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith(("TRADER_ALPACA", "ALPACA_", "APCA_"))}
        try:
            code, _, err = run(["download", "--symbols", "TSLA", "--out", self.d])
            self.assertEqual(code, 1)
            self.assertIn("keys", err)
        finally:
            os.environ.update(env)
        code, _, err = run(["backtest", "--symbols", "TSLA", "--flow", self.d, "--offline", DIR, "--now", AFTER])
        self.assertEqual(code, 1)
        self.assertIn("no flow files", err)


if __name__ == "__main__":
    unittest.main()
