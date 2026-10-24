"""Verify slow metadata work leaves the real Scrapy reactor responsive."""
import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = r'''
import asyncio
import json
from pathlib import Path
import sys
import threading
import time

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
from twisted.internet import asyncioreactor
asyncioreactor.install(loop)
from twisted.internet import defer, task

sys.path.insert(0, str(Path(sys.argv[1]) / 'daily_arxiv'))
from daily_arxiv.pipelines import DailyArxivPipeline

pipeline = DailyArxivPipeline()
lock = threading.Lock()
report = {'active': 0, 'max_active': 0, 'ticks_while_working': 0, 'finished': [], 'worker_threads': []}
main_thread = threading.get_ident()

def metadata(item):
    with lock:
        report['active'] += 1
        report['max_active'] = max(report['max_active'], report['active'])
        report['worker_threads'].append(threading.get_ident() != main_thread)
    try:
        time.sleep(0.2)  # Models slow requests and retry waits, entirely offline.
        if sys.argv[2] == 'error' and item['id'] == 'first':
            raise ValueError('fixture metadata failure')
        return dict(item, title='complete metadata')
    finally:
        with lock:
            report['active'] -= 1
            report['finished'].append(item['id'])

pipeline.enrich_item = metadata

@defer.inlineCallbacks
def run(reactor):
    def tick():
        with lock:
            if report['active']:
                report['ticks_while_working'] += 1
    timer = task.LoopingCall(tick)
    timer.start(0.01)
    first = pipeline.process_item({'id': 'first'}, None)
    second = pipeline.process_item({'id': 'second'}, None)
    results = yield defer.DeferredList([first, second], consumeErrors=True)
    timer.stop()
    report['success'] = [success for success, value in results]
    report['results'] = [value if success else str(value.value) for success, value in results]
    print(json.dumps(report))

task.react(run)
'''


class PipelineThreadingTests(unittest.TestCase):
    def run_fixture(self, mode):
        result = subprocess.run([sys.executable, '-c', FIXTURE, str(ROOT), mode],
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(report['max_active'], 1)
        self.assertTrue(all(report['worker_threads']))
        self.assertGreater(report['ticks_while_working'], 0)
        self.assertEqual(report['finished'], ['first', 'second'])
        return report

    def test_metadata_is_serialized_off_reactor(self):
        report = self.run_fixture('success')
        self.assertEqual(report['success'], [True, True])
        self.assertEqual([item['title'] for item in report['results']], ['complete metadata'] * 2)

    def test_metadata_failure_propagates_and_releases_queue(self):
        report = self.run_fixture('error')
        self.assertEqual(report['success'], [False, True])
        self.assertIn('fixture metadata failure', report['results'][0])
        self.assertEqual(report['results'][1]['title'], 'complete metadata')


if __name__ == '__main__':
    unittest.main()
