""" Tests of the reception of SMS (J4, step 1): reading, decoding, reassembly of the long ones, and the Modem on a fake
port that stores the SMS it receives.

Run from resources/sms4gd:  python -m unittest discover -s tests -t .
"""

import logging
import threading
import unittest
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from unittest import mock

import serial

from tests.test_wwanlib import FakeSerial, answer, simcomTable, waitFor
from wwanlib import ConnectionState, Modem, ModemOptions, SmsIncomplete, SmsReceived
from wwanlib.exceptions import CmsError, NotConnectedError
from wwanlib.executor import Executor, Priority, Transaction
from wwanlib.inbox import Inbox, Reassembler
from wwanlib.pdu import _encodeAddressField, _encodeTimestamp, encodeSmsSubmitPdu
from wwanlib.receipts import ReceiptTracker

NUMBER = '+33612345678'
# Service center time stamp 2026-10-04 12:30:00, time zone +2 h (8 quarters of an hour)
SCTS = bytes.fromhex('62014021030080')
SENT = datetime(2026, 10, 4, 12, 30, 0, tzinfo=timezone(timedelta(hours=2))).timestamp()


def deliverPdus(number: str, text: str, reference: int = 0x42) -> list[str]:
    """ The SMS-DELIVER PDUs a network would hand over for ``text`` (built from the SMS-SUBMIT encoder of the library:
    same user data, an originating address and a time stamp instead of the reference) """
    result = []
    for pdu in encodeSmsSubmitPdu(number, text, requestStatusReport=False, concatReference=reference):
        raw = bytes.fromhex(str(pdu))
        first = raw[1]
        addressEnd = 5 + (raw[3] + 1) // 2
        address, rest = raw[3:addressEnd], raw[addressEnd:]
        deliver = b'\x00' + bytes([first & 0x40]) + address + rest[:2] + SCTS + rest[2:]
        result.append(deliver.hex().upper())
    return result


def statusReportPdu(reference: int, number: str, status: int = 0, when: datetime | None = None,
                    delivered: datetime | None = None) -> str:
    """ The SMS-STATUS-REPORT PDU of a network about the message sent to ``number`` with this TP-MR: the time the SMS
    center received the message (``when``, now by default) and the time of the delivery (``delivered``, the same
    by default). When they differ, the delivery goes **first** as Free does (the standard puts the other first). """
    sent = when or datetime.now(timezone.utc).replace(microsecond=0)
    first = bytes(_encodeTimestamp(delivered or sent))
    second = bytes(_encodeTimestamp(sent))
    return (b'\x00\x06' + bytes([reference]) + bytes(_encodeAddressField(number)) + first + second + bytes([status])).hex().upper()


def cmgrLines(stat: int, pdu: str) -> list[str]:
    return [f'+CMGR: {stat},,{len(pdu) // 2 - 1}', pdu, 'OK']


