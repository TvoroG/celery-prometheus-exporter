from __future__ import print_function
import argparse
import celery
import celery.states
import celery.events
import collections
from itertools import chain
import logging
import prometheus_client
import signal
import sys
import threading
import time
import json
import os

__VERSION__ = (1, 2, 0, 'final', 0)


DEFAULT_BROKER = os.environ.get('BROKER_URL', 'redis://redis:6379/0')
DEFAULT_ADDR = os.environ.get('DEFAULT_ADDR', '0.0.0.0:8888')
DEFAULT_MAX_TASKS_IN_MEMORY = int(os.environ.get('DEFAULT_MAX_TASKS_IN_MEMORY', '10000'))

LOG_FORMAT = '[%(asctime)s] %(name)s:%(levelname)s: %(message)s'

TASKS = prometheus_client.Gauge(
    'celery_tasks', 'Number of tasks per state', ['state'])
TASKS_NAME = prometheus_client.Gauge(
    'celery_tasks_by_name', 'Number of tasks per state and name',
    ['state', 'name'])
TASKS_RUNTIME = prometheus_client.Histogram(
    'celery_tasks_runtime_seconds', 'Task runtime (seconds)',
    ['name'])
WORKERS = prometheus_client.Gauge(
    'celery_workers', 'Number of alive workers')
LATENCY = prometheus_client.Histogram(
    'celery_task_latency', 'Seconds between a task is received and started.')
QUEUE_SIZE = prometheus_client.Gauge(
    'celery_queue_size', 'Size of celery queue',
    ['name']
)
QUEUE_TASKS = prometheus_client.Gauge(
    'celery_queue_tasks', 'Number of tasks in queue',
    ['name', 'task']
)


class MonitorThread(threading.Thread):
    """
    MonitorThread is the thread that will collect the data that is later
    exposed from Celery using its eventing system.
    """

    def __init__(self, app=None, *args, **kwargs):
        self._app = app
        self.log = logging.getLogger('monitor')
        max_tasks_in_memory = kwargs.pop('max_tasks_in_memory', DEFAULT_MAX_TASKS_IN_MEMORY)
        self._state = self._app.events.State(max_tasks_in_memory=max_tasks_in_memory)
        self._known_states = set()
        self._known_states_names = set()
        self._tasks_started = dict()
        super(MonitorThread, self).__init__(*args, **kwargs)

    def run(self):  # pragma: no cover
        self._monitor()

    def _process_event(self, evt):
        # Events might come in in parallel. Celery already has a lock
        # that deals with this exact situation so we'll use that for now.
        with self._state._mutex:
            if celery.events.group_from(evt['type']) == 'task':
                evt_state = evt['type'][5:]
                try:
                    # Celery 4
                    state = celery.events.state.TASK_EVENT_TO_STATE[evt_state]
                except AttributeError:  # pragma: no cover
                    # Celery 3
                    task = celery.events.state.Task()
                    task.event(evt_state)
                    state = task.state
                if state == celery.states.STARTED:
                    self._observe_latency(evt)
                self._collect_tasks(evt, state)

    def _observe_latency(self, evt):
        try:
            prev_evt = self._state.tasks[evt['uuid']]
        except KeyError:  # pragma: no cover
            pass
        else:
            # ignore latency if it is a retry
            if prev_evt.state == celery.states.RECEIVED:
                LATENCY.observe(
                    evt['local_received'] - prev_evt.local_received)

    def _collect_tasks(self, evt, state):
        if state in celery.states.READY_STATES:
            self._incr_ready_task(evt, state)
        else:
            # add event to list of in-progress tasks
            self._state._event(evt)
        self._collect_unready_tasks()

    def _incr_ready_task(self, evt, state):
        TASKS.labels(state=state).inc()
        try:
            # remove event from list of in-progress tasks
            event = self._state.tasks.pop(evt['uuid'])
            TASKS_NAME.labels(state=state, name=event.name).inc()
            if 'runtime' in evt:
                TASKS_RUNTIME.labels(name=event.name) \
                             .observe(evt['runtime'])
        except (KeyError, AttributeError):  # pragma: no cover
            pass

    def _collect_unready_tasks(self):
        # count unready tasks by state
        cnt = collections.Counter(t.state for t in self._state.tasks.values())
        self._known_states.update(cnt.elements())
        for task_state in self._known_states:
            TASKS.labels(state=task_state).set(cnt[task_state])

        # count unready tasks by state and name
        cnt = collections.Counter(
            (t.state, t.name) for t in self._state.tasks.values() if t.name)
        self._known_states_names.update(cnt.elements())
        for task_state in self._known_states_names:
            TASKS_NAME.labels(
                state=task_state[0],
                name=task_state[1],
            ).set(cnt[task_state])

    def _monitor(self):  # pragma: no cover
        while True:
            try:
                with self._app.connection() as conn:
                    recv = self._app.events.Receiver(conn, handlers={
                        '*': self._process_event,
                    })
                    setup_metrics(self._app)
                    recv.capture(limit=None, timeout=None, wakeup=True)
                    self.log.info("Connected to broker")
            except Exception as e:
                self.log.exception("Queue connection failed")
                setup_metrics(self._app)
                time.sleep(5)


