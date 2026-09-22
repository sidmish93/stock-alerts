# NSE trigger alerts

Watches a list of companies you define and pushes a Telegram message to your
phone when one of them crosses a trigger:

**Price past ±5%**, measured against yesterday's close. That alert also brings
the company's **named shareholders** from BSE, restated for block and bulk deals
since the last quarter, as a follow-up message.

Volume does not raise an alert, but every alert carries it: how much has traded,
what that projects to for the full day, and how that compares with the last five
sessions. So a 5% move that comes with 21× normal volume looks different from one
that comes on nothing.

Runs on GitHub Actions: free, and nothing of yours has to stay switched on.

## Setup

### 1. Create the Telegram bot

1. In Telegram, message [@BotFather](https://t.me/BotFather) and send `/newbot`.
2. Pick a name and username. BotFather replies with a token like
   `8123456789:AAF...`. That is your `TELEGRAM_BOT_TOKEN`.
3. Send your new bot any message (say `hi`). A bot cannot start a conversation,
   so this is what allows it to message you.
4. Open this in a browser with your token pasted in:

   ```
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```

   Find `"chat":{"id":123456789` in the reply. That number is your
   `TELEGRAM_CHAT_ID`.

### 2. The universe

`universe.txt` holds **400 NSE-listed companies**: the original 299 coverage
names, grouped by sector across 36 sectors (68 large cap, 83 mid, 148 small),
plus 101 non-coverage PE holdings.

Symbols were resolved by **ISIN**, not by ticker guesswork: a BSE scrip code
becomes an ISIN via BSE's scrip master, and the ISIN becomes a symbol via NSE's
own equity list. Vendor tickers would not have worked — `HDFCB` is HDFCBANK and
`BAF` is BAJFINANCE. All 299 were then confirmed to price on Yahoo and to carry a
BSE scrip code for shareholding.

BSE was dropped because it carries no BSE scrip code, so shareholding cannot be
fetched. NSDL is a BSE-only listing with no `.NS` series on Yahoo. CDSL is
watched only as a non-coverage PE name for the same shareholding reason.

Non-coverage lines look like
`MEESHO, Meesho | noncoverage | Steadview` or
`CLEANMAX, Clean Max | noncoverage | Steadview; Temasek`.
Their alerts say `non coverage company` and every PE book that holds them.

To change it, edit the file: one NSE symbol per line, then the display name used
in alerts. Lines starting with `#` are ignored, so you can stop watching a name
without losing it. Then check it:

```powershell
python -m alerts.worker --once --dry-run --force
```

Anything Yahoo does not recognise is reported as having no usable quote. The full
400 poll a few seconds longer than the original 299.

### 3. Deploy to GitHub Actions

1. Push this folder to a GitHub repository.
2. Settings → Secrets and variables → Actions → add `TELEGRAM_BOT_TOKEN` and
   `TELEGRAM_CHAT_ID`.
3. Actions tab → **NSE trigger alerts** → Run workflow → mode `test`. You should
   get a message on your phone.

That is all. `.github/workflows/alerts.yml` then polls every 10 minutes between
09:15 and 15:30 IST, Monday to Friday, and sends a summary after the close.

**Cost.** Free on a public repo. On a private repo it uses roughly 600 of the
2,000 free minutes a month, so it is free either way.

**Latency.** GitHub queues scheduled jobs and can start them a few minutes late,
so an alert normally reaches you within 10 minutes of the crossing and in the
worst case around 20. It polls every 10 rather than every 15 to keep that worst
case inside your tolerance.

**Why not something always-on.** A Render background worker would be punctual to
the second, but workers are a paid plan and Render's free web tier is spun down
after 15 minutes idle — exactly when the watcher needs to be awake. Given you
ruled out both paying and leaving a laptop on, Actions is the only option that
genuinely fits. `render.yaml` is included anyway, configured as a worker, in case
punctuality ever becomes worth the $7.

### Running locally instead

Optional, and not needed if you deploy the workflow:

```powershell
copy .env.example .env      # then fill in the token and chat id
pip install -r requirements.txt
python -m alerts.worker --test              # check delivery
python -m alerts.worker --once --dry-run    # print what would fire, send nothing
python -m alerts.worker                     # poll continuously
```

`--force` polls even when the market is shut, which is useful for a sanity check
out of hours.

## Daily or intraday: which price rule to keep

Two different percentages can be called "the move", and they disagree exactly
when it matters:

| | Measured against | Includes the overnight gap? |
|---|---|---|
| **daily** | yesterday's close | yes |
| **intraday** | today's open | no |

**Keep `daily`**, which is the default. It is what Screener reports as
`1day return %` — its `dayChangePercent` is literally
`(currentPrice − previousClose) / previousClose` — and it is also what NSE, BSE,
Google Finance and every broker app quote. So when an alert says −3.4%, the
number you see when you go and look it up is the same number. Picking `intraday`
would mean your alerts never agree with any screen you check them against.

What you give up is narrow. The two only diverge on a stock that gaps at the open
and then reverses: gap up 4%, drift 3.5% lower, and it closes +0.4% on the day.
`daily` stays quiet, `intraday` fires. That is a real pattern, but it is the
noisier of the two and the volume trigger usually catches the same day anyway.

The message always prints **both** readings whichever rule fired, so nothing is
hidden — the disabled one is simply not allowed to wake your phone. Set
`PRICE_RULES=daily,intraday` to run both, or `PRICE_RULES=intraday` to swap.

## What an alert looks like

The first line always names the level that fired. Both of these are real readings
from Friday 18 Sep 2026:

```
🟢 JSWINFRA  up past 5% vs prev close
JSW Infrastructure
CMP Rs 348.25   prev close Rs 326.65
vs prev close +6.61%   from open +5.56%
Volume 2.52 cr so far
On pace for 2.52 cr vs 27.67 lakh 5-day avg  (9.12x)
100% of a normal day has traded by now
15:30 IST · 18 Sep
```

```
🔴 GODIGIT  down past 5% vs prev close
Go Digit General Insurance
CMP Rs 239.00   prev close Rs 255.50
vs prev close -6.46%   from open -6.46%
Volume 34.24 lakh so far
On pace for 34.24 lakh vs 1.59 lakh 5-day avg  (21.49x)
100% of a normal day has traded by now
15:30 IST · 18 Sep
```

Both readings appear whichever rule fired, and the volume block is there even
though volume no longer triggers anything. Go Digit at **21× normal volume** is a
very different event from a 6% move on nothing, and that distinction is the
reason the figure stays in the message.

## Shareholders on a 5% move

A ±5% price alert is followed by a second message naming **who actually owns the
company**, following the ipo-tool on your Desktop. That tool does not read
aggregate percentages off a summary page; it goes to BSE's shareholding-pattern
APIs and pulls out the individual holders:

| Endpoint | What it gives |
|---|---|
| `CorporatesSHPSecuritybeta` | the latest published shareholding quarter |
| `Corp_shpPromoterNGroup_ng` | named promoter and promoter-group entities |
| `Corp_shpSec_SHPPubShold_ng` | named public holders, under their category |

Each response carries several tables; the one that matters is the longest whose
rows name a holder, which is the rule the ipo tool uses. Promoter rows are kept
only where the shareholder type is `Promoter` or `Promoter Group`, and anything at
0% is dropped.

For JSW Infrastructure on the move above:

```
📄 JSWINFRA shareholders — JSW Infrastructure

Promoters 73.92%
• 69.52%  Sajjan Jindal, Sangita Jindal (Trustees of…  Promoter
• 2.20%  JSL Limited  Promoter Group
• 2.20%  Siddeshwari Tradex Private Limited  Promoter Group

Largest public holders
• 2.37%  SBI CONTRA FUND  Mutual Fund
• 2.37%  HDFC TRUSTEE COMPANY LIMITED-HDFC FLEXI CAP…  Mutual Fund
• 1.97%  FIDELITY ADVISOR INTERNATIONAL CAPITAL APPR…  FPI
• 1.82%  GOVERNMENT OF SINGAPORE  FPI
• 1.00%  QUANT MUTUAL FUND - QUANT MID CAP FUND  Mutual Fund

BSE shareholding pattern · June 2026 (as of 30 Jun 2026)
```

### No browser needed

The ipo tool drives BSE through Playwright and real Chrome, because BSE returns
403 to plain HTTP clients. A Chrome-impersonating `curl_cffi` handle clears the
same bar, so this needs no browser and runs inside GitHub Actions unchanged.

### Telling holders from subtotals

BSE mixes named entities and category subtotals into the same table. The ipo tool
separates them by whether the rendered page prints the row in **bold** — which is
precisely why it needs a browser. Two signals in the data replace that:

1. A named entity is one shareholder, so `Fld_NoOfShareHolders` is 1. A subtotal
   counts its members: Reliance's `HUF` line reports 6,601, Delhivery's
   `Employees` line 709.
2. A few subtotals do report a count of 1, so their labels are named outright —
   matched **whole**, never as a substring, because `Insurance Companies` is a
   category while `Life Insurance Corporation of India` is a real holder.

Without this, "HUF", "LLP", "Clearing Members" and "Trusts" would appear as
though they were shareholders.

That label list applies to **public rows only**. A promoter row has already been
vouched for by its shareholder type, and some genuine promoters are named exactly
like a category: the President of India holds 96.5% of LIC, 58.89% of ONGC and
65% of SAIL. Applying the list to promoters made every PSU in the universe report
no promoter at all. The holder-count test still applies on both sides, so
aggregate rows are caught either way.

### Other details

- **A separate message, sent after the alert**, so a slow or unavailable BSE
  response can never delay or swallow the alert itself. If BSE has no pattern for
  a symbol, the alert still arrives and the follow-up is skipped.
- **Every name that crosses gets one.** There is no cap. Friday 18 Sep had
  fifteen names past 5% and all fifteen were covered, which added about 40
  seconds to that poll. `SHAREHOLDING_MAX_PER_DAY` can impose a ceiling if you
  ever want one; `0` is no limit.
- **Once per name per day**, however many levels fire — the data is quarterly.
- **Every holder is listed**, however many there are. Reliance's twenty promoter
  vehicles and Lenskart's sixteen named public holders all appear. The note is
  shortened only if it would breach Telegram's hard 4,096-character limit, which
  nothing sampled comes close to — the widest register measured 2,244 characters.
- A company with no promoter says so rather than showing 0.00% — Delhivery is
  professionally managed and reports no promoter at all.
- Symbols resolve to BSE scrip codes through BSE's own equity master, cached for
  a day. Its ticker matches the NSE symbol for ordinary equity; ISIN also works.
- `SHAREHOLDING_AT_PCT=0` switches the whole thing off.

### Restated for block and bulk deals since the quarter

A pattern is as-of its quarter end: a June pattern is the book on 30 June. So
disclosed NSE and BSE block and bulk trades from 1 July through today are applied
in share counts — sells subtracting, buys adding — exactly as `deals.py` does in
the ipo-tool. Percentages come off shares outstanding, taken as the **median** of
`shares ÷ (pct/100)` across holders above 0.05%, so one rounded percentage cannot
skew the denominator.

Real output for Trident, where two promoter entities swapped 13 crore shares
after quarter end:

```
📄 TRIDENT shareholders

Promoters 73.68%
• 45.75% → 48.30%  TRIDENT GROUP LIMITED  Promoter Group
• 27.63% → 25.08%  MADHURAJ FOUNDATION  Promoter Group
• 0.30%  LOTUS GLOBAL FOUNDATION  Promoter Group

Block / bulk deals since 01 Jul
• TRIDENT GROUP LIMITED bought 13.00 cr  45.75% → 48.30%
• MADHURAJ FOUNDATION sold 13.00 cr  27.63% → 25.08%
```

The promoter total correctly stays at 73.68% — nothing left the block, it just
moved between two vehicles. And for Groww, the pre-IPO investors selling down:

```
Block / bulk deals since 01 Jul
• Peak XV Partners Investments VI-1 sold 9.17 cr  15.68% → 14.22%
• YC Holdings II, LLC sold 7.47 cr  8.63% → 7.44%
• Ribbit Capital V L.P. sold 6.32 cr  5.64% → 4.63%
```

Three cleanups stand between the raw feeds and those numbers, all from the ipo
tool, and each one changes the answer:

1. **Block and leftover bulk.** The two feeds often describe the same client-day,
   so block quantity counts once and only bulk beyond it is added.
2. **Self-trades dropped.** A client on both sides of one day on one exchange is
   trading against itself. Kalyan Jewellers had 26 legs in the window that all
   netted to nothing this way — prop desks in and out the same day. Counting them
   would have invented holdings changes that never happened.
3. **One deal, two exchanges.** Near-equal quantities on NSE and BSE are one deal
   counted once; genuinely different quantities are two prints summed.

A buyer the pattern does not name is reported as an amount bought, never as a
stake: whatever it held below the 1% disclosure line is not published, so its
total genuinely cannot be derived. Those read
`bought 12.48 cr (≈0.65% of the company; total stake not disclosed)`, and only
above 0.5% — smaller ones are noise. Unmatched net *sellers* are dropped entirely,
since without a starting stake the sale quantifies nothing.

Two departures from the ipo-tool, both in name matching, both of which I hit on
real data:

- The tool folds `AND` to `&`, but its own tokeniser strips `&` as punctuation
  first, so `X and Y` became `X & Y` while `X & Y` became `X Y` — the two never
  matched. The connector is now dropped on both sides, which matters for joint
  holdings like "Gautambhai Adani & Rajeshbhai Adani".
- Names are also compared ignoring spaces, so `Ribbit Capital V L.P.` matches
  `RIBBIT CAPITAL V LP`. Full equality of the compact forms, so it cannot pull in
  unrelated parties.

Set `POST_SHP_ADJUSTMENT=false` to report the raw quarterly pattern only.

### What this cannot tell you

- **NSE refuses most cloud addresses.** If its feed is unavailable the message
  says so rather than implying nothing traded — `NSE feed unavailable, so this may
  be incomplete`. BSE answers reliably. Both work from a local machine.
- **Only disclosed deals.** Block and bulk reporting has thresholds, so ordinary
  on-market accumulation is invisible. A stake can move without any print.
- **A promoter total can look wrong on a transfer to an undisclosed vehicle.**
  Adani Power reads `74.96% → 74.31%` because Ardour Investment Holding sold
  12.48 cr while Adani Infra (India) bought the identical amount but is not a
  named holder in the pattern, so its purchase sits in the tape-only list instead.
  The two lines appear together, with matching quantities, which is what makes it
  readable — but the header figure counts named promoters only.
- **The base is still quarterly.** The adjustment closes the gap for disclosed
  deals, not for everything that has happened since.

## How the volume figure is worked out

Volume does not trigger an alert, but it appears in every one, and the number is
not a naive ratio.

Your tracker compares volume *so far today* against a *whole day's* average. At
10am a stock has traded only a fraction of its day, so that ratio reads far below
1 however extraordinary the morning has been. Here today's volume is instead
scaled by the share of a day that has normally traded by this time, giving a
projected full-day figure to compare against the last five sessions. So the
message can say *on pace for 49.88 lakh against a 27.67 lakh average* at 09:45,
rather than the misleading *0.12×* the raw comparison would give.

Two details that matter:

- The shape of the intraday curve is the **median** across the last 10 sessions,
  not the mean. On expiry and index-rebalance days the closing auction can be
  half the day's volume — Reliance's final 15-minute bar was 51% of its entire
  volume on 18 Sep — and one such day would badly skew an average.
- Nothing is reported until 3% of a normal day has traded. Projecting a full day
  from the first few minutes divides by almost nothing and turns one early block
  trade into a tenfold surge.

Every alert states how far through a normal day you are, so you can judge how
much weight the projection deserves. `VOLUME_PACING=false` reverts to the
tracker's raw comparison.

## How many messages this actually sends

With 299 names it is worth knowing. Replaying real sessions:

| | Ordinary day (Wed 16 Sep) | Expiry day (Fri 18 Sep) |
|---|---|---|
| Past 5% | 4 | 15 |

Friday 18 Sep was a derivatives-expiry and index-rebalance day. An ordinary day
is a handful of names. `MOVE_LEVELS=3,5` brings the 3% band back if you want it.

### Why volume no longer triggers

It did, at 1.5× as your tracker has it, and on this universe it did not work:

| | Ordinary day | Expiry day |
|---|---|---|
| Names past 1.5× volume | 60 (16% of universe) | 185 (**47% of universe**) |

When 47% of the universe passes a volume threshold, that is the market moving,
not any one stock — so the alert carried no information. It is now off, and
volume is reported inside price alerts instead, where it sharpens a signal you
already care about rather than generating its own noise.

The machinery is intact and still tested. `VOLUME_LEVELS=2.5,4` turns it back on
at a more selective level (24 crossings on an ordinary day rather than 60).

## When you get told, and when you do not

Each level is a latch. It fires the first time the reading goes above it, then
stays quiet however long it sits there. It arms again only once the reading has
fallen back to 0.1 below the level, so:

- 5.2% → **alert**. Drifting 5.3%, 5.9%, 7.0% → silent.
- Easing to 4.9%, then back through 5.0% → **alert again**.
- Wobbling 4.99 / 5.01 / 4.95 → silent, because it never cleared the margin.
- A 3% or 4% move → silent.

Up and down are separate latches, so a stock that runs +5% in the morning and
reverses to −5% after lunch reports both — and its upside latch releases on the
way down, ready for a second attempt. Intraday and daily are separate too.

State lives in `state/alerts.json`, committed back to the repository by the
workflow, because each scheduled run is a fresh machine that would otherwise
replay the whole day. That is also why the run writes a dated heartbeat file:
GitHub disables scheduled workflows on a repository with no activity for 60
days, and one commit a day keeps that timer from expiring.

## Things that stop it lying to you

Silence has to mean "nothing happened", not "the watcher broke". So:

- **Every quote carries the session it belongs to** and is discarded unless that
  matches today. Yahoo keeps serving Friday's close all weekend; without this a
  stock that closed 4% down would alert repeatedly until Monday. This, not the
  holiday list, is what makes stale data safe.
- **If more than half the universe cannot be priced, you get told**, once a day.
  A broken feed otherwise looks exactly like a calm market.
- **A move beyond 25% is flagged, not suppressed.** Past that it is more likely
  a bad tick or an unadjusted corporate action than a real price change, but
  suppressing it could hide a genuine event, so it is sent with a warning.
- **Ex-split and ex-dividend days are flagged**, since part of the move is
  mechanical. Yahoo already restates history for splits — a 2:1 split shows as
  −1.86%, not −50% — so this is a backstop for the hours where Yahoo has not yet
  applied an adjustment, not a correction of its data.
- **A stalled feed is logged** when a quote falls more than 30 minutes behind.

## Why Yahoo and not NSE

NSE's live quote API returns 403 to almost any cloud address, and its historical
endpoint needs page cookies that expire constantly — your tracker carries
`SKIP_NSE_LIVE` on Render for exactly this reason. Yahoo's `.NS` chart endpoint
carries NSE prices and NSE volume, answers from anywhere, and already adjusts for
splits and bonuses, so none of the corporate-action arithmetic the tracker needs
against raw NSE data is required here.

Three requests per symbol, because no single one carries everything: a live
`range=1d` call each poll, plus a daily bars call and an intraday bars call that
are cached for the day. Note that at any range wider than `1d`,
`chartPreviousClose` means the close before the whole window rather than
yesterday's, which is why returns are only ever read from the `1d` call.

## Tuning

Everything is an environment variable; see `.env.example`, or set them under
`env:` in the workflow.

| Variable | Default | Meaning |
|---|---|---|
| `PRICE_RULES` | `daily` | `daily`, `intraday`, or both |
| `MOVE_LEVELS` | `5` | Price levels, percent |
| `VOLUME_LEVELS` | *(empty)* | Volume levels; empty means volume never triggers |
| `SHAREHOLDING_MAX_PER_DAY` | `0` | Ceiling on shareholding notes per day; `0` is no limit |
| `BASELINE_SESSIONS` | `5` | Sessions in the volume average |
| `REARM_MARGIN_PCT` | `0.1` | How far a move must fall back to re-alert |
| `VOLUME_PACING` | `true` | Time-of-day normalisation |
| `PROFILE_SESSIONS` | `10` | Sessions behind the intraday curve |
| `MIN_PROFILE_FRACTION` | `0.03` | How far into the day before volume alerts start |
| `SHAREHOLDING_AT_PCT` | `5` | Price level that brings the shareholders; `0` disables |
| `SHAREHOLDING_PROMOTERS` | `0` | Promoter entities listed; `0` is all of them |
| `SHAREHOLDING_PUBLIC_HOLDERS` | `0` | Public holders listed; `0` is all of them |
| `POST_SHP_ADJUSTMENT` | `true` | Restate holdings for block/bulk deals since quarter end |

Holidays go in `holidays.txt`, one ISO date per line, from nseindia.com →
Resources → Exchange Holidays. It only saves pointless polling; the session-date
check above is what actually keeps stale data from alerting.

## Possible refinements

Deliberately left out, easy to add if they'd earn their keep:

- **A liquidity floor.** A thinly traded name can hit 3% on a handful of shares.
  Skipping anything under a minimum 5-day traded value would cut that noise.
- **A median volume baseline.** One block deal in the last five sessions inflates
  the average and hides a genuine surge behind it. A median resists that, at the
  cost of no longer being the "average" you asked for.
- **Sector or index context.** A 3% fall matters differently when the Nifty is
  down 2.5%. Reporting the move relative to the index would separate stock news
  from market news.
- **Opening-gap alerts.** Right now a stock that gaps 4% at the open and then
  goes nowhere fires the daily rule but not the intraday one, which is correct
  but arguably worth its own label.

## Tests

```powershell
python -m unittest discover -s tests
```

127 cases, covering the baseline window, the intraday profile, latching and
re-arming, which price rule is active, the caution flags, telling BSE's named
holders from its category subtotals (including the government promoters that look
like category labels), the three feed-reconciliation rules behind the post-SHP
adjustment, and the message formatting.
