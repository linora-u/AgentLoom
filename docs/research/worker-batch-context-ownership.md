# Worker batch Context and resource ownership

This is the concurrency contract for Application Run → `BoundWorker.map()` in
[Worker batch execution v1](../specs/worker-batch-execution-v1.md). Copying a
Python Context copies its values by reference; isolation depends on the owners
and synchronization below.

`Application.open()` and `execute_app()` each own one independent Run. Nested
Application scopes restore the enclosing Run's services on exit. A binding
belongs to its original owner even when invoked from another scope or a thread
with no inherited Context. The owner captures its baseline after root services
are ready; each such call executes in a fresh copy. Calls already in the same
owner preserve their parent invocation Context, including child lineage.

Each map owns one executor and one admission window. Capacity ends only after
the invocation and its owned cleanup complete. The owner closes registration,
stops admission, drains accepted batches, retries retained handles, then closes
root services. Failed cleanup is evidence of incomplete ownership release;
it cannot authorize replacement work or a successful Run receipt.

| State | Owner and concurrency contract |
| --- | --- |
| Project/Application settings | Captured when the scope opens. No per-item disk refresh or mutation of process-global configuration. |
| Worker definition, effective model configuration, Skills and Hook plan | Captured at first binding. Cached lexical identity reuses the snapshot even if the definition is later deleted or replaced. Fresh Agents receive separate copied configuration. The next scope observes new disk state. |
| WorkPlan/input | Deeply immutable JSON snapshot; each invocation receives its own mutable copy. Business IDs and input fingerprint do not depend on execution capacity. |
| RuntimeContext | Shared immutable Run identity and paths. Each batch writes its own record; root reference aggregation and manifest read/modify/write are synchronized. |
| Root memory snapshot | One synchronized `RootRunState` per Run. All child Contexts borrow it; no sibling reread of newly activated memory. |
| Agent, Runtime, Tool gateway, HookRun and invocation IDs | Fresh per invocation. No shared mutable Agent or Runtime. Resource keys include Run and invocation ownership, so one sibling cannot close another sibling's handles. |
| CheckpointCoordinator | Root-owned and borrowed by Workers. Worker calls use the normal `SubTaskTrackedAgent` checkpoint path; they do not load/save the Supervisor Runtime checkpoint. Worker allocation/heartbeat state is synchronized. Root teardown follows drain. |
| ContextEngine/ContextStore | One root task store, with synchronized mutations. Checkpoint-disabled runners also retain the store until all accepted batches and cleanup finish. A nested new Application root never borrows the enclosing root's store. |
| Trace, usage and budgets | Shared recorder sequences and model-input-limit access are locked; budget state is indexed by invocation-local Run ID. Outcome usage comes from that invocation's report, never a parent Agent accumulator. |
| Runtime event sink/logger | Borrow the owner's Run services. Root event collection is synchronized. Child terminal reports are delivered to invocation-local evidence rather than the root lifecycle's single Supervisor report slot. |
| Workspace cwd/sys.path | Process-wide state. Overlapping Application scopes must use the same canonical project root; a different project root is rejected before execution. This restriction adds no Worker quota. Tools that mutate cwd or shared business files still need Application-specific coordination. |
| Tool dispatch | Owned by Pi/smolagents. Worker capacity controls top-level batch invocations only; child maps, Agent-as-Tool and Runtime Tool calls use their own existing behavior. |

The synchronous map has no exit deadline and does not call `asyncio.run()`.
Running threads and arbitrary synchronous Tool callbacks cannot be killed by
Future cancellation. Ordinary observer errors disable that observer and record
diagnostics; process-control exceptions request stop, drain, persist, then
propagate. Provider request retries remain Adapter-owned; the batch does not
replay a complete Worker after side effects.

## Explicit offload from an async Application

Applications that need an async entrypoint can own a small offload adapter:

```python
import asyncio
from threading import Event

async def offload_map(worker, plan, *, concurrency):
    stop = Event()
    task = asyncio.create_task(asyncio.to_thread(
        worker.map, plan, concurrency=concurrency, stop_token=stop,
    ))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancellation:
        stop.set()
        # Repeated cancellation requests still cannot detach owned work.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                stop.set()
            except BaseException:
                break
        try:
            task.result()
        except BaseException as failure:
            raise cancellation from failure
        raise
```

Keep the Application scope open until this adapter returns or propagates.
Cancellation stops admission and waits for actual Runtime and resource cleanup;
it does not cancel the offloaded thread. This is explicit Application code, not
a native async batch API. A synchronous Tool that depends on the caller's event
loop must use an Application-approved thread-safe bridge.

The deterministic public acceptance lives in
`tests/application_test/test_worker_batch_application.py`; real Pi/smolagents
runner/CLI and supported-Python evidence are separate delivery gates.
