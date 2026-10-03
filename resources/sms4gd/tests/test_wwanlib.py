""" Tests of wwanlib against a fake serial port (no modem needed).

Run from resources/sms4gd:  python -m unittest discover -s tests -t .

The fake port only plays scripted answers: it is not a modem simulator, it checks the line classification,
the transactions, the timeouts and the connection logic of the library.
"""

import threading
import time
import unittest
from unittest import mock

import serial

from wwanlib import ConnectionState, Modem, ModemIdentified, ModemOptions, StateChanged
from wwanlib.exceptions import CmsError, CommandError, NotConnectedError, PinRequiredError, TimeoutException
from wwanlib.executor import CTRL_Z, Executor, Priority, Step, Transaction
from wwanlib.transport import LineClassifier, LineKind, Transport


class FakeSerial:
    """ In-memory replacement of serial.Serial. ``behavior(fake, data)`` is called on every write. """

    instances: list['FakeSerial'] = []
    behavior = None

    def __init__(self, **kwargs):
        self._rx = bytearray()
        self._cond = threading.Condition()
        self._cancel = False
        self.unplugged = False
        self.closed = False
        self.written: list[tuple[float, bytes]] = []
        FakeSerial.instances.append(self)

    @property
    def in_waiting(self) -> int:
        return len(self._rx)

    def reset_input_buffer(self):
        pass

    def feed(self, data: bytes):
        with self._cond:
            self._rx.extend(data)
            self._cond.notify_all()

    def feedLater(self, delay: float, data: bytes):
        timer = threading.Timer(delay, self.feed, args=(data,))
        timer.daemon = True
        timer.start()

    def unplug(self):
        with self._cond:
            self.unplugged = True
            self._cond.notify_all()

    def read(self, size=1) -> bytes:
        with self._cond:
            if not self._rx and not self.unplugged and not self._cancel:
                self._cond.wait(0.05)
            if self.unplugged:
                raise serial.SerialException('device disconnected')
            self._cancel = False
            data = bytes(self._rx[:size])
            del self._rx[:size]
            return data

    def cancel_read(self):
        with self._cond:
            self._cancel = True
            self._cond.notify_all()

    def write(self, data: bytes):
        if self.unplugged:
            raise serial.SerialException('device disconnected')
        self.written.append((time.monotonic(), data))
        if FakeSerial.behavior:
            FakeSerial.behavior(self, data)

    def close(self):
        self.closed = True

    def commands(self) -> list[str]:
        return [data.decode().strip() for _, data in self.written]


def answer(table: dict, echo: bool = False):
    """ Behavior answering from a table {command: response text} (callables get the fake port) """

    def behavior(fake: FakeSerial, data: bytes):
        command = data.decode().rstrip('\r')
        if echo:
            fake.feed(command.encode() + b'\r\n')
        response = table.get(command)
        if callable(response):
            response(fake)
        elif response is not None:
            fake.feed(response.encode())

    return behavior


