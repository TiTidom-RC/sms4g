""" Sending an SMS: encoding into parts, one transaction per part, and what to do with each result.

``SmsSender.send()`` sends one message and blocks until it is over. It does not queue and does not retry: it
only says what happened (``SendOutcome``), the queue of the library (``outbox.py``) decides what to do next.

A part is never sent twice (a doublon is worse than a lost alert): the outcome is ``retry`` only when the
message surely did not leave the modem.
"""

import logging
import random
import re
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field

from .exceptions import CmeError, CmsError, CommandError, NotConnectedError, TimeoutException
from .executor import CTRL_Z, Priority, Step, Transaction
from .pdu import encodeGsm7, encodeSmsSubmitPdu

log = logging.getLogger(__name__)

PROMPT_TIMEOUT = 5.0  # AT+CMGS=... until the '>' prompt
SUBMIT_TIMEOUT = 35.0  # text or PDU + Ctrl-Z until +CMGS (the network may be slow)

# Errors worth another try: the message may leave later (3GPP TS 27.005 and SimCom). Any other error is final.
TEMPORARY_CMS_ERRORS = frozenset({300, 314, 322, 331, 332, 500})  # ME failure, SIM busy, memory full, no network service, network timeout, unknown
TEMPORARY_CME_ERRORS = frozenset({14, 30, 31, 515})  # SIM busy, no network service, network timeout, please wait
# An error that says nothing (500, unknown) is retried only a few times: a destination the network never accepts (a
# landline, an invalid number) gets it every time, and must not keep the SMS waiting for an hour
LIMITED_RETRY_CMS_ERRORS = frozenset({500})
MAX_LIMITED_RETRIES = 3

NUMBER = re.compile(r'^\+?\d{2,20}$')
NUMBER_SEPARATORS = re.compile(r'[ .\-()]')


@dataclass(frozen=True)
class SmsPart:
    """ What to write for one part: the command that opens the input, then the body (PDU in hex, or the text) """

    command: str
    body: str
    reference: int | None = None  # TP-MR put in the PDU


@dataclass
class SendOutcome:
    """ ``status``: ``sent`` (every part accepted by the SMS center), ``failed`` (final), ``retry`` (nothing left
    the modem and the cause is temporary). ``references`` are the TP-MR returned by the modem, one per accepted part. """

    status: str
    reason: str = ''
    parts: int = 0
    references: list[int | None] = field(default_factory=list)
    limited: bool = False  # `retry` only a few times (`MAX_LIMITED_RETRIES`): the cause may be permanent

    @property
    def sentParts(self) -> int:
        return len(self.references)


def normalizeNumber(number: object) -> str | None:
    """ The number without the separators typed by hand (spaces, dots, dashes, parentheses), None if it is not a
    phone number (a quote or a letter in it would end up in the AT command) """
    if not isinstance(number, str):
        return None
    cleaned = NUMBER_SEPARATORS.sub('', number)
    return cleaned if NUMBER.match(cleaned) else None


def maskNumber(number: str) -> str:
    """ Same rule as the daemon for the logs: first 4 and last 2 characters """
    return number if len(number) <= 6 else number[:4] + 'X' * (len(number) - 6) + number[-2:]


def usesUcs2(text: str) -> bool:
    """ Whether the text has a character outside the GSM-7 alphabet: the whole message is then sent in UCS-2, with
    70 characters per SMS (67 per part when split) instead of 160 (153) """
    try:
        encodeGsm7(text)
    except ValueError:
        return True
    return False


def buildParts(number: str, text: str, reference: int = 0, concatReference: int | None = None,
               requestStatusReport: bool = False, maxPartsPerGroup: int = 0) -> list[SmsPart]:
    """ Encodes a message into the parts to send (PDU mode, the only one: DEC-36).

    :param reference: TP-MR of the first part, the next ones take the following values
    :param concatReference: reference of the first concatenation group (random by default, one more per group)
    :raise ValueError: invalid number or empty message """
    normalized = normalizeNumber(number)
    if normalized is None:
        raise ValueError('invalid number')
    if not isinstance(text, str) or not text.strip():
        raise ValueError('empty message')
    pdus = encodeSmsSubmitPdu(normalized, text, reference=reference, requestStatusReport=requestStatusReport,
                              maxPartsPerGroup=maxPartsPerGroup, concatReference=concatReference)
    return [SmsPart(f'AT+CMGS={pdu.tpduLength}', str(pdu), (reference + index) % 256) for index, pdu in enumerate(pdus)]


def segmentTransaction(part: SmsPart, priority: int) -> Transaction:
    """ The two steps of a part, written one after the other with nothing in between: the command up to the
    '>' prompt, then the body ended by Ctrl-Z. Writing the body is the point of no return (``commit``). The body
    (number and text) is named in the warnings and in the errors: it only appears in the debug line of the write. """
    return Transaction([Step(part.command, PROMPT_TIMEOUT, expectPrompt=True),
                        Step(part.body, SUBMIT_TIMEOUT, terminator=CTRL_Z, commit=True, label='<SMS body>')], priority)


def parseCmgs(lines: list[str]) -> int | None:
    """ TP-MR of the part, from ``+CMGS: <mr>``. None if the modem accepted the part without giving it. """
    for line in lines:
        match = re.match(r'^\+CMGS:\s*(\d+)', line)
        if match:
            return int(match.group(1))
    return None


