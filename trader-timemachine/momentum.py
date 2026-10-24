#!/usr/bin/env python3
"""Momentum lab (trader-timemachine): hold last year's winners, rebalanced once a month.

  python momentum.py backtest --watchlist universo-2016.json --daily giornaliero [--top 10]

At each month's last close it ranks the stocks by their return over the 12 months
before, leaving out the last month (the "12-1" momentum of the research), and holds
the --top best ones in equal weights until the next month's last close. Costs:
slippage on every dollar bought or sold at a rebalance (timemachine's slippage
bps). A stock whose bars stop (delisted, bought out) leaves at its last close and
its money stays in cash until the next rebalance.

Beside it, on the same days:
  SPY            bought once and held
  equal weight   every stock of the list, rebalanced monthly the same way
  random         --top stocks picked at random each month, 200 times: momentum
                 counts only if it beats most of these
  + trend        the same momentum, in cash while SPY closes under its 200-day average

Prices are split-adjusted, not dividend-adjusted, for every line alike.
Use a list picked without hindsight (swing.py universe): stocks picked today
make any rule that buys winners look better than it was.

Standard library only. Nothing here places an order.
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
import swing as sw  # noqa: E402

TOP = 10
LOOKBACK, SKIP = 252, 21          # trading days: 12 months, leaving out the last one
TREND_DAYS = 200
RANDOM_RUNS = 200
SEED = 7


def month_ends(dates):
    """Indexes of the last trading day of each month."""
    return [i for i in range(len(dates) - 1) if dates[i][:7] != dates[i + 1][:7]] + [len(dates) - 1]


def closes_on(calendar, bars):
    """The stock's close on each calendar day, None before its first bar and after its last."""
    by = {b["d"]: b["c"] for b in bars}
    out, last, first, end = [], None, bars[0]["d"], bars[-1]["d"]
    for d in calendar:
        if d < first or d > end:
            out.append(None)
            continue
        last = by.get(d, last)
        out.append(last)
    return out


def score(px, i):
    """12-1 momentum at day i, or None when the stock lacks the history."""
    a, b = i - LOOKBACK, i - SKIP
    if a < 0 or px[a] is None or px[b] is None or px[i] is None or px[a] <= 0:
        return None
    return px[b] / px[a] - 1


def run(px, ends, pick, cost_bps, cash_when=None):
    """Monthly equity curve. pick(i) -> the symbols to hold from month end i."""
    eq, held, curve = 1.0, {}, []
    for k in range(len(ends) - 1):
        i, j = ends[k], ends[k + 1]
        want = [] if cash_when and cash_when(i) else pick(i)
        new = {s: 1.0 / len(want) for s in want} if want else {}
        turnover = sum(abs(new.get(s, 0) - held.get(s, 0)) for s in set(new) | set(held))
        eq *= 1 - turnover * cost_bps / 10000.0
        r = 0.0
        for s, w in new.items():
            p0, p1 = px[s][i], px[s][j]
            if p1 is None:                       # stopped trading during the month: out at its last close
                p1 = next((px[s][t] for t in range(j, i, -1) if px[s][t] is not None), p0)
            r += w * (p1 / p0 - 1)
        eq *= 1 + r
        held = new
        curve.append(eq)
    return curve


def summary(curve, dates):
    """CAGR, volatility, Sharpe (no risk-free rate), worst drawdown, from month-end equity."""
    if len(curve) < 13:
        return None
    rets = [b / a - 1 for a, b in zip([1.0] + curve, curve)]
    years = len(curve) / 12.0
    cagr = curve[-1] ** (1 / years) - 1
    vol = statistics.stdev(rets) * math.sqrt(12)
    peak = dd = 0.0
    for e in [1.0] + curve:
        peak = max(peak, e)
        dd = max(dd, 1 - e / peak)
    half = len(curve) // 2
    first = curve[half - 1] ** (12.0 / half) - 1
    second = (curve[-1] / curve[half - 1]) ** (12.0 / (len(curve) - half)) - 1
    return {"cagr": round(cagr, 4), "vol": round(vol, 4), "sharpe": round(statistics.mean(rets) * 12 / vol, 2) if vol else None,
            "max_dd": round(dd, 4), "final": round(curve[-1], 3), "half1": round(first, 4), "half2": round(second, 4)}


