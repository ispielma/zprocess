from __future__ import division, unicode_literals, print_function, absolute_import
import sys
import os
import threading
import subprocess
import time
import signal
import weakref
import ast
from socket import gethostbyname

import zmq

_path, _cwd = os.path.split(os.getcwd())
if _cwd == 'zprocess' and _path not in sys.path:
    # Running from within zprocess dir? Add to sys.path for testing during
    # development:
    sys.path.insert(0, _path)

import zprocess
from zprocess.security import SecureContext
from zprocess.utils import TimeoutError

PY2 = sys.version_info[0] == 2
if PY2:
    import cPickle as pickle
    str = unicode
else:
    import pickle


class HeartbeatServer(object):
    """A server which receives messages from clients and echoes them back. Each
    process has a HeartbeatServer to provide heartbeating to its subprocesses -
    there is not only one in the top process.
    """
    def __init__(self, bind_address='tcp://127.0.0.1', 
                 shared_secret=None):
        context = SecureContext.instance(shared_secret=shared_secret)
        self.sock = context.socket(zmq.REP)
        self.port = self.sock.bind_to_random_port(bind_address)
        self.mainloop_thread = threading.Thread(target=self.mainloop)
        self.mainloop_thread.daemon = True
        self.mainloop_thread.start()

    def mainloop(self):
        try:
            zmq.proxy(self.sock, self.sock)
        except zmq.ContextTerminated:
            # Shutting down:
            self.sock.close(linger=0)
            return


class HeartbeatClient(object):
    """A heartbeating thread that terminates the process if it doesn't get the
    heartbeats back within one second, unless a lock is held."""
    def __init__(self, server_port, server_host='127.0.0.1', lock=False,
                 shared_secret=None, allow_insecure=False):
        if lock:
            self.lock = threading.Lock()
        else:
            self.lock = None
        context = SecureContext.instance(shared_secret=shared_secret)
        self.sock = context.socket(zmq.REQ, allow_insecure=allow_insecure)
        self.sock.setsockopt(zmq.LINGER, 0)
        server_ip = gethostbyname(server_host)
        self.sock.connect('tcp://{}:{}'.format(server_ip, server_port))
        self.mainloop_thread = threading.Thread(target=self.mainloop)
        self.mainloop_thread.daemon = True
        self.mainloop_thread.start()

    def mainloop(self):
        try:
            pid = str(os.getpid()).encode('utf8')
            while True:
                time.sleep(1)
                self.sock.send(pid, zmq.NOBLOCK)
                if not self.sock.poll(1000):
                    break
                msg = self.sock.recv()
                if not msg == pid:
                    break
            # sys.stderr.write('Heartbeat failure\n')
            if self.lock is not None:
                with self.lock:
                    os.kill(os.getpid(), signal.SIGTERM)
            else:
                os.kill(os.getpid(), signal.SIGTERM)
        except zmq.ContextTerminated:
            # Shutting down:
            self.sock.close(linger=0)
            return


class Broker(object):
    """A broker to collect Event.post() messages from anywhere in the process tree
    and broadcast them to subscribers calling event.wait(). There is only one of
    these, at the top level process in the ProcessTree."""

    # A message subscribers can use to confirm their connection
    # (and any subscriptions) have been processed.
    WELCOME_MESSAGE = b'_zprocess_broker_hello\0'

    def __init__(self, bind_address='tcp://127.0.0.1', shared_secret=None):
        context = SecureContext.instance(shared_secret=shared_secret)
        self.frontend = context.socket(zmq.PULL)
        self.backend = context.socket(zmq.XPUB)

        self.poller = zmq.Poller()
        self.poller.register(self.frontend, zmq.POLLIN)
        self.poller.register(self.backend, zmq.POLLIN)

        self.in_port = self.frontend.bind_to_random_port(bind_address)
        self.out_port = self.backend.bind_to_random_port(bind_address)

        self.mainloop_thread = threading.Thread(target=self.mainloop)
        self.mainloop_thread.daemon = True
        self.mainloop_thread.start()

    def mainloop(self):
        while True:
            try:
                events = dict(self.poller.poll())
                if self.backend in events:
                    msg = self.backend.recv()
                    is_subscription, topic = ord(msg[0:1]), msg[1:]
                    if is_subscription and topic.startswith(self.WELCOME_MESSAGE):
                        # A new subscriber asking for a welcome message to confirm
                        # that we have received all subscriptions made prior to
                        # this request. Send the topic back (it includes a unique
                        # random number to ensure only the recipient gets it)
                        self.backend.send(topic)
                if self.frontend in events:
                    # Forward messages to subscribers:
                    self.backend.send_multipart(self.frontend.recv_multipart())
            except zmq.ContextTerminated:
                # Shutting down:
                self.frontend.close(linger=0)
                self.backend.close(linger=0)
                return


