# This file is part of Jeedom.
#
# Jeedom is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Jeedom is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Jeedom. If not, see <http://www.gnu.org/licenses/>.
#

# Jeedom daemon library of the sms4g plugin. Starting from the standard library of the Jeedom plugins, it keeps what
# the daemon uses (socket, utils) and replaces jeedom_com by jeedom_publisher (see decisions.md, DEC-30).

import json
import logging
import os
import socketserver
import threading
import time
import uuid
from collections.abc import Hashable
from queue import Queue
from socketserver import StreamRequestHandler, TCPServer
from typing import Any

import requests

# ------------------------------------------------------------------------------


class jeedom_publisher():
    """ Outgoing channel to the callback of the plugin (``jeesms4g.php``): one ordered queue and one consumer thread.

    It replaces ``jeedom_com``, whose ``send_change_immediate`` starts a thread for every message (no order, no limit)
    and waits up to 120 s for each of its 3 attempts.

    - ``state(key, payload)``: a state (signal, connection...). If a state of the same key is still waiting, it is
      replaced: only the last one is sent when Jeedom is slow. The new one goes to the end of the queue, so the
      order of the sending is the order of the facts.
    - ``event(payload)``: an event (AT response, received SMS...). Never merged, never reordered. If Jeedom has been
      unreachable for so long that ``MAX_EVENTS`` are waiting, the oldest one is dropped.

    Only one request is in flight at a time, and whatever accumulated meanwhile (at most ``MAX_BATCH`` messages) leaves
    in the next one: nothing is added to the latency when the channel is free, and a burst (start of the daemon,
    reconnection) or a slow Jeedom costs a few requests instead of one per message.

    The body of a request is ``{"messages": [...]}``, also for a single message. Every message carries ``id`` (unique,
    given when it is queued, kept by the next attempts: the PHP side ignores an id it already handled), ``time`` (when
    it happened, unless the caller gives it) and ``type`` plus its own fields (both given by the caller).

    A request is tried ``retries`` times (with growing delays), then abandoned with a log: the channel never blocks
    on an unreachable Jeedom. """

    MAX_EVENTS = 200
    MAX_BATCH = 50

    def __init__(self, url: str, apikey: str, connect_timeout: float = 2.0, read_timeout: float = 10.0,
                 retries: int = 3, retry_delay: float = 1.0):
        self._url = url
        self._apikey = apikey
        self._timeout = (connect_timeout, read_timeout)
        self._retries = retries
        self._retry_delay = retry_delay
        self._session = requests.Session()
        self._condition = threading.Condition()
        self._entries: list[tuple[Hashable | None, dict[str, Any]]] = []
        self._sending = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def test(self) -> bool:
        """ Checks that Jeedom answers on the callback URL """
        try:
            response = self._session.get(self._url, params={'apikey': self._apikey}, timeout=self._timeout, verify=False)
        except requests.RequestException as e:
            logging.error('Callback result as a unknown error: %s. Please check your network configuration page', e)
            return False
        if response.status_code != requests.codes.ok:
            logging.error('Callback error: %s %s. Please check your network configuration page', response.status_code, response.reason)
            return False
        return True

    def start(self) -> None:
        thread = threading.Thread(target=self._run, name='jeedom-publisher', daemon=True)
        thread.start()
        self._thread = thread

    def state(self, key: Hashable, payload: dict[str, Any]) -> None:
        message = self._message(payload)
        with self._condition:
            self._entries = [entry for entry in self._entries if entry[0] != key]
            self._entries.append((key, message))
            self._condition.notify_all()

    def event(self, payload: dict[str, Any]) -> None:
        message = self._message(payload)
        with self._condition:
            if sum(1 for entry in self._entries if entry[0] is None) >= self.MAX_EVENTS:
                index = next(i for i, entry in enumerate(self._entries) if entry[0] is None)
                dropped = self._entries.pop(index)[1]
                logging.error('Jeedom unreachable for too long, event dropped: %s', dropped.get('type'))
            self._entries.append((None, message))
            self._condition.notify_all()

    def flush(self, timeout: float = 2.0) -> bool:
        """ Waits until everything that was queued has been handled (sent or abandoned).
        :return: False if ``timeout`` seconds were not enough """
        with self._condition:
            return self._condition.wait_for(lambda: not self._entries and not self._sending, timeout)

    def stop(self) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(2)
        self._session.close()

    @staticmethod
    def _message(payload: dict[str, Any]) -> dict[str, Any]:
        message = dict(payload)
        message['id'] = uuid.uuid4().hex
        message.setdefault('time', round(time.time(), 3))
        return message

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._entries and not self._stop.is_set():
                    self._condition.wait()
                if self._stop.is_set():
                    return
                batch = self._entries[:self.MAX_BATCH]
                del self._entries[:self.MAX_BATCH]
                self._sending = True
            try:
                self._deliver(batch)
            except Exception:
                logging.exception('Unexpected error while sending to Jeedom')
            finally:
                with self._condition:
                    self._sending = False
                    self._condition.notify_all()

    def _deliver(self, batch: list[tuple[Hashable | None, dict[str, Any]]]) -> None:
        body = {'messages': [message for _, message in batch]}
        for attempt in range(1, self._retries + 1):
            if self._post(body):
                return
            if attempt < self._retries and self._stop.wait(self._retry_delay * attempt):
                return
        events = sum(1 for key, _ in batch if key is None)
        if events:
            logging.error('Not delivered to Jeedom after %d attempts, dropped: %d event(s), %d state(s)', self._retries, events, len(batch) - events)
        else:
            logging.warning('Not delivered to Jeedom after %d attempts, dropped: %d state(s)', self._retries, len(batch))

    def _post(self, body: dict[str, Any]) -> bool:
        logging.debug('Send to jeedom : %s', body)
        try:
            response = self._session.post(self._url, params={'apikey': self._apikey}, json=body, timeout=self._timeout, verify=False)
        except requests.RequestException as e:
            logging.debug('Error on send request to jeedom "%s"', e)
            return False
        if response.status_code != requests.codes.ok:
            logging.debug('Error on send request to jeedom, return code %s', response.status_code)
            return False
        return True

