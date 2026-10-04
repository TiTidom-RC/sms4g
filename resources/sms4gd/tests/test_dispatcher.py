""" Tests of the Dispatcher (no modem and no Jeedom needed). Run from resources/sms4gd:
python -m unittest discover -s tests -t . """

import json
import queue
import time
import unittest
from concurrent.futures import Future

from dispatcher import MAX_RESPONSE_CHARS, Dispatcher, selfTestMessage, smsInboxMessage, smsStatusMessage
from wwanlib import (CmeError, NotConnectedError, SelfTestResult, SignalChanged, SmsDelivery, SmsExpired, SmsFailed, SmsIncomplete, SmsQueued, SmsQueueFullError,
                    SmsReceived, SmsSent, TimeoutException)


class FakeModem:
    def __init__(self):
        self.calls: list[tuple] = []
        self.future: Future = Future()
        self.sms: list[tuple] = []
        self.smsError: Exception | None = None
        self.restarts: list[str] = []
        self.restartError: Exception | None = None
        self.restartFuture: Future = Future()
        self.selfTests = 0

    def restart(self, reason='requested'):
        if self.restartError is not None:
            raise self.restartError
        self.restarts.append(reason)
        return self.restartFuture

    def selfTest(self):
        self.selfTests += 1
        return Future()

    def command(self, command, timeout=10.0, parseError=True):
        self.calls.append((command, timeout, parseError))
        return self.future

    def sendSms(self, number, text, ref=None, maxPartsPerGroup=0):
        if self.smsError is not None:
            raise self.smsError
        self.sms.append((number, text, ref, maxPartsPerGroup))
        return 'sms1'


class FakeOut:
    def __init__(self):
        self.events: list[dict] = []

    def event(self, payload):
        self.events.append(payload)


def message(**fields) -> bytes:
    return json.dumps({'apikey': 'KEY', **fields}).encode()


