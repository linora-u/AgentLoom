# Ticket 02 implementation and handoff

Date: 2026-09-20. Branch: `codex/pi-t02-contracts`. Base: `45ddbfdc`.
Status: implemented; awaiting integration-wide CI and independent review.

## Delivered

- `RuntimeModelSelection` carries existing profile identity, explicit protocol and private profile settings without constructing a Python provider. `RuntimeDefinition.model` is optional; native definitions can omit all smol-specific fields. Conflicting selection/binding identity is rejected. Definitions carry a fresh instance ID.
- `runtime_options` normalizes existing smol fields with source paths. Unknown smol options and differing legacy/new values fail with diagnostics identifying sources. Historical global smol defaults are ignored for native runtimes; explicit Agent/Application legacy smol choices fail. Existing smol planning-interval normalization is preserved.
- Requirements derive from selected tools/toolsets, Workers, Goal, checkpoint and configured Stop handlers. Registry creation also checks actual tool definitions. Goal and Stop are explicit capabilities, enabled for smol. Hook Bundle Stop gates participate in preflight without executing hooks.
- Native fixture applications run through `execute_app` with no tools, no Goal, no Python model binding or Todo state. Selecting unsupported functions fails before constructing the runtime. The fixture is not a Pi adapter.
- Worker tools always construct fresh owners, regardless of binding. A bounded barrier proves two native fixture Workers overlap while keeping instance identity, Hook Run and local Run distinct.
- smol alone installs Todo/terminal tools and Todo policy through its adapter. Both original platform tools and adapter-added tools use AgentLoom governance. Todo state is scoped to the smol runtime and survives its continuation segments. Factory failure closes its composed gateway.
- Original smol YAML and model bindings remain supported. Real smol application verification exercised file tools, Todo, terminal output and Goal continuation. Existing checkpoint/committed Worker tests stay green.

## Verification

Tests used Python 3.12.13 and the existing locked Python dependencies. `PYTHONPATH` pointed to a worktree-local `agentloom -> src` link, with the imported package path verified before testing; no source or provider credentials were copied from the main checkout. Only `config/llm.example.yaml` supplied the local default config.

- 1,379 passed: `tests/agent_test`, `tests/application_test`, `tests/smolagents_test`, `tests/lib_test/runtime`, `tests/hooks_test`, `tests/lib_test/checkpoint`, `tests/test_yaml_agent_factory_parallel.py`.
- 2 passed: coordinator's real smol `execute_app` compatibility tests, executed against this worktree's source (Goal off/on, deterministic provider, real smol loop and file tool).
- Red/green evidence included binding-free RuntimeDefinition, no-tool native Application, fresh Worker instances and contradictory model selections.
- Mypy 2.3.1 checked all 13 changed production modules with `--follow-imports=silent --ignore-missing-imports --python-executable <Python 3.12 environment>`: 12 existing diagnostics in ToolGateway schema inference (6), factory typing (2), application definition iterable typing (2), and configuration cached attributes (2). Running the same check on the unchanged baseline produced the identical 12 diagnostics, with only shifted line numbers; no new diagnostics. The two new modules are included in the changed check. Repository typechecking is not claimed globally clean.
- Ruff F checks on changed assembly/contracts/new modules and native acceptance test passed after removing unused imports. `git diff --check` passed.
- Complete repository CI and parallel standards/spec review are owned by the integration coordinator and have not been claimed completed here. No live provider run or Pi integration is claimed.

## Temporary compatibility forms for ticket 14

1. `RuntimeDefinition.model` and smol constructor fields (`max_steps`, `planning_interval`, `smart_summary`, `todo_mode`, `prompt_template_path`, `max_consecutive_model_errors`) remain available for old direct callers. New application assembly supplies `runtime_options` and `option_sources` instead. smol reconciles these and rejects conflicting values.
2. BaseAgent retains smol-only Python binding resolution and injection for existing framework callers; native runtimes resolve catalog data without the Python provider adapter. Other framework-owned background model operations are unchanged.
3. `runtime.tool_gateway.final_answer_binding` is a lazy compatibility import; its implementation belongs to `adapters.smolagents.terminal`. Future runtime adapters must not use it as a universal completion mechanism.
4. Old execution-config helpers remain for existing read-only tooling and callers. The new option normalization is the construction path; broad smol cleanup remains ticket 04.
5. Only smol is production-registered. Native fixture registration exists solely in tests. Ticket 03 must freeze the capabilities/options against the Pi SDK proof; ticket 07 supplies actual Pi execution, and ticket 09 supplies Pi Goal/Stop behavior.

## Model policy handoff

`RuntimeModelSelection.settings` snapshots the selected LLM profile. The separate effective `model_request_headers` policy must also be resolved per instance when ticket 03 freezes the native model projection and ticket 07 implements Pi translation. Preserve configured header precedence without putting credentials into public metadata, logs or fingerprints; the native fixture in ticket 02 does not claim provider/header interoperability.

## Prompt normalization follow-up

A post-implementation review found that equivalent old/new prompt paths could be compared before both were resolved, and historical global smol prompt paths were still checked for native runtimes. Both were first reproduced as failures, then corrected. Preflight now validates the selected smol option path, while native preflight skips historical smol prompt defaults. The three affected application/config suites passed (88 tests); focused mypy retains only the same two pre-existing definition iterable diagnostics.

## Integration preflight diagnostic fix

The integration suite exposed `toolsets: core_shell` reaching capability derivation before list-shape validation, producing a misleading unknown-single-character-toolset error. Requirements now validate the selected toolset list shape before resolving it. The existing six runner preflight cases reproduce the regression and all pass after the fix.
