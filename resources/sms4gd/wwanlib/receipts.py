""" Delivery reports: following each SMS part sent until the network says what became of it (J5).

The modem tells what happened to a part with an SMS-STATUS-REPORT (``+CDS`` straight from the network, or
``+CDSI`` when it is kept in the SR memory). The report carries the TP-MR the modem gave to the part (the value
``+CMGS`` returned), the recipient, the time the SMS center got the message and a status (TP-ST, 3GPP TS 23.040
9.2.3.15):

* 0x00 - 0x1F  delivered (or forwarded, or replaced): final
* 0x20 - 0x3F  temporary error, the SMS center tries again: **not final**, another report comes for the same TP-MR
* 0x40 - 0x7F  permanent error (0x40 - 0x5F), or temporary error the SMS center gave up (0x60 - 0x7F): final failure

A message is **delivered** when every part is, **undelivered** as soon as one part failed for good, **pending**
while a part is delayed. The states only go forward (sent, pending, final): a late report never takes back a final
state. Nothing is published before the SMS itself is announced as sent (``close``): a report may come while the
next part is still being sent.

A report is matched to a part by its TP-MR, checked against the recipient (last digits, so that ``06...`` and
``+33 6...`` meet without any rule of a country) and the time (a late report of a message sent before a restart of
the daemon must not be taken for the one of a new message that has the same TP-MR). A report that matches nothing
yet waits ``orphanTtl`` seconds (the part may still be registering), then is dropped.

Everything is in memory: after a restart the reports of the earlier messages are dropped. A message without a final
report after ``maxAge`` is reported as unknown.
"""

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .events import SmsDelivery
from .sms import maskNumber

log = logging.getLogger(__name__)

# TP-ST of a temporary error (3GPP TS 23.040 9.2.3.15), for the log
TEMPORARY_REASONS = {
    0x20: 'congestion',
    0x21: 'recipient busy',
    0x22: 'no response from the recipient',
    0x23: 'service rejected',
    0x24: 'quality of service unavailable',
    0x25: 'error in the recipient',
}
PERMANENT_REASONS = {
    0x40: 'remote procedure error',
    0x41: 'incompatible destination',
    0x42: 'connection rejected by the recipient',
    0x43: 'not obtainable',
    0x44: 'quality of service unavailable',
    0x45: 'no interworking available',
    0x46: 'validity period expired',
    0x47: 'deleted by the sender',
    0x48: 'deleted by the SMS center',
    0x49: 'no such message',
    0x4A: 'error in the SMS center',
}

DELIVERED = 'delivered'
PENDING = 'pending'
FAILED = 'failed'
WAITING = 'waiting'  # no report yet

# States of an SMS, in the order they can be reached
SENT = 'sent'
UNDELIVERED = 'undelivered'
UNKNOWN = 'unknown'
_RANK = {SENT: 0, PENDING: 1, DELIVERED: 2, UNDELIVERED: 2, UNKNOWN: 2}

DEFAULT_MAX_AGE = 25 * 3600.0  # the SMS center is asked to keep trying 24 hours (AT+CSMP validity 167), plus one
DEFAULT_ORPHAN_TTL = 60.0
DEFAULT_TIME_WINDOW = 600.0  # seconds between our sending and the time the SMS center puts on the report
MAX_TRACKED = 1000
DIGITS_COMPARED = 8


def classify(status: int) -> tuple[str, str]:
    """ The state of a part and the reason (for the log) given by a TP-ST """
    if 0x00 <= status <= 0x1F:
        return DELIVERED, ''
    if 0x20 <= status <= 0x3F:
        return PENDING, TEMPORARY_REASONS.get(status, f'temporary error 0x{status:02X}')
    if 0x40 <= status <= 0x5F:
        return FAILED, PERMANENT_REASONS.get(status, f'permanent error 0x{status:02X}')
    if 0x60 <= status <= 0x7F:
        return FAILED, f'given up by the SMS center (0x{status:02X})'
    return UNKNOWN, f'reserved status 0x{status:02X}'


def _digits(number: str) -> str:
    return re.sub(r'\D', '', number)[-DIGITS_COMPARED:]


@dataclass
class _Part:
    index: int
    reference: int  # TP-MR
    sentAt: float  # time.time()
    state: str = WAITING
    reason: str = ''


