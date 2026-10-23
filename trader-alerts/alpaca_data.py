"""Alpaca market data in Yahoo's chart shape, so a skill can use Alpaca instead of Yahoo
without changing how it reads the answer.

The same file ships in every skill that fetches prices (trader-alerts, trader-entry,
trader-exit, trader-scanner, trader-timemachine); a test keeps the copies identical.

  answer(path)  takes the Yahoo path the skill would have asked for
                  /v8/finance/chart/TSLA?range=60d&interval=5m
                  /v7/finance/spark?symbols=AAPL,BRK-B&range=3mo&interval=1d
                and returns the same JSON text, built from Alpaca's bars and snapshots.

Which source a skill uses (source()):
  TRADER_DATA=alpaca or TRADER_DATA=yahoo, when set; otherwise Alpaca when its keys are in
  the environment, Yahoo when they are not.

Keys come only from environment variables, never from a file, a flag or the chat, first pair found:
  TRADER_ALPACA_KEY and TRADER_ALPACA_SECRET (names only these skills read: use them when another
  program on the same PC already reads the standard names, so the two never share keys by accident),
  ALPACA_API_KEY and ALPACA_SECRET_KEY (the names Alpaca's own MCP server uses), or
  APCA_API_KEY_ID and APCA_API_SECRET_KEY (the names Alpaca's SDKs use).
Market data needs no trading permission; paper keys work.

Feed (ALPACA_FEED):
  auto (default)  the consolidated tape (SIP) for everything older than 16 minutes, which
                  Alpaca's free plan allows, and IEX for the last 16 minutes of a live session.
                  Prices match the tape; the volume of those last bars is IEX's share only.
  sip             a paid plan: SIP for everything, up to now.
  iex             IEX only: free and live, but its volume is a few percent of the market's
                  and a quiet stock can have 5-minute bars missing.

Only the standard library. GET only, to https://data.alpaca.markets.
"""

import datetime
import json
import os
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://data.alpaca.markets"
KEY_PAIRS = (("TRADER_ALPACA_KEY", "TRADER_ALPACA_SECRET"), ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"),
             ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"))
DELAY = datetime.timedelta(minutes=16)   # the free plan's SIP data is 15 minutes behind
TIMEFRAMES = {"1m": ("1Min", 60), "2m": ("2Min", 120), "5m": ("5Min", 300), "15m": ("15Min", 900),
              "30m": ("30Min", 1800), "60m": ("1Hour", 3600), "1h": ("1Hour", 3600), "1d": ("1Day", 86400)}
# Yahoo range -> (calendar days to ask for, sessions to keep or None for "all in the window")
RANGES = {"1d": (6, 1), "5d": (12, 5), "1mo": (31, None), "60d": (60, None), "3mo": (92, None),
          "6mo": (183, None), "1y": (366, None), "2y": (731, None), "5y": (1827, None)}
SNAP_MAX = 100       # symbols per snapshots request
PAGE = 10000

_urlopen = urllib.request.urlopen   # tests swap these two


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


class AlpacaError(Exception):
    pass


# ---------------------------------------------------------------- settings

def keys():
    """(key, secret) from the environment, or None."""
    for k, s in KEY_PAIRS:
        if os.environ.get(k) and os.environ.get(s):
            return os.environ[k], os.environ[s]
    return None


def source():
    """'alpaca' or 'yahoo'."""
    want = (os.environ.get("TRADER_DATA") or "").strip().lower()
    if want in ("alpaca", "yahoo"):
        return want
    return "alpaca" if keys() else "yahoo"


def feed():
    f = (os.environ.get("ALPACA_FEED") or "auto").strip().lower()
    if f not in ("auto", "sip", "iex"):
        raise AlpacaError("ALPACA_FEED must be auto, sip or iex, not %r" % f)
    return f


# ---------------------------------------------------------------- New York time

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def eastern_offset(utc):
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))
    return -4 if start <= utc.replace(tzinfo=None) < end else -5


def eastern_date(utc):
    return (utc.replace(tzinfo=None) + datetime.timedelta(hours=eastern_offset(utc))).date()


def eastern_to_epoch(day, hh, mm):
    local = datetime.datetime.combine(day, datetime.time(hh, mm))
    for off in (-4, -5):
        utc = local - datetime.timedelta(hours=off)
        if eastern_offset(utc) == off:
            break
    return int(utc.replace(tzinfo=datetime.timezone.utc).timestamp())


def _rfc(dt):
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_t(text):
    t = str(text).replace("Z", "+00:00")
    if "." in t:   # 2026-10-23T13:30:00.123456789+00:00: Python wants at most 6 digits
        head, tail = t.split(".", 1)
        frac = "".join(ch for ch in tail if ch.isdigit())
        t = head + "." + frac[:6].ljust(6, "0") + tail[len(frac):]
    return datetime.datetime.fromisoformat(t)


# ---------------------------------------------------------------- HTTP

