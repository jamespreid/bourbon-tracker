#!/usr/bin/env python3
"""
va_abc.py — self-hosted Virginia ABC inventory tracker (VABourbon-style).

VA ABC's website is a JavaScript app: product prices, store-level inventory,
and lottery buttons are loaded from an internal JSON API, not present in the
page HTML. This module replicates what community trackers do:

  1. DISCOVER (one-time, run on your machine):   python3 bourbon_watch.py --va-discover
     Downloads the site's JS bundles, extracts candidate API endpoints
     (anything matching /webapi/, /api/, or Coveo search paths), probes them,
     and caches working endpoints + product IDs for your target bottles.

  2. TRACK (every scan cycle): queries store-level availability for each
     cached product, diffs quantities against the previous snapshot, and
     emits findings when stock APPEARS or INCREASES at any store — the
     "movers and shakers" logic. Findings carry store address + quantity.

  3. FALLBACK (always on, no API needed): plain page-watch of the lottery
     announcements page and product-list downloads page, which are normal
     server-rendered CMS pages.

NETWORK NOTE (Sept 2026): abc.virginia.gov sits behind a WAF that fingerprints
the TLS handshake. Plain `requests` and even `curl` with a browser User-Agent
get 403. This module therefore uses `curl_cffi`, which impersonates Chrome's
real handshake:   pip3 install curl_cffi
It always builds its own session (ignoring any session passed in from
bourbon_watch.py) so the impersonation is guaranteed.

Be a good citizen: this polls a public state-government site. Defaults are
gentle (inventory at three fixed times per day, product list once per day) and back off
on errors. Don't lower them.
"""

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    from curl_cffi import requests as _http
    _IMPERSONATE = True
except ImportError:  # pragma: no cover
    import requests as _http
    _IMPERSONATE = False
    print("[va] WARNING: curl_cffi not installed — VA ABC will likely return 403.")
    print("     Fix:  pip3 install curl_cffi")

BASE = "https://www.abc.virginia.gov"
LOTTERY_URL = f"{BASE}/products/limited-availability/lottery"
DOWNLOADS_URL = f"{BASE}/products/products-faqs/product-downloads"

# Look like a normal Chrome-on-Mac visitor. The custom "BourbonWatch/…" agent
# used previously is exactly what the WAF flags, so don't reintroduce it.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{BASE}/",
}
JSON_HEADERS = {**HEADERS, "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest"}

_SESSION = None


def _session():
    """One impersonating session per process, warmed up on the homepage so any
    WAF/session cookies are in place before we hit product or API routes."""
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    if _IMPERSONATE:
        s = _http.Session(impersonate="chrome")
    else:
        s = _http.Session()
    s.headers.update(HEADERS)
    try:
        s.get(f"{BASE}/", timeout=30)
        time.sleep(1.0)
    except Exception as e:
        print(f"[va] homepage warm-up failed ({e}); continuing anyway")
    _SESSION = s
    return s


def _get(s, url, **kw):
    """GET with a single retry on 403/429 after a short back-off."""
    kw.setdefault("timeout", 30)
    r = s.get(url, **kw)
    if r.status_code in (403, 429):
        time.sleep(3)
        r = s.get(url, **kw)
    return r


# ---------------------------------------------------------------------------
# 1. Endpoint discovery — run once on a machine with network access
# ---------------------------------------------------------------------------

_API_PATTERNS = [
    r'["\'](/(?:webapi|api|coveo)/[A-Za-z0-9/_\-{}.]+)["\']',          # relative API paths
    r'["\'](https?://[^"\']*coveo[^"\']*/rest/[^"\']+)["\']',           # Coveo cloud endpoints
    r'["\'](https?://(?:www\.)?abc\.virginia\.gov/[^"\']*(?:api|inventory|availab|search)[^"\']*)["\']',
]


