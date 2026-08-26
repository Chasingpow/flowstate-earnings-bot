"""
Flowstate Alpha â€” always-on real-time earnings worker.

Purpose: post "on the spot" instead of waiting for a scheduled sweep.

Two-stage timing:
  1) INSTANT PING  â€” watch the SEC EDGAR live 8-K feed for names we expect to
     report today. The moment an earnings 8-K (Item 2.02) crosses for a
     watchlist name, fire a text alert to Discord within ~1-2 min of release:
     "ğŸ”” $NVDA results are OUT". Optionally parse preliminary EPS/revenue
     straight from the press-release exhibit (PARSE_PRELIM=1).
  2) FULL CARD     â€” then poll FMP for the structured actuals and post the
     branded card (with the Double Beat green box) the instant they populate.

This runs as a long-lived process (systemd / any always-on Linux box), NOT on
GitHub Actions cron â€” cron can't do second/minute-level timing.

Reuses post_earnings.py (FMP + Discord + dedupe) and render_card.py (the card).

Env (in addition to those post_earnings.py reads):
  FMP_API_KEY, DISCORD_WEBHOOK_URL   (required â€” same as the cron bot)
  SEC_USER_AGENT     SEC requires a UA with contact info. default below.
  EDGAR_POLL_SEC     how often to poll the 8-K feed (default 20)
  ACTUALS_POLL_SEC   how often to poll FMP for a pending name (default 45)
  ACTUALS_TIMEOUT_MIN  give up waiting for FMP actuals (default 90)
  WATCHLIST_REFRESH_MIN  rebuild "reporting today" list (default 30)
  PARSE_PRELIM       1 = try to parse prelim numbers into the ping (default 0)
  SELFTEST_TICKER    on startup, post a full card for this ticker to prove the
                     VM can render + post, then continue normally. optional.
  HEARTBEAT_MIN      log a heartbeat every N min (default 15)
"""
import os, sys, json, time, re, datetime as dt
import requests
import post_earnings as pe
import render_card as rc

HERE = os.path.dirname(os.path.abspath(__file__))
ALERTED_PATH = os.path.join(HERE, "state", "alerted.json")

SEC_UA = os.environ.get("SEC_USER_AGENT", "FlowstateAlpha earnings-bot (contact: keithharasymiw@gmail.com)")
EDGAR_POLL = int(os.environ.get("EDGAR_POLL_SEC", "20"))
ACTUALS_POLL = int(os.environ.get("ACTUALS_POLL_SEC", "45"))
ACTUALS_TIMEOUT = int(os.environ.get("ACTUALS_TIMEOUT_MIN", "90")) * 60
WATCHLIST_REFRESH = int(os.environ.get("WATCHLIST_REFRESH_MIN", "30")) * 60
PARSE_PRELIM = os.environ.get("PARSE_PRELIM", "0") == "1"
SELFTEST_TICKER = os.environ.get("SELFTEST_TICKER", "").strip().upper()
HEARTBEAT = int(os.environ.get("HEARTBEAT_MIN", "15")) * 60

EDGAR_CURRENT = ("https://www.sec.gov/cgi-bin/browse-edgar"
                 "?action=getcurrent&type=8-K&company=&dateb=&owner=include&count=100&output=atom")
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
ATOM = "{http://www.w3.org/2005/Atom}"


def log(*a):
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    print(ts, *a, flush=True)


def sec_get(url, timeout=25):
    r = requests.get(url, headers={"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"}, timeout=timeout)
    r.raise_for_status()
    return r


# ---------- ping-dedupe state (so a restart doesn't double-ping) ----------

