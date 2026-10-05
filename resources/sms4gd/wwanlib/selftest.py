""" Self-test of the SMS service: the modem sends an SMS to its own SIM and waits for it to come back and for its
delivery report.

The two ways in (``+CMTI``, ``+CDS``) can stop working one without the other, without a word and without anything
showing in the settings (seen on a SIM7600: the delivery reports stopped for 45 minutes, only ``AT+CRESET`` fixed it).
A SIM that is always on is the only recipient that cannot be off or out of coverage, so a missing answer can only be the
modem.

Nothing of the test reaches the users of the library: the events of the SMS (queued, sent, report) and the SMS received
are caught by ``SelfTest.intercept`` before they are published.
"""

import dataclasses
import logging
import re
import secrets
import threading
import time
from collections.abc import Callable
from typing import Any

from .events import SelfTestResult, SmsDelivery, SmsExpired, SmsFailed, SmsQueued, SmsReceived, SmsSent
from .exceptions import WwanException
from .sms import maskNumber

log = logging.getLogger(__name__)

REF_PREFIX = 'selfTest'  # reference given to the SMS: the events that carry it are the test's
TEXT_PREFIX = 'sms4g test '
TEXT = re.compile(r'^sms4g test ([0-9a-f]{8})$')
NOT_CONNECTED = 'modem not connected'
FOLLOW_UP_DELAY = 300.0  # seconds before the test that checks a restart (or a connection that was not there)


def _tail(number: str) -> str:
    return re.sub(r'\D', '', number)[-8:]


class _Run:
    def __init__(self, token: str):
        self.token = token
        self.sent = False
        self.failed: str | None = None
        self.received = False
        self.reported = False