def discover(state, product_hints, session=None):
    """
    Find VA ABC's JSON API endpoints by mining the site's own JavaScript.
    Caches results under state['va_abc'] = {'endpoints': [...], 'products': {...}}
    """
    _ = session  # deliberately ignored — see NETWORK NOTE at top
    s = _session()
    va = state.setdefault("va_abc", {})

    print(f"[va] TLS impersonation: {'ON (curl_cffi)' if _IMPERSONATE else 'OFF'}")
    print("[va] Fetching product page to locate JS bundles ...")
    r = _get(s, f"{BASE}/products/bourbon")
    if r.status_code == 403:
        print("[va] Still 403 with Chrome impersonation. VA ABC may be blocking this IP")
        print("     range, or requiring a JS challenge. Try again in a few minutes; if it")
        print("     persists, discovery must be done by hand via browser dev tools.")
        return
    r.raise_for_status()
    html = r.text

    bundles = set(re.findall(r'src="([^"]+\.js[^"]*)"', html))
    bundles = {b if b.startswith("http") else BASE + b for b in bundles}
    print(f"[va] Found {len(bundles)} JS bundles; scanning for API routes ...")

    candidates = set()
    # The page HTML itself often embeds config (Coveo org/endpoint, API keys)
    for pat in _API_PATTERNS:
        candidates.update(re.findall(pat, html))
    coveo_keys = set(re.findall(r'(?:accessToken|apiKey|coveoToken)["\']?\s*[:=]\s*["\']([A-Za-z0-9\-_.]{20,})', html))

    for b in sorted(bundles)[:20]:
        try:
            js = _get(s, b).text
        except Exception:
            continue
        for pat in _API_PATTERNS:
            candidates.update(re.findall(pat, js))
        coveo_keys.update(re.findall(r'(?:accessToken|apiKey|coveoToken)["\']?\s*[:=]\s*["\']([A-Za-z0-9\-_.]{20,})', js))
        time.sleep(0.5)

    if not candidates:
        print("[va] No API routes found in bundles. Open the site with browser dev tools")
        print("     (Network tab), load a product page, and note the XHR URLs; add them")
        print("     to state.json under va_abc.endpoints manually.")
        return

    print(f"[va] Candidate endpoints ({len(candidates)}):")
    for c in sorted(candidates):
        print(f"     {c}")
    if coveo_keys:
        print(f"[va] Found {len(coveo_keys)} Coveo-style token(s); cached for search calls.")

    # Probe the ones that look like product search endpoints
    working = []
    probes = [c for c in candidates if re.search(r"search|product|inventory|availab|coveo", c, re.I)]
    for c in sorted(probes)[:12]:
        url = c if c.startswith("http") else BASE + c
        url = url.split("{")[0].rstrip("/")
        try:
            pr = _get(s, url, headers=JSON_HEADERS,
                      params={"q": "eagle rare", "query": "eagle rare"}, timeout=20)
            ct = pr.headers.get("content-type", "")
            if pr.status_code == 200 and "json" in ct:
                working.append(c)
                print(f"[va] ✓ responds with JSON: {c}")
            else:
                print(f"[va]   {pr.status_code} {c}")
        except Exception as e:
            print(f"[va]   error {c}: {e}")
        time.sleep(1)

    va["endpoints"] = sorted(candidates)
    va["working_endpoints"] = working
    va["coveo_tokens"] = sorted(coveo_keys)
    va["discovered_at"] = datetime.now().isoformat()
    print("[va] Cached to state.json. If a search endpoint responded, the tracker will")
    print("     resolve product IDs on the next scan. Otherwise inspect the candidates")
    print("     above (or browser dev tools) and set va_abc.search_endpoint /")
    print("     va_abc.inventory_endpoint in state.json — each takes one minute.")
    _ = product_hints  # reserved for future auto-resolution during discovery


# ---------------------------------------------------------------------------
# 2. Inventory tracking with snapshot diffing
# ---------------------------------------------------------------------------

def _abs(ep):
    ep = ep.split("{")[0].rstrip("/")
    return ep if ep.startswith("http") else BASE + ep


def _resolve_products(va, products, s):
    """Map target bottles to VA ABC product IDs via the cached search endpoint."""
    search = va.get("search_endpoint") or next(
        (e for e in va.get("working_endpoints", []) if re.search(r"search|coveo", e, re.I)), None)
    if not search:
        return {}
    resolved = va.setdefault("product_ids", {})
    for p in products:
        if p["name"] in resolved:
            continue
        q = " ".join(p["keywords"])
        try:
            r = _get(s, _abs(search), headers=JSON_HEADERS,
                     params={"q": q, "query": q, "term": q}, timeout=20)
            data = r.json()
        except Exception as e:
            print(f"  [va] search failed for {p['name']}: {e}")
            continue
        hits = _find_products_in_json(data, p)
        if hits:
            resolved[p["name"]] = hits[0]
            print(f"  [va] resolved {p['name']} -> {hits[0]}")
        time.sleep(1)
    return resolved


def _find_products_in_json(obj, prod, path=""):
    """Recursively find dicts that look like product records matching our keywords."""
    hits = []
    if isinstance(obj, dict):
        name_field = next((obj[k] for k in ("name", "productName", "title", "description")
                           if isinstance(obj.get(k), str)), None)
        id_field = next((obj[k] for k in ("productId", "id", "sku", "code", "productCode")
                         if obj.get(k) is not None), None)
        if name_field and id_field is not None:
            t = name_field.lower()
            if all(kw.lower() in t for kw in prod["keywords"]) and \
               not any(x.lower() in t for x in prod.get("exclude_keywords", [])):
                hits.append({"id": str(id_field), "name": name_field})
        for v in obj.values():
            hits.extend(_find_products_in_json(v, prod))
    elif isinstance(obj, list):
        for v in obj:
            hits.extend(_find_products_in_json(v, prod))
    return hits


DEFAULT_POLL_TIMES = ["05:00", "10:00", "14:00"]