class WorkerMonitoringThread(threading.Thread):
    celery_ping_timeout_seconds = 5
    periodicity_seconds = 5

    def __init__(self, app=None, *args, **kwargs):
        self._app = app
        self.log = logging.getLogger('workers-monitor')
        super(WorkerMonitoringThread, self).__init__(*args, **kwargs)

    def run(self):  # pragma: no cover
        while True:
            self.update_workers_count()
            time.sleep(self.periodicity_seconds)

    def update_workers_count(self):
        try:
            WORKERS.set(len(self._app.control.ping(
                timeout=self.celery_ping_timeout_seconds)))
        except Exception as exc: # pragma: no cover
            self.log.exception("Error while pinging workers")


class QueueMonitoringThread(threading.Thread):
    periodicity_seconds = 15

    def __init__(self, app=None, *args, **kwargs):  # pragma: no cover
        self._app = app
        self.log = logging.getLogger('queue-size')
        super(QueueMonitoringThread, self).__init__(*args, **kwargs)

    def run(self):  # pragma: no cover
        while True:
            try:
                self.update_queues_metrics()
            except Exception as exc:
                self.log.exception("Error while trying to update queues size")
            time.sleep(self.periodicity_seconds)

    def update_queues_metrics(self):
        queue_names = self.get_queue_names()
        queues = self.get_queues(queue_names)

        known_queues = set([])
        known_tasks = set([])

        for queue_name, (size, tasks) in zip(queue_names, chunks(queues, 2)):
            QUEUE_SIZE.labels(name=queue_name).set(size)
            known_queues.add((queue_name,))

            for task_name, count in self.get_tasks_stat(tasks).items():
                QUEUE_TASKS.labels(name=queue_name, task=task_name).set(count)
                known_tasks.add((queue_name, task_name))

        _reset_metrics(QUEUE_SIZE, known_queues)
        _reset_metrics(QUEUE_TASKS, known_tasks)

    def get_queue_names(self):
        active_queues = self._app.control.inspect().active_queues().values()

        names = []
        for node in active_queues:
            for queue in node:
                names.append(queue['name'])

        return names

    def get_queues(self, names):
        with self._app.connection_or_acquire() as connection:
            redis = connection.default_channel.client

            with redis.pipeline() as pipe:
                for name in names:
                    pipe.llen(name)
                    pipe.lrange(name, 0, -1)

                return pipe.execute()

    def get_tasks_stat(self, tasks):
        name2count = collections.defaultdict(int)
        for task in tasks:
            try:
                task_json = json.loads(task)
            except json.decoder.JSONDecodeError:
                continue

            if 'headers' in task_json and 'task' in task_json['headers']:
                name2count[task_json['headers']['task']] += 1
        return name2count


class EnableEventsThread(threading.Thread):
    periodicity_seconds = 5

    def __init__(self, app=None, *args, **kwargs):  # pragma: no cover
        self._app = app
        self.log = logging.getLogger('enable-events')
        super(EnableEventsThread, self).__init__(*args, **kwargs)

    def run(self):  # pragma: no cover
        while True:
            try:
                self.enable_events()
            except Exception as exc:
                self.log.exception("Error while trying to enable events")
            time.sleep(self.periodicity_seconds)

    def enable_events(self):
        self._app.control.enable_events()


