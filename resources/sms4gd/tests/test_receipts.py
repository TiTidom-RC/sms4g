""" Tests of the delivery reports tracker (J5, step 1): the states of an SMS from the reports of its parts.

Run from resources/sms4gd:  python -m unittest discover -s tests -t .
"""

import unittest

from tests.test_inbox import Clock
from wwanlib import SmsDelivery
from wwanlib.receipts import DEFAULT_MAX_AGE, MAX_TRACKED, ReceiptTracker, classify

NUMBER = '0662032692'
REPORT_NUMBER = '+33662032692'


class ClassifyTest(unittest.TestCase):
    def testTheRangesOfTheStandard(self):
        self.assertEqual([classify(status)[0] for status in (0x00, 0x01, 0x1F)], ['delivered'] * 3)
        self.assertEqual([classify(status)[0] for status in (0x20, 0x25, 0x3F)], ['pending'] * 3)
        self.assertEqual([classify(status)[0] for status in (0x40, 0x4A, 0x5F)], ['failed'] * 3)
        self.assertEqual([classify(status)[0] for status in (0x60, 0x7F)], ['failed'] * 2)
        self.assertEqual([classify(status)[0] for status in (0x80, 0xFF)], ['unknown'] * 2)

    def testTheReasonOfATemporaryAndOfAPermanentError(self):
        self.assertEqual(classify(0x21), ('pending', 'recipient busy'))
        self.assertEqual(classify(0x3F), ('pending', 'temporary error 0x3F'))
        self.assertEqual(classify(0x43), ('failed', 'not obtainable'))
        self.assertIn('given up', classify(0x62)[1])


class TrackerTestCase(unittest.TestCase):
    def setUp(self):
        self.events: list[SmsDelivery] = []
        self.clock = Clock()  # monotonic
        self.wall = Clock()  # time of the day, the same origin as the reports
        self.tracker = ReceiptTracker(self.events.append, clock=self.clock, wallClock=self.wall)

    def send(self, smsId='a', parts=1, references=(10,), number=NUMBER, ref='42', close=True):
        for index, reference in enumerate(references, start=1):
            self.tracker.register(smsId, ref, number, index, parts, reference)
        if close:
            self.tracker.close(smsId)

    def report(self, reference=10, status=0, number=REPORT_NUMBER, when=None):
        self.tracker.onReport(reference, number, self.wall.now if when is None else when, status)

    def states(self) -> list[str]:
        return [event.status for event in self.events]


class SinglePartTest(TrackerTestCase):
    def testDelivered(self):
        self.send()
        self.report()
        self.assertEqual(self.events, [SmsDelivery('a', '42', NUMBER, 'delivered', '', 1, 1)])
        self.assertEqual(len(self.tracker), 0)  # nothing more to wait for

    def testNothingIsPublishedWithoutAReport(self):
        self.send()
        self.assertEqual(self.events, [])

    def testPendingThenDelivered(self):
        self.send()
        self.report(status=0x21)
        self.report(status=0x21)  # the same state again is not published again
        self.report(status=0x00)
        self.assertEqual(self.states(), ['pending', 'delivered'])
        self.assertEqual(self.events[0].reason, 'recipient busy')

    def testFailedForGood(self):
        self.send()
        self.report(status=0x43)
        self.assertEqual(self.events, [SmsDelivery('a', '42', NUMBER, 'undelivered', 'not obtainable', 1, 0)])

    def testAFinalStateIsNeverTakenBack(self):
        self.send()
        self.report(status=0x00)
        self.report(status=0x43)
        self.report(status=0x21)
        self.assertEqual(self.states(), ['delivered'])

    def testPendingNeverFollowsAFinalReportOfThePart(self):
        self.send(parts=2, references=(10, 11))
        self.report(reference=10, status=0x00)
        self.report(reference=10, status=0x21)  # a repeated or reordered report
        self.assertEqual(self.events, [])

    def testAnUnusableStatusIsIgnored(self):
        self.send()
        with self.assertLogs('wwanlib.receipts', level='WARNING'):
            self.report(status=0x90)
        self.assertEqual(self.events, [])


