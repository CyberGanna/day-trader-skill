#!/usr/bin/env python3
"""Swing lab (trader-timemachine): the confluence signals on daily bars, held for days.

  python swing.py download --symbols AAPL,TSLA,... --years 10 --out giornaliero
  python swing.py backtest --symbols AAPL,TSLA,... --daily giornaliero
  python swing.py backtest --symbols AAPL,TSLA,... --offline storico      (daily bars built from 5-minute ones)
  python swing.py engine   --symbols AAPL,TSLA,... --daily giornaliero

Intraday, every signal was worth about 0 R before costs and lost after them. On
daily bars the stop is wider, so the same costs are a much smaller part of R.

download reads split-adjusted daily bars from Alpaca (GET /v2/stocks/bars,
timeframe 1Day, SIP history from 2016) and keeps one file a stock.

The signals, each read at a day's close from that day and the days before:
  ma        the 20-day EMA crosses the 50-day EMA
  ichimoku  the close crosses out of the cloud (9/26/52), tenkan on the same side of kijun
  breakout  the close passes the highest high (lowest low) of the 20 days before
  pullback  in an uptrend (above the 50-day average, the 20-day rising), the 2-day
            RSI falls under 10; the mirror in a downtrend
  fvg       price comes back into a fair value gap of the last 20 days and closes outside it
  ob        price comes back into an order block of the last 20 days (the last opposite
            day before a displacement day: a body twice the 20-day average, closing past
            the 3 days before) and closes outside it
  sweep     the day trades under the lowest low of the 20 days before and closes back
            above it (the mirror at the highs)

Every signal gets the same exits: in at the next day's open, the stop 2 ATR(14)
away, the target at 2R, else out at the close of the 10th day. A day that touches
both the stop and the target counts as the stop. A stop that gaps fills at the
open. Slippage and commission come off (timemachine's costs); short borrow fees
do not. One trade at a time a signal a stock. "random" enters on a random tenth
of the days with a random side. "With the trend" keeps only the trades in the
daily trend's direction (close against the 50-day average, the 20-day rising or
falling). A signal earns its place only if it beats random in both halves of the
history.

engine counts the signals (and the trend) that agree over the last 3 days, picks
the level on the first half and judges it once on the second half, with the
confluence engine's gate.

Standard library only. Short sales are simulated for the test only; nothing here
places an order.
"""

import argparse
import datetime
import gzip
import json
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import timemachine as tm  # noqa: E402
import confluence as cf  # noqa: E402
import alpaca_data as ad  # noqa: E402

TARGET_R, STOP_ATR, HOLD = 2.0, 2.0, 10
WARMUP = 60                      # days before the first signal: the 52-day cloud and the 50-day average
LOOKBACK = 20
STATE_DAYS = 3                   # a signal's vote lasts 3 days in the engine
SIGNALS = ("ma", "ichimoku", "breakout", "pullback", "fvg", "ob", "sweep")
NAMES = {"ma": "EMA 20/50 cross", "ichimoku": "Ichimoku cloud", "breakout": "20-day breakout",
         "pullback": "pullback in trend (RSI 2)", "fvg": "fair value gap retest", "ob": "order block retest",
         "sweep": "20-day sweep and reclaim", "random": "random entry"}
RANDOM_SHARE = 0.10
VOTE_LEVELS = (2, 3, 4)
MIN_TRAIN = 100
SEED = 7


# ---------------------------------------------------------------- data

def daily_from_sessions(sessions):
    """Daily bars from full 5-minute sessions: [{"d", "o", "h", "l", "c", "v"}, ...]."""
    return [{"d": d, "o": D[0]["o"], "h": max(b["h"] for b in D), "l": min(b["l"] for b in D), "c": D[-1]["c"],
             "v": sum(b.get("v") or 0 for b in D)} for d, D in sessions]


def daily_path(folder, sym):
    return os.path.join(folder, "%s-daily.json.gz" % sym)


def read_daily(folder, sym):
    path = daily_path(folder, sym)
    if not os.path.exists(path):
        raise tm.TMError("no %s: run swing.py download first" % path)
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)["bars"]


