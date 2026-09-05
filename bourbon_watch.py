#!/usr/bin/env python3
"""
Bourbon Watch v2 — allocated bourbon availability & deal monitor.

v2 changes:
  * Deal quality is now computed from MSRP *and* secondary-market value,
    not a single static threshold. Every finding is classified:
        STEAL : at/near MSRP, or well below secondary  -> instant alert (priority bottles)
        GOOD  : meaningfully below secondary           -> daily digest
        FAIR  : at secondary                           -> digest only if 'include_fair'
  * Only scans retailers you've explicitly vetted (see RETAILERS.md).
    The script will refuse to scan a retailer unless "vetted": true is set,
    as a speed bump against pasting in a random (possibly scam) storefront.

Usage:
  python3 bourbon_watch.py            # one scan cycle (cron this)
  python3 bourbon_watch.py --digest   # send daily digest
  python3 bourbon_watch.py --test-email

Requires: requests, beautifulsoup4
"""

import argparse
import html
import json
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}

# ---------------------------------------------------------------------------
# Config / state
# ---------------------------------------------------------------------------

def load_config():
    if not CONFIG_PATH.exists():
        sys.exit(f"Missing {CONFIG_PATH}. Copy config.example.json to config.json and edit it.")
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"seen": {}, "digest_queue": []}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Deal classification
# ---------------------------------------------------------------------------

def classify_deal(price, prod, rules):
    """
    Classify a listing price against MSRP and secondary value.

    STEAL: price <= msrp * msrp_tolerance   (i.e., near-MSRP on an allocated bottle)
           OR price <= secondary * steal_ratio
    GOOD : price <= secondary * good_ratio
    FAIR : price <= secondary * fair_ratio
    None : overpriced, ignore
    Hard cap: price > msrp * max_msrp_multiple (default 1.5) -> None, always.
    """
    msrp = prod.get("msrp")
    secondary = prod.get("secondary")
    tol = rules.get("msrp_tolerance", 1.15)
    steal = rules.get("steal_ratio", 0.65)
    good = rules.get("good_ratio", 0.85)
    fair = rules.get("fair_ratio", 1.0)

    # Hard ceiling: never call anything a deal above max_msrp_multiple x MSRP,
    # no matter how it compares to secondary. "Fair vs. flippers" is not fair retail.
    cap = rules.get("max_msrp_multiple", 1.5)
    if msrp and cap and price > msrp * cap:
        return None

    if msrp and price <= msrp * tol:
        return "STEAL"
    if secondary:
        if price <= secondary * steal:
            return "STEAL"
        if price <= secondary * good:
            return "GOOD"
        if price <= secondary * fair:
            return "FAIR"
    return None


def pct_of_secondary(price, prod):
    sec = prod.get("secondary")
    if not sec:
        return ""
    return f" · {price / sec * 100:.0f}% of secondary (${sec:.0f})"


# ---------------------------------------------------------------------------
# Product matching
# ---------------------------------------------------------------------------

def norm(s):
    s = s.lower()
    # collapse dotted acronyms so "C.Y.P.B." -> "cypb", "E.H." -> "eh", "W.L." -> "wl"
    s = re.sub(r"\b(?:[a-z]\.){2,}", lambda m: m.group(0).replace(".", ""), s)
    # split digit/letter runs so "375ml" -> "375 ml", "50ml" -> "50 ml", "1.75l" -> "1 75 l"
    s = re.sub(r"(?<=\d)(?=[a-z])|(?<=[a-z])(?=\d)", " ", s)
    return re.sub(r"[^a-z0-9 ]", " ", s)


# Titles containing any of these are never a target bottle, whatever else they say.
GLOBAL_EXCLUDES = ["tequila", "corazon", "expresiones", "mezcal", "rum", "cognac",
                   "empty", "empty bottle", "damaged", "raffle", "ticket", "entry",
                   "gift set", "bundle", "combo", "sampler", "mini", "50 ml", "100 ml",
                   "200 ml", "375", "1 75", "poland", "japan", "export", "taiwan"]
