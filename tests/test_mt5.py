import io
import json
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

mt5 = load("trader-broker", "mt5")
ticket = mt5.ticket
check = load("trader-risk", "check")

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
ORDERS_PATH = os.path.join(FIX, "trader-risk", "orders-tsla.json")
NOW = "2026-10-19 10:15"


def ns(**kw):
    return types.SimpleNamespace(**kw)


class FakeMT5:
    """The parts of MetaQuotes' MetaTrader5 package the bridge uses, with its real constants."""
    TRADE_ACTION_DEAL, TRADE_ACTION_PENDING = 1, 5
    ORDER_TYPE_BUY, ORDER_TYPE_SELL, ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT = 0, 1, 2, 3
    ORDER_TIME_GTC, ORDER_TIME_DAY = 0, 1
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
    TRADE_RETCODE_PLACED, TRADE_RETCODE_DONE = 10008, 10009

    def __init__(self, trade_mode=0, margin_mode=0, symbols=None, positions=None, send_retcode=10009,
                 check_retcode=0):
        self.account = ns(login=5012345, server="Broker-Demo", company="Broker Ltd", trade_mode=trade_mode,
                          margin_mode=margin_mode, balance=10000.0, currency="USD")
        self.symbols = symbols if symbols is not None else {
            "TSLA.US": ns(name="TSLA.US", description="Tesla Inc", trade_contract_size=1, volume_min=1,
                          volume_max=1000, volume_step=1, digits=2, spread=4, trade_stops_level=10,
                          point=0.01, filling_mode=2)}
        self.positions = positions or []
        self.send_retcode, self.check_retcode = send_retcode, check_retcode
        self.checked, self.sent, self.shutdowns = [], [], 0
        self.quotes = []  # ticks served before the steady quote, e.g. bid 0 / ask 0 right after symbol_select

    def initialize(self):
        return True

    def shutdown(self):
        self.shutdowns += 1

    def last_error(self):
        return (1, "Success")

    def terminal_info(self):
        return ns(build=4755, connected=True, trade_allowed=True)

    def account_info(self):
        return self.account

    def symbol_info(self, name):
        return self.symbols.get(name)

    def symbols_get(self, group):
        key = group.strip("*")
        return [ns(name=n) for n in self.symbols if key in n]

    def symbol_select(self, name, enable):
        return name in self.symbols

    def symbol_info_tick(self, name):
        if self.quotes:
            return self.quotes.pop(0)
        return ns(bid=364.10, ask=364.18)

    def positions_get(self, symbol=None):
        return [p for p in self.positions if symbol is None or p.symbol == symbol]

    def order_check(self, req):
        self.checked.append(dict(req))
        return ns(retcode=self.check_retcode, margin=582.69, comment="Done" if not self.check_retcode else "No money")

    def order_send(self, req):
        self.sent.append(dict(req))
        return ns(retcode=self.send_retcode, order=777001, deal=888001, volume=req["volume"], price=364.20,
                  comment="Request executed" if self.send_retcode == 10009 else "Market closed")


