import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtemp, readFile, readdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { SessionManager } from '@earendil-works/pi-coding-agent';
import { createHarness, finalText, jsonLines } from '../harness.mjs';
import { restoreCommittedResult } from '../recovery.mjs';

async function crash(root) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [fileURLToPath(new URL('../crash-child.mjs', import.meta.url)), root], {
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stderr = '';
    child.stderr.on('data', (chunk) => { stderr += chunk; });
    const timer = setTimeout(() => { child.kill('SIGKILL'); reject(new Error('Crash probe timed out')); }, 15000);
    child.on('error', reject);
    child.on('exit', (code, signal) => { clearTimeout(timer); resolve({ code, signal, stderr }); });
  });
}

test('real SIGKILL after host commit recovers native result without rerunning official bash', async () => {
  const root = await mkdtemp(join(tmpdir(), 'agentloom-pi-crash-'));
  let harness;
  try {
    assert.deepEqual(await crash(root), { code: null, signal: 'SIGKILL', stderr: '' });
    assert.equal(await readFile(join(root, 'workspace/effects.log'), 'utf8'), 'effect\n');
    const journal = await jsonLines(join(root, 'journal.jsonl'));
    assert.equal(journal.length, 1);
    assert.equal(journal[0].status, 'committed');
    const [name] = await readdir(join(root, 'sessions'));
    const sessionPath = join(root, 'sessions', name);
    const before = SessionManager.open(sessionPath);
    assert.equal(before.buildSessionContext().messages.filter((m) => m.role === 'toolResult').length, 0);
    const leaf = before.getLeafEntry();
    assert.ok(leaf.type === 'message');
    assert.equal(leaf.message.role, 'assistant');
    const restored = await restoreCommittedResult(sessionPath, journal);
    assert.equal(restored.appended, 1);
    // Repeating recovery (another host restart) must not append duplicate native outcomes.
    assert.equal((await restoreCommittedResult(sessionPath, journal)).appended, 0);
    harness = await createHarness({ root, manager: SessionManager.open(sessionPath), responses: [finalText('resumed')] });
    await harness.session.prompt('Continue the interrupted task from its committed tool result.');
    assert.ok(harness.requests[0].messages.some((m) => m.role === 'tool' && m.tool_call_id === 'effect-once'));
    assert.equal(await readFile(join(root, 'workspace/effects.log'), 'utf8'), 'effect\n');
    const trace = await jsonLines(join(root, 'host-trace.jsonl'));
    assert.equal(trace.filter((r) => r.phase === 'execute').length, 1);
    assert.equal(trace.filter((r) => r.phase === 'hook-start').length, 1);
    assert.equal(harness.manager.buildSessionContext().messages.filter((m) => m.role === 'toolResult').length, 1);
    assert.deepEqual(harness.provider.errors, []);
  } finally {
    if (harness) await harness.close();
    else await rm(root, { recursive: true, force: true });
  }
});

test('uncertain or mismatched host records refuse recovery without changing native state', async () => {
  const root = await mkdtemp(join(tmpdir(), 'agentloom-pi-uncertain-'));
  try {
    assert.equal((await crash(root)).signal, 'SIGKILL');
    const journal = await jsonLines(join(root, 'journal.jsonl'));
    const [name] = await readdir(join(root, 'sessions'));
    const path = join(root, 'sessions', name);
    const original = await readFile(path, 'utf8');
    for (const override of [
      { status: 'uncertain' }, { sessionId: 'different-session' }, { taskId: 'different-task' },
      { args: { command: 'different command' } }, { nativeParent: 'missing-entry' },
    ]) {
      await assert.rejects(restoreCommittedResult(path, [{ ...journal[0], ...override }]), /Unsupported recovery/);
      assert.equal(await readFile(path, 'utf8'), original);
    }
    await assert.rejects(restoreCommittedResult(path, []), /Unsupported recovery/);
    assert.equal(await readFile(join(root, 'workspace/effects.log'), 'utf8'), 'effect\n');
  } finally { await rm(root, { recursive: true, force: true }); }
});
