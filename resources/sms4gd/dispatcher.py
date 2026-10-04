# This file is part of Jeedom.
#
# Jeedom is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Jeedom is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Jeedom. If not, see <http://www.gnu.org/licenses/>.

# Dispatcher: reads the messages that Jeedom sends to the socket, checks the apikey and routes each message to
# its handler by the ``cmd`` field (protocol v2, DEC-27). It never does the work itself: it submits it to the
# library and the answer is sent to Jeedom by the callback of the Future, through JeedomOut.

import json
import logging
import queue
import secrets
import threading
from concurrent.futures import Future
from typing import Any, Protocol

from atfilter import checkAtCommand
from wwanlib import (NotConnectedError, SmsDelivery, SmsExpired, SmsFailed, SmsIncomplete, SmsQueued, SmsQueueFullError,
                     SmsReceived, SmsSent, TimeoutException, maskNumber)

log = logging.getLogger(__name__)

DEFAULT_AT_TIMEOUT = 15.0
MIN_AT_TIMEOUT = 1.0
MAX_AT_TIMEOUT = 180.0  # the longest known command, AT+COPS=?
MAX_RESPONSE_CHARS = 4000


class ModemLike(Protocol):
    def command(self, command: str, timeout: float = ..., parseError: bool = ...) -> Future: ...

    def sendSms(self, number: str, text: str, ref: str | None = ..., maxPartsPerGroup: int = ...) -> str: ...


class OutLike(Protocol):
    def event(self, payload: dict[str, Any]) -> None: ...


def smsStatusMessage(event: Any) -> dict[str, Any] | None:
    """ The `smsStatus` message that tells Jeedom what became of an SMS, from an event of the library (None for any
    other event). It is an event for Jeedom (never merged with another one): `status` is `queued` (could not leave at
    once, it will be tried again), `sent` (every part accepted by the SMS center), `failed` or `expired`, then, when the
    delivery reports are asked for, what the network says: `pending` (delayed, it tries again), `delivered` (every part),
    `undelivered` (a part failed for good, `reason` says why) or `unknown` (no final report in time), with `parts` and
    `deliveredParts`. `ref` is the one given with the request, `smsId` the one of the library. """
    if not isinstance(event, (SmsQueued, SmsSent, SmsFailed, SmsExpired, SmsDelivery)):
        return None
    payload: dict[str, Any] = {'type': 'smsStatus', 'smsId': event.smsId, 'ref': event.ref, 'number': event.number}
    if isinstance(event, SmsQueued):
        payload.update(status='queued', reason=event.reason)
    elif isinstance(event, SmsSent):
        payload.update(status='sent', parts=event.parts, references=list(event.references))
    elif isinstance(event, SmsDelivery):
        payload.update(status=event.status, reason=event.reason, parts=event.parts, deliveredParts=event.deliveredParts)
    elif isinstance(event, SmsFailed):
        payload.update(status='failed', reason=event.reason, parts=event.parts, sentParts=event.sentParts)
    else:
        payload.update(status='expired', reason=event.reason)
    return payload


def smsInboxMessage(event: Any) -> dict[str, Any] | None:
    """ The message that hands Jeedom what the modem received, from an event of the library (None for any other
    event). Events for Jeedom, never merged with another one.
    `smsReceived`: `number` (the sender, as the network gives it), `message` (the whole text, a long SMS is already put
    back together), `parts`, `sent` (date of the SMS center, seconds since 1970 UTC, null if unreadable).
    `smsIncomplete`: a long SMS was given up, its last parts never came (`received` / `expected` parts, `reason`
    `timeout` or `overflow`); the text is not sent, a text with a hole would mislead. """
    if isinstance(event, SmsReceived):
        return {'type': 'smsReceived', 'number': event.number, 'message': event.text, 'parts': event.parts, 'sent': event.sent}
    if isinstance(event, SmsIncomplete):
        return {'type': 'smsIncomplete', 'number': event.number, 'received': event.received, 'expected': event.expected,
                'reason': event.reason}
    return None


