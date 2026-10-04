""" The Modem class: public facade of wwanlib, and the initialization sequence run at each connection """

import logging
import re
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any

from .events import (ConnectionState, EventDispatcher, ModemIdentified, NetworkChanged, Registration, SignalChanged,
                     StateChanged, UnsolicitedNotification)
from .exceptions import (CommandError, IncorrectPinError, NotConnectedError, PduModeNotSupportedError, PinRequiredError,
                         PukRequiredError, SmscNumberUnknownError, TimeoutException, WwanException)
from .executor import Executor, Priority, Transaction
from .inbox import Inbox, Reassembler
from .outbox import Outbox
from .profiles import GENERIC, Profile, detectProfile
from .sms import SmsSender, normalizeNumber
from .supervisor import Supervisor
from .transport import Transport
from .util import lineMatching, lineStartingWith

log = logging.getLogger(__name__)


@dataclass
class ModemOptions:
    deliveryReport: bool = False  # ask the SMS center for delivery reports
    smsc: str | None = None  # SMS center number, None to keep the one of the SIM
    force4g: bool = False  # LTE only (SimCom modems only)
    reconnectBaseDelay: float = 5.0
    reconnectMaxDelay: float = 300.0
    reconnectMaxAttempts: int = 10
    monitorInterval: float = 30.0  # seconds between two readings of the signal and of the network (5 at least)
    readyTimeout: float = 20.0  # seconds to wait for a modem that is still starting to answer AT
    smsTtl: float = 3600.0  # seconds an SMS waits in the queue before it expires
    smsQueueSize: int = 50  # SMS waiting at most: a new one is refused beyond
    segmentPause: float = 0.5  # seconds between two parts of an SMS
    aging: float = 30.0  # seconds after which a waiting transaction rises by one rank (0 = off), see Executor
    concatPartsTtl: float = 300.0  # seconds the parts of a long SMS are waited for before the message is given up
    maxIncompleteSms: int = 50  # long SMS waiting for their last parts at most: beyond, the oldest is given up


class _Session:
    """ Everything that lives for the duration of one connection """

    def __init__(self):
        self.transport: Transport
        self.executor: Executor
        self.profile: Profile = GENERIC
        self.smsReady = False  # the SMS memory is selected and the notifications are set: received SMS can be read

    def run(self, command: str, timeout: float = 10.0, parseError: bool = True, maxHold: float = 180.0) -> list[str]:
        """ Runs an initialization command (highest priority) and waits for its result """
        return self.executor.submit(command, timeout, Priority.CONTROL, parseError, maxHold).result()


