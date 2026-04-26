const API = '';

let activeRunner = 'mycroft';

// Runtime config fetched from /api/config at startup
let _cfg = { argo_ui_url: '', argo_namespace: 'mycroft' };
async function _loadConfig() {
  try { _cfg = await api('/api/config'); } catch (_) {}
}
function _argoLink(wfName) {
  if (!_cfg.argo_ui_url || !wfName) return '';
  return `${_cfg.argo_ui_url}/workflows/${_cfg.argo_namespace}/${wfName}`;
}

// ── Top-level tab navigation ─────────────────────────────────────────────────

document.querySelectorAll('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
  });
});

function switchTab(name) {
  document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.toggle('active', c.id === 'tab-' + name));
}

// ── Right-panel sub-tabs ──────────────────────────────────────────────────────

function setRightTab(name) {
  document.querySelectorAll('.rtab').forEach(b => {
    b.classList.toggle('active', b.dataset.rtab === name);
  });
  document.querySelectorAll('.rtab-content').forEach(c => {
    c.classList.toggle('active', c.id === 'rtab-' + name);
  });
}

// ── Runner toggle ─────────────────────────────────────────────────────────────

function setRunner(runner) {
  activeRunner = runner;
  const btn = document.getElementById('runBtn');
  const previewBtn = document.getElementById('previewBtn');
  if (runner === 'forge') {
    btn.textContent = 'Run with Forge';
    previewBtn.style.display = 'none';
  } else {
    btn.textContent = 'Run Task';
    previewBtn.style.display = '';
  }
}

// ── API helpers ───────────────────────────────────────────────────────────────

async function api(path, opts) {
  const r = await fetch(API + path, opts);
  if (!r.ok) {
    const text = await r.text();
    throw new Error(`${r.status}: ${text}`);
  }
  return r.json();
}

function esc(s) {
  if (!s) return '';
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// ── Test Runner ───────────────────────────────────────────────────────────────

let pollTimer = null;

async function runTask() {
  const instruction = document.getElementById('instruction').value.trim();
  if (!instruction) return;

  const btn = document.getElementById('runBtn');
  btn.disabled = true;
  btn.textContent = 'Running...';

  const traceEl = document.getElementById('traceContent');
  traceEl.innerHTML = '<p class="empty">Submitting task...</p>';

  const statusEl = document.getElementById('traceStatus');
  statusEl.textContent = 'submitting';
  statusEl.className = 'status-badge status-pending';

  document.getElementById('queueStats').style.display = 'none';
  _stopTracePoll();

  try {
    if (activeRunner === 'forge') {
      await runForge(instruction);
    } else {
      await runMycroft(instruction);
    }
  } catch (e) {
    traceEl.innerHTML = `<p style="color:#da3633">Error: ${esc(e.message)}</p>`;
    statusEl.textContent = 'error';
    statusEl.className = 'status-badge status-failed';
  } finally {
    btn.disabled = false;
    btn.textContent = activeRunner === 'forge' ? 'Run with Forge' : 'Run Task';
  }
}

// ── Forge runner ──────────────────────────────────────────────────────────────

async function runForge(instruction) {
  const model = document.getElementById('model').value || 'qwen3:14b';
  const repo = document.getElementById('repo').value.trim();
  const systemPrompt = document.getElementById('systemPrompt').value.trim();

  if (!repo) throw new Error('Repo is required for Forge runs');

  const r = await api('/api/forge/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ instruction, repo, model, system_prompt: systemPrompt || null }),
  });

  if (r.run_id) {
    const statusEl = document.getElementById('traceStatus');
    statusEl.textContent = 'running';
    statusEl.className = 'status-badge status-running';
    document.getElementById('traceContent').innerHTML =
      '<p class="empty">Forge is cloning repo and running...</p>';
    pollForgeRun(r.run_id);
  }
}

function pollForgeRun(runId) {
  _stopTracePoll();
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const r = await api('/api/forge/runs/' + runId);
      renderForgeResult(r);
      if (r.status === 'completed' || r.status === 'failed') {
        clearInterval(pollTimer);
        pollTimer = null;
        const statusEl = document.getElementById('traceStatus');
        statusEl.textContent = r.status;
        statusEl.className = 'status-badge status-' + r.status;
      }
    } catch (e) { /* still running */ }
  }, 2000);
}

function renderForgeResult(r) {
  const el = document.getElementById('traceContent');
  const cards = [];

  if (r.status === 'running') {
    el.innerHTML = '<p class="empty">Forge is working... (cloning, running LLM calls)</p>';
    return;
  }

  if (r.error) {
    cards.push(`
      <div class="trace-card" style="border-left:3px solid #da3633">
        <div class="trace-card-header" onclick="this.parentElement.classList.toggle('expanded')">
          <span style="color:#da3633">Error: ${esc(r.error)}</span>
        </div>
        <div class="trace-card-body">${esc(r.stderr)}</div>
      </div>`);
  }

  if (r.git_diff) {
    cards.push(`
      <div class="trace-card tool-call expanded" onclick="this.classList.toggle('expanded')">
        <div class="trace-card-header">
          <span><span class="trace-tool-name">git diff</span> (${r.files_changed.length} file${r.files_changed.length !== 1 ? 's' : ''})</span>
          <span class="trace-meta">${r.git_diff.length} bytes</span>
        </div>
        <div class="trace-card-body">${esc(r.git_diff)}</div>
      </div>`);
  }

  if (r.files_changed && r.files_changed.length) {
    cards.push(`
      <div class="trace-card llm-response">
        <div class="trace-card-header">
          <span class="trace-content">Files changed: ${r.files_changed.join(', ')}</span>
        </div>
      </div>`);
  }

  if (r.stdout) {
    cards.push(`
      <div class="trace-card" onclick="this.classList.toggle('expanded')">
        <div class="trace-card-header">
          <span>Forge output</span>
          <span class="trace-meta">${r.stdout.length} chars</span>
        </div>
        <div class="trace-card-body">${esc(r.stdout)}</div>
      </div>`);
  }

  if (!cards.length) cards.push('<p class="empty">No changes made</p>');

  const statsEl = document.getElementById('queueStats');
  statsEl.style.display = 'flex';
  statsEl.innerHTML = `
    <span>Exit: <strong>${r.exit_code}</strong></span>
    <span>Duration: <strong>${r.duration_seconds.toFixed(1)}s</strong></span>
    <span>Files: <strong>${r.files_changed.length}</strong></span>
    <span>Status: <strong>${r.status}</strong></span>`;

  el.innerHTML = cards.join('');
}

// ── Mycroft runner ────────────────────────────────────────────────────────────