class Bridge(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.orders = os.path.join(self.d, "orders.json")
        shutil.copy(ORDERS_PATH, self.orders)
        self.cfg = os.path.join(self.d, "config.json")
        with open(CFG_PATH) as fh:
            cfg = json.load(fh)
        cfg["broker"] = "mt5"
        with open(self.cfg, "w") as fh:
            json.dump(cfg, fh)
        self.risk = os.path.join(self.d, "state", "risk.json")
        self.state = os.path.join(self.d, "state", "broker.json")
        self.pf = os.path.join(self.d, "portfolio.json")
        self.fake = FakeMT5()
        mt5.mt = self.fake

    def tearDown(self):
        mt5.mt = None
        shutil.rmtree(self.d)

    def files(self):
        return ["--orders", self.orders, "--config", self.cfg, "--risk", self.risk, "--state", self.state,
                "--now", NOW]

    def run_risk(self):
        with redirect_stdout(io.StringIO()):
            check.main(["--orders", self.orders, "--config", self.cfg, "--portfolio", self.pf, "--prices",
                        os.path.join(self.d, "none"), "--state", self.state, "--now", NOW, "--out", self.risk])

    def approve(self, reply="yes"):
        with redirect_stdout(io.StringIO()):
            return ticket.main(["approve", "--index", "1", "--reply", reply] + self.files())

    def send(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = mt5.main(["send", "--index", "1", "--portfolio", self.pf] + self.files() + list(extra))
        return code, out.getvalue(), err.getvalue()

    def state_doc(self):
        with open(self.state) as fh:
            return json.load(fh)

    # -- the gates

    def test_nothing_goes_out_without_the_risk_check(self):
        code, _, err = self.send()
        self.assertEqual(code, 1)
        self.assertIn("not sent: trader-risk has not checked", err)
        self.assertEqual(self.fake.sent, [])

    def test_nothing_goes_out_without_a_recorded_yes(self):
        self.run_risk()
        code, _, err = self.send()
        self.assertEqual(code, 1)
        self.assertIn("no yes is recorded today", err)
        self.assertEqual(self.fake.sent, [])

    def test_a_vague_reply_is_not_a_yes(self):
        self.run_risk()
        self.assertEqual(self.approve("sure"), 1)
        self.assertEqual(self.send()[0], 1)
        self.assertEqual(self.fake.sent, [])

    def test_a_real_account_is_refused(self):
        self.fake.account.trade_mode = 2
        self.run_risk()
        self.approve()
        code, _, err = self.send()
        self.assertEqual(code, 1)
        self.assertIn("REAL account", err)
        self.assertEqual(self.fake.sent, [])

    def test_a_real_account_needs_both_config_switches(self):
        self.fake.account.trade_mode = 2
        with self.assertRaises(mt5.MT5Error):
            mt5.check_account(self.fake, {"mt5_allow_real": True})
        with self.assertRaises(mt5.MT5Error):
            mt5.check_account(self.fake, {"paper": False})
        self.assertEqual(mt5.check_account(self.fake, {"paper": False, "mt5_allow_real": True})[1], "REAL")

    def test_the_brokers_order_check_has_the_last_word(self):
        self.fake.check_retcode = 10019
        self.run_risk()
        self.approve()
        code, _, err = self.send()
        self.assertEqual(code, 1)
        self.assertIn("order_check refused it: No money", err)
        self.assertEqual(self.fake.sent, [])

    def test_the_ticket_names_metatrader(self):
        self.run_risk()
        out = io.StringIO()
        with redirect_stdout(out):
            ticket.main(["show", "--index", "1"] + self.files())
        self.assertIn("ORDER 1 of 1, PAPER account, MetaTrader 5", out.getvalue())
        self.assertIn("mt5.py send --index 1", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            ticket.main(["approve", "--index", "1", "--reply", "yes"] + self.files())
        self.assertIn("Now run: mt5.py send --index 1", out.getvalue())

    # -- sending

    def test_dry_run_checks_and_sends_nothing(self):
        self.run_risk()
        self.approve()
        code, out, _ = self.send("--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("Dry run", out)
        self.assertEqual(len(self.fake.checked), 1)
        self.assertEqual(self.fake.sent, [])
        self.assertEqual(self.state_doc()["orders"][-1]["status"], "approved")

    def test_the_reel_bracket_goes_out_as_sl_and_tp(self):
        self.run_risk()
        self.approve()
        code, out, err = self.send()
        self.assertEqual(code, 0, err)
        req = self.fake.sent[0]
        self.assertEqual(req, {"symbol": "TSLA.US", "volume": 8.0, "deviation": 20, "magic": mt5.MAGIC,
                               "comment": "trader-broker", "type_time": 1, "type_filling": 1, "action": 1,
                               "type": 0, "price": 364.18, "sl": 358.32, "tp": 370.04})
        last = self.state_doc()["orders"]
        self.assertEqual([r["status"] for r in last[-3:]], ["approved", "placed", "filled"])
        self.assertEqual(last[-2]["broker_order_id"], "777001")
        with open(self.pf) as fh:
            pf = json.load(fh)
        self.assertEqual(pf["positions"][0]["shares"], 8.0)
        self.assertIn("Sent to MetaTrader 5", out)
        self.assertGreaterEqual(self.fake.shutdowns, 1)

    def test_one_yes_one_send(self):
        self.run_risk()
        self.approve()
        self.assertEqual(self.send()[0], 0)
        code, _, err = self.send()
        self.assertEqual(code, 1)
        self.assertIn("already sent", err)
        self.assertEqual(len(self.fake.sent), 1)

    def test_a_refused_send_is_recorded_and_not_retried(self):
        self.fake.send_retcode = 10018
        self.run_risk()
        self.approve()
        code, _, err = self.send()
        self.assertEqual(code, 1)
        self.assertIn("Market closed", err)
        self.assertEqual(self.state_doc()["orders"][-1]["status"], "failed")
        self.assertEqual(self.send()[0], 1)


    def test_no_quote_no_order(self):
        self.fake.quotes = [ns(bid=0.0, ask=0.0)] * 50
        self.run_risk()
        self.approve()
        with mock.patch.object(mt5.time, "sleep"):
            code, _, err = self.send("--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("no price", err)
        self.assertEqual(self.fake.checked, [])


class Requests(unittest.TestCase):
    def setUp(self):
        self.m = FakeMT5()
        self.info = self.m.symbols["TSLA.US"]
        self.tick = self.m.symbol_info_tick("TSLA.US")

    def test_symbol_names(self):
        self.assertEqual(mt5.resolve_symbol(self.m, "tsla", {}), "TSLA.US")
        m = FakeMT5(symbols={"#TSLA": self.info, "TSLA": self.info})
        self.assertEqual(mt5.resolve_symbol(m, "TSLA", {}), "TSLA")
        m = FakeMT5(symbols={"TSLA.US": self.info, "TSLA.OQ": self.info})
        with self.assertRaises(mt5.MT5Error):
            mt5.resolve_symbol(m, "TSLA", {})
        self.assertEqual(mt5.resolve_symbol(m, "TSLA", {"mt5_symbols": {"TSLA": "TSLA.OQ"}}), "TSLA.OQ")
        with self.assertRaises(mt5.MT5Error):
            mt5.resolve_symbol(FakeMT5(symbols={"TSLAX.US": self.info}), "TSLA", {})

    def test_a_quote_that_arrives_late_is_waited_for(self):
        self.m.quotes = [ns(bid=0.0, ask=0.0), ns(bid=0.0, ask=0.0)]
        with mock.patch.object(mt5.time, "sleep") as sleep:
            tick = mt5.live_tick(self.m, "TSLA.US")
        self.assertEqual((tick.bid, tick.ask), (364.10, 364.18))
        self.assertEqual(sleep.call_count, 2)
        self.m.quotes = [ns(bid=0.0, ask=0.0)] * 20
        with mock.patch.object(mt5.time, "sleep"):
            self.assertIsNone(mt5.live_tick(self.m, "TSLA.US"))

    def test_lots_must_be_exact(self):
        cfd = ns(trade_contract_size=10, volume_min=0.1, volume_max=100, volume_step=0.1)
        self.assertEqual(mt5.volume_for(cfd, "8"), 0.8)
        with self.assertRaises(mt5.MT5Error):
            mt5.volume_for(ns(trade_contract_size=100, volume_min=0.01, volume_max=100, volume_step=0.01), "8.5")
        with self.assertRaises(mt5.MT5Error):
            mt5.volume_for(ns(trade_contract_size=1, volume_min=10, volume_max=100, volume_step=1), "8")

    def test_notional_and_odd_types_are_refused(self):
        acct = self.m.account
        with self.assertRaises(mt5.MT5Error):
            mt5.build_request(self.m, {"symbol": "TSLA", "side": "buy", "notional": "1000", "type": "market"},
                              "TSLA.US", self.info, self.tick, acct)
        with self.assertRaises(mt5.MT5Error):
            mt5.build_request(self.m, {"symbol": "TSLA", "side": "buy", "qty": "8", "type": "trailing_stop"},
                              "TSLA.US", self.info, self.tick, acct)

    def test_a_limit_order_is_pending(self):
        req = mt5.build_request(self.m, {"side": "buy", "qty": "8", "type": "limit", "limit_price": "360",
                                         "time_in_force": "gtc"}, "TSLA.US", self.info, self.tick, self.m.account)
        self.assertEqual((req["action"], req["type"], req["price"], req["type_time"]), (5, 2, 360.0, 0))

    def test_a_sell_on_a_hedging_account_closes_the_position(self):
        pos = ns(ticket=4242, symbol="TSLA.US", type=0, volume=8.0, price_open=364.2, sl=358.32, tp=370.04,
                 profit=12.0)
        m = FakeMT5(margin_mode=mt5.HEDGING, positions=[pos])
        sell = {"symbol": "TSLA", "side": "sell", "qty": "8", "type": "market", "time_in_force": "day"}
        req = mt5.build_request(m, sell, "TSLA.US", self.info, self.tick, m.account)
        self.assertEqual((req["position"], req["type"], req["price"]), (4242, 1, 364.10))
        with self.assertRaises(mt5.MT5Error):
            mt5.build_request(FakeMT5(margin_mode=mt5.HEDGING), sell, "TSLA.US", self.info, self.tick,
                              m.account)
        with self.assertRaises(mt5.MT5Error):
            mt5.build_request(m, dict(sell, qty="5"), "TSLA.US", self.info, self.tick, m.account)

    def test_a_stop_too_close_is_refused(self):
        req = {"price": 364.18, "sl": 364.10}
        with self.assertRaises(mt5.MT5Error):
            mt5.stops_ok(self.info, req)
        mt5.stops_ok(self.info, {"price": 364.18, "sl": 358.32, "tp": 370.04})

    def test_no_package_is_one_plain_line(self):
        saved = mt5.mt
        mt5.mt = None
        real = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

        def no_mt5(name, *a, **k):
            if name == "MetaTrader5":
                raise ImportError(name)
            return real(name, *a, **k)
        import builtins
        builtins.__import__ = no_mt5
        try:
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertEqual(mt5.main(["check", "--config", "/nonexistent"]), 1)
        finally:
            builtins.__import__ = real
            mt5.mt = saved
        self.assertIn("pip install MetaTrader5", err.getvalue())


if __name__ == "__main__":
    unittest.main()
