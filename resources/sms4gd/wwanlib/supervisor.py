""" Supervisor: owner of the connection (connect, monitor, reconnect).

While connected it runs the periodic monitoring and moves the state between ``connected`` and ``searching``
according to the registration on the mobile network. The keep-alive and the graduated reaction come later (J6).
"""

import logging
import threading
from collections.abc import Callable
from typing import Any

from .events import ConnectionState, Registration

log = logging.getLogger(__name__)


class Supervisor:
    def __init__(self, connect: Callable[[Any], None], disconnect: Callable[[], None],
                 publish: Callable[[str, dict[str, Any]], None], isFatal: Callable[[Exception], bool],
                 baseDelay: float, maxDelay: float, maxAttempts: int,
                 monitor: Callable[[], str | None] | None = None, monitorInterval: float = 30.0):
        """ :param connect: opens and initializes a connection; receives a token that the connection must use
            to report its failures to ``reportFailure``; raises if it fails
        :param disconnect: closes everything the last ``connect`` opened (also after a failed ``connect``)
        :param publish: called with the new state and its details, once per change
        :param isFatal: True for an error that retrying cannot fix (wrong PIN...)
        :param monitor: called right after the connection, then every ``monitorInterval`` seconds while connected;
            returns the registration on the mobile network (see ``Registration``) or None if it could not be read """
        self._connect = connect
        self._disconnect = disconnect
        self._publish = publish
        self._isFatal = isFatal
        self._baseDelay = baseDelay
        self._maxDelay = maxDelay
        self._maxAttempts = maxAttempts
        self._monitor = monitor
        self._monitorInterval = monitorInterval
        self._stop = threading.Event()
        self._failure = threading.Event()
        self._reason = ''
        self._token: object | None = None
        self._lastState: tuple[str, dict[str, Any]] | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        thread = threading.Thread(target=self._run, name='wwanlib-supervisor', daemon=True)
        thread.start()
        self._thread = thread

    def requestStop(self) -> None:
        self._stop.set()
        self._failure.set()

    def join(self, timeout: float = 5.0) -> None:
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def reportFailure(self, token: object, reason: str) -> None:
        """ Called by the current connection when it is lost (port gone, modem not responding) """
        if token is self._token and not self._failure.is_set():
            self._reason = reason
            self._failure.set()

    @property
    def failed(self) -> bool:
        """ True once the current connection reported a failure (or when stopping) """
        return self._failure.is_set()

    def backoffDelay(self, attempt: int) -> float:
        return min(self._baseDelay * (2 ** (attempt - 1)), self._maxDelay)

    def _setState(self, state: str, **details: Any) -> None:
        if self._stop.is_set() or self._lastState == (state, details):
            return
        self._lastState = (state, details)
        self._publish(state, details)

    def _run(self) -> None:
        self._setState(ConnectionState.CONNECTING)
        error = self._connectOnce()
        while not self._stop.is_set():
            if error is None:
                self._setState(ConnectionState.CONNECTED)
                self._superviseConnection()
                if self._stop.is_set():
                    return
                log.error('Connection lost: %s', self._reason)
                self._closeConnection()
            if not self._reconnect(error):
                return
            error = None

    def _superviseConnection(self) -> None:
        """ Returns when the connection failed or when stopping """
        while not self._failure.is_set():
            if self._monitor is not None:
                try:
                    registration = self._monitor()
                except Exception:
                    log.exception('Error in the monitoring')
                    registration = None
                if self._failure.is_set():
                    return
                if registration == Registration.REGISTERED:
                    self._setState(ConnectionState.CONNECTED)
                elif registration in (Registration.SEARCHING, Registration.DENIED):
                    self._setState(ConnectionState.SEARCHING)
            if self._failure.wait(self._monitorInterval if self._monitor is not None else None):
                return

    def _connectOnce(self) -> Exception | None:
        token = object()
        self._token = token
        self._failure.clear()
        self._reason = ''
        try:
            self._connect(token)
            return None
        except Exception as e:
            if not self._stop.is_set():
                log.error('Connection failed: %s', e)
            # A half-opened connection would leave its thread and port open, contending with the next attempt
            self._closeConnection()
            return e

    def _closeConnection(self) -> None:
        try:
            self._disconnect()
        except Exception:
            log.exception('Error while closing the connection')

    def _reconnect(self, error: Exception | None) -> bool:
        """ :return: True once reconnected, False if given up (the final state is published) or stopped """
        for attempt in range(1, self._maxAttempts + 1):
            if error is not None and self._isFatal(error):
                break
            self._setState(ConnectionState.RECONNECTING, attempt=attempt, maxAttempts=self._maxAttempts)
            delay = self.backoffDelay(attempt)
            log.warning('Attempting reconnection %d/%d in %.0fs', attempt, self._maxAttempts, delay)
            if self._stop.wait(delay):
                return False
            error = self._connectOnce()
            if error is None:
                log.info('Reconnected after %d attempt(s)', attempt)
                return True
            if self._stop.is_set():
                return False
        if error is not None and self._isFatal(error):
            log.error('Fatal error, no further attempt: %s', error)
            self._setState(ConnectionState.DISCONNECTED, reason=str(error), fatal=True, errorType=type(error).__name__)
        else:
            log.error('Maximum number of reconnection attempts reached (%d), giving up', self._maxAttempts)
            self._setState(ConnectionState.DISCONNECTED, reason=str(error) if error else self._reason, fatal=False,
                           errorType=type(error).__name__ if error else None)
        return False
