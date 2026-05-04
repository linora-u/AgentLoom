// ════════════════════════════════════════════════════
// Controls — step navigation, panel, stage, keyboard
// Depends on: state.js, timeline.js
// ════════════════════════════════════════════════════

// ── Step navigation ──

function stepPrev() {
    if (state.currentStepIdx > 0) showStep(state.currentStepIdx - 1, true);
}
function stepNext() {
    if (state.currentStepIdx < state.totalSteps - 1) showStep(state.currentStepIdx + 1);
}
function stepLatest() {
    if (state.totalSteps > 0) showStep(state.totalSteps - 1);
}

// ── Centre the stage viewport ──

function centerStage() {
    const vp = document.getElementById('main-viewport');
    if (vp.scrollWidth > vp.clientWidth)
        vp.scrollLeft = (vp.scrollWidth  - vp.clientWidth)  / 2;
    if (vp.scrollHeight > vp.clientHeight)
        vp.scrollTop  = (vp.scrollHeight - vp.clientHeight) / 2;
    drawConnections();
}

// ── Left panel collapse ──

var panelOpen = true;

function togglePanel() {
    panelOpen = !panelOpen;
    const panel   = document.getElementById('left-panel');
    const content = document.getElementById('panel-content');
    const btn     = document.getElementById('btn-collapse-panel');
    if (panelOpen) {
        panel.style.width           = '300px';
        content.style.opacity       = '1';
        content.style.pointerEvents = '';
        btn.textContent             = '◀';
    } else {
        panel.style.width           = '18px';
        content.style.opacity       = '0';
        content.style.pointerEvents = 'none';
        btn.textContent             = '▶';
    }
    setTimeout(drawConnections, 310);
}

// ── Initialise all control event listeners ──

