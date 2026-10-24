---
name: trader-timemachine
description: >-
  Time Machine. Replays a planned day trade before you risk a dollar: 10,000
  simulated sessions from the stock's own last 60 days of 5-minute bars,
  plus the same rule on every past session. Prints the odds of target first,
  stop first or neither by the close, the average R before and after costs
  (slippage, commission, stops that gap), the history and one honest
  verdict. Its backtest runs the opening-range rule on many stocks at once
  and says whether it has an edge after costs. Use when the user says "run
  the time machine", "replay this trade", "what are the odds", "monte
  carlo", "backtest this setup", "backtest the strategy", "does it make
  money after costs", "is this a coin flip", or after /trader-exit writes a
  plan, before /trader-budget.
argument-hint: "[path to plan.json, or TICKER]"
---

# trader-timemachine

The Time Machine sits between the plan and the money. trader-exit has set
the entry, the stop and the target. Before anything is sized or sent, this
skill asks one question: if today plays out like the stock's own last 60
days, how often does this plan hit the target first, the stop first, or
neither? It answers with 10,000 replays and with the real record of the same
rule on every past session. It is a simulation built from the stock's own
recent bars, not a prediction. Day trading loses most people money, and a
good-looking replay does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run                    # trader/plan.json, 10,000 replays, seed 19
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --write            # also save trader/timemachine.json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --until 15:55      # end each replay at your flat-by time
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --symbol TSLA --date 2026-10-19 \
    --entry 364.18 --stop 358.32 --target 370.04 --entry-time "10:15 AM"
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" chart --from           # ASCII fan chart of the saved run
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" backtest --symbols TSLA,AAPL,NVDA,AMD,META --until 15:55
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" backtest --target-r 2 --stop-rule or_low --write   # trader/watchlist.json
```

Other flags: `--replays N`, `--seed N` (default 19), `--window N` (default 2
bars), `--offline FILE` (a saved Yahoo chart JSON, `.json` or `.json.gz`),
`--now "2026-10-19 10:15"` (New York time) to pin the clock,
`--slippage-bps N` (default 2, each side) and `--commission N` (dollars a
share each side, default 0) for the costs; both also read `slippage_bps`
and `commission_per_share` from `trader/config.json`.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the plan from trader-exit: symbol, date, entry, entry_time / entry_bar, stop, stop_rule, target, target_r, opening range |
| Yahoo chart API | one request: 60 days of 5-minute bars (`range=60d&interval=5m`), free and unofficial, can lag |
| `trader/config.json` | optional: `slippage_bps`, `commission_per_share`, and `max_loss_per_trade_usd` for the backtest's dollars |
| `trader/backtest.json` | with `backtest --write`: the rule, the costs, each symbol's numbers, the pooled stats and every trade |
| `trader/timemachine.json` | with `--write`: `{plan, mc, history}` plus the verdict. `mc` holds the odds, percentiles, the fan-chart bands, 160 sample paths and a 24-bin histogram of the close |

## How it works

1. **The pool.** It keeps only FULL sessions before the plan's date: all 78
   five-minute bars, 9:30 to 4:00 New York time. Half days, gaps and today
   are left out. Every bar becomes four ratios to the bar before it:
   open, high, low and close over the previous close.
2. **One replay.** Start at the entry price. For each bar left in the day,
   pick a past bar from the same time of day (plus or minus 2 bars) at
   random, and scale it to the current price. Bar by bar, it checks the stop
   first, then the target. A bar that touches both counts as the stop.
3. **10,000 replays** give four odds. Target first. Stop first. Neither,
   which means the trade was still open at `--until` (default 4:00 PM, the
   close, or 1:00 PM on an NYSE early-close day). Green, which means target
   first or above the entry at `--until`.
   R is +1 at a 1R target, -1 at the stop, and otherwise the distance from
   the entry at `--until` divided by the risk. A gap through the stop still
   counts as -1R here. A real fill can be worse.
4. **The history.** The plan's own rule (the same opening range, stop rule
   and target R) runs again on every one of those past sessions. That is
   what really happened, not a simulation.
5. **The verdict** is one line from the numbers. It looks only at the
   replays that hit one side, and asks how often the target came first. A
   bigger target needs to win less often to break even: 1 in (1 + R). That
   is 50 % at 1R, 33 % at 2R, 25 % at 3R. Less than 5 points from break-even:
   "Close to a coin flip" at 1R, "Close to break-even" at any other R. 5 to
   15 points: "Leans toward the target" or "Leans toward the stop". 15 or
   more: "Strongly toward" one of them. So a 2R plan with target first
   24.1 % and stop first 50.5 % is close to break-even (it needs 33 % of
   the 74.6 % that hit a side, and got 32 %), not a loser.
6. **After costs.** The odds and the average R above are the plain ones.
   Under them, "After costs" is the average R once two real things are
   counted: slippage and commission on the way in and on the way out
   (`cost_R` = 2 x entry x slippage bps / 10,000 + 2 x commission, over the
   risk a share), and a stop that gaps through fills at that bar's open, not
   at the stop. The history line gets its own after-costs average. When both
   are below zero, the report says so in one line.
7. **Below the verdict**, when it applies: a line when more than 40 % of the
   replays hit neither (the price at `--until` decides those), and the
   average R when its sign goes against the lean. Those can only point the
   other way because of the replays that hit neither.

## The backtest

`timemachine.py backtest` answers the question before the trade: does this
rule make money at all, after costs? It runs the opening-range breakout (the
opening range, the stop rule `or_mid`, `or_low` or `pct` with `--stop-pct`,
and `--target-r`) on every full session in each stock's 60 days of 5-minute
bars, for every ticker in `--symbols` or in `trader/watchlist.json`, then
pools the trades:

- per stock: sessions, how often it fired, target / stop / neither, the
  average R before and after costs;
- together: the win rate after costs, the average R after costs with its
  95 % range (1.96 standard errors either side), the total R, the worst
  drawdown in R, the profit factor and how many stops gapped through;
- in dollars, at `--max-loss` (or the config's `max_loss_per_trade_usd`) a
  trade;
- one verdict: "An edge after costs" only when the whole 95 % range is above
  zero, "Loses after costs" when it is all below, "No proven edge" when it
  includes zero, and "Too few trades to tell" under 30 trades.

One Yahoo request per ticker, half a second apart, 200 tickers at most. A
ticker that fails is skipped and listed, it does not stop the run. Yahoo
keeps about 60 days of 5-minute bars, so a backtest covers a few months at
most: say so with the result.

**Years of history with Alpaca.** With Alpaca's data (see below),
`--range 6mo`, `1y` or `2y` replays that much history instead; Yahoo refuses
it in one line. `--save DIR` keeps every chart fetched so later runs can use
`--offline DIR` without asking Alpaca again. A rate limit (HTTP 429) waits 15
seconds and tries again, up to four times.

**Filters and exits.** `--spy-trend N` takes a trade only when SPY closed
above its N-day average the day before; `--min-rvol X` only when the opening
range traded at least X times its average over the previous 20 sessions.
`--breakeven-r R` moves the stop to the entry once a bar reaches R (from the
next bar on), `--half-r R` sells half there and lets the rest run to the
target, the stop or the close. Both filters look only at what was known at the
entry. Each run also prints the average R of the first and the second half of
its trades: an edge should show in both.

**Compare.** `--compare` runs the rule as given and six variants on the same
data (each filter, both, a 2R target, 2R with the stop moved to the entry, half
at 1R and the rest at 2R) and prints one row each: trades, average R after
costs, its 95 % range, both halves and a one-word verdict. Say with it that the
best of several variants is partly luck: trust one only when its whole 95 %
range is above zero and both halves are positive.

**The confluence lab (`confluence.py`).** Phase 1 of the confluence plan:
the higher-timeframe context and five entry signals, each tested on its own.
`python3 confluence.py backtest --symbols AAPL,MSFT --offline storico` reads
the files `backtest --save` wrote (or 2 years from Alpaca without `--offline`).
The context votes before the open, from earlier sessions only: the daily trend
(close against the 50-day average, the 20-day average rising or falling), the
4-hour and the 1-hour structure (the last two swing highs and lows); two votes
one way set the day's bias (buys only, sells only, or none). The signals on
5-minute bars: EMA 9/21 cross, Ichimoku cloud break (9/26/52), a return into a
fair value gap or an order block opened that day, a sweep of yesterday's low or
high. Phase 2 adds yesterday's volume profile (50 price bins: the point of
control, the 70 % value area, the thinnest bin on each side of the POC as the
low volume nodes): back into value area after opening outside it, two closes
outside it after opening inside, a close through a low volume node; and ICT in
the 9:30-11:00 killzone (yesterday's low or high swept, then a fair value gap
the other way, entered on the first return into it). `--signals fvg,ict` runs a
few. All get the same exits (stop 1 ATR(14), target 2R, out at 15:55, costs
off) and one trade a signal a stock a day, alone and with the context, beside
seeded random entries with the same exits. A signal is a candidate only when it
beats random in both years; say that 12 variants ran on the same data, so a
candidate is not an edge until its own 95 % range is above zero. Short sales are
simulated for the test only. `confluence.py context --symbols ...` prints the
bias for the next session.

**The seed.** The draws come from Python's `random.Random(seed)`, seed 19 by
default. Same plan, same data, same seed: the same numbers, on any machine.
Say the seed with every result so the user can repeat it.

**5-minute bars only.** Yahoo keeps 60 days of 5-min bars but only about 7
days of 1-min bars, too few sessions for a fair pool. It refuses, in one
plain line, when there are fewer than 20 full sessions, when the entry bar is
not in the data (wrong date, not closed yet, a future day), or when the plan
does not make sense (a stop above a buy's entry, an entry off the 5-minute
grid). A live in-progress bar is never used.

## Steps

1. Make sure `trader/plan.json` is today's plan from `/trader-exit`. If there
   is none, run that first, or pass the plan as flags.
2. Run `timemachine.py run --write`. Add `--until 15:55` when the plan has a
   flat-by time, so "neither" means still open at the time you would get out.
3. Tell the user, in this order: the four odds, the median and the 5 to 95
   range of the price at the close, the average R and the average R after
   costs, the history line with its after-costs average, then the verdict
   line and any line under it exactly as printed. Say the seed.
4. If the verdict is a coin flip or close to break-even, say so plainly. Do
   not dress it up. If the history and the replays disagree, say that too.
5. Say once that this is built from the last 60 days and is not a prediction.
   Earnings, news and halts can do things those 60 days never did. Check
   `/trader-news` for the stock before going on.
6. If the user still wants the trade, hand off to `/trader-budget` to size it.

For a strategy rather than one trade, run `backtest` on the watchlist or on
a handful of liquid stocks, with `--until` at the flat-by time. Repeat the
verdict exactly as printed. If it says "No proven edge" or "Loses after
costs", say plainly that the numbers do not support trading the rule as it
is, and suggest paper trading or another rule instead.

## Example: the reel

TSLA, Monday 2026-10-19. The 15-minute opening range is 354.47 to 362.18.
The 10:10 bar closes above it: entry 364.18 at 10:15 AM (bar 8), stop 358.32
(the range midpoint, rounded down), target 370.04 (1R, risk 5.86 a share).

```
10,000 replays of the rest of the session, 10:15 AM to 4:00 PM (69 five-minute bars), seed 19.
  target first   29.4 %
  stop first     25.5 %
  neither        45.1 %   still open at 4:00 PM
  green          51.7 %   target first, or above the entry at 4:00 PM
  Price at 4:00 PM: median 364.36, 90 % of replays between 355.54 and 373.54.
  Average R: +0.034