async function runMycroft(instruction) {
  const workflow = document.getElementById('workflow').value;
  const systemPrompt = document.getElementById('systemPrompt').value.trim();
  const maxTokens = document.getElementById('maxTokens').value;
  const temperature = document.getElementById('temperature').value;
  const maxIterations = document.getElementById('maxIterations').value;
  const gatherModel = document.getElementById('gatherModel').value;
  const writeModel = document.getElementById('writeModel').value;

  // Send tool override only when not all tools are checked
  const allCbs = [...document.querySelectorAll('.tool-cb')];
  const checkedValues = allCbs.filter(cb => cb.checked).map(cb => cb.value);
  const toolsOverride = checkedValues.length < allCbs.length ? checkedValues : null;

  const body = {
    workflow,
    instruction,
    repo: document.getElementById('repo').value.trim(),
    system_prompt: systemPrompt || null,
    notify: document.getElementById('notifyRun').checked,
  };
  if (maxTokens) body.max_tokens = parseInt(maxTokens);
  if (temperature) body.temperature = parseFloat(temperature);
  if (maxIterations) body.max_iterations = parseInt(maxIterations);
  if (gatherModel) body.gather_model = gatherModel;
  if (writeModel) body.write_model = writeModel;
  if (toolsOverride) body.tools_override = toolsOverride;

  const r = await api('/api/tasks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });

  if (r.task_id) {
    document.getElementById('traceStatus').textContent = 'running';
    document.getElementById('traceStatus').className = 'status-badge status-running';
    loadRightTasks();
    document.getElementById('traceTaskSelect').value = r.task_id;
    _startTracePoll(r.task_id);
  }
}

let _tracePollTimer = null;
let _tracePollTaskId = null;

function _startTracePoll(taskId) {
  if (_tracePollTimer && _tracePollTaskId === taskId) return;
  _stopTracePoll();
  _tracePollTaskId = taskId;
  _tracePollTimer = setInterval(async () => {
    try {
      const task = await api('/api/tasks/' + taskId);
      const conv = await api('/api/tasks/' + taskId + '/conversation').catch(() => null);
      renderTrace(conv ? conv.messages : [], task);
      document.getElementById('traceStatus').textContent = task.status;
      document.getElementById('traceStatus').className = 'status-badge status-' + task.status;
      if (task.status !== 'running' && task.status !== 'pending') {
        _stopTracePoll();
        loadRightTasks();
        if (task.status === 'completed') loadReports();
      }
    } catch (e) { /* task may not have conversation yet */ }
  }, 3000);
}

function _stopTracePoll() {
  if (_tracePollTimer) { clearInterval(_tracePollTimer); _tracePollTimer = null; }
  _tracePollTaskId = null;
}

async function _loadTraceForTask(taskId) {
  if (!taskId) return;
  _stopTracePoll();
  document.getElementById('traceStatus').textContent = '';
  document.getElementById('traceStatus').className = 'status-badge';
  document.getElementById('traceContent').innerHTML = '<p class="empty">Loading trace…</p>';
  try {
    const [task, conv] = await Promise.all([
      api('/api/tasks/' + taskId),
      api('/api/tasks/' + taskId + '/conversation').catch(() => null),
    ]);
    renderTrace(conv ? conv.messages : [], task);
    document.getElementById('traceStatus').textContent = task.status;
    document.getElementById('traceStatus').className = 'status-badge status-' + task.status;
    if (task.status === 'running' || task.status === 'pending') _startTracePoll(taskId);
  } catch (e) {
    document.getElementById('traceContent').innerHTML =
      `<p class="empty">Could not load trace: ${esc(e.message)}</p>`;
  }
}

async function setTraceTask(taskId) {
  if (!taskId) return;
  const sel = document.getElementById('traceTaskSelect');
  if (!Array.from(sel.options).some(o => o.value === taskId)) {
    try {
      _allTasks = await api('/api/tasks?limit=100');
      populateTraceDropdown(_allTasks);
      applyTaskFilter();
    } catch (e) {}
  }
  sel.value = taskId;
  await _loadTraceForTask(taskId);
}

async function onTraceTaskSelect() {
  const taskId = document.getElementById('traceTaskSelect').value;
  if (taskId) await _loadTraceForTask(taskId);
  else _stopTracePoll();
}

async function refreshTraceTab() {
  await loadRightTasks();
  const taskId = document.getElementById('traceTaskSelect').value;
  if (taskId) await _loadTraceForTask(taskId);
}

function populateTraceDropdown(tasks) {
  const sel = document.getElementById('traceTaskSelect');
  const current = sel.value;
  sel.innerHTML = '<option value="">— select task —</option>';
  (tasks || _allTasks || []).forEach(t => {
    const opt = document.createElement('option');
    opt.value = t.id;
    const preview = (t.config?.instruction || '').slice(0, 60);
    opt.textContent = `${t.id.slice(0, 8)} [${t.agent_type}] ${t.status}${preview ? ' — ' + preview : ''}`;
    sel.appendChild(opt);
  });
  if (current) sel.value = current;
}

function renderTrace(messages, task) {
  const el = document.getElementById('traceContent');
  if (!messages || !messages.length) {
    el.innerHTML = '<p class="empty">Waiting for agent to start...</p>';
    return;
  }

  const cards = [];
  let userMsgIndex = 0;

  for (const msg of messages) {
    if (msg.role === 'system') {
      // Collapsed by default — system prompt is rarely useful to read inline
      cards.push(`
        <div class="trace-card planning trace-system-prompt" onclick="this.classList.toggle('expanded')">
          <div class="trace-card-header">
            <span class="trace-meta">system prompt — click to expand</span>
            <span class="trace-meta">${(msg.content || '').length} chars</span>
          </div>
          <div class="trace-card-body">${esc(msg.content || '')}</div>
        </div>`);
      continue;
    }

    if (msg.role === 'assistant') {
      if (msg.tool_calls && msg.tool_calls.length) {
        for (const tc of msg.tool_calls) {
          const fn = tc.function || {};
          const name = fn.name || 'unknown';
          const args = fn.arguments || '';
          const isFileOp = ['patch', 'write', 'read', 'fs_search'].includes(name);
          cards.push(`
            <div class="trace-card ${isFileOp ? 'tool-call' : 'planning'}" onclick="this.classList.toggle('expanded')">
              <div class="trace-card-header">
                <span><span class="trace-tool-name">${esc(name)}</span></span>
                <span class="trace-meta">tool call</span>
              </div>
              <div class="trace-card-body">${esc(args)}</div>
            </div>`);
        }
      }
      if (msg.content) {
        cards.push(`
          <div class="trace-card llm-response" onclick="this.classList.toggle('expanded')">
            <div class="trace-card-header">
              <span class="trace-content">${esc(msg.content.slice(0, 80))}${msg.content.length > 80 ? '...' : ''}</span>
              <span class="trace-meta">response</span>
            </div>
            <div class="trace-card-body">${esc(msg.content)}</div>
          </div>`);
      }
    }

    if (msg.role === 'tool') {
      cards.push(`
        <div class="trace-card tool-call" onclick="this.classList.toggle('expanded')">
          <div class="trace-card-header">
            <span class="trace-meta">tool result</span>
            <span class="trace-meta">${(msg.content || '').length} chars</span>
          </div>
          <div class="trace-card-body">${esc((msg.content || '').slice(0, 2000))}</div>
        </div>`);
    }

    if (msg.role === 'user') {
      const isFirst = userMsgIndex === 0;
      userMsgIndex++;
      cards.push(`
        <div class="trace-card" onclick="this.classList.toggle('expanded')">
          <div class="trace-card-header">
            <span>${esc((msg.content || '').slice(0, 80))}</span>
            <span class="trace-meta">${isFirst ? 'instruction' : 'nudge'}</span>
          </div>
          <div class="trace-card-body">${esc(msg.content || '')}</div>
        </div>`);
    }
  }

  const newHtml = cards.join('') || '<p class="empty">No tool calls yet</p>';
  if (el.innerHTML === newHtml) return;

  // Save which card indices are expanded before wiping DOM
  const expanded = new Set();
  el.querySelectorAll('.trace-card').forEach((c, i) => {
    if (c.classList.contains('expanded')) expanded.add(i);
  });

  const prevScrollTop = el.scrollTop;
  const wasAtBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
  el.innerHTML = newHtml;

  // Restore expanded state
  if (expanded.size) {
    el.querySelectorAll('.trace-card').forEach((c, i) => {
      if (expanded.has(i)) c.classList.add('expanded');
    });
  }

  const autoScroll = document.getElementById('traceAutoScroll');
  if (autoScroll && autoScroll.checked && wasAtBottom) {
    el.scrollTop = el.scrollHeight;
  } else {
    el.scrollTop = prevScrollTop;
  }
}

// ── Prompt preview ────────────────────────────────────────────────────────────

const _WORKFLOW_AGENT = {
  'research-quick': 'researcher',
  'research-regular': 'researcher',
  'research-deep': 'researcher',
  'coder': 'coder',
};

async function previewPrompt() {
  const instruction = document.getElementById('instruction').value.trim();
  if (!instruction) return;

  const workflow = document.getElementById('workflow').value;
  const agentType = _WORKFLOW_AGENT[workflow] || 'researcher';

  try {
    const r = await api('/api/tasks/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        agent_type: agentType,
        instruction,
        model: document.getElementById('model').value || null,
      }),
    });

    const panel = document.getElementById('promptPanel');
    panel.style.display = 'block';
    panel.querySelector('#promptContent').innerHTML = `
      <div class="msg msg-system"><div class="role">System Prompt</div><pre>${esc(r.system_prompt)}</pre></div>
      <div class="msg msg-user"><div class="role">User Message</div><pre>${esc(r.user_message)}</pre></div>
      <p style="margin-top:8px;font-size:0.82em;color:#8b949e">Tools: ${r.tools.join(', ')} | Model: ${r.model}</p>`;
    const spEl = document.getElementById('systemPrompt');
    if (!spEl.value.trim()) spEl.value = r.system_prompt;
  } catch (e) {
    alert('Error: ' + e.message);
  }
}

// ── Tasks sub-tab ─────────────────────────────────────────────────────────────

let _allTasks = [];
let _taskListRefreshTimer = null;

async function loadRightTasks() {
  try {
    _allTasks = await api('/api/tasks?limit=100');
    applyTaskFilter();
    populateTraceDropdown(_allTasks);

    // Auto-refresh while any tasks are active
    const hasActive = _allTasks.some(t => t.status === 'running' || t.status === 'pending');
    if (hasActive && !_taskListRefreshTimer) {
      _taskListRefreshTimer = setInterval(async () => {
        _allTasks = await api('/api/tasks?limit=100').catch(() => _allTasks);
        applyTaskFilter();
        populateTraceDropdown(_allTasks);
        const stillActive = _allTasks.some(t => t.status === 'running' || t.status === 'pending');
        if (!stillActive) {
          clearInterval(_taskListRefreshTimer);
          _taskListRefreshTimer = null;
        }
      }, 5000);
    } else if (!hasActive && _taskListRefreshTimer) {
      clearInterval(_taskListRefreshTimer);
      _taskListRefreshTimer = null;
    }
  } catch (e) {
    document.getElementById('rightTaskList').innerHTML = '<p class="empty">Error loading tasks</p>';
  }
}

function _relativeTime(date) {
  const diff = Math.floor((Date.now() - date) / 1000);
  if (diff < 60) return diff + 's ago';
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  return Math.floor(diff / 86400) + 'd ago';
}

function toggleTaskCard(taskId) {
  const actions = document.getElementById('tca-' + taskId);
  if (actions) actions.style.display = actions.style.display === 'none' ? '' : 'none';
}

function _renderTaskList(tasks) {
  const el = document.getElementById('rightTaskList');
  if (!tasks.length) {
    el.innerHTML = '<p class="empty">No tasks</p>';
    return;
  }
  el.innerHTML = tasks.map(t => {
    const hasChain = t.config?.phase || t.config?.parent_task_id;
    const isActive = t.status === 'running' || t.status === 'pending';
    const isFailed = t.status === 'failed';
    const age = t.created_at ? _relativeTime(new Date(t.created_at)) : '';
    const preview = esc((t.config?.instruction || '').slice(0, 100));
    const workflow = esc(t.config?.workflow || '');
    const errMsg = isFailed ? esc((t.result?.error || '').slice(0, 120)) : '';
    const argoUrl = _argoLink(t.argo_workflow_name);
    return `
    <div class="task-card" id="tc-${t.id}">
      <div class="task-card-header" onclick="toggleTaskCard('${t.id}')">
        <span class="task-card-id">${t.id.slice(0, 8)}</span>
        <span class="task-card-agent">${esc(t.agent_type)}</span>
        ${workflow ? `<span class="task-card-workflow">${workflow}</span>` : ''}
        <span class="status-badge status-${t.status}">${t.status}</span>
        <span class="task-card-age">${age}</span>
      </div>
      ${preview ? `<div class="task-card-preview">${preview}</div>` : ''}
      ${errMsg ? `<div class="task-card-error">${errMsg}</div>` : ''}
      <div class="task-card-actions" id="tca-${t.id}" style="display:none">
        <button class="btn-tool-ctrl" onclick="viewReportTaskTrace('${t.id}')">Trace</button>
        <button class="btn-tool-ctrl" onclick="viewKBForTask('${t.id}')">KB</button>
        ${hasChain ? `<button class="btn-tool-ctrl" onclick="viewPipelineChain('${t.id}')">Pipeline</button>` : ''}
        ${argoUrl ? `<a class="btn-tool-ctrl" href="${argoUrl}" target="_blank" rel="noopener">Argo ↗</a>` : ''}
        ${isActive ? `<button class="btn-cancel" onclick="cancelTask('${t.id}')">Cancel</button>` : ''}
        <button class="btn-delete" onclick="deleteTask('${t.id}')">Delete</button>
      </div>
    </div>`;
  }).join('');
}

function applyTaskFilter() {
  const status = document.getElementById('taskFilterStatus').value;
  const ageMinutes = parseInt(document.getElementById('taskFilterAge').value) || 0;
  const q = document.getElementById('taskFilterSearch').value.toLowerCase();
  const cutoff = ageMinutes ? Date.now() - ageMinutes * 60 * 1000 : 0;

  let tasks = _allTasks;
  if (status) tasks = tasks.filter(t => t.status === status);
  if (cutoff) tasks = tasks.filter(t => t.created_at && new Date(t.created_at).getTime() >= cutoff);
  if (q) tasks = tasks.filter(t =>
    (t.config?.instruction || '').toLowerCase().includes(q) ||
    t.agent_type.toLowerCase().includes(q) ||
    t.id.toLowerCase().startsWith(q)
  );
  _renderTaskList(tasks);
}

async function viewRightConversation(taskId) {
  const panel = document.getElementById('rightConvPanel');
  const titleEl = document.getElementById('rightConvId');
  const contentEl = document.getElementById('rightConvContent');

  titleEl.textContent = taskId.slice(0, 8);
  panel.style.display = 'block';

  // Check for linked report (fire-and-forget)
  api('/api/reports?source_task_id=' + taskId + '&limit=1').then(reports => {
    if (reports && reports.length) {
      const r = reports[0];
      const banner = document.createElement('div');
      banner.className = 'xlink-banner';
      banner.textContent = '📄 Report: ' + r.title;
      banner.onclick = () => {
        switchTab('reports');
        selectReport(r.id);
      };
      contentEl.prepend(banner);
    }
  }).catch(() => {});

  try {
    const r = await api('/api/tasks/' + taskId + '/conversation');
    const messages = r.messages || [];
    if (!messages.length) {
      contentEl.innerHTML = '<p class="empty">No conversation data</p>';
      return;
    }
    contentEl.innerHTML = messages.map(m => {
      let content = m.content || '';
      if (m.tool_calls) {
        content += '\n\nTool calls:\n' + m.tool_calls.map(tc =>
          tc.function.name + '(' + tc.function.arguments.slice(0, 200) + ')'
        ).join('\n');
      }
      return `<div class="msg msg-${m.role}"><div class="role">${m.role}</div><pre>${esc(content)}</pre></div>`;
    }).join('');
  } catch (e) {
    contentEl.innerHTML = '<p class="empty">No conversation data yet</p>';
  }
}

