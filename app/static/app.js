/* Shri Balaji Ops - dashboard client */
const S = {
  options: null,
  filters: { date_from: null, date_to: null, departments: [], categories: [], brands: [], stores: [], vehicles: [], reasons: [] },
  widgets: ['headline', 'trend', 'stores', 'categories', 'products', 'swaps', 'rejects', 'quality'],
  data: {}, sort: {}
};

const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const n = v => (v == null ? '—' : Number(v).toLocaleString('en-IN'));
const pct = v => (v == null ? '—' : Number(v).toFixed(2) + '%');

function toast(msg, ms = 4200) {
  const t = document.createElement('div');
  t.className = 'toast'; t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), ms);
}

function gapColor(p) {
  if (p >= 5) return 'var(--bad)';
  if (p >= 2) return 'var(--alert)';
  if (p >= 1) return 'var(--cold)';
  return 'var(--good)';
}

/* ---------------- tabs ---------------- */
$$('.tab').forEach(t => t.onclick = () => {
  $$('.tab').forEach(x => x.classList.remove('active'));
  $$('.view').forEach(x => x.classList.remove('active'));
  t.classList.add('active');
  $('#view-' + t.dataset.view).classList.add('active');
  // filters are irrelevant on admin/config screens
  const hide = ['upload', 'alerts', 'team', 'views'].includes(t.dataset.view);
  $('#filterHost').style.display = hide ? 'none' : '';
  if (t.dataset.view === 'quality') loadUploads();
  if (t.dataset.view === 'team') loadUsers();
  if (t.dataset.view === 'alerts') loadAlertPreview();
  if (t.dataset.view === 'views') loadTemplates();
  if (t.dataset.view === 'funnel') { loadFunnel(); loadPoTrace(); }
});

/* ---------------- multiselects ---------------- */
function buildMulti(el) {
  const key = el.dataset.key;
  const raw = S.options[key] || [];
  const items = raw.map(o => typeof o === 'string' ? { id: o, name: o } : o);
  el.innerHTML = `
    <button class="ms-btn" type="button"><span class="lbl">All</span><span class="cnt"></span></button>
    <div class="ms-pop">
      <input class="ms-search" placeholder="Search…">
      <div class="ms-opts"></div>
    </div>`;
  const btn = el.querySelector('.ms-btn'), pop = el.querySelector('.ms-pop'),
        box = el.querySelector('.ms-opts'), search = el.querySelector('.ms-search');

  const render = (q = '') => {
    box.innerHTML = items
      .filter(i => i.name.toLowerCase().includes(q.toLowerCase()))
      .map(i => `<label class="ms-opt"><input type="checkbox" value="${i.id}"
        ${S.filters[key].includes(i.id) ? 'checked' : ''}><span>${i.name}</span></label>`).join('')
      || '<div class="empty" style="padding:14px">No matches</div>';
    box.querySelectorAll('input').forEach(cb => cb.onchange = () => {
      if (cb.checked) S.filters[key].push(cb.value);
      else S.filters[key] = S.filters[key].filter(v => v !== cb.value);
      syncMulti(el); renderChips();
    });
  };
  btn.onclick = e => {
    e.stopPropagation();
    const open = pop.classList.contains('open');
    $$('.ms-pop').forEach(p => p.classList.remove('open'));
    if (!open) { pop.classList.add('open'); render(search.value); search.focus(); }
  };
  search.oninput = () => render(search.value);
  pop.onclick = e => e.stopPropagation();
  el._render = render;
  syncMulti(el);
}

function syncMulti(el) {
  const key = el.dataset.key, sel = S.filters[key];
  const lbl = el.querySelector('.lbl'), cnt = el.querySelector('.cnt');
  if (!sel.length) { lbl.textContent = 'All'; cnt.textContent = ''; }
  else if (sel.length === 1) {
    const raw = S.options[key] || [];
    const item = raw.map(o => typeof o === 'string' ? { id: o, name: o } : o).find(i => i.id === sel[0]);
    lbl.textContent = item ? item.name : sel[0]; cnt.textContent = '';
  } else { lbl.textContent = sel.length + ' selected'; cnt.textContent = '●'; }
}

document.addEventListener('click', () => $$('.ms-pop').forEach(p => p.classList.remove('open')));

