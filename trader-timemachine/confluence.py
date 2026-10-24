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
SIGNALS = ("ma", "ichimoku", "fvg", "ob", "sweep", "va_return", "va_break", "lvn", "ict")
NAMES = {"ma": "moving averages 9/21", "ichimoku": "Ichimoku cloud", "fvg": "fair value gap",
         "ob": "order block", "sweep": "liquidity sweep", "va_return": "back into value area",
         "va_break": "accepted outside value area", "lvn": "through a low volume node",
         "ict": "ICT: sweep, then FVG, 9:30-11:00", "random": "random entry"}
PROFILE_BINS, VALUE_AREA = 50, 0.70
KILLZONE = 17                         # bars: 9:30 to 11:00, the New York open
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


# ---------------------------------------------------------------- phase 2: yesterday's volume profile, ICT

def profile(D):
    """The session's volume profile on 50 price bins (each bar's volume spread evenly
    over its range): the point of control, the value area (70 % of the volume around
    it) and the thinnest bin between the POC and each edge (the low volume nodes)."""
    lo, hi = min(b["l"] for b in D), max(b["h"] for b in D)
    step = max((hi - lo) / PROFILE_BINS, 0.01)
    n = int((hi - lo) / step) + 1
    vol = [0.0] * n
    for b in D:
        a, z = int((b["l"] - lo) / step), min(n - 1, int((b["h"] - lo) / step))
        for i in range(a, z + 1):
            vol[i] += (b.get("v") or 0) / (z - a + 1)
    price = lambda i: lo + (i + 0.5) * step  # noqa: E731
    poc = max(range(n), key=lambda i: vol[i])
    i = j = poc
    acc, total = vol[poc], sum(vol)
    while acc < VALUE_AREA * total and (i > 0 or j < n - 1):
        down = vol[i - 1] if i > 0 else -1
        up = vol[j + 1] if j < n - 1 else -1
        if up >= down:
            j += 1
            acc += vol[j]
        else:
            i -= 1
            acc += vol[i]

    def thinnest(a, z):
        inside = range(a + 1, z)
        return price(min(inside, key=lambda k: vol[k])) if len(inside) else None
    return {"poc": price(poc), "val": price(i), "vah": price(j), "lvn_up": thinnest(poc, j), "lvn_dn": thinnest(i, poc)}


def sig_va_return(X, base, D, prev):
    """Opened outside yesterday's value area, then a close back inside: toward the POC."""
    p = profile(prev)
    o = D[0]["o"]
    if p["val"] <= o <= p["vah"]:
        return
    for k in range(FIRST, LAST_ENTRY + 1):
        if p["val"] <= D[k]["c"] <= p["vah"]:
            yield k, 1 if o < p["val"] else -1
            return


def sig_va_break(X, base, D, prev):
    """Opened inside yesterday's value area, then two closes in a row outside it: that way."""
    p = profile(prev)
    if not p["val"] <= D[0]["o"] <= p["vah"]:
        return
    for k in range(FIRST, LAST_ENTRY + 1):
        if D[k]["c"] > p["vah"] and D[k - 1]["c"] > p["vah"]:
            yield k, 1
        elif D[k]["c"] < p["val"] and D[k - 1]["c"] < p["val"]:
            yield k, -1


def sig_lvn(X, base, D, prev):
    """A close through one of yesterday's low volume nodes: price tends to travel fast there."""
    p = profile(prev)
    for k in range(FIRST, LAST_ENTRY + 1):
        a, b = D[k - 1]["c"], D[k]["c"]
        for lvn in (p["lvn_up"], p["lvn_dn"]):
            if lvn is None:
                continue
            if a < lvn <= b:
                yield k, 1
            elif a > lvn >= b:
                yield k, -1


def sig_ict(X, base, D, prev):
    """In the New York open killzone: yesterday's low (high) is swept, then a fair value
    gap opens the other way; the entry is the first return into that gap."""
    pdh, pdl = max(b["h"] for b in prev), min(b["l"] for b in prev)
    swept = {1: None, -1: None}

    def zones_at(j):
        if j > KILLZONE:
            return
        if swept[1] is None and D[j]["l"] < pdl:
            swept[1] = j
        if swept[-1] is None and D[j]["h"] > pdh:
            swept[-1] = j
        if j >= 2 and swept[1] is not None and swept[1] < j and D[j]["l"] > D[j - 2]["h"]:
            yield 1, D[j - 2]["h"], D[j]["l"], j
        if j >= 2 and swept[-1] is not None and swept[-1] < j and D[j]["h"] < D[j - 2]["l"]:
            yield -1, D[j]["h"], D[j - 2]["l"], j
    return zone_entries(D, zones_at)