async function deleteTask(taskId) {
  try {
    await api('/api/tasks/' + taskId, { method: 'DELETE' });
    loadRightTasks();
  } catch (e) {
    alert('Error: ' + e.message);
  }
}

async function clearAllTasks() {
  if (!confirm('Delete all tasks? This cannot be undone.')) return;
  try {
    await api('/api/tasks', { method: 'DELETE' });
    document.getElementById('rightConvPanel').style.display = 'none';
    loadRightTasks();
  } catch (e) {
    alert('Error: ' + e.message);
  }
}

// ── Reports tab ───────────────────────────────────────────────────────────────

const md = window.markdownit ? window.markdownit() : null;

let _currentReport = null;
let _reportRawMode = false;
let _reportContent = '';
let _reportListCollapsed = false;

function toggleReportList() {
  _reportListCollapsed = !_reportListCollapsed;
  document.getElementById('reportList').style.display = _reportListCollapsed ? 'none' : '';
  document.getElementById('reportListChevron').textContent = _reportListCollapsed ? '▸' : '▾';
}

function setReportMobileView(view) {
  const split = document.getElementById('reportsSplit');
  if (!split) return;
  split.classList.toggle('show-list', view === 'list');
  split.classList.toggle('show-detail', view === 'detail');
  document.getElementById('reportMobileList').classList.toggle('active', view === 'list');
  document.getElementById('reportMobileDetail').classList.toggle('active', view === 'detail');
}

async function loadReports() {
  try {
    const reports = await api('/api/reports?limit=50');
    const el = document.getElementById('reportList');
    if (!reports.length) {
      el.innerHTML = '<p class="empty" style="padding:12px">No reports yet.</p>';
      return;
    }
    el.innerHTML = reports.map(r => {
      const wf = r.workflow || r.effort || '';
      const tier = wf.split('-').pop();
      const date = r.created_at ? new Date(r.created_at).toLocaleDateString() : '';
      return `
      <div class="report-list-item${_currentReport === r.id ? ' active' : ''}"
           onclick="selectReport('${r.id}')">
        <div class="report-list-title">${esc(r.title)}</div>
        <div class="report-list-meta">
          ${wf ? `<span class="effort-badge effort-${tier}">${wf}</span>` : ''}
          <span>${date}</span>
        </div>
      </div>`;
    }).join('');
  } catch (e) {
    document.getElementById('reportList').innerHTML = '<p class="empty" style="padding:12px">Error loading reports</p>';
  }
}

async function selectReport(id) {
  _currentReport = id;
  _reportRawMode = false;
  document.getElementById('reportRawToggle').textContent = 'Raw';
  document.getElementById('reportDetail').style.display = '';
  document.getElementById('reportEmpty').style.display = 'none';
  setReportMobileView('detail');
  document.getElementById('reportDetailRendered').style.display = '';
  document.getElementById('reportDetailRaw').style.display = 'none';
  document.getElementById('reportDetailTitle').textContent = 'Loading…';
  document.getElementById('reportDetailMeta').innerHTML = '';
  document.getElementById('reportDetailRendered').innerHTML = '<p class="empty">Loading…</p>';
  loadReports();

  try {
    const r = await api('/api/reports/' + id);
    _reportContent = r.content || '';

    document.getElementById('reportDetailTitle').textContent = r.title || id;

    const wf = r.workflow || r.effort || '';
    const date = r.created_at ? new Date(r.created_at).toLocaleString() : '';
    const models = r.models_used && Object.keys(r.models_used).length
      ? Object.entries(r.models_used).map(([k, v]) => `${k}: ${v}`).join(' · ')
      : '';
    const build = r.commit_sha ? `build: ${r.commit_sha}` : '';
    const taskLink = r.source_task_id
      ? `<span class="xlink-badge" onclick="viewReportTaskTrace('${r.source_task_id}')" title="View trace for source task" style="cursor:pointer;color:#58a6ff">view trace ↗</span>`
      : '';
    const metaParts = [
      wf ? `<span class="effort-badge effort-${wf.split('-').pop()}">${wf}</span>` : '',
      date ? `<span>${date}</span>` : '',
      models ? `<span>${esc(models)}</span>` : '',
      build ? `<span>${esc(build)}</span>` : '',
      taskLink,
    ].filter(Boolean);
    document.getElementById('reportDetailMeta').innerHTML = metaParts.join('<span class="meta-sep">·</span>');

    _renderReportContent();
  } catch (e) {
    document.getElementById('reportDetailTitle').textContent = 'Error';
    document.getElementById('reportDetailRendered').innerHTML = `<p class="empty">${esc(e.message)}</p>`;
  }
}

function _renderReportContent() {
  const rendered = document.getElementById('reportDetailRendered');
  const raw = document.getElementById('reportDetailRaw');
  if (_reportRawMode) {
    rendered.style.display = 'none';
    raw.style.display = '';
    raw.textContent = _reportContent;
  } else {
    raw.style.display = 'none';
    rendered.style.display = '';
    rendered.innerHTML = md ? md.render(_reportContent) : '<pre>' + esc(_reportContent) + '</pre>';
  }
}

function toggleReportRaw() {
  _reportRawMode = !_reportRawMode;
  document.getElementById('reportRawToggle').textContent = _reportRawMode ? 'Rendered' : 'Raw';
  _renderReportContent();
}

async function viewReportTaskTrace(taskId) {
  switchTab('trace');
  await setTraceTask(taskId);
}

async function deleteCurrentReport() {
  if (_currentReport === null || _currentReport === undefined) return;
  if (!confirm('Delete this report?')) return;
  try {
    await api('/api/reports/' + _currentReport, { method: 'DELETE' });
    _currentReport = null;
    document.getElementById('reportDetail').style.display = 'none';
    document.getElementById('reportEmpty').style.display = '';
    loadReports();
  } catch (e) {
    alert('Error: ' + e.message);
  }
}

async function clearAllReports() {
  if (!confirm('Delete ALL reports? This cannot be undone.')) return;
  try {
    await api('/api/reports', { method: 'DELETE' });
    _currentReport = null;
    document.getElementById('reportDetail').style.display = 'none';
    document.getElementById('reportEmpty').style.display = '';
    loadReports();
  } catch (e) {
    alert('Error: ' + e.message);
  }
}

// ── Tool allowlist helpers ────────────────────────────────────────────────────

function checkAllTools(checked) {
  document.querySelectorAll('.tool-cb').forEach(cb => { cb.checked = checked; });
}

function setDefaultTools() {
  const unchecked = new Set(['git_clone', 'git_checkout_branch']);
  document.querySelectorAll('.tool-cb').forEach(cb => {
    cb.checked = !unchecked.has(cb.value);
  });
}

// ── Workflow change handler ───────────────────────────────────────────────────

function onWorkflowChange() {
  const workflow = document.getElementById('workflow').value;
  document.getElementById('modelChain').style.display =
    (workflow === 'research-regular' || workflow === 'research-deep') ? '' : 'none';
  document.getElementById('repoGroup').style.display =
    (workflow === 'coder') ? '' : 'none';
}

// ── Models ────────────────────────────────────────────────────────────────────

let _modelList = [];

async function loadModels() {
  try {
    const models = await api('/api/models');
    _modelList = (Array.isArray(models) ? models : (models.data || []))
      .filter(m => m.downloaded !== false)
      .sort((a, b) => (a.name || a.id || '').localeCompare(b.name || b.id || ''));

    ['gatherModel', 'writeModel', 'agentModel'].forEach(selId => {
      const el = document.getElementById(selId);
      _modelList.forEach(m => {
        const opt = document.createElement('option');
        const name = m.name || m.id || '';
        opt.value = name;
        const tags = [];
        if (m.loaded) tags.push('loaded');
        if (m.parameter_count) tags.push(m.parameter_count);
        opt.textContent = name + (tags.length ? ' (' + tags.join(', ') + ')' : '');
        el.appendChild(opt);
      });
    });
  } catch (e) {
    console.warn('Failed to load models:', e);
  }
}

// ── Agents editor ──────────────────────────────────────────────────────────────

const AGENT_MANIFEST_TEMPLATE = `name: my-agent
role: "Describe what this agent does"
goal: "What outcome this agent produces"
model: qwen3:14b
backend: k8s
max_concurrent: 2
max_iterations: 10
tools:
  - web_search
  - web_read
`;

let _currentAgent = null;
let _agentTestTimer = null;
let _agentNames = [];
let _agentModels = {}; // name → default model from manifest

function _extractYamlField(yaml, field) {
  const m = yaml.match(new RegExp(`^${field}:\\s*(.+)`, 'm'));
  return m ? m[1].trim() : '';
}

function _updateYamlField(yaml, field, value) {
  const re = new RegExp(`^(${field}:\\s*).*`, 'm');
  return re.test(yaml) ? yaml.replace(re, `$1${value}`) : yaml + `\n${field}: ${value}`;
}

function _extractResourceField(yaml, field) {
  const m = yaml.match(new RegExp(`^  ${field}:\\s*["']?([^"'\\n]+)["']?\\s*$`, 'm'));
  return m ? m[1].trim() : '';
}

function _setResources(yaml, memory, cpu) {
  const parts = [];
  if (memory) parts.push(`  memory: ${memory}`);
  if (cpu) parts.push(`  cpu: "${cpu}"`);
  const block = parts.length ? `resources:\n${parts.join('\n')}` : '';
  const stripped = yaml.replace(/^resources:(?:\n(?:[ \t].*))*\n?/m, '').trimEnd();
  return block ? stripped + '\n' + block + '\n' : stripped + '\n';
}

// Known built-in group names — used to auto-prefix when loading old manifests
const _BUILTIN_GROUPS = new Set(['web', 'files', 'git', 'github', 'shell', 'todo']);
let _dbGroups = {};  // group -> [tool names], from /api/tools/groups

async function loadToolGroups() {
  try { _dbGroups = await api('/api/tools/groups'); } catch (_) {}
  // Populate the group autocomplete datalist with all known group names
  const dl = document.getElementById('schemaGroupList');
  if (dl) {
    const all = _allGroupNames();
    dl.innerHTML = [...all].sort().map(g => `<option value="${esc(g)}">`).join('');
  }
}

function _allGroupNames() {
  return new Set([..._BUILTIN_GROUPS, ...Object.keys(_dbGroups)]);
}