BUNDLE_RE = re.compile(r"\s\+\s|\s&\s(?![a-z]*\s*(?:sons?|co\b))")   # "A + B", "A & B"
YEAR_RE = re.compile(r"(?<!\d)(19\d\d|20\d\d)(?!\d)")


def title_is_noise(raw_title, rules):
    """Bundles, vintages, tequila-finished-in-bourbon-barrels, minis, etc."""
    t = norm(raw_title)
    if any(kw_in(t, x) for x in GLOBAL_EXCLUDES):
        return True
    if BUNDLE_RE.search(raw_title):
        return True
    min_year = rules.get("exclude_vintage_before", 2022)
    years = [int(y) for y in YEAR_RE.findall(raw_title)]
    if years and min(years) < min_year:
        return True
    return False


def kw_in(text, kw):
    """Whole-word phrase match on normalized text, so '12' does not match '2012'
    or '$120', and 'rye' does not match 'ryerson'."""
    kw = norm(kw).strip()
    if not kw:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", text) is not None


def product_matches(text, p):
    """All keywords present as whole words, and no exclude keyword present."""
    if not all(kw_in(text, kw) for kw in p["keywords"]):
        return False
    if any(kw_in(text, x) for x in p.get("exclude_keywords", [])):
        return False
    return True


def match_product(title, products, rules=None):
    if title_is_noise(title, rules or {}):
        return None
    t = norm(title)
    for p in products:
        if product_matches(t, p):
            return p
    return None


# ---------------------------------------------------------------------------
# Retailer checkers
# ---------------------------------------------------------------------------

def make_finding(prod, title, price, retailer, url, rules, note=None):
    tier = classify_deal(price, prod, rules)
    if tier is None:
        return None
    return {
        "product": prod["name"],
        "title": title,
        "price": price,
        "retailer": retailer,
        "url": url,
        "priority": prod.get("priority", False),
        "tier": tier,
        "msrp": prod.get("msrp"),
        "secondary": prod.get("secondary"),
        "note": note,
    }


def check_shopify(retailer, products, rules, session):
    """Read a Shopify store's public /products.json catalog (reliable prices + stock)."""
    findings = []
    base = retailer["url"].rstrip("/")
    page = 1
    while page <= retailer.get("max_pages", 6):
        url = f"{base}/products.json?limit=250&page={page}"
        try:
            r = session.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            items = r.json().get("products", [])
        except Exception as e:
            print(f"  [warn] {retailer['name']} page {page}: {e}")
            break
        if not items:
            break
        for item in items:
            prod = match_product(item.get("title", ""), products, rules)
            if not prod:
                continue
            for v in item.get("variants", []):
                if not v.get("available", False):
                    continue
                try:
                    price = float(v["price"])
                except (KeyError, ValueError, TypeError):
                    continue
                f = make_finding(prod, item["title"], price, retailer["name"],
                                 f"{base}/products/{item['handle']}", rules)
                if f:
                    findings.append(f)
        page += 1
        time.sleep(1)
    return findings


def _text_blocks(soup, max_len=400):
    """Yield short text blocks (product-card sized) from a page, so keyword and
    price matching happens on one listing at a time instead of the whole page."""
    seen = set()
    for tag in soup.find_all(["li", "article", "tr", "div", "p", "section"]):
        txt = tag.get_text(" ", strip=True)
        if not txt or len(txt) > max_len or "$" not in txt:
            continue
        if txt in seen:
            continue
        seen.add(txt)
        yield txt


