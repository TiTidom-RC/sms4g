""" Tests of the self-test (J6, step 3): the modem sends an SMS to its own SIM and checks that it comes back with its
delivery report. Run from resources/sms4gd:  python -m unittest discover -s tests -t .
"""

import threading
import time
import unittest
from unittest import mock

from tests.test_inbox import FakeSim, deliverPdus, simBehavior, statusReportPdu
from tests.test_wwanlib import FakeSerial, simcomTable, waitFor
from wwanlib import (ConnectionState, Modem, ModemOptions, NotConnectedError, SelfTestResult, SmsDelivery, SmsFailed,
                     SmsQueued, SmsReceived, SmsSent, StateChanged)
from wwanlib.events import SmsExpired
from wwanlib.executor import CTRL_Z, Executor
from wwanlib.pdu import decodeSmsPdu
from wwanlib.selftest import NOT_CONNECTED, REF_PREFIX, TEXT_PREFIX, SelfTest

OWN = '+33767923801'


class Scenario:
    """ What the network does with the SMS of the test: each part can be left out """

    def __init__(self, sent=True, received=True, report=True, failed=None):
        self.sent, self.received, self.report, self.failed = sent, received, report, failed


class SelfTestUnitTest(unittest.TestCase):
    def setUp(self):
        self.results: list[SelfTestResult] = []
        self.submitted: list[tuple[str, str, str]] = []
        self.connected = True
        self.scenario = Scenario()
        self.restarts = 0
        self.restartError: Exception | None = None

    def submit(self, number, text, ref):
        self.submitted.append((number, text, ref))
        threading.Timer(0.05, self.play, args=(text, ref)).start()
        return 'abc'

    def play(self, text, ref):
        scenario = self.scenario
        selfTest = self.selfTest
        if scenario.failed is not None:
            selfTest.intercept(SmsFailed('abc', ref, OWN, scenario.failed))
            return
        if scenario.sent:
            selfTest.intercept(SmsSent('abc', ref, OWN, 1, (7,)))
        if scenario.received:
            selfTest.intercept(SmsReceived(OWN, text, None, 1))
        if scenario.report:
            selfTest.intercept(SmsDelivery('abc', ref, OWN, 'delivered', '', 1, 1))

    def restart(self):
        if self.restartError is not None:
            raise self.restartError
        self.restarts += 1

    def make(self, **kwargs) -> SelfTest:
        options = dict(ownNumber=OWN, expectReceipt=True, timeout=0.4, sendTimeout=0.4, minRestartInterval=3600.0)
        options.update(kwargs)
        self.selfTest = SelfTest(self.submit, self.results.append, lambda: self.connected, self.restart, **options)
        self.addCleanup(self.selfTest.stop)
        return self.selfTest

    def testEverythingComesBack(self):
        result = self.make().execute()
        self.assertEqual((result.status, result.reason, result.restarted), ('ok', '', False))
        number, text, ref = self.submitted[0]
        self.assertEqual(number, OWN)
        self.assertRegex(text, r'^sms4g test [0-9a-f]{8}$')
        self.assertTrue(ref.startswith(REF_PREFIX + ':'))
        self.assertEqual(self.results, [result])
        self.assertEqual(self.restarts, 0)

    def testTheSmsThatDoesNotComeBack(self):
        self.scenario = Scenario(received=False)
        result = self.make().execute()
        self.assertEqual(result.status, 'noReception')

    def testTheDeliveryReportThatDoesNotCome(self):
        self.scenario = Scenario(report=False)
        result = self.make().execute()
        self.assertEqual(result.status, 'noReceipt')
        self.assertIn('delivery report', result.reason)

    def testWithoutReportsOnlyTheReceptionIsTested(self):
        self.scenario = Scenario(report=False)
        self.assertEqual(self.make(expectReceipt=False).execute().status, 'ok')

    def testAReportBeforeTheSentEventIsNotLost(self):
        selfTest = self.make()

        def submit(number, text, ref):
            selfTest.intercept(SmsDelivery('abc', ref, OWN, 'delivered', '', 1, 1))
            selfTest.intercept(SmsReceived(OWN, text, None, 1))
            threading.Timer(0.05, selfTest.intercept, args=(SmsSent('abc', ref, OWN, 1, (7,)),)).start()
            return 'abc'

        selfTest._submit = submit
        self.assertEqual(selfTest.execute().status, 'ok')

    def testNotConnectedIsSkippedAndNotPublishedWhenScheduled(self):
        self.connected = False
        selfTest = self.make()
        result = selfTest.execute(scheduled=True)
        self.assertEqual((result.status, result.reason), ('skipped', NOT_CONNECTED))
        self.assertEqual(self.results, [])
        self.assertEqual(self.submitted, [])
        selfTest.execute()  # asked for: the answer is published
        self.assertEqual([result.status for result in self.results], ['skipped'])

    def testWithoutNumberTheTestIsSkipped(self):
        result = self.make(ownNumber=None).execute()
        self.assertEqual((result.status, self.submitted), ('skipped', []))
        self.assertIn('number', result.reason)

    def testAnSmsThatCannotBeSentIsSkippedNotFailed(self):
        self.scenario = Scenario(failed='+CMS ERROR: 500')
        result = self.make().execute()
        self.assertEqual(result.status, 'skipped')
        self.assertIn('+CMS ERROR: 500', result.reason)

    def testAnSmsThatCannotBeQueuedIsSkipped(self):
        selfTest = self.make()

        def refuse(number, text, ref):
            raise NotConnectedError('Modem stopped')

        selfTest._submit = refuse
        self.assertEqual(selfTest.execute().status, 'skipped')

    def testTwoTestsAtOnceAreRefused(self):
        self.scenario = Scenario(received=False)
        selfTest = self.make(timeout=0.5)
        first = threading.Thread(target=selfTest.execute)
        first.start()
        time.sleep(0.15)
        second = selfTest.execute()
        first.join()
        self.assertEqual((second.status, second.reason), ('skipped', 'a test is already running'))

    def testWhatIsNotOfTheTestIsNotCaught(self):
        selfTest = self.make()
        other = [SmsQueued('x', '42:abc', OWN, 'busy'), SmsSent('x', '42:abc', OWN, 1, (1,)),
                 SmsFailed('x', None, OWN, 'failed'), SmsExpired('x', '42:abc', OWN, 'expired'),
                 SmsDelivery('x', '42:abc', OWN, 'delivered', '', 1, 1),
                 SmsReceived(OWN, 'hello', None, 1),
                 SmsReceived('+33611111111', TEXT_PREFIX + '0a1b2c3d', None, 1),  # the text, from someone else
                 SmsReceived(OWN, TEXT_PREFIX + '0a1b2c3d!', None, 1)]
        self.assertFalse([event for event in other if selfTest.intercept(event)])

    def testTheSmsOfAnEarlierTestIsCaughtToo(self):
        # it came back after the test gave up: it must not reach the users either
        self.assertTrue(self.make().intercept(SmsReceived(OWN.replace('+33', '0'), TEXT_PREFIX + '0a1b2c3d', None, 1)))

    def testTheEventsOfAnEarlierTestDoNotCompleteTheNextOne(self):
        self.scenario = Scenario(received=False, report=False)
        selfTest = self.make()
        selfTest.intercept(SmsReceived(OWN, TEXT_PREFIX + '00000000', None, 1))
        selfTest.intercept(SmsDelivery('old', REF_PREFIX + ':00000000', OWN, 'delivered', '', 1, 1))
        self.assertEqual(selfTest.execute().status, 'noReception')

    def testAutoRestartOnFailureOncePerHour(self):
        self.scenario = Scenario(received=False)
        selfTest = self.make(autoRestart=True)
        first = selfTest.execute()
        self.assertEqual((first.status, first.restarted, self.restarts), ('noReception', True, 1))
        second = selfTest.execute()
        self.assertEqual((second.status, second.restarted, self.restarts), ('noReception', False, 1))
        self.assertIn('intervention needed', second.reason)

    def testNoRestartWithoutTheOption(self):
        self.scenario = Scenario(report=False)
        result = self.make().execute()
        self.assertEqual((result.restarted, self.restarts), (False, 0))

    def testARestartThatIsNotPossibleIsNotCounted(self):
        self.scenario = Scenario(received=False)
        self.restartError = NotConnectedError('Modem not connected')
        selfTest = self.make(autoRestart=True)
        self.assertFalse(selfTest.execute().restarted)
        self.restartError = None
        self.assertTrue(selfTest.execute().restarted)  # it can still be done: the failed one did not use the hour

    def testTheScheduleRunsTheTestAgainAndAgain(self):
        selfTest = self.make(interval=0.1, timeout=0.3)
        selfTest.start()
        self.assertTrue(waitFor(lambda: len(self.results) >= 3))
        self.assertTrue(all(result.status == 'ok' for result in self.results))

    def testAfterARestartTheNextTestComesSoon(self):
        self.scenario = Scenario(received=False)
        selfTest = self.make(interval=30.0, followUp=0.1, autoRestart=True, timeout=0.2)
        selfTest._interval = 0.05
        selfTest.start()
        self.assertTrue(waitFor(lambda: len(self.results) >= 2, 5.0))
        self.assertTrue(self.results[0].restarted)
        self.assertFalse(self.results[1].restarted)