function renderChips() {
  const parts = [];
  const label = { categories: 'Category', brands: 'Brand', stores: 'Store', vehicles: 'Vehicle' };
  for (const k of ['categories', 'brands', 'stores', 'vehicles']) {
    S.filters[k].forEach(v => parts.push(
      `<span class="chip"><b>${label[k]}</b> ${v} <span class="x" data-k="${k}" data-v="${v}">✕</span></span>`));
  }
  const cb = $('#chipbar');
  cb.innerHTML = parts.join('');
  cb.querySelectorAll('.x').forEach(x => x.onclick = () => {
    S.filters[x.dataset.k] = S.filters[x.dataset.k].filter(v => v !== x.dataset.v);
    $$('.ms').forEach(el => { syncMulti(el); if (el._render) el._render(); });
    renderChips(); load();
  });
}

/* ---------------- date range ---------------- */
function applyQuickRange() {
  const v = $('#quickRange').value;
  if (v === '') return;
  const max = S.options.date_max, min = S.options.date_min;
  if (!max) return;
  if (v === '0') { $('#dateFrom').value = min; $('#dateTo').value = max; }
  else {
    const end = new Date(max);
    const start = new Date(max);
    start.setDate(end.getDate() - (parseInt(v) - 1));
    const iso = d => d.toISOString().slice(0, 10);
    $('#dateFrom').value = iso(start) < min ? min : iso(start);
    $('#dateTo').value = max;
  }
}

$('#quickRange').onchange = () => { applyQuickRange(); load(); };
$('#applyBtn').onclick = () => load();
$('#resetBtn').onclick = () => {
  for (const k of ['categories', 'brands', 'stores', 'vehicles', 'reasons']) S.filters[k] = [];
  $('#quickRange').value = '7'; applyQuickRange();
  $$('.ms').forEach(el => { syncMulti(el); if (el._render) el._render(); });
  renderChips(); load();
};

/* ---------------- load ---------------- */
async function boot() {
  S.options = await (await fetch('/api/options')).json();
  $$('.ms').forEach(buildMulti);
  applyQuickRange();
  renderChips();
  $('#coverage').textContent = S.options.date_min
    ? `${S.options.stores.length} stores · data ${S.options.date_min} → ${S.options.date_max}`
    : 'no data loaded yet';
  buildWidgetPicker();
  await load();
}

async function load() {
  S.filters.date_from = $('#dateFrom').value || null;
  S.filters.date_to = $('#dateTo').value || null;
  $('#kpis').innerHTML = '<div class="kpi"><span class="spinner"></span></div>';
  const r = await fetch('/api/dashboard', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...S.filters, widgets: S.widgets })
  });
  if (!r.ok) { toast('Could not load data'); return; }
  S.data = await r.json();
  renderAll();
  if ($('#view-funnel').classList.contains('active')) { loadFunnel(); loadPoTrace(); }
}

function renderAll() {
  const d = S.data;
  if (d.headline) { renderKpis(d.headline); renderFunnel(d.headline); }
  if (d.trend) renderTrend(d.trend);
  if (d.stores) renderStores(d.stores);
  if (d.categories) renderCats(d.categories);
  if (d.products) renderProducts(d.products);
  if (d.swaps) renderSwaps(d.swaps);
  if (d.rejects) renderRejects(d.rejects);
  if (d.quality) renderQuality(d.quality);
}

/* ---------------- renderers ---------------- */
function renderKpis(h) {
  const progress = h.claimable_pct > 0 ? Math.min(100, (0.2 / h.claimable_pct) * 100) : 100;
  const worst = (S.data.categories || []).filter(c => c.dispatched > 300)
    .sort((a, b) => b.gap_pct - a.gap_pct)[0];
  $('#kpis').innerHTML = `
    <div class="kpi hero">
      <div class="kpi-label">Claimable GRN gap</div>
      <div class="kpi-val">${pct(h.claimable_pct)}</div>
      <div class="kpi-sub">target <b>0.20%</b> — dispatched less received, damage excluded</div>
      <div class="track"><div class="fill" style="width:${progress}%"></div></div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Units unaccounted</div>
      <div class="kpi-val">${n(h.claimable_units)}</div>
      <div class="kpi-sub">of ${n(h.dispatched)} dispatched</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Warehouse fulfilment</div>
      <div class="kpi-val">${pct(h.fulfillment_pct)}</div>
      <div class="kpi-sub">${n(h.fulfillment_gap_units)} units never left — not claimable</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Worst category</div>
      <div class="kpi-val" style="font-size:20px;color:${worst ? gapColor(worst.gap_pct) : 'var(--muted)'}">
        ${worst ? worst.category : '—'}</div>
      <div class="kpi-sub">${worst ? pct(worst.gap_pct) + ' · ' + n(worst.claimable_units) + ' units' : 'no data'}</div>
    </div>`;
}