class WriteQueue(object):

    """Provides writing of python objects to the underlying zmq socket,
    with added locking. No reading is supported, once you put an object,
    you can't check what was put or whether the items have been gotten"""

    def __init__(self, sock):
        self.sock = sock
        self.lock = threading.Lock()

    def put(self, obj):
        with self.lock:
            self.sock.send_pyobj(obj, protocol=zprocess.PICKLE_PROTOCOL)


class ReadQueue(object):
    """Provides reading and writing methods to the underlying zmq socket,
    with added locking. Actually there are two sockets, one for reading,
    one for writing. The only real use case for writing is when the
    read socket is blocking, but the process at the other end has died,
    and you need to stop the thread that is blocking on the read. So
    you send it a quit signal with put()."""

    def __init__(self, sock, to_self_sock):
        self.sock = sock
        self.to_self_sock = to_self_sock
        self.socklock = threading.Lock()
        self.to_self_sock_lock = threading.Lock()

    def get(self, timeout=None):
        with self.socklock:
            if timeout is not None:
                if not self.sock.poll(timeout*1000):
                    raise TimeoutError('get() timed out')
            obj = self.sock.recv_pyobj()
        return obj

    def put(self, obj):
        with self.to_self_sock_lock:
            self.to_self_sock.send_pyobj(obj, protocol=zprocess.PICKLE_PROTOCOL)


class OutputInterceptor(object):
    """Redirect stderr or stdout to a zmq PUSH socket"""
    threadlocals_by_server = weakref.WeakValueDictionary()

    def __init__(self, port, host='127.0.0.1', streamname='stdout',
                 shared_secret=None, allow_insecure=False):
        self.port = port
        self.ip = gethostbyname(host)
        self.shared_secret = shared_secret
        self.allow_insecure = allow_insecure
        self.streamname = streamname
        self.real_stream = getattr(sys, streamname)
        self.fileno = self.real_stream.fileno
        self.context = SecureContext.instance(shared_secret=shared_secret)
        # All instances connected to the same server will share a threadlocal
        # object. This way two (or more) instances called from the same thread
        # will be using the same zmq socket, and hence, their messages will
        # arrive in order.
        if (self.ip, port) not in self.threadlocals_by_server:
            self.local = threading.local()
            self.threadlocals_by_server[(self.ip, port)] = self.local
        self.local = self.threadlocals_by_server[(self.ip, port)]

    def new_socket(self):
        # One socket per thread, so we don't have to acquire a lock to send:
        self.local.sock = self.context.socket(zmq.PUSH,
                                              allow_insecure=self.allow_insecure)
        self.local.sock.setsockopt(zmq.LINGER, 0)
        self.local.sock.connect('tcp://%s:%d' % (self.ip, self.port))

    def connect(self):
        setattr(sys, self.streamname, self)

    def disconnect(self):
        setattr(sys, self.streamname, self.real_stream)

    def write(self, s):
        if not hasattr(self.local, 'sock'):
            self.new_socket()
        if isinstance(s, str):
            s = s.encode('utf8')
        self.local.sock.send_multipart([self.streamname.encode('utf8'), s])

    def close(self):
        self.disconnect()
        self.real_stream.close()

    def flush(self):
        pass

    def isatty(self):
        return False


