const API = '/dashboard/v1';
const LIDARR_WEB_BASE = window.LIDARR_WEB_BASE;
const REFRESH_MS = 30000;
let apikey = localStorage.getItem('tidalhires_apikey') || '';
let refreshTimer = null;
let lastUpdate = null;
let activeDrawerJid = null;
let lastConnectors = [];

const $ = (id) => document.getElementById(id);

// HTMX-driven surfaces (Phase 2 slice 3+) authenticate with the stored key.
document.body.addEventListener('htmx:configRequest', (evt) => {
  if (apikey) evt.detail.headers['X-Api-Key'] = apikey;
});

// Auth-carrying downloads: a plain link can't set X-Api-Key, so append it on click.
document.body.addEventListener('click', (evt) => {
  const a = evt.target.closest('a.audit-download');
  if (!a) return;
  evt.preventDefault();
  const base = a.dataset.href;
  const url = base + (base.includes('?') ? '&' : '?') + 'apikey=' + encodeURIComponent(apikey);
  window.open(url, '_blank');
});

if (!apikey) showAuthPrompt();
else init();

function showAuthPrompt() {
  $('auth-prompt').style.display = 'flex';
}
function saveKey() {
  const v = $('auth-key-input').value.trim();
  if (!v) return;
  apikey = v;
  localStorage.setItem('tidalhires_apikey', v);
  $('auth-prompt').style.display = 'none';
  init();
}

async function api(path, opts={}) {
  const url = path + (path.includes('?') ? '&' : '?') + 'apikey=' + encodeURIComponent(apikey);
  const resp = await fetch(API + url, opts);
  if (resp.status === 401) {
    localStorage.removeItem('tidalhires_apikey');
    apikey = '';
    showAuthPrompt();
    throw new Error('auth');
  }
  return resp;
}

function init() {
  refresh();
  refreshTimer = setInterval(refresh, REFRESH_MS);
  setInterval(updateRefreshIndicator, 1000);
  $('filter-status').addEventListener('change', refresh);
  $('filter-decision').addEventListener('change', refresh);
  $('records-search').addEventListener('input', applyRecordsSearch);
  // Topbar search: jump to Review (via Alpine nav event) and filter records.
  $('topbar-search').addEventListener('input', () => {
    window.dispatchEvent(new CustomEvent('goto-section', { detail: 'review' }));
    $('records-search').value = $('topbar-search').value;
    applyRecordsSearch();
  });
  // Restore filters from localStorage
  $('filter-status').value = localStorage.getItem('tidalhires_filter_status') || '';
  $('filter-decision').value = localStorage.getItem('tidalhires_filter_decision') || '';
}

function clearFilters() {
  $('filter-status').value = '';
  $('filter-decision').value = '';
  $('records-search').value = '';
  $('topbar-search').value = '';
  localStorage.removeItem('tidalhires_filter_status');
  localStorage.removeItem('tidalhires_filter_decision');
  refresh();
}

// Client-side filter over the already-loaded records rows. Server-side search
// across all records is future work (needs a search endpoint).
function applyRecordsSearch() {
  const q = ($('records-search').value || '').trim().toLowerCase();
  const rows = $('records-body').querySelectorAll('tr');
  rows.forEach(row => {
    if (row.querySelector('td[colspan]')) return; // "no records" placeholder
    row.style.display = (!q || row.textContent.toLowerCase().includes(q)) ? '' : 'none';
  });
}

async function refresh() {
  try {
    const [sumResp, recResp, timingResp, jobsResp, connectorResp] = await Promise.all([
      api('/summary'),
      api('/records?' + buildFilterParams()),
      api('/timings?window=7d'),
      api('/jobs?state=queued,running,cancelling&limit=20'),
      api('/connectors')
    ]);
    const sum = await sumResp.json();
    const rec = await recResp.json();
    const timings = await timingResp.json();
    const jobs = await jobsResp.json();
    const connectors = await connectorResp.json();
    lastConnectors = connectors.connectors || [];
    renderSummary(sum, connectors.connectors || []);
    renderActiveJobs(jobs.jobs || []);
    renderTimings(timings);
    renderRecords(rec.records);
    renderIntegrations(connectors.connectors || []);
    lastUpdate = Date.now();
    updateRefreshIndicator();
    // Save filter state
    localStorage.setItem('tidalhires_filter_status', $('filter-status').value);
    localStorage.setItem('tidalhires_filter_decision', $('filter-decision').value);
  } catch (e) {
    if (e.message !== 'auth') {
      showToast('Refresh failed: ' + e.message, 'error');
    }
  }
}

