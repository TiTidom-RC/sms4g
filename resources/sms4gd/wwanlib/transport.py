""" Serial port access: opening, the Reader thread and the classification of every received line """

import logging
import re
import threading
from collections.abc import Callable
from enum import Enum

import serial  # pyserial

log = logging.getLogger(__name__)


class LineKind(Enum):
    RESPONSE = 'response'  # belongs to the transaction in progress
    NOTIFICATION = 'notification'  # sent spontaneously by the modem
    IGNORE = 'ignore'  # command echo


class LineClassifier:
    """ Decides whether a received line is the response to the current transaction or a spontaneous
    notification (URC). This is the delicate part of the library: a notification mistaken for a response
    (or the opposite) shifts every following answer. Only the Reader thread uses an instance. """

    # Always spontaneous, even while a command is in progress
    ALWAYS_PREFIXES = ('+CMTI', '+CDSI', '+CDS:', '+CLIP')
    ALWAYS_LINES = ('RING', 'RDY', 'SMS DONE', 'PB DONE')
    # ^NAME: notifications of Huawei-like modems; a response to AT^NAME is told apart by the active command
    CARET_LINE = re.compile(r'^\^([A-Z]+)')
    # Spontaneous unless they answer the command in progress (e.g. "+CPIN: READY" answering AT+CPIN?)
    AMBIGUOUS_PREFIXES = ('+CPIN:', '+CREG:', '+CGREG:', '+CEREG:', '+CUSD:', '+CFUN:')
    COMMAND_NAME = re.compile(r'^AT([+^][A-Z0-9]+)', re.IGNORECASE)
    # A PDU is a hex string: used to recognize the second line of a "+CDS:" notification
    HEX_LINE = re.compile(r'^[0-9A-Fa-f]+$')
    # Gives up waiting for the second line of a "+CDS:" after this many interleaved lines
    MAX_CONTINUATION_SKIP = 5

    def __init__(self, urcPrefixes: tuple[str, ...] = ()):
        self.urcPrefixes = urcPrefixes
        self._expectContinuation = False
        self._skipped = 0

    @property
    def expectingContinuation(self) -> bool:
        return self._expectContinuation

    def classify(self, line: str, activeCommand: str | None) -> LineKind:
        """ :param activeCommand: text of the transaction in progress (or abandoned), None if there is none """
        if activeCommand is not None and line.strip() == activeCommand.strip():
            return LineKind.IGNORE

        if self._expectContinuation:
            if self.HEX_LINE.match(line):
                self._expectContinuation = False
                self._skipped = 0
                return LineKind.NOTIFICATION
            # A response or another notification is interleaved: keep waiting for the real PDU
            self._skipped += 1
            if self._skipped >= self.MAX_CONTINUATION_SKIP:
                log.warning('Gave up waiting for the PDU line of a "+CDS:" notification after %d interleaved lines', self._skipped)
                self._expectContinuation = False
                self._skipped = 0

        if line.startswith('+CDS:'):
            self._expectContinuation = True
            self._skipped = 0
            return LineKind.NOTIFICATION
        if line.startswith(self.ALWAYS_PREFIXES) or line in self.ALWAYS_LINES or line.startswith(self.urcPrefixes):
            return LineKind.NOTIFICATION

        caretMatch = self.CARET_LINE.match(line)
        if caretMatch:
            return LineKind.RESPONSE if self._answers(activeCommand, '^' + caretMatch.group(1)) else LineKind.NOTIFICATION

        for prefix in self.AMBIGUOUS_PREFIXES:
            if line.startswith(prefix):
                return LineKind.RESPONSE if self._answers(activeCommand, prefix[:-1]) else LineKind.NOTIFICATION

        return LineKind.NOTIFICATION if activeCommand is None else LineKind.RESPONSE

    def _answers(self, activeCommand: str | None, name: str) -> bool:
        """ True if ``name`` (e.g. "+CPIN") is the name of the command in progress """
        if activeCommand is None:
            return False
        match = self.COMMAND_NAME.match(activeCommand.strip())
        return match is not None and match.group(1).upper() == name.upper()