def isTemporary(error: BaseException) -> bool:
    """ Whether a failure that left nothing in the network is worth another try """
    if isinstance(error, CmsError):
        return error.code in TEMPORARY_CMS_ERRORS
    if isinstance(error, CmeError):
        return error.code in TEMPORARY_CME_ERRORS
    if isinstance(error, CommandError):
        return False  # a plain ERROR tells nothing: final, to be seen at once
    return isinstance(error, (NotConnectedError, TimeoutException))


def describe(error: BaseException) -> str:
    """ Short reason for Jeedom. Never ``str(error)`` of a CommandError: its command is the body of the part
    (number and text). """
    if isinstance(error, CommandError):
        return f'+{error.type} ERROR: {error.code}' if error.type and error.code is not None else 'ERROR'
    if isinstance(error, TimeoutException):
        return 'timeout'
    if isinstance(error, NotConnectedError):
        return 'modem not connected'
    return type(error).__name__


class SmsSender:
    def __init__(self, submit: Callable[[Transaction], Future], requestStatusReport: bool = False,
                 segmentPause: float = 0.5, stopEvent: threading.Event | None = None, startReference: int | None = None):
        """ :param submit: submits a transaction to the Executor of the current connection. When the modem is not
            connected the Future must fail with ``NotConnectedError``.
        :param segmentPause: seconds between two parts of a message (the memory releases pass meanwhile)
        :param stopEvent: set when the library stops, ends the pauses
        :param startReference: TP-MR of the first part sent, random by default: the reports of the messages sent before
            a restart of the daemon, which may still arrive, then rarely carry the TP-MR of a new message """
        self._submit = submit
        self._requestStatusReport = requestStatusReport
        self._segmentPause = segmentPause
        self._stop = stopEvent or threading.Event()
        self._reference = random.randrange(256) if startReference is None else startReference % 256  # TP-MR of the next part: follows the one the modem returns (+1)

    def send(self, number: str, text: str, maxPartsPerGroup: int = 0,
             onPart: Callable[[int, int, int | None], None] | None = None) -> SendOutcome:
        """ :param onPart: called as soon as a part is accepted, with its position (from 1), the number of parts and
            its TP-MR: the delivery report of a part may come while the next one is being sent """
        try:
            parts = buildParts(number, text, self._reference, random.randrange(256), self._requestStatusReport,
                               max(0, maxPartsPerGroup))
        except ValueError as e:
            return SendOutcome('failed', str(e))
        shown = maskNumber(normalizeNumber(number) or '')
        if usesUcs2(text):
            # The user can then write the message differently (no ç, ê, ô, ’, emoji...): nothing is cleaned for him
            log.info('SMS to %s: %d part(s), UCS-2 (a character is outside the GSM-7 alphabet: 70 characters per part '
                     'instead of 160)', shown, len(parts))
        outcome = SendOutcome('sent', parts=len(parts))
        for index, part in enumerate(parts):
            if index > 0 and self._stop.wait(self._segmentPause):
                return self._failed(outcome, 'daemon stopped')
            transaction = segmentTransaction(part, Priority.NEW_SMS if index == 0 else Priority.SMS_CONTINUATION)
            try:
                lines = self._submit(transaction).result()
            except Exception as error:
                return self._failure(outcome, error, transaction, index, shown)
            reference = parseCmgs(lines)
            if reference is None:
                log.warning('SMS to %s: part %d/%d accepted without +CMGS reference', shown, index + 1, len(parts))
            else:
                self._reference = (reference + 1) % 256
            outcome.references.append(reference)
            if onPart is not None:
                onPart(index + 1, len(parts), reference)
            log.debug('SMS to %s: part %d/%d accepted (TP-MR %s)', shown, index + 1, len(parts), reference)
        return outcome

    @staticmethod
    def _failed(outcome: SendOutcome, reason: str) -> SendOutcome:
        outcome.status = 'failed'
        outcome.reason = reason
        return outcome

    def _failure(self, outcome: SendOutcome, error: Exception, transaction: Transaction, index: int, shown: str) -> SendOutcome:
        # An explicit refusal of the modem proves the part did not leave. Otherwise it did not leave only if its
        # body was never written. Anything else (timeout or lost port after Ctrl-Z) is unknown: never sent again.
        surelyNotSent = isinstance(error, CommandError) or not transaction.committed
        reason = describe(error)
        if index == 0 and surelyNotSent and isTemporary(error):
            log.info('SMS to %s not sent (%s), to be tried again', shown, reason)
            outcome.status = 'retry'
            outcome.reason = reason
            outcome.limited = isinstance(error, CmsError) and error.code in LIMITED_RETRY_CMS_ERRORS
            return outcome
        if index > 0:
            log.error('SMS to %s: part %d/%d failed (%s), the previous parts have been sent', shown, index + 1, outcome.parts, reason)
        elif surelyNotSent:
            log.error('SMS to %s failed (%s)', shown, reason)
        else:
            log.error('SMS to %s failed (%s): the part may have been sent, it is not sent again', shown, reason)
        return self._failed(outcome, reason)
