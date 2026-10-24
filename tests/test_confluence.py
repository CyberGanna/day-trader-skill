import io
import json
import os
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

cf = load("trader-timemachine", "confluence")
DIR = os.path.join(FIX, "trader-timemachine")
AFTER = "2026-10-19 16:30"
COSTS = {"slippage_bps": 0.0, "commission_per_share": 0.0}


def bar(o, h, l, c, v=1000):
    return {"o": o, "h": h, "l": l, "c": c, "v": v}


def flat(p=100.0):
    return [bar(p, p + 0.1, p - 0.1, p) for _ in range(78)]


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cf.main(argv)
    return code, out.getvalue(), err.getvalue()


class Timeframes(unittest.TestCase):
    def test_hour_and_four_hour_candles_start_at_the_open(self):
        D = [bar(i, i + 1, i - 1, i + 0.5) for i in range(78)]
        h1, h4 = cf.candles(D, 12), cf.candles(D, 48)
        self.assertEqual(len(h1), 7)                      # 6 full hours and the last half hour
        self.assertEqual(len(h4), 2)
        self.assertEqual((h1[0]["o"], h1[0]["h"], h1[0]["l"], h1[0]["c"]), (0, 12, -1, 11.5))
        self.assertEqual((h4[1]["o"], h4[1]["c"]), (48, 77.5))

    def test_structure_reads_the_last_two_swings(self):
        def zigzag(points):
            return [{"h": p + 1, "l": p - 1, "o": p, "c": p} for p in points]
        up = zigzag([10, 12, 14, 12, 10, 13, 16, 14, 12, 15, 18, 16, 14])
        self.assertEqual(cf.structure(up), 1)
        self.assertEqual(cf.structure(zigzag([-p for p in [10, 12, 14, 12, 10, 13, 16, 14, 12, 15, 18, 16, 14]])), -1)
        self.assertEqual(cf.structure(zigzag([10, 11, 12])), 0)

    def test_daily_trend_needs_55_days(self):
        self.assertEqual(cf.daily_trend([100 + i for i in range(60)]), 1)
        self.assertEqual(cf.daily_trend([100 - i for i in range(60)]), -1)
        self.assertEqual(cf.daily_trend([100 + i for i in range(54)]), 0)


class Trades(unittest.TestCase):
    def day(self, path):
        """Entry bar 3 closes at 100; then the bars in path as (open, high, low, close)."""
        D = flat()
        D[3] = bar(100, 100, 100, 100)
        for i, b in enumerate(path):
            D[4 + i] = bar(*b)
        for i in range(4 + len(path), 78):
            c = D[3 + len(path)]["c"]
            D[i] = bar(c, c, c, c)
        return D

    def test_target_stop_gap_and_close(self):
        t = cf.trade(self.day([(100, 102.5, 99.5, 102)]), 3, 1, 1.0, 2.0, COSTS)
        self.assertEqual((t["outcome"], t["R"], t["target"], t["stop"]), ("target", 2.0, 102, 99))
        t = cf.trade(self.day([(100, 100.5, 98.5, 99)]), 3, 1, 1.0, 2.0, COSTS)
        self.assertEqual((t["outcome"], t["R"]), ("stop", -1.0))
        t = cf.trade(self.day([(97, 97.5, 96.5, 97)]), 3, 1, 1.0, 2.0, COSTS)
        self.assertEqual((t["outcome"], t["R"], t["R_net"], t["gapped"]), ("stop", -1.0, -3.0, True))
        t = cf.trade(self.day([(100, 100.5, 99.5, 100.5)]), 3, 1, 1.0, 2.0, COSTS)
        self.assertEqual((t["outcome"], t["R"]), ("close", 0.5))

    def test_a_short_is_the_mirror(self):
        t = cf.trade(self.day([(100, 100.5, 97.5, 98)]), 3, -1, 1.0, 2.0, COSTS)
        self.assertEqual((t["side"], t["outcome"], t["R"], t["stop"], t["target"]), ("sell", "target", 2.0, 101, 98))

    def test_costs_come_off(self):
        t = cf.trade(self.day([(100, 102.5, 99.5, 102)]), 3, 1, 1.0, 2.0, {"slippage_bps": 10, "commission_per_share": 0})
        self.assertAlmostEqual(t["R_net"], 2.0 - 0.2)    # 2 x 10 bps of 100 = 0.20 a share, risk 1