function renderIntegrations(connectors) {
  if (!connectors.length) {
    $('integrations-body').innerHTML = '<span class="muted">No connectors registered.</span>';
    return;
  }
  const groups = [
    ['source', 'Sources'],
    ['verifier', 'Verifiers'],
    ['output', 'Outputs'],
  ];
  $('integrations-body').innerHTML = groups.map(([kind, label]) => {
    const items = connectors.filter(c => c.kind === kind);
    if (!items.length) return '';
    return `
      <section class="integration-section">
        <h2>${label}</h2>
        <div class="connector-grid">
          ${items.map(renderConnectorCard).join('')}
        </div>
      </section>
    `;
  }).join('');
}

function statusBadge(status) {
  const map = {
    ok: ['success', 'OK'],
    degraded: ['warning', 'Degraded'],
    blocked: ['danger', 'Blocked'],
    missing: ['danger', 'Missing'],
    disabled: ['neutral', 'Disabled'],
  };
  return map[status] || ['neutral', status || 'Unknown'];
}

function yesNo(value) {
  return value ? 'yes' : 'no';
}

function tagList(items) {
  if (!items || !items.length) return 'none';
  return `<span class="connector-tags">${items.map(item => `<span class="connector-tag">${esc(item)}</span>`).join('')}</span>`;
}

function renderInstallGuidance(connector) {
  const guidance = connector.install_guidance || {};
  if (!guidance.show) return '';
  const actions = guidance.actions || [];
  const actionList = actions.length
    ? `<ul>${actions.map(item => `<li>${esc(item)}</li>`).join('')}</ul>`
    : '';
  return `
    <div class="connector-guidance">
      <strong>Install guidance</strong>
      <div>${esc(guidance.reason || 'Connector needs attention.')}</div>
      ${actionList}
    </div>
  `;
}

function renderConnectorCard(connector) {
  const manifest = connector.manifest || {};
  const runtime = connector.runtime || {};
  const [badgeClass, badgeText] = statusBadge(runtime.health);
  const stateClass = runtime.health || 'unknown';
  const requiredClass = manifest.required ? 'required' : '';
  const version = runtime.version || 'unknown';
  const minVersion = manifest.min_supported_version || 'none';
  const service = manifest.docker_service || 'none';
  const profile = manifest.install_profile || 'none';
  const docsUrl = manifest.docs_url || '';
  const docs = docsUrl ? `<a class="connector-link" href="${esc(docsUrl)}" target="_blank" rel="noreferrer">docs</a>` : 'none';
  const err = runtime.last_error ? `<div class="k">Last error</div><div class="v">${esc(runtime.last_error)}</div>` : '';
  const mode = runtime.mode || 'disabled';
  const modeOptions = ['disabled', 'dry_run', 'import'].map(item => {
    const selected = item === mode ? ' selected' : '';
    return `<option value="${item}"${selected}>${item}</option>`;
  }).join('');
  return `
    <article class="connector-card ${requiredClass} ${esc(stateClass)}">
      <div class="connector-head">
        <div>
          <div class="connector-title">${esc(connector.display_name || manifest.display_name || connector.id)}</div>
          <div class="connector-id">${esc(connector.id || manifest.id || '')}</div>
        </div>
        <span class="badge ${badgeClass}">${esc(badgeText)}</span>
      </div>
      <div class="connector-meta">
        <div class="k">Runtime</div><div class="v">${esc(runtime.mode || 'unknown')} · installed ${yesNo(runtime.installed)} · enabled ${yesNo(runtime.enabled)}</div>
        <div class="k">Required</div><div class="v">${yesNo(manifest.required)}</div>
        <div class="k">Version</div><div class="v">${esc(version)} · min ${esc(minVersion)}</div>
        <div class="k">Service</div><div class="v">${esc(service)} · profile ${esc(profile)}</div>
        <div class="k">Required env</div><div class="v">${tagList(manifest.required_env)}</div>
        <div class="k">Optional env</div><div class="v">${tagList(manifest.optional_env)}</div>
        <div class="k">Capabilities</div><div class="v">${tagList(manifest.capabilities)}</div>
        <div class="k">Last check</div><div class="v">${esc(runtime.last_checked_at || 'unknown')} · ${docs}</div>
        ${err}
      </div>
      ${renderInstallGuidance(connector)}
      <div class="connector-controls">
        <select id="connector-mode-${esc(connector.id)}" aria-label="Mode for ${esc(connector.id)}">${modeOptions}</select>
        <button onclick="saveConnectorConfig('${esc(connector.id)}', true)">Dry run</button>
        <button class="primary" onclick="saveConnectorConfig('${esc(connector.id)}', false)">Apply</button>
      </div>
    </article>
  `;
}