History: the same rule on each of those 59 real sessions fired 33 times:
13 target, 15 stop, 5 neither. Average R -0.062.
Close to a coin flip: target first 29.4 % vs stop first 25.5 %.
45.1 % of the replays hit neither by 4:00 PM; the price at 4:00 PM decides those.
```

At 1R the target has to come first in half of the replays that hit one
side. It did in 53.6 %. The replays came out close to a coin flip, and the
real history was slightly worse than even. That is the honest answer for this setup, and it is what
the user should hear before `/trader-budget` sizes 8 shares.

## Rules

- Every number comes from the run. Never round a coin flip up into an edge.
- The verdict line and the lines under it are printed by the tool. Repeat
  them, do not rewrite them. Never judge a 2R or 3R plan by target first vs
  stop first alone: the verdict already weighs the target's R.
- Simulated odds are not predictions and not promises. Say it once, plainly.
- Free Yahoo data is unofficial and can lag. Bars are 5-minute bars, checked
  bar by bar, never tick by tick and never real time.
- Always say the seed, so any run can be repeated.
- One plan per run. A changed entry, stop, target or date is a new run.
- Lead with the after-costs numbers when they change the picture: a rule
  that is positive before costs and negative after is a losing rule.
- A backtest that looks good on 60 days is a reason to paper trade it, not
  proof. Never call a backtest a forecast.

## Alpaca data instead of Yahoo

`alpaca_data.py`, next to this script, fetches the same bars from Alpaca's
market data API and hands them over in Yahoo's shape, so nothing else changes.
It is used when `TRADER_ALPACA_KEY` and `TRADER_ALPACA_SECRET` are set in your
environment (names only these skills read, so another program's Alpaca keys are
never picked up by accident), or else `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`,
or `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY`; Yahoo when none are. `TRADER_DATA=yahoo` or `TRADER_DATA=alpaca` forces one. Paper keys work:
market data needs no trading permission. Set the keys yourself in your own
terminal, never in the chat. `python3 alpaca_data.py TSLA` says which source
is in use and fetches one chart to prove it.

`ALPACA_FEED=auto` (the default) takes the full consolidated tape (SIP) for
everything older than 16 minutes, which Alpaca's free plan allows, and IEX for
the last 16 minutes of a live session: prices match the tape, but the volume
of those last bars is IEX's share only. `ALPACA_FEED=sip` is for a paid plan,
`ALPACA_FEED=iex` for IEX only.

## What it never does

It never places an order, never sizes a position, never changes the plan,
and never calls a result a sure thing. It never forecasts a P&L or a profit: the backtest's dollars are the past
record of a rule at your risk a trade, not a promise.
It never uses a bar that has not closed yet, and never mixes today's bars
into the pool.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