function renderFunnel(h) {
  const pickLoss = h.ordered - h.picked, transitLoss = h.dispatched - h.received;
  const stage = (v, l, loss, cls, end) => `
    <div class="fstage">
      <div class="fval" ${end ? 'style="color:var(--good)"' : ''}>${n(v)}</div>
      <div class="flabel">${l}</div>
      <div class="fbar ${end ? 'end' : ''}"></div>
      ${loss ? `<div class="floss ${cls}">−${n(loss)} ${cls === 'big' ? 'at picking' : 'in transit / at store'}</div>` : ''}
    </div>`;
  $('#funnel').innerHTML =
    stage(h.ordered, 'Ordered by stores') + '<div class="farrow">→</div>' +
    stage(h.picked, 'Picked & dispatched', pickLoss > 0 ? pickLoss : 0, 'big') +
    '<div class="farrow">→</div>' +
    stage(h.received, 'Received at store', transitLoss > 0 ? transitLoss : 0, 'small') +
    '<div class="farrow">→</div>' +
    stage(h.received - h.damaged, 'Sellable on shelf', 0, '', true);
}

function renderTrend(rows) {
  if (!rows.length) { $('#trend').innerHTML = '<div class="empty">No data in this range.</div>'; return; }
  const max = Math.max(...rows.map(r => r.claimable_pct), 1);
  const w = 100 / rows.length;
  $('#trend').innerHTML = `
    <div style="display:flex;align-items:flex-end;gap:3px;height:130px;padding-top:8px">
      ${rows.map(r => `
        <div style="flex:1;text-align:center" title="${r.date}: ${pct(r.claimable_pct)} — ${n(r.dispatched)} dispatched">
          <div style="font-family:'IBM Plex Mono';font-size:10px;color:var(--muted);margin-bottom:4px">${r.claimable_pct}</div>
          <div style="height:${Math.max(3, (r.claimable_pct / max) * 88)}px;background:${gapColor(r.claimable_pct)};border-radius:4px 4px 0 0;opacity:.85"></div>
        </div>`).join('')}
    </div>
    <div style="display:flex;gap:3px;margin-top:6px">
      ${rows.map(r => `<div style="flex:1;text-align:center;font-size:9.5px;color:var(--muted)" class="mono">${r.date.slice(5)}</div>`).join('')}
    </div>`;
}

function sortable(tblId, rows, cols, key) {
  const st = S.sort[key] || { col: cols.findIndex(c => c.sort) || 0, dir: -1 };
  const c = cols[st.col];
  if (c && c.field) {
    rows = [...rows].sort((a, b) => {
      const x = a[c.field], y = b[c.field];
      return (typeof x === 'string' ? x.localeCompare(y) : x - y) * st.dir;
    });
  }
  const el = $('#' + tblId);
  el.innerHTML = `<thead><tr>${cols.map((c, i) =>
    `<th class="${c.field ? '' : 'no-sort'}" data-i="${i}">${c.label}${st.col === i ? (st.dir < 0 ? ' ↓' : ' ↑') : ''}</th>`).join('')}</tr></thead>
    <tbody>${rows.map(r => `<tr>${cols.map(c => `<td class="${c.cls || ''}">${c.render(r)}</td>`).join('')}</tr>`).join('')}</tbody>`;
  el.querySelectorAll('th[data-i]').forEach(th => th.onclick = () => {
    const i = +th.dataset.i;
    if (!cols[i].field) return;
    S.sort[key] = { col: i, dir: st.col === i ? -st.dir : -1 };
    sortable(tblId, rows, cols, key);
  });
}

