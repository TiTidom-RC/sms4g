""" wwanlib: driving a mobile network modem with AT commands, independently of Jeedom.

Public API (everything else is internal):

* ``Modem`` / ``ModemOptions`` (``modem.py``): ``start()`` (non blocking), ``command()``, ``stop()``,
  ``onEvent()``, ``state``, ``profile``.
* Events (``events.py``): ``StateChanged``, ``ModemIdentified``, ``SignalChanged``, ``NetworkChanged``,
  ``UnsolicitedNotification``; connection states in ``ConnectionState``, network registration in ``Registration``.
* Exceptions (``exceptions.py``): ``WwanException`` is the base class.

Architecture: one Reader thread (the only one that reads the port), one Executor thread (the only one that
writes to it, one transaction at a time), one Supervisor thread (connection, reconnection, monitoring of the
signal and of the network) and one thread that delivers the events. The library only depends on pyserial,
never imports Jeedom and logs through ``logging.getLogger(__name__)``.
"""

from .events import (ConnectionState, ModemIdentified, NetworkChanged, Registration, SignalChanged, StateChanged,
                     UnsolicitedNotification)
from .exceptions import (CmeError, CmsError, CommandError, EncodingError, IncorrectPinError, NotConnectedError,
                         PduModeNotSupportedError, PinRequiredError, PukRequiredError, SmscNumberUnknownError,
                         TimeoutException, WwanException)
from .modem import Modem, ModemOptions
from .profiles import Profile

__all__ = [
    'Modem', 'ModemOptions', 'Profile', 'ConnectionState', 'Registration', 'StateChanged', 'ModemIdentified',
    'SignalChanged', 'NetworkChanged', 'UnsolicitedNotification',
    'WwanException', 'CommandError', 'CmeError', 'CmsError', 'TimeoutException', 'NotConnectedError',
    'PinRequiredError', 'IncorrectPinError', 'PukRequiredError', 'SmscNumberUnknownError', 'EncodingError',
    'PduModeNotSupportedError',
]
