#!/usr/bin/env python3
"""Time Machine for trader-timemachine. Standard library only.

Replays a planned day trade before you risk a dollar: 10,000 simulated runs of
the rest of the session from your entry, plus the same rule on every past
session in the data.

    python3 timemachine.py run                          # trader/plan.json, 10,000 replays, seed 19
    python3 timemachine.py run --write                  # also save trader/timemachine.json
    python3 timemachine.py run --until 15:55            # stop each replay at your flat-by time
    python3 timemachine.py run --seed 7 --replays 20000 --json
    python3 timemachine.py run --symbol TSLA --date 2026-10-19 --entry 364.18 \\
        --stop 358.32 --target 370.04 --entry-time "10:15 AM"
    python3 timemachine.py chart                        # ASCII fan chart of the replays
    python3 timemachine.py chart --from trader/timemachine.json

How the replays work (the reel's sim.py, same algorithm, same random draws):
  1. Fetch 60 days of 5-minute bars (Yahoo v8 chart, range=60d interval=5m;
     one request). Keep only FULL sessions before the plan's date: 78 bars,
     9:30 to 4:00 New York time. Half days, gaps and today are left out.
  2. The pool: for every bar slot 1..77 of the day, each past session's bar as
     ratios to the previous bar's close: (open, high, low, close) / prev close.
  3. One replay starts at the entry price and walks the rest of the session.
     For each bar i it picks a slot j = i + randint(-window, window), kept in
     1..77, then one past bar from pool[j] (random.choice), and scales it to
     the current price. The stop is checked before the target in every bar,
     so a bar that touches both counts as the stop (the conservative call).
  4. Outcomes: "target" or "stop" (whichever was touched first), or "neither"
     when the trade is still open at --until (default 16:00, the close, or
     13:00 on an NYSE early-close day).
     "green" = target first, or above the entry at --until (below for a sell).
     R = the target's R at the target, -1 at the stop, otherwise
     (price at --until - entry) / risk. A gap through the stop still counts
     -1R here; a real fill can be worse.
  5. The price at --until: median and the 5th to 95th percentile of all
     replays (pct(a, q) = sorted a at index int(q * n)). The fan chart uses a
     second run of 2,000 paths with the same seed. The histogram has 24 bins
     between the 1st and 99th percentile.
  6. History: the plan's own rule (an opening-range breakout with the same
     opening range, stop rule and target R) re-run on every one of those past
     full sessions, first close beyond the range = entry, same stop-first rule.

The verdict line looks at the replays that hit one side (target or stop) and
compares the target's share of them, target / (target + stop), with the share
that breaks even at the plan's target R: 1 / (1 + R), so 50 % at 1R and 33 % at
2R. The gap, in percentage points:
  less than 5 points    "Close to a coin flip" (1R) / "Close to break-even" (other R)
  5 to 15 points        "Leans toward the target" / "Leans toward the stop"
  15 points or more     "Strongly toward the target" / "Strongly toward the stop"
Below it: a note when more than 40 % of the replays hit neither (the price at
--until decides those), and the average R when its sign goes against the lean.

5-minute bars only: Yahoo keeps 60 days of 5-min bars but only about 7 days of
1-min bars, too few sessions for a fair pool. The run refuses with a plain
message when there are fewer than 20 full sessions or the entry bar is not in
the data. Same plan, same data and same --seed give the same numbers on any
machine. A simulation built from the stock's own recent bars is not a
prediction: earnings, news and halts can do what the last 60 days never did.

Data: https://query2.finance.yahoo.com/v8/finance/chart/<T>?range=60d&interval=5m
(unofficial and free, can lag; falls back to query1 on 429 or 5xx).
--offline FILE reads a saved chart JSON (.json or .json.gz) instead.
Times are New York (ET) wall-clock; --now "2026-10-19 10:15" pins the clock.
"""

import argparse
import datetime
import gzip
import importlib.util
import json
import math
import os
import random
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
PAUSE = 0.5          # seconds between requests: be polite to free endpoints
BACKOFF = 5.0        # seconds to wait once after a 429
HOSTS = ("query2", "query1")
INTERVAL = 300       # 5-minute bars, in seconds
SESSION_BARS = 78    # 9:30 to 16:00 in 5-minute bars
OPEN_MIN = 9 * 60 + 30
CLOSE_MIN = 16 * 60
MIN_SESSIONS = 20
SAMPLE_PATHS = 160
BAND_PATHS = 2000
BAND_Q = (0.05, 0.25, 0.5, 0.75, 0.95)
HIST_BINS = 24
COIN_FLIP = 0.05     # verdict thresholds, see the docstring
STRONG = 0.15
MANY_NEITHER = 0.40
EPOCH = datetime.datetime(1970, 1, 1)
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
NYSE_EARLY_CLOSE = {  # 1:00 p.m. ET, from https://www.nyse.com/markets/hours-calendars (read 2026-10-13)
    "2026-11-27": "day after Thanksgiving",
    "2026-12-24": "Christmas Eve",
    "2027-11-26": "day after Thanksgiving",
}
EARLY_MIN = 13 * 60
SLIPPAGE_BPS = 2.0   # default slippage a side, in basis points of the entry: a liquid stock, a market order
COMMISSION = 0.0     # default commission a share, each side (Alpaca: 0; IBKR Pro fixed: about 0.005)
MIN_TRADES = 30      # the backtest gives no verdict below this many trades


class TMError(Exception):
    pass