function renderStores(rows) {
  if (!rows.length) { $('#storeTbl').innerHTML = '<tbody><tr><td class="empty">No data.</td></tr></tbody>'; return; }
  const max = Math.max(...rows.map(r => r.gap_pct), 1);
  sortable('storeTbl', rows, [
    { label: 'Store', cls: 'name', render: r => r.name || r.warehouse_id, field: 'name' },
    { label: 'Flagged', render: r => r.days_total ? `${r.days_flagged}/${r.days_total}` : '—', field: 'days_flagged' },
    { label: 'Gap %', field: 'gap_pct', sort: 1, render: r =>
      `<span class="heat"><i style="width:${(r.gap_pct / max) * 100}%;background:${gapColor(r.gap_pct)}"></i></span>${pct(r.gap_pct)}` },
    { label: 'Units', render: r => n(r.claimable_units), field: 'claimable_units' },
    { label: 'Status', render: r => `<span class="pill ${r.status === 'repeat' ? 'crit' : r.status === 'watch' ? 'warn' : 'ok'}">${
      r.status === 'repeat' ? 'Repeat' : r.status === 'watch' ? 'Watch' : 'Clean'}</span>`, field: 'status' }
  ], 'stores');
}

function renderCats(rows) {
  if (!rows.length) { $('#catList').innerHTML = '<div class="empty">No data.</div>'; return; }
  const max = Math.max(...rows.map(r => r.gap_pct), 1);
  $('#catList').innerHTML = rows.map(r => `
    <div class="catrow">
      <div class="catname">${r.category}</div>
      <div class="catbar"><i style="width:${(r.gap_pct / max) * 100}%;background:${gapColor(r.gap_pct)}"></i></div>
      <div class="catval"><b>${pct(r.gap_pct)}</b><br><span style="font-size:10px">${n(r.claimable_units)} u</span></div>
    </div>`).join('');
}

function renderProducts(rows) {
  if (!rows.length) { $('#prodTbl').innerHTML = '<tbody><tr><td class="empty">Nothing short in this selection.</td></tr></tbody>'; return; }
  sortable('prodTbl', rows, [
    { label: 'Product', cls: 'name', render: r => r.description || r.fsn, field: 'description' },
    { label: 'Category', cls: 'name', render: r => r.category || '—', field: 'category' },
    { label: 'Stores hit', render: r => n(r.stores_affected), field: 'stores_affected' },
    { label: 'Dispatched', render: r => n(r.dispatched), field: 'dispatched' },
    { label: 'Short', render: r => n(r.claimable_units), field: 'claimable_units', sort: 1 },
    { label: 'Gap %', render: r => `<span style="color:${gapColor(r.gap_pct)}">${pct(r.gap_pct)}</span>`, field: 'gap_pct' }
  ], 'products');
}

function renderSwaps(rows) {
  if (!rows.length) { $('#swapList').innerHTML = '<div class="empty">No excess/shortage pairs in this range.</div>'; return; }
  $('#swapList').innerHTML = rows.map(r => `
    <div style="border-bottom:1px solid rgba(42,51,61,.55);padding:14px 0">
      <div style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:baseline">
        <div><b style="font-size:13px">${r.description || r.fsn}</b>
          <span class="muted mono" style="font-size:11px;margin-left:8px">${r.date}</span></div>
        <div>
          ${r.same_vehicle_pairs
            ? `<span class="pill crit">${r.same_vehicle_pairs} same-vehicle pair${r.same_vehicle_pairs > 1 ? 's' : ''}</span>`
            : `<span class="pill info">no route link</span>`}
          <span class="pill ${r.spread === 'concentrated' ? 'warn' : 'ok'}">${r.spread}</span>
        </div>
      </div>
      <div class="mono muted" style="font-size:11.5px;margin-top:7px">
        short ${n(r.total_short)} across ${r.stores_short} store(s) &nbsp;·&nbsp;
        excess ${n(r.total_excess)} at ${r.stores_excess} store(s)
      </div>
      <div style="display:flex;gap:22px;margin-top:9px;flex-wrap:wrap;font-size:11.5px">
        <div><div class="muted" style="margin-bottom:3px">Excess at</div>
          ${r.excess_stores.map(s => `<div class="mono">${s.store} +${s.qty}${s.vehicle ? ` <span class="muted">(${s.vehicle})</span>` : ''}</div>`).join('')}</div>
        <div><div class="muted" style="margin-bottom:3px">Short at</div>
          ${r.short_stores.map(s => `<div class="mono">${s.store} −${s.qty}${s.vehicle ? ` <span class="muted">(${s.vehicle})</span>` : ''}</div>`).join('')}</div>
      </div>
    </div>`).join('');
}

