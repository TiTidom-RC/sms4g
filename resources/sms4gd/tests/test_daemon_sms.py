""" Test of the sending of an SMS through the whole daemon chain, without Jeedom nor modem:

    request of Jeedom --> Dispatcher --> wwanlib.Modem (fake port) --events--> jeedom_publisher --HTTP--> fake Jeedom

Run from resources/sms4gd:  python -m unittest discover -s tests -t .
"""

import json
import queue
import threading
import time
import unittest
from unittest import mock

import serial

from dispatcher import Dispatcher, smsInboxMessage, smsStatusMessage
from jeedom.jeedom import jeedom_publisher
from tests.test_inbox import FakeSim, NUMBER as SENDER, SENT, deliverPdus, simBehavior, statusReportPdu
from tests.test_outbox import smsBehavior
from tests.test_publisher import Receiver
from tests.test_wwanlib import FakeSerial, waitFor
from wwanlib import ConnectionState, Modem, ModemOptions
from wwanlib.executor import Executor

NUMBER = '+33612345678'


def receptionBehavior(sim: FakeSim):
    """ Fake modem that sends SMS (as ``smsBehavior``) and holds the SMS memory of ``sim`` """
    sending, memory = smsBehavior(), simBehavior(sim)

    def behavior(fake, data):
        command = data.decode().rstrip('\r')
        (memory if command.startswith(('AT+CMGR=', 'AT+CMGD=', 'AT+CMGL=', 'AT+CPMS?')) else sending)(fake, data)

    return behavior


class DaemonSmsTest(unittest.TestCase):
    def setUp(self):
        FakeSerial.instances.clear()
        self.sim = FakeSim()
        FakeSerial.behavior = receptionBehavior(self.sim)
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
            reconnectBaseDelay=0.05, reconnectMaxDelay=0.1, reconnectMaxAttempts=100, segmentPause=0.0, smsQueueSize=2,
            monitorInterval=0.1, concatPartsTtl=0.5, deliveryReport=True))
        self.modem.onEvent(self.onEvent)
        self.addCleanup(self.modem.stop)
        self.dispatcher = Dispatcher(queue.Queue(), self.modem, self.publisher, 'KEY', diagnostic=False)

    def onEvent(self, event):
        """ What sms4gd.py does with the events of the library, as far as the SMS are concerned """
        message = smsStatusMessage(event) or smsInboxMessage(event)
        if message is not None:
            self.publisher.event(message)

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

    def received(self, kind: str) -> list[dict]:
        return [message for message in self.receiver.received if message.get('type') == kind]

    def testAnSmsReceivedReachesJeedom(self):
        self.startConnected()
        self.sim.add(0, deliverPdus(SENDER, 'Allume le salon')[0])
        FakeSerial.instances[0].feed(b'\r\n+CMTI: "SM",0\r\n')
        self.assertTrue(waitFor(lambda: self.received('smsReceived')))
        message = self.received('smsReceived')[0]
        self.assertEqual({key: message[key] for key in ('type', 'number', 'message', 'parts', 'sent')}, {
            'type': 'smsReceived', 'number': SENDER, 'message': 'Allume le salon', 'parts': 1, 'sent': SENT})
        self.assertRegex(message['id'], r'^[0-9a-f]{32}$')
        self.assertEqual(self.sim.store, {})

    def testTheDeliveryReportsBecomeTheStatesOfTheSms(self):
        self.startConnected()
        self.request(number=NUMBER, message='Hello', ref='42:abc')
        self.assertTrue(waitFor(lambda: [s['status'] for s in self.statuses()] == ['sent']))
        port = FakeSerial.instances[0]
        port.feed(('\r\n+CDS: 26\r\n' + statusReportPdu(7, NUMBER, status=0x21) + '\r\n').encode())  # delayed
        self.assertTrue(waitFor(lambda: [s['status'] for s in self.statuses()] == ['sent', 'pending']))
        port.feed(('\r\n+CDS: 26\r\n' + statusReportPdu(7, NUMBER) + '\r\n').encode())  # delivered
        self.assertTrue(waitFor(lambda: [s['status'] for s in self.statuses()] == ['sent', 'pending', 'delivered']))
        pending, delivered = self.statuses()[1:]
        self.assertEqual((pending['reason'], pending['ref'], pending['number']), ('recipient busy', '42:abc', NUMBER))
        self.assertEqual((delivered['parts'], delivered['deliveredParts'], delivered['reason']), (1, 1, ''))

    def testALongSmsReceivedIsOneMessageForJeedom(self):
        self.startConnected()
        text = ' '.join(f'{index:02d}-abcdefghij' for index in range(1, 29))
        for index, pdu in enumerate(deliverPdus(SENDER, text)):
            self.sim.add(index, pdu)
            FakeSerial.instances[0].feed(f'\r\n+CMTI: "SM",{index}\r\n'.encode())
        self.assertTrue(waitFor(lambda: self.received('smsReceived')))
        self.assertEqual((len(self.received('smsReceived')), self.received('smsReceived')[0]['message'], self.received('smsReceived')[0]['parts']),
                         (1, text, 3))

    def testAnIncompleteSmsIsReportedWithoutItsText(self):
        self.startConnected()
        self.sim.add(0, deliverPdus(SENDER, 'secret ' * 60)[0])
        FakeSerial.instances[0].feed(b'\r\n+CMTI: "SM",0\r\n')
        self.assertTrue(waitFor(lambda: self.received('smsIncomplete')))
        message = self.received('smsIncomplete')[0]
        self.assertEqual({key: message[key] for key in ('number', 'received', 'expected', 'reason')},
                         {'number': SENDER, 'received': 1, 'expected': 3, 'reason': 'timeout'})
        self.assertNotIn('secret', json.dumps(message))
        self.assertEqual(self.received('smsReceived'), [])

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


