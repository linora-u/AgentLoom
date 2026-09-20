import assert from 'node:assert/strict';
import { readFile, readdir } from 'node:fs/promises';
import { join } from 'node:path';
import test from 'node:test';
import { runSession, toolCall, finalText } from '../harness.mjs';

test('strict official write schema permits invalid raw input only after async Python repair', async () => {
  const result = await runSession({
    responses: [toolCall('repair-1', 'write', { path: 7, content: 42 }), finalText('done')],
    hookMode: 'repair',
  });
  try {
    const tool = result.requests[0].tools.find((tool) => tool.function.name === 'write');
    assert.equal(tool.function.parameters.properties.path.type, 'string');
    assert.equal(tool.function.parameters.properties.content.type, 'string');
    assert.deepEqual(tool.function.parameters.required, ['path', 'content']);
    assert.equal(await readFile(join(result.cwd, '7.txt'), 'utf8'), '42');
    assert.deepEqual(result.trace.map(({ phase, callId }) => [phase, callId]), [
      ['hook-start', 'repair-1'], ['hook-allowed', 'repair-1'],
      ['execute', 'repair-1'], ['committed', 'repair-1'],
    ]);
    assert.deepEqual(result.trace[0].raw, { path: 7, content: 42 });
    assert.deepEqual(result.trace[1].final, { path: '7.txt', content: '42' });
    const nativeCall = result.manager.buildSessionContext().messages.find((m) => m.role === 'assistant').content[0];
    assert.ok(nativeCall.type === 'toolCall');
    assert.deepEqual(nativeCall.arguments, { path: '7.txt', content: '42' });
    assert.equal(result.manager.buildSessionContext().messages.filter((m) => m.role === 'toolResult').length, 1);
  } finally { await result.close(); }
});

test('public request hook can opt the write schema into provider strict generation without relaxing types', async () => {
  const result = await runSession({
    responses: [toolCall('strict-repair', 'write', { path: 7, content: 42 }), finalText('done')],
    hookMode: 'repair', strictGeneration: true,
  });
  try {
    const definitions = result.requests[0].tools;
    assert.deepEqual(definitions.map((tool) => tool.function.name), ['write']);
    assert.equal(definitions[0].function.strict, true);
    assert.equal(definitions[0].function.parameters.additionalProperties, false);
    assert.equal(definitions[0].function.parameters.properties.path.type, 'string');
    assert.equal(definitions[0].function.parameters.properties.content.type, 'string');
    assert.deepEqual(definitions[0].function.parameters.required, ['path', 'content']);
    assert.equal(await readFile(join(result.cwd, '7.txt'), 'utf8'), '42');
  } finally { await result.close(); }
});

test('without repair the same invalid raw arguments fail final strict validation', async () => {
  const result = await runSession({
    responses: [toolCall('invalid-1', 'write', { path: 7, content: 42 }), finalText('done')],
  });
  try {
    assert.deepEqual(await readdir(result.cwd), []);
    const outcome = result.manager.buildSessionContext().messages.find((m) => m.role === 'toolResult');
    assert.equal(outcome.isError, true);
    assert.match(JSON.stringify(outcome.content), /validation|Expected string/i);
    assert.equal(result.trace.filter((r) => r.phase === 'execute').length, 0);
  } finally { await result.close(); }
});

for (const hookMode of ['deny', 'fail']) {
  test(`Python Hook ${hookMode} prevents a valid write and never retries the Hook`, async () => {
    const result = await runSession({
      responses: [toolCall('blocked-1', 'write', { path: 'must-not-exist', content: 'unsafe' }), finalText('done')],
      hookMode,
    });
    try {
      assert.deepEqual(await readdir(result.cwd), []);
      assert.equal(result.trace.filter((r) => r.phase === 'hook-start').length, 1);
      assert.equal(result.trace.filter((r) => r.phase === 'execute').length, 0);
      assert.equal(result.manager.buildSessionContext().messages.find((m) => m.role === 'toolResult').isError, true);
      if (hookMode === 'fail') assert.match(result.extensionErrors.join(' '), /Python Hook failed/);
    } finally { await result.close(); }
  });
}

