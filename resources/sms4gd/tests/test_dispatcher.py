""" Tests of the Dispatcher (no modem and no Jeedom needed). Run from resources/sms4gd:
python -m unittest discover -s tests -t . """

import json
import queue
import time
import unittest
from concurrent.futures import Future

from dispatcher import MAX_RESPONSE_CHARS, Dispatcher
from wwanlib import CmeError, NotConnectedError, TimeoutException


class FakeModem:
    def __init__(self):
        self.calls: list[tuple] = []
        self.future: Future = Future()

    def command(self, command, timeout=10.0, parseError=True):
        self.calls.append((command, timeout, parseError))
        return self.future


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

    def testSendSmsIsNotAvailableYet(self):
        with self.assertLogs('dispatcher', level='WARNING') as logs:
            self.dispatch(cmd='sendSms', number='+33600000000', message='hello')
        self.assertIn('not available yet', logs.output[0])
        self.assertEqual(self.modem.calls, [])

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
