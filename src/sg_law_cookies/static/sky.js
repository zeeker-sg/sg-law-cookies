/* SG Law Cookies — the counter map (PRD section 7), Levels 1-3.
   All page JS for counter_map.html.j2; vendored D3 only, no CDNs.

   Level 1 — daily skyline: kuih bangkit rosettes = areas of law, red dot =
     high significance, ochre threads = doctrine ties, yellow price-tag
     scrubber along the bottom.
   Level 2 — focus mode: clicking a rosette fans its cookies out as typed
     nodes: round disc = news, hexagon = judgment, pineapple-tart styling =
     high significance, any kind. Chip-brown dots between cookies are FOLIO
     concepts shared by 2+ cookies in the focused area — the chips on each
     cookie are its FOLIO concepts. On small screens (max-width 720px) the
     fan degrades to a stacked price-card list under the SVG.
   Level 3 — detail panel: right-side parchment card on desktop, bottom
     sheet on mobile. Deep links: #date and #date/cookieid. */

const svg = d3.select('#sky');
const W = 1000, H = 560, CX = W / 2, CY = H / 2 - 6;
const tip = document.getElementById('tip');
const panel = document.getElementById('panel');
const panelBackdrop = document.getElementById('panel-backdrop');
const focusList = document.getElementById('focus-list');
const mobileQuery = window.matchMedia('(max-width: 720px)');

let nodes = [], links = [], sim = null, active = -1;
let SCRUB = [];                       // [{date,total,high}] — at most the 14 most recent days
const dayCache = new Map();           // date → per-day sky JSON, fetched once
let currentDay = null;                // the day JSON currently rendered

let focusArea = null;                 // area node datum while in Level-2 focus mode
let focusSim = null;                  // local force simulation for the fanned cookies
let openCookie = null;                // cookie object while the Level-3 panel is open
let returnFocusEl = null;             // element that gets keyboard focus back on close

const r = c => 16 + Math.sqrt(c) * 15;
const petals = d => 7 + (d.label.length % 4);          // handmade mould variety
const rosette = (R, n) => {
  const line = d3.lineRadial().angle(p => p[0]).radius(p => p[1]).curve(d3.curveCatmullRomClosed);
  return line(d3.range(0, 2 * Math.PI, Math.PI / 72).map(t => [t, R * (0.88 + 0.12 * Math.cos(n * t))]));
};

const cookieR = c => c.sig === 'high' ? 9.5 : c.sig === 'low' ? 7 : 8;
const hexPath = R => {
  let p = '';
  for (let i = 0; i < 6; i++) {
    const a = Math.PI / 3 * i - Math.PI / 2;
    p += (i ? 'L' : 'M') + (R * Math.cos(a)).toFixed(2) + ' ' + (R * Math.sin(a)).toFixed(2);
  }
  return p + 'Z';
};
const esc = s => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

/* ── hash deep links: #date and #date/cookieid ───────────────────── */