def restartsTheModem(fake):
    fake.feed(b'OK\r\n')
    threading.Timer(0.1, fake.unplug).start()


class ModemSelfTestTest(unittest.TestCase):
    """ The whole chain on a fake port: a modem that stores what it receives, keeps the SMS of the test out of what the
    users see, and restarts when asked to """

    def setUp(self):
        FakeSerial.instances.clear()
        self.sim = FakeSim()
        self.scenario = Scenario()
        simple = simBehavior(self.sim, simcomTable(**{'AT+CRESET': restartsTheModem}))

        def behavior(fake, data):
            text = data.decode()
            if text.startswith('AT+CMGS='):
                fake.feed(b'\r\n> ')
            elif text.endswith(CTRL_Z):
                body = decodeSmsPdu(text.rstrip(CTRL_Z))['text']
                fake.feed(b'\r\n+CMGS: 9\r\n\r\nOK\r\n')
                if self.scenario.received:
                    self.sim.add(0, deliverPdus(OWN, body)[0])
                    threading.Timer(0.05, fake.feed, args=(b'\r\n+CMTI: "SM",0\r\n',)).start()
                if self.scenario.report:
                    pdu = statusReportPdu(9, OWN)
                    threading.Timer(0.1, fake.feed, args=(('\r\n+CDS: 26\r\n' + pdu + '\r\n').encode(),)).start()
            else:
                simple(fake, data)

        FakeSerial.behavior = behavior
        patcher = mock.patch('wwanlib.transport.serial.Serial', FakeSerial)
        patcher.start()
        self.addCleanup(patcher.stop)
        for owner, name, value in ((Modem, 'MIN_MONITOR_INTERVAL', 0.05), (Executor, 'READY_INTERVAL', 0.1)):
            patcher = mock.patch.object(owner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.events: list = []

    def start(self, **options) -> Modem:
        modem = Modem('fake', 115200, options=ModemOptions(
            reconnectBaseDelay=0.05, reconnectMaxDelay=0.1, reconnectMaxAttempts=50, segmentPause=0.0,
            deliveryReport=True, ownNumber=OWN, **options))
        modem.onEvent(self.events.append)
        self.addCleanup(modem.stop)
        modem.start()
        self.assertTrue(waitFor(lambda: modem.state == ConnectionState.CONNECTED))
        assert modem._selfTest is not None
        modem._selfTest._timeout = 1.0
        return modem

    def leaked(self) -> list:
        return [event for event in list(self.events)
                if isinstance(event, (SmsQueued, SmsSent, SmsFailed, SmsExpired, SmsDelivery, SmsReceived))]

    def testTheSimReceivesWhatItSentAndItsReport(self):
        modem = self.start()
        result = modem.selfTest().result(5)
        self.assertEqual((result.status, result.restarted), ('ok', False))
        self.assertEqual(self.sim.store, {})  # read, then deleted
        self.assertEqual(self.leaked(), [])
        self.assertTrue(waitFor(lambda: [event for event in self.events if isinstance(event, SelfTestResult)] == [result]))

    def testTheMissingReportIsDetected(self):
        self.scenario = Scenario(report=False)
        modem = self.start()
        result = modem.selfTest().result(5)
        self.assertEqual(result.status, 'noReceipt')
        self.assertEqual(self.leaked(), [])

    def testTheMissingSmsIsDetectedAndTheModemRestarted(self):
        self.scenario = Scenario(received=False)
        modem = self.start(selfTestRestart=True)
        result = modem.selfTest().result(5)
        self.assertEqual((result.status, result.restarted), ('noReception', True))
        self.assertTrue(waitFor(lambda: len(FakeSerial.instances) == 2 and modem.state == ConnectionState.CONNECTED))
        states = [event.state for event in self.events if isinstance(event, StateChanged)]
        self.assertIn('restarting', states)
        self.assertEqual(states[-1], 'connected')

    def testWithoutNumberTheTestIsSkipped(self):
        with self.assertLogs('wwanlib', level='ERROR') as logs:
            modem = Modem('fake', 115200, options=ModemOptions(selfTestInterval=3600.0, ownNumber='abc'))
        self.assertIn('Self-test disabled', logs.output[0])
        self.assertEqual(modem.selfTest().result(2).status, 'skipped')

    def testOnlyTheNormalSmsReachTheUsers(self):
        modem = self.start()
        sms = modem.sendSms(OWN, 'a normal message', ref='42')
        self.assertTrue(waitFor(lambda: [event for event in self.events if isinstance(event, SmsSent)]))
        self.assertEqual([event.smsId for event in self.events if isinstance(event, SmsSent)], [sms])


if __name__ == '__main__':
    unittest.main()
