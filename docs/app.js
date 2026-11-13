/* SHADOW MEME paper dashboard (Telegram Mini App). Phase 9.
   Data: full snapshot (dashboard-data branch, raw.githubusercontent.com) + summary in ?s= (always in the bot's link).
   Everything is rendered with text nodes (token names are attacker-controlled). Never invents data. */
(function () {
  "use strict";
  var DEFAULT_REPO = "ahmadrahmani147370-hype/shadow-meme-bot";
  var Q = new URLSearchParams(location.search);
  var tg = null;
  var UNIS = [["new", "NEW"], ["mcap", "MCAP"], ["volume", "TOP VOLUME"]];
  var state = { snap: null, sum: null, notes: [], source: "", uniFilter: "all" };

  // ---------------------------------------------------------------- helpers
  function h(tag, props) {
    var e = document.createElement(tag);
    if (props) for (var k in props) {
      if (k === "class") e.className = props[k];
      else if (k === "text") e.textContent = props[k];
      else if (k.slice(0, 2) === "on") e.addEventListener(k.slice(2), props[k]);
      else e.setAttribute(k, props[k]);
    }
    for (var i = 2; i < arguments.length; i++) {
      var c = arguments[i];
      if (c === null || c === undefined || c === false) continue;
      if (Array.isArray(c)) c.forEach(function (x) { if (x) e.appendChild(typeof x === "string" ? document.createTextNode(x) : x); });
      else e.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
    }
    return e;
  }
  function num(x) { return typeof x === "number" && isFinite(x); }
  function usd(x, sign) {
    if (!num(x)) return "—";
    var s = Math.abs(x) >= 0.01 || x === 0 ? Math.abs(x).toFixed(2) : Math.abs(x).toPrecision(3);
    return (x < 0 ? "-" : (sign && x > 0 ? "+" : "")) + "$" + s.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }
  function pct(x, sign) { return num(x) ? ((sign !== false && x > 0 ? "+" : "") + x.toFixed(1) + "%") : "—"; }
  function px(x) { return num(x) ? (x >= 1 ? x.toFixed(4) : x.toPrecision(4)) : "—"; }
  function cls(x) { return num(x) ? (x > 0 ? "good" : x < 0 ? "bad" : "") : ""; }
  function n(x) { return num(x) ? String(x) : "—"; }
  function tzName() { return (state.snap && state.snap.tz) || "Asia/Karachi"; }
  function tzLabel() { return tzName() === "Asia/Karachi" ? "PKT" : tzName(); }
  function fmtTime(ts, withDate) {
    if (!num(ts) || ts <= 0) return "—";
    try {
      var o = { timeZone: tzName(), hour: "2-digit", minute: "2-digit", hour12: false };
      if (withDate) { o.day = "2-digit"; o.month = "short"; }
      return new Intl.DateTimeFormat("en-GB", o).format(new Date(ts * 1000));
    } catch (e) { return new Date(ts * 1000).toISOString().slice(5, 16).replace("T", " ") + " UTC"; }
  }
  function ago(ts) {
    if (!num(ts)) return "—";
    var s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 90) return Math.round(s) + "s ago";
    if (s < 5400) return Math.round(s / 60) + " min ago";
    if (s < 172800) return (s / 3600).toFixed(1) + " h ago";
    return Math.round(s / 86400) + " days ago";
  }
  function dur(s) { return num(s) ? (s >= 3600 ? Math.floor(s / 3600) + "h " + Math.floor(s % 3600 / 60) + "m" : Math.floor(s / 60) + " min") : "—"; }
  function kv(k, v, c) { return h("div", { class: "kv" }, h("div", { class: "k", text: k }), h("div", { class: "v " + (c || ""), text: v })); }
  function empty(t) { return h("div", { class: "empty", text: t }); }
  function short(m) { return typeof m === "string" && m.length > 12 ? m.slice(0, 4) + "…" + m.slice(-4) : (m || "?"); }
  function sym(x) { return (x && String(x).trim()) || "?"; }
  function uniLabel(u) { for (var i = 0; i < UNIS.length; i++) if (UNIS[i][0] === u) return UNIS[i][1]; return String(u || "?").toUpperCase(); }
  var MINT_RE = /^[1-9A-HJ-NP-Za-km-z]{32,48}$/;
  function dexLink(mint, label) {
    if (!MINT_RE.test(mint || "")) return h("span", { text: label });
    var url = "https://dexscreener.com/solana/" + mint;
    return h("a", { href: url, target: "_blank", rel: "noopener", text: label, onclick: function (ev) {
      if (tg && tg.openLink) { ev.preventDefault(); tg.openLink(url); } } });
  }
  function body(id) { var b = document.querySelector("#" + id + " .body"); b.textContent = ""; return b; }

  // ---------------------------------------------------------------- data
  function decodeSummary(s) {
    if (!s || s.length > 4000) return null;
    try {
      var b = s.replace(/-/g, "+").replace(/_/g, "/"); while (b.length % 4) b += "=";
      var bin = atob(b), bytes = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      var d = JSON.parse(new TextDecoder().decode(bytes));
      return d && d.v === 1 && num(d.t) ? d : null;
    } catch (e) { return null; }
  }
  function repo() {
    var r = Q.get("repo");
    if (r && /^[\w.-]+\/[\w.-]+$/.test(r)) return r;
    var m = location.hostname.match(/^([\w-]+)\.github\.io$/), seg = location.pathname.split("/").filter(Boolean)[0];
    return m && seg ? m[1] + "/" + seg : DEFAULT_REPO;
  }
  function branch() { var b = Q.get("branch"); return b && /^[\w./-]+$/.test(b) ? b : "dashboard-data"; }
  function validSnap(d) { return d && d.schema === 1 && d.paper_only === true && d.app === "SHADOW MEME" && d.account; }
  function getJSON(url) {
    return fetch(url, { cache: "no-store" }).then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); });
  }
  function loadSnapshot() {
    var d = Q.get("data");
    if (d) {
      if (/^https:\/\//.test(d) || /^[\w./-]+$/.test(d)) return getJSON(d).then(function (x) { state.source = "data=" + d; return x; });
      return Promise.reject(new Error("bad data url"));
    }
    var R = repo(), B = branch();
    var raw = "https://raw.githubusercontent.com/" + R + "/";
    return getJSON("https://api.github.com/repos/" + R + "/git/ref/heads/" + encodeURIComponent(B))
      .then(function (ref) {                       // exact commit = no CDN staleness
        var sha = ref && ref.object && ref.object.sha;
        if (!/^[0-9a-f]{40}$/.test(sha || "")) throw new Error("no ref");
        return getJSON(raw + sha + "/snapshot.json").then(function (x) { state.source = R + "@" + B + " (" + sha.slice(0, 7) + ")"; return x; });
      })
      .catch(function (e) {
        if (String(e.message).indexOf("404") >= 0) throw e;          // branch does not exist yet
        return getJSON(raw + B + "/snapshot.json").then(function (x) { state.source = R + "@" + B; return x; });
      });
  }
  function load() {
    state.notes = [];
    state.sum = decodeSummary(Q.get("s"));
    document.getElementById("updated").textContent = "loading…";
    return loadSnapshot().then(function (snap) {
      if (!validSnap(snap)) throw new Error("invalid snapshot");
      state.snap = snap;
    }).catch(function () { state.snap = null; }).then(function () {
      var s = state.snap, m = state.sum;
      if (s && m && m.i && s.instance && m.i !== s.instance) {
        state.notes.push(["warn", "The saved full data belongs to a different bot install, so it is hidden. Showing the summary from your bot's link only."]);
        state.snap = null; s = null;
      }
      if (s && m && m.t - s.generated_ts > 20 * 60) {
        state.notes.push(["warn", "Full data is from " + fmtTime(s.generated_ts, true) + " " + tzLabel() + " (" + ago(s.generated_ts) +
          "). Headline numbers below come from the newer link summary. Check GITHUB_TOKEN if this stays old."]);
      }
      if (!s && m) state.notes.push(["", "Summary only (from the bot's link). For positions, history, signals and charts set GITHUB_TOKEN on the phone (see README) - the bot then uploads the full data every few minutes."]);
      if (!s && !m) state.notes.push(["warn", "No data yet. Open this dashboard from the bot (/dashboard or the menu button). If you just started the bot, wait a few minutes."]);
      render();
    });
  }

  // ---------------------------------------------------------------- render
  function render() {
    var s = state.snap, m = state.sum;
    var useSum = m && (!s || m.t - s.generated_ts > 20 * 60);
    var st = useSum ? ({ r: "running", p: "paused", o: "offline" })[m.st] || "offline" : (s ? s.status.state : "offline");
    var ts = useSum ? m.t : (s ? s.generated_ts : null);
    var stale = ts && Date.now() / 1000 - ts > 20 * 60;
    var pill = document.getElementById("state");
    pill.className = "pill " + (stale ? "offline" : st);
    pill.textContent = stale ? "⚠ NO UPDATE " + ago(ts).replace(" ago", "") : st === "running" ? "▶ RUNNING" : st === "paused" ? "⏸ PAUSED" : (s || m ? "SNAPSHOT" : "NO DATA");
    document.getElementById("updated").textContent = ts ? "updated " + fmtTime(ts, true) + " " + tzLabel() + " · " + ago(ts) : "no data";
    var nb = document.getElementById("notices"); nb.textContent = "";
    state.notes.forEach(function (x) { nb.appendChild(h("div", { class: "notice " + x[0], text: x[1] })); });
    if (stale && st !== "offline") nb.appendChild(h("div", { class: "notice warn", text: "Last update " + ago(ts) + ": the bot may be stopped, offline, or (with GITHUB_TOKEN) unable to upload. Open the dashboard from the bot again for fresh numbers." }));
    if (st === "paused") nb.appendChild(h("div", { class: "notice warn", text: "Bot is PAUSED: no new paper trades or signal alerts. Scanning and open-position exits continue. /resume in Telegram." }));
    renderAccount(s, useSum ? m : null);
    renderPositions(s); renderHistory(s); renderSignals(s, m); renderJudge(s, m); renderStrategies(s); renderScanner(s);
    document.getElementById("source").textContent = "Data: " + (s ? state.source : "—") + (m ? " · link summary " + fmtTime(m.t, true) + " " + tzLabel() : "") +
      (s ? " · v" + s.version : (m && m.ver ? " · v" + m.ver : ""));
  }

  function renderAccount(s, m) {
    var b = body("account");
    if (!s && !m) { b.appendChild(empty("No paper account data yet.")); return; }
    var a = s ? s.account : null;
    var eq = m ? m.eq : a.equity, start = m ? m.sb : a.start_balance, cash = m ? m.c : a.cash;
    var ret = num(eq) && num(start) && start ? (eq - start) / start * 100 : null;
    b.appendChild(h("div", { class: "big" }, usd(eq), h("small", { class: cls(ret), text: pct(ret) })));
    b.appendChild(h("div", { class: "meta", text: "Equity (cash + open positions) · start " + usd(start) + " · PAPER ONLY" }));
    var closedN = m ? m.cl : a.closed.n, wins = m ? m.w : a.closed.wins;
    b.appendChild(h("div", { class: "grid" },
      kv("Cash", usd(cash)),
      kv("Realized P&L", usd(m ? m.rp : a.realized_pnl, true), cls(m ? m.rp : a.realized_pnl)),
      kv("Unrealized P&L", usd(m ? m.up : a.unrealized_pnl, true), cls(m ? m.up : a.unrealized_pnl)),
      kv("Open", n(m ? m.o : a.n_open) + (a ? " / " + a.max_open : "")),
      kv("Trades today", n(m ? m.td : a.trades_today) + (a ? " / " + a.daily_target : "")),
      kv("Closed · win rate", n(closedN) + " · " + (closedN ? Math.round(wins / closedN * 100) + "%" : "—")),
      a ? kv("Fees paid", usd(a.fees_paid)) : null,
      a ? kv("Trade size", usd(a.trade_size)) : null));
    var pts = s && s.equity_curve && s.equity_curve.length ? s.equity_curve.map(function (p) { return [p[0], p[1]]; })
      : (m && m.ec && m.ec.length ? m.ec.map(function (c, i) { return [i, c / 100]; }) : []);
    b.appendChild(h("h3", { text: "Equity curve" }));
    if (pts.length < 2) b.appendChild(empty("Not enough equity points yet (one is saved every few minutes while trades are open)."));
    else b.appendChild(chart(pts, start, !(s && s.equity_curve && s.equity_curve.length)));
  }

  function chart(pts, base, indexX) {
    var NS = "http://www.w3.org/2000/svg", W = 340, H = 150, P = 22;
    var xs = pts.map(function (p) { return p[0]; }), ys = pts.map(function (p) { return p[1]; }).filter(num);
    if (num(base)) ys.push(base);
    var x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs), y0 = Math.min.apply(null, ys), y1 = Math.max.apply(null, ys);
    if (y1 - y0 < 0.01) { y0 -= 0.5; y1 += 0.5; }
    function X(v) { return P + (x1 > x0 ? (v - x0) / (x1 - x0) : 0.5) * (W - 2 * P); }
    function Y(v) { return H - P + 6 - (v - y0) / (y1 - y0) * (H - 2 * P); }
    var svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 " + W + " " + H); svg.setAttribute("class", "chart"); svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Paper equity curve");
    function el(t, a, txt) { var e = document.createElementNS(NS, t); for (var k in a) e.setAttribute(k, a[k]); if (txt) e.textContent = txt; svg.appendChild(e); return e; }
    if (num(base)) el("line", { x1: P, x2: W - P, y1: Y(base), y2: Y(base), stroke: "#8b949e", "stroke-dasharray": "3 4", "stroke-width": 1 });
    var last = pts[pts.length - 1][1], up = !num(base) || last >= base;
    var d = pts.filter(function (p) { return num(p[1]); }).map(function (p, i) { return (i ? "L" : "M") + X(p[0]).toFixed(1) + " " + Y(p[1]).toFixed(1); }).join(" ");
    el("path", { d: d, fill: "none", stroke: up ? "#22c55e" : "#ef4444", "stroke-width": 2, "stroke-linejoin": "round" });
    el("text", { x: 4, y: 12 }, usd(y1)); el("text", { x: 4, y: H - 4 }, usd(y0));
    if (!indexX) { el("text", { x: P + 40, y: H - 4, "text-anchor": "start" }, fmtTime(x0, true)); el("text", { x: W - 4, y: H - 4, "text-anchor": "end" }, fmtTime(x1, true)); }
    return svg;
  }

  function needFull(b, what) { b.appendChild(empty(what + " needs the full data (GITHUB_TOKEN). Summary mode shows only the headline numbers.")); }

  function renderPositions(s) {
    var b = body("positions");
    if (!s) return needFull(b, "Open positions");
    if (!s.positions.length) return b.appendChild(empty("No open paper positions."));
    var L = h("div", { class: "list" });
    s.positions.forEach(function (p) {
      L.appendChild(h("div", { class: "item" },
        h("div", { class: "row" }, h("span", { class: "t" }, dexLink(p.mint, sym(p.symbol)), " ", h("span", { class: "tag", text: uniLabel(p.universe) })),
          h("span", { class: cls(p.pnl_usd), text: usd(p.pnl_usd, true) + " (" + pct(p.pnl_pct) + ")" })),
        h("div", { class: "meta", text: (p.strategy || "") + " · #" + p.id + " · " + usd(p.size_usd) + " @ " + px(p.entry_price) + " → " + px(p.last_price) +
          " · SL " + px(p.sl) + " · TP " + px(p.tp) + " · opened " + fmtTime(p.open_ts, true) + " " + tzLabel() })));
    });
    b.appendChild(L);
  }

  function renderHistory(s) {
    var b = body("history");
    if (!s) return needFull(b, "Trade history");
    if (!s.history.length) return b.appendChild(empty("No closed paper trades yet."));
    var L = h("div", { class: "list" });
    s.history.forEach(function (p) {
      L.appendChild(h("div", { class: "item" },
        h("div", { class: "row" }, h("span", { class: "t" }, dexLink(p.mint, sym(p.symbol)), " ", h("span", { class: "tag", text: uniLabel(p.universe) })),
          h("span", { class: cls(p.pnl_usd), text: usd(p.pnl_usd, true) + " (" + pct(p.pnl_pct) + ")" })),
        h("div", { class: "meta", text: (p.exit_reason || "?") + " · " + (p.strategy || "") + " · " + px(p.entry_price) + " → " + px(p.exit_price) +
          " · " + fmtTime(p.open_ts, true) + " → " + fmtTime(p.exit_ts) + " " + tzLabel() })));
    });
    b.appendChild(L);
  }

  function renderSignals(s, m) {
    var b = body("signals");
    var today = s ? s.signals.today : null, day = s ? s.signals.last24h : null;
    var g = h("div", { class: "uni" });
    UNIS.forEach(function (u) {
      var t = today ? today[u[0]] || {} : null, d = day ? day[u[0]] || {} : null, ms = m && m.sg ? m.sg[u[0]] : null;
      var bw = t ? t.BUY_WATCH || 0 : (ms ? ms[0] : null), se = t ? t.SELL || 0 : (ms ? ms[1] : null), rj = t ? t.REJECT || 0 : (ms ? ms[2] : null);
      g.appendChild(h("div", { class: "kv" }, h("div", { class: "k", text: u[1] }),
        h("div", { class: "v", text: (s || m) ? "🟢 " + n(bw) + "  🔴 " + n(se) : "—" }),
        h("div", { class: "meta", text: (s || m) ? "reject " + n(rj) + (t ? " · hold " + (t.HOLD || 0) : "") + (d ? " · 24h BW " + (d.BUY_WATCH || 0) : "") : "" })));
    });
    b.appendChild(h("div", { class: "meta", text: "Today (" + tzLabel() + "): 🟢 BUY WATCH · 🔴 SELL" }));
    b.appendChild(g);
    if (!s) return needFull(b, "The signal list");
    var chips = h("div", { class: "chips" });
    [["all", "All"]].concat(UNIS).forEach(function (u) {
      chips.appendChild(h("button", { type: "button", class: state.uniFilter === u[0] ? "on" : "", text: u[1],
        onclick: function () { state.uniFilter = u[0]; renderSignals(state.snap, state.sum); } }));
    });
    b.appendChild(chips);
    var rows = s.signals.recent.filter(function (r) { return state.uniFilter === "all" || r.universe === state.uniFilter; });
    if (!rows.length) return b.appendChild(empty("No BUY WATCH / SELL signals" + (state.uniFilter === "all" ? " yet." : " in this universe yet.")));
    var L = h("div", { class: "list" });
    rows.forEach(function (r) {
      var bw = r.type === "BUY_WATCH";
      L.appendChild(h("div", { class: "item" },
        h("div", { class: "row" }, h("span", { class: "t" }, h("span", { class: "tag " + (bw ? "bw" : "sell"), text: bw ? "BUY WATCH" : r.type }), dexLink(r.mint, sym(r.symbol))),
          h("span", { class: "muted", text: fmtTime(r.ts) })),
        h("div", { class: "meta" }, h("span", { class: "tag", text: uniLabel(r.universe) }),
          r.judge ? h("span", { class: "tag " + (r.judge === "APPROVE" ? "approve" : "veto"), text: r.judge + (num(r.judge_conf) ? " " + r.judge_conf.toFixed(2) : "") }) : null,
          (r.strategy || "") + (bw && num(r.score) ? " · score " + Math.round(r.score) : "") + (r.reason ? " · " + r.reason : "") +
          (num(r.pnl_pct) ? " · result " + (r.outcome || "") + " " + pct(r.pnl_pct) : ""))));
    });
    b.appendChild(L);
  }

  function jblock(t, x) {
    return h("div", { class: "kv" }, h("div", { class: "k", text: t }), h("div", { class: "v", text: n(x.n) }),
      h("div", { class: "meta", text: "closed " + n(x.closed) + " · win " + (num(x.win_rate) ? Math.round(x.win_rate * 100) + "%" : "—") +
        " · avg " + pct(x.avg_pnl_pct) + " · net " + pct(x.avg_net_pct) }));
  }
  function renderJudge(s, m) {
    var b = body("judge");
    if (!s && !m) return b.appendChild(empty("No judge decisions yet."));
    if (!s) { b.appendChild(h("div", { class: "grid" }, kv("Approved today", n(m.ja), "good"), kv("Vetoed today", n(m.jv), "bad"))); return needFull(b, "Judge accuracy"); }
    var j = s.judge;
    if (!j.all.approved.n && !j.all.vetoed.n) return b.appendChild(empty("No judge decisions yet (the judge reviews every BUY WATCH)."));
    b.appendChild(h("div", { class: "grid" }, jblock("Approved (all)", j.all.approved), jblock("Vetoed (all)", j.all.vetoed),
      kv("Veto precision", num(j.all.veto_precision) ? Math.round(j.all.veto_precision * 100) + "%" : "—"),
      kv("Edge approve − veto", pct(j.all.edge_pct), cls(j.all.edge_pct)),
      kv("Today", "✅ " + j.today.approved.n + " · 🚫 " + j.today.vetoed.n)));
    b.appendChild(h("div", { class: "meta", text: "Veto precision = vetoed watches that really lost. Results are virtual outcomes of the watch levels, before fees." }));
    if (j.by_reason.length) {
      b.appendChild(h("h3", { text: "Top veto reasons" }));
      var t = h("table", null, h("tr", null, h("th", { text: "reason" }), h("th", { text: "vetoes" }), h("th", { text: "closed" }), h("th", { text: "right" })));
      j.by_reason.forEach(function (r) { t.appendChild(h("tr", null, h("td", { text: r.code }), h("td", { text: n(r.n) }), h("td", { text: n(r.closed) }),
        h("td", { text: num(r.correct_rate) ? Math.round(r.correct_rate * 100) + "%" : "—" }))); });
      b.appendChild(t);
    }
  }

  function metric(x) {
    if (!x) return "—";
    if (x.test) return "test " + pct(x.test.score) + " on " + n(x.test.n) + (x.train ? " (train " + pct(x.train.score) + " on " + n(x.train.n) + ")" : "");
    return (num(x.version) ? "v" + x.version + " " : "") + pct(x.score) + " on " + n(x.n);
  }
  function renderStrategies(s) {
    var b = body("strategies");
    if (!s) return needFull(b, "Strategy versions and updates");
    var names = Object.keys(s.strategies);
    if (!names.length) b.appendChild(empty("No strategies loaded."));
    var L = h("div", { class: "list" });
    names.forEach(function (k) {
      var x = s.strategies[k];
      var t = h("table", null, h("tr", null, h("th", { text: "universe" }), h("th", { text: "score≥" }), h("th", { text: "SL" }), h("th", { text: "TP" }), h("th", { text: "time" })));
      Object.keys(x.params).forEach(function (u) { var p = x.params[u];
        t.appendChild(h("tr", null, h("td", { text: uniLabel(u) }), h("td", { text: n(p.min_score) }), h("td", { text: n(num(p.sl_pct) ? p.sl_pct : p.max_sl_pct) + "%" }),
          h("td", { text: n(p.tp_pct) + "%" }), h("td", { text: n(p.time_stop_min) + "m" }))); });
      L.appendChild(h("div", { class: "item" },
        h("div", { class: "row" }, h("span", { class: "t", text: k + " v" + x.version }), h("span", { class: "muted", text: x.versions + " versions" })),
        h("div", { class: "meta", text: "paper " + x.paper.n + " trades " + usd(x.paper.pnl_usd, true) + " · watches closed " + x.watches_closed +
          " (" + x.watch_wins + " up, avg " + pct(x.watch_avg_pct) + " before fees)" }), t));
    });
    var jp = s.judge_params;
    L.appendChild(h("div", { class: "item" }, h("div", { class: "row" }, h("span", { class: "t", text: "judge v" + n(jp.version) })),
      h("div", { class: "meta", text: Object.keys(jp.params).map(function (k) { return k + " " + jp.params[k]; }).join(" · ") || "—" })));
    b.appendChild(L);
    b.appendChild(h("h3", { text: "strategy_update history" }));
    if (!s.updates.length) return b.appendChild(empty("No self-learning changes yet (needs enough closed watches; every change also arrives in Telegram)."));
    var U = h("div", { class: "list" });
    s.updates.forEach(function (u) {
      var ch = Object.keys(u.changes).map(function (k) { return k + " " + u.changes[k][0] + " → " + u.changes[k][1]; }).join(", ") || "—";
      U.appendChild(h("div", { class: "item" },
        h("div", { class: "row" }, h("span", { class: "t", text: u.kind.replace(/_/g, " ") + " · " + u.target + " v" + n(u.from_version) + " → v" + n(u.to_version) }),
          h("span", { class: "muted", text: fmtTime(u.ts, true) })),
        h("div", { class: "meta", text: "Changed: " + ch }), h("div", { class: "meta", text: "Why: " + (u.reason || "—") }),
        h("div", { class: "meta", text: "Before: " + metric(u.metrics.before) + " · After: " + metric(u.metrics.after) })));
    });
    b.appendChild(U);
  }

  function renderScanner(s) {
    var b = body("scanner");
    if (!s) return needFull(b, "Scanner health");
    var c = s.scanner, stale = Date.now() / 1000 - s.generated_ts > 20 * 60;
    if (!c.live) b.appendChild(h("div", { class: "notice warn", text: "Snapshot made from the database while the bot was not running." }));
    if (c.live && stale) b.appendChild(h("div", { class: "notice warn", text: "No upload for " + ago(s.generated_ts).replace(" ago", "") + ": the bot may be stopped or offline." }));
    var lastAge = num(c.last_token_ts) ? s.generated_ts - c.last_token_ts : null;
    b.appendChild(h("div", { class: "grid" },
      kv("Mode", c.mode || "—"), kv("Uptime", dur(c.uptime_s)),
      kv("New tokens 1h / 24h", n(c.tokens_1h) + " / " + n(c.tokens_24h)),
      kv("Last new token", num(lastAge) ? dur(lastAge) + " before snapshot" : "—", num(lastAge) && lastAge > 900 ? "warn" : ""),
      kv("Tracked new / mcap / vol", n(c.tracked_new) + " / " + n(c.tracked_mcap) + " / " + n(c.tracked_volume)),
      kv("Safety checked", n(c.safety_checked)), kv("Recent scanner errors", n(c.errors_recent), c.errors_recent > 10 ? "warn" : ""),
      kv("Telegram", c.telegram || "—"), kv("Tokens in DB", n(c.seen_in_db))));
  }

  // ---------------------------------------------------------------- Telegram theme + boot
  function initTelegram() {
    tg = window.Telegram && window.Telegram.WebApp;
    if (!tg) return;
    try {
      tg.ready(); tg.expand();
      var p = tg.themeParams || {}, r = document.documentElement.style;
      if (tg.colorScheme === "dark") {
        if (p.bg_color) r.setProperty("--bg", p.bg_color);
        if (p.secondary_bg_color) r.setProperty("--card", p.secondary_bg_color);
        if (p.text_color) r.setProperty("--text", p.text_color);
        if (p.hint_color) r.setProperty("--muted", p.hint_color);
      }
      if (p.button_color) r.setProperty("--accent", p.button_color);
      if (p.button_text_color) r.setProperty("--accent-text", p.button_text_color);
      var bg = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim() || "#0e1117";
      if (tg.setHeaderColor) tg.setHeaderColor(bg);
      if (tg.setBackgroundColor) tg.setBackgroundColor(bg);
    } catch (e) { /* older Telegram versions */ }
  }
  document.getElementById("refresh").addEventListener("click", function () { load(); });
  if (window.Telegram && window.Telegram.WebApp) initTelegram();
  else window.addEventListener("load", initTelegram);
  window.__shadowLoad = load();
  setInterval(function () { if (!document.hidden) load(); }, 120000);
})();
