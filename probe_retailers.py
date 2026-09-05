#!/usr/bin/env python3
"""
probe_retailers.py — find which candidate shops are Shopify stores and how many
of your target bottles they list. Read-only, gentle (one request per page, 1s
pause). Run from ~/bourbon-watch so it can import bourbon_watch and config.json.

  python3 probe_retailers.py                 # probe the built-in candidate list
  python3 probe_retailers.py somestore.com   # probe one or more extra domains

Output: a ranked table. "shopify" = /products.json responded with a catalog.
"hits" = distinct target bottles found in the catalog (any availability).
"in stock" = of those, how many have an available variant right now.
Then paste-ready config entries for every Shopify shop with >= 1 hit.
"""

import json
import sys
import time

import requests

import bourbon_watch as bw

CANDIDATES = [
    # --- DC / MD (spirits delivery legal in DC; closest to Vienna) ---
    "batch13dc.com", "acebeverage.com", "cellar.com", "calvertwoodley.com",
    "paulswineandspirits.com", "chatsliquors.com", "cairowineandliquor.com",
    "bassins.com", "rodmans.com", "saltandvine.com",
    # --- Kentucky / Southeast ---
    "justinshouseofbourbon.com", "westportwhiskeyandwine.com", "corknbottle.com",
    "bustersliquors.com", "bourboncentral.com", "totalbeverage.com",
    # --- California (most likely to ship) ---
    "bittersandbottles.com", "remedyliquor.com", "nestorliquor.com",
    "oldtowntequila.com", "missionliquor.com", "liquorama.net", "primetimeliquor.com",
    "whwc.com", "klwines.com", "bountyhunterwine.com", "blackwellswines.com",
    "hitimewine.net", "uptownspirits.com", "wallywine.com", "elcerritoliquor.com",
    # --- Northeast ---
    "astorwines.com", "parkaveliquor.com", "winechateau.com", "bottleking.com",
    "warehousewinesandspirits.com", "gothamwines.com",
    # --- Midwest / other shippers ---
    "acespirits.com", "binnys.com", "ozliquor.com", "boozy.ph",
]

HEADERS = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
           "Accept": "application/json"}


def probe(domain, products, max_pages=6):
    base = f"https://{domain}"
    if not domain.startswith("www.") and domain.count(".") == 1:
        pass  # try bare domain first; Shopify usually redirects fine
    s = requests.Session()
    hits, total, pages = {}, 0, 0
    for page in range(1, max_pages + 1):
        try:
            r = s.get(f"{base}/products.json?limit=250&page={page}", headers=HEADERS,
                      timeout=15, allow_redirects=True)
        except Exception as e:
            return {"domain": domain, "shopify": False, "err": str(e)[:60]}
        if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
            if page == 1:
                return {"domain": domain, "shopify": False, "err": f"HTTP {r.status_code}"}
            break
        try:
            items = r.json().get("products", [])
        except Exception:
            return {"domain": domain, "shopify": False, "err": "bad json"}
        if not items:
            break
        pages += 1
        total += len(items)
        for it in items:
            prod = bw.match_product(it.get("title", ""), products)
            if not prod:
                continue
            avail = any(v.get("available") for v in it.get("variants", []))
            prices = [float(v["price"]) for v in it.get("variants", []) if v.get("price")]
            rec = hits.setdefault(prod["name"], {"titles": [], "avail": False, "min": None})
            rec["titles"].append(it.get("title", ""))
            rec["avail"] = rec["avail"] or avail
            if prices:
                rec["min"] = min([p for p in [rec["min"]] + prices if p is not None])
        time.sleep(1)
    return {"domain": domain, "shopify": True, "products": total, "pages": pages, "hits": hits}


def main():
    cfg = bw.load_config()
    products = cfg["products"]
    domains = sys.argv[1:] or CANDIDATES
    results = []
    for d in domains:
        print(f"probing {d} ...", end=" ", flush=True)
        res = probe(d, products)
        if res["shopify"]:
            instock = sum(1 for h in res["hits"].values() if h["avail"])
            print(f"shopify, {res['products']} products, {len(res['hits'])} hits, {instock} in stock")
        else:
            print(f"no ({res.get('err')})")
        results.append(res)

    shop = [r for r in results if r["shopify"]]
    shop.sort(key=lambda r: (-len(r["hits"]), -r["products"]))
    print("\n" + "=" * 78)
    print(f"{'domain':32} {'products':>8} {'hits':>5} {'in stock':>9}")
    print("-" * 78)
    for r in shop:
        instock = sum(1 for h in r["hits"].values() if h["avail"])
        print(f"{r['domain']:32} {r['products']:>8} {len(r['hits']):>5} {instock:>9}")

    print("\nDetail (target bottles seen, lowest listed price, * = in stock now):")
    for r in shop:
        if not r["hits"]:
            continue
        print(f"\n{r['domain']}")
        for name, h in sorted(r["hits"].items()):
            star = "*" if h["avail"] else " "
            price = f"${h['min']:.2f}" if h["min"] else "?"
            print(f"  {star} {name:40} {price:>10}   e.g. {h['titles'][0][:50]}")

    print("\nPaste-ready config entries (vetted:false — check each site, then flip):")
    for r in shop:
        if not r["hits"]:
            continue
        print(json.dumps({"name": r["domain"], "type": "shopify",
                          "url": f"https://{r['domain']}", "max_pages": max(4, r["pages"]),
                          "vetted": False}) + ",")


if __name__ == "__main__":
    main()
