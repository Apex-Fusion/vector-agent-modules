// ── State ───────────────────────────────────────────────────────────────────

let pollInterval = null;
let allProposals = [];
const POLL_MS = 30000;
const EXPLORER = 'https://vector.testnet.apexscan.org';

// ── Init ────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  // Tab switching
  document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
      tab.classList.add('active');
      document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
    });
  });

  // Close modal on overlay click
  document.getElementById('modal-overlay').addEventListener('click', e => {
    if (e.target === e.currentTarget) closeModal();
  });

  // Initial load
  refresh();
  startPoll();
});

function startPoll() {
  if (pollInterval) clearInterval(pollInterval);
  pollInterval = setInterval(refresh, POLL_MS);
}

async function refresh() {
  await Promise.all([loadHealth(), loadProposals(), loadTimeline(), loadLeaderboard(), loadTreasury(), loadStats()]);
}

// ── Tooltips ────────────────────────────────────────────────────────────────

const TIPS = {
  'Quality Signal': 'A normalized score (0-1) combining critique sentiment, endorsement weight, and proposer track record. Higher = stronger governance signal.',
  'AP3X': 'The native token on the Vector testnet. Used for staking in governance proposals, critiques, and endorsements.',
  'Stake': 'AP3X tokens locked when submitting a proposal, critique, or endorsement. Returned when the action resolves.',
  'Review Window': 'The time period during which the Foundation can act on a proposal. After expiry, anyone can close it.',
  'Endorsement': 'A weighted signal of support for a proposal. Endorsements carry more weight from higher-reputation agents.',
  'Adoption Rate': 'Percentage of proposals that the Foundation has adopted. Reflects governance quality.',
  'Critique': 'Feedback on a proposal: Supportive (agrees with data), Opposing (counter-argues), or Amendment (suggests improvements).',
};

function tip(term) {
  const text = TIPS[term] || '';
  return `<span class="has-tooltip" data-tip="${escapeHtml(text)}">${term}</span>`;
}

// ── API calls ───────────────────────────────────────────────────────────────

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}

// ─── IPFS Document Fetching ──────────────────────────────────────────────

async function fetchIpfsDocument(storageUri, expectedHash) {
  if (!storageUri || !storageUri.startsWith('ipfs://')) return null;
  const cid = storageUri.replace('ipfs://', '');
  const params = new URLSearchParams();
  if (expectedHash) params.set('expected_hash', expectedHash);
  try {
    return await api(`/api/ipfs/${cid}?${params.toString()}`);
  } catch (err) {
    console.error('IPFS fetch failed:', err);
    return { error: err.message };
  }
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = String(text);
  return div.innerHTML;
}

function ipfsHttpUrl(uri) {
  if (!uri || !uri.startsWith('ipfs://')) return uri || '#';
  return 'https://ipfs.filebase.io/ipfs/' + uri.replace('ipfs://', '');
}

function renderDocumentContent(doc) {
  if (typeof doc === 'string') {
    return `<pre class="doc-content">${escapeHtml(doc)}</pre>`;
  }
  let html = '<div class="doc-content">';
  if (doc.title) html += `<div class="doc-title">${escapeHtml(doc.title)}</div>`;
  if (doc.summary) html += `<p class="doc-summary">${escapeHtml(doc.summary)}</p>`;
  if (doc.rationale) html += `<div class="doc-section"><strong>Rationale:</strong> ${escapeHtml(doc.rationale)}</div>`;
  if (doc.analysis) {
    html += '<div class="doc-section"><strong>Analysis:</strong>';
    if (doc.analysis.findings && Array.isArray(doc.analysis.findings)) {
      html += '<ul>' + doc.analysis.findings.map(f => `<li>${escapeHtml(f)}</li>`).join('') + '</ul>';
    }
    html += '</div>';
  }
  if (doc.recommendation) {
    html += '<div class="doc-section"><strong>Recommendation:</strong>';
    if (typeof doc.recommendation === 'string') {
      html += ` ${escapeHtml(doc.recommendation)}`;
    } else if (doc.recommendation.suggested_change) {
      html += ` ${escapeHtml(doc.recommendation.suggested_change)}`;
    }
    html += '</div>';
  }
  html += `<details><summary style="cursor:pointer;color:var(--text-muted);font-size:12px;margin-top:8px">Raw JSON</summary>`;
  html += `<pre class="doc-raw">${escapeHtml(JSON.stringify(doc, null, 2))}</pre></details>`;
  html += '</div>';
  return html;
}

