""" Executor: the single writer of the serial port.

Everything that talks to the modem is a *transaction* (one or several steps written one after the
other, without anything else in between). A single thread runs the transactions one at a time,
ordered by priority, and returns each result through a ``Future``.
"""

import itertools
import logging
import queue
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from .exceptions import CmeError, CmsError, CommandError, NotConnectedError, TimeoutException

log = logging.getLogger(__name__)

TERMINATOR = '\r'
CTRL_Z = '\x1a'
ESCAPE = b'\x1b'


class Priority(IntEnum):
    """ Only the *next* transaction is chosen by priority: a transaction in progress is never interrupted """

    CONTROL = 0  # initialization, reconnection, reset
    NETWORK_ACK = 1  # AT+CNMA
    MEMORY_RELEASE = 2  # read + delete of a stored SMS
    SMS_CONTINUATION = 3  # next segment of an SMS being sent
    NEW_SMS = 4  # first segment of a new SMS
    CONSOLE = 5  # diagnostic
    SUPERVISION = 6  # signal, network, keep-alive


@dataclass
class Step:
    """ Data to write, then what to wait for: an end code, or the ``>`` prompt when ``expectPrompt`` is set """

    data: str
    timeout: float = 10.0
    terminator: str = TERMINATOR
    expectPrompt: bool = False


@dataclass
class Transaction:
    steps: list[Step]
    priority: int = Priority.CONSOLE
    parseError: bool = True
    # Upper bound of the time the port stays reserved after a timeout (see Executor)
    maxHold: float = 180.0
    future: Future = field(default_factory=Future)


class _Deadline(Exception):
    """ No line received before the deadline """


class _StepTimeout(Exception):
    def __init__(self, partial: list[str], step: Step):
        super().__init__('step timeout')
        self.partial = partial
        self.step = step


