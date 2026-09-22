import assert from 'node:assert/strict';
import test from 'node:test';
import { createHarness, finalText } from '../harness.mjs';

test('AgentSession automatically compacts through the native provider and reuses its summary', async () => {
  const harness = await createHarness({
    responses: [finalText('Prior work. '.repeat(600), 950), finalText('NATIVE_COMPACTION_SUMMARY'), finalText('continued')],
    compaction: true,
  });
  try {
    await harness.session.prompt('Perform the first long turn.');
    assert.ok(harness.events.some((event) => event.type === 'compaction_start' && event.reason === 'threshold'));
    const end = harness.events.find((event) => event.type === 'compaction_end');
    assert.equal(end.aborted, false);
    assert.equal(end.errorMessage, undefined);
    const compacted = harness.manager.getEntries().find((entry) => entry.type === 'compaction');
    assert.match(compacted.summary, /NATIVE_COMPACTION_SUMMARY/);
    assert.match(JSON.stringify(harness.requests[1].messages), /summar/i);
    await harness.session.prompt('Continue using the compacted session.');
    assert.match(JSON.stringify(harness.requests[2].messages), /NATIVE_COMPACTION_SUMMARY/);
    assert.deepEqual(harness.provider.errors, []);
  } finally { await harness.close(); }
});
