"""
Flowstate Alpha — automated earnings poster (Financial Modeling Prep data source).

Runs on a schedule (GitHub Actions). Pulls freshly-reported earnings from FMP's
earnings calendar (ALL caps), keeps mid/large/mega caps (>= MIN_MARKET_CAP),
renders a Flowstate-branded card, and posts it to Discord.
De-dupes via state/posted.json (keyed by SYMBOL-DATE) so nothing posts twice.

Env vars:
  FMP_API_KEY         (required)
  DISCORD_WEBHOOK_URL (required)
  MIN_MARKET_CAP      (default 2000000000)
  MAX_PER_RUN         (default 12)   max cards posted per run
  MAX_PROFILE         (default 60)   max profile lookups per run (API budget)
  WINDOW_DAYS         (default 2)    calendar window (today back N days)
  DRY_RUN             (default "0")  render but don't post / don't touch state
  SEED_ONLY           (default "0")  mark current reporters as posted WITHOUT posting
  TEST_TICKER         (optional)     force-post latest earnings for these ticker(s),
                                     comma-separated, ignoring dedupe/recency/market cap
"""
import os, sys, json, datetime as dt
import requests
import render_card as rc

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "state", "posted.json")
FMP_BASE = "https://financialmodelingprep.com/stable"

FMP_API_KEY = os.environ.get("FMP_API_KEY", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
MIN_MARKET_CAP = float(os.environ.get("MIN_MARKET_CAP", "2000000000"))
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "12"))
MAX_PROFILE = int(os.environ.get("MAX_PROFILE", "60"))
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "2"))
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
    keep = list(ids)[-8000:]
    with open(STATE_PATH, "w") as f:
        json.dump({"ids": keep, "updated": dt.datetime.utcnow().isoformat() + "Z"}, f, indent=1)


# ---------- FMP ----------