def setup_metrics(app):
    """
    This initializes the available metrics with default values so that
    even before the first event is received, data can be exposed.
    """
    WORKERS.set(0)
    try:
        inspect = app.control.inspect()
        registered_tasks = inspect.registered_tasks().values()
        active_queues = inspect.active_queues().values()
    except Exception:  # pragma: no cover
        _reset_metrics(TASKS)
        _reset_metrics(TASKS_NAME)
    else:
        for state in celery.states.ALL_STATES:
            TASKS.labels(state=state).set(0)
            for task_name in set(chain.from_iterable(registered_tasks)):
                TASKS_NAME.labels(state=state, name=task_name).set(0)


def _reset_metrics(metrics, known_labels=None):
    for metric in metrics.collect():
        for sample in metric.samples:
            if known_labels is not None:
                labels = tuple(sample.labels[label] for label in metrics._labelnames)

                if labels not in known_labels:
                    metrics.labels(**sample.labels).set(0)
            else:
                metrics.labels(**sample.labels).set(0)


def chunks(l, n):
    for i in range(0, len(l), n):
        yield l[i:i + n]


def start_httpd(addr):  # pragma: no cover
    """
    Starts the exposing HTTPD using the addr provided in a separate
    thread.
    """
    host, port = addr.split(':')
    logging.info('Starting HTTPD on {}:{}'.format(host, port))
    prometheus_client.start_http_server(int(port), host)


def shutdown(signum, frame):  # pragma: no cover
    """
    Shutdown is called if the process receives a TERM signal. This way
    we try to prevent an ugly stacktrace being rendered to the user on
    a normal shutdown.
    """
    logging.info("Shutting down")
    sys.exit(0)


def main():  # pragma: no cover
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--broker', dest='broker', default=DEFAULT_BROKER,
        help="URL to the Celery broker. Defaults to {}".format(DEFAULT_BROKER))
    parser.add_argument(
        '--transport-options', dest='transport_options',
        help=("JSON object with additional options passed to the underlying "
              "transport."))
    parser.add_argument(
        '--addr', dest='addr', default=DEFAULT_ADDR,
        help="Address the HTTPD should listen on. Defaults to {}".format(
            DEFAULT_ADDR))
    parser.add_argument(
        '--enable-events', action='store_true',
        help="Periodically enable Celery events")
    parser.add_argument(
        '--tz', dest='tz',
        help="Timezone used by the celery app.")
    parser.add_argument(
        '--verbose', action='store_true', default=False,
        help="Enable verbose logging")
    parser.add_argument(
        '--max_tasks_in_memory', dest='max_tasks_in_memory', default=DEFAULT_MAX_TASKS_IN_MEMORY, type=int,
        help="Tasks cache size. Defaults to {}".format(DEFAULT_MAX_TASKS_IN_MEMORY))
    parser.add_argument(
        '--version', action='version',
        version='.'.join([str(x) for x in __VERSION__]))
    opts = parser.parse_args()

    if opts.verbose:
        logging.basicConfig(level=logging.DEBUG, format=LOG_FORMAT)
    else:
        logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    if opts.tz:
        os.environ['TZ'] = opts.tz
        time.tzset()

    app = celery.Celery(broker=opts.broker)

    if opts.transport_options:
        try:
            transport_options = json.loads(opts.transport_options)
        except ValueError:
            print("Error parsing broker transport options from JSON '{}'"
                  .format(opts.transport_options), file=sys.stderr)
            sys.exit(1)
        else:
            app.conf.broker_transport_options = transport_options

    setup_metrics(app)

    t = MonitorThread(app=app, max_tasks_in_memory=opts.max_tasks_in_memory)
    t.daemon = True
    t.start()

    w = WorkerMonitoringThread(app=app)
    w.daemon = True
    w.start()

    q = QueueMonitoringThread(app=app)
    q.daemon = True
    q.start()

    e = None
    if opts.enable_events:
        e = EnableEventsThread(app=app)
        e.daemon = True
        e.start()

    start_httpd(opts.addr)

    t.join()
    w.join()
    q.join()
    if e is not None:
        e.join()


if __name__ == '__main__':  # pragma: no cover
    main()
