# Skill GitHub Probe Validation Report

- Generated at: 2026-06-14T01:43:11
- Runtime: AgentLoom `SkillsManager` loading + `run_skill_script` execution.
- Target config: `applications/skill_github_probe/config/system.yaml#skill_github_probe.targets`
- Status meaning: `pass` means the skill loaded and expected commands succeeded; `diagnostic` means the skill loaded and missing credentials/local dependencies were diagnosed as expected; `fail` means unexpected behavior.

## Summary

| Target | Category | Status | Commit | Skill | Root cause |
| --- | --- | --- | --- | --- | --- |
| anthropic-skill-creator | skill-authoring | pass | `575462609294` | `skill-creator` | ok |
| anthropic-webapp-testing | frontend-testing | pass | `575462609294` | `webapp-testing` | ok |
| anthropic-pdf | document-processing | diagnostic | `575462609294` | `pdf` | pdf_script_dependency_check: pdf2image is not installed in the current runtime |
| anthropic-xlsx | spreadsheet-processing | diagnostic | `575462609294` | `xlsx` | xlsx_script_dependency_check: openpyxl is not installed in the current runtime |
| addy-test-driven-development | coding | pass | `d187883b7d76` | `test-driven-development` | ok |
| addy-code-review-and-quality | code-review | pass | `d187883b7d76` | `code-review-and-quality` | ok |
| addy-frontend-ui-engineering | frontend-development | pass | `d187883b7d76` | `frontend-ui-engineering` | ok |
| addy-performance-optimization | performance | pass | `d187883b7d76` | `performance-optimization` | ok |
| addy-security-and-hardening | security | pass | `d187883b7d76` | `security-and-hardening` | ok |
| pi-brave-search | search-and-news | diagnostic | `90bb51cae365` | `brave-search` | news_search_without_api_key: BRAVE_API_KEY is required for live Brave Search/news queries |
| pi-browser-tools | browser-automation | pass | `90bb51cae365` | `browser-tools` | ok |
| pi-youtube-transcript | media-transcript | pass | `90bb51cae365` | `youtube-transcript` | ok |
| pi-vscode | ide-integration | diagnostic | `90bb51cae365` | `vscode` | code_cli_check: VS Code code CLI is not installed or not on PATH |

## Details

### anthropic-skill-creator

- Category: skill-authoring
- Source URL: https://github.com/anthropics/skills.git
- Config ref: `57546260929473d4e0d1c1bb75297be2fdfa1949`
- Commit SHA: `57546260929473d4e0d1c1bb75297be2fdfa1949`
- Skill path: `skills/skill-creator`
- Content hash: `4557d54c524c67984e4bab23fa436e90196292ae8df4d2093f0ce0e6951bfc1c`
- Load mode: `eager`
- Loaded skill: `skill-creator`
- Status: **pass**
- Root cause: ok

- Dependencies before: `{"skill": "skill-creator", "base_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/skills/skills/skill-creator", "bins": [{"name": "python", "found": true, "path": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/.venv/bin/python"}], "package_json": null, "package_dependency_count": 0, "node_modules_present": false, "pyproject": null, "requirements_txt": null, "scripts_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/skills/skills/skill-creator/scripts", "ok": true, "missing": []}`
- Dependencies after: `{"skill": "skill-creator", "base_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/skills/skills/skill-creator", "bins": [{"name": "python", "found": true, "path": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/.venv/bin/python"}], "package_json": null, "package_dependency_count": 0, "node_modules_present": false, "pyproject": null, "requirements_txt": null, "scripts_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/skills/skills/skill-creator/scripts", "ok": true, "missing": []}`

Commands:
- `quick_validate`: `python scripts/quick_validate.py .` -> returncode=0, passed=True, reason=pass: ok, audit=/Users/bytedance/code/data_clear/AgentLoom-main-skill/.runtime/skill_runs/skill-creator/runs/20260614_014259_580490

### anthropic-webapp-testing

- Category: frontend-testing
- Source URL: https://github.com/anthropics/skills.git
- Config ref: `57546260929473d4e0d1c1bb75297be2fdfa1949`
- Commit SHA: `57546260929473d4e0d1c1bb75297be2fdfa1949`
- Skill path: `skills/webapp-testing`
- Content hash: `abfaf4cc6cf572346e22fed42dc0cfaacd2f64c5d9667f44a94b02ea0d3b1342`
- Load mode: `on-demand`
- Loaded skill: `webapp-testing`
- Status: **pass**
- Root cause: ok