def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fmp_get(path, params):
    p = dict(params); p["apikey"] = FMP_API_KEY
    r = requests.get(f"{FMP_BASE}/{path}", params=p, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data


def fmp_calendar(date_from, date_to):
    data = fmp_get("earnings-calendar", {"from": date_from, "to": date_to})
    return data if isinstance(data, list) else []


def fmp_history(symbol):
    data = fmp_get("earnings", {"symbol": symbol})
    return data if isinstance(data, list) else []


def fmp_profile(symbol):
    data = fmp_get("profile", {"symbol": symbol})
    if isinstance(data, list) and data:
        return data[0]
    if isinstance(data, dict):
        return data
    return {}


def has_actuals(e):
    return (num(e.get("epsActual")) is not None and num(e.get("epsEstimated")) is not None
            and num(e.get("revenueActual")) not in (None, 0.0)
            and num(e.get("revenueEstimated")) not in (None, 0.0))


def period_label(symbol, report_date):
    """True fiscal period from the income statement when it's fresh; else derive
    from the report date (correct for calendar-fiscal names, ~1 quarter of lag)."""
    try:
        d = fmp_get("income-statement", {"symbol": symbol, "period": "quarter", "limit": 1})
        if isinstance(d, list) and d:
            fdate = d[0].get("date") or ""
            per = d[0].get("period"); yr = d[0].get("calendarYear") or d[0].get("fiscalYear")
            # accept only if the statement's fiscal end is within ~5 months of the report
            if per and yr and fdate:
                try:
                    fd = dt.date.fromisoformat(fdate[:10])
                    rd = dt.date.fromisoformat(report_date[:10])
                    if (rd - fd).days <= 155:
                        return str(per).upper(), str(yr)
                except ValueError:
                    pass
    except Exception as e:
        log("  period lookup err", symbol, e)
    y, m, _ = report_date.split("-"); m = int(m); y = int(y)
    if m in (1, 2, 3):  return "Q4", str(y - 1)
    if m in (4, 5, 6):  return "Q1", str(y)
    if m in (7, 8, 9):  return "Q2", str(y)
    return "Q3", str(y)


def build_d(e, hist, prof):
    sym = e["symbol"]; date = e["date"]
    epsA = num(e["epsActual"]); epsE = num(e["epsEstimated"])
    revA = num(e["revenueActual"]); revE = num(e["revenueEstimated"])

    rep = [h for h in hist if num(h.get("epsActual")) is not None and h.get("date") and h["date"] <= date]
    rep.sort(key=lambda h: h["date"], reverse=True)
    idx = next((i for i, h in enumerate(rep) if h["date"] == date), 0)
    prior_q = rep[idx + 1] if idx + 1 < len(rep) else None
    prior_y = rep[idx + 4] if idx + 4 < len(rep) else None

    eps_yoy = rc.growth(epsA, prior_y["epsActual"]) if prior_y else (0.0, False)
    rev_yoy = rc.growth(revA, prior_y["revenueActual"]) if prior_y else (0.0, False)
    eps_qoq = rc.growth(epsA, prior_q["epsActual"]) if prior_q else (0.0, False)
    rev_qoq = rc.growth(revA, prior_q["revenueActual"]) if prior_q else (0.0, False)

    mc = num(prof.get("marketCap")) or 0.0
    sector = prof.get("sector") or "—"
    name = prof.get("companyName") or sym
    per, yr = period_label(sym, date)

    eps_surp_abs = epsA - epsE
    eps_surp_pct = (epsA - epsE) / abs(epsE) if epsE else 0.0
    rev_surp_pct = (revA - revE) / revE if revE else 0.0

    y, m, dd = date.split("-")
    return {
        "id": f"{sym}-{date}",
        "ticker": sym, "name": name, "period": per, "period_year": yr,
        "eps": epsA, "eps_est": epsE, "eps_type": "Adj",
        "revenue": revA, "revenue_est": revE,
        "eps_surprise": eps_surp_pct, "rev_surprise": rev_surp_pct, "eps_surprise_abs": eps_surp_abs,
        "eps_yoy": eps_yoy, "rev_yoy": rev_yoy, "eps_qoq": eps_qoq, "rev_qoq": rev_qoq,
        "market_cap": mc, "sector": sector, "timing": "",
        "date_str": f"{MON[int(m)-1]} {int(dd)}, {y}",
        "source": "FMP", "source_note": "as reported via Financial Modeling Prep",
    }


# ---------- Discord blurb + post ----------

def _yoy_line(label, val_str, g):
    frac, ok = g
    if not ok:
        return f"• {label}: {val_str}"
    sign = "+" if frac >= 0 else "−"
    emoji = "✅" if frac >= 0 else "\U0001F53B"
    return f"• {label}: {val_str}; {sign}{abs(frac)*100:.0f}% YoY {emoji}"


def _qoq_line(label, g):
    frac, ok = g
    if not ok:
        return None
    if abs(frac) < 0.005:
        return f"• {label}: flat QoQ"
    sign = "+" if frac >= 0 else "−"
    emoji = "✅" if frac >= 0 else "\U0001F53B"
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
    lines.append(f"_Educational only — not financial advice. Source: {d.get('source','FMP')}._")
    return "\n".join(lines)


def post_discord(d, png):
    content = blurb(d)
    if len(content) > 2000:
        content = content[:1960].rstrip() + "\n…"
    payload = {"content": content}
    with open(png, "rb") as fh:
        files = {"file": (f"{d['ticker']}_earnings.png", fh, "image/png")}
        r = requests.post(DISCORD_WEBHOOK_URL, data={"payload_json": json.dumps(payload)},
                          files=files, timeout=60)
    if r.status_code not in (200, 204):
        raise RuntimeError(f"Discord {r.status_code}: {r.text[:300]}")


def process(d, posted, force=False):
    tk = d["ticker"]; mc = d["market_cap"]
    if not force:
        if mc and mc < MIN_MARKET_CAP:
            log(f"  skip {tk}: market cap {rc.money(mc)} < min"); return False
        if not mc:
            log(f"  skip {tk}: no market cap"); return False
    png = os.path.join(HERE, f"_out_{tk}.png")
    rc.render(d, png)
    if DRY_RUN and not force:
        log(f"  [dry] would post {tk} ({d['name']})"); return True
    post_discord(d, png)
    log(f"  posted {tk} ({d['name']}) — {rc.money(mc)}")
    posted.add(d["id"])
    return True


def latest_reported(symbol):
    hist = fmp_history(symbol)
    rep = [h for h in hist if has_actuals(h)]
    rep.sort(key=lambda h: h.get("date", ""), reverse=True)
    if not rep:
        return None, hist
    e = dict(rep[0]); e["symbol"] = symbol
    return e, hist


def main():
    if not FMP_API_KEY or not DISCORD_WEBHOOK_URL:
        log("ERROR: FMP_API_KEY and DISCORD_WEBHOOK_URL must be set."); sys.exit(1)

    # --- test mode: force-post latest earnings for one or more tickers ---
    if TEST_TICKER:
        tickers = [t for t in TEST_TICKER.replace(" ", ",").split(",") if t]
        log(f"TEST MODE: {tickers}")
        posted_any = False
        local = set()
        for tk in tickers:
            try:
                e, hist = latest_reported(tk)
                if not e:
                    log("  no reported earnings found for", tk); continue
                prof = fmp_profile(tk)
                d = build_d(e, hist, prof)
                if process(d, local, force=True):
                    posted_any = True
            except Exception as ex:
                log("  error on", tk, ex)
        if not posted_any:
            log("  nothing posted."); sys.exit(1)
        return

    posted = load_state()
    today = dt.datetime.utcnow().date()
    date_from = (today - dt.timedelta(days=WINDOW_DAYS)).isoformat()
    cal = fmp_calendar(date_from, today.isoformat())
    fresh = [e for e in cal
             if e.get("symbol") and has_actuals(e)
             and f'{e["symbol"]}-{e["date"]}' not in posted]
    # newest first, then by revenue size (proxy for prominence)
    fresh.sort(key=lambda e: (e["date"], num(e.get("revenueActual")) or 0), reverse=True)
    log(f"{len(fresh)} freshly-reported candidate(s) in window {date_from}..{today.isoformat()}")

    if SEED_ONLY:
        for e in fresh:
            posted.add(f'{e["symbol"]}-{e["date"]}')
        save_state(posted)
        log(f"SEED_ONLY: marked {len(fresh)} as posted, none sent.")
        return

    n = 0; profiled = 0
    for e in fresh:
        if n >= MAX_PER_RUN:
            log(f"MAX_PER_RUN ({MAX_PER_RUN}) reached; remaining deferred to next run."); break
        if profiled >= MAX_PROFILE:
            log(f"MAX_PROFILE ({MAX_PROFILE}) reached; remaining deferred to next run."); break
        sym = e["symbol"]; sid = f'{sym}-{e["date"]}'
        try:
            prof = fmp_profile(sym); profiled += 1
            mc = num(prof.get("marketCap")) or 0.0
            if mc and mc < MIN_MARKET_CAP:
                posted.add(sid)  # remember small-caps so we don't re-profile them every run
                continue
            if not mc:
                continue
            hist = fmp_history(sym)
            d = build_d(e, hist, prof)
            if process(d, posted):
                n += 1
        except Exception as ex:
            log("  error on", sym, ex)

    if not DRY_RUN:
        save_state(posted)
    log(f"done: {n} posted.")


if __name__ == "__main__":
    main()
