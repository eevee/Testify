"""
Client-server setup to evenly distribute tests across multiple processes. The server
discovers all test classes and enqueues them, then clients connect to the server,
receive tests to run, and send back their results.

The server keeps track of the overall status of the run and manages timeouts and retries.
"""

import collections
import logging

from .test_fixtures import FIXTURES_WHICH_CAN_RETURN_UNEXPECTED_RESULTS
from .test_runner import TestRunner
import six
import tornado.httpserver
import tornado.ioloop
import tornado.web

_log = logging.getLogger('testify')

try:
    import simplejson as json  # noqa
except ImportError:
    import json
import logging

import time


class Work(collections.namedtuple('Work', ('priority', 'worker', 'runner'))):
    def __lt__(self, other):
        return (
            (self.priority, self.worker.__name__, self.runner) <
            (other.priority, other.worker.__name__, other.runner)
        )


class AsyncDelayedQueue(object):
    def __init__(self):
        self.test_queue = six.moves.queue.PriorityQueue()
        self.worker_queue = six.moves.queue.PriorityQueue()
        self.finalized = False

    def add_worker(self, w_priority, worker, runner=None):
        """Queue up a worker to receive a test."""
        if self.finalized:
            worker(None, None)
            return

        self.worker_queue.put(Work(w_priority, worker, runner))
        tornado.ioloop.IOLoop.instance().add_callback(self.match)

    def add_test(self, t_priority, test):
        """Queue up a test to get given to a worker."""
        # Priority queues need sortable things, so we'll convert the test
        # dict to a tuple representation
        test = tuple(sorted(list(test.items())))
        self.test_queue.put((t_priority, test))
        tornado.ioloop.IOLoop.instance().add_callback(self.match)

    def match(self):
        """Try to pair a test to a worker.

        This loops over each queued worker (and each queued test)
        trying to find a match. It breaks out of the loop as soon as
        it finds a valid worker-test match, re-queueing anything it
        skipped. (In the worst case, this is O(n^2), but most of the
        time no loop iterations beyond the first will be necessary -
        the vast majority of the time, the first worker will match
        the first test).
        """
        worker = None
        runner = None
        test = None

        skipped_workers = []
        while worker is None:
            try:
                w_priority, worker, runner = self.worker_queue.get_nowait()
            except six.moves.queue.Empty:
                break

            while test is None:
                try:
                    t_priority, test = self.test_queue.get_nowait()
                    test = dict(test)
                except six.moves.queue.Empty:
                    break

            if test is None:
                skipped_workers.append(Work(w_priority, worker, runner))
                worker = None
                continue

        for skipped in skipped_workers:
            self.worker_queue.put(skipped)

        if worker is not None:
            worker(w_priority, test)
            tornado.ioloop.IOLoop.instance().add_callback(self.match)

    def empty(self):
        """Returns whether or not we have any pending tests."""
        return self.test_queue.empty()

    def waiting(self):
        """Returns whether or not we have any pending workers."""
        return self.worker_queue.empty()

    def finalize(self):
        """Immediately call any pending workers with None, None
        and ensure that any future get() calls do the same."""
        self.finalized = True
        try:
            while True:
                _, worker, _ = self.worker_queue.get_nowait()
                worker(None, None)
        except six.moves.queue.Empty:
            pass