test('missing message transformation cannot bypass the separate execution gate', async () => {
  const result = await runSession({
    responses: [toolCall('unguarded', 'write', { path: 'must-not-exist', content: 'unsafe' }), finalText('done')],
    disableTransform: true,
  });
  try {
    assert.deepEqual(await readdir(result.cwd), []);
    assert.deepEqual(result.trace, []);
    assert.match(JSON.stringify(result.manager.buildSessionContext().messages), /Missing or mismatched Python authorization/);
  } finally { await result.close(); }
});

test('arguments changed after Python authorization are rejected by the execution gate', async () => {
  const result = await runSession({
    responses: [toolCall('tampered', 'write', { path: 'safe', content: 'approved' }), finalText('done')],
    tamperAfterPrepare: true,
  });
  try {
    assert.deepEqual(await readdir(result.cwd), []);
    assert.equal(result.trace.filter((r) => r.phase === 'hook-start').length, 1);
    assert.equal(result.trace.filter((r) => r.phase === 'execute').length, 0);
  } finally { await result.close(); }
});

test('strict final schema rejects an unexpected property without executing', async () => {
  const result = await runSession({
    responses: [toolCall('extra', 'write', { path: 'unsafe', content: 'unsafe', bypass: true }), finalText('done')],
    strictGeneration: true,
  });
  try {
    assert.deepEqual(await readdir(result.cwd), []);
    assert.equal(result.trace.filter((r) => r.phase === 'execute').length, 0);
  } finally { await result.close(); }
});

test('multi-tool batch preserves identity and sequential Hook order before execution', async () => {
  const result = await runSession({
    responses: [{ calls: [
      { id: 'batch-1', name: 'write', args: { path: 7, content: 42 } },
      { id: 'batch-2', name: 'read', args: { path: '7.txt' } },
      { id: 'batch-3', name: 'write', args: { path: 'forbidden', content: 'unsafe' } },
    ] }, finalText('done')],
    hookMode: { 'batch-1': 'repair', 'batch-2': 'allow', 'batch-3': 'deny' },
  });
  try {
    assert.deepEqual(await readdir(result.cwd), ['7.txt']);
    assert.deepEqual(result.trace.map(({ phase, callId }) => [phase, callId]), [
      ['hook-start', 'batch-1'], ['hook-allowed', 'batch-1'],
      ['hook-start', 'batch-2'], ['hook-allowed', 'batch-2'],
      ['hook-start', 'batch-3'], ['hook-denied', 'batch-3'],
      ['execute', 'batch-1'], ['committed', 'batch-1'],
      ['execute', 'batch-2'], ['committed', 'batch-2'],
    ]);
    const outcomes = result.manager.buildSessionContext().messages.filter((m) => m.role === 'toolResult');
    assert.deepEqual(outcomes.map((m) => [m.toolCallId, m.toolName, m.isError]), [
      ['batch-1', 'write', false], ['batch-2', 'read', false], ['batch-3', 'write', true],
    ]);
    assert.match(JSON.stringify(outcomes[1].content), /42/);
    assert.ok(result.trace.every((r) => r.taskId === 'poc-task' && r.runId === 'poc-run' && r.sessionId === result.manager.getSessionId()));
  } finally { await result.close(); }
});

test('a repeated call identity cannot authorize another operation', async () => {
  const result = await runSession({
    responses: [
      toolCall('duplicate', 'write', { path: 'safe', content: 'safe' }),
      toolCall('duplicate', 'write', { path: 'unsafe', content: 'unsafe' }),
      finalText('done'),
    ],
  });
  try {
    assert.deepEqual(await readdir(result.cwd), ['safe']);
    assert.equal(result.trace.filter((r) => r.phase === 'execute').length, 1);
    assert.equal(result.trace.filter((r) => r.phase === 'hook-start').length, 1);
    assert.match(result.extensionErrors.join(' '), /Duplicate call identity/);
  } finally { await result.close(); }
});
