'use strict';
/* CIF Explorer front end: no framework, no build step. */

const $ = (s, el = document) => el.querySelector(s);

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else el.setAttribute(k, v === true ? '' : v);
    }
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false || kid === '') continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const fmt = n => (n == null ? '' : Number(n).toLocaleString());
function fmtBytes(n) {
  if (n == null) return '';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(1) : n) + ' ' + u[i];
}
const isNull = v => v === '?' || v === '.';
const stop = fn => e => { e.stopPropagation(); fn(e); };

async function api(path, params = {}, opts = {}) {
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v == null || v === '') continue;
    if (Array.isArray(v)) v.forEach(x => usp.append(k, x));
    else usp.append(k, v);
  }
  const qs = usp.toString();
  const res = await fetch(path + (qs ? '?' + qs : ''), opts);
  const body = await res.json().catch(() => ({ error: res.statusText }));
  if (!res.ok) {
    const e = new Error(body.error || res.statusText);
    e.status = res.status;
    throw e;
  }
  return body;
}
const post = (path, body) => api(path, {}, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body || {}),
});

/* ------------------------------------------------------------------ state */

const state = {
  status: null,
  version: 0,
  summary: null,
  table: { cat: null, offset: 0, limit: 100, filters: [], q: '', around: null, selected: null },
  trace: { root: null, open: new Set(), closed: new Set(), limits: {} },
  issuesOpen: false,
  lastReload: null,
};
const cache = { rows: new Map(), children: new Map(), items: new Map() };

function saveHash() {
  const t = state.table;
  const p = new URLSearchParams();
  if (t.cat) p.set('cat', t.cat);
  if (t.offset) p.set('offset', t.offset);
  if (t.limit !== 100) p.set('limit', t.limit);
  if (t.q) p.set('q', t.q);
  t.filters.forEach(f => p.append('f', f.item + ':' + f.value));
  if (t.selected) p.set('sel', t.selected);
  if (state.issuesOpen) p.set('issues', '1');
  history.replaceState(null, '', '#' + p.toString());
}

function saveTrace() {
  const t = state.trace;
  try {
    sessionStorage.setItem('cifx-trace', JSON.stringify({
      root: t.root, open: [...t.open], closed: [...t.closed], limits: t.limits,
    }));
  } catch (e) { /* ignore storage errors */ }
}

function restoreState() {
  const p = new URLSearchParams(location.hash.slice(1));
  const t = state.table;
  t.cat = p.get('cat');
  t.offset = parseInt(p.get('offset') || '0', 10) || 0;
  t.limit = parseInt(p.get('limit') || '100', 10) || 100;
  t.q = p.get('q') || '';
  t.filters = p.getAll('f').filter(s => s.includes(':')).map(s => {
    const i = s.indexOf(':');
    return { item: s.slice(0, i), value: s.slice(i + 1) };
  });
  t.selected = p.get('sel');
  state.issuesOpen = p.get('issues') === '1';
  try {
    const tr = JSON.parse(sessionStorage.getItem('cifx-trace') || 'null');
    if (tr) {
      state.trace.root = tr.root;
      state.trace.open = new Set(tr.open || []);
      state.trace.closed = new Set(tr.closed || []);
      state.trace.limits = tr.limits || {};
    }
  } catch (e) { /* ignore */ }
}

function clearCaches() {
  cache.rows.clear();
  cache.children.clear();
}

function fetchRow(cat, ref) {
  const key = cat + '|' + ref;
  if (!cache.rows.has(key)) {
    cache.rows.set(key, api('/api/row', { cat, ref }).catch(e => ({ error: e.message })));
  }
  return cache.rows.get(key);
}

function fetchChildren(cat, ref, link, limit) {
  const key = [cat, ref, link, limit].join('|');
  if (!cache.children.has(key)) {
    cache.children.set(key, api('/api/children', { cat, ref, link, limit })
      .catch(e => ({ error: e.message, rows: [], count: 0 })));
  }
  return cache.children.get(key);
}

function itemInfo(cat, item) {
  const key = cat + '.' + item;
  if (!cache.items.has(key)) {
    cache.items.set(key, api('/api/dict/item', { cat, item }).catch(() => null));
  }
  return cache.items.get(key);
}

/* --------------------------------------------------------- status & events */

let refreshChain = Promise.resolve();
function scheduleRefresh() {
  refreshChain = refreshChain.then(refreshAll).catch(e => console.error(e));
}

function onStatus(st) {
  state.status = st;
  renderFileInfo(st);
  renderChips(st);
  renderLoading(st);
  const banner = $('#banner');
  if (st.error) {
    banner.className = 'banner';
    banner.textContent = (st.state === 'error' ? 'Could not read the file: '
      : 'Reload failed, still showing the previous version: ') + st.error;
  } else if (st.dictionary_messages && !st.dictionary) {
    banner.className = 'banner info';
    banner.textContent = 'Dictionary: ' + st.dictionary_messages.join('; ');
  } else {
    banner.className = 'banner hidden';
  }
  if (st.state === 'ready' && st.version !== state.version) scheduleRefresh();
}

function connectEvents() {
  const es = new EventSource('/api/events');
  es.onmessage = ev => {
    const msg = JSON.parse(ev.data);
    if (msg.status) onStatus(msg.status);
    if (msg.type === 'issues') {
      refreshChain = refreshChain.then(async () => {
        await refreshSummary();
        if (state.issuesOpen) await renderIssues();
      }).catch(console.error);
    } else if (msg.type === 'validator') {
      if (state.issuesOpen) renderIssues();
    } else if (msg.type === 'changed') {
      setLiveChip('file changed, reloading…', 'warn');
    } else if (msg.type === 'error') {
      setLiveChip('reload failed', 'err');
    }
  };
  es.onerror = () => setLiveChip('disconnected', 'err');
  es.onopen = () => setLiveChip(null);
}

