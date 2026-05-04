// ════════════════════════════════════════════════════
// Timeline — step display, logging, progress, connections
// Depends on: state.js, i18n.js, animations.js
// ════════════════════════════════════════════════════

// ── Show a specific step (renders all history up to targetIdx) ──

function showStep(targetIdx, keepHistory = false) {
    if (!logData || targetIdx < 0 || targetIdx >= state.totalSteps) return;

    state.currentStepIdx = targetIdx;
    state.activeConnection = null;

    // Reset all agents to idle (state.agents misses duplicate-named workers, so reset workerEls too)
    Object.values(state.agents).forEach(el => updateAgentDOM(el, 'idle', '-', I18N[currentLang].waitingCmd, null));
    (state.workerEls || []).forEach(({ el }) => updateAgentDOM(el, 'idle', '-', I18N[currentLang].waitingCmd, null));

    // ── Change A: cleanup when stepping forward ──
    if (!keepHistory) {
        const activeRunIdx = (state.stepToRunIdx && state.stepToRunIdx[targetIdx] !== undefined)
            ? state.stepToRunIdx[targetIdx] : -1;

        // Remove run groups entirely beyond the active run
        logPanel.querySelectorAll('[data-run-idx]').forEach(el => {
            if (parseInt(el.dataset.runIdx) > activeRunIdx) el.remove();
        });

        // Remove entries within the active run that are beyond targetIdx
        if (activeRunIdx >= 0) {
            const activeRunEl = logPanel.querySelector(`[data-run-idx="${activeRunIdx}"]`);
            if (activeRunEl) {
                activeRunEl.querySelectorAll('.log-run-entry').forEach(el => {
                    if (parseInt(el.dataset.step) > targetIdx + 1) el.remove();
                });
                _updateRunCount(activeRunEl, activeRunIdx);
            }
        }
    }

    // ── Change B: demote all existing entries to dim color ──
    logPanel.querySelectorAll('.log-run-entry').forEach(el => {
        el.classList.remove('bg-emerald-900/30','text-emerald-300','font-bold','border-l-2','border-emerald-500','log-new');
        el.classList.add('text-slate-400');
    });

    const agentLocalSteps = {};
    for (let i = 0; i <= targetIdx; i++) {
        const ev = logData.timeline[i];
        const el = state.agents[ev.agent_name];

        agentLocalSteps[ev.agent_name] = (agentLocalSteps[ev.agent_name] || 0) + 1;

        // ── Change C: pass runIdx and local step counters to addLog ──
        const runIdx = state.stepToRunIdx ? state.stepToRunIdx[i] : 0;
        if (!logPanel.querySelector(`.log-run-entry[data-step="${ev.step}"]`)) {
            addLog(ev.step, ev.description || '', runIdx,
                   agentLocalSteps[ev.agent_name],
                   state.agentTotalSteps[ev.agent_name] || 1,
                   ev.event_type);
        }

        if (el) {
            const localStep  = agentLocalSteps[ev.agent_name];
            const agentTotal = state.agentTotalSteps[ev.agent_name] || 1;
            updateAgentDOM(el, ev.status || 'active', `${localStep}/${agentTotal}`, ev.description || '', ev.event_type);
        }

        if (ev.event_type === 'agent_call' && ev.target_agent) {
            state.activeConnection = {
                from: ev.agent_name,
                to:   ev.target_agent,
                toEl: state.agents[ev.target_agent] || null,
            };
        } else if (ev.event_type === 'agent_return') {
            state.activeConnection = null;
        }
    }

    // Highlight latest entry with slide-in animation
    const latest = logPanel.querySelector(`.log-run-entry[data-step="${targetIdx + 1}"]`);
    if (latest) {
        latest.classList.remove('text-slate-400');
        latest.classList.add('bg-emerald-900/30','text-emerald-300','font-bold','border-l-2','border-emerald-500','log-new');
        latest.scrollIntoView({ block: 'nearest' });
    }

    // ── Change D: expand active run (don't touch others — user controls them) ──
    if (state.stepToRunIdx) {
        const activeRunIdx = state.stepToRunIdx[targetIdx];
        const activeRunEl  = logPanel.querySelector(`[data-run-idx="${activeRunIdx}"]`);
        if (activeRunEl) {
            activeRunEl.classList.remove('log-run-collapsed');
            const toggle = activeRunEl.querySelector('.log-run-toggle');
            if (toggle) toggle.textContent = '▼';
        }
    }

    drawConnections();
    updateProgress(targetIdx);
}

// ── Create a run group container ──