class DispatcherTest(unittest.TestCase):
    def setUp(self):
        self.modem = FakeModem()
        self.out = FakeOut()
        self.messages: queue.Queue = queue.Queue()
        self.dispatcher = Dispatcher(self.messages, self.modem, self.out, 'KEY', diagnostic=True)

    def dispatch(self, **fields):
        self.dispatcher.handle(message(**fields))

    # ---- authentication and format ----------------------------------------------------------------

    def testInvalidApikeyIsIgnored(self):
        with self.assertLogs('dispatcher', level='ERROR') as logs:
            self.dispatcher.handle(json.dumps({'apikey': 'WRONG', 'cmd': 'atCommand', 'id': '1', 'command': 'AT+CSQ'}).encode())
        self.assertIn('Invalid apikey', logs.output[0])
        self.assertNotIn('WRONG', ' '.join(logs.output))
        self.assertEqual(self.modem.calls, [])
        self.assertEqual(self.out.events, [])

    def testMissingApikeyIsIgnored(self):
        with self.assertLogs('dispatcher', level='ERROR'):
            self.dispatcher.handle(json.dumps({'cmd': 'atCommand', 'id': '1', 'command': 'AT+CSQ'}).encode())
        self.assertEqual(self.modem.calls, [])

    def testMalformedMessagesAreIgnored(self):
        for raw in (b'not json', b'\xff\xfe', b'[1, 2]', b'"text"', b'null'):
            with self.assertLogs('dispatcher', level='ERROR'):
                self.dispatcher.handle(raw)
        self.assertEqual(self.modem.calls, [])

    def testUnknownCommandIsIgnored(self):
        for cmd in ('doSomething', None, 42):
            with self.assertLogs('dispatcher', level='WARNING') as logs:
                self.dispatch(cmd=cmd)
            self.assertIn('Unknown command', logs.output[0])
        self.assertEqual(self.out.events, [])

    # ---- SMS --------------------------------------------------------------------------------------

    def testSendSmsIsQueuedInTheLibrary(self):
        self.dispatch(cmd='sendSms', number='+33600000000', message='hello', ref='42', maxPartsPerGroup=3)
        self.assertEqual(self.modem.sms, [('+33600000000', 'hello', '42', 3)])
        self.assertEqual(self.out.events, [])  # the result comes later, from the events of the library

    def testSendSmsWithoutReferenceNorLimit(self):
        self.dispatch(cmd='sendSms', number='+33600000000', message='hello')
        self.assertEqual(self.modem.sms, [('+33600000000', 'hello', None, 0)])

    def testSendSmsNeverLogsTheNumberNorTheText(self):
        with self.assertLogs('dispatcher', level='INFO') as logs:
            self.dispatch(cmd='sendSms', number='+33600000012', message='secret text', ref='1')
        text = ' '.join(logs.output)
        self.assertIn('accepted', text)
        self.assertIn('+336XXXXXX12', text)
        self.assertNotIn('+33600000012', text)
        self.assertNotIn('secret', text)

    def testSendSmsReferenceMustBeAText(self):
        with self.assertLogs('dispatcher', level='ERROR'):
            self.dispatch(cmd='sendSms', number='+33600000000', message='hello', ref=42)
        self.assertEqual(self.modem.sms, [])
        self.assertEqual(self.out.events, [])

    def testSendSmsWithoutTextNumberOrMessageIsReported(self):
        with self.assertLogs('dispatcher', level='WARNING'):
            self.dispatch(cmd='sendSms', number=None, message='hello', ref='1')
            self.dispatch(cmd='sendSms', number='+33600000000', message=None, ref='2')
        self.assertEqual(self.modem.sms, [])
        self.assertEqual(self.out.events, [
            {'type': 'smsStatus', 'ref': '1', 'number': '', 'status': 'failed', 'reason': 'invalid number', 'parts': 0, 'sentParts': 0},
            {'type': 'smsStatus', 'ref': '2', 'number': '+33600000000', 'status': 'failed', 'reason': 'empty message', 'parts': 0, 'sentParts': 0}])

    def testSendSmsLimitOfPartsIsNeverNegativeNorGarbage(self):
        for value in ('x', -3, None, []):
            self.dispatch(cmd='sendSms', number='+33600000000', message='hello', maxPartsPerGroup=value)
        self.assertEqual([call[3] for call in self.modem.sms], [0, 0, 0, 0])

    def testSendSmsRefusedWhenTheQueueIsFull(self):
        self.modem.smsError = SmsQueueFullError('full')
        with self.assertLogs('dispatcher', level='WARNING'):
            self.dispatch(cmd='sendSms', number='+33600000000', message='hello', ref='7')
        self.assertEqual(self.out.events, [{'type': 'smsStatus', 'ref': '7', 'number': '+33600000000', 'status': 'failed',
                                            'reason': 'queue full', 'parts': 0, 'sentParts': 0}])

    def testSendSmsRefusedWhenTheModemIsDisconnected(self):
        self.modem.smsError = NotConnectedError('Modem disconnected')
        with self.assertLogs('dispatcher', level='WARNING'):
            self.dispatch(cmd='sendSms', number='+33600000000', message='hello', ref='7')
        self.assertEqual(self.out.events[0]['reason'], 'modem disconnected')

    def testSmsStatusMessages(self):
        self.assertEqual(smsStatusMessage(SmsQueued('id1', '42', '+33600000000', 'modem not connected')), {
            'type': 'smsStatus', 'smsId': 'id1', 'ref': '42', 'number': '+33600000000', 'status': 'queued', 'reason': 'modem not connected'})
        self.assertEqual(smsStatusMessage(SmsSent('id1', '42', '+33600000000', 3, (5, 6, None))), {
            'type': 'smsStatus', 'smsId': 'id1', 'ref': '42', 'number': '+33600000000', 'status': 'sent', 'parts': 3,
            'references': [5, 6, None]})
        self.assertEqual(smsStatusMessage(SmsFailed('id1', None, '+33600000000', '+CMS ERROR: 330', 2, 1)), {
            'type': 'smsStatus', 'smsId': 'id1', 'ref': None, 'number': '+33600000000', 'status': 'failed',
            'reason': '+CMS ERROR: 330', 'parts': 2, 'sentParts': 1})
        self.assertEqual(smsStatusMessage(SmsExpired('id1', '42', '+33600000000', '')), {
            'type': 'smsStatus', 'smsId': 'id1', 'ref': '42', 'number': '+33600000000', 'status': 'expired', 'reason': ''})
        self.assertEqual(smsStatusMessage(SmsDelivery('id1', '42:x', '+33600000000', 'delivered', '', 3, 3)), {
            'type': 'smsStatus', 'smsId': 'id1', 'ref': '42:x', 'number': '+33600000000', 'status': 'delivered', 'reason': '',
            'parts': 3, 'deliveredParts': 3})
        self.assertEqual(smsStatusMessage(SmsDelivery('id1', None, '+33600000000', 'undelivered', 'not obtainable', 2, 1))['status'],
                         'undelivered')
        self.assertIsNone(smsStatusMessage(SignalChanged(20)))
        self.assertIsNone(smsStatusMessage('anything'))
        self.assertIsNone(smsStatusMessage(SmsReceived('+33600000000', 'Hello', None, 1)))

    def testSmsInboxMessages(self):
        self.assertEqual(smsInboxMessage(SmsReceived('+33600000000', 'Allume le salon', 1791109480.0, 1)), {
            'type': 'smsReceived', 'number': '+33600000000', 'message': 'Allume le salon', 'parts': 1, 'sent': 1791109480.0})
        self.assertEqual(smsInboxMessage(SmsReceived('Free', 'x' * 400, None, 3)), {
            'type': 'smsReceived', 'number': 'Free', 'message': 'x' * 400, 'parts': 3, 'sent': None})
        self.assertEqual(smsInboxMessage(SmsIncomplete('+33600000000', 2, 3, 'timeout')), {
            'type': 'smsIncomplete', 'number': '+33600000000', 'received': 2, 'expected': 3, 'reason': 'timeout'})
        self.assertIsNone(smsInboxMessage(SignalChanged(20)))
        self.assertIsNone(smsInboxMessage(SmsSent('id1', None, '+33600000000', 1, (5,))))
    # ---- AT command: refusals ---------------------------------------------------------------------

    def testAtCommandWithoutIdIsIgnored(self):
        with self.assertLogs('dispatcher', level='ERROR'):
            self.dispatch(cmd='atCommand', command='AT+CSQ')
        self.assertEqual(self.modem.calls, [])
        self.assertEqual(self.out.events, [])

    def testAtCommandRefusedWhenDiagnosticModeIsOff(self):
        dispatcher = Dispatcher(self.messages, self.modem, self.out, 'KEY', diagnostic=False)
        with self.assertLogs('dispatcher', level='WARNING'):
            dispatcher.handle(message(cmd='atCommand', id='a1', command='AT+CSQ'))
        self.assertEqual(self.modem.calls, [])
        self.assertEqual(self.out.events, [{'type': 'atResponse', 'requestId': 'a1', 'command': 'AT+CSQ', 'status': 'refused',
                                            'lines': [], 'error': 'diagnostic mode is disabled'}])

    def testAtCommandRefusedByTheFilter(self):
        with self.assertLogs('dispatcher', level='WARNING'):
            self.dispatch(cmd='atCommand', id='a2', command='at+cmgs=5')
        self.assertEqual(self.modem.calls, [])
        event = self.out.events[0]
        self.assertEqual((event['requestId'], event['status'], event['command']), ('a2', 'refused', 'AT+CMGS=5'))
        self.assertIn('sending', event['error'])

    def testSwitchingTheSimOffIsRefusedWhenThereIsAPinAndAllowedOtherwise(self):
        with self.assertLogs('dispatcher', level='WARNING'):
            self.dispatch(cmd='atCommand', id='p1', command='AT+CFUN=0')  # a PIN is assumed by default
        self.assertEqual(self.modem.calls, [])
        self.assertEqual(self.out.events[0]['status'], 'refused')
        self.assertIn('PIN', self.out.events[0]['error'])
        noPin = Dispatcher(self.messages, self.modem, self.out, 'KEY', diagnostic=True, pinConfigured=False)
        noPin.handle(message(cmd='atCommand', id='p2', command='AT+CFUN=0'))
        self.assertEqual([call[0] for call in self.modem.calls], ['AT+CFUN=0'])

    def testChainedCommandIsRefused(self):
        with self.assertLogs('dispatcher', level='WARNING'):
            self.dispatch(cmd='atCommand', id='a3', command='AT+CSQ;ATZ')
        self.assertEqual(self.modem.calls, [])
        self.assertEqual(self.out.events[0]['status'], 'refused')

    def testInvalidCommandTypeIsRefused(self):
        with self.assertLogs('dispatcher', level='WARNING'):
            self.dispatch(cmd='atCommand', id='a4', command=['AT'])
        self.assertEqual(self.modem.calls, [])
        self.assertEqual(self.out.events[0]['status'], 'refused')

    # ---- AT command: execution --------------------------------------------------------------------

    def testAtCommandSendsTheNormalizedCommandAndAnswersWhenDone(self):
        self.dispatch(cmd='atCommand', id='b1', command=' at+csq ', timeout=20)
        self.assertEqual(self.modem.calls, [('AT+CSQ', 20.0, False)])  # raw answer wanted: parseError=False
        self.assertEqual(self.out.events, [])  # nothing before the modem answered: the handler does not wait
        self.modem.future.set_result(['+CSQ: 25,99', 'OK'])
        self.assertEqual(self.out.events, [{'type': 'atResponse', 'requestId': 'b1', 'command': 'AT+CSQ', 'lines': ['+CSQ: 25,99', 'OK'], 'status': 'ok'}])

    def testModemErrorIsReportedWithItsLines(self):
        self.dispatch(cmd='atCommand', id='b2', command='AT+CPMS?')
        self.modem.future.set_result(['+CMS ERROR: 322'])
        event = self.out.events[0]
        self.assertEqual((event['status'], event['error'], event['lines']), ('error', '+CMS ERROR: 322', ['+CMS ERROR: 322']))

    def testErrorWithoutAnyLine(self):
        self.dispatch(cmd='atCommand', id='b3', command='AT')
        self.modem.future.set_result([])
        self.assertEqual((self.out.events[0]['status'], self.out.events[0]['error']), ('error', 'no answer'))

    def testTimeoutKeepsThePartialAnswer(self):
        self.dispatch(cmd='atCommand', id='b4', command='AT+COPS=?', timeout=5)
        self.modem.future.set_exception(TimeoutException(['+COPS: (2,"F"']))
        event = self.out.events[0]
        self.assertEqual((event['status'], event['error'], event['lines']), ('timeout', 'no response within 5 s', ['+COPS: (2,"F"']))

    def testTimeoutWithoutData(self):
        self.dispatch(cmd='atCommand', id='b5', command='AT+COPS=?')
        self.modem.future.set_exception(TimeoutException())
        self.assertEqual((self.out.events[0]['status'], self.out.events[0]['lines']), ('timeout', []))

    def testNotConnected(self):
        self.dispatch(cmd='atCommand', id='b6', command='AT+CSQ')
        self.modem.future.set_exception(NotConnectedError('Modem not connected (state: reconnecting)'))
        event = self.out.events[0]
        self.assertEqual((event['status'], event['error']), ('notConnected', 'Modem not connected (state: reconnecting)'))

    def testOtherErrors(self):
        self.dispatch(cmd='atCommand', id='b7', command='AT+CSQ')
        self.modem.future.set_exception(CmeError('AT+CSQ', 10))
        self.assertEqual(self.out.events[0]['status'], 'error')
        self.assertIn('CmeError', self.out.events[0]['error'])

    def testAlreadyFailedFutureAnswersAtOnce(self):
        self.modem.future.set_exception(NotConnectedError('Modem not connected (state: connecting)'))
        self.dispatch(cmd='atCommand', id='b8', command='AT+CSQ')  # the library returns a failed Future when not connected
        self.assertEqual(self.out.events[0]['status'], 'notConnected')

    def testTimeoutIsBounded(self):
        for given, used in ((None, 15.0), ('abc', 15.0), (0, 1.0), (-5, 1.0), (999, 180.0), ('30', 30.0), (2.5, 2.5)):
            self.modem.calls.clear()
            self.dispatch(cmd='atCommand', id='c', command='AT+CSQ', timeout=given)
            self.assertEqual(self.modem.calls[0][1], used, repr(given))

    def testLongAnswerIsTruncated(self):
        self.dispatch(cmd='atCommand', id='d1', command='AT+CPSI?')
        lines = ['x' * 100] * 100 + ['OK']
        self.modem.future.set_result(lines)
        event = self.out.events[0]
        self.assertTrue(event['truncated'])
        self.assertLessEqual(sum(len(line) + 1 for line in event['lines']), MAX_RESPONSE_CHARS)
        self.assertEqual(event['status'], 'ok')  # the status is the one of the whole answer

    def testAnswerOfAShortResponseIsNotMarkedTruncated(self):
        self.dispatch(cmd='atCommand', id='d2', command='AT+CSQ')
        self.modem.future.set_result(['+CSQ: 1,1', 'OK'])
        self.assertNotIn('truncated', self.out.events[0])

    # ---- thread -----------------------------------------------------------------------------------

    # ---- restart, self-test, number of the SIM ----------------------------------------------------

    def testRestartIsAskedAndAnsweredWhenTheModemTookIt(self):
        with self.assertLogs('dispatcher', level='WARNING'):
            self.dispatch(cmd='restartModem')
        self.assertEqual(self.modem.restarts, ['requested'])
        self.assertEqual(self.out.events, [])  # nothing before the modem answered
        self.modem.restartFuture.set_result(None)
        self.assertEqual(self.out.events, [{'type': 'restartResult', 'status': 'ok', 'reason': ''}])

    def testRestartRefusedBecauseTheModemIsNotConnected(self):
        self.modem.restartError = NotConnectedError('Modem not connected (state: reconnecting)')
        with self.assertLogs('dispatcher', level='WARNING'):
            self.dispatch(cmd='restartModem')
        self.assertEqual(self.out.events, [{'type': 'restartResult', 'status': 'refused',
                                            'reason': 'Modem not connected (state: reconnecting)'}])

    def testRestartRefusedByTheModem(self):
        with self.assertLogs('dispatcher', level='WARNING'):
            self.dispatch(cmd='restartModem')
            self.modem.restartFuture.set_exception(CmeError('AT+CRESET', 100))
        self.assertEqual((self.out.events[0]['status'], self.out.events[0]['reason'].split(':')[0]), ('refused', 'CmeError'))

    def testRestartAndSelfTestDoNotNeedTheDiagnosticMode(self):
        self.dispatcher = Dispatcher(self.messages, self.modem, self.out, 'KEY', diagnostic=False)
        with self.assertLogs('dispatcher', level='INFO'):
            self.dispatch(cmd='restartModem')
            self.dispatch(cmd='selfTest')
        self.assertEqual((self.modem.restarts, self.modem.selfTests), (['requested'], 1))

    def testSelfTestIsRunByTheModemWhoseEventTellsTheResult(self):
        with self.assertLogs('dispatcher', level='INFO'):
            self.dispatch(cmd='selfTest')
        self.assertEqual((self.modem.selfTests, self.out.events), (1, []))

    def testSelfTestMessage(self):
        self.assertEqual(selfTestMessage(SelfTestResult('noReceipt', 'no report', 61.5, True)),
                         {'type': 'selfTest', 'status': 'noReceipt', 'reason': 'no report', 'duration': 61.5, 'restarted': True})
        self.assertIsNone(selfTestMessage(SignalChanged(20)))

    def ownNumber(self, lines=None, error=None):
        with self.assertLogs('dispatcher', level='INFO') as logs:
            self.dispatch(cmd='readOwnNumber')
            if error is not None:
                self.modem.future.set_exception(error)
            else:
                self.modem.future.set_result(lines)
        return self.out.events[0], '\n'.join(logs.output)

    def testTheNumberOfTheSimIsRead(self):
        event, logs = self.ownNumber(['+CNUM: "Messagerie","+33767923801",145', 'OK'])
        self.assertEqual(self.modem.calls, [('AT+CNUM', 15.0, False)])
        self.assertEqual(event, {'type': 'ownNumber', 'number': '+33767923801', 'reason': ''})
        self.assertNotIn('33767923801', logs)  # masked in the log

    def testTheFirstNumberIsTaken(self):
        event, _ = self.ownNumber(['+CNUM: "","0612345678",129', '+CNUM: "Fax","+33611111111",145', 'OK'])
        self.assertEqual(event['number'], '0612345678')

    def testASimThatDoesNotKnowItsNumber(self):
        for lines in (['OK'], ['+CNUM: "","",129', 'OK'], ['+CME ERROR: 100']):
            self.out.events.clear()
            self.modem.future = Future()
            event, _ = self.ownNumber(lines)
            self.assertEqual((event['number'], event['reason']), (None, 'the SIM does not know its number'))

    def testANumberThatCannotBeReadIsExplained(self):
        event, _ = self.ownNumber(error=NotConnectedError('Modem not connected (state: restarting)'))
        self.assertEqual(event['number'], None)
        self.assertIn('NotConnectedError', event['reason'])

    def testThreadHandlesMessagesAndStopsAtOnce(self):
        self.dispatcher.start()
        self.messages.put(message(cmd='atCommand', id='e1', command='AT+CSQ'))
        self.messages.put(b'not json')  # an error must not stop the thread
        self.messages.put(message(cmd='atCommand', id='e2', command='AT+CGMR'))
        deadline = time.monotonic() + 3
        while len(self.modem.calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual([call[0] for call in self.modem.calls], ['AT+CSQ', 'AT+CGMR'])
        started = time.monotonic()
        self.dispatcher.stop()
        self.assertLess(time.monotonic() - started, 0.5)

    def testAnErrorInAHandlerDoesNotKillTheThread(self):
        class Flaky(FakeModem):
            def command(self, command, timeout=10.0, parseError=True):
                super().command(command, timeout, parseError)
                if len(self.calls) == 1:
                    raise RuntimeError('boom')
                return self.future

        modem = Flaky()
        self.dispatcher = Dispatcher(self.messages, modem, self.out, 'KEY', diagnostic=True)
        self.dispatcher.start()
        with self.assertLogs('dispatcher', level='ERROR'):
            self.messages.put(message(cmd='atCommand', id='f1', command='AT+CSQ'))
            self.messages.put(message(cmd='atCommand', id='f2', command='AT+CGMR'))
            deadline = time.monotonic() + 3
            while len(modem.calls) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual([call[0] for call in modem.calls], ['AT+CSQ', 'AT+CGMR'])  # the second one was handled
        self.dispatcher.stop()

if __name__ == '__main__':
    unittest.main()
