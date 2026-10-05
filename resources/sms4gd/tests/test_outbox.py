""" Tests of the queue of the SMS to send (J3, step 2), of the aging of the priorities in the Executor and of the
sending through ``Modem.sendSms`` on a fake port.

Run from resources/sms4gd:  python -m unittest discover -s tests -t .
"""

import threading
import time
import unittest
from unittest import mock

import serial

from tests.test_inbox import statusReportPdu
from tests.test_wwanlib import ExecutorTestCase, FakeSerial, answer, simcomTable, waitFor
from wwanlib import (ConnectionState, Modem, ModemOptions, NotConnectedError, SmsDelivery, SmsExpired, SmsFailed, SmsQueued,
                     SmsQueueFullError, SmsSent, StateChanged)
from wwanlib.events import EventDispatcher
from wwanlib.executor import CTRL_Z, Executor, Priority, Transaction
from wwanlib.outbox import Outbox
from wwanlib.sms import SendOutcome

NUMBER = '+33612345678'
RETRY = SendOutcome('retry', 'modem not connected')


class Script:
    """ Stands for ``SmsSender.send``: plays one scripted outcome per call (the SMS is sent when the script is over) """

    def __init__(self, *outcomes, gate: threading.Event | None = None):
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, str, float]] = []
        self.gate = gate  # when given, send() waits for it
        self.entered = threading.Event()
        self._lock = threading.Lock()

    def send(self, number, text, maxPartsPerGroup):
        with self._lock:
            self.calls.append((number, text, time.monotonic()))
            outcome = self.outcomes.pop(0) if self.outcomes else SendOutcome('sent', parts=1, references=[5])
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(5)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def times(self):
        return [moment for _, _, moment in self.calls]


class OutboxTestCase(unittest.TestCase):
    def setUp(self):
        self.events: list = []
        self.lock = threading.Lock()
        self.outbox = None

    def publish(self, event):
        with self.lock:
            self.events.append(event)

    def make(self, script, **kwargs) -> Outbox:
        kwargs.setdefault('delays', (0.05, 0.1))
        kwargs.setdefault('steadyDelay', 0.15)
        self.outbox = Outbox(script.send, self.publish, **kwargs)
        self.addCleanup(self.outbox.stop)
        self.outbox.start()
        return self.outbox

    def of(self, kind) -> list:
        with self.lock:
            return [event for event in self.events if isinstance(event, kind)]

    def waitEvents(self, kind, count=1, timeout=5.0) -> bool:
        return waitFor(lambda: len(self.of(kind)) >= count, timeout)