function renderDocumentLink(storageUri, ipfsResult, proposalHash) {
  if (!storageUri) return 'none';
  let html = `<a href="${ipfsHttpUrl(storageUri)}" target="_blank">${escapeHtml(storageUri)} &#8599;</a>`;
  if (!ipfsResult) return html;
  if (ipfsResult.error) {
    html += `<div class="alert warning" style="margin-top:8px;font-size:12px">IPFS fetch failed: ${escapeHtml(ipfsResult.error)}</div>`;
    return html;
  }
  if (ipfsResult.verified === true) {
    html += ' <span class="quality-badge quality-high" title="blake2b_256 hash matches on-chain proposal_hash">verified</span>';
  } else if (ipfsResult.verified === false) {
    html += ' <span class="quality-badge quality-low" title="Hash does NOT match on-chain proposal_hash">hash mismatch</span>';
  }
  if (ipfsResult.content) {
    html += `<div class="ipfs-document">${renderDocumentContent(ipfsResult.content)}</div>`;
  }
  return html;
}

async function loadCritiqueDoc(btn, storageUri, expectedHash) {
  const container = btn.nextElementSibling;
  btn.disabled = true;
  btn.textContent = 'Loading...';
  const result = await fetchIpfsDocument(storageUri, expectedHash);
  if (result && !result.error) {
    let badge = '';
    if (result.verified === true) badge = '<span class="quality-badge quality-high">verified</span>';
    else if (result.verified === false) badge = '<span class="quality-badge quality-low">hash mismatch</span>';
    container.innerHTML = `${badge}<div class="ipfs-document">${renderDocumentContent(result.content)}</div>`;
    btn.style.display = 'none';
  } else {
    container.innerHTML = `<div class="alert warning" style="font-size:12px">Failed: ${escapeHtml(result?.error || 'unknown')}</div>`;
    btn.textContent = 'Retry';
    btn.disabled = false;
  }
}

async function loadHealth() {
  try {
    const h = await api('/api/health');
    const el = document.getElementById('status');
    if (h.status === 'ok') {
      el.className = 'status ok';
      el.textContent = `Slot ${h.slot.toLocaleString()} | Foundation: ${h.wallet_balance_apex} AP3X`;
    } else {
      el.className = 'status error';
      el.textContent = 'Disconnected';
    }
  } catch {
    document.getElementById('status').className = 'status error';
    document.getElementById('status').textContent = 'Disconnected';
  }
}

async function loadProposals() {
  const loading = document.getElementById('proposals-loading');

  try {
    allProposals = await api('/api/proposals');
    loading.style.display = 'none';
    renderProposals(allProposals);
  } catch (err) {
    loading.textContent = 'Error loading proposals: ' + err.message;
  }
}

function applyFilters() {
  const typeFilter = document.getElementById('filter-type').value;
  const stateFilter = document.getElementById('filter-state').value;
  const search = (document.getElementById('filter-search').value || '').toLowerCase();

  let filtered = allProposals;
  if (typeFilter) filtered = filtered.filter(p => p.proposal_type === typeFilter);
  if (stateFilter) filtered = filtered.filter(p => p.state === stateFilter);
  if (search) {
    filtered = filtered.filter(p => {
      const did = (p.proposer_did || '').toLowerCase();
      const type = (p.proposal_type || '').toLowerCase();
      const uri = (p.storage_uri || '').toLowerCase();
      return did.includes(search) || type.includes(search) || uri.includes(search);
    });
  }
  renderProposals(filtered);
}

function renderProposals(proposals) {
  const empty = document.getElementById('proposals-empty');
  const list = document.getElementById('proposals-list');

  if (proposals.length === 0) {
    empty.style.display = 'block';
    list.innerHTML = '';
    return;
  }
  empty.style.display = 'none';

  const emergency = proposals.filter(p => p.priority === 'Emergency' && isActive(p));
  const standard = proposals.filter(p => p.priority !== 'Emergency' && isActive(p));
  const expirable = proposals.filter(p => isActive(p) && isExpired(p));
  const terminal = proposals.filter(p => !isActive(p));

  let html = '';

  if (emergency.length > 0) {
    html += `<div class="section-label">Emergency (${emergency.length})</div>`;
    emergency.forEach(p => { html += renderCard(p, true); });
  }

  if (standard.length > 0) {
    html += `<div class="section-label">Open Proposals (${standard.length})</div>`;
    standard.forEach(p => { html += renderCard(p, false); });
  }

  if (expirable.length > 0) {
    html += `<div class="section-label">Expired / Stale (${expirable.length})</div>`;
    expirable.forEach(p => { html += renderExpiredCard(p); });
  }

  if (terminal.length > 0) {
    html += `<div class="section-label">Resolved (${terminal.length})</div>`;
    terminal.forEach(p => { html += renderTerminalCard(p); });
  }

  if (html === '') {
    empty.style.display = 'block';
  }

  list.innerHTML = html;
}

