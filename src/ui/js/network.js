// ════════════════════════════════════════════════════
// Network — SSE, server file list, local file upload
// Depends on: state.js, scene.js
// ════════════════════════════════════════════════════

// ── SSE ──

function startSSE(filePath) {
    stopSSE();
    const url = filePath ? `/api/stream?path=${encodeURIComponent(filePath)}` : '/api/stream';
    state.sseSource = new EventSource(url);

    state.sseSource.addEventListener('update', e => {
        try {
            const resp = JSON.parse(e.data);
            if (resp.data) applyRealtimeUpdate(resp.data);
        } catch (_) {}
    });

    state.sseSource.addEventListener('connected', () => {
        document.getElementById('sse-badge').style.display = 'flex';
        document.getElementById('sse-badge').classList.remove('hidden');
        document.getElementById('sse-error').style.display = 'none';
    });

    state.sseSource.onerror = () => {
        document.getElementById('sse-badge').style.display = 'none';
        document.getElementById('sse-error').style.display = 'flex';
        document.getElementById('sse-error').classList.remove('hidden');
        // Reconnect after 5 s
        setTimeout(() => {
            if (!state.sseSource || state.sseSource.readyState === EventSource.CLOSED)
                startSSE(filePath);
        }, 5000);
    };
}

function stopSSE() {
    if (state.sseSource) { state.sseSource.close(); state.sseSource = null; }
    document.getElementById('sse-badge').style.display = 'none';
    document.getElementById('sse-error').style.display = 'none';
}

// ── Server log list ──

async function fetchLogList() {
    const listEl  = document.getElementById('server-log-list');
    const errorEl = document.getElementById('server-error');
    listEl.innerHTML = '<p class="text-slate-500 text-xs text-center py-3">Loading...</p>';
    errorEl.classList.add('hidden');
    try {
        const data = await fetch('/api/logs').then(r => r.json());
        if (!data.logs || data.logs.length === 0) {
            listEl.innerHTML = '<p class="text-slate-500 text-xs text-center py-4">No log files found</p>';
            return;
        }
        listEl.innerHTML = '';
        data.logs.forEach(log => {
            const btn = document.createElement('button');
            btn.className = 'w-full text-left px-3 py-2 rounded hover:bg-slate-700 transition text-slate-300 text-xs border border-transparent hover:border-slate-600 flex justify-between items-center gap-2';
            btn.innerHTML = `<span class="truncate font-mono">${log.name}</span><span class="shrink-0 text-slate-500">${log.mtime_str}</span>`;
            btn.addEventListener('click', () => loadFromServer(log.path));
            listEl.appendChild(btn);
        });
    } catch (err) {
        listEl.innerHTML = '';
        errorEl.textContent = 'Cannot reach backend: ' + err.message;
        errorEl.classList.remove('hidden');
    }
}

async function loadFromServer(path) {
    const errorEl = document.getElementById('server-error');
    try {
        const data = await fetch('/api/log?path=' + encodeURIComponent(path)).then(r => r.json());
        if (data.error) throw new Error(data.error);
        document.getElementById('server-panel').classList.remove('open');
        applyRealtimeUpdate(data);
    } catch (err) {
        errorEl.textContent = 'Load failed: ' + err.message;
        errorEl.classList.remove('hidden');
    }
}

// ── Auto-connect to Python backend ──

async function tryAutoConnect() {
    try {
        const cfg = await fetch('/api/config', { signal: AbortSignal.timeout(1500) }).then(r => r.ok ? r.json() : null);
        if (!cfg) return;

        // Try loading initial log from startup config
        if (cfg.has_log) {
            try {
                const initial = await fetch('/api/initial', { signal: AbortSignal.timeout(3000) }).then(r => r.json());
                if (initial.log && initial.log.data) {
                    applyRealtimeUpdate(initial.log.data);
                }
            } catch (_) {}
        }

        // If no initial data loaded, try the latest log
        if (!logData) {
            try {
                const resp = await fetch('/api/latest', { signal: AbortSignal.timeout(2000) }).then(r => r.json());
                if (!resp.error && resp.data) applyRealtimeUpdate(resp.data);
            } catch (_) {}
        }

        startSSE(cfg.log_file || null);
    } catch (_) {
        // No backend — stay offline
    }
}

// ── Initialise all network event listeners ──

function initNetwork() {
    const serverPanel   = document.getElementById('server-panel');
    const serverWrapper = document.getElementById('server-menu-wrapper');

    // Server dropdown toggle
    document.getElementById('btn-server').addEventListener('click', e => {
        e.stopPropagation();
        serverPanel.classList.toggle('open');
        if (serverPanel.classList.contains('open')) fetchLogList();
    });
    document.addEventListener('click', e => {
        if (!serverWrapper.contains(e.target)) serverPanel.classList.remove('open');
    });

    // Load latest
    document.getElementById('btn-server-latest').addEventListener('click', async () => {
        const errorEl = document.getElementById('server-error');
        try {
            const resp = await fetch('/api/latest').then(r => r.json());
            if (resp.error) throw new Error(resp.error);
            serverPanel.classList.remove('open');
            applyRealtimeUpdate(resp.data);
        } catch (err) {
            errorEl.textContent = err.message;
            errorEl.classList.remove('hidden');
        }
    });

    // Refresh log list
    document.getElementById('btn-server-refresh').addEventListener('click', fetchLogList);

    // Local file upload
    document.getElementById('file-input').addEventListener('change', function (e) {
        const file = e.target.files[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = ev => {
            try {
                applyRealtimeUpdate(JSON.parse(ev.target.result));
            } catch (err) { alert('JSON parse error: ' + err.message); }
        };
        reader.readAsText(file);
        this.value = '';
    });

    // Language toggle
    document.getElementById('btn-lang').addEventListener('click', () => {
        applyLang(currentLang === 'zh' ? 'en' : 'zh');
    });
}
