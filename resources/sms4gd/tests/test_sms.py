""" Tests of the sending of SMS (J3): PDU encoding, parts, decisions on each result, and the exchanges with a fake port.

Run from resources/sms4gd:  python -m unittest discover -s tests -t .
"""

import threading
import time
import unittest
from concurrent.futures import Future
from unittest import mock

from tests.test_wwanlib import ExecutorTestCase, FakeSerial, answer, waitFor
from wwanlib import sms
from wwanlib.exceptions import CmeError, CmsError, CommandError, NotConnectedError, TimeoutException
from wwanlib.executor import CTRL_Z, Priority, Transaction
from wwanlib.pdu import encodeSmsSubmitPdu
from wwanlib.sms import SmsSender, buildParts, describe, isTemporary, normalizeNumber, parseCmgs, usesUcs2

NUMBER = '+33612345678'


class PduFields:
    """ Fields of an SMS-SUBMIT PDU built without SMSC (the layout is fixed, so they are found by position) """

    def __init__(self, pdu):
        self.data = bytes(pdu.data)
        addressOctets = (self.data[3] + 1) // 2
        self.tpMr = self.data[2]
        self.dcs = self.data[5 + addressOctets + 1]
        self.udhPresent = bool(self.data[1] & 0x40)
        udl = 5 + addressOctets + 2
        self.concat = None
        if self.udhPresent:
            # UDH length, IEI 0, IE length 3, reference, parts, number
            self.concat = tuple(self.data[udl + 4:udl + 7])


class PduEncodingTest(unittest.TestCase):
    def testShortMessageUsesTheGivenTpMr(self):
        pdus = encodeSmsSubmitPdu(NUMBER, 'Hello', reference=42)
        self.assertEqual(len(pdus), 1)
        fields = PduFields(pdus[0])
        self.assertEqual(fields.tpMr, 42)
        self.assertEqual(fields.dcs, 0x00)
        self.assertFalse(fields.udhPresent)

    def testEachPartHasItsOwnTpMrAndTheGroupOneReference(self):
        pdus = encodeSmsSubmitPdu(NUMBER, 'a' * 400, reference=10, concatReference=77)
        self.assertEqual(len(pdus), 3)
        fields = [PduFields(pdu) for pdu in pdus]
        self.assertEqual([f.tpMr for f in fields], [10, 11, 12])
        # the concatenation reference is not the TP-MR (problem G): same for the whole group
        self.assertEqual([f.concat for f in fields], [(77, 3, 1), (77, 3, 2), (77, 3, 3)])

    def testGroupsTakeOneReferenceEachAndTpMrContinues(self):
        pdus = encodeSmsSubmitPdu(NUMBER, 'a' * 700, reference=250, concatReference=254, maxPartsPerGroup=2)
        fields = [PduFields(pdu) for pdu in pdus]
        self.assertEqual(len(fields), 5)  # 153 characters per part
        self.assertEqual([f.tpMr for f in fields], [250, 251, 252, 253, 254])
        self.assertEqual([f.concat for f in fields[:4]], [(254, 2, 1), (254, 2, 2), (255, 2, 1), (255, 2, 2)])
        self.assertIsNone(fields[4].concat)  # the last group has a single part: a plain SMS

    def testReferencesWrapAround(self):
        fields = [PduFields(pdu) for pdu in encodeSmsSubmitPdu(NUMBER, 'a' * 400, reference=254, concatReference=255, maxPartsPerGroup=1)]
        self.assertEqual([f.tpMr for f in fields], [254, 255, 0])

    def testConcatenationReferenceIsRandomByDefault(self):
        references = {PduFields(encodeSmsSubmitPdu(NUMBER, 'a' * 400)[0]).concat[0] for _ in range(40)}
        self.assertGreater(len(references), 1)

    def testUcs2(self):
        pdus = encodeSmsSubmitPdu(NUMBER, '\U0001F600 alerte', reference=1)
        self.assertEqual(len(pdus), 1)
        self.assertEqual(PduFields(pdus[0]).dcs, 0x08)
        pdus = encodeSmsSubmitPdu(NUMBER, '\u0436' * 150, reference=1, concatReference=5)
        self.assertEqual(len(pdus), 3)  # 67 characters per part
        self.assertEqual({PduFields(pdu).dcs for pdu in pdus}, {0x08})
        self.assertEqual([PduFields(pdu).concat for pdu in pdus], [(5, 3, 1), (5, 3, 2), (5, 3, 3)])


