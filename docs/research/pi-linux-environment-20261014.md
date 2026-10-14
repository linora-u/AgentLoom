# Linux Pi environment: read-only findings

Observed on `ssh dev` on 2026-10-14: x86_64, kernel `5.4.143.bsk.8-amd64`, Debian glibc `2.28`, `/usr/bin/node` `22.16.0`, npm `10.9.2`. This investigation changed no remote files, installed no package/runtime, and executed no OpenClaw or downloaded package.

## Minimum runtime

The locked AgentLoom bridge, Pi coding-agent/AI/TUI `1.0.0`, and transitive undici `8.10.2` require Node `>=22.19.0`. AgentLoom's environment-proxy path requires Node `22.21+` on the 22.x line or `24+`. Existing `22.16.0` fails the supported minimum; weakening the version check would not establish acceptance. Sources: [coding-agent metadata](https://registry.npmjs.org/@earendil-works/pi-coding-agent/1.0.0), [AI metadata](https://registry.npmjs.org/@earendil-works/pi-ai/1.0.0), [upstream coding-agent package](https://github.com/earendil-works/pi/blob/v1.0.0/packages/coding-agent/package.json), `src/runtimes/pi/bridge/package-lock.json`, and `src/runtimes/pi/install.py`.

Standard Node `22.21.0` Linux x64 binaries target kernel `>=4.18`, glibc `>=2.28`, and libstdc++ exposing `GLIBCXX_3.4.25`. The observed kernel/glibc meet those requirements and the host's existing `/usr/lib/x86_64-linux-gnu/libstdc++.so.6` exposes that symbol version. Source: [Node v22.21.0 BUILDING.md](https://github.com/nodejs/node/blob/v22.21.0/BUILDING.md). No special Node wheel is required by Pi.

## Existing Pi assets

An independent Pi installation remains at `/home/linyuchi.06/agentloom-validation/openclaw-owner-20261014/.venv/share/pi` (`/home` resolves to the `/data00/home` location). Its historical parent directory name does not change its contents: `ready.json` identifies Pi SDK `1.0.0`, linux/x86_64, fingerprint `cbdc75188a81c9eecf51f9bfd738790a9e43cda9b73da41bfa8ee81571f9c581`, and Node ABI `137`. `bridge/dist/index.js` and the exact SDK packages exist.

The existing system Node exposes ABI `127` and is below the SDK minimum. AgentLoom validates the recorded build identity, including Node ABI; these assets cannot currently count as a ready installation with that executable. Do not edit `ready.json` or restore the removed dedicated Node wheel to make the check pass. The standard Pi installer must verify/build the assets against the selected supported ordinary Node runtime.

## Native dependency evidence

| Locked component | Static evidence | Implication for this host |
| --- | --- | --- |
| Pi TUI `1.0.0` Linux x64 X11 addon | Existing `linux-platform-x11.node` exports `napi_register_module_v1`; `readelf` shows only `libxcb.so.1` as a needed shared library and no GLIBC symbol-version requirement. Upstream uses `-nostdlib`; libxcb exists on dev. | No observed glibc 2.28 blocker for this addon. The official loader only requests the Linux clipboard helper when `DISPLAY` exists, and treats unavailable helpers as optional. |
| esbuild Linux x64 `0.28.2` | Exact npm tarball's ELF has neither `PT_INTERP` nor `PT_DYNAMIC`; SHA-256 `9573bb2233aab0f9ea7647d5cca9726113cc1768de61d66b17267f4db84488f6`. | This static executable introduces no dynamic glibc version requirement. It was inspected, not executed. |
| photon-node `0.3.4` | Published artifact contains JS and `photon_rs_bg.wasm`. | Uses WebAssembly rather than a glibc-linked image addon. |
| quickjs-wasi `3.6.2` | `quickjs.wasm`; all five extension files named `.so` start with WebAssembly magic `00 61 73 6d`, not ELF. | The extension suffix does not indicate Linux shared-library/glibc dependencies. |

First-party sources: [Pi native loader](https://github.com/earendil-works/pi/blob/v1.0.0/packages/tui/src/native-platform.ts), [Pi Linux native build](https://github.com/earendil-works/pi/blob/v1.0.0/packages/tui/native/linux/build.sh), [TUI metadata](https://registry.npmjs.org/@earendil-works/pi-tui/1.0.0), [esbuild Linux metadata](https://registry.npmjs.org/@esbuild/linux-x64/0.28.2), [photon metadata](https://registry.npmjs.org/@silvia-odwyer/photon-node/0.3.4), and [quickjs metadata](https://registry.npmjs.org/quickjs-wasi/3.6.2).

## Smallest standard preparation to verify

Use a normally managed Node 22.x installation at least `22.21` for proxy compatibility, with npm on its ordinary PATH; no Python dependency, glibc, system-service, or execution-owner installation is needed. After that environment choice is authorized, run the existing `loom runtime install pi` entry in the isolated validation Python environment. It installs the unchanged locked npm dependency set with `npm ci --ignore-scripts`, compiles AgentLoom's bridge, imports the exact SDK, verifies the production handshake, and publishes a new ready build identity. It does not require the upstream TUI native build scripts, OpenClaw, a Node wheel, or another backend.

The Node/package metadata and static dependency evidence support this minimal route. They do not replace a Linux Pi handshake or the public Pi/MCP/CLI tests on the resulting build. Those remain required before claiming Linux acceptance.

Fetched metadata/archive observations are retained locally at `/Users/bytedance/.codex/tmp/agentloom-managed-execution-20261014/pi-research/`. The remote validation upload and final acceptance remain coordinated by the root task.
