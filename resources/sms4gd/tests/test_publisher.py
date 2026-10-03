""" Tests of jeedom_publisher, against a small local HTTP server (no Jeedom needed). Run from resources/sms4gd:
python -m unittest discover -s tests -t . """

import json
import re
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlparse

from jeedom.jeedom import jeedom_publisher


class Receiver:
    """ Local server playing the part of jeesms4g.php. ``statuses`` are the answers to the next POST requests
    (200 once the list is empty); ``gate`` holds the answer of every request until it is set. """

    def __init__(self):
        self.posts: list[list[dict]] = []  # the messages of every request
        self.apikeys: list[str] = []
        self.statuses: list[int] = []
        self.gate: threading.Event | None = None
        self.arrived = threading.Event()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                receiver.apikeys.append(parse_qs(urlparse(self.path).query).get('apikey', [''])[0])
                receiver.posts.append(json.loads(body)['messages'])
                receiver.arrived.set()
                if receiver.gate is not None:
                    receiver.gate.wait(5)
                self.send_response(receiver.statuses.pop(0) if receiver.statuses else 200)
                self.end_headers()

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}/jeesms4g.php'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        if self.gate is not None:
            self.gate.set()
        self.server.shutdown()
        self.server.server_close()

    @property
    def received(self) -> list[dict]:
        return [message for post in self.posts for message in post]

    def types(self) -> list:
        return [message.get('type') for message in self.received]


