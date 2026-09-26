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

import logging
import sys
import os
import time
import argparse
import signal
import traceback
import json
from itertools import count
from typing import Optional
from queue import Empty
from gsmmodem.exceptions import TimeoutException
from gsmmodem.modem import GsmModem, StatusReport

try:
    from jeedom.jeedom import jeedom_com, jeedom_socket, jeedom_utils, JEEDOM_SOCKET_MESSAGE

    # Type hints for global instances (initialized later)
    j_com_instance: Optional[jeedom_com] = None
    j_socket_instance: Optional[jeedom_socket] = None

except ImportError as e:
    print("Error: importing module from jeedom folder: %s", e)
    sys.exit(1)

# PARAMETERS #

gsm: Optional[GsmModem] = None
_smsSeq = count()  # compteur thread-safe (CPython) pour différencier les clés du buffer devices:: entre 2 flush


def handleSms(sms):
    logging.info("Got SMS message : %s", sms)
    if not sms.text:
        logging.debug("No text so nothing to do")
        return
    message = sms.text.replace('"', '')
    # message = jeedom_utils.remove_accents(sms.text.replace('"', ''))
    if j_com_instance:
        # Clé unique par message (pas juste le numéro) : sinon 2 SMS du même expéditeur avant le prochain
        # flush s'écrasent dans le buffer (merge_dict remplace la valeur précédente sur la même clé)
        j_com_instance.add_changes(f'devices::{sms.number}#{next(_smsSeq)}', {'number': sms.number, 'message': message})


def handleStatusReport(report):
    status = 'delivered' if report.deliveryStatus == StatusReport.DELIVERED else 'failed'
    logging.info("Delivery report for %s : %s (ref %s)", report.number, status, report.reference)
    if j_com_instance:
        j_com_instance.send_change_immediate({'number': 'deliveryReport', 'destination': report.number, 'status': status, 'reference': report.reference})


def _backoffDelay(attempt):
    return min(_reconnect_base_delay * (2 ** (attempt - 1)), _reconnect_max_delay)


def _isTransientNetworkError(e):
    return isinstance(e, TimeoutException) or str(e) in ('Device not searching for network operator', 'Timeout')


_modem_status = None
_modem_status_extra = None


def _setModemStatus(status, **kwargs):
    """Push the modem connection status to Jeedom (deduplicated on status+details, PHP does the display translation)"""
    global _modem_status, _modem_status_extra
    if status == _modem_status and kwargs == _modem_status_extra:
        return
    _modem_status = status
    _modem_status_extra = kwargs
    if j_com_instance:
        change = {'number': 'modemStatus', 'status': status}
        change.update(kwargs)
        j_com_instance.send_change_immediate(change)


def _createAndConnectModem():
    if _device is None:
        raise ValueError('Device not found')
    modem = GsmModem(
        _device, int(_serial_rate),
        smsReceivedCallbackFunc=handleSms,
        smsStatusReportCallback=handleStatusReport,
        requestDelivery=(_delivery_report == 'yes'),
    )
    try:
        logging.debug("Text mode %s", _text_mode == 'yes')
        modem.smsTextMode = (_text_mode == 'yes')
        if _pin != 'None':
            logging.debug("Enter pin code : %s ", _pin)
            modem.connect(_pin, 5)
        else:
            modem.connect(None, 5)
        if _force_4g == 'yes' and modem.isSimComModem:
            try:
                modem.write('AT+CNMP=38')
                logging.info("Forced LTE-only network mode (AT+CNMP=38)")
            except Exception as e:
                logging.error("Failed to force LTE-only network mode (AT+CNMP=38) : %s", e)
        if _smsc != 'None':
            logging.debug("Configure smsc : %s", _smsc)
            modem.write(f'AT+CSCA="{_smsc}"')
        logging.debug("Waiting for network...")
        modem.waitForNetworkCoverage(timeout=_cycle)
        logging.info("Network coverage acquired")
        if modem.isSimComModem:
            try:
                # First field of the response is the actual RAT in use (LTE/WCDMA/GSM/NO SERVICE...) -
                # useful to spot a fallback to 2G/3G when force_4g is set, or just to see the current mode otherwise
                cpsi = modem.write('AT+CPSI?')
                logging.info("Network system info (AT+CPSI?) : %s", cpsi)
            except Exception as e:
                logging.error("Failed to query network system info (AT+CPSI?) : %s", e)
        try:
            if j_com_instance:
                j_com_instance.send_change_immediate({'number': 'networkName', 'message': str(modem.networkName)})
        except Exception as e:
            logging.error("Exception during send_change_immediate: %s", e)
        for mem in ('ME', 'SM'):
            try:
                modem.write(f'AT+CPMS="{mem}","{mem}","{mem}"')
                modem.write('AT+CMGD=1,4')
            except Exception as e:
                logging.error("Exception clearing '%s' storage: %s", mem, e)
        return modem
    except Exception:
        # Otherwise a failed attempt leaves its read thread and serial port open, contending with the
        # next attempt's connection over the same device and corrupting its I/O (garbled/duplicated bytes)
        try:
            modem.close()
        except Exception:
            pass
        raise


