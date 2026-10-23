#!/usr/bin/env python3
"""MetaTrader 5 bridge for trader-broker. Demo accounts by default.

Sends ONE order that already passed all of trader-broker's gates to a running
MetaTrader 5 terminal, through MetaQuotes' official Python package
(`pip install MetaTrader5`, Windows only, the terminal open and logged in).

    python mt5.py check                     # the terminal, the account (DEMO?), the gates
    python mt5.py symbol TSLA               # the broker's name for TSLA and its trading rules
    python mt5.py send --index 1 --dry-run  # order_check only: what the broker would say
    python mt5.py send --index 1            # order_send, then records the result
    python mt5.py positions                 # open positions with their stop and target

An order goes out only when all of these hold:
  1. trader-risk passed this exact order today (fingerprint, trader/state/risk.json);
  2. the user's explicit yes for it is recorded today (ticket.py approve), and
     nothing has been sent for it since: one yes, one send;
  3. the account is a DEMO account, unless trader/config.json has both
     "paper": false and "mt5_allow_real": true (the user's own decision);
  4. the broker's own order_check accepts it.
Claude Code's permission prompt for the command itself is the last check:
never put "mt5.py send" in an allow list.

The bracket's stop-loss and take-profit ride on the position as MT5 SL and TP.
A sell that closes a buy on a hedging account names the position, so it closes
it instead of opening a short. This file never asks for, reads or stores a
password: it attaches to the terminal the user has already logged in to.
MetaTrader 4 has no Python API and is not supported.
"""

import argparse
import importlib.util
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MAGIC = 190190          # marks this pack's orders in the terminal
DEVIATION = 20          # points of slippage allowed on a market order

ACCOUNT_DEMO, ACCOUNT_CONTEST, ACCOUNT_REAL = 0, 1, 2
HEDGING = 2             # ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
FILL_FOK_FLAG, FILL_IOC_FLAG = 1, 2   # SYMBOL_FILLING_* bits in symbol_info.filling_mode