async function saveConnectorConfig(connectorId, dryRun) {
  const select = $('connector-mode-' + connectorId);
  if (!select) return;
  try {
    const resp = await api('/connectors/' + encodeURIComponent(connectorId) + '/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({mode: select.value, dry_run: dryRun})
    });
    const data = await resp.json();
    if (!resp.ok || data.valid === false) {
      showToast((data.errors || ['Connector config rejected']).join('; '), 'error');
      return;
    }
    showToast(dryRun ? 'Connector config is valid.' : 'Connector config saved.', 'success');
    if (!dryRun) refresh();
  } catch (e) {
    if (e.message !== 'auth') showToast('Connector config failed: ' + e.message, 'error');
  }
}

function renderActiveJobs(jobs) {
  $('active-jobs-panel').style.display = jobs.length ? '' : 'none';
  if (!jobs.length) {
    $('active-jobs-body').innerHTML = '<span class="muted">No active worker jobs.</span>';
    return;
  }
  $('active-jobs-body').innerHTML = `
    <table class="file-evidence">
      <thead><tr><th>Job</th><th>Type</th><th>State</th><th>Progress</th><th>Attempts</th><th>JID</th><th></th></tr></thead>
      <tbody>${jobs.map(j => {
        const p = j.progress || {};
        const pct = Math.max(0, Math.min(100, Number(p.percent ?? 0)));
        const stage = p.stage || j.state || '';
        const message = p.message || '';
        return `
        <tr>
          <td>${j.id}</td>
          <td>${esc(j.type || '')}</td>
          <td>${esc(j.state || '')}</td>
          <td class="progress-cell">
            <div class="progress-meta"><span class="progress-stage">${esc(stage)}</span><span class="progress-percent">${pct}%</span></div>
            <div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div>
            <div class="progress-message">${esc(message)}</div>
          </td>
          <td>${j.attempts ?? 0}/${j.max_attempts ?? 0}</td>
          <td class="jid">${esc((j.jid || '').slice(0, 12))}</td>
          <td><button onclick="cancelJob(${j.id})" ${j.state === 'cancelling' ? 'disabled' : ''}>${j.state === 'cancelling' ? 'Cancelling' : 'Cancel'}</button></td>
        </tr>
      `}).join('')}</tbody>
    </table>
  `;
}

function buildFilterParams() {
  const params = [];
  const s = $('filter-status').value;
  const d = $('filter-decision').value;
  if (s) params.push('status=' + encodeURIComponent(s));
  if (d) params.push('decision=' + encodeURIComponent(d));
  return params.join('&');
}

function connectorHealth(connectors, connectorId, fallback) {
  const c = (connectors || []).find(item => item.id === connectorId);
  return (c && c.runtime && c.runtime.health) || fallback || 'unknown';
}

function renderSummary(s, connectors=[]) {
  const c = s.counts;
  const commands = s.queue.lidarr_commands || {active_count: 0, blocking_count: 0, commands: []};
  const flacHealth = connectorHealth(connectors, 'flac_detective', s.stack_health.flac_detective);
  const lidarrHealth = connectorHealth(connectors, 'lidarr_manual_import', s.stack_health.lidarr);
  const cards = [
    {label: 'Total', val: c.total_decisions, cls: ''},
    {label: 'Imported', val: c.imported, cls: c.imported > 0 ? 'ok' : ''},
    {label: 'Needs review', val: c.needs_review, cls: c.needs_review > 0 ? 'warn' : ''},
    {label: 'Pending import', val: c.pending, cls: c.pending > 0 ? 'attention' : ''},
    {label: 'Policy alerts', val: c.policy_violations || 0, cls: c.policy_violations > 0 ? 'warn' : ''},
    {label: 'Failed', val: c.failed, cls: c.failed > 0 ? 'warn' : ''},
    {label: 'Active jobs', val: c.active_jobs, cls: ''},
    {label: 'Lidarr queue', val: s.queue.lidarr_queue_total ?? '?', cls: ''},
    {label: 'Lidarr commands', val: commands.active_count ?? '?', cls: (commands.blocking_count || 0) > 0 ? 'attention' : ''},
  ];
  $('summary-grid').innerHTML = cards.map(c =>
    `<div class="card ${c.cls}"><div class="label">${c.label}</div><div class="val">${c.val}</div></div>`
  ).join('');
  $('meta').textContent = `${c.total_decisions} records · Stack: tidalhires=${s.stack_health.tidalhires}, flac-detective=${flacHealth}, lidarr=${lidarrHealth}`;
  renderLidarrCommands(commands);
}

