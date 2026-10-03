""" Typed events published by wwanlib and the thread that delivers them to subscribers """

import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


class ConnectionState:
    """ Connection states published by the Supervisor """

    CONNECTING = 'connecting'
    CONNECTED = 'connected'
    RECONNECTING = 'reconnecting'
    DISCONNECTED = 'disconnected'


@dataclass(frozen=True)
class StateChanged:
    """ The connection state changed. ``details`` depends on the state:
    reconnecting -> attempt, maxAttempts ; disconnected -> reason, fatal """

    state: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModemIdentified:
    """ The modem was identified during initialization (published before the ``connected`` state) """

    profile: str
    manufacturer: str
    model: str
    revision: str | None


@dataclass(frozen=True)
class UnsolicitedNotification:
    """ Raw lines of a notification sent spontaneously by the modem (+CMTI, +CDS, RING, RDY...) """

    lines: list[str]


class EventDispatcher:
    """ Delivers events to the subscribers from a dedicated thread, so that a slow callback
    (e.g. an HTTP call to Jeedom) never blocks the Reader, the Executor or the Supervisor """

    def __init__(self):
        self._callbacks: list[Callable[[Any], None]] = []
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def subscribe(self, callback: Callable[[Any], None]) -> None:
        self._callbacks.append(callback)

    def start(self) -> None:
        self._stop.clear()
        thread = threading.Thread(target=self._run, name='wwanlib-events', daemon=True)
        thread.start()
        self._thread = thread

    def post(self, event: Any) -> None:
        self._queue.put(event)

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._queue.put(None)
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            event = self._queue.get()
            if event is None:
                continue
            for callback in list(self._callbacks):
                try:
                    callback(event)
                except Exception:
                    log.exception('Exception in an event callback (event %r)', event)