function _extractTools(yaml) {
  // Handle both "tools:\n  - x" and "tools: []" forms
  const inline = yaml.match(/^tools:\s*\[([^\]]*)\]/m);
  if (inline) return inline[1].split(',').map(s => s.trim()).filter(Boolean);
  const block = yaml.match(/^tools:\s*\n((?:  - .+\n?)*)/m);
  if (!block) return [];
  return block[1].replace(/^  - /gm, '').trim().split('\n').map(s => s.trim().replace(/^["']|["']$/g, '')).filter(Boolean);
}

function _setTools(yaml, tools) {
  // @ is a reserved YAML indicator — must quote tool names that start with it
  const block = tools.length
    ? 'tools:\n' + tools.map(t => `  - ${t.startsWith('@') ? `"${t}"` : t}`).join('\n') + '\n'
    : 'tools: []\n';
  const stripped = yaml
    .replace(/^tools:\s*\[.*\]\s*\n?/m, '')
    .replace(/^tools:\s*\n((?:[ \t].+\n?)*)?\n?/m, '')
    .trimEnd();
  return stripped + '\n' + block;
}

function _extractPerms(yaml, type) {
  const re = new RegExp(`^  ${type}:\\s*\\n((?:    - .+\\n?)*)`, 'm');
  const m = yaml.match(re);
  if (!m || !m[1].trim()) return '';
  return m[1].replace(/^    - /gm, '').trim();
}

function _setPerms(yaml, readLines, writeLines) {
  const mkList = lines => lines.trim().split('\n').map(l => l.trim()).filter(Boolean);
  const readPaths = mkList(readLines);
  const writePaths = mkList(writeLines);
  const sections = [];
  if (readPaths.length) sections.push('  read:\n' + readPaths.map(p => `    - ${p}`).join('\n'));
  if (writePaths.length) sections.push('  write:\n' + writePaths.map(p => `    - ${p}`).join('\n'));
  const permsBlock = sections.length ? 'permissions:\n' + sections.join('\n') + '\n' : '';
  const stripped = yaml.replace(/^permissions:(?:\n(?:[ \t].*))*\n?/m, '').trimEnd();
  return stripped + '\n' + permsBlock;
}

function _extractSystemPrompt(prompts) {
  const m = prompts.match(/SYSTEM_SUPPLEMENT\s*=\s*"""\s*([\s\S]*?)\s*"""/);
  return m ? m[1].trim() : '';
}

function _wrapSystemPrompt(name, content) {
  return `"""System prompt for ${name}."""\n\nSYSTEM_SUPPLEMENT = """\n${content}\n"""\n`;
}

async function loadAgents() {
  try {
    const agents = await api('/api/agents');
    _agentNames = agents.map(a => a.name);
    _agentModels = {};
    for (const a of agents) {
      const m = _extractYamlField(a.manifest || '', 'model');
      if (m) _agentModels[a.name] = m;
    }
    const el = document.getElementById('agentList');
    if (!agents.length) {
      el.innerHTML = '<p class="empty" style="padding:12px">No agents yet</p>';
      return;
    }
    el.innerHTML = agents.map(a => `
      <div class="editor-list-item${_currentAgent === a.name ? ' active' : ''}"
           onclick="selectAgent('${a.name}')">
        <span>${a.name}</span>
      </div>`).join('');
  } catch (e) {
    document.getElementById('agentList').innerHTML =
      '<p class="empty" style="padding:12px">Error loading agents</p>';
  }
}

async function selectAgent(name) {
  try {
    const a = await api('/api/agents/' + name);
    _currentAgent = name;
    document.getElementById('agentName').value = name;
    document.getElementById('agentManifest').value = a.manifest;
    document.getElementById('agentPrompts').value = a.prompts || '';

    // Structured fields
    const model = _extractYamlField(a.manifest, 'model');
    const agentModelEl = document.getElementById('agentModel');
    if (model && !agentModelEl.querySelector(`option[value="${CSS.escape(model)}"]`)) {
      const opt = document.createElement('option');
      opt.value = model; opt.textContent = model;
      agentModelEl.insertBefore(opt, agentModelEl.options[1] || null);
    }
    agentModelEl.value = model;
    document.getElementById('agentMaxIterations').value = _extractYamlField(a.manifest, 'max_iterations');
    document.getElementById('agentMemory').value = _extractResourceField(a.manifest, 'memory');
    document.getElementById('agentCpu').value = _extractResourceField(a.manifest, 'cpu');
    document.getElementById('agentSystemPrompt').value = _extractSystemPrompt(a.prompts || '');

    const tools = _extractTools(a.manifest);
    const allGroups = _allGroupNames();
    // Display group names with @ prefix; individual tools as-is
    document.getElementById('agentToolsList').value =
      tools.map(t => allGroups.has(t) ? `@${t}` : t).join('\n');

    document.getElementById('agentPermsRead').value = _extractPerms(a.manifest, 'read');
    document.getElementById('agentPermsWrite').value = _extractPerms(a.manifest, 'write');

    document.getElementById('agentTestInstruction').value = '';
    document.getElementById('agentTestContext').value = '';
    document.getElementById('agentTestStatus').style.display = 'none';
    document.getElementById('agentTestResult').style.display = 'none';
    document.getElementById('agentEditor').style.display = '';
    document.getElementById('agentEmpty').style.display = 'none';
    loadAgents();
  } catch (e) {
    alert('Error loading agent: ' + e.message);
  }
}

function newAgent() {
  _currentAgent = null;
  document.getElementById('agentName').value = '';
  document.getElementById('agentManifest').value = AGENT_MANIFEST_TEMPLATE;
  document.getElementById('agentPrompts').value = '';
  document.getElementById('agentModel').value = 'qwen3:14b';
  document.getElementById('agentMaxIterations').value = '10';
  document.getElementById('agentMemory').value = '';
  document.getElementById('agentCpu').value = '';
  document.getElementById('agentSystemPrompt').value = '';
  document.getElementById('agentToolsList').value = '';
  document.getElementById('agentPermsRead').value = '';
  document.getElementById('agentPermsWrite').value = '';
  document.getElementById('agentTestStatus').style.display = 'none';
  document.getElementById('agentTestResult').style.display = 'none';
  document.getElementById('agentEditor').style.display = '';
  document.getElementById('agentEmpty').style.display = 'none';
  document.getElementById('agentName').focus();
  loadAgents();
}

async function saveAgent() {
  const name = document.getElementById('agentName').value.trim();
  if (!name) { alert('Agent name is required'); return; }

  // Sync structured fields → raw textareas
  let manifest = document.getElementById('agentManifest').value;
  const model = document.getElementById('agentModel').value;
  const maxIter = document.getElementById('agentMaxIterations').value;
  if (model) manifest = _updateYamlField(manifest, 'model', model);
  if (maxIter) manifest = _updateYamlField(manifest, 'max_iterations', maxIter);
  const memory = document.getElementById('agentMemory').value.trim();
  const cpu = document.getElementById('agentCpu').value.trim();
  if (memory || cpu) manifest = _setResources(manifest, memory, cpu);

  const toolEntries = document.getElementById('agentToolsList').value
    .split('\n').map(s => s.trim()).filter(Boolean)
    // Normalize: @web stays as @web; old bare "web" style preserved if user types it
    .map(t => t);
  manifest = _setTools(manifest, toolEntries);

  const sysPrompt = document.getElementById('agentSystemPrompt').value.trim();
  const prompts = sysPrompt
    ? _wrapSystemPrompt(name, sysPrompt)
    : document.getElementById('agentPrompts').value;

  // Sync permissions textareas → manifest YAML
  const readLines = document.getElementById('agentPermsRead').value;
  const writeLines = document.getElementById('agentPermsWrite').value;
  if (readLines.trim() || writeLines.trim()) {
    manifest = _setPerms(manifest, readLines, writeLines);
  }

  document.getElementById('agentManifest').value = manifest;
  document.getElementById('agentPrompts').value = prompts;

  try {
    const result = await api('/api/agents/' + name, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ manifest, prompts }),
    });
    const canonical = result.name || name;
    _currentAgent = canonical;
    document.getElementById('agentName').value = canonical;
    loadAgents();
    loadWorkflowDropdown();
  } catch (e) {
    alert('Save failed: ' + e.message);
  }
}

async function cloneAgent() {
  if (!_currentAgent) return;
  const newName = prompt(`Clone "${_currentAgent}" as:`, _currentAgent + '-copy');
  if (!newName || !newName.trim()) return;
  const name = newName.trim();
  let manifest = document.getElementById('agentManifest').value;
  manifest = _updateYamlField(manifest, 'name', name);
  const prompts = document.getElementById('agentPrompts').value;
  try {
    const result = await api('/api/agents/' + name, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ manifest, prompts }),
    });
    await selectAgent(result.name || name);
  } catch (e) {
    alert('Clone failed: ' + e.message);
  }
}

async function deleteAgent() {
  if (!_currentAgent) return;
  if (!confirm(`Delete agent "${_currentAgent}"?`)) return;
  try {
    await api('/api/agents/' + _currentAgent, { method: 'DELETE' });
    _currentAgent = null;
    document.getElementById('agentEditor').style.display = 'none';
    document.getElementById('agentEmpty').style.display = '';
    loadAgents();
  } catch (e) {
    alert('Delete failed: ' + e.message);
  }
}

async function testAgent() {
  const rawInstruction = document.getElementById('agentTestInstruction').value.trim();
  const context = document.getElementById('agentTestContext').value.trim();

  const statusEl = document.getElementById('agentTestStatus');
  const resultEl = document.getElementById('agentTestResult');
  const contentEl = document.getElementById('agentTestContent');

  if (!_currentAgent) {
    alert('Select an agent first.');
    return;
  }
  if (!rawInstruction) {
    alert('Enter an instruction.');
    return;
  }

  const instruction = context
    ? `Original question: ${rawInstruction}\n\nPrevious step output:\n${context}`
    : rawInstruction;

  statusEl.style.display = '';
  statusEl.textContent = 'running';
  statusEl.className = 'status-badge status-running';
  resultEl.style.display = '';
  contentEl.innerHTML = '<p class="empty">Submitting...</p>';

  try {
    const r = await api('/api/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        agent_type: _currentAgent,
        instruction,
        model: document.getElementById('agentModel').value || null,
        max_iterations: 6,
        notify: document.getElementById('notifyTest').checked,
      }),
    });
    if (r.task_id) _pollAgentTest(r.task_id);
  } catch (e) {
    statusEl.textContent = 'error';
    statusEl.className = 'status-badge status-failed';
    contentEl.innerHTML = `<p style="color:#da3633">${esc(e.message)}</p>`;
  }
}

function _pollAgentTest(taskId) {
  if (_agentTestTimer) clearInterval(_agentTestTimer);
  const statusEl = document.getElementById('agentTestStatus');
  const contentEl = document.getElementById('agentTestContent');

  _agentTestTimer = setInterval(async () => {
    try {
      const task = await api('/api/tasks/' + taskId);
      if (task.status === 'completed' || task.status === 'failed') {
        clearInterval(_agentTestTimer);
        _agentTestTimer = null;
        statusEl.textContent = task.status;
        statusEl.className = 'status-badge status-' + task.status;
        const result = task.result || {};
        const text = result.summary || result.error || 'No output';
        contentEl.innerHTML = `<pre>${esc(text)}</pre>`;
      }
    } catch (e) { /* still running */ }
  }, 3000);
}

// ── Workflows editor ───────────────────────────────────────────────────────────

let _currentWorkflow = null;
let _pipelineSteps = [];

function _agentOpts(selected) {
  return '<option value="">Select agent...</option>' +
    _agentNames.map(n =>
      `<option value="${n}"${n === selected ? ' selected' : ''}>${n}</option>`
    ).join('');
}

function _modelOpts(selected, agentDefault) {
  const label = agentDefault ? `Default (${agentDefault})` : 'Default (agent config)';
  return `<option value="">${label}</option>` +
    _modelList.map(m => {
      const name = m.name || m.id || '';
      return `<option value="${name}"${name === selected ? ' selected' : ''}>${name}</option>`;
    }).join('');
}

