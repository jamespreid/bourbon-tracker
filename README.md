# 🥃 Bourbon Watch

A self-hosted monitor that scans legitimate online bourbon retailers for your target bottles (Michter's 10, Buffalo Trace portfolio, etc.), then emails **instant alerts** for priority bottles at or below your alert price and a **daily digest** of everything decent it found. Add your whole family to the recipients list — everyone gets the same emails.

## How it works

- Most specialty bourbon retailers run on **Shopify**, which exposes a public JSON catalog (`/products.json`). The script reads live titles, prices, and in-stock status directly from that — far more reliable than HTML scraping.
- Each product has three price levels:
  - `msrp` — informational, shown in emails
  - `alert_price` — at or below this on a `priority` bottle → **instant email**
  - `digest_price` — at or below this → included in the **daily digest**
- Deduplication: the same bottle/retailer/price only triggers once per 24 hours, so you won't get spammed.

## Setup (15 minutes)

1. **Pick an always-on machine.** A Raspberry Pi, an old laptop, or a ~$5/month cloud VM (DigitalOcean, Hetzner, AWS Lightsail).

2. **Install dependencies:**
   ```bash
   pip3 install requests beautifulsoup4
   ```

3. **Configure:**
   ```bash
   cp config.example.json config.json
   nano config.json
   ```
   - Add every family member's email to `recipients`.
   - Set up SMTP. Easiest path: a Gmail account + [App Password](https://myaccount.google.com/apppasswords) (requires 2FA enabled; a normal password will NOT work).
   - Tune `alert_price` / `digest_price` per bottle to your definition of a "good deal."

4. **Test email delivery:**
   ```bash
   python3 bourbon_watch.py --test-email
   ```

5. **Run one manual scan:**
   ```bash
   python3 bourbon_watch.py
   ```

6. **Schedule with cron** (`crontab -e`):
   ```cron
   # Scan every 30 minutes
   */30 * * * * cd /path/to/bourbon_watch && python3 bourbon_watch.py >> watch.log 2>&1

   # Daily digest at 8:00 AM
   0 8 * * * cd /path/to/bourbon_watch && python3 bourbon_watch.py --digest >> watch.log 2>&1
   ```

## Sharing with family

Just add their addresses to `recipients` in `config.json`. One person hosts; everyone receives. If a family member wants their own thresholds or bottles, they can run their own copy with their own `config.json` — the script has no shared server component.

## Adding retailers

- **Shopify stores** (most bourbon specialty shops): add `{"name": "...", "type": "shopify", "url": "https://store.com"}`. To check whether a store is Shopify, visit `https://store.com/products.json` — if you see JSON, it works.
- **Anything else**: add with `"type": "html"` and the exact product or collection page URL. This mode is a heuristic (it looks for your keywords near a price) — treat its results as "go look," not gospel.
- Your **local/state stores**: state-controlled markets (Ohio OHLQ, Virginia ABC, Pennsylvania FWGS, NC ABC) frequently list allocated bottles at true MSRP. Many have inventory pages you can add as `html` retailers with your local store's URL.

## Buying safely — see RETAILERS.md

The full vetting checklist, tiered store directory (state ABCs, California independents, distillery-direct), scam-pattern reference, and secondary-pricing sources now live in **RETAILERS.md**. Read it before adding any retailer, and note the script refuses to scan stores not marked `"vetted": true`.


## Files

| File | Purpose |
|---|---|
| `bourbon_watch.py` | The monitor |
| `config.json` | Your settings (create from `config.example.json`) |
| `state.json` | Auto-generated dedupe/digest state |
| `watch.log` | Cron output log |

## Website (GitHub → Netlify)

The repo doubles as a live price board. `site/index.html` is a static page that reads `site/data/deals.json`; every scan rewrites that JSON (via the `site_export` path in config), so pushing the repo republishes current prices.

**One-time setup:**
1. Create a GitHub repo and push this whole folder (add `config.json`, `state.json`, and `watch.log` to `.gitignore` — never commit SMTP credentials).
2. In Netlify: *Add new site → Import from GitHub*, pick the repo. `netlify.toml` already sets the publish directory to `site/`. Deploy — done.
3. Share the Netlify URL with the family. Search, tier filters (Steal/Good/Fair), and lowest-price-per-bottle are built in.

**Auto-publish after each scan** — extend the cron entry:
```cron
*/30 * * * * cd /path/to/bourbon_watch && python3 bourbon_watch.py >> watch.log 2>&1 && git add site/data/deals.json && git commit -m "price update" --quiet && git push --quiet
```
(Use a GitHub personal access token or SSH key on the host machine. Netlify auto-deploys on every push — updates appear on the site within ~1 minute.)

## VA ABC Tracker (self-hosted VABourbon replacement)

`va_abc.py` polls Virginia ABC's own website for store-level inventory of your target bottles and alerts on new deliveries/quantity increases — the same public data community trackers use, with no external dependency.

**Setup (one-time, on the Mac mini):**
```bash
python3 bourbon_watch.py --va-discover
```
This mines VA ABC's JavaScript for their internal API routes, probes them, and caches working endpoints into `state.json`. If discovery can't auto-confirm an endpoint, it prints candidates; open abc.virginia.gov in a browser with dev tools (Network tab), load any product page, and copy the XHR URLs it calls into `state.json` under `va_abc.search_endpoint` / `va_abc.inventory_endpoint` — a one-minute job. After that, every scan diffs store quantities automatically.

**What it emits:** WATCH/tiered alerts like "Eagle Rare 12 — 12 btl at Store #218 Fairfax (+12)". The lottery announcements page is also watched (plain page-watch) so lottery openings for target bottles trigger alerts.

**Politeness rules (do not lower):** inventory checks are capped at once per hour regardless of scan cadence; discovery sleeps between requests; identify with an honest User-Agent. This is a public state site — being gentle keeps the data flowing for everyone.

**Important reality check:** VA ABC runs true allocation via lotteries — the rarest bottles (Michter's 10, BTAC) will mostly surface on the lottery page, not as store inventory. The inventory diff shines for the semi-allocated tier (Eagle Rare 12, Weller 107/12, E.H. Taylor, Blanton's) where bottles do land on shelves in drops.
