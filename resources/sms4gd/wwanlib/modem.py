""" The Modem class: public facade of wwanlib, and the initialization sequence run at each connection """

import logging
import re
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any

from .events import (ConnectionState, EventDispatcher, ModemIdentified, NetworkChanged, Registration, SignalChanged,
                     StateChanged, UnsolicitedNotification)
from .exceptions import (CommandError, IncorrectPinError, NotConnectedError, PinRequiredError, PukRequiredError,
                         SmscNumberUnknownError, TimeoutException, WwanException)
from .executor import Executor, Priority
from .profiles import GENERIC, Profile, detectProfile
from .supervisor import Supervisor
from .transport import Transport
from .util import lineMatching, lineStartingWith

log = logging.getLogger(__name__)


@dataclass
class ModemOptions:
    textMode: bool = False  # SMS text mode instead of PDU mode
    deliveryReport: bool = False  # ask the SMS center for delivery reports
    smsc: str | None = None  # SMS center number, None to keep the one of the SIM
    force4g: bool = False  # LTE only (SimCom modems only)
    reconnectBaseDelay: float = 5.0
    reconnectMaxDelay: float = 300.0
    reconnectMaxAttempts: int = 10
    monitorInterval: float = 30.0  # seconds between two readings of the signal and of the network (5 at least)


class _Session:
    """ Everything that lives for the duration of one connection """

    def __init__(self):
        self.transport: Transport
        self.executor: Executor
        self.profile: Profile = GENERIC

    def run(self, command: str, timeout: float = 10.0, parseError: bool = True, maxHold: float = 180.0) -> list[str]:
        """ Runs an initialization command (highest priority) and waits for its result """
        return self.executor.submit(command, timeout, Priority.CONTROL, parseError, maxHold).result()


class Modem:
    """ A modem driven by AT commands.

    Usage::

        modem = Modem('/dev/ttyUSB2', 115200, pin=None, options=ModemOptions(textMode=False))
        modem.onEvent(callback)   # StateChanged, ModemIdentified, SignalChanged, NetworkChanged, UnsolicitedNotification
        modem.start()             # does not block: the Supervisor connects (and reconnects) in the background
        lines = modem.command('AT+CSQ', timeout=10).result()
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
        self._supervisor.start()

    def stop(self) -> None:
        """ Stops the threads and closes the port """
        self._supervisor.requestStop()
        self._disconnect()
        self._supervisor.join()
        self._disconnect()
        self._dispatcher.stop()

    def command(self, command: str, timeout: float = 10.0, parseError: bool = True) -> Future:
        """ Sends an AT command (diagnostic priority). The Future resolves to the response lines, or fails with
        ``CommandError`` / ``TimeoutException`` / ``NotConnectedError`` (not connected, e.g. while reconnecting). """
        session = self._session
        if self._state not in (ConnectionState.CONNECTED, ConnectionState.SEARCHING) or session is None:
            failed: Future = Future()
            failed.set_exception(NotConnectedError(f'Modem not connected (state: {self._state})'))
            return failed
        return session.executor.submit(command, timeout, Priority.CONSOLE, parseError)

    # ---- connection (called by the Supervisor) ----------------------------------------------------

    @staticmethod
    def _isFatal(error: Exception) -> bool:
        """ Retrying cannot fix a missing or wrong PIN, and would lock the SIM """
        return isinstance(error, (PinRequiredError, IncorrectPinError, PukRequiredError))

    def _publishState(self, state: str, details: dict[str, Any]) -> None:
        self._state = state
        log.info('State: %s %s', state, details or '')
        self._dispatcher.post(StateChanged(state, dict(details)))
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
        return registration

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

    def _connect(self, token: object) -> None:
        session = _Session()
        self._session = session
        session.executor = Executor(
            write=lambda data: session.transport.write(data),
            onStuck=lambda reason: self._supervisor.reportFailure(token, reason))
        session.transport = Transport(
            self.port, self.baudrate, getContext=session.executor.context, onResponse=session.executor.onResponseLine,
            onNotification=lambda lines: self._dispatcher.post(UnsolicitedNotification(lines)),
            onLost=lambda error: self._supervisor.reportFailure(token, str(error)))
        session.transport.open()
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

        # The modem may still be starting: a single AT, answered as soon as it is ready
        run('AT', timeout=5.0, maxHold=5.0)

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
        run(f'AT+CMGF={1 if self.options.textMode else 0}')
        self._setupSmsCenter(run)
        if self._selectSmsMemory(run):
            self._setupNotifications(run, session.profile)
        if self.options.force4g and session.profile.supportsForce4g:
            try:
                run('AT+CNMP=38')
                log.info('LTE-only network mode forced (AT+CNMP=38)')
            except WwanException as e:
                log.error('Failed to force the LTE-only network mode (AT+CNMP=38): %s', e)

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
        if self.options.smsc:
            run(f'AT+CSCA="{self.options.smsc}"')
            smsc: str | None = self.options.smsc
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
