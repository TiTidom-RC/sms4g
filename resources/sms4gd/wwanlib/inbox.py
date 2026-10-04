""" Inbox: reading the SMS the modem received, and putting the long ones back together (J4).

The modem stores each received SMS in its memory and announces it (``+CMTI: "SM",<index>``, ``AT+CNMI`` with
``<mt>`` = 1). Reading one is **a single transaction**, ``AT+CMGR`` then ``AT+CMGD``: nothing comes between the two,
the slot is always freed once the SMS was read (an SMS that cannot be decoded is deleted too, with an error in the
log: left in the memory it would fail at every reading and fill the memory up), and nothing is deleted when the
reading failed.

The parts of a long SMS are deleted from the memory as soon as they are read and kept here, in memory, until the
message is complete (``Reassembler``). An incomplete message is given up after ``ttl`` seconds: no text with a hole
is ever delivered, ``SmsIncomplete`` tells that something was lost. A restart of the daemon in between loses the
parts waiting (see the documentation, J4 V2).

The SMS stored while nobody was listening (daemon stopped, modem unplugged) are read when the connection is
established (``catchUp``), and the monitoring checks from time to time that the memory holds nothing readable
(a notification may have been lost).
"""

import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import CancelledError, Future
from dataclasses import dataclass, field
from typing import Any

from .events import SmsIncomplete, SmsReceived
from .exceptions import CmsError, NotConnectedError
from .executor import Priority, Step, Transaction
from .pdu import Concatenation, decodeSmsPdu
from .sms import describe, maskNumber

log = logging.getLogger(__name__)

CMTI = re.compile(r'^\+CMTI:\s*"?(\w+)"?\s*,\s*(\d+)')
CMGL_HEADER = re.compile(r'^\+CMGL:\s*(\d+)\s*,\s*(\d+)')
CMGR_HEADER = re.compile(r'^\+CMGR:')
CPMS_USAGE = re.compile(r'^\+CPMS:\s*"?\w+"?\s*,\s*(\d+)\s*,\s*(\d+)')
HEX_LINE = re.compile(r'^[0-9A-Fa-f]+$')

READ_TIMEOUT = 10.0
DELETE_TIMEOUT = 25.0
LIST_TIMEOUT = 30.0
INVALID_INDEX = 321  # +CMS ERROR: the slot is empty (the SMS was already read)
RECEIVED_STATES = (0, 1)  # +CMGL <stat>: received unread / read (2 and 3 are SMS stored to be sent, or sent)
MEMORY_WARNING_PERCENT = 80
NOT_DELETED_MEMORY = 50  # PDU of the SMS read but not deleted, to deliver them only once


def _clean(text: str) -> str:
    """ A lone surrogate (half an emoji cut between two parts, or a corrupt message) would break the logs and the
    JSON: the halves that meet again become the character, the others a replacement character """
    return text.encode('utf-16-le', 'surrogatepass').decode('utf-16-le', 'replace')


@dataclass
class _Group:
    number: str
    expected: int
    firstSeen: float
    texts: dict[int, str] = field(default_factory=dict)
    sent: float | None = None