def _load_alpaca():
    """alpaca_data.py next to this script: Alpaca's bars in Yahoo's shape. None if it is missing."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alpaca_data.py")
    if not os.path.exists(path):
        return None
    spec = importlib.util.spec_from_file_location("alpaca_data", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ALPACA = _load_alpaca()


def data_source():
    """'alpaca' or 'yahoo' (see alpaca_data.py: TRADER_DATA, or Alpaca when its keys are set)."""
    return ALPACA.source() if ALPACA else "yahoo"


def alpaca_answer(path, error):
    """Yahoo's JSON text for a chart or spark path, built from Alpaca's data."""
    try:
        return ALPACA.answer(path)
    except ALPACA.AlpacaError as e:
        raise error(str(e))


# ---------------------------------------------------------------- time (copied from trader-alerts)

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def eastern_offset(utc):
    """Hours from UTC for New York: -4 in daylight time, -5 otherwise.
    DST runs from 2:00 a.m. on the second Sunday of March to 2:00 a.m. on the
    first Sunday of November (US rule since 2007)."""
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))   # 2:00 EST
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))    # 2:00 EDT
    naive = utc.replace(tzinfo=None)
    return -4 if start <= naive < end else -5


def to_eastern(utc):
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=eastern_offset(utc))


def eastern_to_utc(local):
    for off in (-4, -5):
        utc = local - datetime.timedelta(hours=off)
        if eastern_offset(utc) == off:
            return utc
    return local + datetime.timedelta(hours=5)


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as New York time."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise TMError("--now wants a time like \"2026-10-19 10:15\" (New York) or ...Z (UTC)")
    if dt.tzinfo is None:
        return eastern_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def utc_of(ts):
    return EPOCH + datetime.timedelta(seconds=int(ts))


def parse_clock(text, what):
    """'10:15 AM', '10:15am', '15:55' or '3:55' (a bare 1 to 8 o'clock means p.m.) -> minutes after midnight."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*(?:([AaPp])\.?\s*[Mm]\.?)?\s*$", str(text or ""))
    if not m:
        raise TMError("%s wants a New York time like 10:15 AM or 15:55, not %r" % (what, text))
    h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if mi > 59 or h > 23 or (ap and not 1 <= h <= 12):
        raise TMError("%s %r is not a clock time" % (what, text))
    if ap == "p" and h != 12:
        h += 12
    elif ap == "a" and h == 12:
        h = 0
    elif not ap and 1 <= h <= 8:
        h += 12
    return h * 60 + mi


def clock12(minutes):
    h, m = divmod(minutes, 60)
    return "%d:%02d %s" % (h % 12 or 12, m, "AM" if h < 12 else "PM")


def clock24(minutes):
    return "%02d:%02d" % divmod(minutes, 60)


# ---------------------------------------------------------------- data

RANGES = ("60d", "6mo", "1y", "2y")   # 5-minute history: Yahoo keeps 60d, Alpaca years


def chart_url(sym, host="query2", rng="60d"):
    return "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=5m" % (
        host, urllib.parse.quote(sym.upper().replace(".", "-")), rng)


def _sleep(seconds):
    time.sleep(seconds)


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch_chart(sym, rng="60d"):
    """One request for 60 days of 5-min bars. query2 first, one retry after a 429,
    then query1 on 429 or 5xx. With Alpaca as the source, one Alpaca request instead,
    and a longer --range (6mo, 1y, 2y) too."""
    if data_source() == "alpaca":
        return alpaca_answer(chart_url(sym, rng=rng), TMError)
    if rng != "60d":
        raise TMError("Yahoo keeps 60 days of 5-minute bars: a %s range needs Alpaca's data "
                      "(set TRADER_ALPACA_KEY and TRADER_ALPACA_SECRET)" % rng)
    last = None
    for n, host in enumerate(HOSTS):
        if n:
            _sleep(PAUSE)
        for attempt in (0, 1):
            url = chart_url(sym, host)
            try:
                return _get(url)
            except urllib.error.HTTPError as e:
                last = e.code
                try:
                    e.close()
                except Exception:
                    pass
                if e.code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if e.code == 429 or e.code >= 500:
                    break
                if e.code == 404:
                    raise TMError("Yahoo has no chart for %s (HTTP 404): check the ticker" % sym.upper())
                raise TMError("Yahoo answered HTTP %d for %s" % (e.code, sym.upper()))
            except urllib.error.URLError as e:
                raise TMError("could not reach Yahoo: %s" % e.reason)
            except OSError as e:
                raise TMError("could not reach Yahoo: %s" % e)
    if last == 429:
        raise TMError("Yahoo is rate limiting (HTTP 429). Wait a minute and run it again.")
    raise TMError("Yahoo answered HTTP %s for %s on both hosts. Try again later." % (last, sym.upper()))


def read_offline(path, sym):
    """A saved Yahoo chart file (.json or .json.gz), or a folder holding <SYM>-<range>5m.json(.gz)
    (60d first, then the longer ranges that backtest --save writes)."""
    if os.path.isdir(path):
        for name in ["%s-%s5m.json%s" % (sym.upper(), r, gz) for r in RANGES for gz in ("", ".gz")]:
            if os.path.exists(os.path.join(path, name)):
                path = os.path.join(path, name)
                break
        else:
            raise TMError("no %s-60d5m.json(.gz) (or a longer range) in %s" % (sym.upper(), path))
    if not os.path.exists(path):
        raise TMError("no offline chart file %s" % path)
    opener = gzip.open if path.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    except OSError as e:
        raise TMError("could not read %s: %s" % (path, e))


def chart_result(text, sym):
    try:
        data = json.loads(text)
    except ValueError:
        raise TMError("Yahoo returned something that is not JSON for %s" % sym.upper())
    chart = data.get("chart") if isinstance(data, dict) else None
    if not isinstance(chart, dict):
        raise TMError("Yahoo returned something unexpected for %s" % sym.upper())
    res = chart.get("result")
    if not res:
        err = chart.get("error") or {}
        raise TMError("Yahoo has no data for %s: %s" % (sym.upper(), err.get("description") or "unknown ticker"))
    return res[0]


def parse_bars(result, now_utc):
    """Yahoo chart result -> (meta, bars, dropped). Regular-session 5-min bars only,
    oldest first. Dropped: bars with a missing price, bars off the 5-minute grid
    (the live tick, e.g. 10:49:45), bars not complete yet (now < start + 5 min,
    where now is the earlier of --now and the data's own regularMarketTime), and
    bars outside 9:30 to 16:00 New York time."""
    meta = result.get("meta") or {}
    stamps = result.get("timestamp") or []
    q = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}
    cols = [q.get(k) or [] for k in ("open", "high", "low", "close", "volume")]
    as_of = now_utc
    rmt = meta.get("regularMarketTime")
    if isinstance(rmt, (int, float)) and utc_of(rmt) < as_of:
        as_of = utc_of(rmt)
    dropped = {"no_price": 0, "off_grid": 0, "not_complete": 0, "outside_hours": 0}
    bars = {}
    for i, ts in enumerate(stamps):
        o, h, l, c, v = [col[i] if i < len(col) else None for col in cols]
        if ts is None or None in (o, h, l, c):
            dropped["no_price"] += 1
            continue
        ts = int(ts)
        if ts % INTERVAL:
            dropped["off_grid"] += 1
            continue
        start = utc_of(ts)
        local = to_eastern(start)
        mins = local.hour * 60 + local.minute - OPEN_MIN
        if mins < 0 or mins >= SESSION_BARS * 5:
            dropped["outside_hours"] += 1
            continue
        if start + datetime.timedelta(seconds=INTERVAL) > as_of:
            dropped["not_complete"] += 1
            continue
        bars[ts] = {"date": local.date().isoformat(), "slot": mins // 5, "start": local.strftime("%H:%M"),
                    "o": o, "h": h, "l": l, "c": c, "v": v or 0}
    return meta, [bars[t] for t in sorted(bars)], dropped


def split_sessions(bars):
    """{date: {slot: bar}}."""
    days = {}
    for b in bars:
        days.setdefault(b["date"], {})[b["slot"]] = b
    return days


def full_sessions(days, before):
    """Full sessions (all 78 bars) strictly before the plan's date, oldest first,
    and the short ones that were left out (half days, gaps)."""
    full, short = [], []
    for d in sorted(days):
        if d >= before:
            continue
        s = days[d]
        if len(s) == SESSION_BARS:
            full.append((d, [s[i] for i in range(SESSION_BARS)]))
        else:
            short.append({"date": d, "bars": len(s)})
    return full, short


# ---------------------------------------------------------------- the simulation (sim.py)

def build_pool(sessions):
    """pool[i] = every past session's bar i as (o, h, l, c) / bar i-1's close, in date order."""
    pool = [[] for _ in range(SESSION_BARS)]
    for _, D in sessions:
        for i in range(1, SESSION_BARS):
            pc = D[i - 1]["c"]
            pool[i].append((D[i]["o"] / pc, D[i]["h"] / pc, D[i]["l"] / pc, D[i]["c"] / pc))
    return pool


def pct(a, q):
    return a[min(len(a) - 1, int(q * len(a)))]


def cost_r(entry, risk, costs):
    """The round trip's costs in R: slippage on the way in and on the way out
    (bps of the entry, each side) plus the commission per share, each side."""
    if not costs:
        return 0.0
    per_share = 2 * entry * costs.get("slippage_bps", 0) / 10000.0 + 2 * costs.get("commission_per_share", 0)
    return per_share / risk


def stop_fill(stop, bar_open, long):
    """Where a stop really fills: at the stop, or at the open when the bar gaps through it."""
    return min(stop, bar_open) if long else max(stop, bar_open)


