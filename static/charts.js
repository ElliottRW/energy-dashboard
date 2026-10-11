// Small hand-made SVG charts: bars, lines, scatter, calendar and grid heatmaps.
// Colours come from CSS variables so light/dark mode just works (charts re-draw on change).
(function () {
  const NS = "http://www.w3.org/2000/svg";
  let uid = 0;

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function s(tag, attrs, parent) {
    const n = document.createElementNS(NS, tag);
    for (const k in attrs || {}) if (attrs[k] !== undefined && attrs[k] !== null) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }

  function niceStep(range, count) {
    const raw = range / Math.max(1, count);
    const mag = Math.pow(10, Math.floor(Math.log10(raw || 1)));
    const norm = raw / mag;
    const step = norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10;
    return step * mag;
  }

  function ticks(min, max, count) {
    if (max === min) max = min + 1;
    const step = niceStep(max - min, count);
    const lo = Math.floor(min / step) * step;
    const hi = Math.ceil(max / step) * step;
    const out = [];
    for (let v = lo; v <= hi + step / 2; v += step) out.push(+v.toFixed(10));
    return out;
  }

  // ---------- tooltip (one for the whole page) ----------
  const tip = document.createElement("div");
  tip.className = "tooltip";
  tip.setAttribute("role", "status");
  document.addEventListener("DOMContentLoaded", () => document.body.appendChild(tip));

  function showTip(evt, content) {
    tip.replaceChildren();
    if (content.title) {
      const t = document.createElement("div");
      t.className = "tt-title";
      t.textContent = content.title;
      tip.appendChild(t);
    }
    for (const row of content.rows || []) {
      const r = document.createElement("div");
      r.className = "tt-row";
      if (row.color) {
        const k = document.createElement("span");
        k.className = "tt-key" + (row.hatch ? " hatch" : "");
        k.style.setProperty("--k", row.color);
        r.appendChild(k);
      }
      const v = document.createElement("strong");
      v.textContent = row.value;
      r.appendChild(v);
      if (row.label) {
        const l = document.createElement("span");
        l.className = "tt-label";
        l.textContent = row.label;
        r.appendChild(l);
      }
      tip.appendChild(r);
    }
    if (content.note) {
      const n = document.createElement("div");
      n.className = "tt-note";
      n.textContent = content.note;
      tip.appendChild(n);
    }
    tip.classList.add("on");
    const pad = 14;
    const w = tip.offsetWidth, h = tip.offsetHeight;
    let x = evt.clientX + pad, y = evt.clientY + pad;
    if (x + w > window.innerWidth - 8) x = evt.clientX - w - pad;
    if (y + h > window.innerHeight - 8) y = evt.clientY - h - pad;
    tip.style.transform = `translate(${Math.max(4, x)}px, ${Math.max(4, y)}px)`;
  }

  function hideTip() { tip.classList.remove("on"); }

  function frame(container, height, margin) {
    container.replaceChildren();
    const width = Math.max(260, container.clientWidth);
    const svg = s("svg", { width, height, viewBox: `0 0 ${width} ${height}`, class: "chart", role: "img" }, container);
    const m = Object.assign({ top: 12, right: 12, bottom: 26, left: 46 }, margin || {});
    return { svg, width, height, m, pw: width - m.left - m.right, ph: height - m.top - m.bottom };
  }

  function hatch(svg, color, id) {
    let defs = svg.querySelector("defs") || s("defs", {}, svg);
    const p = s("pattern", { id, width: 6, height: 6, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" }, defs);
    s("rect", { width: 6, height: 6, fill: color, "fill-opacity": 0.22 }, p);
    s("line", { x1: 0, y1: 0, x2: 0, y2: 6, stroke: color, "stroke-width": 2.2 }, p);
    return `url(#${id})`;
  }

  function yAxis(f, ys, scale, fmt) {
    const g = s("g", { class: "axis" }, f.svg);
    for (const v of ys) {
      const y = scale(v);
      s("line", { x1: f.m.left, x2: f.width - f.m.right, y1: y, y2: y, class: v === 0 ? "baseline" : "grid" }, g);
      const t = s("text", { x: f.m.left - 6, y: y + 4, "text-anchor": "end", class: "tick" }, g);
      t.textContent = fmt ? fmt(v) : v;
    }
  }

  // x-axis labels that never collide: highlighted label first, then the rest in order if there's room
  function placeLabels(f, items) {
    const w = it => it.lbl.length * 6.2 + 10;
    const placed = [];
    const order = items.filter(it => it.strong).concat(items.filter(it => !it.strong));
    for (const it of order) {
      const a = it.x - w(it) / 2, b = it.x + w(it) / 2;
      if (a < f.m.left - 30 || b > f.width + 10) continue;
      if (placed.some(p => a < p.b && b > p.a)) continue;
      placed.push({ a, b });
      const t = s("text", { x: it.x, y: f.height - 8, "text-anchor": it.anchor || "middle", class: "tick" + (it.strong ? " strong" : "") }, f.svg);
      t.textContent = it.lbl;
    }
  }

  function roundedTop(x, y, w, h, r) {
    if (h <= 0) return "";
    r = Math.min(r, h, w / 2);
    return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
  }
  function roundedBottom(x, y, w, h, r) {
    if (h <= 0) return "";
    r = Math.min(r, h, w / 2);
    return `M${x},${y}H${x + w}V${y + h - r}Q${x + w},${y + h} ${x + w - r},${y + h}H${x + r}Q${x},${y + h} ${x},${y + h - r}Z`;
  }

  // ---------- bars ----------
  // opts: labels[], series[{name, color, values[], forecast[]?}], stacked, height, yFmt, xTick(i) -> label|null,
  //       range[{low, high}]?, marker[{value}]?, highlight (index), onClick(i), tooltip(i) -> {title, rows}
  function bars(container, o) {
    const f = frame(container, o.height || 260, o.margin);
    const n = o.labels.length;
    const totals = o.labels.map((_, i) => {
      let pos = 0, neg = 0;
      for (const sr of o.series) {
        const v = (sr.values[i] || 0) + ((sr.forecast && sr.forecast[i]) || 0);
        if (o.stacked) { if (v >= 0) pos += v; else neg += v; } else { pos = Math.max(pos, v); neg = Math.min(neg, v); }
      }
      return [neg, pos];
    });
    let hi = Math.max(0, ...totals.map(t => t[1]), ...(o.range || []).map(r => (r && r.high) || 0),
      ...(o.marker || []).map(v => v || 0));
    let lo = Math.min(0, ...totals.map(t => t[0]));
    const ys = ticks(lo, hi, o.ticks || 4);
    lo = ys[0]; hi = ys[ys.length - 1];
    const scale = v => f.m.top + f.ph - ((v - lo) / (hi - lo)) * f.ph;
    yAxis(f, ys, scale, o.yFmt);
    const band = f.pw / n;
    const groups = o.stacked ? 1 : o.series.length;
    const bw = Math.max(2, Math.min(o.maxBar || 24, (band * 0.72) / groups));
    const gap = band - bw * groups;
    const pats = o.series.map(sr => hatch(f.svg, sr.color, `h${++uid}`));
    const marks = s("g", {}, f.svg);
    const xlabels = [];
    for (let i = 0; i < n; i++) {
      const x0 = f.m.left + i * band + gap / 2;
      let upTop = 0, downBottom = 0;
      const segs = [];
      o.series.forEach((sr, si) => {
        const parts = [[sr.values[i] || 0, sr.color], [(sr.forecast && sr.forecast[i]) || 0, pats[si]]];
        for (const [v, fill] of parts) {
          if (!v) continue;
          const x = o.stacked ? x0 : x0 + si * bw;
          if (v > 0) {
            const base = o.stacked ? upTop : 0;
            segs.push({ x, y0: base, y1: base + v, fill, up: true });
            if (o.stacked) upTop += v;
          } else {
            const base = o.stacked ? downBottom : 0;
            segs.push({ x, y0: base + v, y1: base, fill, up: false });
            if (o.stacked) downBottom += v;
          }
        }
      });
      // the outermost segment in each direction gets the rounded end; 2px surface gap between segments
      const topIdx = segs.map((sg, k) => sg.up ? k : -1).filter(k => k >= 0);
      segs.forEach((sg, k) => {
        const yTop = scale(sg.y1), yBot = scale(sg.y0);
        let h = yBot - yTop;
        const last = o.stacked ? (sg.up ? k === topIdx[topIdx.length - 1] : false) : true;
        const gapPx = o.stacked && !last && sg.up ? 2 : 0;
        h -= gapPx;
        if (h < 0.5) return;
        const d = sg.up
          ? (last ? roundedTop(sg.x, yTop + gapPx, bw, h, 4) : `M${sg.x},${yTop + gapPx}h${bw}v${h}h${-bw}Z`)
          : roundedBottom(sg.x, yTop, bw, h, 4);
        s("path", { d, fill: sg.fill, class: "bar" }, marks);
      });
      if (o.range && o.range[i] && o.range[i].high > o.range[i].low) {
        const cx = x0 + (o.stacked ? bw / 2 : (bw * groups) / 2);
        const y1 = scale(o.range[i].high), y2 = scale(o.range[i].low);
        s("line", { x1: cx, x2: cx, y1, y2, class: "whisker" }, marks);
        s("line", { x1: cx - 4, x2: cx + 4, y1, y2: y1, class: "whisker" }, marks);
        s("line", { x1: cx - 4, x2: cx + 4, y1: y2, y2, class: "whisker" }, marks);
      }
      if (o.marker && o.marker[i] != null) {
        const y = scale(o.marker[i]);
        s("line", { x1: x0 - 3, x2: x0 + bw * (o.stacked ? 1 : groups) + 3, y1: y, y2: y, class: "marker-line" }, marks);
      }
      const lbl = o.xTick ? o.xTick(i) : o.labels[i];
      if (lbl) xlabels.push({ i, x: f.m.left + i * band + band / 2, lbl: String(lbl), strong: o.highlight === i });
      const hit = s("rect", { x: f.m.left + i * band, y: f.m.top, width: band, height: f.ph, class: "hit", tabindex: o.focusable ? 0 : null }, f.svg);
      if (o.tooltip) {
        const show = e => { hit.classList.add("on"); showTip(e, o.tooltip(i)); };
        hit.addEventListener("pointermove", show);
        hit.addEventListener("pointerleave", () => { hit.classList.remove("on"); hideTip(); });
        hit.addEventListener("focus", () => { const b = hit.getBoundingClientRect(); show({ clientX: b.right, clientY: b.top }); });
        hit.addEventListener("blur", hideTip);
      }
      if (o.onClick) { hit.style.cursor = "pointer"; hit.addEventListener("click", () => o.onClick(i)); }
    }
    placeLabels(f, xlabels);
    if (o.highlight != null) {
      const x = f.m.left + o.highlight * band;
      const r = s("rect", { x, y: f.m.top, width: band, height: f.ph, class: "now-band" }, f.svg);
      f.svg.insertBefore(r, f.svg.firstChild.nextSibling);
    }
    if (o.nowLine != null) {
      const x = f.m.left + o.nowLine * band;
      s("line", { x1: x, x2: x, y1: f.m.top - 4, y2: f.m.top + f.ph, class: "now-line" }, f.svg);
      if (o.nowLabel) {
        const t = s("text", { x: x + 4, y: f.m.top + 6, class: "tick strong" }, f.svg);
        t.textContent = o.nowLabel;
      }
    }
    return f;
  }

  // ---------- lines ----------
  // opts: x[] (numbers – e.g. ms timestamps or slot index), series[{name, color, values[], dash, area}],
  //       band {low[], high[], color}, yFmt, xFmt(x) for tooltip title, xTicks [{x, label}], height, yMin, yMax
  function lines(container, o) {
    const f = frame(container, o.height || 240, o.margin);
    const xs = o.x;
    const all = [];
    o.series.forEach(sr => sr.values.forEach(v => v != null && all.push(v)));
    if (o.band) { o.band.low.forEach(v => v != null && all.push(v)); o.band.high.forEach(v => v != null && all.push(v)); }
    let lo = o.yMin != null ? o.yMin : Math.min(0, ...all);
    let hi = o.yMax != null ? o.yMax : Math.max(...all, lo + 1);
    if (o.yMin == null && Math.min(...all) < 0) lo = Math.min(...all);
    const ys = ticks(lo, hi, o.ticks || 4);
    lo = ys[0]; hi = ys[ys.length - 1];
    const xmin = xs[0], xmax = xs[xs.length - 1];
    const X = v => f.m.left + ((v - xmin) / (xmax - xmin || 1)) * f.pw;
    const Y = v => f.m.top + f.ph - ((v - lo) / (hi - lo)) * f.ph;
    yAxis(f, ys, Y, o.yFmt);
    if (o.shade) {
      for (const sh of o.shade) {
        s("rect", { x: X(sh.from), y: f.m.top, width: Math.max(1, X(sh.to) - X(sh.from)), height: f.ph, class: sh.cls || "shade" }, f.svg);
      }
    }
    placeLabels(f, (o.xTicks || []).map(t => ({ x: X(t.x), lbl: String(t.label), anchor: t.anchor })));
    const path = (vals) => {
      let d = "", pen = false;
      vals.forEach((v, i) => {
        if (v == null) { pen = false; return; }
        d += (pen ? "L" : "M") + X(xs[i]).toFixed(1) + "," + Y(v).toFixed(1);
        pen = true;
      });
      return d;
    };
    if (o.band) {
      let d = "";
      const idx = xs.map((_, i) => i).filter(i => o.band.low[i] != null && o.band.high[i] != null);
      if (idx.length) {
        d = idx.map((i, k) => (k ? "L" : "M") + X(xs[i]) + "," + Y(o.band.high[i])).join("") +
          idx.slice().reverse().map(i => "L" + X(xs[i]) + "," + Y(o.band.low[i])).join("") + "Z";
        s("path", { d, fill: o.band.color, "fill-opacity": 0.14 }, f.svg);
      }
    }
    o.series.forEach(sr => {
      if (sr.area) {
        const idx = sr.values.map((v, i) => v == null ? null : i).filter(i => i != null);
        if (idx.length) {
          const base = Y(Math.max(lo, 0));
          const d = `M${X(xs[idx[0]])},${base}` + idx.map(i => `L${X(xs[i])},${Y(sr.values[i])}`).join("") + `L${X(xs[idx[idx.length - 1]])},${base}Z`;
          s("path", { d, fill: sr.color, "fill-opacity": 0.1 }, f.svg);
        }
      }
      s("path", { d: path(sr.values), fill: "none", stroke: sr.color, "stroke-width": sr.width || 2,
        "stroke-linejoin": "round", "stroke-linecap": "round", "stroke-dasharray": sr.dash || null }, f.svg);
    });
    if (o.markers) {
      for (const mk of o.markers) {
        s("circle", { cx: X(mk.x), cy: Y(mk.y), r: 4.5, fill: mk.color, class: "dot" }, f.svg);
        if (mk.label) {
          const t = s("text", { x: X(mk.x) + (mk.anchor === "end" ? -8 : 8), y: Y(mk.y) + (mk.below ? 18 : -8), "text-anchor": mk.anchor || "start", class: "dlabel" }, f.svg);
          t.textContent = mk.label;
        }
      }
    }
    if (o.nowX != null) {
      s("line", { x1: X(o.nowX), x2: X(o.nowX), y1: f.m.top - 4, y2: f.m.top + f.ph, class: "now-line" }, f.svg);
      if (o.nowLabel) {
        const t = s("text", { x: X(o.nowX) + 4, y: f.m.top + 6, class: "tick strong" }, f.svg);
        t.textContent = o.nowLabel;
      }
    }
    // crosshair
    const cross = s("g", { class: "cross", style: "display:none" }, f.svg);
    const vline = s("line", { y1: f.m.top, y2: f.m.top + f.ph, class: "cross-line" }, cross);
    const dots = o.series.map(sr => s("circle", { r: 4.5, fill: sr.color, class: "dot" }, cross));
    const hit = s("rect", { x: f.m.left, y: f.m.top, width: f.pw, height: f.ph, class: "hit" }, f.svg);
    const nearest = px => {
      const v = xmin + ((px - f.m.left) / f.pw) * (xmax - xmin);
      let lo2 = 0, hi2 = xs.length - 1;
      while (hi2 - lo2 > 1) { const mid = (lo2 + hi2) >> 1; if (xs[mid] < v) lo2 = mid; else hi2 = mid; }
      return Math.abs(xs[lo2] - v) <= Math.abs(xs[hi2] - v) ? lo2 : hi2;
    };
    hit.addEventListener("pointermove", e => {
      const box = f.svg.getBoundingClientRect();
      const i = nearest(e.clientX - box.left);
      cross.style.display = "";
      vline.setAttribute("x1", X(xs[i])); vline.setAttribute("x2", X(xs[i]));
      o.series.forEach((sr, k) => {
        const v = sr.values[i];
        dots[k].style.display = v == null ? "none" : "";
        if (v != null) { dots[k].setAttribute("cx", X(xs[i])); dots[k].setAttribute("cy", Y(v)); }
      });
      const content = o.tooltip ? o.tooltip(i) : {
        title: o.xFmt ? o.xFmt(xs[i]) : String(xs[i]),
        rows: o.series.filter(sr => sr.values[i] != null).map(sr => ({ color: sr.color, value: (o.vFmt || o.yFmt || String)(sr.values[i]), label: sr.name })),
      };
      showTip(e, content);
    });
    hit.addEventListener("pointerleave", () => { cross.style.display = "none"; hideTip(); });
    if (o.onClick) { hit.style.cursor = "pointer"; hit.addEventListener("click", e => { const box = f.svg.getBoundingClientRect(); o.onClick(nearest(e.clientX - box.left)); }); }
    return f;
  }

  // ---------- scatter ----------
  function scatter(container, o) {
    const f = frame(container, o.height || 280, Object.assign({ bottom: 40, left: 50 }, o.margin));
    const px = o.points.map(p => p[0]), py = o.points.map(p => p[1]);
    const xt = ticks(Math.min(...px, ...(o.line || []).map(p => p[0])), Math.max(...px), 6);
    const yt = ticks(0, Math.max(...py, ...(o.line || []).map(p => p[1])), 4);
    const X = v => f.m.left + ((v - xt[0]) / (xt[xt.length - 1] - xt[0])) * f.pw;
    const Y = v => f.m.top + f.ph - ((v - yt[0]) / (yt[yt.length - 1] - yt[0])) * f.ph;
    yAxis(f, yt, Y, o.yFmt);
    for (const v of xt) {
      const t = s("text", { x: X(v), y: f.height - 22, "text-anchor": "middle", class: "tick" }, f.svg);
      t.textContent = o.xFmt ? o.xFmt(v) : v;
    }
    if (o.xLabel) {
      const t = s("text", { x: f.m.left + f.pw / 2, y: f.height - 4, "text-anchor": "middle", class: "tick" }, f.svg);
      t.textContent = o.xLabel;
    }
    const g = s("g", {}, f.svg);
    const pts = o.points.map(p => {
      const c = s("circle", { cx: X(p[0]), cy: Y(p[1]), r: 4, fill: o.color, "fill-opacity": 0.55, class: "dot" }, g);
      return { p, c, x: X(p[0]), y: Y(p[1]) };
    });
    if (o.line && o.line.length) {
      const d = o.line.filter(p => p[0] >= xt[0] && p[0] <= xt[xt.length - 1])
        .map((p, i) => (i ? "L" : "M") + X(p[0]) + "," + Y(Math.max(0, p[1]))).join("");
      s("path", { d, fill: "none", stroke: css("--ink"), "stroke-width": 2, "stroke-linecap": "round" }, f.svg);
    }
    const ring = s("circle", { r: 7, class: "ring", style: "display:none" }, f.svg);
    const hit = s("rect", { x: f.m.left, y: f.m.top, width: f.pw, height: f.ph, class: "hit" }, f.svg);
    hit.addEventListener("pointermove", e => {
      const box = f.svg.getBoundingClientRect();
      const mx = e.clientX - box.left, my = e.clientY - box.top;
      let best = null, bd = 1e9;
      for (const q of pts) { const d = (q.x - mx) ** 2 + (q.y - my) ** 2; if (d < bd) { bd = d; best = q; } }
      if (!best || bd > 900) { ring.style.display = "none"; hideTip(); return; }
      ring.style.display = ""; ring.setAttribute("cx", best.x); ring.setAttribute("cy", best.y);
      showTip(e, o.tooltip(best.p));
    });
    hit.addEventListener("pointerleave", () => { ring.style.display = "none"; hideTip(); });
    if (o.onClick) {
      hit.style.cursor = "pointer";
      hit.addEventListener("click", e => {
        const box = f.svg.getBoundingClientRect();
        const mx = e.clientX - box.left, my = e.clientY - box.top;
        let best = null, bd = 1e9;
        for (const q of pts) { const d = (q.x - mx) ** 2 + (q.y - my) ** 2; if (d < bd) { bd = d; best = q; } }
        if (best && bd < 900) o.onClick(best.p);
      });
    }
    return f;
  }

  // ---------- colour ramps ----------
  function hex2rgb(h) { h = h.replace("#", ""); return [0, 2, 4].map(i => parseInt(h.slice(i, i + 2), 16)); }
  function rampFn(stops) {
    const rgb = stops.map(hex2rgb);
    return t => {
      t = Math.max(0, Math.min(1, t));
      const p = t * (rgb.length - 1), i = Math.min(rgb.length - 2, Math.floor(p)), k = p - i;
      const c = rgb[i].map((v, j) => Math.round(v + (rgb[i + 1][j] - v) * k));
      return `rgb(${c[0]},${c[1]},${c[2]})`;
    };
  }
  function ramp(name) {
    return rampFn(css(`--ramp-${name}`).split(",").map(x => x.trim()));
  }

  // ---------- calendar heatmap ----------
  // opts: days [{d:'YYYY-MM-DD', v}], ramp, fmt(v), onClick(d), tooltip(item)
  function calendar(container, o) {
    container.replaceChildren();
    const days = o.days;
    if (!days.length) return;
    const first = new Date(days[0].d + "T12:00:00");
    const offset = (first.getDay() + 6) % 7;   // Monday = 0
    const weeks = Math.ceil((days.length + offset) / 7);
    const width = Math.max(260, container.clientWidth);
    const left = 28, top = 18;
    const cell = Math.max(6, Math.min(20, Math.floor((width - left - 4) / weeks) - 2));
    const step = cell + 2;
    const height = top + step * 7 + 4;
    const svg = s("svg", { width: Math.min(width, left + weeks * step + 4), height, class: "chart", role: "img" }, container);
    const vals = days.map(x => x.v).filter(v => v != null).sort((a, b) => a - b);
    const lo = vals[Math.floor(vals.length * 0.02)] || 0, hi = vals[Math.floor(vals.length * 0.98)] || 1;
    const col = o.ramp;
    ["Mon", "", "Wed", "", "Fri", "", "Sun"].forEach((l, i) => {
      if (!l) return;
      const t = s("text", { x: 0, y: top + i * step + cell - 1, class: "tick" }, svg);
      t.textContent = l;
    });
    let lastMonth = -1;
    days.forEach((item, k) => {
      const idx = k + offset, w = Math.floor(idx / 7), dow = idx % 7;
      const dt = new Date(item.d + "T12:00:00");
      if (dt.getDate() <= 7 && dow === 0 && dt.getMonth() !== lastMonth) {
        lastMonth = dt.getMonth();
        const t = s("text", { x: left + w * step, y: 11, class: "tick" }, svg);
        t.textContent = dt.toLocaleString("en-GB", { month: "short" });
      }
      const fill = item.v == null ? css("--empty") : col((item.v - lo) / (hi - lo || 1));
      const r = s("rect", { x: left + w * step, y: top + dow * step, width: cell, height: cell, rx: 2, fill, class: "cell" }, svg);
      r.addEventListener("pointermove", e => showTip(e, o.tooltip(item)));
      r.addEventListener("pointerleave", hideTip);
      if (o.onClick) { r.style.cursor = "pointer"; r.addEventListener("click", () => o.onClick(item.d)); }
    });
    return { lo, hi };
  }

  // ---------- grid heatmap (rows × cols) ----------
  function grid(container, o) {
    container.replaceChildren();
    const width = Math.max(260, container.clientWidth);
    const left = 36, top = 4, bottom = 18;
    const cols = o.cols.length, rows = o.rows.length;
    const cw = (width - left) / cols;
    const ch = o.cellHeight || 22;
    const height = top + rows * ch + bottom;
    const svg = s("svg", { width, height, class: "chart", role: "img" }, container);
    const flat = o.values.flat().filter(v => v != null);
    const lo = Math.min(...flat), hi = Math.max(...flat);
    const col = o.ramp;
    o.rows.forEach((rl, ri) => {
      const t = s("text", { x: 0, y: top + ri * ch + ch / 2 + 4, class: "tick" }, svg);
      t.textContent = rl;
      o.cols.forEach((cl, ci) => {
        const v = o.values[ri][ci];
        const r = s("rect", { x: left + ci * cw + 1, y: top + ri * ch + 1, width: cw - 2, height: ch - 2, rx: 2,
          fill: v == null ? css("--empty") : col((v - lo) / (hi - lo || 1)), class: "cell" }, svg);
        r.addEventListener("pointermove", e => showTip(e, o.tooltip(ri, ci, v)));
        r.addEventListener("pointerleave", hideTip);
      });
    });
    o.cols.forEach((cl, ci) => {
      if (!o.colTick || !o.colTick(ci)) return;
      const t = s("text", { x: left + ci * cw + cw / 2, y: height - 4, "text-anchor": "middle", class: "tick" }, svg);
      t.textContent = o.colTick(ci);
    });
    return { lo, hi };
  }

  window.Charts = { bars, lines, scatter, calendar, grid, ramp, css, showTip, hideTip };
})();