def lab(daily, spy, top=TOP, cost_bps=None, runs=RANDOM_RUNS, seed=SEED):
    cost_bps = tm.SLIPPAGE_BPS if cost_bps is None else cost_bps
    cal = [b["d"] for b in spy]
    px = {s: closes_on(cal, B) for s, B in daily.items() if B}
    spx = [b["c"] for b in spy]
    ends = [i for i in month_ends(cal) if i >= LOOKBACK]
    if len(ends) < 14:
        raise tm.TMError("%d months after the first year: the lab needs at least 14" % len(ends))

    def alive(i):
        return [s for s in px if px[s][i] is not None and score(px[s], i) is not None]

    def mom(i):
        ranked = sorted(((score(px[s], i), s) for s in alive(i)), reverse=True)
        return [s for _, s in ranked[:top]]

    def bear(i):
        return i >= TREND_DAYS and spx[i] < statistics.mean(spx[i - TREND_DAYS + 1:i + 1])

    dates = [cal[i] for i in ends[1:]]
    lines = {"momentum": run(px, ends, mom, cost_bps),
             "momentum + trend": run(px, ends, mom, cost_bps, bear),
             "equal weight": run(px, ends, alive, cost_bps),
             "SPY": [spx[j] / spx[ends[0]] for j in ends[1:]]}
    out = {name: summary(c, dates) for name, c in lines.items()}
    rng = random.Random(seed)
    rand = []
    for _ in range(runs):
        c = run(px, ends, lambda i: rng.sample(alive(i), min(top, len(alive(i)))), cost_bps)
        rand.append(summary(c, dates)["cagr"])
    rand.sort()
    m = out["momentum"]["cagr"]
    return {"from": cal[ends[0]], "to": cal[ends[-1]], "months": len(ends) - 1, "stocks": len(px), "top": top,
            "cost_bps": cost_bps, "lines": out,
            "random": {"runs": runs, "median_cagr": round(statistics.median(rand), 4),
                       "p5": round(rand[int(0.05 * runs)], 4), "p95": round(rand[int(0.95 * runs) - 1], 4),
                       "momentum_beats_share": round(sum(1 for x in rand if m > x) / runs, 3)},
            "holdings_now": mom(len(cal) - 1)}


def report(doc):
    out = ["Momentum lab: %d stocks, the %d with the best 12-1 month return, rebalanced monthly, %g bps a side."
           % (doc["stocks"], doc["top"], doc["cost_bps"]),
           "From %s to %s (%d months). Split-adjusted prices, no dividends." % (doc["from"], doc["to"], doc["months"]), "",
           "  %-18s %8s %8s %7s %9s %10s %10s" % ("line", "a year", "swings", "Sharpe", "worst DD", "1st half", "2nd half")]
    for name, st in doc["lines"].items():
        out.append("  %-18s %+7.1f%% %7.1f%% %7s %8.1f%% %+9.1f%% %+9.1f%%" % (
            name, 100 * st["cagr"], 100 * st["vol"], st["sharpe"], 100 * st["max_dd"], 100 * st["half1"], 100 * st["half2"]))
    r = doc["random"]
    out += ["", "Random %d stocks a month, %d runs: median %+.1f%% a year (5-95 %%: %+.1f%% to %+.1f%%). "
            "Momentum beats %.0f %% of them." % (doc["top"], r["runs"], 100 * r["median_cagr"], 100 * r["p5"],
                                                  100 * r["p95"], 100 * r["momentum_beats_share"]),
            "Momentum would hold now: %s." % ", ".join(doc["holdings_now"]),
            "Past data only. Education and tooling, not financial advice. Nothing here places an order."]
    return out


def cmd_backtest(a):
    if not 1 <= a.top <= 50:
        raise tm.TMError("--top goes from 1 to 50")
    daily = {}
    for sym in tm.backtest_symbols(a):
        try:
            daily[sym] = sw.first_run(sw.read_daily(a.daily, sym))
        except tm.TMError:
            continue
    if not daily:
        raise tm.TMError("no daily files in %s: run swing.py download first" % a.daily)
    spy = sw.read_daily(a.daily, a.benchmark)
    doc = lab(daily, spy, a.top, a.slippage_bps, a.runs, a.seed)
    print(json.dumps(doc, indent=2) if a.json else "\n".join(report(doc)))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Momentum lab: last year's winners, rebalanced monthly (trader-timemachine).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("backtest", help="momentum against SPY, equal weight and random picks")
    p.add_argument("--symbols", help="tickers, comma separated")
    p.add_argument("--watchlist", default="trader/watchlist.json", help="used when --symbols is not given")
    p.add_argument("--daily", required=True, help="the folder swing.py download wrote")
    p.add_argument("--benchmark", default="SPY", help="the benchmark's file in --daily, and the trend filter (SPY)")
    p.add_argument("--top", type=int, default=TOP, help="stocks held (%d)" % TOP)
    p.add_argument("--slippage-bps", type=float, help="slippage a side in bps (default %g)" % tm.SLIPPAGE_BPS)
    p.add_argument("--runs", type=int, default=RANDOM_RUNS, help="random portfolios (%d)" % RANDOM_RUNS)
    p.add_argument("--seed", type=int, default=SEED, help="seed of the random portfolios (%d)" % SEED)
    p.add_argument("--json", action="store_true", help="print the numbers as JSON")
    a = ap.parse_args(argv)
    try:
        return {"backtest": cmd_backtest}[a.cmd](a)
    except tm.TMError as e:
        print("momentum: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