function _renderPipelineSteps() {
  const el = document.getElementById('pipelineSteps');
  if (!_pipelineSteps.length) {
    el.innerHTML = '<p class="empty">No steps yet — add a step to build the pipeline.</p>';
    return;
  }
  el.innerHTML = _pipelineSteps.map((step, i) => {
    const agentDefault = _agentModels[step.agent] || '';
    return `
    <div class="pipeline-step">
      <div class="pipeline-step-header">
        <span class="pipeline-step-num">Step ${i + 1}</span>
        <div class="pipeline-step-acts">
          <button class="btn-step-act" onclick="movePipelineStep(${i},-1)" ${i === 0 ? 'disabled' : ''}>↑</button>
          <button class="btn-step-act" onclick="movePipelineStep(${i},1)" ${i === _pipelineSteps.length - 1 ? 'disabled' : ''}>↓</button>
          <button class="btn-step-act btn-step-remove" onclick="removePipelineStep(${i})">×</button>
        </div>
      </div>
      <div class="pipeline-step-body">
        <div class="form-group" style="margin-bottom:8px">
          <label>Step Description <span class="tip" data-tip="Why this step exists — injected into the agent prompt so it knows its role in the pipeline.">ⓘ</span></label>
          <input type="text" placeholder="e.g. Gather web research on the topic"
            value="${esc(step.description || '')}"
            onchange="updatePipelineStep(${i},'description',this.value)">
        </div>
        <div class="form-row" style="margin-bottom:8px">
          <div class="form-group" style="margin-bottom:0">
            <label>Agent</label>
            <select onchange="updatePipelineStep(${i},'agent',this.value)">${_agentOpts(step.agent)}</select>
          </div>
          <div class="form-group" style="margin-bottom:0">
            <label>Model override (optional) <span class="tip" data-tip="Leave blank to use the model defined in the agent's config. Set only when you want this step to use a non-standard model.">ⓘ</span></label>
            <select onchange="updatePipelineStep(${i},'model',this.value)">${_modelOpts(step.model, agentDefault)}</select>
          </div>
          <div class="form-group" style="margin-bottom:0;max-width:100px">
            <label>Max Iter <span class="tip" data-tip="Max tool-call iterations for this step. Overrides agent default.">ⓘ</span></label>
            <input type="number" min="1" max="50" placeholder="Default"
              value="${esc(String(step.max_iterations || ''))}"
              onchange="updatePipelineStep(${i},'max_iterations',this.value ? parseInt(this.value) : '')">
          </div>
        </div>
        <details${step.tools && step.tools.length ? ' open' : ''}>
          <summary style="font-size:0.82em;color:#8b949e;cursor:pointer">Tools Override <span class="tip" data-tip="Comma-separated tool names. Leave empty to use agent defaults.">ⓘ</span></summary>
          <input type="text" class="editor-textarea" style="margin-top:6px;height:auto;padding:6px 8px"
            placeholder="web_search, web_read, wiki_read (blank = agent defaults)"
            value="${esc((step.tools || []).join(', '))}"
            onchange="updatePipelineStep(${i},'tools',this.value.split(',').map(s=>s.trim()).filter(Boolean))">
        </details>
        <details${step.system_suffix ? ' open' : ''} style="margin-top:6px">
          <summary style="font-size:0.82em;color:#8b949e;cursor:pointer">System Prompt Suffix <span class="tip" data-tip="Appended to the agent's default system prompt. Use this to add constraints or output format rules for this step without replacing the agent's core behavior.">ⓘ</span></summary>
          <textarea class="editor-textarea" rows="3" style="margin-top:6px"
            onchange="updatePipelineStep(${i},'system_suffix',this.value)"
            placeholder="e.g. Always format your output as a JSON object.">${esc(step.system_suffix || '')}</textarea>
        </details>
        <details${step.prompt_override ? ' open' : ''} style="margin-top:6px">
          <summary style="font-size:0.82em;color:#e3b341;cursor:pointer">⚠ System Prompt Override <span class="tip" data-tip="Replaces the agent's entire system prompt. Use sparingly — the agent loses all its default behavior and tools context. Prefer 'System Prompt Suffix' for most cases.">ⓘ</span></summary>
          <textarea class="editor-textarea" rows="4" style="margin-top:6px"
            onchange="updatePipelineStep(${i},'prompt_override',this.value)"
            placeholder="Leave empty to use agent default">${esc(step.prompt_override || '')}</textarea>
        </details>
      </div>
    </div>`;
  }).join('');
}

function addPipelineStep() {
  _pipelineSteps.push({ agent: '', model: '', description: '', system_suffix: '', prompt_override: '', max_iterations: '', tools: [] });
  _renderPipelineSteps();
}

function removePipelineStep(i) {
  _pipelineSteps.splice(i, 1);
  _renderPipelineSteps();
}

function movePipelineStep(i, dir) {
  const j = i + dir;
  if (j < 0 || j >= _pipelineSteps.length) return;
  [_pipelineSteps[i], _pipelineSteps[j]] = [_pipelineSteps[j], _pipelineSteps[i]];
  _renderPipelineSteps();
}

function updatePipelineStep(i, field, value) {
  _pipelineSteps[i][field] = value;
  if (field === 'agent') _renderPipelineSteps(); // refresh model placeholder
}

let _customWorkflowNames = [];

async function loadWorkflowDropdown() {
  try {
    const workflows = await api('/api/workflows');
    const custom = workflows.filter(w => w.pipeline_json).sort((a, b) => a.name.localeCompare(b.name));
    _customWorkflowNames = custom.map(w => w.name);

    const sel = document.getElementById('workflow');
    const existing = sel.querySelector('optgroup[label="Custom"]');
    if (existing) existing.remove();
    if (!custom.length) return;

    const group = document.createElement('optgroup');
    group.label = 'Custom';
    custom.forEach(w => {
      const opt = document.createElement('option');
      opt.value = w.name;
      opt.textContent = w.name;
      group.appendChild(opt);
    });
    sel.appendChild(group);
  } catch (e) { /* ignore */ }
}

async function loadWorkflows() {
  try {
    const workflows = await api('/api/workflows');
    const el = document.getElementById('workflowList');
    if (!workflows.length) {
      el.innerHTML = '<p class="empty" style="padding:12px">No workflows yet</p>';
      return;
    }
    el.innerHTML = workflows.map(w => `
      <div class="editor-list-item${_currentWorkflow === w.name ? ' active' : ''}"
           onclick="selectWorkflow('${w.name}')">
        <span>${w.name}</span>
        ${w.pipeline_json ? '<span style="font-size:0.7em;color:#484f58">' + (w.pipeline_json.steps || []).length + ' steps</span>' : ''}
      </div>`).join('');
  } catch (e) {
    document.getElementById('workflowList').innerHTML =
      '<p class="empty" style="padding:12px">Error loading workflows</p>';
  }
}

async function selectWorkflow(name) {
  try {
    const w = await api('/api/workflows/' + name);
    _currentWorkflow = name;
    document.getElementById('workflowName').value = name;
    document.getElementById('workflowContent').value = w.content || '';
    document.getElementById('workflowDescription').value =
      (w.pipeline_json && w.pipeline_json.description) || '';
    _pipelineSteps = ((w.pipeline_json && w.pipeline_json.steps) || []).map(s => ({
      agent: s.agent || '',
      model: s.model || '',
      description: s.description || '',
      system_suffix: s.system_suffix || '',
      prompt_override: s.prompt_override || '',
      max_iterations: s.max_iterations || '',
      tools: s.tools || [],
    }));
    _renderPipelineSteps();
    document.getElementById('workflowEditor').style.display = '';
    document.getElementById('workflowEmpty').style.display = 'none';
    loadWorkflows();
    loadWorkflowDropdown();
    loadWorkflowRunHistory(name);
  } catch (e) {
    alert('Error loading workflow: ' + e.message);
  }
}

function newWorkflow() {
  _currentWorkflow = null;
  _pipelineSteps = [{ agent: '', model: '', description: '', system_suffix: '', prompt_override: '', max_iterations: '', tools: [] }];
  document.getElementById('workflowName').value = '';
  document.getElementById('workflowDescription').value = '';
  document.getElementById('workflowContent').value = '';
  _renderPipelineSteps();
  document.getElementById('workflowEditor').style.display = '';
  document.getElementById('workflowEmpty').style.display = 'none';
  document.getElementById('workflowName').focus();
  loadWorkflows();
  loadWorkflowDropdown();
}

async function saveWorkflow() {
  const name = document.getElementById('workflowName').value.trim();
  if (!name) { alert('Workflow name is required'); return; }

  const pipeline_json = {
    description: document.getElementById('workflowDescription').value.trim(),
    steps: _pipelineSteps.map(s => ({
      agent: s.agent,
      model: s.model || '',
      ...(s.description ? { description: s.description } : {}),
      ...(s.system_suffix ? { system_suffix: s.system_suffix } : {}),
      prompt_override: s.prompt_override || '',
      ...(s.max_iterations ? { max_iterations: parseInt(s.max_iterations) } : {}),
      ...(s.tools && s.tools.length ? { tools: s.tools } : {}),
    })),
  };
  const content = document.getElementById('workflowContent').value;

  try {
    await api('/api/workflows/' + name, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, pipeline_json }),
    });
    _currentWorkflow = name;
    loadWorkflows();
    loadWorkflowDropdown();
  } catch (e) {
    alert('Save failed: ' + e.message);
  }
}

async function cloneWorkflow() {
  if (!_currentWorkflow) return;
  const newName = prompt(`Clone "${_currentWorkflow}" as:`, _currentWorkflow + '-copy');
  if (!newName || !newName.trim()) return;
  const name = newName.trim();
  const pipeline_json = {
    description: document.getElementById('workflowDescription').value.trim(),
    steps: _pipelineSteps.map(s => ({
      agent: s.agent, model: s.model || '',
      ...(s.description ? { description: s.description } : {}),
      ...(s.system_suffix ? { system_suffix: s.system_suffix } : {}),
      prompt_override: s.prompt_override || '',
      ...(s.max_iterations ? { max_iterations: parseInt(s.max_iterations) } : {}),
      ...(s.tools && s.tools.length ? { tools: s.tools } : {}),
    })),
  };
  const content = document.getElementById('workflowContent').value;
  try {
    await api('/api/workflows/' + name, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, pipeline_json }),
    });
    _currentWorkflow = name;
    await selectWorkflow(name);
  } catch (e) {
    alert('Clone failed: ' + e.message);
  }
}

