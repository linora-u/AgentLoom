// ════════════════════════════════════════════════════
// Internationalisation (i18n)
// Depends on: state.js (currentLang, logData)
// ════════════════════════════════════════════════════

const I18N = {
    zh: {
        title:        'Agent 执行可视化',
        liveSync:     '实时同步中',
        disconnected: '连接断开',
        latest:       '最新',
        localJSON:    '本地 JSON',
        server:       '服务器',
        logFiles:     '后端日志文件',
        loadLatest:   '加载最新',
        refreshList:  '刷新列表',
        logListHint:  '点击"刷新列表"获取文件',
        waitingData:  '等待数据...',
        waitingTask:  '等待任务...',
        waitingCmd:   '等待指令...',
        collapsePanel:'折叠/展开面板 (P)',
        fullscreen:   '全屏 (F)',
        prevStep:     '上一步 (←)',
        nextStep:     '下一步 (→)',
        jumpLatest:   '跳到最新 (End)',
        loadLocal:    '加载本地 JSON 文件',
    },
    en: {
        title:        'Agent Visualizer',
        liveSync:     'Live',
        disconnected: 'Disconnected',
        latest:       'Latest',
        localJSON:    'Load JSON',
        server:       'Server',
        logFiles:     'Log Files',
        loadLatest:   'Load Latest',
        refreshList:  'Refresh',
        logListHint:  'Click "Refresh" to list files',
        waitingData:  'Waiting...',
        waitingTask:  'Waiting for task...',
        waitingCmd:   'Waiting...',
        collapsePanel:'Collapse/Expand Panel (P)',
        fullscreen:   'Fullscreen (F)',
        prevStep:     'Previous (←)',
        nextStep:     'Next (→)',
        jumpLatest:   'Jump to Latest (End)',
        loadLocal:    'Load local JSON file',
    }
};

function applyLang(lang) {
    currentLang = lang;

    document.getElementById('btn-lang').textContent = lang === 'zh' ? 'EN' : '中';

    document.querySelectorAll('[data-i18n]').forEach(el => {
        const key = el.dataset.i18n;
        if (I18N[lang][key] !== undefined) el.textContent = I18N[lang][key];
    });

    document.querySelectorAll('[data-i18n-title]').forEach(el => {
        const key = el.dataset.i18nTitle;
        if (I18N[lang][key] !== undefined) el.title = I18N[lang][key];
    });

    if (!logData) {
        document.getElementById('progress-text').textContent = I18N[lang].waitingData;
    }

    // Update idle card descriptions when language switches
    document.querySelectorAll('.desc-text').forEach(el => {
        if (el.textContent === I18N['zh'].waitingCmd  || el.textContent === I18N['en'].waitingCmd ||
            el.textContent === I18N['zh'].waitingTask || el.textContent === I18N['en'].waitingTask) {
            el.textContent = I18N[lang].waitingCmd;
        }
    });
}
