import datetime
import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

tm = load("trader-timemachine", "timemachine")
DIR = os.path.join(FIX, "trader-timemachine")
AFTER = "2026-10-19 16:30"
RULE = {"setup": "orb", "or_minutes": 15, "stop_rule": "or_mid", "target_r": 1.0}
LAST = 77


def bar(o, h, l, c, v=1000):
    return {"o": o, "h": h, "l": l, "c": c, "v": v}


def session(path, or_volume=1000):
    """78 bars: an opening range of 100 to 102 (mid 101), a breakout close at 103 on bar 3
    (entry 103, stop 101, risk 2), then the bars in `path` as (high, low) pairs, then flat
    at the last close."""
    D = [bar(101, 102, 100, 101, or_volume) for _ in range(3)]
    D.append(bar(102, 103, 102, 103))
    for h, l in path:
        D.append(bar(l, h, l, (h + l) / 2))
    while len(D) < 78:
        c = D[-1]["c"]
        D.append(bar(c, c, c, c))
    return D


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = tm.main(argv)
    return code, out.getvalue(), err.getvalue()


class Exits(unittest.TestCase):
    def one(self, path, **extra):
        h = tm.history_replay([("2026-10-01", session(path))], dict(RULE, **extra), LAST)
        return h["trades"][0]

    def test_the_rule_as_before(self):
        t = self.one([(105.5, 103)])                          # target 105 first
        self.assertEqual((t["entry"], t["stop"], t["target"], t["outcome"], t["R"]), (103, 101, 105, "target", 1.0))
        self.assertEqual(self.one([(103.5, 100.5)])["R"], -1.0)

    def test_stop_to_entry_once_one_r_is_reached(self):
        path = [(105.2, 103.5), (104, 102.8)]                 # 1R (105) touched, then back under the entry
        plain = self.one(path, target_r=2.0)
        be = self.one(path, target_r=2.0, breakeven_r=1.0)
        self.assertEqual((plain["outcome"], plain["R"]), ("close", 0.2))     # closes at 103.4
        self.assertEqual((be["outcome"], be["R"]), ("stop", 0.0))

    def test_the_breakeven_stop_waits_for_the_next_bar(self):
        t = self.one([(105.2, 102.5)], target_r=2.0, breakeven_r=1.0)   # same bar dips under 103: not stopped
        self.assertEqual(t["outcome"], "close")

    def test_half_at_one_r(self):
        win = self.one([(105.5, 103), (107.5, 105)], target_r=2.0, half_r=1.0)
        self.assertEqual((win["outcome"], win["R"]), ("target", 1.5))           # (1 + 2) / 2
        lose = self.one([(105.5, 103), (104, 100.5)], target_r=2.0, half_r=1.0)
        self.assertEqual((lose["outcome"], lose["R"]), ("stop", 0.0))           # (1 - 1) / 2
        both = self.one([(105.5, 103), (104, 102.5)], target_r=2.0, half_r=1.0, breakeven_r=1.0)
        self.assertEqual((both["outcome"], both["R"]), ("stop", 0.5))           # (1 + 0) / 2

    def test_a_gap_through_a_moved_stop_fills_at_the_open(self):
        D = session([(105.2, 103.5)])
        D[5] = bar(102, 102.5, 101.5, 102)                    # opens under the entry stop
        t = tm.history_replay([("2026-10-01", D)], dict(RULE, target_r=2.0, breakeven_r=1.0), LAST)["trades"][0]
        self.assertEqual(t["R"], 0.0)
        self.assertLess(t["R_net"], -0.4)
        self.assertTrue(t["gapped"])


