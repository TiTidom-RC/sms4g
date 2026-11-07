""" APN of the modem: what its memory holds (``AT+CGDCONT?``) and what the network gave (``AT+CGCONTRDP``).

In LTE the modem cannot register without a default data connection, opened with the APN of context 1: an APN the
operator does not know (a modem sold with the one of a foreign operator) keeps the modem off the network, SMS
included. An empty APN lets the operator use the default one of the subscription. Context 1 is kept by the modem
(it survives a restart): it is written only when asked, never at each connection.

Pure functions only: the Modem sends the commands (``Modem.readApn`` / ``Modem.setApn``).
"""

import re
from dataclasses import dataclass

# What an APN may hold (labels of letters, digits and dashes, separated by dots); empty = given by the operator.
# Nothing else may reach the AT command, where the APN is written between quotes.
APN_PATTERN = re.compile(r'^[A-Za-z0-9.-]{0,100}$')
CONTEXT = 1  # the context used to register in LTE
# Types tried in order when context 1 is not defined yet (IP for the old 3G modems that do not know IPV4V6)
DEFAULT_PDP_TYPES = ('IPV4V6', 'IP')


@dataclass(frozen=True)
class PdpContext:
    cid: int
    pdpType: str
    apn: str  # empty: given by the operator


@dataclass(frozen=True)
class Connection:
    """ A connection in use, as ``AT+CGCONTRDP`` gives it (one per context, the lines of each address family put together) """
    cid: int
    bearer: int | None  # number the network gave to the connection (bearer id), None when the modem does not say
    apn: str  # APN the network gave (empty when the modem does not say)
    families: tuple[str, ...]  # 'IPv4' and / or 'IPv6' obtained, from the form of the addresses (which are never kept)


@dataclass(frozen=True)
class ApnReading:
    """ Result of ``Modem.readApn``: every context of the memory, every connection in use, nothing left out """
    contexts: tuple[PdpContext, ...]  # the contexts in the memory of the modem (AT+CGDCONT?)
    connections: tuple[Connection, ...]  # the connections in use (AT+CGCONTRDP): empty when there is none, or not read
    connectionsRead: bool = True  # False when the modem did not answer: nothing is known about the connections

    @property
    def configured(self) -> PdpContext | None:
        """ Context 1 in the memory of the modem, None when it is not defined """
        return contextOf(list(self.contexts))

    def connection(self, cid: int = CONTEXT) -> Connection | None:
        return next((connection for connection in self.connections if connection.cid == cid), None)


@dataclass(frozen=True)
class ApnChange:
    """ Result of ``Modem.setApn`` """
    status: str  # 'written', or 'unchanged' (the modem already had it: nothing written, the radio left alone)
    previous: str | None  # APN of context 1 before, None when it was not defined
    configured: PdpContext | None  # context 1 read back after the write
    radio: str | None  # how the modem took the new APN: 'cfun' (radio off and on), 'restart', 'failed'; None if unchanged


def isValidApn(apn: object) -> bool:
    return isinstance(apn, str) and APN_PATTERN.match(apn) is not None


def sameApn(first: str, second: str) -> bool:
    """ An APN is a domain name: the case does not matter """
    return first.lower() == second.lower()


def splitFields(text: str) -> list[str]:
    """ The comma-separated fields of an answer, without their quotes (a comma between quotes is kept) """
    fields: list[str] = []
    current: list[str] = []
    inQuotes = False
    for char in text:
        if char == '"':
            inQuotes = not inQuotes
        elif char == ',' and not inQuotes:
            fields.append(''.join(current).strip())
            current = []
        else:
            current.append(char)
    fields.append(''.join(current).strip())
    return fields


def _answerLines(prefix: str, lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith(prefix)]


def parseContexts(lines: list[str]) -> tuple[list[PdpContext], list[str]]:
    """ ``+CGDCONT: <cid>,"<type>","<apn>",...`` lines. :return: the contexts, and the lines that could not be read """
    contexts: list[PdpContext] = []
    unreadable: list[str] = []
    for line in _answerLines('+CGDCONT:', lines):
        fields = splitFields(line[len('+CGDCONT:'):])
        if len(fields) < 3 or not fields[0].isdigit():
            unreadable.append(line)
            continue
        contexts.append(PdpContext(int(fields[0]), fields[1], fields[2]))
    return contexts, unreadable


def contextOf(contexts: list[PdpContext], cid: int = CONTEXT) -> PdpContext | None:
    return next((context for context in contexts if context.cid == cid), None)


def addressFamilies(field: str) -> tuple[str, ...]:
    """ The families of the addresses of a field, from their form only: ``a.b.c.d`` (or 8 numbers with the mask) is
    IPv4, 16 numbers (32 with the mask) or a form with ``:`` is IPv6. Some modems give both, separated by a space. """
    found: set[str] = set()
    for token in field.split():
        parts = token.split('.')
        if ':' in token:
            found.add('IPv6')
        elif all(part.isdigit() for part in parts):
            if len(parts) in (4, 8):
                found.add('IPv4')
            elif len(parts) in (16, 32):
                found.add('IPv6')
    return tuple(family for family in ('IPv4', 'IPv6') if family in found)


def familiesLabel(families: tuple[str, ...]) -> str:
    return 'IPv4v6' if len(families) == 2 else (families[0] if families else '')


