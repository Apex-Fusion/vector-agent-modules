// ── State ───────────────────────────────────────────────────────────────────

let pollInterval = null;
const POLL_MS = 30000;
let EXPLORER = '';
let leaderboardCache = [];
let challengesCache = [];
let capabilityDebounce = null;

// ── Init ────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', async () => {
  // Tab switching
  document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
      tab.classList.add('active');
      document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
      const name = tab.dataset.tab;
      if (name === 'challenges') loadChallenges();
      if (name === 'sybil')      loadSybil();
      if (name === 'stats')      loadStats();
      if (name === 'leaderboard') loadLeaderboard();
    });
  });

  document.getElementById('modal-overlay').addEventListener('click', e => {
    if (e.target === e.currentTarget) closeModal();
  });

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeModal();
  });

  try {
    const cfg = await api('/api/config');
    EXPLORER = cfg.explorer || '';
    document.getElementById('network-badge').textContent = cfg.network || 'unknown';
  } catch (e) {
    console.error('Config load failed', e);
  }

  refresh();
  pollInterval = setInterval(refresh, POLL_MS);
});

function debouncedLeaderboard() {
  if (capabilityDebounce) clearTimeout(capabilityDebounce);
  capabilityDebounce = setTimeout(loadLeaderboard, 350);
}

async function refresh() {
  await Promise.all([loadHealth(), loadLeaderboard()]);
  const active = document.querySelector('.tab.active')?.dataset.tab;
  if (active === 'challenges') await loadChallenges();
  if (active === 'sybil')      await loadSybil();
  if (active === 'stats')      await loadStats();
}

// ── API helpers ─────────────────────────────────────────────────────────────

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = String(text ?? '');
  return div.innerHTML;
}

function shortDid(did) {
  if (!did) return 'unknown';
  const s = String(did);
  return s.length > 20 ? s.slice(0, 12) + '…' + s.slice(-6) : s;
}

