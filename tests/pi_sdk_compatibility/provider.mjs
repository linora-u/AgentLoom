import { createServer } from 'node:http';

/** A transport fixture, never an Agent loop. Pi makes genuine OpenAI SSE requests. */
export async function startProvider(responses) {
  const requests = [];
  const errors = [];
  const server = createServer(async (req, res) => {
    try {
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      const body = JSON.parse(Buffer.concat(chunks).toString());
      requests.push(body);
      if (req.url !== '/v1/chat/completions' || !body.stream) throw new Error('Unexpected native provider request');
      const response = responses[requests.length - 1];
      if (!response) throw new Error('Unscripted model request');
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      const envelope = (delta, finish = null, usage = undefined) => ({
        id: `response-${requests.length}`, object: 'chat.completion.chunk', created: 1,
        model: body.model, choices: [{ index: 0, delta, finish_reason: finish }], usage,
      });
      const send = (value) => res.write(`data: ${JSON.stringify(value)}\n\n`);
      send(envelope({ role: 'assistant' }));
      if (response.calls) send(envelope({ tool_calls: response.calls.map((call, index) => ({
        index, id: call.id, type: 'function',
        function: { name: call.name, arguments: JSON.stringify(call.args) },
      })) }));
      else send(envelope({ content: response.text }));
      send(envelope({}, response.calls ? 'tool_calls' : 'stop', {
        prompt_tokens: response.tokens ?? 10, completion_tokens: 5,
        total_tokens: (response.tokens ?? 10) + 5,
      }));
      res.end('data: [DONE]\n\n');
    } catch (error) {
      errors.push(String(error));
      if (!res.headersSent) res.writeHead(500);
      res.end(String(error));
    }
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(undefined)));
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('Expected TCP listener');
  return {
    url: `http://127.0.0.1:${address.port}/v1`, requests, errors,
    close: () => new Promise((resolve, reject) => {
      server.closeAllConnections();
      server.close((error) => error ? reject(error) : resolve());
    }),
  };
}

export const toolCall = (id, name, args) => ({ calls: [{ id, name, args }] });
export const finalText = (text, tokens = 10) => ({ text, tokens });
