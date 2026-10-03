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
#
#   Jeedom (PHP) --socket--> Dispatcher --> wwanlib.Modem --events--> jeedom_publisher --HTTP--> jeesms4g.php
#
# Milestone J2: connection state, signal and network are published to Jeedom, and the AT commands sent from Jeedom
# are run in diagnostic mode. Sending, receiving and the delivery reports come back with J3 to J5.

import logging
import os
import re
import signal
import sys
import threading
import traceback
from typing import Optional

from dispatcher import Dispatcher
from utils import Config
from wwanlib import (ConnectionState, Modem, ModemIdentified, ModemOptions, NetworkChanged, Registration, SignalChanged,
                     StateChanged, WwanException)

try:
    from jeedom.jeedom import jeedom_publisher, jeedom_socket, jeedom_utils, JEEDOM_SOCKET_MESSAGE
except ImportError as e:
    print("Error: importing module from jeedom folder: %s", e)
    sys.exit(1)

# PARAMETERS #

config: Config
modem: Optional[Modem] = None
publisher: Optional[jeedom_publisher] = None
dispatcher: Optional[Dispatcher] = None
socketServer: Optional[jeedom_socket] = None
_stopEvent = threading.Event()
_finalStatePublished = False  # la lib a déjà publié disconnected (avec sa cause) : ne pas la remplacer à l'arrêt
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
            if modem.state not in (ConnectionState.CONNECTED, ConnectionState.SEARCHING):
                return
            continue
        logging.info("Diagnostic %s : %s", command, ' | '.join(lines[:-1]))


def publishState(state: str, **details):
    """Publie l'état de connexion (regroupé : seul le dernier part si Jeedom est lent)"""
    if publisher:
        publisher.state('modemState', {'type': 'modemState', 'state': state, **details})


def onModemEvent(event):
    """Callback des événements de wwanlib (thread dédié de la lib) : publie vers Jeedom et journalise"""
    global _identity, _finalStatePublished
    if isinstance(event, ModemIdentified):
        _identity = event
    elif isinstance(event, StateChanged):
        publishState(event.state, **event.details)
        if event.state == ConnectionState.CONNECTING:
            logging.info("Connecting to the modem...")
        elif event.state == ConnectionState.CONNECTED:
            logging.info("Modem connected")
            runDiagnostic()
        elif event.state == ConnectionState.SEARCHING:
            logging.warning("Modem not registered on the mobile network, searching")
        elif event.state == ConnectionState.DISCONNECTED:
            _finalStatePublished = True
            logging.error("Modem disconnected for good (%s), stopping the daemon", event.details.get('reason'))
            # Jeedom relancera le démon (gestion automatique)
            _stopEvent.set()
    elif isinstance(event, SignalChanged):
        logging.info("Signal : %s", event.value)
        if publisher:
            publisher.state('signal', {'type': 'signal', 'value': event.value})
    elif isinstance(event, NetworkChanged):
        logging.info("Network : %s, operator : %s", event.registration, event.operator)
        if publisher:
            publisher.state('network', {'type': 'network', 'registration': event.registration, 'operator': event.operator})


def handler(signum=None, frame=None):
    logging.info("Signal %i caught, exiting...", signum)
    _stopEvent.set()


def shutdown():
    logging.info("Shutting down daemon, cleaning up before exit")
    try:
        if dispatcher:
            dispatcher.stop()
        if modem:
            modem.stop()
    except Exception as e:
        logging.error("Error while stopping the modem : %s", e)
    if publisher:
        # Le modem est arrêté : plus aucun événement de la lib ne peut repasser devant ces états finaux
        if not _finalStatePublished:
            publishState(ConnectionState.DISCONNECTED, reason='daemon stopped', fatal=False)
        publisher.state('signal', {'type': 'signal', 'value': -1})
        publisher.state('network', {'type': 'network', 'registration': Registration.UNKNOWN, 'operator': None})
        publisher.flush(2.0)
        publisher.stop()
    logging.debug("Removing PID file %s", config.pidFile)
    try:
        os.remove(config.pidFile)
    except Exception:
        pass
    try:
        if socketServer:
            socketServer.close()
    except Exception:
        pass
    logging.debug("Exit 0")
    sys.stdout.flush()
    os._exit(0)

# ----------------------------------------------------------------------------


def main():
    global config, modem, publisher, dispatcher, socketServer

    config = Config.fromArgs()
    jeedom_utils.set_log_level(config.logLevel)

    secretFilter = SecretMaskFilter()
    for logHandler in logging.root.handlers:
        logHandler.addFilter(secretFilter)

    logging.info('Start sms4gd')
    logging.info('Log level : %s', config.logLevel)
    logging.info('Socket port : %s', config.socketPort)
    logging.info('Socket host : %s', config.socketHost)
    logging.info('PID file : %s', config.pidFile)
    logging.info('Device : %s', config.device)
    logging.info('Callback : %s', config.callback)
    logging.info('Cycle (signal and network) : %s', config.cycle)
    logging.info('Serial rate : %s', config.serialRate)
    logging.info('Pin : %s', '****' if config.pin else None)
    logging.info('Text mode : %s', config.textMode)
    logging.info('SMSC : %s', config.smsc)
    logging.info('Force 4G only : %s', config.force4g)
    logging.info('Delivery report : %s', config.deliveryReport)
    logging.info('Reconnect base delay : %s', config.reconnectBaseDelay)
    logging.info('Reconnect max delay : %s', config.reconnectMaxDelay)
    logging.info('Reconnect max attempts : %s', config.reconnectMaxAttempts)
    logging.info('Concat parts TTL : %s (not used yet)', config.concatPartsTtl)
    logging.info('Diagnostic mode (AT commands from Jeedom) : %s', config.diagnostic)

    if config.device is None:
        logging.error('No device found')
        shutdown()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    try:
        jeedom_utils.write_pid(str(config.pidFile))
        candidate = jeedom_publisher(config.callback, config.apikey)
        if not candidate.test():
            logging.error('Network communication issues. Please fix your Jeedom network configuration.')
            candidate.stop()
            shutdown()
        publisher = candidate
        publisher.start()
        socketServer = jeedom_socket(port=config.socketPort, address=config.socketHost)
        socketServer.open()
        logging.info("Daemon started, socket ready to receive commands from Jeedom")
        modem = Modem(
            str(config.device), config.serialRate, pin=config.pin,
            options=ModemOptions(
                textMode=config.textMode,
                deliveryReport=config.deliveryReport,
                smsc=config.smsc,
                force4g=config.force4g,
                reconnectBaseDelay=config.reconnectBaseDelay,
                reconnectMaxDelay=config.reconnectMaxDelay,
                reconnectMaxAttempts=config.reconnectMaxAttempts,
                monitorInterval=config.cycle,
            ),
        )
        dispatcher = Dispatcher(JEEDOM_SOCKET_MESSAGE, modem, publisher, config.apikey, config.diagnostic)
        dispatcher.start()
        modem.onEvent(onModemEvent)
        modem.start()
        # Attente par tranches : un signal est traité à la tranche suivante au plus tard, aussi sous Windows
        while not _stopEvent.wait(0.5):
            pass
    except Exception as e:
        logging.error('Fatal error : %s', e)
        logging.debug(traceback.format_exc())
    shutdown()


if __name__ == '__main__':
    main()