- Dependencies before: `{"skill": "webapp-testing", "base_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/skills/skills/webapp-testing", "bins": [{"name": "python", "found": true, "path": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/.venv/bin/python"}], "package_json": null, "package_dependency_count": 0, "node_modules_present": false, "pyproject": null, "requirements_txt": null, "scripts_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/skills/skills/webapp-testing/scripts", "ok": true, "missing": []}`
- Dependencies after: `{"skill": "webapp-testing", "base_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/skills/skills/webapp-testing", "bins": [{"name": "python", "found": true, "path": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/.venv/bin/python"}], "package_json": null, "package_dependency_count": 0, "node_modules_present": false, "pyproject": null, "requirements_txt": null, "scripts_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/skills/skills/webapp-testing/scripts", "ok": true, "missing": []}`

Commands:
- `helper_help`: `python scripts/with_server.py --help` -> returncode=0, passed=True, reason=pass: ok, audit=/Users/bytedance/code/data_clear/AgentLoom-main-skill/.runtime/skill_runs/webapp-testing/runs/20260614_014259_633042

### anthropic-pdf

- Category: document-processing
- Source URL: https://github.com/anthropics/skills.git
- Config ref: `57546260929473d4e0d1c1bb75297be2fdfa1949`
- Commit SHA: `57546260929473d4e0d1c1bb75297be2fdfa1949`
- Skill path: `skills/pdf`
- Content hash: `6a01b6dc757b8856d7eba5e9985d8550783f946d7afdf86402ba324b93239fc0`
- Load mode: `on-demand`
- Loaded skill: `pdf`
- Status: **diagnostic**
- Root cause: pdf_script_dependency_check: pdf2image is not installed in the current runtime

- Dependencies before: `{"skill": "pdf", "base_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/skills/skills/pdf", "bins": [{"name": "python", "found": true, "path": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/.venv/bin/python"}], "package_json": null, "package_dependency_count": 0, "node_modules_present": false, "pyproject": null, "requirements_txt": null, "scripts_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/skills/skills/pdf/scripts", "ok": true, "missing": []}`
- Dependencies after: `{"skill": "pdf", "base_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/skills/skills/pdf", "bins": [{"name": "python", "found": true, "path": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/.venv/bin/python"}], "package_json": null, "package_dependency_count": 0, "node_modules_present": false, "pyproject": null, "requirements_txt": null, "scripts_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/skills/skills/pdf/scripts", "ok": true, "missing": []}`

Commands:
- `pdf_script_dependency_check`: `python scripts/convert_pdf_to_images.py --help` -> returncode=1, passed=True, reason=diagnostic: pdf2image is not installed in the current runtime, audit=/Users/bytedance/code/data_clear/AgentLoom-main-skill/.runtime/skill_runs/pdf/runs/20260614_014259_679754

### anthropic-xlsx

- Category: spreadsheet-processing
- Source URL: https://github.com/anthropics/skills.git
- Config ref: `57546260929473d4e0d1c1bb75297be2fdfa1949`
- Commit SHA: `57546260929473d4e0d1c1bb75297be2fdfa1949`
- Skill path: `skills/xlsx`
- Content hash: `eb217c1b8086a7ffdf2a66a77f0c83f4f48da508c9009e88a1c3adc2f49aa787`
- Load mode: `on-demand`
- Loaded skill: `xlsx`
- Status: **diagnostic**
- Root cause: xlsx_script_dependency_check: openpyxl is not installed in the current runtime

- Dependencies before: `{"skill": "xlsx", "base_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/skills/skills/xlsx", "bins": [{"name": "python", "found": true, "path": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/.venv/bin/python"}], "package_json": null, "package_dependency_count": 0, "node_modules_present": false, "pyproject": null, "requirements_txt": null, "scripts_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/skills/skills/xlsx/scripts", "ok": true, "missing": []}`
- Dependencies after: `{"skill": "xlsx", "base_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/skills/skills/xlsx", "bins": [{"name": "python", "found": true, "path": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/.venv/bin/python"}], "package_json": null, "package_dependency_count": 0, "node_modules_present": false, "pyproject": null, "requirements_txt": null, "scripts_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/skills/skills/xlsx/scripts", "ok": true, "missing": []}`

Commands:
- `xlsx_script_dependency_check`: `python scripts/recalc.py --help` -> returncode=1, passed=True, reason=diagnostic: openpyxl is not installed in the current runtime, audit=/Users/bytedance/code/data_clear/AgentLoom-main-skill/.runtime/skill_runs/xlsx/runs/20260614_014259_722862

### addy-test-driven-development

