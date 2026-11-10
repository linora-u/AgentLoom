# Tool execution Context

Tools can receive trusted execution dependencies without asking the model to
provide them. Annotate a keyword-capable parameter with `ToolContext`:

```python
from agentloom.execution.tool_context import ToolContext
from agentloom.execution.tool_gateway import bind_tool

def save_result(text: str, *, ctx: ToolContext):
    return ctx.bindings["sessions"][ctx.item_id].save(text)

binding = bind_tool(save_result)
```

Only `text` appears in the model schema. The Gateway injects `ctx` after model
argument validation. A model-supplied value for the internal parameter is
rejected. Existing tools without this annotation keep their normal arguments,
including business arguments named `ctx` or `context`.
Delayed annotations must resolve `ToolContext` in the callable's defining module.
An unrelated unknown business type does not hide an internal Context parameter;
an explicitly declared internal type that cannot be resolved fails preflight.

`ToolFactoryContext` is available while creating tools. Its `input` is the
model-visible invocation input; `bindings` and `item_id` are host-only. The
Gateway captures the trusted dependencies at creation, so they remain available
after the factory scope exits. Bindings are a shallow read-only mapping: a
session or lock retains its identity and is not serialized or deep-copied.

`ToolContext` is available during one execution. It contains `call_id`,
`tool_name`, `item_id`, `agent_name`, `bindings`, and optional native `identity`
and `assistant_tools`. Pi supplies the actual ordered tool names from the
current assistant response, including repeated names. These are invocation
facts; decisions about which actions can occur together belong to the tool.
Direct nested calls receive their own Context. A new native dispatch supplies
its own identity; a nested direct call does not inherit the outer native call.
`current_tool_context()` returns the same Context during execution and restores
the previous scope when the call exits.

## Content storage

Use tool-owned files or session state for saved content, revisions, and paging.
Return an explicit query or continuation offset when more content is available.
Keep continuations tied to one result version and report stale versions rather
than joining pages from different revisions.

With `runtime.trace.mode: off`, AgentLoom does not create a TraceRecorder,
temporary Trace, automatic ContextEngine store, or implicit ContextRef copy of
tool results. It also does not automatically add `loom_retrieve_context`.
An explicitly selected retrieval tool can still read an explicitly provided
store. Pi's required native execution journals, checkpoint state, and final Run
results remain available; they are separate from optional tracing. Full mode
retains the existing Trace and ContextRef behavior.
