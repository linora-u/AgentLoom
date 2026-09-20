import { spawn } from 'node:child_process';
import { appendFile, mkdir, mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Check } from 'typebox/value';
import {
  AuthStorage, ModelRegistry, SettingsManager, SessionManager, DefaultResourceLoader,
  createAgentSession, createWriteToolDefinition, createReadToolDefinition, createBashToolDefinition,
} from '@earendil-works/pi-coding-agent';
import { startProvider } from './provider.mjs';
export { toolCall, finalText } from './provider.mjs';

export async function hostRequest(request) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.env.PYTHON ?? 'python3', [fileURLToPath(new URL('./host.py', import.meta.url))], {
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    let stdout = '', stderr = '';
    const timer = setTimeout(() => child.kill('SIGKILL'), 5000);
    child.stdout.on('data', (data) => { stdout += data; });
    child.stderr.on('data', (data) => { stderr += data; });
    child.on('error', reject);
    child.on('close', (code) => {
      clearTimeout(timer);
      if (code !== 0) return reject(new Error(`Python Hook failed: ${stderr}`));
      try { resolve(JSON.parse(stdout)); } catch (error) { reject(error); }
    });
    child.stdin.end(JSON.stringify(request));
  });
}

export async function jsonLines(path) {
  try { return (await readFile(path, 'utf8')).trim().split('\n').filter(Boolean).map((line) => JSON.parse(line)); }
  catch (error) { if (error.code === 'ENOENT') return []; throw error; }
}

/**
 * Public SDK and official tool definitions only; no changes to SDK source or loop.
 * @param {{root?: string, responses: object[], hookMode?: string | Record<string, string>,
 * manager?: SessionManager, crashAfterCommit?: boolean, compaction?: boolean, disableTransform?: boolean,
 * strictGeneration?: boolean, tamperAfterPrepare?: boolean}} options
 */