function renderFileInfo(st) {
  const f = st.file || {};
  const bits = [h('b', null, f.name || '')];
  if (f.size != null) bits.push(' · ' + fmtBytes(f.size));
  if (f.format) bits.push(' · ' + f.format + (f.gzipped ? ' (gz)' : ''));
  if (f.load_seconds != null) bits.push(' · read in ' + f.load_seconds + ' s');
  if (f.memory != null) bits.push(' · ' + fmtBytes(f.memory) + ' in memory');
  const el = $('#fileinfo');
  el.replaceChildren(...bits);
  el.title = f.path + (st.dictionary ? '\nDictionaries: ' + st.dictionary : '');
}

let liveChip = { text: null, cls: '' };
function setLiveChip(text, cls = '') {
  liveChip = { text, cls };
  if (state.status) renderChips(state.status);
}

function renderChips(st) {
  const chips = [];
  if (st.loading && state.version) {
    chips.push(h('span', { class: 'chip' }, `${st.progress.stage} ${Math.round(st.progress.fraction * 100)}%`));
  }
  if (st.checks === 'running') {
    chips.push(h('span', { class: 'chip' }, `checking… ${Math.round((st.check_progress.fraction || 0) * 100)}%`));
  } else if (st.checks === 'done') {
    const c = st.issue_counts || {};
    const toggle = () => toggleIssues();
    if (c.error) chips.push(h('span', { class: 'chip err click', onclick: toggle, title: 'Show dictionary checks' }, `✗ ${c.error} error${c.error > 1 ? 's' : ''}`));
    if (c.warning) chips.push(h('span', { class: 'chip warn click', onclick: toggle, title: 'Show dictionary checks' }, `⚠ ${c.warning} warning${c.warning > 1 ? 's' : ''}`));
    if (!c.error && !c.warning) chips.push(h('span', { class: 'chip ok click', onclick: toggle }, '✓ dictionary checks pass'));
  }
  if (st.validator && st.validator.state === 'running') {
    chips.push(h('span', { class: 'chip' }, 'python-ihm validator running…'));
  }
  if (liveChip.text) {
    chips.push(h('span', { class: 'chip ' + liveChip.cls }, liveChip.text));
  } else if (state.lastReload) {
    chips.push(h('span', { class: 'chip ok flash', title: 'The view follows the file on disk' }, '● live · reloaded ' + state.lastReload));
  } else {
    chips.push(h('span', { class: 'chip', title: 'The view reloads when the file changes on disk' }, '● live'));
  }
  $('#chips').replaceChildren(...chips);
}

function renderLoading(st) {
  const el = $('#loading');
  if (st.state === 'loading' && !state.version) {
    el.classList.remove('hidden');
    $('#loading-title').textContent = 'Loading ' + ((st.file && st.file.name) || '') + '…';
    const p = st.progress || { stage: '', fraction: 0 };
    let frac = p.fraction || 0;
    if (p.stage.startsWith('scanning')) frac *= 0.25;
    else if (p.stage === 'reading') frac = 0.25 + 0.75 * frac;
    $('#loading-bar').style.width = Math.round(frac * 100) + '%';
    $('#loading-stage').textContent = `${p.stage} ${Math.round((p.fraction || 0) * 100)}%`;
  } else {
    el.classList.add('hidden');
  }
}

/* ---------------------------------------------------------------- refresh */

async function refreshAll() {
  const st = state.status;
  if (!st || st.state !== 'ready' || st.version === state.version) return;
  const first = state.version === 0;
  state.version = st.version;
  clearCaches();
  await refreshSummary();
  if (!state.table.cat || !state.summary.categories.some(c => c.name === state.table.cat)) {
    const cats = state.summary.categories.map(c => c.name);
    state.table.cat = cats.includes('ihm_model_list') ? 'ihm_model_list' : cats[0];
  }
  await loadTable({ keepScroll: true });
  await renderTrace();
  if (state.issuesOpen) await renderIssues();
  if (!first) {
    state.lastReload = new Date().toLocaleTimeString();
    liveChip = { text: null, cls: '' };
    renderChips(state.status);
  }
}

async function refreshSummary() {
  state.summary = await api('/api/summary');
  renderSidebar();
}

/* ---------------------------------------------------------------- sidebar */

function renderSidebar() {
  if (!state.summary) return;
  const filter = $('#catfilter').value.trim().toLowerCase();
  const cats = [...state.summary.categories].sort((a, b) => a.name.localeCompare(b.name))
    .filter(c => !filter || c.name.includes(filter));
  const groups = [
    ['IHM', c => c.known && (c.name.startsWith('ihm_') || c.name.startsWith('flr_'))],
    ['Core (PDBx)', c => c.known && !(c.name.startsWith('ihm_') || c.name.startsWith('flr_'))],
    ['Not in dictionary', c => !c.known],
  ];
  const out = [];
  for (const [title, pred] of groups) {
    const list = cats.filter(pred);
    if (!list.length) continue;
    out.push(h('div', { class: 'catgroup' }, `${title} (${list.length})`));
    for (const c of list) out.push(catEntry(c));
  }
  const missing = (state.summary.missing || []).filter(m => !filter || m.name.includes(filter));
  if (missing.length) {
    out.push(h('div', { class: 'catgroup' }, 'Missing from file'));
    for (const m of missing) {
      out.push(h('div', { class: 'cat unknown', onclick: () => toggleIssues(true), title: 'Mandatory category not in the file' },
        h('span', { class: 'name' }, m.name), issueDots(m.issues)));
    }
  }
  $('#catlist').replaceChildren(...out);
}