function createRunGroup(runIdx) {
    const run  = state.runs[runIdx];
    const isSup = run.agentType === 'supervisor';
    const nameColor   = isSup ? '#818cf8' : '#2dd4bf';
    const headerBg    = isSup ? '#1e1b4b' : '#022c22';
    const borderColor = isSup ? '#3730a3' : '#065f46';
    const total       = run.endIdx - run.startIdx + 1;

    const el = document.createElement('div');
    el.className = 'log-run';
    el.dataset.runIdx = runIdx;
    el.style.borderColor = borderColor;
    el.innerHTML = `
        <div class="log-run-header" data-run-header="${runIdx}" style="background:${headerBg}">
            <span class="log-run-toggle">▼</span>
            <span class="log-run-label" style="color:${nameColor}">${run.agentName}</span>
            <span class="log-run-type">[${run.agentType}]</span>
            <span class="log-run-count">0/${total}</span>
        </div>
        <div class="log-run-body"></div>`;
    return el;
}

// ── Update the step count badge on a run group header ──

function _updateRunCount(runEl, runIdx) {
    const countEl = runEl.querySelector('.log-run-count');
    if (!countEl) return;
    const shown = runEl.querySelectorAll('.log-run-entry').length;
    const run   = state.runs[runIdx];
    const total = run ? run.endIdx - run.startIdx + 1 : shown;
    countEl.textContent = `${shown}/${total}`;
}

// ── Append a log entry to the correct run group ──

// Event type → colored badge for log panel
const LOG_EVENT_INFO = {
    start:        { emoji: '🚀', label: 'Start',    color: '#3b82f6' },
    tool_call:    { emoji: '🔧', label: 'Tool',     color: '#10b981' },
    agent_call:   { emoji: '📡', label: 'Dispatch', color: '#7c3aed' },
    activated:    { emoji: '⚡', label: 'Active',   color: '#06b6d4' },
    agent_return: { emoji: '↩️', label: 'Return',   color: '#d97706' },
    completed:    { emoji: '✅', label: 'Done',     color: '#34d399' },
    error:        { emoji: '❌', label: 'Error',    color: '#ef4444' },
};

function addLog(stepNum, desc, runIdx, localStep, localTotal, eventType) {
    // Get or create the run group container
    let runEl = logPanel.querySelector(`[data-run-idx="${runIdx}"]`);
    if (!runEl) {
        runEl = createRunGroup(runIdx);
        logPanel.appendChild(runEl);
    }

    // Build event type badge HTML
    const evInfo = LOG_EVENT_INFO[eventType] || { emoji: '•', label: eventType || '?', color: '#64748b' };
    const badgeHtml = `<span class="log-event-badge" style="color:${evInfo.color};border-color:${evInfo.color}40">${evInfo.emoji}${evInfo.label}</span>`;

    // Append entry inside the group body
    const body  = runEl.querySelector('.log-run-body');
    const entry = document.createElement('div');
    entry.dataset.step = stepNum;
    entry.className    = 'log-run-entry py-1 px-2 rounded text-slate-400 cursor-pointer hover:bg-slate-700/40 transition-colors duration-100';
    entry.title        = `点击跳转到 Step ${stepNum}`;
    entry.innerHTML    = `<span class="log-entry-step">[${localStep}/${localTotal}]</span><span class="log-global-step">#${stepNum}</span>${badgeHtml} ${desc}`;
    body.appendChild(entry);

    // Update step count badge
    _updateRunCount(runEl, runIdx);
}

// ── Update progress bar and step badge ──

function updateProgress(idx) {
    const pct = state.totalSteps > 0 ? ((idx + 1) / state.totalSteps) * 100 : 0;
    document.getElementById('progress-bar').style.width      = `${pct}%`;
    document.getElementById('progress-text').textContent     = `Step ${idx + 1} / ${state.totalSteps}`;
    document.getElementById('step-badge').textContent        = `Step ${idx + 1}/${state.totalSteps}`;
}

// ── SVG connection arrows ──

// Clip a point (cx,cy) outward along direction (ndx,ndy) to the border of a rectangle
// centred at (cx,cy) with half-widths hw, hh. Returns the border point.
function _clipToBorder(cx, cy, ndx, ndy, hw, hh) {
    const tx = ndx !== 0 ? hw / Math.abs(ndx) : Infinity;
    const ty = ndy !== 0 ? hh / Math.abs(ndy) : Infinity;
    const t  = Math.min(tx, ty);
    return { x: cx + ndx * t, y: cy + ndy * t };
}

