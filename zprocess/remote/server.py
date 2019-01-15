from __future__ import unicode_literals, print_function, division
import sys
PY2 = sys.version_info.major == 2
if PY2:
    str = unicode
import os
if PY2:
    import subprocess32 as subprocess
else:
    import subprocess
import logging
import logging.handlers

if __package__ is None:
    sys.path.insert(0, os.path.abspath('../..'))

import zprocess
from zprocess import ZMQServer
from zprocess.remote import DEFAULT_PORT, PROTOCOL_VERSION

ERR_INVALID_COMMAND = 'error: invalid command'

def setup_logging(silent=False):
    if os.name == 'nt':
        logpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'zprocess-remote.log')
    else:
        logpath = '/var/log/zprocess-remote.log'

    handlers = []
    if not silent:
        try:
            handler = logging.handlers.RotatingFileHandler(
                logpath, maxBytes=50 * 1024 ** 2, backupCount=1
            )
            handlers.append(handler)
            file_handler_success = True
        except (OSError, IOError):
            file_handler_success = False
        if sys.stdout is not None and sys.stdout.isatty():
            handlers.append(logging.StreamHandler(sys.stdout))
    kwargs = dict(
        format='[%(asctime)s] %(levelname)s: %(message)s',
        level=logging.DEBUG,
        handlers=handlers,
    )
    if silent:
        del kwargs['handlers']
        kwargs['filename'] = os.devnull
    logging.basicConfig(**kwargs)
    if not silent and file_handler_success:
        msg = 'Can\'t open or do not have permission to write to log file '
        msg += logpath + '. Only terminal logging will be output.'
        logging.warning(msg)


class RemoteProcessServer(ZMQServer):
    def __init__(self, port=None, bind_address='tcp://0.0.0.0', shared_secret=None,
                 allow_insecure=True, silent=False):
        ZMQServer.__init__(self, port=port, bind_address=bind_address,
                           shared_secret=shared_secret,
                           allow_insecure=allow_insecure)
        setup_logging(silent)
        msg = 'This is zprocess-remote server, running on %s:%d'
        logging.info(msg, self.bind_address, self.port)
        # Entries should be removed from this dict if the parent calls __del__ on
        # the proxy, or if the child dies for some other reason.
        self.children = {}

    def proxy_terminate(self, pid):
        return self.children[pid].terminate()

    def proxy_kill(self, pid):
        return self.children[pid].kill()

    def proxy_wait(self, pid):
        # We only wait for 10ms - the client can implement a blocking wait by
        # calling multiple times, we don't want to be blocked here:
        try:
            self.children[pid].wait(0.01)
        except subprocess.TimeoutExpired:
            return None

    def proxy_poll(self, pid):
        return self.children[pid].poll()

    def proxy_returncode(self):
        return self.client.returncode

    def proxy___del__(self, pid):
        del self.children[pid]

    def proxy_Popen(self, cmd, *args, **kwargs):
        cmd, kwargs = args
        if kwargs.pop('prepend_sys_executable', False):
            cmd = [sys.executable] + cmd
        child = subprocess.Popen(cmd, *args, **kwargs)
        self.children[child.pid] = child
        return child.pid

    def handler(self, data):
        command, args, kwargs = data
        logging.info('%s: %s' % (self.sock.peer_ip, command))
        if hasattr(self, 'proxy_' + command):
            return getattr(self, 'proxy_' + command)(*args, **kwargs)
        elif command == 'whoami':
            # Client is requesting its IP address from our perspective
            return self.sock.peer_ip
        elif command == 'hello':
            return 'hello'
        elif command == 'protocol':
            return PROTOCOL_VERSION
        else:
            return ERR_INVALID_COMMAND