def waitFor(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class ClassifierTest(unittest.TestCase):
    def setUp(self):
        self.c = LineClassifier()

    def test_always_spontaneous_even_during_a_command(self):
        for line in ('+CMTI: "SM",3', '+CDSI: "SM",4', 'RING', 'RDY', 'SMS DONE', '+CLIP: "+33600000000",145'):
            self.assertEqual(self.c.classify(line, 'AT+CSQ'), LineKind.NOTIFICATION, line)

    def test_response_when_a_command_is_in_progress(self):
        self.assertEqual(self.c.classify('+CSQ: 20,99', 'AT+CSQ'), LineKind.RESPONSE)
        self.assertEqual(self.c.classify('OK', 'AT+CSQ'), LineKind.RESPONSE)

    def test_notification_when_no_command(self):
        self.assertEqual(self.c.classify('+CSQ: 20,99', None), LineKind.NOTIFICATION)

    def test_ambiguous_prefix_depends_on_the_command(self):
        self.assertEqual(self.c.classify('+CPIN: READY', 'AT+CPIN?'), LineKind.RESPONSE)
        self.assertEqual(self.c.classify('+CPIN: READY', 'AT+CSQ'), LineKind.NOTIFICATION)
        self.assertEqual(self.c.classify('+CPIN: READY', None), LineKind.NOTIFICATION)
        self.assertEqual(self.c.classify('+CREG: 0,1', 'AT+CREG?'), LineKind.RESPONSE)
        self.assertEqual(self.c.classify('+CREG: 1', 'AT+CSQ'), LineKind.NOTIFICATION)

    def test_caret_notification_unless_it_answers_the_command(self):
        self.assertEqual(self.c.classify('^RSSI:20', 'AT+CSQ'), LineKind.NOTIFICATION)
        self.assertEqual(self.c.classify('^SYSINFO:2,3,0,5,1', 'AT^SYSINFO'), LineKind.RESPONSE)

    def test_echo_is_ignored(self):
        self.assertEqual(self.c.classify('AT+CSQ', 'AT+CSQ'), LineKind.IGNORE)

    def test_cds_pdu_continuation(self):
        self.assertEqual(self.c.classify('+CDS: 25', 'AT+CSQ'), LineKind.NOTIFICATION)
        self.assertTrue(self.c.expectingContinuation)
        # A response interleaved before the PDU stays a response
        self.assertEqual(self.c.classify('+CSQ: 20,99', 'AT+CSQ'), LineKind.RESPONSE)
        self.assertEqual(self.c.classify('0791530000000000', 'AT+CSQ'), LineKind.NOTIFICATION)
        self.assertFalse(self.c.expectingContinuation)

    def test_profile_prefixes(self):
        self.c.urcPrefixes = ('^BOOT',)
        self.assertEqual(self.c.classify('^BOOT:1,2', 'AT+CSQ'), LineKind.NOTIFICATION)


class ExecutorTestCase(unittest.TestCase):
    """ Transport + Executor on a fake port """

    def setUp(self):
        FakeSerial.instances.clear()
        FakeSerial.behavior = None
        patcher = mock.patch('wwanlib.transport.serial.Serial', FakeSerial)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.notifications: list[list[str]] = []
        self.lost: list[Exception] = []
        self.stuck: list[str] = []
        self.executor = Executor(write=lambda data: self.transport.write(data), onStuck=self.stuck.append)
        self.transport = Transport('fake', 115200, getContext=self.executor.context, onResponse=self.executor.onResponseLine,
                                   onNotification=self.notifications.append, onLost=self.lost.append)
        self.transport.open()
        self.executor.start()
        self.fake = FakeSerial.instances[0]
        self.addCleanup(self.close)

    def close(self):
        self.executor.stop()
        self.transport.close()


class ExecutorTest(ExecutorTestCase):
    def test_simple_command_with_echo(self):
        FakeSerial.behavior = answer({'AT+CSQ': '\r\n+CSQ: 20,99\r\n\r\nOK\r\n'}, echo=True)
        self.assertEqual(self.executor.submit('AT+CSQ').result(2), ['+CSQ: 20,99', 'OK'])

    def test_errors(self):
        FakeSerial.behavior = answer({'AT+A': 'ERROR\r\n', 'AT+B': '+CME ERROR: 11\r\n', 'AT+C': '+CMS ERROR: 322\r\n',
                                      'AT+D': 'COMMAND NOT SUPPORT\r\n'})
        with self.assertRaises(CommandError):
            self.executor.submit('AT+A').result(2)
        with self.assertRaises(PinRequiredError):
            self.executor.submit('AT+B').result(2)
        with self.assertRaises(CmsError) as ctx:
            self.executor.submit('AT+C').result(2)
        self.assertEqual(ctx.exception.code, 322)
        with self.assertRaises(CommandError):
            self.executor.submit('AT+D').result(2)
        self.assertEqual(self.executor.submit('AT+A', parseError=False).result(2), ['ERROR'])

    def test_pin_is_masked_in_errors(self):
        FakeSerial.behavior = answer({'AT+CPIN="1234"': '+CME ERROR: 16\r\n'})
        with self.assertRaises(CommandError) as ctx:
            self.executor.submit('AT+CPIN="1234"').result(2)
        self.assertNotIn('1234', str(ctx.exception.command))

    def test_notification_during_a_command_is_not_mixed_in_the_response(self):
        FakeSerial.behavior = answer({'AT+CSQ': '+CMTI: "SM",1\r\n+CSQ: 20,99\r\nOK\r\n'})
        self.assertEqual(self.executor.submit('AT+CSQ').result(2), ['+CSQ: 20,99', 'OK'])
        self.assertTrue(waitFor(lambda: self.notifications))
        self.assertEqual(self.notifications[0], ['+CMTI: "SM",1'])

    def test_priority_orders_the_waiting_transactions(self):
        order: list[str] = []

        def behavior(fake, data):
            order.append(data.decode().strip())
            fake.feed(b'OK\r\n')

        FakeSerial.behavior = behavior
        first = self.executor.submit('AT+FIRST', priority=Priority.CONTROL)
        low = self.executor.submit('AT+LOW', priority=Priority.SUPERVISION)
        high = self.executor.submit('AT+HIGH', priority=Priority.CONTROL)
        for future in (first, low, high):
            future.result(2)
        self.assertLess(order.index('AT+HIGH'), order.index('AT+LOW'))

    def test_timeout_releases_the_caller_but_not_the_port(self):
        slow = 'AT+COPS=?'
        FakeSerial.behavior = answer({
            'AT+CSQ': '+CSQ: 20,99\r\nOK\r\n',
            slow: lambda fake: fake.feedLater(0.8, b'+COPS: (1,"X","X","20801")\r\nOK\r\n'),
        })
        started = time.monotonic()
        a = self.executor.submit(slow, timeout=0.3)
        b = self.executor.submit('AT+CSQ')
        with self.assertRaises(TimeoutException):
            a.result(2)
        self.assertLess(time.monotonic() - started, 0.7)  # the caller did not wait for the late answer
        # B gets its own answer, not the late one of A, and is written only after A ended
        self.assertEqual(b.result(3), ['+CSQ: 20,99', 'OK'])
        self.assertGreater(time.monotonic() - started, 0.7)  # coarse Windows clock: the late answer comes at 0.8 s
        self.assertEqual(self.fake.commands(), [slow, 'AT+CSQ'])

    def test_resynchronization_marker_after_the_hold_limit(self):
        FakeSerial.behavior = answer({'AT+CMEE?': '+CMEE: 1\r\nOK\r\n', 'AT+CSQ': '+CSQ: 20,99\r\nOK\r\n'})
        a = self.executor.submit('AT+NOANSWER', timeout=0.2, maxHold=0.4)
        b = self.executor.submit('AT+CSQ')
        with self.assertRaises(TimeoutException):
            a.result(2)
        self.assertEqual(b.result(3), ['+CSQ: 20,99', 'OK'])
        self.assertEqual(self.fake.commands(), ['AT+NOANSWER', 'AT+CMEE?', 'AT+CSQ'])
        self.assertEqual(self.stuck, [])

    def test_modem_not_answering_the_marker_is_stuck(self):
        self.executor.MARKER_TIMEOUT = 0.3
        a = self.executor.submit('AT+NOANSWER', timeout=0.2, maxHold=0.3)
        b = self.executor.submit('AT+CSQ')
        with self.assertRaises(TimeoutException):
            a.result(2)
        with self.assertRaises(NotConnectedError):
            b.result(3)
        self.assertTrue(waitFor(lambda: self.stuck))
        with self.assertRaises(NotConnectedError):
            self.executor.submit('AT').result(1)

    def test_prompt_transaction(self):
        def behavior(fake, data):
            text = data.decode()
            if text.startswith('AT+CMGS'):
                fake.feed(b'\r\n> ')
            elif text.endswith(CTRL_Z):
                fake.feed(b'\r\n+CMGS: 7\r\n\r\nOK\r\n')

        FakeSerial.behavior = behavior
        transaction = Transaction([Step('AT+CMGS=5', expectPrompt=True), Step('00110000', terminator=CTRL_Z, timeout=5)])
        self.assertEqual(self.executor.submitTransaction(transaction).result(2), ['+CMGS: 7', 'OK'])
        self.assertEqual([data for _, data in self.fake.written], [b'AT+CMGS=5\r', b'00110000\x1a'])

    def test_prompt_timeout_sends_escape(self):
        FakeSerial.behavior = answer({'AT+CMEE?': '+CMEE: 1\r\nOK\r\n'})
        transaction = Transaction([Step('AT+CMGS=5', timeout=0.2, expectPrompt=True), Step('00', terminator=CTRL_Z)])
        with self.assertRaises(TimeoutException):
            self.executor.submitTransaction(transaction).result(2)
        self.assertTrue(waitFor(lambda: any(data == b'\x1b' for _, data in self.fake.written)))

    def test_busy_error_is_retried(self):
        calls = []

        def behavior(fake, data):
            calls.append(data)
            fake.feed(b'+CME ERROR: 515\r\n' if len(calls) == 1 else b'OK\r\n')

        FakeSerial.behavior = behavior
        self.assertEqual(self.executor.submit('AT+X').result(3), ['OK'])
        self.assertEqual(len(calls), 2)

    def test_cache_follows_the_succeeded_commands(self):
        FakeSerial.behavior = answer({'AT+CMGF=0': 'OK\r\n', 'AT+CPMS="SM","SM","SM"': '+CPMS: 0,100,0,100,0,100\r\nOK\r\n'})
        self.executor.submit('AT+CMGF=0').result(2)
        self.executor.submit('AT+CPMS="SM","SM","SM"').result(2)
        self.assertEqual(self.executor.cache, {'smsMode': 0, 'smsMemories': ('SM', 'SM', 'SM')})

    def test_lost_port_is_signaled_once_and_fails_the_command(self):
        self.fake.unplug()
        self.assertTrue(waitFor(lambda: self.lost))
        self.assertEqual(len(self.lost), 1)
        with self.assertRaises(NotConnectedError):
            self.executor.submit('AT').result(2)

    def test_stop_fails_pending_transactions(self):
        pending = self.executor.submit('AT+WAIT', timeout=30)
        time.sleep(0.2)
        self.executor.stop()
        with self.assertRaises(NotConnectedError):
            pending.result(2)


def simcomTable(**overrides) -> dict:
    table = {
        'AT': 'OK\r\n', 'ATZ': 'OK\r\n', 'ATE0': 'OK\r\n', 'AT+CFUN?': '+CFUN: 1\r\nOK\r\n', 'AT+CFUN=1': 'OK\r\n',
        'AT+CMEE=1': 'OK\r\n', 'AT+CPIN?': '+CPIN: READY\r\nOK\r\n', 'AT+CGMI': 'SIMCOM INCORPORATED\r\nOK\r\n',
        'AT+CGMM': 'SIMCOM_SIM7600G-H\r\nOK\r\n', 'AT+CGMR': '+CGMR: LE20B04SIM7600G22\r\nOK\r\n', 'AT+COPS=3,0': 'OK\r\n',
        'AT+CMGF=0': 'OK\r\n', 'AT+CSCA?': '+CSCA: "+33695000695",145\r\nOK\r\n', 'AT+CSMP=17,167,0,0': 'OK\r\n',
        'AT+CPMS=?': '+CPMS: ("ME","SM","MT"),("ME","SM","MT"),("ME","SM","MT")\r\nOK\r\n',
        'AT+CPMS="SM","SM","SM"': '+CPMS: 0,100,0,100,0,100\r\nOK\r\n',
        'AT+CNMI=1,1,0,1': 'ERROR\r\n', 'AT+CNMI=0,1,0,1': 'OK\r\n', 'AT+CNMP=38': 'OK\r\n',
        'AT+CSQ': '+CSQ: 20,99\r\nOK\r\n',
    }
    table.update(overrides)
    return table


class ModemTest(unittest.TestCase):
    def setUp(self):
        FakeSerial.instances.clear()
        FakeSerial.behavior = answer(simcomTable(), echo=True)
        patcher = mock.patch('wwanlib.transport.serial.Serial', FakeSerial)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.events: list = []

    def makeModem(self, pin: str | None = None, **optionArgs) -> Modem:
        options = ModemOptions(reconnectBaseDelay=0.05, reconnectMaxDelay=0.1, reconnectMaxAttempts=3, **optionArgs)
        modem = Modem('fake', 115200, pin=pin, options=options)
        modem.onEvent(self.events.append)
        self.addCleanup(modem.stop)
        return modem

    def states(self) -> list[str]:
        return [event.state for event in self.events if isinstance(event, StateChanged)]

    def test_initialization_on_a_simcom(self):
        modem = self.makeModem(force4g=True)
        modem.start()
        self.assertTrue(waitFor(lambda: modem.state == ConnectionState.CONNECTED))
        identified = [event for event in self.events if isinstance(event, ModemIdentified)]
        self.assertEqual(identified[0].profile, 'simcom')
        self.assertEqual(identified[0].revision, 'LE20B04SIM7600G22')
        commands = FakeSerial.instances[0].commands()
        # CNMI cascade: the first candidate is refused, the second accepted and the cascade stops there
        self.assertIn('AT+CNMI=1,1,0,1', commands)
        self.assertIn('AT+CNMI=0,1,0,1', commands)
        self.assertNotIn('AT+CNMI=2,1,2,1', commands)
        # No purge of the SMS memory, SM selected, LTE forced
        self.assertFalse([c for c in commands if c.startswith('AT+CMGD')])
        self.assertIn('AT+CPMS="SM","SM","SM"', commands)
        self.assertIn('AT+CNMP=38', commands)
        self.assertEqual(modem.command('AT+CSQ').result(2), ['+CSQ: 20,99', 'OK'])

    def test_command_when_not_connected(self):
        modem = self.makeModem()
        with self.assertRaises(NotConnectedError):
            modem.command('AT').result(1)

    def test_pin_is_sent_and_never_logged_in_clear(self):
        FakeSerial.behavior = answer(simcomTable(**{'AT+CPIN?': '+CPIN: SIM PIN\r\nOK\r\n', 'AT+CPIN="1234"': 'OK\r\n'}))
        modem = self.makeModem(pin='1234')
        with self.assertLogs('wwanlib', level='DEBUG') as logs:
            modem.start()
            self.assertTrue(waitFor(lambda: modem.state == ConnectionState.CONNECTED))
        self.assertIn('AT+CPIN="1234"', FakeSerial.instances[0].commands())
        self.assertFalse([line for line in logs.output if '1234' in line])

    def test_missing_pin_is_fatal(self):
        FakeSerial.behavior = answer(simcomTable(**{'AT+CPIN?': '+CPIN: SIM PIN\r\nOK\r\n'}))
        modem = self.makeModem()
        modem.start()
        self.assertTrue(waitFor(lambda: modem.state == ConnectionState.DISCONNECTED))
        self.assertEqual(len(FakeSerial.instances), 1)  # no new attempt
        last = [event for event in self.events if isinstance(event, StateChanged)][-1]
        self.assertTrue(last.details['fatal'])

    def test_reconnection_after_the_port_is_lost(self):
        modem = self.makeModem()
        modem.start()
        self.assertTrue(waitFor(lambda: modem.state == ConnectionState.CONNECTED))
        FakeSerial.instances[0].unplug()
        self.assertTrue(waitFor(lambda: len(FakeSerial.instances) == 2 and self.states()[-1] == ConnectionState.CONNECTED))
        self.assertEqual(self.states(), ['connecting', 'connected', 'reconnecting', 'connected'])
        reconnecting = [event for event in self.events if isinstance(event, StateChanged) and event.state == 'reconnecting']
        self.assertEqual(reconnecting[0].details, {'attempt': 1, 'maxAttempts': 3})
        self.assertEqual(modem.command('AT+CSQ').result(2), ['+CSQ: 20,99', 'OK'])

    def test_gives_up_after_the_maximum_number_of_attempts(self):
        def opener(**kwargs):
            raise serial.SerialException('no such device')

        with mock.patch('wwanlib.transport.serial.Serial', opener):
            modem = self.makeModem()
            modem.start()
            self.assertTrue(waitFor(lambda: modem.state == ConnectionState.DISCONNECTED))
        self.assertEqual(self.states(), ['connecting', 'reconnecting', 'reconnecting', 'reconnecting', 'disconnected'])

    def test_stop_is_prompt_and_closes_the_port(self):
        modem = self.makeModem()
        modem.start()
        self.assertTrue(waitFor(lambda: modem.state == ConnectionState.CONNECTED))
        started = time.monotonic()
        modem.stop()
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(FakeSerial.instances[-1].closed)

    def test_stop_during_initialization(self):
        FakeSerial.behavior = None  # the modem never answers
        modem = self.makeModem()
        modem.start()
        self.assertTrue(waitFor(lambda: FakeSerial.instances))
        started = time.monotonic()
        modem.stop()
        self.assertLess(time.monotonic() - started, 3.0)


if __name__ == '__main__':
    unittest.main()
