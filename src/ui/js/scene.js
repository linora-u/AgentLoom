// ════════════════════════════════════════════════════
// Scene — card creation and scene initialisation
// Depends on: state.js, i18n.js, animations.js
// ════════════════════════════════════════════════════

// 2-D image map: agent_type → status → image path
const AVATAR_MAP = {
    supervisor: {
        default:    'picture/supervisor/teacher.png',
        waiting:    'picture/supervisor/waiting.png',
        completed:  'picture/supervisor/completed.png',
        error:      'picture/supervisor/error.png',
    },
    worker: {
        default:    'picture/worker/work.png',
        thinking:   'picture/worker/thinking.png',
        completed:  'picture/worker/completed.png',
        error:      'picture/worker/error.png',
    },
};

// Resolve avatar URL for a given agent type + status, fallback to default
function getAvatarUrl(agentType, status) {
    const map = AVATAR_MAP[agentType] || AVATAR_MAP.worker;
    return map[status] || map.default || '';
}

// ── Compute card sizes and orbital radius based on worker count ──

function computeLayout(workerCount) {
    let cardW, supW, radius;
    if      (workerCount <= 3)  { cardW = 240; supW = 280; radius = 430; }
    else if (workerCount <= 5)  { cardW = 200; supW = 240; radius = 460; }
    else if (workerCount <= 8)  { cardW = 170; supW = 210; radius = 490; }
    else if (workerCount <= 12) { cardW = 145; supW = 180; radius = 530; }
    else                        { cardW = 120; supW = 155; radius = 560; }

    // Anti-overlap: ensure arc between adjacent workers >= cardW + gap
    if (workerCount > 1) {
        const minR = (workerCount * (cardW + 20)) / (2 * Math.PI);
        radius = Math.max(radius, Math.ceil(minR + cardW / 2));
    }
    return { cardW, supW, radius };
}

// ── Create a single agent card (width driven by cardW param) ──

function createAgentCard(agent, cardW) {
    const el = document.createElement('div');
    el.id        = `agent-${agent.name}`;

    // Scale all sizes relative to reference width 240px; floor at 0.72× so text stays readable
    const s      = Math.max(0.72, cardW / 240);
    const sz     = (n, min) => Math.max(min || 6, Math.round(n * s));
    const pad    = sz(16, 8);    // card padding
    const avSize = sz(46, 22);   // avatar width/height
    const avR    = sz(10, 5);    // avatar border-radius
    const idSize = sz(15, 8);    // status-indicator size
    const idBdr  = Math.max(1, Math.round(2.5 * s));  // indicator border width
    const idOff  = -Math.round(idSize * 0.3);          // indicator top/right offset
    const hdrGap = sz(12, 4);    // gap between avatar and text block
    const nameFs = sz(15, 8);    // agent name font-size
    const typeFs = sz(11, 6);    // agent type sub-label
    const stepFs = sz(10, 6);    // step badge font-size
    const stepPx = sz(8, 3);     // step badge horizontal padding
    const stepPy = sz(2, 1);     // step badge vertical padding
    const descFs = sz(14, 7);    // description font-size
    const animH  = Math.max(40, Math.round(cardW * 0.32));

    el.className = 'relative shrink-0 bg-slate-800/90 backdrop-blur-md border-[4px] rounded-xl flex flex-col shadow-2xl transition-all duration-300 status-idle';
    el.style.cssText = `width:${cardW}px;position:absolute;transform:translate(-50%,-50%);padding:${pad}px;`;

    const isSup    = agent.type === 'supervisor';
    const bgColor  = isSup ? 'bg-indigo-600' : 'bg-teal-600';
    const label    = agent.name;
    const avatarUrl = getAvatarUrl(agent.type, 'default');
    el.dataset.agentType = agent.type;  // for updateAgentDOM to resolve avatar

    el.innerHTML = `
        <div class="flex items-center mb-3 pb-3 border-b border-slate-700/50" style="gap:${hdrGap}px">
            <div class="relative shrink-0">
                <div class="${bgColor} flex items-center justify-center shadow-inner border border-white/10 overflow-hidden"
                     style="width:${avSize}px;height:${avSize}px;border-radius:${avR}px">
                    <img src="${avatarUrl}" alt="avatar"
                         class="w-full h-full object-cover"
                         onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
                    <span style="display:none;font-size:${sz(20,10)}px" class="text-white font-bold w-full h-full items-center justify-center">${label.charAt(0).toUpperCase()}</span>
                </div>
                <div class="status-indicator rounded-full bg-slate-500 shadow z-10 transition-colors duration-300"
                     style="position:absolute;width:${idSize}px;height:${idSize}px;top:${idOff}px;right:${idOff}px;border:${idBdr}px solid #1e293b"></div>
            </div>
            <div class="flex-1 min-w-0">
                <div class="font-bold text-slate-100 break-all leading-snug" style="font-size:${nameFs}px">${label}</div>
                <div class="text-slate-500" style="font-size:${typeFs}px">${agent.type}</div>
            </div>
        </div>
        <div style="height:${animH}px" class="bg-slate-900 rounded-lg border border-slate-700/50 mb-3 relative overflow-hidden flex items-center justify-center shadow-inner">
            <div class="anim-layer w-full h-full"></div>
        </div>
        <div class="flex justify-between items-center mb-1" style="gap:4px">
            <div class="font-bold rounded bg-slate-700 text-slate-300 step-text uppercase tracking-wider"
                 style="font-size:${stepFs}px;padding:${stepPy}px ${stepPx}px">Step -</div>
            <span class="event-badge" style="font-size:${sz(9,6)}px"></span>
        </div>
        <div class="text-slate-300 desc-text line-clamp-2 leading-snug" style="font-size:${descFs}px">${I18N[currentLang].waitingTask}</div>
    `;
    return el;
}