function issueDots(c = {}) {
  return [
    c.error ? h('span', { class: 'dot err', title: `${c.error} error(s)` }, c.error) : null,
    c.warning ? h('span', { class: 'dot warn', title: `${c.warning} warning(s)` }, c.warning) : null,
  ];
}

function catEntry(c) {
  return h('div', {
    class: 'cat' + (c.name === state.table.cat ? ' active' : '') + (c.known ? '' : ' unknown'),
    onclick: () => openCategory(c.name),
    title: `${c.name}: ${fmt(c.nrows)} rows, ${c.ncols} items` + (c.line ? `, line ${c.line}` : '') + (c.known ? '' : '\nnot in the dictionary'),
  }, h('span', { class: 'name' }, c.name), issueDots(c.issues), h('span', { class: 'n' }, fmt(c.nrows)));
}

/* ------------------------------------------------------------------ table */

function openCategory(cat, opts = {}) {
  state.table = {
    cat, offset: 0, limit: state.table.limit || 100,
    filters: opts.filters || [], q: opts.q || '',
    around: opts.around != null ? opts.around : null,
    selected: opts.selected || null,
  };
  saveHash();
  renderSidebar();
  return loadTable();
}

let tableToken = 0;
async function loadTable({ keepScroll = false } = {}) {
  const t = state.table;
  if (!t.cat) return;
  const tok = ++tableToken;
  let page;
  try {
    page = await api('/api/table', {
      cat: t.cat, offset: t.offset, limit: t.limit, q: t.q, around: t.around,
      f: t.filters.map(f => `${f.item}:${f.value}`),
    });
  } catch (e) {
    if (tok !== tableToken) return;
    $('#tableview').className = 'empty-state';
    $('#tableview').textContent = e.message;
    return;
  }
  if (tok !== tableToken) return;
  t.offset = page.offset;
  t.around = null;
  saveHash();
  renderTable(page, keepScroll);
  document.querySelectorAll('#catlist .cat').forEach(el => {
    el.classList.toggle('active', el.querySelector('.name').textContent === t.cat);
  });
}

function relChips(info) {
  const chip = (r, dir) => h('span', {
    class: `rel ${dir}` + (r.present ? '' : ' absent'),
    title: (dir === 'up' ? 'parent via ' : 'child via ') + r.via + (r.present ? '' : '\n(not in this file)'),
    onclick: r.present ? () => openCategory(r.cat) : null,
  }, r.cat);
  const dedupe = list => {
    const seen = new Map();
    for (const r of list) {
      const prev = seen.get(r.cat);
      if (!prev) seen.set(r.cat, { ...r });
      else prev.via += '; ' + r.via;
    }
    return [...seen.values()].sort((a, b) => (b.present - a.present) || a.cat.localeCompare(b.cat));
  };
  const parents = dedupe(info.parents);
  const children = dedupe(info.children);
  return h('div', { class: 'rels' },
    parents.length ? h('span', { class: 'lbl' }, '↑ parents') : null,
    parents.map(r => chip(r, 'up')),
    children.length ? h('span', { class: 'lbl', style: { marginLeft: '8px' } }, '↓ children') : null,
    children.map(r => chip(r, 'down')));
}

function cellText(v) {
  if (v == null) return '';
  const nl = v.indexOf('\n');
  let s = nl >= 0 ? v.slice(0, nl) : v;
  if (s.length > 120) s = s.slice(0, 117) + '…';
  return s;
}

