""" Outbox: the queue of the SMS to send, and the thread that sends them (DEC-34).

One thread (``sms-sender``) takes the SMS **one at a time, in order of arrival**. An SMS that cannot leave yet
(modem not connected, network unavailable...) waits for its next attempt without blocking the ones behind it.
Everything is in memory: the SMS still waiting are lost when the daemon restarts (a persistence on disk could
send an SMS twice).

Lifetime: an SMS older than ``ttl`` seconds is dropped and reported as expired. Next attempts: after 10, 20, 40,
80 and 160 s, then every 5 minutes, until it leaves, fails for good or expires. ``retryNow()`` brings every attempt
forward (the modem is connected again). The queue holds ``maxSize`` SMS at most: a new one is then refused, the
oldest is never dropped.
"""

import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .events import SmsExpired, SmsFailed, SmsQueued, SmsSent
from .exceptions import NotConnectedError, SmsQueueFullError
from .sms import SendOutcome, maskNumber, normalizeNumber

log = logging.getLogger(__name__)

RETRY_DELAYS = (10.0, 20.0, 40.0, 80.0, 160.0)
STEADY_DELAY = 300.0


@dataclass
class _Entry:
    smsId: str
    number: str
    text: str
    ref: str | None
    maxPartsPerGroup: int
    expiresAt: float
    nextAttempt: float
    attempts: int = 0
    reason: str = ''  # why the last attempt did not leave
    queued: bool = False  # SmsQueued already published
    inFlight: bool = False
    kicked: bool = False  # retryNow() came while it was being sent

    @property
    def shown(self) -> str:
        return maskNumber(normalizeNumber(self.number) or '')