- Category: coding
- Source URL: https://github.com/addyosmani/agent-skills.git
- Config ref: `d187883b7d761265309cdcc0f202cc76b4b3fb06`
- Commit SHA: `d187883b7d761265309cdcc0f202cc76b4b3fb06`
- Skill path: `skills/test-driven-development`
- Content hash: `6c8e03bc2f631799c1c3c29f9f525193596781213f7fd24ece5a8ddf6a8d773e`
- Load mode: `on-demand`
- Loaded skill: `test-driven-development`
- Status: **pass**
- Root cause: ok

- Dependencies before: `{"skill": "test-driven-development", "base_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/agent-skills/skills/test-driven-development", "bins": [], "package_json": null, "package_dependency_count": 0, "node_modules_present": false, "pyproject": null, "requirements_txt": null, "scripts_dir": null, "ok": true, "missing": []}`
- Dependencies after: `{"skill": "test-driven-development", "base_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/agent-skills/skills/test-driven-development", "bins": [], "package_json": null, "package_dependency_count": 0, "node_modules_present": false, "pyproject": null, "requirements_txt": null, "scripts_dir": null, "ok": true, "missing": []}`

### addy-code-review-and-quality

- Category: code-review
- Source URL: https://github.com/addyosmani/agent-skills.git
- Config ref: `d187883b7d761265309cdcc0f202cc76b4b3fb06`
- Commit SHA: `d187883b7d761265309cdcc0f202cc76b4b3fb06`
- Skill path: `skills/code-review-and-quality`
- Content hash: `9b086a4c540ef77b4589307a1c165ab241b5adcb84f4ae70da67924fe8a83fb0`
- Load mode: `on-demand`
- Loaded skill: `code-review-and-quality`
- Status: **pass**
- Root cause: ok

- Dependencies before: `{"skill": "code-review-and-quality", "base_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/agent-skills/skills/code-review-and-quality", "bins": [], "package_json": null, "package_dependency_count": 0, "node_modules_present": false, "pyproject": null, "requirements_txt": null, "scripts_dir": null, "ok": true, "missing": []}`
- Dependencies after: `{"skill": "code-review-and-quality", "base_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/agent-skills/skills/code-review-and-quality", "bins": [], "package_json": null, "package_dependency_count": 0, "node_modules_present": false, "pyproject": null, "requirements_txt": null, "scripts_dir": null, "ok": true, "missing": []}`

### addy-frontend-ui-engineering

- Category: frontend-development
- Source URL: https://github.com/addyosmani/agent-skills.git
- Config ref: `d187883b7d761265309cdcc0f202cc76b4b3fb06`
- Commit SHA: `d187883b7d761265309cdcc0f202cc76b4b3fb06`
- Skill path: `skills/frontend-ui-engineering`
- Content hash: `58542bff1be0b719134d022ec0ec6f304808ef5e8c900ac426886716e80d3b76`
- Load mode: `on-demand`
- Loaded skill: `frontend-ui-engineering`
- Status: **pass**
- Root cause: ok

- Dependencies before: `{"skill": "frontend-ui-engineering", "base_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/agent-skills/skills/frontend-ui-engineering", "bins": [], "package_json": null, "package_dependency_count": 0, "node_modules_present": false, "pyproject": null, "requirements_txt": null, "scripts_dir": null, "ok": true, "missing": []}`
- Dependencies after: `{"skill": "frontend-ui-engineering", "base_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/agent-skills/skills/frontend-ui-engineering", "bins": [], "package_json": null, "package_dependency_count": 0, "node_modules_present": false, "pyproject": null, "requirements_txt": null, "scripts_dir": null, "ok": true, "missing": []}`

### addy-performance-optimization

- Category: performance
- Source URL: https://github.com/addyosmani/agent-skills.git
- Config ref: `d187883b7d761265309cdcc0f202cc76b4b3fb06`
- Commit SHA: `d187883b7d761265309cdcc0f202cc76b4b3fb06`
- Skill path: `skills/performance-optimization`
- Content hash: `a0ed8ae6affa5e2e67f8739675ee774427401c70d0890c73af6b4f629a2ed2d7`
- Load mode: `on-demand`
- Loaded skill: `performance-optimization`
- Status: **pass**
- Root cause: ok

- Dependencies before: `{"skill": "performance-optimization", "base_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/agent-skills/skills/performance-optimization", "bins": [], "package_json": null, "package_dependency_count": 0, "node_modules_present": false, "pyproject": null, "requirements_txt": null, "scripts_dir": null, "ok": true, "missing": []}`
- Dependencies after: `{"skill": "performance-optimization", "base_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/agent-skills/skills/performance-optimization", "bins": [], "package_json": null, "package_dependency_count": 0, "node_modules_present": false, "pyproject": null, "requirements_txt": null, "scripts_dir": null, "ok": true, "missing": []}`