def waitFor(predicate, timeout=5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class JeedomPublisherTest(unittest.TestCase):
    def setUp(self):
        self.receiver = Receiver()
        self.addCleanup(self.receiver.close)
        self.out = jeedom_publisher(self.receiver.url, 'KEY', retries=3, retry_delay=0.02)
        self.out.start()
        self.addCleanup(self.out.stop)

    def hold(self):
        """ Jeedom does not answer: a first message is in flight, the next ones accumulate """
        self.receiver.gate = threading.Event()
        self.out.event({'type': 'warmup'})
        self.assertTrue(self.receiver.arrived.wait(3))

    # ---- messages ---------------------------------------------------------------------------------

    def testEventsAreDeliveredInOrderWithTheApikey(self):
        for number in range(5):
            self.out.event({'type': 'atResponse', 'number': number})
        self.assertTrue(self.out.flush(5))
        self.assertEqual([message['number'] for message in self.receiver.received], [0, 1, 2, 3, 4])
        self.assertEqual(set(self.receiver.apikeys), {'KEY'})

    def testEveryMessageHasAnIdAndATime(self):
        before = time.time()
        for number in range(20):
            self.out.event({'type': 'x'})
        self.assertTrue(self.out.flush(5))
        messages = self.receiver.received
        self.assertEqual(len({message['id'] for message in messages}), 20)
        for message in messages:
            self.assertRegex(message['id'], r'^[0-9a-f]{32}$')
            self.assertTrue(before - 1 <= message['time'] <= time.time() + 1)

    def testIdIsReservedToThePublisher(self):
        self.out.event({'type': 'x', 'id': 'mine'})
        self.assertTrue(self.out.flush(5))
        self.assertNotEqual(self.receiver.received[0]['id'], 'mine')  # a request id travels in its own field (requestId)

    def testTimeGivenByTheCallerIsKept(self):
        self.out.event({'type': 'x', 'time': 1234.5})
        self.assertTrue(self.out.flush(5))
        self.assertEqual(self.receiver.received[0]['time'], 1234.5)

    def testThePayloadOfTheCallerIsNotModified(self):
        payload = {'type': 'signal', 'value': 20}
        self.out.state('signal', payload)
        self.assertTrue(self.out.flush(5))
        self.assertEqual(payload, {'type': 'signal', 'value': 20})

    # ---- batches ----------------------------------------------------------------------------------

    def testAMessageIsSentAtOnceWhenTheChannelIsFree(self):
        started = time.monotonic()
        self.out.event({'type': 'alone'})
        self.assertTrue(waitFor(lambda: self.receiver.posts))
        self.assertLess(time.monotonic() - started, 0.5)  # no delay added to wait for other messages
        self.assertEqual(self.receiver.types(), ['alone'])

    def testWhatAccumulatesDuringARequestLeavesTogether(self):
        self.hold()
        for number in range(5):
            self.out.event({'type': 'event', 'number': number})
        self.receiver.gate.set()
        self.assertTrue(self.out.flush(5))
        self.assertEqual([len(post) for post in self.receiver.posts], [1, 5])  # 2 requests instead of 6
        self.assertEqual([m.get('number') for m in self.receiver.posts[1]], [0, 1, 2, 3, 4])  # in order

    def testBatchSizeIsLimited(self):
        self.hold()
        with mock.patch.object(jeedom_publisher, 'MAX_BATCH', 3):
            for number in range(7):
                self.out.event({'type': 'event', 'number': number})
            self.receiver.gate.set()
            self.assertTrue(self.out.flush(5))
        self.assertEqual([len(post) for post in self.receiver.posts], [1, 3, 3, 1])
        self.assertEqual([m.get('number') for m in self.receiver.received if m['type'] == 'event'], list(range(7)))

    # ---- states -----------------------------------------------------------------------------------

    def testStateIsReplacedWhileJeedomIsSlow(self):
        self.hold()
        self.out.state('signal', {'type': 'signal', 'value': 10})
        self.out.event({'type': 'event1'})
        self.out.state('signal', {'type': 'signal', 'value': 20})
        self.out.state('network', {'type': 'network', 'registration': 'registered'})
        self.out.state('signal', {'type': 'signal', 'value': 30})
        self.out.event({'type': 'event2'})
        self.receiver.gate.set()
        self.assertTrue(self.out.flush(5))
        # Only the last signal is sent, after what happened before it: the order of the sending is the order of the facts
        self.assertEqual(self.receiver.types(), ['warmup', 'event1', 'network', 'signal', 'event2'])
        self.assertEqual([m['value'] for m in self.receiver.received if m['type'] == 'signal'], [30])

    def testStatesWithDifferentKeysAreAllSent(self):
        self.hold()
        self.out.state(('smsStatus', 1), {'type': 'smsStatus', 'number': 1})
        self.out.state(('smsStatus', 2), {'type': 'smsStatus', 'number': 2})
        self.receiver.gate.set()
        self.assertTrue(self.out.flush(5))
        self.assertEqual([m.get('number') for m in self.receiver.received], [None, 1, 2])

    # ---- failures ---------------------------------------------------------------------------------

    def testRetryKeepsTheSameIds(self):
        self.receiver.statuses = [500, 200]
        self.out.event({'type': 'atResponse'})
        self.assertTrue(self.out.flush(5))
        self.assertEqual(len(self.receiver.posts), 2)
        self.assertEqual(self.receiver.posts[0], self.receiver.posts[1])  # same id: the PHP side can ignore the second

    def testRetryResendsTheWholeBatch(self):
        self.hold()
        self.out.event({'type': 'a'})
        self.out.event({'type': 'b'})
        self.receiver.statuses = [200, 500, 200]  # the warmup is answered, then the batch fails once
        self.receiver.gate.set()
        self.assertTrue(self.out.flush(5))
        self.assertEqual([[m['type'] for m in post] for post in self.receiver.posts], [['warmup'], ['a', 'b'], ['a', 'b']])

    def testBatchIsDroppedAfterTheLastAttemptAndTheChannelKeepsWorking(self):
        self.receiver.statuses = [500, 500, 500]
        with self.assertLogs(level='ERROR') as logs:
            self.out.event({'type': 'lost'})
            self.assertTrue(self.out.flush(5))
        self.assertRegex(logs.output[0], r'dropped: 1 event\(s\), 0 state\(s\)')
        self.out.event({'type': 'next'})
        self.assertTrue(self.out.flush(5))
        self.assertEqual(self.receiver.types(), ['lost', 'lost', 'lost', 'next'])

    def testStatesOnlyAreDroppedWithAWarning(self):
        self.receiver.statuses = [500, 500, 500]
        with self.assertLogs(level='WARNING') as logs:
            self.out.state('signal', {'type': 'signal', 'value': 1})
            self.assertTrue(self.out.flush(5))
        self.assertRegex(logs.output[0], r'WARNING.*dropped: 1 state\(s\)')

    def testOldestEventIsDroppedWhenTooManyAreWaiting(self):
        self.hold()
        with mock.patch.object(jeedom_publisher, 'MAX_EVENTS', 3):
            with self.assertLogs(level='ERROR') as logs:
                for number in range(6):
                    self.out.event({'type': 'event', 'number': number})
        self.assertEqual(len(logs.output), 3)
        self.receiver.gate.set()
        self.assertTrue(self.out.flush(5))
        self.assertEqual([m.get('number') for m in self.receiver.received], [None, 3, 4, 5])  # the three oldest were dropped

    def testStatesDoNotCountAsEvents(self):
        self.hold()
        with mock.patch.object(jeedom_publisher, 'MAX_EVENTS', 2):
            self.out.state('a', {'type': 'a'})
            self.out.state('b', {'type': 'b'})
            self.out.event({'type': 'event1'})
            self.out.event({'type': 'event2'})
        self.receiver.gate.set()
        self.assertTrue(self.out.flush(5))
        self.assertEqual(sorted(self.receiver.types()), ['a', 'b', 'event1', 'event2', 'warmup'])

    # ---- flush, stop, test ------------------------------------------------------------------------

    def testFlushTimesOutWhileJeedomDoesNotAnswer(self):
        self.receiver.gate = threading.Event()
        self.out.event({'type': 'slow'})
        started = time.monotonic()
        self.assertFalse(self.out.flush(0.2))
        self.assertLess(time.monotonic() - started, 1.0)
        self.receiver.gate.set()
        self.assertTrue(self.out.flush(5))

    def testFlushWithNothingToSendReturnsAtOnce(self):
        self.assertTrue(self.out.flush(0.1))

    def testStopDoesNotWaitForTheRetryDelays(self):
        out = jeedom_publisher(self.receiver.url, 'KEY', retries=5, retry_delay=30)
        out.start()
        self.receiver.statuses = [500] * 10
        out.event({'type': 'x'})
        self.assertTrue(waitFor(lambda: self.receiver.posts))
        started = time.monotonic()
        out.stop()
        self.assertLess(time.monotonic() - started, 1.5)

    def testTest(self):
        self.assertTrue(self.out.test())
        unreachable = jeedom_publisher('http://127.0.0.1:1/jeesms4g.php', 'KEY', connect_timeout=0.5)
        with self.assertLogs(level='ERROR'):
            self.assertFalse(unreachable.test())
        unreachable.stop()

    def testUnreachableJeedomNeverBlocksTheCaller(self):
        out = jeedom_publisher('http://127.0.0.1:1/jeesms4g.php', 'KEY', connect_timeout=0.2, retries=2, retry_delay=0.01)
        out.start()
        started = time.monotonic()
        with self.assertLogs(level='ERROR'):
            for number in range(50):
                out.event({'type': 'x', 'number': number})
            self.assertLess(time.monotonic() - started, 0.5)  # queuing is immediate
            time.sleep(0.3)
            out.stop()

    def testIdIsAHexUuid(self):
        self.out.event({'type': 'x'})
        self.assertTrue(self.out.flush(5))
        self.assertTrue(re.fullmatch(r'[0-9a-f]{32}', self.receiver.received[0]['id']))


if __name__ == '__main__':
    unittest.main()
