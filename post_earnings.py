"""
Flowstate Alpha â€” automated earnings poster.

Runs on a schedule (GitHub Actions). Pulls freshly-reported earnings from Benzinga,
keeps mid/large-cap names, renders a Flowstate-branded card, and posts it to Discord.
De-dupes via state/posted.json so nothing is posted twice.

Env vars:
  BENZINGA_API_KEY   (required)
  DISCORD_WEBHOOK_URL(required)
  MIN_MARKET_CAP     (default 2000000000)
  MAX_PER_RUN        (default 12)
  LOOKBACK_HOURS     (default 30)   how recently 'updated' to count as "just reported"
  DRY_RUN            (default "0")  build cards but don't post / don't touch state
  SEED_ONLY          (default "0")  mark current reporters as posted WITHOUT posting (first deploy)
  TEST_TICKER        (optional)     post the latest earnings for this one ticker, ignore dedupe/recency
"""
import os, sys, json, time, datetime as dt
import requests
import render_card as rc

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "state", "posted.json")
BZ_URL = "https://api.benzinga.com/api/v2.1/calendar/earnings"

BENZINGA_API_KEY = os.environ.get("BENZINGA_API_KEY", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
MIN_MARKET_CAP = float(os.environ.get("MIN_MARKET_CAP", "2000000000"))
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "12"))
LOOKBACK_HOURS = float(os.environ.get("LOOKBACK_HOURS", "30"))
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
SEED_ONLY = os.environ.get("SEED_ONLY", "0") == "1"
TEST_TICKER = os.environ.get("TEST_TICKER", "").strip().upper()

MON = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]


def log(*a):
    print(*a, flush=True)


# ---------- state ----------

def load_state():
    try:
        with open(STATE_PATH) as f:
            return set(json.load(f).get("ids", []))
    except Exception:
        return set()


def save_state(ids):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    keep = list(ids)[-8000:]  # cap file size
    with open(STATE_PATH, "w") as f:
        json.dump({"ids": keep, "updated": dt.datetime.utcnow().isoformat() + "Z"}, f, indent=1)


# ---------- benzinga ----------

def bz_get(params):
    p = {"token": BENZINGA_API_KEY, "pagesize": 1000, "page": 0}
    p.update(params)
    r = requests.get(BZ_URL, params=p, headers={"accept": "application/json"}, timeout=30)
    r.raise_for_status()
    return r.json().get("earnings", []) or []


def bz_window():
    today = dt.datetime.utcnow().date()
    start = today - dt.timedelta(days=1)
    return bz_get({"parameters[date_from]": start.isoformat(),
                   "parameters[date_to]": today.isoformat(),
                   "parameters[date_sort]": "date:desc"})


def bz_latest_for(ticker):
    rows = bz_get({"parameters[tickers]": ticker,
                   "parameters[date_sort]": "date:desc", "pagesize": 8})
    return rows


def has_actuals(row):
    def num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    return (num(row.get("eps")) is not None and num(row.get("eps_est")) is not None
            and num(row.get("revenue")) not in (None, 0.0) and num(row.get("revenue_est")) not in (None, 0.0))


def prior_quarter(ticker, current):
    """Find the quarter immediately preceding `current` for QoQ."""
    try:
        rows = [r for r in bz_latest_for(ticker) if has_actuals(r)]
        rows = [r for r in rows if (r.get("period_year"), r.get("period")) !=
                (current.get("period_year"), current.get("period"))]
        rows.sort(key=lambda r: r.get("date", ""), reverse=True)
        for r in rows:
            if r.get("date", "") < current.get("date", ""):
                return r
    except Exception as e:
        log("  prior_quarter err", ticker, e)
    return None


# ---------- enrichment (market cap + sector) ----------

def enrich(ticker):
    mc, sector = 0.0, "â€”"
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        try:
            fi = t.fast_info
            mc = float(getattr(fi, "market_cap", 0) or 0)
        except Exception:
            pass
        info = {}
        try:
            info = t.get_info()
        except Exception:
            pass
        if not mc:
            mc = float(info.get("marketCap") or 0)
        sector = info.get("sector") or sector
    except Exception as e:
        log("  enrich err", ticker, e)
    return mc, sector


# ---------- normalize + timing ----------

def timing_of(row):
    t = (row.get("time") or "00:00:00")[:5]
    try:
        hh = int(t[:2])
    except ValueError:
        hh = 0
    return "BEFORE OPEN" if hh < 12 else "AFTER CLOSE"


def normalize(row, mc, sector):
    prior = prior_quarter(row["ticker"], row)
    epsq = rc.growth(row["eps"], prior["eps"]) if prior else (0.0, False)
    revq = rc.growth(row["revenue"], prior["revenue"]) if prior else (0.0, False)
    y, m, dd = row["date"].split("-")
    return {
        "id": row["id"], "ticker": row["ticker"], "name": row.get("name") or row["ticker"],
        "period": row.get("period", ""), "period_year": row.get("period_year", ""),
        "eps": float(row["eps"]), "eps_est": float(row["eps_est"]),
        "eps_type": row.get("eps_type") or "GAAP",
        "revenue": float(row["revenue"]), "revenue_est": float(row["revenue_est"]),
        "eps_surprise": float(row.get("eps_surprise_percent") or 0),
        "rev_surprise": float(row.get("revenue_surprise_percent") or 0),
        "eps_surprise_abs": float(row.get("eps_surprise") or (float(row["eps"]) - float(row["eps_est"]))),
        "eps_yoy": rc.growth(row["eps"], row.get("eps_prior")),
        "rev_yoy": rc.growth(row["revenue"], row.get("revenue_prior")),
        "eps_qoq": epsq, "rev_qoq": revq,
        "market_cap": mc, "sector": sector, "timing": timing_of(row),
        "date_str": f"{MON[int(m)-1]} {int(dd)}, {y}",
    }