class Outbox:
    def __init__(self, send: Callable[[str, str, int], SendOutcome], publish: Callable[[Any], None],
                 ttl: float = 3600.0, maxSize: int = 50, delays: tuple[float, ...] = RETRY_DELAYS,
                 steadyDelay: float = STEADY_DELAY, stopEvent: threading.Event | None = None):
        """ :param send: sends one message and blocks until it is over (``SmsSender.send``)
        :param publish: receives the events (``SmsQueued``, ``SmsSent``, ``SmsFailed``, ``SmsExpired``)
        :param stopEvent: set when the library stops (also ends the pauses between the parts of a message) """
        self._send = send
        self._publish = publish
        self._ttl = ttl
        self._maxSize = maxSize
        self._delays = delays
        self._steadyDelay = steadyDelay
        self._stopEvent = stopEvent or threading.Event()
        self._cond = threading.Condition()
        self._entries: list[_Entry] = []
        self._stopped = False
        self._abandon: str | None = None
        self._thread: threading.Thread | None = None

    def __len__(self) -> int:
        with self._cond:
            return len(self._entries)

    # ---- public API -------------------------------------------------------------------------------

    def start(self) -> None:
        with self._cond:
            self._stopped = False
            self._abandon = None
        self._stopEvent.clear()
        thread = threading.Thread(target=self._run, name='sms-sender', daemon=True)
        thread.start()
        self._thread = thread

    def submit(self, number: str, text: str, ref: str | None = None, maxPartsPerGroup: int = 0) -> str:
        """ Puts an SMS in the queue, it never blocks.
        :return: the identifier of the SMS, given back in its events
        :raise SmsQueueFullError: the queue is full
        :raise NotConnectedError: stopping, or the connection was given up """
        with self._cond:
            if self._stopped or self._abandon is not None:
                raise NotConnectedError('Modem stopped' if self._stopped else self._abandon)
            if len(self._entries) >= self._maxSize:
                raise SmsQueueFullError(f'The queue of SMS is full ({self._maxSize})')
            now = time.monotonic()
            entry = _Entry(secrets.token_hex(8), number, text, ref, maxPartsPerGroup, now + self._ttl, now)
            self._entries.append(entry)
            self._cond.notify_all()
        return entry.smsId

    def retryNow(self) -> None:
        """ The modem is connected again: every SMS that waits is tried at once """
        with self._cond:
            now = time.monotonic()
            for entry in self._entries:
                if entry.inFlight:
                    entry.kicked = True
                else:
                    entry.nextAttempt = now
            self._cond.notify_all()

    def failAll(self, reason: str) -> None:
        """ The connection is given up: the SMS that wait fail at once, and a new one is refused """
        events: list[Any] = []
        with self._cond:
            self._abandon = reason
            for entry in [entry for entry in self._entries if not entry.inFlight]:
                self._entries.remove(entry)
                events.append(self._failed(entry, reason))
            self._cond.notify_all()
        self._emit(events)

    def stop(self, timeout: float = 5.0) -> None:
        """ Stops the thread. The SMS still in the queue fail (``daemon stopped``): Jeedom does not wait for them. """
        with self._cond:
            self._stopped = True
            self._cond.notify_all()
        self._stopEvent.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        events: list[Any] = []
        with self._cond:
            for entry in self._entries:
                events.append(self._failed(entry, 'daemon stopped'))
            self._entries.clear()
        self._emit(events)

    # ---- thread -----------------------------------------------------------------------------------

    def _run(self) -> None:
        while True:
            entry = self._take()
            if entry is None:
                return
            try:
                outcome = self._send(entry.number, entry.text, entry.maxPartsPerGroup)
            except Exception:
                log.exception('Unexpected error while sending an SMS')
                outcome = SendOutcome('failed', 'internal error')
            self._finish(entry, outcome)

    def _take(self) -> _Entry | None:
        """ Waits for the next SMS to send (None when stopping). Expired SMS are reported on the way. """
        while True:
            events: list[Any] = []
            with self._cond:
                if self._stopped:
                    return None
                now = time.monotonic()
                for entry in [entry for entry in self._entries if not entry.inFlight and entry.expiresAt <= now]:
                    self._entries.remove(entry)
                    events.append(self._expired(entry))
                due = next((entry for entry in self._entries if not entry.inFlight and entry.nextAttempt <= now), None)
                if due is not None:
                    due.inFlight = True
                elif not events:
                    self._cond.wait(self._sleep(now))
                    continue
            self._emit(events)
            if due is not None:
                return due

    def _sleep(self, now: float) -> float | None:
        waiting = [min(entry.nextAttempt, entry.expiresAt) for entry in self._entries if not entry.inFlight]
        return max(0.0, min(waiting) - now) if waiting else None

    def _finish(self, entry: _Entry, outcome: SendOutcome) -> None:
        events: list[Any] = []
        with self._cond:
            entry.inFlight = False
            if entry not in self._entries:
                return  # stop() already reported it
            if outcome.status == 'retry' and (self._stopped or self._abandon is not None):
                outcome = SendOutcome('failed', 'daemon stopped' if self._stopped else str(self._abandon))
            if outcome.status == 'sent':
                self._entries.remove(entry)
                log.info('SMS to %s sent (%d part(s))', entry.shown, outcome.parts)
                events.append(SmsSent(entry.smsId, entry.ref, entry.number, outcome.parts, tuple(outcome.references)))
            elif outcome.status == 'failed':
                self._entries.remove(entry)
                events.append(SmsFailed(entry.smsId, entry.ref, entry.number, outcome.reason, outcome.parts, outcome.sentParts))
            else:
                self._schedule(entry, outcome.reason, events)
            self._cond.notify_all()
        self._emit(events)

    def _schedule(self, entry: _Entry, reason: str, events: list[Any]) -> None:
        now = time.monotonic()
        entry.attempts += 1
        entry.reason = reason
        if now >= entry.expiresAt:
            self._entries.remove(entry)
            events.append(self._expired(entry))
            return
        if entry.kicked:
            entry.kicked = False
            delay = 0.0
        else:
            delay = self._delays[entry.attempts - 1] if entry.attempts <= len(self._delays) else self._steadyDelay
        # An attempt that would fall after the expiry never happens: the SMS expires at its time
        entry.nextAttempt = min(now + delay, entry.expiresAt)
        if not entry.queued:
            entry.queued = True
            events.append(SmsQueued(entry.smsId, entry.ref, entry.number, reason))

    @staticmethod
    def _failed(entry: _Entry, reason: str) -> SmsFailed:
        return SmsFailed(entry.smsId, entry.ref, entry.number, reason)

    @staticmethod
    def _expired(entry: _Entry) -> SmsExpired:
        log.error('SMS to %s expired after %d attempt(s) (%s)', entry.shown, entry.attempts, entry.reason or 'no attempt')
        return SmsExpired(entry.smsId, entry.ref, entry.number, entry.reason)

    def _emit(self, events: list[Any]) -> None:
        for event in events:
            try:
                self._publish(event)
            except Exception:
                log.exception('Exception while publishing %r', event)
