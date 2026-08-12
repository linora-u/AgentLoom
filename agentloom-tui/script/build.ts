#!/usr/bin/env bun

import { chmod, mkdir } from "node:fs/promises"
import { dirname, resolve } from "node:path"
import { createSolidTransformPlugin } from "@opentui/solid/bun-plugin"

const packageRoot = resolve(import.meta.dir, "..")
const repositoryRoot = resolve(packageRoot, "..")
const outfile = resolve(packageRoot, readOutfile(process.argv.slice(2)) ?? "dist/agentloom")
const target = `bun-${process.platform === "win32" ? "windows" : process.platform}-${process.arch}`
const version = await readProjectVersion(resolve(repositoryRoot, "pyproject.toml"))

await mkdir(dirname(outfile), { recursive: true })
process.chdir(packageRoot)

const result = await Bun.build({
  conditions: ["bun", "node"],
  tsconfig: "./tsconfig.json",
  plugins: [createSolidTransformPlugin()],
  format: "esm",
  minify: true,
  sourcemap: "none",
  splitting: true,
  compile: {
    autoloadBunfig: false,
    autoloadDotenv: false,
    autoloadTsconfig: true,
    autoloadPackageJson: true,
    target: target as Bun.Build.CompileTarget,
    outfile,
  },
  entrypoints: ["./src/cli/standalone.ts"],
  define: { "process.env.AGENTLOOM_VERSION": JSON.stringify(version) },
})

if (!result.success) {
  for (const log of result.logs) console.error(log)
  process.exit(1)
}

await chmod(outfile, 0o755)
const smoke = Bun.spawnSync({ cmd: [outfile, "--version"], stdout: "pipe", stderr: "pipe" })
if (smoke.exitCode !== 0) {
  process.stderr.write(new TextDecoder().decode(smoke.stderr))
  throw new Error(`standalone smoke test failed with exit code ${smoke.exitCode}`)
}
process.stdout.write(`Built ${outfile} (${new TextDecoder().decode(smoke.stdout).trim()})\n`)

function readOutfile(argv: readonly string[]) {
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index]!
    if (argument === "--outfile") {
      const value = argv[index + 1]
      if (!value || value.startsWith("--")) throw new Error("--outfile requires a path")
      return value
    }
    if (argument.startsWith("--outfile=")) {
      const value = argument.slice("--outfile=".length)
      if (!value) throw new Error("--outfile requires a path")
      return value
    }
    throw new Error(`Unknown build option: ${argument}`)
  }
  return undefined
}

async function readProjectVersion(path: string) {
  const source = await Bun.file(path).text()
  const project = source.match(/^\[project\]\s*$([\s\S]*?)(?=^\[|(?![\s\S]))/m)?.[1]
  const version = project?.match(/^version\s*=\s*"([^"]+)"\s*$/m)?.[1]
  if (!version) throw new Error(`Unable to read [project].version from ${path}`)
  return version
}
