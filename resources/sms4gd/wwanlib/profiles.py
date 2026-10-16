""" Manufacturer-specific behaviors (generic, SimCom, Huawei, Quectel).

A profile is plain data: the Modem picks it from the manufacturer name returned by ``AT+CGMI``
and the other modules read it. Voice / USSD / call-related settings of the former gsmmodem are
intentionally not carried over (see the documentation, reference E).
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class NetworkMode:
    """ How a modem is read and set to "LTE only" and back to automatic (it may not survive a restart of the modem) """
    query: str  # command that reads the current mode
    lteOnly: re.Pattern[str]  # matches a line of the answer when the mode is LTE only
    lteOnlyCommand: str
    autoCommand: str


@dataclass(frozen=True)
class Profile:
    name: str
    # AT+CNMI parameter sets tried in order until the modem accepts one. <mt> is always 1
    # (+CMTI store-and-notify), the only incoming SMS notification format parsed from J4 on.
    cnmiCandidates: tuple[str, ...]
    # Line prefixes that are always spontaneous notifications for this manufacturer
    urcPrefixes: tuple[str, ...] = ()
    # LTE only / automatic, None when the modem cannot be driven this way
    networkMode: NetworkMode | None = None
    # Commands the daemon runs for the diagnostic logged at each connection
    diagnosticCommands: tuple[str, ...] = ()


# AT+CNMI with <ds>=1: the delivery reports come straight from the network (+CDS), not through the SR memory.
# Tried by the Modem after the candidates of the profile, or before the <ds>=2 ones when the SR memory has no slot.
DIRECT_REPORTS = '2,1,0,1,0'

GENERIC = Profile(name='generic', cnmiCandidates=('2,1,0,2',))

# SimCom (SIM7600...): <ds>=1 (direct +CDS) avoids the SR-memory read bug of some firmwares but may be
# rejected for a given <mode>,<mt>,<bm> combination, hence the cascade down to the <ds>=2 known to work.
SIMCOM = Profile(
    name='simcom',
    cnmiCandidates=('1,1,0,1', '0,1,0,1', '2,1,2,1', '2,1,0,2'),
    networkMode=NetworkMode('AT+CNMP?', re.compile(r'^\+CNMP: 38\b'), 'AT+CNMP=38', 'AT+CNMP=2'),
    diagnosticCommands=('AT+CPSI?',),
)

# E3372 in "stick" (AT) mode. ^SYSCFGEX=<acqorder>,<band>,<roam>,<srvdomain>,<lteband>: "03" = LTE only, "00" = automatic.
HUAWEI = Profile(
    name='huawei',
    cnmiCandidates=('2,1,0,2',),
    urcPrefixes=('^RSSI', '^BOOT', '^MODE', '^HCSQ', '^SIMST', '^SRVST', '^STIN', '^DSFLOWRPT'),
    networkMode=NetworkMode(
        'AT^SYSCFGEX?', re.compile(r'^\^SYSCFGEX: "03"'),
        'AT^SYSCFGEX="03",3FFFFFFF,1,2,7FFFFFFFFFFFFFFF', 'AT^SYSCFGEX="00",3FFFFFFF,1,2,7FFFFFFFFFFFFFFF'),
    diagnosticCommands=('AT^SYSCFGEX?', 'AT^SYSINFOEX', 'AT^GETPORTMODE'),
)

# AT+QCFG="nwscanmode",<mode>,<effect>: mode 3 = LTE only, 0 = automatic; effect 1 = without restarting the modem.
QUECTEL = Profile(
    name='quectel',
    cnmiCandidates=('2,1,0,2',),
    urcPrefixes=('+QIND:',),
    networkMode=NetworkMode(
        'AT+QCFG="nwscanmode"', re.compile(r'^\+QCFG: "nwscanmode",3\b'),
        'AT+QCFG="nwscanmode",3,1', 'AT+QCFG="nwscanmode",0,1'),
    diagnosticCommands=('AT+QNWINFO', 'AT+QCSQ', 'AT+QCFG="nwscanmode"', 'AT+QINISTAT', 'AT+QURCCFG="urcport"',
                        'AT+QCFG="servicedomain"'),
)


def detectProfile(manufacturer: str) -> Profile:
    """ Picks the profile from the manufacturer name returned by AT+CGMI """
    name = manufacturer.lower()
    if 'simcom' in name:
        return SIMCOM
    if 'huawei' in name:
        return HUAWEI
    if 'quectel' in name:
        return QUECTEL
    return GENERIC
