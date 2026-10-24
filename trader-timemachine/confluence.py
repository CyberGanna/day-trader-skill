#!/usr/bin/env python3
"""Confluence lab (trader-timemachine), phase 1 of the confluence plan.

  python confluence.py backtest --symbols AAPL,MSFT,... --offline storico
  python confluence.py context  --symbols AAPL,MSFT,... [--offline storico]

The context: before the open, the daily trend (close against the 50-day average,
the 20-day average rising or falling), the 4-hour and the 1-hour structure
(the last two confirmed swing highs and lows, both higher or both lower) each
vote +1, -1 or 0. Two votes or more one way set the day's bias: buys only,
sells only, or none. Only sessions before the day are read.

Five 5-minute entry signals, each looked at on its own:
  ma        the 9-bar EMA crosses the 21-bar EMA
  ichimoku  the close crosses out of the cloud (9/26/52), tenkan on the same side of kijun
  fvg       price comes back into a fair value gap opened earlier the same day
  ob        price comes back into an order block (the last opposite bar before a
            displacement bar: a body twice the 20-bar average, closing past the 3 bars before)
  sweep     a bar trades through yesterday's low (high) and closes back inside

Every signal gets the same exits, so they can be compared: the entry at the
signal bar's close (9:45 to 15:05 New York), the stop 1 ATR(14) away, the
target at 2R, else out at the 15:55 close. A stop that gaps fills at the open;
slippage and commission come off (timemachine's costs). One trade a signal a
stock a day: the first that fires (in the bias's direction, with the context).
"random" enters at a random bar and side with the same exits: a signal earns its
place only if it beats random in both halves of the history (year 1, year 2).
Short sales are simulated for the test only; nothing here places an order.

Standard library only.
"""

import argparse
import datetime
import json
import math
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import timemachine as tm  # noqa: E402

FIRST, LAST_ENTRY, EXIT = 3, 66, 76   # bars: 9:45 close is the first entry, 15:05 the last, out at 15:55
WARMUP = 55                           # sessions before the first tested day: the 50-day average needs them
TARGET_R, STOP_ATR = 2.0, 1.0
SIGNALS = ("ma", "ichimoku", "fvg", "ob", "sweep")
NAMES = {"ma": "moving averages 9/21", "ichimoku": "Ichimoku cloud", "fvg": "fair value gap",
         "ob": "order block", "sweep": "liquidity sweep", "random": "random entry"}
SEED = 7


# ---------------------------------------------------------------- data

def sessions_of(text, sym, now_utc):
    """Full 78-bar regular sessions, oldest first: [(date, [bar, ...]), ...]."""
    _, bars, _ = tm.parse_bars(tm.chart_result(text, sym), now_utc)
    full, _ = tm.full_sessions(tm.split_sessions(bars), "9999-12-31")
    return full


def candles(D, size):
    """A session's bars grouped from 9:30: 12 = 1-hour candles (the last one half an
    hour), 48 = 4-hour candles (9:30 to 13:30, then 13:30 to 16:00)."""
    out = []
    for i in range(0, len(D), size):
        g = D[i:i + size]
        out.append({"o": g[0]["o"], "h": max(b["h"] for b in g), "l": min(b["l"] for b in g), "c": g[-1]["c"]})
    return out


# ---------------------------------------------------------------- the context

def pivots(C, n=2):
    """Confirmed swing highs and lows: higher (lower) than the n candles on each side."""
    hi, lo = [], []
    for i in range(n, len(C) - n):
        side = [C[j] for j in range(i - n, i + n + 1) if j != i]
        if all(C[i]["h"] > c["h"] for c in side):
            hi.append(C[i]["h"])
        if all(C[i]["l"] < c["l"] for c in side):
            lo.append(C[i]["l"])
    return hi, lo


def structure(C):
    """+1 higher highs and higher lows, -1 lower highs and lower lows, else 0."""
    hi, lo = pivots(C)
    if len(hi) < 2 or len(lo) < 2:
        return 0
    if hi[-1] > hi[-2] and lo[-1] > lo[-2]:
        return 1
    if hi[-1] < hi[-2] and lo[-1] < lo[-2]:
        return -1
    return 0


def daily_trend(closes):
    """+1 above the 50-day average with the 20-day average rising (against 5 days
    before), -1 the mirror, else 0. Needs 55 closes."""
    if len(closes) < 55:
        return 0
    sma50, sma20 = statistics.mean(closes[-50:]), statistics.mean(closes[-20:])
    before = statistics.mean(closes[-25:-5])
    if closes[-1] > sma50 and sma20 > before:
        return 1
    if closes[-1] < sma50 and sma20 < before:
        return -1
    return 0