GENERATORS = {"ma": sig_ma, "ichimoku": sig_ichimoku, "fvg": sig_fvg, "ob": sig_ob, "sweep": sig_sweep,
              "va_return": sig_va_return, "va_break": sig_va_break, "lvn": sig_lvn, "ict": sig_ict}


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

def variants(signals=SIGNALS):
    return [(s, ctx) for ctx in (False, True) for s in tuple(signals) + ("random",)]


def run_symbol(sym, sessions, costs, target_r=TARGET_R, stop_atr=STOP_ATR, warmup=WARMUP, seed=SEED,
               signals=SIGNALS):
    """Every variant's trades on one stock, and the contexts of the tested days."""
    X = indicators(sessions)
    rng = random.Random("%s-%s" % (seed, sym))
    out = {v: [] for v in variants(signals)}
    days = []
    for i in range(max(1, warmup), len(sessions)):
        d, D = sessions[i]
        ctx = context(sessions[:i])
        days.append({"date": d, **ctx})
        base = i * tm.SESSION_BARS
        prev = sessions[i - 1][1]
        rk, rs = rng.randint(FIRST, LAST_ENTRY), rng.choice((1, -1))
        for name in tuple(signals) + ("random",):
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


def lab(charts, now_utc, costs, target_r=TARGET_R, stop_atr=STOP_ATR, warmup=WARMUP, seed=SEED,
        signals=SIGNALS):
    every = {v: [] for v in variants(signals)}
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
        out, ds = run_symbol(sym, sessions, costs, target_r, stop_atr, warmup, seed, signals)
        for v in out:
            every[v] += out[v]
        days += [dict(x, symbol=sym) for x in ds]
        per[sym] = {"sessions": len(sessions), "tested_days": len(ds), "first": ds[0]["date"], "last": ds[-1]["date"]}
    if not days:
        raise tm.TMError("no stock had enough history: " + "; ".join("%s: %s" % kv for kv in failed.items()))
    cut = split_date([x["date"] for x in days])
    rows = []
    for name, with_ctx in variants(signals):
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
    out = ["Confluence lab: %d stocks, %s, stop %g ATR, target %gR, out at 15:55."
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
    out.append("R after costs. %d variants on the same data: a YES is a candidate for phase 4, not an edge. "
               "An edge needs its own 95 %% range above zero too." % len(doc["rows"]))
    if doc["failed"]:
        out.append("Skipped: " + "; ".join("%s (%s)" % kv for kv in sorted(doc["failed"].items())))
    out.append("Education and tooling, not financial advice. Nothing here places an order.")
    return out


# ---------------------------------------------------------------- phase 4: the confluence engine

STATE_BARS = 6                        # a signal's vote lasts 30 minutes after it fires
FEATURES = SIGNALS + ("D", "H4", "H1")
VOTE_LEVELS = (2, 3, 4, 5, 6)
SCORE_QUANTILES = (0.80, 0.90, 0.95, 0.98)
MIN_TRAIN, GATE_TRADES = 100, 100


def votes_at(events, k):
    """Each signal's vote at bar k: the side of its last firing in the STATE_BARS bars up to k."""
    v = {}
    for name, ev in events.items():
        last = 0
        for kk, side in ev:
            if kk > k:
                break
            if k - kk < STATE_BARS:
                last = side
        v[name] = last
    return v


def engine_samples(sym, sessions, costs, target_r=TARGET_R, stop_atr=STOP_ATR, warmup=WARMUP):
    """Every bar where a signal fires, both sides: the votes as seen from that side
    (+1 agrees, -1 disagrees) and the trade it would have made. Grouped by day."""
    X = indicators(sessions)
    days = []
    for i in range(max(1, warmup), len(sessions)):
        d, D = sessions[i]
        ctx = context(sessions[:i])
        base = i * tm.SESSION_BARS
        events = {name: [e for e in GENERATORS[name](X, base, D, sessions[i - 1][1])] for name in SIGNALS}
        bars = sorted({k for ev in events.values() for k, _ in ev if FIRST <= k <= LAST_ENTRY})
        cands = []
        for k in bars:
            v = votes_at(events, k)
            v.update({"D": ctx["D"], "H4": ctx["H4"], "H1": ctx["H1"]})
            for side in (1, -1):
                t = trade(D, k, side, stop_atr * X["atr"][base + k], target_r, costs)
                if t:
                    t.update({"symbol": sym, "date": d})
                    cands.append({"k": k, "x": {f: v[f] * side for f in FEATURES}, "trade": t})
        days.append({"symbol": sym, "date": d, "cands": cands})
    return days


def first_trades(days, score, level):
    """One trade a stock a day: the first candidate whose score reaches the level."""
    out = []
    for day in days:
        hit = next((c for c in day["cands"] if score(c["x"]) >= level), None)
        if hit:
            out.append(hit["trade"])
    return out


def avg_net(trades):
    return statistics.mean(t["R_net"] for t in trades) if trades else None


def learn_weights(days):
    """Each vote's weight from the training days only: the average R after costs when
    it agrees with the side, minus the average over every candidate."""
    rows = [c for d in days for c in d["cands"]]
    if not rows:
        return {f: 0.0 for f in FEATURES}
    overall = statistics.mean(c["trade"]["R_net"] for c in rows)
    w = {}
    for f in FEATURES:
        agree = [c["trade"]["R_net"] for c in rows if c["x"][f] == 1]
        w[f] = round(statistics.mean(agree) - overall, 4) if len(agree) >= tm.MIN_TRADES else 0.0
    return w


def pick(train, score, levels):
    """The level with the best average on the training days (at least MIN_TRAIN trades)."""
    best = None
    for lv in levels:
        tr = first_trades(train, score, lv)
        if len(tr) >= MIN_TRAIN and (best is None or avg_net(tr) > best[1]):
            best = (lv, avg_net(tr), len(tr))
    return best


def gate(st):
    """Phase 4's gate on the test year: enough trades, the 95 % range above zero, both halves positive."""
    if st["trades"] < GATE_TRADES:
        return False
    return st["ci95_net"][0] > 0 and min(st["halves_R_net"]) > 0


def engine(charts, now_utc, costs, target_r=TARGET_R, stop_atr=STOP_ATR, warmup=WARMUP):
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
        ds = engine_samples(sym, sessions, costs, target_r, stop_atr, warmup)
        days += ds
        per[sym] = {"sessions": len(sessions), "tested_days": len(ds)}
    if not days:
        raise tm.TMError("no stock had enough history: " + "; ".join("%s: %s" % kv for kv in failed.items()))
    cut = split_date([d["date"] for d in days])
    train = [d for d in days if d["date"] < cut]
    test = [d for d in days if d["date"] >= cut]
    rules = []
    count = lambda x: sum(x.values())  # noqa: E731
    w = learn_weights(train)
    weighted = lambda x: sum(w[f] * x[f] for f in FEATURES)  # noqa: E731
    scores = sorted(weighted(c["x"]) for d in train for c in d["cands"])
    levels = sorted({round(tm.pct(scores, q), 4) for q in SCORE_QUANTILES}) if scores else []
    for name, score, lvls in (("votes", count, VOTE_LEVELS), ("weighted", weighted, levels)):
        best = pick(train, score, lvls)
        if not best:
            rules.append({"rule": name, "level": None})
            continue
        trades = sorted(first_trades(test, score, best[0]), key=lambda t: (t["date"], t["time"]))
        st = tm.backtest_stats(trades)
        rules.append({"rule": name, "level": best[0], "train_avg": round(best[1], 3), "train_trades": best[2],
                      "test": st, "passes": gate(st)})
    return {"split": cut, "target_r": target_r, "stop_atr": stop_atr, "costs": dict(costs), "symbols": per,
            "failed": failed, "weights": w, "rules": rules, "state_bars": STATE_BARS}


def engine_report(doc):
    out = ["Confluence engine (phase 4): %d stocks, %s, stop %g ATR, target %gR, out at 15:55."
           % (len(doc["symbols"]), tm.cost_words(doc["costs"]), doc["stop_atr"], doc["target_r"]),
           "Trained on year 1 (before %s), judged once on year 2. A vote lasts %d bars (30 minutes)."
           % (doc["split"], doc["state_bars"]), "",
           "Weights learned on year 1 (R after costs when the vote agrees, against the average):"]
    for f, v in sorted(doc["weights"].items(), key=lambda kv: -kv[1]):
        out.append("  %-34s %+.3f" % (NAMES.get(f, "context " + f), v))
    out += ["", "  %-34s %-9s %10s %8s %8s  %-17s %s" % ("rule", "level", "year 1 avg", "trades", "year 2",
                                                         "95 % range", "gate")]
    words = {"votes": "count of agreeing votes", "weighted": "weighted score"}
    for r in doc["rules"]:
        if r["level"] is None:
            out.append("  %-34s never reached %d trades on year 1" % (words[r["rule"]], MIN_TRAIN))
            continue
        st = r["test"]
        if not st["trades"]:
            out.append("  %-34s %-9g %+10.3f %8d %8s" % (words[r["rule"]], r["level"], r["train_avg"], 0, "-"))
            continue
        lo, hi = st.get("ci95_net", (0, 0))
        out.append("  %-34s %-9g %+10.3f %8d %+8.3f  %+.3f to %+.3f %s" % (
            words[r["rule"]], r["level"], r["train_avg"], st["trades"], st["avg_R_net"], lo, hi,
            "PASSES" if r["passes"] else "fails"))
    out.append("")
    out.append("The gate (year 2 only): at least %d trades, the 95 %% range above zero, both halves positive. "
               "Only a rule that passes goes on to the shadow journal." % GATE_TRADES)
    if doc["failed"]:
        out.append("Skipped: " + "; ".join("%s (%s)" % kv for kv in sorted(doc["failed"].items())))
    out.append("Education and tooling, not financial advice. Nothing here places an order.")
    return out


# ---------------------------------------------------------------- phase 6: live, from the IBKR connector (shadow only)

LIVE_LEVEL = 4


def ibkr_rows(path):
    """The JSON the IBKR connector's get_price_history returns, saved as-is ->
    bars in New York time: {"date", "min" (minutes from 9:30), "o", "h", "l", "c", "v"}."""
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError) as e:
        raise tm.TMError("could not read %s: %s" % (path, e))
    if isinstance(d, dict) and isinstance(d.get("result"), dict):
        d = d["result"]
    rows, vol = [], (d.get("volume") or []) if isinstance(d, dict) else []
    for i, ts in enumerate((d.get("time") or []) if isinstance(d, dict) else []):
        try:
            o, h, l, c = (float(d[k][i]) for k in ("open", "high", "low", "close"))
            utc = datetime.datetime.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S")
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        local = tm.to_eastern(utc)
        rows.append({"date": local.date().isoformat(), "min": local.hour * 60 + local.minute - tm.OPEN_MIN,
                     "o": o, "h": h, "l": l, "c": c,
                     "v": float(vol[i]) if i < len(vol) and vol[i] is not None else 0.0})
    if not rows:
        raise tm.TMError("%s has no bars" % path)
    return rows


