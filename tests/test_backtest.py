import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

tm = load("trader-timemachine", "timemachine")

DIR = os.path.join(FIX, "trader-timemachine")
TSLA = os.path.join(DIR, "TSLA-60d5m.json.gz")
with gzip.open(TSLA, "rt", encoding="utf-8") as _fh:
    TEXT = _fh.read()
NOW = "2026-10-19 10:50"
AFTER = "2026-10-19 16:30"
COSTS = {"slippage_bps": 2.0, "commission_per_share": 0.0}
RULE = {"setup": "orb", "or_minutes": 15, "stop_rule": "or_mid", "target_r": 1.0}
PLAN = {"symbol": "TSLA", "side": "buy", "date": "2026-10-19", "setup": "orb", "entry": 364.18,
        "entry_time": "10:15 AM", "opening_range": {"minutes": 15}, "stop": 358.32, "stop_rule": "or_mid",
        "target": 370.04, "interval": "5m"}


def renamed(sym, scale=1.0):
    """The TSLA fixture as another ticker, prices times scale."""
    doc = json.loads(TEXT)
    res = doc["chart"]["result"][0]
    res["meta"]["symbol"] = sym
    q = res["indicators"]["quote"][0]
    for k in ("open", "high", "low", "close"):
        q[k] = [None if x is None else x * scale for x in q[k]]
    return json.dumps(doc)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = tm.main(argv)
    return code, out.getvalue(), err.getvalue()


def trade(R, R_net=None, outcome="close", gapped=False, date="2026-01-02"):
    return {"date": date, "time": "10:15 AM", "symbol": "X", "outcome": outcome, "R": R,
            "R_net": R if R_net is None else R_net, "gapped": gapped}


class Costs(unittest.TestCase):
    def test_cost_in_r(self):
        # 2 bps a side on 364.18 = 0.1457 a share round trip, over 5.86 risk
        self.assertAlmostEqual(tm.cost_r(364.18, 5.86, COSTS), 2 * 364.18 * 0.0002 / 5.86)
        self.assertAlmostEqual(tm.cost_r(100, 1, {"slippage_bps": 0, "commission_per_share": 0.005}), 0.01)
        self.assertEqual(tm.cost_r(100, 1, None), 0.0)

    def test_a_stop_that_gaps_fills_at_the_open(self):
        self.assertEqual(tm.stop_fill(358.32, 355.0, True), 355.0)
        self.assertEqual(tm.stop_fill(358.32, 360.0, True), 358.32)
        self.assertEqual(tm.stop_fill(358.32, 361.0, False), 361.0)

    def test_the_reel_numbers_stay_and_net_is_added(self):
        doc = tm.time_machine(TEXT, tm.normalize_plan(PLAN), tm.parse_now(NOW), costs=COSTS)
        m, h = doc["mc"], doc["history"]
        self.assertEqual((m["p_target"], m["p_stop"], m["avg_R"]), (0.2938, 0.2548, 0.034))
        self.assertEqual(h["avg_R"], -0.062)
        self.assertEqual(m["avg_R_net"], 0.009)
        self.assertEqual(h["avg_R_net"], -0.108)
        self.assertEqual(m["costs"], COSTS)
        self.assertLess(m["avg_R_net"], m["avg_R"])

    def test_no_costs_net_only_counts_the_gaps(self):
        doc = tm.time_machine(TEXT, tm.normalize_plan(PLAN), tm.parse_now(NOW))
        h = doc["history"]
        for t in h["trades"]:
            if t["gapped"]:
                self.assertLess(t["R_net"], -1.0)
            else:
                self.assertEqual(t["R_net"], t["R"])
        self.assertLessEqual(doc["mc"]["avg_R_net"], doc["mc"]["avg_R"])

    def test_the_report_shows_the_costs(self):
        d = tempfile.mkdtemp()
        try:
            code, out, _ = run(["run", "--plan", os.path.join(d, "none.json"), "--symbol", "TSLA", "--date",
                                "2026-10-19", "--entry", "364.18", "--stop", "358.32", "--target", "370.04",
                                "--entry-time", "10:15 AM", "--offline", TSLA, "--now", NOW,
                                "--config", os.path.join(d, "none.json")])
        finally:
            shutil.rmtree(d)
        self.assertEqual(code, 0)
        self.assertIn("After costs: +0.009 R (slippage 2 bps a side", out)
        self.assertIn("After costs the history averages -0.108 R a trade.", out)