function initControls() {
    // Step buttons
    document.getElementById('btn-prev').addEventListener('click', stepPrev);
    document.getElementById('btn-next').addEventListener('click', stepNext);
    document.getElementById('btn-latest').addEventListener('click', stepLatest);

    // Progress bar click-to-seek
    document.getElementById('progress-bar-wrap').addEventListener('click', e => {
        if (!state.totalSteps) return;
        const rect  = e.currentTarget.getBoundingClientRect();
        const ratio = (e.clientX - rect.left) / rect.width;
        showStep(Math.min(state.totalSteps - 1, Math.max(0, Math.floor(ratio * state.totalSteps))));
    });

    // Log entry click-to-seek (also handles run group header toggle)
    logPanel.addEventListener('click', e => {
        // Toggle run group collapse on header click
        const runHeader = e.target.closest('[data-run-header]');
        if (runHeader) {
            const runEl = runHeader.closest('.log-run');
            if (!runEl) return;
            runEl.classList.toggle('log-run-collapsed');
            const toggle = runHeader.querySelector('.log-run-toggle');
            if (toggle) toggle.textContent = runEl.classList.contains('log-run-collapsed') ? '▶' : '▼';
            return;
        }
        // Navigate to step on entry click
        const entry = e.target.closest('.log-run-entry');
        if (!entry) return;
        const step = parseInt(entry.dataset.step);
        if (!isNaN(step) && step >= 1) showStep(step - 1, true);
    });

    // Panel collapse
    document.getElementById('btn-collapse-panel').addEventListener('click', togglePanel);

    // Fullscreen
    document.getElementById('btn-fullscreen').addEventListener('click', () => {
        if (!document.fullscreenElement) document.documentElement.requestFullscreen().catch(() => {});
        else document.exitFullscreen().catch(() => {});
    });

    // Redraw connections on window resize
    window.addEventListener('resize', drawConnections);

    // Stage drag-to-pan
    (function () {
        const vp = document.getElementById('main-viewport');
        let dragging = false, ox = 0, oy = 0, sl = 0, st = 0;
        vp.addEventListener('mousedown', e => {
            if (e.button !== 0) return;
            if (e.target.closest('button,input,label,[data-card-draggable]')) return;
            dragging = true;
            ox = e.clientX; oy = e.clientY;
            sl = vp.scrollLeft; st = vp.scrollTop;
            vp.style.cursor = 'grabbing';
            e.preventDefault();
        });
        window.addEventListener('mouseup', () => {
            if (dragging) { dragging = false; vp.style.cursor = 'grab'; }
        });
        window.addEventListener('mousemove', e => {
            if (!dragging) return;
            vp.scrollLeft = sl - (e.clientX - ox);
            vp.scrollTop  = st - (e.clientY - oy);
        });
    })();

    // Panel width resizer
    (function () {
        const resizer = document.getElementById('panel-resizer');
        const panel   = document.getElementById('left-panel');
        let resizing = false, startX = 0, startW = 0;

        resizer.addEventListener('mousedown', e => {
            if (e.button !== 0) return;
            resizing = true;
            startX   = e.clientX;
            startW   = panel.offsetWidth;
            resizer.classList.add('dragging');
            panel.style.transition       = 'none';
            document.body.style.cursor    = 'col-resize';
            document.body.style.userSelect = 'none';
            e.preventDefault();
        });

        window.addEventListener('mouseup', () => {
            if (!resizing) return;
            resizing = false;
            resizer.classList.remove('dragging');
            panel.style.transition        = '';
            document.body.style.cursor    = '';
            document.body.style.userSelect = '';
            setTimeout(drawConnections, 50);
        });

        window.addEventListener('mousemove', e => {
            if (!resizing) return;
            const newW    = Math.max(120, Math.min(600, startW + (e.clientX - startX)));
            panel.style.width = newW + 'px';
            const content = document.getElementById('panel-content');
            if (newW < 60) {
                content.style.opacity       = '0';
                content.style.pointerEvents = 'none';
            } else {
                content.style.opacity       = '1';
                content.style.pointerEvents = '';
            }
        });
    })();

    // Card drag (worker cards only — supervisor stays fixed)
    (function () {
        let drag = null;

        // Capture phase so we intercept before the viewport-pan listener
        document.addEventListener('mousedown', e => {
            if (e.button !== 0) return;
            if (e.target.closest('button,input,label')) return;
            const card = e.target.closest('[data-card-draggable]');
            if (!card) return;
            drag = {
                el:     card,
                startX: e.clientX,
                startY: e.clientY,
                startL: parseFloat(card.style.left),
                startT: parseFloat(card.style.top),
                rafId:  null,
            };
            card.style.transition = 'none'; // disable 300ms position transition during drag
            card.style.cursor = 'grabbing';
            card.style.zIndex = '20';
            e.stopPropagation();   // prevent viewport pan
            e.preventDefault();
        }, true);

        window.addEventListener('mousemove', e => {
            if (!drag) return;
            drag.el.style.left = (drag.startL + (e.clientX - drag.startX)) + 'px';
            drag.el.style.top  = (drag.startT + (e.clientY - drag.startY)) + 'px';
            // With transition disabled, getBoundingClientRect() is accurate immediately.
            // Use RAF only to avoid multiple redraws in the same frame.
            if (drag.rafId) cancelAnimationFrame(drag.rafId);
            drag.rafId = requestAnimationFrame(drawConnections);
        });

        window.addEventListener('mouseup', () => {
            if (!drag) return;
            if (drag.rafId) cancelAnimationFrame(drag.rafId);
            drag.el.style.transition = ''; // restore transition
            drag.el.style.cursor = 'grab';
            drag.el.style.zIndex = '';
            drag = null;
            drawConnections();
        });
    })();

    // Keyboard shortcuts
    document.addEventListener('keydown', e => {
        if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
        switch (e.key) {
            case 'ArrowLeft':  e.preventDefault(); stepPrev();   break;
            case 'ArrowRight': e.preventDefault(); stepNext();   break;
            case 'End':        e.preventDefault(); stepLatest(); break;
            case 'p': case 'P': togglePanel(); break;
            case 'f': case 'F':
                if (!document.fullscreenElement) document.documentElement.requestFullscreen().catch(() => {});
                else document.exitFullscreen().catch(() => {});
                break;
        }
    });
}
