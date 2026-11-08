""" Tests of the check of the port where a Quectel modem sends its notifications (received SMS, delivery reports).

Run from resources/sms4gd:  python -m unittest tests.test_notificationport
"""

import time
import unittest
from unittest import mock

from dispatcher import notificationPortMessage
from tests.test_wwanlib import FakeSerial, answer, simcomTable, waitFor
from wwanlib import ConnectionState, Modem, ModemOptions, NotificationPortChecked
from wwanlib.executor import Executor

QUECTEL = {'AT+CGMI': 'Quectel\r\nOK\r\n', 'AT+CNMI=2,1,0,1': 'OK\r\n', 'AT+QCFG="nwscanmode"': '+QCFG: "nwscanmode",0\r\nOK\r\n'}
# Not absolute: the Supervisor only checks that an absolute port exists
BY_ID = 'usb-Quectel_EC25-EUX_18f85173-if{:02x}-port0'


def urcPort(name: str) -> dict:
    return {'AT+QURCCFG="urcport"': f'+QURCCFG: "urcport","{name}"\r\nOK\r\n'}


class NotificationPortCheckTest(unittest.TestCase):
    def setUp(self):
        FakeSerial.instances.clear()
        patcher = mock.patch('wwanlib.transport.serial.Serial', FakeSerial)
        patcher.start()
        self.addCleanup(patcher.stop)
        for owner, name, value in ((Modem, 'MIN_MONITOR_INTERVAL', 0.05), (Executor, 'READY_INTERVAL', 0.1)):
            patcher = mock.patch.object(owner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.events: list = []

    def connect(self, port: str, **table) -> list[str]:
        FakeSerial.behavior = answer(simcomTable(**table), echo=True)
        modem = Modem(port, 115200, options=ModemOptions(reconnectBaseDelay=0.05, reconnectMaxDelay=0.1, reconnectMaxAttempts=3))
        modem.onEvent(self.events.append)
        self.addCleanup(modem.stop)
        modem.start()
        self.assertTrue(waitFor(lambda: modem.state == ConnectionState.CONNECTED))
        return FakeSerial.instances[0].commands()

    def checks(self) -> list[NotificationPortChecked]:
        return [event for event in list(self.events) if isinstance(event, NotificationPortChecked)]

    def assertNothingTold(self):
        time.sleep(0.2)  # the events are delivered by their own thread
        self.assertEqual(self.checks(), [])

    def testTheWrongPortIsToldWithTheInterfaceToUse(self):
        with self.assertLogs('wwanlib', level='WARNING') as logs:
            commands = self.connect(BY_ID.format(3), **QUECTEL, **urcPort('usbat'))
            self.assertTrue(waitFor(self.checks))
        self.assertEqual(self.checks(), [NotificationPortChecked(False, 'usbat', 2)])
        self.assertIn('not to the serial port in use (usbmodem)', '\n'.join(logs.output))
        self.assertFalse([command for command in commands if command.startswith('AT+QURCCFG="urcport",')])  # read only

    def testTheRightPortIsToldOnTheAtPort(self):
        self.connect(BY_ID.format(2), **QUECTEL, **urcPort('usbat'))
        self.assertTrue(waitFor(self.checks))
        self.assertEqual(self.checks(), [NotificationPortChecked(True, 'usbat')])

    def testTheRightPortIsToldOnTheModemPortWhateverTheCaseOfTheAnswer(self):
        self.connect(BY_ID.format(3), **QUECTEL, **urcPort('USBMODEM'))
        self.assertTrue(waitFor(self.checks))
        self.assertEqual(self.checks(), [NotificationPortChecked(True, 'usbmodem')])

    def testNothingIsToldForAPortWithoutItsInterfaceInTheName(self):
        commands = self.connect('ttyUSB3', **QUECTEL, **urcPort('usbat'))
        self.assertIn('AT+QURCCFG="urcport"', commands)  # still read, for the log
        self.assertNothingTold()

    def testNothingIsToldForAnInterfaceThatIsNotAtOrModem(self):
        self.connect(BY_ID.format(1), **QUECTEL, **urcPort('usbat'))
        self.assertNothingTold()

    def testNothingIsToldWhenTheNotificationsGoToAnotherKindOfPort(self):
        self.connect(BY_ID.format(3), **QUECTEL, **urcPort('uart1'))
        self.assertNothingTold()

    def testNothingIsToldWhenTheAnswerCannotBeRead(self):
        for answerText in ('OK\r\n', '+QURCCFG: urcport\r\nOK\r\n'):
            with self.subTest(answerText):
                FakeSerial.instances.clear()
                self.connect(BY_ID.format(3), **QUECTEL, **{'AT+QURCCFG="urcport"': answerText})
                self.assertNothingTold()

    def testNothingIsToldWhenTheModemRefusesTheReading(self):
        self.connect(BY_ID.format(3), **QUECTEL, **{'AT+QURCCFG="urcport"': 'ERROR\r\n'})
        self.assertNothingTold()

    def testOtherModemsAreNotAsked(self):
        commands = self.connect(BY_ID.format(3))  # SimCom
        self.assertNotIn('AT+QURCCFG="urcport"', commands)
        self.assertNothingTold()


class NotificationPortMessageTest(unittest.TestCase):
    def testTheMessageForJeedom(self):
        self.assertEqual(notificationPortMessage(NotificationPortChecked(False, 'usbat', 2)),
                         {'type': 'notificationPort', 'matches': False, 'modemPort': 'usbat', 'advisedInterface': 2})
        self.assertEqual(notificationPortMessage(NotificationPortChecked(True, 'usbat'))['matches'], True)
        self.assertIsNone(notificationPortMessage(object()))


if __name__ == '__main__':
    unittest.main()
