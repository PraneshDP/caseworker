// Caseworker Morning Console — UI Client Application Logic
// Matches styles.css structure and connects to SSE /api/events and REST API.

(function () {
  'use strict';

  // --- State ---
  let config = null;
  let currentRunState = null;
  let eventSource = null;
  let casesData = [];
  let actionsData = [];
  let pendingApprovals = [];
  let activeTab = 'review';
  let activeActionFilter = 'all';
  let currentEditAction = null;
  let latestSeqAtRunStart = 0;

  // --- DOM Elements ---
  const el = {
    chipThreshold: document.getElementById('chip-threshold'),
    chipModel: document.getElementById('chip-model'),
    chipChain: document.getElementById('chip-chain'),
    statusPill: document.getElementById('status-pill'),
    statusText: document.getElementById('status-text'),
    actorInput: document.getElementById('actor'),
    bypassToggle: document.getElementById('bypass'),
    btnStart: document.getElementById('btn-start'),
    btnCancel: document.getElementById('btn-cancel'),
    banners: document.getElementById('banners'),

    runId: document.getElementById('run-id'),
    progressFill: document.getElementById('progress-fill'),
    progressText: document.getElementById('progress-text'),
    statsList: document.getElementById('stats'),
    caseCount: document.getElementById('case-count'),
    caseload: document.getElementById('caseload'),

    tabs: document.querySelectorAll('.tabs .tab'),
    panels: document.querySelectorAll('.panel'),
    tabCountReview: document.getElementById('tab-count-review'),

    reviewEmpty: document.getElementById('review-empty'),
    reviewStack: document.getElementById('review-stack'),

    actionFilters: document.getElementById('action-filters'),
    actionsList: document.getElementById('actions-list'),

    auditRunSelect: document.getElementById('audit-run'),
    btnLoadAudit: document.getElementById('btn-load-audit'),
    btnVerify: document.getElementById('btn-verify'),
    chainStatus: document.getElementById('chain-status'),
    auditList: document.getElementById('audit-list'),

    btnGuardrails: document.getElementById('btn-guardrails'),
    guardrailsBody: document.getElementById('guardrails-body'),

    policyQ: document.getElementById('policy-q'),
    btnPolicy: document.getElementById('btn-policy'),
    policyBody: document.getElementById('policy-body'),

    traceVerbose: document.getElementById('trace-verbose'),
    trace: document.getElementById('trace'),

    editModal: document.getElementById('edit-modal'),
    editClose: document.getElementById('edit-close'),
    editPayload: document.getElementById('edit-payload'),
    editError: document.getElementById('edit-error'),
    editReason: document.getElementById('edit-reason'),
    editCancel: document.getElementById('edit-cancel'),
    editSubmit: document.getElementById('edit-submit'),
    toasts: document.getElementById('toasts'),

    btnClearChat: document.getElementById('btn-clear-chat'),
    chatMessages: document.getElementById('chat-messages'),
    chatInput: document.getElementById('chat-input'),
    btnChatSend: document.getElementById('btn-chat-send'),
    chatSuggestions: document.getElementById('chat-suggestions')
  };

  // --- Initialization ---
  async function init() {
    setupEventListeners();
    await loadConfig();
    await loadCases();
    await loadRunHistory();
    await loadActionsFromLatest();
    await loadGuardrails();
    connectSSE();
    pollState();
  }

  // --- API Helper ---
  async function api(path, options = {}) {
    try {
      const res = await fetch(path, {
        headers: { 'Content-Type': 'application/json' },
        ...options
      });
      if (!res.ok) {
        const text = await res.text();
        let msg = `HTTP ${res.status}`;
        try {
          const json = JSON.parse(text);
          if (json.error || json.message) msg = json.error || json.message;
        } catch (_) {
          if (text) msg = text;
        }
        throw new Error(msg);
      }
      return await res.json();
    } catch (err) {
      console.error(`API error on ${path}:`, err);
      throw err;
    }
  }

  function showToast(message, type = 'info') {
    const toast = document.createElement('div');
    const toastClass = (type === 'success' || type === 'ok') ? 'ok' : (type === 'error' || type === 'bad') ? 'bad' : 'warn';
    toast.className = `toast ${toastClass}`;
    toast.textContent = message;
    el.toasts.appendChild(toast);
    setTimeout(() => {
      toast.style.opacity = '0';
      setTimeout(() => toast.remove(), 250);
    }, 3500);
  }

  // --- Config & Data Loading ---
  async function loadConfig() {
    try {
      config = await api('/api/config');
      el.chipThreshold.textContent = `τ ${(config.threshold || 0.4).toFixed(2)}`;
      el.chipModel.textContent = `model ${config.model || 'gemini'}`;
      el.chipChain.textContent = `chain SHA-256`;
    } catch (e) {
      console.warn('Could not load config:', e);
    }
  }

  async function loadCases() {
    try {
      const data = await api('/api/cases');
      casesData = data.cases || [];
      el.caseCount.textContent = casesData.length;
      renderCaseload();
    } catch (e) {
      console.warn('Could not load cases:', e);
    }
  }

  async function loadGuardrails() {
    try {
      const data = await api('/api/guardrails');
      renderGuardrails(data);
    } catch (e) {
      console.warn('Could not load guardrails:', e);
    }
  }

  async function loadRunHistory() {
    try {
      const res = await api('/api/runs');
      const runs = Array.isArray(res) ? res : (res.runs || []);
      el.auditRunSelect.innerHTML = '';
      if (!runs || runs.length === 0) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.textContent = 'No runs available';
        el.auditRunSelect.appendChild(opt);
        return;
      }
      runs.forEach(r => {
        const opt = document.createElement('option');
        opt.value = r.run_id;
        opt.textContent = `Run ${r.run_id} (${r.run_date || 'completed'})`;
        el.auditRunSelect.appendChild(opt);
      });
      if (runs[0] && runs[0].run_id) {
        loadAuditLedger(runs[0].run_id);
      }
    } catch (e) {
      console.warn('Could not load run history:', e);
    }
  }

  async function loadActionsFromLatest() {
    try {
      const res = await api('/api/runs/latest/ledger');
      if (res && res.entries) {
        const actions = res.entries.filter(e => e.record_type === 'action');
        if (actions.length > 0) {
          actionsData = actions.map(e => ({
            action_id: e.action_id || '',
            case_id: e.referral_id || '',
            description: e.description || e.action_kind || '',
            detail: e.resolution_detail || e.detail || '',
            score: e.risk_score !== undefined ? e.risk_score : 0,
            status: (e.resolution || (e.executed ? 'auto_executed' : 'refused_and_escalated')),
            executed: Boolean(e.executed)
          }));
          renderActions();
        }
      }
    } catch (_) {}
  }

  // --- SSE & Polling ---
  const SSE_EVENTS = [
    'run_queued', 'run_started', 'referral_started', 'action_planned',
    'risk_classified', 'action_gated', 'action_executed', 'action_refused',
    'step_declined', 'security_event', 'referral_finished', 'run_finished', 'state'
  ];

  function connectSSE() {
    if (eventSource) eventSource.close();
    eventSource = new EventSource('/api/events');

    const handleSSE = function (e) {
      try {
        const event = JSON.parse(e.data);
        if (event.seq && event.seq <= latestSeqAtRunStart) return;
        handleStreamEvent(event);
      } catch (err) {
        console.error('SSE parse error:', err);
      }
    };

    eventSource.onmessage = handleSSE;
    SSE_EVENTS.forEach(evt => {
      eventSource.addEventListener(evt, handleSSE);
    });
  }

  async function pollState() {
    try {
      const res = await api('/api/runs/current');
      if (res && res.state) {
        updateRunUI(res.state, res.pending || []);
      }
    } catch (_) {}
    setTimeout(pollState, 1500);
  }

  function handleStreamEvent(event) {
    if (!event || !event.event || event.event === 'state') return;
    appendTrace(event);
    if (event.event === 'action_executed' || event.event === 'action_gated' || event.event === 'action_refused') {
      const payload = event.payload || {};
      actionsData.push({
        action_id: payload.action_id || '',
        case_id: payload.referral_id || payload.case_id || '',
        description: payload.description || payload.action_kind || '',
        detail: payload.detail || payload.reason || '',
        score: payload.risk_score || (payload.risk ? payload.risk.score : 0),
        status: (event.event === 'action_executed') ? (payload.status || 'auto_executed') : (event.event === 'action_gated') ? 'pending' : 'rejected',
        executed: event.event === 'action_executed'
      });
      renderActions();
    }
  }

  // --- Run Actions ---
  async function startRun() {
    const actor = (el.actorInput.value || 'j.alvarez').trim();
    const autoApprove = el.bypassToggle.checked;

    el.btnStart.disabled = true;
    el.btnCancel.disabled = false;

    // Reset trace for fresh run
    traceEventsList = [];
    el.trace.innerHTML = '';
    actionsData = [];
    renderActions();

    // Capture current sequence before start
    try {
      const currentState = await api('/api/runs/current');
      latestSeqAtRunStart = (currentState && currentState.state && currentState.state.latest_seq) || 0;
    } catch (_) {
      latestSeqAtRunStart = 0;
    }

    connectSSE();

    try {
      const res = await api('/api/runs', {
        method: 'POST',
        body: JSON.stringify({
          actor: actor,
          auto_approve: autoApprove
        })
      });
      if (res.started) {
        showToast(`Morning run started as ${actor}`, 'success');
        updateRunUI(res.state, []);
      }
    } catch (err) {
      showToast(err.message, 'error');
      el.btnStart.disabled = false;
      el.btnCancel.disabled = true;
    }
  }

  async function cancelRun() {
    try {
      await api('/api/runs/current/cancel', { method: 'POST' });
      showToast('Run cancelled', 'warn');
    } catch (err) {
      showToast(err.message, 'error');
    } finally {
      el.btnStart.disabled = false;
      el.btnCancel.disabled = true;
      try {
        const res = await api('/api/runs/current');
        if (res && res.state) {
          updateRunUI(res.state, res.pending || []);
        }
      } catch (_) {}
    }
  }

  // --- Render Helpers ---
  function updateRunUI(state, pending) {
    currentRunState = state;
    pendingApprovals = pending || [];

    el.statusText.textContent = state.status || 'idle';
    el.statusPill.className = `status-pill status-${state.status || 'idle'}`;

    if (state.running) {
      el.btnStart.disabled = true;
      el.btnCancel.disabled = false;
      el.runId.textContent = state.run_id ? `run ${state.run_id}` : 'preparing';
      el.progressText.textContent = `Processing referrals... (${pendingApprovals.length} awaiting human review)`;
      el.progressFill.style.width = '65%';
    } else {
      el.btnStart.disabled = false;
      el.btnCancel.disabled = true;
      if (state.status === 'completed') {
        el.runId.textContent = `run ${state.run_id}`;
        el.progressText.textContent = 'Morning run completed.';
        el.progressFill.style.width = '100%';
        loadRunHistory();
      } else if (state.status === 'cancelled') {
        el.runId.textContent = `run ${state.run_id || 'cancelled'}`;
        el.progressText.textContent = 'Run cancelled — press “Start morning run” to start.';
        el.progressFill.style.width = '0%';
      } else if (state.status === 'failed') {
        el.progressText.textContent = `Run failed: ${state.error || ''}`;
        el.progressFill.style.width = '0%';
      } else {
        el.progressText.textContent = 'Idle — press “Start morning run”.';
        el.progressFill.style.width = '0%';
      }
    }

    el.tabCountReview.textContent = pendingApprovals.length;
    if (pendingApprovals.length > 0) {
      el.tabCountReview.className = 'tab-count hot';
    } else {
      el.tabCountReview.className = 'tab-count';
    }
    renderPendingApprovals();
  }

  function renderCaseload() {
    el.caseload.innerHTML = '';
    casesData.forEach(c => {
      const item = document.createElement('div');
      const urgency = (c.urgency || 'Standard').toLowerCase();
      const pClass = (urgency === 'high') ? 'p-urgent' : (urgency === 'low' ? 'p-low' : 'p-normal');
      item.className = `case ${pClass}`;
      item.innerHTML = `
        <div class="case-top">
          <span class="case-name">${c.referral_id || c.id}</span>
          <span class="case-id">${c.resident_ref || ''}</span>
        </div>
        <div class="case-sub">${c.requested_action || c.summary || ''}</div>
        <div class="case-flags">
          <span class="tag ${urgency === 'high' ? 'danger' : 'info'}">${c.urgency || 'Standard'}</span>
          <span class="tag">${c.source || ''}</span>
        </div>
      `;
      el.caseload.appendChild(item);
    });
  }

  function renderPendingApprovals() {
    el.reviewStack.innerHTML = '';
    if (pendingApprovals.length === 0) {
      el.reviewEmpty.classList.remove('hidden');
      return;
    }
    el.reviewEmpty.classList.add('hidden');

    pendingApprovals.forEach(appr => {
      const card = document.createElement('div');
      const actionId = appr.action_id || appr.id;
      const layer = (appr.risk && appr.risk.gate_layer) ? appr.risk.gate_layer : 'score_threshold';
      const riskScore = (appr.risk && appr.risk.score !== undefined) ? Number(appr.risk.score).toFixed(3) : '0.400';
      const reasonText = (appr.risk && appr.risk.reason) ? appr.risk.reason : 'Exceeds threshold or requires mandatory supervisor review';
      const explanation = (appr.risk && appr.risk.gate_layer_explanation) ? appr.risk.gate_layer_explanation : 'Review required by authority policy.';

      card.className = 'approval';
      card.dataset.layer = layer;
      card.innerHTML = `
        <div class="approval-halt">
          <span class="halt-dot"></span>
          <span>HUMAN REVIEW REQUIRED (${layer.replace(/_/g, ' ').toUpperCase()})</span>
          <span class="halt-spacer">${appr.referral_id || appr.case_id || ''}</span>
        </div>
        <div class="approval-head">
          <div class="approval-title">${appr.description || appr.action_kind || 'Proposed Action'}</div>
          <div class="approval-meta">
            <span class="tag danger">Risk Score: ${riskScore}</span>
            <span class="tag">Threshold: τ = ${config ? config.threshold : 0.4}</span>
            <span class="tag info">Task: ${appr.task_id || 'N/A'}</span>
          </div>
        </div>
        <div class="approval-why">
          <b>Why this requires human decision:</b>
          <span>${reasonText}</span>
          <em>${explanation}</em>
        </div>
        <div class="approval-foot">
          <div class="approval-buttons">
            <button class="btn btn-primary btn-approve" data-id="${actionId}">Approve & Execute</button>
            <button class="btn btn-ghost btn-edit" data-id="${actionId}">Edit Payload</button>
            <div class="spacer"></div>
            <button class="btn btn-danger btn-reject" data-id="${actionId}">Reject Action</button>
          </div>
        </div>
      `;

      card.querySelector('.btn-approve').addEventListener('click', () => decideApproval(actionId, 'approve'));
      card.querySelector('.btn-reject').addEventListener('click', () => {
        const reason = prompt('Reason for rejection (recorded in audit ledger):', 'Action rejected by caseworker');
        if (reason !== null && reason.trim()) {
          decideApproval(actionId, 'reject', null, reason.trim());
        }
      });
      card.querySelector('.btn-edit').addEventListener('click', () => openEditModal(appr));

      el.reviewStack.appendChild(card);
    });
  }

  async function decideApproval(actionId, decision, payload = null, reason = '') {
    if (!actionId) {
      showToast('Missing action ID', 'error');
      return;
    }
    try {
      await api(`/api/approvals/${actionId}`, {
        method: 'POST',
        body: JSON.stringify({
          decision: decision,
          actor: el.actorInput.value || 'j.alvarez',
          payload: payload,
          reason: reason || (decision === 'approve' ? 'Approved by caseworker' : 'Rejected by caseworker')
        })
      });
      showToast(`Action ${decision}d`, 'success');
      pollState();
    } catch (err) {
      showToast(err.message, 'error');
    }
  }

  function openEditModal(action) {
    currentEditAction = action;
    el.editPayload.value = JSON.stringify(action.payload || {}, null, 2);
    el.editReason.value = '';
    el.editError.classList.add('hidden');
    el.editModal.classList.remove('hidden');
  }

  function closeEditModal() {
    el.editModal.classList.add('hidden');
    currentEditAction = null;
  }

  function renderActions() {
    el.actionsList.innerHTML = '';
    if (actionsData.length === 0) {
      el.actionsList.innerHTML = '<p class="dim" style="padding: 16px;">No actions recorded yet. Start a morning run or load an existing run.</p>';
      return;
    }
    const filtered = actionsData.filter(a => {
      if (activeActionFilter === 'all') return true;
      if (activeActionFilter === 'gated') return a.status === 'pending' || a.event === 'action_gated';
      if (activeActionFilter === 'executed') return a.executed === true || a.status === 'auto_executed' || a.status === 'approved';
      if (activeActionFilter === 'blocked') return a.executed === false || a.status === 'rejected' || a.status === 'refused_and_escalated';
      return true;
    });

    filtered.forEach(a => {
      const row = document.createElement('div');
      row.className = 'action-row';
      const statusKey = (a.status || 'auto_executed').replace(/-/g, '_');
      const statusClass = `st-${statusKey}`;
      row.innerHTML = `
        <div class="action-case">${a.case_id}</div>
        <div class="action-desc">
          ${a.description}
          <small>${a.detail || ''}</small>
        </div>
        <div class="action-score">${a.score !== undefined ? Number(a.score).toFixed(3) : '0.000'}</div>
        <div class="action-status"><span class="st ${statusClass}">${a.status}</span></div>
      `;
      el.actionsList.appendChild(row);
    });
  }

  async function loadAuditLedger(runId) {
    const target = runId || el.auditRunSelect.value || 'latest';
    try {
      const res = await api(`/api/runs/${target}/ledger`);
      renderAudit(res);
    } catch (err) {
      showToast(err.message, 'error');
    }
  }

  function renderAudit(data) {
    el.auditList.innerHTML = '';
    const ver = data.verification || {};
    const entryCount = data.entries ? data.entries.length : 0;
    el.chainStatus.className = `chain-status show ${ver.valid ? 'ok' : 'fail'}`;
    el.chainStatus.innerHTML = `
      <strong>${ver.valid ? '✅ Hash Chain Verified' : '❌ Hash Chain Broken'}</strong> &mdash; 
      <span>${entryCount} entries verified with 0 tampering.</span>
      <code>Final Hash: ${ver.final_hash || 'N/A'}</code>
    `;

    if (!data.entries || data.entries.length === 0) {
      el.auditList.innerHTML = '<p class="dim" style="padding: 16px;">No ledger entries found for this run.</p>';
      return;
    }

    data.entries.forEach((e, idx) => {
      const row = document.createElement('div');
      row.className = 'ledger-entry';
      const time = e.logged_at || e.timestamp || e.finished_at || e.proposed_at || '';
      const timeDisplay = time ? time.substring(11, 19) : '';
      let summaryText = e.description || e.action_description || e.detail || e.resolution_detail || '';
      if (!summaryText) {
        if (e.record_type === 'run_finished') {
          summaryText = `Run completed: ${e.stats ? e.stats.total_referrals : 12} referrals triaged, ${e.stats ? e.stats.auto_executed : 0} auto-executed, ${e.stats ? e.stats.refused : 0} refused/escalated.`;
        } else if (e.record_type === 'run_started') {
          summaryText = `Run started by ${e.actor || 'operator'} across ${e.referral_count || 12} referrals.`;
        } else if (e.record_type === 'injection_quarantined') {
          summaryText = `Injection quarantined: ${e.fields ? e.fields.join(', ') : ''} matched ${e.patterns ? e.patterns.join(', ') : ''}.`;
        } else {
          summaryText = JSON.stringify(e);
        }
      }

      row.innerHTML = `
        <div class="ledger-top">
          <span class="ledger-seq">#${idx + 1}</span>
          <span class="tag info">${e.record_type || 'entry'}</span>
          ${e.resolution ? `<span class="tag ${e.resolution === 'auto_executed' ? 'good' : 'warn'}">${e.resolution}</span>` : ''}
          ${e.referral_id ? `<span class="mono bold">${e.referral_id}</span>` : ''}
          ${timeDisplay ? `<span class="mono dim">${timeDisplay}</span>` : ''}
        </div>
        <div class="ledger-body">${summaryText}</div>
        <div class="ledger-hash">entry_hash: <code>${e.entry_hash ? e.entry_hash.substring(0, 16) + '...' : 'none'}</code> &bull; prev: <code>${e.prev_hash ? e.prev_hash.substring(0, 16) + '...' : 'genesis'}</code></div>
        <details class="ledger-detail">
          <summary>View raw JSON record</summary>
          <pre class="code">${JSON.stringify(e, null, 2)}</pre>
        </details>
      `;
      el.auditList.appendChild(row);
    });
  }

  async function verifyCurrentChain() {
    const runId = el.auditRunSelect.value;
    if (!runId) return;
    try {
      const res = await api(`/api/runs/${runId}/verify`);
      if (res.valid) {
        showToast(`Chain verified: ${res.records} entries intact`, 'success');
      } else {
        showToast(`Chain INVALID: ${res.error}`, 'error');
      }
      loadAuditLedger(runId);
    } catch (err) {
      showToast(err.message, 'error');
    }
  }

  function renderGuardrails(data) {
    el.guardrailsBody.innerHTML = `
      <div class="verdict ${data.unreachable_tasks && data.unreachable_tasks.length === 0 ? 'ok' : 'fail'}">
        <b>${data.verdict}</b> &mdash; Review Threshold: τ = ${data.threshold} | Hard Blocked Action Types: ${data.hard_blocked_actions.length}
      </div>
      <table class="grid">
        <thead>
          <tr>
            <th>Task ID</th>
            <th>Order</th>
            <th>Default Action</th>
            <th style="text-align:right;">Base Risk</th>
            <th>Reachability</th>
          </tr>
        </thead>
        <tbody>
          ${(data.tasks || []).map(t => `
            <tr>
              <td><code>${t.task_id}</code></td>
              <td>${t.order}</td>
              <td>${t.default_action_type || t.default_action_kind}</td>
              <td class="num">${Number(t.base || 0).toFixed(2)}</td>
              <td>${t.can_gate_with_signals ? '<span class="tag good">Reachable</span>' : '<span class="tag warn">Static</span>'}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    `;
  }

  async function searchPolicy() {
    const q = (el.policyQ.value || '').trim();
    if (!q) return;
    el.policyBody.innerHTML = '<p class="dim" style="padding:16px;">Searching policy knowledge base with RRF hybrid retrieval...</p>';
    try {
      const res = await api(`/api/policy/search?q=${encodeURIComponent(q)}`);
      el.policyBody.innerHTML = '';
      if (!res.results || res.results.length === 0) {
        el.policyBody.innerHTML = '<p class="dim" style="padding:16px;">No matching policy clauses found.</p>';
        return;
      }
      res.results.forEach(r => {
        const hit = document.createElement('div');
        hit.className = 'policy-hit';
        hit.innerHTML = `
          <div class="policy-hit-top">
            <span class="policy-clause">Clause ${r.clause_id || 'N/A'}</span>
            <span class="badge">RRF: ${r.rrf_score.toFixed(4)}</span>
          </div>
          <div class="policy-path">${r.section_path || ''}</div>
          <div class="policy-content">${r.content.replace(/\n/g, '<br/>')}</div>
        `;
        el.policyBody.appendChild(hit);
      });
    } catch (err) {
      el.policyBody.innerHTML = `<p class="error" style="padding:16px;">Search failed: ${err.message}</p>`;
    }
  }

  let traceEventsList = [];

  function renderTrace() {
    if (!el.trace) return;
    el.trace.innerHTML = '';
    traceEventsList.forEach(event => {
      const item = document.createElement('div');
      const timeStr = event._timeStr || (new Date()).toLocaleTimeString();
      let kClass = 'k-ok';
      if (event.event && event.event.includes('gated')) kClass = 'k-gate';
      if (event.event && (event.event.includes('refused') || event.event.includes('error'))) kClass = 'k-block';
      if (event.event && event.event.includes('referral')) kClass = 'k-case';
      if (event.event && event.event.includes('decision')) kClass = 'k-decide';

      item.className = `trace-item ${kClass}`;
      item.innerHTML = `
        <span class="trace-time">${timeStr}</span>
        <div class="trace-text">
          <b>${event.event}</b>
          <span class="dim">${event.payload && event.payload.detail ? event.payload.detail : (event.payload && event.payload.referral_id ? event.payload.referral_id : '')}</span>
          ${el.traceVerbose && el.traceVerbose.checked ? `<pre class="code">${JSON.stringify(event.payload || {}, null, 2)}</pre>` : ''}
        </div>
      `;
      el.trace.appendChild(item);
    });
    el.trace.scrollTop = el.trace.scrollHeight;
  }

  function appendTrace(event) {
    const now = new Date();
    const timeStr = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}:${String(now.getSeconds()).padStart(2, '0')}`;
    event._timeStr = timeStr;
    traceEventsList.push(event);
    renderTrace();
  }

  // --- Assistant Chatbot ---
  let chatHistory = [];

  function formatMarkdown(text) {
    if (!text) return '';
    let escaped = text
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
    
    escaped = escaped.replace(/^### (.*$)/gim, '<h4 style="margin:6px 0 4px;font-size:13.5px;color:var(--text);">$1</h4>');
    escaped = escaped.replace(/^## (.*$)/gim, '<h4 style="margin:8px 0 4px;font-size:14px;color:var(--text);">$1</h4>');
    escaped = escaped.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
    escaped = escaped.replace(/\*(.*?)\*/g, '<em>$1</em>');
    escaped = escaped.replace(/`([^`]+)`/g, '<code style="background:var(--surface-3);padding:1px 4px;border-radius:3px;font-family:var(--mono);font-size:11.5px;">$1</code>');
    
    const lines = escaped.split('\n');
    let html = '';
    let inList = false;
    for (let line of lines) {
      if (line.startsWith('- ') || line.startsWith('* ')) {
        if (!inList) { html += '<ul style="margin:4px 0 6px 18px;padding:0;">'; inList = true; }
        html += `<li>${line.substring(2)}</li>`;
      } else if (/^\d+\.\s/.test(line)) {
        if (!inList) { html += '<ol style="margin:4px 0 6px 18px;padding:0;">'; inList = true; }
        html += `<li>${line.replace(/^\d+\.\s/, '')}</li>`;
      } else {
        if (inList) { html += '</ul>'; inList = false; }
        if (line.trim().length > 0) {
          html += `<p style="margin:4px 0;">${line}</p>`;
        }
      }
    }
    if (inList) html += '</ul>';
    return html;
  }

  async function sendChatMessage(promptText) {
    const text = (promptText || el.chatInput.value || '').trim();
    if (!text) return;
    el.chatInput.value = '';

    // Append user message
    const userBubble = document.createElement('div');
    userBubble.className = 'chat-bubble chat-user';
    userBubble.innerHTML = `
      <div class="chat-bubble-header">
        <span class="chat-author">You</span>
      </div>
      <div class="chat-bubble-body">
        <p>${text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')}</p>
      </div>
    `;
    el.chatMessages.appendChild(userBubble);

    // Typing indicator
    const typingBubble = document.createElement('div');
    typingBubble.className = 'chat-bubble chat-ai';
    typingBubble.id = 'chat-typing-indicator';
    typingBubble.innerHTML = `
      <div class="chat-typing">
        <span></span><span></span><span></span>
      </div>
    `;
    el.chatMessages.appendChild(typingBubble);
    el.chatMessages.scrollTop = el.chatMessages.scrollHeight;

    chatHistory.push({ role: 'user', content: text });

    try {
      const res = await api('/api/chat', {
        method: 'POST',
        body: JSON.stringify({
          query: text,
          history: chatHistory,
          run_id: (currentRunState && currentRunState.run_id) ? currentRunState.run_id : 'latest'
        })
      });

      const typing = document.getElementById('chat-typing-indicator');
      if (typing) typing.remove();

      const aiBubble = document.createElement('div');
      aiBubble.className = 'chat-bubble chat-ai';

      let sourcesHtml = '';
      if (res.sources && res.sources.length > 0) {
        sourcesHtml = `
          <div class="chat-sources">
            <span class="dim" style="font-size:10.5px;margin-right:2px;">Sources:</span>
            ${res.sources.map(s => `<span class="tag info">${s.title}</span>`).join(' ')}
          </div>
        `;
      }

      const modeBadge = (res.mode === 'llm') 
        ? '<span class="badge" style="background:#e0f2fe;color:#0284c7;">Gemini 3.6</span>'
        : '<span class="badge">Grounded</span>';

      aiBubble.innerHTML = `
        <div class="chat-bubble-header">
          <span class="chat-author">Assistant</span>
          ${modeBadge}
        </div>
        <div class="chat-bubble-body">
          ${formatMarkdown(res.reply)}
          ${sourcesHtml}
        </div>
      `;
      el.chatMessages.appendChild(aiBubble);
      chatHistory.push({ role: 'assistant', content: res.reply });
    } catch (err) {
      const typing = document.getElementById('chat-typing-indicator');
      if (typing) typing.remove();

      const errorBubble = document.createElement('div');
      errorBubble.className = 'chat-bubble chat-ai';
      errorBubble.innerHTML = `
        <div class="chat-bubble-header">
          <span class="chat-author">Assistant</span>
          <span class="tag danger">Error</span>
        </div>
        <div class="chat-bubble-body">
          <p style="color:var(--danger);">Could not retrieve answer: ${err.message}</p>
        </div>
      `;
      el.chatMessages.appendChild(errorBubble);
    }

    el.chatMessages.scrollTop = el.chatMessages.scrollHeight;
  }

  function clearChat() {
    chatHistory = [];
    el.chatMessages.innerHTML = `
      <div class="chat-bubble chat-ai">
        <div class="chat-bubble-header">
          <span class="chat-author">Assistant</span>
          <span class="badge">Grounded</span>
        </div>
        <div class="chat-bubble-body">
          <p>Chat cleared. Ask any question or select a suggestion chip above to clear your doubts!</p>
        </div>
      </div>
    `;
  }

  function setupEventListeners() {
    el.tabs.forEach(tab => {
      tab.addEventListener('click', () => {
        el.tabs.forEach(t => t.classList.remove('active'));
        el.panels.forEach(p => p.classList.remove('active'));
        tab.classList.add('active');
        activeTab = tab.dataset.tab;
        const targetPanel = document.getElementById(`panel-${activeTab}`);
        if (targetPanel) targetPanel.classList.add('active');
        if (activeTab === 'audit') {
          loadAuditLedger(el.auditRunSelect.value || 'latest');
        }
        if (activeTab === 'actions' && actionsData.length === 0) {
          loadActionsFromLatest();
        }
      });
    });

    if (el.actionFilters) {
      el.actionFilters.querySelectorAll('.pill').forEach(btn => {
        btn.addEventListener('click', () => {
          el.actionFilters.querySelectorAll('.pill').forEach(b => b.classList.remove('active'));
          btn.classList.add('active');
          activeActionFilter = btn.dataset.filter;
          renderActions();
        });
      });
    }

    el.btnStart.addEventListener('click', startRun);
    el.btnCancel.addEventListener('click', cancelRun);
    if (el.traceVerbose) el.traceVerbose.addEventListener('change', renderTrace);
    el.auditRunSelect.addEventListener('change', () => loadAuditLedger(el.auditRunSelect.value));

    el.btnLoadAudit.addEventListener('click', () => loadAuditLedger(el.auditRunSelect.value));
    el.btnVerify.addEventListener('click', verifyCurrentChain);
    el.btnGuardrails.addEventListener('click', loadGuardrails);
    el.btnPolicy.addEventListener('click', searchPolicy);
    el.policyQ.addEventListener('keydown', e => { if (e.key === 'Enter') searchPolicy(); });

    // Chatbot Event Listeners
    if (el.btnChatSend) el.btnChatSend.addEventListener('click', () => sendChatMessage());
    if (el.chatInput) el.chatInput.addEventListener('keydown', e => { if (e.key === 'Enter') sendChatMessage(); });
    if (el.btnClearChat) el.btnClearChat.addEventListener('click', clearChat);

    if (el.chatSuggestions) {
      el.chatSuggestions.querySelectorAll('.chip-btn').forEach(btn => {
        btn.addEventListener('click', () => {
          const prompt = btn.dataset.prompt;
          if (prompt) sendChatMessage(prompt);
        });
      });
    }

    el.editClose.addEventListener('click', closeEditModal);
    el.editCancel.addEventListener('click', closeEditModal);
    el.editSubmit.addEventListener('click', () => {
      const actionId = currentEditAction ? (currentEditAction.action_id || currentEditAction.id) : null;
      if (!actionId) return;
      let parsed = null;
      try {
        parsed = JSON.parse(el.editPayload.value);
      } catch (err) {
        el.editError.textContent = `Invalid JSON: ${err.message}`;
        el.editError.classList.remove('hidden');
        return;
      }
      const reason = el.editReason.value.trim();
      if (!reason) {
        el.editError.textContent = 'Reason is required for audit trail';
        el.editError.classList.remove('hidden');
        return;
      }
      decideApproval(actionId, 'edit', parsed, reason);
      closeEditModal();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

