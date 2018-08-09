from __future__ import print_function, division, absolute_import, unicode_literals
from bisect import insort
import threading
from collections import defaultdict
import enum

try:
    from time import monotonic
except ImportError:
    from time import time as monotonic

import zmq

MAX_RESPONSE_TIME = 1  # second
MAX_ABSENT_TIME = 1  # second

INVALID_NUMBERS = {float('nan'), float('inf'), float('-inf')}

ERR_NOT_HELD = b'error: lock not held'
ERR_INVALID_REENTRY = b'error: lock already held read-only, cannot re-enter as writer'
ERR_CONCURRENT = b'error: multiple concurrent requests with same key and client_id'
ERR_INVALID_COMMAND = b'error: invalid command'
ERR_WRONG_NUM_ARGS = b'error: wrong number of arguments'
ERR_TIMEOUT_INVALID = b'error: timeout not a valid number'
ERR_READ_ONLY_WRONG = b"error: argument 4 if present can only 'read_only'"


class AlreadyWaiting(ValueError):
    pass


class InvalidReentry(ValueError):
    pass


class NotHeld(ValueError):
    pass


class Lock(object):
    """A reentrant readers-writer lock. Implementation gives priority to writers.
    Readers may re-enter as a reader but not as a writer."""

    def __init__(self, key, server):
        self.key = key
        self.server = server
        self.waiting_readers = set()
        self.waiting_writers = set()
        self.readers = defaultdict(int)  # {client_id: reentrancy_level}
        self.writer = None
        self.writer_reentrancy_level = 0
        self.invalid = False

    @classmethod
    def instance(cls, key, server):
        """Get an existing instance of the lock, if any, from server.active_locks,
        otherwise make a new one."""
        if key in server.active_locks:
            return server.active_locks[key]
        else:
            inst = cls(key, server)
            server.active_locks[key] = inst
            return inst

    def _invalid(self):
        msg = "Cannot re-use Lock instance after all clients released - "
        msg += "call Lock.instance() for a new instance."
        raise RuntimeError(msg)

    def _check_cleanup(self):
        """Delete the instance from the ZMQServer's dict of active locks if there are no
        readers, writers or waiters"""
        if not any((self.readers, self.waiting_readers, self.waiting_writers)):
            if self.writer is None:
                self.invalid = True
                del self.server.active_locks[self.key]

    def acquire(self, client_id, read_only):
        """Attempt to acquire or re-enter the lock for the given client. Return True on
        success, or False upon failure. In the latter case, the client will be added to
        an internal list of clients that are waiting for the lock. Raises AlreadyWaiting
        if the client has previously requested the lock unsuccessfully and has not since
        called give_up(), and raises InvalidReentry if a client that has acquired the
        lock as read-only attempts to re-enter it as read-write."""
        if self.invalid:
            self._invalid()
        try:
            if client_id in self.waiting_readers or client_id in self.waiting_writers:
                raise AlreadyWaiting('Client already waiting')
            if read_only:
                if self.writer is not None:
                    # Reader must wait if there is a writer:
                    self.waiting_readers.add(client_id)
                    return False
                if self.waiting_writers:
                    # Reader can re-enter the lock if there are waiting writers, but
                    # must wait to acquire it initially:
                    if client_id in self.readers:
                        self.readers[client_id] += 1
                        return True
                    else:
                        self.waiting_readers.add(client_id)
                        return False
                else:
                    # Acquire or re-enter the lock:
                    self.readers[client_id] += 1
                    return True
            else:
                if client_id in self.readers:
                    msg = 'Cannot re-enter read-only lock as a writer'
                    raise InvalidReentry(msg)
                # The writer can acquire or re-enter the lock if there are no other
                # readers or writers:
                if self.writer in (None, client_id) and not self.readers:
                    self.writer = client_id
                    self.writer_reentrancy_level += 1
                    return True
                else:
                    self.waiting_writers.add(client_id)
                    return False
        finally:
            self._check_cleanup()

    def release(self, client_id, fully=False):
        """Release the lock held by the given client, or decrease its re-entrancy level
        by one. If this makes the lock available for other waiting clients, acquire the
        lock for those clients. Return a set of client ids that acquired the lock in
        this way. Raises NotHeld if the lock was not held by the client. If fully is
        True than the lock is completely released regardless of reentrancy level."""
        if self.invalid:
            self._invalid()
        try:
            if client_id in self.readers:
                self.readers[client_id] -= 1
                if self.readers[client_id] == 0 or fully:
                    del self.readers[client_id]
                # Is the lock now available for a writer?
                if self.waiting_writers and not self.readers:
                    self.writer = self.waiting_writers.pop()
                    self.writer_reentrancy_level = 1
                    return {self.writer}
            elif client_id == self.writer:
                self.writer_reentrancy_level -= 1
                if self.writer_reentrancy_level == 0 or fully:
                    self.writer = None
                    self.writer_reentrancy_level = 0
                # Is there a waiting writer to give the lock to?
                if self.waiting_writers:
                    self.writer = self.waiting_writers.pop()
                    self.writer_reentrancy_level = 1
                    return {self.writer}
                # Are there waiting readers to give the lock to?
                if self.waiting_readers:
                    for reader_client_id in self.waiting_readers:
                        self.readers[reader_client_id] += 1
                    acquired = self.waiting_readers
                    self.waiting_readers = set()
                    return acquired
            else:
                raise NotHeld('Lock not held')
            return set()
        finally:
            self._check_cleanup()

    def give_up(self, client_id):
        """Remove the client from the list of waiting clients"""
        if self.invalid:
            self._invalid()
        if client_id in self.waiting_readers:
            self.waiting_readers.remove(client_id)
        elif client_id in self.waiting_writers:
            self.waiting_writers.remove(client_id)
        self._check_cleanup()


