# Managed execution installation correction — #125

Status: the OpenClaw package-installation approach was withdrawn on 2026-10-14. The current instruction permits OpenClaw source reference only and prohibits installing, executing or packaging its original package, SDK or vendored runtime assets. The earlier automatic-provisioning implementation was restored before any commit; its generated payloads, isolated installations and caches were removed by the parent task. Its earlier green results are not acceptance evidence for the replacement implementation.

## Installation correction

The `durable` extra now contains only the existing DBOS 3.2.0 and filelock requirement. Its dedicated `nodejs-wheel-binaries` dependency and lock entry are removed. Python and all other locked package versions remain unchanged. CI no longer installs the OpenClaw execution-owner SDK or exports its runtime location.

The historical `loom runtime install execution-owner` command rejects the request with an explicit source-reference-only error. Direct `install_openclaw`, Node resolver and installed-asset resolver entrypoints reject with the same diagnostic. The installer contains no npm/Node execution, download, cache inspection or owner-selection logic. Pi's existing installation entrypoint and Node version declaration remain unchanged.

This is a containment correction, not a completed execution owner. #124 supplies the source-informed Python implementation; finite/durable wiring and the public fault matrix must validate that implementation before the full spec can be accepted. Old OpenClaw owner tests do not demonstrate its behavior and are not rerun by this installation correction.

## Source and historical artifact findings

The research checkout is OpenClaw 2026.10.3 at `59cb44796b0f1f0daa0e37af8b1495abb45d9bc1`. The official npm 2026.10.3 [SLSA statement](https://registry.npmjs.org/-/npm/v1/attestations/openclaw@2026.10.3) instead identified the published build commit as `ff338bdb01f2057229b50307ac0a8c1ad8b65a2a`. Seven of ten inspected process-source blobs differed, including the child adapter, relay, group anchor and Linux subreaper. Owned-stdio and runtime URL helper blobs matched. Source reference must identify which commit was inspected rather than treating the two as equivalent.

The withdrawn artifact's original `dist/child-DBWCsvzX.mjs` hash was `f6eec7373ea85d662b56265991db968f7122b595c0f040e65c1706268425ef79`. Its initial literal-import count of 89 files excluded runtime-selected helpers and external dependencies and was not a complete closure. The later abandoned packaging investigation measured 162 original OpenClaw files, 54 npm packages and 2,257 payload files: 13,180,786 compressed bytes and 44,243,262 expanded bytes. The host Node wheel added 55,666,374 download bytes and approximately 195,683,733 installed bytes. These are historical costs of a rejected route; none is a dependency requirement or deliverable of the replacement Python owner.

The original OpenClaw source license is MIT. The source-informed implementation must retain applicable source attribution and identify any code actually reused. No original executable artifact is included as evidence or a deliverable of this correction.

## Correction evidence

The public CLI test first failed because the old path attempted the third-party Node API. The direct installer test independently failed for the same reason. Both runs denied third-party Node/npm, OS process creation and network calls at their boundaries, so neither executed or downloaded an original runtime. After the correction, both entrypoints returned the explicit disabled diagnostic and the two tests passed.

Validation uses the existing Python environment directly, without synchronization, package installation, SDK construction or OpenClaw execution. Both new regressions and five existing source-installer boundary tests passed (seven total); static checks passed. `uv lock --offline` removed only the Node wheel package, and all remaining locked versions were compared against the baseline and remained identical. This evidence proves the withdrawn installation path is inert; it does not claim that a new owner already satisfies cleanup, recovery or default installation acceptance.