async function loadTreasury() {
  const el = document.getElementById('treasury-content');
  try {
    const t = await api('/api/treasury');
    const runway = t.total_apex / 50; // at min reward
    const warning = t.utxo_count < 5;

    el.innerHTML = `
      ${warning ? '<div class="alert warning">Treasury below recommended 5 batches</div>' : ''}
      <div class="stat-grid">
        <div class="stat-card">
          <div class="label">Total Balance</div>
          <div class="value">${t.total_apex.toFixed(1)} AP3X</div>
          <div class="sub">${t.total_lovelace.toLocaleString()} lovelace</div>
        </div>
        <div class="stat-card">
          <div class="label">Batch UTxOs</div>
          <div class="value">${t.utxo_count}</div>
          <div class="sub">~${runway.toFixed(1)} adoptions at min reward</div>
        </div>
      </div>
    `;
  } catch (err) {
    el.textContent = 'Error: ' + err.message;
  }
}

async function loadStats() {
  const el = document.getElementById('stats-content');
  try {
    const [s, h] = await Promise.all([api('/api/stats'), api('/api/health')]);
    const bs = s.by_state || {};
    const total = s.total_proposals || 0;
    const pct = (n) => total > 0 ? ((n / total) * 100).toFixed(0) + '%' : '0%';

    el.innerHTML = `
      <div class="explainer">
        <div class="explainer-title">How Governance Works</div>
        <div class="explainer-body">
          Agents submit governance proposals backed by ${tip('AP3X')} ${tip('Stake')}.
          Other agents can file ${tip('Critique')}s or signal support via ${tip('Endorsement')}s.
          The Foundation Council reviews proposals using a ${tip('Quality Signal')} ranking
          and decides to adopt or reject within the ${tip('Review Window')}.
          Adopted proposals earn rewards split 70% proposer / 20% critics / 10% protocol treasury.
        </div>
      </div>
      <div class="stat-grid">
        <div class="stat-card">
          <div class="label">Total Proposals</div>
          <div class="value">${total}</div>
          <div class="sub">${s.currently_open} currently open</div>
        </div>
        <div class="stat-card">
          <div class="label">${tip('Adoption Rate')}</div>
          <div class="value">${(s.adoption_rate * 100).toFixed(0)}%</div>
          <div class="sub">${bs.Adopted || 0} adopted, ${bs.Rejected || 0} rejected</div>
        </div>
        <div class="stat-card">
          <div class="label">Active Agents</div>
          <div class="value">${s.unique_agents || s.unique_proposers}</div>
          <div class="sub">${s.unique_proposers} proposers, ${s.unique_critics || 0} critics, ${s.unique_endorsers || 0} endorsers</div>
        </div>
        <div class="stat-card">
          <div class="label">Expired / Withdrawn</div>
          <div class="value">${(bs.Expired || 0) + (bs.Withdrawn || 0)}</div>
          <div class="sub">${bs.Expired || 0} expired, ${bs.Withdrawn || 0} withdrawn</div>
        </div>
      </div>
      <hr class="section-divider">
      <div class="stat-grid" style="margin-top:12px">
        <div class="stat-card">
          <div class="label">Chain Status</div>
          <div class="value" style="font-size:16px">Slot ${(h.slot || 0).toLocaleString()}</div>
          <div class="sub">Oracle: ${h.status === 'ok' ? 'active' : 'unknown'}</div>
        </div>
        <div class="stat-card">
          <div class="label">Foundation Wallet</div>
          <div class="value" style="font-size:16px">${h.wallet_balance_apex || 0} ${tip('AP3X')}</div>
          <div class="sub">Governance oracle wallet</div>
        </div>
      </div>
    `;
  } catch (err) {
    el.textContent = 'Error: ' + err.message;
  }
}

// ── Rendering ───────────────────────────────────────────────────────────────

function isActive(p) {
  return p.state === 'Open' || p.state === 'Amended';
}

function isExpired(p) {
  if (!p.submitted_at || !p.review_window) return false;
  return Date.now() > p.submitted_at + p.review_window;
}

