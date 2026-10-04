""" Manufacturer-specific behaviours (generic, SimCom, Huawei).

A profile is plain data: the Modem picks it from the manufacturer name returned by ``AT+CGMI``
and the other modules read it. Voice / USSD / call-related settings of the former gsmmodem are
intentionally not carried over (see the documentation, reference E).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    name: str
    # AT+CNMI parameter sets tried in order until the modem accepts one. <mt> is always 1
    # (+CMTI store-and-notify), the only incoming SMS notification format parsed from J4 on.
    cnmiCandidates: tuple[str, ...]
    # Line prefixes that are always spontaneous notifications for this manufacturer
    urcPrefixes: tuple[str, ...] = ()
    # Whether AT+CNMP=38 (LTE only) is available
    supportsForce4g: bool = False
    # Commands the daemon runs for the diagnostic logged at each connection
    diagnosticCommands: tuple[str, ...] = ()


GENERIC = Profile(name='generic', cnmiCandidates=('2,1,0,2',))

# SimCom (SIM7600...): <ds>=1 (direct +CDS) avoids the SR-memory read bug of some firmwares but may be
# rejected for a given <mode>,<mt>,<bm> combination, hence the cascade down to the <ds>=2 known to work.
SIMCOM = Profile(
    name='simcom',
    cnmiCandidates=('1,1,0,1', '0,1,0,1', '2,1,2,1', '2,1,0,2'),
    supportsForce4g=True,
    diagnosticCommands=('AT+CPSI?',),
)

# The former code had no SMS-specific Huawei behaviour besides the ^ notifications and the
# "COMMAND NOT SUPPORT" end code (recognized for every profile). Not testable at the moment.
HUAWEI = Profile(
    name='huawei',
    cnmiCandidates=('2,1,0,2',),
    urcPrefixes=('^RSSI', '^BOOT', '^MODE', '^HCSQ', '^SIMST', '^SRVST', '^STIN', '^DSFLOWRPT'),
)


def detectProfile(manufacturer: str) -> Profile:
    """ Picks the profile from the manufacturer name returned by AT+CGMI """
    name = manufacturer.lower()
    if 'simcom' in name:
        return SIMCOM
    if 'huawei' in name:
        return HUAWEI
    return GENERIC