function parseHash() {
  const h = location.hash.replace(/^#/, '');
  if (!h) return null;
  const parts = h.split('/');
  return { date: parts[0], cookie: parts[1] || null };
}

function setHash(h) {
  // replaceState: updates the URL without a reload and without firing
  // hashchange, so our own navigation never loops through the listener.
  if (location.hash.replace(/^#/, '') === h) return;
  history.replaceState(null, '', '#' + h);
}

/* ── data ────────────────────────────────────────────────────────── */

async function loadDay(date) {
  if (dayCache.has(date)) return dayCache.get(date);
  const res = await fetch(`/data/sky/${date}.json`);
  if (!res.ok) throw new Error(`sky data missing for ${date}`);
  const day = await res.json();
  dayCache.set(date, day);
  return day;
}

function buildNodes(day) {
  const prev = new Map(nodes.map(n => [n.id, n]));
  const out = [{ id: '__centre', centre: true, fx: CX, fy: CY }];
  day.areas.forEach((a, i) => {
    const old = prev.get(a.label);
    const angle = (i / day.areas.length) * 2 * Math.PI - Math.PI / 2;
    out.push({
      id: a.label, label: a.label, count: a.count, high: a.high, cookies: a.cookies,
      x: old ? old.x : CX + Math.cos(angle) * 200,
      y: old ? old.y : CY + Math.sin(angle) * 160,
    });
  });
  return out;
}

function findCookie(day, id) {
  for (const a of (day.areas || [])) {
    for (const c of (a.cookies || [])) if (c.id === id) return { area: a, cookie: c };
  }
  return null;
}

/* ── Level 1 — daily skyline ─────────────────────────────────────── */

async function render(idx) {
  exitFocus();        // day change exits focus mode (and the panel with it)
  hidePanel();
  active = idx;
  const day = await loadDay(SCRUB[idx].date);
  if (active !== idx) return;  // a later click superseded this render mid-fetch
  currentDay = day;
  setHash(day.date);
  const when = new Date(day.date + 'T12:00:00');
  document.getElementById('dayname').textContent =
    when.toLocaleDateString('en-SG', { weekday: 'long', day: 'numeric', month: 'long' });
  const t = day.totals;
  document.getElementById('stats').innerHTML =
    `<b>${t.total}</b> cookies &nbsp;·&nbsp; ${t.news} news ○ &nbsp;·&nbsp; ${t.judgments} judgments ⬡ &nbsp;·&nbsp; <span class="hot">${t.high} still warm ▲</span>`;
  document.querySelectorAll('.day').forEach((el, i) => el.classList.toggle('active', i === idx));

  nodes = buildNodes(day);
  links = nodes.filter(n => !n.centre).map(n => ({ source: '__centre', target: n.id, spoke: true }));
  (day.ties || []).forEach(t => links.push({ source: t.a, target: t.b, w: t.w }));

  if (sim) sim.stop();
  sim = d3.forceSimulation(nodes)
    .force('link', d3.forceLink(links).id(d => d.id)
      .distance(d => d.spoke ? 150 + r(d.target.count || 1) : 90)
      .strength(d => d.spoke ? .18 : Math.min(.7, .22 * d.w)))
    .force('charge', d3.forceManyBody().strength(-260))
    .force('collide', d3.forceCollide(d => d.centre ? 50 : r(d.count) + 26))
    .force('x', d3.forceX(CX).strength(.05)).force('y', d3.forceY(CY).strength(.07))
    .on('tick', tick);

  // edges: dotted spokes to the centre, ochre weighted doctrine ties
  const edge = svg.selectAll('line.e').data(links, d => (d.source.id || d.source) + '|' + (d.target.id || d.target));
  edge.exit().remove();
  edge.enter().append('line').attr('class', 'e');
  svg.selectAll('line.e')
    .classed('spoke', d => !!d.spoke)
    .attr('stroke', d => d.spoke ? 'var(--line-strong)' : 'var(--ochre)')
    .attr('stroke-dasharray', d => d.spoke ? '2 6' : null)
    .attr('stroke-opacity', d => d.spoke ? .7 : .55)
    .attr('stroke-width', d => d.spoke ? 1 : Math.min(3.5, .9 * (d.w || 1)));

  // node groups — one kuih bangkit rosette per area of law
  const g = svg.selectAll('g.n').data(nodes.filter(n => !n.centre), d => d.id);
  g.exit().transition().duration(450).style('opacity', 0).remove();
  const enter = g.enter().append('g').attr('class', 'n').style('cursor', 'pointer').attr('filter', 'url(#soft)');
  enter.append('circle').attr('class', 'halo').attr('fill', 'none');
  enter.append('path').attr('class', 'body');
  enter.append('circle').attr('class', 'dot');
  enter.append('text').attr('class', 'cnt').attr('text-anchor', 'middle').attr('dy', '.34em');
  enter.append('text').attr('class', 'lbl').attr('text-anchor', 'middle');
  const all = enter.merge(g);
  all.attr('tabindex', 0).attr('role', 'button')
    .attr('aria-label', d => `${d.label} — ${d.count} cookie${d.count > 1 ? 's' : ''}, press Enter to open`);
  all.select('path.body').transition().duration(600)
    .attr('d', d => rosette(r(d.count), petals(d)))
    .attr('fill', 'url(#bangkit)')
    .attr('stroke', d => d.high ? 'var(--terracotta)' : 'var(--line-strong)')
    .attr('stroke-opacity', 1)
    .attr('stroke-width', d => d.high ? 2 : 1.2)
    .attr('stroke-dasharray', d => d.high ? '1 4' : null)
    .attr('stroke-linecap', 'round');
  all.select('circle.dot')
    .attr('fill', '#C2412E')
    .attr('cy', d => -r(d.count) * 0.42)
    .transition().duration(600)
    .attr('r', d => d.high ? Math.max(4, r(d.count) * 0.12) : 0);
  all.select('circle.halo')
    .attr('stroke', 'var(--terracotta)').attr('stroke-opacity', .45)
    .attr('r', d => r(d.count) + 9)
    .style('display', d => d.high ? null : 'none');
  all.select('text.cnt').text(d => d.count).attr('font-size', d => 13 + Math.sqrt(d.count) * 2).attr('fill', 'var(--chip)');
  all.select('text.lbl').text(d => d.label.replace(' and ', ' & ').replace(' Law', ''))
    .attr('dy', d => r(d.count) + 16);
  all.on('mousemove', (ev, d) => {
      if (focusArea && focusArea.id === d.id) { tip.style.opacity = 0; return; }
      tip.style.opacity = 1; tip.style.left = (ev.clientX + 16) + 'px'; tip.style.top = (ev.clientY - 10) + 'px';
      tip.innerHTML = `<span class="a">${esc(d.label)} · ${d.count} cookie${d.count > 1 ? 's' : ''}</span><ul>` +
        d.cookies.slice(0, 4).map(c => `<li>${c.sig === 'high' ? '▲ ' : ''}${c.kind === 'judgment' ? '⬡ ' : '○ '}${esc(c.h)}…</li>`).join('') +
        (d.cookies.length > 4 ? `<li>+${d.cookies.length - 4} more</li>` : '') + '</ul>';
    }).on('mouseleave', () => { tip.style.opacity = 0; });
  all.on('click', (ev, d) => { ev.stopPropagation(); tip.style.opacity = 0; toggleFocus(d); })
    .on('keydown', (ev, d) => {
      if (ev.key === 'Enter') { ev.preventDefault(); ev.stopPropagation(); toggleFocus(d); }
    });

  // centre date node — yellow price tag
  const c = svg.selectAll('g.c').data([day.date]);
  const cEnter = c.enter().append('g').attr('class', 'c').attr('transform', `translate(${CX},${CY})`);
  cEnter.append('rect').attr('x', -34).attr('y', -26).attr('width', 68).attr('height', 52).attr('rx', 2)
    .attr('fill', '#F7E27D').attr('stroke', '#D9B43B').attr('transform', 'rotate(-2)');
  cEnter.append('text').attr('class', 'lbl').attr('text-anchor', 'middle').attr('dy', '-3');
  cEnter.append('text').attr('class', 'cnt').attr('text-anchor', 'middle').attr('dy', '15').attr('font-size', '17');
  svg.select('g.c .lbl').attr('fill', 'var(--terracotta)')
    .text(when.toLocaleDateString('en-SG', { month: 'short' }).toUpperCase());
  svg.select('g.c .cnt').text(+day.date.slice(8));

  // pulse animation on halos
  svg.selectAll('circle.halo')
    .transition().duration(1600).ease(d3.easeSinInOut).attr('stroke-opacity', .12)
    .transition().duration(1600).ease(d3.easeSinInOut).attr('stroke-opacity', .45)
    .on('end', function repeat() {
      d3.select(this)
        .transition().duration(1600).ease(d3.easeSinInOut).attr('stroke-opacity', .12)
        .transition().duration(1600).ease(d3.easeSinInOut).attr('stroke-opacity', .45)
        .on('end', repeat);
    });
}

function tick() {
  svg.selectAll('line.e')
    .attr('x1', d => d.source.x).attr('y1', d => d.source.y)
    .attr('x2', d => d.target.x).attr('y2', d => d.target.y);
  svg.selectAll('g.n').attr('transform', d => `translate(${d.x},${d.y})`);
}

/* ── Level 2 — focus mode (cluster expansion) ────────────────────── */

function toggleFocus(d) {
  if (focusArea && focusArea.id === d.id) exitFocus();
  else enterFocus(d);
}

function enterFocus(d) {
  exitFocus();
  focusArea = d;
  if (sim) sim.stop();                 // freeze the skyline under the fan
  svg.classed('focused', true);
  svg.selectAll('g.n').classed('focus-area', n => n.id === d.id);
  if (mobileQuery.matches) renderFocusList(d);
  else renderFocusNodes(d);
}

function exitFocus() {
  if (!focusArea) return;
  const wasFocused = focusArea.id;
  focusArea = null;
  if (focusSim) { focusSim.stop(); focusSim = null; }
  svg.classed('focused', false);
  svg.selectAll('g.focus-layer').remove();
  svg.selectAll('g.n').classed('focus-area', false);
  focusList.hidden = true;
  focusList.innerHTML = '';
  if (sim) sim.alpha(0.15).restart();
  // return keyboard focus to the rosette that was expanded (a11y)
  const origin = svg.selectAll('g.n').filter(n => n.id === wasFocused).node();
  if (origin && typeof origin.focus === 'function') origin.focus();
}

// Desktop: fan the area's cookies out around its rosette as typed nodes,
// with chip-brown concept dots pulling sharing cookies into a huddle.
function renderFocusNodes(d) {
  const cookies = d.cookies.map(c => Object.assign({}, c, { type: 'cookie' }));
  const ring = r(d.count) + 48;
  cookies.forEach((c, i) => {
    const a = (i / cookies.length) * 2 * Math.PI - Math.PI / 2;
    c.x = d.x + Math.cos(a) * ring;
    c.y = d.y + Math.sin(a) * ring;
  });

  // concept chips: only concepts shared by 2+ cookies in the focused area;
  // single-cookie concepts stay invisible — the specks on the cookie itself
  // are decoration enough.
  const byConcept = new Map();
  cookies.forEach(c => (c.concepts || []).forEach(label => {
    if (!byConcept.has(label)) byConcept.set(label, []);
    byConcept.get(label).push(c);
  }));
  const chips = [], chipLinks = [];
  byConcept.forEach((shared, label) => {
    if (shared.length < 2) return;
    const chip = { type: 'chip', label, x: d.x, y: d.y - ring * 0.4 };
    chips.push(chip);
    shared.forEach(c => chipLinks.push({ source: chip, target: c }));
  });

  const layer = svg.append('g').attr('class', 'focus-layer');
  const linkSel = layer.selectAll('line.chip-link').data(chipLinks).enter()
    .append('line').attr('class', 'chip-link');
  const chipSel = layer.selectAll('circle.chip-node').data(chips).enter()
    .append('circle').attr('class', 'chip-node').attr('r', 4.2);
  chipSel.append('title').text(c => c.label);

  const cg = layer.selectAll('g.cookie').data(cookies).enter().append('g')
    .attr('class', c => 'cookie' + (c.sig === 'high' ? ' hot' : ''))
    .attr('tabindex', 0).attr('role', 'button')
    .attr('aria-label', c => (c.headline || c.h || '') + ', press Enter for detail')
    .style('cursor', 'pointer').attr('filter', 'url(#soft)');
  cg.each(function (c) {
    const g = d3.select(this), R = cookieR(c);
    // disc for news, hexagon for judgments; pineapple-tart fill when high
    if (c.kind === 'judgment') g.append('path').attr('class', 'body').attr('d', hexPath(R));
    else g.append('circle').attr('class', 'body').attr('r', R);
    g.select('.body')
      .attr('fill', c.sig === 'high' ? 'url(#tart)' : 'url(#bangkit)')
      .attr('stroke', c.sig === 'high' ? 'var(--terracotta)' : 'var(--line-strong)')
      .attr('stroke-width', c.sig === 'high' ? 1.6 : 1)
      .attr('stroke-dasharray', c.sig === 'high' ? '1 3' : null)
      .attr('stroke-linecap', 'round');
    // the chips on each cookie are its FOLIO concepts
    const specks = Math.min((c.concepts || []).length, 3);
    for (let i = 0; i < specks; i++) {
      const a = (i / 3) * 2 * Math.PI + 0.7;
      g.append('circle').attr('class', 'speck').attr('r', 1.6)
        .attr('cx', Math.cos(a) * R * 0.45).attr('cy', Math.sin(a) * R * 0.45);
    }
    if (c.sig === 'high') {
      g.append('circle').attr('class', 'dot').attr('fill', '#C2412E')
        .attr('cy', -R * 0.55).attr('r', 2.6);
    }
  });
  cg.on('click', (ev, c) => { ev.stopPropagation(); tip.style.opacity = 0; openPanel(c, ev.currentTarget); })
    .on('keydown', function (ev, c) {
      if (ev.key === 'Enter') { ev.preventDefault(); ev.stopPropagation(); openPanel(c, this); }
    })
    .on('mousemove', (ev, c) => {
      tip.style.opacity = 1; tip.style.left = (ev.clientX + 16) + 'px'; tip.style.top = (ev.clientY - 10) + 'px';
      tip.innerHTML = `<span class="a">${c.sig === 'high' ? '▲ ' : ''}${c.kind === 'judgment' ? '⬡ judgment' : '○ news'}${c.src ? ' · ' + esc(c.src) : ''}</span>` + esc(c.h || '');
    }).on('mouseleave', () => { tip.style.opacity = 0; });

  focusSim = d3.forceSimulation(cookies.concat(chips))
    .force('link', d3.forceLink(chipLinks).distance(30).strength(.6))
    .force('charge', d3.forceManyBody().strength(-50))
    .force('collide', d3.forceCollide(n => n.type === 'chip' ? 7 : cookieR(n) + 8))
    .force('radial', d3.forceRadial(n => n.type === 'chip' ? ring * 0.72 : ring, d.x, d.y).strength(.5))
    .on('tick', () => {
      cookies.concat(chips).forEach(n => {
        n.x = Math.max(16, Math.min(W - 16, n.x));
        n.y = Math.max(16, Math.min(H - 16, n.y));
      });
      linkSel.attr('x1', l => l.source.x).attr('y1', l => l.source.y)
        .attr('x2', l => l.target.x).attr('y2', l => l.target.y);
      chipSel.attr('cx', n => n.x).attr('cy', n => n.y);
      cg.attr('transform', n => `translate(${n.x},${n.y})`);
    });
}

// Mobile (max-width 720px): a well-sorted list beats spatial — stacked
// price-card list under the SVG (PRD section 7.7).
function renderFocusList(d) {
  focusList.innerHTML = '';
  const head = document.createElement('p');
  head.className = 'fl-head';
  head.textContent = `${d.label} — ${d.count} cookie${d.count === 1 ? '' : 's'}`;
  focusList.appendChild(head);
  d.cookies.forEach(c => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'fcard' + (c.sig === 'high' ? ' hot' : '');
    const meta = document.createElement('span');
    meta.className = 'meta';
    const tag = document.createElement('span');
    tag.className = 'tag';
    tag.textContent = c.src || (c.kind === 'judgment' ? 'JUDGMENT' : 'NEWS');
    const glyph = document.createElement('span');
    glyph.textContent = (c.kind === 'judgment' ? '⬡ judgment' : '○ news') +
      (c.sig === 'high' ? ' · ▲ still warm' : '');
    meta.appendChild(tag);
    meta.appendChild(glyph);
    const h = document.createElement('span');
    h.className = 'h';
    h.textContent = c.headline || c.h || '';
    b.appendChild(meta);
    b.appendChild(h);
    b.addEventListener('click', () => openPanel(c, b));
    focusList.appendChild(b);
  });
  focusList.hidden = false;
}

// If the viewport crosses the 720px line mid-focus, re-render the right mode.
mobileQuery.addEventListener('change', () => {
  if (!focusArea) return;
  const d = focusArea;
  exitFocus();
  enterFocus(d);
});

/* ── Level 3 — cookie detail panel ───────────────────────────────── */

function openPanel(c, fromEl) {
  openCookie = c;
  returnFocusEl = fromEl || document.activeElement;

  const bits = [(c.kind === 'judgment' ? '⬡ JUDGMENT' : '○ NEWS')];
  if (c.sig === 'high') bits.push('▲ STILL WARM');
  else if (c.sig) bits.push(c.sig.toUpperCase());
  if (c.src) bits.push(c.src);
  document.getElementById('panel-meta').textContent = bits.join(' · ');

  document.getElementById('panel-headline').textContent = c.headline || c.h || '';
  document.getElementById('panel-summary').textContent = c.summary || '';

  const why = document.getElementById('panel-why');
  why.hidden = !c.why;
  why.querySelector('span').textContent = c.why || '';

  const issuesBox = document.getElementById('panel-issues');
  const issuesList = issuesBox.querySelector('.issues');
  issuesList.innerHTML = '';
  const issues = c.issues || [];
  issues.forEach(it => {
    const det = document.createElement('details');
    if (issues.length === 1) det.open = true;
    const sum = document.createElement('summary');
    sum.textContent = it.q || '';
    const p = document.createElement('p');
    p.textContent = it.hold || '';
    det.appendChild(sum);
    det.appendChild(p);
    issuesList.appendChild(det);
  });
  issuesBox.hidden = !issues.length;

  const pills = document.getElementById('panel-concepts');
  pills.innerHTML = '';
  (c.concepts || []).forEach(label => {
    const s = document.createElement('span');
    s.className = 'pill';
    s.textContent = label;
    pills.appendChild(s);
  });
  pills.hidden = !(c.concepts || []).length;

  const go = document.getElementById('panel-source');
  if (c.url) {
    go.href = c.url;
    go.textContent = `READ ${c.kind === 'judgment' ? 'JUDGMENT' : 'SOURCE'} ↗`;
    go.hidden = false;
  } else {
    go.hidden = true;
  }

  panelBackdrop.hidden = false;
  panel.hidden = false;
  document.getElementById('panel-close').focus();
  if (active >= 0 && c.id) setHash(`${SCRUB[active].date}/${c.id}`);
}

// Visual close only — no hash side effects (used on day change / hashchange).
function hidePanel() {
  if (!panel || panel.hidden) return;
  panel.hidden = true;
  panelBackdrop.hidden = true;
  openCookie = null;
  if (returnFocusEl && document.contains(returnFocusEl)) returnFocusEl.focus();
  returnFocusEl = null;
}

function closePanel() {
  if (!panel || panel.hidden) return;
  hidePanel();
  if (active >= 0) setHash(SCRUB[active].date);
}

// Deep-link target: focus the cookie's area, then open its panel.
function openById(id) {
  if (!currentDay) return;
  const hit = findCookie(currentDay, id);
  if (!hit) return;
  const areaNode = nodes.find(n => n.label === hit.area.label);
  if (areaNode && (!focusArea || focusArea.id !== areaNode.id)) enterFocus(areaNode);
  openPanel(hit.cookie);
}

/* ── global close + navigation wiring ────────────────────────────── */

document.getElementById('panel-close').addEventListener('click', closePanel);
panelBackdrop.addEventListener('click', closePanel);

document.addEventListener('keydown', ev => {
  if (ev.key !== 'Escape') return;
  if (panel && !panel.hidden) closePanel();
  else if (focusArea) exitFocus();
});

// clicking the SVG backdrop (empty parchment) collapses focus mode
svg.on('click', ev => {
  if (focusArea && ev.target === svg.node()) exitFocus();
});

// manual hash edits / back-forward: restore without a full reload
window.addEventListener('hashchange', async () => {
  const want = parseHash();
  if (!want) { hidePanel(); exitFocus(); return; }
  const idx = SCRUB.findIndex(s => s.date === want.date);
  if (idx < 0) {
    // unknown date in the hash: normalise back to the displayed day so the
    // URL never disagrees with the page state
    hidePanel(); exitFocus();
    if (active >= 0 && SCRUB[active]) setHash(SCRUB[active].date);
    return;
  }
  if (idx !== active) await render(idx);
  if (want.cookie) {
    if (!openCookie || openCookie.id !== want.cookie) openById(want.cookie);
  } else if (openCookie) {
    hidePanel();
  }
});

/* ── boot ────────────────────────────────────────────────────────── */

(async function init() {
  let days = [];
  try {
    const res = await fetch('/data/sky/index.json');
    if (res.ok) days = (await res.json()).days || [];
  } catch (e) { days = []; }

  // At most the 14 most recent days on the scrubber; the index is ascending,
  // so slice from the tail. Older days become reachable via the archive in a
  // later phase.
  SCRUB = days.slice(-14);

  if (!SCRUB.length) {
    document.getElementById('sky').style.display = 'none';
    document.getElementById('sky-empty').hidden = false;
    document.getElementById('dayname').textContent = 'a quiet day';
    return;
  }

  const scrub = document.getElementById('scrub');
  SCRUB.forEach((d, i) => {
    const b = document.createElement('button');
    b.className = 'day';
    const dt = new Date(d.date + 'T12:00:00');
    b.innerHTML = `${dt.toLocaleDateString('en-SG', { month: 'short' }).toUpperCase()}<b>${dt.getDate()}</b>`;
    b.onclick = () => render(i);   // day change exits focus + closes the panel
    scrub.appendChild(b);
  });

  // restore deep link (#date or #date/cookieid); default = most recent day
  const want = parseHash();
  let idx = SCRUB.length - 1;
  if (want) {
    const j = SCRUB.findIndex(s => s.date === want.date);
    if (j >= 0) idx = j;
  }
  await render(idx);
  if (want && want.cookie && SCRUB[idx].date === want.date) openById(want.cookie);
})();