def load_daily(a):
    """{SYMBOL: daily bars}, from --daily or from the 5-minute files in --offline."""
    out, failed = {}, {}
    for sym in tm.backtest_symbols(a):
        try:
            if a.daily:
                out[sym] = read_daily(a.daily, sym)
            else:
                text = tm.read_offline(a.offline, sym)
                out[sym] = daily_from_sessions(cf.sessions_of(text, sym, tm.parse_now(a.now)))
        except tm.TMError as e:
            failed[sym] = str(e)
    return out, failed


def cmd_download(a):
    if not ad.keys():
        raise tm.TMError("no Alpaca keys in the environment: set TRADER_ALPACA_KEY and TRADER_ALPACA_SECRET")
    if not 1 <= a.years <= 10:
        raise tm.TMError("--years goes from 1 to 10")
    syms = tm.backtest_symbols(a)
    os.makedirs(a.out, exist_ok=True)
    end = ad._now()
    start = end - datetime.timedelta(days=int(365.25 * a.years))
    got = {}
    for i in range(0, len(syms), 50):
        try:
            got.update(ad.bars(syms[i:i + 50], "1Day", start, end))
        except ad.AlpacaError as e:
            raise tm.TMError(str(e))
    for sym in syms:
        rows = [{"d": ad.eastern_date(ad._parse_t(b["t"])).isoformat(), "o": b["o"], "h": b["h"], "l": b["l"],
                 "c": b["c"], "v": b.get("v", 0)} for b in got.get(sym) or []]
        if not rows:
            print("%-6s no daily bars from Alpaca" % sym)
            continue
        with gzip.open(daily_path(a.out, sym), "wt", encoding="utf-8") as fh:
            json.dump({"symbol": sym, "bars": rows}, fh)
        print("%-6s %d days, %s to %s" % (sym, len(rows), rows[0]["d"], rows[-1]["d"]))
    return 0


# ---------------------------------------------------------------- indicators and signals

def rsi(closes, n=2):
    out, up, dn = [50.0], 0.0, 0.0
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        a = 1.0 / n
        up, dn = up + a * (max(ch, 0) - up), dn + a * (max(-ch, 0) - dn)
        out.append(100.0 if dn == 0 else 100 - 100 / (1 + up / dn))
    return out


def indicators(B):
    closes = [b["c"] for b in B]
    tenkan, kijun, top, bot = cf.ichimoku(B)
    trend = [cf.daily_trend(closes[:i + 1]) for i in range(len(B))]
    return {"e20": cf.ema(closes, 20), "e50": cf.ema(closes, 50), "atr": cf.atr(B), "rsi": rsi(closes),
            "tenkan": tenkan, "kijun": kijun, "top": top, "bot": bot, "trend": trend,
            "body": [abs(b["c"] - b["o"]) for b in B]}


def sig_ma(B, X, i):
    a, b = X["e20"], X["e50"]
    if a[i - 1] <= b[i - 1] and a[i] > b[i]:
        return 1
    if a[i - 1] >= b[i - 1] and a[i] < b[i]:
        return -1
    return 0


def sig_ichimoku(B, X, i):
    top, bot = X["top"], X["bot"]
    if top[i] is None or top[i - 1] is None:
        return 0
    c, p = B[i]["c"], B[i - 1]["c"]
    if p <= top[i - 1] and c > top[i] and X["tenkan"][i] > X["kijun"][i]:
        return 1
    if p >= bot[i - 1] and c < bot[i] and X["tenkan"][i] < X["kijun"][i]:
        return -1
    return 0


def sig_breakout(B, X, i):
    w = B[i - LOOKBACK:i]
    if B[i]["c"] > max(b["h"] for b in w):
        return 1
    if B[i]["c"] < min(b["l"] for b in w):
        return -1
    return 0


def sig_pullback(B, X, i):
    t, r = X["trend"][i], X["rsi"][i]
    if t == 1 and r < 10:
        return 1
    if t == -1 and r > 90:
        return -1
    return 0


def retest(B, i, zones):
    """zones: (day formed, side, low, high). A zone is used once: the first later day
    that trades into it. The signal fires when that day closes back outside on the zone's side."""
    for j, side, lo, hi in reversed(zones):
        if not i - LOOKBACK <= j < i - 1:
            continue
        if any((b["l"] <= hi) if side == 1 else (b["h"] >= lo) for b in B[j + 1:i]):
            continue
        b = B[i]
        if side == 1 and b["l"] <= hi and b["c"] > hi:
            return 1
        if side == -1 and b["h"] >= lo and b["c"] < lo:
            return -1
    return 0


