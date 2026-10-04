""" wwanlib: driving a mobile network modem with AT commands, independently of Jeedom.

Public API (everything else is internal):

* ``Modem`` / ``ModemOptions`` (``modem.py``): ``start()`` (non blocking), ``command()``, ``stop()``,
  ``onEvent()``, ``state``, ``profile``.
* ``Modem.sendSms()``: queues an SMS (``outbox.py``, ``sms.py``); the result comes as an event.
  ``maskNumber()``: a phone number as it may appear in a log.
* Received SMS (``inbox.py``): read as soon as the modem announces them, long ones reassembled; the result comes as
  an event, ``SmsReceived`` or ``SmsIncomplete``.
* Events (``events.py``): ``StateChanged``, ``ModemIdentified``, ``SignalChanged``, ``NetworkChanged``,
  ``UnsolicitedNotification``, ``SmsQueued``, ``SmsSent``, ``SmsFailed``, ``SmsExpired``, ``SmsReceived``, ``SmsIncomplete``, ``SmsDelivery``; connection states in ``ConnectionState``, network registration in ``Registration``.
* Exceptions (``exceptions.py``): ``WwanException`` is the base class.

Architecture: one Reader thread (the only one that reads the port), one Executor thread (the only one that
writes to it, one transaction at a time), one Supervisor thread (connection, reconnection, monitoring of the
signal and of the network, and the expiry of the incomplete long SMS), one thread that sends the queued SMS
(``sms-sender``) and one thread that delivers the events. Received SMS have no thread: the Reader notices them and
the Executor reads them. The library only depends on pyserial,
never imports Jeedom and logs through ``logging.getLogger(__name__)``.
"""

from .events import (ConnectionState, ModemIdentified, NetworkChanged, Registration, SignalChanged, SmsExpired,
                     SmsDelivery, SmsFailed, SmsIncomplete, SmsQueued, SmsReceived, SmsSent, StateChanged,
                     UnsolicitedNotification)
from .exceptions import (CmeError, CmsError, CommandError, EncodingError, IncorrectPinError, NotConnectedError,
                         PduModeNotSupportedError, PinRequiredError, PukRequiredError, SmscNumberUnknownError,
                         SmsQueueFullError, TimeoutException, WwanException)
from .modem import Modem, ModemOptions
from .profiles import Profile
from .sms import maskNumber

__all__ = [
    'Modem', 'ModemOptions', 'Profile', 'ConnectionState', 'Registration', 'StateChanged', 'ModemIdentified',
    'SignalChanged', 'NetworkChanged', 'UnsolicitedNotification',
    'WwanException', 'CommandError', 'CmeError', 'CmsError', 'TimeoutException', 'NotConnectedError',
    'PinRequiredError', 'IncorrectPinError', 'PukRequiredError', 'SmscNumberUnknownError', 'EncodingError',
    'maskNumber', 'PduModeNotSupportedError', 'SmsQueueFullError', 'SmsQueued', 'SmsSent', 'SmsFailed', 'SmsExpired',
    'SmsReceived', 'SmsIncomplete', 'SmsDelivery',
]