class BuildPartsTest(unittest.TestCase):
    def testNumberNormalization(self):
        self.assertEqual(normalizeNumber('+33 6 12.34-56 78'), '+33612345678')
        self.assertEqual(normalizeNumber('(06) 12 34 56 78'), '0612345678')
        self.assertEqual(normalizeNumber('3631'), '3631')
        for invalid in ('', 'abc', '+33"; AT', '6', '+', None, 12345, '+' + '1' * 21, '06 12 ab'):
            self.assertIsNone(normalizeNumber(invalid), repr(invalid))

    def testUcs2Detection(self):
        for text in ('Alerte : fuite d\'eau d\u00e9tect\u00e9e ! \u20ac', 'a' * 300, '\u00e9 \u00e8 \u00e0 \u00f9 \u00c7 \u00e4 \u00f6 \u00f1 \u00fc'):
            self.assertFalse(usesUcs2(text), text)
        for text in ('gar\u00e7on', 'fen\u00eatre', 'contr\u00f4le', 'l\u2019eau', '\U0001F600', '\u0436'):
            self.assertTrue(usesUcs2(text), text)
    def testPduParts(self):
        parts = buildParts(NUMBER, 'Hello', reference=3)
        self.assertEqual(len(parts), 1)
        pdu = encodeSmsSubmitPdu(NUMBER, 'Hello', reference=3, requestStatusReport=False)[0]
        self.assertEqual(parts[0].command, f'AT+CMGS={pdu.tpduLength}')
        self.assertEqual(parts[0].body, str(pdu))
        self.assertEqual(parts[0].reference, 3)

    def testInvalidInput(self):
        with self.assertRaises(ValueError):
            buildParts('not a number', 'Hello')
        with self.assertRaises(ValueError):
            buildParts(NUMBER, '  ')

    def testPartsAreEncodedWithTheGivenReferences(self):
        parts = buildParts(NUMBER, 'a' * 400, reference=9, concatReference=1)
        self.assertEqual([p.reference for p in parts], [9, 10, 11])


class ResultTest(unittest.TestCase):
    def testParseCmgs(self):
        self.assertEqual(parseCmgs(['+CMGS: 12', 'OK']), 12)
        self.assertEqual(parseCmgs(['+CMGS:7', 'OK']), 7)
        self.assertIsNone(parseCmgs(['OK']))

    def testTemporaryAndFinalErrors(self):
        for code in (300, 314, 322, 331, 332, 500):
            self.assertTrue(isTemporary(CmsError('x', code)), code)
        for code in (14, 30, 31, 515):
            self.assertTrue(isTemporary(CmeError('x', code)), code)
        for code in (302, 303, 304, 305, 310, 330, 321):
            self.assertFalse(isTemporary(CmsError('x', code)), code)
        for code in (3, 4, 10, 13, 50):
            self.assertFalse(isTemporary(CmeError('x', code)), code)
        self.assertFalse(isTemporary(CommandError('x')))  # unknown: final
        self.assertTrue(isTemporary(NotConnectedError('x')))
        self.assertTrue(isTemporary(TimeoutException()))

    def testReasonNeverContainsTheBodyOfThePart(self):
        body = '0011000B91' + '33' * 20
        self.assertEqual(describe(CmsError(body, 500)), '+CMS ERROR: 500')
        self.assertEqual(describe(CmeError(body, 30)), '+CME ERROR: 30')
        self.assertEqual(describe(CommandError(body)), 'ERROR')
        self.assertEqual(describe(TimeoutException()), 'timeout')
        self.assertEqual(describe(NotConnectedError('x')), 'modem not connected')


class FakeExecutor:
    """ Plays one scripted result per submitted transaction: ``None`` (accepted, TP-MR counted from 100),
    an exception, or a ``(committed, exception)`` pair for a failure after the body was written """

    def __init__(self, *results):
        self.results = list(results)
        self.transactions: list[Transaction] = []

    def submit(self, transaction: Transaction) -> Future:
        self.transactions.append(transaction)
        result = self.results.pop(0) if self.results else None
        future: Future = Future()
        if result is None:
            transaction.committed = True
            future.set_result([f'+CMGS: {99 + len(self.transactions)}', 'OK'])
        elif isinstance(result, tuple):
            transaction.committed, error = result
            future.set_exception(error)
        else:
            future.set_exception(result)
        return future