class SelfTest:
    def __init__(self, submit: Callable[[str, str, str], str], publish: Callable[[Any], None],
                 isConnected: Callable[[], bool], restart: Callable[[], None], ownNumber: str | None,
                 expectReceipt: bool, interval: float = 0.0, autoRestart: bool = False, timeout: float = 60.0,
                 sendTimeout: float = 60.0, followUp: float = FOLLOW_UP_DELAY, minRestartInterval: float = 3600.0):
        """ :param submit: queues an SMS (``Outbox.submit``): number, text, reference
        :param publish: receives the ``SelfTestResult``
        :param restart: restarts the modem (may raise ``WwanException``)
        :param expectReceipt: whether the delivery reports are asked for: without them only the reception is tested
        :param interval: seconds between two tests (0: none but the ones asked for)
        :param autoRestart: restart the modem when the test fails, once per ``minRestartInterval`` at most
        :param timeout: seconds, from the sending, the SMS and its report are waited for """
        self._submit = submit
        self._publish = publish
        self._isConnected = isConnected
        self._restart = restart
        self._ownNumber = ownNumber
        self._expectReceipt = expectReceipt
        self._interval = interval
        self._autoRestart = autoRestart
        self._timeout = timeout
        self._sendTimeout = sendTimeout
        self._followUp = followUp
        self._minRestartInterval = minRestartInterval
        self._cond = threading.Condition()
        self._run: _Run | None = None
        self._lastRestart: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- scheduling -------------------------------------------------------------------------------

    def start(self) -> None:
        if self._interval <= 0:
            return
        self._stop.clear()
        thread = threading.Thread(target=self._loop, name='wwanlib-selftest', daemon=True)
        thread.start()
        self._thread = thread

    def stop(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(2.0)

    def _loop(self) -> None:
        delay = self._interval
        while not self._stop.wait(delay):
            try:
                result = self.execute(scheduled=True)
            except Exception:
                log.exception('Error in the self-test')
                delay = self._interval
                continue
            delay = self._interval
            if result.restarted or (result.status == 'skipped' and result.reason == NOT_CONNECTED):
                delay = min(self._interval, self._followUp)

    # ---- the test ---------------------------------------------------------------------------------

    def execute(self, scheduled: bool = False) -> SelfTestResult:
        """ Runs the test (blocks up to about ``sendTimeout`` + ``timeout``) and publishes its result. A test that was
        not even tried because the modem is not connected is not published when it comes from the schedule. """
        result = self._attempt()
        if result.status in ('noReception', 'noReceipt'):
            result = self._react(result)
        if not (scheduled and result.status == 'skipped' and result.reason == NOT_CONNECTED):
            self._publish(result)
        return result

    def _attempt(self) -> SelfTestResult:
        started = time.monotonic()

        def finish(status: str, reason: str = '') -> SelfTestResult:
            return SelfTestResult(status, reason, round(time.monotonic() - started, 1))

        if not self._ownNumber:
            return finish('skipped', 'no number for the SIM')
        with self._cond:
            if self._run is not None:
                return finish('skipped', 'a test is already running')
            if not self._isConnected():
                return finish('skipped', NOT_CONNECTED)
            token = secrets.token_hex(4)
            run = self._run = _Run(token)
        try:
            try:
                self._submit(self._ownNumber, TEXT_PREFIX + token, f'{REF_PREFIX}:{token}')
            except WwanException as e:
                return finish('skipped', f'the SMS could not be queued ({e})')
            with self._cond:
                self._cond.wait_for(lambda: run.sent or run.failed is not None or self._stop.is_set(), self._sendTimeout)
                if run.failed is not None:
                    return finish('skipped', f'the SMS could not be sent ({run.failed})')
                if not run.sent:
                    return finish('skipped', 'the SMS stayed in the queue')
                self._cond.wait_for(lambda: self._complete(run) or self._stop.is_set(), self._timeout)
                if self._complete(run):
                    return finish('ok')
                if not run.received:
                    return finish('noReception', 'the SMS sent to the SIM did not come back')
                return finish('noReceipt', 'the SMS came back but its delivery report did not')
        finally:
            with self._cond:
                self._run = None

    def _complete(self, run: _Run) -> bool:
        return run.received and (run.reported or not self._expectReceipt)

    def _react(self, result: SelfTestResult) -> SelfTestResult:
        log.error('Self-test failed: %s', result.reason)
        if not self._autoRestart:
            return result
        now = time.monotonic()
        if self._lastRestart is not None and now - self._lastRestart < self._minRestartInterval:
            log.error('The modem was already restarted less than %d minutes ago: no other restart, it needs a look',
                      self._minRestartInterval // 60)
            return dataclasses.replace(result, reason=result.reason + ' (again after a restart: intervention needed)')
        try:
            self._restart()
        except WwanException as e:
            log.warning('The modem could not be restarted after the failed self-test (%s)', e)
            return result
        self._lastRestart = now
        return dataclasses.replace(result, restarted=True)

    # ---- the events of the test -------------------------------------------------------------------

    def intercept(self, event: Any) -> bool:
        """ True when the event belongs to the test (it must not be published). Called for every SMS event. """
        if isinstance(event, (SmsQueued, SmsSent, SmsFailed, SmsExpired, SmsDelivery)):
            ref = event.ref
            if isinstance(ref, str) and ref.startswith(REF_PREFIX + ':'):
                self._onSmsEvent(event, ref.split(':', 1)[1])
                return True
            return False
        if isinstance(event, SmsReceived):
            match = TEXT.match(event.text)
            if match and self._ownNumber and _tail(event.number) == _tail(self._ownNumber):
                with self._cond:
                    run = self._run
                    if run is not None and run.token == match.group(1):
                        run.received = True
                        self._cond.notify_all()
                    else:
                        log.debug('SMS of an earlier self-test received from %s, ignored', maskNumber(event.number))
                return True
        return False

    def _onSmsEvent(self, event: Any, token: str) -> None:
        with self._cond:
            run = self._run
            if run is None or run.token != token:
                return
            if isinstance(event, SmsSent):
                run.sent = True
            elif isinstance(event, SmsDelivery):
                run.reported = True
            elif isinstance(event, (SmsFailed, SmsExpired)):
                run.failed = event.reason
            self._cond.notify_all()