function renderRejects(rows) {
  if (!rows.length) { $('#rejTbl').innerHTML = '<tbody><tr><td class="empty">No reject records.</td></tr></tbody>'; return; }
  sortable('rejTbl', rows, [
    { label: 'Reason', cls: 'name', render: r => r.reason, field: 'reason' },
    { label: 'Category', cls: 'name', render: r => r.category, field: 'category' },
    { label: 'Incidents', render: r => n(r.incidents), field: 'incidents' },
    { label: 'Units', render: r => n(r.qty), field: 'qty', sort: 1 }
  ], 'rejects');
}

function renderQuality(rows) {
  $('#qualityList').innerHTML = rows.length
    ? rows.map(i => `<div class="issue"><div class="sev ${i.severity}"></div>
        <div><b>${i.title}</b><p>${i.detail}</p></div></div>`).join('')
    : '<div class="empty">No issues found in the loaded data.</div>';
}

/* ---------- funnel / cycle ---------- */
let funnelGroupBy = 'brand';

$('#funnelGroupBy') && $('#funnelGroupBy').querySelectorAll('.seg-btn').forEach(b => {
  b.onclick = () => {
    $('#funnelGroupBy').querySelectorAll('.seg-btn').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    funnelGroupBy = b.dataset.v;
    loadFunnel();
  };
});

async function loadFunnel() {
  $('#funnelTbl').innerHTML = '<div class="empty">Loading…</div>';
  S.filters.date_from = $('#dateFrom').value || null;
  S.filters.date_to = $('#dateTo').value || null;
  const j = await (await fetch('/api/funnel', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...S.filters, group_by: funnelGroupBy })
  })).json();
  renderCycleFunnel(j.rows || [], funnelGroupBy);
}

function gapCell(val, base) {
  if (!val) return `<span class="gap-ok">0</span>`;
  const pct = base ? Math.round(100 * val / base) : 0;
  return `<span class="gap-bad">${n(val)}${base ? ` <small>(${pct}%)</small>` : ''}</span>`;
}

function renderCycleFunnel(rows, groupBy) {
  const keyLbl = groupBy === 'fsn' ? 'FSN' : 'Brand';
  if (!rows.length) {
    $('#funnelTbl').innerHTML = '<div class="empty">No data for this filter — try widening the date range, '
      + 'or check that Indent and Warehouse Inbound files have been uploaded.</div>';
    return;
  }
  $('#funnelTbl').innerHTML = `
    <thead><tr>
      <th>${keyLbl}</th>${groupBy === 'fsn' ? '<th>Brand</th>' : ''}
      <th class="num">PO raised</th><th class="num">Vendor delivered</th><th class="num">Vendor gap</th>
      <th class="num">Store ordered</th><th class="num">Picked</th><th class="num">Fulfillment gap</th>
      <th class="num">Store received</th><th class="num">Claimable gap</th>
    </tr></thead>
    <tbody>${rows.map(r => `
      <tr>
        <td class="name">${groupBy === 'fsn' ? r.fsn : r.brand}</td>
        ${groupBy === 'fsn' ? `<td>${r.brand}</td>` : ''}
        <td class="num">${n(r.indent_qty)}</td>
        <td class="num">${n(r.inbound_received)}</td>
        <td class="num">${gapCell(r.vendor_gap, r.indent_qty)}</td>
        <td class="num">${n(r.store_ordered)}</td>
        <td class="num">${n(r.picked)}</td>
        <td class="num">${gapCell(r.fulfillment_gap, r.store_ordered)}</td>
        <td class="num">${n(r.store_received)}</td>
        <td class="num">${gapCell(r.claimable_gap, r.picked)}</td>
      </tr>`).join('')}
    </tbody>`;
}

async function loadPoTrace() {
  $('#poTraceTbl').innerHTML = '<div class="empty">Loading…</div>';
  const j = await (await fetch('/api/po-trace', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(S.filters)
  })).json();
  renderPoTrace(j.rows || []);
}

