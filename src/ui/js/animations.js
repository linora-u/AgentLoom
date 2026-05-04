// ════════════════════════════════════════════════════
// Agent card animations
// Each status value maps to a border color, dot color,
// and a preview animation rendered inside .anim-layer.
// Depends on: state.js (I18N, currentLang via i18n.js)
// ════════════════════════════════════════════════════

// Maps status → dot / border hex color
const STATUS_COLORS = {
    idle:       '#64748b',
    thinking:   '#3b82f6',
    planning:   '#7c3aed',
    codeact:    '#06b6d4',
    searching:  '#d97706',
    active:     '#10b981',
    waiting:    '#f59e0b',
    reviewing:  '#6366f1',
    reflecting: '#8b5cf6',
    completed:  '#34d399',
    error:      '#ef4444',
};

// All status CSS classes — kept here so classList.remove() stays in one place
const ALL_STATUS_CLASSES = [
    'status-idle','status-thinking','status-planning','status-codeact',
    'status-searching','status-active','status-waiting','status-reviewing',
    'status-reflecting','status-completed','status-error',
];

// ── Per-status anim-layer HTML builders ──────────────────

function animIdle() {
    return `<div class="relative w-full h-full flex justify-center items-center bg-slate-900 overflow-hidden">
        <span class="absolute text-slate-300 font-bold text-xl anim-z-1" style="top:18px;left:42%">Z</span>
        <span class="absolute text-slate-400 font-bold text-base anim-z-2" style="top:28px;left:57%">z</span>
        <span class="absolute text-slate-500 font-bold text-sm anim-z-1" style="top:40px;left:65%">z</span>
        <span class="absolute text-slate-400 font-bold text-lg anim-z-2" style="top:14px;left:28%">Z</span>
        <span class="absolute text-slate-500 font-bold text-xs anim-z-1" style="top:50px;left:50%">z</span>
    </div>`;
}

function animThinking() {
    return `<div class="relative w-full h-full flex justify-center items-center bg-slate-900 overflow-hidden">
        <svg class="absolute anim-gear text-blue-500/20 w-20 h-20" viewBox="0 0 24 24" fill="currentColor">
            <path d="M19.14,12.94c0.04-0.3,0.06-0.61,0.06-0.94c0-0.32-0.02-0.64-0.06-0.94l2.03-1.58c0.18-0.14,0.23-0.41,0.12-0.61l-1.92-3.32c-0.12-0.22-0.37-0.29-0.59-0.22l-2.39,0.96c-0.5-0.38-1.03-0.7-1.62-0.94L14.4,2.81c-0.04-0.24-0.24-0.41-0.48-0.41h-3.84c-0.24,0-0.43,0.17-0.47,0.41L9.25,5.35C8.66,5.59,8.12,5.92,7.63,6.29L5.24,5.33c-0.22-0.08-0.47,0-0.59,0.22L2.73,8.87C2.62,9.08,2.66,9.34,2.86,9.48l2.03,1.58C4.84,11.36,4.8,11.69,4.8,12s0.02,0.64,0.06,0.94l-2.03,1.58c-0.18,0.14-0.23,0.41-0.12,0.61l1.92,3.32c0.12,0.22,0.37,0.29,0.59,0.22l2.39-0.96c0.5,0.38,1.03,0.7,1.62,0.94l0.36,2.54c0.05,0.24,0.24,0.41,0.48,0.41h3.84c0.24,0,0.44-0.17,0.47-0.41l0.36-2.54c0.59-0.24,1.13-0.56,1.62-0.94l2.39,0.96c0.22,0.08,0.47,0,0.59-0.22l1.92-3.32c0.12-0.22,0.07-0.49-0.12-0.61L19.14,12.94zM12,15.6c-1.99,0-3.6-1.61-3.6-3.6s1.61-3.6,3.6-3.6s3.6,1.61,3.6,3.6S13.99,15.6,12,15.6z"/>
        </svg>
        <span class="w-6 h-6 rounded-full bg-yellow-400 anim-thinking-bulb shadow-[0_0_15px_rgba(234,179,8,0.8)] z-10"></span>
    </div>`;
}

