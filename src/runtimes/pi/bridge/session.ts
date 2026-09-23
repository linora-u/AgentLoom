import type { AgentSession } from "@earendil-works/pi-coding-agent";

const INSTRUCTION_ONLY_TURN = "agentloom_instruction_only_turn";
const OVERFLOW_RECOVERY_ATTEMPTED = "_overflowRecoveryAttempted";

function beginInstructionOnlyTurn(session: AgentSession): void {
  // Pi 0.79.4 normally opens a fresh overflow-recovery budget on user
  // message_start. Our model-invisible custom trigger is the equivalent turn
  // boundary, so mirror that one state transition in this pinned SDK adapter.
  if (!Reflect.has(session, OVERFLOW_RECOVERY_ATTEMPTED) ||
      !Reflect.set(session, OVERFLOW_RECOVERY_ATTEMPTED, false)) {
    throw new Error("Pi SDK does not support instruction-only session turns");
  }
}

/** Install the projection rule for AgentLoom's session-only turn trigger. */
export function enableInstructionOnlyTurns(session: AgentSession): void {
  const convertToLlm = session.agent.convertToLlm;
  session.agent.convertToLlm = messages => convertToLlm(messages.filter(message =>
    message.role !== "custom" || message.customType !== INSTRUCTION_ONLY_TURN));
}

/** Run a complete SDK turn without adding model-visible input. */
export async function runInstructionOnlyTurn(session: AgentSession): Promise<void> {
  // A session-only custom record gives overflow recovery a non-assistant turn
  // boundary. enableInstructionOnlyTurns keeps it out of every model request.
  beginInstructionOnlyTurn(session);
  await session.sendCustomMessage({
    customType: INSTRUCTION_ONLY_TURN,
    content: [],
    display: false,
  }, {triggerTurn: true});
}