class Event(object):

    def __init__(self, process_tree, event_name, role='wait'):
        # Ensure we have a broker, whether it's in this process or a parent one:
        self.event_name = event_name
        # We null terminate the event name otherwise any subscriber subscribing to
        # and event *starting* with our event name will also receive it, which
        # we do not want:
        self._encoded_event_name = self.event_name.encode('utf8') + b'\0'
        self.role = role
        if not role in ['wait', 'post', 'both']:
            raise ValueError("role must be 'wait', 'post', or 'both'")
        self.can_wait = self.role in ['wait', 'both']
        self.can_post = self.role in ['post', 'both']
        context = SecureContext.instance(shared_secret=process_tree.shared_secret)
        if self.can_wait:
            self.sub = context.socket(zmq.SUB,
                                      allow_insecure=process_tree.allow_insecure)
            self.sub.set_hwm(1000)
            self.sub.setsockopt(zmq.SUBSCRIBE, self._encoded_event_name)
            self.sub.connect('tcp://127.0.0.1:%s' % process_tree.broker_out_port)
            # Request a welcome message from the broker confirming it receives this
            # subscription request. This is important so that by the time this
            # __init__ method returns, the caller can know for sure that if the
            # broker receives a message, it will definitely be forwarded to the
            # subscribers and not discarded. It is important that this come after
            # connect() and after the other setsockopt for our event subscription,
            # otherwise the two subscription requests may be sent in the opposite
            # order, preventing us from relying on the receipt of a welcome message
            # as confirmation that the other subscription was received. We use a
            # unique random number to prevent treating *other* subscribers' welcome
            # messages as our own. This is a lot of hoops to jump through when it
            # would be really nice if zeroMQ could just have a way of saying "block
            # until all subscription messages processed", which is all we're really
            # doing.
            unique_id = os.urandom(32)
            self.sub.setsockopt(zmq.SUBSCRIBE, Broker.WELCOME_MESSAGE + unique_id)
            # Allow 5 seconds to connect to the Broker:
            events = self.sub.poll(flags=zmq.POLLIN, timeout=5000)
            if not events:
                raise TimeoutError("Could not connect to event broker")
            assert self.sub.recv() == Broker.WELCOME_MESSAGE + unique_id
            # Great, we're definitely connected to the broker now, and it has
            # processed our subscription. Remove the welcome event subscription
            # and proceed:
            self.sub.setsockopt(zmq.UNSUBSCRIBE,
                                Broker.WELCOME_MESSAGE + unique_id)
            self.sublock = threading.Lock()
        if self.can_post:
            self.push = context.socket(zmq.PUSH,
                                       allow_insecure=process_tree.allow_insecure)
            self.push.connect('tcp://127.0.0.1:%s' % process_tree.broker_in_port)
            self.pushlock = threading.Lock()

    def post(self, identifier, data=None):
        if not self.can_post:
            msg = ("Instantiate Event with role='post' " +
                   "or 'both' to be able to post events")
            raise ValueError(msg)
        with self.pushlock:
            self.push.send_multipart([self._encoded_event_name,
                                    str(identifier).encode('utf8'),
                                    pickle.dumps(data,
                                        protocol=zprocess.PICKLE_PROTOCOL)])

    def wait(self, identifier, timeout=None):
        identifier = str(identifier)
        if not self.can_wait:
            msg = ("Instantiate Event with role='wait' " +
                   "or 'both' to be able to wait for events")
            raise ValueError(msg)
        # First check through events that are already in the buffer:
        while True:
            with self.sublock:
                events = self.sub.poll(0, flags=zmq.POLLIN)
                if not events:
                    break
                encoded_event_name, event_id, data = self.sub.recv_multipart()
                event_id = event_id.decode('utf8')
                data = pickle.loads(data)
                assert encoded_event_name == self._encoded_event_name
                if event_id == identifier:
                    return data
        # Since we might have to make several recv() calls before we get the
        # right identifier, we must implement our own timeout:
        start_time = time.time()
        while timeout is None or (time.time() < start_time + timeout):
            with self.sublock:
                if timeout is not None:
                    # How long left before the elapsed time is greater than
                    # timeout?
                    remaining = (start_time + timeout - time.time())
                    poll_timeout = max(0, remaining)
                    events = self.sub.poll(1000 * poll_timeout, flags=zmq.POLLIN)
                    if not events:
                        break
                encoded_event_name, event_id, data = self.sub.recv_multipart()
                event_id = event_id.decode('utf8')
                data = pickle.loads(data)
                assert encoded_event_name == self._encoded_event_name
                if event_id == identifier:
                    return data
        raise TimeoutError('No event received: timed out')