class Modem:
    """ A modem driven by AT commands.

    Usage::

        modem = Modem('/dev/ttyUSB2', 115200, pin=None, options=ModemOptions(deliveryReport=True))
        modem.onEvent(callback)   # StateChanged, ModemIdentified, SignalChanged, NetworkChanged, UnsolicitedNotification,
                                  # SmsQueued, SmsSent, SmsFailed, SmsExpired, SmsReceived, SmsIncomplete
        modem.start()             # does not block: the Supervisor connects (and reconnects) in the background
        lines = modem.command('AT+CSQ', timeout=10).result()
        smsId = modem.sendSms('+33612345678', 'Hello', ref='42')   # queued, SmsSent / SmsFailed / SmsExpired follow
        modem.stop()

    The library never imports Jeedom, takes its settings from the constructor only and logs through
    ``logging.getLogger(__name__)``: it never configures the logging.
    """

    MIN_MONITOR_INTERVAL = 5.0  # seconds

    def __init__(self, port: str, baudrate: int = 115200, pin: str | None = None, options: ModemOptions | None = None):
        self.port = port
        self.baudrate = baudrate
        self._pin = pin
        self.options = options or ModemOptions()
        self._state = ConnectionState.DISCONNECTED
        self._profile: Profile = GENERIC
        self._session: _Session | None = None
        self._dispatcher = EventDispatcher()
        self._lastSignal: int | None = None
        self._lastNetwork: tuple[str, str | None] | None = None
        self._smsStop = threading.Event()
        self._sender = SmsSender(self._submitTransaction, self.options.deliveryReport, self.options.segmentPause, self._smsStop)
        self._outbox = Outbox(self._sender.send, self._dispatcher.post, self.options.smsTtl, self.options.smsQueueSize,
                              stopEvent=self._smsStop)
        self._inbox = Inbox(self._submitTransaction, self._dispatcher.post, self._readMemory,
                            Reassembler(self.options.concatPartsTtl, self.options.maxIncompleteSms))
        self._supervisor = Supervisor(
            connect=self._connect, disconnect=self._disconnect, publish=self._publishState, isFatal=self._isFatal,
            baseDelay=self.options.reconnectBaseDelay, maxDelay=self.options.reconnectMaxDelay,
            maxAttempts=self.options.reconnectMaxAttempts, monitor=self._monitor,
            monitorInterval=max(self.MIN_MONITOR_INTERVAL, self.options.monitorInterval))

    # ---- public API -------------------------------------------------------------------------------

    @property
    def state(self) -> str:
        """ Current connection state (see ``ConnectionState``) """
        return self._state

    @property
    def profile(self) -> Profile:
        """ Profile of the identified modem (generic until the first connection) """
        return self._profile

    def onEvent(self, callback: Callable[[Any], None]) -> None:
        """ Subscribes to the events (``StateChanged``, ``ModemIdentified``, ``SignalChanged``, ``NetworkChanged``,
        ``UnsolicitedNotification``).
        Callbacks run in a dedicated thread: a slow callback never blocks the modem. """
        self._dispatcher.subscribe(callback)

    def start(self) -> None:
        """ Starts the library. Does not block: connection and reconnection run in the background. """
        self._state = ConnectionState.CONNECTING
        self._dispatcher.start()
        self._outbox.start()
        self._supervisor.start()

    def stop(self) -> None:
        """ Stops the threads and closes the port """
        self._smsStop.set()  # ends the pauses between the parts of an SMS
        self._supervisor.requestStop()
        self._disconnect()  # fails the transaction in progress: the SMS being sent returns at once
        self._supervisor.join()
        self._disconnect()
        self._outbox.stop()  # the SMS still waiting fail ("daemon stopped")
        self._dispatcher.stop()  # delivers the last events, the final states of the SMS included

    def command(self, command: str, timeout: float = 10.0, parseError: bool = True) -> Future:
        """ Sends an AT command (diagnostic priority). The Future resolves to the response lines, or fails with
        ``CommandError`` / ``TimeoutException`` / ``NotConnectedError`` (not connected, e.g. while reconnecting). """
        session = self._session
        if self._state not in (ConnectionState.CONNECTED, ConnectionState.SEARCHING) or session is None:
            failed: Future = Future()
            failed.set_exception(NotConnectedError(f'Modem not connected (state: {self._state})'))
            return failed
        return session.executor.submit(command, timeout, Priority.CONSOLE, parseError)

    def sendSms(self, number: str, text: str, ref: str | None = None, maxPartsPerGroup: int = 0) -> str:
        """ Queues an SMS. Does not block: the result comes as an event, ``SmsSent`` or ``SmsFailed`` or ``SmsExpired``
        (``SmsQueued`` first when it cannot leave at once, e.g. while the modem reconnects).

        :param ref: the caller's own reference, given back untouched in the events
        :param maxPartsPerGroup: a long message is split into groups of at most that many linked parts (0 = one group)
        :return: the identifier of the SMS (``smsId`` of its events)
        :raise SmsQueueFullError: too many SMS are waiting, this one is refused
        :raise NotConnectedError: the connection was given up (or the library stopped) """
        if self._state == ConnectionState.DISCONNECTED:
            raise NotConnectedError('Modem disconnected')
        return self._outbox.submit(number, text, ref, maxPartsPerGroup)

    def _submitTransaction(self, transaction: Transaction) -> Future:
        """ Submits the transaction of an SMS part to the connection in use (failed Future when there is none) """
        session = self._session
        if self._state not in (ConnectionState.CONNECTED, ConnectionState.SEARCHING) or session is None:
            failed: Future = Future()
            failed.set_exception(NotConnectedError(f'Modem not connected (state: {self._state})'))
            return failed
        return session.executor.submitTransaction(transaction)

    # ---- connection (called by the Supervisor) ----------------------------------------------------

    @staticmethod
    def _isFatal(error: Exception) -> bool:
        """ Retrying cannot fix a missing or wrong PIN (and would lock the SIM), nor a modem without the PDU mode """
        return isinstance(error, (PinRequiredError, IncorrectPinError, PukRequiredError, PduModeNotSupportedError))

    def _publishState(self, state: str, details: dict[str, Any]) -> None:
        self._state = state
        log.info('State: %s %s', state, details or '')
        self._dispatcher.post(StateChanged(state, dict(details)))
        if state == ConnectionState.CONNECTED:
            self._outbox.retryNow()  # the SMS that wait are tried at once, not at the end of their delay
            session = self._session
            if session is not None and session.smsReady:
                self._inbox.catchUp()  # the SMS received while nobody was listening
        elif state == ConnectionState.DISCONNECTED:
            self._outbox.failAll('modem disconnected')  # the connection is given up: nobody will send them
        if state in (ConnectionState.CONNECTING, ConnectionState.RECONNECTING, ConnectionState.DISCONNECTED):
            self._publishSignal(-1)
            self._publishNetwork(Registration.UNKNOWN, None)

    def _publishSignal(self, value: int) -> None:
        if value != self._lastSignal:
            self._lastSignal = value
            self._dispatcher.post(SignalChanged(value))

    def _publishNetwork(self, registration: str, operator: str | None) -> None:
        if (registration, operator) != self._lastNetwork:
            self._lastNetwork = (registration, operator)
            self._dispatcher.post(NetworkChanged(registration, operator))

    # ---- monitoring (called by the Supervisor, from its own thread) -------------------------------

    def _monitor(self) -> str | None:
        """ Reads the signal and the network registration. :return: the registration, None if it could not be read """
        session = self._session
        if session is None:
            return None
        try:
            signal = self._readSignal(session)
            registration = self._readRegistration(session)
            operator = self._readOperator(session) if registration == Registration.REGISTERED else None
        except WwanException as e:
            log.debug('Monitoring failed: %s', e)
            return None
        if signal is not None:
            self._publishSignal(signal)
        if registration is not None:
            self._publishNetwork(registration, operator)
        self._monitorInbox(session)
        return registration

    def _monitorInbox(self, session: _Session) -> None:
        """ Gives up the long SMS that stay incomplete, and checks that the memory holds nothing readable: a
        notification may have been lost (the port was resynchronized...) """
        self._inbox.sweep()
        if not session.smsReady:
            return
        try:
            if self._inbox.checkMemory(self._query(session, 'AT+CPMS?')):
                log.info('An SMS is waiting in the memory without notification, reading it')
                self._inbox.catchUp()
        except WwanException as e:
            log.debug('SMS memory check failed: %s', e)

    def _query(self, session: _Session, command: str) -> list[str]:
        """ Monitoring command (lowest priority). Gives up as soon as the connection failed, so that a lost port
        does not delay the reconnection by the timeout of the command. """
        future = session.executor.submit(command, 5.0, Priority.SUPERVISION, maxHold=10.0)
        while True:
            try:
                return future.result(timeout=0.2)
            except TimeoutError:
                if self._supervisor.failed:
                    raise NotConnectedError('Connection lost') from None

    def _readSignal(self, session: _Session) -> int | None:
        match = lineMatching(r'^\+CSQ:\s*(\d+),', self._query(session, 'AT+CSQ'))
        if match is None:
            return None
        value = int(match.group(1))
        return -1 if value == 99 else value

    def _readRegistration(self, session: _Session) -> str | None:
        """ CS (+CREG) and PS / LTE (+CEREG) registrations: registered as soon as one of them is (1 home, 5 roaming) """
        statuses: list[int] = []
        for command, name in (('AT+CREG?', 'CREG'), ('AT+CEREG?', 'CEREG')):
            try:
                lines = self._query(session, command)
            except CommandError:
                continue  # not supported by this modem
            match = lineMatching(rf'^\+{name}:\s*\d+,(\d)', lines)
            if match:
                statuses.append(int(match.group(1)))
        if not statuses:
            return None
        if any(status in (1, 5) for status in statuses):
            return Registration.REGISTERED
        if 3 in statuses:
            return Registration.DENIED
        return Registration.SEARCHING

    def _readOperator(self, session: _Session) -> str | None:
        try:
            match = lineMatching(r'^\+COPS:\s*\d+,\d+,"([^"]*)"', self._query(session, 'AT+COPS?'))
        except CommandError:
            return None
        return match.group(1) if match else None

    def _readMemory(self) -> str | None:
        """ The memory selected for reading and deleting SMS, as the Executor knows it from the commands that succeeded """
        session = self._session
        memories = session.executor.cache.get('smsMemories') if session is not None else None
        return memories[0] if memories else None

    def _onNotification(self, lines: list[str]) -> None:
        """ Called by the Reader thread: it must never block nor fail """
        self._dispatcher.post(UnsolicitedNotification(lines))
        session = self._session
        if session is not None and session.smsReady:
            try:
                self._inbox.onNotification(lines)
            except Exception:
                log.exception('Error while handling a notification')

    def _connect(self, token: object) -> None:
        session = _Session()
        self._session = session
        self._inbox.reset()
        session.executor = Executor(
            write=lambda data: session.transport.write(data),
            onStuck=lambda reason: self._supervisor.reportFailure(token, reason), aging=self.options.aging)
        session.transport = Transport(
            self.port, self.baudrate, getContext=session.executor.context, onResponse=session.executor.onResponseLine,
            onNotification=self._onNotification,
            onLost=lambda error: self._supervisor.reportFailure(token, str(error)))
        session.transport.open()
        session.executor.waitReady(self.options.readyTimeout)
        session.executor.start()
        self._initialize(session)

    def _disconnect(self) -> None:
        session, self._session = self._session, None
        if session is None:
            return
        # The executor first: it fails the pending transactions, so nobody keeps waiting on a closing port
        session.executor.stop()
        session.transport.close()

    # ---- initialization sequence ------------------------------------------------------------------

    def _initialize(self, session: _Session) -> None:
        run = session.run


        pinChecked = False
        try:
            run('ATZ')
        except CommandError:
            # Some modems require the SIM PIN at this stage already
            log.warning('ATZ refused, unlocking the SIM first')
            run('AT+CMEE=1', parseError=False)
            self._unlockSim(run)
            pinChecked = True
            run('ATZ')
        run('ATE0')
        try:
            cfun = lineStartingWith('+CFUN:', run('AT+CFUN?'))
            if cfun and int(cfun[7:].split(',')[0]) != 1:
                run('AT+CFUN=1')
        except (CommandError, ValueError):
            pass  # AT+CFUN not supported
        run('AT+CMEE=1')  # ATZ may have reset it; the resynchronization marker relies on it
        if not pinChecked:
            self._unlockSim(run)

        manufacturer = self._identity(run('AT+CGMI')[0])
        model = self._identity(run('AT+CGMM')[0])
        try:
            revision: str | None = self._identity(run('AT+CGMR')[0])
        except CommandError:
            revision = None
        session.profile = detectProfile(manufacturer)
        session.transport.setUrcPrefixes(session.profile.urcPrefixes)
        self._profile = session.profile
        log.info('Modem identified: %s %s (%s), profile %s', manufacturer, model, revision, session.profile.name)
        self._dispatcher.post(ModemIdentified(session.profile.name, manufacturer, model, revision))

        run('AT+COPS=3,0', parseError=False)  # long alphanumeric operator name
        self._setupSmsFormat(run)
        self._setupSmsCenter(run)
        if self._selectSmsMemory(run):
            self._setupNotifications(run, session.profile)
            session.smsReady = True
        if self.options.force4g and session.profile.supportsForce4g:
            try:
                run('AT+CNMP=38')
                log.info('LTE-only network mode forced (AT+CNMP=38)')
            except WwanException as e:
                log.error('Failed to force the LTE-only network mode (AT+CNMP=38): %s', e)

    @staticmethod
    def _setupSmsFormat(run: Callable[..., list[str]]) -> None:
        """ SMS are always handled in PDU mode (DEC-36). A modem that says it does not offer it cannot be used: the
        connection stops with a clear reason instead of failing at the first SMS. An answer that cannot be read is
        no obstacle: AT+CMGF=0 tells. """
        try:
            answer = lineStartingWith('+CMGF', run('AT+CMGF=?'))
        except CommandError:
            answer = None
        modes = Modem._supportedModes(answer) if answer else None
        if modes is not None and 0 not in modes:
            raise PduModeNotSupportedError(f'The modem does not offer the SMS PDU mode ({answer})')
        run('AT+CMGF=0')

    @staticmethod
    def _supportedModes(answer: str) -> set[int] | None:
        """ Modes of ``+CMGF: (0,1)`` (some modems write a range, ``(0-1)``). None if the answer cannot be read. """
        match = re.search(r'\(([^)]*)\)', answer)
        if match is None:
            return None
        modes: set[int] = set()
        for item in match.group(1).split(','):
            item = item.strip()
            span = re.fullmatch(r'(\d+)-(\d+)', item)
            if span:
                modes.update(range(int(span.group(1)), int(span.group(2)) + 1))
            elif item.isdigit():
                modes.add(int(item))
            else:
                return None
        return modes or None

    @staticmethod
    def _identity(line: str) -> str:
        """ Some firmwares (SimCom) prefix the identification with the command name, e.g. '+CGMR: LE20B04SIM7600G22' """
        return re.sub(r'^\+CGM[IMR]:\s*', '', line)

    def _unlockSim(self, run: Callable[..., list[str]]) -> None:
        """ Enters the PIN if the SIM asks for it """
        try:
            cpin = lineStartingWith('+CPIN', run('AT+CPIN?', timeout=15.0))
        except TimeoutException as timeout:
            # Some modems (Wavecom) do not end the +CPIN response with OK
            cpin = lineStartingWith('+CPIN', timeout.data) if timeout.data else None
            if cpin is None:
                raise
        if cpin != '+CPIN: READY':
            if self._pin:
                run(f'AT+CPIN="{self._pin}"')
            else:
                raise PinRequiredError('AT+CPIN')

    @staticmethod
    def _readSmsc(run: Callable[..., list[str]]) -> str | None:
        try:
            lines = run('AT+CSCA?')
        except SmscNumberUnknownError:
            return None  # some modems answer CMS 330 when it is not set
        match = lineMatching(r'\+CSCA:\s*"([^,]+)",(\d+)$', lines)
        return match.group(1) if match else None

    def _setupSmsCenter(self, run: Callable[..., list[str]]) -> None:
        smsc: str | None = normalizeNumber(self.options.smsc) if self.options.smsc else None
        if self.options.smsc and smsc is None:
            # What is written in the AT command must be a phone number: nothing else reaches the modem
            log.error('SMS center %r is not a phone number (digits, optionally after a +): ignored, the one of the SIM is used',
                      self.options.smsc)
        if smsc is not None:
            try:
                run(f'AT+CSCA="{smsc}"')
            except CommandError as e:
                log.warning('SMS center %s refused by the modem (%s): the one of the SIM is kept', smsc, e)
                smsc = self._readSmsc(run)
        else:
            smsc = self._readSmsc(run)
        run('AT+CSMP=49,167,0,0' if self.options.deliveryReport else 'AT+CSMP=17,167,0,0', parseError=False)
        # Some modems erase the SMS center when the SMS parameters are set
        if smsc is not None and self._readSmsc(run) != smsc:
            run(f'AT+CSCA="{smsc}"')

    @staticmethod
    def _selectSmsMemory(run: Callable[..., list[str]]) -> bool:
        """ Selects SM (100 slots on SIM7600, against 23 for ME) for reading, writing and receiving, else ME.
        Nothing is purged here: SMS received while the daemon was stopped are still stored. """
        try:
            cpms = lineStartingWith('+CPMS', run('AT+CPMS=?'))
        except CommandError:
            cpms = None
        groups = re.findall(r'\(([^()]*)\)', cpms.split(':', 1)[1]) if cpms and ':' in cpms else []
        chosen: list[str] = []
        for group in groups:
            names = [name.strip().strip('"') for name in group.split(',')]
            memory = 'SM' if 'SM' in names else 'ME' if 'ME' in names else None
            if memory is None:
                chosen = []
                break
            chosen.append(memory)
        if not chosen:
            log.warning('SMS memory selection not supported by the modem (%s): SMS reading unavailable', cpms)
            return False
        Modem._logSmsMemoryUsage(run, groups[0])
        run('AT+CPMS=' + ','.join(f'"{memory}"' for memory in chosen))
        return True

    @staticmethod
    def _logSmsMemoryUsage(run: Callable[..., list[str]], readableMemories: str) -> None:
        """ Logs how full each SMS memory is (ME, SM, SR). Selecting a memory has no side effect: the final selection follows. """
        usage = []
        for memory in ('ME', 'SM', 'SR'):
            if f'"{memory}"' not in readableMemories:
                continue
            try:
                counts = lineMatching(r'^\+CPMS:\s*(\d+),(\d+)', run(f'AT+CPMS="{memory}"'))
            except CommandError:
                continue
            if counts:
                usage.append(f'{memory} {counts.group(1)}/{counts.group(2)}')
        if usage:
            log.info('SMS memory usage: %s', ', '.join(usage))

    @staticmethod
    def _setupNotifications(run: Callable[..., list[str]], profile: Profile) -> None:
        for cnmi in profile.cnmiCandidates:
            try:
                run('AT+CNMI=' + cnmi)
                log.info('Message notifications set with AT+CNMI=%s', cnmi)
                return
            except CommandError:
                continue
        try:
            run('AT+CNMI=2,1,0,1,0')
            log.info('Message notifications set with AT+CNMI=2,1,0,1,0')
        except CommandError:
            log.warning('Incoming SMS notifications not supported by the modem: SMS receiving unavailable')