class FakeSim:
    """ The SMS memory of a modem and the transactions of the inbox, played without a serial port """

    def __init__(self):
        self.store: dict[int, tuple[int, str]] = {}  # index -> (stat, pdu)
        self.transactions: list[Transaction] = []
        self.errors: dict[str, Exception] = {}  # command -> the error it ends with

    def add(self, index: int, pdu: str, stat: int = 0) -> None:
        self.store[index] = (stat, pdu)

    def submit(self, transaction: Transaction) -> Future:
        self.transactions.append(transaction)
        transaction.responses = []
        future: Future = Future()
        lines: list[str] = []
        for step in transaction.steps:
            command = step.data
            error = self.errors.get(command)
            if command.startswith('AT+CMGR=') and error is None and int(command[8:]) not in self.store:
                error = CmsError(command, 321)
            if error is not None:
                transaction.responses.append([f'+CMS ERROR: {error.code}'])
                future.set_exception(error)
                return future
            if command.startswith('AT+CMGR='):
                stat, pdu = self.store[int(command[8:])]
                lines = cmgrLines(stat, pdu)
            elif command.startswith('AT+CMGD='):
                self.store.pop(int(command[8:]), None)
                lines = ['OK']
            elif command == 'AT+CMGL=4':
                lines = []
                for index, (stat, pdu) in sorted(self.store.items()):
                    lines += [f'+CMGL: {index},{stat},,{len(pdu) // 2 - 1}', pdu]
                lines.append('OK')
            else:
                lines = ['+CPMS: 0,100,0,100,0,100', 'OK']
            transaction.responses.append(lines)
        future.set_result(lines)
        return future

    def commands(self) -> list[list[str]]:
        return [[step.data for step in transaction.steps] for transaction in self.transactions]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class ReassemblerTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.reassembler = Reassembler(ttl=300.0, maxGroups=2, memory=3600.0, maxRemembered=3, clock=self.clock)

    def add(self, position, text, reference=7, expected=3, number=NUMBER, sent=SENT):
        return self.reassembler.add(number, reference, expected, position, text, sent)

    def testTheMessageIsCompleteWhenTheLastPartArrives(self):
        self.assertEqual(self.add(1, 'abc '), [])
        self.assertEqual(self.add(2, 'def '), [])
        self.assertEqual(self.add(3, 'ghi'), [SmsReceived(NUMBER, 'abc def ghi', SENT, 3)])
        self.assertEqual(len(self.reassembler), 0)

    def testThePartsAreSortedWhateverTheirOrderOfArrival(self):
        self.add(3, 'ghi')
        self.add(1, 'abc ')
        self.assertEqual(self.add(2, 'def ')[0].text, 'abc def ghi')

    def testTheEarliestDateIsKept(self):
        self.add(2, 'b', sent=SENT + 5)
        self.add(1, 'a', sent=SENT)
        self.assertEqual(self.add(3, 'c', sent=SENT + 10)[0].sent, SENT)

    def testARepeatedPartIsIgnored(self):
        self.add(1, 'a')
        self.add(1, 'CHANGED')
        self.add(2, 'b')
        self.assertEqual(self.add(3, 'c')[0].text, 'abc')

    def testMessagesAreToldApartBySenderReferenceAndSize(self):
        self.reassembler = Reassembler(maxGroups=10)
        self.add(1, 'a')
        self.add(1, 'x', reference=8)
        self.add(1, 'y', number='+33600000000')
        self.add(1, 'z', expected=2)
        self.assertEqual(len(self.reassembler), 4)

    def testAMessageThatStaysIncompleteIsGivenUp(self):
        self.add(1, 'a')
        self.add(3, 'c')
        self.clock.now += 299
        self.assertEqual(self.reassembler.expire(), [])
        self.clock.now += 2
        self.assertEqual(self.reassembler.expire(), [SmsIncomplete(NUMBER, 2, 3, 'timeout')])
        self.assertEqual(len(self.reassembler), 0)

    def testTheLifetimeStartsWithTheFirstPart(self):
        self.add(1, 'a')
        self.clock.now += 200
        self.add(2, 'b')
        self.clock.now += 101
        self.assertEqual(self.reassembler.expire(), [SmsIncomplete(NUMBER, 2, 3, 'timeout')])

    def testALatePartOfAnAbandonedMessageIsIgnored(self):
        self.add(1, 'a')
        self.clock.now += 301
        self.reassembler.expire()
        with self.assertLogs('wwanlib.inbox', level='WARNING') as logs:
            self.assertEqual(self.add(2, 'b'), [])
        self.assertIn('already abandoned', logs.output[0])
        self.assertEqual(len(self.reassembler), 0)

    def testARepeatedPartAfterTheDeliveryIsIgnored(self):
        self.add(1, 'a', expected=2)
        self.assertEqual(len(self.add(2, 'b', expected=2)), 1)
        self.assertEqual(self.add(2, 'b', expected=2), [])
        self.assertEqual(len(self.reassembler), 0)

    def testWhatWasAbandonedIsForgottenAfterAWhile(self):
        self.add(1, 'a')
        self.clock.now += 301
        self.reassembler.expire()
        self.clock.now += 3600
        self.add(1, 'a')
        self.assertEqual(len(self.reassembler), 1)

    def testTheOldestMessageIsGivenUpWhenThereAreTooMany(self):
        self.add(1, 'a', reference=1)
        self.add(1, 'b', reference=2)
        self.assertEqual(self.add(1, 'c', reference=3), [SmsIncomplete(NUMBER, 1, 3, 'overflow')])
        self.assertEqual(len(self.reassembler), 2)
        # reference 1 was the one given up: its next part is ignored, reference 2 goes on
        self.assertEqual(self.add(2, 'x', reference=1), [])
        self.add(2, 'y', reference=2)
        self.assertEqual(len(self.reassembler), 2)

    def testTheMemoryOfClosedMessagesIsLimited(self):
        for reference in range(5):
            self.add(1, 'a', reference=reference, expected=1 + 1)
            self.add(2, 'b', reference=reference, expected=2)
        self.assertEqual(len(self.reassembler._closed), 3)

    def testTheTwoHalvesOfAnEmojiMeetAgain(self):
        self.add(1, 'a\ud83d', expected=2)
        self.assertEqual(self.add(2, '\ude00b', expected=2)[0].text, 'a\U0001F600b')

    def testALoneHalfBecomesAReplacementCharacter(self):
        self.add(1, 'a\ud83d', expected=2)
        self.assertEqual(self.add(2, 'b', expected=2)[0].text, 'a\ufffdb')