class Transport:
    """ Owns the serial port and its single reader thread.

    The reader hands every line to ``onResponse`` or to ``onNotification`` depending on the classification;
    the Executor tells what it is waiting for through ``getContext`` (active command, prompt expected).
    ``onLost`` is called once when the port disappears (device unplugged...). """

    def __init__(self, port: str, baudrate: int, getContext: Callable[[], tuple[str | None, bool]],
                 onResponse: Callable[[str], None], onNotification: Callable[[list[str]], None],
                 onLost: Callable[[Exception], None], urcPrefixes: tuple[str, ...] = ()):
        self.port = port
        self.baudrate = baudrate
        self._getContext = getContext
        self._onResponse = onResponse
        self._onNotification = onNotification
        self._onLost = onLost
        self._classifier = LineClassifier(urcPrefixes)
        self._serial: serial.Serial | None = None
        self._thread: threading.Thread | None = None
        self._alive = False
        self._lostSignaled = False
        self._lostLock = threading.Lock()

    def setUrcPrefixes(self, urcPrefixes: tuple[str, ...]) -> None:
        self._classifier.urcPrefixes = urcPrefixes

    def open(self) -> None:
        log.info('Opening port %s at %d bps', self.port, self.baudrate)
        self._serial = serial.Serial(port=self.port, baudrate=self.baudrate, dsrdtr=True, rtscts=False, timeout=1)
        # Discard stray bytes left by a previous session
        self._serial.reset_input_buffer()
        self._alive = True
        thread = threading.Thread(target=self._readLoop, name='wwanlib-reader', daemon=True)
        thread.start()
        self._thread = thread

    def write(self, data: bytes) -> None:
        port = self._serial
        if port is None or not self._alive:
            raise serial.SerialException('Port is closed')
        try:
            port.write(data)
        except (serial.SerialException, OSError) as e:
            self._signalLost(e)
            raise

    def close(self) -> None:
        self._alive = False
        port = self._serial
        if port is not None:
            try:
                port.cancel_read()
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(3)
        if port is not None:
            try:
                port.close()
            except Exception:
                pass
        log.debug('Port %s closed', self.port)

    def _signalLost(self, error: Exception) -> None:
        with self._lostLock:
            if self._lostSignaled or not self._alive:
                return
            self._lostSignaled = True
        log.error('Serial port lost: %s', error)
        self._onLost(error)

    def _readLoop(self) -> None:
        port = self._serial
        assert port is not None
        buffer = bytearray()
        pending: list[str] = []
        try:
            while self._alive:
                data = port.read(port.in_waiting or 1)
                if not data:
                    # Idle: nothing more is coming for the notification being collected
                    self._flush(pending)
                    continue
                for byte in data:
                    buffer.append(byte)
                    if buffer.endswith(b'\r\n'):
                        raw = bytes(buffer[:-2])
                        buffer.clear()
                        self._handleRaw(raw, pending)
                        if not self._classifier.expectingContinuation and port.in_waiting == 0:
                            self._flush(pending)
                    elif buffer == b'> ' and self._getContext()[1]:
                        buffer.clear()
                        self._onResponse('> ')
        except (serial.SerialException, OSError) as e:
            self._signalLost(e)

    def _handleRaw(self, raw: bytes, pending: list[str]) -> None:
        try:
            line = raw.decode()
        except UnicodeDecodeError:
            log.debug('Undecodable line ignored: %r', raw)
            return
        if not line:
            return
        kind = self._classifier.classify(line, self._getContext()[0])
        if kind is LineKind.RESPONSE:
            self._onResponse(line)
        elif kind is LineKind.NOTIFICATION:
            pending.append(line)
        else:
            log.debug('Echo ignored')

    def _flush(self, pending: list[str]) -> None:
        if pending:
            lines = list(pending)
            pending.clear()
            log.debug('Notification: %s', lines)
            self._onNotification(lines)