class OutboxTest(OutboxTestCase):
    def testSmsAreSentOneAtATimeInOrder(self):
        script = Script()
        outbox = self.make(script)
        ids = [outbox.submit(NUMBER, text, ref=str(index)) for index, text in enumerate(('one', 'two', 'three'))]
        self.assertTrue(self.waitEvents(SmsSent, 3))
        self.assertEqual([call[1] for call in script.calls], ['one', 'two', 'three'])
        self.assertEqual([event.smsId for event in self.of(SmsSent)], ids)
        self.assertEqual([event.ref for event in self.of(SmsSent)], ['0', '1', '2'])
        self.assertEqual(len(set(ids)), 3)
        self.assertEqual(self.of(SmsSent)[0].references, (5,))
        self.assertEqual(len(outbox), 0)

    def testSubmitNeverBlocks(self):
        gate = threading.Event()
        script = Script(gate=gate)
        outbox = self.make(script)
        started = time.monotonic()
        for _ in range(5):
            outbox.submit(NUMBER, 'text')
        self.assertLess(time.monotonic() - started, 0.2)  # the first one is stuck in send(), the others wait
        gate.set()
        self.assertTrue(self.waitEvents(SmsSent, 5))

    def testTemporaryFailureIsTriedAgainWithGrowingDelays(self):
        script = Script(RETRY, RETRY, RETRY)
        outbox = self.make(script, delays=(0.1, 0.2), steadyDelay=0.3)
        outbox.submit(NUMBER, 'text', ref='r')
        self.assertTrue(self.waitEvents(SmsSent))
        times = script.times()
        self.assertEqual(len(times), 4)
        self.assertGreaterEqual(times[1] - times[0], 0.08)  # 1st delay
        self.assertGreaterEqual(times[2] - times[1], 0.18)  # 2nd delay
        self.assertGreaterEqual(times[3] - times[2], 0.28)  # then the steady delay
        queued = self.of(SmsQueued)
        self.assertEqual(len(queued), 1)  # announced once
        self.assertEqual((queued[0].ref, queued[0].reason), ('r', 'modem not connected'))

    def testAnSmsThatWaitsDoesNotBlockTheNextOnes(self):
        calls = []

        def send(number, text, maxPartsPerGroup):
            calls.append(text)
            if text == 'first' and calls.count('first') == 1:
                return RETRY
            return SendOutcome('sent', parts=1, references=[1])

        self.outbox = Outbox(send, self.publish, delays=(0.4,))
        self.addCleanup(self.outbox.stop)
        self.outbox.start()
        self.outbox.submit(NUMBER, 'first')
        self.outbox.submit(NUMBER, 'second')
        self.assertTrue(self.waitEvents(SmsSent, 2))
        self.assertEqual(calls, ['first', 'second', 'first'])  # the second one did not wait for the delay of the first

    def testFinalFailureIsNotTriedAgain(self):
        script = Script(SendOutcome('failed', '+CMS ERROR: 330', parts=3, references=[1]))
        outbox = self.make(script)
        outbox.submit(NUMBER, 'text', ref='r')
        self.assertTrue(self.waitEvents(SmsFailed))
        failed = self.of(SmsFailed)[0]
        self.assertEqual((failed.ref, failed.reason, failed.parts, failed.sentParts), ('r', '+CMS ERROR: 330', 3, 1))
        time.sleep(0.3)
        self.assertEqual(len(script.calls), 1)
        self.assertEqual(len(outbox), 0)

    def testExpiry(self):
        script = Script(*[SendOutcome('retry', '+CMS ERROR: 331')] * 100)
        outbox = self.make(script, ttl=0.4, delays=(0.1,), steadyDelay=0.1)
        started = time.monotonic()
        outbox.submit(NUMBER, 'text', ref='r')
        self.assertTrue(self.waitEvents(SmsExpired))
        self.assertLess(abs(time.monotonic() - started - 0.4), 0.25)
        expired = self.of(SmsExpired)[0]
        self.assertEqual((expired.ref, expired.reason), ('r', '+CMS ERROR: 331'))
        self.assertFalse(self.of(SmsSent) or self.of(SmsFailed))
        count = len(script.calls)
        time.sleep(0.3)
        self.assertEqual(len(script.calls), count)  # no attempt after the expiry
        self.assertEqual(len(outbox), 0)

    def testAnAttemptThatWouldFallAfterTheExpiryNeverHappens(self):
        script = Script(RETRY, RETRY)
        outbox = self.make(script, ttl=0.3, delays=(30.0,))
        started = time.monotonic()
        outbox.submit(NUMBER, 'text')
        self.assertTrue(self.waitEvents(SmsExpired, timeout=3))
        self.assertLess(time.monotonic() - started, 1.0)  # expired on time, not at the end of the delay of 30 s
        self.assertEqual(len(script.calls), 1)

    def testSmsThatWaitedBehindAStuckSendExpiresWithoutAnyAttempt(self):
        gate = threading.Event()
        script = Script(gate=gate)
        outbox = self.make(script, ttl=0.2)
        outbox.submit(NUMBER, 'stuck')  # in send(), waiting for the gate
        script.entered.wait(2)
        outbox.submit(NUMBER, 'waiting')  # behind it, its lifetime ends meanwhile
        time.sleep(0.4)
        gate.set()  # the thread is free: it reports the expiry instead of sending the SMS
        self.assertTrue(self.waitEvents(SmsExpired, timeout=3))
        self.assertEqual(self.of(SmsExpired)[0].reason, '')
        self.assertEqual([call[1] for call in script.calls], ['stuck'])

    def testRetryNowBringsTheAttemptsForward(self):
        script = Script(RETRY)
        outbox = self.make(script, delays=(30.0,))
        outbox.submit(NUMBER, 'text')
        self.assertTrue(self.waitEvents(SmsQueued))
        time.sleep(0.2)
        self.assertEqual(len(script.calls), 1)  # waiting for 30 s
        outbox.retryNow()
        self.assertTrue(self.waitEvents(SmsSent, timeout=2))

    def testRetryNowWhileTheSmsIsBeingSent(self):
        gate = threading.Event()
        script = Script(RETRY, gate=gate)
        outbox = self.make(script, delays=(30.0,))
        outbox.submit(NUMBER, 'text')
        script.entered.wait(2)
        outbox.retryNow()  # the modem reconnected while this attempt was running (and will fail)
        gate.set()
        self.assertTrue(self.waitEvents(SmsSent, timeout=2))
        self.assertEqual(len(script.calls), 2)

    def testQueueIsLimitedAndTheOldestIsKept(self):
        gate = threading.Event()
        script = Script(gate=gate)
        outbox = self.make(script, maxSize=3)
        outbox.submit(NUMBER, 'one')
        script.entered.wait(2)
        outbox.submit(NUMBER, 'two')
        outbox.submit(NUMBER, 'three')
        with self.assertRaises(SmsQueueFullError):
            outbox.submit(NUMBER, 'four')
        gate.set()
        self.assertTrue(self.waitEvents(SmsSent, 3))
        self.assertEqual([call[1] for call in script.calls], ['one', 'two', 'three'])  # nothing was dropped
        outbox.submit(NUMBER, 'five')  # room again
        self.assertTrue(self.waitEvents(SmsSent, 4))

    def testFailAllFailsTheWaitingSmsAndRefusesNewOnes(self):
        script = Script(RETRY, RETRY)
        outbox = self.make(script, delays=(30.0,))
        outbox.submit(NUMBER, 'one', ref='1')
        outbox.submit(NUMBER, 'two', ref='2')
        self.assertTrue(self.waitEvents(SmsQueued, 2))
        outbox.failAll('modem disconnected')
        failed = self.of(SmsFailed)
        self.assertEqual(sorted((event.ref, event.reason) for event in failed), [('1', 'modem disconnected'), ('2', 'modem disconnected')])
        self.assertEqual(len(outbox), 0)
        with self.assertRaises(NotConnectedError):
            outbox.submit(NUMBER, 'three')

    def testFailAllWhileSendingFailsTheSmsEvenIfItWasToBeTriedAgain(self):
        gate = threading.Event()
        script = Script(RETRY, gate=gate)
        outbox = self.make(script)
        outbox.submit(NUMBER, 'text', ref='r')
        script.entered.wait(2)
        outbox.failAll('modem disconnected')
        gate.set()
        self.assertTrue(self.waitEvents(SmsFailed))
        self.assertEqual(self.of(SmsFailed)[0].reason, 'modem disconnected')
        self.assertFalse(self.of(SmsQueued))
        self.assertEqual(len(outbox), 0)

    def testStopFailsWhatWaits(self):
        script = Script(RETRY, RETRY)
        outbox = self.make(script, delays=(30.0,))
        outbox.submit(NUMBER, 'one', ref='1')
        outbox.submit(NUMBER, 'two', ref='2')
        self.assertTrue(self.waitEvents(SmsQueued, 2))
        outbox.stop()
        # the events are published before stop() returns: Jeedom does not wait for them
        self.assertEqual(sorted((event.ref, event.reason) for event in self.of(SmsFailed)),
                         [('1', 'daemon stopped'), ('2', 'daemon stopped')])
        with self.assertRaises(NotConnectedError):
            outbox.submit(NUMBER, 'three')

    def testStopWhileSendingReportsTheSmsOnce(self):
        gate = threading.Event()
        script = Script(RETRY, gate=gate)
        outbox = self.make(script)
        outbox.submit(NUMBER, 'text', ref='r')
        script.entered.wait(2)
        stopper = threading.Thread(target=outbox.stop)
        stopper.start()
        time.sleep(0.1)
        gate.set()  # the attempt ends with "to be tried again" while stopping
        stopper.join(5)
        self.assertEqual([(event.ref, event.reason) for event in self.of(SmsFailed)], [('r', 'daemon stopped')])
        self.assertFalse(self.of(SmsQueued) or self.of(SmsSent))

    def testSmsSentWhileStoppingIsReportedAsSent(self):
        gate = threading.Event()
        script = Script(gate=gate)
        outbox = self.make(script)
        outbox.submit(NUMBER, 'text')
        script.entered.wait(2)
        stopper = threading.Thread(target=outbox.stop)
        stopper.start()
        time.sleep(0.1)
        gate.set()
        stopper.join(5)
        self.assertEqual(len(self.of(SmsSent)), 1)
        self.assertFalse(self.of(SmsFailed))

    def testAnErrorInSendFailsTheSmsAndTheThreadGoesOn(self):
        script = Script(RuntimeError('boom'))
        outbox = self.make(script)
        outbox.submit(NUMBER, 'one')
        outbox.submit(NUMBER, 'two')
        self.assertTrue(self.waitEvents(SmsSent))
        self.assertEqual([event.reason for event in self.of(SmsFailed)], ['internal error'])

    def testAnErrorInTheSubscriberDoesNotKillTheThread(self):
        calls = []

        def publish(event):
            calls.append(event)
            if len(calls) == 1:
                raise RuntimeError('boom')

        self.outbox = Outbox(Script().send, publish)
        self.addCleanup(self.outbox.stop)
        self.outbox.start()
        self.outbox.submit(NUMBER, 'one')
        self.outbox.submit(NUMBER, 'two')
        self.assertTrue(waitFor(lambda: len(calls) == 2))


