"""
Flowstate Alpha — automated earnings poster.

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
    data = r.json()
    if isinstance(data, dict):
        return data.get("earnings", []) or []
    if isinstance(data, list):
        out = []
        for it in data:
            if isinstance(it, dict) and "earnings" in it:
                out.extend(it.get("earnings") or [])
            elif isinstance(it, dict):
                out.append(it)
        return out
    return []


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
    mc, sector = 0.0, "—"
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


def _yoy_line(label, val_str, g):
    frac, ok = g
    if not ok:
        return f"• {label}: {val_str}"
    sign = "+" if frac >= 0 else "−"
    emoji = "✅" if frac >= 0 else "🔻"
    return f"• {label}: {val_str}; {sign}{abs(frac)*100:.0f}% YoY {emoji}"


def _qoq_line(label, g):
    frac, ok = g
    if not ok:
        return None
    if abs(frac) < 0.005:
        return f"• {label}: flat QoQ"
    sign = "+" if frac >= 0 else "−"
    emoji = "✅" if frac >= 0 else "🔻"
    return f"• {label}: {sign}{abs(frac)*100:.0f}% QoQ {emoji}"


def blurb(d):
    beats = int(d["eps_surprise"] >= 0) + int(d["rev_surprise"] >= 0)
    verdict = "Double Beat" if beats == 2 else ("Double Miss" if beats == 0 else "Mixed")
    rev_beat = d["rev_surprise"] >= 0
    eps_beat = d["eps_surprise"] >= 0
    rev_c = (f"• Revenue: {'beat +' if rev_beat else 'miss '}{d['rev_surprise']*100:.1f}% "
             f"(est. {rc.money(d['revenue_est'])}) {'✅' if rev_beat else '❌'}")
    eps_c = (f"• EPS: {'beat +' if eps_beat else 'miss -'}${abs(d['eps_surprise_abs']):.2f} "
             f"(est. {rc.eps_fmt(d['eps_est'])}) {'✅' if eps_beat else '❌'}")
    lines = [
        f"**${d['ticker']} — {d['name']} · {d['period']} {d['period_year']} EARNINGS**",
        _yoy_line("Revenue", rc.money(d["revenue"]), d["rev_yoy"]),
        _yoy_line(f"EPS ({d['eps_type']})", rc.eps_fmt(d["eps"]), d["eps_yoy"]),
        "*vs Consensus:*",
        rev_c,
        eps_c,
    ]
    qoq = [x for x in (_qoq_line("Revenue", d["rev_qoq"]), _qoq_line("EPS", d["eps_qoq"])) if x]
    if qoq:
        lines.append("*Sequential:*")
        lines += qoq
    lines.append(f"Verdict: **{verdict}** {'✅' if beats==2 else ('❌' if beats==0 else '⚠️')}")
    lines.append("_Educational only — not financial advice. Source: Benzinga._")
    return "\n".join(lines)


def post_discord(d, png):
    content = blurb(d)
    if len(content) > 2000:            # Discord hard limit on message content
        content = content[:1960].rstrip() + "\n…"
    payload = {"content": content}
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
    log(f"  posted {tk} ({d['name']}) — {rc.money(mc)}")
    posted.add(row["id"])
    return True


def main():
    if not BENZINGA_API_KEY or not DISCORD_WEBHOOK_URL:
        log("ERROR: BENZINGA_API_KEY and DISCORD_WEBHOOK_URL must be set."); sys.exit(1)

    # --- test mode: force-post one or more tickers' latest, ignore dedupe/recency/state ---
    if TEST_TICKER:
        tickers = [t for t in TEST_TICKER.replace(" ", ",").split(",") if t]
        log(f"TEST MODE: {tickers}")
        local = set()
        posted_any = False
        for tk in tickers:
            rows = [r for r in bz_latest_for(tk) if has_actuals(r)]
            if not rows:
                log("  no reported earnings found for", tk); continue
            rows.sort(key=lambda r: r.get("date", ""), reverse=True)
            try:
                if process(rows[0], local, None, force=True):
                    posted_any = True
            except Exception as e:
                log("  error on", tk, e)
        if not posted_any:
            log("  nothing posted (no finalized earnings for given ticker[s])."); sys.exit(1)
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
        save_state(posted)
        log(f"SEED_ONLY: marked {len(fresh)} as posted, none sent.")
        return

    n = 0
    for r in fresh:
        if n >= MAX_PER_RUN:
            log(f"MAX_PER_RUN ({MAX_PER_RUN}) reached; {len(fresh)-n} deferred to next run."); break
        try:
            if process(r, posted, None):
                n += 1
        except Exception as e:
            log("  error on", r.get("ticker"), e)

    if not DRY_RUN:
        save_state(posted)
    log(f"done: {n} posted.")


if __name__ == "__main__":
    main()
