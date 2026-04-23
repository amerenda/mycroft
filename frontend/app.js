const API = '';

let activeRunner = 'mycroft';

// ── Top-level tab navigation ─────────────────────────────────────────────────

document.querySelectorAll('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
  });
});

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

  // Switch to Trace so live output is visible
  setRightTab('trace');

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
  const model = document.getElementById('model').value;
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
    model: model || null,
    system_prompt: systemPrompt || null,
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
    const statusEl = document.getElementById('traceStatus');
    statusEl.textContent = 'running';
    statusEl.className = 'status-badge status-running';
    loadRightTasks();
    pollMycroftTask(r.task_id);
  }
}

function pollMycroftTask(taskId) {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const task = await api('/api/tasks/' + taskId);
      const conv = await api('/api/tasks/' + taskId + '/conversation').catch(() => null);
      renderTrace(conv ? conv.messages : [], task);

      if (task.status === 'completed' || task.status === 'failed') {
        clearInterval(pollTimer);
        pollTimer = null;
        loadRightTasks();
        if (task.status === 'completed') loadReports();

        const statusEl = document.getElementById('traceStatus');
        statusEl.textContent = task.status;
        statusEl.className = 'status-badge status-' + task.status;
      }
    } catch (e) { /* task may not have conversation yet */ }
  }, 3000);
}