class LimitedRetriesTest(OutboxTestCase):
    def testAnErrorThatSaysNothingFailsAfterThreeRetries(self):
        limited = SendOutcome('retry', '+CMS ERROR: 500', limited=True)
        script = Script(limited, limited, limited, limited)
        self.make(script, delays=(0.02, 0.02, 0.02, 0.02))
        self.outbox.submit(NUMBER, 'Hello', '42')
        self.assertTrue(self.waitEvents(SmsFailed))
        failed = self.of(SmsFailed)[0]
        self.assertEqual((failed.ref, failed.reason, failed.sentParts), ('42', '+CMS ERROR: 500', 0))
        self.assertEqual(len(script.calls), 4)  # the first attempt and three retries
        self.assertEqual(len(self.of(SmsQueued)), 1)  # told once that it waits, then that it failed
        self.assertEqual(len(self.outbox), 0)

    def testOtherTemporaryErrorsAreRetriedUntilTheLifetimeEnds(self):
        script = Script(*[SendOutcome('retry', '+CMS ERROR: 331')] * 6)
        self.make(script, delays=(0.02,), steadyDelay=0.02)
        self.outbox.submit(NUMBER, 'Hello', '42')
        self.assertTrue(waitFor(lambda: len(script.calls) >= 6))
        self.assertFalse(self.of(SmsFailed))

    def testASuccessAfterTwoRefusalsIsSent(self):
        limited = SendOutcome('retry', '+CMS ERROR: 500', limited=True)
        script = Script(limited, limited)
        self.make(script, delays=(0.02, 0.02, 0.02))
        self.outbox.submit(NUMBER, 'Hello', '42')
        self.assertTrue(self.waitEvents(SmsSent))
        self.assertFalse(self.of(SmsFailed))