function drawConnections() {
    svgLayer.innerHTML = '';
    if (!logData || !logData.config) return;

    const agents   = logData.config.agents || [];
    const supAgent = agents.find(a => a.type === 'supervisor');
    if (!supAgent) return;
    const supName = supAgent.name;
    const supEl   = state.agents[supName];
    if (!supEl || !state.workerEls || state.workerEls.length === 0) return;

    const sr   = document.getElementById('stage').getBoundingClientRect();
    const fR   = supEl.getBoundingClientRect();
    const sx   = fR.left - sr.left + fR.width  / 2;
    const sy   = fR.top  - sr.top  + fR.height / 2;
    const sHW  = fR.width  / 2;
    const sHH  = fR.height / 2;

    (state.workerEls || []).forEach(({ name, el: wEl }) => {
        if (!wEl) return;

        const tR  = wEl.getBoundingClientRect();
        const ex  = tR.left - sr.left + tR.width  / 2;
        const ey  = tR.top  - sr.top  + tR.height / 2;
        const wHW = tR.width  / 2;
        const wHH = tR.height / 2;

        const dx  = ex - sx;
        const dy  = ey - sy;
        const len = Math.sqrt(dx * dx + dy * dy);
        if (len < 1) return;
        const ndx = dx / len;
        const ndy = dy / len;

        // Clip start to supervisor border, end to worker border
        const p1 = _clipToBorder(sx, sy,  ndx,  ndy, sHW, sHH);
        const p2 = _clipToBorder(ex, ey, -ndx, -ndy, wHW, wHH);

        const x1 = p1.x, y1 = p1.y;
        const x2 = p2.x, y2 = p2.y;
        const d  = `M ${x1} ${y1} L ${x2} ${y2}`;

        const isActive = !!(state.activeConnection &&
            (state.activeConnection.toEl
                ? wEl === state.activeConnection.toEl
                : state.activeConnection.to === name));

        // Arrowhead geometry (tip at border point)
        const aLen = 11, aHW = 5;
        const bx   = x2 - ndx * aLen;   // base centre
        const by   = y2 - ndy * aLen;
        const pts  = (fill) => {
            const p = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
            p.setAttribute('points',
                `${x2},${y2} ${bx + (-ndy)*aHW},${by + ndx*aHW} ${bx - (-ndy)*aHW},${by - ndx*aHW}`);
            p.setAttribute('fill', fill);
            return p;
        };

        // Static dim line (always present)
        const staticLine = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        staticLine.setAttribute('d', d);
        staticLine.setAttribute('fill', 'none');
        staticLine.setAttribute('stroke', isActive ? '#1e3a5f' : '#1e293b');
        staticLine.setAttribute('stroke-width', isActive ? '2' : '1.5');
        staticLine.setAttribute('opacity', isActive ? '0.7' : '0.9');
        svgLayer.appendChild(staticLine);
        svgLayer.appendChild(pts(isActive ? '#1e3a5f' : '#475569'));

        if (isActive) {
            // Glow layer
            const glow = document.createElementNS('http://www.w3.org/2000/svg', 'path');
            glow.setAttribute('d', d);
            glow.setAttribute('fill', 'none');
            glow.setAttribute('stroke', '#38bdf8');
            glow.setAttribute('stroke-width', '8');
            glow.setAttribute('opacity', '0.12');
            svgLayer.appendChild(glow);

            // Flowing dashed line
            const flowLine = document.createElementNS('http://www.w3.org/2000/svg', 'path');
            flowLine.setAttribute('d', d);
            flowLine.setAttribute('fill', 'none');
            flowLine.setAttribute('stroke', '#38bdf8');
            flowLine.setAttribute('stroke-width', '2.5');
            flowLine.setAttribute('stroke-dasharray', '10 10');
            flowLine.style.filter = 'drop-shadow(0 0 4px #38bdf8)';
            flowLine.classList.add('arrow-active-line');
            svgLayer.appendChild(flowLine);

            // Active arrowhead (blue polygon, drawn on top)
            const activeArrow = pts('#38bdf8');
            activeArrow.style.filter = 'drop-shadow(0 0 4px #38bdf8)';
            svgLayer.appendChild(activeArrow);

            // Moving dot along path
            const dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
            dot.setAttribute('r', '4');
            dot.setAttribute('fill', '#e0f2fe');
            dot.style.filter     = 'drop-shadow(0 0 6px #38bdf8)';
            dot.style.offsetPath = `path('${d}')`;
            dot.style.animation  = 'move-dot 1.1s cubic-bezier(0.45,0,0.55,1) infinite';
            svgLayer.appendChild(dot);
        }
    });
}
