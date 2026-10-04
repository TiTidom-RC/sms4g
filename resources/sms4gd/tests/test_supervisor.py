""" Tests of the Supervisor on its own: reconnection delays cut short by the return of the port """

import threading
import time
import unittest
from unittest import mock

from tests.test_wwanlib import waitFor
from wwanlib import supervisor
from wwanlib.supervisor import Supervisor


class Harness:
    def __init__(self, failures: int = 0, **options):
        self.tokens: list = []
        self.states: list[tuple[str, dict]] = []
        self.connectAttempts: list[float] = []
        self.failures = failures  # the connections that fail after the first one
        self.portPresent = True
        self.connected = threading.Event()
        self.reconnected = threading.Event()
        defaults = dict(baseDelay=5.0, maxDelay=5.0, maxAttempts=5, portPresent=lambda: self.portPresent,
                        portPollInterval=0.02, portSettle=0.02)
        defaults.update(options)
        self.supervisor = Supervisor(self.connect, lambda: None, lambda state, details: self.states.append((state, details)),
                                     lambda error: False, **defaults)

    def connect(self, token):
        self.connectAttempts.append(time.monotonic())
        self.tokens.append(token)
        if len(self.connectAttempts) > 1:
            if self.failures > 0:
                self.failures -= 1
                raise OSError('busy')
            self.reconnected.set()
        self.connected.set()

    def lose(self):
        self.supervisor.reportFailure(self.tokens[-1], 'port lost')


class SupervisorPortTest(unittest.TestCase):
    def start(self, **kwargs) -> Harness:
        harness = Harness(**kwargs)
        self.addCleanup(harness.supervisor.requestStop)
        harness.supervisor.start()
        self.assertTrue(harness.connected.wait(2))
        return harness

    def testThePortComingBackEndsTheDelay(self):
        harness = self.start()
        harness.portPresent = False
        harness.lose()
        time.sleep(0.2)
        before = time.monotonic()
        harness.portPresent = True
        self.assertTrue(harness.reconnected.wait(2))  # the delay was 5 s
        self.assertLess(harness.connectAttempts[-1] - before, 1.0)

    def testAPortThatNeverLeftIsWaitedForInFull(self):
        harness = self.start(baseDelay=0.4, maxDelay=0.4)
        lost = time.monotonic()
        harness.lose()
        self.assertTrue(harness.reconnected.wait(3))
        self.assertGreaterEqual(harness.connectAttempts[-1] - lost, 0.35)

    def testWithoutPortInformationTheDelayIsWaitedInFull(self):
        harness = self.start(baseDelay=0.3, maxDelay=0.3, portPresent=None)
        lost = time.monotonic()
        harness.lose()
        self.assertTrue(harness.reconnected.wait(3))
        self.assertGreaterEqual(harness.connectAttempts[-1] - lost, 0.25)

    def testAnAttemptThatFailsRightAfterTheReturnIsRetriedQuickly(self):
        # the device is still busy while the system sets the port up (Errno 16)
        with mock.patch.object(supervisor, 'QUICK_RETRY_DELAY', 0.05):
            harness = self.start(failures=2)
            harness.portPresent = False
            harness.lose()
            time.sleep(0.1)
            harness.portPresent = True
            self.assertTrue(harness.reconnected.wait(3))
        self.assertEqual(len(harness.connectAttempts), 4)
        # the attempts made because the port came back do not use the budget: still attempt 1
        attempts = [details['attempt'] for state, details in harness.states if state == 'reconnecting']
        self.assertEqual(set(attempts), {1})

    def stateNames(self, harness) -> list[str]:
        return [state for state, _ in harness.states]

    def testARestartShowsRestartingUntilTheModemIsBack(self):
        harness = self.start(baseDelay=0.05, maxDelay=0.05, portPresent=None)
        self.assertTrue(harness.supervisor.beginRestart('requested', 30))
        harness.lose()
        self.assertTrue(harness.reconnected.wait(3))
        self.assertTrue(waitFor(lambda: self.stateNames(harness)[-1] == 'connected'))
        self.assertEqual(self.stateNames(harness), ['connecting', 'connected', 'restarting', 'connected'])
        self.assertEqual(harness.states[2], ('restarting', {'reason': 'requested'}))
        self.assertTrue(harness.supervisor.beginRestart('auto', 30))  # the next one is possible

    def testASecondRestartIsRefusedWhileOneIsInProgress(self):
        harness = self.start()
        self.assertTrue(harness.supervisor.beginRestart('requested', 30))
        self.assertFalse(harness.supervisor.beginRestart('auto', 30))

    def testAModemThatDoesNotRestartGoesBackToNormal(self):
        harness = self.start()
        self.assertTrue(harness.supervisor.beginRestart('requested', 0.2))
        self.assertTrue(waitFor(lambda: self.stateNames(harness)[-1] == 'connected'))
        self.assertEqual(self.stateNames(harness), ['connecting', 'connected', 'restarting', 'connected'])
        self.assertTrue(harness.supervisor.beginRestart('requested', 30))

    def testARefusedRestartGoesBackToConnected(self):
        harness = self.start()
        harness.supervisor.beginRestart('requested', 30)
        harness.supervisor.cancelRestart()
        self.assertEqual(self.stateNames(harness), ['connecting', 'connected', 'restarting', 'connected'])

    def testTheMonitoringPausesDuringARestart(self):
        calls = []
        harness = self.start(monitor=lambda: calls.append(1) or 'registered', monitorInterval=0.05)
        harness.supervisor.beginRestart('requested', 30)
        time.sleep(0.15)
        count = len(calls)
        time.sleep(0.3)
        self.assertEqual(len(calls), count)

    def testStoppingEndsTheWait(self):
        harness = self.start()
        harness.portPresent = False
        harness.lose()
        time.sleep(0.1)
        stopped = time.monotonic()
        harness.supervisor.requestStop()
        harness.supervisor.join(2)
        self.assertLess(time.monotonic() - stopped, 1.0)


if __name__ == '__main__':
    unittest.main()