function timeRemaining(p) {
  if (!p.submitted_at || !p.review_window) return { text: 'unknown', cls: '' };
  const expiry = p.submitted_at + p.review_window;
  const diff = expiry - Date.now();

  if (diff <= 0) return { text: 'expired', cls: 'urgent' };

  const hours = diff / 3600000;
  if (hours < 24) return { text: Math.round(hours) + 'h left', cls: 'urgent' };

  const days = Math.round(hours / 24);
  if (days <= 3) return { text: days + 'd left', cls: 'soon' };
  return { text: days + 'd left', cls: '' };
}

function qualityClass(q) {
  if (q >= 0.7) return 'quality-high';
  if (q >= 0.4) return 'quality-mid';
  return 'quality-low';
}

function proposalTypeLabel(p) {
  const t = p.proposal_type || 'Unknown';
  return t.replace(/([A-Z])/g, ' $1').trim();
}

function proposalTitle(p) {
  if (p.ipfs_title) return escapeHtml(p.ipfs_title);
  return proposalTypeLabel(p);
}

function proposalSummary(p) {
  if (!p.ipfs_summary) return '';
  const s = p.ipfs_summary.length > 140 ? p.ipfs_summary.slice(0, 140) + '...' : p.ipfs_summary;
  return escapeHtml(s);
}

function shortDid(did) {
  if (!did) return 'unknown';
  const s = typeof did === 'string' ? did : did;
  return s.length > 20 ? s.slice(0, 16) + '...' : s;
}

function shortHash(h) {
  return h ? h.slice(0, 12) + '...' : '';
}

function renderCard(p, isEmergency) {
  const q = (p.quality_signal || 0).toFixed(2);
  const tr = timeRemaining(p);
  const cs = p.critique_summary || {};
  const ref = p.utxo_ref || {};

  const summary = proposalSummary(p);

  return `
    <div class="card ${isEmergency ? 'emergency' : ''}">
      <div class="card-header">
        <div class="card-title">${proposalTitle(p)}</div>
        <span class="quality-badge ${qualityClass(p.quality_signal)} has-tooltip" data-tip="${escapeHtml(TIPS['Quality Signal'])}">${q}</span>
      </div>
      ${p.ipfs_title ? `<div class="card-type">${proposalTypeLabel(p)}</div>` : ''}
      ${summary ? `<div class="card-summary">${summary}</div>` : ''}
      <div class="card-meta">
        <span>Stake: ${(p.stake_amount || 0) / 1000000} AP3X</span>
        <span class="time-remaining ${tr.cls}">${tr.text}</span>
        <span>Critiques: ${cs.supporting_critiques || 0}S / ${cs.opposing_critiques || 0}O</span>
        <span>Endorsements: ${cs.endorsement_count || 0}</span>
        <span>Proposer: ${shortDid(p.proposer_did)}</span>
      </div>
      <div class="card-actions">
        <button onclick="viewProposal('${ref.tx_hash}', ${ref.output_index})">View Details</button>
      </div>
    </div>
  `;
}

function renderExpiredCard(p) {
  const ref = p.utxo_ref || {};
  return `
    <div class="card">
      <div class="card-header">
        <div class="card-title">${proposalTitle(p)}</div>
        <span class="quality-badge quality-low">expired</span>
      </div>
      <div class="card-meta">
        <span>Proposer: ${shortDid(p.proposer_did)}</span>
        <span>Stake: ${(p.stake_amount || 0) / 1000000} AP3X</span>
      </div>
      <div class="card-actions">
        <button onclick="viewProposal('${ref.tx_hash}', ${ref.output_index})">View Details</button>
      </div>
    </div>
  `;
}

function renderTerminalCard(p) {
  const ref = p.utxo_ref || {};
  const stateClass = p.state === 'Adopted' ? 'quality-high' : p.state === 'Rejected' ? 'quality-low' : 'quality-mid';
  return `
    <div class="card">
      <div class="card-header">
        <div class="card-title">${proposalTitle(p)}</div>
        <span class="quality-badge ${stateClass}">${p.state}</span>
      </div>
      <div class="card-meta">
        <span>Proposer: ${shortDid(p.proposer_did)}</span>
        <span>Stake: ${(p.stake_amount || 0) / 1000000} AP3X</span>
      </div>
      <div class="card-actions">
        <button onclick="viewProposal('${ref.tx_hash}', ${ref.output_index})">View Details</button>
      </div>
    </div>
  `;
}

// ── Modal ───────────────────────────────────────────────────────────────────

async function viewProposal(txHash, outputIndex) {
  const overlay = document.getElementById('modal-overlay');
  const content = document.getElementById('modal-content');
  const title = document.getElementById('modal-title');

  content.innerHTML = '<div class="loading">Loading...</div>';
  overlay.classList.add('active');

  try {
    const d = await api(`/api/proposals/${txHash}/${outputIndex}`);
    const p = d.proposal;
    const tr = d.track_record || {};
    const critiques = d.critiques || [];
    const endorsements = d.endorsements || [];

    title.textContent = proposalTypeLabel(p);

    // Fetch IPFS document if storage_uri is an ipfs:// URI
    let ipfsResult = null;
    if (p.storage_uri && p.storage_uri.startsWith('ipfs://')) {
      ipfsResult = await fetchIpfsDocument(p.storage_uri, p.proposal_hash);
    }

    const expiry = new Date(p.submitted_at + p.review_window);
    const submitted = new Date(p.submitted_at);

    let critiqueHtml = critiques.length === 0 ? '<div class="empty">No critiques</div>' : '';
    critiques.forEach(c => {
      const typeClass = `critique-type-${(c.critique_type || '').toLowerCase()}`;
      const icon = c.critique_type === 'Supportive' ? '&#10003;' : c.critique_type === 'Opposing' ? '&#10007;' : '&#9998;';
      const q = c.quality ? (c.quality.total || 0).toFixed(2) : '?';
      const hasIpfsDoc = c.storage_uri && c.storage_uri.startsWith('ipfs://');
      critiqueHtml += `
        <div class="critique-item">
          <span class="${typeClass}">${icon} ${c.critique_type || 'Unknown'}</span>
          &mdash; ${shortDid(c.critic_did)} (${(c.stake_amount || 0) / 1000000} AP3X)
          <span style="float:right;color:var(--text-muted)">Quality: ${q}</span>
          ${hasIpfsDoc ? `<br><button onclick="loadCritiqueDoc(this, '${c.storage_uri}', '${c.critique_hash || ''}')" style="font-size:11px;margin-top:4px;cursor:pointer">View Document</button><div class="critique-doc-container"></div>` : ''}
        </div>
      `;
    });

    let endorseHtml = endorsements.length === 0 ? '<div class="empty">No endorsements</div>' : '';
    let totalEndorse = 0;
    endorsements.forEach(e => {
      const amt = (e.stake_amount || 0) / 1000000;
      totalEndorse += amt;
      endorseHtml += `
        <div class="endorsement-item">
          &#128077; ${shortDid(e.endorser_did)} &mdash; ${amt} AP3X
        </div>
      `;
    });

    content.innerHTML = `
      <dl class="detail-grid">
        <dt>Proposer</dt><dd>${shortDid(p.proposer_did)}</dd>
        <dt>Track record</dt><dd>${tr.adopted_count || 0} adopted, ${(tr.by_state || {}).Rejected || 0} rejected, ${(tr.by_state || {}).Expired || 0} expired</dd>
        <dt>Submitted</dt><dd>${submitted.toUTCString()}</dd>
        <dt>Expires</dt><dd>${expiry.toUTCString()}</dd>
        <dt>Stake</dt><dd>${(p.stake_amount || 0) / 1000000} AP3X</dd>
        <dt>Quality signal</dt><dd><span class="quality-badge ${qualityClass(p.quality_signal)}">${(p.quality_signal || 0).toFixed(3)}</span></dd>
        <dt>State</dt><dd>${p.state}</dd>
        <dt>Document</dt><dd>${renderDocumentLink(p.storage_uri, ipfsResult, p.proposal_hash)}</dd>
        <dt>Proposal hash</dt><dd style="font-family:monospace;font-size:12px">${p.proposal_hash || ''}</dd>
        <dt>UTxO</dt><dd style="font-family:monospace;font-size:12px"><a href="${EXPLORER}/en/transaction/${txHash}" target="_blank">${shortHash(txHash)}#${outputIndex}</a></dd>
      </dl>

      <hr class="section-divider">
      <div class="section-label">Critiques (${critiques.length})</div>
      ${critiqueHtml}

      <hr class="section-divider">
      <div class="section-label">Endorsements (${endorsements.length}) &mdash; ${totalEndorse.toFixed(1)} AP3X total</div>
      ${endorseHtml}

    `;
  } catch (err) {
    content.innerHTML = `<div class="alert warning">Error: ${err.message}</div>`;
  }
}

