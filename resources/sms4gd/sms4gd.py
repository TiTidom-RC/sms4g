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

# Daemon of the sms4g plugin: glue between Jeedom and the wwanlib library, which owns the modem.
# Reduced to the minimum during the rewrite (milestone J1): it starts the modem, logs a diagnostic
# at each connection and stops cleanly. Sending, receiving and the Jeedom callbacks come back with J2 to J5.

import argparse
import json
import logging
import os
import re
import secrets
import signal
import sys
import threading
import traceback
from queue import Empty
from typing import Optional

from wwanlib import (ConnectionState, Modem, ModemIdentified, ModemOptions, NetworkChanged, SignalChanged, StateChanged,
                     WwanException)

try:
    from jeedom.jeedom import jeedom_com, jeedom_socket, jeedom_utils, JEEDOM_SOCKET_MESSAGE

    # Type hints for global instances (initialized later)
    j_com_instance: Optional[jeedom_com] = None
    j_socket_instance: Optional[jeedom_socket] = None

except ImportError as e:
    print("Error: importing module from jeedom folder: %s", e)
    sys.exit(1)

# PARAMETERS #

modem: Optional[Modem] = None
_stopEvent = threading.Event()
_identity: Optional[ModemIdentified] = None
_DIAGNOSTIC_COMMANDS = ('AT+CSQ', 'AT+CREG?', 'AT+COPS?', 'AT+CPMS?', 'AT+CNMI?', 'AT+CSMS?')


