import { createHarness, toolCall } from './harness.mjs';

const harness = await createHarness({
  root: process.argv[2], crashAfterCommit: true,
  responses: [toolCall('effect-once', 'bash', { command: "printf 'effect\\n' >> effects.log; cat effects.log" })],
});
try {
  await harness.session.prompt('Run the single append operation.');
  throw new Error('Expected crash point was not reached');
} finally { await harness.close(); }