function renderTable(page, keepScroll) {
  const view = $('#tableview');
  const oldWrap = view.querySelector('.tablewrap');
  const sTop = keepScroll && oldWrap ? oldWrap.scrollTop : 0;
  const sLeft = keepScroll && oldWrap ? oldWrap.scrollLeft : 0;
  const t = state.table;
  const info = page.info;
  const keys = new Set(info.keys || []);

  const descEl = info.description
    ? h('div', { class: 'desc', title: 'click to expand', onclick: e => e.currentTarget.classList.toggle('open') }, info.description)
    : null;
  const head = h('div', { class: 'tv-head' },
    h('div', { class: 'tv-title' },
      h('h2', null, page.cat),
      h('span', { class: 'muted' }, `${fmt(page.nrows)} row${page.nrows === 1 ? '' : 's'} · ${page.items.length} items` + (info.line ? ` · line ${fmt(info.line)}` : '')),
      info.known ? null : h('span', { class: 'chip err' }, 'not in dictionary'),
      (info.keys || []).length ? h('span', { class: 'muted small' }, 'key: ' + info.keys.join(', ')) : null),
    descEl,
    relChips(info));

  // toolbar: filters, search, pager
  const q = h('input', { type: 'search', placeholder: 'contains…', value: t.q, title: 'Show rows where any value contains this text' });
  q.addEventListener('keydown', e => {
    if (e.key === 'Enter') { t.q = q.value.trim(); t.offset = 0; loadTable(); }
  });
  q.addEventListener('search', () => { if (!q.value) { t.q = ''; t.offset = 0; loadTable(); } });
  const filters = t.filters.map((f, i) => h('span', { class: 'filter' }, `${f.item} = ${f.value}`,
    h('button', { title: 'remove filter', onclick: () => { t.filters.splice(i, 1); t.offset = 0; loadTable(); } }, '×')));
  const last = Math.max(0, page.total - 1);
  const go = off => { t.offset = Math.max(0, Math.min(off, last)); loadTable(); };
  const from = page.total ? page.offset + 1 : 0;
  const to = Math.min(page.offset + page.limit, page.total);
  const limitSel = h('select', { title: 'rows per page', onchange: e => { t.limit = +e.target.value; t.offset = 0; loadTable(); } },
    [50, 100, 250, 500, 1000].map(n => h('option', { value: n, selected: n === t.limit }, n)));
  const pager = h('div', { class: 'pager' },
    h('button', { class: 'btn small', disabled: page.offset === 0, onclick: () => go(0) }, '«'),
    h('button', { class: 'btn small', disabled: page.offset === 0, onclick: () => go(page.offset - page.limit) }, '‹'),
    h('span', null, `${fmt(from)}–${fmt(to)} of ${fmt(page.total)}` + (page.total !== page.nrows ? ` (filtered from ${fmt(page.nrows)})` : '')),
    h('button', { class: 'btn small', disabled: to >= page.total, onclick: () => go(page.offset + page.limit) }, '›'),
    h('button', { class: 'btn small', disabled: to >= page.total, onclick: () => go(Math.floor(last / page.limit) * page.limit) }, '»'),
    limitSel);
  const kv = page.nrows === 1 && page.rows.length === 1 && !t.filters.length && !t.q;
  const toolbar = h('div', { class: 'toolbar' }, filters, q, kv ? null : h('span', { class: 'hint' }, 'double-click a cell to filter by it'), h('span', { class: 'spacer' }), pager);

  let content;
  if (kv) {
    const row = page.rows[0];
    content = h('table', { class: 'grid kv' },
      h('tbody', null, page.items.map((it, j) => {
        const v = row.values[j];
        const flag = row.flags[j];
        return h('tr', { class: row.ref === t.selected ? 'sel' : '', dataset: { ref: row.ref }, onclick: () => selectRow(page.cat, row.ref) },
          h('td', { class: 'k' + (it.known ? '' : ' bad'), dataset: { cat: page.cat, item: it.item } }, it.name, it.mandatory ? h('span', { class: 'mand' }, '*') : null),
          h('td', { class: (isNull(v) ? 'null' : '') + (flag ? ' bad' : ''), title: flag ? '⚠ ' + flag : null }, v));
      })));
  } else if (!page.rows.length) {
    content = h('div', { class: 'empty-state' }, 'No rows match.');
  } else {
    const thead = h('thead', null, h('tr', null,
      h('th', { class: 'rn' }, '#'),
      page.items.map(it => h('th', {
        class: (keys.has(it.item) ? 'key' : '') + (it.known ? '' : ' unknown'),
        dataset: { cat: page.cat, item: it.item },
      }, it.name, it.mandatory ? h('span', { class: 'mand' }, '*') : null))));
    const tbody = h('tbody', null, page.rows.map(row => h('tr', {
      class: row.ref === t.selected ? 'sel' : '',
      dataset: { ref: row.ref },
      onclick: () => selectRow(page.cat, row.ref),
    },
      h('td', { class: 'rn' }, row.idx + 1),
      row.values.map((v, j) => {
        const flag = row.flags[j];
        const cls = [isNull(v) ? 'null' : '', flag ? 'bad' : '', v && v.includes('\n') ? 'multi' : ''].join(' ').trim();
        return h('td', {
          class: cls || null,
          title: (flag ? '⚠ ' + flag + '\n' : '') + (v && v.length > 40 || flag ? v : ''),
          ondblclick: stop(() => {
            const item = page.items[j].item;
            if (!t.filters.some(f => f.item === item)) t.filters.push({ item, value: v });
            t.offset = 0;
            loadTable();
          }),
        }, cellText(v));
      }))));
    content = h('table', { class: 'grid' }, thead, tbody);
  }
  const wrap = h('div', { class: 'tablewrap' }, content);
  view.className = '';
  view.replaceChildren(head, toolbar, wrap);
  wrap.scrollTop = sTop;
  wrap.scrollLeft = sLeft;
  if (!keepScroll) {
    const sel = wrap.querySelector('tr.sel');
    if (sel) sel.scrollIntoView({ block: 'center' });
  }
}

function markSelected() {
  document.querySelectorAll('#tableview tr').forEach(tr => {
    tr.classList.toggle('sel', !!tr.dataset.ref && tr.dataset.ref === state.table.selected);
  });
}

/* ------------------------------------------------------------------ trace */

function selectRow(cat, ref) {
  if (cat === state.table.cat) {
    state.table.selected = ref;
    saveHash();
    markSelected();
  }
  state.trace.root = { cat, ref };
  state.trace.open = new Set();
  state.trace.closed = new Set();
  state.trace.limits = {};
  saveTrace();
  $('#trace-body').scrollTop = 0;
  renderTrace();
}

async function showInTable(cat, ref) {
  const row = await fetchRow(cat, ref);
  if (row.error) return;
  await openCategory(cat, { around: row.idx, selected: ref });
}

function toggleOpen(path) {
  const o = state.trace.open;
  if (o.has(path)) o.delete(path); else o.add(path);
  saveTrace();
  renderTrace();
}

function toggleFields(path, isRoot) {
  const set = isRoot ? state.trace.closed : state.trace.open;
  if (set.has(path)) set.delete(path); else set.add(path);
  saveTrace();
  renderTrace();
}

let traceToken = 0;
async function renderTrace() {
  const body = $('#trace-body');
  const root = state.trace.root;
  if (!root || !state.version) {
    body.className = 'empty-state';
    body.textContent = 'Click a table row to trace it up and down the hierarchy.';
    $('#trace-root-label').textContent = '';
    return;
  }
  const tok = ++traceToken;
  const scroll = body.scrollTop;
  const el = await rowNode({ cat: root.cat, ref: root.ref, path: 'r', ancestors: new Set(), summary: null, rel: null });
  if (tok !== traceToken) return;
  body.className = '';
  body.replaceChildren(el);
  body.scrollTop = scroll;
}