def _get(path, params):
    k = keys()
    if not k:
        raise AlpacaError("no Alpaca keys in the environment: set TRADER_ALPACA_KEY and TRADER_ALPACA_SECRET "
                          "(see the skill's SKILL.md), or TRADER_DATA=yahoo")
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"APCA-API-KEY-ID": k[0], "APCA-API-SECRET-KEY": k[1],
                                               "Accept": "application/json"})
    try:
        with _urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            body = ""
        finally:
            try:
                e.close()
            except Exception:
                pass
        if e.code in (401, 403):
            hint = ("Alpaca refused the keys (HTTP %d)" % e.code if e.code == 401 or "subscription" not in body
                    else "Alpaca's plan does not include this data (HTTP 403): set ALPACA_FEED=auto or iex")
            raise AlpacaError(hint)
        if e.code == 429:
            raise AlpacaError("Alpaca is rate limiting (HTTP 429). Wait a minute and run it again.")
        raise AlpacaError("Alpaca answered HTTP %d: %s" % (e.code, body.strip() or "no detail"))
    except (urllib.error.URLError, OSError) as e:
        raise AlpacaError("could not reach Alpaca: %s" % getattr(e, "reason", e))
    except ValueError:
        raise AlpacaError("Alpaca returned something that is not JSON")


def _bars(symbols, timeframe, start, end, which):
    """{SYMBOL: [bar, ...]} oldest first, all pages."""
    out = {s: [] for s in symbols}
    token = None
    while True:
        params = {"symbols": ",".join(symbols), "timeframe": timeframe, "start": _rfc(start), "end": _rfc(end),
                  "limit": PAGE, "adjustment": "split", "feed": which, "sort": "asc"}
        if token:
            params["page_token"] = token
        doc = _get("/v2/stocks/bars", params)
        for sym, rows in (doc.get("bars") or {}).items():
            out.setdefault(sym, []).extend(rows or [])
        token = doc.get("next_page_token")
        if not token:
            return out


def bars(symbols, timeframe, start, end):
    """Bars over [start, end) by the feed rule in the module doc."""
    f = feed()
    now = _now()
    end = min(end, now)
    if f != "auto":
        return _bars(symbols, timeframe, start, end, f)
    cut = now - DELAY
    out = _bars(symbols, timeframe, start, min(end, cut), "sip") if start < cut else {s: [] for s in symbols}
    if end > cut and timeframe != "1Day":
        tail = _bars(symbols, timeframe, max(start, cut), end, "iex")
        for sym, rows in tail.items():
            have = {b["t"] for b in out.get(sym, [])}
            out.setdefault(sym, []).extend(b for b in rows if b["t"] not in have)
    return out


def snapshots(symbols):
    """{SYMBOL: snapshot}. Live (IEX) unless ALPACA_FEED=sip."""
    which = "sip" if feed() == "sip" else "iex"
    out = {}
    for i in range(0, len(symbols), SNAP_MAX):
        doc = _get("/v2/stocks/snapshots", {"symbols": ",".join(symbols[i:i + SNAP_MAX]), "feed": which})
        out.update({k: v for k, v in (doc or {}).items() if isinstance(v, dict)})
    return out


# ---------------------------------------------------------------- Yahoo shape

def to_alpaca(ysym):
    return urllib.parse.unquote(ysym).upper().replace("-", ".")


def to_yahoo(sym):
    return sym.upper().replace(".", "-")


def _window(rng, interval):
    if interval not in TIMEFRAMES:
        raise AlpacaError("no Alpaca bars for interval %s" % interval)
    if rng not in RANGES:
        raise AlpacaError("no Alpaca equivalent for Yahoo range %s" % rng)
    days, keep = RANGES[rng]
    now = _now()
    return now - datetime.timedelta(days=days + (1 if interval == "1d" else 0)), now, keep