class Signals(unittest.TestCase):
    def test_sweep_of_yesterdays_low(self):
        prev = flat(100)                                 # yesterday: low 99.9, high 100.1
        D = flat(100)
        D[5] = bar(100, 100, 99.5, 100)                  # trades through 99.9, closes back above
        self.assertEqual(list(cf.sig_sweep(None, 0, D, prev)), [(5, 1)])
        D[5] = bar(100, 100.5, 99.95, 100)               # through the high, back under it
        self.assertEqual(list(cf.sig_sweep(None, 0, D, prev)), [(5, -1)])

    def test_fair_value_gap_entry_on_the_way_back(self):
        D = flat(100)
        D[4] = bar(100, 101, 99.9, 101)
        D[5] = bar(101, 102, 100.5, 102)                 # low 100.5 > bar 3 high 100.1: a gap 100.1 to 100.5
        D[6] = bar(102, 102.2, 101.5, 102)
        D[7] = bar(102, 102, 100.3, 101)                 # back into the gap, closes above 100.1
        for i in range(8, 78):
            D[i] = bar(101, 101.1, 100.9, 101)
        self.assertEqual(next(iter(cf.sig_fvg(None, 0, D, None))), (7, 1))

    def test_the_context_reads_only_earlier_sessions(self):
        sessions = [("2026-01-%02d" % (i + 1), flat(100 + i)) for i in range(25)]
        _, days = cf.run_symbol("X", sessions, COSTS, warmup=20)
        changed = sessions[:-1] + [(sessions[-1][0], flat(10))]
        _, days2 = cf.run_symbol("X", changed, COSTS, warmup=20)
        self.assertEqual(days, days2)                    # the last day's bars never move a context


class Profile(unittest.TestCase):
    def test_poc_and_value_area(self):
        D = [bar(100, 100.1, 99.9, 100, 10000) for _ in range(50)]    # most volume at 100
        D += [bar(100, 100.5, 99.5, 100, 2000) for _ in range(20)]
        D += [bar(104, 105, 103, 104, 100) for _ in range(8)]          # a thin trip up to 105
        p = cf.profile(D)
        self.assertAlmostEqual(p["poc"], 100, delta=0.2)
        self.assertLess(p["vah"], 101)
        self.assertGreater(p["val"], 99)

    def test_back_into_value_area_goes_toward_the_poc(self):
        prev = [bar(100, 100.5, 99.5, 100, 10000) for _ in range(78)]
        D = flat(103)                                                  # opens above value
        D[4] = bar(102, 102, 100, 100.2)                               # first close back inside
        self.assertEqual(list(cf.sig_va_return(None, 0, D, prev)), [(4, -1)])
        self.assertEqual(list(cf.sig_va_return(None, 0, flat(100), prev)), [])   # opened inside

    def test_ict_needs_the_sweep_before_the_gap(self):
        prev = flat(100)                                               # yesterday's low 99.9
        D = flat(100)
        D[2] = bar(100, 100, 99.5, 99.8)                               # sweeps 99.9
        D[3] = bar(99.8, 100.2, 99.7, 100.1)
        D[4] = bar(100.1, 101, 100.3, 101)                             # low 100.3 > bar 2 high 100: a gap
        D[5] = bar(101, 101.2, 100.2, 100.6)                           # back into it, closes above 100
        for i in range(6, 78):
            D[i] = bar(100.6, 100.7, 100.5, 100.6)
        self.assertEqual(next(iter(cf.sig_ict(None, 0, D, prev))), (5, 1))
        D[2] = bar(100, 100.05, 99.95, 100)                            # no sweep: no trade
        D[3] = bar(99.95, 100.2, 99.95, 100.1)
        self.assertEqual(list(cf.sig_ict(None, 0, D, prev)), [])