function viaText(via) {
  return (via || []).map(v => `${v.child}=${v.value}`).join(', ');
}

async function rowNode({ cat, ref, path, ancestors, summary, rel }) {
  const id = cat + '|' + ref;
  const isRoot = path === 'r';
  const cyc = ancestors.has(id);
  const open = !cyc && (isRoot || state.trace.open.has(path));
  const labelEl = h('span', { class: 'rowlabel' }, summary ? summary.label : '');
  const head = h('div', { class: 'node-head', onclick: (cyc || isRoot) ? null : () => toggleOpen(path) },
    h('span', { class: 'caret' }, isRoot ? '' : (cyc ? '↺' : (open ? '▾' : '▸'))),
    rel ? h('span', { class: 'dir ' + rel.dir }, rel.dir === 'up' ? '↑' : '↓') : null,
    h('span', { class: 'cattag' }, cat),
    labelEl,
    rel && rel.via ? h('span', { class: 'via' }, 'via ' + rel.via) : null,
    cyc ? h('span', { class: 'cycle' }, '(already open above)') : null,
    h('span', { class: 'spacer' }),
    h('span', { class: 'acts' },
      h('button', { class: 'btn small', title: 'Show this row in the table', onclick: stop(() => showInTable(cat, ref)) }, '⌖ table'),
      isRoot ? null : h('button', { class: 'btn small', title: 'Trace from this row', onclick: stop(() => selectRow(cat, ref)) }, '⤒ root'),
      h('button', { class: 'btn small', title: 'Expand all ancestors of this row', onclick: stop(() => expandUp(cat, ref, path)) }, '↑ all')));
  const node = h('div', { class: 'node' + (isRoot ? ' root' : '') }, head);
  if (!open) return node;

  const row = await fetchRow(cat, ref);
  if (row.error) {
    if (isRoot) $('#trace-root-label').textContent = `${cat} · (missing)`;
    node.append(h('div', { class: 'note missing' }, row.error));
    return node;
  }
  labelEl.textContent = row.label;
  if (isRoot) $('#trace-root-label').textContent = `${cat} · ${row.label} · row ${row.idx + 1} of ${fmt(row.nrows)}`;
  const body = h('div', { class: 'node-body' });
  node.append(body);

  // fields
  const fpath = path + '/f';
  const fopen = isRoot ? !state.trace.closed.has(fpath) : state.trace.open.has(fpath);
  const nbad = row.fields.filter(f => f.flag).length;
  body.append(h('div', { class: 'section', style: { cursor: 'pointer' }, onclick: () => toggleFields(fpath, isRoot) },
    (fopen ? '▾ ' : '▸ ') + `fields (${row.fields.length})`,
    nbad ? h('span', { class: 'bad-label' }, ` · ${nbad} problem${nbad > 1 ? 's' : ''}`) : null));
  if (fopen) body.append(fieldsTable(cat, row.fields));

  const anc = new Set(ancestors);
  anc.add(id);

  // parents
  const live = row.parents.filter(p => p.status !== 'null');
  const na = row.parents.filter(p => p.status === 'null');
  if (live.length) {
    const nOk = live.filter(p => p.status === 'ok' || p.status === 'ambiguous').length;
    const nMissing = live.filter(p => p.status === 'missing').length;
    body.append(h('div', { class: 'section' }, `↑ parents (${nOk})`, nMissing ? h('span', { class: 'bad-label' }, ` · ${nMissing} broken`) : null));
    const parts = await Promise.all(live.map(p => parentEntry(p, path, anc)));
    parts.forEach(p => body.append(...p));
  }
  if (na.length) {
    body.append(h('div', { class: 'note dim', title: 'Links whose value is . or ? in this row' },
      'n/a: ' + na.map(p => `${p.parent} (${viaText(p.via)})`).join(', ')));
  }

  // children
  if (row.children.length) {
    const withRows = row.children.filter(c => c.count > 0);
    const empty = row.children.filter(c => c.count === 0);
    body.append(h('div', { class: 'section' }, `↓ children (${withRows.length})`));
    const groups = await Promise.all(withRows.map(c => childGroup(cat, ref, c, path, anc)));
    groups.forEach(g => body.append(g));
    if (empty.length) {
      body.append(h('div', { class: 'note dim' }, 'no rows in: ' + [...new Set(empty.map(c => c.child))].join(', ')));
    }
  }
  return node;
}

function fieldsTable(cat, fields) {
  return h('table', { class: 'fields' }, h('tbody', null, fields.map(f => {
    let v = f.value;
    if (v && v.length > 3000) v = v.slice(0, 3000) + ' …';
    return h('tr', { class: f.flag ? 'bad' : null, title: f.flag ? '⚠ ' + f.flag : null },
      h('td', { class: 'k', dataset: { cat, item: f.item } }, f.name),
      h('td', { class: 'v' + (isNull(f.value) ? ' null' : '') }, v));
  })));
}

async function parentEntry(p, path, anc) {
  const via = viaText(p.via);
  if (p.status === 'no_category') {
    return [h('div', { class: 'note dim' }, `↑ ${p.parent} (${via}): category not in file`)];
  }
  if (p.status === 'no_item') {
    return [h('div', { class: 'note dim' }, `↑ ${p.parent} (${via}): linked item not in file`)];
  }
  if (p.status === 'missing') {
    const want = (p.via || []).map(v => {
      const i = p.items.indexOf(v.child);
      return `${i >= 0 ? p.parent_items[i] : v.child}=${v.value}`;
    }).join(', ');
    return [h('div', { class: 'note missing', title: 'Broken link: the parent row does not exist' },
      `✗ ↑ ${p.parent}: no row with ${want}`)];
  }
  const out = [];
  if (p.status === 'ambiguous') {
    out.push(h('div', { class: 'note dim' }, `↑ ${p.parent} (${via}) matches ${fmt(p.count)} rows`));
  }
  const nodes = await Promise.all(p.rows.map(r => rowNode({
    cat: r.cat, ref: r.ref, path: path + '/u:' + p.link + ':' + r.ref,
    ancestors: anc, summary: r, rel: { dir: 'up', via },
  })));
  out.push(...nodes);
  return out;
}