async function loadWorkflowRunHistory(name) {
  const el = document.getElementById('workflowRunHistory');
  el.innerHTML = '<p class="empty">Loading…</p>';
  try {
    const runs = await api('/api/workflows/' + name + '/runs?limit=20');
    if (!runs.length) { el.innerHTML = '<p class="empty">No runs yet</p>'; return; }
    // Group by root tasks only (no gather-phase dupes) — prefer 'write' and standalone
    const visible = runs.filter(r => {
      const phase = r.config?.phase || '';
      return phase !== 'gather' && !phase.startsWith('pipeline-step-') || r.config?.is_last_step;
    });
    el.innerHTML = (visible.length ? visible : runs).map(r => {
      const date = r.created_at ? new Date(r.created_at).toLocaleString() : '';
      const dur = r.completed_at && r.created_at
        ? Math.round((new Date(r.completed_at) - new Date(r.created_at)) / 1000) + 's'
        : '…';
      const phase = r.config?.phase || '';
      return `
        <div class="wf-run-row" onclick="viewRightConversation('${r.id}');switchTab('runner');setRightTab('tasks')">
          <span class="wf-run-date">${date}</span>
          <span class="wf-run-agent">${esc(r.agent_type)}</span>
          ${phase ? `<span class="wf-run-phase">${esc(phase)}</span>` : ''}
          <span class="status-badge status-${r.status}">${r.status}</span>
          <span class="wf-run-phase">${dur}</span>
        </div>`;
    }).join('');
  } catch (e) {
    el.innerHTML = `<p class="empty">Error: ${esc(e.message)}</p>`;
  }
}

function runWorkflowFromEditor(quick = false) {
  if (!_currentWorkflow) return;
  switchTab('runner');
  loadWorkflowDropdown().then(() => {
    const sel = document.getElementById('workflow');
    sel.value = _currentWorkflow;
    if (sel.value !== _currentWorkflow) {
      const opt = document.createElement('option');
      opt.value = _currentWorkflow;
      opt.textContent = _currentWorkflow;
      sel.appendChild(opt);
      sel.value = _currentWorkflow;
    }
    onWorkflowChange();
    const iterEl = document.getElementById('maxIterations');
    if (quick) {
      document.getElementById('advancedOptions').open = true;
      iterEl.value = '2';
    } else {
      iterEl.value = '';
    }
    document.getElementById('instruction').focus();
  });
}

async function deleteWorkflow() {
  if (!_currentWorkflow) return;
  if (!confirm(`Delete workflow "${_currentWorkflow}"?`)) return;
  try {
    await api('/api/workflows/' + _currentWorkflow, { method: 'DELETE' });
    _currentWorkflow = null;
    document.getElementById('workflowEditor').style.display = 'none';
    document.getElementById('workflowEmpty').style.display = '';
    loadWorkflows();
    loadWorkflowDropdown();
  } catch (e) {
    alert('Delete failed: ' + e.message);
  }
}

// ── Tool schemas editor ───────────────────────────────────────────────────────

let _currentSchema = null;

function _schemaListItem(s) {
  const badge = s.group ? `<span class="schema-group-badge">@${esc(s.group)}</span>` : '';
  return `<div class="editor-list-item${_currentSchema === s.name ? ' active' : ''}"
       onclick="selectSchema('${s.name}')">
    <span>${esc(s.name)}</span>
    <span style="display:flex;gap:6px;align-items:center">${badge}<span style="font-size:0.7em;color:#484f58">v${s.version}</span></span>
  </div>`;
}

async function loadSchemas() {
  try {
    const schemas = await api('/api/tools/schemas');
    const el = document.getElementById('schemaList');
    if (!schemas.length) {
      el.innerHTML = '<p class="empty" style="padding:12px">No schemas yet</p>';
      return;
    }
    // Group schemas by their tool_group, ungrouped last
    const grouped = {};
    const ungrouped = [];
    for (const s of schemas) {
      if (s.group) { (grouped[s.group] = grouped[s.group] || []).push(s); }
      else { ungrouped.push(s); }
    }
    const sections = [];
    for (const [grp, items] of Object.entries(grouped).sort()) {
      sections.push(`<div class="schema-group-header">@${esc(grp)}</div>`);
      sections.push(...items.map(s => _schemaListItem(s)));
    }
    if (ungrouped.length) {
      if (sections.length) sections.push(`<div class="schema-group-header">ungrouped</div>`);
      sections.push(...ungrouped.map(s => _schemaListItem(s)));
    }
    el.innerHTML = sections.join('');
  } catch (e) {
    document.getElementById('schemaList').innerHTML =
      '<p class="empty" style="padding:12px">Error loading schemas</p>';
  }
}

const SCHEMA_TEMPLATE = {
  type: 'function',
  function: {
    name: 'my_tool',
    description: 'What this tool does',
    parameters: {
      type: 'object',
      properties: {
        input: { type: 'string', description: 'The input value' },
      },
      required: ['input'],
    },
  },
};

function newSchema() {
  _currentSchema = null;
  const nameEl = document.getElementById('schemaEditorName');
  nameEl.value = '';
  nameEl.readOnly = false;
  document.getElementById('schemaVersion').value = '1.0.0';
  document.getElementById('schemaDbVersion').value = '—';
  document.getElementById('schemaChangelog').value = '';
  document.getElementById('schemaGroup').value = '';
  document.getElementById('schemaContent').value = JSON.stringify(SCHEMA_TEMPLATE, null, 2);
  document.getElementById('schemaHistory').innerHTML = '';
  document.getElementById('schemaEditor').style.display = '';
  document.getElementById('schemaEmpty').style.display = 'none';
  nameEl.focus();
  loadSchemas();
}

async function selectSchema(name) {
  try {
    const s = await api('/api/tools/schemas/' + name);
    _currentSchema = name;
    const nameEl = document.getElementById('schemaEditorName');
    nameEl.value = name;
    nameEl.readOnly = true;
    document.getElementById('schemaVersion').value = s.schema_version || '1.0.0';
    document.getElementById('schemaDbVersion').value = 'v' + s.version;
    document.getElementById('schemaChangelog').value = '';
    document.getElementById('schemaGroup').value = s.group || '';
    document.getElementById('schemaContent').value = JSON.stringify(s.schema, null, 2);
    document.getElementById('schemaEditor').style.display = '';
    document.getElementById('schemaEmpty').style.display = 'none';
    loadSchemas();
    loadSchemaHistory(name);
  } catch (e) {
    alert('Error loading schema: ' + e.message);
  }
}

async function loadSchemaHistory(name) {
  const el = document.getElementById('schemaHistory');
  el.innerHTML = '<p class="empty">Loading…</p>';
  try {
    const history = await api('/api/tools/schemas/' + name + '/history');
    if (!history.length) { el.innerHTML = '<p class="empty">No history</p>'; return; }
    el.innerHTML = history.map(h => `
      <div class="schema-history-row">
        <span class="schema-history-v">v${h.version}</span>
        <span class="schema-history-semver">${esc(h.schema_version)}</span>
        <span class="schema-history-log">${esc(h.changelog || '—')}</span>
        <span class="schema-history-by">${esc(h.updated_by)}</span>
        <span class="schema-history-date">${h.created_at ? new Date(h.created_at).toLocaleDateString() : ''}</span>
      </div>`).join('');
  } catch (e) {
    el.innerHTML = '<p class="empty">Error loading history</p>';
  }
}

async function saveSchema() {
  const name = document.getElementById('schemaEditorName').value.trim();
  if (!name) { alert('Tool name is required'); return; }
  let schema;
  try {
    schema = JSON.parse(document.getElementById('schemaContent').value);
  } catch (e) {
    alert('Invalid JSON: ' + e.message);
    return;
  }
  const schema_version = document.getElementById('schemaVersion').value.trim() || '1.0.0';
  const changelog = document.getElementById('schemaChangelog').value.trim();
  try {
    const r = await api('/api/tools/schemas/' + name, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ schema, schema_version, changelog, updated_by: 'ui', group: document.getElementById('schemaGroup').value.trim() }),
    });
    _currentSchema = name;
    document.getElementById('schemaEditorName').readOnly = true;
    document.getElementById('schemaDbVersion').value = 'v' + r.version;
    document.getElementById('schemaChangelog').value = '';
    loadToolGroups();
    loadSchemas();
    loadSchemaHistory(name);
  } catch (e) {
    alert('Save failed: ' + e.message);
  }
}

async function deleteSchemaSelected() {
  if (!_currentSchema) return;
  if (!confirm(`Delete all versions of schema "${_currentSchema}"?`)) return;
  try {
    await api('/api/tools/schemas/' + _currentSchema, { method: 'DELETE' });
    _currentSchema = null;
    document.getElementById('schemaEditorName').value = '';
    document.getElementById('schemaEditorName').readOnly = false;
    document.getElementById('schemaEditor').style.display = 'none';
    document.getElementById('schemaEmpty').style.display = '';
    loadSchemas();
  } catch (e) {
    alert('Delete failed: ' + e.message);
  }
}

// ── SSE + toast ───────────────────────────────────────────────────────────────

function showToast(msg, type = 'info', duration = 5000) {
  const container = document.getElementById('toast-container');
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  container.appendChild(el);
  setTimeout(() => el.remove(), duration);
}

function connectSSE() {
  const es = new EventSource('/api/events');

  es.addEventListener('task_update', e => {
    const d = JSON.parse(e.data);
    const id8 = d.task_id ? d.task_id.slice(0, 8) : '?';
    if (d.status === 'completed') {
      showToast(`Task ${id8} completed`, 'success');
    } else if (d.status === 'failed') {
      const hint = d.error ? `: ${d.error.slice(0, 80)}` : '';
      showToast(`Task ${id8} failed${hint}`, 'error', 8000);
    }
    loadRightTasks();
  });

  es.addEventListener('report_saved', e => {
    const d = JSON.parse(e.data);
    showToast(`Report saved: ${d.title ? d.title.slice(0, 50) : ''}`, 'success');
    loadReports();
  });

  es.onerror = () => {
    es.close();
    setTimeout(connectSSE, 5000);
  };
}

// ── Run Monitor ───────────────────────────────────────────────────────────────

let _runMonitorInterval = null;
let _runMonitorTaskId = null;

function closeRunMonitor() {
  if (_runMonitorInterval) { clearInterval(_runMonitorInterval); _runMonitorInterval = null; }
  _runMonitorTaskId = null;
  document.getElementById('rightPipelinePanel').style.display = 'none';
}

async function viewPipelineChain(taskId) {
  _runMonitorTaskId = taskId;
  document.getElementById('rightPipelinePanel').style.display = '';
  document.getElementById('rightPipelineContent').innerHTML =
    '<p class="empty" style="padding:12px">Loading…</p>';
  await _refreshRunMonitor();
}

