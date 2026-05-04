// ════════════════════════════════════════════════════
// Main — bootstrap entry point
// Depends on: state.js, i18n.js, animations.js,
//             scene.js, timeline.js, controls.js, network.js
// ════════════════════════════════════════════════════

window.addEventListener('DOMContentLoaded', () => {
    // Assign DOM refs declared in state.js
    svgLayer = document.getElementById('svg-layer');
    logPanel = document.getElementById('log-panel');

    // Wire up all event listeners
    initControls();
    initNetwork();

    // Apply default language
    applyLang('en');

    // Try to connect to the Python backend
    tryAutoConnect();

    // Centre stage after layout settles
    setTimeout(centerStage, 300);
});