function dfmToAp3x(dfm) {
  const n = Number(dfm || 0) / 1_000_000;
  return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function tierClass(tier) {
  const t = (tier || '').toLowerCase();
  return `tier-${t || 'unverified'}`;
}

function severityClass(sev) {
  if (sev >= 0.7) return 'severity-high';
  if (sev >= 0.4) return 'severity-mid';
  return 'severity-low';
}

function slotToRelative(slot) {
  if (!slot) return '—';
  return `slot ${Number(slot).toLocaleString()}`;
}

// ── Health ──────────────────────────────────────────────────────────────────

async function loadHealth() {
  const el = document.getElementById('status');
  try {
    const h = await api('/health');
    if (h.status === 'ok') {
      el.className = 'status ok';
      const agents = h.agents_indexed || '0';
      const slot  = h.last_poll_slot ? Number(h.last_poll_slot).toLocaleString() : '—';
      el.textContent = `Indexed ${agents} agents · last slot ${slot}`;
    } else {
      el.className = 'status error';
      el.textContent = 'Indexer disconnected';
    }
  } catch {
    el.className = 'status error';
    el.textContent = 'Disconnected';
  }
}

// ── Leaderboard ─────────────────────────────────────────────────────────────

async function loadLeaderboard() {
  const el = document.getElementById('leaderboard-content');
  const cap  = document.getElementById('filter-capability').value.trim();
  const tier = document.getElementById('filter-tier').value.trim();

  const params = new URLSearchParams();
  params.set('limit', '200');
  if (cap)  params.set('capability', cap);
  if (tier) params.set('min_tier', tier);

  try {
    leaderboardCache = await api('/v1/reputation/leaderboard?' + params.toString());
    renderLeaderboard();
  } catch (err) {
    el.textContent = 'Error: ' + err.message;
  }
}

function renderLeaderboard() {
  const el = document.getElementById('leaderboard-content');
  const search = (document.getElementById('filter-search').value || '').toLowerCase();

  let rows = leaderboardCache;
  if (search) {
    rows = rows.filter(r => (r.agent_did || '').toLowerCase().includes(search));
  }

  if (rows.length === 0) {
    el.innerHTML = '<div class="empty">No agents match the current filters.</div>';
    return;
  }

  let html = `
    <table class="leaderboard-table">
      <thead>
        <tr>
          <th>#</th>
          <th>Agent DID</th>
          <th>Tier</th>
          <th>Score</th>
          <th>Self-stake</th>
          <th>Endorsements</th>
          <th>Challenges</th>
          <th>History</th>
        </tr>
      </thead>
      <tbody>
  `;
  rows.forEach((a, i) => {
    const rank = i + 1;
    const rankClass = rank <= 3 ? `rank-${rank}` : '';
    html += `
      <tr class="${rankClass}" onclick="viewAgent('${escapeHtml(a.agent_did)}')">
        <td class="rank-cell">${rank}</td>
        <td class="did-cell">${escapeHtml(shortDid(a.agent_did))}</td>
        <td><span class="tier-badge ${tierClass(a.tier)}">${escapeHtml(a.tier || 'Unverified')}</span></td>
        <td class="score-cell">${dfmToAp3x(a.net_score)} AP3X</td>
        <td>${dfmToAp3x(a.self_stake)}</td>
        <td>${dfmToAp3x(a.endorsement_total)}</td>
        <td>${dfmToAp3x(a.challenge_total)}</td>
        <td>${dfmToAp3x(a.history_bonus)}</td>
      </tr>
    `;
  });
  html += '</tbody></table>';
  el.innerHTML = html;
}

// ── Challenges ──────────────────────────────────────────────────────────────

async function loadChallenges() {
  const el = document.getElementById('challenges-content');
  try {
    // There's no cross-agent challenges endpoint; derive from leaderboard.
    // For each indexed agent, fetch their challenges and flatten.
    const scores = leaderboardCache.length > 0
      ? leaderboardCache
      : await api('/v1/reputation/leaderboard?limit=200');
    const results = await Promise.all(
      scores.map(s => api(`/v1/reputation/challenges/${encodeURIComponent(s.agent_did)}`).catch(() => []))
    );
    challengesCache = results.flat();
    renderChallenges();
  } catch (err) {
    el.textContent = 'Error: ' + err.message;
  }
}

function renderChallenges() {
  const el = document.getElementById('challenges-content');
  const filter = document.getElementById('challenge-state-filter').value;
  let rows = challengesCache;
  if (filter) rows = rows.filter(c => c.state === filter);

  if (rows.length === 0) {
    el.innerHTML = '<div class="empty">No challenges match the current filter.</div>';
    return;
  }

  rows.sort((a, b) => (b.created_at || 0) - (a.created_at || 0));

  el.innerHTML = rows.map(c => {
    const stateCls = `state-${(c.state || 'open').toLowerCase()}`;
    return `
      <div class="challenge-card">
        <div>
          <div class="c-title">${escapeHtml(c.capability || 'unknown capability')}</div>
          <div class="c-meta">
            <span>Target: <span class="mono">${shortDid(c.target_did)}</span></span>
            <span>Challenger: <span class="mono">${shortDid(c.challenger_did)}</span></span>
            <span>Stake: ${dfmToAp3x(c.stake_amount)} AP3X</span>
            <span>${slotToRelative(c.created_at)}</span>
          </div>
        </div>
        <div class="c-state">
          <span class="${stateCls}">${escapeHtml(c.state || 'Open')}</span>
          ${c.outcome ? `<span class="tier-badge tier-novice">${escapeHtml(c.outcome)}</span>` : ''}
          <button onclick="viewAgent('${escapeHtml(c.target_did)}')">Target</button>
        </div>
      </div>
    `;
  }).join('');
}

// ── Sybil ───────────────────────────────────────────────────────────────────

async function loadSybil() {
  const el = document.getElementById('sybil-content');
  try {
    const flags = await api('/v1/reputation/sybil');
    if (flags.length === 0) {
      el.innerHTML = '<div class="empty">No sybil flags detected. The endorsement graph looks clean.</div>';
      return;
    }
    el.innerHTML = flags.map(f => {
      const related = (f.related_dids || '').split(',').filter(Boolean);
      return `
        <div class="sybil-card">
          <div class="s-row">
            <div class="s-type">${escapeHtml(f.flag_type || 'unknown')}</div>
            <span class="sybil-flag ${severityClass(f.severity)}">severity ${Number(f.severity).toFixed(2)}</span>
          </div>
          <div class="s-agent">Agent: ${shortDid(f.agent_did)}</div>
          ${f.details ? `<div class="s-details">${escapeHtml(f.details)}</div>` : ''}
          ${related.length ? `<div class="s-related">Related: ${related.map(shortDid).join(' · ')}</div>` : ''}
          <div style="margin-top:8px"><button onclick="viewAgent('${escapeHtml(f.agent_did)}')">Inspect agent</button></div>
        </div>
      `;
    }).join('');
  } catch (err) {
    el.textContent = 'Error: ' + err.message;
  }
}

// ── Stats ───────────────────────────────────────────────────────────────────

async function loadStats() {
  const el = document.getElementById('stats-content');
  try {
    const s = await api('/v1/reputation/stats');
    const tiers = s.tier_distribution || {};
    const tierOrder = ['Elite', 'Trusted', 'Established', 'Novice', 'Unverified'];
    const total = Object.values(tiers).reduce((a, b) => a + b, 0) || 1;

    let tierHtml = '';
    for (const name of tierOrder) {
      const count = tiers[name] || 0;
      const pct = (count / total) * 100;
      tierHtml += `
        <div class="tier-bar">
          <span class="tier-badge ${tierClass(name)}">${name}</span>
          <div class="bar-track"><div class="bar-fill" style="width:${pct.toFixed(1)}%;background:${tierBarColor(name)}"></div></div>
          <span class="bar-count">${count}</span>
        </div>
      `;
    }

    const lastPollSlot = s.last_poll_slot ? Number(s.last_poll_slot).toLocaleString() : '—';
    const lastPollTime = s.last_poll_time
      ? new Date(Number(s.last_poll_time) * 1000).toLocaleString()
      : '—';

    el.innerHTML = `
      <div class="stat-grid">
        <div class="stat-card">
          <div class="label">Indexed agents</div>
          <div class="value">${s.total_agents || 0}</div>
          <div class="sub">${s.sybil_flagged_agents || 0} flagged for sybil review</div>
        </div>
        <div class="stat-card">
          <div class="label">Total staked</div>
          <div class="value">${Number(s.total_staked_ap3x || 0).toLocaleString()} AP3X</div>
          <div class="sub">${Number(s.total_staked_dfm || 0).toLocaleString()} DFM</div>
        </div>
        <div class="stat-card">
          <div class="label">Endorsements</div>
          <div class="value">${s.total_endorsements || 0}</div>
          <div class="sub">${dfmToAp3x(s.total_endorsement_value_dfm)} AP3X total value</div>
        </div>
        <div class="stat-card">
          <div class="label">Challenges</div>
          <div class="value">${s.active_challenges || 0} open</div>
          <div class="sub">${s.total_challenges || 0} total (all states)</div>
        </div>
      </div>

      <hr class="section-divider">
      <div class="section-label">Tier distribution</div>
      <div class="tier-bars">${tierHtml}</div>

      <hr class="section-divider">
      <div class="section-label">Indexer status</div>
      <div class="stat-grid">
        <div class="stat-card">
          <div class="label">Last poll slot</div>
          <div class="value" style="font-size:18px">${lastPollSlot}</div>
          <div class="sub">${lastPollTime}</div>
        </div>
      </div>
    `;
  } catch (err) {
    el.textContent = 'Error: ' + err.message;
  }
}

function tierBarColor(name) {
  switch ((name || '').toLowerCase()) {
    case 'elite': return 'var(--purple)';
    case 'trusted': return 'var(--green)';
    case 'established': return 'var(--accent)';
    case 'novice': return 'var(--yellow)';
    default: return 'var(--text-muted)';
  }
}

// ── Agent detail modal ──────────────────────────────────────────────────────

async function viewAgent(did) {
  if (!did) return;
  const overlay = document.getElementById('modal-overlay');
  const content = document.getElementById('modal-content');
  const title   = document.getElementById('modal-title');

  title.textContent = 'Agent ' + shortDid(did);
  content.innerHTML = '<div class="loading">Loading...</div>';
  overlay.classList.add('active');

  try {
    const d = await api(`/v1/reputation/agent/${encodeURIComponent(did)}`);
    const s = d.score || {};
    const caps = new Set();
    (d.stakes || []).forEach(st => (st.capabilities || []).forEach(c => caps.add(c)));

    const breakdown = `
      <div class="score-breakdown">
        <div class="score-item positive">
          <div class="label">Self-stake</div>
          <div class="val">${dfmToAp3x(s.self_stake)}</div>
        </div>
        <div class="score-item positive">
          <div class="label">Endorsements</div>
          <div class="val">+${dfmToAp3x(s.endorsement_total)}</div>
        </div>
        <div class="score-item negative">
          <div class="label">Challenges</div>
          <div class="val">−${dfmToAp3x(s.challenge_total)}</div>
        </div>
        <div class="score-item positive">
          <div class="label">History bonus</div>
          <div class="val">+${dfmToAp3x(s.history_bonus)}</div>
        </div>
        <div class="score-item negative">
          <div class="label">Decay</div>
          <div class="val">−${dfmToAp3x(s.decay)}</div>
        </div>
      </div>
    `;

    const stakeRows = (d.stakes || []).map(st => `
      <div class="list-row">
        <div>Stake: <strong>${dfmToAp3x(st.stake_amount)} AP3X</strong> · history ${st.history_points || 0} pts</div>
        <div class="small-did">UTXO ${shortDid(st.utxo_ref)} · last updated slot ${Number(st.last_updated || 0).toLocaleString()}</div>
        <div class="caps-list" style="margin-top:4px">${
          (st.capabilities || []).map(c => `<span class="capability-chip">${escapeHtml(c)}</span>`).join('') || '<span class="small-did">no capabilities</span>'
        }</div>
      </div>
    `).join('') || '<div class="empty">No active stake.</div>';

    const recvRows = (d.endorsements_received || []).map(e => `
      <div class="list-row">
        <div><strong>${dfmToAp3x(e.stake_amount)} AP3X</strong> from <span class="mono">${shortDid(e.endorser_did)}</span></div>
        <div class="caps-list" style="margin-top:4px">${
          (e.capabilities || []).map(c => `<span class="capability-chip">${escapeHtml(c)}</span>`).join('') || ''
        }</div>
      </div>
    `).join('') || '<div class="empty">No endorsements received.</div>';

    const givenRows = (d.endorsements_given || []).map(e => `
      <div class="list-row">
        <div><strong>${dfmToAp3x(e.stake_amount)} AP3X</strong> to <span class="mono">${shortDid(e.target_did)}</span></div>
        <div class="caps-list" style="margin-top:4px">${
          (e.capabilities || []).map(c => `<span class="capability-chip">${escapeHtml(c)}</span>`).join('') || ''
        }</div>
      </div>
    `).join('') || '<div class="empty">No endorsements given.</div>';

    const chalRows = (d.challenges || []).map(c => {
      const stateCls = `state-${(c.state || 'open').toLowerCase()}`;
      return `
        <div class="list-row">
          <div><strong>${escapeHtml(c.capability)}</strong>
            <span class="${stateCls}" style="margin-left:8px">${escapeHtml(c.state || 'Open')}</span>
            ${c.outcome ? `<span class="tier-badge tier-novice" style="margin-left:4px">${escapeHtml(c.outcome)}</span>` : ''}
          </div>
          <div class="small-did">By ${shortDid(c.challenger_did)} · ${dfmToAp3x(c.stake_amount)} AP3X · slot ${Number(c.created_at || 0).toLocaleString()}</div>
        </div>
      `;
    }).join('') || '<div class="empty">No challenges.</div>';

    const bonusRows = (d.history_bonuses || []).map(b => `
      <div class="list-row">
        <div><strong>${escapeHtml(b.source)}</strong> · ${b.bonus_points || 0} bonus points</div>
        <div class="small-did">Source ref: ${shortDid(b.source_ref)} · slot ${Number(b.created_at || 0).toLocaleString()}</div>
      </div>
    `).join('') || '<div class="empty">No history bonuses.</div>';

    const sybilRows = (d.sybil_flags || []).map(f => `
      <div class="list-row">
        <div><strong>${escapeHtml(f.flag_type)}</strong>
          <span class="sybil-flag ${severityClass(f.severity)}" style="margin-left:8px">severity ${Number(f.severity).toFixed(2)}</span>
        </div>
        <div class="small-did">${escapeHtml(f.details || '')}</div>
      </div>
    `).join('');

    content.innerHTML = `
      <dl class="detail-grid">
        <dt>Agent DID</dt><dd class="mono">${escapeHtml(did)}</dd>
        <dt>Tier</dt><dd><span class="tier-badge ${tierClass(s.tier)}">${escapeHtml(s.tier || 'Unverified')}</span></dd>
        <dt>Net score</dt><dd><strong>${dfmToAp3x(s.net_score)} AP3X</strong></dd>
        <dt>Capabilities</dt><dd>${caps.size ? [...caps].map(c => `<span class="capability-chip">${escapeHtml(c)}</span>`).join(' ') : '<span class="small-did">none</span>'}</dd>
        <dt>Last updated</dt><dd>slot ${Number(s.last_updated_slot || 0).toLocaleString()}</dd>
      </dl>

      <div class="section-label">Score breakdown</div>
      ${breakdown}

      <hr class="section-divider">
      <div class="section-label">Self-stake (${(d.stakes || []).length})</div>
      <div class="row-list">${stakeRows}</div>

      <hr class="section-divider">
      <div class="section-label">Endorsements received (${(d.endorsements_received || []).length})</div>
      <div class="row-list">${recvRows}</div>

      <hr class="section-divider">
      <div class="section-label">Endorsements given (${(d.endorsements_given || []).length})</div>
      <div class="row-list">${givenRows}</div>

      <hr class="section-divider">
      <div class="section-label">Challenges (${(d.challenges || []).length})</div>
      <div class="row-list">${chalRows}</div>

      <hr class="section-divider">
      <div class="section-label">History bonuses (${(d.history_bonuses || []).length})</div>
      <div class="row-list">${bonusRows}</div>

      ${sybilRows ? `
        <hr class="section-divider">
        <div class="section-label">Sybil flags (${(d.sybil_flags || []).length})</div>
        <div class="row-list">${sybilRows}</div>
      ` : ''}
    `;
  } catch (err) {
    const msg = err && err.message ? err.message : 'Unknown error';
    if (msg.includes('404') || msg.toLowerCase().includes('not found')) {
      content.innerHTML = '<div class="empty">Agent not indexed yet. Try again after the next indexer poll.</div>';
    } else {
      content.innerHTML = `<div class="empty" style="color:var(--red)">Error: ${escapeHtml(msg)}</div>`;
    }
  }
}

function closeModal() {
  document.getElementById('modal-overlay').classList.remove('active');
}
