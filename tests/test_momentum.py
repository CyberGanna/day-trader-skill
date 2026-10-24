import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load

mo = load("trader-timemachine", "momentum")


def series(n, growth, start="2018-01-01", stop=None):
    d0, out, p, k = datetime.date.fromisoformat(start), [], 100.0, 0
    while len(out) < n:
        d = d0 + datetime.timedelta(days=k)
        k += 1
        if d.weekday() >= 5:
            continue
        if stop and d.isoformat() > stop:
            break
        out.append({"d": d.isoformat(), "o": p, "h": p, "l": p, "c": p, "v": 1})
        p *= 1 + growth
    return out


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mo.main(argv)
    return code, out.getvalue(), err.getvalue()


class Lab(unittest.TestCase):
    def test_holds_the_winner_and_beats_random(self):
        n = 700
        daily = {"UP": series(n, 0.002), "FLAT": series(n, 0.0), "DOWN": series(n, -0.001),
                 "GONE": series(n, 0.0005, stop="2018-09-01")}
        doc = mo.lab(daily, series(n, 0.0003), top=1, cost_bps=0, runs=20)
        self.assertEqual(doc["holdings_now"], ["UP"])
        self.assertGreater(doc["lines"]["momentum"]["cagr"], doc["lines"]["equal weight"]["cagr"])
        self.assertEqual(doc["random"]["momentum_beats_share"], 1.0)

    def test_month_ends_and_a_delisted_stock(self):
        cal = ["2020-01-30", "2020-01-31", "2020-02-03", "2020-02-28"]
        self.assertEqual(mo.month_ends(cal), [1, 3])
        px = mo.closes_on(cal, [{"d": "2020-01-30", "c": 5}, {"d": "2020-01-31", "c": 6}])
        self.assertEqual(px, [5, 6, None, None])
        curve = mo.run({"X": [10, 10, 12, None]}, [0, 3], lambda i: ["X"], 0)
        self.assertAlmostEqual(curve[0], 1.2)                 # out at its last close

    def test_cli(self):
        d = tempfile.mkdtemp()
        try:
            for sym, g in (("SPY", 0.0003), ("AAA", 0.001), ("BBB", -0.0005)):
                with gzip.open(os.path.join(d, "%s-daily.json.gz" % sym), "wt", encoding="utf-8") as fh:
                    json.dump({"symbol": sym, "bars": series(600, g)}, fh)
            code, out, err = run(["backtest", "--symbols", "AAA,BBB", "--daily", d, "--top", "1", "--runs", "10"])
            self.assertEqual(code, 0, err)
            self.assertIn("Momentum would hold now: AAA.", out)
            code, _, err = run(["backtest", "--symbols", "ZZZ", "--daily", d])
            self.assertEqual(code, 1)
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
