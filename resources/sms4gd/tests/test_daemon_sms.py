""" Test of the sending of an SMS through the whole daemon chain, without Jeedom nor modem:

    request of Jeedom --> Dispatcher --> wwanlib.Modem (fake port) --events--> jeedom_publisher --HTTP--> fake Jeedom

Run from resources/sms4gd:  python -m unittest discover -s tests -t .
"""

import json
import queue
import threading
import unittest
from unittest import mock

import serial

from dispatcher import Dispatcher, smsStatusMessage
from jeedom.jeedom import jeedom_publisher
from tests.test_outbox import smsBehavior
from tests.test_publisher import Receiver
from tests.test_wwanlib import FakeSerial, waitFor
from wwanlib import ConnectionState, Modem, ModemOptions
from wwanlib.executor import Executor

NUMBER = '+33612345678'


class DaemonSmsTest(unittest.TestCase):
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

        self.receiver = Receiver()
        self.addCleanup(self.receiver.close)
        self.publisher = jeedom_publisher(self.receiver.url, 'KEY', retries=2, retry_delay=0.02)
        self.publisher.start()
        self.addCleanup(self.publisher.stop)
        self.modem = Modem('fake', 115200, options=ModemOptions(
            reconnectBaseDelay=0.05, reconnectMaxDelay=0.1, reconnectMaxAttempts=100, segmentPause=0.0, smsQueueSize=2))
        self.modem.onEvent(self.onEvent)
        self.addCleanup(self.modem.stop)
        self.dispatcher = Dispatcher(queue.Queue(), self.modem, self.publisher, 'KEY', diagnostic=False)

    def onEvent(self, event):
        """ What sms4gd.py does with the events of the library, as far as the SMS are concerned """
        status = smsStatusMessage(event)
        if status is not None:
            self.publisher.event(status)

    def request(self, **fields):
        self.dispatcher.handle(json.dumps({'apikey': 'KEY', 'cmd': 'sendSms', **fields}).encode())

    def statuses(self) -> list[dict]:
        return [message for message in self.receiver.received if message.get('type') == 'smsStatus']

    def startConnected(self):
        self.modem.start()
        self.assertTrue(waitFor(lambda: self.modem.state == ConnectionState.CONNECTED))

    def testASmsIsSentAndJeedomIsTold(self):
        self.startConnected()
        self.request(number=NUMBER, message='Hello', ref='42', maxPartsPerGroup=0)
        self.assertTrue(waitFor(lambda: self.statuses()))
        status = self.statuses()[0]
        self.assertEqual({key: status[key] for key in ('type', 'ref', 'number', 'status', 'parts', 'references')},
                         {'type': 'smsStatus', 'ref': '42', 'number': NUMBER, 'status': 'sent', 'parts': 1, 'references': [7]})
        self.assertRegex(status['smsId'], r'^[0-9a-f]{16}$')
        self.assertRegex(status['id'], r'^[0-9a-f]{32}$')  # the envelope of the publisher: Jeedom ignores a repeated one
        self.assertEqual(len(self.statuses()), 1)

    def testALongSmsIsOneStatus(self):
        self.startConnected()
        self.request(number=NUMBER, message='a' * 400, ref='long')
        self.assertTrue(waitFor(lambda: self.statuses()))
        self.assertEqual((self.statuses()[0]['status'], self.statuses()[0]['parts'], len(self.statuses()[0]['references'])), ('sent', 3, 3))

    def testQueuedWhileTheModemReconnectsThenSent(self):
        self.startConnected()
        self.blocked.set()
        FakeSerial.instances[0].unplug()
        self.assertTrue(waitFor(lambda: self.modem.state == ConnectionState.RECONNECTING))
        self.request(number=NUMBER, message='Hello', ref='r')
        self.assertTrue(waitFor(lambda: [s['status'] for s in self.statuses()] == ['queued']))
        self.assertEqual(self.statuses()[0]['reason'], 'modem not connected')
        self.blocked.clear()
        self.assertTrue(waitFor(lambda: [s['status'] for s in self.statuses()] == ['queued', 'sent']))
        self.assertEqual([s['ref'] for s in self.statuses()], ['r', 'r'])

    def testFullQueueAndStopAreReported(self):
        self.blocked.set()
        self.modem.start()
        self.request(number=NUMBER, message='one', ref='a')
        self.request(number=NUMBER, message='two', ref='b')
        self.request(number=NUMBER, message='three', ref='c')  # the queue holds 2 SMS
        self.assertTrue(waitFor(lambda: any(s['ref'] == 'c' for s in self.statuses())))
        refused = [s for s in self.statuses() if s['ref'] == 'c'][0]
        self.assertEqual((refused['status'], refused['reason']), ('failed', 'queue full'))
        self.modem.stop()  # the two SMS that wait fail, and Jeedom is told before the daemon exits
        self.assertTrue(self.publisher.flush(5))
        stopped = sorted(s['ref'] for s in self.statuses() if s['status'] == 'failed' and s['reason'] == 'daemon stopped')
        self.assertEqual(stopped, ['a', 'b'])


if __name__ == '__main__':
    unittest.main()