def _load_ticket():
    spec = importlib.util.spec_from_file_location("trader_ticket", os.path.join(HERE, "ticket.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ticket = _load_ticket()
mt = None   # the MetaTrader5 module, imported on first use (tests put a fake here)


class MT5Error(Exception):
    pass


def terminal():
    global mt
    if mt is None:
        try:
            import MetaTrader5 as m
        except ImportError:
            raise MT5Error("the MetaTrader5 package is missing: on the Windows PC with MetaTrader 5, "
                           "run  pip install MetaTrader5")
        mt = m
    if not mt.initialize():
        raise MT5Error("could not attach to MetaTrader 5 (%s): open the terminal, log in to the demo "
                       "account and allow algo trading" % (mt.last_error(),))
    return mt


def account_kind(info):
    return {ACCOUNT_DEMO: "DEMO", ACCOUNT_CONTEST: "CONTEST"}.get(info.trade_mode, "REAL")


def check_account(m, cfg):
    info = m.account_info()
    if info is None:
        raise MT5Error("the terminal is not logged in to an account")
    kind = account_kind(info)
    if kind == "REAL" and not (cfg.get("paper") is False and cfg.get("mt5_allow_real") is True):
        raise MT5Error("account %s on %s is a REAL account. This bridge trades demo accounts only, unless "
                       "trader/config.json has \"paper\": false and \"mt5_allow_real\": true, set by you"
                       % (info.login, info.server))
    return info, kind


# ---------------------------------------------------------------- symbols

def resolve_symbol(m, symbol, cfg):
    """The broker's name for a ticker: the config's mt5_symbols map, the ticker
    itself, then the one broker symbol that is the ticker plus a prefix/suffix
    (TSLA.US, #TSLA, TSLA.OQ). More than one candidate is a question for the user."""
    symbol = symbol.upper()
    mapped = (cfg.get("mt5_symbols") or {}).get(symbol)
    if mapped:
        if m.symbol_info(mapped) is None:
            raise MT5Error("mt5_symbols maps %s to %s, but the broker has no %s" % (symbol, mapped, mapped))
        return mapped
    if m.symbol_info(symbol) is not None:
        return symbol
    found = [s.name for s in (m.symbols_get("*%s*" % symbol) or [])]
    close = [n for n in found if n.upper().strip("#._-").split(".")[0].split("_")[0] == symbol]
    if len(close) == 1:
        return close[0]
    if not close:
        raise MT5Error("the broker lists no symbol for %s. Add it to mt5_symbols in trader/config.json, "
                       "e.g. {\"%s\": \"%s.US\"}" % (symbol, symbol, symbol))
    raise MT5Error("several broker symbols match %s: %s. Pick one in mt5_symbols in trader/config.json"
                   % (symbol, ", ".join(sorted(close))))


def volume_for(info, qty):
    """Shares -> MT5 lots. Refuses anything that is not an exact, allowed lot size."""
    size = float(getattr(info, "trade_contract_size", 1) or 1)
    vol = float(qty) / size
    step = float(info.volume_step or 0.01)
    if abs(round(vol / step) * step - vol) > 1e-9:
        raise MT5Error("%s shares is %g lots at %g shares a lot, not a multiple of the broker's %g lot step"
                       % (qty, vol, size, step))
    if vol < float(info.volume_min) - 1e-9 or vol > float(info.volume_max) + 1e-9:
        raise MT5Error("%g lots is outside the broker's %g to %g" % (vol, info.volume_min, info.volume_max))
    return round(vol, 8)


def filling_for(m, info):
    modes = int(getattr(info, "filling_mode", 0) or 0)
    if modes & FILL_FOK_FLAG:
        return m.ORDER_FILLING_FOK
    if modes & FILL_IOC_FLAG:
        return m.ORDER_FILLING_IOC
    return m.ORDER_FILLING_RETURN


# ---------------------------------------------------------------- the request

def build_request(m, order, broker_symbol, info, tick, account):
    side = str(order.get("side", "")).lower()
    if side not in ("buy", "sell"):
        raise MT5Error("side must be buy or sell, got %r" % order.get("side"))
    if order.get("qty") in (None, ""):
        raise MT5Error("MT5 needs a share quantity: this order is in dollars (notional)")
    typ = str(order.get("type", "market")).lower()
    req = {"symbol": broker_symbol, "volume": volume_for(info, order["qty"]), "deviation": DEVIATION,
           "magic": MAGIC, "comment": "trader-broker",
           "type_time": m.ORDER_TIME_DAY if str(order.get("time_in_force", "day")).lower() == "day"
           else m.ORDER_TIME_GTC,
           "type_filling": filling_for(m, info)}
    if typ == "market":
        req.update(action=m.TRADE_ACTION_DEAL, type=m.ORDER_TYPE_BUY if side == "buy" else m.ORDER_TYPE_SELL,
                   price=tick.ask if side == "buy" else tick.bid)
    elif typ == "limit":
        if order.get("limit_price") is None:
            raise MT5Error("a limit order needs limit_price")
        req.update(action=m.TRADE_ACTION_PENDING,
                   type=m.ORDER_TYPE_BUY_LIMIT if side == "buy" else m.ORDER_TYPE_SELL_LIMIT,
                   price=float(order["limit_price"]))
        req["type_filling"] = m.ORDER_FILLING_RETURN
    else:
        raise MT5Error("the MT5 bridge sends market and limit orders only, not %r" % typ)
    if order.get("stop_loss_stop_price") is not None:
        req["sl"] = float(order["stop_loss_stop_price"])
    if order.get("take_profit_limit_price") is not None:
        req["tp"] = float(order["take_profit_limit_price"])
    if side == "sell" and account.margin_mode == HEDGING and req["action"] == m.TRADE_ACTION_DEAL:
        longs = [p for p in (m.positions_get(symbol=broker_symbol) or []) if p.type == m.POSITION_TYPE_BUY]
        match = [p for p in longs if abs(float(p.volume) - req["volume"]) < 1e-9]
        if not longs:
            raise MT5Error("no open buy position in %s to sell: this bridge never opens a short" % broker_symbol)
        if not match:
            raise MT5Error("the open %s positions are %s lots, none is %g: close them one at a time"
                           % (broker_symbol, ", ".join("%g" % p.volume for p in longs), req["volume"]))
        req["position"] = match[0].ticket
        req.pop("sl", None)
        req.pop("tp", None)
    return req


def stops_ok(info, req):
    """The broker's minimum distance between the price and the SL / TP, in points."""
    level = float(getattr(info, "trade_stops_level", 0) or 0) * float(info.point)
    for k in ("sl", "tp"):
        if k in req and level and abs(req["price"] - req[k]) < level - 1e-9:
            raise MT5Error("the %s at %s is closer to the price %s than the broker allows (%s)"
                           % ({"sl": "stop-loss", "tp": "take-profit"}[k], req[k], req["price"], level))


# ---------------------------------------------------------------- commands

def live_tick(m, name, tries=10, wait=0.3):
    """The terminal's quote, waiting briefly after symbol_select: the first tick of a symbol
    just added to Market Watch reads bid 0 / ask 0 until a price arrives. None if none comes."""
    for i in range(tries):
        tick = m.symbol_info_tick(name)
        if tick is not None and tick.bid > 0 and tick.ask > 0:
            return tick
        if i < tries - 1:
            time.sleep(wait)
    return None


def _cfg(a):
    return ticket.read_json(a.config, "config", required=False) or {}


def cmd_check(a):
    cfg = _cfg(a)
    m = terminal()
    try:
        ti = m.terminal_info()
        info, kind = check_account(m, cfg)
        print("MetaTrader 5 %s, connected: %s, algo trading allowed: %s" % (
            getattr(ti, "build", "?"), bool(getattr(ti, "connected", False)),
            bool(getattr(ti, "trade_allowed", False))))
        print("Account %s on %s (%s): %s, balance %.2f %s, %s" % (
            info.login, info.server, getattr(info, "company", ""), kind, info.balance, info.currency,
            "hedging" if info.margin_mode == HEDGING else "netting"))
        if not getattr(ti, "trade_allowed", True):
            print("Turn on Algo Trading in the terminal's toolbar, or every send will be refused.")
    finally:
        m.shutdown()
    return 0


def cmd_symbol(a):
    cfg = _cfg(a)
    m = terminal()
    try:
        name = resolve_symbol(m, a.ticker, cfg)
        m.symbol_select(name, True)
        info, tick = m.symbol_info(name), live_tick(m, name)
        print("%s -> %s (%s)" % (a.ticker.upper(), name, getattr(info, "description", "")))
        print("  1 lot = %g shares, lots from %g to %g in steps of %g" % (
            getattr(info, "trade_contract_size", 1), info.volume_min, info.volume_max, info.volume_step))
        print("  digits %d, spread %d points, minimum stop distance %d points" % (
            info.digits, getattr(info, "spread", 0), getattr(info, "trade_stops_level", 0)))
        if tick:
            print("  bid %s, ask %s" % (tick.bid, tick.ask))
        else:
            print("  no price right now: its market is closed, or the broker sends no quotes for it")
    finally:
        m.shutdown()
    return 0


def cmd_positions(a):
    m = terminal()
    try:
        rows = m.positions_get() or []
        if not rows:
            print("No open positions.")
        for p in rows:
            print("#%s %s %s %g lots at %s, SL %s, TP %s, P/L %.2f" % (
                p.ticket, "BUY" if p.type == m.POSITION_TYPE_BUY else "SELL", p.symbol, p.volume,
                p.price_open, p.sl or "-", p.tp or "-", p.profit))
    finally:
        m.shutdown()
    return 0


def cmd_send(a):
    orders, cfg, risk, state, today, now = ticket._load(a)
    order = ticket._pick(orders, a.index)
    fp = ticket.fingerprint(order)
    rv, why = ticket.risk_verdict(risk, fp, today)
    if rv is None:
        raise MT5Error("not sent: " + why)
    if rv.get("verdict") != "PASS":
        raise MT5Error("not sent: trader-risk BLOCKED this order: " + "; ".join(rv.get("reasons", [])))
    rec = ticket.latest(state, fp, today)
    if not rec or rec.get("status") != "approved":
        raise MT5Error("not sent: no yes is recorded today for order %d, or it was already sent. "
                       "Show the ticket, ask, then ticket.py approve --index %d --reply \"...\""
                       % (a.index, a.index))
    m = terminal()
    try:
        account, kind = check_account(m, cfg)
        name = resolve_symbol(m, order.get("symbol", ""), cfg)
        if not m.symbol_select(name, True):
            raise MT5Error("the terminal could not show %s in Market Watch" % name)
        info, tick = m.symbol_info(name), live_tick(m, name)
        if info is None or tick is None:
            raise MT5Error("no price for %s right now: is its market open?" % name)
        req = build_request(m, order, name, info, tick, account)
        stops_ok(info, req)
        chk = m.order_check(req)
        if chk is None or chk.retcode != 0:
            raise MT5Error("the broker's order_check refused it: %s" % (
                getattr(chk, "comment", None) or m.last_error()))
        shown = dict(req)
        print("%s account %s: %s" % (kind, account.login, json.dumps(shown, sort_keys=True)))
        if a.dry_run:
            print("Dry run: order_check accepted it (margin %.2f). Nothing was sent." % chk.margin)
            return 0
        res = m.order_send(req)
        ok = res is not None and res.retcode in (m.TRADE_RETCODE_DONE, m.TRADE_RETCODE_PLACED)
    finally:
        m.shutdown()
    argv = ["result", "--index", str(a.index), "--orders", a.orders, "--config", a.config,
            "--risk", a.risk, "--state", a.state, "--portfolio", a.portfolio]
    if a.now:
        argv += ["--now", a.now]
    if ok:
        argv += ["--status", "placed", "--broker-id", str(res.order or res.deal)]
        if res.retcode == m.TRADE_RETCODE_DONE and res.volume:
            shares = float(res.volume) * float(getattr(info, "trade_contract_size", 1) or 1)
            argv += ["--filled-qty", ticket._str_num(shares), "--filled-price", repr(float(res.price))]
    else:
        argv += ["--status", "failed", "--note", "MT5 %s: %s" % (
            getattr(res, "retcode", "?"), getattr(res, "comment", "") or "no answer")]
    ticket.main(argv)
    if not ok:
        raise MT5Error("the broker refused it (%s). Nothing is retried without a new risk check and a new yes"
                       % getattr(res, "comment", "no answer"))
    print("Sent to MetaTrader 5: order %s, deal %s." % (res.order, res.deal))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="MetaTrader 5 bridge for trader-broker (demo accounts by default).")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("check", help="the terminal and the account")
    p.add_argument("--config", default="trader/config.json")
    p = sub.add_parser("symbol", help="the broker's symbol for a ticker and its rules")
    p.add_argument("ticker")
    p.add_argument("--config", default="trader/config.json")
    p = sub.add_parser("positions", help="open positions")
    p = sub.add_parser("send", help="send one approved order")
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--dry-run", action="store_true", help="order_check only, nothing is sent")
    p.add_argument("--orders", default="trader/orders.json")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--risk", default="trader/state/risk.json")
    p.add_argument("--state", default="trader/state/broker.json")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--now")
    p.add_argument("--today")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"check": cmd_check, "symbol": cmd_symbol, "positions": cmd_positions, "send": cmd_send}[a.cmd](a)
    except (MT5Error, ticket.TicketError) as e:
        print("mt5.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