async function childGroup(cat, ref, c, path, anc) {
  const gpath = path + '/d:' + c.link;
  const open = state.trace.open.has(gpath);
  const filters = c.via.map(v => ({ item: v.child, value: v.value }));
  const openTable = stop(() => openCategory(c.child, { filters }));
  const head = h('div', { class: 'node-head', onclick: () => toggleOpen(gpath) },
    h('span', { class: 'caret' }, open ? '▾' : '▸'),
    h('span', { class: 'dir down' }, '↓'),
    h('span', { class: 'cattag' }, c.child),
    h('span', { class: 'count' }, `${fmt(c.count)} row${c.count === 1 ? '' : 's'}`),
    h('span', { class: 'via' }, 'via ' + viaText(c.via)),
    h('span', { class: 'spacer' }),
    h('span', { class: 'acts' }, h('button', { class: 'btn small', title: 'Open these rows in the table', onclick: openTable }, '⌖ table')));
  const node = h('div', { class: 'node' }, head);
  if (!open) return node;
  const limit = state.trace.limits[gpath] || 25;
  const page = await fetchChildren(cat, ref, c.link, limit);
  const body = h('div', { class: 'node-body' });
  if (page.error) body.append(h('div', { class: 'note missing' }, page.error));
  const kids = await Promise.all(page.rows.map(r => rowNode({
    cat: r.cat, ref: r.ref, path: gpath + '/' + r.ref, ancestors: anc, summary: r, rel: null,
  })));
  kids.forEach(k => body.append(k));
  if (page.count > page.rows.length) {
    body.append(h('div', { class: 'more' },
      h('button', {
        class: 'linkbtn', onclick: () => {
          state.trace.limits[gpath] = Math.min(limit * 4, 1000);
          saveTrace();
          renderTrace();
        },
      }, `show more (${fmt(page.count - page.rows.length)} not shown)`),
      '  ',
      h('button', { class: 'linkbtn', onclick: openTable }, 'open all in table')));
  }
  node.append(body);
  return node;
}

async function expandUp(cat, ref, path) {
  const seen = new Set([cat + '|' + ref]);
  async function rec(c, r, p, depth) {
    state.trace.open.add(p);
    if (depth > 25) return;
    const row = await fetchRow(c, r);
    if (row.error) return;
    const jobs = [];
    for (const par of row.parents) {
      for (const pr of (par.rows || []).slice(0, 3)) {
        const id = pr.cat + '|' + pr.ref;
        if (seen.has(id)) continue;
        seen.add(id);
        jobs.push(rec(pr.cat, pr.ref, p + '/u:' + par.link + ':' + pr.ref, depth + 1));
      }
    }
    await Promise.all(jobs);
  }
  await rec(cat, ref, path, 0);
  saveTrace();
  renderTrace();
}

/* ----------------------------------------------------------------- issues */

function toggleIssues(force) {
  state.issuesOpen = force != null ? force : !state.issuesOpen;
  $('#issues').classList.toggle('hidden', !state.issuesOpen);
  saveHash();
  if (state.issuesOpen) renderIssues();
}

async function renderIssues() {
  const data = await api('/api/issues').catch(e => ({ error: e.message }));
  const body = $('#issues-body');
  if (data.error) { body.textContent = data.error; return; }
  const showInfo = $('#show-info').checked;
  const list = (data.issues || []).filter(i => showInfo || i.severity !== 'info');
  const counts = {};
  (data.issues || []).forEach(i => { counts[i.severity] = (counts[i.severity] || 0) + 1; });
  $('#issues-summary').textContent = data.ready
    ? `${counts.error || 0} errors · ${counts.warning || 0} warnings · ${counts.info || 0} info`
    : 'checks running…';
  const icon = { error: '✗', warning: '⚠', info: 'ℹ' };
  const present = new Set((state.summary ? state.summary.categories : []).map(c => c.name));
  const rows = list.map(i => {
    const where = i.category + (i.items.length ? '.' + i.items.join(', ') : '');
    const exists = present.has(i.category);
    const jump = (k) => {
      if (!exists) return;
      const ref = (i.refs || [])[k];
      if (ref == null) return openCategory(i.category);
      openCategory(i.category, { around: i.rows[k], selected: ref }).then(() => selectRow(i.category, ref));
    };
    const examples = (i.refs || []).slice(0, 12).map((ref, k) =>
      h('a', { onclick: () => jump(k), title: 'show this row' }, 'row ' + (i.rows[k] + 1)));
    return h('div', { class: 'issue' },
      h('span', { class: 'sev ' + i.severity }, icon[i.severity] || '•'),
      h('span', { class: 'where' + (exists ? '' : ' muted'), title: where, onclick: exists ? () => jump(0) : null }, where),
      h('div', null,
        h('div', null, i.message, i.count ? h('span', { class: 'muted' }, ` · ${fmt(i.count)} row${i.count > 1 ? 's' : ''}`) : null),
        i.values && i.values.length ? h('div', { class: 'vals' }, 'values: ' + i.values.slice(0, 8).join(' | ') + (i.values.length > 8 ? ' …' : '')) : null,
        examples.length ? h('div', { class: 'vals' }, examples) : null));
  });
  if (data.ready && !list.length) rows.push(h('div', { class: 'empty-state' }, 'No problems found by the live checks.'));
  const v = data.validator || {};
  const vmsgs = v.messages || [];
  const vcount = vmsgs.length
    ? `${fmt(vmsgs.length)}${v.truncated ? '+' : ''} problem(s)` + (v.truncated ? ' (truncated)' : '')
    : 'no problems';
  const vbox = h('div', { class: 'validator' },
    h('b', null, 'python-ihm validator '),
    h('span', { class: 'muted' }, v.state === 'idle' ? '(not run; checks the whole file with Dictionary.validate)'
      : v.state === 'running' ? 'running…'
        : `${vcount} · ${v.seconds} s` + (v.version !== state.version ? ' · ran on an earlier version of the file' : '')),
    vmsgs.length ? h('pre', null, vmsgs.join('\n\n')) : null);
  $('#run-validator').disabled = v.state === 'running';
  body.replaceChildren(...rows, vbox);
}