function renderPoTrace(rows) {
  if (!rows.length) {
    $('#poTraceTbl').innerHTML = '<div class="empty">No indent or warehouse-inbound data for this filter yet.</div>';
    return;
  }
  $('#poTraceTbl').innerHTML = `
    <thead><tr>
      <th>PO Reference</th><th>Brand</th><th>FSN</th>
      <th class="num">Indent qty</th><th class="num">Inbound received</th><th class="num">Vendor gap</th>
    </tr></thead>
    <tbody>${rows.map(r => `
      <tr>
        <td class="name">${r.matched
          ? r.po_reference
          : `<span class="tag-unref">No reference</span>`}</td>
        <td>${r.brand}</td>
        <td>${r.fsn}</td>
        <td class="num">${n(r.indent_qty)}</td>
        <td class="num">${n(r.inbound_received)}</td>
        <td class="num">${gapCell(r.vendor_gap, r.indent_qty)}</td>
      </tr>`).join('')}
    </tbody>`;
}

/* ---------- upload templates ---------- */
let uploadTmplLoaded = false;

async function loadUploadTemplates() {
  if (uploadTmplLoaded) return;
  try {
    const j = await (await fetch('/api/templates')).json();
    $('#tmplList').innerHTML = (j.templates || []).map(t => `
      <div class="tmpl-row">
        <div class="tmpl-stage">${t.stage}</div>
        <div class="tmpl-main">
          <div class="tmpl-name">${t.label}</div>
          <div class="tmpl-why">${t.why}</div>
          <div class="tmpl-cols">${(t.headers || []).join('  ·  ')}</div>
          ${(t.notes || []).map(n => `<div class="tmpl-note">${n}</div>`).join('')}
        </div>
        <div class="tmpl-dl"><a class="btn ghost sm" href="/api/template/${t.key}">CSV</a></div>
      </div>`).join('');
    uploadTmplLoaded = true;
  } catch (e) {
    $('#tmplList').innerHTML =
      '<div class="tmpl-note">Could not load the list — the "Download all" zip still works.</div>';
  }
}

$('#tmplToggle') && ($('#tmplToggle').onclick = async () => {
  const box = $('#tmplList'), btn = $('#tmplToggle');
  if (box.style.display !== 'none') {
    box.style.display = 'none'; btn.textContent = 'Show list'; return;
  }
  await loadUploadTemplates();
  box.style.display = ''; btn.textContent = 'Hide list';
});

async function loadUploads() {
  const rows = await (await fetch('/api/uploads')).json();
  $('#uploadTbl').innerHTML = rows.length ? `
    <thead><tr><th class="no-sort">When</th><th class="no-sort">File</th><th class="no-sort">Type</th>
    <th class="no-sort">Dates</th><th class="no-sort">Loaded</th><th class="no-sort">Excluded</th>
    <th class="no-sort">Notes</th></tr></thead>
    <tbody>${rows.map(r => `<tr>
      <td>${r.at}</td><td class="name">${r.filename}</td>
      <td><span class="pill ${r.status === 'ok' ? 'ok' : 'crit'}">${r.type}</span></td>
      <td style="font-size:11px">${r.dates || '—'}</td>
      <td>${n(r.loaded)}</td><td>${r.dropped ? n(r.dropped) : '—'}</td>
      <td class="name muted" style="font-size:11px;max-width:340px">${r.notes || ''}</td></tr>`).join('')}</tbody>`
    : '<tbody><tr><td class="empty">Nothing uploaded yet.</td></tr></tbody>';
}

/* ---------------- saved views ---------------- */
const WIDGET_LABELS = {
  headline: 'KPIs', trend: 'Trend', stores: 'Store ranking', categories: 'Categories',
  products: 'Products', swaps: 'Route & swaps', rejects: 'Rejects', quality: 'Data quality'
};

function buildWidgetPicker() {
  $('#widgetPicker').innerHTML = Object.entries(WIDGET_LABELS).map(([k, l]) =>
    `<label><input type="checkbox" value="${k}" ${S.widgets.includes(k) ? 'checked' : ''}>${l}</label>`).join('');
  $('#widgetPicker').querySelectorAll('input').forEach(cb => cb.onchange = () => {
    S.widgets = [...$('#widgetPicker').querySelectorAll('input:checked')].map(i => i.value);
  });
}