def context(prior):
    """The bias for the next session, from the sessions before it only."""
    d = daily_trend([D[-1]["c"] for _, D in prior[-60:]])
    h4 = structure([c for _, D in prior[-30:] for c in candles(D, 48)])
    h1 = structure([c for _, D in prior[-10:] for c in candles(D, 12)])
    s = d + h4 + h1
    return {"D": d, "H4": h4, "H1": h1, "bias": 1 if s >= 2 else -1 if s <= -2 else 0}


# ---------------------------------------------------------------- indicators (one series across sessions)

def ema(xs, n):
    a, out, e = 2.0 / (n + 1), [], None
    for x in xs:
        e = x if e is None else e + a * (x - e)
        out.append(e)
    return out


def atr(F, n=14):
    tr = [F[0]["h"] - F[0]["l"]] + [max(b["h"], p["c"]) - min(b["l"], p["c"]) for p, b in zip(F, F[1:])]
    out, run = [], 0.0
    for i, x in enumerate(tr):
        run += x - (tr[i - n] if i >= n else 0)
        out.append(run / min(i + 1, n))
    return out


def midpoint(F, n, i):
    w = F[max(0, i - n + 1):i + 1]
    return (max(b["h"] for b in w) + min(b["l"] for b in w)) / 2


def ichimoku(F):
    """tenkan, kijun and the cloud drawn at each bar (span A and B worked out 26 bars before)."""
    tenkan = [midpoint(F, 9, i) for i in range(len(F))]
    kijun = [midpoint(F, 26, i) for i in range(len(F))]
    span_b = [midpoint(F, 52, i) for i in range(len(F))]
    top, bot = [None] * len(F), [None] * len(F)
    for i in range(78, len(F)):
        a, b = (tenkan[i - 26] + kijun[i - 26]) / 2, span_b[i - 26]
        top[i], bot[i] = max(a, b), min(a, b)
    return tenkan, kijun, top, bot


def indicators(sessions):
    F = [b for _, D in sessions for b in D]
    closes = [b["c"] for b in F]
    tenkan, kijun, top, bot = ichimoku(F)
    body = [abs(b["c"] - b["o"]) for b in F]
    return {"F": F, "e9": ema(closes, 9), "e21": ema(closes, 21), "atr": atr(F), "tenkan": tenkan,
            "kijun": kijun, "top": top, "bot": bot, "body": body}


# ---------------------------------------------------------------- the signals: (bar, side) in time order

def sig_ma(X, base, D, prev):
    e9, e21 = X["e9"], X["e21"]
    for k in range(FIRST, LAST_ENTRY + 1):
        g = base + k
        if e9[g - 1] <= e21[g - 1] and e9[g] > e21[g]:
            yield k, 1
        elif e9[g - 1] >= e21[g - 1] and e9[g] < e21[g]:
            yield k, -1


def sig_ichimoku(X, base, D, prev):
    F, top, bot, tk, kj = X["F"], X["top"], X["bot"], X["tenkan"], X["kijun"]
    for k in range(FIRST, LAST_ENTRY + 1):
        g = base + k
        if top[g - 1] is None:
            continue
        if F[g]["c"] > top[g] and F[g - 1]["c"] <= top[g - 1] and tk[g] > kj[g]:
            yield k, 1
        elif F[g]["c"] < bot[g] and F[g - 1]["c"] >= bot[g - 1] and tk[g] < kj[g]:
            yield k, -1


def zone_entries(D, zones_at):
    """Zones (side, low, high, formed bar) from zones_at(j); a later bar that trades
    into a zone and closes on its side enters once; a close through it ends the zone."""
    live = []
    for k in range(0, LAST_ENTRY + 1):
        b, keep = D[k], []
        for side, lo, hi, j in live:
            if side == 1 and b["l"] <= hi:
                if k >= FIRST and b["c"] > lo:
                    yield k, 1
                continue
            if side == -1 and b["h"] >= lo:
                if k >= FIRST and b["c"] < hi:
                    yield k, -1
                continue
            keep.append((side, lo, hi, j))
        live = keep + list(zones_at(k))


def sig_fvg(X, base, D, prev):
    def zones_at(j):
        if j >= 2 and D[j]["l"] > D[j - 2]["h"]:
            yield 1, D[j - 2]["h"], D[j]["l"], j
        if j >= 2 and D[j]["h"] < D[j - 2]["l"]:
            yield -1, D[j]["h"], D[j - 2]["l"], j
    return zone_entries(D, zones_at)


