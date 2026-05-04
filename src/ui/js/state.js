// ════════════════════════════════════════════════════
// Global state — shared across all modules
// ════════════════════════════════════════════════════

var logData      = null;    // currently loaded JSON data
var currentLang  = 'en';   // active UI language

// DOM refs — assigned in main.js after DOMContentLoaded
var svgLayer, logPanel;

var state = {
    currentStepIdx:   -1,   // 0-based index of the currently displayed step
    totalSteps:        0,
    agents:            {},  // { agentName: cardElement } — last card wins for duplicate names
    workerEls:         [],  // [{ name, el }] — ALL worker cards in order (handles duplicates)
    agentTotalSteps:   {},  // { agentName: N } — total events per agent, computed on load
    activeConnection:  null,
    sseSource:         null,
    runs:              [],  // [{agentName, agentType, startIdx, endIdx}] — consecutive same-agent runs
    stepToRunIdx:      [],  // maps each 0-based timeline index to its run index
};