async function loadTemplates() {
  const rows = await (await fetch('/api/templates')).json();
  $('#tmplList').innerHTML = rows.length ? rows.map(t => `
    <div class="tmpl">
      <div><b>${t.name}</b>
        <div class="tmpl-meta">${describeConfig(t.config)}</div></div>
      <div style="display:flex;gap:7px">
        <button class="btn sm" data-load="${t.id}">Open</button>
        <button class="btn sm ghost" data-del="${t.id}">Delete</button>
      </div>
    </div>`).join('') : '<div class="empty">No saved views yet.</div>';

  $('#tmplList').querySelectorAll('[data-load]').forEach(b => b.onclick = () => {
    const t = rows.find(x => x.id == b.dataset.load);
    Object.assign(S.filters, { departments: [], categories: [], brands: [], stores: [], vehicles: [], reasons: [] }, t.config.filters);
    S.widgets = t.config.widgets.length ? t.config.widgets : S.widgets;
    $('#dateFrom').value = S.filters.date_from || '';
    $('#dateTo').value = S.filters.date_to || '';
    $('#quickRange').value = '';
    $$('.ms').forEach(el => { syncMulti(el); if (el._render) el._render(); });
    buildWidgetPicker(); renderChips();
    $$('.tab')[0].click(); load();
    toast(`Opened “${t.name}”`);
  });
  $('#tmplList').querySelectorAll('[data-del]').forEach(b => b.onclick = async () => {
    await fetch('/api/templates/' + b.dataset.del, { method: 'DELETE' });
    loadTemplates();
  });
}

function describeConfig(c) {
  const f = c.filters || {}, bits = [];
  if (f.date_from || f.date_to) bits.push(`${f.date_from || '…'} → ${f.date_to || '…'}`);
  for (const k of ['categories', 'brands', 'stores', 'vehicles'])
    if (f[k] && f[k].length) bits.push(`${f[k].length} ${k}`);
  bits.push(`${(c.widgets || []).length} panels`);
  return bits.join(' · ');
}

$('#saveTmpl').onclick = async () => {
  const name = $('#tmplName').value.trim();
  if (!name) { toast('Give the view a name first'); return; }
  S.filters.date_from = $('#dateFrom').value || null;
  S.filters.date_to = $('#dateTo').value || null;
  const r = await fetch('/api/templates', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, filters: S.filters, widgets: S.widgets })
  });
  if (r.ok) { $('#tmplName').value = ''; toast(`Saved “${name}”`); loadTemplates(); }
};

