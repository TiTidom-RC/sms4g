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

# Configuration of the daemon (command line).

import argparse
from dataclasses import dataclass


@dataclass
class Config:
    """ Settings of the daemon, read from the command line given by ``deamon_start()`` """

    device: str | None = None
    socketPort: int = 55115
    socketHost: str = '127.0.0.1'
    logLevel: str = 'error'
    callback: str = ''
    apikey: str = ''
    cycle: float = 30.0  # seconds between two readings of the signal and of the network
    serialRate: int = 9600
    pin: str | None = None
    smsc: str | None = None
    force4g: bool = False
    deliveryReport: bool = False
    reconnectBaseDelay: float = 5.0
    reconnectMaxDelay: float = 300.0
    reconnectMaxAttempts: int = 10
    concatPartsTtl: float = 300.0
    smsTtl: float = 3600.0  # seconds an SMS waits in the queue before it expires
    diagnostic: bool = False  # AT commands from Jeedom allowed (diagnostic mode)
    messagePause: float = 0.0  # seconds between the end of an SMS and the start of the next one
    replyDelay: float = 0.0  # seconds after an SMS was received before an SMS may be sent
    selfTest: float = 0.0  # hours between two self-tests (the modem sends an SMS to its own SIM), 0 = none
    ownNumber: str | None = None  # number of the SIM, for the self-test
    autoRestart: bool = False  # restart the modem when the self-test fails
    pidFile: str = '/tmp/sms4gd.pid'

    MIN_SELF_TEST_HOURS = 1.0  # a test costs an SMS: a lower value is raised to this one

    @property
    def selfTestInterval(self) -> float:
        """ Seconds between two self-tests (0 = none) """
        return max(self.selfTest, self.MIN_SELF_TEST_HOURS) * 3600.0 if self.selfTest > 0 else 0.0

    @classmethod
    def fromArgs(cls, argv: list[str] | None = None) -> 'Config':
        parser = argparse.ArgumentParser(description='SMS Daemon for Jeedom plugin')
        parser.add_argument("--device", help="Device", type=str)
        parser.add_argument("--socketport", help="Socketport for server", type=str)
        parser.add_argument("--loglevel", help="Log Level for the daemon", type=str)
        parser.add_argument("--callback", help="Callback", type=str)
        parser.add_argument("--apikey", help="Apikey", type=str)
        parser.add_argument("--cycle", help="Seconds between two readings of the signal and of the network", type=str)
        parser.add_argument("--serialrate", help="Serial rate of device", type=str)
        parser.add_argument("--pin", help="Pin sim code", type=str)
        parser.add_argument("--smsc", help="Smsc number", type=str)
        parser.add_argument("--force4g", help="Force LTE-only network mode (SimCom modems only)", type=str)
        parser.add_argument("--deliveryreport", help="Request SMS delivery status report", type=str)
        parser.add_argument("--reconnectbasedelay", help="Base delay (s) before first reconnect attempt", type=str)
        parser.add_argument("--reconnectmaxdelay", help="Max delay (s) between reconnect attempts", type=str)
        parser.add_argument("--reconnectmaxattempts", help="Max number of reconnect attempts before giving up", type=str)
        parser.add_argument("--concatpartsttl", help="Max age (s) of incomplete concatenated SMS parts before they are discarded", type=str)
        parser.add_argument("--smsttl", help="Seconds an SMS waits in the queue before it expires", type=str)
        parser.add_argument("--diagnostic", help="Allow the AT commands sent from Jeedom (yes / no)", type=str)
        parser.add_argument("--messagepause", help="Seconds between the end of an SMS and the start of the next one", type=str)
        parser.add_argument("--replydelay", help="Seconds after a received SMS before an SMS may be sent", type=str)
        parser.add_argument("--selftest", help="Hours between two self-tests of the SMS service (0 = none)", type=str)
        parser.add_argument("--ownnumber", help="Number of the SIM, for the self-test", type=str)
        parser.add_argument("--autorestart", help="Restart the modem when the self-test fails (yes / no)", type=str)
        parser.add_argument("--pid", help="Pid file", type=str)
        args = parser.parse_args(argv)

        config = cls()
        if args.device:
            config.device = args.device
        if args.socketport:
            config.socketPort = int(args.socketport)
        if args.loglevel:
            config.logLevel = args.loglevel
        if args.callback:
            config.callback = args.callback
        if args.apikey:
            config.apikey = args.apikey
        if args.cycle:
            config.cycle = float(args.cycle)
        if args.serialrate:
            config.serialRate = int(args.serialrate)
        if args.pin and args.pin != 'None':
            config.pin = args.pin
        if args.smsc and args.smsc != 'None':
            config.smsc = args.smsc
        if args.force4g:
            config.force4g = args.force4g == 'yes'
        if args.deliveryreport:
            config.deliveryReport = args.deliveryreport == 'yes'
        if args.reconnectbasedelay:
            config.reconnectBaseDelay = float(args.reconnectbasedelay)
        if args.reconnectmaxdelay:
            config.reconnectMaxDelay = float(args.reconnectmaxdelay)
        if args.reconnectmaxattempts:
            config.reconnectMaxAttempts = int(args.reconnectmaxattempts)
        if args.concatpartsttl:
            config.concatPartsTtl = float(args.concatpartsttl)
        if args.smsttl:
            config.smsTtl = float(args.smsttl)
        if args.diagnostic:
            config.diagnostic = args.diagnostic == 'yes'
        if args.messagepause:
            config.messagePause = max(0.0, float(args.messagepause))
        if args.replydelay:
            config.replyDelay = max(0.0, float(args.replydelay))
        if args.selftest:
            config.selfTest = max(0.0, float(args.selftest))
        if args.ownnumber and args.ownnumber != 'None':
            config.ownNumber = args.ownnumber
        if args.autorestart:
            config.autoRestart = args.autorestart == 'yes'
        if args.pid:
            config.pidFile = args.pid
        return config
