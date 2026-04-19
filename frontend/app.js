const API = '';

let activeRunner = 'forge'; // 'forge' or 'mycroft'

// ── Tab navigation ──────────────────────────────────────────────────────────

document.querySelectorAll('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
  });
});

// ── Runner toggle ───────────────────────────────────────────────────────────

function setRunner(runner) {
  activeRunner = runner;
  document.querySelectorAll('.toggle').forEach(b => {
    b.classList.toggle('active', b.dataset.runner === runner);
  });
  const btn = document.getElementById('runBtn');
  const agentGroup = document.getElementById('agentTypeGroup');
  const agentSelect = document.getElementById('agentType');
  const previewBtn = document.getElementById('previewBtn');

  if (runner === 'forge') {
    btn.textContent = 'Run with Forge';
    previewBtn.style.display = 'none';
  } else {
    btn.textContent = 'Run with Mycroft';
    previewBtn.style.display = '';
  }
}

// ── API helpers ─────────────────────────────────────────────────────────────

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

// ── Test Runner ─────────────────────────────────────────────────────────────

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

  const statsEl = document.getElementById('queueStats');
  statsEl.style.display = 'none';

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
    btn.textContent = activeRunner === 'forge' ? 'Run with Forge' : 'Run with Mycroft';
  }
}

// ── Forge runner ────────────────────────────────────────────────────────────

async function runForge(instruction) {
  const model = document.getElementById('model').value || 'qwen3:14b';
  const repo = document.getElementById('repo').value.trim();
  const systemPrompt = document.getElementById('systemPrompt').value.trim();

  if (!repo) {
    throw new Error('Repo is required for Forge runs');
  }

  const r = await api('/api/forge/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      instruction,
      repo,
      model,
      system_prompt: systemPrompt || null,
    }),
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
    } catch (e) {
      // Still running
    }
  }, 2000);
}

function renderForgeResult(r) {
  const el = document.getElementById('traceContent');
  const cards = [];

  if (r.status === 'running') {
    el.innerHTML = '<p class="empty">Forge is working... (cloning, running LLM calls)</p>';
    return;
  }

  // Error
  if (r.error) {
    cards.push(`
      <div class="trace-card" style="border-left:3px solid #da3633">
        <div class="trace-card-header" onclick="this.parentElement.classList.toggle('expanded')">
          <span style="color:#da3633">Error: ${esc(r.error)}</span>
        </div>
        <div class="trace-card-body">${esc(r.stderr)}</div>
      </div>
    `);
  }

  // Git diff
  if (r.git_diff) {
    cards.push(`
      <div class="trace-card tool-call expanded" onclick="this.classList.toggle('expanded')">
        <div class="trace-card-header">
          <span><span class="trace-tool-name">git diff</span> (${r.files_changed.length} file${r.files_changed.length !== 1 ? 's' : ''})</span>
          <span class="trace-meta">${r.git_diff.length} bytes</span>
        </div>
        <div class="trace-card-body">${esc(r.git_diff)}</div>
      </div>
    `);
  }

  // Files changed
  if (r.files_changed && r.files_changed.length) {
    cards.push(`
      <div class="trace-card llm-response">
        <div class="trace-card-header">
          <span class="trace-content">Files changed: ${r.files_changed.join(', ')}</span>
        </div>
      </div>
    `);
  }

  // Stdout (Forge output)
  if (r.stdout) {
    cards.push(`
      <div class="trace-card" onclick="this.classList.toggle('expanded')">
        <div class="trace-card-header">
          <span>Forge output</span>
          <span class="trace-meta">${r.stdout.length} chars</span>
        </div>
        <div class="trace-card-body">${esc(r.stdout)}</div>
      </div>
    `);
  }

  if (!cards.length) {
    cards.push('<p class="empty">No changes made</p>');
  }

  // Stats bar
  const statsEl = document.getElementById('queueStats');
  statsEl.style.display = 'flex';
  statsEl.innerHTML = `
    <span>Exit: <strong>${r.exit_code}</strong></span>
    <span>Duration: <strong>${r.duration_seconds.toFixed(1)}s</strong></span>
    <span>Files: <strong>${r.files_changed.length}</strong></span>
    <span>Status: <strong>${r.status}</strong></span>
  `;

  el.innerHTML = cards.join('');
}

// ── Mycroft runner ──────────────────────────────────────────────────────────

async function runMycroft(instruction) {
  const model = document.getElementById('model').value;
  const systemPrompt = document.getElementById('systemPrompt').value.trim();

  const r = await api('/api/tasks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      agent_type: document.getElementById('agentType').value,
      instruction,
      repo: document.getElementById('repo').value.trim(),
      model: model || null,
      system_prompt: systemPrompt || null,
    }),
  });

  if (r.task_id) {
    const statusEl = document.getElementById('traceStatus');
    statusEl.textContent = 'running';
    statusEl.className = 'status-badge status-running';
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
        loadTasks();

        const statusEl = document.getElementById('traceStatus');
        statusEl.textContent = task.status;
        statusEl.className = 'status-badge status-' + task.status;
      }
    } catch (e) {
      // Task may not have conversation yet
    }
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
          const cardClass = isFileOp ? 'tool-call' : 'planning';

          cards.push(`
            <div class="trace-card ${cardClass}" onclick="this.classList.toggle('expanded')">
              <div class="trace-card-header">
                <span><span class="trace-tool-name">${esc(name)}</span></span>
                <span class="trace-meta">tool call</span>
              </div>
              <div class="trace-card-body">${esc(args)}</div>
            </div>
          `);
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
          </div>
        `);
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
        </div>
      `);
    }

    if (msg.role === 'user' && messages.indexOf(msg) > 1) {
      cards.push(`
        <div class="trace-card" onclick="this.classList.toggle('expanded')">
          <div class="trace-card-header">
            <span>${esc(msg.content.slice(0, 80))}</span>
            <span class="trace-meta">nudge</span>
          </div>
          <div class="trace-card-body">${esc(msg.content)}</div>
        </div>
      `);
    }
  }

  el.innerHTML = cards.join('') || '<p class="empty">No tool calls yet</p>';
}