async function _refreshRunMonitor() {
  const taskId = _runMonitorTaskId;
  if (!taskId) return;
  const content = document.getElementById('rightPipelineContent');

  try {
    const chain = await api('/api/tasks/' + taskId + '/pipeline');
    if (!chain || chain.length <= 1) {
      closeRunMonitor();
      if (chain && chain.length === 1) viewRightConversation(taskId);
      return;
    }

    const runId = chain.find(t => t.config?.run_id)?.config?.run_id;

    const [originalEntry, scratchEntry, ...stepOutputs] = await Promise.all([
      runId
        ? api('/api/kb/entry?path=' + encodeURIComponent(`/runs/${runId}/original`)).catch(() => null)
        : Promise.resolve(null),
      runId
        ? api('/api/kb/entry?path=' + encodeURIComponent(`/runs/${runId}/scratch`)).catch(() => null)
        : Promise.resolve(null),
      ...chain.map(t => api('/api/tasks/' + t.id + '/kb-result').catch(() => null)),
    ]);

    const anyActive = chain.some(t => t.status === 'running' || t.status === 'pending');
    if (anyActive && !_runMonitorInterval) {
      _runMonitorInterval = setInterval(_refreshRunMonitor, 5000);
    } else if (!anyActive) {
      clearInterval(_runMonitorInterval);
      _runMonitorInterval = null;
    }

    const overallStatus = chain.every(t => t.status === 'completed') ? 'completed'
      : chain.some(t => t.status === 'failed') ? 'failed'
      : anyActive ? 'running' : 'mixed';

    let html = `<div class="rm-header">
      <span class="rm-run-id">run: ${runId ? runId.slice(0, 8) : 'n/a'}</span>
      <span class="status-badge status-${overallStatus}">${overallStatus}</span>
      <button class="btn-tool-ctrl rm-refresh-btn" onclick="_refreshRunMonitor()" title="Refresh">↺</button>
    </div>`;

    html += `<details class="rm-section" open>
      <summary class="rm-label">Original Brief</summary>
      <pre class="rm-pre">${esc(originalEntry?.content || '(not available)')}</pre>
    </details>`;

    chain.forEach((t, i) => {
      const dur = t.completed_at && t.created_at
        ? Math.round((new Date(t.completed_at) - new Date(t.created_at)) / 1000) + 's' : '';
      const output = stepOutputs[i];
      const instruction = t.config?.instruction || '';
      const isActive = t.status === 'running' || t.status === 'pending';

      html += `<details class="rm-step"${isActive ? ' open' : ''}>
        <summary class="rm-step-summary">
          <span class="rm-step-n">Step ${i + 1}</span>
          <span class="rm-step-agent">${esc(t.agent_type)}</span>
          <span class="status-badge status-${t.status}">${t.status}</span>
          ${dur ? `<span class="rm-dur">${dur}</span>` : ''}
        </summary>
        <div class="rm-step-body">
          <details class="rm-subsection">
            <summary class="rm-sublabel">Instruction</summary>
            <pre class="rm-pre">${esc(instruction.slice(0, 4000))}${instruction.length > 4000 ? '\n…' : ''}</pre>
          </details>
          <div class="rm-actions">
            <button class="btn-tool-ctrl" onclick="viewReportTaskTrace('${t.id}')">Trace ↗</button>
          </div>
          <details class="rm-subsection"${output ? ' open' : ''}>
            <summary class="rm-sublabel">Output</summary>
            ${output
              ? `<pre class="rm-pre">${esc((output.content || '(empty)').slice(0, 4000))}${(output.content || '').length > 4000 ? '\n…' : ''}</pre>`
              : `<p class="rm-pending">${isActive ? 'In progress…' : '(none yet)'}</p>`}
          </details>
        </div>
      </details>`;
    });

    html += `<details class="rm-section">
      <summary class="rm-label">Scratch</summary>
      <pre class="rm-pre">${esc(scratchEntry?.content || '(empty)')}</pre>
    </details>`;

    content.innerHTML = html;

  } catch(e) {
    content.innerHTML = `<p class="empty" style="padding:12px">Error: ${esc(e.message)}</p>`;
  }
}

async function viewStepOutput(taskId, btn) {
  const step = btn.closest('.pipeline-chain-step');
  let outputEl = step.querySelector('.step-output');
  if (outputEl) { outputEl.remove(); return; }

  btn.textContent = '…';
  try {
    const r = await api('/api/tasks/' + taskId + '/kb-result');
    outputEl = document.createElement('div');
    outputEl.className = 'step-output';
    outputEl.style.cssText = 'grid-column:1/-1;padding:8px 12px;background:#0d1117;border-top:1px solid #21262d;font-size:0.8em;white-space:pre-wrap;max-height:200px;overflow-y:auto;color:#c9d1d9';
    outputEl.textContent = r.content || '(empty)';
    step.insertAdjacentElement('afterend', outputEl);
    btn.textContent = 'Hide';
  } catch (e) {
    btn.textContent = 'Output';
    showToast('No KB output for this step', 'info');
  }
}

async function cancelTask(taskId) {
  if (!confirm('Cancel this task?')) return;
  try {
    await api('/api/tasks/' + taskId + '/cancel', { method: 'POST' });
    loadRightTasks();
  } catch (e) {
    alert('Error: ' + e.message);
  }
}

// ── Logs tab ──────────────────────────────────────────────────────────────────

let _logAutoRefreshTimer = null;
let _logSince = null;
let _allLogLines = [];

async function loadLogs(append = false) {
  const level = document.getElementById('logLevel').value;
  const logger = document.getElementById('logLogger').value.trim();
  const q = document.getElementById('logSearch').value.trim();

  const params = new URLSearchParams({ limit: 500 });
  if (level) params.set('level', level);
  if (logger) params.set('logger', logger);
  if (q) params.set('q', q);
  if (append && _logSince != null) params.set('since', _logSince);

  try {
    const records = await api('/api/logs?' + params.toString());
    if (!append) {
      _allLogLines = records;
    } else {
      _allLogLines = _allLogLines.concat(records);
      if (_allLogLines.length > 2000) _allLogLines = _allLogLines.slice(-2000);
    }
    if (records.length) _logSince = records[records.length - 1].ts;
    _renderLogs(_allLogLines);
  } catch (e) {
    console.warn('Failed to load logs:', e);
  }
}

function _renderLogs(records) {
  const el = document.getElementById('logLines');
  if (!records.length) {
    el.innerHTML = '<p class="empty" style="padding:12px">No log records match the current filters.</p>';
    return;
  }

  el.innerHTML = records.map(r => {
    const ts = new Date(r.ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const lvl = r.level || 'INFO';
    return `<div class="log-line">
      <span class="log-ts">${ts}</span>
      <span class="log-lvl log-lvl-${lvl}">${lvl}</span>
      <span class="log-src" title="${esc(r.logger)}">${esc(r.logger)}</span>
      <span class="log-msg">${esc(r.msg)}</span>
    </div>`;
  }).join('');

  if (document.getElementById('logAutoScroll').checked) {
    el.scrollTop = el.scrollHeight;
  }
}

function applyLogFilters() {
  _logSince = null;
  loadLogs(false);
}

function clearLogView() {
  _allLogLines = [];
  _logSince = null;
  document.getElementById('logLines').innerHTML = '';
}

function toggleLogAutoRefresh() {
  const enabled = document.getElementById('logAutoRefresh').checked;
  if (enabled) {
    _logAutoRefreshTimer = setInterval(() => loadLogs(true), 3000);
  } else {
    if (_logAutoRefreshTimer) clearInterval(_logAutoRefreshTimer);
    _logAutoRefreshTimer = null;
  }
}

// Start auto-refresh when logs tab is activated
document.querySelectorAll('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    if (btn.dataset.tab === 'logs') {
      loadLogs(false);
      if (document.getElementById('logAutoRefresh').checked && !_logAutoRefreshTimer) {
        _logAutoRefreshTimer = setInterval(() => loadLogs(true), 3000);
      }
    } else {
      if (_logAutoRefreshTimer) {
        clearInterval(_logAutoRefreshTimer);
        _logAutoRefreshTimer = null;
      }
    }
  });
});

// ── KB Explorer ───────────────────────────────────────────────────────────────

let _kbPath = '/';
let _kbEntry = null;
let _kbEditMode = false;
let _kbTasksExpanded = false;
let _kbTasksAge = '20';
let _kbChildren = [];
let _kbTaskFilterId = null;
let _kbTaskFilterTimer = null;

// Navigate to a path and load its children
async function kbNavigate(path) {
  _kbPath = path;
  _kbEntry = null;
  _kbEditMode = false;
  _kbTaskFilterId = null;
  document.getElementById('kbFilterTask').value = '';
  document.getElementById('kbFilterTaskClear').style.display = 'none';
  document.getElementById('kbDetail').style.display = 'none';
  document.getElementById('kbEmpty').style.display = 'flex';
  renderKBBreadcrumb(path);
  await loadKBChildren(path);
}

async function loadKBChildren(path) {
  const age = document.getElementById('kbFilterAge').value;
  const params = new URLSearchParams({ path });
  if (age) params.set('since_minutes', age);
  params.set('limit', 1000);

  try {
    const data = await api('/api/kb/children?' + params.toString());
    _kbChildren = data || [];
    renderKBTree(_kbChildren, path);
  } catch (e) {
    document.getElementById('kbTree').innerHTML =
      `<div class="kb-tree-item" style="color:#da3633">Error: ${esc(e.message)}</div>`;
  }
}

function renderKBTree(children, currentPath) {
  const tree = document.getElementById('kbTree');
  tree.innerHTML = '';

  const isRoot = currentPath === '/' || currentPath === '';

  if (!isRoot) {
    // Back button
    const segs = currentPath.replace(/\/$/, '').split('/');
    const parentPath = segs.slice(0, -1).join('/') + '/';
    const back = document.createElement('div');
    back.className = 'kb-tree-item kb-back';
    back.innerHTML = '<span class="kb-item-icon">←</span><span class="kb-item-name">back</span>';
    back.onclick = () => kbNavigate(parentPath || '/');
    tree.appendChild(back);
  }

  if (children.length === 0 && isRoot) {
    tree.innerHTML += '<div class="kb-tree-item" style="color:#484f58">No entries found</div>';
  }

  for (const child of children) {
    if (isRoot && child.name === 'tasks') continue; // tasks shown via toggle at bottom
    tree.appendChild(makeKBTreeItem(child, isRoot));
  }

  // Tasks toggle at root
  if (isRoot) {
    const tasksChild = children.find(c => c.name === 'tasks');
    const toggle = document.createElement('div');
    toggle.className = 'kb-tasks-toggle';
    toggle.innerHTML = `<span>${_kbTasksExpanded ? '▾' : '▸'}</span><span class="kb-item-name">tasks/</span><span class="kb-item-count">${tasksChild ? tasksChild.count : ''}</span>`;
    toggle.onclick = () => kbToggleTasks();
    tree.appendChild(toggle);

    if (_kbTasksExpanded) {
      const filterDiv = document.createElement('div');
      filterDiv.className = 'kb-tasks-filter';
      filterDiv.innerHTML = `
        <select id="kbTasksAge">
          <option value="20">Last 20 min</option>
          <option value="60">Last hour</option>
          <option value="360">Last 6h</option>
          <option value="1440">Today</option>
          <option value="">All time</option>
        </select>
        <button class="btn-tool-ctrl" onclick="kbBrowseTasks()">Browse</button>
      `;
      tree.appendChild(filterDiv);
      filterDiv.querySelector('#kbTasksAge').value = _kbTasksAge;
    }
  }

  if (children.length === 0 && !isRoot) {
    const empty = document.createElement('div');
    empty.className = 'kb-tree-item';
    empty.style.color = '#484f58';
    empty.textContent = 'Empty';
    tree.appendChild(empty);
  }
}

function makeKBTreeItem(child) {
  const item = document.createElement('div');
  item.className = 'kb-tree-item';
  item.dataset.path = child.path;

  const isDir = child.type === 'dir';
  const iconClass = isDir ? 'kb-item-icon kb-item-icon-dir' : 'kb-item-icon kb-item-icon-entry';
  const icon = isDir ? '▶' : '◆';
  const nameSuffix = isDir ? '/' : '';
  const countStr = child.count > 1 ? child.count : '';

  item.innerHTML = `
    <span class="${iconClass}">${icon}</span>
    <span class="kb-item-name">${esc(child.name)}${nameSuffix}</span>
    <span class="kb-item-count">${countStr}</span>
  `;

  if (isDir) {
    item.onclick = () => kbNavigate(child.path + '/');
  } else {
    item.onclick = () => kbSelectEntry(child.path);
  }
  return item;
}

