'use strict';
/* Internship Copilot - vanilla JS single-page UI. Every dynamic string goes through textContent (no innerHTML). */

// ============================================================ utilities
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const sleep = ms => new Promise(r => setTimeout(r, ms));

function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  let value;
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k === 'value') value = v;
    else if (k.startsWith('on') && typeof v === 'function') e.addEventListener(k.slice(2), v);
    else if (['checked', 'disabled', 'selected', 'hidden', 'open'].includes(k)) e[k] = !!v;
    else e.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    e.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  if (value !== undefined) e.value = value;
  return e;
}
const clear = el => { while (el.firstChild) el.removeChild(el.firstChild); return el; };
const put = (parent, ...kids) => { for (const k of kids.flat(Infinity)) { if (k !== null && k !== undefined && k !== false) parent.append(k instanceof Node ? k : document.createTextNode(String(k))); } return parent; };
function autosize(ta) { ta.style.height = 'auto'; ta.style.height = (ta.scrollHeight + 2) + 'px'; }

class ApiError extends Error {
  constructor(message, status, data) { super(message); this.status = status; this.data = data; }
}
function errorMessage(data, status) {
  if (!data) return `Request failed (${status}).`;
  if (typeof data === 'string') return data.slice(0, 300);
  const d = data.detail !== undefined ? data.detail : data;
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) return d.map(x => (x.loc ? x.loc.slice(1).join('.') + ': ' : '') + x.msg).join('; ');
  if (d && d.message) return d.message;
  if (d && d.errors) return d.errors.join('; ');
  return `Request failed (${status}).`;
}
async function api(path, opts = {}) {
  const init = { method: opts.method || 'GET', headers: {} };
  if (opts.body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.body); }
  let r;
  try { r = await fetch(path, init); } catch (e) { throw new ApiError('Cannot reach the app server: is it still running?', 0); }
  const ct = r.headers.get('content-type') || '';
  const data = ct.includes('json') ? await r.json() : await r.text();
  if (!r.ok) throw new ApiError(errorMessage(data, r.status), r.status, data);
  return data;
}