def simulate(pool, entry, stop, target, k, last, replays=10000, seed=19, window=2, side="buy", costs=None):
    """Monte Carlo replays of bars k+1..last. Same draws and order as the reel's sim.py.
    The odds and avg_R are the reel's (fills at the stop and the target). avg_R_net
    also fills a stop at the bar's open when the bar gaps through it, and takes
    off the costs (cost_r)."""
    long = side == "buy"
    risk = (entry - stop) if long else (stop - entry)
    r_target = round(((target - entry) if long else (entry - target)) / risk, 4)
    stop_R = []   # the real R of each stop outcome, gap included

    def slot(rng, i):
        return i if window == 0 else min(SESSION_BARS - 1, max(1, i + rng.randint(-window, window)))

    rng = random.Random(seed)
    choice = rng.choice
    outcomes, closes, paths = [], [], []
    for n in range(replays):
        p = entry
        outcome = None
        keep = n < SAMPLE_PATHS
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            hi, lo, cl = p * h, p * l, p * c
            if outcome is None:
                if long:
                    if lo <= stop:
                        outcome = "stop"        # both inside one bar: count the loss (conservative)
                    elif hi >= target:
                        outcome = "target"
                else:
                    if hi >= stop:
                        outcome = "stop"
                    elif lo <= target:
                        outcome = "target"
                if outcome == "stop":
                    f = stop_fill(stop, p * o, long)
                    stop_R.append(((f - entry) if long else (entry - f)) / risk)
            p = cl
            if keep:
                path.append(round(p, 2))
        outcomes.append(outcome or "close")
        closes.append(p)
        if keep:
            paths.append(path)
    cnt = {x: outcomes.count(x) for x in ("target", "stop", "close")}
    green = sum(1 for o, p in zip(outcomes, closes)
                if o == "target" or (o == "close" and (p > entry if long else p < entry)))
    Rs = [r_target if o == "target" else -1.0 if o == "stop" else ((p - entry) if long else (entry - p)) / risk
          for o, p in zip(outcomes, closes)]
    gaps = iter(stop_R)
    c_r = cost_r(entry, risk, costs)
    Rs_net = [(next(gaps) if o == "stop" else R) - c_r for o, R in zip(outcomes, Rs)]
    srt = sorted(closes)
    # percentile bands per bar for the fan chart: a second run of 2,000 paths, same seed
    rng = random.Random(seed)
    choice = rng.choice
    allp = []
    for n in range(BAND_PATHS):
        p = entry
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            p = p * c
            path.append(p)
        allp.append(path)
    bands = []
    for j in range(last - k + 1):
        col = sorted(pp[j] for pp in allp)
        bands.append([round(pct(col, q), 2) for q in BAND_Q])
    lo_, hi_ = pct(srt, 0.01), pct(srt, 0.99)
    W = (hi_ - lo_) / HIST_BINS
    bins = [0] * HIST_BINS
    for c in srt:
        b = int((c - lo_) / W) if W > 0 else 0
        if 0 <= b < HIST_BINS:
            bins[b] += 1
    return {
        "replays": replays, "seed": seed, "window": window, "bars_left": last - k,
        "p_target": round(cnt["target"] / replays, 4), "p_stop": round(cnt["stop"] / replays, 4),
        "p_neither": round(cnt["close"] / replays, 4), "p_green": round(green / replays, 4),
        "median_close": round(pct(srt, 0.5), 2), "p5_close": round(pct(srt, 0.05), 2),
        "p95_close": round(pct(srt, 0.95), 2), "avg_R": round(statistics.mean(Rs), 3),
        "avg_R_net": round(statistics.mean(Rs_net), 3), "cost_R": round(c_r, 4), "costs": dict(costs or {}),
        "r_at_target": r_target, "band_paths": BAND_PATHS, "bands": bands, "sample_paths": paths,
        "hist": {"lo": round(lo_, 2), "hi": round(hi_, 2), "bins": bins},
    }


# ---------------------------------------------------------------- stop rules and the history replay

def cents_away(x, long):
    """Round a stop to the cent, away from the entry: down for a buy, up for a sell."""
    c = round(x * 100, 4)
    return (math.floor(c) if long else math.ceil(c)) / 100.0


def opening_range(D, n):
    """(high, low) of the first n bars of a session given as {slot: bar} or a list."""
    rows = [D[i] for i in range(n)]
    return max(b["h"] for b in rows), min(b["l"] for b in rows)


def rule_stop(rule, oh, ol, entry, long):
    r = rule["stop_rule"]
    if r == "or_mid":
        return cents_away((oh + ol) / 2, long)
    if r in ("or_low", "or_high"):
        return cents_away(ol if long else oh, long)
    frac = rule["stop_pct"] / 100.0
    return cents_away(entry * (1 - frac) if long else entry * (1 + frac), long)


def history_replay(sessions, rule, last, side="buy", costs=None, allow=None):
    """The plan's rule on every past full session: entry = the first bar after the
    opening range to close beyond it (up to the --until bar), then stop first, then
    target, bar by bar, else the close at --until. R_net fills a stop at the open
    of a bar that gaps through it and takes off the costs.
    Optional exits in the rule: breakeven_r moves the stop to the entry once a bar
    reaches entry + that many R (from the next bar on); half_r sells half there and
    the rest runs to the stop, the target or the close. allow = the session dates
    the filters let through (None = all)."""
    long = side == "buy"
    n = rule["or_minutes"] // 5
    be_r, half_r = rule.get("breakeven_r"), rule.get("half_r")
    trades = []
    for d, D in sessions:
        if allow is not None and d not in allow:
            continue
        oh, ol = opening_range(D, n)
        kk = next((i for i in range(n, last + 1) if (D[i]["c"] > oh if long else D[i]["c"] < ol)), None)
        if kk is None:
            continue
        e = round(D[kk]["c"], 2)
        s = rule_stop(rule, oh, ol, e, long)
        risk = (e - s) if long else (s - e)
        if risk <= 0:
            continue
        t = round(e + rule["target_r"] * risk, 2) if long else round(e - rule["target_r"] * risk, 2)
        gain = (lambda p: (p - e) / risk) if long else (lambda p: (e - p) / risk)
        reached = (lambda b, p: b["h"] >= p) if long else (lambda b, p: b["l"] <= p)
        cur, half, oc, R_stop, R_fill = s, None, None, -1.0, -1.0
        for b in D[kk + 1:last + 1]:
            if (b["l"] <= cur) if long else (b["h"] >= cur):
                oc = "stop"
                R_stop, R_fill = gain(cur), gain(stop_fill(cur, b["o"], long))
                break
            if half_r and half is None and reached(b, e + half_r * risk if long else e - half_r * risk):
                half = float(half_r)
            if reached(b, t):
                oc = "target"
                break
            if be_r and reached(b, e + be_r * risk if long else e - be_r * risk):
                cur = max(cur, e) if long else min(cur, e)
        rest = gain(t) if oc == "target" else R_stop if oc == "stop" else gain(D[last]["c"])
        rest_fill = R_fill if oc == "stop" else rest
        R = rest if half is None else (half + rest) / 2
        R_net = (rest_fill if half is None else (half + rest_fill) / 2) - cost_r(e, risk, costs)
        trades.append({"date": d, "time": clock12(OPEN_MIN + 5 * (kk + 1)), "entry": e, "stop": s, "target": t,
                       "outcome": oc or "close", "R": round(R, 2), "R_net": round(R_net, 2),
                       "gapped": oc == "stop" and R_fill < R_stop})
    return {
        "sessions": len(sessions), "triggered": len(trades),
        "target": sum(1 for x in trades if x["outcome"] == "target"),
        "stop": sum(1 for x in trades if x["outcome"] == "stop"),
        "close": sum(1 for x in trades if x["outcome"] == "close"),
        "green": sum(1 for x in trades if x["R"] > 0),
        "avg_R": round(statistics.mean(x["R"] for x in trades), 3) if trades else None,
        "avg_R_net": round(statistics.mean(x["R_net"] for x in trades), 3) if trades else None,
        "rule": rule, "trades": trades,
    }


# ---------------------------------------------------------------- the plan

def _num(v, what):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise TMError("the plan needs a number for %s, got %r" % (what, v))
    if not x > 0 or math.isinf(x):
        raise TMError("the plan's %s must be a positive price, got %r" % (what, v))
    return x


def load_plan(a, now_utc):
    """The plan from --plan (trader/plan.json) or from the flags, checked and normalized."""
    if a.symbol:
        missing = [f for f in ("entry", "stop", "target", "entry_time") if getattr(a, f) is None]
        if missing:
            raise TMError("with --symbol also give %s" % ", ".join("--" + m.replace("_", "-") for m in missing))
        raw = {"symbol": a.symbol, "side": a.side, "date": a.date or to_eastern(now_utc).date().isoformat(),
               "entry": a.entry, "stop": a.stop, "target": a.target, "entry_time": a.entry_time,
               "setup": a.setup, "opening_range": {"minutes": a.or_minutes}, "stop_rule": a.stop_rule,
               "interval": "5m"}
        src = "flags"
    else:
        if not os.path.exists(a.plan):
            raise TMError("no plan at %s: run /trader-exit first, or pass --symbol --date --entry --stop "
                          "--target --entry-time" % a.plan)
        try:
            with open(a.plan, encoding="utf-8") as fh:
                raw = json.load(fh)
        except ValueError as e:
            raise TMError("%s is not valid JSON: %s" % (a.plan, e))
        if not isinstance(raw, dict):
            raise TMError("%s is not a plan" % a.plan)
        src = a.plan
    return normalize_plan(raw, src)


