import { afterEach, describe, expect, test } from "bun:test"
import { chmod, mkdir, mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { dirname, join, resolve } from "node:path"

const repositoryRoot = resolve(import.meta.dir, "../..")
const temporaryDirectories: string[] = []

afterEach(async () => {
  await Promise.all(temporaryDirectories.splice(0).map((directory) => rm(directory, { recursive: true, force: true })))
})

describe("source installer", () => {
  test("installs a locked Python environment and standalone wrapper without activation", async () => {
    const fixture = await createFixture()
    const installRoot = join(fixture.root, "agentloom home")

    const result = Bun.spawnSync({
      cmd: [join(repositoryRoot, "install"), "--no-modify-path"],
      cwd: repositoryRoot,
      env: {
        ...process.env,
        HOME: fixture.home,
        PATH: `${fixture.tools}:${process.env.PATH ?? ""}`,
        AGENTLOOM_INSTALL_DIR: installRoot,
        AGENTLOOM_TEST_LOG: fixture.log,
        SHELL: "/bin/zsh",
      },
      stdout: "pipe",
      stderr: "pipe",
    })

    expect(result.exitCode, new TextDecoder().decode(result.stderr)).toBe(0)
    const installedRoot = await realpath(installRoot)
    const calls = await readFile(fixture.log, "utf8")
    expect(calls).toContain(`uv|sync --frozen --no-editable --no-dev --project ${repositoryRoot}`)
    expect(calls).toContain("bun|install --frozen-lockfile")
    expect(calls).toContain("bun|run build -- --outfile")

    const wrapper = join(installedRoot, "bin", "agentloom")
    const runtime = join(installedRoot, "bin", "agentloom-tui")
    expect((await readFile(wrapper, "utf8"))).toContain("AGENTLOOM_PYTHON")
    expect(await Bun.file(runtime).exists()).toBe(true)

    const invocation = Bun.spawnSync({ cmd: [wrapper, "--version"], env: { ...process.env, AGENTLOOM_PYTHON: undefined } })
    expect(invocation.exitCode).toBe(0)
    expect(new TextDecoder().decode(invocation.stdout).trim()).toBe(`python=${join(installedRoot, "venv", "bin", "python")}`)
    expect(await Bun.file(join(fixture.home, ".zshrc")).exists()).toBe(false)
  })

  test("adds the command directory to an existing shell config once", async () => {
    const fixture = await createFixture()
    const installRoot = join(fixture.root, "installed")
    const shellConfig = join(fixture.home, ".zshrc")
    await writeFile(shellConfig, "# existing\n")
    const env = {
      ...process.env,
      HOME: fixture.home,
      PATH: `${fixture.tools}:${process.env.PATH ?? ""}`,
      AGENTLOOM_INSTALL_DIR: installRoot,
      AGENTLOOM_TEST_LOG: fixture.log,
      SHELL: "/bin/zsh",
    }

    for (let run = 0; run < 2; run += 1) {
      const result = Bun.spawnSync({ cmd: [join(repositoryRoot, "install")], cwd: repositoryRoot, env, stderr: "pipe" })
      expect(result.exitCode, new TextDecoder().decode(result.stderr)).toBe(0)
    }

    const config = await readFile(shellConfig, "utf8")
    const installedRoot = await realpath(installRoot)
    expect(config.match(/# AgentLoom/g)).toHaveLength(1)
    const pathCommand = `export PATH="${join(installedRoot, "bin")}:$PATH"`
    expect(config.split("\n").filter((line) => line === pathCommand)).toHaveLength(1)
  })
})

async function createFixture() {
  const root = await mkdtemp(join(tmpdir(), "agentloom-install-test-"))
  temporaryDirectories.push(root)
  const home = join(root, "home")
  const tools = join(root, "tools")
  const log = join(root, "calls.log")
  await mkdir(home, { recursive: true })
  await mkdir(tools, { recursive: true })
  await writeExecutable(
    join(tools, "uv"),
    `#!/bin/sh
set -eu
printf 'uv|%s\\n' "$*" >> "$AGENTLOOM_TEST_LOG"
mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"
printf '#!/bin/sh\\n' > "$UV_PROJECT_ENVIRONMENT/bin/python"
chmod +x "$UV_PROJECT_ENVIRONMENT/bin/python"
`,
  )
  await writeExecutable(
    join(tools, "bun"),
    `#!/bin/sh
set -eu
printf 'bun|%s\\n' "$*" >> "$AGENTLOOM_TEST_LOG"
if [ "\${1:-}" = "run" ] && [ "\${2:-}" = "build" ]; then
  shift 2
  [ "\${1:-}" = "--" ] && shift
  [ "\${1:-}" = "--outfile" ]
  outfile="$2"
  mkdir -p "$(dirname "$outfile")"
  printf '%s\\n' '#!/bin/sh' 'printf "python=%s\\n" "$AGENTLOOM_PYTHON"' > "$outfile"
  chmod +x "$outfile"
fi
`,
  )
  return { root, home, tools, log }
}

async function writeExecutable(path: string, content: string) {
  await mkdir(dirname(path), { recursive: true })
  await writeFile(path, content)
  await chmod(path, 0o755)
}