class BeforeTheAnnouncementTest(TrackerTestCase):
    def testAReportBeforeTheRegistrationWaitsForIt(self):
        self.report()
        self.send()
        self.assertEqual(self.states(), ['delivered'])

    def testAReportBeforeTheAnnouncementIsPublishedAfterIt(self):
        self.send(close=False)
        self.report()
        self.assertEqual(self.events, [])  # "sent" has not been told yet
        self.tracker.close('a')
        self.assertEqual(self.states(), ['delivered'])

    def testTheFirstPartIsDeliveredWhileTheSecondIsBeingSent(self):
        self.tracker.register('a', '42', NUMBER, 1, 3, 10)
        self.report(reference=10)
        self.tracker.register('a', '42', NUMBER, 2, 3, 11)
        self.tracker.register('a', '42', NUMBER, 3, 3, 12)
        self.tracker.close('a')
        self.assertEqual(self.events, [])  # one part out of three
        self.report(reference=11)
        self.report(reference=12)
        self.assertEqual(self.events, [SmsDelivery('a', '42', NUMBER, 'delivered', '', 3, 3)])


class SeveralPartsTest(TrackerTestCase):
    def testDeliveredWhenEveryPartIs(self):
        self.send(parts=3, references=(10, 11, 12))
        self.report(reference=12)
        self.report(reference=10)
        self.assertEqual(self.events, [])
        self.report(reference=11)
        self.assertEqual(self.events, [SmsDelivery('a', '42', NUMBER, 'delivered', '', 3, 3)])

    def testOneFailedPartIsEnoughAndTheOthersAreStillFollowed(self):
        self.send(parts=3, references=(10, 11, 12))
        self.report(reference=10)
        self.report(reference=11, status=0x41)
        self.assertEqual(self.events, [SmsDelivery('a', '42', NUMBER, 'undelivered', 'incompatible destination', 3, 1)])
        self.assertEqual(len(self.tracker), 1)
        self.report(reference=12)  # the last part is still taken for a part of this SMS, nothing more is published
        self.assertEqual(self.states(), ['undelivered'])
        self.assertEqual(len(self.tracker), 0)

    def testOneDelayedPartMakesThePendingState(self):
        self.send(parts=2, references=(10, 11))
        self.report(reference=10)
        self.report(reference=11, status=0x22)
        self.assertEqual(self.states(), ['pending'])
        self.report(reference=11)
        self.assertEqual(self.states(), ['pending', 'delivered'])

    def testTheFailureOfOneSmsDoesNotTouchAnother(self):
        self.send('a', references=(10,))
        self.send('b', references=(11,), number='0600000000', ref='43')
        self.report(reference=11, number='+33600000000', status=0x43)
        self.assertEqual([(event.smsId, event.status) for event in self.events], [('b', 'undelivered')])


class AbandonedTest(TrackerTestCase):
    def testTheReportsOfAnSmsThatFailedAfterSomePartsLeftChangeNothing(self):
        self.send(parts=3, references=(10, 11), close=False)
        self.tracker.abandon('a')  # the third part was refused: Jeedom was told "failed"
        self.report(reference=10)
        self.report(reference=11)
        self.tracker.close('a')
        self.assertEqual(self.events, [])