def _result(ysym, interval, rows, keep, snap=None):
    """Alpaca bars (+ snapshot) -> one Yahoo chart result, or None when there are no bars."""
    tf_secs = TIMEFRAMES[interval][1]
    daily = interval == "1d"
    stamped = []
    for b in rows:
        t = _parse_t(b["t"])
        day = eastern_date(t)
        ts = eastern_to_epoch(day, 9, 30) if daily else int(t.timestamp())
        if not daily and not eastern_to_epoch(day, 9, 30) <= ts < eastern_to_epoch(day, 16, 0):
            continue   # Alpaca includes pre- and post-market bars; Yahoo's default chart does not
        stamped.append((day, ts, b))
    prev_close = None
    if keep:
        days = sorted({d for d, _, _ in stamped})[-keep:]
        before = [b for d, _, b in stamped if days and d < days[0]]
        if daily and before:
            prev_close = before[-1]["c"]
        stamped = [x for x in stamped if x[0] in days]
    if not stamped:
        return None
    last_day, last_ts, last = stamped[-1]
    now = _now()
    meta = {"currency": "USD", "symbol": ysym, "exchangeName": "Alpaca", "instrumentType": "EQUITY",
            "dataGranularity": interval, "range": None, "source": "alpaca",
            "regularMarketPrice": last["c"],
            "regularMarketTime": int(min(now.timestamp(), (last_ts if daily else last_ts + tf_secs))),
            "regularMarketVolume": int(last.get("v") or 0)}
    same_day = [b for d, _, b in stamped if d == last_day]
    meta["regularMarketDayHigh"] = max(b["h"] for b in same_day)
    meta["regularMarketDayLow"] = min(b["l"] for b in same_day)
    if prev_close is not None:
        meta["chartPreviousClose"] = prev_close
    if snap:
        trade = snap.get("latestTrade") or {}
        day_bar, prev_bar = snap.get("dailyBar") or {}, snap.get("prevDailyBar") or {}
        if trade.get("p") and trade.get("t"):
            meta["regularMarketPrice"] = trade["p"]
            meta["regularMarketTime"] = int(_parse_t(trade["t"]).timestamp())
        if day_bar.get("h"):
            meta["regularMarketDayHigh"], meta["regularMarketDayLow"] = day_bar["h"], day_bar["l"]
            meta["regularMarketVolume"] = int(day_bar.get("v") or 0)
        if prev_bar.get("c"):
            meta["previousClose"] = prev_bar["c"]
    quote = {k: [b.get(k[0]) for _, _, b in stamped] for k in ("open", "high", "low", "close")}
    quote["volume"] = [int(b.get("v") or 0) for _, _, b in stamped]
    return {"meta": meta, "timestamp": [ts for _, ts, _ in stamped], "indicators": {"quote": [quote]}}


def _no_data(ysym):
    return {"code": "Not Found", "description": "No data found, symbol may be delisted (Alpaca has no bars for %s)"
            % ysym}


def chart(ysym, rng, interval):
    """Yahoo v8 chart JSON (as a dict) for one symbol."""
    start, end, keep = _window(rng, interval)
    sym = to_alpaca(ysym)
    rows = bars([sym], TIMEFRAMES[interval][0], start, end).get(sym) or []
    snap = snapshots([sym]).get(sym) if rows else None
    res = _result(to_yahoo(sym), interval, rows, keep, snap)
    if res is None:
        return {"chart": {"result": None, "error": _no_data(to_yahoo(sym))}}
    res["meta"]["range"] = rng
    return {"chart": {"result": [res], "error": None}}


def spark(ysyms, rng, interval):
    """Yahoo v7 spark JSON (as a dict) for many symbols; unknown symbols are left out."""
    start, end, keep = _window(rng, interval)
    syms = [to_alpaca(s) for s in ysyms]
    got = bars(syms, TIMEFRAMES[interval][0], start, end)
    result = []
    for sym in syms:
        res = _result(to_yahoo(sym), interval, got.get(sym) or [], keep)
        if res is not None:
            res["meta"]["range"] = rng
            result.append({"symbol": to_yahoo(sym), "response": [res]})
    return {"spark": {"result": result, "error": None if result else _no_data(",".join(ysyms))}}


def answer(path_or_url):
    """A Yahoo chart or spark path (or full URL) -> the JSON text Yahoo would have sent."""
    u = urllib.parse.urlsplit(path_or_url)
    q = dict(urllib.parse.parse_qsl(u.query))
    rng, interval = q.get("range", "1d"), q.get("interval", "1d")
    if "/v8/finance/chart/" in u.path:
        return json.dumps(chart(u.path.rsplit("/", 1)[-1], rng, interval))
    if u.path.endswith("/v7/finance/spark"):
        return json.dumps(spark([s for s in q.get("symbols", "").split(",") if s], rng, interval))
    raise AlpacaError("no Alpaca equivalent for %s" % u.path)


def main(argv=None):
    """python alpaca_data.py [TICKER]: which source the skills use, and one live check."""
    import sys
    argv = sys.argv[1:] if argv is None else argv
    sym = (argv[0] if argv else "SPY").upper()
    k = keys()
    pair = next((p for p in KEY_PAIRS if os.environ.get(p[0]) and os.environ.get(p[1])), None)
    print("Source: %s (TRADER_DATA=%s, Alpaca keys %s), feed %s" % (
        source(), os.environ.get("TRADER_DATA") or "not set", "in " + pair[0] if pair else "not found",
        os.environ.get("ALPACA_FEED") or "auto"))
    if not k:
        print("Set TRADER_ALPACA_KEY and TRADER_ALPACA_SECRET in your environment to use Alpaca.")
        return 1
    try:
        doc = chart(to_yahoo(sym), "1d", "5m")
    except AlpacaError as e:
        print("Alpaca: " + str(e))
        return 1
    res = (doc["chart"]["result"] or [None])[0]
    if not res:
        print("Alpaca has no bars for %s." % sym)
        return 1
    m, n = res["meta"], len(res["timestamp"])
    last = datetime.datetime.fromtimestamp(res["timestamp"][-1], datetime.timezone.utc)
    print("%s: %d five-minute bars, last at %s UTC, last price %s, previous close %s" % (
        sym, n, last.strftime("%Y-%m-%d %H:%M"), m.get("regularMarketPrice"), m.get("previousClose", "?")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