class rs(enum.IntEnum):
    """enum for the state of a lock request"""

    # We haven't done anything with the request yet:
    INITIAL = 0
    # The client has asked for a lock and is waiting for a response:
    PRESENT_WAITING = 1
    # The client was told to retry, but hasn't yet done so:
    ABSENT_WAITING = 2
    # The client was told to retry, hasn't yet done so, but has been granted the lock in
    # the meantime:
    ABSENT_HELD = 3
    # The client has the lock and knows it:
    HELD = 4


class LockRequest(object):
    """Object representing an active lock request. Functionally similar to a
    coroutine"""

    def __init__(self, key, client_id, server):
        self.key = key
        self.client_id = client_id
        self.routing_id = None
        self.timeout = None
        self.read_only = None
        self.server = server
        self.state = rs.INITIAL
        self.advise_retry_task = None
        self.give_up_task = None
        self.timeout_task = None

    @classmethod
    def instance(cls, key, client_id, server):
        """Get an existing instance of the request, if any, from server.active_requests,
        otherwise make a new one."""
        if (key, client_id) in server.active_requests:
            return server.active_requests[key, client_id]
        else:
            inst = cls(key, client_id, server)
            server.active_requests[key, client_id] = inst
            return inst

    def on_triggered_acquisition(self):
        """The lock has been acquired for this client in response to being released by
        one or more other clients"""
        if self.state is rs.PRESENT_WAITING:
            self.server.send(self.routing_id, b'ok')
            self.schedule_timeout_release(self.timeout)
            self.cancel_advise_retry()
            self.state = rs.HELD
        elif self.state is rs.ABSENT_WAITING:
            self.cancel_give_up()
            self.schedule_timeout_release(MAX_ABSENT_TIME)
            self.state = rs.ABSENT_HELD
        else:
            raise ValueError(self.state)  # pragma: no cover

    def _initial_acquisition(self, routing_id, timeout, read_only):
        self.routing_id = routing_id
        self.timeout = timeout
        self.read_only = read_only
        lock = Lock.instance(self.key, self.server)
        if lock.acquire(self.client_id, read_only):
            self.server.send(routing_id, b'ok')
            self.schedule_timeout_release(timeout)
            self.state = rs.HELD
        else:
            self.schedule_advise_retry()
            self.state = rs.PRESENT_WAITING

    def release(self, fully=False):
        """Release the lock for the client, and process any triggered acquisitions. If
        fully is True, the lock will be completely released, regardless of the current
        reentrancy level."""
        lock = Lock.instance(self.key, self.server)
        acquirers = lock.release(self.client_id, fully=fully)
        for client_id in acquirers:
            other_request = LockRequest.instance(self.key, client_id, self.server)
            other_request.on_triggered_acquisition()
        self._cleanup()

    def acquire_request(self, routing_id, timeout, read_only):
        """A client has requested to acquire the lock"""
        if self.state is rs.INITIAL:
            # First attempt to acquire the lock:
            self._initial_acquisition(routing_id, timeout, read_only)
        elif self.state is rs.HELD:
            # A re-entry of an already held lock:
            lock = Lock.instance(self.key, self.server)
            try:
                assert lock.acquire(self.client_id, read_only)
                self.server.send(routing_id, b'ok')
                # Extend the timeout if necessary:
                if monotonic() + timeout > self.timeout_task.due_at:
                    self.cancel_timeout_release()
                    self.schedule_timeout_release(timeout)
            except InvalidReentry:
                self.server.send(routing_id, ERR_INVALID_REENTRY)
        elif self.state is rs.ABSENT_HELD:
            # A retry attempt, and the lock was acquired whilst the client was absent.
            self.server.send(routing_id, b'ok')
            self.cancel_timeout_release()
            self.schedule_timeout_release(timeout)
            self.state = rs.HELD
        elif self.state is rs.ABSENT_WAITING:
            # A retry attempt, but the lock is still not free:
            self.cancel_give_up()
            if read_only != self.read_only:
                # Client has changed their mind about whether they want a read_only
                # lock. Give up and start again:
                self.give_up(cleanup=False)
                self._initial_acquisition(routing_id, timeout, read_only)
            else:
                self.timeout = timeout
                self.routing_id = routing_id
                self.schedule_advise_retry()
                self.state = rs.PRESENT_WAITING
        elif self.state is rs.PRESENT_WAITING:
            # Client not allowed to make two requests without waiting for a response:
            self.server.send(routing_id, ERR_CONCURRENT)
        else:
            raise ValueError(self.state)  # pragma: no cover

    def release_request(self, routing_id):
        if self.state is rs.HELD:
            self.server.send(routing_id, b'ok')
            self.release()
            self.cancel_timeout_release()
        elif self.state is rs.ABSENT_HELD:
            # A lie, but the client didn't follow protocol, so no lock for you:
            self.server.send(routing_id, ERR_NOT_HELD)
            self.release()
            self.cancel_timeout_release()
        elif self.state is rs.PRESENT_WAITING:
            # Client not allowed to make two requests without waiting for a response:
            self.server.send(routing_id, ERR_CONCURRENT)
        elif self.state in (rs.INITIAL, rs.ABSENT_WAITING):
            self.server.send(routing_id, ERR_NOT_HELD)
        else:
            raise ValueError(self.state)  # pragma: no cover

    def schedule_advise_retry(self):
        self.advise_retry_task = Task(MAX_RESPONSE_TIME, self.advise_retry)
        self.server.tasks.add(self.advise_retry_task)

    def cancel_advise_retry(self):
        self.server.tasks.cancel(self.advise_retry_task)

    def advise_retry(self):
        """Tell the client to retry acquiring the lock"""
        self.server.send(self.routing_id, b'retry')
        self.schedule_give_up()
        self.state = rs.ABSENT_WAITING

    def schedule_give_up(self):
        self.give_up_task = Task(MAX_ABSENT_TIME, self.give_up)
        self.server.tasks.add(self.give_up_task)

    def cancel_give_up(self):
        self.server.tasks.cancel(self.give_up_task)

    def give_up(self, cleanup=True):
        """Stop trying to acquire the lock"""
        lock = Lock.instance(self.key, self.server)
        lock.give_up(self.client_id)
        if cleanup:
            self._cleanup()

    def schedule_timeout_release(self, timeout):
        self.timeout_task = Task(timeout, self.release, fully=True)
        self.server.tasks.add(self.timeout_task)

    def cancel_timeout_release(self):
        self.server.tasks.cancel(self.timeout_task)

    def _cleanup(self):
        del self.server.active_requests[self.key, self.client_id]