class Executor:
    # End codes of a command (COMMAND NOT SUPPORT: some Huawei modems)
    FINAL_CODE = re.compile(r'^(OK|ERROR|\+CM[ES] ERROR:.*|COMMAND NOT SUPPORT)$')
    CM_ERROR = re.compile(r'^\+(CM[ES]) ERROR: (\d+)$')
    # Answer of AT+CMEE?, used to resynchronize with the modem after an abandoned command
    MARKER = re.compile(r'^\+CMEE: \d')
    CMGF_COMMAND = re.compile(r'^AT\+CMGF=(\d)$', re.IGNORECASE)
    CPMS_COMMAND = re.compile(r'^AT\+CPMS="?(\w+)"?(?:,"?(\w+)"?)?(?:,"?(\w+)"?)?$', re.IGNORECASE)
    BUSY_CODES = (515, 14)  # "please wait" / "SIM busy"
    BUSY_MAX_RETRIES = 5
    # After an Escape, a modem that does not answer is resynchronized without waiting for maxHold
    ESCAPE_GRACE = 2.0
    MARKER_TIMEOUT = 10.0
    POLL_INTERVAL = 0.5
    # Waiting for a modem that is still starting (right after a USB replug): one AT per interval
    READY_INTERVAL = 1.0
    # After the first answer, the probes sent earlier may still answer: let them arrive, then discard them
    READY_SETTLE = 0.3

    def __init__(self, write: Callable[[bytes], None], onStuck: Callable[[str], None]):
        """ :param write: writes raw bytes to the port (raises on a lost port)
        :param onStuck: called when the modem does not answer anymore (the connection must be rebuilt) """
        self._write = write
        self._onStuck = onStuck
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._lines: queue.Queue = queue.Queue()
        self._sequence = itertools.count()
        self._lock = threading.Lock()
        self._stopped = False
        self._stopEvent = threading.Event()
        self._thread: threading.Thread | None = None
        self._active: str | None = None
        self._promptWanted = False
        self._inPrompt = False
        self._writeWait = 0.0
        self._cache: dict[str, Any] = {}
        self.lastExchange: float | None = None  # time.monotonic() of the last answer from the modem

    # ---- public API -------------------------------------------------------------------------------

    def start(self) -> None:
        thread = threading.Thread(target=self._run, name='wwanlib-executor', daemon=True)
        thread.start()
        self._thread = thread

    def waitReady(self, timeout: float) -> None:
        """ Waits until the modem answers ``AT``. A modem that has just been (re)plugged needs several seconds
        before it listens: the bytes written meanwhile are lost, hence one probe per ``READY_INTERVAL``.

        Must be called before ``start()``, from the thread that opens the connection: nothing else can use the
        port, so the late answers of the probes can neither be taken for an answer to another command.

        :raise TimeoutException: if the modem did not answer within ``timeout`` seconds
        :raise NotConnectedError: if stopped meanwhile """
        started = time.monotonic()
        deadline = started + timeout
        probes = 0
        self._active = 'AT'
        try:
            while True:
                self._discardStale()
                probes += 1
                log.debug('write: AT (waiting for the modem, probe %d)', probes)
                self._write(('AT' + TERMINATOR).encode())
                try:
                    attemptEnd = min(deadline, time.monotonic() + self.READY_INTERVAL)
                    while self._nextLine(attemptEnd) != 'OK':
                        pass
                    break
                except _Deadline:
                    if time.monotonic() >= deadline:
                        raise TimeoutException() from None
            log.debug('The modem answers AT after %.1fs (%d probe(s))', time.monotonic() - started, probes)
            self._stopEvent.wait(self.READY_SETTLE)
            self._discardStale()
        finally:
            self._active = None

    def stop(self, timeout: float = 5.0) -> None:
        self._shutdown(NotConnectedError('Modem stopped'))
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def submit(self, command: str, timeout: float = 10.0, priority: int = Priority.CONSOLE,
               parseError: bool = True, maxHold: float = 180.0) -> Future:
        """ Submits a single AT command. The Future resolves to the list of response lines (end code included) """
        return self.submitTransaction(Transaction([Step(command, timeout)], priority, parseError, maxHold))

    def submitTransaction(self, transaction: Transaction) -> Future:
        with self._lock:
            if self._stopped:
                transaction.future.set_exception(NotConnectedError('Modem not connected'))
            else:
                self._queue.put((int(transaction.priority), next(self._sequence), transaction))
        return transaction.future

    def context(self) -> tuple[str | None, bool]:
        """ What the Reader needs to classify lines: the command in progress (or abandoned) and whether the ``>`` prompt is awaited """
        return self._active, self._promptWanted

    @property
    def cache(self) -> dict[str, Any]:
        """ Modem state known from the commands that succeeded: ``smsMode`` (0 PDU / 1 text), ``smsMemories`` (names without quotes) """
        return dict(self._cache)

    def onResponseLine(self, line: str) -> None:
        """ Called by the Reader for every line that belongs to the transaction in progress """
        self._lines.put(line)

    # ---- thread -----------------------------------------------------------------------------------

    def _shutdown(self, error: Exception) -> None:
        with self._lock:
            self._stopped = True
            self._stopEvent.set()
            pending = []
            while True:
                try:
                    transaction = self._queue.get_nowait()[2]
                except queue.Empty:
                    break
                if transaction is not None:
                    pending.append(transaction)
        for transaction in pending:
            if not transaction.future.done():
                transaction.future.set_exception(error)
        # Wake the thread at once: it may be waiting for a transaction or for a line of the modem
        self._queue.put((-1, next(self._sequence), None))
        self._lines.put(None)

    def _run(self) -> None:
        while not self._stopEvent.is_set():
            try:
                transaction = self._queue.get(timeout=self.POLL_INTERVAL)[2]
            except queue.Empty:
                continue
            if transaction is None or not transaction.future.set_running_or_notify_cancel():
                continue
            try:
                self._process(transaction)
            except Exception as e:
                log.exception('Unexpected error in the Executor')
                if not transaction.future.done():
                    transaction.future.set_exception(e)
            finally:
                self._active = None
                self._promptWanted = False
        log.debug('Executor stopped')

    def _process(self, transaction: Transaction) -> None:
        busyRetries = 0
        lastBusyCode = 0
        while True:
            started = time.monotonic()
            try:
                lines, stepIndex = self._runSteps(transaction)
            except _StepTimeout as timeout:
                log.warning('Timeout on %s', self._mask(timeout.step.data))
                transaction.future.set_exception(TimeoutException(timeout.partial or None))
                self._holdPort(timeout.step, started, transaction.maxHold)
                return
            except NotConnectedError as e:
                self._cancelInput()
                transaction.future.set_exception(e)
                return
            except OSError as e:  # serial.SerialException is an OSError; the Transport already signaled the loss
                self._cancelInput()
                transaction.future.set_exception(NotConnectedError(f'Serial write failed: {e}'))
                return

            self.lastExchange = time.monotonic()
            command = self._mask(transaction.steps[stepIndex].data)
            error = self._parseError(command, lines) if transaction.parseError else None
            busyCode = self._busyCode(lines)
            if busyCode is not None and transaction.parseError and stepIndex == 0 and busyRetries < self.BUSY_MAX_RETRIES:
                busyRetries += 1
                lastBusyCode = busyCode
                self._writeWait += 0.2
                log.debug('Device/SIM busy (code %d), retry %d/%d in %.1fs', busyCode, busyRetries, self.BUSY_MAX_RETRIES, self._writeWait)
                if self._stopEvent.wait(self._writeWait):
                    transaction.future.set_exception(NotConnectedError('Modem stopped'))
                    return
                continue
            if busyRetries:
                # A slow modem (515) keeps a small pause between commands; a SIM that was only initializing (14) does not
                self._writeWait = 0.1 if lastBusyCode == 515 else 0.0
            if error is not None:
                transaction.future.set_exception(error)
            else:
                self._updateCache(transaction)
                transaction.future.set_result(lines)
            if self._writeWait > 0:
                self._stopEvent.wait(self._writeWait)
            return
    # ---- one transaction --------------------------------------------------------------------------

    def _runSteps(self, transaction: Transaction) -> tuple[list[str], int]:
        """ :return: the lines of the last step run, and its index. Stops at the first step that ends with an error. """
        lines: list[str] = []
        for index, step in enumerate(transaction.steps):
            lines = self._runStep(step)
            if index < len(transaction.steps) - 1 and not self._ok(lines):
                return lines, index
            self._inPrompt = step.expectPrompt and lines[-1].startswith('>')
        self._inPrompt = False
        return lines, len(transaction.steps) - 1

    def _runStep(self, step: Step) -> list[str]:
        self._discardStale()
        self._active = step.data
        self._promptWanted = step.expectPrompt
        log.debug('write: %s', self._mask(step.data))
        self._write((step.data + step.terminator).encode())
        self._inPrompt = False
        deadline = time.monotonic() + step.timeout
        lines: list[str] = []
        while True:
            try:
                line = self._nextLine(deadline)
            except _Deadline:
                raise _StepTimeout(lines, step) from None
            lines.append(line)
            if step.expectPrompt and line.startswith('>'):
                return lines
            if self.FINAL_CODE.match(line):
                log.debug('response: %s', lines)
                return lines

    def _discardStale(self) -> None:
        while True:
            try:
                stale = self._lines.get_nowait()
            except queue.Empty:
                return
            if stale is not None:
                log.debug('Stale line discarded: %s', stale)

    def _nextLine(self, deadline: float) -> str:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _Deadline
            try:
                line = self._lines.get(timeout=min(remaining, self.POLL_INTERVAL))
                if line is None:
                    raise NotConnectedError('Modem stopped')
                return line
            except queue.Empty:
                if self._stopEvent.is_set():
                    raise NotConnectedError('Modem stopped') from None

    # ---- timeouts (the caller is released at once, the port is not) -------------------------------

    def _holdPort(self, step: Step, started: float, maxHold: float) -> None:
        """ The abandoned command may still answer: nothing is written until its end code arrives, so that
        its answer is never taken for the one of the next command. Limited to maxHold, then resynchronized. """
        if step.expectPrompt:
            # Input mode may still be (or about to be) entered: cancel it
            self._cancelInput(force=True)
            limit = time.monotonic() + self.ESCAPE_GRACE
        else:
            limit = started + maxHold
        try:
            while True:
                line = self._nextLine(limit)
                if self.FINAL_CODE.match(line):
                    log.debug('Abandoned command ended with: %s', line)
                    return
                log.debug('Line of an abandoned command discarded: %s', line)
        except _Deadline:
            pass
        except NotConnectedError:
            return
        log.warning('No end code for %s, resynchronizing with AT+CMEE?', self._mask(step.data))
        try:
            resynchronized = self._resynchronize()
        except (NotConnectedError, OSError):
            return
        if resynchronized:
            log.info('Resynchronized with the modem')
        else:
            log.error('The modem does not answer the resynchronization marker')
            self._shutdown(NotConnectedError('Modem not responding'))
            self._onStuck('Modem not responding')

    def _resynchronize(self) -> bool:
        """ Writes AT+CMEE? and discards everything until its answer (+CMEE: n then OK) """
        self._active = 'AT+CMEE?'
        self._promptWanted = False
        self._write(('AT+CMEE?' + TERMINATOR).encode())
        deadline = time.monotonic() + self.MARKER_TIMEOUT
        seenMarker = False
        while True:
            try:
                line = self._nextLine(deadline)
            except _Deadline:
                return False
            if self.MARKER.match(line):
                seenMarker = True
            elif seenMarker and self.FINAL_CODE.match(line):
                return True

    def _cancelInput(self, force: bool = False) -> None:
        """ Leaves the ``>`` input mode of AT+CMGS (Escape) """
        if force or self._inPrompt:
            self._inPrompt = False
            try:
                self._write(ESCAPE)
                log.debug('Escape sent')
            except OSError:
                pass

    # ---- results ----------------------------------------------------------------------------------

    def _ok(self, lines: list[str]) -> bool:
        return bool(lines) and (lines[-1] == 'OK' or lines[-1].startswith('>'))

    def _busyCode(self, lines: list[str]) -> int | None:
        match = self.CM_ERROR.match(lines[-1]) if lines else None
        if match and int(match.group(2)) in self.BUSY_CODES:
            return int(match.group(2))
        return None

    def _parseError(self, command: str, lines: list[str]) -> Exception | None:
        last = lines[-1]
        if 'ERROR' in last:
            match = self.CM_ERROR.match(last)
            if match:
                code = int(match.group(2))
                return CmeError(command, code) if match.group(1) == 'CME' else CmsError(command, code)
            return CommandError(command)
        if last == 'COMMAND NOT SUPPORT':
            return CommandError(f'{command} ({last})')
        return None

    def _updateCache(self, transaction: Transaction) -> None:
        for step in transaction.steps:
            cmgf = self.CMGF_COMMAND.match(step.data)
            if cmgf:
                self._cache['smsMode'] = int(cmgf.group(1))
                continue
            cpms = self.CPMS_COMMAND.match(step.data)
            if cpms:
                self._cache['smsMemories'] = tuple(name for name in cpms.groups() if name)

    @staticmethod
    def _mask(command: str) -> str:
        """ The PIN never appears in clear text, not even in the logs of a library used alone """
        if command.upper().startswith('AT+CPIN='):
            return 'AT+CPIN="****"'
        return command