class MessagePauseTest(OutboxTestCase):
    def testTheNextSmsWaitsForThePauseEvenWhenItComesLater(self):
        script = Script()
        self.make(script, messagePause=0.4)
        self.outbox.submit(NUMBER, 'one', '1')
        self.assertTrue(self.waitEvents(SmsSent))
        time.sleep(0.1)  # the second one arrives while the pause runs, the queue being empty
        self.outbox.submit(NUMBER, 'two', '2')
        self.assertTrue(self.waitEvents(SmsSent, 2))
        first, second = script.times()
        self.assertGreaterEqual(second - first, 0.4)

    def testNoPauseByDefault(self):
        script = Script()
        self.make(script)
        for text in ('one', 'two', 'three'):
            self.outbox.submit(NUMBER, text)
        self.assertTrue(self.waitEvents(SmsSent, 3))
        self.assertLess(script.times()[-1] - script.times()[0], 0.3)

    def testAnSmsThatDidNotLeaveDoesNotStartThePause(self):
        script = Script(SendOutcome('retry', 'timeout'))
        self.make(script, messagePause=0.5, delays=(0.05,))
        self.outbox.submit(NUMBER, 'one', '1')
        self.assertTrue(self.waitEvents(SmsSent))
        first, second = script.times()
        self.assertLess(second - first, 0.4)

    def testTheQueueExpiryIsNotDelayedByThePause(self):
        script = Script()
        self.make(script, messagePause=0.5, ttl=0.2)
        self.outbox.submit(NUMBER, 'one', '1')
        self.outbox.submit(NUMBER, 'two', '2')
        self.assertTrue(self.waitEvents(SmsExpired))