def _catchUpStoredSms(modem):
    # Appel unique post-connexion (pas a chaque cycle) : la reception temps reel passe par +CMTI,
    # repeter ce polling en continu ferait courir une race avec lui (meme SMS livre deux fois si
    # +CMTI le traite pendant que ce polling le voit encore comme non lu)
    try:
        modem.processStoredSms(True)
    except Exception as e:
        logging.error("Failed to process stored SMS after connect : %s", e)


def _reconnectLoop():
    global gsm
    try:
        if gsm:
            gsm.close()
    except Exception:
        pass
    # No modem instance to query while reconnecting - signalStrength convention (gsmmodem): -1 = unknown
    if j_com_instance:
        j_com_instance.send_change_immediate({'number': 'signalStrength', 'message': '-1'})
    attempt = 0
    while attempt < _reconnect_max_attempts:
        attempt += 1
        delay = _backoffDelay(attempt)
        _setModemStatus('reconnecting', attempt=attempt, max_attempts=_reconnect_max_attempts)
        logging.warning("Attempting modem reconnection %d/%d in %.0fs", attempt, _reconnect_max_attempts, delay)
        time.sleep(delay)
        try:
            gsm = _createAndConnectModem()
            logging.info("Modem reconnection successful after %d attempt(s)", attempt)
            _setModemStatus('connected')
            _catchUpStoredSms(gsm)
            return True
        except Exception as e:
            logging.error("Reconnection attempt %d/%d failed : %s", attempt, _reconnect_max_attempts, e)
    logging.error("Maximum number of reconnection attempts reached (%d), giving up", _reconnect_max_attempts)
    _setModemStatus('disconnected')
    return False


def listen():
    global gsm
    if j_socket_instance:
        j_socket_instance.open()
    logging.info("Daemon started, socket ready to receive commands from Jeedom")
    logging.info("Connecting to GSM Modem...")
    _setModemStatus('connecting')
    try:
        gsm = _createAndConnectModem()
        _setModemStatus('connected')
        _catchUpStoredSms(gsm)
    except Exception as e:
        logging.error("Unexpected error while starting to listen (%s): %s", type(e).__name__, e)
        if j_com_instance:
            j_com_instance.send_change_immediate({'number': 'none', 'message': str(e)})
        logging.error("Initial connection failed, entering reconnection loop")
        if not _reconnectLoop():
            shutdown()
            return
    consecutive_network_failures = 0
    try:
        while 1:
            sleep_duration = _cycle
            if gsm and j_com_instance:
                try:
                    ss = gsm.signalStrength
                except Exception as e:
                    logging.debug("Failed to read signal strength : %s", e)
                    ss = -1
                j_com_instance.send_change_immediate({'number': 'signalStrength', 'message': str(ss)})
            try:
                if gsm:
                    gsm.waitForNetworkCoverage(timeout=_cycle)
                    consecutive_network_failures = 0
                    _setModemStatus('connected')
                    gsm.purgeStaleSmsParts(_concat_parts_ttl)
            except Exception as e:
                if _isTransientNetworkError(e):
                    consecutive_network_failures += 1
                    sleep_duration = _backoffDelay(consecutive_network_failures)
                    _setModemStatus('searching')
                    logging.warning("Temporary network loss (%s), rechecking in %.0fs (attempt %d)", e, sleep_duration, consecutive_network_failures)
                else:
                    logging.error("Exception on GSM : %s", e)
                    logging.error("Modem connection lost, attempting reconnection...")
                    if not _reconnectLoop():
                        shutdown()
                        return
                    consecutive_network_failures = 0
            try:
                read_socket()
            except Exception as e:
                logging.error("Exception on socket : %s", e)
            # Attente interruptible : un SMS sortant remis dans la queue reveille la boucle immédiatement
            # au lieu d'attendre la fin du cycle (read_socket() le traitera au prochain tour)
            try:
                pending_message = JEEDOM_SOCKET_MESSAGE.get(timeout=sleep_duration)
                JEEDOM_SOCKET_MESSAGE.put(pending_message)
            except Empty:
                pass
    except KeyboardInterrupt:
        shutdown()