function animPlanning() {
    return `<div class="w-full h-full p-3 bg-[#0a0520] flex flex-col gap-2 justify-center overflow-hidden">
        <div class="flex items-center gap-2">
            <div class="w-2 h-2 rounded-full bg-violet-500 shrink-0"></div>
            <div class="h-1.5 bg-violet-800/70 rounded flex-1"></div>
        </div>
        <div class="flex items-center gap-2">
            <div class="w-2 h-2 rounded-full bg-violet-400 shrink-0"></div>
            <div class="h-1.5 bg-violet-700/60 rounded" style="width:75%"></div>
        </div>
        <div class="flex items-center gap-2">
            <div class="w-2 h-2 rounded-full bg-violet-300 shrink-0 animate-pulse"></div>
            <div class="h-1.5 bg-violet-500/50 rounded animate-pulse" style="width:50%"></div>
        </div>
    </div>`;
}

function animSearching() {
    return `<div class="w-full h-full bg-[#0a0800] relative overflow-hidden flex items-center justify-center">
        <div class="absolute inset-0 opacity-20" style="background:repeating-linear-gradient(0deg,transparent,transparent 6px,rgba(217,119,6,0.2) 6px,rgba(217,119,6,0.2) 7px)"></div>
        <div class="absolute w-full h-0.5 bg-amber-400/70" style="animation:scan-line 1.4s linear infinite;"></div>
        <svg class="w-9 h-9 text-amber-400 opacity-70 z-10" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
            <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
        </svg>
    </div>`;
}

function animReflecting() {
    return `<div class="w-full h-full bg-[#050010] relative overflow-hidden flex items-center justify-center">
        <div class="absolute w-16 h-16 rounded-full border border-violet-400/30 animate-ping" style="animation-duration:2s"></div>
        <div class="absolute w-10 h-10 rounded-full border border-violet-400/50 animate-ping" style="animation-duration:2s;animation-delay:0.4s"></div>
        <div class="absolute w-5  h-5  rounded-full border border-violet-300/70 animate-ping" style="animation-duration:2s;animation-delay:0.8s"></div>
        <div class="w-2.5 h-2.5 rounded-full bg-violet-400 z-10" style="filter:drop-shadow(0 0 6px #8b5cf6)"></div>
    </div>`;
}

function animReviewing() {
    return `<div class="w-full h-full p-3 bg-[#050510] flex flex-col gap-1.5 justify-center">
        <div class="h-1.5 bg-indigo-800/80 rounded w-full"></div>
        <div class="h-1.5 bg-indigo-700/60 rounded" style="width:85%"></div>
        <div class="h-1.5 bg-indigo-600/50 rounded" style="width:65%"></div>
        <div class="flex items-center gap-1.5 mt-1.5">
            <div class="w-2 h-2 rounded-full bg-indigo-400 animate-pulse shrink-0"></div>
            <span class="text-indigo-400 text-[10px] font-mono">reviewing...</span>
        </div>
    </div>`;
}

function animCompleted() {
    return `<div class="w-full h-full flex items-center justify-center bg-slate-900">
        <svg class="w-11 h-11 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"
             style="filter:drop-shadow(0 0 10px #34d399)">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7"/>
        </svg>
    </div>`;
}

function animError() {
    return `<div class="w-full h-full flex items-center justify-center bg-slate-900">
        <svg class="w-11 h-11 text-red-400 animate-pulse" fill="none" stroke="currentColor" viewBox="0 0 24 24"
             style="filter:drop-shadow(0 0 10px #ef4444)">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M6 18L18 6M6 6l12 12"/>
        </svg>
    </div>`;
}