class DecisionTest(unittest.TestCase):
    def send(self, *results, text='Hello', stop=None, **kwargs):
        executor = FakeExecutor(*results)
        sender = SmsSender(executor.submit, segmentPause=0, stopEvent=stop, **kwargs)
        return sender.send(NUMBER, text), executor

    def testSent(self):
        outcome, executor = self.send()
        self.assertEqual((outcome.status, outcome.parts, outcome.sentParts, outcome.references), ('sent', 1, 1, [100]))
        self.assertEqual(executor.transactions[0].priority, Priority.NEW_SMS)

    def testNextPartsAreContinuations(self):
        outcome, executor = self.send(text='a' * 400)
        self.assertEqual((outcome.status, outcome.parts, outcome.references), ('sent', 3, [100, 101, 102]))
        self.assertEqual([t.priority for t in executor.transactions], [Priority.NEW_SMS, Priority.SMS_CONTINUATION, Priority.SMS_CONTINUATION])

    def testOkWithoutReferenceIsSent(self):
        class NoReference(FakeExecutor):
            def submit(self, transaction):
                future: Future = Future()
                transaction.committed = True
                future.set_result(['OK'])
                return future

        sender = SmsSender(NoReference().submit, segmentPause=0)
        outcome = sender.send(NUMBER, 'Hello')
        self.assertEqual((outcome.status, outcome.references), ('sent', [None]))

    def testTpMrFollowsTheOneOfTheModem(self):
        # the next message starts after the TP-MR returned by the modem (+1, modulo 256)
        seen = []

        def submit(transaction):
            seen.append(transaction.steps[1].data)
            future: Future = Future()
            transaction.committed = True
            future.set_result(['+CMGS: 255', 'OK'])
            return future

        sender = SmsSender(submit, segmentPause=0)
        sender._reference = 7
        sender.send(NUMBER, 'one')
        self.assertEqual(sender._reference, 0)
        sender.send(NUMBER, 'two')
        self.assertEqual(seen[1], str(encodeSmsSubmitPdu(NUMBER, 'two', reference=0, requestStatusReport=False)[0]))

    def testInvalidInputTouchesNothing(self):
        executor = FakeExecutor()
        sender = SmsSender(executor.submit, segmentPause=0)
        self.assertEqual(sender.send('abc', 'Hello').reason, 'invalid number')
        self.assertEqual(sender.send(NUMBER, '').reason, 'empty message')
        self.assertEqual(executor.transactions, [])

    def testUcs2MessageIsAnnouncedInTheLogsWithoutItsText(self):
        with self.assertLogs('wwanlib', 'INFO') as logs:
            outcome, _ = self.send(text='Fuite d\u00e9tect\u00e9e : gar\u00e7on')
        self.assertEqual(outcome.status, 'sent')
        text = '\n'.join(logs.output)
        self.assertIn('1 part(s), UCS-2', text)
        self.assertNotIn('gar\u00e7on', text)
        self.assertNotIn(NUMBER, text)

    def testGsm7MessageIsNotAnnounced(self):
        with self.assertNoLogs('wwanlib', 'INFO'):
            self.send(text='Fuite d\u00e9tect\u00e9e : \u00e0 v\u00e9rifier')
    def testNotConnectedBeforeAnyWriteIsTriedAgain(self):
        outcome, _ = self.send(NotConnectedError('Modem not connected'))
        self.assertEqual((outcome.status, outcome.reason, outcome.sentParts), ('retry', 'modem not connected', 0))

    def testTemporaryRefusalOfTheFirstPartIsTriedAgain(self):
        # explicit refusal at the prompt, or after the body: either way the part did not leave
        for committed in (False, True):
            outcome, _ = self.send((committed, CmsError('x', 331)))
            self.assertEqual((outcome.status, outcome.reason), ('retry', '+CMS ERROR: 331'), committed)

    def testFinalRefusalFails(self):
        for error, reason in ((CmsError('x', 330), '+CMS ERROR: 330'), (CmeError('x', 10), '+CME ERROR: 10'),
                              (CommandError('00110000'), 'ERROR'), (CmsError('x', 999), '+CMS ERROR: 999')):
            outcome, _ = self.send((True, error))
            self.assertEqual((outcome.status, outcome.reason), ('failed', reason))

    def testTimeoutAtThePromptIsTriedAgain(self):
        outcome, _ = self.send((False, TimeoutException()))
        self.assertEqual((outcome.status, outcome.reason), ('retry', 'timeout'))

    def testTimeoutAfterTheBodyIsNeverSentAgain(self):
        outcome, _ = self.send((True, TimeoutException()))
        self.assertEqual((outcome.status, outcome.reason, outcome.sentParts), ('failed', 'timeout', 0))

    def testLostPortAfterTheBodyIsNeverSentAgain(self):
        outcome, _ = self.send((True, NotConnectedError('Modem stopped')))
        self.assertEqual((outcome.status, outcome.reason), ('failed', 'modem not connected'))

    def testFailureOnALaterPartIsNeverSentAgain(self):
        # even a temporary refusal: the first part is out, sending again would duplicate it
        outcome, executor = self.send(None, (True, CmsError('x', 331)), text='a' * 400)
        self.assertEqual((outcome.status, outcome.reason, outcome.parts, outcome.sentParts), ('failed', '+CMS ERROR: 331', 3, 1))
        self.assertEqual(len(executor.transactions), 2)  # the third part was not tried

    def testStopBetweenPartsStopsTheMessage(self):
        stop = threading.Event()
        stop.set()
        outcome, executor = self.send(text='a' * 400, stop=stop)
        self.assertEqual((outcome.status, outcome.reason, outcome.sentParts), ('failed', 'daemon stopped', 1))
        self.assertEqual(len(executor.transactions), 1)


