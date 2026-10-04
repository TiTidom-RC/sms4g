""" Supervisor: owner of the connection (connect, monitor, reconnect).

While connected it runs the periodic monitoring and moves the state between ``connected`` and ``searching``
according to the registration on the mobile network. When the connection is lost it reconnects with a growing
delay, cut short as soon as the port that disappeared comes back (the modem restarted, the key was plugged again).
"""

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from .events import ConnectionState, Registration

log = logging.getLogger(__name__)

PORT_SETTLE = 1.0  # seconds left to the system to finish setting up a port that just came back
QUICK_RETRY_DELAY = 2.0  # delay after an attempt made because the port came back and that failed (device still busy)
MAX_QUICK_ATTEMPTS = 5  # such attempts per reconnection: they do not use the attempts budget, but cannot last forever


class Supervisor:
    def __init__(self, connect: Callable[[Any], None], disconnect: Callable[[], None],
                 publish: Callable[[str, dict[str, Any]], None], isFatal: Callable[[Exception], bool],
                 baseDelay: float, maxDelay: float, maxAttempts: int,
                 monitor: Callable[[], str | None] | None = None, monitorInterval: float = 30.0,
                 portPresent: Callable[[], bool] | None = None, portPollInterval: float = 1.0,
                 portSettle: float = PORT_SETTLE):
        """ :param connect: opens and initializes a connection; receives a token that the connection must use
            to report its failures to ``reportFailure``; raises if it fails
        :param disconnect: closes everything the last ``connect`` opened (also after a failed ``connect``)
        :param publish: called with the new state and its details, once per change
        :param isFatal: True for an error that retrying cannot fix (wrong PIN...)
        :param monitor: called right after the connection, then every ``monitorInterval`` seconds while connected;
            returns the registration on the mobile network (see ``Registration``) or None if it could not be read
        :param portPresent: tells whether the port exists (None: unknown, the delays are always waited in full): when
            the port was missing and comes back during a delay, the delay ends and the next attempt is made at once """
        self._connect = connect
        self._disconnect = disconnect
        self._publish = publish
        self._isFatal = isFatal
        self._baseDelay = baseDelay
        self._maxDelay = maxDelay
        self._maxAttempts = maxAttempts
        self._monitor = monitor
        self._monitorInterval = monitorInterval
        self._portPresent = portPresent
        self._portPollInterval = portPollInterval
        self._portSettle = portSettle
        self._restartLock = threading.Lock()
        self._restartReason: str | None = None  # a restart of the modem was asked for and is not over
        self._restartTimer: threading.Timer | None = None
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
        self._endRestart()

    def join(self, timeout: float = 5.0) -> None:
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def reportFailure(self, token: object, reason: str) -> None:
        """ Called by the current connection when it is lost (port gone, modem not responding) """
        if token is self._token and not self._failure.is_set():
            self._reason = reason
            self._failure.set()

    def beginRestart(self, reason: str, timeout: float) -> bool:
        """ The modem is going to restart: the loss of the connection is expected, the state is ``restarting`` from now
        on (also while reconnecting) and the monitoring pauses. If the connection is still there after ``timeout``
        seconds, the modem did not restart: everything goes back to normal.
        :return: False when a restart is already in progress (or when stopping) """
        with self._restartLock:
            if self._restartReason is not None or self._stop.is_set():
                return False
            self._restartReason = reason
            timer = threading.Timer(timeout, self._restartExpired)
            timer.daemon = True
            self._restartTimer = timer
            timer.start()
        self._setState(ConnectionState.RESTARTING, reason=reason)
        return True

    def cancelRestart(self) -> None:
        """ The modem refused to restart """
        self._endRestart()
        if not self._failure.is_set():
            self._setState(ConnectionState.CONNECTED)

    def _endRestart(self) -> None:
        with self._restartLock:
            self._restartReason = None
            timer, self._restartTimer = self._restartTimer, None
        if timer is not None:
            timer.cancel()

    def _restartExpired(self) -> None:
        with self._restartLock:
            if self._restartTimer is not threading.current_thread():
                return
        if self._failure.is_set():
            return  # the connection was lost: the reconnection ends the restart
        log.warning('The modem did not restart')
        self._endRestart()
        self._setState(ConnectionState.CONNECTED)

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
                self._endRestart()
                self._setState(ConnectionState.CONNECTED)
                self._superviseConnection()
                if self._stop.is_set():
                    return
                if self._restartReason is None:
                    log.error('Connection lost: %s', self._reason)
                else:
                    log.info('Connection closed by the restart of the modem (%s)', self._reason)
                self._closeConnection()
            if not self._reconnect(error):
                return
            error = None

    def _superviseConnection(self) -> None:
        """ Returns when the connection failed or when stopping """
        while not self._failure.is_set():
            if self._monitor is not None and self._restartReason is None:
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

    def _wait(self, delay: float, watchPort: bool = True) -> bool | None:
        """ Waits before an attempt. :return: None when stopping, True if the port that was missing came back (the
        delay is cut short), False once the delay elapsed """
        present = self._portPresent
        if present is None or not watchPort:
            return None if self._stop.wait(delay) else False
        missing = not present()
        deadline = time.monotonic() + delay
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self._stop.wait(min(self._portPollInterval, remaining)):
                return None
            if not present():
                missing = True
            elif missing:
                log.info('The port is back')
                return None if self._stop.wait(self._portSettle) else True

    def _announce(self, attempt: int, retryIn: float | None = None) -> None:
        reason = self._restartReason
        if reason is not None:
            self._setState(ConnectionState.RESTARTING, reason=reason)
        elif retryIn is None:
            self._setState(ConnectionState.RECONNECTING, attempt=attempt, maxAttempts=self._maxAttempts)
        else:
            self._setState(ConnectionState.RECONNECTING, attempt=attempt, maxAttempts=self._maxAttempts, retryIn=retryIn)

    def _reconnect(self, error: Exception | None) -> bool:
        """ :return: True once reconnected, False if given up (the final state is published) or stopped """
        attempt = 1
        quick = 0  # attempts made because the port came back
        afterQuick = False  # the last attempt was one of them (or its retry), and it failed
        while attempt <= self._maxAttempts:
            if error is not None and self._isFatal(error):
                break
            watching = quick < MAX_QUICK_ATTEMPTS
            delay = self.backoffDelay(attempt)
            if afterQuick and watching:
                delay = min(delay, QUICK_RETRY_DELAY)
            # Two states per attempt: the announcement (retryIn), then the attempt itself once the delay elapsed
            self._announce(attempt, delay)
            log.warning('Reconnection attempt %d/%d in %.0fs', attempt, self._maxAttempts, delay)
            returned = self._wait(delay, watching)
            if returned is None:
                return False
            self._announce(attempt)
            log.info('Reconnection attempt %d/%d', attempt, self._maxAttempts)
            error = self._connectOnce()
            if error is None:
                log.info('Reconnected after %d attempt(s)', attempt)
                return True
            if self._stop.is_set():
                return False
            if watching and (returned or afterQuick):
                quick += 1
                afterQuick = True
            else:
                afterQuick = False
                attempt += 1
        self._endRestart()
        if error is not None and self._isFatal(error):
            log.error('Fatal error, no further attempt: %s', error)
            self._setState(ConnectionState.DISCONNECTED, reason=str(error), fatal=True, errorType=type(error).__name__)
        else:
            log.error('Maximum number of reconnection attempts reached (%d), giving up', self._maxAttempts)
            self._setState(ConnectionState.DISCONNECTED, reason=str(error) if error else self._reason, fatal=False,
                           errorType=type(error).__name__ if error else None)
        return False