class InboxTest(unittest.TestCase):
    def setUp(self):
        self.sim = FakeSim()
        self.events: list = []
        self.memory = 'SM'
        self.inbox = Inbox(self.sim.submit, self.events.append, lambda: self.memory,
                           Reassembler(ttl=300.0, maxGroups=50))

    def received(self) -> list[SmsReceived]:
        return [event for event in self.events if isinstance(event, SmsReceived)]

    def notify(self, index: int, memory: str = 'SM') -> None:
        self.inbox.onNotification([f'+CMTI: "{memory}",{index}'])

    def testAShortSmsIsReadThenDeleted(self):
        self.sim.add(0, deliverPdus('+33698765432', 'Hello')[0])
        self.notify(0)
        self.assertEqual(self.events, [SmsReceived('+33698765432', 'Hello', SENT, 1)])
        self.assertEqual(self.sim.commands(), [['AT+CMGR=0', 'AT+CMGD=0']])
        self.assertEqual(self.sim.transactions[0].priority, Priority.MEMORY_RELEASE)
        self.assertEqual(self.sim.store, {})

    def testTheIndexZeroIsAnIndex(self):
        self.sim.add(0, deliverPdus(NUMBER, 'zero')[0])
        self.sim.add(1, deliverPdus(NUMBER, 'one')[0])
        self.notify(0)
        self.notify(1)
        self.assertEqual([event.text for event in self.received()], ['zero', 'one'])

    def testAccentsAndTheEuroSign(self):
        self.sim.add(0, deliverPdus(NUMBER, 'Température élevée : 25 € {ok}')[0])
        self.notify(0)
        self.assertEqual(self.received()[0].text, 'Température élevée : 25 € {ok}')

    def testUcs2WithAnEmoji(self):
        self.sim.add(0, deliverPdus(NUMBER, 'Allumé ç ê ô ’ \U0001F600')[0])
        self.notify(0)
        self.assertEqual(self.received()[0].text, 'Allumé ç ê ô ’ \U0001F600')

    def testALongSmsInGsm7IsReassembled(self):
        text = ' '.join(f'{index:02d}-abcdefghij' for index in range(1, 29))
        pdus = deliverPdus(NUMBER, text)
        self.assertEqual(len(pdus), 3)
        for index, pdu in enumerate(pdus):
            self.sim.add(index, pdu)
        self.notify(2)
        self.notify(0)
        self.assertEqual(self.events, [])
        self.notify(1)
        self.assertEqual(self.events, [SmsReceived(NUMBER, text, SENT, 3)])
        self.assertEqual(self.sim.store, {})  # every part was deleted when it was read

    def testALongSmsInUcs2IsReassembled(self):
        text = ''.join(f'é{index:03d} ' for index in range(60)) + '\U0001F600'
        pdus = deliverPdus(NUMBER, text, reference=0x7F)
        self.assertGreater(len(pdus), 2)
        for index, pdu in enumerate(pdus):
            self.sim.add(index, pdu)
            self.notify(index)
        self.assertEqual(self.received()[0].text, text)
        self.assertEqual(self.received()[0].parts, len(pdus))

    def testNothingIsDeliveredForAnIncompleteMessage(self):
        pdus = deliverPdus(NUMBER, 'x' * 400)
        self.sim.add(0, pdus[0])
        self.sim.add(1, pdus[2])
        self.notify(0)
        self.notify(1)
        self.assertEqual(self.events, [])
        self.assertEqual(len(self.inbox.reassembler), 1)

    def testSweepGivesUpTheIncompleteMessagesAndSaysSo(self):
        clock = Clock()
        self.inbox = Inbox(self.sim.submit, self.events.append, lambda: self.memory, Reassembler(ttl=60.0, clock=clock))
        self.sim.add(0, deliverPdus(NUMBER, 'x' * 400)[0])
        self.notify(0)
        self.inbox.sweep()
        self.assertEqual(self.events, [])
        clock.now += 61
        self.inbox.sweep()
        self.assertEqual(self.events, [SmsIncomplete(NUMBER, 1, 3, 'timeout')])

    def testANotificationAboutAnotherMemorySelectsItFirst(self):
        self.sim.add(2, deliverPdus(NUMBER, 'Hello')[0])
        self.notify(2, memory='ME')
        self.assertEqual(self.sim.commands(), [['AT+CPMS="ME"', 'AT+CMGR=2', 'AT+CMGD=2', 'AT+CPMS="SM"']])  # and the usual one back
        self.assertEqual(self.received()[0].text, 'Hello')

    def testTheSelectedMemoryIsNotSelectedAgain(self):
        self.sim.add(2, deliverPdus(NUMBER, 'Hello')[0])
        self.notify(2, memory='sm')
        self.assertEqual(self.sim.commands(), [['AT+CMGR=2', 'AT+CMGD=2']])

    def testAFailedReadingDeletesNothing(self):
        self.sim.add(0, deliverPdus(NUMBER, 'Hello')[0])
        self.sim.errors['AT+CMGR=0'] = CmsError('AT+CMGR=0', 500)
        with self.assertLogs('wwanlib.inbox', level='WARNING') as logs:
            self.notify(0)
        self.assertIn('could not be read', logs.output[0])
        self.assertEqual(self.events, [])
        self.assertEqual(self.sim.commands(), [['AT+CMGR=0', 'AT+CMGD=0']])  # the Executor stops at the first failure
        self.assertEqual(self.sim.transactions[0].responses, [['+CMS ERROR: 500']])
        self.assertIn(0, self.sim.store)

    def testAnEmptySlotIsNotAnError(self):
        with self.assertNoLogs('wwanlib.inbox', level='WARNING'):
            self.notify(4)
        self.assertEqual(self.events, [])

    def testAnSmsThatCannotBeDecodedIsDeletedAndLogged(self):
        self.sim.add(0, 'FFFFFFFF')
        with self.assertLogs('wwanlib.inbox', level='ERROR') as logs:
            self.notify(0)
        self.assertIn('SMS at index 0 unreadable (4 bytes), deleted', logs.output[0])
        self.assertEqual(self.events, [])
        self.assertEqual(self.sim.store, {})

    def testAReadSmsWhoseDeletionFailedIsDeliveredOnlyOnce(self):
        self.sim.add(0, deliverPdus(NUMBER, 'Hello')[0])
        self.sim.errors['AT+CMGD=0'] = CmsError('AT+CMGD=0', 500)
        with self.assertLogs('wwanlib.inbox', level='WARNING') as logs:
            self.notify(0)
            self.notify(0)
        self.assertIn('read but not deleted', logs.output[0])
        self.assertEqual(len(self.received()), 1)
        self.assertIn(0, self.sim.store)

    def testNotConnectedIsNotAWarning(self):
        def refuse(transaction):
            future: Future = Future()
            future.set_exception(NotConnectedError('not connected'))
            return future

        self.inbox = Inbox(refuse, self.events.append, lambda: self.memory)
        with self.assertNoLogs('wwanlib.inbox', level='WARNING'):
            self.notify(0)
        self.assertEqual(self.events, [])

    def testOtherNotificationsAreIgnored(self):
        self.inbox.onNotification(['+CDS: 26', '07913396050056F906DA', 'RING', '+CMTI: garbage'])
        self.assertEqual(self.sim.transactions, [])

    def testTheTextIsInTheInfoLogsAndTheNumberIsMasked(self):
        self.sim.add(0, deliverPdus('+33698765432', 'Code 123456\nvalable 5 min')[0])
        with self.assertLogs('wwanlib.inbox', level='INFO') as logs:
            self.notify(0)
        text = '\n'.join(logs.output)
        self.assertIn("SMS received from +336XXXXXX32 (25 characters): 'Code 123456\\nvalable 5 min'", text)
        self.assertNotIn('+33698765432', text)

    def testEveryPartOfALongSmsIsInTheInfoLogs(self):
        pdus = deliverPdus(NUMBER, 'a' * 200 + 'b' * 200)
        for index, pdu in enumerate(pdus):
            self.sim.add(index, pdu)
        with self.assertLogs('wwanlib.inbox', level='INFO') as logs:
            for index in range(len(pdus)):
                self.notify(index)
        text = '\n'.join(logs.output)
        self.assertIn('SMS part 1/3 received from +336XXXXXX78 (reference 66): ' + repr('a' * 153), text)
        self.assertIn('SMS part 3/3 received', text)

    def testTheTextOfAnAbandonedMessageIsStillInTheLogs(self):
        self.sim.add(0, deliverPdus(NUMBER, 'x' * 400)[0])
        with self.assertLogs('wwanlib.inbox', level='INFO') as logs:
            self.notify(0)
        self.assertIn('x' * 153, '\n'.join(logs.output))
    def testCatchUpReadsTheReceivedSmsAndLeavesTheOthersAlone(self):
        self.sim.add(0, deliverPdus(NUMBER, 'first')[0], stat=1)
        self.sim.add(1, deliverPdus(NUMBER, 'sent by us')[0], stat=3)
        self.sim.add(2, deliverPdus(NUMBER, 'second')[0], stat=0)
        self.inbox.catchUp()
        self.assertEqual([event.text for event in self.received()], ['first', 'second'])
        self.assertEqual(sorted(self.sim.store), [1])
        self.assertEqual(self.sim.commands()[0], ['AT+CMGL=4'])

    def testCatchUpIsNotRepeatedWhileItRuns(self):
        gate = Future()
        submitted = []

        def submit(transaction):
            submitted.append(transaction)
            return gate

        self.inbox = Inbox(submit, self.events.append, lambda: self.memory)
        self.inbox.catchUp()
        self.inbox.catchUp()
        self.assertEqual(len(submitted), 1)
        gate.set_result(['OK'])
        self.inbox.catchUp()
        self.assertEqual(len(submitted), 2)

    def testTheMemoryCheckFindsAnSmsWithoutNotification(self):
        usage = lambda used: [f'+CPMS: "SM",{used},100,"SM",{used},100,"SM",{used},100', 'OK']  # noqa: E731
        self.assertFalse(self.inbox.checkMemory(usage(0)))
        self.assertTrue(self.inbox.checkMemory(usage(1)))

    def testWhatIsLeftAloneIsNotAnAlarm(self):
        usage = lambda used: [f'+CPMS: "SM",{used},100,"SM",{used},100,"SM",{used},100', 'OK']  # noqa: E731
        self.sim.add(1, deliverPdus(NUMBER, 'sent by us')[0], stat=3)
        self.sim.add(5, deliverPdus(NUMBER, 'unreadable')[0], stat=0)
        self.sim.errors['AT+CMGR=5'] = CmsError('AT+CMGR=5', 500)
        with self.assertLogs('wwanlib.inbox', level='WARNING'):
            self.inbox.catchUp()
        self.assertFalse(self.inbox.checkMemory(usage(2)))
        self.assertTrue(self.inbox.checkMemory(usage(3)))

    def testTheMemoryWarningComesOncePerCrossing(self):
        usage = lambda used: ['+CPMS: "SM",%d,100,"SM",%d,100,"SM",%d,100' % (used, used, used), 'OK']  # noqa: E731
        with self.assertLogs('wwanlib.inbox', level='WARNING') as logs:
            self.inbox.checkMemory(usage(79))
            self.inbox.checkMemory(usage(80))
            self.inbox.checkMemory(usage(90))
            self.inbox.checkMemory(usage(10))
            self.inbox.checkMemory(usage(85))
        self.assertEqual(len([line for line in logs.output if 'full' in line]), 2)
        self.assertIn('80% full (80/100)', logs.output[0])

    def testAnUnreadableAnswerOfTheMemoryCheckIsNotAnAlarm(self):
        self.assertFalse(self.inbox.checkMemory(['ERROR']))


