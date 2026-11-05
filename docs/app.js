/* Frankfurt's Giants · Shadows — #30DayMapChallenge 2026 · Day 5 · Senses: Sight */
(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));
  const fmt = (v, d = 0) => v == null || Number.isNaN(v) ? "–" : Number(v).toLocaleString("en-GB", { minimumFractionDigits: d, maximumFractionDigits: d });
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  const C = { tower: "#e8590c", bld: dark ? "#4a4743" : "#e7e2da", shade: dark ? "rgba(5,8,25,0.55)" : "rgba(28,32,70,0.42)", roof: "#3b4cca" };
  const LOSS = ["#fde0aa", "#f9b05c", "#e87034", "#bd3627", "#6e162a"];

  const st = { m: 6, t: 0, mode: "moment", view: "3d", sel: null };
  let map, S, T, IDX = {}, cache = {}, playing = null, LI = null, popup;

  const j = (u) => fetch(u, { cache: "no-cache" }).then((r) => { if (!r.ok) throw new Error(u + " " + r.status); return r.json(); });
  const mm = (m) => String(m).padStart(2, "0");
  Promise.all([j("data/stats.json"), j("data/towers.geojson"), j("data/sunloss/index.json").catch(() => null)])
    .then(([s, t, li]) => { S = s; T = t; LI = li; initPanel(); initMap(); })
    .catch((e) => { $("#loading").textContent = "Could not load the data: " + e.message; });

  const index = (m) => IDX[m] ? Promise.resolve(IDX[m]) : j(`data/shadows/${mm(m)}/index.json`).then((x) => (IDX[m] = x));
  function step(m, t) {
    const k = `${m}/${t}`;
    if (!cache[k]) cache[k] = j(`data/shadows/${mm(m)}/${mm(t)}.json`).catch(() => ({ type: "FeatureCollection", features: [] }));
    return cache[k];
  }

  // ------------------------------------------------------------------ map
  function initMap() {
    const protocol = new pmtiles.Protocol();
    maplibregl.addProtocol("pmtiles", protocol.tile);
    map = new maplibregl.Map({
      container: "map", hash: true, maxPitch: 75, attributionControl: false,
      center: [8.6735, 50.1115], zoom: 14.6, pitch: 58, bearing: -25,
      style: `https://basemaps.cartocdn.com/gl/${dark ? "dark-matter" : "positron"}-gl-style/style.json`
    });
    map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "bottom-right");
    map.addControl(new maplibregl.AttributionControl({ compact: true, customAttribution: "3D buildings © HVBG Hessen (dl-de/zero-2-0) · names © OpenStreetMap contributors" }));
    popup = new maplibregl.Popup({ closeButton: true, maxWidth: "320px", offset: 8 });
    let ready = false;
    const setup = () => {
      if (ready || !(map.style && map.style._loaded)) return;
      ready = true;
      const first = map.getStyle().layers.find((l) => l.type === "symbol")?.id;
      if (LI) {
        map.addSource("loss", { type: "image", url: `data/sunloss/${mm(st.m)}.png`, coordinates: LI.corners });
        map.addLayer({ id: "loss", type: "raster", source: "loss", layout: { visibility: "none" }, paint: { "raster-opacity": 0.9, "raster-fade-duration": 0, "raster-resampling": "nearest" } }, first);
      }
      map.addSource("sh", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.addLayer({ id: "shg", type: "fill", source: "sh", filter: ["==", ["get", "k"], "g"], paint: { "fill-color": C.shade, "fill-antialias": false } }, first);
      map.addSource("b", { type: "vector", url: "pmtiles://" + new URL("data/buildings.pmtiles", location.href).href });
      map.addLayer({ id: "b3", type: "fill-extrusion", source: "b", "source-layer": "b", minzoom: 12,
        paint: { "fill-extrusion-color": ["case", ["==", ["get", "t"], 1], C.tower, C.bld], "fill-extrusion-height": ["get", "h"], "fill-extrusion-base": 0,
                 "fill-extrusion-opacity": 0.92, "fill-extrusion-vertical-gradient": true } });
      // shadows on roofs: a thin dark slab on top of each shaded roof part, at the roof's own height
      map.addLayer({ id: "shr", type: "fill-extrusion", source: "sh", filter: ["==", ["get", "k"], "r"],
        paint: { "fill-extrusion-color": dark ? "#0b0f2a" : "#26306e", "fill-extrusion-base": ["get", "h"], "fill-extrusion-height": ["+", ["get", "h"], 0.5], "fill-extrusion-opacity": 0.75 } });
      map.addLayer({ id: "shr2", type: "fill", source: "sh", filter: ["==", ["get", "k"], "r"], layout: { visibility: "none" }, paint: { "fill-color": C.roof, "fill-opacity": 0.55 } });
      map.addSource("tw", { type: "geojson", data: T, promoteId: "i" });
      map.addLayer({ id: "twl", type: "line", source: "tw", paint: { "line-color": dark ? "#fff" : "#1f1d1a", "line-width": ["case", ["boolean", ["feature-state", "sel"], false], 3, 0], "line-opacity": 0.9 } });
      map.addLayer({ id: "twf", type: "fill", source: "tw", paint: { "fill-color": "#000", "fill-opacity": 0 } });

      map.on("click", "twf", (e) => selectTower(e.features[0].properties.i, e.lngLat));
      map.on("mouseenter", "twf", () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", "twf", () => { map.getCanvas().style.cursor = ""; });
      $("#loading").classList.add("done");
      try { if (!localStorage.getItem("d5intro")) $("#intro").hidden = false; } catch (e) { $("#intro").hidden = false; }
      applyView(); loadMonth(st.m, true);
    };
    map.on("load", setup);
    map.on("styledata", () => setTimeout(setup, 0));
    const poll = setInterval(() => { setup(); if (ready) clearInterval(poll); }, 300);
    window._map = map;
  }

  function applyView() {
    if (!map || !map.getLayer("b3")) return;
    const is3 = st.view === "3d";
    map.setLayoutProperty("b3", "visibility", is3 ? "visible" : "none");
    map.setLayoutProperty("shr", "visibility", is3 && st.mode === "moment" ? "visible" : "none");
    map.setLayoutProperty("shr2", "visibility", !is3 && st.mode === "moment" ? "visible" : "none");
    map.setLayoutProperty("shg", "visibility", st.mode === "moment" ? "visible" : "none");
    if (map.getLayer("loss")) map.setLayoutProperty("loss", "visibility", st.mode === "loss" ? "visible" : "none");
    map.setPaintProperty("twf", "fill-opacity", is3 ? 0 : 0.85);
    map.setPaintProperty("twf", "fill-color", C.tower);
    map.easeTo({ pitch: is3 ? 58 : 0, duration: 600 });
  }

  // ------------------------------------------------------------------ time
  function loadMonth(m, keepNoon) {
    return index(m).then((ix) => {
      st.m = m;
      const n = ix.times.length;
      if (keepNoon || st.t >= n) {
        const noon = ix.times.findIndex((x) => x >= "17:00");
        st.t = Math.max(0, noon >= 0 ? noon : Math.floor(n / 2));
      }
      $("#time").max = n - 1;
      if (map && map.getSource("loss")) map.getSource("loss").updateImage({ url: `data/sunloss/${mm(m)}.png`, coordinates: LI.corners });
      showStep(); renderPanel();
      for (let k = 0; k < n; k++) step(m, k);           // prefetch the day
    });
  }
  function showStep() {
    const ix = IDX[st.m]; if (!ix) return;
    const t = st.t;
    step(st.m, t).then((fc) => { if (t === st.t && map && map.getSource("sh")) map.getSource("sh").setData(fc); });
    $("#time").value = t; $("#timeO").textContent = ix.times[t];
    $("#clockDay").textContent = `21 ${MON[st.m - 1]}`; $("#clockTime").textContent = st.mode === "loss" ? "all day" : ix.times[t];
    sunDial(ix.sun[t]);
    $("#ticks").innerHTML = [0, Math.floor((ix.times.length - 1) / 2), ix.times.length - 1].map((k) => `<span>${ix.times[k]}</span>`).join("");
    dayChart();
    lede();
  }
  function sunDial([az, alt]) {
    const r = 15, a = (az - 180) * Math.PI / 180, len = r * Math.cos(alt * Math.PI / 180);
    const sx = 20 + Math.sin(az * Math.PI / 180) * len, sy = 20 - Math.cos(az * Math.PI / 180) * len;
    $("#sunDial").innerHTML = `<svg viewBox="0 0 40 40" width="40" height="40"><circle cx="20" cy="20" r="${r}" fill="none" stroke="var(--line)"/><text x="20" y="7" text-anchor="middle" font-size="6" fill="var(--ink-3)">N</text>
      <line x1="20" y1="20" x2="${20 - (sx - 20) * 0.9}" y2="${20 - (sy - 20) * 0.9}" stroke="var(--ink-2)" stroke-width="2" stroke-linecap="round"/>
      <circle cx="${sx}" cy="${sy}" r="4" fill="#f5b014"/></svg><span class="mono">${fmt(alt, 0)}°</span>`;
    void a;
  }
  function play() {
    $("#playBtn").classList.add("on"); $("#playBtn").textContent = "❚❚ Pause";
    playing = setInterval(() => {
      const n = IDX[st.m].times.length;
      st.t = (st.t + 1) % n; showStep();
    }, 700);
  }
  function stop() { if (!playing) return; clearInterval(playing); playing = null; $("#playBtn").classList.remove("on"); $("#playBtn").textContent = "▶ Play the day"; }

  // ------------------------------------------------------------------ panel
  function initPanel() {
    $("#monthSeg").innerHTML = MON.map((x, i) => `<button data-m="${i + 1}">${x}</button>`).join("");
    $$("#monthSeg button").forEach((b) => b.onclick = () => { stop(); loadMonth(+b.dataset.m, false); });
    $("#time").oninput = (e) => { stop(); st.t = +e.target.value; showStep(); };
    $$("#modeSeg button").forEach((b) => b.onclick = () => { st.mode = b.dataset.m; stop(); applyView(); renderPanel(); showStep(); });
    $$("#viewSeg button").forEach((b) => b.onclick = () => { st.view = b.dataset.v; $$("#viewSeg button").forEach((x) => x.classList.toggle("on", x === b)); applyView(); });
    $("#playBtn").onclick = () => playing ? stop() : (st.mode === "loss" && (st.mode = "moment", applyView(), renderPanel()), play());
    $("#introGo").onclick = () => { $("#intro").hidden = true; try { localStorage.setItem("d5intro", "1"); } catch (e) { /* ignore */ } };
    $("#dayChart").onclick = (e) => {
      const ix = IDX[st.m]; if (!ix) return;
      const r = e.currentTarget.getBoundingClientRect(), x = (e.clientX - r.left) / r.width * 330;
      st.t = Math.max(0, Math.min(ix.times.length - 1, Math.round((x - 34) / (286 / Math.max(1, ix.times.length - 1)))));
      stop(); if (st.mode === "loss") { st.mode = "moment"; applyView(); renderPanel(); } showStep();
    };
    const c = S.casters || {};
    $("#dq").innerHTML = `${fmt(c.parts)} LoD2 building parts of ${fmt(c.buildings)} buildings are 30 m or taller. ${fmt(c.osm_with_caster)} of the ${fmt(c.osm_total)} OpenStreetMap buildings tagged with 10+ storeys contain such a part; ${fmt(c.osm_without_caster)} are lower than 30 m in the measured model and cast no shadow here.`
      + (c.roof_planes ? ` Heights and roof shapes come from ${fmt(c.roof_planes)} measured LoD2 roof planes around the towers, not from storey counts.` : "");
    $("#downloads").innerHTML = `<span>Download:</span><a href="data/towers.geojson">towers (GeoJSON)</a><a href="data/stats.json">stats.json</a><a href="https://github.com/barisparlatangiller/30DMC26-D05-Sight-Frankfurt-HighRise-Shadows/tree/data">all shadows (data branch)</a>`;
    renderPanel();
  }
  function renderPanel() {
    $$("#monthSeg button").forEach((b) => b.classList.toggle("on", +b.dataset.m === st.m));
    $$("#modeSeg button").forEach((b) => b.classList.toggle("on", b.dataset.m === st.mode));
    $("#timeBlock").style.display = st.mode === "loss" ? "none" : "";
    const d = (S.shadows && S.shadows.days || {})[mm(st.m)];
    $("#dayHint").textContent = d ? `Sun above 5° from ${d.first} to ${d.last}, highest at ${fmt(d.max_alt)}°. ${d.steps} steps of 30 minutes.` : "";
    $("#rankMonth").textContent = `21 ${MON[st.m - 1]}`;
    const rows = T.features.map((f) => { const v = (f.properties.m || {})[mm(st.m)] || [0, 0, 0]; return { p: f.properties, v: v[0] + v[1], k: v[2] }; })
      .filter((x) => x.v > 0).sort((a, b) => b.v - a.v);
    const seen = new Set(), top = [];
    for (const r of rows) { const key = r.p.n || r.p.i; if (seen.has(key)) continue; seen.add(key); top.push(r); if (top.length === 10) break; }
    const mx = top[0]?.v || 1;
    $("#rank").innerHTML = top.map((r, i) => `<li data-i="${r.p.i}"><span class="n">${i + 1}</span><span>${esc(r.p.n || "Unnamed building")}<small>${fmt(r.p.h)} m · ${fmt(r.k)} roofs shaded</small></span>
      <span class="bar" style="width:${Math.max(4, 70 * r.v / mx)}px;background:var(--shade)"></span><span class="v">${fmt(r.v, 1)}</span></li>`).join("");
    $$("#rank li").forEach((li) => li.onclick = () => {
      const f = T.features.find((x) => x.properties.i === +li.dataset.i); if (!f) return;
      const c = centroid(f.geometry); map.flyTo({ center: c, zoom: 16, pitch: st.view === "3d" ? 60 : 0, duration: 900 });
      selectTower(+li.dataset.i, { lng: c[0], lat: c[1] });
    });
    legend();
  }
  function centroid(g) {
    const ring = (g.type === "Polygon" ? g.coordinates : g.coordinates[0])[0];
    return [ring.reduce((a, p) => a + p[0], 0) / ring.length, ring.reduce((a, p) => a + p[1], 0) / ring.length];
  }
  function lede() {
    const ix = IDX[st.m]; const d = (S.shadows && S.shadows.days || {})[mm(st.m)]; const c = S.casters || {};
    if (!ix || !d) { $("#lede").innerHTML = "&nbsp;"; return; }
    if (st.mode === "loss") {
      const l = (S.sunloss || {})[mm(st.m)];
      $("#lede").innerHTML = `<b>21 ${MON[st.m - 1]}, whole day:</b> the towers take at least one hour of direct sun from <b>${fmt(l ? l.km2[1] : d.loss_km2_1h, 2)} km²</b> of streets and roofs, and three hours or more from ${fmt(l ? l.km2[3] : d.loss_km2_3h, 2)} km².`;
      return;
    }
    const g = d.ground_km2[st.t] || 0, r = d.roof_km2[st.t] || 0;
    $("#lede").innerHTML = `<b>21 ${MON[st.m - 1]}, ${ix.times[st.t]}</b> · sun ${fmt(ix.sun[st.t][1])}° high: Frankfurt's ${fmt(c.buildings)} buildings of 30 m and more shade <b>${fmt(g * 100, 1)} ha</b> of ground and <b>${fmt(r * 100, 1)} ha</b> of other roofs.`;
  }
  function dayChart() {
    const ix = IDX[st.m]; const d = (S.shadows && S.shadows.days || {})[mm(st.m)];
    if (!ix || !d) return;
    const n = ix.times.length, x0 = 34, x1 = 320, y0 = 10, y1 = 112;
    const tot = d.ground_km2.map((g, i) => g + (d.roof_km2[i] || 0));
    // fixed scale across months so seasons compare
    const all = Object.values(S.shadows.days).flatMap((x) => x.ground_km2.map((g, i) => g + (x.roof_km2[i] || 0)));
    const mx = Math.max(...all, 0.01) * 1.05, X = (i) => x0 + (n > 1 ? i * (x1 - x0) / (n - 1) : 0), Y = (v) => y1 - (y1 - y0) * v / mx;
    let s = `<line x1="${x0}" x2="${x1}" y1="${y1}" y2="${y1}" stroke="var(--line)"/>`;
    s += `<path d="M${X(0)},${y1} ${d.ground_km2.map((v, i) => `L${X(i)},${Y(v)}`).join(" ")} L${X(n - 1)},${y1}Z" fill="var(--shade)" opacity=".75"/>`;
    s += `<path d="${tot.map((v, i) => `${i ? "L" : "M"}${X(i)},${Y(v)}`).join(" ")} L${X(n - 1)},${Y(d.ground_km2[n - 1])} ${d.ground_km2.slice().reverse().map((v, k) => `L${X(n - 1 - k)},${Y(v)}`).join(" ")}Z" fill="var(--roof)" opacity=".55"/>`;
    s += `<text x="${x0 - 4}" y="${Y(mx / 1.05) + 3}" text-anchor="end">${fmt(mx / 1.05 * 100)} ha</text><text x="${x0 - 4}" y="${y1}" text-anchor="end">0</text>`;
    [0, Math.floor((n - 1) / 2), n - 1].forEach((k) => { s += `<text x="${X(k)}" y="${y1 + 13}" text-anchor="middle">${ix.times[k]}</text>`; });
    if (st.mode === "moment") s += `<line x1="${X(st.t)}" x2="${X(st.t)}" y1="${y0 - 4}" y2="${y1}" stroke="var(--ink)" stroke-width="1.4"/><circle cx="${X(st.t)}" cy="${y0 - 4}" r="3" fill="#f5b014"/>`;
    s += `<text x="${x0}" y="136">same scale for all months</text>`;
    $("#dayChart").innerHTML = s;
  }

  function selectTower(i, ll) {
    const f = T.features.find((x) => x.properties.i === +i); if (!f) return;
    const p = f.properties;
    if (st.sel != null) map.setFeatureState({ source: "tw", id: st.sel }, { sel: false });
    st.sel = p.i; map.setFeatureState({ source: "tw", id: p.i }, { sel: true });
    const v = (p.m || {})[mm(st.m)] || [0, 0, 0];
    popup.setLngLat(ll).setHTML(`<div class="pop"><b>${esc(p.n || "Unnamed building")}</b><small>${esc([p.a, p.use].filter(Boolean).join(" · "))}</small>
      <table class="ptab"><tr><td>measured height</td><td>${fmt(p.h, 1)} m</td></tr>${p.lv ? `<tr><td>storeys (OSM)</td><td>${p.lv}</td></tr>` : ""}
      ${p.h_osm ? `<tr><td>height in OSM</td><td>${fmt(p.h_osm)} m</td></tr>` : ""}${p.arch ? `<tr><td>architect</td><td>${esc(p.arch)}</td></tr>` : ""}${p.year ? `<tr><td>built</td><td>${esc(p.year)}</td></tr>` : ""}</table>
      <div class="verdict"><b>21 ${MON[st.m - 1]}:</b> shades ${fmt(v[0], 1)} ha·h of ground and ${fmt(v[1], 1)} ha·h of roofs, on ${fmt(v[2])} other buildings over the day.</div>
      ${p.osm ? `<small><a href="https://www.openstreetmap.org/${esc(p.osm)}" target="_blank" rel="noopener">OpenStreetMap ↗</a></small>` : ""}</div>`).addTo(map);
  }

  function legend() {
    const L = $("#legend");
    if (st.mode === "loss") {
      const b = LI ? LI.bins : [0.5, 1, 2, 3, 5];
      L.innerHTML = `<div class="lg-title">Direct sun lost to the towers · 21 ${MON[st.m - 1]}</div><div class="lg-sub">hours over the whole day, ground and roofs</div>
        <div class="vsteps">${LOSS.map((c) => `<i style="background:${c}"></i>`).join("")}</div>
        <div class="vticks">${b.map((x, k) => `<span>${x}${k === b.length - 1 ? "+ h" : ""}</span>`).join("")}</div>`;
      return;
    }
    L.innerHTML = `<div class="lg-title">Shadows of buildings ≥ 30 m</div><div class="lg-sub">21 ${MON[st.m - 1]}, local time</div>
      <div class="lg-row"><i class="sq" style="background:${C.tower}"></i>building of 30 m or more <small>${fmt((S.casters || {}).buildings)}</small></div>
      <div class="lg-row"><i class="sq" style="background:${C.shade}"></i>shadow on the ground</div>
      <div class="lg-row"><i class="sq" style="background:${dark ? "#0b0f2a" : "#26306e"}"></i>shadow on a lower roof, at its height</div>
      <div class="lg-row"><i class="sq" style="background:${C.bld};border:1px solid var(--line)"></i>other building (receives, casts none)</div>`;
  }
})();