def check_html_page(retailer, products, rules, session):
    """Generic keyword-near-price heuristic for non-Shopify pages (state ABCs, etc.).
    Matches each product against individual page blocks that contain a price,
    so a keyword somewhere on the page can't be paired with an unrelated price."""
    from bs4 import BeautifulSoup

    findings = []
    try:
        r = session.get(retailer["url"], headers=HEADERS, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print(f"  [warn] {retailer['name']}: {e}")
        return findings

    soup = BeautifulSoup(r.text, "html.parser")
    best = {}
    for block in _text_blocks(soup):
        bnorm = norm(block)
        if re.search(r"sold\s*out|out\s*of\s*stock|unavailable|notify me", bnorm):
            continue
        prod = match_product(block, products, rules)
        if not prod:
            continue
        prices = [float(m.replace(",", "")) for m in re.findall(r"\$\s*([\d,]+\.?\d{0,2})", block)]
        prices = [p for p in prices if 15 <= p <= 5000]
        if not prices:
            continue
        price = min(prices)
        if prod["name"] not in best or price < best[prod["name"]][1]:
            best[prod["name"]] = (prod, price, block[:120])

    for prod, price, snippet in best.values():
        f = make_finding(prod, f"{prod['name']} — {snippet}", price,
                         retailer["name"], retailer["url"], rules,
                         note="Price parsed heuristically — verify on page before buying.")
        if f:
            findings.append(f)
    return findings


def check_watch_page(retailer, products, rules, session):
    """
    Presence watcher for pages without prices (availability pages, lottery
    announcements). Fires a WATCH finding when a target bottle's keywords appear
    together in one text block (a list item, table row, paragraph) — not merely
    somewhere on the page — and exclusions are honored. No price classification.
    WATCH findings go to the daily digest only, never the instant alert.
    """
    from bs4 import BeautifulSoup

    findings = []
    try:
        r = session.get(retailer["url"], headers=HEADERS, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print(f"  [warn] {retailer['name']}: {e}")
        return findings

    soup = BeautifulSoup(r.text, "html.parser")
    blocks = []
    seen = set()
    for tag in soup.find_all(["li", "tr", "p", "h1", "h2", "h3", "h4", "td", "span", "div"]):
        txt = tag.get_text(" ", strip=True)
        if txt and len(txt) <= 300 and txt not in seen:
            seen.add(txt)
            blocks.append(txt)

    hit = {}
    for block in blocks:
        prod = match_product(block, products, rules)
        if prod and prod["name"] not in hit:
            hit[prod["name"]] = (prod, block[:140])

    for prod, snippet in hit.values():
        findings.append({
            "product": prod["name"],
            "title": f"{prod['name']} listed — \"{snippet}\"",
            "price": None,
            "retailer": retailer["name"],
            "url": retailer["url"],
            "priority": prod.get("priority", False),
            "tier": "WATCH",
            "msrp": prod.get("msrp"),
            "secondary": prod.get("secondary"),
            "note": retailer.get("note", "Appeared on watched page — check details at the link."),
        })
    return findings


CHECKERS = {"shopify": check_shopify, "html": check_html_page, "watch": check_watch_page}


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

TIER_BADGE = {"STEAL": "🟢 STEAL", "GOOD": "🟡 GOOD", "FAIR": "⚪ FAIR", "WATCH": "👁 WATCH"}

TABLE_STYLE = (
    "<table border='1' cellpadding='8' cellspacing='0' "
    "style='border-collapse:collapse;font-family:sans-serif'>"
    "<tr><th>Deal</th><th>Bottle</th><th>Price</th><th>Retailer</th><th>Link</th></tr>"
)


def deal_row(d):
    msrp = f"MSRP ${d['msrp']:.0f}" if d.get("msrp") else ""
    if d.get("price") is not None:
        ctx = ", ".join(x for x in [msrp, pct_of_secondary(d["price"], d).strip(" ·")] if x)
        price_cell = f"${d['price']:.2f}<br><small>{ctx}</small>"
    else:
        price_cell = f"—<br><small>{msrp}</small>"
    note = f"<br><small>{html.escape(d['note'])}</small>" if d.get("note") else ""
    return (
        f"<tr><td>{TIER_BADGE.get(d['tier'], d['tier'])}</td>"
        f"<td><b>{html.escape(d['title'])}</b>{note}</td>"
        f"<td>{price_cell}</td>"
        f"<td>{html.escape(d['retailer'])}</td>"
        f"<td><a href='{d['url']}'>View</a></td></tr>"
    )


def send_email(cfg, subject, html_body):
    smtp = cfg["smtp"]
    recipients = cfg["recipients"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp["from_address"]
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP(smtp["host"], smtp.get("port", 587)) as server:
        server.starttls()
        server.login(smtp["username"], smtp["password"])
        server.sendmail(smtp["from_address"], recipients, msg.as_string())
    print(f"  [mail] Sent '{subject}' to {len(recipients)} recipient(s)")


def send_instant_alert(cfg, deals):
    rows = "".join(deal_row(d) for d in deals)
    body = (
        "<h2>&#128293; Priority bottle alert</h2>"
        "<p>At/near MSRP or well under secondary. These vanish in minutes — "
        "verify the listing and buy fast, but only through the linked vetted retailer. "
        "Pay by credit card. If checkout demands a wire, Zelle, or a surprise "
        "'insurance' fee, walk away.</p>"
        f"{TABLE_STYLE}{rows}</table>"
        "<p><small>Bourbon Watch · check your state's shipping rules.</small></p>"
    )
    names = ", ".join(sorted({d["product"] for d in deals}))
    send_email(cfg, f"🥃 STEAL: {names}", body)


def send_digest(cfg, state):
    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
    include_fair = cfg.get("deal_rules", {}).get("include_fair_in_digest", False)
    recent = [d for d in state["digest_queue"]
              if d["found_at"] >= cutoff and (include_fair or d["tier"] != "FAIR")]
    if not recent:
        body = "<p>No qualifying deals in the last 24 hours. The hunt continues. 🥃</p>"
    else:
        best = {}
        for d in recent:
            key = (d["retailer"], d["title"])
            if key not in best or (d["price"] or 0) < (best[key]["price"] or 0):
                best[key] = d
        order = {"STEAL": 0, "GOOD": 1, "FAIR": 2, "WATCH": 3}
        deals = sorted(best.values(),
                       key=lambda x: (order.get(x["tier"], 4), not x["priority"], x["price"] or 0))
        rows = "".join(deal_row(d) for d in deals)
        body = (
            f"<h2>Daily Bourbon Watch digest — {datetime.now():%b %d, %Y}</h2>"
            f"{TABLE_STYLE}{rows}</table>"
            "<p><small>🟢 near MSRP / far under secondary · 🟡 under secondary · "
            "⚪ at secondary · 👁 spotted on a watched page (no price). "
            "Nothing above 1.5× MSRP is ever listed. Prices at scan time; verify before purchase.</small></p>"
        )
    send_email(cfg, f"🥃 Bourbon Watch daily digest — {datetime.now():%b %d}", body)
    keep = (datetime.now() - timedelta(hours=48)).isoformat()
    state["digest_queue"] = [d for d in state["digest_queue"] if d["found_at"] >= keep]


# ---------------------------------------------------------------------------
# Website export
# ---------------------------------------------------------------------------

def write_site_export(cfg, findings):
    """
    Write data/deals.json for the static site. The site fetches this file
    relative to itself; committing + pushing the repo makes Netlify redeploy.
    """
    export_path = cfg.get("site_export")
    if not export_path:
        return
    path = (BASE_DIR / export_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "sample": False,
        "products": [
            {
                "name": p["name"],
                "msrp": p.get("msrp"),
                "secondary": p.get("secondary"),
                "priority": p.get("priority", False),
            }
            for p in cfg["products"]
        ],
        "listings": [
            {
                "product": f["product"],
                "title": f["title"],
                "price": f["price"],
                "retailer": f["retailer"],
                "url": f["url"],
                "tier": f["tier"],
                "note": f.get("note"),
            }
            for f in findings if f.get("price") is not None
        ],
    }
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2)
    print(f"  [site] Wrote {len(payload['listings'])} listings to {path}")

    update_history(path.parent / "history.json", findings)


def update_history(path, findings, max_days=365):
    """
    Append today's lowest observed price per product to history.json.
    One point per product per day (the day's lowest wins), capped at max_days.
    Shape: { "Product Name": [ {"d": "2026-08-19", "p": 44.99, "r": "Retailer"}, ... ] }
    """
    history = {}
    if path.exists():
        try:
            with open(path) as fh:
                history = json.load(fh)
        except Exception:
            history = {}

    today = datetime.now().strftime("%Y-%m-%d")
    lows = {}
    for f in findings:
        if f.get("price") is None:
            continue
        cur = lows.get(f["product"])
        if cur is None or f["price"] < cur["price"]:
            lows[f["product"]] = f

    for name, f in lows.items():
        series = history.setdefault(name, [])
        if series and series[-1]["d"] == today:
            if f["price"] < series[-1]["p"]:
                series[-1] = {"d": today, "p": f["price"], "r": f["retailer"]}
        else:
            series.append({"d": today, "p": f["price"], "r": f["retailer"]})
        del series[:-max_days]

    with open(path, "w") as fh:
        json.dump(history, fh, indent=2)
    print(f"  [site] History updated for {len(lows)} product(s)")


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------

def scan(cfg, state):
    session = requests.Session()
    products = cfg["products"]
    rules = cfg.get("deal_rules", {})
    all_findings = []

    for retailer in cfg["retailers"]:
        if not retailer.get("vetted", False):
            print(f"Skipping {retailer['name']} — not marked vetted:true. "
                  f"Read RETAILERS.md, verify it, then flip the flag.")
            continue
        print(f"Checking {retailer['name']} ...")
        rtype = retailer.get("type", "shopify")
        if rtype == "va_abc":
            try:
                import va_abc
                all_findings.extend(va_abc.check_inventory(retailer, products, rules, session, state))
            except Exception as e:
                print(f"  [warn] {retailer['name']} failed: {e}")
            continue
        checker = CHECKERS.get(rtype)
        if not checker:
            print(f"  [warn] unknown type {rtype}")
            continue
        try:
            all_findings.extend(checker(retailer, products, rules, session))
        except Exception as e:
            print(f"  [warn] {retailer['name']} failed: {e}")

    now = datetime.now().isoformat()
    instant, queued = [], 0

    for d in all_findings:
        price_key = f"{d['price']:.2f}" if d.get("price") is not None else "seen"
        key = f"{d['retailer']}|{d['title']}|{price_key}"
        last_seen = state["seen"].get(key)
        fresh = not last_seen or last_seen < (datetime.now() - timedelta(hours=24)).isoformat()
        if not fresh:
            continue
        if d["tier"] == "STEAL" and d["priority"]:
            instant.append(d)
        state["digest_queue"].append({**d, "found_at": now})
        queued += 1
        state["seen"][key] = now

    print(f"Scan complete: {len(all_findings)} classified deals, "
          f"{len(instant)} instant alerts, {queued} queued for digest.")
    if instant:
        send_instant_alert(cfg, instant)

    write_site_export(cfg, all_findings)

    prune = (datetime.now() - timedelta(days=7)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= prune}


def main():
    ap = argparse.ArgumentParser(description="Bourbon Watch monitor")
    ap.add_argument("--digest", action="store_true")
    ap.add_argument("--test-email", action="store_true")
    ap.add_argument("--va-discover", action="store_true",
                    help="one-time: discover VA ABC API endpoints and cache them")
    args = ap.parse_args()

    cfg = load_config()
    state = load_state()

    if args.test_email:
        send_email(cfg, "🥃 Bourbon Watch test", "<p>SMTP works! You're all set.</p>")
        return
    if args.va_discover:
        import va_abc
        va_abc.discover(state, cfg["products"])
        save_state(state)
        return
    if args.digest:
        send_digest(cfg, state)
    else:
        scan(cfg, state)
    save_state(state)


if __name__ == "__main__":
    main()