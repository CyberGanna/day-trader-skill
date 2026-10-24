#!/usr/bin/env python3
"""Gamma levels (trader-timemachine), phase 5 of the confluence plan: a live filter.

  python gamma.py TSLA [NVDA ...] [--days 30] [--json]

Where options dealers' hedging may slow the price down or speed it up. For each
option on the stock that expires within --days (30), it reads the open interest
(GET https://paper-api.alpaca.markets/v2/options/contracts, Alpaca's trading API,
read only) and the gamma (GET https://data.alpaca.markets/v1beta1/options/snapshots,
the free "indicative" feed), then sums the gamma exposure by strike:

  gamma x open interest x 100 shares x spot^2 x 1 %   (calls +, puts -)

The sign follows the usual assumption that dealers are long the calls and short
the puts that the public trades; it is an assumption, not a fact about who holds
what. It prints the call wall (the strike with the largest positive exposure),
the put wall (the largest negative), the zero-gamma level (where the running
total from the lowest strike up changes sign) and the total. Above the zero
level dealers tend to damp moves, below it to amplify them.

There is no free history of open interest, so these levels cannot be
backtested: they are a filter the shadow journal judges.

Standard library only. GET only. Nothing here places an order.
"""

import argparse
import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alpaca_data as ad  # noqa: E402

TRADING = "https://paper-api.alpaca.markets"
DAYS = 30
_urlopen = urllib.request.urlopen


def _get_trading(path, params):
    k = ad.keys()
    if not k:
        raise ad.AlpacaError("no Alpaca keys in the environment: set TRADER_ALPACA_KEY and TRADER_ALPACA_SECRET")
    req = urllib.request.Request(TRADING + path + "?" + urllib.parse.urlencode(params),
                                 headers={"APCA-API-KEY-ID": k[0], "APCA-API-SECRET-KEY": k[1],
                                          "Accept": "application/json"})
    try:
        with _urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise ad.AlpacaError("Alpaca's trading API answered HTTP %d" % e.code)
    except (urllib.error.URLError, OSError) as e:
        raise ad.AlpacaError("could not reach Alpaca: %s" % getattr(e, "reason", e))
    except ValueError:
        raise ad.AlpacaError("Alpaca returned something that is not JSON")


def contracts(sym, until):
    out, token = [], None
    while True:
        params = {"underlying_symbols": sym, "status": "active", "expiration_date_lte": until, "limit": 1000}
        if token:
            params["page_token"] = token
        doc = _get_trading("/v2/options/contracts", params)
        out += doc.get("option_contracts") or []
        token = doc.get("next_page_token")
        if not token:
            return out


def option_snapshots(sym, until):
    out, token = {}, None
    while True:
        params = {"feed": "indicative", "limit": 1000, "expiration_date_lte": until}
        if token:
            params["page_token"] = token
        doc = ad._get("/v1beta1/options/snapshots/%s" % urllib.parse.quote(sym), params)
        out.update(doc.get("snapshots") or {})
        token = doc.get("next_page_token")
        if not token:
            return out


def spot(sym):
    snap = ad.snapshots([sym]).get(sym) or {}
    p = (snap.get("latestTrade") or {}).get("p") or (snap.get("dailyBar") or {}).get("c")
    if not p:
        raise ad.AlpacaError("no price for %s" % sym)
    return float(p)


def levels(conts, snaps, price):
    """Gamma exposure by strike, the walls, the zero-gamma level and the total."""
    by = {}
    for c in conts:
        try:
            oi = float(c.get("open_interest") or 0)
            strike = float(c["strike_price"])
        except (KeyError, TypeError, ValueError):
            continue
        gamma = ((snaps.get(c.get("symbol")) or {}).get("greeks") or {}).get("gamma")
        if not oi or gamma is None:
            continue
        sign = 1 if c.get("type") == "call" else -1
        by[strike] = by.get(strike, 0.0) + sign * float(gamma) * oi * 100 * price * price * 0.01
    if not by:
        return None
    strikes = sorted(by)
    run, zero, prev = 0.0, None, None
    for s in strikes:
        before = run
        run += by[s]
        if prev is not None and before < 0 <= run or prev is not None and before > 0 >= run:
            zero = s
        prev = s
    pos = max(strikes, key=lambda s: by[s])
    neg = min(strikes, key=lambda s: by[s])
    return {"spot": price, "call_wall": pos if by[pos] > 0 else None, "put_wall": neg if by[neg] < 0 else None,
            "zero_gamma": zero, "total": round(sum(by.values())), "strikes": len(strikes)}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Gamma levels from Alpaca's options data (trader-timemachine).")
    ap.add_argument("symbols", nargs="+", help="tickers")
    ap.add_argument("--days", type=int, default=DAYS, help="options expiring within this many days (%d)" % DAYS)
    ap.add_argument("--json", action="store_true", help="print the numbers as JSON")
    a = ap.parse_args(argv)
    until = (datetime.date.today() + datetime.timedelta(days=a.days)).isoformat()
    rows = {}
    for sym in [s.upper() for s in a.symbols]:
        try:
            lv = levels(contracts(sym, until), option_snapshots(sym, until), spot(sym))
        except ad.AlpacaError as e:
            print("gamma: %s: %s" % (sym, e), file=sys.stderr)
            continue
        rows[sym] = lv
        if a.json:
            continue
        if not lv:
            print("%-6s no options with open interest and gamma" % sym)
            continue
        fmt = lambda x: "-" if x is None else "%.2f" % x  # noqa: E731
        print("%-6s spot %.2f  call wall %s  put wall %s  zero gamma %s  total %s (%s)" % (
            sym, lv["spot"], fmt(lv["call_wall"]), fmt(lv["put_wall"]), fmt(lv["zero_gamma"]),
            "{:,}".format(lv["total"]), "damps moves" if lv["total"] > 0 else "amplifies moves"))
    if a.json:
        print(json.dumps(rows, indent=2))
    elif rows:
        print("Sign convention: dealers long calls, short puts (an assumption). Not financial advice.")
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