export async function createHarness({ root, responses, hookMode = 'allow', manager: suppliedManager,
  crashAfterCommit = false, compaction = false, disableTransform = false, strictGeneration = false,
  tamperAfterPrepare = false }) {
  root ??= await mkdtemp(join(tmpdir(), 'agentloom-pi-poc-'));
  const cwd = join(root, 'workspace');
  await mkdir(cwd, { recursive: true });
  const provider = await startProvider(responses);
  let createdSession;
  try {
    const manager = suppliedManager ?? SessionManager.create(cwd, join(root, 'sessions'));
    const identity = (callId, toolName) => ({
      root, taskId: 'poc-task', runId: 'poc-run', sessionId: manager.getSessionId(), callId, toolName,
    });
    const auth = AuthStorage.inMemory();
    auth.setRuntimeApiKey('agentloom-poc', 'fixture-key-not-a-secret');
    const registry = ModelRegistry.inMemory(auth);
    registry.registerProvider('agentloom-poc', {
      api: 'openai-completions', baseUrl: provider.url, apiKey: 'fixture-key-not-a-secret',
      models: [{ id: 'poc-model', name: 'PoC model', reasoning: false, input: ['text'],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 1000, maxTokens: 100,
        compat: { supportsDeveloperRole: false, supportsUsageInStreaming: true, supportsStrictMode: true } }],
    });
    const approvals = new Map();
    const seenCalls = new Set();
    const nativeTools = strictGeneration
      ? [{ ...createWriteToolDefinition(cwd), parameters: { ...createWriteToolDefinition(cwd).parameters, additionalProperties: false } }]
      : [createWriteToolDefinition(cwd), createReadToolDefinition(cwd), createBashToolDefinition(cwd)];
    const extensionErrors = [];
    const settings = SettingsManager.inMemory({
      retry: { enabled: false }, compaction: { enabled: compaction, reserveTokens: 100, keepRecentTokens: 100 },
    });
    const loader = new DefaultResourceLoader({
      cwd, agentDir: join(root, 'agent'), settingsManager: settings,
      noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
      systemPrompt: 'You are the deterministic AgentLoom SDK compatibility probe.',
      extensionFactories: [(pi) => {
        if (strictGeneration) pi.on('before_provider_request', ({ payload }) => {
          // Only the write tool is enabled here: all fields are required, no nullable-option rewrite.
          const replacement = structuredClone(/** @type {{tools?: {function: {strict?: boolean}}[]}} */ (payload));
          for (const tool of replacement.tools ?? []) tool.function.strict = true;
          return replacement;
        });
        if (!disableTransform) pi.on('message_end', async ({ message }) => {
          if (message.role !== 'assistant') return;
          const replacement = structuredClone(message);
          const batchIds = new Set();
          for (const part of replacement.content) {
            if (part.type !== 'toolCall') continue;
            if (batchIds.has(part.id) || seenCalls.has(part.id)) {
              for (const call of replacement.content) if (call.type === 'toolCall') approvals.delete(call.id);
              throw new Error(`Duplicate call identity: ${part.id}`);
            }
            batchIds.add(part.id);
          }
          for (const id of batchIds) seenCalls.add(id);
          for (const part of replacement.content) {
            if (part.type !== 'toolCall') continue;
            if (approvals.has(part.id)) throw new Error(`Duplicate call identity: ${part.id}`);
            // Record pending before awaiting IPC. An extension exception cannot confer permission.
            approvals.set(part.id, { allowed: false, name: part.name });
            const decision = await hostRequest({ ...identity(part.id, part.name), action: 'prepare',
              mode: typeof hookMode === 'string' ? hookMode : hookMode[part.id] ?? 'allow', raw: part.arguments });
            if (decision.allowed) {
              const tool = nativeTools.find((tool) => tool.name === part.name);
              if (!tool || !Check(tool.parameters, decision.final)) {
                approvals.set(part.id, { allowed: false, name: part.name, reason: 'Final strict schema validation failed' });
                continue;
              }
              part.arguments = decision.final;
              approvals.set(part.id, { allowed: true, name: part.name, args: JSON.stringify(part.arguments) });
            }
          }
          return { message: replacement };
        });
        if (tamperAfterPrepare) pi.on('message_end', ({ message }) => {
          if (message.role !== 'assistant') return;
          const replacement = structuredClone(message);
          for (const part of replacement.content) if (part.type === 'toolCall') part.arguments.content = 'tampered';
          return { message: replacement };
        });
        pi.on('tool_call', ({ toolCallId, toolName, input }) => {
          const permit = approvals.get(toolCallId);
          if (!permit?.allowed || permit.name !== toolName || permit.args !== JSON.stringify(input)) {
            return { block: true, reason: permit?.reason ?? 'Missing or mismatched Python authorization' };
          }
        });
      }],
    });
    await loader.reload();
    const tools = nativeTools
      .map((tool) => ({
        ...tool,
        executionMode: /** @type {const} */ ('sequential'),
        execute: async (callId, args, signal, onUpdate, ctx) => {
          const permit = approvals.get(callId);
          if (!permit?.allowed || permit.name !== tool.name || permit.args !== JSON.stringify(args)) {
            throw new Error('Fail-closed executor gate');
          }
          approvals.delete(callId); // One-shot grant. No second execution on the same permit.
          await appendFile(join(root, 'host-trace.jsonl'), JSON.stringify({ ...identity(callId, tool.name), phase: 'execute' }) + '\n');
          const result = await tool.execute(callId, args, signal, onUpdate, ctx);
          await hostRequest({ ...identity(callId, tool.name), action: 'commit', args, result, nativeParent: manager.getLeafId() });
          if (crashAfterCommit) process.kill(process.pid, 'SIGKILL');
          return result;
        },
      }));
    const { session } = await createAgentSession({
      cwd, agentDir: join(root, 'agent'), authStorage: auth, modelRegistry: registry,
      model: registry.find('agentloom-poc', 'poc-model'), thinkingLevel: 'off',
      tools: tools.map((tool) => tool.name), customTools: tools,
      resourceLoader: loader, settingsManager: settings, sessionManager: manager,
    });
    createdSession = session;
    await session.bindExtensions({ onError: (error) => extensionErrors.push(error.error) });
    const events = [];
    session.subscribe((event) => events.push(event));
    return { root, cwd, manager, session, provider, requests: provider.requests, events, extensionErrors,
      async close() { session.dispose(); await provider.close(); await rm(root, { recursive: true, force: true }); } };
  } catch (error) {
    createdSession?.dispose();
    await provider.close();
    await rm(root, { recursive: true, force: true });
    throw error;
  }
}

export async function runSession(options) {
  const harness = await createHarness(options);
  try {
    await harness.session.prompt('Run the scripted compatibility scenario.');
    if (harness.provider.errors.length) throw new Error(harness.provider.errors.join('\n'));
    return { ...harness, trace: await jsonLines(join(harness.root, 'host-trace.jsonl')) };
  } catch (error) { await harness.close(); throw error; }
}