// ── Compute run groups (consecutive same-agent sequences) ──

function _computeRuns(data) {
    const runs         = [];
    const stepToRunIdx = [];
    const agents       = data.config?.agents || [];
    let cur = null;
    (data.timeline || []).forEach((ev, i) => {
        if (!cur || ev.agent_name !== cur.agentName) {
            const def = agents.find(a => a.name === ev.agent_name);
            cur = {
                agentName: ev.agent_name,
                agentType: def ? def.type : 'worker',
                startIdx:  i,
                endIdx:    i,
            };
            runs.push(cur);
        } else {
            cur.endIdx = i;
        }
        stepToRunIdx[i] = runs.length - 1;
    });
    return { runs, stepToRunIdx };
}

function initScene(data) {
    logData = data;
    state.totalSteps     = (data.timeline || []).length;
    state.currentStepIdx = -1;
    state.activeConnection = null;

    // Pre-compute how many events belong to each agent
    state.agentTotalSteps = {};
    (data.timeline || []).forEach(ev => {
        state.agentTotalSteps[ev.agent_name] = (state.agentTotalSteps[ev.agent_name] || 0) + 1;
    });

    // Pre-compute run groups (consecutive same-agent sequences)
    const r = _computeRuns(data);
    state.runs         = r.runs;
    state.stepToRunIdx = r.stepToRunIdx;

    const orbitalStage = document.getElementById('orbital-stage');
    orbitalStage.innerHTML = '';
    svgLayer.innerHTML = '';
    logPanel.innerHTML = '';
    state.agents    = {};
    state.workerEls = [];

    const agents  = data.config?.agents || [];
    const supData = agents.find(a => a.type === 'supervisor');
    const workers = agents.filter(a => a.type !== 'supervisor');

    const { cardW, supW, radius } = computeLayout(workers.length);

    // Stage: diameter + card width on each side + padding
    const stageSize = 2 * radius + 2 * Math.max(cardW, supW) + 80;
    orbitalStage.style.width  = stageSize + 'px';
    orbitalStage.style.height = stageSize + 'px';

    const cx = stageSize / 2;
    const cy = stageSize / 2;

    // Supervisor at center
    if (supData) {
        const supEl = createAgentCard(supData, supW);
        supEl.style.left = cx + 'px';
        supEl.style.top  = cy + 'px';
        orbitalStage.appendChild(supEl);
        state.agents[supData.name] = supEl;
    }

    // Workers evenly distributed around the orbit, starting from top (−90°)
    workers.forEach((w, i) => {
        const angle = (2 * Math.PI * i / workers.length) - Math.PI / 2;
        const wx = cx + radius * Math.cos(angle);
        const wy = cy + radius * Math.sin(angle);
        const el = createAgentCard(w, cardW);
        el.style.left = wx + 'px';
        el.style.top  = wy + 'px';
        el.dataset.cardDraggable = '1';        // worker cards are draggable
        el.style.cursor = 'grab';
        orbitalStage.appendChild(el);
        state.agents[w.name] = el;           // last card wins — used by timeline
        state.workerEls.push({ name: w.name, el }); // all cards — used by drawConnections
    });

    setTimeout(drawConnections, 50);
    setTimeout(centerStage, 100);
}

function applyRealtimeUpdate(data) {
    const newTotal = (data.timeline || []).length;

    // Rebuild cards only when the agent roster changes
    const needRebuild =
        !logData ||
        JSON.stringify(data.config) !== JSON.stringify(logData.config);

    if (needRebuild) {
        initScene(data);
    } else {
        logData = data;
        state.totalSteps = newTotal;
        // Re-compute per-agent totals for the updated timeline
        state.agentTotalSteps = {};
        (data.timeline || []).forEach(ev => {
            state.agentTotalSteps[ev.agent_name] = (state.agentTotalSteps[ev.agent_name] || 0) + 1;
        });
        // Re-compute run groups for the updated timeline
        const r = _computeRuns(data);
        state.runs         = r.runs;
        state.stepToRunIdx = r.stepToRunIdx;
    }

    if (newTotal > 0) showStep(newTotal - 1);
}