function fmtAge(sec) {
  if (sec === null || sec === undefined) return 'unknown';
  if (sec >= 3600) return (sec / 3600).toFixed(1) + 'h';
  if (sec >= 60) return Math.floor(sec / 60) + 'm';
  return Math.max(0, Math.floor(sec)) + 's';
}

function renderLidarrCommands(commands) {
  const blocking = (commands.commands || []).filter(c => c.blocking);
  $('lidarr-command-panel').style.display = blocking.length ? '' : 'none';
  if (!blocking.length) {
    $('lidarr-command-body').innerHTML = '<span class="muted">No blocking Lidarr commands.</span>';
    return;
  }
  $('lidarr-command-body').innerHTML = `
    <div class="command-list">
      ${blocking.map(c => `
        <div class="command-item">
          <span class="name">${esc(c.name || '')} #${esc(c.id ?? '')}</span>
          <span>${esc(c.status || '')}</span>
          <span class="age">${fmtAge(c.age_sec)}</span>
          <span class="reason">${esc(c.blocking_reason || c.message || 'Blocking Lidarr queue')}</span>
        </div>
      `).join('')}
    </div>
  `;
}

function fmtSec(v) {
  if (!v && v !== 0) return '—';
  if (v >= 60) return (v / 60).toFixed(1) + 'm';
  return Number(v).toFixed(1) + 's';
}

function renderTimings(t) {
  const stages = t.stages || {};
  const entries = Object.entries(stages);
  if (!entries.length || !t.sample_count) {
    $('timings-body').innerHTML = '<span class="muted">No timing data yet; new jobs will populate this.</span>';
    return;
  }
  $('timings-body').innerHTML = `
    <div class="meta">${t.sample_count} timed jobs in ${esc(t.window)}${t.regression_flag ? ' · regression flag' : ''}</div>
    <div class="timing-grid">
      ${entries.map(([stage, stats]) => `
        <div class="timing-item">
          <span class="stage">${esc(stage)}</span>
          <span class="numbers">med ${fmtSec(stats.median)} · p95 ${fmtSec(stats.p95)}</span>
        </div>
      `).join('')}
    </div>
  `;
}

const STATUS_BADGES = {
  needs_review: ['warning', '⚠ Needs review'],
  imported: ['success', '✓ Imported'],
  promoted: ['info', '★ Promoted'],
  failed: ['danger', '✗ Failed'],
  pending: ['warning', '… Pending'],
  policy_violation: ['danger', '! Policy alert'],
  blocked: ['neutral', '⊘ Blocked'],
  discarded: ['neutral', '× Discarded'],
  expired: ['neutral', '× Expired'],
};

function renderRecords(recs) {
  if (!recs.length) {
    $('records-body').innerHTML = '<tr><td colspan="9" class="muted">No records match these filters</td></tr>';
    return;
  }
  $('records-body').innerHTML = recs.map(r => {
    const [cls, txt] = STATUS_BADGES[r.derived_status] || ['neutral', r.derived_status];
    const albumId = r.album_ids[0];
    const deeplink = albumId
      ? `<a class="deeplink" href="${LIDARR_WEB_BASE}/album/${albumId}" target="_blank" onclick="event.stopPropagation()" title="Open in Lidarr">↗</a>`
      : '';
    const when = r.ts_iso ? r.ts_iso.slice(11,19) : '';
    return `<tr onclick="openDrawer('${r.jid}')">
      <td><span class="badge ${cls}" title="${esc(r.status_reason || '')}">${txt}</span></td>
      <td class="reason" title="${esc(r.status_reason || '')}">${esc(r.status_reason || '')}</td>
      <td class="title">${esc(r.title || '')}</td>
      <td>${esc(r.verification_decision || '')}</td>
      <td>${esc(r.import_outcome || '')}</td>
      <td>${esc(r.lifecycle_state || '')}</td>
      <td>${r.score ?? ''}</td>
      <td class="jid">${(r.jid || '').slice(0,8)} · ${when}</td>
      <td>${deeplink}</td>
    </tr>`;
  }).join('');
  applyRecordsSearch();
}

