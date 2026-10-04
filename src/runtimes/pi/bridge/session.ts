import type { AgentSession } from "@earendil-works/pi-coding-agent";

const INSTRUCTION_ONLY_TURN = "agentloom_instruction_only_turn";

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
  await session.sendCustomMessage({
    customType: INSTRUCTION_ONLY_TURN,
    content: [],
    display: false,
  }, {triggerTurn: true});
}