let toastTimer;
function toast(msg) {
  const t = $('#toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), 2600);
}
// safety net: no failure is ever silent
window.addEventListener('unhandledrejection', e => { toast('Error: ' + ((e.reason && e.reason.message) || e.reason)); });
window.addEventListener('error', e => { toast('Error: ' + e.message); });
async function copyText(text, label) {
  try { await navigator.clipboard.writeText(text); }
  catch (e) {
    const ta = h('textarea', { style: 'position:fixed;opacity:0' }); ta.value = text; document.body.append(ta); ta.select();
    try { document.execCommand('copy'); } catch (_) { /* ignore */ } ta.remove();
  }
  toast(`${label || 'Text'} copied`);
}
const notice = (kind, ...kids) => h('div', { class: `notice ${kind}` }, ...kids);
const ICON = { ok: '✓', warn: '!', bad: '✕', info: 'i', unknown: '?' };
function fitRows(items) {
  return items.map(i => h('div', { class: `fit-row ${i.status}` },
    h('span', { class: 'ic' }, ICON[i.status] || '?'),
    h('span', {}, i.label, i.detail ? h('span', { class: 'd' }, i.detail) : null)));
}
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
const todayISO = () => new Date().toISOString().slice(0, 10);
const fmtDate = s => (s || '').slice(0, 10);

// ============================================================ state
const S = { status: null, blueprints: null, domains: [], job: null, spDomains: new Set(), app: null, appTab: 'doc', editorKey: null, edText: '' };
const LS = {
  get(k, d) { try { const v = localStorage.getItem('copilot.' + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem('copilot.' + k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
};

// ============================================================ tabs & status
function showTab(name) {
  $$('.tab').forEach(t => t.classList.toggle('active', t.id === 'tab-' + name));
  $$('#tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  if (name === 'tracker') loadTracker();
  if (name === 'profile') openEditor(S.editorKey || 'profile');
  if (name === 'setup') renderSetup();
  window.scrollTo({ top: 0 });
}
document.addEventListener('click', e => {
  const t = e.target.closest('[data-goto]');
  if (t) { e.preventDefault(); showTab(t.dataset.goto); if (t.dataset.file) openEditor(t.dataset.file); }
});
$('#tabs').addEventListener('click', e => { const b = e.target.closest('button[data-tab]'); if (b) showTab(b.dataset.tab); });

async function refreshStatus() {
  try { S.status = await api('/api/status'); } catch (e) { S.status = null; renderPills(); return; }
  renderPills(); renderGlobalWarnings(); fillModelSelect();
  $('#demo-banner').hidden = !S.status.demo;
}
function renderPills() {
  const box = clear($('#pills')); const s = S.status;
  if (!s) { box.append(h('span', { class: 'pill bad' }, 'Server not reachable')); return; }
  const l = s.llm;
  if (s.demo) box.append(h('span', { class: 'pill warn', title: 'No model: templated text' }, 'Demo mode'));
  else if (!l.ok) box.append(h('span', { class: 'pill bad', title: l.error }, 'Ollama not running'));
  else if (!l.model_installed) box.append(h('span', { class: 'pill warn', title: 'ollama pull ' + l.model }, `Model ${l.model} missing`));
  else box.append(h('span', { class: 'pill ok', title: 'Ollama ' + l.version }, `Ollama · ${l.model}`));
  box.append(h('span', { class: 'pill ' + (s.latex.engine ? 'ok' : 'warn'), title: s.latex.path || 'Install TeX Live / MiKTeX to get PDFs' },
    s.latex.engine ? `LaTeX · ${s.latex.engine}` : 'No LaTeX (.tex only)'));
  const p = s.profile;
  box.append(h('span', { class: 'pill ' + (!p.ok ? 'bad' : p.example ? 'warn' : 'ok'), title: p.errors.join('\n') },
    !p.ok ? 'Profile has errors' : p.example ? 'Sample profile' : `Profile · ${p.name}`));
}
function renderGlobalWarnings() {
  const box = clear($('#global-warnings')); const s = S.status; if (!s) return;
  if (s.config_error) box.append(notice('warn', h('b', {}, 'Settings problem: '), s.config_error + ' ', h('a', { href: '#', 'data-goto': 'profile', 'data-file': 'config' }, 'Open the settings file')));
  if (!s.profile.ok) box.append(notice('error', h('b', {}, 'Your profile file has errors: '), s.profile.errors.slice(0, 3).join(' · '), ' ',
    h('a', { href: '#', 'data-goto': 'profile', 'data-file': 'profile' }, 'Fix it')));
  else if (s.profile.example) box.append(notice('warn', h('b', {}, 'You are using the sample profile. '),
    'Letters and CVs are built only from your profile, so replace it with your real data first. ',
    h('a', { href: '#', 'data-goto': 'profile', 'data-file': 'profile' }, 'Edit my profile')));
  if (!s.demo && !s.llm.ok) box.append(notice('warn', h('b', {}, 'The AI model is not reachable. '), s.llm.error + ' ',
    h('a', { href: '#', 'data-goto': 'setup' }, 'Setup help')));
  else if (!s.demo && s.llm.ok && !s.llm.model_installed) box.append(notice('warn', h('b', {}, `Model “${s.llm.model}” is not installed. `),
    'Run ', h('code', {}, 'ollama pull ' + s.llm.model), ' or pick an installed model in ', h('a', { href: '#', 'data-goto': 'setup' }, 'Setup'), '.'));
}
function fillModelSelect() {
  const sel = $('#model-select'); const s = S.status; if (!s || !sel) return;
  const cur = sel.value || s.settings.model;
  clear(sel);
  const names = s.llm.models.map(m => m.name);
  if (!names.includes(s.settings.model)) names.unshift(s.settings.model);
  names.forEach(n => sel.append(h('option', { value: n }, n + (n === s.settings.model ? ' (default)' : ''))));
  sel.value = names.includes(cur) ? cur : s.settings.model;
}

// ============================================================ task progress
async function runTask(taskId, box, title) {
  box.hidden = false; box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  let last = null;
  const cancelBtn = h('button', { onclick: async () => { await api(`/api/tasks/${taskId}/cancel`, { method: 'POST' }); cancelBtn.disabled = true; cancelBtn.textContent = 'Cancelling…'; } }, 'Cancel');
  for (;;) {
    const t = await api(`/api/tasks/${taskId}`);
    last = t;
    clear(box).append(
      h('h2', {}, t.status === 'queued' ? 'Waiting for the model…' : title),
      t.status === 'queued' ? h('p', { class: 'muted' }, `Queue position ${t.queue_position}: one text is generated at a time.`) : null,
      h('ul', { class: 'steps' }, t.steps.map(s => h('li', { class: s.status }, h('span', { class: 'dot' }), s.label, s.detail ? h('small', { class: 'muted' }, ' – ' + s.detail) : null))),
      h('div', { class: 'row' }, h('span', { class: 'muted small' }, `${t.detail ? t.detail + ' · ' : ''}${Math.round(t.elapsed)} s`), cancelBtn),
      h('p', { class: 'muted small' }, 'Local models can take a minute or two per letter on a laptop without a GPU. You can keep this tab open.'));
    if (['done', 'error', 'cancelled'].includes(t.status)) break;
    await sleep(700);
  }
  return last;
}
function taskFailed(box, t) {
  clear(box).append(
    t.status === 'cancelled' ? notice('info', 'Cancelled.') : notice('error', h('b', {}, 'Could not finish: '), t.error,
      t.error_kind === 'model_missing' || t.error_kind === 'unreachable' ? h('span', {}, ' ', h('a', { href: '#', 'data-goto': 'setup' }, 'Open Setup')) : null),
    h('button', { onclick: () => { box.hidden = true; } }, 'Close'));
}

// ============================================================ NEW APPLICATION
function setJob(res, sourceNote) {
  S.job = res.job;
  $('#job-preview').hidden = false; $('#gen-card').hidden = false; $('#progress-card').hidden = true;
  $('#job-title').value = res.job.title || ''; $('#job-company').value = res.job.company || '';
  $('#job-lang').value = res.job.language || 'unknown'; $('#job-desc').value = res.job.description || '';
  $('#job-chars').textContent = (res.job.description || '').length;
  $('#job-source').textContent = sourceNote || ({ jsonld: '(structured data from the page)', html: '(text extracted from the page)', pasted: '(pasted text)', bookmarklet: '(sent by the bookmarklet)' }[res.job.source] || '');
  if (res.job.url) $('#job-url').value = res.job.url;
  $('#company-url').value = res.job.company_url || $('#company-url').value;
  clear($('#fit-list')).append(...fitRows(res.fit || []));
  $('#job-preview').scrollIntoView({ behavior: 'smooth', block: 'start' });
  updateGenHint();
}
function currentJob() {
  return { ...S.job, title: $('#job-title').value.trim(), company: $('#job-company').value.trim(), language: $('#job-lang').value, description: $('#job-desc').value.trim() };
}
const recheckFit = debounce(async () => {
  if (!S.job) return;
  $('#job-chars').textContent = $('#job-desc').value.length;
  try { clear($('#fit-list')).append(...fitRows(await api('/api/job/fit', { method: 'POST', body: currentJob() }))); } catch (e) { /* ignore */ }
  updateGenHint();
}, 700);
['#job-desc', '#job-title', '#job-lang'].forEach(s => $(s).addEventListener('input', recheckFit));
$('#job-lang').addEventListener('change', recheckFit);

function updateGenHint() {
  const j = S.job ? currentJob() : null; const hint = $('#gen-hint'); const btn = $('#btn-generate');
  const ok = j && j.description.length >= 80;
  btn.disabled = !ok;
  hint.textContent = !j ? '' : !ok ? 'The job text is too short.' : '';
}
function fetchError(err) {
  const box = $('#fetch-error'); box.hidden = false; clear(box);
  const d = err.data && err.data.detail;
  box.append(h('b', {}, err.message));
  if (d && d.blocked) {
    $('#paste-box').hidden = false; $('.bookmarklet').open = true;
    box.append(h('div', { class: 'small' }, 'Paste the text below or use the bookmarklet: both always work.'));
  }
}
$('#fetch-form').addEventListener('submit', async e => {
  e.preventDefault(); const url = $('#job-url').value.trim(); if (!url) return;
  const btn = $('#btn-fetch'); btn.disabled = true; btn.textContent = 'Fetching…'; $('#fetch-error').hidden = true;
  try { setJob(await api('/api/job/fetch', { method: 'POST', body: { url } })); }
  catch (err) { fetchError(err); }
  finally { btn.disabled = false; btn.textContent = 'Fetch'; }
});
$('#btn-toggle-paste').addEventListener('click', () => { $('#paste-box').hidden = !$('#paste-box').hidden; });
$('#btn-use-paste').addEventListener('click', async () => {
  const text = $('#paste-text').value.trim();
  try {
    setJob(await api('/api/job/ingest', { method: 'POST', body: { source: 'paste', text, title: $('#paste-title').value.trim(), company: $('#paste-company').value.trim(), url: $('#job-url').value.trim() } }));
    $('#fetch-error').hidden = true;
  } catch (err) { fetchError(err); }
});
async function loadSample(lang) {
  try {
    const s = await api('/api/sample/' + lang);
    $('#fetch-error').hidden = true;
    setJob(await api('/api/job/ingest', { method: 'POST', body: { source: 'paste', text: s.text, company: 'Nimbus Logistics' } }), '(sample posting)');
  } catch (err) { fetchError(err); }
}
$('#btn-sample-fr').addEventListener('click', () => loadSample('fr'));
$('#btn-sample-en').addEventListener('click', () => loadSample('en'));

const BP_MAIN = { letter: 'letter.yaml', email: 'email.yaml' };
function bpItems(kind) { return (S.blueprints && S.blueprints[kind] && S.blueprints[kind].items) || []; }
function bpKey(kind, file) { return file === BP_MAIN[kind] ? (kind === 'letter' ? 'letter_blueprint' : 'email_blueprint') : 'bp:' + file; }
function renderBlueprintSummary() {
  const items = bpItems('letter'); const sel = $('#bp-select'); if (!items.length) return;
  const saved = LS.get('bp_letter', BP_MAIN.letter); const cur = sel.value || (items.some(i => i.file === saved) ? saved : items[0].file);
  clear(sel); items.forEach(i => sel.append(h('option', { value: i.file }, i.name))); sel.value = items.some(i => i.file === cur) ? cur : items[0].file;
  const bp = items.find(i => i.file === sel.value);
  const box = clear($('#bp-summary'));
  box.append(...bp.paragraphs.map(p => h('span', { class: 'tag' + (p.kind === 'fixed' ? ' fixed' : ''), title: p.kind === 'fixed' ? 'your own text' : 'written by the model' }, p.label)));
  if (bp.errors) box.append(notice('error', 'This form has errors: ' + bp.errors[0]));
  const sp = bpItems('email'); $('#sp-bp-wrap').hidden = sp.length < 2;
  if (sp.length > 1) { const s2 = $('#sp-bp'); const c2 = s2.value; clear(s2); sp.forEach(i => s2.append(h('option', { value: i.file }, i.name))); if (sp.some(i => i.file === c2)) s2.value = c2; }
}
$('#bp-select').addEventListener('change', () => { LS.set('bp_letter', $('#bp-select').value); renderBlueprintSummary(); });
$('#bp-edit').addEventListener('click', () => { showTab('profile'); openEditor(bpKey('letter', $('#bp-select').value)); });
const chosenForm = (kind, selectId) => { const v = $(selectId).value; return !v || v === BP_MAIN[kind] ? '' : v; };

$('#btn-generate').addEventListener('click', async () => {
  const lang = ($('input[name=lang]:checked') || {}).value || 'auto';
  LS.set('lang', lang); LS.set('with_cv', $('#with-cv').checked);
  const body = {
    job: currentJob(), lang, note: $('#note').value.trim(), contact_name: $('#contact-name').value.trim(), contact_role: $('#contact-role').value.trim(),
    greeting_style: $('#greeting').value, company_url: $('#company-url').value.trim(), with_cv: $('#with-cv').checked, blueprint: chosenForm('letter', '#bp-select'),
    model: $('#model-select').value === (S.status && S.status.settings.model) ? '' : $('#model-select').value,
  };
  const box = $('#progress-card'); const btn = $('#btn-generate'); btn.disabled = true;
  try {
    const { task_id } = await api('/api/application/generate', { method: 'POST', body });
    box.scrollIntoView({ behavior: 'smooth', block: 'start' });
    const t = await runTask(task_id, box, 'Writing your application…');
    if (t.status === 'done') { box.hidden = true; await openApp(t.result.application_id); } else taskFailed(box, t);
  } catch (err) { box.hidden = false; clear(box).append(notice('error', err.message, err.data && err.data.detail && err.data.detail.where === 'setup' ? h('span', {}, ' ', h('a', { href: '#', 'data-goto': 'setup' }, 'Open Setup')) : null)); }
  finally { btn.disabled = false; updateGenHint(); }
});

// ---- bookmarklet
function bookmarkletHref() {
  const app = location.origin + '/';
  const code = `(function(){var APP=${JSON.stringify(app)};function jl(){var out=null;try{document.querySelectorAll('script[type="application/ld+json"]').forEach(function(s){try{(function walk(x){if(!x||typeof x!=='object')return;if(Array.isArray(x)){x.forEach(walk);return}var t=x['@type'];if(t==='JobPosting'||(Array.isArray(t)&&t.indexOf('JobPosting')>-1)){out=out||x}if(x['@graph'])walk(x['@graph'])})(JSON.parse(s.textContent))}catch(e){}})}catch(e){}return out}var sel=String(window.getSelection?window.getSelection():'').trim();var m=document.querySelector('main')||document.body;var p={url:location.href,title:document.title,selection:sel.slice(0,25000),text:(m.innerText||'').slice(0,25000),jsonld:jl()};var b=function(o){return btoa(unescape(encodeURIComponent(JSON.stringify(o)))).replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'')};var s=b(p);if(s.length>900000){p.jsonld=null;s=b(p)}window.open(APP+'#ingest='+s,'_blank')})();`;
  return 'javascript:' + encodeURIComponent(code);
}
async function handleIngestHash() {
  const m = location.hash.match(/^#ingest=(.+)$/); if (!m) return;
  history.replaceState(null, '', location.pathname);
  showTab('new');
  try {
    let b64 = m[1].replace(/-/g, '+').replace(/_/g, '/'); while (b64.length % 4) b64 += '=';
    const payload = JSON.parse(decodeURIComponent(escape(atob(b64))));
    payload.source = 'bookmarklet';
    setJob(await api('/api/job/ingest', { method: 'POST', body: payload }));
    toast('Job page received from the bookmarklet');
  } catch (err) { fetchError(err instanceof ApiError ? err : new ApiError('The bookmarklet data could not be read: ' + err.message, 0)); }
}
window.addEventListener('hashchange', handleIngestHash);

// ============================================================ SPONTANEOUS
function renderDomainChips() {
  const box = clear($('#sp-domains'));
  S.domains.forEach(d => {
    const c = h('span', { class: 'chip' + (S.spDomains.has(d.key) ? ' on' : ''), role: 'button', tabindex: 0, onclick: () => { S.spDomains.has(d.key) ? S.spDomains.delete(d.key) : S.spDomains.add(d.key); renderDomainChips(); } }, d.short);
    box.append(c);
  });
}
$('#btn-sp-generate').addEventListener('click', async () => {
  const lang = ($('input[name=sp-lang]:checked') || {}).value || 'auto';
  const body = {
    company: $('#sp-company').value.trim(), company_url: $('#sp-url').value.trim(), contact_name: $('#sp-contact').value.trim(),
    contact_role: $('#sp-role').value.trim(), contact_email: $('#sp-email').value.trim(), domains: [...S.spDomains], role_focus: $('#sp-focus').value.trim(),
    note: $('#sp-note').value.trim(), lang, greeting_style: $('#sp-greeting').value, with_cv: $('#sp-cv').checked, blueprint: $('#sp-bp-wrap').hidden ? '' : chosenForm('email', '#sp-bp'),
  };
  const hint = $('#sp-hint'); hint.textContent = '';
  if (!body.company) { hint.textContent = 'Please enter the company.'; return; }
  if (!body.domains.length && !body.role_focus) { hint.textContent = 'Pick at least one domain or describe what you are aiming for.'; return; }
  const box = $('#sp-progress'); const btn = $('#btn-sp-generate'); btn.disabled = true;
  try {
    const { task_id } = await api('/api/spontaneous/generate', { method: 'POST', body });
    box.scrollIntoView({ behavior: 'smooth', block: 'start' });
    const t = await runTask(task_id, box, 'Writing your e-mail…');
    if (t.status === 'done') { box.hidden = true; await openApp(t.result.application_id); } else taskFailed(box, t);
  } catch (err) { box.hidden = false; clear(box).append(notice('error', err.message)); }
  finally { btn.disabled = false; }
});

// ============================================================ TAILOR CV
const tailorBody = () => ({
  text: $('#tl-text').value, title: $('#tl-title').value.trim(), company: $('#tl-company').value.trim(),
  lang: ($('input[name=tl-lang]:checked') || {}).value || 'auto',
});
function tailorShow(kind, msg) { const b = $('#tl-error'); b.hidden = !msg; clear(b); if (msg) b.append(h('span', {}, msg)); b.className = 'notice ' + kind; b.hidden = !msg; }
function renderTailorReport(rep) {
  const box = clear($('#tl-report')); box.hidden = false;
  const state = { picked: new Set(rep.default_keywords) };
  const sync = () => { $('#tl-build').textContent = `Build tailored CV (${state.picked.size} keyword${state.picked.size === 1 ? '' : 's'} added)`; };
  const chip = (it, kind) => {
    const on = state.picked.has(it.display);
    return h('label', { class: 'kw ' + kind, title: kind === 'gap' ? 'Not found in your profile: tick only if you really have this skill' : '' },
      h('input', { type: 'checkbox', checked: on, onchange: e => { e.target.checked ? state.picked.add(it.display) : state.picked.delete(it.display); sync(); } }),
      it.display, it.count > 1 ? h('small', { class: 'muted' }, ` ×${it.count}`) : null);
  };
  const add = rep.items.filter(i => i.status === 'covered' && !i.in_cv);
  const there = rep.items.filter(i => i.in_cv);
  const gaps = rep.items.filter(i => i.status === 'gap');
  put(box, h('div', { class: 'row' }, h('b', {}, 'Keyword match'), h('span', { class: 'pill warn' }, `${rep.score_before}% now`), '→', h('span', { class: 'pill ok' }, `${rep.score_after}% with the ticked keywords`)));
  (rep.notes || []).forEach(n => box.append(notice('info', n)));
  if (!rep.items.length) { box.append(notice('warn', 'No known technology keyword was found in this text. Check that you pasted the job description.')); }
  if (add.length) put(box, h('h3', {}, 'Add to your CV (backed by your profile)'), h('div', { class: 'kwlist' }, add.map(i => chip(i, 'add'))));
  if (there.length) put(box, h('h3', {}, 'Already visible on your CV'), h('div', { class: 'kwlist' }, there.map(i => h('span', { class: 'kw there' }, '✓ ' + i.display))));
  if (gaps.length) put(box, h('h3', {}, 'Asked by the recruiter, but not in your profile'),
    h('p', { class: 'muted small' }, 'Never added automatically. Tick a keyword only if you genuinely have the skill (then add it to your profile too).'),
    h('div', { class: 'kwlist' }, gaps.map(i => chip(i, 'gap'))));
  put(box, h('div', { class: 'row' }, h('button', { class: 'primary big', id: 'tl-build', onclick: tailorBuild(state) }, '')));
  sync();
  box._state = state;
}
function tailorBuild(state) {
  return async e => {
    const btn = e.currentTarget; btn.disabled = true;
    try {
      const a = await api('/api/tailor/build', { method: 'POST', body: { ...tailorBody(), keywords: [...state.picked] } });
      await openApp(a.id);
    } catch (err) { tailorShow('error', err.message); }
    finally { btn.disabled = false; }
  };
}
$('#btn-tl-analyze').addEventListener('click', async () => {
  const btn = $('#btn-tl-analyze'); btn.disabled = true; tailorShow('', '');
  try {
    const rep = await api('/api/tailor/analyze', { method: 'POST', body: tailorBody() });
    renderTailorReport(rep);
  } catch (err) { clear($('#tl-report')).hidden = true; tailorShow('error', err.message); }
  finally { btn.disabled = false; }
});
$('#btn-tl-sample').addEventListener('click', async () => {
  const lang = ($('input[name=tl-lang]:checked') || {}).value === 'fr' ? 'fr' : 'en';
  $('#tl-text').value = (await api('/api/sample/' + lang)).text;
});

// ============================================================ TRACKER
async function loadTracker() {
  const data = await api('/api/applications'); const items = data.items;
  const filter = $('#tr-filter');
  if (filter.options.length <= 1) data.statuses.forEach(s => filter.append(h('option', { value: s }, s)));
  const f = filter.value; const today = todayISO();
  const due = items.filter(a => a.status === 'sent' && a.follow_up_on && a.follow_up_on <= today);
  const badge = $('#due-badge'); badge.hidden = !due.length; badge.textContent = due.length;
  clear($('#due-box'));
  if (due.length) $('#due-box').append(notice('warn', h('b', {}, `${due.length} follow-up${due.length > 1 ? 's' : ''} due: `),
    due.map(a => h('a', { href: '#', onclick: e => { e.preventDefault(); openApp(a.id); } }, `${a.company} `))));
  const rows = items.filter(a => !f || a.status === f);
  const box = clear($('#tracker-table'));
  if (!rows.length) { box.append(h('p', { class: 'muted' }, items.length ? 'Nothing with that status.' : 'No application yet: generate one from the first tab.')); return; }
  box.append(h('table', { class: 'tr' },
    h('thead', {}, h('tr', {}, ['Created', 'Type', 'Company', 'Role', 'Contact', 'Status', 'Follow-up', ''].map(x => h('th', {}, x)))),
    h('tbody', {}, rows.map(a => h('tr', {},
      h('td', {}, fmtDate(a.created_at)), h('td', {}, a.kind === 'job' ? 'Job' : a.kind === 'cv' ? 'CV only' : 'Spontaneous'),
      h('td', {}, h('a', { href: '#', onclick: e => { e.preventDefault(); openApp(a.id); } }, a.company || '(no name)')),
      h('td', {}, a.role), h('td', {}, a.contact),
      h('td', {}, statusSelect(a.status, async v => { await api(`/api/application/${a.id}/status`, { method: 'PUT', body: { status: v } }); toast('Status updated'); loadTracker(); })),
      h('td', { class: a.status === 'sent' && a.follow_up_on && a.follow_up_on <= today ? 'overdue' : '' }, a.follow_up_on || '–'),
      h('td', {}, h('button', { class: 'small danger-outline', onclick: async () => { if (confirm(`Delete the application to ${a.company}? This removes its files.`)) { await api(`/api/application/${a.id}`, { method: 'DELETE' }); loadTracker(); } } }, 'Delete')))))));
}
const STATUSES = ['drafted', 'sent', 'replied', 'interview', 'offer', 'rejected', 'withdrawn'];
function statusSelect(value, onchange) {
  return h('select', { class: 'st-' + value, onchange: e => onchange(e.target.value) }, STATUSES.map(s => h('option', { value: s, selected: s === value }, s)));
}
$('#tr-filter').addEventListener('change', loadTracker);

// ============================================================ PROFILE & TEMPLATES
const ED_HELP = {
  profile: 'Your facts: identity, availability, education, experience, projects, skills, languages. Nothing outside this file can appear in a letter or CV. Text fields can be a string or {en: …, fr: …}.',
  letter_blueprint: 'The FORM and GENERAL CONTENT of your cover letters: tone, avoided phrases, paragraph order, which paragraphs are written by the model and which are your own fixed text.',
  email_blueprint: 'Same idea for the short spontaneous e-mail (hook, value, ask).',
  cv_template: 'The LaTeX of your CV. \\VAR{x} prints a value (auto-escaped), \\BLOCK{ for … } loops. See “How do I use my own LaTeX CV?” below.',
  letter_template: 'The LaTeX layout of the cover letter PDF (header, date, recipient, subject, body, signature).',
  config: 'Technical settings: model, context size, temperatures, LaTeX engine, page limit, fetch options.',
};
const ek = key => encodeURIComponent(key);
const isForm = key => key === 'letter_blueprint' || key === 'email_blueprint' || key.startsWith('bp:');
async function openEditor(key) {
  S.editorKey = key;
  const list = await api('/api/editor');
  clear($('#file-list')).append(...list.map(f => h('button', { class: 'fbtn' + (f.key === key ? ' active' : ''), onclick: () => openEditor(f.key) }, f.label, h('small', {}, f.syntax === 'latex' ? 'LaTeX' : 'YAML'))));
  const f = await api('/api/editor/' + ek(key));
  S.edText = f.text;
  $('#ed-title').textContent = f.label; $('#ed-help').textContent = ED_HELP[key] || (isForm(key) ? ED_HELP.letter_blueprint : ''); $('#ed-text').value = f.text;
  $('#ed-reset').hidden = f.has_default === false; $('#ed-duplicate').hidden = !isForm(key);
  $('#ed-state').textContent = f.is_default ? 'default version' : 'customised'; clear($('#ed-msgs')); $('#ed-preview').hidden = true;
  $('#ed-cv-tools').hidden = key !== 'cv_template'; $('#data-dir').textContent = S.status ? S.status.data_dir : '';
}
function showEdMsgs(res) {
  const box = clear($('#ed-msgs'));
  if (res.errors && res.errors.length) box.append(notice('error', h('b', {}, 'Not saved. '), h('ul', {}, res.errors.map(e => h('li', {}, e)))));
  if (res.warnings && res.warnings.length) box.append(notice('warn', h('ul', {}, res.warnings.map(e => h('li', {}, e)))));
  if (res.ok && !(res.warnings || []).length) box.append(notice('ok', 'Looks good.'));
}
$('#ed-save').addEventListener('click', async () => {
  try { const r = await api('/api/editor/' + ek(S.editorKey), { method: 'PUT', body: { text: $('#ed-text').value } }); showEdMsgs(r); toast('Saved'); await refreshStatus(); await loadBlueprints(); $('#ed-state').textContent = 'customised'; }
  catch (e) { showEdMsgs(e.data || { errors: [e.message] }); }
});
$('#ed-validate').addEventListener('click', async () => {
  try { showEdMsgs(await api(`/api/editor/${ek(S.editorKey)}/validate`, { method: 'POST', body: { text: $('#ed-text').value } })); } catch (e) { showEdMsgs({ errors: [e.message] }); }
});
$('#ed-reset').addEventListener('click', async () => {
  if (!confirm('Replace this file with the default version? (A .bak backup of your current file is kept.)')) return;
  const f = await api(`/api/editor/${ek(S.editorKey)}/reset`, { method: 'POST' }); $('#ed-text').value = f.text; clear($('#ed-msgs')); toast('Reset to default'); refreshStatus();
});
async function buildTestCv(lang) {
  const box = clear($('#ed-preview')); box.hidden = false; box.append(h('p', { class: 'muted' }, 'Compiling…'));
  // save first so the preview reflects what you see
  try { await api('/api/editor/cv_template', { method: 'PUT', body: { text: $('#ed-text').value } }); } catch (e) { showEdMsgs(e.data || { errors: [e.message] }); box.hidden = true; return; }
  const r = await api('/api/cv/preview', { method: 'POST', body: { lang } });
  clear(box);
  if (r.ok) put(box, r.message ? notice('warn', r.message) : null, h('p', { class: 'muted small' }, `${r.pages} page(s), engine ${r.engine}`), h('iframe', { class: 'pdf', src: r.pdf + '?t=' + Date.now() + '#view=FitH' }));
  else put(box, notice('error', h('b', {}, 'The CV did not compile. '), r.message), r.log_tail ? h('pre', { class: 'mail' }, r.log_tail) : null);
}
$('#ed-duplicate').addEventListener('click', async () => {
  const name = prompt('Name of the new form (it starts as a copy of this one):'); if (!name) return;
  const key = S.editorKey; const source = key === 'letter_blueprint' ? 'letter.yaml' : key === 'email_blueprint' ? 'email.yaml' : key.slice(3);
  const r = await api('/api/blueprints/duplicate', { method: 'POST', body: { source, name } });
  await loadBlueprints(); toast('Form created: choose it on the “New application” tab'); await openEditor(r.key);
});
$('#ed-build-en').addEventListener('click', () => buildTestCv('en'));
$('#ed-build-fr').addEventListener('click', () => buildTestCv('fr'));

// ============================================================ SETUP
const MODELS = [
  ['qwen3.5:4b', '3.4 GB', 'Modest laptops (8 GB RAM), quick drafts'],
  ['gemma4:e4b', '≈ 7–10 GB', 'Default: good French and English on a 16 GB laptop'],
  ['qwen3.5:9b', '6.6 GB', 'Better writing, 16 GB RAM or a GPU with 8+ GB'],
  ['gemma4:12b', '≈ 8 GB', 'Best quality that still fits a 16 GB machine'],
  ['qwen3.5:27b', '17 GB', '32 GB RAM or a 24 GB GPU'],
];
async function renderSetup() {
  await refreshStatus();
  const s = S.status; const box = clear($('#setup-body')); if (!s) return;
  const l = s.llm;
  const modelCard = h('div', { class: 'card' }, h('h2', {}, 'AI model (Ollama, runs on your computer)'),
    s.demo ? notice('warn', 'Demo mode is on (COPILOT_LLM=fake). Unset it to use a real model.') : null,
    s.demo ? null : !l.ok ? h('div', {}, notice('error', l.error), h('ol', {}, h('li', {}, 'Install Ollama from ', h('a', { href: 'https://ollama.com/download', target: '_blank', rel: 'noopener' }, 'ollama.com/download'), ' and start it.'),
      h('li', {}, 'Download a model: ', h('code', {}, 'ollama pull ' + s.settings.model)), h('li', {}, 'Come back here and press ', h('b', {}, 'Refresh'), '.')))
      : h('div', {}, notice(l.model_installed ? 'ok' : 'warn', l.model_installed ? `Ollama ${l.version} is running; model “${l.model}” is ready.` : `Ollama ${l.version} is running but “${l.model}” is not installed.`),
        h('h3', {}, 'Installed models'),
        l.models.length ? h('table', { class: 'tr' }, h('thead', {}, h('tr', {}, ['', 'Model', 'Size', 'Parameters'].map(x => h('th', {}, x)))),
          h('tbody', {}, l.models.map(m => h('tr', {}, h('td', {}, h('input', { type: 'radio', name: 'usemodel', checked: m.name === l.model, onchange: async () => { await api('/api/settings/model', { method: 'POST', body: { model: m.name } }); toast('Model set to ' + m.name); renderSetup(); } })),
            h('td', {}, m.name), h('td', {}, m.size_gb + ' GB'), h('td', {}, m.params + ' ' + m.quant))))) : h('p', { class: 'muted' }, 'No model installed yet.')),
    h('h3', {}, 'Which model?'), h('p', { class: 'muted small' }, 'Sizes are approximate: check ollama.com/library. Bigger models write better but need more memory and time. Always read what is generated.'),
    h('table', { class: 'tr' }, h('thead', {}, h('tr', {}, ['Model', 'Disk', 'Good for', 'Install'].map(x => h('th', {}, x)))),
      h('tbody', {}, MODELS.map(([n, sz, d]) => h('tr', {}, h('td', {}, n), h('td', {}, sz), h('td', {}, d), h('td', {}, h('button', { class: 'small', onclick: () => copyText('ollama pull ' + n, 'Command') }, 'Copy “ollama pull”')))))),
    h('div', { class: 'row' }, h('button', { onclick: renderSetup }, 'Refresh')));
  const latexCard = h('div', { class: 'card' }, h('h2', {}, 'LaTeX'),
    s.latex.engine ? notice('ok', `Found ${s.latex.engine} (${s.latex.path}). PDFs are compiled locally.`) : notice('warn', 'No LaTeX engine found. You still get .tex files you can compile in Overleaf.'),
    h('ul', { class: 'small' }, h('li', {}, 'Windows: install ', h('b', {}, 'MiKTeX'), ' (installs missing packages on demand).'), h('li', {}, 'macOS: ', h('b', {}, 'MacTeX'), ' or BasicTeX + ', h('code', {}, 'sudo tlmgr install enumitem titlesec microtype lm collection-langfrench')),
      h('li', {}, 'Linux: ', h('code', {}, 'sudo apt install texlive-latex-extra texlive-lang-french texlive-fonts-recommended'))));
  const fetchCard = h('div', { class: 'card' }, h('h2', {}, 'Job pages that need JavaScript (optional)'),
    h('p', { class: 'small' }, s.playwright ? 'Playwright is installed: such pages are loaded in a headless browser automatically.' : 'Not installed. Usually unnecessary: the bookmarklet and paste box work everywhere.'),
    !s.playwright ? h('p', {}, h('code', {}, 'pip install playwright && playwright install chromium')) : null);
  const bm = h('div', { class: 'card' }, h('h2', {}, 'Bookmarklet'), h('p', { class: 'small' }, 'Drag to your bookmarks bar, then click it on any job page (LinkedIn, Indeed, WTTJ, company sites). It sends only the page text to this local app.'),
    h('p', {}, h('a', { class: 'bm', href: bookmarkletHref() }, '➜ Send to Copilot')));
  const privacy = h('div', { class: 'card' }, h('h2', {}, 'Privacy & safety'), h('ul', { class: 'small' },
    h('li', {}, 'Everything runs on your computer. Your profile, letters and CVs never leave it.'),
    h('li', {}, 'Network access is limited to: the job page (and company website) you ask for, and Ollama on localhost.'),
    h('li', {}, 'Nothing is ever sent or submitted automatically: you review, download, and send yourself.'),
    h('li', {}, 'Data folder: ', h('code', {}, s.data_dir))));
  box.append(modelCard, latexCard, fetchCard, bm, privacy);
}

// ============================================================ APPLICATION WORKSPACE
async function openApp(id) {
  S.app = await api('/api/application/' + id); S.appTab = S.app.kind === 'cv' ? 'cv' : 'doc';
  $$('.tab').forEach(t => t.classList.toggle('active', t.id === 'tab-app')); $$('#tabs button').forEach(b => b.classList.remove('active'));
  renderApp(); window.scrollTo({ top: 0 });
}
const pdfUrl = (id, f) => `/api/download/${id}/${f}?inline=1&t=${Date.now()}#view=FitH`;
const dl = (id, f) => `/api/download/${id}/${f}`;
const hasFile = (a, f) => (a.file_info || []).some(x => x.name === f);

function renderApp() {
  const a = S.app; const box = clear($('#app-body')); const isJob = a.kind === 'job'; const isCv = a.kind === 'cv';
  box.append(
    h('div', { class: 'row between' },
      h('div', { class: 'row' }, h('button', { onclick: () => showTab(isJob ? 'new' : isCv ? 'tailor' : 'spont') }, '← New'), h('button', { onclick: () => showTab('tracker') }, 'Tracker'),
        h('h2', { style: 'margin:0' }, a.company, h('small', { class: 'muted' }, ` ${isJob || isCv ? a.role : 'Spontaneous application'} · ${a.lang.toUpperCase()}`))),
      h('div', { class: 'row' }, h('span', { class: 'muted small' }, 'Status'), statusSelect(a.status, async v => { await api(`/api/application/${a.id}/status`, { method: 'PUT', body: { status: v } }); S.app = await api('/api/application/' + a.id); renderApp(); toast('Status updated'); }))),
    ...(a.messages || []).map(m => notice(/SAMPLE|too long|nothing more/i.test(m) ? 'warn' : 'info', m)),
    h('div', { class: 'ws' }, h('div', {}, isCv ? null : docCard(a), cvCard(a), analysisCard(a), trackCard(a)), previewCard(a)));
  requestAnimationFrame(() => $$('.para textarea', box).forEach(autosize));
}

// ---- document (letter or e-mail) editor
function docCard(a) {
  const d = a.doc; if (!d) return h('div', { class: 'card' }, notice('warn', 'No document was generated.'));
  const isJob = a.kind === 'job'; const fields = {};
  const regenBox = h('div', { id: 'regen-progress', class: 'inline-progress', hidden: true });
  const inputs = [];
  const mk = (label, key, value) => { const i = h('input', { type: 'text', value }); fields[key] = i; return h('label', {}, label, i); };
  const paraEls = d.paragraphs.map(p => {
    const ta = h('textarea', { rows: Math.max(3, Math.ceil(p.text.length / 85)), value: p.text }); fields['p:' + p.id] = ta;
    const wc = h('span', { class: 'wc' });
    const upd = () => { const n = (ta.value.match(/\S+/g) || []).length; wc.textContent = `${n} / ${p.max_words} words`; wc.classList.toggle('over', n > p.max_words * 1.15); };
    ta.addEventListener('input', () => { upd(); autosize(ta); }); upd();
    const extra = h('input', { type: 'text', placeholder: 'Optional instruction, e.g. “mention my Kubernetes homelab” or “shorter”' });
    return h('div', { class: 'para' },
      h('div', { class: 'ph' }, h('span', { class: 'pl' }, p.label, ' ', h('span', { class: 'tag' + (p.kind === 'fixed' ? ' fixed' : '') }, p.kind === 'fixed' ? 'your text' : p.edited ? 'edited' : 'generated')), wc),
      ta, ...(p.flags || []).map(f => h('div', { class: `flag ${f.level}` }, f.message)),
      p.kind === 'generated' ? h('div', { class: 'regen' }, extra, h('button', { class: 'small', onclick: () => regenerate([p.id], extra.value, regenBox) }, 'Regenerate')) : null);
  });
  const edits = () => {
    const paragraphs = {}; d.paragraphs.forEach(p => { paragraphs[p.id] = fields['p:' + p.id].value; });
    return { paragraphs, subject: fields.subject.value, salutation: fields.salutation.value, closing: fields.closing.value };
  };
  const save = async () => { S.app = await api(`/api/application/${a.id}/edits`, { method: 'PUT', body: edits() }); renderApp(); toast(isJob ? 'Saved and PDF rebuilt' : 'Saved'); };
  async function regenerate(ids, extra, box) {
    await api(`/api/application/${a.id}/edits`, { method: 'PUT', body: edits() });
    try {
      const { task_id } = await api(`/api/application/${a.id}/regenerate`, { method: 'POST', body: { paragraph_ids: ids, extra } });
      const t = await runTask(task_id, box, 'Rewriting…');
      if (t.status === 'done') { S.app = await api('/api/application/' + a.id); renderApp(); } else taskFailed(box, t);
    } catch (err) { box.hidden = false; clear(box).append(notice('error', err.message)); }
  }
  const actions = isJob
    ? h('div', { class: 'row' }, h('button', { class: 'primary', onclick: save }, 'Save & rebuild PDF'), h('button', { onclick: () => regenerate(['*'], '', regenBox) }, 'Regenerate all paragraphs'),
        hasFile(a, 'letter.pdf') ? h('a', { class: 'btn', href: dl(a.id, 'letter.pdf') }, 'Download PDF') : null,
        hasFile(a, 'letter.tex') ? h('a', { class: 'btn', href: dl(a.id, 'letter.tex') }, '.tex') : null,
        hasFile(a, 'letter.tex') ? h('button', { onclick: () => openInOverleaf(a.id, 'letter.tex') }, 'Open in Overleaf') : null)
    : h('div', { class: 'row' }, h('button', { class: 'primary', onclick: save }, 'Save'), h('button', { onclick: () => regenerate(['*'], '', regenBox) }, 'Regenerate'),
        h('button', { onclick: () => copyText(fields.subject.value, 'Subject') }, 'Copy subject'),
        h('button', { onclick: async () => { await save(); copyText(S.app.body_text.replace(/^Subject:.*\n\n/, ''), 'E-mail body'); } }, 'Copy body'),
        h('a', { class: 'btn', href: a.mailto || '#', title: 'Opens your mail app with subject and text filled in. Attach the CV yourself.' }, 'Open in mail app'),
        hasFile(a, 'email.eml') ? h('a', { class: 'btn', href: dl(a.id, 'email.eml') }, 'Download .eml draft') : null);
  return h('div', {},
    h('div', { class: 'card' }, h('h2', {}, isJob ? 'Cover letter' : 'E-mail'), regenBox,
      !isJob && a.contact.email ? h('p', { class: 'small' }, 'To: ', h('b', {}, a.contact.email)) : null,
      mk('Subject', 'subject', d.subject), mk('Salutation', 'salutation', d.salutation), ...paraEls, mk('Closing', 'closing', d.closing),
      h('p', { class: 'muted small' }, 'Signature: ', d.signature.join(' · ')), actions,
      !isJob ? h('p', { class: 'muted small' }, 'Remember to attach the CV (', h('b', {}, 'cv.pdf'), '). Nothing is sent automatically.') : null));
}

async function openInOverleaf(id, file) {
  const tex = await (await fetch(dl(id, file))).text();
  const f = h('form', { action: 'https://www.overleaf.com/docs', method: 'post', target: '_blank', style: 'display:none' },
    h('input', { type: 'hidden', name: 'encoded_snip', value: encodeURIComponent(tex) }));
  document.body.append(f); f.submit(); f.remove();
}

// ---- CV plan editor
function cvCard(a) {
  const plan = a.cv_plan; if (!plan) return h('div', {});
  const items = a.cv_items || { projects: [], experience: [] };
  const state = { projects: [], experience: [] };
  const order = (all, selected) => [...selected.filter(i => all.some(x => x.id === i)), ...all.map(x => x.id).filter(i => !selected.includes(i))];
  const build = (kind, all, selected) => order(all, selected).map(id => {
    const it = all.find(x => x.id === id);
    const bsel = plan.bullet_ids[id] || it.bullets.map(b => b.id);
    const bOrder = order(it.bullets, bsel);
    return { id, name: it.name, on: selected.includes(id), score: plan.scores[id], matched: plan.matched[id] || [], bullets: bOrder.map(bid => ({ id: bid, text: it.bullets.find(b => b.id === bid).text, on: bsel.includes(bid) })) };
  });
  state.projects = build('p', items.projects, plan.project_ids); state.experience = build('e', items.experience, plan.experience_ids);
  const headline = h('input', { type: 'text', value: plan.headline });
  const kwInput = h('input', { type: 'text', value: (plan.extra_keywords || []).join(', '), placeholder: 'e.g. Kubernetes, CI/CD, Terraform' });
  const listBox = h('div', {});
  const draw = () => {
    clear(listBox);
    [['Projects (order = order on the CV)', 'projects'], ['Experience', 'experience']].forEach(([title, key]) => {
      listBox.append(h('h3', {}, title));
      state[key].forEach((it, idx) => listBox.append(h('div', { class: 'cvitem' },
        h('div', { class: 't' }, h('input', { type: 'checkbox', checked: it.on, onchange: e => { it.on = e.target.checked; } }), it.name,
          it.matched.length ? h('span', { class: 'tag' }, 'matches: ' + it.matched.slice(0, 4).join(', ')) : null,
          h('span', { class: 'score' }, it.score !== undefined ? 'relevance ' + it.score.toFixed(1) : ''),
          h('button', { class: 'small', title: 'Move up', disabled: idx === 0, onclick: () => { [state[key][idx - 1], state[key][idx]] = [state[key][idx], state[key][idx - 1]]; draw(); } }, '↑'),
          h('button', { class: 'small', title: 'Move down', disabled: idx === state[key].length - 1, onclick: () => { [state[key][idx + 1], state[key][idx]] = [state[key][idx], state[key][idx + 1]]; draw(); } }, '↓')),
        ...it.bullets.map(b => h('label', { class: 'b' }, h('input', { type: 'checkbox', checked: b.on, onchange: e => { b.on = e.target.checked; } }), b.text)))));
    });
  };
  draw();
  const collect = () => {
    const p = JSON.parse(JSON.stringify(plan));
    p.headline = headline.value;
    p.extra_keywords = kwInput.value.split(',').map(x => x.trim()).filter(Boolean);
    p.project_ids = state.projects.filter(x => x.on).map(x => x.id);
    p.experience_ids = state.experience.filter(x => x.on).map(x => x.id);
    p.bullet_ids = {};
    [...state.projects, ...state.experience].forEach(it => { p.bullet_ids[it.id] = it.bullets.filter(b => b.on).map(b => b.id); });
    return p;
  };
  return h('div', { class: 'card' }, h('h2', {}, 'Tailored CV'),
    h('p', { class: 'muted small' }, 'Only selects and reorders what is in your profile. Untick or reorder, then rebuild.'),
    ...(plan.notes || []).slice(0, 4).map(n => h('div', { class: 'muted small' }, '• ' + n)),
    h('label', {}, 'Headline', headline),
    h('label', {}, 'Extra keywords (shown under “Other keywords” in Skills; only list what you can really claim)', kwInput), listBox,
    h('div', { class: 'row' }, h('button', { class: 'primary', onclick: async () => { S.app = await api(`/api/application/${a.id}/edits`, { method: 'PUT', body: { cv_plan: collect() } }); S.appTab = 'cv'; renderApp(); toast('CV rebuilt'); } }, 'Rebuild CV'),
      hasFile(a, 'cv.pdf') ? h('a', { class: 'btn', href: dl(a.id, 'cv.pdf') }, 'Download CV (PDF)') : null,
      hasFile(a, 'cv.tex') ? h('a', { class: 'btn', href: dl(a.id, 'cv.tex') }, '.tex') : null,
      hasFile(a, 'cv.tex') ? h('button', { onclick: () => openInOverleaf(a.id, 'cv.tex') }, 'Open in Overleaf') : null));
}

// ---- analysis / evidence
function analysisCard(a) {
  const an = a.analysis; const parts = [];
  if (a.fit && a.fit.length) parts.push(h('h3', {}, 'Fit with your availability'), h('div', { class: 'fit-list' }, ...fitRows(a.fit)));
  if (an) parts.push(h('h3', {}, 'What the posting asks'), h('dl', { class: 'kv' },
    h('dt', {}, 'Mission'), h('dd', {}, an.mission || '–'), h('dt', {}, 'Must have'), h('dd', {}, (an.must_have || []).join(' · ') || '–'),
    h('dt', {}, 'Nice to have'), h('dd', {}, (an.nice_to_have || []).join(' · ') || '–'), h('dt', {}, 'Tech detected'), h('dd', {}, (an.stack || []).join(', ') || '–'),
    h('dt', {}, 'Domains'), h('dd', {}, (an.domains || []).join(', ') || '–')));
  if (a.kind !== 'cv') {
    parts.push(h('h3', {}, 'Company facts used (verified in the sources)'));
    if (a.facts && a.facts.length) a.facts.forEach(f => parts.push(h('div', {}, '• ' + f.fact, h('blockquote', { class: 'q' }, '“' + f.quote + '” – ' + f.source))));
    else parts.push(h('p', { class: 'muted small' }, 'None found, so the company paragraph stays general. Add a note or a company website and regenerate.'));
  }
  if (a.evidence && a.evidence.length) {
    parts.push(h('h3', {}, 'Your work the writer was given'));
    a.evidence.forEach(e => parts.push(h('div', { class: 'small' }, h('b', {}, e.title), e.matched.length ? h('span', { class: 'muted' }, '  ← ' + e.matched.slice(0, 5).join(', ')) : null)));
  }
  if (a.job) parts.push(h('details', {}, h('summary', {}, 'Job text'), h('pre', { class: 'mail' }, a.job.description), a.url ? h('p', { class: 'small' }, h('a', { href: a.url, target: '_blank', rel: 'noopener' }, a.url)) : null));
  parts.push(h('p', { class: 'muted small' }, (a.kind === 'cv' ? '' : `Model: ${a.model || '?'} · `) + `folder: data/applications/${a.id}`));
  return h('details', { class: 'card', open: false }, h('summary', {}, h('b', {}, a.kind === 'cv' ? 'Job text and fit' : 'Why this letter? Analysis, facts and fit')), ...parts);
}

// ---- tracking card
function trackCard(a) {
  const fu = h('input', { type: 'date', value: a.follow_up_on || '' }); const note = h('textarea', { rows: 2, placeholder: 'Private notes (who you spoke to, next step…)', value: a.note || '' });
  return h('div', { class: 'card' }, h('h2', {}, 'Tracking'),
    h('div', { class: 'grid2' }, h('label', {}, 'Follow-up date', fu), h('div', {}, h('div', { class: 'muted small' }, a.sent_at ? 'Sent on ' + a.sent_at : 'Not marked as sent yet'),
      h('button', { class: 'small', onclick: async () => { await api(`/api/application/${a.id}/status`, { method: 'PUT', body: { status: 'sent', sent_on: todayISO() } }); S.app = await api('/api/application/' + a.id); renderApp(); toast('Marked as sent: follow-up date set'); } }, 'I sent it today'))),
    h('label', {}, 'Notes', note),
    h('button', { class: 'small', onclick: async () => { await api(`/api/application/${a.id}/status`, { method: 'PUT', body: { status: a.status, note: note.value, follow_up_on: fu.value } }); toast('Saved'); } }, 'Save notes'));
}

// ---- preview (right column)
function previewCard(a) {
  const isJob = a.kind !== 'spontaneous'; const tabs = [];
  if (a.kind === 'job' && hasFile(a, 'letter.pdf')) tabs.push(['doc', 'Letter', 'letter.pdf']);
  if (hasFile(a, 'cv.pdf')) tabs.push(['cv', 'CV', 'cv.pdf']);
  const body = h('div', {});
  const draw = () => {
    clear(body);
    if (!isJob && S.appTab === 'doc') {
      body.append(h('div', { class: 'small muted' }, 'Subject'), h('pre', { class: 'mail' }, a.doc ? a.doc.subject : ''), h('div', { class: 'small muted' }, 'Body'), h('pre', { class: 'mail' }, (a.body_text || '').replace(/^Subject:.*\n\n/, '')));
      return;
    }
    const t = tabs.find(x => x[0] === S.appTab) || tabs[0];
    if (!t) { body.append(notice('warn', 'No PDF available. ', (a.messages || []).filter(m => /^(Letter|CV):/.test(m)).join(' ') || 'LaTeX may be missing: see Setup. The .tex files can be compiled in Overleaf.')); return; }
    body.append(h('iframe', { class: 'pdf', src: pdfUrl(a.id, t[2]), title: t[1] }), h('p', { class: 'muted small' }, 'Preview blank? ', h('a', { href: dl(a.id, t[2]) + '?inline=1', target: '_blank', rel: 'noopener' }, 'Open the PDF in a new tab'), '.'));
  };
  const tabBtns = h('div', { class: 'panel-tabs' });
  const all = isJob ? tabs : [['doc', 'E-mail', null], ...tabs];
  all.forEach(([k, label]) => tabBtns.append(h('button', { class: S.appTab === k ? 'active' : '', onclick: () => { S.appTab = k; renderApp(); } }, label)));
  draw();
  return h('div', { class: 'card sticky' }, tabBtns, body);
}

// ============================================================ boot
async function loadBlueprints() { try { S.blueprints = await api('/api/blueprints'); renderBlueprintSummary(); } catch (e) { /* ignore */ } }
async function boot() {
  $('#bookmarklet').href = bookmarkletHref();
  $('#bookmarklet').addEventListener('click', e => { e.preventDefault(); toast('Drag this button to your bookmarks bar instead of clicking it'); });
  const savedLang = LS.get('lang', 'auto'); const r = $(`input[name=lang][value=${savedLang}]`); if (r) r.checked = true;
  $('#with-cv').checked = LS.get('with_cv', true);
  await refreshStatus();
  try { S.domains = await api('/api/domains'); renderDomainChips(); } catch (e) { /* ignore */ }
  await loadBlueprints();
  await handleIngestHash();
  loadTracker().catch(() => {});
  setInterval(() => { if (!document.hidden) refreshStatus(); }, 15000);
}
boot();