async function openDrawer(jid) {
  activeDrawerJid = jid;
  $('drawer').classList.add('open');
  $('drawer-overlay').classList.add('open');
  $('drawer-body').innerHTML = '<p class="muted">Loading…</p>';
  $('drawer-actions').innerHTML = '';
  try {
    const resp = await api('/record/' + jid);
    if (!resp.ok) throw new Error('not found');
    const d = await resp.json();
    if (activeDrawerJid !== jid) return;
    renderDrawer(d);
  } catch (e) {
    if (activeDrawerJid !== jid) return;
    $('drawer-body').innerHTML = '<p class="muted">Failed to load record</p>';
  }
}
function closeDrawer() {
  activeDrawerJid = null;
  $('drawer').classList.remove('open');
  $('drawer-overlay').classList.remove('open');
}

function identityBadgeClass(decision) {
  const map = {
    WRONG_ALBUM: 'danger',
    AMBIGUOUS_EDITION: 'warning',
    INSUFFICIENT_EVIDENCE: 'warning',
    SAME_FAMILY: 'info',
    SAME_RELEASE: 'success',
  };
  return map[decision] || 'neutral';
}

function fmtIdentityValue(value) {
  if (value === null || value === undefined || value === '') return '—';
  return String(value);
}

function fmtIdentityNumber(value, suffix='') {
  if (value === null || value === undefined || value === '') return '—';
  const n = Number(value);
  if (!Number.isFinite(n)) return esc(String(value));
  return n.toFixed(n % 1 === 0 ? 0 : 1) + suffix;
}

function inlineList(items, fallback='—') {
  if (!items || !items.length) return fallback;
  return items.map(item => esc(String(item))).join(', ');
}

function renderReleaseSwitchEvents(events) {
  if (!events || !events.length) return '';
  const items = events.map(e => {
    const reasons = (e.reasons || []).length
      ? `<ul class="switch-reasons">${e.reasons.map(r => `<li>${esc(String(r))}</li>`).join('')}</ul>`
      : '';
    const result = e.result && e.result !== e.event ? ' · ' + esc(String(e.result)) : '';
    return `<li>
      <span class="switch-event">${esc(String(e.event || ''))}${result}</span>
      <span class="switch-meta muted">${esc(String(e.mode || ''))} · ${esc(String(e.old_release_id ?? '—'))} → ${esc(String(e.new_release_id ?? '—'))} · ${esc(String(e.when || ''))}</span>
      ${reasons}
    </li>`;
  }).join('');
  return `
    <section>
      <h3>Release switch audit</h3>
      <ul class="switch-event-list">${items}</ul>
    </section>
  `;
}

function renderReleaseIdentity(identity) {
  if (!identity) return '';
  const observed = identity.observed || {};
  const reasons = (identity.reasons || []).length
    ? `<ul class="identity-reasons">${identity.reasons.map(r => `<li>${esc(r)}</li>`).join('')}</ul>`
    : '<p class="muted">No release-identity reasons recorded.</p>';
  const lidarrRejections = (identity.lidarr_rejections || []).length
    ? `<div class="kvrow"><span class="k">Lidarr rejection</span><span class="v">${inlineList(identity.lidarr_rejections)}</span></div>`
    : '';
  return `
    <section>
      <h2>Release identity</h2>
      <div class="kvrow"><span class="k">Decision</span><span class="v"><span class="badge ${identityBadgeClass(identity.decision)}">${esc(identity.decision || 'UNKNOWN')}</span></span></div>
      <div class="kvrow"><span class="k">Confidence</span><span class="v">${fmtIdentityNumber(identity.confidence, '%')}</span></div>
      <div class="kvrow"><span class="k">Best release</span><span class="v">${esc(fmtIdentityValue(identity.best_release_id))}</span></div>
      <div class="kvrow"><span class="k">Current release</span><span class="v">${esc(fmtIdentityValue(identity.current_release_id))}</span></div>
      <div class="kvrow"><span class="k">Identity score</span><span class="v">${fmtIdentityNumber(identity.score, '%')}</span></div>
      <div class="kvrow"><span class="k">Track delta</span><span class="v">${esc(fmtIdentityValue(identity.track_count_delta))}</span></div>
      <div class="kvrow"><span class="k">Title similarity</span><span class="v">${identity.title_similarity === null || identity.title_similarity === undefined ? '—' : fmtIdentityNumber(Number(identity.title_similarity) * 100, '%')}</span></div>
      ${lidarrRejections}
      <h3>Reasons</h3>
      ${reasons}
      <h3>Observed metadata</h3>
      <div class="kvrow"><span class="k">Files</span><span class="v">${esc(fmtIdentityValue(observed.file_count))}</span></div>
      <div class="kvrow"><span class="k">Track titles</span><span class="v">${inlineList(observed.track_titles)}</span></div>
      <div class="kvrow"><span class="k">Artists</span><span class="v">${inlineList(observed.artist_names)}</span></div>
      <div class="kvrow"><span class="k">Albums</span><span class="v">${inlineList(observed.album_titles)}</span></div>
      <div class="kvrow"><span class="k">Artist MBIDs</span><span class="v">${inlineList(observed.artist_mbids)}</span></div>
      <div class="kvrow"><span class="k">Release-group MBIDs</span><span class="v">${inlineList(observed.release_group_mbids)}</span></div>
      <div class="kvrow"><span class="k">Release MBIDs</span><span class="v">${inlineList(observed.release_mbids)}</span></div>
      <div class="media-note">${esc(identity.sensor_summary || 'Release identity is evaluated separately from the audio score.')}</div>
    </section>
  `;
}