class LoadCosts(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cfg = os.path.join(self.d, "config.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def args(self, **kw):
        a = type("A", (), {"slippage_bps": None, "commission": None, "config": self.cfg})()
        for k, v in kw.items():
            setattr(a, k, v)
        return a

    def test_defaults(self):
        self.assertEqual(tm.load_costs(self.args()), {"slippage_bps": 2.0, "commission_per_share": 0.0})

    def test_config_then_flags(self):
        with open(self.cfg, "w") as fh:
            json.dump({"slippage_bps": 5, "commission_per_share": 0.005}, fh)
        self.assertEqual(tm.load_costs(self.args()), {"slippage_bps": 5.0, "commission_per_share": 0.005})
        self.assertEqual(tm.load_costs(self.args(slippage_bps=1.0))["slippage_bps"], 1.0)

    def test_bad_values(self):
        with self.assertRaises(tm.TMError):
            tm.load_costs(self.args(slippage_bps=-1.0))
        with open(self.cfg, "w") as fh:
            fh.write("{nope")
        with self.assertRaises(tm.TMError):
            tm.load_costs(self.args())


class Backtest(unittest.TestCase):
    def test_tsla_matches_the_time_machine_history(self):
        doc = tm.backtest({"TSLA": TEXT}, RULE, tm.parse_now(AFTER), costs=COSTS)
        st, p = doc["stats"], doc["symbols"][0]
        # the pool's 59 sessions; 10-19 is not complete in the fixture (it was fetched at 10:49)
        self.assertEqual((p["sessions"], p["triggered"], p["target"], p["stop"], p["close"]), (59, 33, 13, 15, 5))
        self.assertEqual((st["trades"], st["avg_R"], st["avg_R_net"]), (33, -0.062, -0.108))
        self.assertEqual(st["ci95_net"], [-0.432, 0.217])
        self.assertTrue(st["verdict"].startswith("No proven edge"))

    def test_many_symbols_pool_their_trades(self):
        charts = {"TSLA": TEXT, "AAAA": renamed("AAAA", 0.5), "BBBB": renamed("BBBB", 2.0)}
        doc = tm.backtest(charts, RULE, tm.parse_now(AFTER), costs=COSTS)
        self.assertEqual([p["symbol"] for p in doc["symbols"]], ["AAAA", "BBBB", "TSLA"])
        self.assertEqual(doc["stats"]["trades"], 99)
        dates = [(t["date"], t["time"]) for t in doc["trades"]]
        self.assertEqual(dates, sorted(dates))

    def test_a_broken_chart_is_skipped_not_fatal(self):
        doc = tm.backtest({"TSLA": TEXT, "ZZZZ": "not json"}, RULE, tm.parse_now(AFTER), costs=COSTS)
        self.assertIn("ZZZZ", doc["failed"])
        self.assertEqual(doc["stats"]["trades"], 33)

    def test_until_must_be_on_the_grid(self):
        with self.assertRaises(tm.TMError):
            tm.backtest({"TSLA": TEXT}, RULE, tm.parse_now(AFTER), until="15:52")


class Verdicts(unittest.TestCase):
    def test_nothing_fired(self):
        self.assertIn("never fired", tm.backtest_stats([])["verdict"])

    def test_too_few(self):
        self.assertTrue(tm.backtest_stats([trade(1.0)] * 10)["verdict"].startswith("Too few trades"))

    def test_an_edge(self):
        st = tm.backtest_stats([trade(1.0), trade(0.8), trade(-1.0, outcome="stop")] * 20)
        self.assertTrue(st["verdict"].startswith("An edge after costs"), st)
        self.assertGreater(st["ci95_net"][0], 0)

    def test_a_loser(self):
        st = tm.backtest_stats([trade(-1.0, outcome="stop"), trade(-0.8), trade(1.0)] * 20)
        self.assertTrue(st["verdict"].startswith("Loses after costs"), st)

    def test_drawdown_profit_factor_and_gaps(self):
        ts = [trade(1.0), trade(-1.0, outcome="stop"), trade(-1.5, outcome="stop", gapped=True), trade(2.0)]
        st = tm.backtest_stats(ts)
        self.assertEqual(st["max_drawdown_R"], 2.5)
        self.assertEqual(st["profit_factor_net"], 1.2)
        self.assertEqual(st["gap_stops"], 1)
        self.assertEqual(st["total_R_net"], 0.5)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cfg = os.path.join(self.d, "config.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_backtest_report_and_write(self):
        out_path = os.path.join(self.d, "bt.json")
        code, out, err = run(["backtest", "--symbols", "TSLA", "--offline", DIR, "--now", AFTER,
                              "--max-loss", "50", "--config", self.cfg, "--write", out_path])
        self.assertEqual(code, 0, err)
        self.assertIn("TSLA          59     33      13    15        5  -0.062        -0.108", out)
        self.assertIn("At $50 risked a trade: about -$5.40 a trade on average", out)
        self.assertIn("No proven edge", out)
        self.assertIn("not financial advice", out)
        with open(out_path) as fh:
            self.assertEqual(json.load(fh)["stats"]["trades"], 33)

    def test_symbols_from_the_watchlist(self):
        wl = os.path.join(self.d, "watchlist.json")
        with open(wl, "w") as fh:
            json.dump({"hits": [{"symbol": "TSLA"}, {"symbol": "tsla"}, {"symbol": "NOPE"}]}, fh)
        code, out, _ = run(["backtest", "--watchlist", wl, "--offline", DIR, "--now", AFTER, "--config", self.cfg])
        self.assertEqual(code, 0)
        self.assertIn("Skipped NOPE: no NOPE-60d5m.json(.gz)", out)

    def test_errors_are_one_plain_line(self):
        cases = [
            ["backtest", "--watchlist", os.path.join(self.d, "none.json")],
            ["backtest", "--symbols", "TSLA", "--stop-rule", "pct"],
            ["backtest", "--symbols", "TSLA", "--or-minutes", "7"],
            ["backtest", "--symbols", "TSLA", "--target-r", "0"],
        ]
        for argv in cases:
            code, out, err = run(argv + ["--offline", DIR, "--config", self.cfg])
            self.assertEqual(code, 1, argv)
            self.assertTrue(err.startswith("timemachine.py: "), (argv, err))
            self.assertEqual(err.count("\n"), 1, argv)

    def test_the_network_path_is_polite(self):
        calls, sleeps = [], []
        saved = (tm.fetch_chart, tm._sleep)
        tm.fetch_chart = lambda sym, rng="60d": calls.append(sym) or renamed(sym)
        tm._sleep = sleeps.append
        try:
            code, out, _ = run(["backtest", "--symbols", "AAAA,BBBB", "--now", AFTER, "--config", self.cfg])
        finally:
            tm.fetch_chart, tm._sleep = saved
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["AAAA", "BBBB"])
        self.assertEqual(sleeps, [tm.PAUSE])


class SkillFile(unittest.TestCase):
    def test_description_fits_the_upload_limit(self):
        # claude.ai refuses a skill whose description is over 1024 characters
        import re
        from _load import ROOT
        for d in sorted(os.listdir(ROOT)):
            path = os.path.join(ROOT, d, "SKILL.md")
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            desc = re.search(r"description: >-\n(.*?)\n[a-z-]+:", text, re.S).group(1)
            self.assertLessEqual(len(" ".join(desc.split())), 1024, d)


if __name__ == "__main__":
    unittest.main()