def fvg_zones(B, i):
    out = []
    for j in range(max(2, i - LOOKBACK), i):
        if B[j]["l"] > B[j - 2]["h"]:
            out.append((j, 1, B[j - 2]["h"], B[j]["l"]))
        elif B[j]["h"] < B[j - 2]["l"]:
            out.append((j, -1, B[j]["h"], B[j - 2]["l"]))
    return out


def ob_zones(B, X, i):
    out = []
    for j in range(max(LOOKBACK + 1, i - LOOKBACK), i):
        avg = statistics.mean(X["body"][j - LOOKBACK:j])
        b = B[j]
        if not avg or X["body"][j] < 2 * avg:
            continue
        up = b["c"] > b["o"] and b["c"] > max(x["h"] for x in B[j - 3:j])
        dn = b["c"] < b["o"] and b["c"] < min(x["l"] for x in B[j - 3:j])
        if not (up or dn):
            continue
        k = next((k for k in range(j - 1, j - 6, -1) if (B[k]["c"] < B[k]["o"]) == up), None)
        if k is not None:
            out.append((j, 1 if up else -1, B[k]["l"], B[k]["h"]))
    return out


def sig_fvg(B, X, i):
    return retest(B, i, fvg_zones(B, i))


def sig_ob(B, X, i):
    return retest(B, i, ob_zones(B, X, i))


def sig_sweep(B, X, i):
    w = B[i - LOOKBACK:i]
    lo, hi = min(b["l"] for b in w), max(b["h"] for b in w)
    b = B[i]
    if b["l"] < lo and b["c"] > lo:
        return 1
    if b["h"] > hi and b["c"] < hi:
        return -1
    return 0


GENERATORS = {"ma": sig_ma, "ichimoku": sig_ichimoku, "breakout": sig_breakout, "pullback": sig_pullback,
              "fvg": sig_fvg, "ob": sig_ob, "sweep": sig_sweep}


# ---------------------------------------------------------------- one trade

def trade(B, i, side, atr_now, costs, target_r=TARGET_R, stop_atr=STOP_ATR, hold=HOLD):
    """A signal at day i's close: in at day i+1's open, stop and target day by day, else out
    at the close of day i+hold. Returns the trade and the day it ended, or None."""
    if i + 1 >= len(B):
        return None
    long = side == 1
    e = round(B[i + 1]["o"], 2)
    risk = round(stop_atr * atr_now, 2)
    if risk <= 0:
        return None
    s = round(e - risk, 2) if long else round(e + risk, 2)
    t = round(e + target_r * risk, 2) if long else round(e - target_r * risk, 2)

    def gain(p):
        return ((p - e) if long else (e - p)) / risk
    last = min(i + hold, len(B) - 1)
    out, R, R_fill, end = "close", gain(B[last]["c"]), None, last
    for j in range(i + 1, last + 1):
        b = B[j]
        if (b["l"] <= s) if long else (b["h"] >= s):
            out, R, R_fill, end = "stop", gain(s), gain(tm.stop_fill(s, b["o"], long) if j > i + 1 else s), j
            break
        if (b["h"] >= t) if long else (b["l"] <= t):
            out, R, end = "target", gain(max(t, b["o"]) if long else min(t, b["o"])) if j > i + 1 else gain(t), j
            break
    net = (R if R_fill is None else R_fill) - tm.cost_r(e, risk, costs)
    return {"side": "buy" if long else "sell", "date": B[i + 1]["d"], "time": "09:30", "entry": e, "stop": s,
            "target": t, "outcome": out, "days": end - i, "R": round(R, 3), "R_net": round(net, 3),
            "gapped": R_fill is not None and R_fill < R}, end


# ---------------------------------------------------------------- the lab

def variants(signals=SIGNALS):
    return [(s, ctx) for ctx in (False, True) for s in tuple(signals) + ("random",)]