def sig_ob(X, base, D, prev):
    F, body = X["F"], X["body"]

    def zones_at(j):
        g = base + j
        if j < 1 or g < 20:
            return
        avg = statistics.mean(body[g - 20:g]) or 1e-9
        b = F[g]
        before = F[g - 3:g]
        if b["c"] - b["o"] >= 2 * avg and b["c"] > max(x["h"] for x in before):
            ob = next((D[i] for i in range(j - 1, max(-1, j - 4), -1) if D[i]["c"] < D[i]["o"]), None)
            if ob:
                yield 1, ob["l"], ob["h"], j
        if b["o"] - b["c"] >= 2 * avg and b["c"] < min(x["l"] for x in before):
            ob = next((D[i] for i in range(j - 1, max(-1, j - 4), -1) if D[i]["c"] > D[i]["o"]), None)
            if ob:
                yield -1, ob["l"], ob["h"], j
    return zone_entries(D, zones_at)


def sig_sweep(X, base, D, prev):
    pdh, pdl = max(b["h"] for b in prev), min(b["l"] for b in prev)
    for k in range(FIRST, LAST_ENTRY + 1):
        b = D[k]
        if b["l"] < pdl < b["c"]:
            yield k, 1
        elif b["h"] > pdh > b["c"]:
            yield k, -1


GENERATORS = {"ma": sig_ma, "ichimoku": sig_ichimoku, "fvg": sig_fvg, "ob": sig_ob, "sweep": sig_sweep}


# ---------------------------------------------------------------- one trade

def trade(D, k, side, risk_atr, target_r, costs):
    """Enter at bar k's close; stop first, then the target, bar by bar; else the exit bar's close."""
    long = side == 1
    e = round(D[k]["c"], 2)
    s = round(e - risk_atr, 2) if long else round(e + risk_atr, 2)
    risk = (e - s) if long else (s - e)
    if risk <= 0:
        return None
    t = round(e + target_r * risk, 2) if long else round(e - target_r * risk, 2)

    def gain(p):
        return ((p - e) if long else (e - p)) / risk
    out, R, R_fill = "close", gain(D[EXIT]["c"]), None
    for b in D[k + 1:EXIT + 1]:
        if (b["l"] <= s) if long else (b["h"] >= s):
            out, R, R_fill = "stop", gain(s), gain(tm.stop_fill(s, b["o"], long))
            break
        if (b["h"] >= t) if long else (b["l"] <= t):
            out, R = "target", gain(t)
            break
    net = (R if R_fill is None else R_fill) - tm.cost_r(e, risk, costs)
    return {"side": "buy" if long else "sell", "time": tm.clock24(tm.OPEN_MIN + 5 * (k + 1)), "entry": e,
            "stop": s, "target": t, "outcome": out, "R": round(R, 3), "R_net": round(net, 3),
            "gapped": out == "stop" and R_fill < R}


# ---------------------------------------------------------------- the lab

def variants():
    return [(s, ctx) for ctx in (False, True) for s in SIGNALS + ("random",)]


def run_symbol(sym, sessions, costs, target_r=TARGET_R, stop_atr=STOP_ATR, warmup=WARMUP, seed=SEED):
    """Every variant's trades on one stock, and the contexts of the tested days."""
    X = indicators(sessions)
    rng = random.Random("%s-%s" % (seed, sym))
    out = {v: [] for v in variants()}
    days = []
    for i in range(max(1, warmup), len(sessions)):
        d, D = sessions[i]
        ctx = context(sessions[:i])
        days.append({"date": d, **ctx})
        base = i * tm.SESSION_BARS
        prev = sessions[i - 1][1]
        rk, rs = rng.randint(FIRST, LAST_ENTRY), rng.choice((1, -1))
        for name in SIGNALS + ("random",):
            fired = [(rk, rs)] if name == "random" else list(GENERATORS[name](X, base, D, prev))
            for with_ctx in (False, True):
                if with_ctx:
                    if not ctx["bias"]:
                        continue
                    pick = (rk, ctx["bias"]) if name == "random" else next(
                        ((k, s) for k, s in fired if s == ctx["bias"]), None)
                else:
                    pick = fired[0] if fired else None
                if not pick:
                    continue
                k, s = pick
                t = trade(D, k, s, stop_atr * X["atr"][base + k], target_r, costs)
                if t:
                    t.update({"symbol": sym, "date": d})
                    out[(name, with_ctx)].append(t)
    return out, days


def split_date(dates):
    """The middle of the tested calendar span: year 1 before it, year 2 from it."""
    lo, hi = datetime.date.fromisoformat(min(dates)), datetime.date.fromisoformat(max(dates))
    return (lo + (hi - lo) / 2).isoformat()