def _slot_is_due(va, retailer):
    """
    Only poll at a few fixed local times per day (config: retailer['poll_times'],
    e.g. ["05:00", "10:00", "14:00"]). The scanner may run every 15 min, but VA ABC
    is only contacted once per slot: the first scan at/after a slot time that
    hasn't already been served today.
    """
    now = datetime.now()
    slots = []
    for t in retailer.get("poll_times", DEFAULT_POLL_TIMES):
        try:
            hh, mm = (int(x) for x in t.split(":"))
            slots.append(now.replace(hour=hh, minute=mm, second=0, microsecond=0))
        except ValueError:
            print(f"  [va] ignoring bad poll_times entry: {t!r}")
    due = [s for s in slots if s <= now]
    if not due:
        return False
    latest_slot = max(due)
    last = va.get("last_inventory_check")
    if last and datetime.fromisoformat(last) >= latest_slot:
        return False  # this slot already served
    return True


def check_inventory(retailer, products, rules, session, state):
    """
    Checker entrypoint (type: 'va_abc'). Diffs store-level quantities per target
    product; emits WATCH findings for new/increased stock. Contacts VA ABC only
    at the fixed daily times in retailer['poll_times'] (default 05:00/10:00/14:00).
    """
    _ = session  # ignored — we need our own impersonating session
    va = state.setdefault("va_abc", {})
    findings = []

    if not _slot_is_due(va, retailer):
        return findings
    va["last_inventory_check"] = datetime.now().isoformat()

    inv_ep = va.get("inventory_endpoint") or next(
        (e for e in va.get("working_endpoints", []) if re.search(r"inventor|availab", e, re.I)), None)

    if not va.get("endpoints"):
        print("  [va] Not set up yet — run: python3 bourbon_watch.py --va-discover")
        return findings

    s = _session()
    ids = _resolve_products(va, products, s)

    if not inv_ep or not ids:
        print("  [va] No inventory endpoint/product IDs cached yet; see state.json va_abc notes.")
        return findings

    snapshots = va.setdefault("snapshots", {})
    for prod in products:
        rec = ids.get(prod["name"])
        if not rec:
            continue
        url = _abs(inv_ep) + "/" + rec["id"]
        try:
            r = _get(s, url, headers=JSON_HEADERS, timeout=20)
            data = r.json()
        except Exception as e:
            print(f"  [va] inventory fetch failed for {prod['name']}: {e}")
            continue

        stores = _extract_store_quantities(data)
        prev = snapshots.get(prod["name"], {})
        for store_key, info in stores.items():
            old_qty = prev.get(store_key, {}).get("qty", 0)
            if info["qty"] > old_qty:
                findings.append({
                    "product": prod["name"],
                    "title": f"{rec['name']} — {info['qty']} btl at {info['label']} (+{info['qty']-old_qty})",
                    "price": info.get("price"),
                    "retailer": "Virginia ABC",
                    "url": f"{BASE}/products",
                    "priority": prod.get("priority", False),
                    "tier": "WATCH" if info.get("price") is None else None,
                    "msrp": prod.get("msrp"),
                    "secondary": prod.get("secondary"),
                    "note": "New/increased VA ABC store stock. Verify in-store availability; allocated items may be lottery-only.",
                })
        snapshots[prod["name"]] = stores
        time.sleep(1)

    # classify any priced findings through the normal rules
    from bourbon_watch import classify_deal
    for f in findings:
        if f["tier"] is None:
            f["tier"] = classify_deal(f["price"], {"msrp": f["msrp"], "secondary": f["secondary"]}, rules) or "WATCH"
    return findings


def _extract_store_quantities(obj, out=None):
    """Walk inventory JSON for store-like records: something with a quantity and a store name/number."""
    if out is None:
        out = {}
    if isinstance(obj, dict):
        qty = next((obj[k] for k in ("quantity", "qty", "quantityAvailable", "onHand", "available")
                    if isinstance(obj.get(k), (int, float))), None)
        label = next((str(obj[k]) for k in ("storeName", "name", "address", "storeNumber", "storeId")
                      if obj.get(k) is not None), None)
        price = next((obj[k] for k in ("price", "retailPrice", "currentPrice")
                      if isinstance(obj.get(k), (int, float))), None)
        if qty is not None and label:
            out[label] = {"qty": int(qty), "label": label, "price": price}
        for v in obj.values():
            _extract_store_quantities(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _extract_store_quantities(v, out)
    return out


# ---------------------------------------------------------------------------
# 3. Page fetch helper for the lottery/downloads "watch" pages
# ---------------------------------------------------------------------------

def fetch_page(url, timeout=30):
    """Fetch a VA ABC CMS page through the impersonating session. bourbon_watch.py
    can call this for any abc.virginia.gov 'watch' retailer so those pages don't
    403 either."""
    r = _get(_session(), url, timeout=timeout)
    r.raise_for_status()
    return r.text