def fire_all(B, X, signals, rng):
    """{name: {day: side}} for every signal and the random entries, from the warmup on."""
    out = {n: {} for n in tuple(signals) + ("random",)}
    for i in range(WARMUP, len(B) - 1):
        for n in signals:
            s = GENERATORS[n](B, X, i)
            if s:
                out[n][i] = s
        if rng.random() < RANDOM_SHARE:
            out["random"][i] = rng.choice((1, -1))
    return out


def run_symbol(sym, B, costs, signals=SIGNALS, seed=SEED, **exits):
    X = indicators(B)
    fired = fire_all(B, X, signals, random.Random("%s-%s" % (seed, sym)))
    out = {}
    for name, with_ctx in variants(signals):
        trades, free = [], 0
        for i in sorted(fired[name]):
            side = fired[name][i]
            if i < free or (with_ctx and X["trend"][i] != side):
                continue
            r = trade(B, i, side, X["atr"][i], costs, **exits)
            if r:
                t, free = r
                t["symbol"] = sym
                trades.append(t)
        out[(name, with_ctx)] = trades
    return out, X, fired


def lab(daily, costs, signals=SIGNALS, seed=SEED, **exits):
    every = {v: [] for v in variants(signals)}
    per, failed, dates = {}, {}, []
    for sym, B in daily.items():
        if len(B) <= WARMUP + LOOKBACK:
            failed[sym] = "%d days: the lab needs more than %d" % (len(B), WARMUP + LOOKBACK)
            continue
        out, _, _ = run_symbol(sym, B, costs, signals, seed, **exits)
        for v in out:
            every[v] += out[v]
        per[sym] = {"days": len(B), "first": B[0]["d"], "last": B[-1]["d"]}
        dates += [B[WARMUP]["d"], B[-1]["d"]]
    if not per:
        raise tm.TMError("no stock had enough history: " + "; ".join("%s: %s" % kv for kv in failed.items()))
    cut = cf.split_date(dates)
    rows = [{"signal": n, "context": c, "stats": cf.stats(every[(n, c)], cut)} for n, c in variants(signals)]
    for r in rows:
        tr = every[(r["signal"], r["context"])]
        r["buys"] = round(statistics.mean(t["R_net"] for t in tr if t["side"] == "buy"), 3) if any(
            t["side"] == "buy" for t in tr) else None
        r["sells"] = round(statistics.mean(t["R_net"] for t in tr if t["side"] == "sell"), 3) if any(
            t["side"] == "sell" for t in tr) else None
    base = {r["context"]: r["stats"] for r in rows if r["signal"] == "random"}
    for r in rows:
        b, st = base[r["context"]], r["stats"]
        r["beats_random"] = (r["signal"] != "random" and None not in (st["year1"], st["year2"], b["year1"], b["year2"])
                             and min(st["year1_trades"], st["year2_trades"]) >= tm.MIN_TRADES
                             and st["year1"] > b["year1"] and st["year2"] > b["year2"])
    return {"split": cut, "exits": dict({"target_r": TARGET_R, "stop_atr": STOP_ATR, "hold": HOLD}, **exits),
            "costs": dict(costs), "symbols": per, "failed": failed, "rows": rows}