def normalize_plan(raw, src="flags"):
    """A plan dict (trader/plan.json shape) -> the checked plan the time machine runs."""
    sym = str(raw.get("symbol") or "").strip().upper()
    if not re.match(r"^[A-Z][A-Z0-9.\-]{0,9}$", sym):
        raise TMError("the plan needs a ticker symbol, got %r" % raw.get("symbol"))
    side = str(raw.get("side") or "buy").lower()
    if side not in ("buy", "sell"):
        raise TMError("side must be buy or sell, got %r" % raw.get("side"))
    interval = str(raw.get("interval") or "5m")
    if interval != "5m":
        raise TMError("5-minute bars only (the plan says %s): Yahoo keeps 60 days of 5-min bars but only about "
                      "7 days of 1-min bars, too few sessions for a fair replay pool" % interval)
    try:
        date = datetime.date.fromisoformat(str(raw.get("date") or "")).isoformat()
    except ValueError:
        raise TMError("the plan needs a date like 2026-10-19, got %r" % raw.get("date"))
    entry, stop, target = (_num(raw.get(k), k) for k in ("entry", "stop", "target"))
    long = side == "buy"
    if long and not stop < entry < target:
        raise TMError("a buy needs stop < entry < target (got stop %s, entry %s, target %s)" % (stop, entry, target))
    if not long and not target < entry < stop:
        raise TMError("a sell needs target < entry < stop (got target %s, entry %s, stop %s)" % (target, entry, stop))
    k = None
    if raw.get("entry_time"):
        close_min = parse_clock(raw["entry_time"], "entry_time")
        if (close_min - OPEN_MIN) % 5:
            raise TMError("entry_time %s is not on the 5-minute grid (use the bar's close, e.g. 10:15 AM)"
                          % raw["entry_time"])
        k = (close_min - OPEN_MIN) // 5 - 1
    if raw.get("entry_bar") is not None:
        try:
            kb = int(raw["entry_bar"])
        except (TypeError, ValueError):
            raise TMError("entry_bar must be a bar number, got %r" % raw["entry_bar"])
        if k is not None and kb != k:
            raise TMError("the plan disagrees with itself: entry_bar %d but entry_time %s is bar %d"
                          % (kb, raw["entry_time"], k))
        k = kb
    if k is None:
        raise TMError("the plan needs entry_time (the entry bar's close, e.g. 10:15 AM) or entry_bar")
    if not 0 <= k <= SESSION_BARS - 2:
        raise TMError("the entry must close between 9:35 AM and 3:55 PM to leave bars to replay "
                      "(got %s)" % clock12(OPEN_MIN + 5 * (k + 1)))
    orng = raw.get("opening_range") or {}
    or_min = orng.get("minutes", 15) if isinstance(orng, dict) else 15
    try:
        or_min = int(or_min)
    except (TypeError, ValueError):
        raise TMError("opening_range.minutes must be a number")
    if or_min < 5 or or_min % 5 or or_min >= 390:
        raise TMError("opening_range.minutes must be a multiple of 5 (got %s)" % or_min)
    risk = (entry - stop) if long else (stop - entry)
    return {
        "symbol": sym, "side": side, "date": date, "setup": str(raw.get("setup") or "orb"),
        "entry": entry, "entry_time": clock12(OPEN_MIN + 5 * (k + 1)), "entry_bar": k,
        "stop": stop, "target": target, "risk_per_share": round(risk, 2),
        "target_r": round(((target - entry) if long else (entry - target)) / risk, 2),
        "opening_range": dict(orng, minutes=or_min) if isinstance(orng, dict) else {"minutes": or_min},
        "stop_rule": str(raw.get("stop_rule") or "auto"), "interval": "5m",
        "flat_by": raw.get("flat_by"), "from": src,
    }


def resolve_rule(plan, entry_day):
    """The rule the history replay uses: orb, the plan's opening range, its stop rule
    (or_mid, or_low / or_high, or 'pct' = the plan's stop distance as a percent of
    the entry), and the plan's target R. 'auto' works the stop rule out from the
    plan day's own opening range. None when the setup is not orb."""
    if plan["setup"].lower() != "orb":
        return None
    long = plan["side"] == "buy"
    n = plan["opening_range"]["minutes"] // 5
    rule = {"setup": "orb", "or_minutes": plan["opening_range"]["minutes"], "stop_rule": plan["stop_rule"],
            "target_r": plan["target_r"],
            "stop_pct": round(100.0 * plan["risk_per_share"] / plan["entry"], 4)}
    far = "or_low" if long else "or_high"
    if rule["stop_rule"] == "auto":
        rule["stop_rule"] = "pct"
        if all(i in entry_day for i in range(n)):
            oh, ol = opening_range(entry_day, n)
            if abs(cents_away((oh + ol) / 2, long) - plan["stop"]) < 0.005:
                rule["stop_rule"] = "or_mid"
            elif abs(cents_away(ol if long else oh, long) - plan["stop"]) < 0.005:
                rule["stop_rule"] = far
    elif rule["stop_rule"] in ("or_low", "or_high") and rule["stop_rule"] != far:
        rule["stop_rule"] = "pct"
    elif rule["stop_rule"] not in ("or_mid", far):
        rule["stop_rule"] = "pct"
    if rule["stop_rule"] != "pct":
        del rule["stop_pct"]
    return rule


# ---------------------------------------------------------------- the run

def _until12(mc):
    t = mc.get("until") or "16:00"
    return clock12(int(t[:2]) * 60 + int(t[3:]))


def lean(mc):
    """(gap, share, break_even): the target's share of the replays that hit one side,
    minus the share that breaks even at the target's R. None when nothing hit a side."""
    pt, ps = mc["p_target"], mc["p_stop"]
    if pt + ps <= 0:
        return None
    r = mc.get("r_at_target") or 1.0
    share, even = pt / (pt + ps), 1.0 / (1.0 + r)
    return share - even, share, even


def verdict(mc):
    """One line from the numbers, see the docstring for the thresholds."""
    pt, ps = mc["p_target"], mc["p_stop"]
    lv = lean(mc)
    if lv is None:
        return "No replay reached the target or the stop by %s: the price at %s decides every one." % (
            _until12(mc), _until12(mc))
    d, share, even = lv
    r = mc.get("r_at_target") or 1.0
    one_r = abs(r - 1.0) < 0.005
    if abs(d) < COIN_FLIP:
        head = "Close to a coin flip" if one_r else "Close to break-even"
    elif abs(d) < STRONG:
        head = "Leans toward the target" if d > 0 else "Leans toward the stop"
    else:
        head = "Strongly toward the target" if d > 0 else "Strongly toward the stop"
    odds = "target first %.1f %% vs stop first %.1f %%" % (100 * pt, 100 * ps)
    if one_r:
        return "%s: %s." % (head, odds)
    return ("%s: at %gR the target has to come first in %.0f %% of the replays that hit one side; "
            "it did in %.0f %% (%s)." % (head, round(r, 2), 100 * even, 100 * share, odds))


def verdict_notes(mc):
    """The lines printed right below the verdict, so nothing reads as one-sided."""
    notes = []
    pn = mc.get("p_neither", 0)
    if pn > MANY_NEITHER and lean(mc) is not None:
        notes.append("%.1f %% of the replays hit neither by %s; the price at %s decides those."
                     % (100 * pn, _until12(mc), _until12(mc)))
    lv = lean(mc)
    avg = mc.get("avg_R")
    if lv and abs(lv[0]) >= COIN_FLIP and avg is not None and avg != 0 and (avg > 0) != (lv[0] > 0):
        notes.append("Average R is %+.3f all the same: the replays that hit neither pull it the other way." % avg)
    return notes