function renderTrace(messages, task) {
  const el = document.getElementById('traceContent');
  if (!messages || !messages.length) {
    el.innerHTML = '<p class="empty">Waiting for agent to start...</p>';
    return;
  }

  const cards = [];

  for (const msg of messages) {
    if (msg.role === 'system') continue;

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

    if (msg.role === 'user' && messages.indexOf(msg) > 1) {
      cards.push(`
        <div class="trace-card" onclick="this.classList.toggle('expanded')">
          <div class="trace-card-header">
            <span>${esc(msg.content.slice(0, 80))}</span>
            <span class="trace-meta">nudge</span>
          </div>
          <div class="trace-card-body">${esc(msg.content)}</div>
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

  el.innerHTML = newHtml;

  // Restore expanded state
  if (expanded.size) {
    el.querySelectorAll('.trace-card').forEach((c, i) => {
      if (expanded.has(i)) c.classList.add('expanded');
    });
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
  const agentType = _WORKFLOW_AGENT[workflow] || workflow;

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

async function loadRightTasks() {
  try {
    const tasks = await api('/api/tasks?limit=20');
    const el = document.getElementById('rightTaskList');
    if (!tasks.length) {
      el.innerHTML = '<p class="empty">No tasks yet</p>';
      return;
    }
    el.innerHTML = tasks.map(t => `
      <div class="task-row">
        <span class="task-info" onclick="viewRightConversation('${t.id}')">
          ${t.id.slice(0, 8)} &mdash; ${t.agent_type} &mdash; ${esc((t.config?.instruction || '').slice(0, 60))}
        </span>
        <div class="task-actions">
          <span class="status-badge status-${t.status}">${t.status}</span>
          <button class="btn-delete" onclick="deleteTask('${t.id}')" title="Delete">&#10005;</button>
        </div>
      </div>`).join('');
  } catch (e) {
    document.getElementById('rightTaskList').innerHTML = '<p class="empty">Error loading tasks</p>';
  }
}

async function viewRightConversation(taskId) {
  const panel = document.getElementById('rightConvPanel');
  const titleEl = document.getElementById('rightConvId');
  const contentEl = document.getElementById('rightConvContent');

  titleEl.textContent = taskId.slice(0, 8);
  panel.style.display = 'block';

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
    const metaParts = [
      wf ? `<span class="effort-badge effort-${wf.split('-').pop()}">${wf}</span>` : '',
      date ? `<span>${date}</span>` : '',
      models ? `<span>${esc(models)}</span>` : '',
      build ? `<span>${esc(build)}</span>` : '',
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

async function deleteCurrentReport() {
  if (!_currentReport) return;
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

    ['model', 'gatherModel', 'writeModel', 'agentModel'].forEach(selId => {
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

function _extractYamlField(yaml, field) {
  const m = yaml.match(new RegExp(`^${field}:\\s*(.+)`, 'm'));
  return m ? m[1].trim() : '';
}

function _updateYamlField(yaml, field, value) {
  const re = new RegExp(`^(${field}:\\s*).*`, 'm');
  return re.test(yaml) ? yaml.replace(re, `$1${value}`) : yaml + `\n${field}: ${value}`;
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
    document.getElementById('agentSystemPrompt').value = _extractSystemPrompt(a.prompts || '');

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
  document.getElementById('agentSystemPrompt').value = '';
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

  const sysPrompt = document.getElementById('agentSystemPrompt').value.trim();
  const prompts = sysPrompt
    ? _wrapSystemPrompt(name, sysPrompt)
    : document.getElementById('agentPrompts').value;

  document.getElementById('agentManifest').value = manifest;
  document.getElementById('agentPrompts').value = prompts;

  try {
    await api('/api/agents/' + name, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ manifest, prompts }),
    });
    _currentAgent = name;
    loadAgents();
  } catch (e) {
    alert('Save failed: ' + e.message);
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
  const instruction = document.getElementById('agentTestInstruction').value.trim();
  if (!instruction || !_currentAgent) return;

  const statusEl = document.getElementById('agentTestStatus');
  const resultEl = document.getElementById('agentTestResult');
  const contentEl = document.getElementById('agentTestContent');

  statusEl.style.display = '';
  statusEl.textContent = 'running';
  statusEl.className = 'status-badge status-running';
  resultEl.style.display = '';
  contentEl.innerHTML = '<p class="empty">Submitting...</p>';

  try {
    const r = await api('/api/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ agent_type: _currentAgent, instruction, max_iterations: 6 }),
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

function _modelOpts(selected) {
  return '<option value="">Default</option>' +
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
  el.innerHTML = _pipelineSteps.map((step, i) => `
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
        <div class="form-row" style="margin-bottom:8px">
          <div class="form-group" style="margin-bottom:0">
            <label>Agent</label>
            <select onchange="updatePipelineStep(${i},'agent',this.value)">${_agentOpts(step.agent)}</select>
          </div>
          <div class="form-group" style="margin-bottom:0">
            <label>Model Override</label>
            <select onchange="updatePipelineStep(${i},'model',this.value)">${_modelOpts(step.model)}</select>
          </div>
        </div>
        <details${step.prompt_override ? ' open' : ''}>
          <summary style="font-size:0.82em;color:#8b949e;cursor:pointer">Prompt Override</summary>
          <textarea class="editor-textarea" rows="4" style="margin-top:6px"
            onchange="updatePipelineStep(${i},'prompt_override',this.value)"
            placeholder="Leave empty to use agent default">${esc(step.prompt_override || '')}</textarea>
        </details>
      </div>
    </div>`).join('');
}

function addPipelineStep() {
  _pipelineSteps.push({ agent: '', model: '', prompt_override: '' });
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
    _pipelineSteps = (w.pipeline_json && w.pipeline_json.steps) || [];
    _renderPipelineSteps();
    document.getElementById('workflowEditor').style.display = '';
    document.getElementById('workflowEmpty').style.display = 'none';
    loadWorkflows();
  } catch (e) {
    alert('Error loading workflow: ' + e.message);
  }
}

function newWorkflow() {
  _currentWorkflow = null;
  _pipelineSteps = [{ agent: '', model: '', prompt_override: '' }];
  document.getElementById('workflowName').value = '';
  document.getElementById('workflowDescription').value = '';
  document.getElementById('workflowContent').value = '';
  _renderPipelineSteps();
  document.getElementById('workflowEditor').style.display = '';
  document.getElementById('workflowEmpty').style.display = 'none';
  document.getElementById('workflowName').focus();
  loadWorkflows();
}

async function saveWorkflow() {
  const name = document.getElementById('workflowName').value.trim();
  if (!name) { alert('Workflow name is required'); return; }

  const pipeline_json = {
    description: document.getElementById('workflowDescription').value.trim(),
    steps: _pipelineSteps.map(s => ({
      agent: s.agent,
      model: s.model || '',
      prompt_override: s.prompt_override || '',
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
  } catch (e) {
    alert('Save failed: ' + e.message);
  }
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
  } catch (e) {
    alert('Delete failed: ' + e.message);
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────

loadModels();
loadRightTasks();
loadReports();
loadAgents();
loadWorkflows();
setInterval(loadRightTasks, 30000);
setInterval(loadReports, 60000);