class ReplyDelayTest(OutboxTestCase):
    def testNoSmsStartsRightAfterAReception(self):
        script = Script()
        self.make(script, replyDelay=0.4)
        received = time.monotonic()
        self.outbox.noteReception()
        self.outbox.submit(NUMBER, 'the reply', '1')  # the reply is asked for at once
        self.assertTrue(self.waitEvents(SmsSent))
        self.assertGreaterEqual(script.times()[0] - received, 0.4)

    def testNoDelayWithoutAReception(self):
        script = Script()
        self.make(script, replyDelay=0.4)
        started = time.monotonic()
        self.outbox.submit(NUMBER, 'a notification', '1')
        self.assertTrue(self.waitEvents(SmsSent))
        self.assertLess(script.times()[0] - started, 0.3)

    def testNoDelayByDefault(self):
        script = Script()
        self.make(script)
        started = time.monotonic()
        self.outbox.noteReception()
        self.outbox.submit(NUMBER, 'the reply', '1')
        self.assertTrue(self.waitEvents(SmsSent))
        self.assertLess(script.times()[0] - started, 0.3)

    def testTheMessagePauseDoesNotShortenTheDelay(self):
        script = Script()
        self.make(script, replyDelay=0.5, messagePause=0.05)
        self.outbox.submit(NUMBER, 'one', '1')
        self.assertTrue(self.waitEvents(SmsSent))
        received = time.monotonic()
        self.outbox.noteReception()
        self.outbox.submit(NUMBER, 'two', '2')
        self.assertTrue(self.waitEvents(SmsSent, 2))
        self.assertGreaterEqual(script.times()[1] - received, 0.5)  # the pause (0.05 s) of the first one did not replace it


class FakeTracker:
    """ Stands for the ReceiptTracker: records what the outbox tells it """

    def __init__(self):
        self.calls: list[tuple] = []

    def register(self, smsId, ref, number, index, parts, reference):
        self.calls.append(('register', ref, number, index, parts, reference))

    def close(self, smsId):
        self.calls.append(('close',))

    def abandon(self, smsId):
        self.calls.append(('abandon',))


class PartsScript(Script):
    """ A Script whose send() tells each part it sends, as SmsSender does """

    def __init__(self, *outcomes, told=2):
        super().__init__(*outcomes)
        self.told = told

    def send(self, number, text, maxPartsPerGroup, onPart=None):
        for index in range(1, self.told + 1):
            if onPart is not None:
                onPart(index, 3, 9 + index)
        return super().send(number, text, maxPartsPerGroup)


class TrackerWiringTest(OutboxTestCase):
    def setUp(self):
        super().setUp()
        self.tracker = FakeTracker()

    def run_(self, script, ref='42:x', **kwargs):
        outbox = self.make(script, tracker=self.tracker, **kwargs)
        outbox.submit(NUMBER, 'Hello', ref)
        return script

    def testEachPartIsRegisteredThenTheSmsIsClosedAfterItsEvent(self):
        self.run_(PartsScript(SendOutcome('sent', parts=2, references=[10, 11]), told=2))
        self.assertTrue(self.waitEvents(SmsSent))
        self.assertTrue(waitFor(lambda: ('close',) in self.tracker.calls))
        self.assertEqual(self.tracker.calls, [('register', '42:x', NUMBER, 1, 3, 10), ('register', '42:x', NUMBER, 2, 3, 11), ('close',)])

    def testAFailureAfterSomePartsLeftAbandonsTheFollowing(self):
        self.run_(PartsScript(SendOutcome('failed', '+CMS ERROR: 500', parts=3, references=[10, 11]), told=2))
        self.assertTrue(self.waitEvents(SmsFailed))
        self.assertTrue(waitFor(lambda: ('abandon',) in self.tracker.calls))
        self.assertNotIn(('close',), self.tracker.calls)

    def testAFailureBeforeAnyPartLeftTellsNothing(self):
        self.run_(PartsScript(SendOutcome('failed', 'invalid number'), told=0))
        self.assertTrue(self.waitEvents(SmsFailed))
        time.sleep(0.1)
        self.assertEqual(self.tracker.calls, [])

    def testAnSmsThatWillBeTriedAgainTellsNothingYet(self):
        script = self.run_(PartsScript(RETRY, told=0), delays=(5.0,), steadyDelay=5.0)
        self.assertTrue(waitFor(lambda: len(script.calls) >= 1))
        time.sleep(0.1)
        self.assertEqual(self.tracker.calls, [])