/* ----------------------------------------------------------------- search */

let searchToken = 0;
async function runSearch(q) {
  const pop = $('#searchresults');
  const tok = ++searchToken;
  if (!q) { pop.classList.add('hidden'); return; }
  pop.classList.remove('hidden');
  pop.replaceChildren(h('div', { class: 'empty-state' }, 'Searching…'));
  const res = await api('/api/search', { q }).catch(e => ({ error: e.message }));
  if (tok !== searchToken) return;
  if (res.error) { pop.replaceChildren(h('div', { class: 'empty-state' }, res.error)); return; }
  if (!res.results.length) { pop.replaceChildren(h('div', { class: 'empty-state' }, `No value contains “${q}”.`)); return; }
  const out = [];
  for (const r of res.results) {
    out.push(h('div', { class: 'sr-cat', onclick: () => { pop.classList.add('hidden'); openCategory(r.cat, { q }); } },
      r.cat, h('span', { class: 'muted' }, `${fmt(r.count)} row${r.count > 1 ? 's' : ''}`)));
    for (const row of r.rows) {
      out.push(h('div', {
        class: 'sr-row', title: row.label, onclick: () => {
          pop.classList.add('hidden');
          openCategory(r.cat, { q, around: row.idx, selected: row.ref }).then(() => selectRow(r.cat, row.ref));
        },
      }, row.label));
    }
  }
  pop.replaceChildren(...out);
}

/* --------------------------------------------------------------- tooltips */

let ttEl = null;
let ttTimer = null;
document.addEventListener('mouseover', e => {
  const el = e.target.closest('[data-item]');
  if (!el || el === ttEl) return;
  ttEl = el;
  clearTimeout(ttTimer);
  ttTimer = setTimeout(() => showTip(el), 350);
});
document.addEventListener('mouseout', e => {
  const el = e.target.closest('[data-item]');
  if (el && !el.contains(e.relatedTarget)) {
    clearTimeout(ttTimer);
    ttEl = null;
    $('#tooltip').classList.add('hidden');
  }
});

async function showTip(el) {
  const info = await itemInfo(el.dataset.cat, el.dataset.item);
  if (ttEl !== el || !info) return;
  const tip = $('#tooltip');
  const meta = [];
  if (!info.known) meta.push('not defined in the dictionary');
  if (info.type) meta.push('type ' + info.type);
  if (info.mandatory != null) meta.push(info.mandatory ? 'mandatory' : 'optional');
  if (info.units) meta.push(info.units);
  if (info.range) meta.push('range ' + info.range);
  const kids = [
    h('div', { class: 'tt-name' }, info.name || `_${el.dataset.cat}.${el.dataset.item}`),
    meta.length ? h('div', { class: 'tt-meta' }, meta.join(' · ')) : null,
    info.description ? h('div', { class: 'tt-desc' }, info.description) : null,
  ];
  if (info.enum && info.enum.length) {
    const vals = info.enum.slice(0, 20).map(e => e.value).join(' | ');
    kids.push(h('div', { class: 'tt-enum' }, 'allowed: ' + vals + (info.enum_total > 20 ? ` … (${info.enum_total})` : '')));
  }
  const inFile = new Set((state.summary ? state.summary.categories : []).map(c => c.name));
  const list = xs => {
    // categories in this file first, then the rest, capped
    const sorted = [...xs].sort((a, b) => (inFile.has(b) - inFile.has(a)) || a.localeCompare(b));
    const shown = sorted.slice(0, 10).map(c => (inFile.has(c) ? c : c + '°'));
    return shown.join(', ') + (sorted.length > 10 ? ` … (+${sorted.length - 10})` : '');
  };
  if ((info.parents || []).length) kids.push(h('div', { class: 'tt-meta' }, '↑ refers to: ' + list(info.parents)));
  if ((info.children || []).length) kids.push(h('div', { class: 'tt-meta' }, '↓ referenced by: ' + list(info.children)));
  if ([...(info.parents || []), ...(info.children || [])].some(c => !inFile.has(c))) {
    kids.push(h('div', { class: 'tt-meta small' }, '° not in this file'));
  }
  tip.replaceChildren(...kids);
  tip.classList.remove('hidden');
  const r = el.getBoundingClientRect();
  const tw = tip.offsetWidth;
  const th = tip.offsetHeight;
  let x = Math.min(r.left, window.innerWidth - tw - 8);
  let y = r.bottom + 6;
  if (y + th > window.innerHeight - 8) y = Math.max(8, r.top - th - 6);
  tip.style.left = Math.max(8, x) + 'px';
  tip.style.top = y + 'px';
}

/* ------------------------------------------------------------------ setup */