def blurb(d):
    beats = int(d["eps_surprise"] >= 0) + int(d["rev_surprise"] >= 0)
    verdict = "DOUBLE BEAT" if beats == 2 else ("DOUBLE MISS" if beats == 0 else "MIXED")
    return (f"**${d['ticker']} Â· {d['name']} â€” {d['period']} {d['period_year']}**  Â·  {verdict}\n"
            f"Revenue {rc.money(d['revenue'])} ({d['rev_surprise']*100:+.1f}% vs est) Â· "
            f"EPS {rc.eps_fmt(d['eps'])} ({d['eps_surprise']*100:+.1f}% vs est)\n"
            f"_Educational only â€” not financial advice. Source: Benzinga._")


def post_discord(d, png):
    payload = {"content": blurb(d)}
    with open(png, "rb") as fh:
        files = {"file": (f"{d['ticker']}_earnings.png", fh, "image/png")}
        r = requests.post(DISCORD_WEBHOOK_URL, data={"payload_json": json.dumps(payload)},
                          files=files, timeout=60)
    if r.status_code not in (200, 204):
        raise RuntimeError(f"Discord {r.status_code}: {r.text[:300]}")


def process(row, posted, count_ref, force=False):
    tk = row["ticker"]
    mc, sector = enrich(tk)
    if not force:
        if mc and mc < MIN_MARKET_CAP:
            log(f"  skip {tk}: market cap {rc.money(mc)} < min"); return False
        if not mc and int(row.get("importance") or 0) < 4:
            log(f"  skip {tk}: no market cap and low importance"); return False
    d = normalize(row, mc, sector)
    png = os.path.join(HERE, f"_out_{tk}.png")
    rc.render(d, png)
    if DRY_RUN and not force:
        log(f"  [dry] would post {tk} ({d['name']})"); return True
    post_discord(d, png)
    log(f"  posted {tk} ({d['name']}) â€” {rc.money(mc)}")
    posted.add(row["id"])
    return True


def main():
    if not BENZINGA_API_KEY or not DISCORD_WEBHOOK_URL:
        log("ERROR: BENZINGA_API_KEY and DISCORD_WEBHOOK_URL must be set."); sys.exit(1)

    # --- test mode: post one ticker's latest, ignore dedupe/recency/state ---
    if TEST_TICKER:
        log(f"TEST MODE: {TEST_TICKER}")
        rows = [r for r in bz_latest_for(TEST_TICKER) if has_actuals(r)]
        if not rows:
            log("  no reported earnings found for", TEST_TICKER); sys.exit(1)
        rows.sort(key=lambda r: r.get("date", ""), reverse=True)
        process(rows[0], set(), None, force=True)
        return

    posted = load_state()
    rows = bz_window()
    now = time.time()
    fresh = []
    for r in rows:
        if not r.get("id") or r["id"] in posted:
            continue
        if not has_actuals(r):
            continue
        upd = float(r.get("updated") or 0)
        if upd and (now - upd) > LOOKBACK_HOURS * 3600:
            continue
        fresh.append(r)
    fresh.sort(key=lambda r: float(r.get("updated") or 0), reverse=True)
    log(f"{len(fresh)} freshly-reported candidate(s)")

    if SEED_ONLY:
        for r in fresh:
            posted.add(r["id"])
        save_state({ÜİY
BˆÙÊˆ”ÑQQÓÓ“NˆX\šÙYÛ[Šœ™\Ú
_H\ÈÜİY›Û™HÙ[ˆŠBˆ™]\›‚‚ˆˆHˆ›Üˆˆ[ˆœ™\Ú‚ˆYˆˆHPVÔT—Ô•S‚ˆÙÊˆ“PVÔT—Ô•Sˆ
ÓPVÔT—Ô•SŸJH™XXÚYÈÛ[Šœ™\Ú
K[ŸHY™\œ™YÈ™^[‹ˆŠNÈœ™XZÂˆN‚ˆYˆ›ØÙ\ÜÊ‹ÜİY›Û™JN‚ˆˆ
ÏHBˆ^Ù\^Ù\[Ûˆ\ÈN‚ˆÙÊˆ\œ›ÜˆÛˆ‹‹™Ù]
XÚÙ\ˆŠKJB‚ˆYˆ›İ–WÔ•S‚ˆØ]™WÜİ]JÜİY
BˆÙÊˆ™Û™NˆÛŸHÜİYˆŠB‚‚šYˆ×Û˜[YW×ÈOH—×ÛXZ[—×È‚ˆXZ[Š
B