class DeliveryReportTest(unittest.TestCase):
    def setUp(self):
        self.sim = FakeSim()
        self.events: list = []
        self.tracker = ReceiptTracker(self.events.append)
        self.inbox = Inbox(self.sim.submit, self.events.append, lambda: 'SM', receipts=self.tracker)
        self.tracker.register('a', '42', '0662032692', 1, 1, 218)
        self.tracker.close('a')

    def testADirectReportReachesTheTracker(self):
        self.inbox.onNotification(['+CDS: 26', statusReportPdu(218, '+33662032692')])
        self.assertEqual([(event.smsId, event.status) for event in self.events], [('a', 'delivered')])
        self.assertEqual(self.sim.transactions, [])  # nothing is read: the report came with the notification

    def testADeliveryHoursAfterTheSendingIsMatched(self):
        # the report of a message delivered much later (the phone was off): the delivery time is first, far from the sending
        now = datetime.now(timezone.utc).replace(microsecond=0)
        pdu = statusReportPdu(218, '+33662032692', when=now, delivered=now + timedelta(hours=5))
        self.inbox.onNotification(['+CDS: 26', pdu])
        self.assertEqual([(event.smsId, event.status) for event in self.events], [('a', 'delivered')])

    def testTheStatusOfThePduIsUsed(self):
        self.inbox.onNotification(['+CDS: 26', statusReportPdu(218, '+33662032692', status=0x43)])
        self.assertEqual([(event.status, event.reason) for event in self.events], [('undelivered', 'not obtainable')])

    def testAReportIsNeverAReceivedSms(self):
        self.inbox.onNotification(['+CDS: 26', statusReportPdu(218, '+33662032692')])
        self.assertFalse([event for event in self.events if isinstance(event, SmsReceived)])

    def testAReportThatWasNotAskedForIsIgnored(self):
        inbox = Inbox(self.sim.submit, self.events.append, lambda: 'SM')
        with self.assertNoLogs('wwanlib.inbox', level='WARNING'):
            inbox.onNotification(['+CDS: 26', statusReportPdu(218, '+33662032692')])
        self.assertEqual(self.events, [])

    def testALineThatIsNotAPduAfterCdsIsNotAReport(self):
        self.inbox.onNotification(['+CDS: 26', 'OK'])
        self.inbox.onNotification(['+CDS: 26'])
        self.assertEqual(self.events, [])

    def testAnUnreadableReportIsLogged(self):
        with self.assertLogs('wwanlib.inbox', level='ERROR') as logs:
            self.inbox.onNotification(['+CDS: 26', 'FFFF'])
        self.assertIn('Delivery report unreadable (2 bytes)', logs.output[0])

    def testAReportKeptInTheSrMemoryIsReadThenDeleted(self):
        self.sim.add(0, statusReportPdu(218, '+33662032692'), stat=1)
        self.inbox.onNotification(['+CDSI: "SR",0'])
        self.assertEqual(self.sim.commands(), [['AT+CPMS="SR"', 'AT+CMGR=0', 'AT+CMGD=0', 'AT+CPMS="SM"']])
        self.assertEqual(self.sim.store, {})
        self.assertEqual([(event.smsId, event.status) for event in self.events], [('a', 'delivered')])

    def testTheMemoryIsSelectedBackWhenTheReadFails(self):
        # seen on a SIM7600: +CDSI announces an index that SR does not hold (+CMS ERROR: 321), the modem would stay on SR
        self.inbox.onNotification(['+CDSI: "SR",27'])
        self.assertEqual(self.sim.commands(), [['AT+CPMS="SR"', 'AT+CMGR=27', 'AT+CMGD=27', 'AT+CPMS="SM"'],
                                               ['AT+CPMS="SM"']])

    def testTheMemoryIsSelectedBackEvenWhenTheDeletionFailed(self):
        self.sim.add(0, statusReportPdu(218, '+33662032692'), stat=1)
        self.sim.errors['AT+CMGD=0'] = CmsError('AT+CMGD=0', 500)
        with self.assertLogs('wwanlib.inbox', level='WARNING') as logs:
            self.inbox.onNotification(['+CDSI: "SR",0'])
        self.assertIn('read but not deleted', logs.output[0])
        self.assertEqual([event.status for event in self.events], ['delivered'])