def read_socket():
    if not JEEDOM_SOCKET_MESSAGE.empty():
        logging.debug("Message received from Jeedom socket")
        message = json.loads(JEEDOM_SOCKET_MESSAGE.get().decode("utf-8"))
        if message['apikey'] != _apikey:
            logging.error("Invalid apikey from socket (number=%s)", message.get('number'))
            return
        if gsm:
            try:
                gsm.waitForNetworkCoverage(timeout=_cycle)
                logging.info("Sending message to %s: %s", message['number'], message['message'])
                gsm.sendSms(message['number'], message['message'])
            except Exception as e:
                logging.error("Failed to send SMS to %s : %s", message['number'], e)
                if j_com_instance:
                    j_com_instance.send_change_immediate({'number': 'deliveryReport', 'destination': message['number'], 'status': 'failed'})


def handler(signum=None, frame=None):
    logging.info("Signal %i caught, exiting...", signum)
    shutdown()


def shutdown():
    logging.info("Shutting down daemon, cleaning up before exit")
    # Envoi synchrone : garantit que Jeedom reflète bien l'état déconnecté avant la fin du process
    if j_com_instance:
        j_com_instance.send_change_sync({'number': 'modemStatus', 'status': 'disconnected'})
        j_com_instance.send_change_sync({'number': 'signalStrength', 'message': '-1'})
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


_log_level = "error"
_socket_port = 55115
_socket_host = '127.0.0.1'
_device = None
_pidfile = '/tmp/sms4gd.pid'
_apikey = ''
_callback = ''
_cycle = 30
_cycleComm = 0.5  # cycle du buffer add_changes (jeedom_com), indépendant du cycle de scrutation principal
_serial_rate = 9600
_pin = 'None'
_text_mode = 'no'
_smsc = 'None'
_force_4g = 'no'
_delivery_report = 'no'
_reconnect_base_delay = 5.0
_reconnect_max_delay = 300.0
_reconnect_max_attempts = 10
_concat_parts_ttl = 300.0


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
    _socket_port = int(args.socketport)
if args.loglevel:
    _log_level = args.loglevel
if args.callback:
    _callback = args.callback
if args.apikey:
    _apikey = args.apikey
if args.cycle:
    _cycle = float(args.cycle)
if args.serialrate:
    _serial_rate = int(args.serialrate)
if args.pin:
    _pin = args.pin
if args.textmode:
    _text_mode = args.textmode
if args.smsc:
    _smsc = args.smsc
if args.force4g:
    _force_4g = args.force4g
if args.deliveryreport:
    _delivery_report = args.deliveryreport
if args.reconnectbasedelay:
    _reconnect_base_delay = float(args.reconnectbasedelay)
if args.reconnectmaxdelay:
    _reconnect_max_delay = float(args.reconnectmaxdelay)
if args.reconnectmaxattempts:
    _reconnect_max_attempts = int(args.reconnectmaxattempts)
if args.concatpartsttl:
    _concat_parts_ttl = float(args.concatpartsttl)
if args.pid:
    _pidfile = args.pid

_socket_port = int(_socket_port)
_cycle = float(_cycle)

jeedom_utils.set_log_level(_log_level)

logging.info('Start sms4gd')
logging.info('Log level : %s', _log_level)
logging.info('Socket port : %s', _socket_port)
logging.info('Socket host : %s', _socket_host)
logging.info('PID file : %s', _pidfile)
logging.info('Device : %s', _device)
logging.info('Callback : %s', _callback)
logging.info('Cycle : %s', _cycle)
logging.info('Serial rate : %s', _serial_rate)
logging.info('Pin : %s', '***' if _pin and _pin != 'None' else _pin)
logging.info('Text mode : %s', _text_mode)
logging.info('SMSC : %s', _smsc)
logging.info('Force 4G only : %s', _force_4g)
logging.info('Delivery report : %s', _delivery_report)
logging.info('Reconnect base delay : %s', _reconnect_base_delay)
logging.info('Reconnect max delay : %s', _reconnect_max_delay)
logging.info('Reconnect max attempts : %s', _reconnect_max_attempts)
logging.info('Concat parts TTL : %s', _concat_parts_ttl)


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
    j_socket_instance = jeedom_socket(port=_socket_port, address=_socket_host)
    listen()
except Exception as e:
    logging.error('Fatal error : %s', e)
    logging.debug(traceback.format_exc())
    shutdown()