def time_machine(text, plan, now_utc, replays=10000, seed=19, window=2, until=None, source="yahoo", costs=None):
    """Chart JSON text + a normalized plan -> the timemachine.json document."""
    if not 1 <= replays <= 1000000:
        raise TMError("--replays must be between 1 and 1,000,000")
    if not 0 <= window <= 10:
        raise TMError("--window must be between 0 and 10 bars")
    sym = plan["symbol"]
    result = chart_result(text, sym)
    meta, bars, dropped = parse_bars(result, now_utc)
    msym = str(meta.get("symbol") or "").upper()
    if msym and msym != sym.replace(".", "-"):
        raise TMError("the data is for %s but the plan is for %s" % (msym, sym))
    gran = meta.get("dataGranularity")
    if gran and gran != "5m":
        raise TMError("the data has %s bars; the time machine needs 5-minute bars" % gran)
    if not bars:
        raise TMError("no complete 5-minute bars for %s in the data" % sym)
    k = plan["entry_bar"]
    early = plan["date"] in NYSE_EARLY_CLOSE
    end_min = EARLY_MIN if early else CLOSE_MIN
    until_min = end_min if until is None else parse_clock(until, "--until")
    if (until_min - OPEN_MIN) % 5 or until_min > end_min:
        raise TMError("--until must be on the 5-minute grid and no later than %s%s"
                      % (clock12(end_min), " (an early close)" if early else ""))
    last = (until_min - OPEN_MIN) // 5 - 1
    if last <= k:
        raise TMError("--until %s leaves nothing to replay after the %s entry" % (clock12(until_min),
                                                                             plan["entry_time"]))
    days = split_sessions(bars)
    entry_day = days.get(plan["date"], {})
    if k not in entry_day:
        reason = ("no bars for that day" if not entry_day else
                  "the data stops at the %s bar" % entry_day[max(entry_day)]["start"])
        raise TMError("the entry is not in the data: no complete %s bar (closing %s) on %s for %s (%s)"
                      % (clock24(OPEN_MIN + 5 * k), plan["entry_time"], plan["date"], sym, reason))
    eb = entry_day[k]
    warn = []
    if not eb["l"] - 0.01 <= plan["entry"] <= eb["h"] + 0.01:
        warn.append("the plan's entry %.2f is outside that bar's range %.2f to %.2f"
                    % (plan["entry"], eb["l"], eb["h"]))
    sessions, short = full_sessions(days, plan["date"])
    if len(sessions) < MIN_SESSIONS:
        raise TMError("only %d full session(s) of %s before %s in the data; the time machine needs at least %d"
                      % (len(sessions), sym, plan["date"], MIN_SESSIONS))
    pool = build_pool(sessions)
    mc = simulate(pool, plan["entry"], plan["stop"], plan["target"], k, last, replays, seed, window, plan["side"],
                  costs)
    mc = dict(mc, until=clock24(until_min), pool_sessions=len(sessions), pool_from=sessions[0][0],
              pool_to=sessions[-1][0],
              band_times=[clock24(OPEN_MIN + 5 * (j + 1)) for j in range(k, last + 1)])
    rule = resolve_rule(plan, entry_day)
    plan_out = {kk: v for kk, v in plan.items() if v is not None}
    if rule:
        plan_out["stop_rule"] = rule["stop_rule"]
        history = history_replay(sessions, rule, last, plan["side"], costs)
    else:
        history = {"sessions": len(sessions), "triggered": 0, "rule": None, "trades": [],
                   "note": "skipped: the history replay knows the opening-range breakout (orb) only, "
                           "this plan's setup is %r" % plan["setup"]}
    last_bar = bars[-1]
    return {
        "created_by": "trader-timemachine",
        "created_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "data": {"symbol": msym or sym, "interval": "5m", "range": "60d",
                 "last_bar": "%s %s ET" % (last_bar["date"], last_bar["start"]),
                 "dropped": dropped, "short_sessions_left_out": short},
        "plan": plan_out,
        "mc": mc,
        "history": history,
        "verdict": verdict(mc),
        "verdict_notes": verdict_notes(mc),
        "warnings": warn,
        "note": ("A simulation built from %s's own last %d full sessions. It is not a prediction: earnings, "
                 "news and halts can do things those sessions never did. Same seed, same numbers." % (sym, len(sessions))),
    }


# ---------------------------------------------------------------- costs

def load_costs(a):
    """Slippage and commission: the flags, else trader/config.json
    (slippage_bps, commission_per_share), else the defaults above."""
    cfg = {}
    path = getattr(a, "config", None) or "trader/config.json"
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                cfg = json.load(fh)
        except ValueError as e:
            raise TMError("%s is not valid JSON: %s" % (path, e))
        if not isinstance(cfg, dict):
            cfg = {}
    out = {}
    for key, flag, default in (("slippage_bps", "slippage_bps", SLIPPAGE_BPS),
                               ("commission_per_share", "commission", COMMISSION)):
        v = getattr(a, flag, None)
        if v is None:
            v = cfg.get(key, default)
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise TMError("%s must be a number, got %r" % (key, v))
        if not 0 <= v < 1000 or math.isnan(v):
            raise TMError("%s must be zero or more, got %r" % (key, v))
        out[key] = v
    return out


def cost_words(costs):
    return "slippage %g bps a side, commission $%g a share a side" % (
        costs.get("slippage_bps", 0), costs.get("commission_per_share", 0))


# ---------------------------------------------------------------- backtest

RVOL_LOOKBACK, RVOL_MIN_HISTORY = 20, 10


def market_closes(text, now_utc):
    """SPY's (or any index's) chart -> [(date, close)] of its full sessions, oldest first."""
    _, bars, _ = parse_bars(chart_result(text, "SPY"), now_utc)
    sessions, _ = full_sessions(split_sessions(bars), "9999-12-31")
    return [(d, D[-1]["c"]) for d, D in sessions]


def session_filter(sessions, rule, closes=None):
    """The session dates the rule's filters let through, or None when it has none.
    min_rvol: the opening range's volume is at least that many times its average over
    the previous 20 sessions (10 at least). spy_trend: the market's last close BEFORE
    the session is above its own N-session average. Both use only what is known at
    the entry: no look-ahead."""
    spy_n, rvol = rule.get("spy_trend"), rule.get("min_rvol")
    if not spy_n and not rvol:
        return None
    ok = {d for d, _ in sessions}
    if rvol:
        n = rule["or_minutes"] // 5
        vols = [(d, sum(D[i]["v"] for i in range(n))) for d, D in sessions]
        keep = set()
        for i, (d, v) in enumerate(vols):
            prev = [x for _, x in vols[max(0, i - RVOL_LOOKBACK):i]]
            if len(prev) >= RVOL_MIN_HISTORY and statistics.mean(prev) > 0 and v >= rvol * statistics.mean(prev):
                keep.add(d)
        ok &= keep
    if spy_n:
        if not closes:
            raise TMError("the market-trend filter needs SPY's bars, and there are none")
        keep = set()
        for d in ok:
            prev = [c for dd, c in closes if dd < d][-spy_n:]
            if len(prev) == spy_n and prev[-1] > statistics.mean(prev):
                keep.add(d)
        ok &= keep
    return ok


def backtest(charts, rule, now_utc, until=None, costs=None, side="buy", market=None):
    """{SYM: chart JSON text} + an orb rule -> the backtest document. The rule runs on
    every full session in each chart (today too, once it is complete): the same
    history replay the run uses, with the costs. market = SPY's chart text, for the
    rule's spy_trend filter."""
    until_min = CLOSE_MIN if until is None else parse_clock(until, "--until")
    if (until_min - OPEN_MIN) % 5 or until_min > CLOSE_MIN:
        raise TMError("--until must be on the 5-minute grid and no later than 4:00 PM")
    last = (until_min - OPEN_MIN) // 5 - 1
    if last < rule["or_minutes"] // 5:
        raise TMError("--until %s ends before the opening range does" % clock12(until_min))
    closes = market_closes(market, now_utc) if market and rule.get("spy_trend") else None
    per, trades, failed = [], [], {}
    for sym in sorted(charts):
        try:
            meta, bars, _ = parse_bars(chart_result(charts[sym], sym), now_utc)
        except TMError as e:
            failed[sym] = str(e)
            continue
        sessions, _ = full_sessions(split_sessions(bars), "9999-12-31")
        h = history_replay(sessions, rule, last, side, costs, session_filter(sessions, rule, closes))
        for t in h["trades"]:
            trades.append(dict(t, symbol=sym))
        per.append({"symbol": sym, "sessions": h["sessions"], "triggered": h["triggered"],
                    "target": h["target"], "stop": h["stop"], "close": h["close"],
                    "avg_R": h["avg_R"], "avg_R_net": h["avg_R_net"]})
    trades.sort(key=lambda t: (t["date"], t["time"], t["symbol"]))
    return {
        "created_by": "trader-timemachine backtest",
        "created_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rule": rule, "until": clock24(until_min), "costs": dict(costs or {}),
        "symbols": per, "failed": failed, "stats": backtest_stats(trades), "trades": trades,
    }