def simBehavior(sim: FakeSim, table: dict | None = None):
    """ Fake modem: the answers of ``simcomTable`` plus the SMS memory of ``sim`` (AT+CMGR, AT+CMGD, AT+CMGL, AT+CPMS?) """
    simple = answer(table if table is not None else simcomTable(), echo=True)

    def behavior(fake, data):
        command = data.decode().rstrip('\r')
        text = None
        if command.startswith('AT+CMGR='):
            index = int(command[8:])
            text = ('\r\n' + '\r\n'.join(cmgrLines(*sim.store[index])[:-1]) + '\r\n\r\nOK\r\n') if index in sim.store \
                else '\r\n+CMS ERROR: 321\r\n'
        elif command.startswith('AT+CMGD='):
            sim.store.pop(int(command[8:]), None)
            text = '\r\nOK\r\n'
        elif command == 'AT+CMGL=4':
            body = ''.join(f'\r\n+CMGL: {index},{stat},,{len(pdu) // 2 - 1}\r\n{pdu}' for index, (stat, pdu) in sorted(sim.store.items()))
            text = body + '\r\n\r\nOK\r\n'
        elif command == 'AT+CPMS?':
            used = len(sim.store)
            text = f'\r\n+CPMS: "SM",{used},100,"SM",{used},100,"SM",{used},100\r\n\r\nOK\r\n'
        if text is None:
            simple(fake, data)
        else:
            fake.feed(command.encode() + b'\r\n' + text.encode())

    return behavior


