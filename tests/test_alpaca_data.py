import datetime
import filecmp
import io
import json
import os
import unittest
import urllib.error
import urllib.parse
from unittest import mock

from _load import load, ROOT

AD = load("trader-timemachine", "alpaca_data")
COPIES = ["trader-alerts", "trader-entry", "trader-exit", "trader-scanner", "trader-timemachine"]
NOW = datetime.datetime(2026, 10, 23, 15, 0, tzinfo=datetime.timezone.utc)   # 11:00 New York, a Friday
KEYS = {"ALPACA_API_KEY": "PKTEST", "ALPACA_SECRET_KEY": "secret"}


def bar(t, c, v=1000):
    return {"t": t, "o": c - 0.1, "h": c + 0.2, "l": c - 0.3, "c": c, "v": v, "n": 10, "vw": c}


def five_min(day, start_hh, start_mm, count, first=100.0):
    t0 = datetime.datetime.combine(day, datetime.time(start_hh, start_mm), datetime.timezone.utc)
    return [bar((t0 + datetime.timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"), first + i * 0.1)
            for i in range(count)]


class FakeAlpaca:
    """Serves /v2/stocks/bars and /v2/stocks/snapshots from in-memory tables, records each request."""

    def __init__(self, sip=None, iex=None, snaps=None, page=None, status=None):
        self.sip, self.iex, self.snaps = sip or {}, iex or {}, snaps or {}
        self.page, self.status, self.calls = page, status, []

    def __call__(self, req, timeout=None):
        u = urllib.parse.urlsplit(req.full_url)
        q = dict(urllib.parse.parse_qsl(u.query))
        self.calls.append((u.path, q, dict(req.header_items())))
        if self.status:
            code, body = self.status
            raise urllib.error.HTTPError(req.full_url, code, "no", {}, io.BytesIO(body.encode()))
        if u.path.endswith("/snapshots"):
            doc = {s: self.snaps[s] for s in q["symbols"].split(",") if s in self.snaps}
        else:
            table = self.sip if q["feed"] == "sip" else self.iex
            start, end = AD._parse_t(q["start"]), AD._parse_t(q["end"])
            rows = [(s, b) for s in q["symbols"].split(",") for b in table.get(s, [])
                    if start <= AD._parse_t(b["t"]) < end]
            skip = int(q.get("page_token") or 0)
            size = self.page or len(rows)
            chunk = rows[skip:skip + size]
            doc = {"bars": {}, "next_page_token": str(skip + size) if skip + size < len(rows) else None}
            for s, b in chunk:
                doc["bars"].setdefault(s, []).append(b)
        return io.BytesIO(json.dumps(doc).encode())


class Base(unittest.TestCase):
    def setUp(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ALPACA_", "APCA_", "TRADER_DATA"))}
        env.update(KEYS)
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()
        self.now = mock.patch.object(AD, "_now", lambda: NOW)
        self.now.start()

    def tearDown(self):
        self.now.stop()
        self.env.stop()

    def serve(self, fake):
        p = mock.patch.object(AD, "_urlopen", fake)
        p.start()
        self.addCleanup(p.stop)
        return fake


class Settings(Base):
    def test_source_follows_the_keys_unless_told(self):
        self.assertEqual(AD.source(), "alpaca")
        os.environ["TRADER_DATA"] = "yahoo"
        self.assertEqual(AD.source(), "yahoo")
        del os.environ["TRADER_DATA"], os.environ["ALPACA_API_KEY"]
        self.assertEqual(AD.source(), "yahoo")
        os.environ.update(APCA_API_KEY_ID="k", APCA_API_SECRET_KEY="s")
        self.assertEqual(AD.source(), "alpaca")

    def test_the_skills_own_key_names_come_first(self):
        os.environ.update(TRADER_ALPACA_KEY="mine", TRADER_ALPACA_SECRET="my-secret")
        self.assertEqual(AD.keys(), ("mine", "my-secret"))
        os.environ.pop("TRADER_ALPACA_SECRET")
        self.assertEqual(AD.keys(), ("PKTEST", "secret"))   # half a pair is ignored

    def test_no_keys_is_a_plain_error(self):
        os.environ.pop("ALPACA_API_KEY")
        with self.assertRaises(AD.AlpacaError) as e:
            AD.chart("TSLA", "1d", "5m")
        self.assertIn("TRADER_ALPACA_KEY", str(e.exception))

    def test_keys_go_in_headers_only(self):
        fake = self.serve(FakeAlpaca())
        AD.chart("TSLA", "1d", "5m")
        path, q, headers = fake.calls[0]
        self.assertEqual(headers.get("Apca-api-key-id"), "PKTEST")
        self.assertNotIn("secret", json.dumps(q))

    def test_refused_keys_and_plan_limits(self):
        self.serve(FakeAlpaca(status=(401, '{"message":"unauthorized."}')))
        with self.assertRaisesRegex(AD.AlpacaError, "refused the keys"):
            AD.chart("TSLA", "1d", "5m")
        self.serve(FakeAlpaca(status=(403, '{"message":"subscription does not permit querying recent SIP data"}')))
        with self.assertRaisesRegex(AD.AlpacaError, "ALPACA_FEED"):
            AD.chart("TSLA", "1d", "5m")


class Shape(Base):
    def test_auto_feed_takes_sip_then_iex_for_the_last_16_minutes(self):
        day = NOW.date()
        sip = five_min(day, 13, 30, 18)    # 9:30 to 10:55, sip has everything
        iex = five_min(day, 13, 30, 18, first=200.0)
        fake = self.serve(FakeAlpaca(sip={"TSLA": sip}, iex={"TSLA": iex}))
        doc = AD.chart("TSLA", "1d", "5m")
        feeds = [(q["feed"], q["end"]) for p, q, _ in fake.calls if p.endswith("/bars")]
        self.assertEqual(feeds, [("sip", "2026-10-23T14:44:00Z"), ("iex", "2026-10-23T15:00:00Z")])
        closes = doc["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        self.assertEqual(len(closes), 18)
        self.assertAlmostEqual(closes[0], 100.0)          # 9:30 from sip
        self.assertAlmostEqual(closes[-1], 201.7)         # 10:55 from the live iex tail

    def test_sip_feed_and_iex_feed_are_single_requests(self):
        fake = self.serve(FakeAlpaca())
        os.environ["ALPACA_FEED"] = "sip"
        AD.chart("TSLA", "1d", "5m")
        self.assertEqual([q["feed"] for p, q, _ in fake.calls if p.endswith("/bars")], ["sip"])
        os.environ["ALPACA_FEED"] = "bogus"
        with self.assertRaises(AD.AlpacaError):
            AD.chart("TSLA", "1d", "5m")

    def test_one_day_keeps_the_last_session_and_meta_comes_from_the_snapshot(self):
        sip = five_min(datetime.date(2026, 10, 22), 13, 30, 78, 90.0) + five_min(NOW.date(), 13, 30, 12)
        snap = {"latestTrade": {"p": 101.5, "t": "2026-10-23T14:59:58.123456789Z"},
                "dailyBar": {"o": 100, "h": 102, "l": 99, "c": 101.5, "v": 5000},
                "prevDailyBar": {"c": 97.25}}
        self.serve(FakeAlpaca(sip={"TSLA": sip}, snaps={"TSLA": snap}))
        r = AD.chart("TSLA", "1d", "5m")["chart"]["result"][0]
        self.assertEqual(len(r["timestamp"]), 12)
        m = r["meta"]
        self.assertEqual((m["symbol"], m["dataGranularity"], m["source"]), ("TSLA", "5m", "alpaca"))
        self.assertEqual((m["regularMarketPrice"], m["previousClose"], m["regularMarketDayHigh"]), (101.5, 97.25, 102))
        self.assertEqual(m["regularMarketTime"], int(datetime.datetime(2026, 10, 23, 14, 59, 58,
                                                                       tzinfo=datetime.timezone.utc).timestamp()))

    def test_extended_hours_bars_are_left_out_like_yahoo(self):
        rows = five_min(NOW.date(), 8, 0, 66)   # pre-market, 4:00 to 9:25 New York
        rows += five_min(NOW.date(), 13, 30, 6)
        self.serve(FakeAlpaca(sip={"TSLA": rows}, iex={"TSLA": rows}))
        r = AD.chart("TSLA", "1d", "5m")["chart"]["result"][0]
        first = datetime.datetime.fromtimestamp(r["timestamp"][0], datetime.timezone.utc)
        self.assertEqual((len(r["timestamp"]), first.strftime("%H:%M")), (6, "13:30"))

    def test_daily_bars_are_stamped_at_the_open_like_yahoo(self):
        rows = [bar("2026-10-%02dT04:00:00Z" % d, 100.0 + d) for d in (19, 20, 21, 22)]
        self.serve(FakeAlpaca(sip={"BRK.B": rows}))
        doc = json.loads(AD.answer("/v7/finance/spark?symbols=BRK-B,NOPE&range=3mo&interval=1d"))
        res = doc["spark"]["result"]
        self.assertEqual([x["symbol"] for x in res], ["BRK-B"])
        ts = res[0]["response"][0]["timestamp"][0]
        self.assertEqual(datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%H:%M"), "13:30")

    def test_rate_limits_wait_and_retry(self):
        ok = FakeAlpaca(sip={"TSLA": five_min(NOW.date(), 13, 30, 6)})
        busy = FakeAlpaca(status=(429, "{}"))
        answers = [busy, busy, ok, ok, ok, ok]
        self.serve(lambda req, timeout=None: answers.pop(0)(req, timeout))
        with mock.patch.object(AD, "_sleep") as sleep:
            r = AD.chart("TSLA", "1d", "5m")["chart"]["result"][0]
        self.assertEqual(len(r["timestamp"]), 6)
        self.assertEqual(sleep.call_count, 2)
        self.serve(FakeAlpaca(status=(429, "{}")))
        with mock.patch.object(AD, "_sleep"), self.assertRaisesRegex(AD.AlpacaError, "rate limiting"):
            AD.chart("TSLA", "1d", "5m")

    def test_pages_are_followed(self):
        sip = five_min(datetime.date(2026, 10, 22), 13, 30, 78)
        fake = self.serve(FakeAlpaca(sip={"TSLA": sip}, page=20))
        r = AD.chart("TSLA", "5d", "5m")["chart"]["result"][0]
        self.assertEqual(len(r["timestamp"]), 78)
        self.assertEqual(len([c for c in fake.calls if c[0].endswith("/bars")]), 4 + 1)  # 4 sip pages, 1 iex

    def test_an_unknown_symbol_is_yahoos_no_data_answer(self):
        self.serve(FakeAlpaca())
        doc = AD.chart("ZZZZ", "60d", "5m")
        self.assertIsNone(doc["chart"]["result"])
        self.assertIn("No data found", doc["chart"]["error"]["description"])

    def test_a_path_alpaca_cannot_answer(self):
        with self.assertRaises(AD.AlpacaError):
            AD.answer("/v1/finance/search?q=tsla")
        with self.assertRaises(AD.AlpacaError):
            AD.chart("TSLA", "max", "5m")


class Command(Base):
    def test_check_prints_the_source_and_one_chart(self):
        self.serve(FakeAlpaca(sip={"SPY": five_min(NOW.date(), 13, 30, 12)},
                              snaps={"SPY": {"prevDailyBar": {"c": 99.5}}}))
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            self.assertEqual(AD.main(["spy"]), 0)
        self.assertIn("Source: alpaca", out.getvalue())
        self.assertIn("keys in ALPACA_API_KEY", out.getvalue())
        self.assertIn("SPY: 12 five-minute bars", out.getvalue())
        self.assertIn("previous close 99.5", out.getvalue())
        os.environ.pop("ALPACA_API_KEY")
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(AD.main([]), 1)


class Skills(Base):
    """Each skill reads the adapter's answer with its own Yahoo parser."""

    def setUp(self):
        super().setUp()
        day = datetime.date(2026, 10, 22)
        self.rows = five_min(day, 13, 30, 78, 100.0) + five_min(NOW.date(), 13, 30, 18, 110.0)
        self.serve(FakeAlpaca(sip={"TSLA": self.rows}, iex={"TSLA": self.rows}, snaps={"TSLA": {
            "latestTrade": {"p": 111.8, "t": "2026-10-23T15:00:00Z"}, "dailyBar": {"h": 112, "l": 109.5, "v": 9},
            "prevDailyBar": {"c": 107.7}}}))

    def skill(self, folder, name):
        mod = load(folder, name)
        mod.ALPACA._now = lambda: NOW
        mod.ALPACA._urlopen = AD._urlopen
        return mod

    def test_entry(self):
        entry = self.skill("trader-entry", "entry")
        ch = entry.parse_chart(entry.yahoo_get(entry.chart_path("TSLA", "1d", "5m")), "TSLA")
        self.assertEqual((ch["symbol"], len(ch["bars"]), ch["prev_close"]), ("TSLA", 18, 107.7))

    def test_timemachine(self):
        tm = self.skill("trader-timemachine", "timemachine")
        meta, bars, dropped = tm.parse_bars(tm.chart_result(tm.fetch_chart("TSLA"), "TSLA"), NOW.replace(tzinfo=None))
        self.assertEqual(len(bars), 78 + 18)
        self.assertEqual(dropped["not_complete"], 0)

    def test_alerts(self):
        al = self.skill("trader-alerts", "alerts")
        q = al.parse_yahoo(al.fetch_yahoo("TSLA"), "TSLA")
        self.assertEqual((q["price"], q["prev_close"], q["source"]), (111.8, 107.7, "alpaca"))

    def test_scanner_and_exit(self):
        scan = self.skill("trader-scanner", "scan")
        s = scan.parse_chart(scan.yahoo_get(scan.chart_path("TSLA", "1d", "5m")), "TSLA")
        self.assertEqual(len(s["t"]), 18)
        ex = self.skill("trader-exit", "exit")
        self.assertIn('"TSLA"', ex.yahoo_get("/v8/finance/chart/TSLA?range=1d&interval=5m"))

    def test_yahoo_stays_the_default_without_keys(self):
        for k in KEYS:
            os.environ.pop(k)
        entry = self.skill("trader-entry", "entry")
        self.assertEqual(entry.data_source(), "yahoo")

    def test_an_alpaca_failure_is_the_skills_own_error(self):
        entry = self.skill("trader-entry", "entry")
        entry.ALPACA._urlopen = FakeAlpaca(status=(401, "{}"))
        with self.assertRaises(entry.EntryError):
            entry.yahoo_get(entry.chart_path("TSLA", "1d", "5m"))


class Copies(unittest.TestCase):
    def test_every_skill_ships_the_same_adapter(self):
        first = os.path.join(ROOT, COPIES[0], "alpaca_data.py")
        for folder in COPIES[1:]:
            self.assertTrue(filecmp.cmp(first, os.path.join(ROOT, folder, "alpaca_data.py"), shallow=False), folder)


if __name__ == "__main__":
    unittest.main()