class ReplyDelayChainTest(unittest.TestCase):
    """ A reply asked for as soon as an SMS is received (what an interaction does) leaves after the reply delay """

    def setUp(self):
        FakeSerial.instances.clear()
        self.sim = FakeSim()
        FakeSerial.behavior = receptionBehavior(self.sim)
        patcher = mock.patch('wwanlib.transport.serial.Serial', FakeSerial)
        patcher.start()
        self.addCleanup(patcher.stop)
        for owner, name, value in ((Modem, 'MIN_MONITOR_INTERVAL', 0.05), (Executor, 'READY_INTERVAL', 0.1)):
            patcher = mock.patch.object(owner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_(self, **options) -> tuple[float, float]:
        """ :return: when the SMS was announced and when the reply was written to the port """
        modem = Modem('fake', 115200, options=ModemOptions(
            reconnectBaseDelay=0.05, reconnectMaxDelay=0.1, reconnectMaxAttempts=100, segmentPause=0.0, **options))
        replied = threading.Event()

        def onEvent(event):
            if smsInboxMessage(event) is not None and not replied.is_set():
                replied.set()
                modem.sendSms(SENDER, 'the answer', ref='1')  # the reply of an interaction, at once

        modem.onEvent(onEvent)
        self.addCleanup(modem.stop)
        modem.start()
        self.assertTrue(waitFor(lambda: modem.state == ConnectionState.CONNECTED))
        port = FakeSerial.instances[0]
        self.sim.add(0, deliverPdus(SENDER, 'a question')[0])
        announced = time.monotonic()
        port.feed(b'\r\n+CMTI: "SM",0\r\n')
        self.assertTrue(waitFor(lambda: any(data.startswith(b'AT+CMGS=') for _, data in port.written)))
        written = next(moment for moment, data in port.written if data.startswith(b'AT+CMGS='))
        return announced, written

    def testTheReplyWaitsForTheDelay(self):
        announced, written = self.run_(replyDelay=0.5)
        self.assertGreaterEqual(written - announced, 0.5)

    def testWithoutDelayTheReplyLeavesAtOnce(self):
        announced, written = self.run_()
        self.assertLess(written - announced, 0.4)


if __name__ == '__main__':
    unittest.main()