async function previewPrompt() {
  const instruction = document.getElementById('instruction').value.trim();
  if (!instruction) return;

  try {
    const r = await api('/api/tasks/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        agent_type: document.getElementById('agentType').value,
        instruction,
        model: document.getElementById('model').value || null,
      }),
    });

    const panel = document.getElementById('promptPanel');
    panel.style.display = 'block';
    panel.querySelector('#promptContent').innerHTML = `
      <div class="msg msg-system"><div class="role">System Prompt</div><pre>${esc(r.system_prompt)}</pre></div>
      <div class="msg msg-user"><div class="role">User Message</div><pre>${esc(r.user_message)}</pre></div>
      <p style="margin-top:8px;font-size:0.82em;color:#8b949e">Tools: ${r.tools.join(', ')} | Model: ${r.model}</p>
    `;
    const spEl = document.getElementById('systemPrompt');
    if (!spEl.value.trim()) spEl.value = r.system_prompt;
  } catch (e) {
    alert('Error: ' + e.message);
  }
}

// ── Tasks Tab ───────────────────────────────────────────────────────────────

async function loadTasks() {
  try {
    const tasks = await api('/api/tasks?limit=20');
    const el = document.getElementById('taskList');
    if (!tasks.length) {
      el.innerHTML = '<p class="empty">No tasks yet</p>';
      return;
    }
    el.innerHTML = tasks.map(t => `
      <div class="task-row">
        <span class="task-info" onclick="viewConversation('${t.id}')">
          ${t.id.slice(0, 8)} &mdash; ${t.agent_type} &mdash; ${esc((t.config?.instruction || '').slice(0, 60))}
        </span>
        <div class="task-actions">
          <span class="status-badge status-${t.status}">${t.status}</span>
          <button class="btn-delete" onclick="deleteTask('${t.id}')" title="Delete">&#10005;</button>
        </div>
      </div>
    `).join('');
  } catch (e) {
    document.getElementById('taskList').innerHTML = '<p class="empty">Error loading tasks</p>';
  }
}

async function viewConversation(taskId) {
  try {
    const r = await api('/api/tasks/' + taskId + '/conversation');
    const panel = document.getElementById('conversationPanel');
    panel.style.display = 'block';
    document.getElementById('convTaskId').textContent = '(' + taskId.slice(0, 8) + ')';

    const messages = r.messages || [];
    if (!messages.length) {
      document.getElementById('conversationContent').innerHTML = '<p class="empty">No conversation data</p>';
      return;
    }

    document.getElementById('conversationContent').innerHTML = messages.map(m => {
      let content = m.content || '';
      if (m.tool_calls) {
        content += '\n\nTool calls:\n' + m.tool_calls.map(tc =>
          tc.function.name + '(' + tc.function.arguments.slice(0, 200) + ')'
        ).join('\n');
      }
      return `<div class="msg msg-${m.role}"><div class="role">${m.role}</div><pre>${esc(content)}</pre></div>`;
    }).join('');
  } catch (e) {
    const panel = document.getElementById('conversationPanel');
    panel.style.display = 'block';
    document.getElementById('convTaskId').textContent = '(' + taskId.slice(0, 8) + ')';
    document.getElementById('conversationContent').innerHTML = '<p class="empty">No conversation data yet</p>';
  }
}

async function deleteTask(taskId) {
  try {
    await api('/api/tasks/' + taskId, { method: 'DELETE' });
    loadTasks();
  } catch (e) {
    alert('Error: ' + e.message);
  }
}

async function clearAllTasks() {
  if (!confirm('Delete all tasks? This cannot be undone.')) return;
  try {
    await api('/api/tasks', { method: 'DELETE' });
    loadTasks();
  } catch (e) {
    alert('Error: ' + e.message);
  }
}

// ── Init ────────────────────────────────────────────────────────────────────

async function loadModels() {
  try {
    const models = await api('/api/models');
    const el = document.getElementById('model');
    const list = Array.isArray(models) ? models : (models.data || []);
    list
      .filter(m => m.downloaded !== false)
      .sort((a, b) => (a.name || a.id || '').localeCompare(b.name || b.id || ''))
      .forEach(m => {
        const opt = document.createElement('option');
        const name = m.name || m.id || '';
        opt.value = name;
        const tags = [];
        if (m.loaded) tags.push('loaded');
        if (m.parameter_count) tags.push(m.parameter_count);
        opt.textContent = name + (tags.length ? ' (' + tags.join(', ') + ')' : '');
        el.appendChild(opt);
      });
  } catch (e) {
    console.warn('Failed to load models:', e);
  }
}

loadModels();
loadTasks();
setInterval(loadTasks, 30000);
