# Flowstate Alpha — real-time worker deploy

This runs the earnings bot as an **always-on service** so it posts *the moment*
a company reports, instead of waiting for a scheduled sweep. It watches the SEC
EDGAR live 8-K feed for names expected to report today and fires an instant
Discord ping within ~1–2 minutes of the release, then posts the full card once
the structured numbers land on FMP.

You need one small Linux VM that stays on 24/7. Free options below.

---

## Option A — Oracle Cloud Always Free (recommended, $0 forever)

1. **Create an account** at cloud.oracle.com (a card is required for identity
   verification, but Always Free resources are never charged). Pick a home
   region close to you.

2. **Create a VM instance**: Menu → Compute → Instances → Create instance.
   - Image: **Canonical Ubuntu 22.04**.
   - Shape: **VM.Standard.A1.Flex** (Ampere/ARM, Always Free — 1 OCPU / 6 GB is
     plenty). If A1 shows "out of capacity," use **VM.Standard.E2.1.Micro**
     (AMD, also Always Free) or try another availability domain.
   - Add your SSH public key (or let it generate one and download it).
   - Create.

3. **Open outbound only** — no inbound ports needed; the worker only makes
   outbound calls to SEC/FMP/Discord. Default egress is open, so nothing to do.

4. **SSH in**: `ssh ubuntu@<public-ip>` (Ubuntu images use user `ubuntu`).

5. **Install and run**:
   ```bash
   sudo git clone https://github.com/Chasingpow/flowstate-earnings-bot /opt/flowstate-earnings-bot
   cd /opt/flowstate-earnings-bot/deploy
   sudo bash bootstrap.sh
   sudo nano /opt/flowstate-earnings-bot/.env      # paste FMP_API_KEY + DISCORD_WEBHOOK_URL
   sudo systemctl restart flowstate-worker
   journalctl -u flowstate-worker -f               # watch it live (Ctrl-C to stop watching)
   ```
   If the repo is private, clone with a token instead:
   `sudo git clone https://<YOUR_GITHUB_PAT>@github.com/Chasingpow/flowstate-earnings-bot /opt/flowstate-earnings-bot`

6. **Prove it works**: set `SELFTEST_TICKER=AAPL` in `.env`, `sudo systemctl
   restart flowstate-worker`. Within a few seconds an AAPL card should post to
   Discord. Then blank out `SELFTEST_TICKER` and restart to run normally.

That's it — it now restarts on crash and on VM reboot automatically.

---

## Option B — Google Cloud free e2-micro (also $0)

Same idea. Create a project → Compute Engine → VM instance → machine type
**e2-micro** in a US free-tier region (us-west1/us-central1/us-east1), Ubuntu
22.04, allow default egress. Then run the same five commands from step 5.

---

## Operating it

- **Watch logs:** `journalctl -u flowstate-worker -f`
- **Restart:** `sudo systemctl restart flowstate-worker`
- **Stop:** `sudo systemctl stop flowstate-worker`
- **Update code:** `cd /opt/flowstate-earnings-bot && sudo git pull && sudo systemctl restart flowstate-worker`
- **Change settings:** edit `/opt/flowstate-earnings-bot/.env`, then restart.

### Timing knobs (`.env`)
- `EDGAR_POLL_SEC` — how often it checks the 8-K feed (default 20s). The instant
  ping lands within roughly this interval + SEC's own publish lag (~1–2 min).
- `MIN_MARKET_CAP` — only ping/post names at/above this cap (default $2B).
- `PARSE_PRELIM=1` — also parse preliminary EPS/revenue from the press release
  into the instant ping. Off by default; turn on once you've seen it read a few
  releases correctly.

### Avoiding double posts with the old cron bot
Once the worker is validated, **turn off the GitHub Actions schedule** so the
worker is the single automated poster (keep `workflow_dispatch` for manual
backfills). Comment out the three `cron:` lines in
`.github/workflows/earnings.yml`. The two systems keep separate de-dupe state,
so leaving both on the schedule can double-post.

---

## What "on the spot" really means

- **Instant ping (~1–2 min):** EDGAR publishes the 8-K within a couple minutes
  of the company filing it; the worker catches it on the next poll and pings.
  This is about as fast as is possible without a paid low-latency news feed.
- **Full card (release-dependent):** the branded card needs structured numbers,
  which FMP populates minutes to tens of minutes after the release. The ping is
  what gets you looking *before* that.
- Turning on `PARSE_PRELIM` puts unofficial numbers in the ping itself, closing
  most of that gap for the big names — verify against the linked filing.