function renderDrawer(d) {
  $('drawer-title').textContent = d.context.title || d.jid;
  $('drawer-jid').textContent = 'JID: ' + d.jid;
  const v = d.verification;
  const comps = v.components || {};
  const compsRows = Object.entries(comps).map(([k,val]) => `<div class="kvrow"><span class="k">${k}</span><span class="v">${val}</span></div>`).join('');
  const timings = d.timings || {};
  const timingRows = Object.entries(timings)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([k,val]) => `<div class="kvrow"><span class="k">${esc(k)}</span><span class="v">${Number(val).toFixed(1)}s</span></div>`).join('');
  const overrides = (v.overrides || []).length ? v.overrides.join(', ') : '—';
  const reviewBox = v.review_reason ? `<section><h2 style="color:var(--status-warning)">Why this needs review</h2><p>${esc(v.review_reason)}</p></section>` : '';
  const sensorRows = (d.sensors || []).map(s => {
    const badgeClass = s.status === 'pass' ? 'success' : (s.status === 'fail' ? 'danger' : (s.status === 'warn' ? 'warning' : 'neutral'));
    const runtime = s.duration_ms || s.duration_ms === 0 ? fmtSec(Number(s.duration_ms) / 1000) : '—';
    return `<div class="kvrow"><span class="k"><span class="badge ${badgeClass}">${esc(s.status || '')}</span> ${esc(s.name || '')}</span><span class="v">${esc(runtime)} · ${esc(s.summary || '')}</span></div>`;
  }).join('');
  const fileRows = (d.files || []).slice(0, 40).map(f => {
    const sr = f.sample_rate ? `${f.sample_rate} Hz` : '—';
    const bits = f.bit_depth ? `${f.bit_depth} bit` : '—';
    const cutoff = f.cutoff_hz ? `${f.cutoff_hz} Hz` : '—';
    const nyquist = f.nyquist_hz ? `${f.nyquist_hz} Hz` : '—';
    const verdict = f.detective_verdict || (f.error ? 'ERROR' : '—');
    return `<tr><td title="${esc(f.filename || '')}">${esc(f.filename || '')}</td><td>${esc(sr)}</td><td>${esc(bits)}</td><td>${esc(cutoff)}</td><td>${esc(nyquist)}</td><td>${esc(verdict)}</td></tr>`;
  }).join('');
  const media = d.media || {available: false, reason: 'Audio review unavailable for this record.'};
  const mediaSection = media.available ? `
    <section>
      <h2>Audio review</h2>
      <audio controls preload="none" style="width:100%" src="${API}/audio-sample/${encodeURIComponent(d.jid)}?apikey=${encodeURIComponent(apikey)}"></audio>
      <div class="media-note">20s sample from the first retained audio file. Use this only as manual review evidence, not as the policy decision.</div>
      <img class="spectrum" alt="Spectrum preview with time and frequency legend" title="Click to enlarge spectrum" loading="lazy" data-title="${esc(d.context.title || d.jid)}" src="${API}/spectrum/${encodeURIComponent(d.jid)}?apikey=${encodeURIComponent(apikey)}" onclick="openImageOverlay(this.src, this.dataset.title)" onerror="this.style.display='none'; this.nextElementSibling.textContent='Spectrum unavailable for this record.'">
      <div class="media-note">Click to enlarge. X axis is time; Y axis is frequency. A hard shelf near the top can indicate upsampled hi-res.</div>
    </section>
  ` : (media.review_relevant ? `
    <section>
      <h2>Audio review</h2>
      <div class="media-unavailable">${esc(media.reason || 'Audio review unavailable for this record.')}</div>
    </section>
  ` : '');

  $('drawer-body').innerHTML = `
    ${reviewBox}
    <section>
      <h2>Status explanation</h2>
      <p>${esc(d.status_reason || 'No status explanation available.')}</p>
    </section>
    <section>
      <h2>Decision</h2>
      <div class="kvrow"><span class="k">Decision</span><span class="v">${esc(v.decision || '')}</span></div>
      <div class="kvrow"><span class="k">Outcome</span><span class="v">${esc(v.outcome || '')}</span></div>
      <div class="kvrow"><span class="k">Verdict</span><span class="v">${esc(v.verdict || '')}</span></div>
      <div class="kvrow"><span class="k">Score</span><span class="v">${v.score ?? '—'}</span></div>
      <div class="kvrow"><span class="k">Overrides</span><span class="v">${esc(overrides)}</span></div>
    </section>
    ${renderReleaseIdentity(d.release_identity)}
    ${renderReleaseSwitchEvents(d.release_switch_events)}
    <section>
      <h2>Score components</h2>
      ${compsRows || '<p class="muted">No component data</p>'}
    </section>
    <section>
      <h2>Sensor evidence</h2>
      ${sensorRows || '<p class="muted">No structured sensor evidence yet; older records predate sensors[].</p>'}
    </section>
    <section>
      <h2>File evidence</h2>
      ${fileRows ? `<table class="file-evidence"><thead><tr><th>File</th><th>SR</th><th>Bits</th><th>Cutoff</th><th>Nyquist</th><th>Verdict</th></tr></thead><tbody>${fileRows}</tbody></table>` : '<p class="muted">No per-file evidence yet.</p>'}
    </section>
    <section>
      <h2>Timings</h2>
      ${timingRows || '<p class="muted">No timing data yet; new jobs will record stage timings.</p>'}
    </section>
    ${mediaSection}
    <section>
      <h2>Existing in library</h2>
      <div class="kvrow"><span class="k">Current quality</span><span class="v">${esc(d.context.existing.label)}</span></div>
      <div class="kvrow"><span class="k">Current kbps</span><span class="v">${d.context.existing.kbps ?? '—'}</span></div>
    </section>
    <section>
      <h2>Lidarr context</h2>
      <div id="lidarr-context" class="muted">Loading Lidarr context…</div>
    </section>
    <section>
      <h2>Lifecycle</h2>
      <div class="kvrow"><span class="k">State</span><span class="v">${esc(d.lifecycle.state || '')}</span></div>
      <div class="kvrow"><span class="k">Actor</span><span class="v">${esc(d.lifecycle.actor || '—')}</span></div>
    </section>
  `;

  const actionDefs = {
    promote: {label: 'Promote', cls: 'btn-promote', desc: 'Will import these files manually even though verification flagged them. Risk is accepted.'},
    discard: {label: 'Discard', cls: 'btn-discard', desc: 'Deletes /output/' + d.jid + '/ and blocklists this grab in Lidarr.'},
    retry_import: {label: 'Retry import', cls: 'btn-retry', desc: 'Re-trigger Lidarr ManualImport. Verification is unchanged.'},
  };
  $('drawer-actions').innerHTML = d.available_actions.length
    ? d.available_actions.map(a => `<button class="${actionDefs[a].cls}" onclick="confirmAction('${d.jid}','${a}','${actionDefs[a].label}','${esc(actionDefs[a].desc)}')">${actionDefs[a].label}</button>`).join('')
    : '<div class="muted">No actions available for this state</div>';
  setTimeout(() => loadLidarrContext(d.jid), 0);
}

