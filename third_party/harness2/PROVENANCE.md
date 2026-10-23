# Vendored Harness²

Source: https://github.com/google-research/harness2

Pinned revision: `1cfe254e8d25b15f37048c3730134686abe60335`.

The baseline commit contains the upstream tracked source tree unchanged except for omitted promotional media in `assets/`. It includes upstream tests, documentation, dependency pins and third-party license/provenance notices. Benchmark datasets, model services and optional benchmark environments are neither fetched nor installed. The root AgentLoom distribution installs the `harness2` Python package directly; the upstream standalone Python version constraint is not used as a runtime dependency. The stdlib method core is verified on AgentLoom's Python 3.14.8 environment.

Subsequent commits contain the limited AgentLoom integration changes listed in `harness2/PATCHES.md`. Google retains copyright in upstream files; the Apache-2.0 license is preserved in `LICENSE` and installed beside the package.
