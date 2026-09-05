(function () {
  "use strict";
  var TIER_ORDER = { STEAL: 0, GOOD: 1, FAIR: 2 };
  var TIER_COLOR = { STEAL: "var(--amber)", GOOD: "var(--gold)", FAIR: "var(--neutral)", ABOVE: "var(--ink-faint)" };
  var lastDeals = null;

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function fmt(n) {
    if (n == null || isNaN(n)) return "—";
    return "$" + (n % 1 === 0 ? n.toLocaleString("en-US") : n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
  }
  function fmtPrice(n) {
    if (n == null || isNaN(n)) return "—";
    return "$" + n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function relTime(iso) {
    var s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 60) return "just now";
    var m = Math.round(s / 60);
    if (m < 60) return m + " min ago";
    var h = Math.floor(m / 60);
    if (h < 24) return h + (h === 1 ? " hr " : " hrs ") + (m % 60 ? (m % 60) + " min " : "") + "ago";
    var d = Math.round(h / 24);
    return d + (d === 1 ? " day ago" : " days ago");
  }
  function tierOf(price, msrp, secondary) {
    if (price <= msrp * 1.05 || price <= secondary * 0.65) return "STEAL";
    if (price <= secondary * 0.85) return "GOOD";
    if (price <= secondary) return "FAIR";
    return "ABOVE";
  }

  function renderHeader(deals) {
    var age = Date.now() - new Date(deals.generated_at).getTime();
    var stale = age > 2 * 3600 * 1000;
    var n = deals.listings.length;
    $("scan-meta").innerHTML =
      "Last scan: " + esc(relTime(deals.generated_at)) +
      '<span class="sep">·</span>' +
      n + (n === 1 ? " live listing" : " live listings") +
      (stale ? '<span class="stale-flag">scanner may be asleep</span>' : "");
  }

  function renderListings(deals) {
    var byName = {};
    deals.products.forEach(function (p) { byName[p.name] = p; });
    var chip = $("listing-count");
    chip.textContent = deals.listings.length;
    chip.classList.toggle("zero", deals.listings.length === 0);

    if (!deals.listings.length) {
      $("listings").innerHTML =
        '<div class="empty-state"><strong>Nothing at fair retail right now.</strong><br>The scanner checks every 15 minutes.</div>';
      return;
    }
    var sorted = deals.listings.slice().sort(function (a, b) {
      var t = (TIER_ORDER[a.tier] ?? 9) - (TIER_ORDER[b.tier] ?? 9);
      if (t) return t;
      var pa = byName[a.product] && byName[a.product].priority ? 1 : 0;
      var pb = byName[b.product] && byName[b.product].priority ? 1 : 0;
      if (pa !== pb) return pb - pa;
      return a.price - b.price;
    });
    $("listings").innerHTML = sorted.map(function (l) {
      var prod = byName[l.product] || {};
      return (
        '<article class="card t-' + l.tier.toLowerCase() + '">' +
          '<div class="card-top"><h3>' + esc(l.product) + '</h3>' +
          '<span class="badge badge-' + l.tier.toLowerCase() + '">' + esc(l.tier) + "</span></div>" +
          '<div class="price-row"><span class="price">' + fmtPrice(l.price) + "</span>" +
          '<span class="refs">MSRP ' + fmt(prod.msrp) + " · Secondary " + fmt(prod.secondary) + "</span></div>" +
          '<a class="retailer" href="' + esc(l.url) + '" target="_blank" rel="noopener">' + esc(l.retailer) + " ↗</a>" +
          (l.note ? '<p class="note">' + esc(l.note) + "</p>" : "") +
        "</article>"
      );
    }).join("");
  }

  function renderWatchlist(deals) {
    var rows = deals.products.slice().sort(function (a, b) {
      if (!!a.priority !== !!b.priority) return a.priority ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
    $("watchlist").innerHTML = rows.map(function (p) {
      return (
        '<div class="wl-row"><span class="wl-name">' +
        (p.priority ? '<span class="wl-star" title="Priority bottle">★</span>' : "") +
        esc(p.name) + "</span>" +
        '<span class="wl-nums">' + fmt(p.msrp) + '<span class="slash">/</span>' + fmt(p.secondary) + "</span></div>"
      );
    }).join("");
  }

  function sparkline(series, msrp) {
    var cutoff = Date.now() - 90 * 864e5;
    var pts = series.filter(function (pt) { return new Date(pt.d + "T12:00:00").getTime() >= cutoff; });
    if (!pts.length) return null;
    var W = 320, H = 84, PX = 6, PT = 8, PB = 8;
    var prices = pts.map(function (p) { return p.p; });
    var lo = Math.min.apply(null, prices), hi = Math.max.apply(null, prices);
    var yMin = Math.min(lo, msrp), yMax = Math.max(hi, msrp);
    if (yMax === yMin) yMax = yMin + 1;
    var t0 = new Date(pts[0].d + "T12:00:00").getTime();
    var t1 = new Date(pts[pts.length - 1].d + "T12:00:00").getTime();
    if (t1 === t0) t1 = t0 + 1;
    function x(t) { return pts.length === 1 ? W - PX : PX + ((t - t0) / (t1 - t0)) * (W - 2 * PX); }
    function y(v) { return PT + (1 - (v - yMin) / (yMax - yMin)) * (H - PT - PB); }
    var path = pts.length > 1 ? pts.map(function (p, i) {
      return (i ? "L" : "M") + x(new Date(p.d + "T12:00:00").getTime()).toFixed(1) + " " + y(p.p).toFixed(1);
    }).join(" ") : "";
    var my = y(msrp).toFixed(1);
    var last = pts[pts.length - 1];
    return {
      lo: lo, hi: hi, last: last,
      svg:
        '<svg viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="none" role="img" aria-label="90-day price history">' +
        '<line x1="' + PX + '" y1="' + my + '" x2="' + (W - PX) + '" y2="' + my + '" stroke="var(--ink-faint)" stroke-width="1" stroke-dasharray="3 4"></line>' +
        (path ? '<path d="' + path + '" fill="none" stroke="var(--gold)" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"></path>' : "") +
        '<circle cx="' + x(new Date(last.d + "T12:00:00").getTime()).toFixed(1) + '" cy="' + y(last.p).toFixed(1) + '" r="3.5" fill="LASTCOLOR"></circle>' +
        "</svg>"
    };
  }

  function renderHistory(deals, history) {
    var byName = {};
    deals.products.forEach(function (p) { byName[p.name] = p; });
    var cards = [];
    Object.keys(history).sort().forEach(function (name) {
      var prod = byName[name];
      if (!prod || !Array.isArray(history[name])) return;
      var sp = sparkline(history[name], prod.msrp);
      if (!sp) return;
      var tier = tierOf(sp.last.p, prod.msrp, prod.secondary);
      var color = TIER_COLOR[tier];
      cards.push(
        '<div class="spark-card">' +
          '<div class="spark-head"><h3>' + esc(name) + '</h3>' +
          '<span class="spark-last" style="color:' + color + '"><span class="dot" style="background:' + color + '"></span>' + fmtPrice(sp.last.p) + "</span></div>" +
          sp.svg.replace("LASTCOLOR", color) +
          '<div class="spark-range"><span>low ' + fmtPrice(sp.lo) + "</span><span>MSRP " + fmt(prod.msrp) + " ⋯</span><span>high " + fmtPrice(sp.hi) + "</span></div>" +
        "</div>"
      );
    });
    $("history-section").hidden = cards.length === 0;
    $("history").innerHTML = cards.join("");
  }

  function showError() {
    $("loading").hidden = true;
    if (!lastDeals) {
      $("listings-section").hidden = true;
      $("watchlist-section").hidden = true;
      $("history-section").hidden = true;
      $("error-panel").hidden = false;
    }
  }

  function render(deals, history) {
    lastDeals = deals;
    $("loading").hidden = true;
    $("error-panel").hidden = true;
    renderHeader(deals);
    renderListings(deals);
    renderWatchlist(deals);
    renderHistory(deals, history);
    $("listings-section").hidden = false;
    $("watchlist-section").hidden = false;
  }

  function getJSON(url) {
    return fetch(url, { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error(url + " → " + r.status);
      return r.json();
    });
  }

  function load() {
    Promise.all([getJSON("./data/deals.json"), getJSON("./data/history.json")])
      .then(function (res) { render(res[0], res[1]); })
      .catch(function () { showError(); });
  }

  load();
  setInterval(load, 5 * 60 * 1000);
  setInterval(function () { if (lastDeals) renderHeader(lastDeals); }, 60 * 1000);
})();