def stats(trades, cut):
    st = tm.backtest_stats(sorted(trades, key=lambda t: (t["date"], t["time"])))
    for key, part in (("year1", [t for t in trades if t["date"] < cut]), ("year2", [t for t in trades if t["date"] >= cut])):
        st[key] = round(statistics.mean(t["R_net"] for t in part), 3) if part else None
        st[key + "_trades"] = len(part)
    return st


def lab(charts, now_utc, costs, target_r=TARGET_R, stop_atr=STOP_ATR, warmup=WARMUP, seed=SEED):
    every = {v: [] for v in variants()}
    days, per, failed = [], {}, {}
    for sym, text in charts.items():
        try:
            sessions = sessions_of(text, sym, now_utc)
        except tm.TMError as e:
            failed[sym] = str(e)
            continue
        if len(sessions) <= warmup:
            failed[sym] = "%d full sessions: the context needs more than %d" % (len(sessions), warmup)
            continue
        out, ds = run_symbol(sym, sessions, costs, target_r, stop_atr, warmup, seed)
        for v in out:
            every[v] += out[v]
        days += [dict(x, symbol=sym) for x in ds]
        per[sym] = {"sessions": len(sessions), "tested_days": len(ds), "first": ds[0]["date"], "last": ds[-1]["date"]}
    if not days:
        raise tm.TMError("no stock had enough history: " + "; ".join("%s: %s" % kv for kv in failed.items()))
    cut = split_date([x["date"] for x in days])
    rows = []
    for name, with_ctx in variants():
        st = stats(every[(name, with_ctx)], cut)
        rows.append({"signal": name, "context": with_ctx, "stats": st})
    base = {r["context"]: r["stats"] for r in rows if r["signal"] == "random"}
    for r in rows:
        b, st = base[r["context"]], r["stats"]
        r["beats_random"] = (r["signal"] != "random" and None not in (st["year1"], st["year2"], b["year1"], b["year2"])
                             and min(st["year1_trades"], st["year2_trades"]) >= tm.MIN_TRADES
                             and st["year1"] > b["year1"] and st["year2"] > b["year2"])
    bias = {k: sum(1 for x in days if x["bias"] == v) for k, v in (("buy", 1), ("sell", -1), ("none", 0))}
    return {"split": cut, "target_r": target_r, "stop_atr": stop_atr, "costs": dict(costs), "symbols": per,
            "failed": failed, "bias_days": bias, "rows": rows}


def lab_report(doc):
    out = ["Confluence lab, phase 1: %d stocks, %s, stop %g ATR, target %gR, out at 15:55."
           % (len(doc["symbols"]), tm.cost_words(doc["costs"]), doc["stop_atr"], doc["target_r"]),
           "Year 1 = before %s, year 2 = from it. Context days: %d buys only, %d sells only, %d no bias."
           % (doc["split"], doc["bias_days"]["buy"], doc["bias_days"]["sell"], doc["bias_days"]["none"]), ""]
    head = "  %-34s %6s %8s  %-17s %7s %7s  %-8s %s" % ("signal", "trades", "avg R", "95 % range", "year 1",
                                                        "year 2", "verdict", "beats random")
    for with_ctx in (False, True):
        out.append("Alone (buys and sells):" if not with_ctx else "With the D/H4/H1 context (its direction only):")
        out.append(head)
        for r in doc["rows"]:
            if r["context"] != with_ctx:
                continue
            st = r["stats"]
            if not st["trades"]:
                out.append("  %-34s %6d" % (NAMES[r["signal"]], 0))
                continue
            lo, hi = st.get("ci95_net", (0, 0))
            y1, y2 = st["year1"], st["year2"]
            out.append("  %-34s %6d %+8.3f  %+.3f to %+.3f %7s %7s  %-8s %s" % (
                NAMES[r["signal"]], st["trades"], st["avg_R_net"], lo, hi,
                "%+.3f" % y1 if y1 is not None else "-", "%+.3f" % y2 if y2 is not None else "-",
                tm.short_verdict(st), "-" if r["signal"] == "random" else "YES" if r["beats_random"] else "no"))
        out.append("")
    keep = ["%s%s" % (NAMES[r["signal"]], " + context" if r["context"] else "") for r in doc["rows"] if r["beats_random"]]
    out.append("Beat random in both years: %s." % (", ".join(keep) if keep else "none"))
    out.append("R after costs. 12 variants on the same data: a YES is a candidate for phase 4, not an edge. "
               "An edge needs its own 95 % range above zero too.")
    if doc["failed"]:
        out.append("Skipped: " + "; ".join("%s (%s)" % kv for kv in sorted(doc["failed"].items())))
    out.append("Education and tooling, not financial advice. Nothing here places an order.")
    return out


