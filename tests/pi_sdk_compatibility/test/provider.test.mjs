import assert from 'node:assert/strict';
import test from 'node:test';
import { startProvider } from '../provider.mjs';

test('provider keeps exception details out of HTTP responses', async () => {
  const provider = await startProvider([]);
  try {
    const response = await fetch(`${provider.url}/chat/completions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: 'fixture', stream: true }),
    });

    assert.equal(response.status, 500);
    assert.equal(await response.text(), 'Provider request failed');
    assert.deepEqual(provider.errors, ['Error: Unscripted model request']);
  } finally {
    await provider.close();
  }
});