class Task(object):
    def __init__(self, due_in, func, *args, **kwargs):
        """Wrapper for a function call to be executed after a specified time interval.
        due_in is how long in the future, in seconds, the function should be called,
        func is the function to call. All subsequent arguments and keyword arguments
        will be passed to the function."""
        self.due_at = monotonic() + due_in
        self.func = func
        self.args = args
        self.kwargs = kwargs
        self.called = False

    def due_in(self):
        """The time interval in seconds until the task is due"""
        return self.due_at - monotonic()

    def __call__(self):
        if self.called:
            raise RuntimeError('Task has already been called')
        self.called = True
        return self.func(*self.args, **self.kwargs)

    def __gt__(self, other):
        # Tasks due sooner are 'greater than' tasks due later. This is necessary for
        # insort() and pop() as used with TaskQueue.
        return self.due_at < other.due_at


class TaskQueue(list):
    """A list of pending tasks due at certain times. Tasks are stored with the soonest
    due at the end of the list, to be removed with pop()"""

    def add(self, task):
        """Insert the task into the queue, maintaining sort order"""
        insort(self, task)

    def next(self):
        """Return the next due task, without removing it from the queue"""
        return self[-1]

    def cancel(self, task):
        self.remove(task)


class ZMQLockServer(object):
    def __init__(self, port=None, bind_address='tcp://0.0.0.0'):
        self.port = port
        self._initial_port = port
        self.bind_address = bind_address
        self.context = None
        self.router = None
        self.tasks = TaskQueue()
        self.active_locks = {}

        # Lock-acquiring clients we haven't replied to yet:
        self.active_requests = {}

        self.run_thread = None
        self.stopping = False
        self.started = threading.Event()
        self.running = False

    def run(self):
        self.context = zmq.Context.instance()
        self.router = self.context.socket(zmq.ROUTER)
        poller = zmq.Poller()
        poller.register(self.router, zmq.POLLIN)
        if self.port is not None:
            self.router.bind('%s:%d' % (self.bind_address, self.port))
        else:
            self.port = self.router.bind_to_random_port(self.bind_address)
        print('This is zlock server, running on %s:%d' % (self.bind_address, self.port))
        self.running = True
        self.started.set()
        while True:
            # Wait until we receive a request or a task is due:
            if self.tasks:
                timeout = max(0, 1000 * self.tasks.next().due_in())
            else:
                timeout = None
            events = poller.poll(timeout)
            if events:
                # A request was received:
                request = self.router.recv_multipart()
                # print('received:', request)
                if len(request) < 3 or request[1] != b'':
                    # Not well formed as [routing_id, '', command, ...]
                    continue  # pragma: no cover
                routing_id, command, args = request[0], request[2], request[3:]
                if command == b'hello':
                    self.send(routing_id, b'hello')
                elif command == b'acquire':
                    self.acquire_request(routing_id, args)
                elif command == b'release':
                    self.release_request(routing_id, args)
                elif command == b'stop' and self.stopping:
                    self.send(routing_id, b'ok')
                    break
                else:
                    self.send(routing_id, ERR_INVALID_COMMAND)
            else:
                # A task is due:
                task = self.tasks.pop()
                task()
        self.router.close()
        self.router = None
        self.context = None
        self.port = self._initial_port
        self.running = False
        self.started.clear()

    def run_in_thread(self):
        """Run the main loop in a separate thread, returning immediately"""
        self.run_thread = threading.Thread(target=self.run)
        self.run_thread.daemon = True
        self.run_thread.start()
        if not self.started.wait(timeout=2):
            raise RuntimeError('Server failed to start')  # pragma: no cover

    def stop(self):
        if not self.running:
            raise RuntimeError('Not running')
        self.stopping = True
        sock = self.context.socket(zmq.REQ)
        sock.connect('tcp://127.0.0.1:%d' % self.port)
        sock.send(b'stop')
        assert sock.recv() == b'ok'
        sock.close()
        if self.run_thread is not None:
            self.run_thread.join()
        self.stopping = False

    def send(self, routing_id, message):
        # print('sending:', [routing_id, b'', message])
        self.router.send_multipart([routing_id, b'', message])

    def acquire_request(self, routing_id, args):
        if not 3 <= len(args) <= 4:
            self.send(routing_id, ERR_WRONG_NUM_ARGS)
            return
        key, client_id, timeout = args[:3]
        try:
            timeout = float(timeout)
        except ValueError:
            self.send(routing_id, ERR_TIMEOUT_INVALID)
            return
        if timeout in INVALID_NUMBERS:
            self.send(routing_id, ERR_TIMEOUT_INVALID)
            return
        if len(args) == 4:
            if args[3] != b'read_only':
                self.send(routing_id, ERR_READ_ONLY_WRONG)
                return
            read_only = True
        else:
            read_only = False
        request = LockRequest.instance(key, client_id, self)
        request.acquire_request(routing_id, timeout, read_only)

    def release_request(self, routing_id, args):
        if not len(args) == 2:
            self.send(routing_id, ERR_WRONG_NUM_ARGS)
            return
        key, client_id = args
        request = LockRequest.instance(key, client_id, self)
        request.release_request(routing_id)


if __name__ == '__main__':
    port = 7339
    server = ZMQLockServer(port)
    server.run()