def lab_report(doc):
    x = doc["exits"]
    out = ["Swing lab: %d stocks, %s, in at the next open, stop %g ATR(14), target %gR, out after %d days."
           % (len(doc["symbols"]), tm.cost_words(doc["costs"]), x["stop_atr"], x["target_r"], x["hold"]),
           "First half = before %s, second half = from it." % doc["split"], ""]
    head = "  %-28s %6s %8s %8s  %-17s %7s %7s %7s %7s  %-8s %s" % (
        "signal", "trades", "gross R", "net R", "95 % range", "half 1", "half 2", "buys", "sells", "verdict",
        "beats random")
    for with_ctx in (False, True):
        out.append("Alone (buys and sells):" if not with_ctx else "With the daily trend (its direction only):")
        out.append(head)
        for r in doc["rows"]:
            if r["context"] != with_ctx:
                continue
            st = r["stats"]
            if not st["trades"]:
                out.append("  %-28s %6d" % (NAMES[r["signal"]], 0))
                continue
            lo, hi = st["ci95_net"]
            f = lambda v: "-" if v is None else "%+.3f" % v  # noqa: E731
            out.append("  %-28s %6d %+8.3f %+8.3f  %+.3f to %+.3f %7s %7s %7s %7s  %-8s %s" % (
                NAMES[r["signal"]], st["trades"], st["avg_R"], st["avg_R_net"], lo, hi, f(st["year1"]),
                f(st["year2"]), f(r["buys"]), f(r["sells"]), tm.short_verdict(st),
                "-" if r["signal"] == "random" else "YES" if r["beats_random"] else "no"))
        out.append("")
    keep = ["%s%s" % (NAMES[r["signal"]], " + trend" if r["context"] else "") for r in doc["rows"] if r["beats_random"]]
    out.append("Beat random in both halves: %s." % (", ".join(keep) if keep else "none"))
    out.append("%d variants on the same data: a YES is a candidate, not an edge. An edge needs its own 95 %% range "
               "above zero too. Shorts pay no borrow fee here." % len(doc["rows"]))
    if doc["failed"]:
        out.append("Skipped: " + "; ".join("%s (%s)" % kv for kv in sorted(doc["failed"].items())))
    out.append("Education and tooling, not financial advice. Nothing here places an order.")
    return out


# ---------------------------------------------------------------- the engine

def votes(fired, X, i):
    """Net votes per side over the last STATE_DAYS days, the trend included."""
    up = dn = 0
    for name, days in fired.items():
        if name == "random":
            continue
        last = next((days[j] for j in range(i, i - STATE_DAYS, -1) if j in days), 0)
        up, dn = up + (last == 1), dn + (last == -1)
    t = X["trend"][i]
    return up + (t == 1), dn + (t == -1)


def engine_trades(sym, B, costs, level, signals=SIGNALS, **exits):
    X = indicators(B)
    fired = fire_all(B, X, signals, random.Random(0))
    trades, free = [], 0
    for i in range(WARMUP, len(B) - 1):
        if i < free:
            continue
        up, dn = votes(fired, X, i)
        side = 1 if up >= level and up > dn else -1 if dn >= level and dn > up else 0
        if not side:
            continue
        r = trade(B, i, side, X["atr"][i], costs, **exits)
        if r:
            t, free = r
            t["symbol"] = sym
            trades.append(t)
    return trades


def engine(daily, costs, signals=SIGNALS, **exits):
    usable = {s: B for s, B in daily.items() if len(B) > WARMUP + LOOKBACK}
    if not usable:
        raise tm.TMError("no stock had enough history")
    cut = cf.split_date([d for B in usable.values() for d in (B[WARMUP]["d"], B[-1]["d"])])
    rows = []
    for lv in VOTE_LEVELS:
        tr = [t for s, B in usable.items() for t in engine_trades(s, B, costs, lv, signals, **exits)]
        rows.append({"level": lv, "train": [t for t in tr if t["date"] < cut], "test": [t for t in tr if t["date"] >= cut]})
    ok = [r for r in rows if len(r["train"]) >= MIN_TRAIN]
    best = max(ok, key=lambda r: cf.avg_net(r["train"])) if ok else None
    doc = {"split": cut, "costs": dict(costs), "symbols": sorted(usable), "levels": [
        {"level": r["level"], "train_trades": len(r["train"]), "train_avg": cf.avg_net(r["train"]),
         "test_trades": len(r["test"]), "test_avg": cf.avg_net(r["test"])} for r in rows], "picked": None}
    if best:
        st = tm.backtest_stats(sorted(best["test"], key=lambda t: t["date"]))
        doc["picked"] = {"level": best["level"], "test": st, "passes": cf.gate(st)}
    return doc