class EventDispatcherTest(unittest.TestCase):
    def testTheLastEventsAreDeliveredAtStop(self):
        received = []

        def slow(event):
            time.sleep(0.05)
            received.append(event)

        dispatcher = EventDispatcher()
        dispatcher.subscribe(slow)
        dispatcher.start()
        for index in range(5):
            dispatcher.post(index)
        dispatcher.stop(timeout=3)
        self.assertEqual(received, [0, 1, 2, 3, 4])


class AgingTest(ExecutorTestCase):
    """ A transaction that waits rises by one rank every ``aging`` seconds (``SMS_CONTINUATION`` at the highest) """

    def slowThen(self):
        def behavior(fake, data):
            if data.decode().startswith('AT+SLOW'):
                fake.feedLater(0.6, b'OK\r\n')
            else:
                fake.feed(b'OK\r\n')

        FakeSerial.behavior = behavior

    def commands(self):
        return [command for command in self.fake.commands() if command != 'AT+SLOW']

    def testConsoleWaitingForALongTimeOvertakesAnSms(self):
        self.executor._aging = 0.2
        self.slowThen()
        slow = self.executor.submit('AT+SLOW', priority=Priority.NEW_SMS)
        time.sleep(0.1)
        console = self.executor.submit('AT+CONSOLE', priority=Priority.CONSOLE)
        time.sleep(0.3)
        sms = self.executor.submit('AT+SMS', priority=Priority.NEW_SMS)
        for future in (slow, console, sms):
            future.result(5)
        # 0.5 s of waiting for the console (two ranks: SMS_CONTINUATION), 0.2 s for the SMS (one rank, same one):
        # equal ranks, the older one first
        self.assertEqual(self.commands(), ['AT+CONSOLE', 'AT+SMS'])

    def testWithoutAgingTheSmsAlwaysWins(self):
        self.executor._aging = 0
        self.slowThen()
        slow = self.executor.submit('AT+SLOW', priority=Priority.NEW_SMS)
        time.sleep(0.1)
        console = self.executor.submit('AT+CONSOLE', priority=Priority.CONSOLE)
        time.sleep(0.3)
        sms = self.executor.submit('AT+SMS', priority=Priority.NEW_SMS)
        for future in (slow, console, sms):
            future.result(5)
        self.assertEqual(self.commands(), ['AT+SMS', 'AT+CONSOLE'])

    def testTheHighRanksNeverChange(self):
        self.executor._aging = 0.1
        self.slowThen()
        slow = self.executor.submit('AT+SLOW', priority=Priority.NEW_SMS)
        time.sleep(0.05)
        console = self.executor.submit('AT+CONSOLE', priority=Priority.CONSOLE)
        time.sleep(0.4)
        memory = self.executor.submit('AT+MEMORY', priority=Priority.MEMORY_RELEASE)
        for future in (slow, console, memory):
            future.result(5)
        # the console rose to SMS_CONTINUATION at best: the release of the memory (a higher rank) is still first
        self.assertEqual(self.commands(), ['AT+MEMORY', 'AT+CONSOLE'])

    def testNoAgingUnderTheContinuationRank(self):
        executor = Executor(write=lambda data: None, onStuck=lambda reason: None, aging=1.0)
        transaction = Transaction([], priority=Priority.SMS_CONTINUATION)
        transaction.queuedAt = time.monotonic() - 100
        self.assertEqual(executor._effectivePriority(transaction, time.monotonic()), Priority.SMS_CONTINUATION)
        transaction.priority = Priority.SUPERVISION
        self.assertEqual(executor._effectivePriority(transaction, time.monotonic()), Priority.SMS_CONTINUATION)
        transaction.priority = Priority.MEMORY_RELEASE
        self.assertEqual(executor._effectivePriority(transaction, time.monotonic()), Priority.MEMORY_RELEASE)