class Reassembler:
    """ The parts of the long SMS received so far. Thread safe (the parts arrive from the Executor thread, the
    expiry is checked from the monitoring thread).

    A message is identified by its sender, its concatenation reference and its number of parts. What was delivered
    or given up is remembered for ``memory`` seconds (``maxRemembered`` at most): a late or repeated part of such a
    message is then ignored, instead of starting a message that could never be completed. """

    def __init__(self, ttl: float = 300.0, maxGroups: int = 50, memory: float = 3600.0, maxRemembered: int = 100,
                 clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl
        self._maxGroups = maxGroups
        self._memory = memory
        self._maxRemembered = maxRemembered
        self._clock = clock
        self._lock = threading.Lock()
        self._groups: dict[tuple, _Group] = {}  # in order of arrival of the first part
        self._closed: OrderedDict[tuple, tuple[float, bool]] = OrderedDict()  # key -> (when, delivered)

    def __len__(self) -> int:
        with self._lock:
            return len(self._groups)

    def add(self, number: str, reference: int, expected: int, position: int, text: str,
            sent: float | None) -> list[SmsReceived | SmsIncomplete]:
        """ Adds a part (``position`` from 1 to ``expected``). :return: what became final: the message when it is
        complete, and the oldest message given up when there are too many (``overflow``) """
        key = (number, reference, expected)
        shown = maskNumber(number)
        events: list[SmsReceived | SmsIncomplete] = []
        with self._lock:
            now = self._clock()
            self._forget(now)
            closed = self._closed.get(key)
            if closed is not None:
                if closed[1]:
                    log.debug('SMS part %d/%d from %s ignored: message already delivered', position, expected, shown)
                else:
                    log.warning('SMS part %d/%d from %s ignored: message already abandoned', position, expected, shown)
                return events
            group = self._groups.get(key)
            if group is None:
                while len(self._groups) >= self._maxGroups:
                    events.append(self._abandon(next(iter(self._groups)), 'overflow', now))
                group = self._groups[key] = _Group(number, expected, now)
            if position in group.texts:
                log.debug('SMS part %d/%d from %s ignored: already received', position, expected, shown)
                return events
            group.texts[position] = text
            if sent is not None and (group.sent is None or sent < group.sent):
                group.sent = sent
            # The text of every part is in the log (info): if a message is incomplete or wrongly put back together, every piece is there
            log.info('SMS part %d/%d received from %s (reference %d): %r', position, expected, shown, reference, text)
            if len(group.texts) == expected:
                del self._groups[key]
                self._remember(key, True, now)
                whole = _clean(''.join(group.texts[index] for index in sorted(group.texts)))
                log.info('SMS from %s complete (%d parts, %d characters)', shown, expected, len(whole))
                events.append(SmsReceived(number, whole, group.sent, expected))
        return events

    def expire(self) -> list[SmsIncomplete]:
        """ Gives up the messages whose parts did not all arrive within the lifetime """
        with self._lock:
            now = self._clock()
            self._forget(now)
            late = [key for key, group in self._groups.items() if now - group.firstSeen >= self._ttl]
            return [self._abandon(key, 'timeout', now) for key in late]

    def _abandon(self, key: tuple, reason: str, now: float) -> SmsIncomplete:
        group = self._groups.pop(key)
        self._remember(key, False, now)
        have = ', '.join(str(index) for index in sorted(group.texts))
        log.warning('SMS from %s abandoned: parts %s of %d received (%s)', maskNumber(group.number), have, group.expected, reason)
        return SmsIncomplete(group.number, len(group.texts), group.expected, reason)

    def _remember(self, key: tuple, delivered: bool, now: float) -> None:
        self._closed[key] = (now, delivered)
        self._closed.move_to_end(key)
        while len(self._closed) > self._maxRemembered:
            self._closed.popitem(last=False)

    def _forget(self, now: float) -> None:
        while self._closed:
            key = next(iter(self._closed))
            if now - self._closed[key][0] < self._memory:
                break
            del self._closed[key]


class Inbox:
    def __init__(self, submit: Callable[[Transaction], Future], publish: Callable[[Any], None],
                 readMemory: Callable[[], str | None], reassembler: Reassembler | None = None):
        """ :param submit: submits a transaction to the Executor of the current connection (a failed Future with
            ``NotConnectedError`` when there is none)
        :param publish: receives the events (``SmsReceived``, ``SmsIncomplete``)
        :param readMemory: name of the memory selected for reading and deleting (None if unknown): a notification
            about another memory makes the transaction select it first """
        self._submit = submit
        self._publish = publish
        self._readMemory = readMemory
        self.reassembler = Reassembler() if reassembler is None else reassembler  # (an empty one is falsy)
        self._lock = threading.Lock()
        self._leftover: set[int] = set()  # indexes that are in the memory and are not to be read (or could not be)
        self._notDeleted: OrderedDict[str, None] = OrderedDict()
        self._cataloging = False
        self._memoryWarned = False

    def reset(self) -> None:
        """ A new connection starts: what was known of the memory is not valid anymore (the parts waiting are kept) """
        with self._lock:
            self._leftover.clear()
            self._cataloging = False

    # ---- what triggers a reading ------------------------------------------------------------------

    def onNotification(self, lines: list[str]) -> None:
        """ Called for every notification of the modem; does not block """
        for line in lines:
            match = CMTI.match(line)
            if match:
                self.fetch(match.group(1), int(match.group(2)))

    def catchUp(self) -> None:
        """ Reads every SMS stored in the memory (received while nobody was listening, or whose notification was
        lost). Does not block. """
        with self._lock:
            if self._cataloging:
                return
            self._cataloging = True
        transaction = Transaction([Step('AT+CMGL=4', LIST_TIMEOUT)], Priority.MEMORY_RELEASE)
        self._submit(transaction).add_done_callback(self._listed)

    def sweep(self) -> None:
        """ Gives up the long messages that wait for their last parts for too long """
        for event in self.reassembler.expire():
            self._publish(event)

    def checkMemory(self, lines: list[str]) -> bool:
        """ Reads the answer of ``AT+CPMS?``: warns once when the memory gets full (80 %).
        :return: True if the memory holds an SMS that this class neither left alone nor failed to read: a
            notification was lost, ``catchUp`` is needed """
        for line in lines:
            match = CPMS_USAGE.match(line)
            if match:
                used, total = int(match.group(1)), int(match.group(2))
                break
        else:
            return False
        if total > 0 and used * 100 >= total * MEMORY_WARNING_PERCENT:
            if not self._memoryWarned:
                self._memoryWarned = True
                log.warning('SMS memory %d%% full (%d/%d)', used * 100 // total, used, total)
        else:
            self._memoryWarned = False
        with self._lock:
            return used > len(self._leftover)

    # ---- reading one SMS --------------------------------------------------------------------------

    def fetch(self, memory: str | None, index: int) -> None:
        """ Reads then deletes the SMS at ``index`` (in ``memory``, the selected one when None). Does not block. """
        steps: list[Step] = []
        current = self._readMemory()
        if memory and current and memory.upper() != current.upper():
            steps.append(Step(f'AT+CPMS="{memory}"'))
        position = len(steps)
        steps += [Step(f'AT+CMGR={index}', READ_TIMEOUT), Step(f'AT+CMGD={index}', DELETE_TIMEOUT)]
        transaction = Transaction(steps, Priority.MEMORY_RELEASE)
        self._submit(transaction).add_done_callback(lambda future: self._fetched(future, transaction, index, position))

    def _fetched(self, future: Future, transaction: Transaction, index: int, position: int) -> None:
        """ Runs in the thread that completes the transaction (the Executor): it must stay short """
        try:
            try:
                error = future.exception()
            except CancelledError:
                return
            reading = transaction.responses[position] if len(transaction.responses) > position else []
            if error is not None and not (reading and reading[-1] == 'OK'):
                self._notRead(index, error)
                return
            if error is not None:
                with self._lock:
                    self._leftover.add(index)
                log.warning('SMS at index %d read but not deleted (%s): it stays in the memory', index, describe(error))
            self._received(index, reading, deleted=error is None)
        except Exception:
            log.exception('Error while processing the SMS at index %d', index)

    def _notRead(self, index: int, error: BaseException) -> None:
        if isinstance(error, CmsError) and error.code == INVALID_INDEX:
            log.debug('SMS at index %d: nothing to read (already read)', index)
        elif isinstance(error, NotConnectedError):
            log.debug('SMS at index %d not read: %s (it is read at the next catch-up)', index, describe(error))
        else:
            with self._lock:
                self._leftover.add(index)
            log.warning('SMS at index %d could not be read (%s): it stays in the memory', index, describe(error))

    def _received(self, index: int, reading: list[str], deleted: bool) -> None:
        pdu = next((reading[i + 1] for i, line in enumerate(reading[:-1])
                    if CMGR_HEADER.match(line) and HEX_LINE.match(reading[i + 1])), None)
        if pdu is None:
            log.debug('SMS at index %d: nothing to read', index)
            return
        if not deleted:
            with self._lock:
                if pdu in self._notDeleted:
                    return  # delivered by an earlier reading, whose deletion failed
                self._notDeleted[pdu] = None
                while len(self._notDeleted) > NOT_DELETED_MEMORY:
                    self._notDeleted.popitem(last=False)
        else:
            with self._lock:
                self._leftover.discard(index)
        try:
            decoded = decodeSmsPdu(pdu)
        except Exception:
            log.error('SMS at index %d unreadable (%d bytes), %s', index, len(pdu) // 2, 'deleted' if deleted else 'not deleted')
            return
        if decoded.get('type') != 'SMS-DELIVER':
            log.warning('SMS at index %d is not a received SMS (%s), ignored', index, decoded.get('type'))
            return
        number = str(decoded.get('number') or 'unknown')
        text = str(decoded.get('text') or '')
        try:
            sent: float | None = decoded['time'].timestamp()
        except (KeyError, ValueError, OverflowError, OSError):
            sent = None
        shown = maskNumber(number)
        concatenation = next((element for element in decoded.get('udh') or [] if isinstance(element, Concatenation)), None)
        if concatenation is None or concatenation.parts < 2 or not 1 <= concatenation.number <= concatenation.parts:
            text = _clean(text)
            log.info('SMS received from %s (%d characters): %r', shown, len(text), text)
            self._publish(SmsReceived(number, text, sent, 1))
            return
        # The parts are cleaned together once joined: half an emoji may end one part and the other half start the next
        for event in self.reassembler.add(number, concatenation.reference, concatenation.parts, concatenation.number,
                                          text, sent):
            self._publish(event)

    # ---- the SMS stored in the memory -------------------------------------------------------------

    def _listed(self, future: Future) -> None:
        try:
            try:
                error = future.exception()
            except CancelledError:
                return
            if error is not None:
                if isinstance(error, NotConnectedError):
                    log.debug('Stored SMS not listed: %s', describe(error))
                else:
                    log.warning('Stored SMS could not be listed (%s)', describe(error))
                return
            lines = future.result()
            readable: list[int] = []
            untouched: set[int] = set()
            for line in lines:
                match = CMGL_HEADER.match(line)
                if match is None:
                    continue
                index, state = int(match.group(1)), int(match.group(2))
                if state in RECEIVED_STATES:
                    readable.append(index)
                else:
                    untouched.add(index)
            with self._lock:
                self._leftover = untouched
            if readable or untouched:
                log.info('%d stored SMS to read, %d left alone', len(readable), len(untouched))
            for index in readable:
                self.fetch(None, index)
        except Exception:
            log.exception('Error while listing the stored SMS')
        finally:
            with self._lock:
                self._cataloging = False