async function kbSelectEntry(path) {
  document.querySelectorAll('.kb-tree-item').forEach(i => i.classList.remove('active'));
  document.querySelectorAll('.kb-task-record-item').forEach(i => i.classList.remove('active'));
  document.querySelectorAll(`[data-path="${CSS.escape(path)}"]`).forEach(i => i.classList.add('active'));

  try {
    const data = await api('/api/kb/entry?' + new URLSearchParams({ path }).toString());
    _kbEntry = data;
    renderKBDetail(data);
  } catch (e) {
    showToast('Failed to load entry: ' + e.message, 'error');
  }
}

function renderKBDetail(entry) {
  document.getElementById('kbEmpty').style.display = 'none';
  const detail = document.getElementById('kbDetail');
  detail.style.display = 'flex';

  document.getElementById('kbDetailPath').textContent = entry.scope;

  // Metadata
  const embBadge = entry.needs_embedding
    ? '<span style="color:#58a6ff">⊙ embed</span>'
    : '';
  const srcStr = entry.source ? `source: ${esc(entry.source)}` : '';
  const tsStr = entry.created_at ? new Date(entry.created_at).toLocaleString() : '';
  document.getElementById('kbDetailMeta').innerHTML =
    [srcStr, tsStr, embBadge].filter(Boolean).join('<span class="meta-sep">·</span>');

  // Content
  const content = document.getElementById('kbDetailContent');
  const text = entry.content || '';
  const looksMarkdown = /^#|\*\*|-\s|\[.*\]\(/m.test(text);
  if (looksMarkdown && md) {
    content.className = 'kb-content kb-markdown report-body';
    content.innerHTML = md.render(text);
  } else {
    content.className = 'kb-content';
    content.textContent = text || '(empty)';
  }

  // Reset edit state
  document.getElementById('kbDetailEdit').value = text;
  document.getElementById('kbDetailEdit').style.display = 'none';
  content.style.display = '';
  document.getElementById('kbDetailFooter').style.display = 'none';
  document.getElementById('kbEditBtn').textContent = 'Edit';
  _kbEditMode = false;
}

function renderKBBreadcrumb(path) {
  const crumb = document.getElementById('kbBreadcrumb');
  crumb.innerHTML = '';

  const root = document.createElement('span');
  root.className = 'kb-bc-seg';
  root.textContent = '/';
  root.onclick = () => kbNavigate('/');
  crumb.appendChild(root);

  if (path === '/' || path === '') return;

  const parts = path.replace(/\/$/, '').split('/').filter(Boolean);
  let accumulated = '/';

  for (let i = 0; i < parts.length; i++) {
    const sep = document.createElement('span');
    sep.className = 'kb-bc-sep';
    sep.textContent = '/';
    crumb.appendChild(sep);

    accumulated += parts[i] + '/';
    const isLast = i === parts.length - 1;
    const seg = document.createElement('span');
    seg.className = isLast ? 'kb-bc-current' : 'kb-bc-seg';
    seg.textContent = parts[i];
    if (!isLast) {
      const p = accumulated;
      seg.onclick = () => kbNavigate(p);
    }
    crumb.appendChild(seg);
  }

  // Delete subtree button
  const spacer = document.createElement('span');
  spacer.className = 'kb-bc-spacer';
  crumb.appendChild(spacer);

  const delBtn = document.createElement('button');
  delBtn.className = 'btn-danger';
  delBtn.style.cssText = 'padding:2px 7px;font-size:0.72em;flex-shrink:0';
  delBtn.textContent = 'Delete subtree';
  delBtn.onclick = () => kbDeleteSubtree(path);
  crumb.appendChild(delBtn);
}

async function kbDeleteEntry() {
  if (!_kbEntry) return;
  if (!confirm('Delete KB entry:\n' + _kbEntry.scope + '?')) return;

  try {
    await api('/api/kb/entry?' + new URLSearchParams({ path: _kbEntry.scope }).toString(), { method: 'DELETE' });
    showToast('Entry deleted', 'success');
    document.getElementById('kbDetail').style.display = 'none';
    document.getElementById('kbEmpty').style.display = 'flex';
    _kbEntry = null;
    await loadKBChildren(_kbPath);
  } catch (e) {
    showToast('Failed to delete: ' + e.message, 'error');
  }
}

async function kbDeleteSubtree(path) {
  try {
    const { count } = await api('/api/kb/count?' + new URLSearchParams({ path }).toString());
    if (!confirm(`Delete ${count} entries under ${path}?`)) return;
    const result = await api('/api/kb/subtree?' + new URLSearchParams({ path }).toString(), { method: 'DELETE' });
    showToast(`Deleted ${result.count} entries`, 'success');
    // Navigate to parent
    const segs = path.replace(/\/$/, '').split('/');
    const parent = segs.slice(0, -1).join('/') + '/';
    await kbNavigate(parent || '/');
  } catch (e) {
    showToast('Failed: ' + e.message, 'error');
  }
}

function kbToggleEdit() {
  _kbEditMode = !_kbEditMode;
  const content = document.getElementById('kbDetailContent');
  const edit = document.getElementById('kbDetailEdit');
  const footer = document.getElementById('kbDetailFooter');
  const btn = document.getElementById('kbEditBtn');

  if (_kbEditMode) {
    content.style.display = 'none';
    edit.style.display = 'block';
    edit.focus();
    footer.style.display = 'flex';
    btn.textContent = 'Cancel';
  } else {
    kbCancelEdit();
  }
}

function kbCancelEdit() {
  if (_kbEntry) document.getElementById('kbDetailEdit').value = _kbEntry.content || '';
  _kbEditMode = false;
  document.getElementById('kbDetailContent').style.display = '';
  document.getElementById('kbDetailEdit').style.display = 'none';
  document.getElementById('kbDetailFooter').style.display = 'none';
  document.getElementById('kbEditBtn').textContent = 'Edit';
}

async function kbSaveEntry() {
  if (!_kbEntry) return;
  const content = document.getElementById('kbDetailEdit').value;

  try {
    await api('/api/kb/entry', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: _kbEntry.scope, content, source: 'ui' }),
    });
    _kbEntry.content = content;
    renderKBDetail(_kbEntry);
    showToast('Saved', 'success');
  } catch (e) {
    showToast('Failed to save: ' + e.message, 'error');
  }
}

function kbCopyPath() {
  if (!_kbEntry) return;
  navigator.clipboard.writeText(_kbEntry.scope).then(() => showToast('Path copied', 'info'));
}

function kbToggleTasks() {
  _kbTasksExpanded = !_kbTasksExpanded;
  renderKBTree(_kbChildren, _kbPath);
}

function kbBrowseTasks() {
  const sel = document.getElementById('kbTasksAge');
  _kbTasksAge = sel ? sel.value : '20';
  // Navigate to /tasks/ with the age filter applied
  const ageEl = document.getElementById('kbFilterAge');
  if (ageEl && _kbTasksAge) ageEl.value = _kbTasksAge;
  kbNavigate('/tasks/');
}

function kbRefresh() {
  if (_kbTaskFilterId) {
    kbLoadTaskFilter(_kbTaskFilterId);
  } else {
    loadKBChildren(_kbPath);
  }
}

function kbApplyFilters() {
  if (_kbTaskFilterId) {
    kbLoadTaskFilter(_kbTaskFilterId);
  } else {
    loadKBChildren(_kbPath);
  }
}

function kbOnTaskInput(val) {
  clearTimeout(_kbTaskFilterTimer);
  document.getElementById('kbFilterTaskClear').style.display = val ? '' : 'none';

  if (!val) {
    _kbTaskFilterId = null;
    renderKBBreadcrumb(_kbPath);
    loadKBChildren(_kbPath);
    return;
  }

  _kbTaskFilterTimer = setTimeout(() => {
    if (val.length >= 8) kbLoadTaskFilter(val);
  }, 400);
}

async function kbLoadTaskFilter(taskId) {
  _kbTaskFilterId = taskId;
  document.getElementById('kbFilterTaskClear').style.display = '';

  // Show task context in breadcrumb
  const crumb = document.getElementById('kbBreadcrumb');
  crumb.innerHTML = `<span style="color:#8b949e;font-size:0.85em">Run: <code style="color:#58a6ff">${esc(taskId.substring(0, 8))}</code></span>`;

  try {
    const data = await api('/api/kb/task/' + taskId);
    renderKBTaskRecords(data);
  } catch (e) {
    document.getElementById('kbTree').innerHTML =
      `<div class="kb-tree-item" style="color:#da3633">Task not found or error: ${esc(e.message)}</div>`;
  }
}

function renderKBTaskRecords(data) {
  const tree = document.getElementById('kbTree');
  tree.innerHTML = '';

  if (!data.records || data.records.length === 0) {
    tree.innerHTML = '<div class="kb-tree-item" style="color:#484f58">No KB records found for this run</div>';
    return;
  }

  const direct = data.records.filter(r => r.group === 'direct');
  const during = data.records.filter(r => r.group === 'during');

  function addGroup(label, records) {
    if (records.length === 0) return;
    const hdr = document.createElement('div');
    hdr.className = 'kb-section-header';
    hdr.textContent = `${label} (${records.length})`;
    tree.appendChild(hdr);

    for (const r of records) {
      const item = document.createElement('div');
      item.className = 'kb-task-record-item';
      item.dataset.path = r.scope;
      const ts = r.created_at ? new Date(r.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '';
      const groupClass = r.group === 'direct' ? 'kb-task-record-group-direct' : 'kb-task-record-group-during';
      const groupLabel = r.group === 'direct' ? 'direct' : 'written during';
      item.innerHTML = `
        <div class="kb-task-record-scope">${esc(r.scope)}</div>
        <div class="kb-task-record-meta">
          ${r.source ? esc(r.source) + ' · ' : ''}${ts}
          <span class="kb-task-record-group ${groupClass}">${groupLabel}</span>
        </div>
      `;
      item.onclick = () => kbSelectEntry(r.scope);
      tree.appendChild(item);
    }
  }

  addGroup('Direct records', direct);
  addGroup('Written during run', during);
}

function kbClearTaskFilter() {
  document.getElementById('kbFilterTask').value = '';
  _kbTaskFilterId = null;
  document.getElementById('kbFilterTaskClear').style.display = 'none';
  renderKBBreadcrumb(_kbPath);
  loadKBChildren(_kbPath);
}

// Cross-tab: jump to KB and filter by a task ID
function viewKBForTask(taskId) {
  switchTab('kb');
  document.getElementById('kbFilterTask').value = taskId;
  document.getElementById('kbFilterTaskClear').style.display = '';
  kbLoadTaskFilter(taskId);
}

// Load KB root when tab is clicked
document.querySelectorAll('.tab').forEach(btn => {
  if (btn.dataset.tab === 'kb') {
    btn.addEventListener('click', () => {
      if (_kbChildren.length === 0) kbNavigate('/');
    });
  }
});

// ── Init ──────────────────────────────────────────────────────────────────────

_loadConfig();
loadModels();
loadToolGroups();
loadRightTasks();
loadReports();
loadAgents();
loadWorkflows();
loadWorkflowDropdown();
loadSchemas();
connectSSE();