def engine_report(doc):
    out = ["Swing engine: %d stocks, %s. Votes last %d days; the daily trend votes too."
           % (len(doc["symbols"]), tm.cost_words(doc["costs"]), STATE_DAYS),
           "Level picked on the first half (before %s), judged once on the second." % doc["split"], "",
           "  level  half-1 trades  half-1 avg R  half-2 trades  half-2 avg R"]
    f = lambda v: "-" if v is None else "%+.3f" % v  # noqa: E731
    for r in doc["levels"]:
        out.append("  %5d  %13d  %12s  %13d  %12s" % (r["level"], r["train_trades"], f(r["train_avg"]),
                                                     r["test_trades"], f(r["test_avg"])))
    p = doc["picked"]
    out.append("")
    if not p:
        out.append("No level had %d trades in the first half: nothing to judge." % MIN_TRAIN)
    else:
        st = p["test"]
        out.append("Picked level %d. Second half: %d trades, %s R after costs (95 %% range %s to %s): %s."
                   % (p["level"], st["trades"], f(st.get("avg_R_net")), f(st.get("ci95_net", [None])[0]),
                      f(st.get("ci95_net", [None, None])[1]), "PASSES the gate" if p["passes"] else "fails the gate"))
    out.append("The gate: at least %d trades, the 95 %% range above zero, both halves positive." % cf.GATE_TRADES)
    out.append("Education and tooling, not financial advice. Nothing here places an order.")
    return out


# ---------------------------------------------------------------- command line

def exits_of(a):
    if not a.target_r > 0 or not a.stop_atr > 0 or not 1 <= a.hold <= 60:
        raise tm.TMError("--target-r and --stop-atr must be more than 0, --hold from 1 to 60 days")
    return {"target_r": a.target_r, "stop_atr": a.stop_atr, "hold": a.hold}


def cmd_backtest(a):
    exits = exits_of(a)
    daily, failed = load_daily(a)
    doc = lab(daily, tm.load_costs(a), SIGNALS, a.seed, **exits)
    doc["failed"].update(failed)
    print(json.dumps(doc, indent=2) if a.json else "\n".join(lab_report(doc)))
    return 0


def cmd_engine(a):
    exits = exits_of(a)
    daily, failed = load_daily(a)
    doc = engine(daily, tm.load_costs(a), SIGNALS, **exits)
    doc["failed"] = failed
    if a.json:
        print(json.dumps(doc, indent=2))
    else:
        print("\n".join(engine_report(doc)))
        if failed:
            print("Skipped: " + "; ".join("%s (%s)" % kv for kv in sorted(failed.items())))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Swing lab: the confluence signals on daily bars (trader-timemachine).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("download", help="daily bars from Alpaca, one file a stock")
    p.add_argument("--symbols", help="tickers, comma separated")
    p.add_argument("--watchlist", default="trader/watchlist.json", help="used when --symbols is not given")
    p.add_argument("--years", type=int, default=10, help="years of history, 1 to 10 (10)")
    p.add_argument("--out", default="giornaliero", help="folder to write <SYM>-daily.json.gz into (giornaliero)")
    for name, help_ in (("backtest", "every signal alone and with the trend, against random"),
                        ("engine", "count the agreeing signals, fitted on the first half")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--symbols", help="tickers, comma separated")
        p.add_argument("--watchlist", default="trader/watchlist.json", help="used when --symbols is not given")
        src = p.add_mutually_exclusive_group(required=True)
        src.add_argument("--daily", help="the folder swing.py download wrote")
        src.add_argument("--offline", help="a folder of 5-minute files (backtest --save): daily bars are built from them")
        p.add_argument("--now", help="the time to read the 5-minute data at, YYYY-MM-DD HH:MM New York (default: now)")
        p.add_argument("--target-r", type=float, default=TARGET_R, help="target in R (%g)" % TARGET_R)
        p.add_argument("--stop-atr", type=float, default=STOP_ATR, help="stop distance in daily ATRs (%g)" % STOP_ATR)
        p.add_argument("--hold", type=int, default=HOLD, help="days at most in a trade (%d)" % HOLD)
        p.add_argument("--slippage-bps", type=float, help="slippage a side in bps (default %g)" % tm.SLIPPAGE_BPS)
        p.add_argument("--commission", type=float, help="commission a share a side in $ (default %g)" % tm.COMMISSION)
        p.add_argument("--config", default="trader/config.json", help="costs from here when the flags are not given")
        p.add_argument("--seed", type=int, default=SEED, help="seed of the random entries (%d)" % SEED)
        p.add_argument("--json", action="store_true", help="print the numbers as JSON")
    a = ap.parse_args(argv)
    try:
        return {"download": cmd_download, "backtest": cmd_backtest, "engine": cmd_engine}[a.cmd](a)
    except tm.TMError as e:
        print("swing: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