class TestRunnerServer(TestRunner):
    def __init__(self, *args, **kwargs):
        self.serve_port = kwargs.pop('serve_port')
        self.runner_timeout = kwargs['options'].runner_timeout
        self.revision = kwargs['options'].revision
        self.server_timeout = kwargs['options'].server_timeout
        self.shutdown_delay_for_connection_close = kwargs['options'].shutdown_delay_for_connection_close
        self.shutdown_delay_for_outstanding_runners = kwargs['options'].shutdown_delay_for_outstanding_runners
        self.disable_requeueing = kwargs['options'].disable_requeueing

        self.pair_queue = AsyncDelayedQueue()
        self.checked_out = {}  # Keyed on class path (module class).
        self.failed_rerun_methods = set()  # Set of (class_path, method) who have failed.
        self.timeout_rerun_methods = set()  # Set of (class_path, method) who were sent to a client but results never came.
        self.previous_run_results = {}  # Keyed on (class_path, method), values are result dictionaries.
        self.runners = set()  # The set of runner_ids who have asked for tests.
        self.runners_outstanding = set()  # The set of runners who have posted results but haven't asked for the next test yet.
        self.shutting_down = False  # Whether shutdown() has been called.

        super(TestRunnerServer, self).__init__(*args, **kwargs)

    def get_next_test(self, runner_id, on_test_callback, on_empty_callback):
        """Enqueue a callback (which should take one argument, a test_dict) to be called when the next test is available."""

        self.runners.add(runner_id)

        def callback(w_priority, test_dict):
            if not test_dict:
                return on_empty_callback()

            # if there's just one worker run the test even if it's failed there before
            if (
                    test_dict.get('last_runner', None) != runner_id or
                    len(self.runners) <= 1
            ):
                self.check_out_class(runner_id, test_dict)
                on_test_callback(test_dict)
            else:
                # Put the test back in the queue, but give the worker a lower priority.
                self.pair_queue.add_test(0, test_dict)
                self.pair_queue.add_worker(w_priority + 1, callback, runner=runner_id)

        self.pair_queue.add_worker(0, callback, runner=runner_id)

    def report_result(self, runner_id, result):
        class_path = '%s %s' % (result['method']['module'], result['method']['class'])
        d = self.checked_out.get(class_path)

        if not d:
            raise ValueError("Class %s not checked out." % class_path)
        if d['runner'] != runner_id:
            raise ValueError("Class %s checked out by runner %s, not %s" % (class_path, d['runner'], runner_id))
        if result['method']['name'] not in d['methods']:
            # If class_teardown failed, the client will send us a result to let us
            # know. If that happens, don't worry about the apparently un-checked
            # out test method.
            if result['method']['fixture_type'] in FIXTURES_WHICH_CAN_RETURN_UNEXPECTED_RESULTS:
                pass
            else:
                raise ValueError("Method %s not checked out by runner %s." % (result['method']['name'], runner_id))

        self.activity()

        if result['success']:
            d['passed_methods'][result['method']['name']] = result
        else:
            d['failed_methods'][result['method']['name']] = result
            self.failure_count += 1
            if self.failure_limit and self.failure_count >= self.failure_limit:
                logging.error('Too many failures, shutting down.')
                return self.early_shutdown()

        d['timeout_time'] = time.time() + self.runner_timeout

        # class_teardowns are special.
        if result['method']['fixture_type'] not in FIXTURES_WHICH_CAN_RETURN_UNEXPECTED_RESULTS:
            d['methods'].remove(result['method']['name'])

        if not d['methods']:
            self.check_in_class(runner_id, class_path, finished=True)

    def run(self):
        class TestsHandler(tornado.web.RequestHandler):
            @tornado.web.asynchronous
            def get(handler):
                runner_id = handler.get_argument('runner')

                if self.shutting_down:
                    self.runners_outstanding.discard(runner_id)
                    return handler.finish(json.dumps({
                        'finished': True,
                    }))

                if self.revision and self.revision != handler.get_argument('revision'):
                    return handler.send_error(
                        409,
                        reason="Incorrect revision %s -- server is running revision %s" % (
                            handler.get_argument('revision'), self.revision
                        ),
                    )

                def callback(test_dict):
                    self.runners_outstanding.discard(runner_id)
                    handler.finish(json.dumps({
                        'class': test_dict['class_path'],
                        'methods': test_dict['methods'],
                        'finished': False,
                    }))

                def empty_callback():
                    self.runners_outstanding.discard(runner_id)
                    handler.finish(json.dumps({
                        'finished': True,
                    }))

                self.get_next_test(runner_id, callback, empty_callback)

            def finish(handler, *args, **kwargs):
                super(TestsHandler, handler).finish(*args, **kwargs)
                tornado.ioloop.IOLoop.instance().add_callback(handler.after_finish)

            def after_finish(handler):
                if self.shutting_down and not self.runners_outstanding:
                    iol = tornado.ioloop.IOLoop.instance()
                    iol.add_callback(iol.stop)

        class ResultsHandler(tornado.web.RequestHandler):
            def post(handler):
                runner_id = handler.get_argument('runner')
                self.runners_outstanding.add(runner_id)
                result = json.loads(handler.request.body.decode('UTF-8'))

                try:
                    self.report_result(runner_id, result)
                except ValueError as e:
                    return handler.send_error(409, reason=str(e))

                return handler.finish("kthx")

            def get_error_html(handler, status_code, **kwargs):
                reason = kwargs.pop('reason', None)
                if reason:
                    return reason
                else:
                    return super(ResultsHandler, handler).get_error_html(status_code, **kwargs)

        try:
            # Enqueue all of our tests.
            discovered_tests = []
            try:
                discovered_tests = self.discover()
            except Exception as exc:
                _log.debug("Test discovery blew up!: %r" % exc)
                raise
            for test_instance in discovered_tests:
                test_dict = {
                    'class_path': '%s %s' % (test_instance.__module__, test_instance.__class__.__name__),
                    'methods': [test.__name__ for test in test_instance.runnable_test_methods()],
                }

                if test_dict['methods']:
                    # When the client has finished running the entire TestCase,
                    # it will signal us by sending back a result with method
                    # name 'run'. Add this result to the list we expect to get
                    # back from the client.
                    test_dict['methods'].append('run')
                    self.pair_queue.add_test(0, test_dict)

            # Start an HTTP server.
            application = tornado.web.Application([
                (r"/tests", TestsHandler),
                (r"/results", ResultsHandler),
            ])

            server = tornado.httpserver.HTTPServer(application)
            server.listen(self.serve_port)

            def timeout_server():
                if time.time() > self.last_activity_time + self.server_timeout:
                    logging.error('No client activity for %ss, shutting down.' % self.server_timeout)
                    self.shutdown()
                else:
                    tornado.ioloop.IOLoop.instance().add_timeout(self.last_activity_time + self.server_timeout, timeout_server)
            self.activity()
            timeout_server()  # Set the first callback.

            tornado.ioloop.IOLoop.instance().start()

        finally:
            # Report what happened, even if something went wrong.
            report = [reporter.report() for reporter in self.test_reporters]
            return all(report)

    def activity(self):
        self.last_activity_time = time.time()

    def check_out_class(self, runner, test_dict):
        self.activity()

        self.checked_out[test_dict['class_path']] = {
            'runner': runner,
            'class_path': test_dict['class_path'],
            'methods': set(test_dict['methods']),
            'failed_methods': {},
            'passed_methods': {},
            'start_time': time.time(),
            'timeout_time': time.time() + self.runner_timeout,
        }

        self.timeout_class(runner, test_dict['class_path'])

    def check_in_class(self, runner, class_path, timed_out=False, finished=False, early_shutdown=False):
        if not timed_out:
            self.activity()

        if 1 != len([opt for opt in (timed_out, finished, early_shutdown) if opt]):
            raise ValueError("Must set exactly one of timed_out, finished, or early_shutdown.")

        if class_path not in self.checked_out:
            raise ValueError("Class path %r not checked out." % class_path)
        if not early_shutdown and self.checked_out[class_path]['runner'] != runner:
            raise ValueError("Class path %r not checked out by runner %r." % (class_path, runner))

        d = self.checked_out.pop(class_path)

        passed_methods = list(d['passed_methods'].items())
        failed_methods = list(d['failed_methods'].items())
        tests_to_report = passed_methods[:]
        requeue_methods = []

        for method, result in failed_methods:
            if self.disable_requeueing:
                # If requeueing is disabled we'll report failed methods immediately.
                tests_to_report.append((method, result))

            else:
                if (class_path, method) in self.failed_rerun_methods:
                    # failed methods already rerun, no need to requeue.
                    tests_to_report.append((method, result))

                elif result['method']['fixture_type'] in FIXTURES_WHICH_CAN_RETURN_UNEXPECTED_RESULTS:
                    # Unexpexpected fixture failures, we'll report but no need to requeue.
                    tests_to_report.append((method, result))

                elif early_shutdown:
                    # Server is shutting down. Just report the failure, no need to requeue.
                    tests_to_report.append((method, result))

                else:
                    # Otherwise requeue the method to be run on a different builder.
                    requeue_methods.append((method, result))

        for method, result_dict in tests_to_report:
            for reporter in self.test_reporters:
                result_dict['previous_run'] = self.previous_run_results.get((class_path, method), None)
                reporter.test_start(result_dict)
                reporter.test_complete(result_dict)

        # Requeue failed tests
        requeue_dict = {
            'last_runner': runner,
            'class_path': d['class_path'],
            'methods': [],
        }

        for method, result_dict in requeue_methods:
            requeue_dict['methods'].append(method)
            self.failed_rerun_methods.add((class_path, method))
            result_dict['previous_run'] = self.previous_run_results.get((class_path, method), None)
            self.previous_run_results[(class_path, method)] = result_dict

        if requeue_dict['methods']:
            # When the client has finished running the entire TestCase,
            # it will signal us by sending back a result with method
            # name 'run'. Add this result to the list we expect to get
            # back from the client.
            requeue_dict['methods'].append('run')

        if finished:
            if len(d['methods']) != 0:
                raise ValueError(
                    "check_in_class called with finished=True but this class "
                    "(%s) still has %d methods without results." % (
                        class_path, len(d['methods'])
                    )
                )
        elif timed_out:
            # Requeue or report timed-out tests.

            for method in d['methods']:
                # Fake the results dict.
                module, _, classname = class_path.partition(' ')
                result_dict = self._fake_result(class_path, method, runner)

                if (class_path, method) not in self.timeout_rerun_methods and not self.disable_requeueing:
                    requeue_dict['methods'].append(method)
                    self.timeout_rerun_methods.add((class_path, method))
                    self.previous_run_results[(class_path, method)] = result_dict
                else:
                    for reporter in self.test_reporters:
                        reporter.test_start(result_dict)
                        reporter.test_complete(result_dict)

        if requeue_dict['methods']:
            self.pair_queue.add_test(-1, requeue_dict)

        if self.pair_queue.empty() and len(self.checked_out) == 0:
            self.shutdown()

    def _fake_result(self, class_path, method, runner):
        error_message = "The runner running this method (%s) didn't respond within %ss.\n" % (runner, self.runner_timeout)
        module, _, classname = class_path.partition(' ')

        return {
            'previous_run': self.previous_run_results.get((class_path, method), None),
            'start_time': time.time() - self.runner_timeout,
            'end_time': time.time(),
            'run_time': float(self.runner_timeout),
            'normalized_run_time': "%.2fs" % (self.runner_timeout),
            'complete': True,  # We've tried running the test.
            'success': False,
            'failure': None,
            'error': True,
            'interrupted': None,
            'exception_info': error_message,
            'exception_info_pretty': error_message,
            'exception_only': error_message,
            'runner_id': runner,
            'method': {
                'module': module,
                'class': classname,
                'name': method,
                'full_name': "%s.%s" % (class_path, method),
                'fixture_type': None,
            }
        }

    def timeout_class(self, runner, class_path):
        """Check that it's actually time to rerun this class; if not, reset the timeout. Check the class in and rerun it."""
        d = self.checked_out.get(class_path, None)

        if not d:
            return

        if time.time() < d['timeout_time']:
            # We're being called for the first time, or someone has updated
            # timeout_time since the timeout was set (e.g. results came in)
            tornado.ioloop.IOLoop.instance().add_timeout(d['timeout_time'], lambda: self.timeout_class(runner, class_path))
            return

        try:
            self.check_in_class(runner, class_path, timed_out=True)
        except ValueError:
            # If another builder has checked out the same class in the mean time, don't throw an error.
            pass

    def early_shutdown(self):
        for class_path in list(self.checked_out.keys()):
            self.check_in_class(None, class_path, early_shutdown=True)
        self.shutdown()

    def shutdown(self):
        if self.shutting_down:
            # Try not to shut down twice.
            return

        self.shutting_down = True
        self.pair_queue.finalize()
        iol = tornado.ioloop.IOLoop.instance()
        # Can't immediately call stop, otherwise the runner currently POSTing
        # its results will get a Connection Refused when it tries to ask for
        # the next test.

        # Without this check, we could end up queueing a stop() call on a
        # tornado server we spin up later, causing it to hang mysteriously.
        # XXX: accessing private for tornado2+3 compatibility
        if iol._running:
            if self.runners_outstanding:
                # Stop in 5 seconds if all the runners_outstanding don't come back by then.
                iol.add_timeout(time.time() + self.shutdown_delay_for_outstanding_runners, iol.stop)
            else:
                # Give tornado enough time to finish writing to all the clients, then shut down.
                iol.add_timeout(time.time() + self.shutdown_delay_for_connection_close, iol.stop)
        else:
            _log.error("TestRunnerServer on port %s has been asked to shutdown but its IOLoop is not running."
                       " Perhaps it died an early death due to discovery failure." % self.serve_port
                       )

# vim: set ts=4 sts=4 sw=4 et:
