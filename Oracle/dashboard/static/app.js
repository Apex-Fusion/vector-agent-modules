// ── State ───────────────────────────────────────────────────────────────────

let pollInterval = null;
const POLL_MS = 30000;
const EXPLORER = 'https://vector.testnet.apexscan.org/en/transaction';

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
  await Promise.all([loadHealth(), loadProposals(), loadTreasury(), loadStats()]);
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

async function loadHealth() {
  try {
    const h = await api('/api/health');
    const el = document.getElementById('status');
    if (h.status === 'ok') {
      el.className = 'status ok';
      el.textContent = `Slot ${h.slot.toLocaleString()} | ${h.wallet_balance_apex} AP3X | ${h.signing_mode}`;
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
  const empty = document.getElementById('proposals-empty');
  const list = document.getElementById('proposals-list');

  try {
    const proposals = await api('/api/proposals');
    loading.style.display = 'none';

    if (proposals.length === 0) {
      empty.style.display = 'block';
      list.innerHTML = '';
      return;
    }
    empty.style.display = 'none';

    // Separate emergency, standard (open/amended), and expired/stale
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

    if (html === '') {
      empty.style.display = 'block';
    }

    list.innerHTML = html;
  } catch (err) {
    loading.textContent = 'Error loading proposals: ' + err.message;
  }
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
      <div class="stat-grid">
        <div class="stat-card">
          <div class="label">Total Proposals</div>
          <div class="value">${total}</div>
          <div class="sub">${s.currently_open} currently open</div>
        </div>
        <div class="stat-card">
          <div class="label">Adoption Rate</div>
          <div class="value">${(s.adoption_rate * 100).toFixed(0)}%</div>
          <div class="sub">${bs.Adopted || 0} adopted, ${bs.Rejected || 0} rejected</div>
        </div>
        <div class="stat-card">
          <div class="label">Unique Proposers</div>
          <div class="value">${s.unique_proposers}</div>
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
          <div class="value" style="font-size:16px">${h.wallet_balance_apex || 0} AP3X</div>
          <div class="sub">Signing: ${h.signing_mode || 'unknown'}</div>
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
  if (t === 'ParameterChange') {
    const params = p.type_params || {};
    return `Parameter: ${params.param_name || '?'} &rarr; ${params.new_value || '?'}`;
  }
  if (t === 'TreasurySpend') return `Treasury: ${((p.type_params || {}).amount || 0) / 1000000} AP3X`;
  return t.replace(/([A-Z])/g, ' $1').trim();
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

  return `
    <div class="card ${isEmergency ? 'emergency' : ''}">
      <div class="card-header">
        <div class="card-title">${proposalTypeLabel(p)}</div>
        <span class="quality-badge ${qualityClass(p.quality_signal)}">${q}</span>
      </div>
      <div class="card-meta">
        <span>Stake: ${(p.stake_amount || 0) / 1000000} AP3X</span>
        <span class="time-remaining ${tr.cls}">${tr.text}</span>
        <span>Critiques: ${cs.supporting_critiques || 0}S / ${cs.opposing_critiques || 0}O</span>
        <span>Endorsements: ${cs.endorsement_count || 0}</span>
        <span>Proposer: ${shortDid(p.proposer_did)}</span>
      </div>
      <div class="card-meta">
        <span>URI: <a href="${p.storage_uri || '#'}" target="_blank">${p.storage_uri || 'none'}</a></span>
      </div>
      <div class="card-actions">
        <button onclick="viewProposal('${ref.tx_hash}', ${ref.output_index})">View</button>
        <button class="primary" onclick="quickAction('adopt', '${ref.tx_hash}', ${ref.output_index})">Adopt</button>
        <button class="danger" onclick="quickAction('reject', '${ref.tx_hash}', ${ref.output_index})">Reject</button>
        <button class="warn" onclick="quickAction('extend', '${ref.tx_hash}', ${ref.output_index})">Extend</button>
      </div>
    </div>
  `;
}

function renderExpiredCard(p) {
  const ref = p.utxo_ref || {};
  return `
    <div class="card">
      <div class="card-header">
        <div class="card-title">${proposalTypeLabel(p)}</div>
        <span class="quality-badge quality-low">expired</span>
      </div>
      <div class="card-meta">
        <span>Proposer: ${shortDid(p.proposer_did)}</span>
        <span>Stake: ${(p.stake_amount || 0) / 1000000} AP3X</span>
      </div>
      <div class="card-actions">
        <button class="danger" onclick="expireOnChain('${ref.tx_hash}', ${ref.output_index})">Expire On-Chain</button>
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

    const expiry = new Date(p.submitted_at + p.review_window);
    const submitted = new Date(p.submitted_at);

    let critiqueHtml = critiques.length === 0 ? '<div class="empty">No critiques</div>' : '';
    critiques.forEach(c => {
      const typeClass = `critique-type-${(c.critique_type || '').toLowerCase()}`;
      const icon = c.critique_type === 'Supportive' ? '&#10003;' : c.critique_type === 'Opposing' ? '&#10007;' : '&#9998;';
      const q = c.quality ? (c.quality.total || 0).toFixed(2) : '?';
      critiqueHtml += `
        <div class="critique-item">
          <span class="${typeClass}">${icon} ${c.critique_type || 'Unknown'}</span>
          &mdash; ${shortDid(c.critic_did)} (${(c.stake_amount || 0) / 1000000} AP3X)
          <span style="float:right;color:var(--text-muted)">Quality: ${q}</span>
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
        <dt>Document</dt><dd><a href="${p.storage_uri || '#'}" target="_blank">${p.storage_uri || 'none'} &#8599;</a></dd>
        <dt>Proposal hash</dt><dd style="font-family:monospace;font-size:12px">${p.proposal_hash || ''}</dd>
        <dt>UTxO</dt><dd style="font-family:monospace;font-size:12px"><a href="${EXPLORER}/${txHash}" target="_blank">${shortHash(txHash)}#${outputIndex}</a></dd>
      </dl>

      <hr class="section-divider">
      <div class="section-label">Critiques (${critiques.length})</div>
      ${critiqueHtml}

      <hr class="section-divider">
      <div class="section-label">Endorsements (${endorsements.length}) &mdash; ${totalEndorse.toFixed(1)} AP3X total</div>
      ${endorseHtml}

      <hr class="section-divider">
      <div class="reward-calc">
        <label>Reward amount (AP3X):</label>
        <input type="number" id="reward-input" value="100" min="50" max="500" step="10"
               oninput="updateRewardBreakdown()">
        <div class="reward-breakdown" id="reward-breakdown"></div>
      </div>

      <div class="action-form">
        <label style="font-size:13px;margin-bottom:4px;display:block">Reasoning:</label>
        <textarea id="reasoning-input" placeholder="Enter reasoning for adoption or rejection..."></textarea>
        <div class="action-buttons">
          <button class="primary" onclick="doAction('adopt', '${txHash}', ${outputIndex})">Adopt</button>
          <button class="danger" onclick="doAction('reject', '${txHash}', ${outputIndex})">Reject</button>
          <button class="warn" onclick="doAction('extend', '${txHash}', ${outputIndex})">Extend Review</button>
        </div>
      </div>
    `;

    updateRewardBreakdown();
  } catch (err) {
    content.innerHTML = `<div class="alert warning">Error: ${err.message}</div>`;
  }
}

function updateRewardBreakdown() {
  const input = document.getElementById('reward-input');
  const el = document.getElementById('reward-breakdown');
  if (!input || !el) return;
  const v = parseFloat(input.value) || 0;
  const proposer = (v * 0.7).toFixed(1);
  const critics = (v * 0.2).toFixed(1);
  const protocol = (v * 0.1).toFixed(1);
  el.textContent = `Proposer (70%): ${proposer} AP3X | Critics (20%): ${critics} AP3X | Protocol (10%): ${protocol} AP3X`;
}

function closeModal() {
  document.getElementById('modal-overlay').classList.remove('active');
}

// ── Actions ─────────────────────────────────────────────────────────────────

async function doAction(action, txHash, outputIndex) {
  const reasoning = document.getElementById('reasoning-input')?.value || '';
  if (!reasoning.trim()) {
    notify('Please enter reasoning', 'warning');
    return;
  }

  if (action === 'adopt') {
    const reward = parseFloat(document.getElementById('reward-input')?.value || '100');
    const rewardLovelace = Math.round(reward * 1000000);
    if (rewardLovelace < 50000000 || rewardLovelace > 500000000) {
      notify('Reward must be between 50 and 500 AP3X', 'warning');
      return;
    }
    await submitAction('/api/adopt', {
      tx_hash: txHash, output_index: outputIndex,
      reasoning, reward_amount: rewardLovelace,
    });
  } else if (action === 'reject') {
    await submitAction('/api/reject', {
      tx_hash: txHash, output_index: outputIndex, reasoning,
    });
  } else if (action === 'extend') {
    // Default: extend by 3 days (in POSIX ms)
    await submitAction('/api/extend', {
      tx_hash: txHash, output_index: outputIndex,
      additional_ms: 259200000,
    });
  }
}

async function quickAction(action, txHash, outputIndex) {
  if (action === 'adopt' || action === 'reject') {
    viewProposal(txHash, outputIndex);
    return;
  }
  if (action === 'extend') {
    if (!confirm('Extend review window by 3 days?')) return;
    await submitAction('/api/extend', {
      tx_hash: txHash, output_index: outputIndex,
      additional_ms: 259200000,
    });
  }
}

async function expireOnChain(txHash, outputIndex) {
  if (!confirm('Expire this proposal on-chain? Stake will be returned to the proposer.')) return;
  await submitAction('/api/expire', { tx_hash: txHash, output_index: outputIndex });
}

async function submitAction(endpoint, body) {
  try {
    notify('Submitting transaction...', 'info');
    const result = await api(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    notify(`Transaction submitted: ${shortHash(result.tx_hash)}`, 'success');
    closeModal();
    setTimeout(refresh, 5000);
  } catch (err) {
    notify('Error: ' + err.message, 'warning');
  }
}

function notify(msg, type) {
  const el = document.getElementById('notification');
  el.className = `alert ${type}`;
  el.textContent = msg;
  el.style.display = 'block';
  setTimeout(() => { el.style.display = 'none'; }, 6000);
}
