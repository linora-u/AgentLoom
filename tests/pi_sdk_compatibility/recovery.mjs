import { isDeepStrictEqual } from 'node:util';
import { SessionManager } from '@earendil-works/pi-coding-agent';

/**
 * A deliberately bounded recovery proof, not the future production journal.
 * Supports a one-call native turn, including idempotent re-entry after append.
 * All checks precede mutation. Never infer success from a missing host record.
 */
export async function restoreCommittedResult(sessionPath, journal) {
  const manager = SessionManager.open(sessionPath);
  const branch = manager.getBranch();
  const assistant = branch.findLast((entry) => entry.type === 'message' && entry.message.role === 'assistant');
  const unsupported = () => { throw new Error('Unsupported recovery: native state and committed host result do not align'); };
  if (!assistant || assistant.type !== 'message' || assistant.message.role !== 'assistant') return unsupported();
  const calls = assistant.message.content.filter((part) => part.type === 'toolCall');
  if (calls.length !== 1 || journal.length !== 1) return unsupported();
  const call = calls[0];
  const record = journal[0];
  if (record.status !== 'committed' || record.sessionId !== manager.getSessionId() || record.taskId !== 'poc-task' ||
    record.runId !== 'poc-run' || record.callId !== call.id || record.toolName !== call.name ||
    record.nativeParent !== assistant.id || !isDeepStrictEqual(record.args, call.arguments) ||
    !Array.isArray(record.result?.content)) return unsupported();
  const tail = branch.slice(branch.indexOf(assistant) + 1);
  if (tail.length) {
    if (tail.length !== 1 || tail[0].type !== 'message' || tail[0].message.role !== 'toolResult') return unsupported();
    const existing = tail[0].message;
    if (existing.toolCallId !== call.id || existing.toolName !== call.name || existing.isError !== false ||
      !isDeepStrictEqual(existing.content, record.result.content) ||
      !isDeepStrictEqual(existing.details, record.result.details)) return unsupported();
    return { appended: 0 };
  }
  manager.appendMessage({
    role: 'toolResult', toolCallId: call.id, toolName: call.name,
    content: record.result.content, details: record.result.details, isError: false, timestamp: Date.now(),
  });
  return { appended: 1 };
}