class MatchingTest(TrackerTestCase):
    def testTheNumberIsComparedByItsLastDigits(self):
        self.send(number='+33 6 62 03 26 92')
        self.report(number='0662032692')
        self.assertEqual(self.states(), ['delivered'])

    def testAnotherRecipientIsNotTheSameMessage(self):
        self.send()
        self.report(number='+33600000000')
        self.assertEqual(self.events, [])

    def testALateReportOfAnOlderMessageDoesNotMatchANewOne(self):
        self.send('old', references=(10,))
        self.wall.now += 3 * 3600
        self.send('new', references=(10,), ref='43')  # same TP-MR, the counter restarted after a restart
        self.report(reference=10, when=self.wall.now - 3 * 3600)  # the report of the old one, its own time
        self.assertEqual([(event.smsId, event.status) for event in self.events], [('old', 'delivered')])

    def testTheSendingTimeMayBeTheSecondTimeStampOfTheReport(self):
        # Free puts the time of the delivery first and the time the SMS center got the message second, the standard
        # the other way round: a delivery hours later must still be matched
        self.send()
        self.tracker.onReport(10, REPORT_NUMBER, self.wall.now + 5 * 3600, 0, self.wall.now + 1)
        self.assertEqual(self.states(), ['delivered'])

    def testTheSendingTimeMayBeTheFirstTimeStampOfTheReport(self):
        self.send()
        self.tracker.onReport(10, REPORT_NUMBER, self.wall.now + 1, 0, self.wall.now + 5 * 3600)
        self.assertEqual(self.states(), ['delivered'])

    def testAReportWhoseTwoTimeStampsAreFarFromTheSendingIsNotMatched(self):
        self.send()
        with self.assertLogs('wwanlib.receipts', level='WARNING'):
            self.tracker.onReport(10, REPORT_NUMBER, self.wall.now + 7200, 0, self.wall.now + 9000)
        self.assertEqual(self.events, [])

    def testAnOrphanReportKeepsBothTimeStampsUntilThePartIsRegistered(self):
        self.tracker.onReport(10, REPORT_NUMBER, self.wall.now + 5 * 3600, 0, self.wall.now)
        self.send()
        self.assertEqual(self.states(), ['delivered'])
    def testAReportWithTheRightTpMrButAnotherTimeSaysThatTheClockMayBeWrong(self):
        self.send()
        with self.assertLogs('wwanlib.receipts', level='WARNING') as logs:
            self.report(when=self.wall.now - 7200)
        self.assertIn('7200 s apart', logs.output[0])
        self.assertIn('clock', logs.output[0])
        self.assertEqual(self.events, [])

    def testTheTimeOfTheSmsCenterIsTolerated(self):
        self.send()
        self.report(when=self.wall.now + 120)
        self.assertEqual(self.states(), ['delivered'])

    def testAReportWithoutATimeIsMatchedWithoutTheTimeCheck(self):
        self.send()
        self.tracker.onReport(10, REPORT_NUMBER, None, 0)
        self.assertEqual(self.states(), ['delivered'])

    def testTheCounterWentRoundBetweenTwoMessagesToTheSameNumber(self):
        self.send('first', references=(10,))
        self.wall.now += 1000
        self.send('second', references=(10,), ref='43')
        self.report(reference=10, when=self.wall.now)  # the report of the second
        self.assertEqual([(event.smsId, event.status) for event in self.events], [('second', 'delivered')])

    def testAPartWithoutATpMrCannotBeFollowed(self):
        self.tracker.register('a', '42', NUMBER, 1, 1, None)
        self.assertEqual(len(self.tracker), 0)


class LifetimeTest(TrackerTestCase):
    def testAReportNobodyClaimedIsDroppedAfterAWhile(self):
        self.report()
        self.clock.now += 59
        self.tracker.expire()
        self.send()  # still matched: the report waited
        self.assertEqual(self.states(), ['delivered'])

    def testAnOrphanReportIsDroppedAndSaid(self):
        self.report(number='+33611111111')
        self.clock.now += 61
        with self.assertLogs('wwanlib.receipts', level='INFO') as logs:
            self.tracker.expire()
        self.assertIn('ignored: no SMS of this daemon matches', logs.output[0])
        self.send(number='0611111111')
        self.assertEqual(self.events, [])  # too late

    def testAnSmsWithoutFinalReportBecomesUnknown(self):
        self.send(parts=2, references=(10, 11))
        self.report(reference=10)
        self.clock.now += DEFAULT_MAX_AGE - 1
        self.tracker.expire()
        self.assertEqual(self.events, [])
        self.clock.now += 2
        with self.assertLogs('wwanlib.receipts', level='WARNING'):
            self.tracker.expire()
        self.assertEqual(self.events, [SmsDelivery('a', '42', NUMBER, 'unknown', 'no report', 2, 1)])
        self.assertEqual(len(self.tracker), 0)

    def testAnSmsAlreadyFinalIsNotReportedUnknown(self):
        self.send()
        self.report(status=0x43)
        self.clock.now += DEFAULT_MAX_AGE + 1
        self.tracker.expire()
        self.assertEqual(self.states(), ['undelivered'])

    def testTheNumberOfFollowedSmsIsLimited(self):
        for index in range(MAX_TRACKED + 5):
            self.clock.now += 1
            self.tracker.register(f'sms{index}', None, NUMBER, 1, 1, index % 256)
        self.assertEqual(len(self.tracker), MAX_TRACKED)


if __name__ == '__main__':
    unittest.main()
