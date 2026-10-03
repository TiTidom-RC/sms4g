""" Filter of the AT commands that may be sent through the diagnostic console (J2, DEC-28).

Everything that is not explicitly allowed is refused. The command that is checked is normalized (upper case,
spaces removed outside quotes) and it is that normalized form which must be sent: what was checked is what
the modem receives.
"""

import re
from typing import NamedTuple

MAX_LENGTH = 128


class AtVerdict(NamedTuple):
    allowed: bool
    reason: str  # why it was refused ('' when allowed)
    command: str  # normalized command ('' when it could not be normalized)


# Commands refused whatever their form (they send or delete SMS, lock the SIM, restart the modem)
_FORBIDDEN_ANY = {
    'CMGS': 'sending SMS is not done from the console',
    'CMGW': 'writing SMS is not done from the console',
    'CMSS': 'sending SMS is not done from the console',
    'CMGD': 'deleting SMS is not done from the console',
    'CLCK': 'locking facilities (SIM, network) is forbidden',
    'CPWD': 'changing passwords is forbidden',
    'CRESET': 'restarting the modem is not available from the console',
}

# Commands whose settings are managed by the library: reading is allowed, writing is not
_FORBIDDEN_WRITE = {
    'CMGF': 'the SMS mode is managed by the library',
    'CMEE': 'the error reporting is managed by the library',
    'CPIN': 'entering a PIN is forbidden',
    'CPMS': 'the SMS memory selection is managed by the library',
    'CSCA': 'the SMS center is managed by the library',
    'CSMP': 'the SMS parameters are managed by the library',
    'IPR': 'changing the port speed would cut the link',
}

# Basic commands (no + prefix) refused explicitly, to give a clear reason
_FORBIDDEN_BASIC = (
    (re.compile(r'^ATE'), 'echo is managed by the library'),
    (re.compile(r'^ATZ'), 'resetting the configuration is forbidden'),
    (re.compile(r'^AT&F'), 'resetting the configuration is forbidden'),
    (re.compile(r'^AT&W'), 'saving the configuration is forbidden'),
    (re.compile(r'^ATD'), 'calls are not available'),
    (re.compile(r'^ATA'), 'calls are not available'),
    (re.compile(r'^ATH'), 'calls are not available'),
)

# Commands run without parameter that only give information
_INFORMATIVE = {'AT', 'AT+CGMI', 'AT+CGMM', 'AT+CGMR', 'AT+CSQ', 'AT+CNUM'}
_INFORMATIVE_PATTERN = re.compile(r'^ATI\d{0,2}$')

# Allowed writes: the whole normalized command must match
_ALLOWED_WRITES = (
    re.compile(r'^AT\+CNMI=\d(,\d){0,4}$'),
    re.compile(r'^AT\+CSMS=[01]$'),
    re.compile(r'^AT\+CFUN=[14]$'),
)

_EXTENDED = re.compile(r'^AT\+([A-Z0-9]+)(\?|=\?|=(.*))?$')
_VENDOR_READ = re.compile(r'^AT\^[A-Z0-9]+(\?|=\?)$')
_CFUN_RESET = re.compile(r'^AT\+CFUN=\d,1$')


def normalize(command: str) -> str | None:
    """ Upper case and no space outside quotes. None if the command is not a single printable ASCII command
    with balanced quotes and no ``;`` outside quotes (which would chain a second command). """
    result: list[str] = []
    inQuotes = False
    for char in command.strip():
        if not 32 <= ord(char) <= 126:
            return None
        if char == '"':
            inQuotes = not inQuotes
            result.append(char)
        elif inQuotes:
            result.append(char)
        elif char == ';':
            return None
        elif char != ' ':
            result.append(char.upper())
    return None if inQuotes else ''.join(result)


def checkAtCommand(command: object) -> AtVerdict:
    """ Decides whether a command may be sent. :return: the verdict, with the normalized command to send """
    if not isinstance(command, str) or not command.strip():
        return AtVerdict(False, 'empty or invalid command', '')
    if len(command) > MAX_LENGTH:
        return AtVerdict(False, f'command too long (more than {MAX_LENGTH} characters)', '')
    normalized = normalize(command)
    if normalized is None:
        return AtVerdict(False, 'a single printable command with balanced quotes is expected (no ";")', '')
    if not normalized.startswith('AT'):
        return AtVerdict(False, 'not an AT command', normalized)

    def refuse(reason: str) -> AtVerdict:
        return AtVerdict(False, reason, normalized)

    for pattern, reason in _FORBIDDEN_BASIC:
        if pattern.match(normalized):
            return refuse(reason)

    extended = _EXTENDED.match(normalized)
    if extended:
        name, form, parameters = extended.group(1), extended.group(2), extended.group(3)
        if name in _FORBIDDEN_ANY:
            return refuse(_FORBIDDEN_ANY[name])
        if name == 'CFUN' and _CFUN_RESET.match(normalized):
            return refuse('restarting the modem is not available from the console')
        isRead = form in ('?', '=?')
        if parameters is not None and not isRead and name in _FORBIDDEN_WRITE:
            return refuse(_FORBIDDEN_WRITE[name])
        if isRead:
            return AtVerdict(True, '', normalized)
    elif _VENDOR_READ.match(normalized):
        return AtVerdict(True, '', normalized)

    if normalized in _INFORMATIVE or _INFORMATIVE_PATTERN.match(normalized):
        return AtVerdict(True, '', normalized)
    if any(pattern.match(normalized) for pattern in _ALLOWED_WRITES):
        return AtVerdict(True, '', normalized)
    return refuse('command not allowed in diagnostic mode')