class Cli(unittest.TestCase):
    def setUp(self):
        self.env = {k: os.environ.pop(k) for k in list(os.environ)
                    if k.startswith(("TRADER_ALPACA", "ALPACA_", "APCA_", "TRADER_DATA"))}

    def tearDown(self):
        os.environ.update(self.env)

    def test_lab_on_the_fixture(self):
        argv = ["backtest", "--symbols", "TSLA", "--offline", DIR, "--now", AFTER, "--warmup", "20",
                "--config", "none.json"]
        code, out, err = run(argv)
        self.assertEqual(code, 0, err)
        self.assertIn("Confluence lab: 1 stocks", out)
        self.assertIn("With the D/H4/H1 context", out)
        self.assertIn("Beat random in both years:", out)
        a = json.loads(run(argv + ["--json"])[1])
        b = json.loads(run(argv + ["--json"])[1])
        self.assertEqual(a, b)                           # the random entries are seeded
        self.assertEqual(len(a["rows"]), 20)

    def test_context_and_the_data_it_needs(self):
        code, out, err = run(["context", "--symbols", "TSLA", "--offline", DIR, "--now", AFTER])
        self.assertEqual(code, 0, err)
        self.assertIn("TSLA   after 2026-10-16", out)
        code, _, err = run(["backtest", "--symbols", "TSLA"])
        self.assertEqual(code, 1)
        self.assertIn("--offline", err)
        code, _, err = run(["backtest", "--symbols", "TSLA", "--offline", DIR, "--signals", "fvg,nope"])
        self.assertEqual(code, 1)
        self.assertIn("unknown signal nope", err)


if __name__ == "__main__":
    unittest.main()


class Engine(unittest.TestCase):
    def test_a_vote_lasts_six_bars(self):
        ev = {"fvg": [(5, 1), (20, -1)], "ma": []}
        self.assertEqual(cf.votes_at(ev, 5), {"fvg": 1, "ma": 0})
        self.assertEqual(cf.votes_at(ev, 10), {"fvg": 1, "ma": 0})
        self.assertEqual(cf.votes_at(ev, 11), {"fvg": 0, "ma": 0})
        self.assertEqual(cf.votes_at(ev, 21), {"fvg": -1, "ma": 0})

    def cand(self, k, R, **x):
        feats = {f: 0 for f in cf.FEATURES}
        feats.update(x)
        return {"k": k, "x": feats, "trade": {"R_net": R, "date": "2026-01-01", "time": "10:00"}}

    def test_weights_first_trades_and_the_gate(self):
        good = [{"cands": [self.cand(5, 1.0, fvg=1), self.cand(6, -1.0, fvg=-1)]} for _ in range(40)]
        w = cf.learn_weights(good)
        self.assertEqual(w["fvg"], 1.0)                 # agrees: +1 against an average of 0
        self.assertEqual(w["ma"], 0.0)                  # never agreed 30 times: no weight
        day = {"cands": [self.cand(5, -1.0, fvg=1), self.cand(9, 2.0, fvg=1, ma=1)]}
        self.assertEqual(cf.first_trades([day], lambda x: sum(x.values()), 2), [day["cands"][1]["trade"]])
        self.assertFalse(cf.gate({"trades": 50, "ci95_net": [0.1, 0.3], "halves_R_net": [0.2, 0.2]}))
        self.assertTrue(cf.gate({"trades": 150, "ci95_net": [0.01, 0.3], "halves_R_net": [0.1, 0.2]}))
        self.assertFalse(cf.gate({"trades": 150, "ci95_net": [0.01, 0.3], "halves_R_net": [-0.1, 0.4]}))

    def test_engine_on_the_fixture(self):
        saved = cf.MIN_TRAIN
        cf.MIN_TRAIN = 5
        try:
            code, out, err = run(["engine", "--symbols", "TSLA", "--offline", DIR, "--now", AFTER,
                                  "--warmup", "20", "--config", "none.json"])
        finally:
            cf.MIN_TRAIN = saved
        self.assertEqual(code, 0, err)
        self.assertIn("Confluence engine (phase 4)", out)
        self.assertIn("count of agreeing votes", out)
        self.assertIn("Weights learned on year 1", out)