def modemBehavior(refusals=None, mr=None, noPrompt=False, noResult=False):
    """ Fake modem for AT+CMGS: '>' prompt, then +CMGS and OK after Ctrl-Z. ``refusals`` maps the n-th body (1-based)
    or 'prompt' to an end code that replaces the normal answer. """
    refusals = refusals or {}
    mr = mr if mr is not None else iter(range(20, 400))
    bodies = []

    def behavior(fake, data):
        text = data.decode()
        if text.startswith('AT+CMGS'):
            if noPrompt:
                return
            fake.feed(refusals['prompt'].encode() if 'prompt' in refusals else b'\r\n> ')
        elif text.endswith(CTRL_Z):
            bodies.append(text)
            if noResult:
                return
            code = refusals.get(len(bodies))
            fake.feed(code.encode() if code else f'\r\n+CMGS: {next(mr) % 256}\r\n\r\nOK\r\n'.encode())
        elif text.startswith('AT+CMEE?'):
            fake.feed(b'+CMEE: 1\r\nOK\r\n')

    return behavior


class SendingOnAFakePortTest(ExecutorTestCase):
    def sender(self, **kwargs):
        kwargs.setdefault('segmentPause', 0.0)
        return SmsSender(self.executor.submitTransaction, **kwargs)

    def written(self):
        return [data.decode() for _, data in self.fake.written]

    def testShortMessage(self):
        FakeSerial.behavior = modemBehavior()
        pdu = encodeSmsSubmitPdu(NUMBER, 'Hello', reference=0, requestStatusReport=False)[0]
        outcome = self.sender().send(NUMBER, 'Hello')
        self.assertEqual((outcome.status, outcome.references), ('sent', [20]))
        self.assertEqual(self.written(), [f'AT+CMGS={pdu.tpduLength}\r', f'{pdu}{CTRL_Z}'])

    def testPartsAreSentOneAfterTheOther(self):
        FakeSerial.behavior = modemBehavior()
        outcome = self.sender(segmentPause=0.2).send(NUMBER, 'a' * 400)
        self.assertEqual((outcome.status, outcome.parts, outcome.references), ('sent', 3, [20, 21, 22]))
        writes = self.fake.written
        self.assertEqual(len(writes), 6)
        # prompt then body for each part, in this order, never another command in between
        for index in range(0, 6, 2):
            self.assertTrue(writes[index][1].decode().startswith('AT+CMGS='))
            self.assertTrue(writes[index + 1][1].decode().endswith(CTRL_Z))
        # pause between two parts
        self.assertGreaterEqual(writes[2][0] - writes[1][0], 0.15)
        self.assertGreaterEqual(writes[4][0] - writes[3][0], 0.15)

    def testNextMessageContinuesAfterTheTpMrOfTheModem(self):
        FakeSerial.behavior = modemBehavior(mr=iter([200, 201]))
        sender = self.sender()
        sender.send(NUMBER, 'one')
        sender.send(NUMBER, 'two')
        pdu = encodeSmsSubmitPdu(NUMBER, 'two', reference=201, requestStatusReport=False)[0]
        self.assertEqual(self.written()[3], f'{pdu}{CTRL_Z}')

    def testRefusalAtThePromptIsTriedAgain(self):
        FakeSerial.behavior = modemBehavior(refusals={'prompt': '+CMS ERROR: 331\r\n'})
        outcome = self.sender().send(NUMBER, 'Hello')
        self.assertEqual((outcome.status, outcome.reason, outcome.sentParts), ('retry', '+CMS ERROR: 331', 0))
        self.assertEqual(len(self.written()), 1)  # the body was not written

    def testRefusalAfterTheBodyIsTriedAgain(self):
        FakeSerial.behavior = modemBehavior(refusals={1: '\r\n+CMS ERROR: 500\r\n'})
        outcome = self.sender().send(NUMBER, 'Hello')
        self.assertEqual((outcome.status, outcome.reason), ('retry', '+CMS ERROR: 500'))

    def testFinalRefusalDoesNotLeakTheMessage(self):
        FakeSerial.behavior = modemBehavior(refusals={1: '\r\nERROR\r\n'})
        outcome = self.sender().send(NUMBER, 'Hello')
        self.assertEqual((outcome.status, outcome.reason), ('failed', 'ERROR'))

    def testPromptNeverComesIsTriedAgain(self):
        FakeSerial.behavior = modemBehavior(noPrompt=True)
        with mock.patch.object(sms, 'PROMPT_TIMEOUT', 0.2):
            outcome = self.sender().send(NUMBER, 'Hello')
        self.assertEqual((outcome.status, outcome.reason), ('retry', 'timeout'))
        self.assertTrue(waitFor(lambda: any(data == b'\x1b' for _, data in self.fake.written)))  # the input mode is left

    def testNoAnswerAfterTheBodyIsNotSentAgain(self):
        FakeSerial.behavior = modemBehavior(noResult=True)
        with mock.patch.object(sms, 'SUBMIT_TIMEOUT', 0.3):
            started = time.monotonic()
            outcome = self.sender().send(NUMBER, 'Hello')
        self.assertEqual((outcome.status, outcome.reason), ('failed', 'timeout'))
        self.assertLess(time.monotonic() - started, 2.0)
        time.sleep(0.5)
        self.assertEqual(sum(1 for text in self.written() if text.endswith(CTRL_Z)), 1)  # written once, never again

    def testLogsAboveDebugNeverShowTheMessage(self):
        FakeSerial.behavior = modemBehavior(noResult=True)
        body = str(encodeSmsSubmitPdu(NUMBER, 'secret text', reference=0, requestStatusReport=False)[0])
        with mock.patch.object(sms, 'SUBMIT_TIMEOUT', 0.3), self.assertLogs('wwanlib', 'INFO') as logs:
            self.sender().send(NUMBER, 'secret text')
        text = '\n'.join(logs.output)
        self.assertIn('Timeout on <SMS body>', text)
        self.assertNotIn(body, text)
        self.assertNotIn(NUMBER, text)
        self.assertNotIn('secret', text)

    def testCommitIsSetOnlyWhenTheBodyIsWritten(self):
        part = buildParts(NUMBER, 'Hello')[0]
        FakeSerial.behavior = modemBehavior(refusals={'prompt': '+CMS ERROR: 331\r\n'})
        refused = sms.segmentTransaction(part, Priority.NEW_SMS)
        with self.assertRaises(CmsError):
            self.executor.submitTransaction(refused).result(2)
        self.assertFalse(refused.committed)
        FakeSerial.behavior = modemBehavior(refusals={1: '\r\n+CMS ERROR: 500\r\n'})
        accepted = sms.segmentTransaction(part, Priority.NEW_SMS)
        with self.assertRaises(CmsError):
            self.executor.submitTransaction(accepted).result(2)
        self.assertTrue(accepted.committed)
    def testFailureOfASecondPartStopsTheMessage(self):
        FakeSerial.behavior = modemBehavior(refusals={2: '\r\n+CMS ERROR: 331\r\n'})
        outcome = self.sender().send(NUMBER, 'a' * 400)
        self.assertEqual((outcome.status, outcome.parts, outcome.sentParts), ('failed', 3, 1))
        self.assertEqual(sum(1 for text in self.written() if text.endswith(CTRL_Z)), 2)  # the third part never left

    def testConsoleCommandWaitsBetweenTheTwoStepsOfAPart(self):
        # a command from another thread never slips between the prompt and the body
        FakeSerial.behavior = modemBehavior()
        FakeSerial.behavior = self.withCsq(FakeSerial.behavior)
        console = self.executor.submit('AT+CSQ', priority=Priority.CONSOLE)
        outcome = self.sender().send(NUMBER, 'a' * 400)
        self.assertEqual(outcome.status, 'sent')
        console.result(5)
        written = self.written()
        self.assertIn('AT+CSQ\r', written)
        for index, text in enumerate(written):
            if text.startswith('AT+CMGS'):
                self.assertTrue(written[index + 1].endswith(CTRL_Z), written)  # the body follows its prompt at once

    @staticmethod
    def withCsq(inner):
        csq = answer({'AT+CSQ': '+CSQ: 20,99\r\nOK\r\n'})

        def behavior(fake, data):
            if data.decode().startswith('AT+CSQ'):
                csq(fake, data)
            else:
                inner(fake, data)

        return behavior


if __name__ == '__main__':
    unittest.main()