class Process(object):
    """A class providing similar functionality to multiprocessing.Process, but
    using zmq for communication and creating processes in a fresh environment
    rather than by forking (or imitation forking as in Windows). Do not override
    its methods other than run()."""

    def __init__(self, process_tree, output_redirection_port=None):
        self._redirection_port = output_redirection_port
        self.process_tree = process_tree
        self.to_child = None
        self.from_child = None
        self.child = None
        self.to_parent = None
        self.from_parent = None
        self.kill_lock = None
        

    def start(self, *args, **kwargs):
        """Call in the parent process to start a subprocess. Passes args and
        kwargs to the run() method"""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'process_class_wrapper.py')
        child_details = self.process_tree.subprocess(path,
                            output_redirection_port=self._redirection_port)
        self.to_child, self.from_child, self.child = child_details
        # Get the file that the class definition is in (not this file you're
        # reading now, rather that of the subclass):
        module_file = os.path.abspath(sys.modules[self.__module__].__file__)
        basepath, extension = os.path.splitext(module_file)
        if extension == '.pyc':
            module_file = basepath + '.py'
        if not os.path.exists(module_file):
            # Nope? How about this extension then?
            module_file = basepath + '.pyw'
        if not os.path.exists(module_file):
            # Still no? Well I can't really work out what the extension is then,
            # can I?
            msg = ("Can't find module file, what's going on, does " +
                   "it have an unusual extension?")
            raise NotImplementedError(msg)
        # Send it to the child process so it can execute it in __main__,
        # otherwise class definitions from the users __main__ module will not be
        # unpickleable. Note that though executed in __main__, the code's
        # __name__ will not be __main__, and so any main block won't execute,
        # which is good!
        self.to_child.put([self.__module__, module_file, sys.path])
        self.to_child.put(self.__class__)
        self.to_child.put([args, kwargs])
        return self.to_child, self.from_child

    def _run(self):
        """Called in the child process to set up the connection with the
        parent"""
        self.to_parent = self.process_tree.to_parent
        self.from_parent = self.process_tree.from_parent
        self.kill_lock = self.process_tree.kill_lock
        args, kwargs = self.from_parent.get()
        self.run(*args, **kwargs)

    def terminate(self):
        try:
            self.child.terminate()
            self.child.wait()
        except WindowsError if os.name == 'nt' else None:
            pass  # process is already dead

    def run(self, *args, **kwargs):
        """The method that gets called in the subprocess. To be overridden by
        subclasses"""
        pass


class ProcessTree(object):
    def __init__(self, shared_secret=None, allow_insecure=False):
        self.shared_secret = shared_secret
        self.allow_insecure = allow_insecure
        self.broker = None
        self.broker_in_port = None
        self.broker_out_port = None
        self.heartbeat_server = None
        self.heartbeat_client = None
        self.to_parent = None
        self.from_parent = None
        self.kill_lock = None

    def _check_broker(self):
        if self.broker_in_port is None:
            # We don't have a parent with a broker: it is our responsibility to
            # make a broker:
            self.broker = Broker(shared_secret=self.shared_secret)
            self.broker_in_port = self.broker.in_port
            self.broker_out_port = self.broker.out_port

    def event(self, event_name, role='wait'):
        self._check_broker()
        return Event(self, event_name, role=role)

    def subprocess(self, path, output_redirection_port=None):
        context = SecureContext.instance(shared_secret=self.shared_secret)
        to_child = context.socket(zmq.PUSH, allow_insecure=self.allow_insecure)
        from_child = context.socket(zmq.PULL, allow_insecure=self.allow_insecure)
        to_self = context.socket(zmq.PUSH, allow_insecure=self.allow_insecure)

        port_from_child = from_child.bind_to_random_port('tcp://127.0.0.1')
        to_self.connect('tcp://127.0.0.1:%s' % port_from_child)
        port_to_child = to_child.bind_to_random_port('tcp://127.0.0.1')
        self._check_broker()
        if self.heartbeat_server is None:
            # First child process, we need a heartbeat server:
            self.heartbeat_server = HeartbeatServer(
                                        shared_secret=self.shared_secret)

        #TODO: fix this:
        # If a custom process identifier has been set in zlock, ensure the child
        # inherits it:
        try:
            zlock = sys.modules['zprocess.zlock']
            zlock_process_identifier_prefix = zlock.process_identifier_prefix
        except KeyError:
            zlock_process_identifier_prefix = ''

        child = subprocess.Popen([sys.executable, '-u', path,
                                  str(port_from_child),
                                  str(port_to_child),
                                  str(self.heartbeat_server.port),
                                  repr(output_redirection_port),
                                  str(self.broker_in_port),
                                  str(self.broker_out_port),
                                  zlock_process_identifier_prefix,
                                  repr(self.shared_secret),
                                  str(self.allow_insecure)])
        # The child has 15 seconds to connect to us:
        events = from_child.poll(15000)
        if not events:
            raise RuntimeError('child process did not connect within the timeout.')
        assert from_child.recv() == b'hello'

        to_child = WriteQueue(to_child)
        from_child = ReadQueue(from_child, to_self)

        return to_child, from_child, child

    def _connect_to_parent(self, 
        lock, port_to_parent, port_from_parent, port_to_heartbeat_server,
        output_redirection_port, broker_in_port, broker_out_port,
        zlock_process_identifier_prefix):

        # If a custom process identifier has been set in zlock, ensure we
        # inherit it:
        if zlock_process_identifier_prefix:
            import zprocess.locking
            # Append '-sub' to indicate we're a subprocess, if it's not already
            # there
            if not zlock_process_identifier_prefix.endswith('sub'):
                zlock_process_identifier_prefix += 'sub'
            # Only set it if the user has not already set it to something in
            # this process:
            if not zprocess.locking.process_identifier_prefix:
                zprocess.locking.set_client_process_name(
                    zlock_process_identifier_prefix)

        context = SecureContext.instance(shared_secret=self.shared_secret)
        to_parent = context.socket(zmq.PUSH, allow_insecure=self.allow_insecure)
        from_parent = context.socket(zmq.PULL, allow_insecure=self.allow_insecure)
        to_self = context.socket(zmq.PUSH, allow_insecure=self.allow_insecure)

        port_to_self = to_self.bind_to_random_port('tcp://127.0.0.1')
        from_parent.connect('tcp://127.0.0.1:%d' % port_from_parent)
        from_parent.connect('tcp://127.0.0.1:%d' % port_to_self)
        to_parent.connect("tcp://127.0.0.1:%s" % port_to_parent)
        to_parent.send(b'hello')

        self.from_parent = ReadQueue(from_parent, to_self)
        self.to_parent = WriteQueue(to_parent)

        if output_redirection_port is not None:
            stdout = OutputInterceptor(output_redirection_port,
                                       shared_secret=self.shared_secret,
                                       allow_insecure=self.allow_insecure)
            stderr = OutputInterceptor(output_redirection_port,
                                       streamname='stderr',
                                       shared_secret=self.shared_secret,
                                       allow_insecure=self.allow_insecure)
            stdout.connect()
            stderr.connect()

        self.heartbeat_client = HeartbeatClient(port_to_heartbeat_server,
                                                shared_secret=self.shared_secret,
                                                allow_insecure=self.allow_insecure,
                                                lock=lock)

        self.broker_in_port = broker_in_port
        self.broker_out_port = broker_out_port
        self.kill_lock = self.heartbeat_client.lock

    @classmethod
    def connect_to_parent(cls, lock=False):
        port_to_parent = int(sys.argv[1])
        port_from_parent = int(sys.argv[2])
        port_to_heartbeat_server = int(sys.argv[3])
        output_redirection_port = ast.literal_eval(sys.argv[4])
        broker_in_port = int(sys.argv[5])
        broker_out_port = int(sys.argv[6])
        zlock_process_identifier_prefix = sys.argv[7]
        shared_secret = ast.literal_eval(sys.argv[8])
        allow_insecure = ast.literal_eval(sys.argv[9])

        process_tree = cls(shared_secret=shared_secret,
                           allow_insecure=allow_insecure)

        process_tree._connect_to_parent(
            lock, port_to_parent, port_from_parent, port_to_heartbeat_server,
            output_redirection_port, broker_in_port, broker_out_port,
            zlock_process_identifier_prefix)

        return process_tree