def load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def ibkr_doc(rows):
    """Bars -> the IBKR connector's JSON shape (times in UTC)."""
    return {"time": [r[0] for r in rows], "open": [r[1]["o"] for r in rows], "high": [r[1]["h"] for r in rows],
            "low": [r[1]["l"] for r in rows], "close": [r[1]["c"] for r in rows], "volume": [r[1]["v"] for r in rows]}


class Live(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.d = tempfile.mkdtemp()
        text = cf.tm.read_offline(DIR, "TSLA")
        self.sessions = cf.sessions_of(text, "TSLA", cf.tm.parse_now(AFTER))
        self.today = self.sessions[-1][0]

        def utc(date, minutes):
            local = cf.datetime.datetime.fromisoformat(date) + cf.datetime.timedelta(minutes=9 * 60 + 30 + minutes)
            return cf.tm.eastern_to_utc(local).strftime("%Y-%m-%dT%H:%M:%SZ")
        daily = [(utc(d, 0), {"o": D[0]["o"], "h": max(b["h"] for b in D), "l": min(b["l"] for b in D),
                              "c": D[-1]["c"], "v": 1}) for d, D in self.sessions]
        hourly = []
        for d, D in self.sessions:
            for start, end in ((0, 6), (6, 18), (18, 30), (30, 42), (42, 54), (54, 66), (66, 78)):
                g = D[start:end]
                hourly.append((utc(d, 5 * start), {"o": g[0]["o"], "h": max(b["h"] for b in g),
                                                   "l": min(b["l"] for b in g), "c": g[-1]["c"], "v": 1}))
        five = [(utc(d, 5 * i), b) for d, D in self.sessions[-2:] for i, b in enumerate(D)]
        five_live = five[:78 + 24]                                    # today until 11:30
        for name, rows in (("daily", daily), ("1h", hourly), ("5m-live", five_live), ("5m", five)):
            with open(os.path.join(self.d, "TSLA-%s.json" % name), "w") as fh:
                json.dump(ibkr_doc(rows), fh)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d)

    def test_context_from_ibkr_matches_the_lab(self):
        rows = cf.ibkr_rows(os.path.join(self.d, "TSLA-daily.json"))
        self.assertEqual(rows[-1]["date"], self.today)
        c = cf.live_context(rows, cf.ibkr_rows(os.path.join(self.d, "TSLA-1h.json")), self.today)
        lab = cf.context(self.sessions[:-1])
        self.assertEqual(c["D"], lab["D"])                            # IBKR's hours (9:30, 10:00, ...) set H1 and H4
        self.assertIn(c["bias"], (-1, 0, 1))

    def test_live_then_review(self):
        ctxf, jf = os.path.join(self.d, "ctx.json"), os.path.join(self.d, "proposte.json")
        code, out, err = run(["live-context", "--ibkr", self.d, "--symbols", "TSLA", "--date", self.today, "--out", ctxf])
        self.assertEqual(code, 0, err)
        code, out, err = run(["live", "--ibkr", self.d, "--context", ctxf, "--symbols", "TSLA", "--level", "1",
                              "--journal", jf])
        self.assertEqual(code, 0, err)
        self.assertIn("TSLA   at 11:30", out)
        self.assertIn("Shadow only: nothing was ordered", out)
        journal = load_json(jf)
        self.assertEqual(len(journal), 1)
        p = journal[0]
        self.assertLessEqual(p["bar"], 23)                            # never a bar after the data
        run(["live", "--ibkr", self.d, "--context", ctxf, "--symbols", "TSLA", "--level", "1", "--journal", jf])
        self.assertEqual(len(load_json(jf)), 1)                 # one proposal a stock a day
        code, out, err = run(["live-review", "--ibkr", self.d, "--journal", jf, "--date", self.today,
                              "--config", "none.json"])
        self.assertEqual(code, 0, err)
        self.assertIn("Shadow journal: 1 proposals", out)
        self.assertIn("result", load_json(jf)[0])

    def test_live_day_never_reads_ahead(self):
        rows = cf.ibkr_rows(os.path.join(self.d, "TSLA-5m-live.json"))
        prev, D, last = cf.live_day(rows, self.today)
        self.assertEqual(last, 23)
        self.assertEqual(D[30]["h"], D[23]["c"])                      # padding is flat at the last close