def load_alerted():
    try:
        with open(ALERTED_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_alerted(d):
    # keep only the last 3 days of ping keys
    days = sorted(d.keys())[-3:]
    d = {k: d[k] for k in)^\ßBˆÜË›XZÙY\œÊÜËœ]™\›˜[YJST•QÔU
K^\İÛÚÏUYJBˆÚ]Ü[ŠST•QÔUÈŠH\È‚ˆœÛÛ‹™[\
‹[™[LJB‚‚ˆÈKKKKKKKKKHXÚÙ\ˆOˆÒRÈKKKKKKKKKB‚™YˆØYØÚZ×ÛX\

N‚ˆˆˆÕPÒÑTˆÚZ×Ú[H[™ØÚZ×Ú[ˆPÒÑTŸHœ›ÛHÑPÉÜÈÙ™šXÚX[š[Kˆˆˆ‚ˆ]HHÙX×ÙÙ]
PÒÑT”×ÕT“
KšœÛÛŠ
Bˆ˜ËÌHßKßBˆ›Üˆ›İÈ[ˆ]K˜[Y\Ê
N‚ˆÈHİŠ›İË™Ù]
XÚÙ\ˆ‹ˆŠJK\\Š
BˆÚZÈH[
›İË™Ù]
˜ÚZ×Üİˆ‹
JBˆYˆÈ[™ÚZÎ‚ˆ˜Öİ×HHÚZÂˆÌœÙ]Y˜][
ÚZËÊHÈš\œİ
\™Ù\İ
HÚ[œÈÛˆÚ\™YÒRÂˆ™]\›ˆ˜ËÌ‚‚ˆÈKKKKKKKKKHØ]Ú\İˆ˜[Y\È^XİYÈ™\ÜÙ^HKKKKKKKKKB‚™YˆZ[İØ]Ú\İ
˜ÊN‚ˆˆˆÒRÜÈÙH^XİÈ™\ÜÙ^H
UËZ\ÚÚ[™İÎˆY\İ\™^K‹Ù^HÈØ]ÚˆY\‹Zİ\œÈ™\ÜÈ]Ü›ÜÜÈZYšYÚUÊKˆ™]\›œÈØÚZ×Ú[ˆŞ[X›ÛKˆˆˆ‚ˆÙ^HH™]][YK]Û›İÊ
K™]J
Bˆœ›HH
Ù^HH[YY[J^\ÏLJJKš\ÛÙ›Ü›X]

BˆØ[HK™›\ØØ[[™\Šœ›KÙ^Kš\ÛÙ›Ü›X]

JBˆÛHßBˆ›ÜˆH[ˆØ[‚ˆŞ[HH
K™Ù]
œŞ[X›ÛŠHÜˆˆŠK\\Š
BˆÚZÈH˜Ë™Ù]
Ş[JBˆYˆÚZÎ‚ˆÛØÚZ×HHŞ[Bˆ™]\›ˆÛ‚‚ˆÈKKKKKKKKKHQĞTˆRÈ™YYKKKKKKKKKB‚™Yˆ\œÙWÙ™YY
[İ^
N‚ˆˆˆ–ZY[XİÎˆØÚZËXØÙ\ÜÚ[Û‹\›\]Y]_Hœ›ÛHHÙ]İ\œ™[]ÛH™YYˆˆˆ‚ˆ[\Ü[™]™YK‘[[Y[™YH\ÈUˆİ]H×BˆN‚ˆ›ÛİHU™œ›Û\İš[™Ê[İ^
Bˆ^Ù\^Ù\[Ûˆ\ÈN‚ˆÙÊ™™YY\œÙH\œ›Üˆ‹JBˆ™]\›ˆİ]ˆ›Üˆ[H[ˆ›Ûİ™š[™[
ˆĞUÓ_Y[HŠN‚ˆ]HH
[K™š[™^
ˆĞUÓ_]]HŠHÜˆˆŠKœİš\

Bˆ\]YH
[K™š[™^
ˆĞUÓ_]\]YŠHÜˆˆŠKœİš\

Bˆ™YˆHˆ‚ˆ[šÈH[K™š[™
ˆĞUÓ_[[šÈŠBˆYˆ[šÈ\È›İ›Û™N‚ˆ™YˆH[šË™Ù]
š™Yˆ‹ˆŠBˆHH™KœÙX\˜Ú
ˆ‹Ù]KÊ
ÊKÈ‹™YŠBˆÚZÈH[
K™Ü›İ\
JJHYˆH[ÙH›Û™BˆXØÈHˆ‚ˆXHH™KœÙX\˜Ú
ˆ‹ÊÌNW^ÌLKVÌNW^ÌŸKVÌNW^ÍŸJKZ[™^‹™YŠHÜˆ™KœÙX\˜Ú
ˆŠÌNJH‹™YŠBˆYˆXN‚ˆXØÈHXK™Ü›İ\
JBˆYˆ›İXØÎ‚ˆXØÈH™YˆÈ˜[˜XÚÈY\HÙ^Bˆİ]˜\[™
È˜ÚZÈˆÚZË˜XØÙ\ÜÚ[ÛˆˆXØË\›ˆ™Y‹\]Yˆ\]Y]Hˆ]_JBˆ™]\›ˆİ]‚‚™Yˆ\×ÙX\›š[™Ü×Ùš[[™Ê[™^İ\›
N‚ˆˆˆ™\İYY™›ÜÛÛ™š\›HHRÈ\È[ˆX\›š[™ÜÈ™[X\ÙH
][H‹ŒˆÈVNNJK‚ˆ\›Z\ÜÚ]™NˆÛˆ[H™]Ú›İX›H™]\›ˆYKÚ[˜ÙHHÒRÈ\È[™XYHØ]YˆÈ˜[Y\ÈÙH^XİÈ™\ÜÙ^Kˆˆˆ‚ˆYˆ›İ[™^İ\›‚ˆ™]\›ˆYBˆN‚ˆ[HÙX×ÙÙ]
[™^İ\›[Y[İ]LMJK^ˆ^Ù\^Ù\[Û‚ˆ™]\›ˆYBˆİÈH[›İÙ\Š
BˆYˆŒ‹Œˆˆ[ˆ[Üˆœ™\İ[ÈÙˆÜ\˜][ÛœÈˆ[ˆİÎ‚ˆ™]\›ˆYBˆYˆ™^NNHˆ[ˆİÈÜˆ™^NHˆ[ˆİÈÜˆœ™\ÜÈ™[X\ÙHˆ[ˆİÎ‚ˆ™]\›ˆYBˆÈ™]ÚYš[™H]ÛÚÜÈZÙHH›Û‹YX\›š[™ÜÈRÂˆ™]\›ˆ˜[ÙB‚‚ˆÈKKKKKKKKKH™[[Z[˜\H[X™\ˆ\œÙH
ÜZ[‹™\İY™›Ü
HKKKKKKKKKB‚“SÓ‘VWÔ‘HH™K˜ÛÛ\[Jˆ—	×ÏÊÌNWVÌNK—JŠWÏÊš[[ÛŸZ[[ÛŸ›Ÿ[ŸŸJWˆ‹™K’JB‘T×Ô‘HH™K˜ÛÛ\[JˆŠÎ™[]YÊÊOÊÎ›™]ÊÊOÙX\›š[™ÜÏ×ÊÜ\—ÊÊÎ™[]YÊÊOÜÚ\™V×‰W^ÌJ
ËO×	×ÏÖÌNWJ×–ÌNW^ÌŸW
OÊH‹™K’JB‚‚™Yˆ^˜XİÜ™[[J[™^İ\›
N‚ˆˆˆ•HÈ[™]™[YH
ÈTÈœ›ÛHH™\ÜË\™[X\ÙH^Xš]ˆ™]\›œÈHÚÜˆİš[™ÈZÙH	Ü™[[Nˆ™]ˆ‰L‹ŒĞˆ0­ÈTÈ‰‹ŒL	ÈÜˆ	ÉÈYˆ›İÛÛ™šY[ˆˆˆ‚ˆN‚ˆYHÙX×ÙÙ]
[™^İ\›[Y[İ]LMJK^ˆÈš[™Hš[X\H™\ÜË\™[X\ÙH^Xš]
^NNJ‹šJBˆHH™KœÙX\˜Ú
‰Ú™YHŠ×ˆ—J™^OÎNV×ˆ—J—šV×ˆ—JŠH‰ËY™K’JBˆYˆ›İN‚ˆ™]\›ˆˆ‚ˆØÈHK™Ü›İ\
JBˆYˆØËœİ\İÚ]
‹ÈŠN‚ˆØÈHšÎ‹ËİİİËœÙXË™Ûİˆˆ
ÈØÂˆ[Yˆ›İØËœİ\İÚ]
šŠN‚ˆ˜\ÙHH[™^İ\›œœÜ]
‹È‹JVÌBˆØÈH˜\ÙH
È‹Èˆ
ÈØÂˆ^H™KœİXŠˆ×—JÏˆ‹ˆ‹ÙX×ÙÙ]
ØË[Y[İ]LŒ
K^
Bˆ^H™KœİXŠˆ—ÊÈ‹ˆ‹^
Bˆ^Ù\^Ù\[Û‚ˆ™]\›ˆˆ‚ˆ\ÈH›Û™BˆYHHT×Ô‘KœÙX\˜Ú
^
BˆYˆYN‚ˆ\ÈHYK™Ü›İ\
JKœ™\XÙJˆ‹ˆŠBˆ™]ˆH›Û™Bˆ\ˆH™KœÙX\˜Ú
ˆŠÎİ[ÊÊOÊÎ›™]ÊÊOÜ™]™[Y\ÏÖ×‰^ÌÌHˆ
ÈSÓ‘VWÔ‘Kœ]\›‹^™K’JBˆYˆ\‚ˆ™]ˆH‰ˆ
È\‹™Ü›İ\
JH
È\‹™Ü›İ\
ŠK\\Š
VÌBˆš]ÈH×BˆYˆ™]‚ˆš]Ë˜\[™
ˆœ™]ˆÜ™]ŸHŠBˆYˆ\Î‚ˆš]Ë˜\[™
ˆ‘TÈÙ\ßHŠBˆ™]\›ˆ
œ™[[Nˆˆ
Èˆ0­È‹š›Ú[Šš]ÊJHYˆš]È[ÙHˆ‚‚‚ˆÈKKKKKKKKKH\ØÛÜ™KKKKKKKKKB‚™YˆÜİİ^
\ÙÊN‚ˆˆH™\]Y\İËœÜİ
K‘TĞÓÔ‘ÕÑP’ÓÒ×ÕT“œÛÛ^È˜ÛÛ[ˆ\ÙÖÎŒŒ_K[Y[İ]LÌ
BˆYˆ‹œİ]\×ØÛÙH›İ[ˆ
ŒŒ
N‚ˆ˜Z\ÙH[[YQ\œ›ÜŠˆ‘\ØÛÜ™^Ü‹œİ]\×ØÛÙ_NˆÜ‹^ÎŒŒ_HŠB‚‚™Yˆ[œİ[Ü[™ÊŞ[X›Û˜[YKXË\]Y[™^İ\›™[[OHˆŠN‚ˆÚ[ˆHˆ‚ˆHH™KœÙX\˜Ú
ˆ•
ÌŸN—ÌŸJH‹\]YÜˆˆŠBˆYˆN‚ˆÚ[ˆHˆˆ0­Èš[YÛK™Ü›İ\
J_HU‚ˆØ\H˜Ë›[Û™^JXÊHYˆXÈ[ÙHˆ‚ˆXYHˆ¼'å%
Š‰ÜŞ[X›ÛH8 %Û˜[Y_JŠˆ™\İ[È\™HÕUİÚ[ŸH‚ˆ[™LˆHˆØØ\H0­ÈRÈ\İÜ›ÜÜÙYHÚ\™H8 %[Ø\™[˜ÛÛZ[™È\È[X™\œÈÛÛ™š\›Kˆ‚ˆ\ÈHÚXY[™L—BˆYˆ™[[N‚ˆ\Ë˜\[™
ˆ¸¦¨HÜ™[[_HÊ[›Ù™šXÚX[\œÙYœ›ÛHH™[X\ÙH8 %™\šYJWÈŠBˆYˆ[™^İ\›‚ˆ\Ë˜\[™
ˆÚ[™^İ\›OˆŠBˆÜİİ^
—ˆ‹š›Ú[Š\ÊJB‚‚ˆÈKKKKKKKKKH[Ø\™
™]\ÙHÜ›Ûˆ›İÙÚXÊHKKKKKKKKKB‚™YˆWÙ[ØØ\™
Ş[X›Û™\ÜÙ]KÜİY
N‚ˆˆˆ”™]\›ˆYHYˆHØ\™Ø\ÈÜİY
XİX[ÈÙ\™H™XYJKˆˆˆ‚ˆ\İHK™›\Ú\İÜJŞ[X›Û
Bˆ›İÜÈHÚ›Üˆ[ˆ\İYˆKš\×ØXİX[Ê
H[™™Ù]
™]HŠWBˆYˆ›İ›İÜÎ‚ˆ™]\›ˆ˜[ÙBˆ›İÜËœÛÜ
Ù^O[[X™HŠ…²&FFR%ÒÂ&WfW'6SÕG'VR¢RÒæöæP¢f÷"‚–â&÷w3 ¢–b…²&FFR%ÒÓÒ&W÷'EöFFS ¢RÒ€¢'&V°¢–bR—2æöæS ¢266WBF†RæWvW7B&W÷'FVB&÷r–b—Bw2v—F†–â"F—2öbF†Rf–Æ–æp¢F÷Ò&÷w5³Ğ¢G'“ ¢–b'2‚†GBæFFRæg&öÖ—6öf÷&ÖB‡F÷²&FFR%Ò’ÒGBæFFRæg&öÖ—6öf÷&ÖB‡&W÷'EöFFR’’æF—2’ÃÒ# ¢RÒF÷ ¢W†6WBW†6WF–öã ¢RÒæöæP¢–bR—2æöæS ¢&WGW&âfÇ6P¢RÒF–7B†R“²U²'7–Ö&öÂ%ÒÒ7–Ö&öÀ¢6–BÒb'·7–Ö&öÇÒ×¶U²vFFRu×Ò ¢–b6–B–â÷7FVC ¢&WGW&âG'VR2Ç&VG’†fR—@¢&öbÒRæf×÷&öf–ÆR‡7–Ö&öÂ¢BÒRæ'V–ÆEöB†RÂ†—7BÂ&öb¢ærÒ÷2çF‚æ¦ö–â„„U$RÂb%÷'E÷·7–Ö&öÇÒçær"¢&2ç&VæFW"†BÂær¢Rç÷7EöF—66÷&B†BÂær¢÷7FVBæFB‡6–B¢Rç6fU÷7FFR‡÷7FVB¢Æör†b"÷7FVBeTÄÂ6&B·7–Ö&öÇÒ‡¶E²væÖRu×Ò’·&2æÖöæW’†E²vÖ&¶WEö6uÒ—Ò"¢&WGW&âG'VP  ¢2ÒÒÒÒÒÒÒÒÒÒÖ–âÆö÷ÒÒÒÒÒÒÒÒÒĞ ¦FVbÖ–â‚“ ¢–bæ÷BRädÕô•ô´U’÷"æ÷BRäD•44õ$EõtT$„ôôµõU$Ã ¢Æör‚$U%$õ#¢dÕô•ô´U’æBD•44õ$EõtT$„ôôµõU$Â×W7B&R6WBâ"“²7—2æW†—Bƒ ¢Æör‚$fÆ÷w7FFR&VÂ×F–ÖRv÷&¶W"7F'F–ærâ"¢Æör†b"TDt"öÆÂ´TDt%õôÄÇ×2+r7GVÇ2öÆÂ´5ETÅ5õôÄÇ×2+rÖ–â6·&2æÖöæW’‡RäÔ”åôÔ$´UEô4—Ò+r&VÆ–Ó×µ%4Uõ$TÄ”×Ò" ¢÷7FVBÒRæÆöE÷7FFR‚¢ÆW'FVBÒÆöEöÆW'FVB‚ ¢–b4TÄeDU5EõD”4´U# ¢G'“ ¢Æör†b%4TÄeDU5C¢÷7F–ærgVÆÂ6&Bf÷"µ4TÄeDU5EõD”4´U'ÒFò&÷fRF†RdÒ6â&VæFW"·÷7N(
b"¢RÂ†—7BÒRæÆFW7E÷&W÷'FVB…4TÄeDU5EõD”4´U"¢–bS ¢&öbÒRæf×÷&öf–ÆR…4TÄeDU5EõD”4´U"¢BÒRæ'V–ÆEöB†RÂ†—7BÂ&öb¢ærÒ÷2çF‚æ¦ö–â„„U$RÂb%÷'E÷µ4TÄeDU5EõD”4´U'Òçær"¢&2ç&VæFW"†BÂær“²Rç÷7EöF—66÷&B†BÂær¢Æör‚"4TÄeDU5Bö²(	B6&B÷7FVBâ"¢VÇ6S ¢Æör‚"4TÄeDU5C¢æò&W÷'FVBV&æ–æw2f÷VæC²6¶—–ærâ"¢W†6WBW†6WF–öâ2Wƒ ¢Æör‚"4TÄeDU5BW'&÷#¢"ÂW‚ ¢C&2Â3'BÒÆöEö6–µöÖ‚¢vF6†Æ—7BÒ'V–ÆE÷vF6†Æ—7B‡C&2¢Æör†b'vF6†Æ—7C¢¶ÆVâ‡vF6†Æ—7B—ÒæÖW2W‡V7FVBFò&W÷'BFöF’"¢Æ7E÷vÂÒF–ÖRçF–ÖR‚¢Æ7Eö†"Òã ¢6VVåö62ÒÍ•Ğ ¤€€€€€€€€€€Œ€àµ,…•ÍÍ¥½¹Ì…±É•…‘ä¡…¹‘±•(€€€Á•¹‘¥¹œ€ôíô€€€€€€€€€€€€€€ŒÍåµ‰½°€´øíÉ•Á½ÉÑ}‘…Ñ”°Í¥¹•ô(€€€™¥ÉÍÑ}Á…ÍÌ€ôQÉÕ”((€€€İ¡¥±”QÉÕ”è(€€€€€€€¹½Ü€ôÑ¥µ”¹Ñ¥µ” ¤((€€€€€€€€ŒÉ•™É•Í ¥¬µ…À€¬İ…Ñ¡±¥ÍĞÁ•É¥½‘¥…±±ä(€€€€€€€¥˜¹½Ü€´±…ÍÑ}İ°€øô]Q!1%MQ}IIM è(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€ĞÉŒ°ŒÉĞ€ô±½…‘}¥­}µ…À ¤(€€€€€€€€€€€€€€€İ…Ñ¡±¥ÍĞ€ô‰Õ¥±‘}İ…Ñ¡±¥ÍĞ¡ĞÉŒ¤(€€€€€€€€€€€€€€€±½œ¡˜‰İ…Ñ¡±¥ÍĞÉ•™É•Í¡•èí±•¸¡İ…Ñ¡±¥ÍĞ¥ô¹…µ•Ìˆ¤(€€€€€€€€€€€•á•ÁĞá•ÁÑ¥½¸…Ì•àè(€€€€€€€€€€€€€€€±½œ ‰İ…Ñ¡±¥ÍĞÉ•™É•Í •ÉÉ½Èèˆ°•à¤(€€€€€€€€€€€±…ÍÑ}İ°€ô¹½Ü((€€€€€€€€ŒÁ½±°H€àµ,™¥É•¡½Í”(€€€€€€€ÑÉäè(€€€€€€€€€€€™••€ôÁ…ÉÍ•}™••¡Í•}•Ğ¡I}UII9P¤¹Ñ•áĞ¤(€€€€€€€€€€€¹•İ}¡¥ÑÌ€ô€À(€€€€€€€€€€€™½È¥Ğ¥¸™••è(€€€€€€€€€€€€€€€¥˜©t["accession"] in seen_acc:
                    continue
                seen_acc.add(it["accession"])
                if)š\œİÜ\ÜÎ‚ˆÛÛ[YHÈÛ‰İš\™HÛˆH˜XÚÛÙÈ™\Ù[]İ\\ˆÚZÈH]È˜ÚZÈ—BˆYˆÚZÊ—2æöæR÷"6–²¹½Ğ¥¸İ…Ñ¡±¥ÍĞè(€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€Íåµ‰½°€ôİ…Ñ¡±¥ÍÑm¥­t(€€€€€€€€€€€€€€€Ñ½‘…ä€ô¤t.datetime.utcnow().date().isoformat()
                akey =)ˆİÙ^_NÜŞ[X›ÛH‚ˆYŠÆW'FVBævWB‡FöF’Â·Ò’ævWB‡7–Ö&öÂ“ ¢6öçF–çVP¢–b¹½Ğ¥Í}•…É¹¥¹Í}™¥±¥¹œ¡¥Ñl‰ÕÉ°‰t¤è(€€€€€€€€€€€€€€€€€€€±½œ¡˜ˆ€€àµ,™½ÈíÍåµ‰½±ô‰ÕĞ®ot an earnings filing; skipping")
                    continue
                prof = pe.fmp_profile(symbol)
                mc = pe.num(prof.get("marketCap")) or 0.0
                name = prof.get("companyName") or symbol
                if mc and mc < pe.MIWÓPT’ÑUĞĞT‚ˆÙÊˆˆÜŞ[X›ÛH™\ÜY]Ü˜Ë›[Û™^JXÊ_HZ[ˆØ\Êæ÷–ær"¢ÆW'FVBç6WFFVfVÇB‡FöF’Â·Ò•·7–Ö&öÅÒÒ'6ÖÆÂ ¢6fUöÆW'FVB†ÆW'FVB¢6öçF–çVP¢&VÆ–ÒÒW‡G&7E÷&VÆ–Ò†—E²'W&Â%Ò’–b%4Uõ$TÄ”ÒVÇ6R" ¢–ç7FçE÷–ær‡7–Ö&öÂÂæÖRÂÖ2Â—E²'WFFVB%ÒÂ¥Ñl‰ÕÉ°‰t°ÁÉ•±¥´¤(€€€€€€€€€€€€€€€±½œ¡˜ˆ€ƒŠj„A%9íÍåµ‰½±ô€¡í¹…µ•ô¤íÉŒ¹µ½¹•ä¡µŒ¥ôˆ¤(€€€€€€€€€€€€€€€…±•ÉÑ•¹Í•Ñ‘•™…Õ±Ğ¡Ñ½‘…ä°íô¥mÍåµ‰½±t€ô€‰Á¥¹•ˆ(€€€€€€€€€€€€€€€Í…Ù•}…±•ÉÑ•¡…±•ÉÑ•¤(€€€€€€€€€€€€€€€Á•¹‘¥¹mÍåµ‰½±t€ôì‰É•Á½ÉÑ}‘…Ñ”ˆèÑ½‘…ä°€‰Í¥¹”ˆè¹½İô(€€€€€€€€€€€€€€€¹•İ}¡¥ÑÌ€¬ô€Ä(€€€€€€€€€€€¥˜¦irst_pass:
                log(f"primed on {len(seen_acc)} recent 8-Ks (no pings for backlog)")
            first_pass = False
        except Exception as)^‚ˆÙÊ‘QĞTˆÛ\œ›Üˆ‹^
B‚ˆÈÛÜšÈ[™[™È˜[Y\ÈİØ\™H[Ø\™ˆÛ™HH×Bˆ›ÜˆŞ[X›Û[™›È[ˆ[™[™Ëš][\Ê
N‚ˆN‚ˆYˆWÙ[ØØ\™
Ş[X›Û[™›ÖÈœ™\ÜÙ]H—KÜİY
N‚ˆÛ™K˜\[™
Ş[X›Û
Bˆ[Yˆ›İÈJ–æfõ²'6–æ6R%Òâ5ETÅ5õD”ÔTõUC ¢Æör†b"·7–Ö&öÇÓ¢7GVÇ27F–ÆÂæ÷BöâdÕgFW"F–ÖV÷WC²G&÷–ærgVÆÂ6&B"¢G'“ ¢÷7E÷FW‡B†b.(KûˆòG·7–Ö&öÇÓ¢&W7VÇG2&R÷WB'WBF†RFFfVVB†6âwBV&Æ—6†VB7G'V7GW&VBçVÖ&W'2–WB(	B6†V6²F†Rf–Æ–ærF—&V7FÇ’â"¢W†6WBá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€€€€€€€€€Á…ÍÌ(€€€€€€€€€€€€€€€€€€€‘½¹”¹…ÁÁ•¹¡Íåµ‰½°¤(€€€€€€€€€€€•á•ÁĞ…xception as ex:
                log(f"  full-card)\œ›ÜˆÜŞ[X›ÛNˆ‹
W‚¢f÷"2–â‘½¹”è(€€€€€€€€€€€Á•¹‘¥¹œ¹Á½À¡Ì°one)

        # heartbeat
        if now - last_hb >= HEARTBEAT:
            log(f"heartbeat Â· watchlist={len(watchlist)} Â· pending={len(pending)} Â· seen={len(seen_acc)}")
            last_hb = now

        # trim seen set so it doesn't grow unbounded
        if len(seen_acc) > 4000:
            seen_acc = set(list(seen_acc)[-2000:])

        time.sleep(EDGAR_POLL)


if __name__ == "__main__":
    main()