/* UI preferences (theme, panel sizes). The server writes the saved ones
   into the page and keeps them in ~/.config/cif_explorer/prefs.json, so they
   are shared by every run (localStorage would not be: it is per port). */
const prefs = JSON.parse($('#prefs').textContent || '{}');
function savePref(key, value) {
  if (value == null) delete prefs[key]; else prefs[key] = value;
  post('/api/prefs', { [key]: value == null ? null : value }).catch(() => {});
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const next = theme === 'dark' ? 'light' : 'dark';
  const btn = $('#theme');
  btn.title = `Switch to the ${next} theme`;
  btn.setAttribute('aria-label', btn.title);
}

/* Drag `handle` to set the width (or height, with `vertical`) of `panel`.
   Double-click the handle to go back to the default from the stylesheet. */
function resizable(handle, panel, { key, min, max, size, vertical = false }) {
  const prop = vertical ? 'height' : 'width';
  const cls = vertical ? 'resizing-y' : 'resizing-x';
  const apply = px => {
    panel.style[prop] = Math.round(Math.max(min, Math.min(max(), px))) + 'px';
  };
  const restore = () => {
    if (prefs[key] > 0) apply(prefs[key]);
  };
  handle.addEventListener('pointerdown', e => {
    if (e.button !== 0) return;
    e.preventDefault();
    handle.setPointerCapture(e.pointerId);
    handle.classList.add('dragging');
    document.body.classList.add(cls);
    let moved = false;
    const move = ev => { moved = true; apply(size(ev)); };
    const up = () => {
      handle.removeEventListener('pointermove', move);
      handle.removeEventListener('pointerup', up);
      handle.removeEventListener('pointercancel', up);
      handle.classList.remove('dragging');
      document.body.classList.remove(cls);
      if (moved) savePref(key, parseFloat(panel.style[prop]));
    };
    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', up);
    handle.addEventListener('pointercancel', up);
  });
  handle.addEventListener('dblclick', () => {
    panel.style[prop] = '';
    savePref(key, null);
  });
  // a smaller window must not squeeze the table out of view; growing it
  // again brings back the saved size
  window.addEventListener('resize', restore);
  return restore;
}

function setupUI() {
  $('#catfilter').addEventListener('input', renderSidebar);
  $('#searchform').addEventListener('submit', e => { e.preventDefault(); runSearch($('#search').value.trim()); });
  $('#search').addEventListener('search', () => {
    if (!$('#search').value) { searchToken++; $('#searchresults').classList.add('hidden'); }
  });
  document.addEventListener('click', e => {
    if (!e.target.closest('#searchresults') && !e.target.closest('#searchform')) {
      searchToken++;
      $('#searchresults').classList.add('hidden');
    }
  });
  document.addEventListener('keydown', e => {
    if (e.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) {
      e.preventDefault();
      $('#search').focus();
    } else if (e.key === 'Escape') {
      searchToken++;
      $('#searchresults').classList.add('hidden');
      $('#tooltip').classList.add('hidden');
    }
  });
  $('#trace-up').addEventListener('click', () => {
    const r = state.trace.root;
    if (r) expandUp(r.cat, r.ref, 'r');
  });
  $('#trace-collapse').addEventListener('click', () => {
    state.trace.open = new Set();
    state.trace.limits = {};
    saveTrace();
    renderTrace();
  });
  setTheme(prefs.theme || 'dark');
  $('#theme').addEventListener('click', () => {
    const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    setTheme(theme);
    savePref('theme', theme);
  });
  $('#issues-close').addEventListener('click', () => toggleIssues(false));
  $('#show-info').addEventListener('change', renderIssues);
  $('#run-validator').addEventListener('click', async () => {
    const big = state.status && state.status.file && state.status.file.size > 200 * 1024 * 1024;
    if (big && !confirm('The python-ihm validator reads the whole file again and can take several minutes on a file this size. Run it?')) return;
    await post('/api/validate');
    renderIssues();
  });
  // resizable panels; the table in the middle always keeps MAIN_MIN pixels
  const sidebar = $('#sidebar'), trace = $('#trace'), issues = $('#issues');
  const MAIN_MIN = 300, SPLITS = 10;
  // apply raw saved widths before clamping so sidebar isn't clamped against
  // trace's CSS default before trace's saved width is applied
  if (prefs.sidebar_width > 0) sidebar.style.width = Math.round(prefs.sidebar_width) + 'px';
  if (prefs.trace_width > 0) trace.style.width = Math.round(prefs.trace_width) + 'px';
  const restores = [
    resizable($('#sidebar-split'), sidebar, {
      key: 'sidebar_width', min: 140,
      max: () => window.innerWidth - trace.offsetWidth - SPLITS - MAIN_MIN,
      size: ev => ev.clientX - sidebar.getBoundingClientRect().left,
    }),
    resizable($('#trace-split'), trace, {
      key: 'trace_width', min: 280,
      max: () => window.innerWidth - sidebar.offsetWidth - SPLITS - MAIN_MIN,
      size: ev => window.innerWidth - ev.clientX,
    }),
    resizable($('#issues-split'), issues, {
      key: 'issues_height', min: 80, vertical: true,
      max: () => window.innerHeight - $('#layout').getBoundingClientRect().top - 120,
      size: ev => window.innerHeight - ev.clientY,
    }),
  ];
  restores.forEach(fn => fn());
  if (state.issuesOpen) $('#issues').classList.remove('hidden');
}

async function boot() {
  restoreState();
  setupUI();
  connectEvents();
  try {
    onStatus(await api('/api/status'));
  } catch (e) {
    $('#banner').className = 'banner';
    $('#banner').textContent = 'Cannot reach the cif_explorer server: ' + e.message;
  }
}

boot();