function animCodeActWorker() {
    return `<div class="w-full h-full bg-[#010d03] relative overflow-hidden p-2 font-mono text-[9px] leading-[14px]">
        <div class="absolute inset-0 opacity-15" style="background:repeating-linear-gradient(0deg,transparent,transparent 13px,rgba(52,211,153,0.12) 13px,rgba(52,211,153,0.12) 14px)"></div>
        <div style="animation:terminal-scroll 5s linear infinite">
            <div><span class="text-emerald-500">$</span> <span class="text-emerald-300">init_agent</span> <span class="text-cyan-600">--id</span> <span class="text-yellow-300/80">w1</span></div>
            <div><span class="text-cyan-500">▶</span> <span class="text-slate-400">ctx loaded</span> <span class="text-green-400">✓</span></div>
            <div><span class="text-emerald-500">$</span> <span class="text-emerald-300">call_tool</span><span class="text-slate-500">(</span><span class="text-orange-300/80">"search"</span><span class="text-slate-500">)</span></div>
            <div><span class="text-yellow-400">!</span> <span class="text-slate-400">tokens:</span> <span class="text-white/60">1,024</span></div>
            <div><span class="text-emerald-500">$</span> <span class="text-emerald-300">write_output</span><span class="text-slate-500">(data)</span></div>
            <div><span class="text-green-400">✓</span> <span class="text-slate-400">step</span> <span class="text-emerald-300">done</span></div>
            <div><span class="text-emerald-500">$</span> <span class="text-emerald-300">init_agent</span> <span class="text-cyan-600">--id</span> <span class="text-yellow-300/80">w1</span></div>
            <div><span class="text-cyan-500">▶</span> <span class="text-slate-400">ctx loaded</span> <span class="text-green-400">✓</span></div>
            <div><span class="text-emerald-500">$</span> <span class="text-emerald-300">call_tool</span><span class="text-slate-500">(</span><span class="text-orange-300/80">"search"</span><span class="text-slate-500">)</span></div>
            <div><span class="text-yellow-400">!</span> <span class="text-slate-400">tokens:</span> <span class="text-white/60">1,024</span></div>
            <div><span class="text-emerald-500">$</span> <span class="text-emerald-300">write_output</span><span class="text-slate-500">(data)</span></div>
            <div><span class="text-green-400">✓</span> <span class="text-slate-400">step</span> <span class="text-emerald-300">done</span></div>
        </div>
        <div class="absolute top-0 left-0 right-0 h-3" style="background:linear-gradient(to bottom,#010d03,transparent)"></div>
        <div class="absolute bottom-0 left-0 right-0 h-5" style="background:linear-gradient(to top,#010d03,transparent)"></div>
        <div class="absolute bottom-1.5 right-2 w-1 h-2.5 bg-emerald-400" style="animation:blink-caret .6s step-end infinite;box-shadow:0 0 4px #34d399"></div>
    </div>`;
}

function animActiveSupervisor() {
    return `<div class="relative w-full h-full bg-[#06040f] flex items-end justify-center gap-[3px] px-3 pb-2 overflow-hidden">
        <div class="absolute inset-0" style="background:radial-gradient(ellipse at 50% 120%,rgba(140,0,255,0.18) 0%,transparent 60%)"></div>
        <div class="absolute top-2 left-3 text-purple-300/60 text-[9px] font-mono tracking-wider">PROCESSING</div>
        <div class="w-[9%] rounded-sm" style="background:linear-gradient(to top,#1a0040,#cc00ff);box-shadow:0 0 10px #cc00ff88;animation:eq-bar 0.9s  ease-in-out infinite alternate;animation-delay:-0.3s"></div>
        <div class="w-[9%] rounded-sm" style="background:linear-gradient(to top,#000d40,#0066ff);box-shadow:0 0 10px #0066ff88;animation:eq-bar 1.1s  ease-in-out infinite alternate;animation-delay:-0.1s"></div>
        <div class="w-[9%] rounded-sm" style="background:linear-gradient(to top,#003300,#39ff14);box-shadow:0 0 10px #39ff1488;animation:eq-bar 0.85s ease-in-out infinite alternate;animation-delay:-0.6s"></div>
        <div class="w-[9%] rounded-sm" style="background:linear-gradient(to top,#3d1a00,#ff6600);box-shadow:0 0 10px #ff660088;animation:eq-bar 1.0s  ease-in-out infinite alternate;animation-delay:-0.8s"></div>
        <div class="w-[9%] rounded-sm" style="background:linear-gradient(to top,#400020,#ff0080);box-shadow:0 0 10px #ff008088;animation:eq-bar 1.2s  ease-in-out infinite alternate;animation-delay:-0.4s"></div>
        <div class="w-[9%] rounded-sm" style="background:linear-gradient(to top,#003340,#00e5ff);box-shadow:0 0 10px #00e5ff88;animation:eq-bar 0.95s ease-in-out infinite alternate;animation-delay:-0.7s"></div>
        <div class="w-[9%] rounded-sm" style="background:linear-gradient(to top,#1a0060,#7b00ff);box-shadow:0 0 10px #7b00ff88;animation:eq-bar 1.05s ease-in-out infinite alternate;animation-delay:-0.2s"></div>
        <div class="w-[9%] rounded-sm" style="background:linear-gradient(to top,#003320,#00ff88);box-shadow:0 0 10px #00ff8888;animation:eq-bar 0.88s ease-in-out infinite alternate;animation-delay:-0.5s"></div>
    </div>`;
}