class SecretMaskFilter(logging.Filter):
    """Masque en direct les valeurs sensibles pouvant apparaître dans les logs de bibliothèques
    tierces (ex: requests/urllib3 loguant l'URL complète d'une requête) ou du démon lui-même."""

    @staticmethod
    def _maskPhone(m: 're.Match') -> str:
        number = m.group(0)
        prefix, suffix = number[:4], number[-2:]
        return prefix + ('X' * (len(number) - len(prefix) - len(suffix))) + suffix

    _RULES = [
        (re.compile(r'(["\']?apikey["\']?\s*[:=]\s*["\']?)[^"\'&\s]+', re.IGNORECASE), r'\1sEcReT'),
        (re.compile(r'(AT\+CPIN=")[^"]+(")', re.IGNORECASE), r'\1****\2'),
        (re.compile(r'\+\d{6,15}'), _maskPhone),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = message
        for pattern, repl in self._RULES:
            redacted = pattern.sub(repl, redacted)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def runDiagnostic():
    """Journalise l'état du modem à chaque connexion (identité, signal, réseau, mémoire SMS, notifications).
    Sert notamment à analyser l'erreur 322 : valeurs de CNMI / CSMS et remplissage de la mémoire (CPMS)."""
    if modem is None:
        return
    if _identity:
        logging.info("Diagnostic : profile=%s, manufacturer=%s, model=%s, revision=%s", _identity.profile, _identity.manufacturer, _identity.model, _identity.revision)
    for command in _DIAGNOSTIC_COMMANDS + modem.profile.diagnosticCommands:
        try:
            lines = modem.command(command, timeout=15).result()
        except WwanException as e:
            logging.warning("Diagnostic %s failed : %s", command, e)
            if modem.state != ConnectionState.CONNECTED:
                return
            continue
        logging.info("Diagnostic %s : %s", command, ' | '.join(lines[:-1]))


def onModemEvent(event):
    """Callback des événements de wwanlib (thread dédié de la lib)"""
    global _identity
    if isinstance(event, ModemIdentified):
        _identity = event
    elif isinstance(event, StateChanged):
        if event.state == ConnectionState.CONNECTING:
            logging.info("Connecting to the modem...")
        elif event.state == ConnectionState.CONNECTED:
            logging.info("Modem connected")
            runDiagnostic()
        elif event.state == ConnectionState.SEARCHING:
            logging.warning("Modem not registered on the mobile network, searching")
        elif event.state == ConnectionState.RECONNECTING:
            logging.warning("Modem reconnecting %s/%s", event.details.get('attempt'), event.details.get('maxAttempts'))
        elif event.state == ConnectionState.DISCONNECTED:
            logging.error("Modem disconnected for good (%s), stopping the daemon", event.details.get('reason'))
            # Jeedom relancera le démon (gestion automatique)
            _stopEvent.set()
    elif isinstance(event, SignalChanged):
        logging.info("Signal : %s", event.value)
    elif isinstance(event, NetworkChanged):
        logging.info("Network : %s, operator : %s", event.registration, event.operator)


def readSocket(raw):
    message = json.loads(raw.decode("utf-8"))
    if not secrets.compare_digest(str(message.get('apikey', '')).encode(), _apikey.encode()):
        logging.error("Invalid apikey from socket")
        return
    logging.warning("Request from Jeedom ignored: not available yet in this version of the daemon (rewrite in progress)")


def handler(signum=None, frame=None):
    logging.info("Signal %i caught, exiting...", signum)
    _stopEvent.set()


def shutdown():
    logging.info("Shutting down daemon, cleaning up before exit")
    try:
        if modem:
            modem.stop()
    except Exception as e:
        logging.error("Error while stopping the modem : %s", e)
    logging.debug("Removing PID file %s", _pidfile)
    try:
        os.remove(_pidfile)
    except Exception:
        pass
    try:
        if j_socket_instance:
            j_socket_instance.close()
    except Exception:
        pass
    logging.debug("Exit 0")
    sys.stdout.flush()
    os._exit(0)

# ----------------------------------------------------------------------------


_logLevel = "error"
_socketPort = 55115
_socketHost = '127.0.0.1'
_device = None
_pidfile = '/tmp/sms4gd.pid'
_apikey = ''
_callback = ''
_cycle = 30
_cycleComm = 0.5
_serialRate = 9600
_pin = 'None'
_textMode = 'no'
_smsc = 'None'
_force4g = 'no'
_deliveryReport = 'no'
_reconnectBaseDelay = 5.0
_reconnectMaxDelay = 300.0
_reconnectMaxAttempts = 10
_concatPartsTtl = 300.0


parser = argparse.ArgumentParser(description='SMS Daemon for Jeedom plugin')
parser.add_argument("--device", help="Device", type=str)
parser.add_argument("--socketport", help="Socketport for server", type=str)
parser.add_argument("--loglevel", help="Log Level for the daemon", type=str)
parser.add_argument("--callback", help="Callback", type=str)
parser.add_argument("--apikey", help="Apikey", type=str)
parser.add_argument("--cycle", help="Cycle to send event", type=str)
parser.add_argument("--serialrate", help="Serial rate of device", type=str)
parser.add_argument("--pin", help="Pin sim code", type=str)
parser.add_argument("--textmode", help="Force text mode", type=str)
parser.add_argument("--smsc", help="Smsc number", type=str)
parser.add_argument("--force4g", help="Force LTE-only network mode (SimCom modems only)", type=str)
parser.add_argument("--deliveryreport", help="Request SMS delivery status report", type=str)
parser.add_argument("--reconnectbasedelay", help="Base delay (s) before first reconnect attempt", type=str)
parser.add_argument("--reconnectmaxdelay", help="Max delay (s) between reconnect attempts", type=str)
parser.add_argument("--reconnectmaxattempts", help="Max number of reconnect attempts before giving up", type=str)
parser.add_argument("--concatpartsttl", help="Max age (s) of incomplete concatenated SMS parts before they are discarded", type=str)
parser.add_argument("--pid", help="Pid file", type=str)
args = parser.parse_args()

if args.device:
    _device = args.device
if args.socketport:
    _socketPort = int(args.socketport)
if args.loglevel:
    _logLevel = args.loglevel
if args.callback:
    _callback = args.callback
if args.apikey:
    _apikey = args.apikey
if args.cycle:
    _cycle = float(args.cycle)
if args.serialrate:
    _serialRate = int(args.serialrate)
if args.pin:
    _pin = args.pin
if args.textmode:
    _textMode = args.textmode
if args.smsc:
    _smsc = args.smsc
if args.force4g:
    _force4g = args.force4g
if args.deliveryreport:
    _deliveryReport = args.deliveryreport
if args.reconnectbasedelay:
    _reconnectBaseDelay = float(args.reconnectbasedelay)
if args.reconnectmaxdelay:
    _reconnectMaxDelay = float(args.reconnectmaxdelay)
if args.reconnectmaxattempts:
    _reconnectMaxAttempts = int(args.reconnectmaxattempts)
if args.concatpartsttl:
    _concatPartsTtl = float(args.concatpartsttl)
if args.pid:
    _pidfile = args.pid

_socketPort = int(_socketPort)
_cycle = float(_cycle)

jeedom_utils.set_log_level(_logLevel)

_secretFilter = SecretMaskFilter()
for _h in logging.root.handlers:
    _h.addFilter(_secretFilter)

logging.info('Start sms4gd')
logging.info('Log level : %s', _logLevel)
logging.info('Socket port : %s', _socketPort)
logging.info('Socket host : %s', _socketHost)
logging.info('PID file : %s', _pidfile)
logging.info('Device : %s', _device)
logging.info('Callback : %s', _callback)
logging.info('Cycle : %s (not used yet)', _cycle)
logging.info('Serial rate : %s', _serialRate)
logging.info('Pin : %s', '****' if _pin and _pin != 'None' else _pin)
logging.info('Text mode : %s', _textMode)
logging.info('SMSC : %s', _smsc)
logging.info('Force 4G only : %s', _force4g)
logging.info('Delivery report : %s', _deliveryReport)
logging.info('Reconnect base delay : %s', _reconnectBaseDelay)
logging.info('Reconnect max delay : %s', _reconnectMaxDelay)
logging.info('Reconnect max attempts : %s', _reconnectMaxAttempts)
logging.info('Concat parts TTL : %s (not used yet)', _concatPartsTtl)


if _device is None:
    logging.error('No device found')
    shutdown()

signal.signal(signal.SIGINT, handler)
signal.signal(signal.SIGTERM, handler)

try:
    jeedom_utils.write_pid(str(_pidfile))
    j_com_instance = jeedom_com(apikey=_apikey, url=_callback, cycle=_cycleComm)
    if not j_com_instance.test():
        logging.error('Network communication issues. Please fix your Jeedom network configuration.')
        shutdown()
    j_socket_instance = jeedom_socket(port=_socketPort, address=_socketHost)
    j_socket_instance.open()
    logging.info("Daemon started, socket ready to receive commands from Jeedom")
    modem = Modem(
        str(_device), _serialRate,
        pin=None if _pin == 'None' else _pin,
        options=ModemOptions(
            textMode=(_textMode == 'yes'),
            deliveryReport=(_deliveryReport == 'yes'),
            smsc=None if _smsc == 'None' else _smsc,
            force4g=(_force4g == 'yes'),
            reconnectBaseDelay=_reconnectBaseDelay,
            reconnectMaxDelay=_reconnectMaxDelay,
            reconnectMaxAttempts=_reconnectMaxAttempts,
        ),
    )
    modem.onEvent(onModemEvent)
    modem.start()
    while not _stopEvent.is_set():
        try:
            rawMessage = JEEDOM_SOCKET_MESSAGE.get(timeout=1)
        except Empty:
            continue
        try:
            readSocket(rawMessage)
        except Exception as e:
            logging.error("Exception on socket : %s", e)
    shutdown()
except Exception as e:
    logging.error('Fatal error : %s', e)
    logging.debug(traceback.format_exc())
    shutdown()