/* ---------------- upload ---------------- */
if (window.USER.role === 'admin') {
  let queue = [];
  const drop = $('#drop'), input = $('#fileInput');

  const refresh = () => {
    $('#fileList').innerHTML = queue.map((f, i) =>
      `<div class="fileitem"><span>${f.name}</span>
       <span class="muted mono" style="font-size:11px">${(f.size / 1024).toFixed(0)} KB
       <span style="cursor:pointer;margin-left:10px" data-rm="${i}">✕</span></span></div>`).join('');
    $('#uploadActions').style.display = queue.length ? 'flex' : 'none';
    $('#fileList').querySelectorAll('[data-rm]').forEach(x => x.onclick = () => {
      queue.splice(+x.dataset.rm, 1); refresh();
    });
  };

  ['dragenter', 'dragover'].forEach(e => drop.addEventListener(e, ev => {
    ev.preventDefault(); drop.classList.add('over');
  }));
  ['dragleave', 'drop'].forEach(e => drop.addEventListener(e, ev => {
    ev.preventDefault(); drop.classList.remove('over');
  }));
  drop.addEventListener('drop', ev => { queue.push(...ev.dataTransfer.files); refresh(); });
  input.onchange = () => { queue.push(...input.files); refresh(); input.value = ''; };
  $('#clearFiles').onclick = () => { queue = []; refresh(); $('#uploadResults').innerHTML = ''; };

  $('#uploadBtn').onclick = async () => {
    if (!queue.length) return;
    const btn = $('#uploadBtn');
    btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Importing…';
    const fd = new FormData();
    queue.forEach(f => fd.append('files', f));
    try {
      const r = await fetch('/api/upload', { method: 'POST', body: fd });

      // The old code went straight to j.results.map(). When the server
      // returned an error the body had no results array, .map() threw, and
      // the catch below reported "Import failed" - making a server-side
      // problem look like a frontend one. Each failure mode now reports
      // itself accurately.
      let j = null;
      try { j = await r.json(); } catch (_) { j = null; }

      if (!r.ok) {
        const msg = (j && (j.detail || j.error)) ||
          (r.status === 413 ? 'File too large for the server to accept.' :
           r.status === 502 || r.status === 503 ?
             'The server restarted mid-import — usually it ran out of memory. Try one file at a time, or a bigger instance.' :
             `Server returned ${r.status}.`);
        $('#uploadResults').innerHTML = `<div class="res bad"><b>Upload rejected</b> — ${msg}</div>`;
        toast('Upload rejected — see details');
        btn.disabled = false; btn.textContent = 'Import';
        return;
      }
      if (!j || !Array.isArray(j.results)) {
        $('#uploadResults').innerHTML =
          '<div class="res bad"><b>Unexpected response</b> — the import may have partly succeeded. Check the Uploads tab before re-importing.</div>';
        loadUploads && loadUploads();
        btn.disabled = false; btn.textContent = 'Import';
        return;
      }

      $('#uploadResults').innerHTML = j.results.map(x => {
        const dates = Array.isArray(x.dates) ? x.dates : [];
        const notes = Array.isArray(x.notes) ? x.notes : [];
        const size = x.size_mb ? ` <span class="muted">(${x.size_mb} MB)</span>` : '';
        return x.ok
          ? `<div class="res ok"><b>${x.type}</b> — ${n(x.rows_loaded)} rows loaded${
              x.rows_dropped ? `, ${n(x.rows_dropped)} excluded` : ''}${
              dates.length ? ` · ${dates.join(', ')}` : ''}${size}
              ${notes.length ? `<div class="rnote">${notes.join('<br>')}</div>` : ''}</div>`
          : `<div class="res bad"><b>${x.filename || 'File'}</b> — ${x.error || 'Import failed'}</div>`;
      }).join('');

      const failed = j.results.filter(x => !x.ok).length;
      queue = []; refresh();
      S.options = await (await fetch('/api/options')).json();
      $$('.ms').forEach(buildMulti);
      $('#coverage').textContent = `${S.options.stores.length} stores · data ${S.options.date_min} → ${S.options.date_max}`;
      applyQuickRange(); await load();
      toast(failed
        ? `${j.results.length - failed} of ${j.results.length} files imported — ${failed} failed`
        : 'Import finished — dashboard refreshed');
    } catch (e) {
      $('#uploadResults').innerHTML =
        `<div class="res bad"><b>Could not reach the server</b> — ${e.message}. The import may have partly succeeded; check the Uploads tab.</div>`;
      toast('Upload interrupted — see details');
    }
    btn.disabled = false; btn.textContent = 'Import';
  };

  $('#runAlerts').onclick = async () => {
    const j = await (await fetch('/api/alerts/run', { method: 'POST' })).json();
    toast(j.sent ? 'Digest emailed' : (j.note || j.reason || 'Nothing to send'));
    loadAlertPreview();
  };

  $('#addUser').onclick = async () => {
    const r = await fetch('/api/users', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: $('#nuName').value, email: $('#nuEmail').value,
        password: $('#nuPass').value, role: $('#nuRole').value })
    });
    if (r.ok) { $('#nuName').value = ''; $('#nuEmail').value = ''; loadUsers(); toast('Added'); }
    else toast((await r.json()).detail || 'Could not add');
  };
}

async function loadAlertPreview() {
  const j = await (await fetch('/api/alerts/preview')).json();
  $('#alertPreview').textContent = j.body || 'Nothing currently meets an alert rule.';
}

async function loadUsers() {
  const rows = await (await fetch('/api/users')).json();
  $('#userTbl').innerHTML = `
    <thead><tr><th class="no-sort">Name</th><th class="no-sort">Email</th>
    <th class="no-sort">Role</th><th class="no-sort"></th></tr></thead>
    <tbody>${rows.map(u => `<tr><td class="name">${u.name || '—'}</td><td class="name">${u.email}</td>
      <td><span class="pill ${u.role === 'admin' ? 'info' : 'ok'}">${u.role}</span></td>
      <td style="text-align:right">${u.email === window.USER.email ? ''
        : `<button class="btn sm ghost" data-du="${u.id}">Remove</button>`}</td></tr>`).join('')}</tbody>`;
  $('#userTbl').querySelectorAll('[data-du]').forEach(b => b.onclick = async () => {
    await fetch('/api/users/' + b.dataset.du, { method: 'DELETE' }); loadUsers();
  });
}

boot();
