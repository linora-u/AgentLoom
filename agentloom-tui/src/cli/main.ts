import { stat } from "node:fs/promises"
import { BridgeClient, PythonTransport } from "../bridge"
import { formatSnapshot, parseCliArgs } from "./args"

export const VERSION = "0.1.0"
const MAX_BRIDGE_DIAGNOSTIC_CHARS = 4_000

export const HELP = `AgentLoom Application Studio — create, inspect, modify, validate and run Applications

Usage:
  agentloom [--project <path>]
  agentloom --snapshot [--project <path>]
  agentloom update
  agentloom schedules --project <path> <command>

Options:
  --project <path>  AgentLoom project root (default: current directory)
  --snapshot        Print a sanitized status summary and exit
  -h, --help        Show this help
  -v, --version     Show version

Inside the TUI:
  Enter              Send a message
  /help               Show context-aware Studio help
  /new                Start a fresh Studio Session without prior conversation memory
  /models             Select a Studio model from config/llm.yaml
  /refresh            Re-index the project catalog
  /schedule           Show durable schedule commands
  Ctrl-X              Commands, permissions, Applications, main Agents, Runs and Skills
  ↑ / ↓ / Enter       Select and open a workbench entry
  PgUp / PgDn         Scroll the open detail view (mouse wheel also works)
  Ctrl-Y              Copy selected TUI text through OSC52
  Esc                 Close details, reject a permission/question, or interrupt the active Agent Loop
  Ctrl-C              Exit

Permissions:
  Application Only is the default. Ctrl-X toggles Full Access; it can be preset before selection
  and remains active while switching Applications until toggled off or the TUI exits.

Updates:
  TUI checks the recorded trusted checkout in the background. Confirm update/restart in Ctrl-X.
  From a source checkout, rerun ./install. After installation, run agentloom update.

Run durable schedules (separate terminal):
  agentloom schedules --project <path> serve
`

export async function main(argv = process.argv.slice(2)): Promise<number> {
  let client: BridgeClient | undefined
  const diagnostics: string[] = []
  try {
    const args = parseCliArgs(argv)
    if (args.help) {
      process.stdout.write(HELP)
      return 0
    }
    if (args.version) {
      process.stdout.write(`${VERSION}\n`)
      return 0
    }

    const project = await stat(args.projectRoot).catch(() => undefined)
    if (!project?.isDirectory()) throw new Error(`Project directory does not exist: ${args.projectRoot}`)

    const transport = new PythonTransport({
      projectRoot: args.projectRoot,
      onStderr: (chunk) => {
        diagnostics.push(chunk)
        if (diagnostics.length > 20) diagnostics.shift()
      },
    })
    client = new BridgeClient(transport)
    if (args.snapshot) {
      const snapshot = await client.bootstrap()
      process.stdout.write(formatSnapshot(snapshot) + "\n")
      return 0
    }

    // Non-interactive --help/--version/--snapshot must not pay OpenTUI's
    // renderer preload cost. JSX runtime hooks are installed immediately
    // before importing the interactive app.
    await import("@opentui/solid/preload")
    const { runInteractiveStudio } = await import("./interactive")
    await runInteractiveStudio({ bridge: client, projectRoot: args.projectRoot })
    return 0
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    process.stderr.write(formatBridgeFailure(message, diagnostics))
    return 1
  } finally {
    await client?.close().catch(() => undefined)
  }
}

export function formatBridgeFailure(message: string, chunks: readonly string[]): string {
  let diagnostic = chunks
    .join("")
    .replace(/\x1B\[[0-?]*[ -/]*[@-~]/g, "")
    .replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001A\u001C-\u001F\u007F]/g, "")
    .trim()
  if (!diagnostic) return `agentloom: ${message}\n`
  if (diagnostic.length > MAX_BRIDGE_DIAGNOSTIC_CHARS) {
    diagnostic = `… earlier bridge output omitted …\n${diagnostic.slice(-MAX_BRIDGE_DIAGNOSTIC_CHARS)}`
  }
  return `agentloom: ${message}\nPython bridge diagnostics:\n${diagnostic}\n`
}
