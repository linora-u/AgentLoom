import { afterEach, describe, expect, test } from "bun:test"
import { mkdir, mkdtemp, realpath, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { basename, join, resolve } from "node:path"
import { BridgeClient } from "../../src/bridge/client"
import {
  PythonTransport,
  resolvePythonBridgeCommand,
  resolveProjectRoot,
} from "../../src/bridge/python-transport"

const temporaryDirectories: string[] = []
const repositoryRoot = resolve(import.meta.dir, "../../..")

afterEach(async () => {
  await Promise.all(temporaryDirectories.splice(0).map((directory) => rm(directory, { recursive: true, force: true })))
})

describe("PythonTransport", () => {
  test("uses the project argument for the bridge working directory", () => {
    expect(resolveProjectRoot(["--project", "./target"], "/workspace")).toBe("/workspace/target")
    expect(resolveProjectRoot(["--project=./other"], "/workspace")).toBe("/workspace/other")
    expect(resolveProjectRoot([], "/workspace")).toBe("/workspace")
  })

  test("prefers the installed Python, then a project venv, and only then uv", async () => {
    const projectRoot = await mkdtemp(join(tmpdir(), "agentloom-python-command-"))
    temporaryDirectories.push(projectRoot)

    expect(resolvePythonBridgeCommand(projectRoot, { AGENTLOOM_PYTHON: "/managed/python" })).toEqual([
      "/managed/python",
      "-I",
      "-u",
      "-m",
      "src.tui_bridge",
    ])

    const projectPython = join(projectRoot, ".venv", "bin", "python")
    await mkdir(join(projectRoot, ".venv", "bin"), { recursive: true })
    await writeFile(projectPython, "")
    expect(resolvePythonBridgeCommand(projectRoot, {})).toEqual([
      projectPython,
      "-I",
      "-u",
      "-m",
      "src.tui_bridge",
    ])

    await rm(join(projectRoot, ".venv"), { recursive: true })
    expect(resolvePythonBridgeCommand(projectRoot, {})).toEqual([
      "uv",
      "run",
      "python",
      "-I",
      "-u",
      "-m",
      "src.tui_bridge",
    ])
  })

  test("loads the installed bridge instead of a project-local src.tui_bridge", async () => {
    const projectRoot = await mkdtemp(join(tmpdir(), "agentloom-isolated-bridge-"))
    temporaryDirectories.push(projectRoot)
    const fakeBridge = join(projectRoot, "src", "tui_bridge")
    await mkdir(fakeBridge, { recursive: true })
    await writeFile(join(projectRoot, "src", "__init__.py"), "")
    await writeFile(join(fakeBridge, "__init__.py"), "")
    await writeFile(
      join(fakeBridge, "__main__.py"),
      [
        "import json, sys",
        "for raw in sys.stdin:",
        "    request = json.loads(raw)",
        "    result = {'project': {'root': 'PROJECT-LOCAL-FAKE', 'name': 'PROJECT-LOCAL-FAKE'}, 'models': {'default': '', 'configured': False, 'items': []}, 'systems': [], 'runs': []}",
        "    print(json.dumps({'id': request['id'], 'ok': True, 'result': result}), flush=True)",
      ].join("\n"),
    )
    const installedPython =
      process.platform === "win32"
        ? join(repositoryRoot, ".venv", "Scripts", "python.exe")
        : join(repositoryRoot, ".venv", "bin", "python")
    const transport = new PythonTransport({
      projectRoot,
      command: resolvePythonBridgeCommand(projectRoot, { AGENTLOOM_PYTHON: installedPython }),
    })
    const client = new BridgeClient(transport)

    try {
      const snapshot = await client.bootstrap()

      expect(snapshot.project).toEqual({
        root: await realpath(projectRoot),
        name: basename(projectRoot),
      })
    } finally {
      await client.close()
    }
  })

  test("exchanges one JSON object per line while keeping stderr separate", async () => {
    const projectRoot = await mkdtemp(join(tmpdir(), "agentloom-transport-"))
    temporaryDirectories.push(projectRoot)
    const stderr: string[] = []
    const line = Promise.withResolvers<string>()
    const failure = Promise.withResolvers<Error>()
    const transport = new PythonTransport({
      projectRoot,
      command: [
        "python3",
        "-u",
        "-c",
        [
          "import json, os, sys",
          "for raw in sys.stdin:",
          "    print('bridge diagnostic', file=sys.stderr, flush=True)",
          "    print(json.dumps({'request': json.loads(raw), 'cwd': os.getcwd()}), flush=True)",
        ].join("\n"),
      ],
      onStderr: (chunk) => stderr.push(chunk),
    })

    await transport.start({
      line: line.resolve,
      error: failure.resolve,
      close: () => {},
    })
    await transport.send('{"id":"request-1","method":"bootstrap","params":{}}')

    const response = JSON.parse(await Promise.race([line.promise, failure.promise.then((error) => Promise.reject(error))]))
    expect(response).toEqual({
      request: { id: "request-1", method: "bootstrap", params: {} },
      cwd: await realpath(projectRoot),
    })
    await waitFor(() => stderr.join("").includes("bridge diagnostic"))
    expect(stderr.join("")).toContain("bridge diagnostic")
    await transport.close()
  })

  test("rejects multiline or non-object requests before writing to Python", async () => {
    const transport = new PythonTransport({ command: ["python3", "-u", "-c", "import sys; sys.stdin.read()"] })
    await transport.start({ line: () => {}, error: () => {}, close: () => {} })

    await expect(transport.send('{"id":"one"}\n{"id":"two"}')).rejects.toThrow("one JSON object")
    await expect(transport.send("[]")).rejects.toThrow("JSON object")
    await transport.close()
  })

  test("fails and terminates a bridge that emits an unbounded unterminated line", async () => {
    const failure = Promise.withResolvers<Error>()
    const transport = new PythonTransport({
      command: [
        "python3",
        "-u",
        "-c",
        "import sys; sys.stdout.write('x' * 512); sys.stdout.flush(); sys.stdin.read()",
      ],
      maxLineBytes: 128,
    })
    await transport.start({ line: () => {}, error: failure.resolve, close: () => {} })

    const error = await failure.promise

    expect(error).toBeInstanceOf(Error)
    expect(error.message).toContain("maximum line length")
    await transport.close()
  })

  test("measures outgoing request limits in UTF-8 bytes", async () => {
    const transport = new PythonTransport({
      command: ["python3", "-u", "-c", "import sys; sys.stdin.read()"],
      maxLineBytes: 32,
    })
    await transport.start({ line: () => {}, error: () => {}, close: () => {} })

    await expect(transport.send(JSON.stringify({ value: "界".repeat(8) }))).rejects.toThrow("maximum line length")
    await transport.close()
  })

  test("measures incoming response limits in UTF-8 bytes", async () => {
    const failure = Promise.withResolvers<Error>()
    const transport = new PythonTransport({
      command: [
        "python3",
        "-u",
        "-c",
        "print('{\"value\":\"' + '界' * 40 + '\"}', flush=True); input()",
      ],
      maxLineBytes: 96,
    })
    await transport.start({ line: () => {}, error: failure.resolve, close: () => {} })

    const error = await failure.promise

    expect(error.message).toContain("maximum line length")
    await transport.close()
  })
})

async function waitFor(predicate: () => boolean) {
  while (!predicate()) await Bun.sleep(1)
}