class Dispatcher:
    def __init__(self, messages: 'queue.Queue[bytes | None]', modem: ModemLike, out: OutLike, apikey: str, diagnostic: bool):
        """ :param messages: the queue filled by the socket of Jeedom's daemon library (raw JSON lines)
        :param diagnostic: whether the AT commands sent from Jeedom are allowed (the daemon is the authority,
            the PHP side only checks it too) """
        self._messages = messages
        self._modem = modem
        self._out = out
        self._apikey = apikey
        self._diagnostic = diagnostic
        self._thread: threading.Thread | None = None
        self._handlers = {'atCommand': self._atCommand, 'sendSms': self._sendSms}

    def start(self) -> None:
        thread = threading.Thread(target=self._run, name='dispatcher', daemon=True)
        thread.start()
        self._thread = thread

    def stop(self) -> None:
        self._messages.put(None)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(2)

    def _run(self) -> None:
        while True:
            raw = self._messages.get()
            if raw is None:
                return
            try:
                self.handle(raw)
            except Exception:
                log.exception('Error while handling a message from Jeedom')

    def handle(self, raw: bytes) -> None:
        try:
            message = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            log.error('Message from Jeedom ignored: not a JSON message')
            return
        if not isinstance(message, dict):
            log.error('Message from Jeedom ignored: not a JSON object')
            return
        # compare_digest: no timing difference whatever the first wrong character is
        if not secrets.compare_digest(str(message.get('apikey', '')).encode(), self._apikey.encode()):
            log.error('Invalid apikey from socket')
            return
        command = message.get('cmd')
        handler = self._handlers.get(command) if isinstance(command, str) else None
        if handler is None:
            log.warning('Unknown command from Jeedom ignored: %r', command)
            return
        handler(message)

    # ---- handlers ---------------------------------------------------------------------------------

    def _sendSms(self, message: dict[str, Any]) -> None:
        """ Queues an SMS in the library. The result comes later as an event (``smsStatus``, see ``smsStatusMessage``),
        except when the request is refused here: it is then reported at once. ``ref`` is Jeedom's own reference
        (the command), given back untouched. """
        ref = message.get('ref')
        if ref is not None and not isinstance(ref, str):
            log.error('SMS ignored: the reference must be a text')
            return
        number = message.get('number')
        text = message.get('message')
        if not isinstance(number, str):
            self._rejectSms(ref, '', 'invalid number')
            return
        if not isinstance(text, str):
            self._rejectSms(ref, number, 'empty message')
            return
        try:
            maxPartsPerGroup = max(0, int(message.get('maxPartsPerGroup') or 0))
        except (TypeError, ValueError):
            maxPartsPerGroup = 0
        try:
            smsId = self._modem.sendSms(number, text, ref, maxPartsPerGroup)
        except SmsQueueFullError:
            self._rejectSms(ref, number, 'queue full')
        except NotConnectedError:
            self._rejectSms(ref, number, 'modem disconnected')
        else:
            # Never the text of the message in the logs
            log.info('SMS %s accepted for %s (%d characters)', smsId, maskNumber(number), len(text))

    def _rejectSms(self, ref: str | None, number: str, reason: str) -> None:
        log.warning('SMS to %s refused (%s)', maskNumber(number), reason)
        self._out.event({'type': 'smsStatus', 'ref': ref, 'number': number, 'status': 'failed', 'reason': reason,
                         'parts': 0, 'sentParts': 0})
    def _atCommand(self, message: dict[str, Any]) -> None:
        requestId = message.get('id')
        command = message.get('command')
        if not isinstance(requestId, str) or not requestId:
            log.error('AT command ignored: no request id')
            return
        shown = command if isinstance(command, str) else ''

        if not self._diagnostic:
            self._refuse(requestId, shown, 'diagnostic mode is disabled')
            return
        verdict = checkAtCommand(command)
        if not verdict.allowed:
            self._refuse(requestId, verdict.command or shown, verdict.reason)
            return

        timeout = self._timeout(message.get('timeout'))
        log.info('AT command accepted: %s (timeout %gs)', verdict.command, timeout)
        # parseError=False: the raw answer (+CME ERROR: 14, +CMS ERROR: 322...) is what a diagnostic needs
        future = self._modem.command(verdict.command, timeout, False)
        future.add_done_callback(lambda done: self._answer(requestId, verdict.command, timeout, done))

    @staticmethod
    def _timeout(value: Any) -> float:
        try:
            timeout = float(value)
        except (TypeError, ValueError):
            return DEFAULT_AT_TIMEOUT
        return min(max(timeout, MIN_AT_TIMEOUT), MAX_AT_TIMEOUT)

    def _refuse(self, requestId: str, command: str, reason: str) -> None:
        log.warning('AT command refused (%s): %s', reason, command)
        self._out.event({'type': 'atResponse', 'requestId': requestId, 'command': command, 'status': 'refused', 'lines': [], 'error': reason})

    def _answer(self, requestId: str, command: str, timeout: float, done: Future) -> None:
        """ Callback of the Future: runs in the Executor thread, so it only builds the payload and queues it """
        try:
            self._out.event(self._response(requestId, command, timeout, done))
        except Exception:
            log.exception('Error while building the answer to an AT command')

    @staticmethod
    def _response(requestId: str, command: str, timeout: float, done: Future) -> dict[str, Any]:
        payload: dict[str, Any] = {'type': 'atResponse', 'requestId': requestId, 'command': command, 'lines': []}
        error = done.exception()
        if error is None:
            lines = done.result()
            payload['lines'] = lines
            payload['status'] = 'ok' if lines and lines[-1] == 'OK' else 'error'
            if payload['status'] == 'error':
                payload['error'] = lines[-1] if lines else 'no answer'
        elif isinstance(error, TimeoutException):
            payload['status'] = 'timeout'
            payload['error'] = f'no response within {timeout:g} s'
            if isinstance(error.data, list):
                payload['lines'] = error.data
        elif isinstance(error, NotConnectedError):
            payload['status'] = 'notConnected'
            payload['error'] = str(error)
        else:
            payload['status'] = 'error'
            payload['error'] = f'{type(error).__name__}: {error}'
        return Dispatcher._truncate(payload)

    @staticmethod
    def _truncate(payload: dict[str, Any]) -> dict[str, Any]:
        kept: list[str] = []
        size = 0
        for line in payload['lines']:
            size += len(line) + 1
            if size > MAX_RESPONSE_CHARS:
                payload['truncated'] = True
                break
            kept.append(line)
        payload['lines'] = kept
        return payload
