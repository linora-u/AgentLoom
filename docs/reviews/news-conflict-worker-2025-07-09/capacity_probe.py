"""Real SDK queues/processes; a controlled no-model Host holds five slots."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from threading import Event
import time

from agentloom.app import Application
from agentloom.execution.concurrency import WorkItem, WorkerOutcome
from agentloom.execution.durable import HostSpec

CASE = Path(__file__).resolve().parent
PROJECT = CASE.parents[2]
APP = PROJECT / 'applications/news_agent'
ROOT = CASE / 'execution'
WORKERS = ('workflows/validation.yaml', 'workflows/conflict.yaml')


def emit(event, **fields):
    fd = os.open(CASE / 'events.jsonl', os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps({'event': event, 'time_ns': time.monotonic_ns(), **fields})+'\n').encode())
    finally:
        os.close(fd)


class ProbeHost:
    def reconcile(self, task):
        return None
    def authorize(self, task, invocation_id, assert_current):
        assert_current()
    def revoke(self, task, invocation_id):
        pass
    def invoke(self, task, invocation_id):
        conflict = task.worker == WORKERS[1]
        emit('started', id=task.item.id, conflict=conflict)
        while conflict and not (CASE / 'release').exists():
            time.sleep(.05)
        emit('returned', id=task.item.id, conflict=conflict)
        return WorkerOutcome(task.item.id, 0, 'success', output={'status': 'completed'}).to_dict()


def host(options):
    return ProbeHost()


def events():
    path = CASE / 'events.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def wait_for(predicate, process, timeout=55):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        if process.poll() is not None:
            raise RuntimeError(f'executor exited: {process.returncode}; see consumer.log')
        time.sleep(.1)
    raise TimeoutError('SDK queue probe timed out; see consumer.log')


def main():
    spec = HostSpec('capacity_probe:host', {}, allow_replay=False, max_attempts=1)
    with Application.open(APP, project_root=PROJECT, file_logging=False) as app, app.durable(root=ROOT) as execution:
        if len(sys.argv)>1:
            stop=Event()
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
            execution.executor(WORKERS, host=spec).run(stop_token=stop)
            return
        normal, conflict = [execution.worker(path) for path in WORKERS]
        assert normal.queue_name != conflict.queue_name
        queues = {path: execution.client.retrieve_queue(worker.queue_name) for path,worker in zip(WORKERS,(normal,conflict))}
        assert queues[WORKERS[0]].worker_concurrency == queues[WORKERS[0]].global_concurrency == 20
        assert queues[WORKERS[1]].worker_concurrency == queues[WORKERS[1]].global_concurrency == 5
        handles=conflict.submit_many([WorkItem(f'conflict-{n}', {'query':'no-model capacity probe'}) for n in range(6)])
        environment=dict(os.environ)
        environment['PYTHONPATH']=str(CASE)+os.pathsep+environment.get('PYTHONPATH','')
        with (CASE/'consumer.log').open('w') as log:
            process=subprocess.Popen([sys.executable,str(Path(__file__)),'consume'],env=environment,stdout=log,stderr=log)
            try:
                wait_for(lambda:len([e for e in events() if e['event']=='started' and e['conflict']])==5,process)
                ordinary=normal.submit(WorkItem('ordinary',{'query':'independent no-model probe'}))
                wait_for(lambda:execution.status(ordinary.workflow_id).status=='SUCCESS',process)
                before=[execution.status(h.workflow_id).status for h in handles]
                assert before.count('ENQUEUED')==1, before
                assert len([e for e in events() if e['event']=='started' and e['conflict']])==5
                print(json.dumps({'five_conflicts_held':True,'ordinary_completed_while_conflicts_held':True,'sixth_waiting':True}),flush=True)
                (CASE/'release').touch()
                wait_for(lambda:all(execution.status(h.workflow_id).status=='SUCCESS' for h in handles),process)
                execution.verify_terminal_completion()
                resources=execution.resource_status()
                assert len(resources)==7 and all(r['physical_exit_confirmed'] and r['exit_receipt_persisted'] for r in resources)
                report={'worker_capacities':{'validation':20,'conflict':5},'different_queue_names':True,
                        'held_conflicts':5,'sixth_waited':True,'ordinary_completed_while_conflicts_held':True,
                        'resource_exit_receipts':resources,'all_seven_successful':True,'model_calls':0}
                (CASE/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
            finally:
                (CASE/'release').touch()
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=55)
            assert process.returncode==0
        print('real SDK independent queue probe passed',flush=True)


if __name__=='__main__':
    main()
