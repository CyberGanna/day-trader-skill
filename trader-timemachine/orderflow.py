#!/usr/bin/env python3
"""Order flow lab (trader-timemachine), phase 3 of the confluence plan.

  python orderflow.py download --symbols TSLA,AMD --days 20 --out flusso
  python orderflow.py backtest --symbols TSLA,AMD --flow flusso --offline storico

download reads every trade of the regular session from Alpaca (GET
/v2/stocks/trades, SIP feed, the free plan's history), day by day, and keeps
only what the signals need, one small file a stock a day (<SYM>-flow-<date>.json.gz):
for each 5-minute bar the buying and the selling volume (the tick rule: a trade
above the last different price is a buy, below it a sell) and the footprint
(buying and selling volume at each cent). Busy stocks print hundreds of
thousands of trades a day, so a day takes a minute or more: start with a few
stocks and 20 days.

backtest tests three order flow signals with the confluence lab's exits (stop
1 ATR(14), target 2R, out at 15:55, costs off) beside random entries, first
half of the days against the second:
  cvd_div      a new high of the day while the cumulative delta stays under its
               level at the previous high: sell (the mirror at a new low: buy)
  delta_push   a close above the last hour's high with a delta 1.5 times the
               last hour's average: buy (the mirror below the low: sell)
  absorption   sellers twice the buyers in the bar's lowest quarter, the close in
               its upper half: buy (the mirror at the top: sell)
The 5-minute bars come from the files timemachine.py backtest --save wrote.

Standard library only. GET only. Nothing here places an order.
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
import alpaca_data as ad  # noqa: E402
import confluence as cf  # noqa: E402
import timemachine as tm  # noqa: E402

SIGNALS = ("cvd_div", "delta_push", "absorption")
NAMES = {"cvd_div": "CVD divergence at a new high/low", "delta_push": "breakout with strong delta",
         "absorption": "absorption (footprint)", "random": "random entry"}
LOOKBACK = 12           # bars: the last hour
PUSH, ABSORB = 1.5, 2.0


# ---------------------------------------------------------------- download

def session_bounds(day):
    return ad.eastern_to_epoch(day, 9, 30), ad.eastern_to_epoch(day, 16, 0)


def aggregate(trades, open_epoch, state=None):
    """Adds trades [(epoch seconds, price, size), ...] in time order to the day's
    bars: {slot: {"buy", "sell", "fp": {price in cents: [buy, sell]}}}. state carries
    the tick rule's last price and side from one page to the next."""
    state = state if state is not None else {"bars": {}, "last": None, "side": 0}
    bars = state["bars"]
    for t, p, s in trades:
        slot = int((t - open_epoch) // 300)
        if not 0 <= slot < tm.SESSION_BARS:
            continue
        last = state["last"]
        if last is not None and p != last:
            state["side"] = 1 if p > last else -1
        if last is None or p != last:
            state["last"] = p
        side = state["side"]
        if not side:
            continue
        b = bars.setdefault(slot, {"buy": 0, "sell": 0, "fp": {}})
        cell = b["fp"].setdefault(int(round(p * 100)), [0, 0])
        if side > 0:
            b["buy"] += s
            cell[0] += s
        else:
            b["sell"] += s
            cell[1] += s
    return state


def fetch_day(sym, day):
    """The day's flow, or None when Alpaca has no trades (a holiday)."""
    start, end = session_bounds(day)
    state, token, n = None, None, 0
    while True:
        params = {"symbols": sym, "start": ad._rfc(datetime.datetime.fromtimestamp(start, datetime.timezone.utc)),
                  "end": ad._rfc(datetime.datetime.fromtimestamp(end, datetime.timezone.utc)),
                  "limit": ad.PAGE, "feed": "sip", "sort": "asc"}
        if token:
            params["page_token"] = token
        doc = ad._get("/v2/stocks/trades", params)
        rows = (doc.get("trades") or {}).get(sym) or []
        n += len(rows)
        state = aggregate([(ad._parse_t(r["t"]).timestamp(), float(r["p"]), int(r["s"])) for r in rows
                           if r.get("p") and r.get("s")], start, state)
        token = doc.get("next_page_token")
        if not token:
            break
    if not n:
        return None
    return {"symbol": sym, "date": day.isoformat(), "trades": n,
            "bars": {str(k): {"buy": v["buy"], "sell": v["sell"], "fp": [[c, b, s] for c, (b, s) in sorted(v["fp"].items())]}
                     for k, v in sorted(state["bars"].items())}}


def flow_path(folder, sym, day):
    return os.path.join(folder, "%s-flow-%s.json.gz" % (sym, day))


def past_weekdays(today, n):
    d, out = today, []
    while len(out) < n:
        d -= datetime.timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return sorted(out)


def cmd_download(a):
    if not ad.keys():
        raise tm.TMError("download needs Alpaca's keys: set TRADER_ALPACA_KEY and TRADER_ALPACA_SECRET")
    if not 1 <= a.days <= 250:
        raise tm.TMError("--days must be 1 to 250")
    os.makedirs(a.out, exist_ok=True)
    today = ad.eastern_date(ad._now())
    for sym in tm.backtest_symbols(a):
        for day in past_weekdays(today, a.days):
            path = flow_path(a.out, sym, day.isoformat())
            if os.path.exists(path):
                continue
            try:
                doc = fetch_day(sym, day)
            except ad.AlpacaError as e:
                print("  %s %s: %s" % (sym, day, e))
                continue
            if doc is None:
                print("  %s %s: no trades (market closed)" % (sym, day))
                continue
            with gzip.open(path, "wt", encoding="utf-8") as fh:
                json.dump(doc, fh)
            print("  %s %s: %d trades" % (sym, day, doc["trades"]), flush=True)
    return 0


# ---------------------------------------------------------------- the signals

def load_flow(folder, sym):
    """{date: {slot: {"buy", "sell", "fp": [[cents, buy, sell], ...]}}}."""
    out = {}
    prefix = "%s-flow-" % sym
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if name.startswith(prefix) and name.endswith(".json.gz"):
            with gzip.open(os.path.join(folder, name), "rt", encoding="utf-8") as fh:
                doc = json.load(fh)
            out[doc["date"]] = {int(k): v for k, v in doc["bars"].items()}
    return out


def deltas(F):
    return [F.get(k, {}).get("buy", 0) - F.get(k, {}).get("sell", 0) for k in range(tm.SESSION_BARS)]


def sig_cvd_div(D, F):
    d = deltas(F)
    cvd, run = [], 0
    for x in d:
        run += x
        cvd.append(run)
    hi_k = lo_k = 0
    done = set()
    for k in range(1, cf.LAST_ENTRY + 1):
        if D[k]["h"] > D[hi_k]["h"]:
            if k >= cf.FIRST and cvd[k] < cvd[hi_k] and -1 not in done:
                done.add(-1)
                yield k, -1
            hi_k = k
        if D[k]["l"] < D[lo_k]["l"]:
            if k >= cf.FIRST and cvd[k] > cvd[lo_k] and 1 not in done:
                done.add(1)
                yield k, 1
            lo_k = k


def sig_delta_push(D, F):
    d = deltas(F)
    for k in range(max(cf.FIRST, LOOKBACK), cf.LAST_ENTRY + 1):
        past = D[k - LOOKBACK:k]
        avg = statistics.mean(abs(x) for x in d[k - LOOKBACK:k]) or 1
        if D[k]["c"] > max(b["h"] for b in past) and d[k] > PUSH * avg:
            yield k, 1
        elif D[k]["c"] < min(b["l"] for b in past) and d[k] < -PUSH * avg:
            yield k, -1


def sig_absorption(D, F):
    for k in range(cf.FIRST, cf.LAST_ENTRY + 1):
        b, fb = D[k], F.get(k)
        rng = b["h"] - b["l"]
        if not fb or rng <= 0:
            continue
        low_cut, high_cut = (b["l"] + rng / 4) * 100, (b["h"] - rng / 4) * 100
        lb = sum(x[1] for x in fb["fp"] if x[0] <= low_cut)
        ls = sum(x[2] for x in fb["fp"] if x[0] <= low_cut)
        hb = sum(x[1] for x in fb["fp"] if x[0] >= high_cut)
        hs = sum(x[2] for x in fb["fp"] if x[0] >= high_cut)
        mid = (b["h"] + b["l"]) / 2
        if ls >= ABSORB * max(lb, 1) and b["c"] > mid:
            yield k, 1
        elif hb >= ABSORB * max(hs, 1) and b["c"] < mid:
            yield k, -1


GENERATORS = {"cvd_div": sig_cvd_div, "delta_push": sig_delta_push, "absorption": sig_absorption}


# ---------------------------------------------------------------- the lab

def run_symbol(sym, sessions, flow, costs, target_r=cf.TARGET_R, stop_atr=cf.STOP_ATR, seed=cf.SEED):
    X = cf.indicators(sessions)
    rng = random.Random("%s-%s" % (seed, sym))
    out = {name: [] for name in SIGNALS + ("random",)}
    for i, (d, D) in enumerate(sessions):
        if d not in flow or i == 0:
            continue
        base = i * tm.SESSION_BARS
        rk, rs = rng.randint(cf.FIRST, cf.LAST_ENTRY), rng.choice((1, -1))
        for name in SIGNALS + ("random",):
            fired = [(rk, rs)] if name == "random" else list(GENERATORS[name](D, flow[d]))
            if not fired:
                continue
            k, s = fired[0]
            t = cf.trade(D, k, s, stop_atr * X["atr"][base + k], target_r, costs)
            if t:
                t.update({"symbol": sym, "date": d})
                out[name].append(t)
    return out


def lab(charts, flows, now_utc, costs, target_r=cf.TARGET_R, stop_atr=cf.STOP_ATR, seed=cf.SEED):
    every = {name: [] for name in SIGNALS + ("random",)}
    dates, failed = [], {}
    for sym, text in charts.items():
        flow = flows.get(sym) or {}
        if not flow:
            failed[sym] = "no flow files: run orderflow.py download first"
            continue
        try:
            sessions = cf.sessions_of(text, sym, now_utc)
        except tm.TMError as e:
            failed[sym] = str(e)
            continue
        out = run_symbol(sym, sessions, flow, costs, target_r, stop_atr, seed)
        for name in out:
            every[name] += out[name]
        dates += [d for d, _ in sessions if d in flow]
    if not dates:
        raise tm.TMError("no day has both flow and 5-minute bars: " +
                         ("; ".join("%s: %s" % kv for kv in failed.items()) or "check --flow and --offline"))
    cut = cf.split_date(dates)
    rows = [{"signal": n, "stats": cf.stats(every[n], cut)} for n in SIGNALS + ("random",)]
    base = rows[-1]["stats"]
    for r in rows:
        st = r["stats"]
        r["beats_random"] = (r["signal"] != "random" and None not in (st["year1"], st["year2"], base["year1"], base["year2"])
                             and st["year1"] > base["year1"] and st["year2"] > base["year2"])
    return {"split": cut, "days": len(set(dates)), "costs": dict(costs), "target_r": target_r, "stop_atr": stop_atr,
            "symbols": sorted(s for s in charts if s not in failed), "failed": failed, "rows": rows}


def lab_report(doc):
    out = ["Order flow lab (phase 3): %d stocks, %d days with flow, %s, stop %g ATR, target %gR."
           % (len(doc["symbols"]), doc["days"], tm.cost_words(doc["costs"]), doc["stop_atr"], doc["target_r"]),
           "First half = before %s, second half = from it." % doc["split"], "",
           "  %-34s %6s %8s  %-17s %7s %7s  %-8s %s" % ("signal", "trades", "avg R", "95 % range", "1st", "2nd",
                                                      "verdict", "beats random")]
    for r in doc["rows"]:
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
    out.append("R after costs. A few weeks of flow is a small sample: a YES here is a hint, not an edge.")
    if doc["failed"]:
        out.append("Skipped: " + "; ".join("%s (%s)" % kv for kv in sorted(doc["failed"].items())))
    out.append("Education and tooling, not financial advice. Nothing here places an order.")
    return out


def cmd_backtest(a):
    charts, flows, failed = {}, {}, {}
    for sym in tm.backtest_symbols(a):
        try:
            charts[sym] = tm.read_offline(a.offline, sym)
        except tm.TMError as e:
            failed[sym] = str(e)
            continue
        flows[sym] = load_flow(a.flow, sym)
    doc = lab(charts, flows, tm.parse_now(a.now), tm.load_costs(a), a.target_r, a.stop_atr)
    doc["failed"].update(failed)
    print(json.dumps(doc, indent=2) if a.json else "\n".join(lab_report(doc)))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Order flow lab: trades from Alpaca, delta and footprint signals "
                                             "(trader-timemachine).")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("download", help="every trade of the last N sessions, kept as 5-minute flow files")
    p.add_argument("--symbols", help="tickers, comma separated")
    p.add_argument("--watchlist", default="trader/watchlist.json", help="used when --symbols is not given")
    p.add_argument("--days", type=int, default=20, help="weekdays back from yesterday (20)")
    p.add_argument("--out", default="flusso", help="folder for the flow files (flusso)")
    p = sub.add_parser("backtest", help="test the order flow signals against random entries")
    p.add_argument("--symbols", help="tickers, comma separated")
    p.add_argument("--watchlist", default="trader/watchlist.json", help="used when --symbols is not given")
    p.add_argument("--flow", default="flusso", help="folder the download wrote (flusso)")
    p.add_argument("--offline", required=True, help="folder of <SYM>-2y5m.json.gz files (backtest --save writes them)")
    p.add_argument("--now", help="the time to read the data at, YYYY-MM-DD HH:MM New York (default: now)")
    p.add_argument("--target-r", type=float, default=cf.TARGET_R, help="target in R (%g)" % cf.TARGET_R)
    p.add_argument("--stop-atr", type=float, default=cf.STOP_ATR, help="stop distance in 5-minute ATRs (%g)" % cf.STOP_ATR)
    p.add_argument("--slippage-bps", type=float, help="slippage a side in bps (default %g)" % tm.SLIPPAGE_BPS)
    p.add_argument("--commission", type=float, help="commission a share a side in $ (default %g)" % tm.COMMISSION)
    p.add_argument("--config", default="trader/config.json", help="costs from here when the flags are not given")
    p.add_argument("--json", action="store_true", help="print the numbers as JSON")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return cmd_download(a) if a.cmd == "download" else cmd_backtest(a)
    except (tm.TMError, ad.AlpacaError) as e:
        print("orderflow: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