class Filters(unittest.TestCase):
    def days(self, n):
        start = datetime.date(2026, 6, 1)
        return [(start + datetime.timedelta(days=i)).isoformat() for i in range(n)]

    def test_volume_filter_needs_history_and_a_busy_open(self):
        dates = self.days(14)
        sessions = [(d, session([(105.5, 103)], or_volume=3000 if i in (11, 13) else 1000))
                    for i, d in enumerate(dates)]
        allow = tm.session_filter(sessions, dict(RULE, min_rvol=1.5))
        self.assertEqual(sorted(allow), [dates[11], dates[13]])
        h = tm.history_replay(sessions, RULE, LAST, allow=allow)
        self.assertEqual(h["triggered"], 2)

    def test_market_trend_filter_uses_only_earlier_closes(self):
        dates = self.days(6)
        closes = [(d, c) for d, c in zip(dates, (10, 11, 12, 9, 8, 13))]
        sessions = [(d, session([])) for d in dates]
        allow = tm.session_filter(sessions, dict(RULE, spy_trend=3), closes)
        # day 4: closes 10, 11, 12 -> 12 > 11 yes; day 5: 11, 12, 9 -> no; day 6: 12, 9, 8 -> no
        self.assertEqual(sorted(allow), [dates[3]])
        with self.assertRaises(tm.TMError):
            tm.session_filter(sessions, dict(RULE, spy_trend=3), None)

    def test_no_filters_means_every_session(self):
        self.assertIsNone(tm.session_filter([("2026-06-01", session([]))], RULE))


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cfg = os.path.join(self.d, "none.json")
        self.env = {k: os.environ.pop(k) for k in list(os.environ)
                    if k.startswith(("TRADER_ALPACA", "ALPACA_", "APCA_", "TRADER_DATA"))}

    def tearDown(self):
        os.environ.update(self.env)
        shutil.rmtree(self.d)

    def test_compare_lists_every_variant_it_can_run(self):
        code, out, err = run(["backtest", "--symbols", "TSLA", "--offline", DIR, "--now", AFTER,
                              "--config", self.cfg, "--compare"])
        self.assertEqual(code, 0, err)
        self.assertIn("Backtest compare: 1 stocks", out)
        self.assertIn("target 2R, stop to entry at 1R", out)
        self.assertIn("half at 1R, rest 2R, stop to entry", out)
        self.assertNotIn("SPY above", out)             # no SPY file offline: that variant is left out
        self.assertIn("partly luck", out)

    def test_one_run_with_exits_and_halves(self):
        code, out, err = run(["backtest", "--symbols", "TSLA", "--offline", DIR, "--now", AFTER,
                              "--config", self.cfg, "--target-r", "2", "--breakeven-r", "1"])
        self.assertEqual(code, 0, err)
        self.assertIn("Also: stop to the entry once 1R is reached.", out)
        self.assertIn("First half of the trades", out)

    def test_a_long_range_needs_alpaca(self):
        code, _, err = run(["backtest", "--symbols", "TSLA", "--range", "1y", "--config", self.cfg])
        self.assertEqual(code, 1)
        self.assertIn("needs Alpaca's data", err)
        code, _, err = run(["backtest", "--symbols", "TSLA", "--offline", DIR, "--config", self.cfg,
                            "--target-r", "1", "--half-r", "1"])
        self.assertEqual(code, 1)
        self.assertIn("--half-r must be below --target-r", err)

    def test_save_writes_each_chart_and_offline_reads_it_back(self):
        text = tm.read_offline(DIR, "TSLA")
        saved = (tm.fetch_chart, tm._sleep, tm.data_source)
        tm.fetch_chart = lambda sym, rng="60d": text
        tm._sleep = lambda s: None
        tm.data_source = lambda: "alpaca"
        try:
            code, _, err = run(["backtest", "--symbols", "TSLA", "--range", "1y", "--now", AFTER,
                                "--config", self.cfg, "--save", self.d, "--spy-trend", "20"])
        finally:
            tm.fetch_chart, tm._sleep, tm.data_source = saved
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(os.listdir(self.d)), ["SPY-1y5m.json.gz", "TSLA-1y5m.json.gz"])
        self.assertEqual(tm.read_offline(self.d, "SPY"), text)


if __name__ == "__main__":
    unittest.main()