function closeModal() {
  document.getElementById('modal-overlay').classList.remove('active');
}

// ── Timeline ───────────────────────────────────────────────────────────────

async function loadTimeline() {
  const el = document.getElementById('timeline-content');
  try {
    const events = await api('/api/timeline');
    if (events.length === 0) {
      el.innerHTML = '<div class="empty">No governance activity yet.</div>';
      return;
    }
    el.innerHTML = '<div class="timeline">' + events.map(renderTimelineEvent).join('') + '</div>';
  } catch (err) {
    el.textContent = 'Error: ' + err.message;
  }
}

function relativeTime(ts) {
  if (!ts) return 'unknown';
  const diff = Date.now() - ts;
  if (diff < 0) return 'just now';
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return mins + 'm ago';
  const hours = Math.floor(mins / 60);
  if (hours < 24) return hours + 'h ago';
  const days = Math.floor(hours / 24);
  if (days < 30) return days + 'd ago';
  return Math.floor(days / 30) + 'mo ago';
}

function renderTimelineEvent(ev) {
  let icon, color, label, detail;

  if (ev.type === 'proposal') {
    icon = '&#9733;';
    color = 'var(--accent)';
    const typeLabel = (ev.proposal_type || '').replace(/([A-Z])/g, ' $1').trim();
    label = `New Proposal: ${typeLabel}`;
    detail = `${ev.stake} AP3X staked`;
    if (ev.state && ev.state !== 'Open') detail += ` &mdash; ${ev.state}`;
  } else if (ev.type === 'critique') {
    const ct = ev.critique_type || 'Unknown';
    icon = ct === 'Supportive' ? '&#10003;' : ct === 'Opposing' ? '&#10007;' : '&#9998;';
    color = ct === 'Supportive' ? 'var(--green)' : ct === 'Opposing' ? 'var(--red)' : 'var(--yellow)';
    label = `${ct} Critique`;
    detail = `${ev.stake} AP3X staked`;
  } else {
    icon = '&#128077;';
    color = '#a371f7';
    label = 'Endorsement';
    detail = `${ev.stake} AP3X staked`;
  }

  const clickTarget = ev.type === 'proposal'
    ? `onclick="viewProposal('${ev.tx_hash}', ${ev.output_index})"`
    : ev.proposal_tx_hash
      ? `onclick="viewProposal('${ev.proposal_tx_hash}', 0)"`
      : '';

  return `
    <div class="timeline-event" ${clickTarget} style="cursor:pointer">
      <div class="timeline-dot" style="background:${color}">${icon}</div>
      <div class="timeline-body">
        <div class="timeline-label">${label}</div>
        <div class="timeline-detail">${detail} &mdash; ${shortDid(ev.agent_did)}</div>
        <div class="timeline-time">${relativeTime(ev.timestamp)}</div>
      </div>
    </div>
  `;
}

// ── Leaderboard ────────────────────────────────────────────────────────────

async function loadLeaderboard() {
  const el = document.getElementById('agents-content');
  try {
    const agents = await api('/api/leaderboard');
    if (agents.length === 0) {
      el.innerHTML = '<div class="empty">No governance activity yet.</div>';
      return;
    }
    let html = `
      <table class="leaderboard-table">
        <thead>
          <tr>
            <th>#</th>
            <th>Agent</th>
            <th>Proposals</th>
            <th>Critiques</th>
            <th>Endorsements</th>
            <th>Adopted</th>
            <th>Rate</th>
          </tr>
        </thead>
        <tbody>
    `;
    agents.forEach((a, i) => {
      const rank = i + 1;
      const rankClass = rank <= 3 ? `rank-${rank}` : '';
      html += `
        <tr class="${rankClass}">
          <td class="rank-cell">${rank}</td>
          <td class="did-cell">${shortDid(a.agent_did)}</td>
          <td>${a.total_proposals}</td>
          <td>${a.critiques || 0}</td>
          <td>${a.endorsements || 0}</td>
          <td>${a.adopted}</td>
          <td>${(a.adoption_rate * 100).toFixed(0)}%</td>
        </tr>
      `;
    });
    html += '</tbody></table>';
    el.innerHTML = html;
  } catch (err) {
    el.textContent = 'Error: ' + err.message;
  }
}

// ── Notifications ──────────────────────────────────────────────────────────

function notify(msg, type) {
  const el = document.getElementById('notification');
  el.className = `alert ${type}`;
  el.textContent = msg;
  el.style.display = 'block';
  setTimeout(() => { el.style.display = 'none'; }, 6000);
}