### addy-security-and-hardening

- Category: security
- Source URL: https://github.com/addyosmani/agent-skills.git
- Config ref: `d187883b7d761265309cdcc0f202cc76b4b3fb06`
- Commit SHA: `d187883b7d761265309cdcc0f202cc76b4b3fb06`
- Skill path: `skills/security-and-hardening`
- Content hash: `05bc67d417db252f0a1fa6b3edeb892036dad685c8aa39e4c08c8e5be5b719d0`
- Load mode: `on-demand`
- Loaded skill: `security-and-hardening`
- Status: **pass**
- Root cause: ok

- Dependencies before: `{"skill": "security-and-hardening", "base_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/agent-skills/skills/security-and-hardening", "bins": [], "package_json": null, "package_dependency_count": 0, "node_modules_present": false, "pyproject": null, "requirements_txt": null, "scripts_dir": null, "ok": true, "missing": []}`
- Dependencies after: `{"skill": "security-and-hardening", "base_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/agent-skills/skills/security-and-hardening", "bins": [], "package_json": null, "package_dependency_count": 0, "node_modules_present": false, "pyproject": null, "requirements_txt": null, "scripts_dir": null, "ok": true, "missing": []}`

### pi-brave-search

- Category: search-and-news
- Source URL: https://github.com/badlogic/pi-skills.git
- Config ref: `90bb51cae36515a648515b633a81c0c6efc8c74d`
- Commit SHA: `90bb51cae36515a648515b633a81c0c6efc8c74d`
- Skill path: `brave-search`
- Content hash: `3ef4a75592717fae1215a4a1f7d5c5bde10e8857332dd8b5d8e44c39bc6e672a`
- Load mode: `on-demand`
- Loaded skill: `brave-search`
- Status: **diagnostic**
- Root cause: news_search_without_api_key: BRAVE_API_KEY is required for live Brave Search/news queries

- Dependencies before: `{"skill": "brave-search", "base_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/pi-skills/brave-search", "bins": [{"name": "node", "found": true, "path": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/.venv/bin/node"}], "package_json": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/pi-skills/brave-search/package.json", "package_dependency_count": 4, "node_modules_present": false, "pyproject": null, "requirements_txt": null, "scripts_dir": null, "ok": false, "missing": ["package:node_modules"]}`
- Dependencies after: `{"skill": "brave-search", "base_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/pi-skills/brave-search", "bins": [{"name": "node", "found": true, "path": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/.venv/bin/node"}], "package_json": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/pi-skills/brave-search/package.json", "package_dependency_count": 4, "node_modules_present": true, "pyproject": null, "requirements_txt": null, "scripts_dir": null, "ok": true, "missing": []}`

Commands:
- `npm_install`: `npm install --ignore-scripts --no-audit --no-fund` -> returncode=0, passed=True, reason=pass: ok, audit=/Users/bytedance/code/data_clear/AgentLoom-main-skill/.runtime/skill_runs/brave-search/runs/20260614_014308_190300
- `news_search_without_api_key`: `PATH=/opt/homebrew/bin:/usr/local/bin:$PATH node search.js "AI news" -n 1 --freshness pd` -> returncode=1, passed=True, reason=diagnostic: BRAVE_API_KEY is required for live Brave Search/news queries, audit=/Users/bytedance/code/data_clear/AgentLoom-main-skill/.runtime/skill_runs/brave-search/runs/20260614_014308_945113

### pi-browser-tools

- Category: browser-automation
- Source URL: https://github.com/badlogic/pi-skills.git
- Config ref: `90bb51cae36515a648515b633a81c0c6efc8c74d`
- Commit SHA: `90bb51cae36515a648515b633a81c0c6efc8c74d`
- Skill path: `browser-tools`
- Content hash: `a5a536d3c73e561252864633357010070a33e6a79e059e50cd89dc7695c7cf30`
- Load mode: `on-demand`
- Loaded skill: `browser-tools`
- Status: **pass**
- Root cause: ok