def smsBehavior(table=None, mr=7):
    """ Fake modem: the answers of ``simcomTable`` plus the sending of an SMS (prompt, then +CMGS and OK) """
    simple = answer(table if table is not None else simcomTable(), echo=True)

    def behavior(fake, data):
        text = data.decode()
        if text.startswith('AT+CMGS='):
            fake.feed(b'\r\n> ')
        elif text.endswith(CTRL_Z):
            fake.feed(f'\r\n+CMGS: {mr}\r\n\r\nOK\r\n'.encode())
        else:
            simple(fake, data)

    return behavior


class ModemSmsTest(unittest.TestCase):
    def setUp(self):
        FakeSerial.instances.clear()
        FakeSerial.behavior = smsBehavior()
        self.blocked = threading.Event()  # while set, the port cannot be opened

        def opener(**kwargs):
            if self.blocked.is_set():
                raise serial.SerialException('no such device')
            return FakeSerial(**kwargs)

        patcher = mock.patch('wwanlib.transport.serial.Serial', opener)
        patcher.start()
        self.addCleanup(patcher.stop)
        for owner, name, value in ((Modem, 'MIN_MONITOR_INTERVAL', 0.05), (Executor, 'READY_INTERVAL', 0.1)):
            patcher = mock.patch.object(owner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.events: list = []

    def makeModem(self, maxAttempts=100, **options) -> Modem:
        modem = Modem('fake', 115200, options=ModemOptions(
            reconnectBaseDelay=0.05, reconnectMaxDelay=0.1, reconnectMaxAttempts=maxAttempts, segmentPause=0.0, **options))
        modem.onEvent(self.events.append)
        self.addCleanup(modem.stop)
        return modem

    def of(self, kind) -> list:
        return [event for event in list(self.events) if isinstance(event, kind)]

    def waitEvents(self, kind, count=1, timeout=5.0) -> bool:
        return waitFor(lambda: len(self.of(kind)) >= count, timeout)

    def waitConnected(self, modem):
        self.assertTrue(waitFor(lambda: modem.state == ConnectionState.CONNECTED))

    def testADeliveryReportIsFollowedThroughTheModem(self):
        modem = self.makeModem(deliveryReport=True)
        modem.start()
        self.waitConnected(modem)
        smsId = modem.sendSms(NUMBER, 'Hello', ref='42:abc')
        self.assertTrue(self.waitEvents(SmsSent))
        FakeSerial.instances[0].feed(('\r\n+CDS: 26\r\n' + statusReportPdu(7, NUMBER) + '\r\n').encode())
        self.assertTrue(self.waitEvents(SmsDelivery))
        self.assertEqual(self.of(SmsDelivery), [SmsDelivery(smsId, '42:abc', NUMBER, 'delivered', '', 1, 1)])
        events = [event for event in self.events if isinstance(event, (SmsSent, SmsDelivery))]
        self.assertIsInstance(events[0], SmsSent)  # "sent" is told first

    def testAReportArrivingDuringThePauseBetweenTwoSmsIsHandled(self):
        modem = self.makeModem(deliveryReport=True, messagePause=3.0)
        modem.start()
        self.waitConnected(modem)
        first = modem.sendSms(NUMBER, 'one', ref='1')
        modem.sendSms(NUMBER, 'two', ref='2')
        self.assertTrue(self.waitEvents(SmsSent))
        FakeSerial.instances[0].feed(('\r\n+CDS: 26\r\n' + statusReportPdu(7, NUMBER) + '\r\n').encode())
        self.assertTrue(self.waitEvents(SmsDelivery, timeout=1.5))  # the second SMS still waits
        self.assertEqual([(event.smsId, event.status) for event in self.of(SmsDelivery)], [(first, 'delivered')])
        self.assertEqual(len(self.of(SmsSent)), 1)

    def testNothingIsFollowedWhenTheReportsAreNotAsked(self):
        modem = self.makeModem(deliveryReport=False)
        modem.start()
        self.waitConnected(modem)
        modem.sendSms(NUMBER, 'Hello')
        self.assertTrue(self.waitEvents(SmsSent))
        FakeSerial.instances[0].feed(('\r\n+CDS: 26\r\n' + statusReportPdu(7, NUMBER) + '\r\n').encode())
        self.assertFalse(waitFor(lambda: self.of(SmsDelivery), 0.5))

    def testAnSmsIsSentAndReported(self):
        modem = self.makeModem()
        modem.start()
        self.waitConnected(modem)
        smsId = modem.sendSms(NUMBER, 'Hello', ref='42')
        self.assertTrue(self.waitEvents(SmsSent))
        sent = self.of(SmsSent)[0]
        self.assertEqual((sent.smsId, sent.ref, sent.number, sent.parts, sent.references), (smsId, '42', NUMBER, 1, (7,)))
        self.assertFalse(self.of(SmsQueued))
        self.assertTrue([c for c in FakeSerial.instances[0].commands() if c.startswith('AT+CMGS=')])

    def testALongSmsIsSentInParts(self):
        modem = self.makeModem()
        modem.start()
        self.waitConnected(modem)
        modem.sendSms(NUMBER, 'a' * 400)
        self.assertTrue(self.waitEvents(SmsSent))
        self.assertEqual(self.of(SmsSent)[0].parts, 3)
        prompts = [c for c in FakeSerial.instances[0].commands() if c.startswith('AT+CMGS=')]
        self.assertEqual(len(prompts), 3)

    def testAnSmsWaitsForTheReconnectionThenLeavesAtOnce(self):
        modem = self.makeModem()
        modem.start()
        self.waitConnected(modem)
        self.blocked.set()
        FakeSerial.instances[0].unplug()
        self.assertTrue(waitFor(lambda: modem.state == ConnectionState.RECONNECTING))
        modem.sendSms(NUMBER, 'Hello', ref='r')
        self.assertTrue(self.waitEvents(SmsQueued))
        self.assertEqual(self.of(SmsQueued)[0].reason, 'modem not connected')
        self.assertFalse(self.of(SmsSent))
        self.blocked.clear()  # the modem is back: the default first delay is 10 s, the SMS must not wait for it
        self.assertTrue(self.waitEvents(SmsSent, timeout=5))
        self.assertEqual(self.of(SmsSent)[0].ref, 'r')

    def testSmsWaitingWhenTheConnectionIsGivenUpFail(self):
        self.blocked.set()
        modem = self.makeModem(maxAttempts=2)
        modem.start()
        modem.sendSms(NUMBER, 'Hello', ref='r')
        self.assertTrue(self.waitEvents(SmsFailed, timeout=5))
        failed = self.of(SmsFailed)[0]
        self.assertEqual((failed.ref, failed.reason), ('r', 'modem disconnected'))
        self.assertTrue(waitFor(lambda: [event.state for event in self.of(StateChanged)][-1:] == [ConnectionState.DISCONNECTED]))
        with self.assertRaises(NotConnectedError):
            modem.sendSms(NUMBER, 'Later')

    def testStopFailsTheWaitingSmsAndTheEventsAreDelivered(self):
        self.blocked.set()
        modem = self.makeModem()
        modem.start()
        modem.sendSms(NUMBER, 'Hello', ref='r')
        self.assertTrue(self.waitEvents(SmsQueued))
        modem.stop()
        failed = self.of(SmsFailed)
        self.assertEqual([(event.ref, event.reason) for event in failed], [('r', 'daemon stopped')])

    def testTheQueueRefusesTheSmsBeyondItsSize(self):
        self.blocked.set()
        modem = self.makeModem(smsQueueSize=2)
        modem.start()
        modem.sendSms(NUMBER, 'one')
        modem.sendSms(NUMBER, 'two')
        with self.assertRaises(SmsQueueFullError):
            modem.sendSms(NUMBER, 'three')

    def testAnInvalidNumberFailsWithoutTouchingTheModem(self):
        modem = self.makeModem()
        modem.start()
        self.waitConnected(modem)
        modem.sendSms('not a number', 'Hello', ref='r')
        self.assertTrue(self.waitEvents(SmsFailed))
        self.assertEqual(self.of(SmsFailed)[0].reason, 'invalid number')
        self.assertFalse([c for c in FakeSerial.instances[0].commands() if c.startswith('AT+CMGS')])


if __name__ == '__main__':
    unittest.main()