# ------------------------------------------------------------------------------


class jeedom_utils():

    @staticmethod
    def convert_log_level(level='error'):
        LEVELS = {'debug': logging.DEBUG,
                  'info': logging.INFO,
                  'notice': logging.WARNING,
                  'warning': logging.WARNING,
                  'error': logging.ERROR,
                  'critical': logging.CRITICAL,
                  'none': logging.CRITICAL}
        return LEVELS.get(level, logging.CRITICAL)

    @staticmethod
    def set_log_level(level='error'):
        FORMAT = '[%(asctime)-15s][%(levelname)s] : %(message)s'
        logging.basicConfig(level=jeedom_utils.convert_log_level(level), format=FORMAT, datefmt="%Y-%m-%d %H:%M:%S")

    @staticmethod
    def write_pid(path):
        pid = str(os.getpid())
        logging.debug("Writing PID " + pid + " to " + str(path))
        with open(path, 'w') as pid_file:
            pid_file.write("%s\n" % pid)

# ------------------------------------------------------------------------------


JEEDOM_SOCKET_MESSAGE = Queue()


class jeedom_socket_handler(StreamRequestHandler):
    def handle(self):
        logging.debug("Client connected to [%s:%d]", self.client_address[0], self.client_address[1])
        lg = self.rfile.readline()
        JEEDOM_SOCKET_MESSAGE.put(lg)
        try:
            lgdecode = json.loads(lg.strip())
            # The apikey is masked by SecretMaskFilter (sms4gd.py)
            logging.debug("Message read from socket :: %s", str(json.dumps(lgdecode).encode('utf-8')))
        except Exception as error:
            logging.error("JSON Exception :: %s", error)
            logging.debug("Message read from socket (raw) :: %s", str(lg.strip()))
        logging.debug("Client disconnected from [%s:%d]", self.client_address[0], self.client_address[1])


class jeedom_socket():

    def __init__(self, address='localhost', port=55000):
        self.address = address
        self.port = port
        socketserver.TCPServer.allow_reuse_address = True

    def open(self):
        self.netAdapter = TCPServer((self.address, self.port), jeedom_socket_handler)
        if self.netAdapter:
            logging.debug("Socket interface started")
            threading.Thread(target=self.loopNetServer, daemon=True).start()
        else:
            logging.error("Cannot start socket interface")

    def loopNetServer(self):
        logging.debug("LoopNetServer Thread started")
        logging.debug("Listening on: [%s:%d]" % (self.address, self.port))
        self.netAdapter.serve_forever()
        logging.debug("LoopNetServer Thread stopped")

    def close(self):
        self.netAdapter.shutdown()

# ------------------------------------------------------------------------------
# END
# ------------------------------------------------------------------------------