class ModemReceptionTest(unittest.TestCase):
    def setUp(self):
        FakeSerial.instances.clear()
        self.sim = FakeSim()
        FakeSerial.behavior = simBehavior(self.sim)
        self.blocked = threading.Event()

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

    def makeModem(self, **options) -> Modem:
        modem = Modem('fake', 115200, options=ModemOptions(
            reconnectBaseDelay=0.05, reconnectMaxDelay=0.1, reconnectMaxAttempts=100, segmentPause=0.0, **options))
        modem.onEvent(self.events.append)
        self.addCleanup(modem.stop)
        return modem

    def of(self, kind) -> list:
        return [event for event in list(self.events) if isinstance(event, kind)]

    def start(self, **options) -> Modem:
        modem = self.makeModem(**options)
        modem.start()
        self.assertTrue(waitFor(lambda: modem.state == ConnectionState.CONNECTED))
        return modem

    def commands(self) -> list[str]:
        return [command for fake in FakeSerial.instances for command in fake.commands()]

    def testAnSmsAnnouncedByTheModemIsReadAndDeleted(self):
        self.start()
        self.sim.add(0, deliverPdus('+33698765432', 'Hello')[0])
        FakeSerial.instances[0].feed(b'\r\n+CMTI: "SM",0\r\n')
        self.assertTrue(waitFor(lambda: self.of(SmsReceived)))
        self.assertEqual(self.of(SmsReceived), [SmsReceived('+33698765432', 'Hello', SENT, 1)])
        commands = self.commands()
        self.assertEqual(commands[commands.index('AT+CMGR=0') + 1], 'AT+CMGD=0')
        self.assertEqual(self.sim.store, {})

    def testALongSmsArrivesAsOneMessage(self):
        self.start()
        text = ' '.join(f'{index:02d}-abcdefghij' for index in range(1, 29))
        for index, pdu in enumerate(deliverPdus(NUMBER, text)):
            self.sim.add(index, pdu)
            FakeSerial.instances[0].feed(f'\r\n+CMTI: "SM",{index}\r\n'.encode())
        self.assertTrue(waitFor(lambda: self.of(SmsReceived)))
        self.assertEqual(self.of(SmsReceived), [SmsReceived(NUMBER, text, SENT, 3)])
        self.assertEqual(self.sim.store, {})

    def testTheSmsStoredWhileTheDaemonWasStoppedAreReadAtTheConnection(self):
        self.sim.add(0, deliverPdus(NUMBER, 'first')[0], stat=1)
        self.sim.add(1, deliverPdus(NUMBER, 'second')[0], stat=0)
        self.start()
        self.assertTrue(waitFor(lambda: len(self.of(SmsReceived)) == 2))
        self.assertEqual([event.text for event in self.of(SmsReceived)], ['first', 'second'])
        self.assertEqual(self.sim.store, {})
        self.assertIn('AT+CMGL=4', self.commands())

    def testTheSmsStoredWhileTheModemWasUnpluggedAreReadAfterTheReconnection(self):
        self.start()
        self.blocked.set()
        FakeSerial.instances[0].unplug()
        self.sim.add(0, deliverPdus(NUMBER, 'while away')[0])
        self.blocked.clear()
        self.assertTrue(waitFor(lambda: self.of(SmsReceived)))
        self.assertEqual(self.of(SmsReceived)[0].text, 'while away')

    def testALostNotificationIsFoundByTheMonitoring(self):
        self.start(monitorInterval=0.1)
        self.sim.add(3, deliverPdus(NUMBER, 'no notification')[0])  # no +CMTI for this one
        self.assertTrue(waitFor(lambda: self.of(SmsReceived)))
        self.assertEqual(self.of(SmsReceived)[0].text, 'no notification')
        self.assertEqual(self.sim.store, {})

    def testAnSmsLeftAloneDoesNotMakeTheMonitoringReadAgainAndAgain(self):
        self.sim.add(1, deliverPdus(NUMBER, 'sent by us')[0], stat=3)
        self.start(monitorInterval=0.1)
        waitFor(lambda: self.commands().count('AT+CPMS?') >= 4)
        self.assertEqual(self.commands().count('AT+CMGL=4'), 1)  # the one of the connection
        self.assertEqual(self.of(SmsReceived), [])

    def testALongSmsThatStaysIncompleteIsGivenUp(self):
        self.start(monitorInterval=0.1, concatPartsTtl=0.3)
        self.sim.add(0, deliverPdus(NUMBER, 'x' * 400)[0])
        FakeSerial.instances[0].feed(b'\r\n+CMTI: "SM",0\r\n')
        self.assertTrue(waitFor(lambda: self.of(SmsIncomplete)))
        self.assertEqual(self.of(SmsIncomplete), [SmsIncomplete(NUMBER, 1, 3, 'timeout')])
        self.assertEqual(self.of(SmsReceived), [])
        self.assertEqual(self.sim.store, {})

    def testTheNotificationsStillReachTheSubscribers(self):
        self.start()
        self.sim.add(0, deliverPdus(NUMBER, 'Hello')[0])
        FakeSerial.instances[0].feed(b'\r\n+CMTI: "SM",0\r\n')
        self.assertTrue(waitFor(lambda: self.of(SmsReceived)))
        notifications = [event.lines for event in self.events if type(event).__name__ == 'UnsolicitedNotification']
        self.assertIn(['+CMTI: "SM",0'], notifications)


if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    unittest.main()
