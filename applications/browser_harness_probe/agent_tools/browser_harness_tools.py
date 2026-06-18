"""Deterministic tools for probing browser-harness from AgentLoom."""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_DEMO_URL = "https://github.com/browser-use/browser-harness"
DEFAULT_TIMEOUT_SECONDS = 90
REAL_MODE_NAME = "agentloom-real-probe"

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, default=str)


def _browser_harness_command() -> str | None:
    path = shutil.which("browser-harness")
    if path:
        return path
    user_local = Path.home() / ".local" / "bin" / "browser-harness"
    if user_local.exists():
        return str(user_local)
    return None


def _chrome_command() -> str | None:
    env_path = os.environ.get("BH_CHROME_PATH") or os.environ.get("CHROME_PATH")
    candidates = [
        env_path,
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
        shutil.which("google-chrome-stable"),
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_devtools(port: int, process: subprocess.Popen, timeout_seconds: float) -> None:
    deadline = time.time() + timeout_seconds
    last_error = ""
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"isolated Chrome exited early with code {process.returncode}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=0.5) as response:
                if response.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001 - this is a polling loop.
            last_error = str(exc)
        time.sleep(0.2)
    raise TimeoutError(f"isolated Chrome DevTools endpoint did not open on port {port}: {last_error}")


def _start_isolated_chrome() -> tuple[subprocess.Popen, dict[str, str], str, int]:
    chrome = _chrome_command()
    if not chrome:
        raise FileNotFoundError("Chrome executable not found. Set BH_CHROME_PATH or install Google Chrome.")

    port = _free_port()
    cache_root = Path.home() / "Library" / "Caches" / "AgentLoom" / "browser_harness_probe"
    if os.name != "posix" or not str(cache_root).startswith(str(Path.home())):
        cache_root = Path(tempfile.gettempdir()) / "agentloom-browser-harness-probe"
    cache_root.mkdir(parents=True, exist_ok=True)
    profile_dir = tempfile.mkdtemp(prefix="chrome-profile-", dir=str(cache_root))

    process = subprocess.Popen(
        [
            chrome,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wait_for_devtools(port, process, timeout_seconds=20)

    env = os.environ.copy()
    env["BU_CDP_URL"] = f"http://127.0.0.1:{port}"
    env["BU_NAME"] = f"agentloom-isolated-{port}"
    return process, env, profile_dir, port


def _stop_daemon(env: dict[str, str]) -> dict[str, Any]:
    command = _browser_harness_command()
    if not command:
        return {"success": False, "error": "browser-harness command not found"}
    completed = subprocess.run(
        [command, "--reload"],
        text=True,
        capture_output=True,
        env=env,
        timeout=15,
        check=False,
    )
    return {
        "success": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _run_harness(script: str, env: dict[str, str], timeout_seconds: int) -> dict[str, Any]:
    command = _browser_harness_command()
    if not command:
        return {
            "success": False,
            "error": "browser-harness command not found; run `uv tool install -e /Users/bytedance/code/browser-harness`.",
        }
    started_at = time.monotonic()
    try:
        completed = subprocess.run(
            [command],
            input=script,
            text=True,
            capture_output=True,
            env=env,
            timeout=timeout_seconds,
            check=False,
        )
        elapsed = round(time.monotonic() - started_at, 3)
        return {
            "success": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "elapsed_seconds": elapsed,
            "command": command,
        }
    except subprocess.TimeoutExpired as exc:
        elapsed = round(time.monotonic() - started_at, 3)
        return {
            "success": False,
            "returncode": None,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "elapsed_seconds": elapsed,
            "error": f"browser-harness timed out after {timeout_seconds}s",
            "command": command,
        }


def browser_harness_doctor(timeout_seconds: str = "30") -> str:
    """Run `browser-harness --doctor` and return structured diagnostics.

    Args:
        timeout_seconds: Maximum seconds to wait for the doctor command.

    Returns:
        JSON text with command, return code, stdout, stderr, and success.
    """

    command = _browser_harness_command()
    if not command:
        return _json({"success": False, "error": "browser-harness command not found"})

    started_at = time.monotonic()
    try:
        completed = subprocess.run(
            [command, "--doctor"],
            text=True,
            capture_output=True,
            timeout=int(timeout_seconds),
            check=False,
        )
        return _json(
            {
                "success": completed.returncode == 0,
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "elapsed_seconds": round(time.monotonic() - started_at, 3),
                "command": command,
            }
        )
    except subprocess.TimeoutExpired as exc:
        return _json(
            {
                "success": False,
                "returncode": None,
                "stdout": exc.stdout or "",
                "stderr": exc.stderr or "",
                "elapsed_seconds": round(time.monotonic() - started_at, 3),
                "error": f"browser-harness --doctor timed out after {timeout_seconds}s",
                "command": command,
            }
        )


def run_browser_harness_python(script: str, browser_mode: str = "real", timeout_seconds: str = "60") -> str:
    """Run Python code through browser-harness via stdin.

    Args:
        script: Python code to execute with browser-harness helpers pre-imported.
        browser_mode: `real` connects to the user's Chrome; `isolated` launches a separate Chrome profile.
        timeout_seconds: Maximum seconds to wait for the harness command.

    Returns:
        JSON text with mode, stdout, stderr, return code, elapsed time, and setup metadata.
    """

    mode = browser_mode.strip().lower()
    if mode not in {"real", "isolated"}:
        return _json({"success": False, "error": "browser_mode must be `real` or `isolated`", "mode": mode})
    if not script.strip():
        return _json({"success": False, "error": "script must be non-empty", "mode": mode})

    timeout = int(timeout_seconds)
    if mode == "real":
        env = os.environ.copy()
        env.pop("BU_CDP_URL", None)
        env.pop("BU_CDP_WS", None)
        env["BU_NAME"] = REAL_MODE_NAME
        result = _run_harness(script, env=env, timeout_seconds=timeout)
        result.update(
            {
                "mode": mode,
                "setup": {
                    "connection": "real Chrome via chrome://inspect remote debugging",
                    "bu_name": REAL_MODE_NAME,
                },
            }
        )
        return _json(result)

    chrome_process: subprocess.Popen | None = None
    env: dict[str, str] | None = None
    profile_dir = ""
    port: int | None = None
    daemon_stop: dict[str, Any] | None = None
    try:
        chrome_process, env, profile_dir, port = _start_isolated_chrome()
        result = _run_harness(script, env=env, timeout_seconds=timeout)
        daemon_stop = _stop_daemon(env)
    except Exception as exc:  # noqa: BLE001 - return failures as tool output.
        result = {"success": False, "error": str(exc)}
    finally:
        if env is not None and daemon_stop is None:
            try:
                daemon_stop = _stop_daemon(env)
            except Exception as exc:  # noqa: BLE001
                daemon_stop = {"success": False, "error": str(exc)}
        if chrome_process is not None and chrome_process.poll() is None:
            chrome_process.terminate()
            try:
                chrome_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                chrome_process.kill()

    result.update(
        {
            "mode": mode,
            "setup": {
                "connection": "isolated Chrome via BU_CDP_URL",
                "profile_dir": profile_dir,
                "port": port,
                "bu_cdp_url": f"http://127.0.0.1:{port}" if port is not None else None,
                "daemon_stop": daemon_stop,
            },
        }
    )
    return _json(result)


def run_demo_probe(
    url: str = DEFAULT_DEMO_URL,
    browser_mode: str = "isolated",
    timeout_seconds: str = str(DEFAULT_TIMEOUT_SECONDS),
) -> str:
    """Open a URL in a new tab with browser-harness and print page_info().

    Args:
        url: URL to open. Defaults to the browser-harness GitHub repository.
        browser_mode: `real` or `isolated`.
        timeout_seconds: Maximum seconds to wait for the harness command.

    Returns:
        JSON text from run_browser_harness_python.
    """

    safe_url = json.dumps(url)
    script = f"""
import json

target_url = {safe_url}
new_tab(target_url)
wait_for_load()
info = page_info()
print(json.dumps({{"target_url": target_url, "page_info": info}}, ensure_ascii=False, default=str))
"""
    return run_browser_harness_python(
        script=script,
        browser_mode=browser_mode,
        timeout_seconds=timeout_seconds,
    )


_ZSXQ_SCRAPER_SCRIPT_TEMPLATE = r'''
import json, time, re, sys

TARGET_URL = __TARGET_URL_JSON__
GROUP_ID = __GROUP_ID_JSON__
SINCE_DATE = __SINCE_DATE_JSON__          # ISO date string like "2024-01-01"
OWNER_NAME = __OWNER_NAME_JSON__          # may be empty -> auto-detect
MAX_ROUNDS = __MAX_ROUNDS__
SCROLL_PAUSE = __SCROLL_PAUSE__
STALL_LIMIT = __STALL_LIMIT__


def log(obj):
    print("EVENT " + json.dumps(obj, ensure_ascii=False), flush=True)


def _attach_to_zsxq_tab():
    """Find an existing Chrome tab on the target group and attach to it.

    Uses browser-harness `switch_tab` so the daemon's session actually
    follows. Returns True if a matching tab was found.
    """
    try:
        tabs = list_tabs(include_chrome=False)
    except Exception as exc:
        log({"warn": "list_tabs_failed", "error": str(exc)})
        return False
    matches = [t for t in tabs if GROUP_ID in (t.get("url") or "")]
    if not matches:
        return False
    switch_tab(matches[0]["targetId"])
    return True


# 1) Connect to the right tab. Never goto_url on a foreign tab.
if _attach_to_zsxq_tab():
    log({"step": "attached_existing_zsxq_tab"})
else:
    log({"step": "opening_new_tab", "url": TARGET_URL})
    new_tab(TARGET_URL)

try:
    wait_for_load(timeout=30)
except Exception as exc:
    log({"warn": "wait_for_load_failed", "error": str(exc)})
time.sleep(2.0)

info = page_info()
log({"step": "page_ready", "url": info.get("url"), "title": info.get("title")})

if GROUP_ID not in (info.get("url") or ""):
    log({"fatal": "not_on_target_group", "url": info.get("url")})
    print("FINAL_JSON_BEGIN", flush=True)
    print(json.dumps({"error": "not_on_target_group", "url": info.get("url"), "items": []}, ensure_ascii=False))
    print("FINAL_JSON_END", flush=True)
    sys.exit(0)

# 2) Scroll to top so we start from the newest post.
try:
    js("window.scrollTo(0, 0)")
    time.sleep(1.5)
except Exception as exc:
    log({"warn": "scroll_to_top_failed", "error": str(exc)})


# 3) Expand script. zsxq uses Angular custom triggers:
#   <p class="showAll">展开全部</p>           — folded body
#   <p class="showAllQuestion">展开全部</p>   — folded question on Q&A posts
# After Angular re-renders, the <p> is removed (or text changes), so it is
# safe to keep clicking every visible "展开全部" each round.
EXPAND_JS = r"""
(() => {
  let clicked = 0, candidates = 0;
  document.querySelectorAll('p.showAll, p.showAllQuestion').forEach(p => {
    candidates += 1;
    if ((p.innerText || '').trim() !== '展开全部') return;
    // skip if hidden (display:none means the row is collapsed/already expanded)
    const style = window.getComputedStyle(p);
    if (style.display === 'none' || style.visibility === 'hidden') return;
    try { p.click(); clicked += 1; } catch (e) {}
  });
  return {clicked, candidates};
})()
"""


# 4) Extraction script. Each card is `div.topic-container`.
EXTRACT_JS = r"""
(() => {
  const cards = Array.from(document.querySelectorAll('div.topic-container'));
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const TIME_RX = /(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})\D{0,3}(\d{1,2}:\d{2})?/;

  const items = cards.map(card => {
    const header = card.querySelector('app-topic-header');
    const headerText = norm(header ? header.innerText : '');

    let author = '';
    let timeText = '';
    let datetime = '';
    const m = headerText.match(TIME_RX);
    if (m) {
      const y = m[1];
      const mo = String(m[2]).padStart(2, '0');
      const d = String(m[3]).padStart(2, '0');
      const hm = m[4] || '';
      datetime = hm ? (y + '-' + mo + '-' + d + 'T' + hm) : (y + '-' + mo + '-' + d);
      timeText = m[0];
      author = norm(headerText.slice(0, m.index));
    } else {
      const parts = headerText.split(/\s+/);
      author = parts[0] || '';
      timeText = headerText;
    }

    const contentEl = card.querySelector('app-talk-content') || card;
    let content = norm(contentEl.innerText);
    // Strip residual expand button text if Angular hasn't re-rendered yet.
    content = content.replace(/\s*展开全部\s*/g, ' ').replace(/\s+/g, ' ').trim();

    const links = [];
    contentEl.querySelectorAll('a').forEach(a => {
      const href = a.getAttribute('href');
      if (!href) return;
      if (href.startsWith('javascript:')) return;
      if (href.startsWith('#')) return;
      links.push(href);
    });

    // Detect whether the card still has a visible "展开全部" trigger -> truncated.
    let truncated = false;
    card.querySelectorAll('p.showAll, p.showAllQuestion').forEach(p => {
      if ((p.innerText || '').trim() !== '展开全部') return;
      const style = window.getComputedStyle(p);
      if (style.display !== 'none' && style.visibility !== 'hidden') truncated = true;
    });

    const idKey = (datetime || timeText) + '|' + content.slice(0, 80);

    return {idKey, author, timeText, datetime, content, links, truncated};
  }).filter(it => it.content && it.content.length > 0);

  return {
    cards_count: cards.length,
    items: items,
    scroll_height: document.documentElement.scrollHeight,
    scroll_y: window.scrollY,
    inner_height: window.innerHeight,
    url: location.href,
  };
})()
"""


def parse_when(item):
    raw = (item.get('datetime') or '').strip()
    if raw:
        return raw
    txt = (item.get('timeText') or '').strip()
    m = re.search(r'(20\d{2})[-/年]\s*(\d{1,2})[-/月]\s*(\d{1,2})', txt)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return ''


def is_before(iso_like, since):
    if not iso_like:
        return False
    return iso_like[:10] < since


detected_owner = OWNER_NAME or ''
collected = {}
oldest_owner_iso = ''
stall = 0
prev_total = 0
prev_scroll_height = 0
total_truncated_seen = 0
total_expand_clicks = 0

for round_idx in range(MAX_ROUNDS):
    # Step A: click every visible "展开全部" trigger. Two passes catch buttons
    # that only appear after the first click renders nested questions.
    expand_info = {"clicked": 0, "candidates": 0}
    for _ in range(2):
        try:
            res = js(EXPAND_JS) or {}
        except Exception as exc:
            log({"round": round_idx, "warn": "expand_js_failed", "error": str(exc)})
            break
        expand_info["clicked"] += int(res.get("clicked") or 0)
        expand_info["candidates"] += int(res.get("candidates") or 0)
        if not res.get("clicked"):
            break
        time.sleep(0.45)
    total_expand_clicks += expand_info["clicked"]

    # Step B: extract.
    try:
        data = js(EXTRACT_JS)
    except Exception as exc:
        log({"round": round_idx, "warn": "extract_js_failed", "error": str(exc)})
        time.sleep(1.0)
        continue

    items = data.get('items') or []

    # Auto-detect owner from the first card that has a non-empty author.
    if not detected_owner:
        for it in items:
            if it.get('author'):
                detected_owner = it['author']
                log({"step": "detected_owner", "owner": detected_owner})
                break

    owner_items = [it for it in items if (not detected_owner) or it.get('author') == detected_owner]
    new_in_round = 0
    truncated_in_round = 0
    for it in owner_items:
        if it.get('truncated'):
            truncated_in_round += 1
        key = it.get('idKey') or (parse_when(it) + '|' + it.get('content', '')[:80])
        if key in collected:
            # Replace with longer content if we got a fuller version this round.
            old_len = len(collected[key].get('content') or '')
            new_len = len(it.get('content') or '')
            if new_len > old_len:
                collected[key] = it
            continue
        collected[key] = it
        new_in_round += 1
    total_truncated_seen += truncated_in_round

    round_oldest = ''
    for it in owner_items:
        iso = parse_when(it)
        if iso and (round_oldest == '' or iso < round_oldest):
            round_oldest = iso
    if round_oldest and (oldest_owner_iso == '' or round_oldest < oldest_owner_iso):
        oldest_owner_iso = round_oldest

    log({
        "round": round_idx,
        "cards": data.get('cards_count'),
        "expand_candidates": expand_info["candidates"],
        "expand_clicked": expand_info["clicked"],
        "truncated_in_round": truncated_in_round,
        "owner": detected_owner,
        "owner_in_round": len(owner_items),
        "new_in_round": new_in_round,
        "collected_total": len(collected),
        "round_oldest": round_oldest,
        "oldest_owner_iso": oldest_owner_iso,
        "scroll_y": data.get('scroll_y'),
        "scroll_height": data.get('scroll_height'),
    })

    if oldest_owner_iso and is_before(oldest_owner_iso, SINCE_DATE):
        log({"step": "stop_reason", "reason": "reached_since_date"})
        break

    # Scroll the window. Most zsxq feeds lazy-load on near-bottom scroll.
    try:
        js("window.scrollTo(0, document.documentElement.scrollHeight)")
    except Exception as exc:
        log({"warn": "scroll_bottom_failed", "error": str(exc)})
    try:
        js("window.dispatchEvent(new Event('scroll'))")
    except Exception:
        pass

    time.sleep(SCROLL_PAUSE)

    total = len(collected)
    sh = data.get('scroll_height') or 0
    grew = total > prev_total or sh > prev_scroll_height
    if grew:
        stall = 0
    else:
        stall += 1
    prev_total = total
    prev_scroll_height = sh
    if stall >= STALL_LIMIT:
        log({"step": "stop_reason", "reason": "no_growth", "stall_rounds": stall})
        break


# Optional final pass: scroll back through the page once to expand anything we
# missed (rare: cards far above viewport whose Angular trigger lazy-rendered).
log({"step": "final_expand_pass_begin", "remaining_truncated_seen": total_truncated_seen})
try:
    js("window.scrollTo(0, 0)")
    time.sleep(1.0)
except Exception:
    pass
final_clicks = 0
for _ in range(6):
    try:
        res = js(EXPAND_JS) or {}
    except Exception:
        break
    clicked = int(res.get("clicked") or 0)
    final_clicks += clicked
    if not clicked:
        break
    time.sleep(0.5)
    try:
        js("window.scrollBy(0, window.innerHeight * 0.9)")
    except Exception:
        pass
log({"step": "final_expand_pass_done", "final_clicks": final_clicks})

# Re-extract everything once more so any newly expanded content lands.
try:
    data = js(EXTRACT_JS)
    items = data.get('items') or []
    for it in items:
        if detected_owner and it.get('author') != detected_owner:
            continue
        key = it.get('idKey') or (parse_when(it) + '|' + it.get('content', '')[:80])
        old = collected.get(key)
        if not old:
            collected[key] = it
        else:
            if len(it.get('content') or '') > len(old.get('content') or ''):
                collected[key] = it
except Exception as exc:
    log({"warn": "final_extract_failed", "error": str(exc)})


# Filter posts older than SINCE_DATE and sort newest -> oldest.
final = []
truncated_remaining = 0
for v in collected.values():
    iso = parse_when(v)
    if iso and is_before(iso, SINCE_DATE):
        continue
    if v.get('truncated'):
        truncated_remaining += 1
    final.append({
        "id": v.get('idKey', ''),
        "author": v.get('author', ''),
        "time_text": v.get('timeText', ''),
        "datetime": v.get('datetime', ''),
        "time_iso": iso,
        "content": v.get('content', ''),
        "links": v.get('links', []),
        "truncated": bool(v.get('truncated')),
    })

final.sort(key=lambda v: v.get('time_iso') or v.get('time_text') or '', reverse=True)

print("FINAL_JSON_BEGIN", flush=True)
print(json.dumps({
    "owner": detected_owner,
    "since_date": SINCE_DATE,
    "url": info.get("url"),
    "count": len(final),
    "expand_clicks_total": total_expand_clicks + final_clicks,
    "truncated_remaining": truncated_remaining,
    "items": final,
}, ensure_ascii=False))
print("FINAL_JSON_END", flush=True)
'''


def _extract_group_id(url: str) -> str:
    match = re.search(r"/group/([A-Za-z0-9]+)", url)
    if match:
        return match.group(1)
    return url.rstrip("/").split("/")[-1]


def _resolve_csv_path(csv_path: str) -> Path:
    p = Path(csv_path).expanduser()
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _parse_final_json(stdout: str) -> dict[str, Any] | None:
    begin = stdout.find("FINAL_JSON_BEGIN")
    end = stdout.find("FINAL_JSON_END")
    if begin == -1 or end == -1 or end <= begin:
        return None
    body = stdout[begin + len("FINAL_JSON_BEGIN"):end].strip()
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        # Sometimes the script may emit additional lines; try the last JSON-looking block.
        last_brace = body.rfind("}")
        first_brace = body.find("{")
        if first_brace == -1 or last_brace == -1:
            return None
        try:
            return json.loads(body[first_brace:last_brace + 1])
        except json.JSONDecodeError:
            return None


def _collect_events(stdout: str, limit: int = 40) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        if not line.startswith("EVENT "):
            continue
        payload = line[len("EVENT "):].strip()
        try:
            events.append(json.loads(payload))
        except json.JSONDecodeError:
            continue
    if len(events) > limit:
        return events[:5] + [{"_truncated": len(events) - limit}] + events[-(limit - 5):]
    return events


def scrape_zsxq_owner_posts(
    group_url: str = "https://wx.zsxq.com/group/51111541884844",
    since_date: str = "2024-01-01",
    owner_name: str = "",
    max_scrolls: str = "300",
    scroll_pause_seconds: str = "1.4",
    stall_limit: str = "8",
    csv_path: str = "zsxq_owner_posts.csv",
    timeout_seconds: str = "1200",
) -> str:
    """Scrape group-owner (`楼主`) posts from a zsxq.com group via the user's real Chrome.

    Precondition: the user must already be logged into zsxq in their Chrome and
    have allowed remote debugging (chrome://inspect/#remote-debugging). The
    script reuses any existing Chrome tab open at the target group; otherwise
    it navigates the active tab to `group_url`.

    The scraper scrolls down repeatedly, clicks `展开/全文` style triggers to
    unfold collapsed posts, and collects posts authored by the group owner. It
    stops as soon as it has surfaced a post older than `since_date`, or when
    no new posts appear for `stall_limit` consecutive rounds.

    Args:
        group_url: Full zsxq group URL like
            `https://wx.zsxq.com/group/<group_id>`.
        since_date: ISO date string. Posts strictly older than this date are
            dropped. Defaults to `2024-01-01`.
        owner_name: Group-owner display name. Empty string auto-detects it as
            the first author seen on the page.
        max_scrolls: Maximum scroll iterations. String to satisfy AgentLoom
            tool schema; parsed as int.
        scroll_pause_seconds: Seconds to wait between scroll iterations.
        stall_limit: Stop after this many consecutive scrolls produced no new
            posts.
        csv_path: Output CSV path. Relative paths are resolved against the
            AgentLoom project root.
        timeout_seconds: Hard timeout for the underlying browser-harness call.

    Returns:
        JSON text describing the run: csv_path, owner, post counts, time
        range, browser-harness exit details, and the most recent events.
    """

    group_id = _extract_group_id(group_url)
    if not group_id:
        return _json({"success": False, "error": "could not parse group id from URL", "group_url": group_url})

    script = (
        _ZSXQ_SCRAPER_SCRIPT_TEMPLATE
        .replace("__TARGET_URL_JSON__", json.dumps(group_url))
        .replace("__GROUP_ID_JSON__", json.dumps(group_id))
        .replace("__SINCE_DATE_JSON__", json.dumps(since_date))
        .replace("__OWNER_NAME_JSON__", json.dumps(owner_name or ""))
        .replace("__MAX_ROUNDS__", str(int(max_scrolls)))
        .replace("__SCROLL_PAUSE__", str(float(scroll_pause_seconds)))
        .replace("__STALL_LIMIT__", str(int(stall_limit)))
    )

    env = os.environ.copy()
    env.pop("BU_CDP_URL", None)
    env.pop("BU_CDP_WS", None)
    env["BU_NAME"] = REAL_MODE_NAME

    harness_result = _run_harness(script, env=env, timeout_seconds=int(timeout_seconds))

    payload = _parse_final_json(harness_result.get("stdout", "") or "")
    events = _collect_events(harness_result.get("stdout", "") or "")

    result: dict[str, Any] = {
        "harness": {
            "success": harness_result.get("success"),
            "returncode": harness_result.get("returncode"),
            "elapsed_seconds": harness_result.get("elapsed_seconds"),
            "command": harness_result.get("command"),
            "stderr_tail": (harness_result.get("stderr") or "")[-2000:],
        },
        "events": events,
        "group_id": group_id,
        "group_url": group_url,
        "since_date": since_date,
    }

    if payload is None:
        result.update(
            {
                "success": False,
                "error": "could not parse FINAL_JSON from browser-harness output",
                "stdout_tail": (harness_result.get("stdout") or "")[-2000:],
            }
        )
        return _json(result)

    if "error" in payload and not payload.get("items"):
        result.update({"success": False, **payload})
        return _json(result)

    items = payload.get("items", []) or []
    csv_out = _resolve_csv_path(csv_path)
    with csv_out.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(["时间", "内容", "超链接"])
        for it in items:
            time_value = it.get("time_iso") or it.get("datetime") or it.get("time_text") or ""
            content_value = it.get("content") or ""
            links = it.get("links") or []
            seen_links: list[str] = []
            for link in links:
                if link and link not in seen_links:
                    seen_links.append(link)
            link_value = "\n".join(seen_links)
            writer.writerow([time_value, content_value, link_value])

    iso_values = [it.get("time_iso") for it in items if it.get("time_iso")]
    earliest = min(iso_values) if iso_values else ""
    latest = max(iso_values) if iso_values else ""

    truncated_remaining = int(payload.get("truncated_remaining") or 0)
    expand_clicks_total = int(payload.get("expand_clicks_total") or 0)

    result.update(
        {
            "success": True,
            "owner": payload.get("owner"),
            "post_count": len(items),
            "csv_path": str(csv_out),
            "time_range": {"earliest": earliest, "latest": latest},
            "page_url": payload.get("url"),
            "expand_clicks_total": expand_clicks_total,
            "truncated_remaining": truncated_remaining,
        }
    )
    return _json(result)