# ---------------------------------------------------------------- the commands

def load_charts(a, rng):
    charts, failed = {}, {}
    for n, sym in enumerate(tm.backtest_symbols(a)):
        try:
            if a.offline:
                charts[sym] = tm.read_offline(a.offline, sym)
            else:
                if n:
                    tm._sleep(tm.PAUSE)
                charts[sym] = tm.fetch_chart(sym, rng)
        except tm.TMError as e:
            failed[sym] = str(e)
    return charts, failed


def cmd_backtest(a):
    if not a.target_r > 0 or not a.stop_atr > 0:
        raise tm.TMError("--target-r and --stop-atr must be more than 0")
    if not a.offline and tm.data_source() != "alpaca":
        raise tm.TMError("the lab needs years of bars: use --offline with a folder that backtest --save wrote, "
                         "or set TRADER_ALPACA_KEY and TRADER_ALPACA_SECRET")
    charts, failed = load_charts(a, a.range)
    doc = lab(charts, tm.parse_now(a.now), tm.load_costs(a), a.target_r, a.stop_atr, a.warmup, a.seed)
    doc["failed"].update(failed)
    if a.json:
        print(json.dumps(doc, indent=2))
    else:
        print("\n".join(lab_report(doc)))
    return 0


def cmd_context(a):
    if not a.offline and tm.data_source() != "alpaca":
        raise tm.TMError("the context needs 6 months of bars: use --offline, or set the Alpaca keys")
    now = tm.parse_now(a.now)
    charts, failed = load_charts(a, "6mo")
    word = {1: "buys only", -1: "sells only", 0: "no bias, stay out"}
    sign = {1: "up", -1: "down", 0: "flat"}
    rows = []
    for sym, text in charts.items():
        try:
            sessions = sessions_of(text, sym, now)
        except tm.TMError as e:
            failed[sym] = str(e)
            continue
        if len(sessions) < WARMUP:
            failed[sym] = "%d full sessions, the context needs %d" % (len(sessions), WARMUP)
            continue
        c = context(sessions)
        rows.append(dict(c, symbol=sym, after=sessions[-1][0]))
    if a.json:
        print(json.dumps({"rows": rows, "failed": failed}, indent=2))
        return 0
    print("Context for the next session (from the sessions up to the date shown):")
    for r in rows:
        print("  %-6s after %s   D %-4s  H4 %-4s  H1 %-4s  -> %s" % (
            r["symbol"], r["after"], sign[r["D"]], sign[r["H4"]], sign[r["H1"]], word[r["bias"]]))
    for sym, why in sorted(failed.items()):
        print("  %-6s skipped: %s" % (sym, why))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Confluence lab: higher-timeframe context and entry signals, "
                                             "tested against random entries (trader-timemachine).")
    sub = ap.add_subparsers(dest="cmd")
    for name, help_ in (("backtest", "test each signal alone and with the context"),
                        ("context", "the D/H4/H1 bias for the next session")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--symbols", help="tickers, comma separated")
        p.add_argument("--watchlist", default="trader/watchlist.json", help="used when --symbols is not given")
        p.add_argument("--offline", help="a folder of <SYM>-<range>5m.json.gz files (backtest --save writes them)")
        p.add_argument("--now", help="the time to read the data at, YYYY-MM-DD HH:MM New York (default: now)")
        p.add_argument("--json", action="store_true", help="print the numbers as JSON")
        if name == "backtest":
            p.add_argument("--range", default="2y", choices=tm.RANGES[1:], help="history to fetch from Alpaca (2y)")
            p.add_argument("--target-r", type=float, default=TARGET_R, help="target in R (%g)" % TARGET_R)
            p.add_argument("--stop-atr", type=float, default=STOP_ATR, help="stop distance in 5-minute ATRs (%g)" % STOP_ATR)
            p.add_argument("--slippage-bps", type=float, help="slippage a side in bps (default %g)" % tm.SLIPPAGE_BPS)
            p.add_argument("--commission", type=float, help="commission a share a side in $ (default %g)" % tm.COMMISSION)
            p.add_argument("--config", default="trader/config.json", help="costs from here when the flags are not given")
            p.add_argument("--warmup", type=int, default=WARMUP, help="sessions read before the first tested day (%d)" % WARMUP)
            p.add_argument("--seed", type=int, default=SEED, help="seed of the random entries (%d)" % SEED)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return cmd_backtest(a) if a.cmd == "backtest" else cmd_context(a)
    except tm.TMError as e:
        print("confluence: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