def live_context(daily, hourly, today):
    """The context from daily bars (D) and the connector's 1-hour bars (H1, and H4 as
    groups of four 1-hour bars within each session), sessions before `today` only."""
    closes = [r["c"] for r in daily if r["date"] < today]
    by = {}
    for r in hourly:
        if r["date"] < today and 0 <= r["min"] < 390:
            by.setdefault(r["date"], []).append(r)
    dates = sorted(by)
    d = daily_trend(closes[-60:])
    h4 = structure([c for x in dates[-30:] for c in candles(by[x], 4)])
    h1 = structure([c for x in dates[-10:] for c in by[x]])
    s = d + h4 + h1
    return {"D": d, "H4": h4, "H1": h1, "bias": 1 if s >= 2 else -1 if s <= -2 else 0,
            "daily_closes": len(closes), "after": dates[-1] if dates else None}


def live_day(rows, today):
    """(yesterday's 78 bars, today's bars padded to 78, the last real bar) from 5-minute rows."""
    days = {}
    for r in rows:
        if 0 <= r["min"] < 390 and r["min"] % 5 == 0:
            days.setdefault(r["date"], {})[r["min"] // 5] = r
    prev = [d for d in sorted(days) if d < today and len(days[d]) == tm.SESSION_BARS]
    if not prev or today not in days:
        raise tm.TMError("need yesterday's full session and today's bars (period TWO_DAYS)")
    t = days[today]
    last = -1
    while last + 1 in t:
        last += 1
    if last < FIRST:
        raise tm.TMError("today has %d bars: the first entry is at 9:45" % (last + 1))
    D = [t[i] for i in range(last + 1)]
    c = D[-1]["c"]
    D += [{"o": c, "h": c, "l": c, "c": c, "v": 0.0} for _ in range(tm.SESSION_BARS - len(D))]
    return [days[prev[-1]][i] for i in range(tm.SESSION_BARS)], D, last


def live_signal(rows, ctx, today, level=LIVE_LEVEL, stop_atr=STOP_ATR, target_r=TARGET_R):
    """Today's votes at the last bar, and the first bar so far where the agreeing
    votes (signals for 30 minutes + the context) reach the level: a shadow proposal."""
    prev, D, last = live_day(rows, today)
    X = indicators([("prev", prev), (today, D)])
    base = tm.SESSION_BARS
    events = {n: [e for e in GENERATORS[n](X, base, D, prev) if e[0] <= last] for n in SIGNALS}
    ctx_votes = {"D": ctx["D"], "H4": ctx["H4"], "H1": ctx["H1"]}
    now = votes_at(events, last)
    now.update(ctx_votes)
    proposal = None
    for k in sorted({k for ev in events.values() for k, _ in ev if FIRST <= k <= min(last, LAST_ENTRY)}):
        v = votes_at(events, k)
        v.update(ctx_votes)
        n = sum(v.values())
        if abs(n) >= level:
            side = 1 if n > 0 else -1
            e = round(D[k]["c"], 2)
            risk = round(stop_atr * X["atr"][base + k], 2)
            if risk <= 0:
                continue
            proposal = {"date": today, "bar": k, "time": tm.clock24(tm.OPEN_MIN + 5 * (k + 1)),
                        "side": "buy" if side > 0 else "sell", "entry": e,
                        "stop": round(e - side * risk, 2), "target": round(e + side * target_r * risk, 2),
                        "votes": n, "with": sorted(f for f in FEATURES if v[f] == side)}
            break
    return {"last_bar": tm.clock24(tm.OPEN_MIN + 5 * (last + 1)), "votes_now": now,
            "count_now": sum(now.values()), "proposal": proposal}


def read_journal(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except ValueError as e:
        raise tm.TMError("%s is not valid JSON: %s" % (path, e))
    return data if isinstance(data, list) else []


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, path)


def review(entry, rows, costs):
    """How a shadow proposal ended, from the whole day's 5-minute bars."""
    D = [None] * tm.SESSION_BARS
    for r in rows:
        if r["date"] == entry["date"] and 0 <= r["min"] < 390 and r["min"] % 5 == 0:
            D[r["min"] // 5] = r
    if None in D[:EXIT + 1]:
        return None
    side = 1 if entry["side"] == "buy" else -1
    t = trade(D, entry["bar"], side, abs(entry["entry"] - entry["stop"]), abs(entry["target"] - entry["entry"]) /
              abs(entry["entry"] - entry["stop"]), costs)
    return t and {"outcome": t["outcome"], "R": t["R"], "R_net": t["R_net"]}


def cmd_live_context(a):
    today = a.date or tm.to_eastern(tm.parse_now(a.now)).date().isoformat()
    out, failed = {}, {}
    for sym in tm.backtest_symbols(a):
        try:
            c = live_context(ibkr_rows(os.path.join(a.ibkr, "%s-daily.json" % sym)),
                             ibkr_rows(os.path.join(a.ibkr, "%s-1h.json" % sym)), today)
        except tm.TMError as e:
            failed[sym] = str(e)
            continue
        if c["daily_closes"] < 55:
            failed[sym] = "%d daily bars: the 50-day average needs 55 (step_count 70)" % c["daily_closes"]
            continue
        out[sym] = c
    write_json(a.out, {"date": today, "context": out, "failed": failed})
    word = {1: "buys only", -1: "sells only", 0: "no bias"}
    for sym, c in out.items():
        print("  %-6s D %+d  H4 %+d  H1 %+d  -> %s" % (sym, c["D"], c["H4"], c["H1"], word[c["bias"]]))
    for sym, why in sorted(failed.items()):
        print("  %-6s skipped: %s" % (sym, why))
    print("Wrote %s." % a.out)
    return 0


def cmd_live(a):
    try:
        with open(a.context, encoding="utf-8") as fh:
            cdoc = json.load(fh)
    except (OSError, ValueError) as e:
        raise tm.TMError("no context file %s (run live-context first): %s" % (a.context, e))
    today = cdoc.get("date")
    journal = read_journal(a.journal) if a.journal else []
    seen = {(j["symbol"], j["date"]) for j in journal}
    for sym in tm.backtest_symbols(a):
        ctx = (cdoc.get("context") or {}).get(sym)
        if not ctx:
            print("  %-6s no context for today: skipped" % sym)
            continue
        try:
            res = live_signal(ibkr_rows(os.path.join(a.ibkr, "%s-5m-live.json" % sym)), ctx, today, a.level)
        except tm.TMError as e:
            print("  %-6s skipped: %s" % (sym, e))
            continue
        p = res["proposal"]
        line = "  %-6s at %s: %+d votes now" % (sym, res["last_bar"], res["count_now"])
        if p:
            line += "; shadow %s at %s %.2f, stop %.2f, target %.2f (%+d votes: %s)" % (
                p["side"], p["time"], p["entry"], p["stop"], p["target"], p["votes"], ", ".join(p["with"]))
            if (sym, today) not in seen:
                journal.append(dict(p, symbol=sym))
                seen.add((sym, today))
        print(line)
    if a.journal:
        write_json(a.journal, journal)
    print("Shadow only: nothing was ordered. Level %d agreeing votes." % a.level)
    return 0


def cmd_live_review(a):
    journal = read_journal(a.journal)
    costs = tm.load_costs(a)
    for j in journal:
        if j["date"] != a.date or "result" in j:
            continue
        path = os.path.join(a.ibkr, "%s-5m.json" % j["symbol"])
        if not os.path.exists(path):
            print("  %-6s no %s: not reviewed" % (j["symbol"], path))
            continue
        res = review(j, ibkr_rows(path), costs)
        if res:
            j["result"] = res
            print("  %-6s %s at %s %.2f -> %s, %+.2f R (%+.2f R after costs)" % (
                j["symbol"], j["side"], j["time"], j["entry"], res["outcome"], res["R"], res["R_net"]))
    write_json(a.journal, journal)
    done = [j["result"]["R_net"] for j in journal if "result" in j]
    if done:
        print("Shadow journal: %d proposals, %d green, average %+.3f R after costs, total %+.2f R."
              % (len(done), sum(1 for r in done if r > 0), statistics.mean(done), sum(done)))
        if len(done) < tm.MIN_TRADES:
            print("It takes %d before the average means much (now %d)." % (tm.MIN_TRADES, len(done)))
    return 0


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
    signals = SIGNALS
    if a.signals:
        signals = tuple(x.strip() for x in a.signals.split(",") if x.strip())
        bad = [x for x in signals if x not in GENERATORS]
        if bad or not signals:
            raise tm.TMError("unknown signal %s: pick from %s" % (", ".join(bad) or "(none)", ", ".join(SIGNALS)))
    doc = lab(charts, tm.parse_now(a.now), tm.load_costs(a), a.target_r, a.stop_atr, a.warmup, a.seed, signals)
    doc["failed"].update(failed)
    if a.json:
        print(json.dumps(doc, indent=2))
    else:
        print("\n".join(lab_report(doc)))
    return 0


def cmd_engine(a):
    if not a.target_r > 0 or not a.stop_atr > 0:
        raise tm.TMError("--target-r and --stop-atr must be more than 0")
    if not a.offline and tm.data_source() != "alpaca":
        raise tm.TMError("the engine needs years of bars: use --offline with a folder that backtest --save wrote, "
                         "or set TRADER_ALPACA_KEY and TRADER_ALPACA_SECRET")
    charts, failed = load_charts(a, a.range)
    doc = engine(charts, tm.parse_now(a.now), tm.load_costs(a), a.target_r, a.stop_atr, a.warmup)
    doc["failed"].update(failed)
    print(json.dumps(doc, indent=2) if a.json else "\n".join(engine_report(doc)))
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
                        ("engine", "combine the votes: learn on year 1, judge on year 2"),
                        ("context", "the D/H4/H1 bias for the next session")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--symbols", help="tickers, comma separated")
        p.add_argument("--watchlist", default="trader/watchlist.json", help="used when --symbols is not given")
        p.add_argument("--offline", help="a folder of <SYM>-<range>5m.json.gz files (backtest --save writes them)")
        p.add_argument("--now", help="the time to read the data at, YYYY-MM-DD HH:MM New York (default: now)")
        p.add_argument("--json", action="store_true", help="print the numbers as JSON")
        if name in ("backtest", "engine"):
            p.add_argument("--range", default="2y", choices=tm.RANGES[1:], help="history to fetch from Alpaca (2y)")
            p.add_argument("--target-r", type=float, default=TARGET_R, help="target in R (%g)" % TARGET_R)
            p.add_argument("--stop-atr", type=float, default=STOP_ATR, help="stop distance in 5-minute ATRs (%g)" % STOP_ATR)
            p.add_argument("--slippage-bps", type=float, help="slippage a side in bps (default %g)" % tm.SLIPPAGE_BPS)
            p.add_argument("--commission", type=float, help="commission a share a side in $ (default %g)" % tm.COMMISSION)
            p.add_argument("--config", default="trader/config.json", help="costs from here when the flags are not given")
            p.add_argument("--warmup", type=int, default=WARMUP, help="sessions read before the first tested day (%d)" % WARMUP)
        if name == "backtest":
            p.add_argument("--signals", help="only these, comma separated (default all): " + ",".join(SIGNALS))
            p.add_argument("--seed", type=int, default=SEED, help="seed of the random entries (%d)" % SEED)
    p = sub.add_parser("live-context", help="today's D/H4/H1 bias from the IBKR connector's daily and 1-hour bars")
    p.add_argument("--ibkr", required=True, help="folder with <SYM>-daily.json (step_count 70) and <SYM>-1h.json")
    p.add_argument("--symbols", help="tickers, comma separated")
    p.add_argument("--watchlist", default="trader/watchlist.json", help="used when --symbols is not given")
    p.add_argument("--date", help="today, YYYY-MM-DD (default: today in New York)")
    p.add_argument("--now", help=argparse.SUPPRESS)
    p.add_argument("--out", required=True, help="the context file to write")
    p = sub.add_parser("live", help="votes so far today and the shadow proposal, from the IBKR 5-minute bars")
    p.add_argument("--ibkr", required=True, help="folder with <SYM>-5m-live.json (FIVE_MINS, period TWO_DAYS)")
    p.add_argument("--context", required=True, help="the file live-context wrote")
    p.add_argument("--symbols", help="tickers, comma separated")
    p.add_argument("--watchlist", default="trader/watchlist.json", help="used when --symbols is not given")
    p.add_argument("--level", type=int, default=LIVE_LEVEL, help="agreeing votes for a proposal (%d)" % LIVE_LEVEL)
    p.add_argument("--journal", help="shadow journal to add the day's first proposal per stock to")
    p = sub.add_parser("live-review", help="how the day's shadow proposals ended")
    p.add_argument("--ibkr", required=True, help="folder with the day's <SYM>-5m.json (FIVE_MINS, period ONE_DAY)")
    p.add_argument("--journal", required=True, help="the shadow journal")
    p.add_argument("--date", required=True, help="the day, YYYY-MM-DD")
    p.add_argument("--slippage-bps", type=float, help="slippage a side in bps (default %g)" % tm.SLIPPAGE_BPS)
    p.add_argument("--commission", type=float, help="commission a share a side in $ (default %g)" % tm.COMMISSION)
    p.add_argument("--config", default="trader/config.json", help="costs from here when the flags are not given")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"backtest": cmd_backtest, "engine": cmd_engine, "context": cmd_context, "live-context": cmd_live_context,
                "live": cmd_live, "live-review": cmd_live_review}[a.cmd](a)
    except tm.TMError as e:
        print("confluence: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