async function loadLidarrContext(jid) {
  const target = $('lidarr-context');
  if (!target || activeDrawerJid !== jid) return;
  try {
    const resp = await api('/lidarr-context/' + encodeURIComponent(jid));
    if (activeDrawerJid !== jid) return;
    if (!resp.ok) {
      target.innerHTML = '<span class="muted">Lidarr context unavailable</span>';
      return;
    }
    renderLidarrContext(await resp.json());
  } catch (e) {
    if (activeDrawerJid !== jid) return;
    target.innerHTML = '<span class="muted">Lidarr context unavailable</span>';
  }
}

function renderLidarrContext(ctx) {
  const target = $('lidarr-context');
  const album = ctx.album;
  const albumHtml = album
    ? `<div class="kvrow"><span class="k">Album</span><span class="v"><a href="${esc(album.url)}" target="_blank">${esc(album.artist_name || '')} — ${esc(album.title || '')}</a></span></div>
       <div class="kvrow"><span class="k">Tracks</span><span class="v">${album.track_file_count ?? '—'} / ${album.track_count ?? '—'} files</span></div>
       <div class="kvrow"><span class="k">Release</span><span class="v">${esc(album.release_title || '—')}</span></div>`
    : '<p class="muted">No Lidarr album mapping found.</p>';
  const queue = ctx.queue || {};
  const queueHtml = (queue.queue_entries || []).length
    ? queue.queue_entries.map(q => `<div class="kvrow"><span class="k">${esc(q.status || 'queue')}</span><span class="v">${esc(q.title || q.download_id || '')}</span></div>`).join('')
    : '<p class="muted">No matching Lidarr queue entries.</p>';
  const hist = ctx.grab_history || [];
  const histHtml = hist.length
    ? hist.map(h => `<div class="kvrow"><span class="k">${esc(h.event_type || '')}</span><span class="v">${esc((h.indexer || 'unknown') + ' · ' + (h.ts || '') + (h.reason ? ' · ' + h.reason : ''))}</span></div>`).join('')
    : '<p class="muted">No matching Lidarr history entries.</p>';
  target.innerHTML = `
    ${albumHtml}
    <h3>Queue</h3>
    ${queueHtml}
    <h3>Recent history</h3>
    ${histHtml}
  `;
}

