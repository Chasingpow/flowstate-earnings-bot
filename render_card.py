"""
Flowstate Alpha earnings card renderer.
Fills an HTML template with earnings data and screenshots it to PNG via headless Chromium.
"""
import os, sys, glob, shutil, subprocess, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------- formatting helpers ----------

def money(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "n/a"
    a = abs(v)
    if a >= 1e12:  s = f"${v/1e12:.2f}T"
    elif a >= 1e9: s = f"${v/1e9:.2f}B"
    elif a >= 1e6: s = f"${v/1e6:.0f}M"
    elif a >= 1e3: s = f"${v/1e3:.0f}K"
    else:          s = f"${v:.0f}"
    return s

def eps_fmt(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "n/a"
    return (f"-${abs(v):.2f}" if v < 0 else f"${v:.2f}")

def pct1(frac):
    return f"{frac*100:.1f}%"

def growth(cur, prior):
    """Return (frac, ok). ok=False if prior is non-positive / meaningless."""
    try:
        cur = float(cur); prior = float(prior)
    except (TypeError, ValueError):
        return (0.0, False)
    if prior == 0:
        return (0.0, False)
    if prior < 0:  # sign flip makes % misleading
        return ((cur - prior) / abs(prior), False)
    return ((cur - prior) / prior, True)

CY = "var(--green)"; CR = "var(--red)"; CM = "var(--muted)"

def delta_span(frac, ok, label):
    if not ok:
        return f'<span style="color:{CM}">— {label}</span>'
    arrow = "▲" if frac >= 0 else "▼"
    color = CY if frac >= 0 else CR
    return f'<span style="color:{color}">{arrow} {abs(frac)*100:.0f}% {label}</span>'

def chip(is_beat, text):
    cls = "g" if is_beat else "r"
    return f'<span class="chip {cls}">{text}</span>'

# ---------- template ----------

TEMPLATE = r"""<!DOCTYPE html><html><head><meta charset="utf-8"/><style>
@font-face{font-family:'Inter';font-weight:400;src:url('fonts/inter-400.woff2') format('woff2');}
@font-face{font-family:'Inter';font-weight:500;src:url('fonts/inter-500.woff2') format('woff2');}
@font-face{font-family:'Inter';font-weight:600;src:url('fonts/inter-600.woff2') format('woff2');}
@font-face{font-family:'Inter';font-weight:700;src:url('fonts/inter-700.woff2') format('woff2');}
@font-face{font-family:'Inter';font-weight:800;src:url('fonts/inter-800.woff2') format('woff2');}
@font-face{font-family:'Grotesk';font-weight:500;src:url('fonts/grotesk-500.woff2') format('woff2');}
@font-face{font-family:'Grotesk';font-weight:700;src:url('fonts/grotesk-700.woff2') format('woff2');}
*{margin:0;padding:0;box-sizing:border-box;}
:root{--cyan:#25D7EE;--blue:#3E7DF6;--green:#3BD07F;--red:#F4655F;--amber:#F7AB15;--yellow:#FACC15;--white:#EEF1FF;--muted:#8B93B4;--card:rgba(128,150,214,0.065);--cardbd:rgba(150,170,235,0.14);}
html,body{margin:0;padding:0;}
body{width:1200px;box-sizing:border-box;font-family:'Inter',sans-serif;color:var(--white);position:relative;padding:44px 54px 42px;background:radial-gradient(1100px 700px at 12% -10%,#241a5e 0%,rgba(36,26,94,0) 55%),radial-gradient(900px 600px at 108% 120%,#34206b 0%,rgba(52,32,107,0) 50%),linear-gradient(150deg,#0b0e28 0%,#141149 55%,#1c1550 100%);}
body:before{content:"";position:absolute;inset:0;background:radial-gradient(1400px 500px at 50% -20%,rgba(62,125,246,0.10),transparent 60%);pointer-events:none;}
.wrap{position:relative;z-index:2;display:flex;flex-direction:column;}
.mid{display:flex;flex-direction:column;}
.hdr{display:flex;justify-content:space-between;align-items:flex-start;}
.brand{display:flex;align-items:center;gap:14px;}
.logo{width:42px;height:42px;border-radius:12px;background:linear-gradient(140deg,var(--cyan),var(--blue));position:relative;box-shadow:0 6px 18px rgba(62,125,246,0.35);}
.logo:after{content:"";position:absolute;left:11px;top:11px;width:20px;height:20px;border-radius:6px;background:#0e1140;}
.logo:before{content:"";position:absolute;left:16px;top:8px;width:6px;height:26px;border-radius:4px;background:linear-gradient(var(--cyan),var(--blue));z-index:2;}
.brand .txt .n{font-family:'Grotesk';font-weight:700;font-size:19px;letter-spacing:2.5px;}
.brand .txt .s{font-size:10px;font-weight:600;letter-spacing:3.5px;color:var(--muted);margin-top:3px;}
.hmeta{text-align:right;}
.hmeta .d{font-size:11px;font-weight:600;letter-spacing:2.5px;color:var(--muted);}
.hmeta .t{font-size:12px;font-weight:700;letter-spacing:2.5px;color:var(--cyan);margin-top:5px;}
.tick{margin-top:26px;display:flex;align-items:baseline;gap:16px;}
.tick .sym{font-family:'Grotesk';font-weight:700;font-size:66px;line-height:0.9;background:linear-gradient(120deg,var(--cyan),var(--blue));-webkit-background-clip:text;background-clip:text;color:transparent;}
.tick .cap{font-size:13px;font-weight:600;letter-spacing:2px;color:var(--muted);text-transform:uppercase;padding-bottom:6px;}
.head{margin-top:16px;font-size:34px;font-weight:800;letter-spacing:-0.5px;}
.head b{font-weight:800;}
.cards{margin-top:28px;display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;}
.c{background:var(--card);border:1px solid var(--cardbd);border-radius:16px;padding:22px 22px;position:relative;min-height:204px;display:flex;flex-direction:column;}
.c .lab{font-size:10.5px;font-weight:700;letter-spacing:2.5px;color:var(--muted);}
.c .val{font-family:'Grotesk';font-weight:700;font-size:42px;margin-top:14px;letter-spacing:-0.5px;}
.c .est{font-size:13px;font-weight:500;color:var(--muted);margin-top:10px;}
.chip{display:inline-block;align-self:flex-start;font-size:11px;font-weight:700;letter-spacing:0.5px;padding:3px 9px;border-radius:20px;margin-top:auto;}
.chip.g{background:rgba(59,208,127,0.14);color:var(--green);border:1px solid rgba(59,208,127,0.30);}
.chip.r{background:rgba(244,101,95,0.14);color:var(--red);border:1px solid rgba(244,101,95,0.30);}
.chip.a{background:rgba(247,171,21,0.14);color:var(--amber);border:1px solid rgba(247,171,21,0.32);}
.delta{font-size:13px;font-weight:600;margin-top:14px;color:var(--muted);}
.delta .m{color:var(--muted);font-weight:500;}
.verdict .val{font-size:32px;}
.tags{margin-top:22px;display:grid;grid-template-columns:repeat(4,1fr);gap:12px;}
.tag{font-size:11px;font-weight:600;letter-spacing:0.4px;color:#b9c0dd;background:rgba(255,255,255,0.035);border:1px solid rgba(255,255,255,0.08);padding:9px 12px;border-radius:8px;text-align:center;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.tag b{color:var(--cyan);font-weight:700;}
.foot{margin-top:26px;display:flex;justify-content:space-between;align-items:flex-end;}
.foot .disc{font-size:10.5px;color:#666e92;letter-spacing:0.3px;line-height:1.5;max-width:640px;}
.foot .mk{font-family:'Grotesk';font-weight:700;font-size:14px;letter-spacing:3px;}
.foot .mk span{color:var(--cyan);}
.rule{height:1px;background:linear-gradient(90deg,rgba(255,255,255,0.10),rgba(255,255,255,0));margin-top:16px;}
</style></head><body><div class="wrap">
<div class="hdr">
 <div class="brand"><div class="logo"></div><div class="txt"><div class="n">FLOWSTATE ALPHA</div><div class="s">MARKET BREAKDOWN</div></div></div>
 <div class="hmeta"><div class="d">{{DATELINE}}</div><div class="t">{{TAG}}</div></div>
</div>
<div class="mid">
<div class="tick"><div class="sym">{{TICKER}}</div><div class="cap">{{CAP}}</div></div>
<div class="head">{{HEADLINE}}</div>
<div class="cards">
 <div class="c"><div class="lab">REVENUE</div><div class="val">{{REV_VAL}}</div><div class="est">Est. {{REV_EST}}</div>{{REV_CHIP}}<div class="delta">{{REV_DELTA}}</div></div>
 <div class="c"><div class="lab">{{EPS_LABEL}}</div><div class="val">{{EPS_VAL}}</div><div class="est">Est. {{EPS_EST}}</div>{{EPS_CHIP}}<div class="delta">{{EPS_DELTA}}</div></div>
 <div class="c verdict"><div class="lab">VERDICT</div><div class="val" style="color:{{VERDICT_COLOR}}">{{VERDICT_VAL}}</div><div class="est">{{VERDICT_SUB}}</div>{{VERDICT_CHIP}}<div class="delta">{{VERDICT_DELTA}}</div></div>
</div>
<div class="tags">{{TAGS}}</div>
</div>
<div class="foot"><div><div class="rule"></div><div class="disc" style="margin-top:12px;">Educational content only — not financial advice. Figures are estimate-vs-actual {{SRCNOTE}}; verify against primary filings. Do your own research.</div></div><div class="mk">FLOWSTATE<span>ALPHA</span></div></div>
</div></body></html>"""


def build_html(d):
    """d: normalized dict from post_earnings.normalize()."""
    eps_beat = d["eps_surprise"] >= 0
    rev_beat = d["rev_surprise"] >= 0
    beats = int(eps_beat) + int(rev_beat)

    # verdict
    if beats == 2:
        vval, vcolor, vsub = "DOUBLE BEAT", CY, "Revenue &amp; EPS above Street"
        vchip = '<span class="chip g">2 / 2 BEATS</span>'
        head = 'Beat on <b style="color:var(--green)">both lines.</b>'
    elif beats == 0:
        vval, vcolor, vsub = "DOUBLE MISS", CR, "Revenue &amp; EPS below Street"
        vchip = '<span class="chip r">0 / 2 BEATS</span>'
        head = 'Missed <b style="color:var(--red)">both lines.</b>'
    else:
        vval, vcolor, vsub = "MIXED", "var(--amber)", "One beat, one miss"
        vchip = '<span class="chip a">1 / 2 BEATS</span>'
        if eps_beat:
            head = 'Beat EPS, <b style="color:var(--red)">missed revenue.</b>'
        else:
            head = 'Beat revenue, <b style="color:var(--red)">missed EPS.</b>'

    rev_chip = chip(rev_beat, ("BEAT +" if rev_beat else "MISS ") + pct1(d["rev_surprise"]))
    if eps_beat:
        eps_chip = chip(True, f"BEAT +${abs(d['eps_surprise_abs']):.2f}")
    else:
        eps_chip = chip(False, f"MISS -${abs(d['eps_surprise_abs']):.2f}")

    rev_delta = f'{delta_span(*d["rev_yoy"], "YoY")} <span class="m">·</span> {delta_span(*d["rev_qoq"], "QoQ")}'
    eps_delta = f'{delta_span(*d["eps_yoy"], "YoY")} <span class="m">·</span> {delta_span(*d["eps_qoq"], "QoQ")}'

    tier = "LARGE CAP" if d["market_cap"] >= 10e9 else ("MID CAP" if d["market_cap"] >= 2e9 else "SMALL CAP")
    capstr = money(d["market_cap"]) if d["market_cap"] else "n/a"
    tags = "".join([
        f'<div class="tag">MKT CAP <b>{capstr}</b> · {tier}</div>',
        f'<div class="tag">SECTOR · {d["sector"].upper()}</div>',
        f'<div class="tag">{("REPORTED · " + d["timing"]) if d.get("timing") else "RESULTS"}</div>',
        f'<div class="tag">SOURCE · {d.get("source","BENZINGA")}</div>',
    ])

    repl = {
        "DATELINE": (f'{d["date_str"]} · {d["timing"]}' if d.get("timing") else d["date_str"]),
        "TAG": f'EARNINGS · {vval}',
        "TICKER": f'${d["ticker"]}',
        "CAP": f'{d["name"]} · {d["period"]} {d["period_year"]} · vs consensus',
        "HEADLINE": head,
        "REV_VAL": money(d["revenue"]),
        "REV_EST": money(d["revenue_est"]),
        "REV_CHIP": rev_chip,
        "REV_DELTA": rev_delta,
        "EPS_LABEL": f'EPS · {d["eps_type"].upper()}',
        "EPS_VAL": eps_fmt(d["eps"]),
        "EPS_EST": eps_fmt(d["eps_est"]),
        "EPS_CHIP": eps_chip,
        "EPS_DELTA": eps_delta,
        "VERDICT_COLOR": vcolor,
        "VERDICT_VAL": vval,
        "VERDICT_SUB": vsub,
        "VERDICT_CHIP": vchip,
        "VERDICT_DELTA": f'Surprise: <span style="color:{CM}">Rev {pct1(d["rev_surprise"])} · EPS {pct1(d["eps_surprise"])}</span>',
        "TAGS": tags,
        "SRCNOTE": d.get("source_note", "as reported by Benzinga"),
    }
    html = TEMPLATE
    for k, v in repl.items():
        html = html.replace("{{" + k + "}}", str(v))
    return html


def find_chrome():
    if os.environ.get("CHROME_BIN") and os.path.exists(os.environ["CHROME_BIN"]):
        return os.environ["CHROME_BIN"]
    for c in ["google-chrome-stable", "google-chrome", "chromium-browser", "chromium"]:
        p = shutil.which(c)
        if p:
            return p
    for pat in ["/opt/pw-browsers/**/chrome", "/opt/pw-browsers/**/headless_shell"]:
        m = glob.glob(pat, recursive=True)
        if m:
            return m[0]
    raise RuntimeError("No Chromium/Chrome binary found. Set CHROME_BIN.")


def render(d, out_png):
    html = build_html(d)
    html_path = os.path.join(HERE, "_card_tmp.html")
    with open(html_path, "w") as f:
        f.write(html)
    chrome = find_chrome()
    cmd = [chrome, "--headless", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
           "--force-device-scale-factor=2", "--window-size=1200,1400",
           f"--screenshot={out_png}", "file://" + html_path]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _crop_to_content(out_png)
    return out_png


def _crop_to_content(png):
    """Trim to the lowest real content row (text/marks) + a bottom margin, so
    the card is exactly content-height with even padding and no dead space."""
    from PIL import Image
    im = Image.open(png).convert("RGB")
    W, H = im.size
    px = im.load()
    margin = 84
    def is_content_row(y):
        bright = dark = False
        for x in range(0, W, 5):
            r, g, b = px[x, y]
            if r > 150 or g > 165 or b > 205:
                bright = True
            elif r < 60 and g < 60 and b < 120:
                dark = True
            if bright and dark:
                return True
        return False
    bottom = None
    for y in range(H - 1, -1, -1):
        if is_content_row(y):
            bottom = y
            break
    if bottom is not None:
        cut = min(H, bottom + margin)
        im.crop((0, 0, W, cut)).save(png)


if __name__ == "__main__":
    import json
    fixture = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    render(fixture, os.path.join(HERE, "test_out.png"))
    print("wrote", os.path.join(HERE, "test_out.png"))
