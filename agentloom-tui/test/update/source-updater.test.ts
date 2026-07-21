import { afterEach, describe, expect, test } from "bun:test"
import { chmod, mkdir, mkdtemp, rm, utimes, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { createSourceUpdateClient } from "../../src/update/source-updater"

const temporaryDirectories: string[] = []

afterEach(async () => {
  await Promise.all(temporaryDirectories.splice(0).map((path) => rm(path, { recursive: true, force: true })))
})

describe("trusted source updates", () => {
  test("detects source changes newer than the installed compatible unit", async () => {
    const root = await mkdtemp(join(tmpdir(), "agentloom-update-"))
    temporaryDirectories.push(root)
    const sourceRoot = join(root, "source")
    const installRoot = join(root, "installed")
    await mkdir(join(sourceRoot, "agentloom-tui/src"), { recursive: true })
    await mkdir(installRoot, { recursive: true })
    await writeFile(join(installRoot, "installed-at"), "1000\n")
    const source = join(sourceRoot, "agentloom-tui/src/main.ts")
    await writeFile(source, "export {}\n")
    await utimes(source, 2_000, 2_000)

    const updater = createSourceUpdateClient({
      AGENTLOOM_SOURCE_ROOT: sourceRoot,
      AGENTLOOM_INSTALL_ROOT: installRoot,
      AGENTLOOM_COMMAND: join(installRoot, "bin/agentloom"),
    })

    expect(await updater?.check()).toMatchObject({
      available: true,
      sourceRoot,
    })
  })

  test("runs the installed update command without modifying an unknown checkout", async () => {
    const root = await mkdtemp(join(tmpdir(), "agentloom-update-command-"))
    temporaryDirectories.push(root)
    const sourceRoot = join(root, "source")
    const installRoot = join(root, "installed")
    const command = join(installRoot, "bin/agentloom")
    const marker = join(root, "updated")
    await mkdir(join(sourceRoot, "agentloom-tui/src"), { recursive: true })
    await mkdir(join(installRoot, "bin"), { recursive: true })
    await writeFile(join(installRoot, "installed-at"), "1000\n")
    await writeFile(command, `#!/bin/sh\n[ "$1" = update ]\nprintf '%s' updated > '${marker}'\n`)
    await chmod(command, 0o755)
    const updater = createSourceUpdateClient({
      AGENTLOOM_SOURCE_ROOT: sourceRoot,
      AGENTLOOM_INSTALL_ROOT: installRoot,
      AGENTLOOM_COMMAND: command,
    })

    await updater?.install()

    expect(await Bun.file(marker).text()).toBe("updated")
  })

  test("disables product updates when the trusted install metadata is absent", () => {
    expect(createSourceUpdateClient({})).toBeUndefined()
  })
})
