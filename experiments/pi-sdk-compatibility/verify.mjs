import { createHash } from 'node:crypto';
import { execFileSync, spawnSync } from 'node:child_process';
import { mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { VERSION } from '@earendil-works/pi-coding-agent';

const cwd = fileURLToPath(new URL('.', import.meta.url));
const json = async (path) => JSON.parse(await readFile(join(cwd, path), 'utf8'));
const lock = await json('package-lock.json');
const distribution = await json('npm-distribution.json');
const sdkLock = lock.packages['node_modules/@earendil-works/pi-coding-agent'];
if (VERSION !== distribution.version || VERSION !== sdkLock.version ||
    sdkLock.integrity !== distribution.dist.integrity || sdkLock.resolved !== distribution.dist.tarball) {
  throw new Error('Installed SDK, lockfile and registry distribution evidence disagree');
}
for (const [path, entry] of Object.entries(lock.packages)) {
  if (path && (!entry.integrity || !entry.resolved?.startsWith('https://registry.npmjs.org/'))) {
    throw new Error(`Dependency is not registry/integrity pinned: ${path}`);
  }
}
const sourceFiles = (await readdir(cwd)).filter((name) => /\.(mjs|py|json)$/.test(name) && name !== 'RESULT.json');
sourceFiles.push(...(await readdir(join(cwd, 'test'))).map((name) => `test/${name}`));
const files = Object.fromEntries(await Promise.all(sourceFiles.sort().map(async (name) =>
  [name, createHash('sha256').update(await readFile(join(cwd, name))).digest('hex')])));
const checks = [
  { name: 'typecheck', args: ['node_modules/typescript/bin/tsc', '--noEmit'] },
  { name: 'sdk-tests', args: ['--test', '--test-concurrency=1', '--test-reporter=tap', ...sourceFiles.filter((name) => name.endsWith('.test.mjs'))] },
].map(({ name, args }) => {
  const result = spawnSync(process.execPath, args, { cwd, encoding: 'utf8', timeout: 60000 });
  return { name, command: ['node', ...args].join(' '), status: result.status === 0 ? 'PASS' : 'FAIL',
    exitCode: result.status, signal: result.signal, stdout: result.stdout, stderr: result.stderr,
    error: result.error?.message };
});
const report = {
  status: checks.every((check) => check.status === 'PASS') ? 'PASS' : 'FAIL',
  recordedAt: new Date().toISOString(), node: process.version, executable: process.execPath,
  platform: process.platform, architecture: process.arch,
  python: execFileSync(process.env.PYTHON ?? 'python3', ['--version'], { encoding: 'utf8' }).trim(),
  gitHeadAtRun: execFileSync('git', ['rev-parse', 'HEAD'], { cwd, encoding: 'utf8' }).trim(),
  sdk: { version: VERSION, importPath: relative(cwd, fileURLToPath(import.meta.resolve('@earendil-works/pi-coding-agent'))),
    tarball: sdkLock.resolved, integrity: sdkLock.integrity, nodeRequirement: distribution.engines.node,
    lockedDependencyCount: Object.keys(lock.packages).length - 1 },
  source: { sha256: createHash('sha256').update(JSON.stringify(files)).digest('hex'), files },
  checks,
  liveProvider: { status: 'NOT-RUN', reason: 'Ticket 01 uses deterministic loopback responses through the real native provider; no live credentials are required.' },
};
const output = join(cwd, 'evidence/result.json');
await mkdir(dirname(output), { recursive: true });
await writeFile(output, JSON.stringify(report, null, 2) + '\n');
console.log(JSON.stringify({ status: report.status, node: report.node, sdk: VERSION, sourceSha256: report.source.sha256, output }));
if (report.status !== 'PASS') process.exitCode = 1;