function confirmAction(jid, action, label, desc) {
  $('modal-title').textContent = label + '?';
  $('modal-desc').textContent = desc;
  const btn = $('modal-confirm');
  btn.textContent = label;
  btn.className = 'confirm-' + (action === 'retry_import' ? 'retry' : action);
  btn.onclick = () => doAction(jid, action);
  $('modal-overlay').classList.add('open');
}
function closeModal() { $('modal-overlay').classList.remove('open'); }

function openImageOverlay(src, title) {
  $('image-title').textContent = title || 'Spectrum preview';
  $('image-full').src = src;
  $('image-overlay').classList.add('open');
}
function closeImageOverlay() {
  $('image-overlay').classList.remove('open');
  $('image-full').removeAttribute('src');
}

async function doAction(jid, action) {
  closeModal();
  showToast('Processing ' + action + '…');
  try {
    const resp = await fetch(API + '/action/' + jid + '?apikey=' + encodeURIComponent(apikey), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: action})
    });
    if (resp.status === 409) {
      const body = await resp.json();
      showToast('Action not allowed: ' + (body.allowed || []).join(', '), 'error');
      return;
    }
    if (!resp.ok) {
      showToast('Action failed: HTTP ' + resp.status, 'error');
      return;
    }
    showToast(action + ' OK', 'success');
    closeDrawer();
    refresh();
  } catch (e) {
    showToast('Action failed: ' + e.message, 'error');
  }
}

async function cancelJob(jobId) {
  showToast('Cancelling job ' + jobId + '…');
  try {
    const resp = await fetch(API + '/jobs/' + jobId + '/cancel?apikey=' + encodeURIComponent(apikey), {
      method: 'POST'
    });
    if (resp.status === 409) {
      const body = await resp.json().catch(() => ({}));
      showToast(body.error || 'Job already finished', 'error');
      refresh();
      return;
    }
    if (!resp.ok) {
      showToast('Cancel failed: HTTP ' + resp.status, 'error');
      return;
    }
    const body = await resp.json();
    showToast(body.state === 'cancelling' ? 'Cancellation requested' : 'Job cancelled', 'success');
    refresh();
  } catch (e) {
    showToast('Cancel failed: ' + e.message, 'error');
  }
}

function showToast(msg, kind='') {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'show ' + kind;
  setTimeout(() => t.classList.remove('show'), 3500);
}

function updateRefreshIndicator() {
  if (!lastUpdate) {
    $('refresh-status').textContent = 'Updated: never';
    return;
  }
  const sec = Math.floor((Date.now() - lastUpdate) / 1000);
  $('refresh-status').textContent = `Updated ${sec}s ago`;
  $('refresh-dot').className = 'refresh-dot' + (sec > 45 ? ' stale' : '');
}

function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    closeImageOverlay();
    closeModal();
    closeDrawer();
  }
});
