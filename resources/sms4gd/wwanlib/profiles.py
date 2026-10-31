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
    # Tried in order: the first one the modem accepts is used
    lteOnlyCommands: tuple[str, ...]
    autoCommands: tuple[str, ...]


@dataclass(frozen=True)
class Profile:
    name: str
    # AT+CNMI parameter sets tried in order until the modem accepts one. <mt> is always 1
    # (+CMTI store-and-notify), the only incoming SMS notification format parsed from J4 on.
    # When the SR memory has no slot, the <ds>=2 sets (reports kept in the SR memory) are tried last.
    cnmiCandidates: tuple[str, ...]
    # Line prefixes that are always spontaneous notifications for this manufacturer
    urcPrefixes: tuple[str, ...] = ()
    # LTE only / automatic, None when the modem cannot be driven this way
    networkMode: NetworkMode | None = None
    # Commands that restart the modem, strongest first: the first one the modem accepts is used.
    # AT+CFUN=1,1 (3GPP) restarts the radio stack only on some modems (SimCom: it does not clear the delivery-report bug).
    restartCommands: tuple[str, ...] = ('AT+CFUN=1,1',)
    # Commands the daemon runs for the diagnostic logged at each connection
    diagnosticCommands: tuple[str, ...] = ()
    # ESSAI : selects the SMS service, before the other SMS settings
    smsServiceCommand: str | None = None


# Mode 2 (notifications kept while the port is busy), then mode 1 (dropped meanwhile) for a firmware that refuses mode 2.
# Mode 0 is left out: the modem accepts it but may keep every notification in its buffer.
# <ds>=1 (+CDS straight from the network, syntax validated on the Quectel BG96) after <ds>=2 (reports kept in the SR memory)
GENERIC = Profile(name='generic', cnmiCandidates=('2,1,0,2', '2,1,0,1', '1,1,0,2', '1,1,0,1'))

# SimCom (SIM7600...): <ds>=1 (direct +CDS) avoids the SR-memory read bug of some firmwares but may be
# rejected for a given <mode>,<mt>,<bm> combination, hence the cascade down to the <ds>=2 known to work.
# The last set (4 parameters, validated on the BG96) was never tried on a SimCom: the 5-parameter form is refused.
SIMCOM = Profile(
    name='simcom',
    cnmiCandidates=('1,1,0,1', '0,1,0,1', '2,1,2,1', '2,1,0,2', '2,1,0,1'),
    networkMode=NetworkMode('AT+CNMP?', re.compile(r'^\+CNMP: 38\b'), ('AT+CNMP=38',), ('AT+CNMP=2',)),
    restartCommands=('AT+CRESET', 'AT+CFUN=1,1'),
    diagnosticCommands=('AT+CPSI?',),
    # smsServiceCommand='AT+CSMS=1',  # ESSAI : désactivé pour tester CSMS=0 avec AT+CNMA après chaque +CDS
)

# E3372 in "stick" (AT) mode. ^SYSCFGEX=<acqorder>,<band>,<roam>,<srvdomain>,<lteband>,<reserve1>,<reserve2>: "03" = LTE only,
# "00" = automatic. 40000000,2,4,40000000 leaves the bands, the roaming and the domain as they are; explicit bands come second
# for a firmware that refuses 40000000 (not verified on a modem yet). The E3372 answers ^SYSCFGEX:"00" without a space.
HUAWEI = Profile(
    name='huawei',
    cnmiCandidates=('2,1,0,2', '2,1,0,1'),
    urcPrefixes=('^RSSI', '^BOOT', '^MODE', '^HCSQ', '^SIMST', '^SRVST', '^STIN', '^DSFLOWRPT'),
    networkMode=NetworkMode(
        'AT^SYSCFGEX?', re.compile(r'^\^SYSCFGEX:\s*"03"'),
        ('AT^SYSCFGEX="03",40000000,2,4,40000000,,', 'AT^SYSCFGEX="03",3FFFFFFF,2,4,7FFFFFFFFFFFFFFF,,'),
        ('AT^SYSCFGEX="00",40000000,2,4,40000000,,', 'AT^SYSCFGEX="00",3FFFFFFF,2,4,7FFFFFFFFFFFFFFF,,')),
    restartCommands=('AT^RESET', 'AT+CFUN=1,1'),
    diagnosticCommands=('AT^SYSCFGEX?', 'AT^SYSINFOEX', 'AT^GETPORTMODE'),
)

# AT+QCFG="nwscanmode",<mode>,<effect>: mode 3 = LTE only, 0 = automatic; effect 1 = without restarting the modem.
# <ds>=1 first: the BG96 (SR memory 0/0) keeps the <ds>=2 delivery reports back, and the SR memory depends on the SIM.
QUECTEL = Profile(
    name='quectel',
    cnmiCandidates=('2,1,0,1', '2,1,0,2'),
    urcPrefixes=('+QIND:',),
    networkMode=NetworkMode(
        'AT+QCFG="nwscanmode"', re.compile(r'^\+QCFG: "nwscanmode",3\b'),
        ('AT+QCFG="nwscanmode",3,1',), ('AT+QCFG="nwscanmode",0,1',)),
    restartCommands=('AT+CFUN=1,1',),
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
