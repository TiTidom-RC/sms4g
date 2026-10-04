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
    SEARCHING = 'searching'  # connected to the modem, but not registered on the mobile network
    RECONNECTING = 'reconnecting'
    DISCONNECTED = 'disconnected'


class Registration:
    """ Registration on the mobile network, as published in ``NetworkChanged`` """

    REGISTERED = 'registered'
    SEARCHING = 'searching'
    DENIED = 'denied'
    UNKNOWN = 'unknown'


@dataclass(frozen=True)
class StateChanged:
    """ The connection state changed. ``details`` depends on the state:
    reconnecting -> attempt, maxAttempts, and retryIn (seconds) while waiting for the attempt to start ;
    disconnected -> reason, fatal, errorType (class name of the cause) """

    state: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SignalChanged:
    """ Signal quality: AT+CSQ value from 0 to 31, -1 when unknown (not connected, or reported as 99) """

    value: int


@dataclass(frozen=True)
class NetworkChanged:
    """ Registration on the mobile network (see ``Registration``) and operator name (None when not registered) """

    registration: str
    operator: str | None


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


@dataclass(frozen=True)
class SmsQueued:
    """ An SMS could not leave at once (modem not connected, network unavailable...): it stays in the queue and
    is tried again until it is sent, fails or expires. Published once per SMS. ``ref`` is the caller's own
    reference, given back untouched. """

    smsId: str
    ref: str | None
    number: str
    reason: str


@dataclass(frozen=True)
class SmsSent:
    """ Every part of the SMS was accepted by the SMS center (``references``: the TP-MR returned by the modem) """

    smsId: str
    ref: str | None
    number: str
    parts: int
    references: tuple[int | None, ...]


@dataclass(frozen=True)
class SmsFailed:
    """ Final failure: refused by the modem or the network, or interrupted (``reason`` is short and never contains
    the text). ``sentParts`` > 0 means that the first parts have been sent. """

    smsId: str
    ref: str | None
    number: str
    reason: str
    parts: int = 0
    sentParts: int = 0


@dataclass(frozen=True)
class SmsExpired:
    """ The SMS stayed in the queue longer than its lifetime; ``reason`` is why its last attempt failed """

    smsId: str
    ref: str | None
    number: str
    reason: str


@dataclass(frozen=True)
class SmsDelivery:
    """ What the network says became of an SMS that was sent, from its delivery reports (``deliveryReport`` option).
    ``status``: ``pending`` (delayed, the network tries again), ``delivered`` (every part), ``undelivered`` (a part
    failed for good), ``unknown`` (no final report in time). ``reason``: why (never the text). ``deliveredParts`` of
    ``parts`` are delivered. Only published when the state moves forward, once the SMS was announced as sent. """

    smsId: str
    ref: str | None
    number: str
    status: str
    reason: str
    parts: int
    deliveredParts: int


@dataclass(frozen=True)
class SmsReceived:
    """ An SMS was received (a long one is reassembled from its parts: ``parts``). ``number`` is the sender as the
    network gives it (+33..., a short number, or a name such as "Free"). ``sent`` is the date the SMS center put on it
    (seconds since 1970, UTC), None when unreadable. """

    number: str
    text: str
    sent: float | None
    parts: int


@dataclass(frozen=True)
class SmsIncomplete:
    """ A long SMS was given up before its last parts arrived (the text is never shown, a text with a hole is
    misleading). ``reason``: ``timeout`` (the parts did not all arrive in time) or ``overflow`` (too many incomplete
    messages at once, this was the oldest). """

    number: str
    received: int
    expected: int
    reason: str


class EventDispatcher:
    """ Delivers events to the subscribers from a dedicated thread, so that a slow callback
    (e.g. an HTTP call to Jeedom) never blocks the Reader, the Executor or the Supervisor """

    def __init__(self):
        self._callbacks: list[Callable[[Any], None]] = []
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None

    def subscribe(self, callback: Callable[[Any], None]) -> None:
        self._callbacks.append(callback)

    def start(self) -> None:
        thread = threading.Thread(target=self._run, name='wwanlib-events', daemon=True)
        thread.start()
        self._thread = thread

    def post(self, event: Any) -> None:
        self._queue.put(event)

    def stop(self, timeout: float = 2.0) -> None:
        """ Delivers the events posted before, then stops (the last ones, e.g. the final states of the SMS, must
        not be lost) """
        self._queue.put(None)
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout)

    def _run(self) -> None:
        while True:
            event = self._queue.get()
            if event is None:
                return
            for callback in list(self._callbacks):
                try:
                    callback(event)
                except Exception:
                    log.exception('Exception in an event callback (event %r)', event)