def backtest_stats(trades):
    """Pooled numbers over every trade, after costs: win rate, average R with a 95 %
    range for the true average (1.96 standard errors), profit factor, worst drawdown
    in R (trades in date order), and the verdict."""
    n = len(trades)
    st = {"trades": n}
    if not n:
        st["verdict"] = "The rule never fired: nothing to judge."
        return st
    net = [t["R_net"] for t in trades]
    avg = statistics.mean(net)
    sd = statistics.stdev(net) if n > 1 else 0.0
    half = 1.96 * sd / math.sqrt(n)
    wins, losses = sum(r for r in net if r > 0), -sum(r for r in net if r < 0)
    eq = peak = dd = 0.0
    for r in net:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    st.update({
        "win_rate": round(sum(1 for r in net if r > 0) / n, 4),
        "avg_R": round(statistics.mean(t["R"] for t in trades), 3),
        "avg_R_net": round(avg, 3), "ci95_net": [round(avg - half, 3), round(avg + half, 3)],
        "profit_factor_net": round(wins / losses, 2) if losses else None,
        "total_R_net": round(sum(net), 2), "max_drawdown_R": round(dd, 2),
        "gap_stops": sum(1 for t in trades if t.get("gapped")),
        "halves_R_net": [round(statistics.mean(net[:n // 2]), 3) if n > 1 else None,
                         round(statistics.mean(net[n // 2:]), 3)],
    })
    lo, hi = st["ci95_net"]
    if n < MIN_TRADES:
        st["verdict"] = ("Too few trades to tell: %d, and it takes at least %d before the average means much."
                         % (n, MIN_TRADES))
    elif lo > 0:
        st["verdict"] = ("An edge after costs: %+.3f R a trade, and the 95 %% range (%+.3f to %+.3f) stays above "
                         "zero. Past data only: it can stop working." % (avg, lo, hi))
    elif hi < 0:
        st["verdict"] = ("Loses after costs: %+.3f R a trade, and the 95 %% range (%+.3f to %+.3f) stays below "
                         "zero. Do not trade this rule as it is." % (avg, lo, hi))
    else:
        st["verdict"] = ("No proven edge: %+.3f R a trade after costs, but the 95 %% range (%+.3f to %+.3f) "
                         "includes zero. It could be luck either way." % (avg, lo, hi))
    return st


def usd(x):
    return "%s$%.2f" % ("-" if x < 0 else "+", abs(x))


def rule_extras(ru):
    """The rule's filters and exits in words, or []."""
    out = []
    if ru.get("spy_trend"):
        out.append("only when SPY closed above its %d-day average the day before" % ru["spy_trend"])
    if ru.get("min_rvol"):
        out.append("only when the opening range traded at least %gx its usual volume" % ru["min_rvol"])
    if ru.get("half_r"):
        out.append("half sold at %gR" % ru["half_r"])
    if ru.get("breakeven_r"):
        out.append("stop to the entry once %gR is reached" % ru["breakeven_r"])
    return out


def data_note(doc):
    rng = doc.get("range", "60d")
    if rng == "60d":
        return ("Yahoo keeps about 60 days of 5-minute bars, so this is a few months at most. "
                "Alpaca's data (backtest --range 1y or 2y) goes back years.")
    return "History: %s of 5-minute bars from Alpaca." % rng


def backtest_report(doc, max_loss=None):
    ru, st = doc["rule"], doc["stats"]
    stop_words = {"or_mid": "the opening-range midpoint", "or_low": "the opening-range low"}.get(
        ru["stop_rule"], "%.2f %% under the entry" % ru.get("stop_pct", 0))
    lines = ["Backtest: %d-min opening-range breakout, buy, stop at %s, %gR target, out by %s."
             % (ru["or_minutes"], stop_words, ru["target_r"], clock12(int(doc["until"][:2]) * 60 + int(doc["until"][3:]))),
             "Costs: %s; a stop that gaps fills at the open." % cost_words(doc["costs"])]
    if rule_extras(ru):
        lines.append("Also: %s." % "; ".join(rule_extras(ru)))
    lines.append("")
    if doc["symbols"]:
        lines.append("  symbol  sessions  fired  target  stop  neither   avg R   after costs")
        for p in doc["symbols"]:
            lines.append("  %-6s  %8d  %5d  %6d  %4d  %7d  %6s  %12s" % (
                p["symbol"], p["sessions"], p["triggered"], p["target"], p["stop"], p["close"],
                "-" if p["avg_R"] is None else "%+.3f" % p["avg_R"],
                "-" if p["avg_R_net"] is None else "%+.3f" % p["avg_R_net"]))
        lines.append("")
    for sym, why in sorted(doc["failed"].items()):
        lines.append("Skipped %s: %s" % (sym, why))
    if st["trades"]:
        lines += [
            "All together: %d trades, %.0f %% winners after costs." % (st["trades"], 100 * st["win_rate"]),
            "  Average R %+.3f before costs, %+.3f after (95 %% range %+.3f to %+.3f)."
            % (st["avg_R"], st["avg_R_net"], st["ci95_net"][0], st["ci95_net"][1]),
            "  Total %+.2f R after costs, worst drawdown %.2f R%s."
            % (st["total_R_net"], st["max_drawdown_R"],
               "" if st["profit_factor_net"] is None else ", profit factor %.2f" % st["profit_factor_net"]),
        ]
        if st["gap_stops"]:
            lines.append("  %d stop(s) gapped through and filled worse than the stop." % st["gap_stops"])
        h1, h2 = st.get("halves_R_net") or [None, None]
        if h1 is not None:
            lines.append("  First half of the trades %+.3f R, second half %+.3f R: an edge should show in both."
                         % (h1, h2))
        if max_loss:
            lines.append("  At $%g risked a trade: about %s a trade on average, %s in total, worst drawdown $%.2f."
                         % (max_loss, usd(st["avg_R_net"] * max_loss), usd(st["total_R_net"] * max_loss),
                            st["max_drawdown_R"] * max_loss))
    lines += ["", st["verdict"], data_note(doc) +
              " Past results do not promise future ones. Education and tooling, not financial advice."]
    return lines


VARIANTS = (
    ("as given", {}),
    ("+ SPY above its 20-day average", {"spy_trend": 20}),
    ("+ opening volume >= 1.5x usual", {"min_rvol": 1.5}),
    ("+ both filters", {"spy_trend": 20, "min_rvol": 1.5}),
    ("target 2R", {"target_r": 2.0}),
    ("target 2R, stop to entry at 1R", {"target_r": 2.0, "breakeven_r": 1.0}),
    ("half at 1R, rest 2R, stop to entry", {"target_r": 2.0, "half_r": 1.0, "breakeven_r": 1.0}),
)


def compare(charts, rule, now_utc, until=None, costs=None, market=None):
    """The same data through each variant of the rule: filters and exits side by side."""
    rows = []
    for label, extra in VARIANTS:
        ru = dict(rule, **extra)
        if ru.get("spy_trend") and not market:
            continue
        doc = backtest(charts, ru, now_utc, until, costs, market=market)
        rows.append({"variant": label, "rule": ru, "stats": doc["stats"]})
    base = backtest(charts, rule, now_utc, until, costs)
    return {"created_by": "trader-timemachine backtest --compare", "created_at": base["created_at"],
            "until": base["until"], "costs": base["costs"], "symbols": [p["symbol"] for p in base["symbols"]],
            "failed": base["failed"], "variants": rows}


def short_verdict(st):
    if st["trades"] < MIN_TRADES:
        return "too few"
    lo, hi = st["ci95_net"]
    return "EDGE" if lo > 0 else "loses" if hi < 0 else "no proof"


def compare_report(doc, max_loss=None):
    lines = ["Backtest compare: %d stocks, out by %s, costs %s." % (
        len(doc["symbols"]), doc["until"], cost_words(doc["costs"])), "",
        "  %-36s %6s %8s  %-17s %8s %8s  %s" % ("variant", "trades", "avg R", "95 % range", "1st half",
                                                "2nd half", "verdict")]
    for r in doc["variants"]:
        st = r["stats"]
        if not st["trades"]:
            lines.append("  %-36s %6d" % (r["variant"], 0))
            continue
        h1, h2 = st["halves_R_net"]
        lines.append("  %-36s %6d %+8.3f  %+.3f to %+.3f %8s %+8.3f  %s" % (
            r["variant"], st["trades"], st["avg_R_net"], st["ci95_net"][0], st["ci95_net"][1],
            "-" if h1 is None else "%+.3f" % h1, h2, short_verdict(st)))
    for sym, why in sorted(doc["failed"].items()):
        lines.append("Skipped %s: %s" % (sym, why))
    lines += ["", "R after costs. %d variants on the same data: the best of them is partly luck. Trust one only if "
              "its 95 %% range stays above zero AND both halves are positive." % len(doc["variants"]),
              data_note(doc) + " Education and tooling, not financial advice."]
    if max_loss:
        lines.insert(-2, "At $%g risked a trade, +0.100 R a trade is about $%.2f." % (max_loss, 0.1 * max_loss))
    return lines


# ---------------------------------------------------------------- output

def pc(x):
    return "%.1f %%" % (100.0 * x)


def report(doc):
    p, m, h = doc["plan"], doc["mc"], doc["history"]
    long = p["side"] == "buy"
    until = clock12(int(m["until"][:2]) * 60 + int(m["until"][3:]))
    wd = WEEKDAYS[datetime.date.fromisoformat(p["date"]).weekday()]
    r = p["target_r"]
    lines = [
        "%s %s, %s %s: entry %.2f at %s (bar %d), stop %.2f, target %.2f (%sR, risk %.2f a share)."
        % (p["symbol"], p["side"], wd, p["date"], p["entry"], p["entry_time"], p["entry_bar"], p["stop"],
           p["target"], ("%g" % r), p["risk_per_share"]),
        "%s replays of the rest of the session, %s to %s (%d five-minute bars), seed %d."
        % ("{:,}".format(m["replays"]), p["entry_time"], until, m["bars_left"], m["seed"]),
        "Each bar is drawn whole from %s's own 5-min bars at the same time of day (+-%d bars) over its last "
        "%d full sessions (%s to %s)." % (p["symbol"], m["window"], m["pool_sessions"], m["pool_from"], m["pool_to"]),
        "",
        "  target first   %s" % pc(m["p_target"]),
        "  stop first     %s" % pc(m["p_stop"]),
        "  neither        %-8s still open at %s" % (pc(m["p_neither"]), until),
        "  green          %-8s target first, or %s the entry at %s" % (pc(m["p_green"]),
                                                                       "above" if long else "below", until),
        "",
        "  Price at %s: median %.2f, 90 %% of replays between %.2f and %.2f."
        % (until, m["median_close"], m["p5_close"], m["p95_close"]),
        "  Average R: %+.3f (R = the %.2f risk: %sR at the target, -1R at the stop)."
        % (m["avg_R"], p["risk_per_share"], ("+%g" % m["r_at_target"]), ),
    ]
    if "avg_R_net" in m:
        lines.append("  After costs: %+.3f R (%s = %.3f R a trade; a stop that gaps fills at the open)."
                     % (m["avg_R_net"], cost_words(m.get("costs") or {}), m.get("cost_R", 0)))
    lines.append("")
    if h.get("rule"):
        ru = h["rule"]
        stop_words = {"or_mid": "opening-range midpoint", "or_low": "opening-range low",
                      "or_high": "opening-range high"}.get(ru["stop_rule"],
                                                           "%.2f %% from the entry" % ru.get("stop_pct", 0))
        if h["triggered"]:
            lines.append("History: the same rule (%d-min opening range, stop at the %s, %gR target) on each of "
                         "those %d real sessions fired %d times: %d target, %d stop, %d neither. Average R %+.3f."
                         % (ru["or_minutes"], stop_words, ru["target_r"], h["sessions"], h["triggered"],
                            h["target"], h["stop"], h["close"], h["avg_R"]))
            if h.get("avg_R_net") is not None:
                lines.append("After costs the history averages %+.3f R a trade." % h["avg_R_net"])
        else:
            lines.append("History: the same rule never fired in those %d sessions." % h["sessions"])
    else:
        lines.append("History: " + h.get("note", "skipped."))
    lines += ["", doc["verdict"]] + list(doc.get("verdict_notes") or [])
    nets = [x for x in (m.get("avg_R_net"), h.get("avg_R_net")) if x is not None]
    if nets and all(x < 0 for x in nets):
        lines.append("After costs, both the replays and the history lose money on average: think twice.")
    lines.append("")
    for w in doc.get("warnings") or []:
        lines.append("Note: " + w + ".")
    if doc["data"]["short_sessions_left_out"]:
        lines.append("Left out %d short session(s) (half days or gaps): %s."
                     % (len(doc["data"]["short_sessions_left_out"]),
                        ", ".join(x["date"] for x in doc["data"]["short_sessions_left_out"])))
    lines.append(doc["note"] + " Repeat this run with --seed %d." % m["seed"])
    return lines


def render_chart(doc, height=17):
    """ASCII fan chart of the bands: one column per 5-min bar."""
    p, m = doc["plan"], doc["mc"]
    bands, times = m["bands"], m["band_times"]
    lo = min([b[0] for b in bands] + [p["stop"], p["target"]])
    hi = max([b[4] for b in bands] + [p["stop"], p["target"]])
    pad = (hi - lo) * 0.04 or 0.01
    lo, hi = lo - pad, hi + pad
    step = (hi - lo) / (height - 1)

    def row(price):
        return max(0, min(height - 1, int(round((hi - price) / step))))
    grid = [[" "] * len(bands) for _ in range(height)]
    for j, b in enumerate(bands):
        r5, r25, r50, r75, r95 = [row(x) for x in b]
        for r in range(r95, r5 + 1):
            grid[r][j] = "."
        for r in range(r75, r25 + 1):
            grid[r][j] = ":"
        grid[r50][j] = "*"
    marks = {row(p["target"]): "target", row(p["stop"]): "stop"}
    if row(p["entry"]) not in marks:
        marks[row(p["entry"])] = "entry"
    for r in (row(p["target"]), row(p["stop"])):
        grid[r] = ["-" if ch == " " else ch for ch in grid[r]]
    out = ["%s %s, %s replay paths from the %s entry (seed %d)" % (
        p["symbol"], p["date"], "{:,}".format(m.get("band_paths", BAND_PATHS)), p["entry_time"], m["seed"])]
    for r in range(height):
        label = marks.get(r)
        price = {"target": p["target"], "stop": p["stop"], "entry": p["entry"]}.get(label, hi - r * step)
        left = ("%9.2f |" % price) if (label or r in (0, height - 1)) else "          |"
        out.append(left + "".join(grid[r]) + ("  " + label if label else ""))
    axis = [" "] * len(bands)
    for j in sorted({0, len(bands) // 2, len(bands) - 1}):
        t = times[j]
        hh, mm = int(t[:2]), t[3:]
        lab = "%d:%s" % (hh % 12 or 12, mm)
        s = max(0, min(j, len(bands) - len(lab)))
        axis[s:s + len(lab)] = list(lab)
    out.append("          +" + "-" * len(bands))
    out.append("           " + "".join(axis))
    out.append("* median   : middle half of the paths   . 90 % of the paths   - stop and target")
    return out


# ---------------------------------------------------------------- commands

def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
        fh.write("\n")


def compute(a):
    now = parse_now(a.now)
    plan = load_plan(a, now)
    if a.offline:
        text, source = read_offline(a.offline, plan["symbol"]), "offline"
    else:
        text, source = fetch_chart(plan["symbol"]), data_source()
    return time_machine(text, plan, now, a.replays, a.seed, a.window, a.until, source, load_costs(a))


def cmd_run(a):
    doc = compute(a)
    if a.write:
        _write(a.write, doc)
    if a.json:
        slim = dict(doc, mc={k: v for k, v in doc["mc"].items() if k != "sample_paths"})
        print(json.dumps(slim, indent=2))
    else:
        print("\n".join(report(doc)))
        if a.write:
            print("Wrote %s." % a.write)
        print("Next: /trader-budget sizes it to your budget and your max loss per trade.")
    return 0


def cmd_chart(a):
    if a.from_file:
        if not os.path.exists(a.from_file):
            raise TMError("no %s: run timemachine.py run --write first" % a.from_file)
        try:
            with open(a.from_file, encoding="utf-8") as fh:
                doc = json.load(fh)
            doc["mc"]["bands"]
        except (ValueError, KeyError, TypeError):
            raise TMError("%s is not a time machine result" % a.from_file)
    else:
        doc = compute(a)
    print("\n".join(render_chart(doc)))
    print("\n".join([doc.get("verdict", "")] + list(doc.get("verdict_notes") or [])))
    return 0


def backtest_symbols(a):
    if a.symbols:
        syms = [x.strip().upper() for x in a.symbols.split(",") if x.strip()]
    elif os.path.exists(a.watchlist):
        try:
            with open(a.watchlist, encoding="utf-8") as fh:
                wl = json.load(fh)
        except ValueError as e:
            raise TMError("%s is not valid JSON: %s" % (a.watchlist, e))
        rows = wl.get("hits", []) if isinstance(wl, dict) else wl
        syms = [str(r.get("symbol") if isinstance(r, dict) else r).upper() for r in rows or []]
    else:
        raise TMError("give --symbols TSLA,AAPL,... (or run /trader-scanner for a trader/watchlist.json)")
    syms = list(dict.fromkeys(s for s in syms if re.match(r"^[A-Z][A-Z0-9.\-]{0,9}$", s)))
    if not syms:
        raise TMError("no valid tickers to backtest")
    if len(syms) > 200:
        raise TMError("%d tickers is too many for one run (200 at most): be polite to the free data" % len(syms))
    return syms


def cmd_backtest(a):
    now = parse_now(a.now)
    if a.or_minutes < 5 or a.or_minutes % 5 or a.or_minutes >= 390:
        raise TMError("--or-minutes must be a multiple of 5")
    if not a.target_r > 0:
        raise TMError("--target-r must be more than 0")
    rule = {"setup": "orb", "or_minutes": a.or_minutes, "stop_rule": a.stop_rule, "target_r": a.target_r}
    if a.stop_rule == "pct":
        if not a.stop_pct or not 0 < a.stop_pct < 50:
            raise TMError("--stop-rule pct needs --stop-pct, e.g. 1.5")
        rule["stop_pct"] = a.stop_pct
    for key in ("spy_trend", "min_rvol", "half_r", "breakeven_r"):
        val = getattr(a, key, None)
        if val is not None:
            if not val > 0:
                raise TMError("--%s must be more than 0" % key.replace("_", "-"))
            rule[key] = val
    if rule.get("half_r") and rule["half_r"] >= rule["target_r"]:
        raise TMError("--half-r must be below --target-r")
    if a.range != "60d" and not a.offline and data_source() != "alpaca":
        raise TMError("--range %s needs Alpaca's data: set TRADER_ALPACA_KEY and TRADER_ALPACA_SECRET" % a.range)
    costs = load_costs(a)
    want_market = a.compare or rule.get("spy_trend")
    charts, failed, market = {}, {}, None
    symbols = backtest_symbols(a)
    for n, sym in enumerate(symbols + (["SPY"] if want_market and "SPY" not in symbols else [])):
        try:
            if a.offline:
                text = read_offline(a.offline, sym)
            else:
                if n:
                    _sleep(PAUSE)
                text = fetch_chart(sym, a.range)
                if a.save:
                    os.makedirs(a.save, exist_ok=True)
                    with gzip.open(os.path.join(a.save, "%s-%s5m.json.gz" % (sym, a.range)), "wt",
                                   encoding="utf-8") as fh:
                        fh.write(text)
        except TMError as e:
            if sym in symbols:
                failed[sym] = str(e)
            continue
        if sym == "SPY":
            market = text
        if sym in symbols:
            charts[sym] = text
    if a.compare:
        doc = compare(charts, rule, now, a.until, costs, market)
        doc["failed"].update(failed)
        doc["range"] = a.range
        if a.write:
            _write(a.write, doc)
        if a.json:
            print(json.dumps(doc, indent=2))
        else:
            print("\n".join(compare_report(doc, a.max_loss)))
        return 0
    doc = backtest(charts, rule, now, a.until, costs, market=market)
    doc["failed"].update(failed)
    doc["range"] = a.range
    if a.write:
        _write(a.write, doc)
    if a.json:
        print(json.dumps(doc, indent=2))
        return 0
    max_loss = a.max_loss
    if max_loss is None and os.path.exists(a.config):
        try:
            with open(a.config, encoding="utf-8") as fh:
                max_loss = (json.load(fh) or {}).get("max_loss_per_trade_usd")
        except (ValueError, AttributeError):
            max_loss = None
    print("\n".join(backtest_report(doc, max_loss)))
    if a.write:
        print("Wrote %s." % a.write)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Time Machine: replay a planned day trade (trader-timemachine).")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--plan", default="trader/plan.json", help="the plan from trader-exit")
        p.add_argument("--symbol", help="or give the plan as flags: ticker")
        p.add_argument("--side", default="buy", choices=["buy", "sell"])
        p.add_argument("--date", help="the plan's day, YYYY-MM-DD (default today, New York)")
        p.add_argument("--entry", type=float)
        p.add_argument("--stop", type=float)
        p.add_argument("--target", type=float)
        p.add_argument("--entry-time", help='the entry bar\'s close, e.g. "10:15 AM"')
        p.add_argument("--setup", default="orb")
        p.add_argument("--or-minutes", type=int, default=15)
        p.add_argument("--stop-rule", default="auto", choices=["auto", "or_mid", "or_low", "or_high", "pct"])
        p.add_argument("--replays", type=int, default=10000)
        p.add_argument("--seed", type=int, default=19)
        p.add_argument("--window", type=int, default=2, help="draw from the same time of day +- this many bars")
        p.add_argument("--until", help="end each replay at this time (default 16:00, the close)")
        p.add_argument("--offline", metavar="FILE", help="a saved Yahoo chart JSON (.json or .json.gz), no network")
        p.add_argument("--now", help='pretend it is this time ("2026-10-19 10:15" New York, or ISO with Z)')
        cost_flags(p)

    def cost_flags(p):
        p.add_argument("--slippage-bps", type=float, help="slippage a side in bps of the entry (default %g, "
                                                           "or slippage_bps in the config)" % SLIPPAGE_BPS)
        p.add_argument("--commission", type=float, help="commission a share a side in $ (default %g, "
                                                         "or commission_per_share in the config)" % COMMISSION)
        p.add_argument("--config", default="trader/config.json")

    p = sub.add_parser("run", help="replay the plan: 10,000 simulated sessions plus the history")
    common(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("--write", nargs="?", const="trader/timemachine.json", metavar="PATH",
                   help="save the result (default trader/timemachine.json)")
    p = sub.add_parser("chart", help="ASCII fan chart of the replays")
    common(p)
    p.add_argument("--from", dest="from_file", nargs="?", const="trader/timemachine.json", metavar="PATH",
                   help="draw a saved result instead of running again")
    p = sub.add_parser("backtest", help="the orb rule on every session of many stocks, after costs")
    p.add_argument("--symbols", help="comma-separated tickers (default: trader/watchlist.json)")
    p.add_argument("--watchlist", default="trader/watchlist.json")
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--stop-rule", default="or_mid", choices=["or_mid", "or_low", "pct"])
    p.add_argument("--stop-pct", type=float, help="with --stop-rule pct: the stop, in %% under the entry")
    p.add_argument("--target-r", type=float, default=1.0)
    p.add_argument("--until", help="out by this time (default 16:00); your flat-by, e.g. 15:55")
    p.add_argument("--max-loss", type=float, help="$ risked a trade, to show R in dollars "
                                                   "(default max_loss_per_trade_usd in the config)")
    p.add_argument("--offline", metavar="DIR", help="a folder of saved charts, <SYM>-60d5m.json(.gz)")
    p.add_argument("--now", help='pretend it is this time ("2026-10-19 16:00" New York)')
    p.add_argument("--range", default="60d", choices=RANGES,
                   help="history of 5-minute bars (default 60d; 6mo, 1y, 2y need Alpaca's data)")
    p.add_argument("--save", metavar="DIR", help="also save each chart fetched, for --offline later")
    p.add_argument("--spy-trend", type=int, metavar="N", help="only when SPY closed above its N-day average")
    p.add_argument("--min-rvol", type=float, metavar="X",
                   help="only when the opening range traded at least X times its usual volume")
    p.add_argument("--half-r", type=float, metavar="R", help="sell half at R, the rest runs to the target")
    p.add_argument("--breakeven-r", type=float, metavar="R", help="move the stop to the entry once R is reached")
    p.add_argument("--compare", action="store_true",
                   help="run the rule and its filter and exit variants side by side")
    cost_flags(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("--write", nargs="?", const="trader/backtest.json", metavar="PATH",
                   help="save the result (default trader/backtest.json)")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"run": cmd_run, "chart": cmd_chart, "backtest": cmd_backtest}[a.cmd](a)
    except TMError as e:
        print("timemachine.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
