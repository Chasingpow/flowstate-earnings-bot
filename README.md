# Flowstate Alpha — automated earnings cards

Auto-posts a Flowstate-branded earnings card to Discord whenever a **mid/large-cap**
company reports. Data from **Benzinga**, market-cap filter via **yfinance**, cards
rendered as PNG with headless Chrome, delivered to a **Discord webhook**. A GitHub
Action runs it on a schedule; `state/posted.json` prevents duplicate posts.

## How it works

1. Every ~15 min during earnings windows, pull Benzinga earnings for today/yesterday.
2. Keep rows with reported actuals, updated in the last `LOOKBACK_HOURS`, not already posted.
3. Look up market cap (yfinance); keep names ≥ `MIN_MARKET_CAP` (default $2B).
4. Render the Flowstate card (EPS + Revenue, estimate-vs-actual, beat/miss, YoY, QoQ, verdict).
5. Post the card + a short blurb to Discord. Record the Benzinga `id` so it never reposts.

## One-time setup

**1. Add repository secrets** — repo → *Settings → Secrets and variables → Actions → New repository secret*:

| Secret | Value |
|---|---|
| `BENZINGA_API_KEY` | your Benzinga API token (`bz.…`) |
| `DISCORD_WEBHOOK_URL` | the channel webhook URL |

**2. Seed the de-dupe state (run once)** so you don't get flooded with everything
already reported today. Actions → *Flowstate earnings poster* → **Run workflow** →
set `seed_only = 1` → Run. This marks current reporters as "already posted" without sending anything.

**3. Test a live post.** Run workflow again with `test_ticker = V` (or any ticker).
It posts that company's most recent earnings card to Discord immediately, ignoring
dedupe/recency. Great for confirming the webhook + look.

After that, the schedule takes over automatically. Nothing else to do.

## Tuning (env in `.github/workflows/earnings.yml`)

- `MIN_MARKET_CAP` — market-cap floor (default `2000000000` = $2B).
- `MAX_PER_RUN` — cap posts per run so peak days don't spam (default 12).
- `LOOKBACK_HOURS` — how recently a report must have been updated to count as "just reported".
- `DRY_RUN=1` — build cards, log, but don't post or change state.

## Notes

- **Scope:** cards cover EPS + Revenue (estimate vs actual, beat/miss, YoY, QoQ). Segment
  revenue and forward guidance are not available via the earnings API, so they're not on the card.
- **Not financial advice.** Cards carry an educational disclaimer; verify figures against filings.
- **Security:** the Benzinga key lives only as a GitHub secret. If it was ever shared in plain
  text, regenerate it in Benzinga.
- Local render test: `python test_local.py` (writes sample PNGs).