@dataclass
class _Sms:
    smsId: str
    ref: str | None
    number: str
    parts: int
    registeredAt: float
    entries: dict[int, _Part] = field(default_factory=dict)  # by part index
    closed: bool = False  # every part registered and the SMS announced as sent
    published: str = SENT
    final: bool = False  # a final state was published (or the SMS failed): the reports that follow change nothing

    @property
    def shown(self) -> str:
        return maskNumber(self.number)

    def deliveredParts(self) -> int:
        return sum(1 for part in self.entries.values() if part.state == DELIVERED)


@dataclass
class _Orphan:
    arrivedAt: float
    reference: int
    number: str
    reportTime: float | None
    status: int


class ReceiptTracker:
    def __init__(self, publish: Callable[[Any], None], maxAge: float = DEFAULT_MAX_AGE,
                 orphanTtl: float = DEFAULT_ORPHAN_TTL, timeWindow: float = DEFAULT_TIME_WINDOW,
                 clock: Callable[[], float] = time.monotonic, wallClock: Callable[[], float] = time.time):
        """ :param publish: receives the ``SmsDelivery`` events
        :param clock: monotonic time, for the lifetimes
        :param wallClock: time of the day, compared with the time the SMS center puts on a report """
        self._publish = publish
        self._maxAge = maxAge
        self._orphanTtl = orphanTtl
        self._timeWindow = timeWindow
        self._clock = clock
        self._wallClock = wallClock
        self._lock = threading.Lock()
        self._sms: dict[str, _Sms] = {}
        self._orphans: list[_Orphan] = []

    def __len__(self) -> int:
        with self._lock:
            return len(self._sms)

    # ---- what the sending tells ------------------------------------------------------------------

    def register(self, smsId: str, ref: str | None, number: str, index: int, parts: int, reference: int | None) -> None:
        """ A part was accepted by the SMS center (``+CMGS`` gave its TP-MR). Called part by part: the report of
        the first part often comes while the second one is being sent. """
        if reference is None:
            log.debug('SMS %s part %d/%d has no TP-MR: its delivery cannot be followed', smsId, index, parts)
            return
        with self._lock:
            sms = self._sms.get(smsId)
            if sms is None:
                if len(self._sms) >= MAX_TRACKED:
                    oldest = min(self._sms.values(), key=lambda item: item.registeredAt)
                    del self._sms[oldest.smsId]
                    log.warning('Too many SMS wait for their delivery report, the oldest is not followed anymore')
                sms = self._sms[smsId] = _Sms(smsId, ref, number, parts, self._clock())
            sms.entries[index] = _Part(index, reference, self._wallClock())
            self._rematch(sms)

    def close(self, smsId: str) -> None:
        """ The SMS was announced as sent (every part is registered): what the reports said until now can be published """
        with self._lock:
            sms = self._sms.get(smsId)
            if sms is None:
                return
            sms.closed = True
            self._aggregate(sms)

    def abandon(self, smsId: str) -> None:
        """ The sending failed after some parts left: Jeedom was told ``failed``, the reports of these parts change nothing """
        with self._lock:
            sms = self._sms.get(smsId)
            if sms is not None:
                sms.final = True
                sms.closed = True

    # ---- what the network tells ------------------------------------------------------------------

    def onReport(self, reference: int, number: str, reportTime: float | None, status: int) -> None:
        """ A status report: ``reference`` is the TP-MR, ``number`` the recipient, ``reportTime`` the time the SMS
        center received our message (seconds since 1970, None when unreadable) """
        with self._lock:
            now = self._clock()
            found = self._find(reference, number, reportTime)
            if found is None:
                self._orphans.append(_Orphan(now, reference, number, reportTime, status))
                gap = self._timeGap(reference, number, reportTime)
                if gap is not None:
                    log.warning('Delivery report for TP-MR %d (%s): same TP-MR and recipient as an SMS sent, but %d s apart: the ' 
                                'clock of this machine must be right (the reports are matched by time too)', reference, maskNumber(number), gap)
                else:
                    log.debug('Delivery report for TP-MR %d (%s) does not match a part yet', reference, maskNumber(number))
                return
            self._apply(found[0], found[1], status)

    def expire(self) -> None:
        """ Drops the reports nobody claimed, reports as unknown the SMS that got no final report in time, forgets the old ones """
        with self._lock:
            now = self._clock()
            for orphan in [item for item in self._orphans if now - item.arrivedAt >= self._orphanTtl]:
                self._orphans.remove(orphan)
                log.info('Delivery report for TP-MR %d (%s) ignored: no SMS of this daemon matches (sent before a restart?)',
                         orphan.reference, maskNumber(orphan.number))
            for sms in list(self._sms.values()):
                if now - sms.registeredAt < self._maxAge:
                    continue
                if not sms.final and sms.closed:
                    sms.final = True
                    sms.published = UNKNOWN
                    log.warning('SMS to %s: no final delivery report after %d h, state unknown', sms.shown,
                                int(self._maxAge // 3600))
                    self._publish(SmsDelivery(sms.smsId, sms.ref, sms.number, UNKNOWN, 'no report', sms.parts,
                                              sms.deliveredParts()))
                del self._sms[sms.smsId]

    # ---- inside (the lock is held) ---------------------------------------------------------------

    def _find(self, reference: int, number: str, reportTime: float | None) -> tuple[_Sms, _Part] | None:
        """ The part a report is about: same TP-MR, same recipient, sent at the time the report says. When several
        parts match (the TP-MR counter went round), the one still waiting for a report, then the latest. """
        candidates = []
        for sms in self._sms.values():
            if _digits(sms.number) != _digits(number):
                continue
            for part in sms.entries.values():
                if part.reference != reference:
                    continue
                if reportTime is not None and abs(reportTime - part.sentAt) > self._timeWindow:
                    continue
                candidates.append((sms, part))
        if not candidates:
            return None
        return max(candidates, key=lambda item: (item[1].state in (WAITING, PENDING), item[1].sentAt))

    def _timeGap(self, reference: int, number: str, reportTime: float | None) -> int | None:
        """ When a report has the TP-MR and the recipient of a part but not its time: the gap in seconds (the clock is wrong) """
        if reportTime is None:
            return None
        gaps = [abs(reportTime - part.sentAt) for sms in self._sms.values() if _digits(sms.number) == _digits(number)
                for part in sms.entries.values() if part.reference == reference]
        return int(min(gaps)) if gaps else None

    def _rematch(self, sms: _Sms) -> None:
        """ A part was just registered: the reports that came before it are applied now """
        for orphan in list(self._orphans):
            found = self._find(orphan.reference, orphan.number, orphan.reportTime)
            if found is not None and found[0] is sms:
                self._orphans.remove(orphan)
                self._apply(found[0], found[1], orphan.status)

    def _apply(self, sms: _Sms, part: _Part, status: int) -> None:
        state, reason = classify(status)
        shown = f'SMS to {sms.shown}: part {part.index}/{sms.parts} (TP-MR {part.reference})'
        if state == UNKNOWN:
            log.warning('%s: unusable delivery report (%s)', shown, reason)
            return
        if part.state in (DELIVERED, FAILED):
            log.debug('%s: delivery report ignored, the part is already %s', shown, part.state)
            return
        part.state, part.reason = state, reason
        if state == DELIVERED:
            log.info('%s delivered', shown)
        elif state == PENDING:
            log.info('%s delayed (%s)', shown, reason)
        else:
            log.warning('%s not delivered (%s)', shown, reason)
        self._aggregate(sms)
        if sms.final and len(sms.entries) == sms.parts and all(item.state in (DELIVERED, FAILED) for item in sms.entries.values()):
            del self._sms[sms.smsId]  # nothing more to wait for

    def _aggregate(self, sms: _Sms) -> None:
        """ Publishes the state of the SMS when it moved forward (and every part is registered) """
        if not sms.closed or sms.final:
            return
        parts = list(sms.entries.values())
        failed = [part for part in parts if part.state == FAILED]
        if failed:
            state, reason = UNDELIVERED, failed[0].reason
        elif len(parts) == sms.parts and all(part.state == DELIVERED for part in parts):
            state, reason = DELIVERED, ''
        elif any(part.state == PENDING for part in parts):
            state, reason = PENDING, next(part.reason for part in parts if part.state == PENDING)
        else:
            return
        if state == sms.published or _RANK[state] < _RANK[sms.published]:
            return
        sms.published = state
        sms.final = _RANK[state] == _RANK[DELIVERED]
        self._publish(SmsDelivery(sms.smsId, sms.ref, sms.number, state, reason, sms.parts, sms.deliveredParts()))
