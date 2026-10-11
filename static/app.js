// Energy dashboard – builds every tab from /api/dashboard.
(function () {
  const C = window.Charts;
  const FUEL = {
    elec: { name: "Electricity", color: "--elec", ramp: "elec" },
    gas: { name: "Gas", color: "--gas", ramp: "gas" },
    export: { name: "Solar export", color: "--export", ramp: "export" },
  };
  const state = {
    data: null, tab: "overview", fcUnit: "cost", fcFuel: "elec", patFuel: "elec", range: "365",
    topFuel: "elec", wxFuel: "gas", calYear: "last",
  };
  const $ = sel => document.querySelector(sel);
  const color = f => C.css(FUEL[f] ? FUEL[f].color : f);

  // ---------- formatting ----------
  const nf0 = new Intl.NumberFormat("en-GB", { maximumFractionDigits: 0 });
  const nf1 = new Intl.NumberFormat("en-GB", { maximumFractionDigits: 1, minimumFractionDigits: 1 });
  const money = (v, dp) => {
    if (v == null || isNaN(v)) return "–";
    const neg = v < 0 ? "−" : "";
    const a = Math.abs(v);
    if (dp === 0 || (dp == null && a >= 1000)) return `${neg}£${nf0.format(Math.round(a))}`;
    return `${neg}£${a.toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  };
  const kwh = (v, dp = 1) => v == null ? "–" : `${dp === 0 ? nf0.format(v) : (Math.abs(v) >= 100 ? nf0.format(v) : nf1.format(v))} kWh`;
  const pence = v => v == null ? "–" : `${v.toFixed(1)}p`;
  const d8 = s => new Date(s + "T12:00:00");
  const fmtDate = (s, opts) => d8(s).toLocaleDateString("en-GB", opts || { weekday: "short", day: "numeric", month: "short", year: "numeric" });
  const monthLabel = (k, long) => new Date(k + "-15T12:00:00").toLocaleDateString("en-GB", long ? { month: "long", year: "numeric" } : { month: "short" });
  const timeOf = ts => new Date(ts * 1000).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  const dayTime = ts => new Date(ts * 1000).toLocaleString("en-GB", { weekday: "short", hour: "2-digit", minute: "2-digit" });
  const slotLabel = i => `${String(Math.floor(i / 2)).padStart(2, "0")}:${i % 2 ? "30" : "00"}`;
  const ago = ts => {
    if (!ts) return "never";
    const m = Math.round((Date.now() / 1000 - ts) / 60);
    if (m < 2) return "just now";
    if (m < 60) return `${m} min ago`;
    const h = Math.round(m / 60);
    return h < 36 ? `${h} h ago` : `${Math.round(h / 24)} days ago`;
  };

  // ---------- DOM helpers ----------
  function h(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const k in attrs || {}) {
      const v = attrs[k];
      if (v == null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "style" && typeof v === "object") Object.assign(n.style, v);
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else if (k === "vars") for (const p in v) n.style.setProperty(p, v[p]);
      else n.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) {
      if (kid == null || kid === false) continue;
      n.appendChild(typeof kid === "string" || typeof kid === "number" ? document.createTextNode(String(kid)) : kid);
    }
    return n;
  }

  // charts are drawn after the card is in the page (they need its width) and redrawn on resize
  let drawers = [];
  function chartBox(draw, cls) {
    const box = h("div", { class: "chart-box " + (cls || "") });
    drawers.push(() => { if (box.isConnected) draw(box); });
    return box;
  }
  function redraw() { drawers.forEach(fn => fn()); }

  function card(title, sub, body, opts = {}) {
    return h("section", { class: `card ${opts.span || "span-12"} ${opts.cls || ""}` },
      (title || opts.right) ? h("div", { class: "card-head" },
        h("div", {}, title ? h("h2", {}, title) : null, sub ? h("p", { class: "sub" }, sub) : null),
        opts.right || null) : null,
      body);
  }

  function seg(options, value, onChange) {
    return h("div", { class: "seg", role: "group" }, options.map(([v, label]) =>
      h("button", { type: "button", "aria-pressed": String(v === value), onclick: () => onChange(v) }, label)));
  }

  function legend(items) {
    return h("div", { class: "legend" }, items.map(it => h("span", {},
      h("i", { class: `sw ${it.kind || ""}`, vars: { "--k": it.color || "transparent" } }), it.label)));
  }

  function tile(label, value, detail, delta, span) {
    return h("section", { class: `card tile ${span || "span-3"}` },
      h("div", { class: "label" }, label),
      h("div", { class: "value" }, value),
      detail ? h("div", { class: "detail" }, detail) : null,
      delta || null);
  }

  // up = bad for spending, good for export
  function deltaEl(now, then, { upIsGood = false, unit = "%", suffix = "" } = {}) {
    if (now == null || then == null || !then) return null;
    const pct = (now - then) / Math.abs(then) * 100;
    const flat = Math.abs(pct) < 1;
    const good = flat ? null : (pct > 0) === upIsGood;
    const arrow = flat ? "→" : pct > 0 ? "▲" : "▼";
    return h("div", { class: `delta ${flat ? "flat" : good ? "good" : "bad"}` },
      `${arrow} ${Math.abs(pct).toFixed(0)}${unit} ${suffix}`);
  }

  function tableView(cols, rows, opts = {}) {
    return h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, cols.map(c => h("th", { class: c.num ? "num" : "" }, c.label)))),
      h("tbody", {}, rows.map(r => h("tr", {
        class: [opts.rowClass ? opts.rowClass(r) : "", opts.onClick ? "click" : ""].join(" "),
        onclick: opts.onClick ? () => opts.onClick(r) : null,
      }, cols.map(c => h("td", { class: c.num ? "num" : "" }, c.get(r))))))));
  }

  // a chart with a "Table" toggle – the table is the accessible twin of the chart
  function withTable(chartEl, tableFn, start) {
    const wrap = h("div", {});
    let showing = start || "chart";
    const btn = h("button", { class: "btn", type: "button" });
    const render = () => {
      wrap.replaceChildren(showing === "chart" ? chartEl : tableFn());
      btn.textContent = showing === "chart" ? "Table" : "Chart";
      if (showing === "chart") requestAnimationFrame(redraw);
    };
    btn.addEventListener("click", () => { showing = showing === "chart" ? "table" : "chart"; render(); });
    render();
    return { el: wrap, btn };
  }

  const icons = {
    good: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="12" cy="12" r="10"/><path d="m8 12 3 3 5-6"/></svg>',
    warn: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M12 3 2 20h20z"/><path d="M12 10v4M12 17v.5"/></svg>',
    info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="12" cy="12" r="10"/><path d="M12 11v6M12 7.5v.5"/></svg>',
  };
  function icon(kind) { const s = h("span", { class: "ic", "aria-label": kind }); s.innerHTML = icons[kind]; return s; }

  // ======================================================================
  // Data loading & sync
  // ======================================================================
  async function load() {
    const main = $("#main");
    main.classList.add("fade");
    try {
      const r = await fetch("/api/dashboard");
      const d = await r.json();
      main.classList.remove("fade");
      if (d.empty) { renderEmpty(d); pollSync(); return; }
      state.data = d;
      renderChrome();
      render();
    } catch (e) {
      main.classList.remove("fade");
      main.replaceChildren(h("div", { class: "loading" }, "Couldn't load data – is the app still running? ", String(e)));
    }
  }

  let polling = false;
  async function pollSync() {
    if (polling) return;
    polling = true;
    const btn = $("#sync-btn"), st = $("#sync-status");
    try {
      for (;;) {
        const s = await (await fetch("/api/sync")).json();
        if (s.demo) { st.textContent = "Demo data"; btn.disabled = true; return; }
        if (s.running) {
          btn.disabled = true;
          st.textContent = (s.step || "Syncing") + "…";
          await new Promise(r => setTimeout(r, 1500));
          continue;
        }
        btn.disabled = false;
        if (s.error) st.textContent = "Sync failed: " + s.error;
        return s;
      }
    } finally { polling = false; }
  }

  async function startSync() {
    await fetch("/api/sync", { method: "POST" });
    const s = await pollSync();
    if (s && !s.error) load();
  }

  function renderEmpty(d) {
    $("#subtitle").textContent = "";
    $("#main").replaceChildren(h("div", { class: "card setup" },
      h("h2", {}, "Downloading your data…"),
      h("p", { class: "sub" }, "The first sync reads up to three years of half-hourly readings – it usually takes under a minute."),
      d.sync && d.sync.error ? h("p", {}, "Problem: " + d.sync.error) : null));
    setTimeout(load, 4000);
  }

  function renderChrome() {
    const d = state.data;
    const parts = [];
    for (const f of ["elec", "gas"]) {
      const p = d.prices_now[f];
      if (p) parts.push(`${FUEL[f].name}: ${p.name}`);
    }
    if (d.prices_now.export) parts.push(`Export: ${d.prices_now.export.name}`);
    $("#subtitle").textContent = parts.join(" · ");
    const st = $("#sync-status");
    if (d.demo) {
      st.textContent = "Demo data";
      $("#sync-btn").disabled = true;
      $("#banner").replaceChildren(h("div", { class: "banner" }, icon("warn"),
        h("div", {}, h("strong", {}, "You're looking at made-up demo data. "),
          "To see your own, run ", h("code", {}, ".venv/bin/python setup_keys.py"),
          " in Terminal (it asks for your Octopus account number and API key), then restart the app.")));
    } else {
      st.textContent = `Updated ${ago(d.account.last_sync)}`;
      $("#banner").replaceChildren();
    }
  }

  // ======================================================================
  // Tabs
  // ======================================================================
  const TABS = { overview, forecast, patterns, peaks, bills, prices, planet, about };

  function render() {
    if (!state.data) return;
    drawers = [];
    document.querySelectorAll("#tabs button").forEach(b => b.setAttribute("aria-selected", String(b.dataset.tab === state.tab)));
    const main = $("#main");
    main.replaceChildren(h("div", { class: "grid" }, TABS[state.tab](state.data)));
    requestAnimationFrame(redraw);
  }

  function setTab(t) {
    state.tab = TABS[t] ? t : "overview";
    if (location.hash !== "#" + state.tab) history.replaceState(null, "", "#" + state.tab);
    render();
    window.scrollTo({ top: 0 });
  }

  // ---------------------------------------------------------------- Overview
  function overview(d) {
    const k = d.kpis;
    const bills = d.fuels.filter(f => f !== "export");
    const out = [];

    // hero: this month
    const cur = d.monthly.find(m => m.status === "current");
    const soFar = k.month_so_far || 0, proj = k.month_projected || 0;
    const days = cur ? cur.days : 30;
    const done = k.month_days || 0;
    // cumulative spend this month vs the same month last year
    const mDays = d.daily.filter(x => x.d.startsWith(k.month));
    const lyKey = `${+k.month.slice(0, 4) - 1}${k.month.slice(4)}`;
    const lyDays = d.daily.filter(x => x.d.startsWith(lyKey));
    const dayCost = x => bills.reduce((a, f) => a + (x[f + "_cost"] || 0), 0);
    const cum = arr => { let t = 0; const out = Array(days).fill(null); arr.forEach(x => { const i = +x.d.slice(8) - 1; if (i < days) { t += dayCost(x); out[i] = t; } }); return out; };
    const thisCum = cum(mDays.filter(x => !(x.partial || []).length));
    const lastCum = lyDays.length >= days - 2 ? cum(lyDays) : null;
    const lastDone = thisCum.reduce((a, v, i) => v != null ? i : a, -1);
    const predCum = Array(days).fill(null);
    if (lastDone >= 0) {
      const start = thisCum[lastDone];
      for (let i = lastDone; i < days; i++) predCum[i] = start + (proj - start) * (i - lastDone) / Math.max(1, days - 1 - lastDone);
    } else for (let i = 0; i < days; i++) predCum[i] = proj * (i + 1) / days;
    out.push(h("section", { class: "card hero span-5" },
      h("div", {},
        h("div", { class: "label" }, `${monthLabel(k.month, true)} is heading for`),
        h("div", { class: "value" }, money(proj, 0)),
        h("div", { class: "range" }, `Likely between ${money(k.month_low, 0)} and ${money(k.month_high, 0)} · ${money(soFar)} so far (${done} of ${days} days)`),
        deltaEl(proj, k.month_last_year, { suffix: `vs ${monthLabel(k.month)} last year (${money(k.month_last_year, 0)})` })),
      h("div", {},
        chartBox(box => C.lines(box, {
          x: [...Array(days).keys()].map(i => i + 1), height: 170, yFmt: v => `£${nf0.format(v)}`, yMin: 0, ticks: 3,
          series: [
            { name: "This month", color: C.css("--accent"), values: thisCum },
            { name: "Predicted", color: C.css("--accent"), values: predCum, dash: "4 4" },
            ...(lastCum ? [{ name: `${monthLabel(lyKey)} ${lyKey.slice(0, 4)}`, color: C.css("--muted"), values: lastCum, width: 1.5 }] : []),
          ],
          xTicks: [1, 8, 15, 22, days].map(x => ({ x, label: String(x) })),
          xFmt: x => `${x} ${monthLabel(k.month)}`, vFmt: v => money(v),
          markers: [{ x: days, y: proj, color: C.css("--accent"), label: money(proj, 0), anchor: "end", below: true }],
        })),
        legend([{ color: C.css("--accent"), kind: "line", label: "Spent so far" }, { color: C.css("--accent"), kind: "dash", label: "Predicted" },
          ...(lastCum ? [{ color: C.css("--muted"), kind: "line", label: `Same month last year` }] : [])]))));

    const yr = k.year_projected;
    out.push(h("div", { class: "span-7 grid", style: { gap: "16px" } },
      tile(`${k.year} total (predicted)`, money(yr, 0), `${money(k.year_so_far, 0)} so far · likely ${money(k.year_low, 0)}–${money(k.year_high, 0)}`,
        deltaEl(yr, k.last_year, { suffix: `vs ${k.year - 1} (${money(k.last_year, 0)})` }), "span-6"),
      tile("Next 12 months", money(k.next12, 0), `likely ${money(k.next12_low, 0)}–${money(k.next12_high, 0)} at today's prices`, null, "span-6"),
      ...bills.map(f => {
        const p = k.per_fuel[f];
        return tile(`${FUEL[f].name} · last 30 days`, `${money(p.avg_daily_cost_30)}/day`, `${kwh(p.avg_daily_kwh_30)} a day on average`, null, "span-6");
      }),
      d.fuels.includes("export") ? tile("Solar export · last 12 months", money(k.per_fuel.export.cost_365, 0),
        `${kwh(k.per_fuel.export.kwh_365, 0)} sent to the grid`, null, "span-6") : null,
      d.payments ? balanceTile(d.payments) :
        d.baseload ? tile("Always on", `${d.baseload.watts} W`, `≈ ${money(d.baseload.cost_year, 0)} a year, day and night`, null, "span-6") : null,
    ));
    if (d.payments) out.push(paymentPlanner(d.payments));

    // insights
    const insightsCard = card("What stands out", "Worked out from your readings, the weather and your tariff",
      h("ul", { class: "insights" }, d.insights.map(t => h("li", { class: t.tone }, icon(t.tone), h("span", {}, t.text)))),
      { span: "span-12" });

    // 12 months back + 3 ahead
    const idx = d.monthly.findIndex(m => m.status === "current");
    const months = d.monthly.slice(Math.max(0, idx - 11), idx + 4);
    const monthlyCard = card("Monthly bills", "Last 12 months and the next 3 · electricity + gas, including standing charges",
      h("div", {}, chartBox(box => monthlyChart(box, months, "cost", bills, 250)),
        h("div", { style: { marginTop: "10px" } }, monthlyLegend(bills, "cost"))),
      { span: "span-12" });

    // last 30 days
    const last = d.daily.slice(-30);
    const lastCard = card("Last 30 days", "Daily cost · click a day to see it hour by hour",
      h("div", {}, chartBox(box => C.bars(box, {
        labels: last.map(x => x.d), height: 200, stacked: true, yFmt: v => `£${v}`,
        series: bills.map(f => ({ name: FUEL[f].name, color: color(f), values: last.map(x => x[f + "_cost"] || 0) })),
        xTick: i => (i % 5 === 0 || i === last.length - 1) ? d8(last[i].d).toLocaleDateString("en-GB", { day: "numeric", month: "short" }) : null,
        tooltip: i => ({ title: fmtDate(last[i].d) + (last[i].t != null ? ` · ${last[i].t}°C` : ""),
          rows: bills.map(f => ({ color: color(f), value: money(last[i][f + "_cost"]), label: `${FUEL[f].name} · ${kwh(last[i][f])}` })),
          note: (last[i].partial || []).length ? "Some readings missing" : null }),
        onClick: i => openDay(last[i].d),
      })), h("div", { style: { marginTop: "8px" } }, legend(bills.map(f => ({ color: color(f), label: FUEL[f].name }))))),
      { span: "span-12" });

    out.push(h("div", { class: "col span-5" }, insightsCard, card("Your tariffs", null, tariffSummary(d), { span: "span-12" })));
    out.push(h("div", { class: "col span-7" }, monthlyCard, lastCard));
    return out;
  }

  function endsNote(p) {
    if (!p.ends) return null;
    const days = Math.round((d8(p.ends) - d8(state.data.today)) / 86400000);
    return h("div", { class: "small", style: { marginTop: "2px" } },
      h("span", { class: "pill" + (days <= 60 ? " blue" : "") }, `${p.fixed ? "Fixed" : "Ends"} until ${fmtDate(p.ends, { day: "numeric", month: "short", year: "numeric" })} · ${days} days left`),
      p.rollover ? h("span", { class: "muted" }, ` then ${p.rollover.name} – ${pence(p.rollover.unit)}/kWh at today's price`) : null);
  }

  function balanceTile(p) {
    const credit = p.balance >= 0;
    const dd = p.direct_debit;
    return h("section", { class: "card tile span-6" },
      h("div", { class: "label" }, "Account balance"),
      h("div", { class: "value" }, money(Math.abs(p.balance), 0), h("span", { class: "muted", style: { fontSize: "14px", fontWeight: 550 } }, credit ? " in credit" : " owed")),
      h("div", { class: "detail" }, dd ? `Direct debit ${money(dd.amount, 0)} on the ${ordinal(dd.day)}` : "No direct debit found",
        p.at ? ` · checked ${ago(p.at)}` : ""),
      p.overdue > 0 ? h("div", { class: "delta bad" }, `${money(p.overdue)} overdue`) : null);
  }

  const ordinal = n => n + (n % 10 === 1 && n !== 11 ? "st" : n % 10 === 2 && n !== 12 ? "nd" : n % 10 === 3 && n !== 13 ? "rd" : "th");

  function paymentPlanner(p) {
    const dd = p.direct_debit;
    const ms = p.months;
    const chip = (k, v, d, strong) => h("div", { class: "chip", style: strong ? { boxShadow: "0 0 0 2px var(--accent)" } : null },
      h("div", { class: "k" }, k), h("div", { class: "v" }, v), d ? h("div", { class: "d" }, d) : null);
    const ddVerdict = dd ? (p.current_goes_negative
      ? `below £0 by ${monthLabel(p.current_goes_negative, true)}`
      : `lowest ${money(p.current_lowest, 0)} in ${monthLabel(p.current_lowest_month)}`) : null;
    const series = [];
    if (p.path_current) series.push({ name: `Your ${money(dd.amount, 0)} direct debit`, color: C.css("--temp"), values: p.path_current });
    series.push({ name: `${money(p.min_typical, 0)} a month`, color: C.css("--accent"), values: p.path_min });
    series.push({ name: `${money(p.min_cautious, 0)} a month (cold winter)`, color: C.css("--accent"), values: p.path_cautious, dash: "5 4" });
    const chart = chartBox(box => C.lines(box, {
      x: ms.map((_, i) => i), height: 240, yFmt: v => `${v < 0 ? "−" : ""}£${nf0.format(Math.abs(v))}`,
      series,
      xTicks: ms.map((m, i) => ({ x: i, label: monthLabel(m) + (m.endsWith("-01") ? ` ’${m.slice(2, 4)}` : "") })),
      tooltip: i => ({ title: `End of ${monthLabel(ms[i], true)}`,
        rows: [...series.map(sr => ({ color: sr.color, value: money(sr.values[i], 0), label: sr.name })),
          { value: money(p.cost[i], 0), label: `predicted bills${p.payments[i] ? "" : " (no payment this month)"}` }] }),
    }));
    const tbl = withTable(chart, () => tableView([
      { label: "Month", get: i => monthLabel(ms[i], true) },
      { label: "Predicted bills", num: true, get: i => money(p.cost[i], 0) },
      { label: "Cold-winter bills", num: true, get: i => money(p.cost_cautious[i], 0) },
      ...(p.path_current ? [{ label: `Balance on ${money(dd.amount, 0)}`, num: true, get: i => money(p.path_current[i], 0) }] : []),
      { label: `Balance on ${money(p.min_typical, 0)}`, num: true, get: i => money(p.path_min[i], 0) },
    ], ms.map((_, i) => i)));
    return card("How much should you pay each month?",
      `Starting from your ${money(p.balance, 0)} balance, taking off each month's predicted bills (minus solar export) and adding one payment a month. Bills over the next 12 months: about ${money(p.total_12, 0)}.`,
      h("div", {},
        h("div", { class: "chips", style: { marginBottom: "14px" } },
          dd ? chip("Your direct debit now", `${money(dd.amount, 0)}/month`, ddVerdict) : null,
          chip("Lowest to stay above £0", `${money(p.min_typical, 0)}/month`, "based on a normal winter", true),
          chip("Safer – if it's a cold winter", `${money(p.min_cautious, 0)}/month`, "uses the top of the likely range"),
          chip("End the year where you are now", `${money(p.keep_balance, 0)}/month`, `keeps about ${money(p.balance, 0)} in credit`),
          p.octopus && p.octopus.suggested_dd != null ? chip("Octopus suggests", `${money(p.octopus.suggested_dd, 0)}/month`,
            `their minimum ${money(p.octopus.minimum_dd, 0)}${p.octopus.projected_balance != null ? ` · they project ${money(p.octopus.projected_balance, 0)}` : ""}`) : null),
        tbl.el,
        h("div", { style: { marginTop: "8px" } }, legend(series.map(sr => ({ color: sr.color, kind: sr.dash ? "dash" : "line", label: sr.name })))),
        h("p", { class: "note" }, "Balance at the end of each month. ",
          p.unbilled != null ? `Octopus last billed you up to ${fmtDate(p.last_billed, { day: "numeric", month: "short" })}; the ${money(p.unbilled)} you've used since then is counted this month. ` : "This month's bill is counted in full in case none of it has been billed yet. ",
          "Prices switch to Flexible when your fix ends. Octopus's own suggestion uses their method (usually a little more cautious, and it doesn't know about your solar or the weather forecast). You can change your direct debit in the Octopus app.")),
      { span: "span-12", right: tbl.btn });
  }

  function tariffSummary(d) {
    return h("div", { class: "grid", style: { gap: "10px" } }, Object.keys(FUEL).filter(x => d.prices_now[x]).map(f => [f, d.prices_now[f]]).map(([f, p]) =>
      h("div", { class: "span-12 tariff-card" },
        h("div", {}, h("i", { class: "dot-key", vars: { "--k": color(f) } }), h("span", { class: "muted small" }, FUEL[f].name)),
        h("div", { class: "name" }, p.name),
        h("div", { class: "small" },
          p.unit_now != null ? `${pence(p.unit_now)}/kWh right now` : "",
          p.unit_effective != null && Math.abs((p.unit_effective || 0) - (p.unit_now || 0)) > 0.05 ? ` · you paid ${pence(p.unit_effective)} on average (30 days)` : "",
          f !== "export" ? ` · ${pence(p.standing)}/day standing charge` : ""),
        endsNote(p))));
  }

  function monthlyLegend(fuels, unit) {
    const items = fuels.map(f => ({ color: color(f), label: FUEL[f].name }));
    items.push({ color: C.css("--ink-2"), kind: "hatch", label: "Predicted" });
    items.push({ kind: "whisker", label: "Likely range" });
    items.push({ kind: "tick", label: "Same month last year" });
    return legend(items);
  }

  function monthlyChart(box, months, unit, fuels, height) {
    const cur = months.findIndex(m => m.status === "current");
    const key = unit === "cost" ? ["cost", "fcost", "low_cost", "high_cost", "total_cost"] : ["kwh", "fkwh", "low_kwh", "high_kwh", "total_kwh"];
    const byKey = Object.fromEntries(state.data.monthly.map(m => [m.month, m]));
    const lastYear = months.map(m => {
      const ly = byKey[`${+m.month.slice(0, 4) - 1}${m.month.slice(4)}`];
      if (!ly || fuels.some(f => !ly[f] || ly[f].days < ly.days - 2)) return null;
      return fuels.reduce((a, f) => a + (ly[f][key[0]] || 0), 0);
    });
    const f$ = v => unit === "cost" ? money(v, v >= 100 ? 0 : 2) : kwh(v, 0);
    C.bars(box, {
      labels: months.map(m => m.month), height, stacked: true,
      yFmt: v => unit === "cost" ? `£${nf0.format(v)}` : nf0.format(v),
      series: fuels.map(f => ({ name: FUEL[f].name, color: color(f), values: months.map(m => m[f][key[0]]), forecast: months.map(m => m[f][key[1]]) })),
      range: months.map(m => m[fuels[0]][key[1]] > 0 ? {
        low: fuels.reduce((a, f) => a + (m[f][key[2]] || 0), 0), high: fuels.reduce((a, f) => a + (m[f][key[3]] || 0), 0) } : null),
      marker: lastYear,
      highlight: cur >= 0 ? cur : null,
      xTick: i => {
        const m = months[i].month;
        const mon = +m.slice(5);
        const every = months.length > 30 ? 3 : months.length > 18 ? 2 : 1;
        if (every > 1 && (mon - 1) % every && i !== cur) return null;
        if (every > 1 && cur >= 0 && i !== cur && Math.abs(i - cur) < every) return null;
        return mon === 1 || i === 0 ? `${monthLabel(m)} ’${m.slice(2, 4)}` : monthLabel(m);
      },
      tooltip: i => {
        const m = months[i];
        const rows = [];
        for (const f of fuels) {
          if (m[f][key[0]]) rows.push({ color: color(f), value: f$(m[f][key[0]]), label: `${FUEL[f].name}${m[f][key[1]] ? " so far" : ""}` });
          if (m[f][key[1]]) rows.push({ color: color(f), hatch: true, value: f$(m[f][key[1]]), label: `${FUEL[f].name} predicted` });
        }
        const total = fuels.reduce((a, f) => a + (m[f][key[4]] || 0), 0);
        const pred = fuels.some(f => m[f][key[1]]);
        rows.push({ value: f$(total), label: pred ? `total (likely ${f$(fuels.reduce((a, f) => a + (m[f][key[2]] || 0), 0))}–${f$(fuels.reduce((a, f) => a + (m[f][key[3]] || 0), 0))})` : "total" });
        if (lastYear[i] != null) rows.push({ value: f$(lastYear[i]), label: "same month last year" });
        return { title: monthLabel(m.month, true) + (m.temp != null ? ` · avg ${m.temp}°C` : ""), rows,
          note: m.status === "past" && fuels.some(f => m[f].days < m.days - 1) ? `Readings missing for some days` : null };
      },
    });
  }

  // ---------------------------------------------------------------- Predictions
  function forecast(d) {
    const out = [];
    const bills = d.fuels.filter(f => f !== "export");
    const unit = state.fcUnit;
    const fuels = unit === "cost" ? bills : [state.fcFuel];
    out.push(h("div", { class: "filter-row" },
      h("span", { class: "lbl" }, "Show"),
      seg([["cost", "£ cost"], ["kwh", "kWh"]], unit, v => { state.fcUnit = v; render(); }),
      unit === "kwh" ? seg(d.fuels.map(f => [f, FUEL[f].name]), state.fcFuel, v => { state.fcFuel = v; render(); }) : null,
      unit === "cost" ? h("span", { class: "muted small" }, "Electricity + gas, including standing charges. Export earnings not subtracted.") : null));

    // years
    const ykey = unit === "cost" ? ["cost", "fcost", "total_cost", "low_cost", "high_cost"] : ["kwh", "fkwh", "total_kwh", "low_kwh", "high_kwh"];
    const f$ = v => unit === "cost" ? money(v, 0) : kwh(v, 0);
    const years = d.yearly;
    const thisYear = new Date(d.today).getFullYear();
    const yearCards = years.map(y => {
      const total = fuels.reduce((a, f) => a + (y[f][ykey[2]] || 0), 0);
      const pred = fuels.some(f => y[f][ykey[1]] > 0);
      const partial = !pred && fuels.some(f => y[f].days < y.days - 3);
      const prev = years.find(p => p.year === y.year - 1);
      const prevTotal = prev && !fuels.some(f => prev[f].days < prev.days - 3) ? fuels.reduce((a, f) => a + (prev[f][ykey[2]] || 0), 0) : null;
      return h("div", { class: "chip" },
        h("div", { class: "k" }, `${y.year}${y.year === thisYear ? " · this year" : y.year > thisYear ? " · predicted" : partial ? " · part year" : ""}`),
        h("div", { class: "v" }, f$(total)),
        pred ? h("div", { class: "d" }, `likely ${f$(fuels.reduce((a, f) => a + (y[f][ykey[3]] || 0), 0))}–${f$(fuels.reduce((a, f) => a + (y[f][ykey[4]] || 0), 0))}`) : null,
        partial ? h("div", { class: "d" }, `${Math.min(...fuels.map(f => y[f].days))} days of data`) : null,
        prevTotal && !partial ? deltaEl(total, prevTotal, { upIsGood: fuels[0] === "export", suffix: `vs ${y.year - 1}` }) : null);
    });
    const yc = withTable(chartBox(box => C.bars(box, {
      labels: years.map(y => String(y.year)), height: 220, stacked: true, maxBar: 56,
      yFmt: v => unit === "cost" ? `£${nf0.format(v)}` : nf0.format(v),
      series: fuels.map(f => ({ name: FUEL[f].name, color: color(f), values: years.map(y => y[f][ykey[0]]), forecast: years.map(y => y[f][ykey[1]]) })),
      range: years.map(y => fuels.some(f => y[f][ykey[1]] > 0) ? { low: fuels.reduce((a, f) => a + y[f][ykey[3]], 0), high: fuels.reduce((a, f) => a + y[f][ykey[4]], 0) } : null),
      highlight: years.findIndex(y => y.year === thisYear),
      tooltip: i => {
        const y = years[i];
        return { title: String(y.year), rows: fuels.flatMap(f => [
          y[f][ykey[0]] ? { color: color(f), value: f$(y[f][ykey[0]]), label: `${FUEL[f].name}${y[f][ykey[1]] ? " so far" : ""}` } : null,
          y[f][ykey[1]] ? { color: color(f), hatch: true, value: f$(y[f][ykey[1]]), label: `${FUEL[f].name} predicted` } : null]).filter(Boolean),
          note: fuels.some(f => y[f].days && y[f].days < y.days - 3 && !y[f][ykey[1]]) ? "Part year – data starts mid-year" : null };
      },
    })), () => tableView([
      { label: "Year", get: y => String(y.year) },
      ...fuels.map(f => ({ label: FUEL[f].name, num: true, get: y => f$(y[f][ykey[2]]) })),
      { label: "Of which predicted", num: true, get: y => f$(fuels.reduce((a, f) => a + y[f][ykey[1]], 0)) },
      { label: "Likely range", num: true, get: y => fuels.some(f => y[f][ykey[1]] > 0) ? `${f$(fuels.reduce((a, f) => a + y[f][ykey[3]], 0))}–${f$(fuels.reduce((a, f) => a + y[f][ykey[4]], 0))}` : "–" },
    ], years));
    out.push(card("Year by year", "Actual so far, plus what the rest of the year is likely to add", h("div", {},
      h("div", { class: "chips", style: { marginBottom: "14px" } }, yearCards), yc.el), { span: "span-12", right: yc.btn }));

    // months
    const idx = d.monthly.findIndex(m => m.status === "current");
    const months = d.monthly.slice(Math.max(0, idx - 23));
    const mc = withTable(chartBox(box => monthlyChart(box, months, unit, fuels, 280)), () => tableView([
      { label: "Month", get: m => monthLabel(m.month, true) },
      ...fuels.map(f => ({ label: FUEL[f].name, num: true, get: m => (unit === "cost" ? money : v => kwh(v, 0))(m[f][unit === "cost" ? "total_cost" : "total_kwh"]) })),
      { label: "Likely range", num: true, get: m => m[fuels[0]][unit === "cost" ? "fcost" : "fkwh"] ? `${(unit === "cost" ? v => money(v, 0) : v => kwh(v, 0))(fuels.reduce((a, f) => a + m[f][unit === "cost" ? "low_cost" : "low_kwh"], 0))}–${(unit === "cost" ? v => money(v, 0) : v => kwh(v, 0))(fuels.reduce((a, f) => a + m[f][unit === "cost" ? "high_cost" : "high_kwh"], 0))}` : "" },
      { label: "Avg temp", num: true, get: m => m.temp != null ? `${m.temp}°C` : "–" },
      { label: "Normal temp", num: true, get: m => m.normal_temp != null ? `${m.normal_temp}°C` : "–" },
      { label: "", get: m => m.status === "future" ? h("span", { class: "pill" }, "predicted") : m.status === "current" ? h("span", { class: "pill blue" }, "this month") : "" },
    ], months, { rowClass: m => m.status }));
    out.push(card("Month by month", "The last two years and predictions to the end of next year. Hover for details.",
      h("div", {}, mc.el, h("div", { style: { marginTop: "10px" } }, monthlyLegend(fuels, unit))), { span: "span-12", right: mc.btn }));

    // next 14 days
    const wx = d.weather;
    if (wx && wx.next_days.length) {
      const nd = wx.next_days;
      out.push(card("The next two weeks", "Predicted from the weather forecast",
        h("div", {}, chartBox(box => C.bars(box, {
          labels: nd.map(x => x.d), height: 200, stacked: true, yFmt: v => `£${v}`,
          series: bills.map(f => ({ name: FUEL[f].name, color: color(f), values: nd.map(() => 0), forecast: nd.map(x => x[f + "_cost"] || 0) })),
          xTick: i => d8(nd[i].d).toLocaleDateString("en-GB", i % 2 ? { day: "numeric" } : { weekday: "short", day: "numeric" }),
          tooltip: i => ({ title: `${fmtDate(nd[i].d)} · ${nd[i].t}°C (normal ${nd[i].normal}°C)`,
            rows: bills.map(f => ({ color: color(f), hatch: true, value: money(nd[i][f + "_cost"]), label: `${FUEL[f].name} · ${kwh(nd[i][f])}` })) }),
        })), h("div", { style: { marginTop: "8px" } }, legend(bills.map(f => ({ color: color(f), kind: "hatch", label: FUEL[f].name + " (predicted)" }))))),
        { span: "span-7" }));
      out.push(card("Forecast temperature", "Daily average °C vs normal for the time of year",
        chartBox(box => C.lines(box, {
          x: nd.map((_, i) => i), height: 200, yFmt: v => `${v}°`, yMin: Math.min(0, ...nd.map(x => Math.min(x.t, x.normal))) ,
          series: [{ name: "Forecast", color: C.css("--temp"), values: nd.map(x => x.t) },
            { name: "Normal", color: C.css("--muted"), values: nd.map(x => x.normal), dash: "4 4" }],
          xTicks: nd.map((x, i) => i % 3 === 0 ? { x: i, label: d8(x.d).toLocaleDateString("en-GB", { weekday: "short", day: "numeric" }) } : null).filter(Boolean),
          xFmt: i => fmtDate(nd[i].d), vFmt: v => `${v}°C`,
        })), { span: "span-5", right: legend([{ color: C.css("--temp"), kind: "line", label: "Forecast" }, { color: C.css("--muted"), kind: "dash", label: "Normal" }]) }));
    }

    out.push(card("How the predictions work", null, methodText(d), { span: "span-12" }));
    return out;
  }

  function methodText(d) {
    const rows = Object.entries(d.models).map(([f, m]) => h("li", {},
      h("strong", {}, FUEL[f].name + ": "),
      m.kind === "weather"
        ? `${nf1.format(m.base)} kWh a day, ${nf1.format(Math.abs(m.slope))} kWh ${m.slope >= 0 ? "more" : "less"} for every degree below 15.5°C` +
          (m.weekend ? `, ${m.weekend > 0 ? "+" : "−"}${nf1.format(Math.abs(m.weekend))} kWh at weekends` : "") +
          `. Learned from ${m.days} days; explains ${Math.round((m.r2 || 0) * 100)}% of the day-to-day variation` +
          (m.provisional ? " (provisional – needs a winter of data to learn your heating)." : ".")
        : m.kind === "seasonal" ? "No weather data, so: same month last year, adjusted for your recent trend." : "Not enough data yet."));
    return h("div", { class: "small" },
      h("p", { style: { marginTop: 0 } }, "Each day is predicted from its temperature: colder days mean more heating (and more lighting). For the next two weeks the real weather forecast is used; after that, the average temperature for that date over the last six years. Costs use what you're paying now – if prices change, so will the bills."),
      h("ul", {}, rows),
      ...Object.entries(d.prices_now).filter(([, p]) => p.ends && p.rollover).map(([f, p]) =>
        h("p", {}, `Your ${FUEL[f].name.toLowerCase()} deal ends on ${fmtDate(p.ends, { day: "numeric", month: "long", year: "numeric" })}. After that the predictions assume you roll onto ${p.rollover.name} at today's price (${pence(p.rollover.unit)}/kWh, ${pence(p.rollover.standing)}/day) – if you fix again, they'll update once Octopus has your new tariff.`)),
      h("p", {}, "The ‘likely range’ (shown as whiskers) covers roughly 8 in 10 outcomes: it allows for how much your real days bounce around the model, and how much each month's weather varies from year to year. Recent changes in your habits are given extra weight for the next few weeks."));
  }

  // ---------------------------------------------------------------- Patterns
  function patterns(d) {
    const out = [];
    const f = state.patFuel;
    const fuelName = FUEL[f].name;
    out.push(h("div", { class: "filter-row" }, h("span", { class: "lbl" }, "Fuel"),
      seg(d.fuels.map(x => [x, FUEL[x].name]), f, v => { state.patFuel = v; render(); })));

    // calendar heatmap
    const years = [...new Set(d.daily.map(x => x.d.slice(0, 4)))];
    let days;
    if (state.calYear === "last") {
      const end = d8(d.daily[d.daily.length - 1].d);
      const start = new Date(end); start.setDate(start.getDate() - 364);
      days = d.daily.filter(x => d8(x.d) >= start);
    } else days = d.daily.filter(x => x.d.startsWith(state.calYear));
    const calItems = fillDays(days).map(x => ({ d: x.d, v: x[f] != null ? x[f] : null, row: x }));
    const ramp = C.ramp(FUEL[f].ramp);
    let range = { lo: 0, hi: 1 };
    const scaleEl = h("div", { class: "scale" });
    const cal = chartBox(box => {
      range = C.calendar(box, { days: calItems, ramp, onClick: openDay,
        tooltip: it => ({ title: fmtDate(it.d) + (it.row.t != null ? ` · ${it.row.t}°C` : ""),
          rows: it.v == null ? [{ value: "No data" }] : [{ color: color(f), value: kwh(it.v), label: `${fuelName} · ${money(it.row[f + "_cost"])}` },
            it.row[f + "_exp"] != null ? { value: kwh(it.row[f + "_exp"]), label: "expected for the weather" } : null].filter(Boolean),
          note: "Click for the hour-by-hour view" }) }) || range;
      scaleEl.replaceChildren(h("span", {}, kwh(range.lo)),
        h("span", { class: "bar", style: { background: `linear-gradient(90deg, ${[0, .25, .5, .75, 1].map(ramp).join(",")})` } }),
        h("span", {}, kwh(range.hi) + " a day"));
    });
    out.push(card(`Every day at a glance`, "Darker = more energy. Click any day to see it hour by hour.",
      h("div", {}, cal, scaleEl), { span: "span-12",
        right: seg([["last", "Last 12 months"], ...years.map(y => [y, y])], state.calYear, v => { state.calYear = v; render(); }) }));

    // daily line with expected
    const rangeDays = state.range === "all" ? d.daily : d.daily.slice(-Number(state.range));
    const xs = rangeDays.map(x => d8(x.d).getTime());
    const hasExp = rangeDays.some(x => x[f + "_exp"] != null);
    const xTicks = monthTicks(rangeDays);
    out.push(card(`Daily ${fuelName.toLowerCase()}`, hasExp ? "Solid: what you used. Dashed: what we'd expect for that day's weather." : null,
      h("div", {}, chartBox(box => C.lines(box, {
        x: xs, height: 240, yFmt: v => nf0.format(v), xTicks,
        series: [{ name: fuelName, color: color(f), values: rangeDays.map(x => x[f] != null ? x[f] : null), area: true },
          ...(hasExp ? [{ name: "Expected for the weather", color: C.css("--muted"), values: rangeDays.map(x => x[f + "_exp"] != null ? x[f + "_exp"] : null), dash: "4 4", width: 1.5 }] : [])],
        tooltip: i => ({ title: fmtDate(rangeDays[i].d) + (rangeDays[i].t != null ? ` · ${rangeDays[i].t}°C` : ""),
          rows: [{ color: color(f), value: kwh(rangeDays[i][f]), label: `${fuelName} · ${money(rangeDays[i][f + "_cost"])}` },
            hasExp && rangeDays[i][f + "_exp"] != null ? { color: C.css("--muted"), value: kwh(rangeDays[i][f + "_exp"]), label: "expected" } : null].filter(Boolean) }),
        onClick: i => openDay(rangeDays[i].d),
      })),
        h("div", { style: { marginTop: "6px" } }, chartBox(box => C.lines(box, {
          x: xs, height: 110, yFmt: v => `${v}°`, xTicks, margin: { top: 8 },
          series: [{ name: "Temperature", color: C.css("--temp"), values: rangeDays.map(x => x.t), width: 1.5 }],
          xFmt: ms => fmtDate(new Date(ms).toISOString().slice(0, 10)), vFmt: v => `${v}°C`,
        }))),
        h("div", { class: "note" }, "Lower chart: average outdoor temperature (°C) for the same days.")),
      { span: "span-12", right: h("div", { class: "controls" },
        hasExp ? legend([{ color: color(f), kind: "line", label: fuelName }, { color: C.css("--muted"), kind: "dash", label: "Expected" }]) : null,
        seg([["90", "90 days"], ["365", "1 year"], ["all", "All"]], state.range, v => { state.range = v; render(); })) }));

    // profiles
    const p = d.patterns[f];
    const slots = [...Array(48).keys()];
    const slotTicks = [0, 12, 24, 36, 47].map(i => ({ x: i, label: i === 47 ? "23:30" : slotLabel(i) }));
    out.push(card("Your average day", "Last 90 days · kWh per half hour",
      chartBox(box => C.lines(box, {
        x: slots, height: 230, yFmt: v => v.toFixed(2), xTicks: slotTicks,
        series: [{ name: "Weekdays", color: color(f), values: p.weekday }, { name: "Weekends", color: C.css("--temp"), values: p.weekend }],
        markers: null,
        xFmt: i => `${slotLabel(i)}–${slotLabel((i + 1) % 48)}`, vFmt: v => `${v.toFixed(2)} kWh`,
        shade: f !== "export" ? [{ from: 32, to: 38, cls: "shade-alt" }] : null,
      })), { span: "span-6", right: legend([{ color: color(f), kind: "line", label: "Weekdays" }, { color: C.css("--temp"), kind: "line", label: "Weekends" }]) }));
    const hasSeason = p.winter.some(v => v != null) && p.summer.some(v => v != null);
    out.push(card("Winter vs summer", hasSeason ? "Average day in Dec–Feb vs Jun–Aug, all your data" : "Needs a winter and a summer of data",
      hasSeason ? chartBox(box => C.lines(box, {
        x: slots, height: 230, yFmt: v => v.toFixed(2), xTicks: slotTicks,
        series: [{ name: "Winter", color: C.css("--temp"), values: p.winter }, { name: "Summer", color: C.css("--summer"), values: p.summer }],
        xFmt: i => `${slotLabel(i)}–${slotLabel((i + 1) % 48)}`, vFmt: v => `${v.toFixed(2)} kWh`,
      })) : h("div", { class: "empty" }, "Not enough data yet"),
      { span: "span-6", right: hasSeason ? legend([{ color: C.css("--temp"), kind: "line", label: "Winter" }, { color: C.css("--summer"), kind: "line", label: "Summer" }]) : null }));

    // week x hour
    const days7 = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
    out.push(card("When in the week", "Average kWh in each hour, last 90 days. Darker = more.",
      chartBox(box => C.grid(box, {
        rows: days7, cols: [...Array(24).keys()], values: p.week_hour, ramp: C.ramp(FUEL[f].ramp), cellHeight: 24,
        colTick: c => c % 3 === 0 ? `${String(c).padStart(2, "0")}:00` : null,
        tooltip: (r, c, v) => ({ title: `${days7[r]} ${String(c).padStart(2, "0")}:00–${String(c + 1).padStart(2, "0")}:00`, rows: [{ color: color(f), value: v == null ? "–" : kwh(v, 2).replace(".0 ", " "), label: "average" }] }),
      })), { span: "span-8" }));
    out.push(card("By day of the week", "Average per day, last 12 months",
      chartBox(box => C.bars(box, {
        labels: days7, height: 200, yFmt: v => nf0.format(v),
        series: [{ name: fuelName, color: color(f), values: p.by_weekday }],
        tooltip: i => ({ title: days7[i], rows: [{ color: color(f), value: kwh(p.by_weekday[i]), label: "a day on average" }] }),
      })), { span: "span-4" }));
    return out;
  }

  function fillDays(days) {
    if (!days.length) return [];
    const map = Object.fromEntries(days.map(x => [x.d, x]));
    const out = [];
    const a = d8(days[0].d), b = d8(days[days.length - 1].d);
    for (let t = new Date(a); t <= b; t.setDate(t.getDate() + 1)) {
      const key = t.toISOString().slice(0, 10);
      out.push(map[key] || { d: key });
    }
    return out;
  }

  function monthTicks(days) {
    const out = [];
    const every = days.length > 500 ? 3 : days.length > 200 ? 2 : 1;
    let n = 0;
    days.forEach((x, i) => {
      if (x.d.endsWith("-01") || (i === 0 && days.length < 60)) {
        if (n++ % every === 0) out.push({ x: d8(x.d).getTime(), label: x.d.slice(5, 7) === "01" ? x.d.slice(0, 4) : monthLabel(x.d.slice(0, 7)) });
      }
    });
    if (days.length <= 100) {
      out.length = 0;
      days.forEach((x, i) => { if (i % 14 === 0) out.push({ x: d8(x.d).getTime(), label: d8(x.d).toLocaleDateString("en-GB", { day: "numeric", month: "short" }) }); });
    }
    return out;
  }

  // ---------------------------------------------------------------- Big days
  function peaks(d) {
    const out = [];
    const pk = d.peaks;
    const unusual = pk.unusual || [];
    out.push(card("Unusual days", "Recent days that used much more than expected for the weather (last 90 days)",
      unusual.length ? tableView([
        { label: "Day", get: u => fmtDate(u.d) },
        { label: "Fuel", get: u => h("span", {}, h("i", { class: "dot-key", vars: { "--k": color(u.fuel) } }), FUEL[u.fuel].name) },
        { label: "Used", num: true, get: u => kwh(u.kwh) },
        { label: "Expected", num: true, get: u => kwh(u.expected) },
        { label: "Extra", num: true, get: u => h("strong", {}, "+" + kwh(u.extra)) },
        { label: "Temp", num: true, get: u => u.t != null ? `${u.t}°C` : "–" },
      ], unusual, { onClick: u => openDay(u.d) }) : h("div", { class: "empty" }, "Nothing unusual in the last 90 days 👍"),
      { span: "span-7" }));

    // peak-time share & always-on
    const ps = pk.peak_share;
    const bl = d.baseload;
    out.push(h("div", { class: "span-5 grid", style: { gap: "16px", alignContent: "start" } },
      ps ? h("section", { class: "card span-12" },
        h("h2", {}, "4pm–7pm share"),
        h("p", { class: "sub" }, "The pricey, high-carbon part of the day. Last 30 days of electricity."),
        h("div", { style: { display: "flex", alignItems: "baseline", gap: "8px", margin: "10px 0 8px" } },
          h("span", { style: { fontSize: "32px", fontWeight: 650 } }, `${Math.round(ps.kwh_share)}%`),
          h("span", { class: "muted small" }, `of your electricity · ${ps.cost_share != null ? Math.round(ps.cost_share) + "% of the cost" : ""}`)),
        h("div", { class: "meter" }, h("span", { style: { width: `${ps.kwh_share}%` } })),
        h("div", { class: "meter-legend" }, h("span", {}, "Those 3 hours are 12.5% of the day"), h("span", {}, "lower is better"))) : null,
      bl ? h("section", { class: "card span-12" },
        h("h2", {}, "Always-on power"),
        h("p", { class: "sub" }, "The least your home draws overnight – fridge, freezer, router, things on standby."),
        h("div", { class: "chips", style: { margin: "10px 0" } },
          h("div", { class: "chip" }, h("div", { class: "k" }, "Right now"), h("div", { class: "v" }, `${bl.watts} W`)),
          h("div", { class: "chip" }, h("div", { class: "k" }, "Per year"), h("div", { class: "v" }, money(bl.cost_year, 0)), h("div", { class: "d" }, kwh(bl.kwh_year, 0))),
          bl.share ? h("div", { class: "chip" }, h("div", { class: "k" }, "Share of your use"), h("div", { class: "v" }, `${bl.share}%`)) : null),
        chartBox(box => C.lines(box, {
          x: bl.monthly.map((_, i) => i), height: 140, yFmt: v => `${v} W`, yMin: 0,
          series: [{ name: "Always on", color: color("elec"), values: bl.monthly.map(m => m.watts) }],
          xTicks: bl.monthly.map((m, i) => i % 3 === 0 ? { x: i, label: monthLabel(m.month) + (m.month.endsWith("-01") ? ` ’${m.month.slice(2, 4)}` : "") } : null).filter(Boolean),
          xFmt: i => monthLabel(bl.monthly[i].month, true), vFmt: v => `${v} W`,
        })),
        h("p", { class: "note" }, "Every 10 W you switch off for good saves about ", money(10 / 1000 * 8760 * ((d.prices_now.elec || {}).unit_effective || 25) / 100, 0), " a year.")) : null));

    // top days
    const f = state.topFuel;
    const top = (pk.top_days[f] || []);
    out.push(card("Your biggest days", "Highest use on record · click for the hour-by-hour view",
      tableView([
        { label: "#", get: (_, i) => "" },
        { label: "Day", get: t => fmtDate(t.d) },
        { label: "Used", num: true, get: t => kwh(t.kwh) },
        { label: f === "export" ? "Earned" : "Cost", num: true, get: t => money(t.cost) },
        { label: "Temp", num: true, get: t => t.t != null ? `${t.t}°C` : "–" },
        { label: "", get: t => h("div", { class: "inline-bar", style: { width: `${t.kwh / top[0].kwh * 100}%`, background: color(f) } }) },
      ].filter(c => c.label !== "#"), top, { onClick: t => openDay(t.d) }),
      { span: "span-7", right: seg(d.fuels.map(x => [x, FUEL[x].name]), f, v => { state.topFuel = v; render(); }) }));

    if (pk.top_halfhours.length) {
      out.push(card("Biggest half-hours", "Electricity · average power over the half hour",
        tableView([
          { label: "When", get: t => `${fmtDate(t.d, { day: "numeric", month: "short", year: "numeric" })} ${t.time}` },
          { label: "Power", num: true, get: t => `${t.kw} kW` },
          { label: "Energy", num: true, get: t => kwh(t.kwh, 2).replace(".0 ", " ") },
          { label: "Price", num: true, get: t => pence(t.price) },
          { label: "Cost", num: true, get: t => money(t.cost) },
        ], pk.top_halfhours, { onClick: t => openDay(t.d) }), { span: "span-5" }));
    }

    // monthly peaks heat: highest day each month
    const byMonth = {};
    for (const x of d.daily) {
      if (x[f] == null) continue;
      const k = x.d.slice(0, 7);
      if (!byMonth[k] || x[f] > byMonth[k][f]) byMonth[k] = x;
    }
    const mk = Object.keys(byMonth).sort().slice(-24);
    out.push(card("Biggest day each month", `${FUEL[f].name} · last 24 months`,
      chartBox(box => C.bars(box, {
        labels: mk, height: 200, yFmt: v => nf0.format(v),
        series: [{ name: FUEL[f].name, color: color(f), values: mk.map(k => byMonth[k][f]) }],
        xTick: i => i % 2 ? null : monthLabel(mk[i]) + (mk[i].endsWith("-01") ? ` ’${mk[i].slice(2, 4)}` : ""),
        tooltip: i => ({ title: fmtDate(byMonth[mk[i]].d), rows: [{ color: color(f), value: kwh(byMonth[mk[i]][f]), label: money(byMonth[mk[i]][f + "_cost"]) }], note: "Click to open the day" }),
        onClick: i => openDay(byMonth[mk[i]].d),
      })), { span: "span-12" }));
    return out;
  }

  // ---------------------------------------------------------------- Bills & balance
  function bills(d) {
    const out = [];
    const p = d.payments;
    if (p) {
      out.push(h("div", { class: "span-12 grid", style: { gap: "16px" } }, balanceTile(p),
        p.octopus && p.octopus.suggested_dd != null ? tile("Octopus suggests paying", `${money(p.octopus.suggested_dd)}/month`,
          `minimum ${money(p.octopus.minimum_dd)} · our estimate ${money(p.min_typical, 0)}–${money(p.min_cautious, 0)}`, null, "span-6") : null));
      out.push(paymentPlanner(p));
    }
    const tx = d.transactions || [];
    if (tx.length) {
      const pts = tx.slice().reverse();
      out.push(card("Your balance over time", "After every charge, payment and credit · positive = in credit",
        chartBox(box => C.lines(box, {
          x: pts.map(t => d8(t.date).getTime() + pts.indexOf(t)), height: 220, yFmt: v => `${v < 0 ? "−" : ""}£${nf0.format(Math.abs(v))}`,
          series: [{ name: "Balance", color: C.css("--accent"), values: pts.map(t => t.balance), area: true }],
          xTicks: monthTicks(fillDays(pts.map(t => ({ d: t.date })))),
          tooltip: i => ({ title: fmtDate(pts[i].date), rows: [{ color: C.css("--accent"), value: money(pts[i].balance), label: "balance after" },
            { value: `${pts[i].type === "Charge" ? "−" : "+"}${money(Math.abs(pts[i].amount))}`, label: pts[i].title }] }),
        })), { span: "span-12" }));
    }
    const bc = d.bills;
    if (bc && bc.rows.length) {
      const fuelCell = (r, f) => {
        const v = r.fuels[f];
        if (!v) return "–";
        return h("div", {}, money(v.octopus), h("div", { class: "muted small" }, `ours ${money(v.ours)}`));
      };
      const flagged = bc.rows.filter(r => r.flag === "check");
      out.push(card("Checking Octopus's bills", `Each charge re-worked from your half-hourly readings and tariff prices. Across fully-metered periods Octopus charged ${money(bc.octopus_total)} and we calculate ${money(bc.ours_total)}.`,
        h("div", {},
          h("ul", { class: "insights", style: { marginBottom: "12px" } }, h("li", { class: flagged.length ? "warn" : "good" }, icon(flagged.length ? "warn" : "good"),
            h("span", {}, flagged.length ? `${flagged.length} bill${flagged.length > 1 ? "s" : ""} differ by more than 8% (and £1) from what your readings say – worth a look.`
              : "Every bill matches your meter readings (within 8% / £1). Small differences come from gas calorific values and rounding."))),
          tableView([
            { label: "Statement", get: r => fmtDate(r.issued, { day: "numeric", month: "short", year: "numeric" }) },
            { label: "Usage period", get: r => `${fmtDate(r.from, { day: "numeric", month: "short" })} – ${fmtDate(r.to, { day: "numeric", month: "short" })}` },
            ...["elec", "gas", "export"].filter(f => bc.rows.some(r => r.fuels[f])).map(f => ({ label: FUEL[f].name, num: true, get: r => fuelCell(r, f) })),
            { label: "Net charged", num: true, get: r => h("strong", {}, money(r.octopus_net)) },
            { label: "Our calculation", num: true, get: r => money(r.ours_net) },
            { label: "Difference", num: true, get: r => `${r.diff > 0 ? "+" : r.diff < 0 ? "−" : ""}${money(Math.abs(r.diff))}` },
            { label: "", get: r => r.flag === "ok" ? h("span", { class: "pill" }, "✓ matches") : r.flag === "gaps" ? h("span", { class: "pill" }, "readings missing") : h("span", { class: "pill blue" }, "check") },
          ], bc.rows),
          h("p", { class: "note" }, "Export credits are shown as amounts paid to you and subtracted in the net figures. ‘Readings missing’ means some half-hours in that period never arrived from the meter, so our figure is incomplete – Octopus will have estimated those.")),
        { span: "span-12" }));
    }
    if (tx.length) {
      const shown = state.allTx ? tx : tx.slice(0, 15);
      out.push(card("Transactions", `Everything on your account, newest first · ${tx.length} in total`, h("div", {}, tableView([
        { label: "Date", get: t => fmtDate(t.date, { day: "numeric", month: "short", year: "numeric" }) },
        { label: "What", get: t => h("span", {}, t.title, t.reversed ? h("span", { class: "pill", style: { marginLeft: "6px" } }, "reversed") : null) },
        { label: "Type", get: t => t.type === "Charge" && t.amount < 0 ? "Export credit" : t.type },
        { label: "Amount", num: true, get: t => t.type === "Charge" ? (t.amount < 0 ? h("span", { class: "delta good" }, `+${money(-t.amount)}`) : `−${money(t.amount)}`) : h("span", { class: "delta good" }, `+${money(t.amount)}`) },
        { label: "Balance after", num: true, get: t => money(t.balance) },
      ], shown), tx.length > 15 ? h("div", { style: { marginTop: "10px" } }, h("button", { class: "btn", type: "button",
        onclick: () => { state.allTx = !state.allTx; render(); } }, state.allTx ? "Show fewer" : `Show all ${tx.length}`)) : null), { span: "span-12" }));
    }
    if (!out.length) out.push(card("No account data yet", null, h("div", { class: "empty" }, d.demo ? "Bills and balance need your own Octopus account – not available in demo mode." : "These appear after the next sync.")));
    return out;
  }

  // ---------------------------------------------------------------- Prices & tariffs
  function prices(d) {
    const out = [];
    if (d.renewal) out.push(renewalCard(d.renewal));
    if (d.price_history) out.push(historyCard(d.price_history));
    for (const f of Object.keys(FUEL).filter(x => d.prices_now[x])) {
      const p = d.prices_now[f];
      out.push(h("section", { class: "card span-4 tariff-card" },
        h("div", {}, h("i", { class: "dot-key", vars: { "--k": color(f) } }), h("span", { class: "muted small" }, FUEL[f].name)),
        h("div", { class: "name" }, p.name),
        h("div", { class: "muted small" }, p.tariff),
        endsNote(p),
        h("div", { class: "chips", style: { marginTop: "6px" } },
          h("div", { class: "chip" }, h("div", { class: "k" }, f === "export" ? "Paid to you" : "Unit rate now"), h("div", { class: "v" }, `${pence(p.unit_now)}/kWh`)),
          p.unit_effective != null ? h("div", { class: "chip" }, h("div", { class: "k" }, "Your average (30 days)"), h("div", { class: "v" }, `${pence(p.unit_effective)}/kWh`)) : null,
          f !== "export" ? h("div", { class: "chip" }, h("div", { class: "k" }, "Standing charge"), h("div", { class: "v" }, `${pence(p.standing)}/day`), h("div", { class: "d" }, `${money(p.standing * 365 / 100, 0)} a year`)) : null)));
    }

    const ag = d.agile;
    const agileCards = [];
    if (ag && ag.slots.length) {
      const nowTs = Date.now() / 1000;
      const slots = ag.slots;
      const nowIdx = slots.findIndex(s => s.ts <= nowTs && nowTs < s.ts + 1800);
      const cheapest = ag.cheapest || [];
      agileCards.push(card(ag.on_agile ? "Agile prices today & tomorrow" : "Agile prices in your area",
        (ag.on_agile ? "" : "You're not on Agile – this is what you'd pay. ") + "Tomorrow's prices usually appear around 4pm.",
        h("div", {},
          chartBox(box => C.bars(box, {
            labels: slots.map(s => s.ts), height: 240, yFmt: v => `${v}p`,
            series: [{ name: "Price", color: color("elec"), values: slots.map(s => s.p) }], maxBar: 10,
            nowLine: nowIdx >= 0 ? nowIdx : null, nowLabel: "now",
            xTick: i => new Date(slots[i].ts * 1000).getMinutes() === 0 && new Date(slots[i].ts * 1000).getHours() % 6 === 0 ? (new Date(slots[i].ts * 1000).getHours() === 0 ? new Date(slots[i].ts * 1000).toLocaleDateString("en-GB", { weekday: "short" }) : timeOf(slots[i].ts)) : null,
            tooltip: i => ({ title: `${dayTime(slots[i].ts)}–${timeOf(slots[i].ts + 1800)}`, rows: [{ color: color("elec"), value: `${slots[i].p.toFixed(2)}p/kWh`, label: slots[i].p < 0 ? "you get paid to use power!" : "" }] }),
          })),
          h("div", { class: "chips", style: { marginTop: "12px" } }, cheapest.map(c =>
            h("div", { class: "chip" }, h("div", { class: "k" }, `Cheapest ${c.hours} h from now`), h("div", { class: "v" }, `${timeOf(c.ts)}–${timeOf(c.ts + c.hours * 3600)}`),
              h("div", { class: "d" }, `${c.ts * 1000 > Date.now() + 86400000 / 2 ? new Date(c.ts * 1000).toLocaleDateString("en-GB", { weekday: "short" }) + " · " : ""}avg ${pence(c.avg)}/kWh`))),
            ag.today_avg != null ? h("div", { class: "chip" }, h("div", { class: "k" }, "Today's average"), h("div", { class: "v" }, `${pence(ag.today_avg)}/kWh`)) : null),
          ag.negative.length ? h("p", { class: "note" }, `⚡ ${ag.negative.length} upcoming half-hour${ag.negative.length > 1 ? "s" : ""} with negative prices – you'd be paid to use electricity.`) : null,
          ag.your_avg != null ? h("p", { class: "note" }, `Last 30 days you paid ${pence(ag.your_avg)}/kWh on average; using power evenly through the day would have cost ${pence(ag.market_avg)}/kWh.`) : null),
        { span: "span-12" }));
    }

    if (ag && ag.on_agile) out.push(...agileCards);

    // effective price per month
    const bills = d.fuels.filter(f => f !== "export");
    const pm = d.monthly.filter(m => m.status !== "future" && bills.some(f => m[f].ppk != null));
    if (pm.length > 1) {
      out.push(card("What you actually paid per kWh", "Unit costs ÷ kWh used each month (excludes standing charges)",
        chartBox(box => C.lines(box, {
          x: pm.map((_, i) => i), height: 220, yFmt: v => `${v}p`, yMin: 0,
          series: bills.map(f => ({ name: FUEL[f].name, color: color(f), values: pm.map(m => m[f].ppk) })),
          xTicks: pm.map((m, i) => i % 3 === 0 ? { x: i, label: monthLabel(m.month) + ` ’${m.month.slice(2, 4)}` } : null).filter(Boolean),
          xFmt: i => monthLabel(pm[i].month, true), vFmt: v => `${v.toFixed(1)}p/kWh`,
        })), { span: "span-6", right: legend(bills.map(f => ({ color: color(f), kind: "line", label: FUEL[f].name }))) }));
    }

    // tariff history
    const th = d.tariff_history;
    if (th.length) {
      const t0 = Math.min(...th.map(t => t.from)), t1 = Date.now() / 1000;
      const fuels = [...new Set(th.map(t => t.fuel))];
      out.push(card("Tariff history", `Since ${new Date(t0 * 1000).toLocaleDateString("en-GB", { month: "short", year: "numeric" })}`,
        h("div", { class: "timeline" }, fuels.map(f => h("div", { class: "row" }, h("span", { class: "muted" }, FUEL[f] ? FUEL[f].name : f),
          h("div", { class: "track" }, th.filter(t => t.fuel === f).map(t => {
            const a = Math.max(t.from, t0), b = Math.min(t.to || t1, t1);
            return h("div", { class: "seg-bar", title: `${t.name} · ${t.tariff}`, style: { left: `${(a - t0) / (t1 - t0) * 100}%`, width: `calc(${(b - a) / (t1 - t0) * 100}% - 2px)`, background: color(f) } }, t.name);
          }))))), { span: "span-6" }));
    }

    // comparison
    const cmp = d.compare;
    if (cmp) {
      const max = Math.max(cmp.yours, ...cmp.rows.map(r => r.total));
      out.push(card("Would another tariff be cheaper?",
        `Your real half-hourly use from ${fmtDate(cmp.from, { day: "numeric", month: "short" })} to ${fmtDate(cmp.to, { day: "numeric", month: "short" })} (${cmp.days} days), priced on each tariff. Tariffs without gas keep your current gas price.`,
        h("div", {},
          tableView([
            { label: "Tariff", get: r => h("div", {}, h("strong", {}, r.name), r.current ? h("span", { class: "pill blue", style: { marginLeft: "6px" } }, "your tariff") : null, h("div", { class: "muted small" }, r.blurb)) },
            { label: "Electricity", num: true, get: r => r.elec != null ? money(r.elec) : "yours" },
            { label: "Gas", num: true, get: r => cmp.yours_gas ? (r.gas != null ? money(r.gas) : "yours") : "–" },
            { label: "Total", num: true, get: r => h("strong", {}, money(r.total)) },
            { label: "vs what you paid", num: true, get: r => h("span", { class: r.diff < -0.5 ? "delta good" : r.diff > 0.5 ? "delta bad" : "" }, Math.abs(r.diff) < 0.5 ? "same" : `${r.diff < 0 ? "−" : "+"}${money(Math.abs(r.diff))}`) },
            { label: "Per year", num: true, get: r => Math.abs(r.diff_year) < 1 ? "–" : `${r.diff_year < 0 ? "save" : "+"} ${money(Math.abs(r.diff_year), 0)}` },
            { label: "", get: r => h("div", { style: { width: "120px" } }, h("div", { class: "inline-bar" + (r.yoursRow ? " yours" : ""), style: { width: `${r.total / max * 100}%` } })) },
          ], [{ name: "What you actually paid", blurb: "Your real bills for these days", elec: cmp.yours_elec, gas: cmp.yours_gas, total: cmp.yours, diff: 0, diff_year: 0, yoursRow: true }, ...cmp.rows], { rowClass: r => r.yoursRow ? "current" : "" }),
          h("p", { class: "note" }, "Fixed tariffs that started recently are priced at today's fixed rate for the whole period (", h("em", {}, "estimated"), "). Octopus Go and Cosy only make sense with an EV or heat pump. The ‘per year’ figure scales these days up and ignores the seasons – treat it as a rough guide."),
          cmp.rows.some(r => r.estimated_history) ? h("p", { class: "note" }, "Estimated (today's price used for earlier days): " + cmp.rows.filter(r => r.estimated_history).map(r => r.name).join(", ") + ".") : null),
        { span: "span-12" }));
    }
    if (!(ag && ag.on_agile)) out.push(...agileCards);
    return out;
  }

  function renewalCard(rn) {
    const fuels = Object.keys(rn.kwh);
    const best = rn.options.find(o => o.total != null && o.kind !== "current");
    const cell = x => x ? h("div", {}, h("strong", {}, money(x.cost, 0)), h("div", { class: "muted small" }, `${pence(x.unit)}/kWh${x.average ? " avg" : ""} · ${pence(x.standing)}/day`)) : "–";
    return card("When your fix ends", null, h("div", {},
      h("div", { class: "chips", style: { marginBottom: "12px" } },
        h("div", { class: "chip" }, h("div", { class: "k" }, "Your fix ends"), h("div", { class: "v" }, fmtDate(rn.ends, { day: "numeric", month: "short", year: "numeric" })), h("div", { class: "d" }, `${rn.days_left} days to go`)),
        h("div", { class: "chip" }, h("div", { class: "k" }, "Can you renew yet?"), h("div", { class: "v" }, rn.can_renew ? "Yes – open now" : "Not yet"), h("div", { class: "d" }, rn.can_renew ? "check the Octopus app for your offers" : "Octopus opens renewals in the weeks before your end date")),
        h("div", { class: "chip" }, h("div", { class: "k" }, "Predicted use, 12 months after"), h("div", { class: "v" }, fuels.map(f => kwh(rn.kwh[f], 0)).join(" + ")), h("div", { class: "d" }, fuels.map(f => FUEL[f].name.toLowerCase()).join(" + "))),
        best ? h("div", { class: "chip", style: { boxShadow: "0 0 0 2px var(--accent)" } }, h("div", { class: "k" }, "Cheapest option today"), h("div", { class: "v" }, best.name), h("div", { class: "d" }, `${money(best.total, 0)} for the year`)) : null),
      tableView([
        { label: "Option", get: o => h("div", {}, h("strong", {}, o.name), h("div", { class: "muted small" },
          o.kind === "rollover" ? "what happens if you do nothing" : o.kind === "tracker" ? "daily wholesale price – average of the last 90 days" :
          o.kind === "current" ? "for reference – what you pay now" : `${o.term ? o.term + " months" : "fixed"}${o.exit_fee ? ` · ${money(o.exit_fee / 100, 0)} exit fee per fuel` : " · no exit fees"}`)) },
        ...fuels.map(f => ({ label: FUEL[f].name, num: true, get: o => cell(o[f]) })),
        { label: "12 months", num: true, get: o => o.total != null ? h("strong", {}, money(o.total, 0)) : "–" },
        { label: "vs doing nothing", num: true, get: o => o.vs_rollover == null ? "–" : Math.abs(o.vs_rollover) < 1 ? "–" : h("span", { class: o.vs_rollover < 0 ? "delta good" : "delta bad" }, `${o.vs_rollover < 0 ? "−" : "+"}${money(Math.abs(o.vs_rollover), 0)}`) },
      ], rn.options, { rowClass: o => o.kind === "current" ? "future" : "" }),
      h("p", { class: "note" }, `Priced on your predicted use for the 12 months from ${fmtDate(rn.ends, { day: "numeric", month: "short" })}, using each tariff's price today. Fixed prices change almost daily with the wholesale market, so re-check when renewal opens – Octopus may also offer you loyalty deals in the app that aren't public.`)),
      { span: "span-12" });
  }

  function historyCard(ph) {
    const f = state.histFuel && ph.fuels[state.histFuel] ? state.histFuel : Object.keys(ph.fuels)[0];
    const data = ph.fuels[f];
    const names = ph.names;
    const allTot = Object.values(ph.fuels).reduce((a, x) => { for (const k in x.totals) a[k] = (a[k] || 0) + x.totals[k]; return a; }, {});
    const lineColor = { yours: C.css("--temp"), "Flexible Octopus": C.css("--muted"), "Octopus Tracker": C.css("--summer") };
    const ms = data.months;
    const series = [{ name: "What you paid", color: lineColor.yours, values: ms.map(m => m.yours_ppk) },
      ...names.map(n => ({ name: n, color: lineColor[n] || C.css("--muted"), values: ms.map(m => m[n + "_ppk"]), dash: n === "Flexible Octopus" ? "5 4" : null }))];
    return card("Did fixing pay off?", `Your real half-hourly use since ${monthLabel(ph.since, true)}, priced on each tariff at the prices of the time (including standing charges).`,
      h("div", {},
        h("div", { class: "chips", style: { marginBottom: "12px" } },
          h("div", { class: "chip" }, h("div", { class: "k" }, "You paid (electricity + gas)"), h("div", { class: "v" }, money(allTot.yours, 0))),
          ...names.map(n => {
            const diff = allTot[n] - allTot.yours;
            return h("div", { class: "chip" }, h("div", { class: "k" }, `On ${n}`), h("div", { class: "v" }, money(allTot[n], 0)),
              h("div", { class: `d ${diff > 0 ? "delta good" : "delta bad"}`, style: { marginTop: 0 } }, diff > 0 ? `fixing saved you ${money(diff, 0)}` : `would have saved ${money(-diff, 0)}`));
          })),
        chartBox(box => C.lines(box, {
          x: ms.map((_, i) => i), height: 220, yFmt: v => `${v}p`, yMin: 0,
          series, xTicks: ms.map((m, i) => ({ x: i, label: monthLabel(m.month) })),
          tooltip: i => ({ title: monthLabel(ms[i].month, true) + ` · ${kwh(ms[i].kwh, 0)}`,
            rows: [{ color: lineColor.yours, value: `${pence(ms[i].yours_ppk)} · ${money(ms[i].yours)}`, label: "you" },
              ...names.map(n => ({ color: lineColor[n], value: `${pence(ms[i][n + "_ppk"])} · ${money(ms[i][n])}`, label: n }))] }),
        })),
        h("div", { style: { marginTop: "8px" } }, legend(series.map(sr => ({ color: sr.color, kind: sr.dash ? "dash" : "line", label: sr.name })))),
        h("p", { class: "note" }, "Lines show the average unit price per kWh each month; the £ figures in the tooltip include standing charges.")),
      { span: "span-12", right: seg(Object.keys(ph.fuels).map(x => [x, FUEL[x].name]), f, v => { state.histFuel = v; render(); }) });
  }

  // ---------------------------------------------------------------- Weather, carbon & solar
  function planet(d) {
    const out = [];
    const wx = d.weather;
    if (wx) {
      const wxFuels = Object.keys(FUEL).filter(x => x in wx.fuels);
      const f = wxFuels.includes(state.wxFuel) ? state.wxFuel : wxFuels[0];
      const w = wx.fuels[f];
      out.push(card("How the weather drives your use", "Each dot is a day from the last 12 months. The line is what we'd expect on a weekday.",
        h("div", {},
          chartBox(box => C.scatter(box, {
            points: w.points, color: color(f), line: w.line, height: 300, xLabel: "Average outdoor temperature (°C)", xFmt: v => `${v}°`, yFmt: v => nf0.format(v),
            tooltip: p => ({ title: fmtDate(p[2]), rows: [{ color: color(f), value: kwh(p[1]), label: `at ${p[0]}°C` }], note: "Click to open the day" }),
            onClick: p => openDay(p[2]),
          })),
          w.slope != null ? h("p", { class: "note" }, f === "export"
            ? `Warmer days tend to be sunnier: each degree warmer is worth about ${nf1.format(-w.slope)} kWh of export.`
            : `Each degree colder adds about ${nf1.format(w.slope)} kWh a day${w.slope_cost ? ` (≈ ${(w.slope_cost * 100).toFixed(0)}p)` : ""}. The temperature explains ${Math.round((w.r2 || 0) * 100)}% of your day-to-day ${FUEL[f].name.toLowerCase()} use.`
            + (w.provisional ? " (Using a typical figure until there's a winter's data.)" : "")) : null),
        { span: "span-7", right: seg(wxFuels.map(x => [x, FUEL[x].name]), f, v => { state.wxFuel = v; render(); }) }));
      // heating degree comparison: monthly temp vs normal
      const ms = d.monthly.filter(m => m.status !== "future" && m.temp != null).slice(-18);
      out.push(card("Warmer or colder than normal?", "Monthly average temperature vs the 6-year normal",
        chartBox(box => C.bars(box, {
          labels: ms.map(m => m.month), height: 300, yFmt: v => `${v > 0 ? "+" : ""}${v}°`,
          stacked: true,
          series: [{ name: "Warmer", color: C.css("--warm"), values: ms.map(m => m.normal_temp != null ? Math.max(0, +(m.temp - m.normal_temp).toFixed(1)) : 0) },
            { name: "Colder", color: C.css("--elec"), values: ms.map(m => m.normal_temp != null ? Math.min(0, +(m.temp - m.normal_temp).toFixed(1)) : 0) }],
          xTick: i => i % 3 ? null : monthLabel(ms[i].month) + ` ’${ms[i].month.slice(2, 4)}`,
          tooltip: i => ({ title: monthLabel(ms[i].month, true), rows: [{ color: C.css(ms[i].temp >= ms[i].normal_temp ? "--warm" : "--elec"), value: `${ms[i].temp}°C`, label: `normal ${ms[i].normal_temp}°C (${ms[i].temp >= ms[i].normal_temp ? "+" : ""}${(ms[i].temp - ms[i].normal_temp).toFixed(1)}°)` }] }),
        })), { span: "span-5", right: legend([{ color: C.css("--warm"), label: "Warmer" }, { color: C.css("--elec"), label: "Colder" }]) }));
    }

    const cb = d.carbon;
    if (cb) {
      const totalKg = (cb.year_kg.elec || 0) + (cb.year_kg.gas || 0);
      out.push(h("div", { class: "span-12 grid", style: { gap: "16px" } },
        tile("Carbon footprint · 12 months", `${(totalKg / 1000).toFixed(1)} t CO₂`,
          `${nf0.format(cb.year_kg.elec || 0)} kg from electricity · ${nf0.format(cb.year_kg.gas || 0)} kg from gas`, null, "span-4"),
        tile("Like driving", `${nf0.format(totalKg / 0.17)} km`, "in an average petrol car", null, "span-4"),
        cb.your_intensity ? tile("Your electricity’s carbon", `${cb.your_intensity} g/kWh`,
          `the GB grid averaged ${cb.grid_intensity} g/kWh over the same 28 days` + (cb.regional_avg != null && cb.basis === "national" ? ` · your region's own generation: ${cb.regional_avg} g/kWh` : ""),
          deltaEl(cb.your_intensity, cb.grid_intensity, { suffix: "vs grid average" }), "span-4")
          : tile("Your electricity’s carbon", "–", "grid data not available", null, "span-4")));
      if (cb.daily.length) {
        out.push(card("Daily CO₂", "Last 28 days · electricity uses the real half-hourly carbon intensity of the GB grid",
          h("div", {}, chartBox(box => C.bars(box, {
            labels: cb.daily.map(x => x.d), height: 220, stacked: true, yFmt: v => `${v} kg`,
            series: [{ name: "Electricity", color: color("elec"), values: cb.daily.map(x => x.elec) }, { name: "Gas", color: color("gas"), values: cb.daily.map(x => x.gas) }],
            xTick: i => i % 7 ? null : d8(cb.daily[i].d).toLocaleDateString("en-GB", { day: "numeric", month: "short" }),
            tooltip: i => ({ title: fmtDate(cb.daily[i].d), rows: [{ color: color("elec"), value: `${cb.daily[i].elec} kg`, label: "electricity" }, { color: color("gas"), value: `${cb.daily[i].gas} kg`, label: "gas" }] }),
            onClick: i => openDay(cb.daily[i].d),
          })), h("div", { style: { marginTop: "8px" } }, legend([{ color: color("elec"), label: "Electricity" }, { color: color("gas"), label: "Gas" }]))),
          { span: "span-6" }));
      }
      if (cb.forecast.length) {
        const fc = cb.forecast;
        const g = cb.greenest;
        const gi = g ? fc.findIndex(x => x.ts === g.ts) : -1;
        out.push(card("Greenest time to use power", g ? `Next 48 hours · cleanest 3 hours start ${dayTime(g.ts)} (${g.avg} g/kWh)` : "Next 48 hours",
          chartBox(box => C.lines(box, {
            x: fc.map(x => x.ts), height: 220, yFmt: v => `${v}`, yMin: 0,
            series: [{ name: "Grid carbon intensity", color: C.css("--carbon"), values: fc.map(x => x.g), area: true }],
            shade: gi >= 0 ? [{ from: fc[gi].ts, to: fc[Math.min(fc.length - 1, gi + 6)].ts }] : null,
            xTicks: fc.filter(x => new Date(x.ts * 1000).getMinutes() === 0 && new Date(x.ts * 1000).getHours() % 6 === 0).map(x => ({ x: x.ts, label: new Date(x.ts * 1000).getHours() === 0 ? new Date(x.ts * 1000).toLocaleDateString("en-GB", { weekday: "short" }) : timeOf(x.ts) })),
            xFmt: ts => dayTime(ts), vFmt: v => `${v} gCO₂/kWh`,
          })), { span: "span-6" }));
      }
    }

    const sol = d.solar;
    if (sol && sol.months.length) {
      const ms = sol.months;
      const alt = sol.alternatives[0];
      const series = [{ name: "What you earned (Outgoing)", color: color("export"), values: ms.map(m => m.earned) },
        ...(alt ? [{ name: alt.name, color: C.css("--temp"), values: ms.map(m => m[alt.name] || 0) }] : [])];
      out.push(card("Your solar panels", `Since ${monthLabel(sol.since, true)}`,
        h("div", {},
          h("div", { class: "chips", style: { marginBottom: "12px" } },
            h("div", { class: "chip" }, h("div", { class: "k" }, "Exported"), h("div", { class: "v" }, kwh(sol.export_kwh, 0)), h("div", { class: "d" }, `earned ${money(sol.earned)}`)),
            alt ? h("div", { class: "chip" }, h("div", { class: "k" }, `On ${alt.name}`), h("div", { class: "v" }, money(alt.earned)),
              h("div", { class: `d ${alt.earned > sol.earned ? "delta bad" : "delta good"}`, style: { marginTop: 0 } },
                alt.earned > sol.earned ? `${money(alt.earned - sol.earned)} more than you got` : `${money(sol.earned - alt.earned)} less – your tariff is better`)) : null),
          chartBox(box => C.bars(box, {
            labels: ms.map(m => m.month), height: 220, yFmt: v => `£${v}`, series,
            xTick: i => monthLabel(ms[i].month),
            tooltip: i => ({ title: monthLabel(ms[i].month, true), rows: [
              ...series.map(sr => ({ color: sr.color, value: money(sr.values[i]), label: sr.name })),
              { value: kwh(ms[i].export_kwh, 0), label: "exported" }] }),
          })),
          h("div", { style: { marginTop: "8px" } }, legend(series.map(sr => ({ color: sr.color, label: sr.name }))))),
        { span: "span-12" }));
    }
    if (!out.length) out.push(card("Nothing here yet", null, h("div", { class: "empty" }, "Weather and carbon data appear after the first sync.")));
    return out;
  }

  // ---------------------------------------------------------------- Data
  function about(d) {
    const out = [];
    const q = d.quality;
    out.push(card("Your data", null, tableView([
      { label: "Fuel", get: r => h("span", {}, h("i", { class: "dot-key", vars: { "--k": color(r.f) } }), FUEL[r.f].name) },
      { label: "From", get: r => fmtDate(r.first) },
      { label: "To", get: r => fmtDate(r.last) },
      { label: "Days", num: true, get: r => nf0.format(r.days) },
      { label: "Incomplete days (last year)", num: true, get: r => r.missing_365 },
      { label: "Octopus's yearly estimate", num: true, get: r => {
        const m = (d.account.meters || {})[r.f];
        return m && m.estimate_kwh ? kwh(m.estimate_kwh, 0) : "–";
      } },
      { label: "Your last 12 months", num: true, get: r => kwh(d.kpis.per_fuel[r.f].kwh_365, 0) },
    ], Object.entries(q).map(([f, v]) => Object.assign({ f }, v))), { span: "span-12" }));
    out.push(card("Settings in use", null, h("dl", { class: "kv" },
      h("dt", {}, "Region"), h("dd", {}, d.account.region || "–"),
      h("dt", {}, "Area (for weather & carbon)"), h("dd", {}, d.account.outcode || "–"),
      h("dt", {}, "Gas meter units"), h("dd", {}, d.account.gas_units === "m3" ? "m³ (converted to kWh: × 1.02264 × 39.5 ÷ 3.6)" : d.account.gas_units || "–"),
      h("dt", {}, "Last sync"), h("dd", {}, d.account.last_sync ? new Date(d.account.last_sync * 1000).toLocaleString("en-GB") : "–"),
      h("dt", {}, "Dashboard built in"), h("dd", {}, `${d.build_seconds}s`)), { span: "span-6" }));
    out.push(card("Where the numbers come from", null, h("ul", { class: "small", style: { margin: 0, paddingLeft: "18px" } },
      h("li", {}, "Half-hourly meter readings, your tariffs and their prices: the Octopus Energy API."),
      h("li", {}, "Temperatures: Open-Meteo (free weather service), for the area of your postcode only."),
      h("li", {}, "Grid carbon intensity: the National Grid ESO Carbon Intensity API for your region."),
      h("li", {}, "Gas CO₂: 0.183 kg per kWh (UK government conversion factor)."),
      h("li", {}, "Smart meter readings usually arrive a day late, so ‘today’ is always predicted.")), { span: "span-6" }));
    return out;
  }

  // ======================================================================
  // Day drawer
  // ======================================================================
  async function openDay(ds) {
    const dr = $("#drawer"), back = $("#drawer-back");
    dr.replaceChildren(h("div", { class: "loading" }, "Loading…"));
    dr.classList.add("on"); back.classList.add("on"); dr.setAttribute("aria-hidden", "false");
    const day = await (await fetch(`/api/day/${ds}`)).json();
    const fuels = Object.keys(day.totals).sort((a, b) => Object.keys(FUEL).indexOf(a) - Object.keys(FUEL).indexOf(b));
    const slots = day.slots;
    const labels = slots.map(s => s.time);
    const tick = i => i % 8 === 0 ? labels[i] : null;
    const prevNext = n => { const t = d8(ds); t.setDate(t.getDate() + n); return t.toISOString().slice(0, 10); };
    const localDrawers = [];
    const box = draw => { const b = h("div", { class: "chart-box" }); localDrawers.push(() => draw(b)); return b; };
    const kids = [
      h("div", { class: "drawer-head" },
        h("div", {}, h("h2", {}, fmtDate(ds, { weekday: "long", day: "numeric", month: "long", year: "numeric" })),
          h("div", { class: "muted small" }, day.t != null ? `Average ${day.t.toFixed(1)}°C` : "")),
        h("button", { class: "btn", onclick: () => openDay(prevNext(-1)), "aria-label": "Previous day" }, "‹"),
        h("button", { class: "btn", onclick: () => openDay(prevNext(1)), "aria-label": "Next day" }, "›"),
        h("button", { class: "btn", onclick: closeDay }, "Close")),
      h("div", { class: "chips", style: { marginTop: "14px" } }, fuels.map(f => h("div", { class: "chip" },
        h("div", { class: "k" }, h("i", { class: "dot-key", vars: { "--k": color(f) } }), FUEL[f].name),
        h("div", { class: "v" }, kwh(day.totals[f].kwh)),
        h("div", { class: "d" }, `${money(day.totals[f].cost)}${day.totals[f].standing ? ` incl. ${money(day.totals[f].standing)} standing` : f === "export" ? " earned" : ""}`)))),
    ];
    for (const f of fuels) {
      if (!slots.some(s => s[f] != null)) continue;
      kids.push(h("section", { class: "card" }, h("h2", {}, FUEL[f].name), h("p", { class: "sub" }, "kWh per half hour"),
        box(b => C.bars(b, {
          labels, height: 170, yFmt: v => v.toFixed(1), series: [{ name: FUEL[f].name, color: color(f), values: slots.map(s => s[f]) }], xTick: tick, maxBar: 12,
          tooltip: i => ({ title: `${labels[i]}–${labels[(i + 1) % 48]}`, rows: [{ color: color(f), value: kwh(slots[i][f], 2).replace(".0 ", " "), label: `${(slots[i][f] * 2).toFixed(2)} kW avg` },
            slots[i][f + "_p"] != null ? { value: `${slots[i][f + "_p"].toFixed(1)}p/kWh`, label: `· ${money(slots[i][f + "_cost"])}` } : null].filter(Boolean) }),
        }))));
    }
    if (slots.some(s => s.elec_p != null) && new Set(slots.map(s => s.elec_p)).size > 2) {
      kids.push(h("section", { class: "card" }, h("h2", {}, "Electricity price"), h("p", { class: "sub" }, "p/kWh on your tariff that day"),
        box(b => C.lines(b, { x: slots.map((_, i) => i), height: 150, yFmt: v => `${v}p`,
          series: [{ name: "Price", color: color("elec"), values: slots.map(s => s.elec_p) }],
          xTicks: [0, 8, 16, 24, 32, 40].map(i => ({ x: i, label: labels[i] })), xFmt: i => labels[i], vFmt: v => `${v.toFixed(1)}p/kWh` }))));
    }
    if (slots.some(s => s.carbon != null)) {
      kids.push(h("section", { class: "card" }, h("h2", {}, "Grid carbon intensity"), h("p", { class: "sub" }, "gCO₂ per kWh in your region"),
        box(b => C.lines(b, { x: slots.map((_, i) => i), height: 150, yFmt: v => `${v}`, yMin: 0,
          series: [{ name: "Carbon", color: C.css("--carbon"), values: slots.map(s => s.carbon) }],
          xTicks: [0, 8, 16, 24, 32, 40].map(i => ({ x: i, label: labels[i] })), xFmt: i => labels[i], vFmt: v => `${v} g/kWh` }))));
    }
    dr.replaceChildren(...kids);
    dr.scrollTop = 0;
    requestAnimationFrame(() => localDrawers.forEach(fn => fn()));
  }
  function closeDay() {
    $("#drawer").classList.remove("on"); $("#drawer-back").classList.remove("on");
    $("#drawer").setAttribute("aria-hidden", "true");
    C.hideTip();
  }

  // ======================================================================
  // Wiring
  // ======================================================================
  document.querySelectorAll("#tabs button").forEach(b => b.addEventListener("click", () => setTab(b.dataset.tab)));
  $("#drawer-back").addEventListener("click", closeDay);
  document.addEventListener("keydown", e => { if (e.key === "Escape") closeDay(); });
  $("#sync-btn").addEventListener("click", startSync);
  $("#theme-btn").addEventListener("click", () => {
    const root = document.documentElement;
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("theme", root.dataset.theme); } catch (e) { /* private mode */ }
    render();
  });
  try { const t = localStorage.getItem("theme"); if (t) document.documentElement.dataset.theme = t; } catch (e) { /* ignore */ }
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", render);
  let rt;
  window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(redraw, 120); });
  window.addEventListener("hashchange", () => setTab(location.hash.slice(1)));
  state.tab = TABS[location.hash.slice(1)] ? location.hash.slice(1) : "overview";
  load();
  pollSync();
  setInterval(() => { if (state.data && !state.data.demo) $("#sync-status").textContent = `Updated ${ago(state.data.account.last_sync)}`; }, 60000);
})();