# Backwards compatability follows:

_default_process_tree = ProcessTree()

# Allow instantiating an Event without a ProcessTree as the first argument,
# insert a default ProcessTree:
_Event = Event
class Event(_Event):
    def __init__(self, *args, **kwargs):
        # Convert the keyword argument renaming:
        if 'type' in kwargs:
            kwargs['role'] = kwargs['type']
            del kwargs['type']
        if not args or not isinstance(args[0], ProcessTree):
            args = (_default_process_tree,) + args
        _Event.__init__(self, *args, **kwargs)

# Allow instantiating a Process() without a ProcessTree as the first argument,
# insert a default ProcessTree:
_Process = Process
class Process(_Process):
    def __init__(self, *args, **kwargs):
        if not args or not isinstance(args[0], ProcessTree):
            args = (_default_process_tree,) + args
        _Process.__init__(self, *args, **kwargs)

# New way is to call ProcessTree.connect_to_parent(lock) and get back a
# ProcessTree. This is the old way, returning queues and (optionally) a lock
# instead:
def setup_connection_with_parent(lock=False):
    process_tree = ProcessTree.connect_to_parent(lock)
    if lock:
        return (process_tree.to_parent,
                process_tree.from_parent,
                process_tree.kill_lock)
    else:
        return process_tree.to_parent, process_tree.from_parent

# New way is to instantiate a ProcessTree and call
# process_tree.subprocess(). Old way is:
def subprocess_with_queues(path, output_redirection_port=None):
    if output_redirection_port == 0: # This used to mean no redirection
        output_redirection_port = None
    return _default_process_tree.subprocess(path, output_redirection_port)



__all__ = ['Process', 'ProcessTree', 'setup_connection_with_parent',
           'subprocess_with_queues', 'Event']