def parseConnections(lines: list[str]) -> tuple[tuple[Connection, ...], list[str]]:
    """ ``+CGCONTRDP: <cid>,<bearer id>,<apn>,<local address and mask>,...`` lines (with or without quotes, depending on
    the modem; one line per address family on some modems). :return: the connections, in the order of the answer, and the
    lines that could not be read """
    grouped: dict[int, dict] = {}
    unreadable: list[str] = []
    for line in _answerLines('+CGCONTRDP:', lines):
        fields = splitFields(line[len('+CGCONTRDP:'):])
        if len(fields) < 3 or not fields[0].isdigit():  # <cid>,<bearer id>,<apn> at least
            unreadable.append(line)
            continue
        entry = grouped.setdefault(int(fields[0]), {'bearer': None, 'apn': '', 'families': set()})
        if entry['bearer'] is None and fields[1].isdigit():
            entry['bearer'] = int(fields[1])
        if not entry['apn']:
            entry['apn'] = fields[2]
        if len(fields) > 3:
            entry['families'].update(addressFamilies(fields[3]))
    connections = tuple(Connection(cid, entry['bearer'], entry['apn'],
                                   tuple(family for family in ('IPv4', 'IPv6') if family in entry['families']))
                        for cid, entry in grouped.items())
    return connections, unreadable


# Addresses in the answers, for the logs: the fields after <cid>,<bearer id>,<apn> of +CGCONTRDP (address and mask, gateway,
# DNS...), and the address field of +CGDCONT. A line stops at a quote, a bracket or a "|" (a list or a join of lines).
_CGCONTRDP_ADDRESSES = re.compile(r"(\+CGCONTRDP:\s*\d+,[^,'|\]\r\n]*,[^,'|\]\r\n]*)([^'|\]\r\n]*)")
_CGDCONT_ADDRESS = re.compile(r'(\+CGDCONT:\s*\d+,"[^"]*","[^"]*",)("[^"]*")')
_ADDRESS = re.compile(r'(?<![0-9A-Za-z.:])[0-9A-Fa-f.:]*[.:][0-9A-Fa-f.:]*(?![0-9A-Za-z.:])')


def _maskAddress(address: str) -> str:
    """ The form of the address is kept, and its start, which tells its kind without telling the line (shared by millions):
    ``10.x.x.x`` (private IPv4), ``2a01:x:x::x``, ``253.222.x…`` (IPv6 written as 16 numbers, fd… = private). A mask that
    follows the address (8 or 32 numbers) stays, ``0.0.0.0`` (no address) too. """
    if ':' in address:
        groups = address.split(':')
        first = next((index for index, group in enumerate(groups) if group), None)
        return ':'.join(group if index == first or not group else 'x' for index, group in enumerate(groups))
    parts = address.split('.')
    if not all(part.isdigit() for part in parts):
        return re.sub(r'[0-9A-Fa-f]', 'x', address)
    if all(int(part) == 0 for part in parts):
        return address
    kept = 2 if len(parts) in (16, 32) else 1
    length = {8: 4, 32: 16}.get(len(parts), len(parts))  # what follows is the mask
    return '.'.join(part if index < kept or index >= length else 'x' for index, part in enumerate(parts))


def maskAddresses(text: str) -> str:
    """ The addresses of the +CGCONTRDP and +CGDCONT answers in a text, masked (see ``_maskAddress``): the context, the
    connection number and the APN stay readable. Anything else is left untouched. """
    def mask(match: re.Match) -> str:
        return match.group(1) + _ADDRESS.sub(lambda address: _maskAddress(address.group(0)), match.group(2))

    return _CGDCONT_ADDRESS.sub(mask, _CGCONTRDP_ADDRESSES.sub(mask, text))


def describeContext(context: PdpContext | None) -> str:
    """ Context 1 as the logs show it: never the raw answer, which holds IP addresses """
    if context is None:
        return 'context 1 not defined'
    return f'context {context.cid}: {context.pdpType}, APN "{context.apn}"' + (' (given by the operator)' if not context.apn else '')


def describeReading(reading: ApnReading) -> str:
    """ Every context as the logs show it, in the same order as the configuration page:
    ``1 IP "" -> free IPv4 #5 | 2 IPV4V6 "ims" -> ims IPv6 #6 | 3 IPV4V6 "" inactive``. Never the addresses. """
    parts: list[str] = []
    cids = [context.cid for context in reading.contexts]
    cids += [connection.cid for connection in reading.connections if connection.cid not in cids]
    for cid in sorted(cids):
        context = contextOf(list(reading.contexts), cid)
        text = f'{cid} {context.pdpType} "{context.apn}"' if context is not None else f'{cid} (not in the memory)'
        connection = reading.connection(cid)
        if connection is not None:
            details = [connection.apn or '""', familiesLabel(connection.families)]
            if connection.bearer is not None:
                details.append(f'#{connection.bearer}')
            text += ' -> ' + ' '.join(detail for detail in details if detail)
        elif reading.connectionsRead:
            text += ' inactive'
        parts.append(text)
    if not parts:
        parts.append('no context in the memory')
    if not reading.connectionsRead:
        parts.append('connections not read (no answer from the modem)')
    return ' | '.join(parts)