function animWaiting() {
    return `<div class="relative w-full h-full bg-[#0c0e14] flex flex-col items-center justify-center gap-2 overflow-hidden">
        <div class="absolute inset-0 opacity-10" style="background:radial-gradient(circle at 50% 50%,#f59e0b 0%,transparent 70%)"></div>
        <div class="flex items-end gap-1.5">
            <div class="w-2 h-2 rounded-full bg-amber-400" style="animation:waiting-dot 1.2s ease-in-out infinite;animation-delay:0s"></div>
            <div class="w-2 h-2 rounded-full bg-amber-400" style="animation:waiting-dot 1.2s ease-in-out infinite;animation-delay:0.2s"></div>
            <div class="w-2 h-2 rounded-full bg-amber-400" style="animation:waiting-dot 1.2s ease-in-out infinite;animation-delay:0.4s"></div>
        </div>
        <div class="text-amber-400/70 text-[10px] font-mono tracking-widest uppercase">waiting…</div>
    </div>`;
}

// ── Event type badge config ──────────────────────────────

const EVENT_TYPE_INFO = {
    start:        { emoji: '🚀', label: 'Start',    color: '#3b82f6' },
    tool_call:    { emoji: '🔧', label: 'Tool',     color: '#10b981' },
    agent_call:   { emoji: '📡', label: 'Dispatch', color: '#7c3aed' },
    activated:    { emoji: '⚡', label: 'Active',   color: '#06b6d4' },
    agent_return: { emoji: '↩️', label: 'Return',   color: '#d97706' },
    completed:    { emoji: '✅', label: 'Done',     color: '#34d399' },
    error:        { emoji: '❌', label: 'Error',    color: '#ef4444' },
};

// ── Main update function ─────────────────────────────────

function updateAgentDOM(el, status, stepText, descText, eventType) {
    el.classList.remove(...ALL_STATUS_CLASSES);
    el.classList.add(`status-${status}`);

    const color = STATUS_COLORS[status] || STATUS_COLORS.idle;
    const indicator = el.querySelector('.status-indicator');
    indicator.style.backgroundColor = color;
    indicator.style.boxShadow = status !== 'idle' ? `0 0 10px ${color}` : 'none';

    el.querySelector('.step-text').textContent = `Step ${stepText}`;
    const descEl = el.querySelector('.desc-text');
    descEl.textContent = descText;
    descEl.title = descText;

    // ── Update event type badge on card ──
    const badge = el.querySelector('.event-badge');
    if (badge && eventType) {
        const info = EVENT_TYPE_INFO[eventType] || { emoji: '•', label: eventType, color: '#64748b' };
        badge.textContent = `${info.emoji}${info.label}`;
        badge.style.color = info.color;
        badge.style.borderColor = info.color + '60';
        badge.style.display = '';
    } else if (badge) {
        badge.style.display = 'none';
    }

    // ── Update avatar based on status ──
    const agentType = el.dataset.agentType || 'worker';
    const avatarImg = el.querySelector('img[alt="avatar"]');
    if (avatarImg && typeof getAvatarUrl === 'function') {
        const newSrc = getAvatarUrl(agentType, status);
        if (newSrc && avatarImg.src !== newSrc && !avatarImg.src.endsWith(newSrc)) {
            avatarImg.src = newSrc;
        }
    }

    const animLayer    = el.querySelector('.anim-layer');
    const isSupervisor = agentType === 'supervisor';

    switch (status) {
        case 'idle':       animLayer.innerHTML = animIdle();           break;
        case 'thinking':   animLayer.innerHTML = animThinking();       break;
        case 'planning':   animLayer.innerHTML = animPlanning();       break;
        case 'searching':  animLayer.innerHTML = animSearching();      break;
        case 'reflecting': animLayer.innerHTML = animReflecting();     break;
        case 'reviewing':  animLayer.innerHTML = animReviewing();      break;
        case 'completed':  animLayer.innerHTML = animCompleted();      break;
        case 'error':      animLayer.innerHTML = animError();          break;
        case 'codeact':    animLayer.innerHTML = animCodeActWorker();  break;
        case 'active':
            animLayer.innerHTML = isSupervisor ? animActiveSupervisor() : animCodeActWorker();
            break;
        case 'waiting':
            animLayer.innerHTML = animWaiting();
            break;
        default:
            animLayer.innerHTML = isSupervisor ? animActiveSupervisor() : animCodeActWorker();
    }
}