- Dependencies before: `{"skill": "browser-tools", "base_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/pi-skills/browser-tools", "bins": [{"name": "node", "found": true, "path": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/.venv/bin/node"}], "package_json": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/pi-skills/browser-tools/package.json", "package_dependency_count": 9, "node_modules_present": false, "pyproject": null, "requirements_txt": null, "scripts_dir": null, "ok": false, "missing": ["package:node_modules"]}`
- Dependencies after: `{"skill": "browser-tools", "base_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/pi-skills/browser-tools", "bins": [{"name": "node", "found": true, "path": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/.venv/bin/node"}], "package_json": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/pi-skills/browser-tools/package.json", "package_dependency_count": 9, "node_modules_present": true, "pyproject": null, "requirements_txt": null, "scripts_dir": null, "ok": true, "missing": []}`

Commands:
- `npm_install`: `npm install --ignore-scripts --no-audit --no-fund` -> returncode=0, passed=True, reason=pass: ok, audit=/Users/bytedance/code/data_clear/AgentLoom-main-skill/.runtime/skill_runs/browser-tools/runs/20260614_014309_316178
- `browser_nav_usage`: `node browser-nav.js` -> returncode=1, passed=True, reason=pass: ok, audit=/Users/bytedance/code/data_clear/AgentLoom-main-skill/.runtime/skill_runs/browser-tools/runs/20260614_014310_966497

### pi-youtube-transcript

- Category: media-transcript
- Source URL: https://github.com/badlogic/pi-skills.git
- Config ref: `90bb51cae36515a648515b633a81c0c6efc8c74d`
- Commit SHA: `90bb51cae36515a648515b633a81c0c6efc8c74d`
- Skill path: `youtube-transcript`
- Content hash: `e644fa0e0fecfbf02f0b84c183ea74b1e168f6258ad5def9810738a63d485b3f`
- Load mode: `on-demand`
- Loaded skill: `youtube-transcript`
- Status: **pass**
- Root cause: ok

- Dependencies before: `{"skill": "youtube-transcript", "base_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/pi-skills/youtube-transcript", "bins": [{"name": "node", "found": true, "path": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/.venv/bin/node"}], "package_json": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/pi-skills/youtube-transcript/package.json", "package_dependency_count": 1, "node_modules_present": false, "pyproject": null, "requirements_txt": null, "scripts_dir": null, "ok": false, "missing": ["package:node_modules"]}`
- Dependencies after: `{"skill": "youtube-transcript", "base_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/pi-skills/youtube-transcript", "bins": [{"name": "node", "found": true, "path": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/.venv/bin/node"}], "package_json": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/pi-skills/youtube-transcript/package.json", "package_dependency_count": 1, "node_modules_present": true, "pyproject": null, "requirements_txt": null, "scripts_dir": null, "ok": true, "missing": []}`

Commands:
- `npm_install`: `npm install --ignore-scripts --no-audit --no-fund` -> returncode=0, passed=True, reason=pass: ok, audit=/Users/bytedance/code/data_clear/AgentLoom-main-skill/.runtime/skill_runs/youtube-transcript/runs/20260614_014311_141945
- `usage_path`: `node transcript.js` -> returncode=1, passed=True, reason=pass: ok, audit=/Users/bytedance/code/data_clear/AgentLoom-main-skill/.runtime/skill_runs/youtube-transcript/runs/20260614_014311_888662

### pi-vscode

- Category: ide-integration
- Source URL: https://github.com/badlogic/pi-skills.git
- Config ref: `90bb51cae36515a648515b633a81c0c6efc8c74d`
- Commit SHA: `90bb51cae36515a648515b633a81c0c6efc8c74d`
- Skill path: `vscode`
- Content hash: `432b806c4b180ec553cb895934b1276e9f7cb0c30dc1e60de9a714bbc47de46f`
- Load mode: `on-demand`
- Loaded skill: `vscode`
- Status: **diagnostic**
- Root cause: code_cli_check: VS Code code CLI is not installed or not on PATH

- Dependencies before: `{"skill": "vscode", "base_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/pi-skills/vscode", "bins": [], "package_json": null, "package_dependency_count": 0, "node_modules_present": false, "pyproject": null, "requirements_txt": null, "scripts_dir": null, "ok": true, "missing": []}`
- Dependencies after: `{"skill": "vscode", "base_dir": "/Users/bytedance/code/data_clear/AgentLoom-main-skill/applications/skill_github_probe/.runtime/repos/pi-skills/vscode", "bins": [], "package_json": null, "package_dependency_count": 0, "node_modules_present": false, "pyproject": null, "requirements_txt": null, "scripts_dir": null, "ok": true, "missing": []}`

Commands:
- `code_cli_check`: `code --version` -> returncode=127, passed=True, reason=diagnostic: VS Code code CLI is not installed or not on PATH, audit=/Users/bytedance/code/data_clear/AgentLoom-main-skill/.runtime/skill_runs/vscode/runs/20260614_014311_960428
