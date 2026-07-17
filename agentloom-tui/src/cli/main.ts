import { stat } from "node:fs/promises"
import { BridgeClient, PythonTransport } from "../bridge"
import { runTui } from "../app"
import { formatSnapshot, parseCliArgs } from "./args"

export const VERSION = "0.1.0"
const MAX_BRIDGE_DIAGNOSTIC_CHARS = 4_000

export const HELP = `AgentLoom TUI — create Agent YAML and inspect project execution state

Usage:
  agentloom [--project <path>]
  agentloom --snapshot [--project <path>]

Options:
  --project <path>  AgentLoom project root (default: current directory)
  --snapshot        Print a sanitized status summary and exit
  -h, --help        Show this help
  -v, --version     Show version

Inside the TUI:
  Enter              Send a Builder message
  /models             List configured models from config/llm.yaml
  /model <type>       Select a configured model from config/llm.yaml
  /refresh            Refresh Agent Systems and Runs
  /apply              Explicitly write the validated YAML draft
  Tab / ↑ / ↓ / Enter Browse and open Agent/Run details
  Ctrl-C              Exit
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
    const snapshot = await client.bootstrap()

    if (args.snapshot) {
      process.stdout.write(formatSnapshot(snapshot) + "\n")
      return 0
    }

    await runTui({ client, snapshot, projectRoot: args.projectRoot })
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
