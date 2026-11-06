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
# <AcT> of +COPS in LTE (3GPP 27.007: 7 E-UTRAN, 9 E-UTRAN NB-S1, 10 E-UTRA on 5GC, 13 E-UTRA-NR dual). Quectel gives
# 8 for LTE-M (eMTC) on the BG96, where 3GPP has EC-GSM-IoT, which these modems do not offer.
LTE_ACCESS_TECHNOLOGIES = frozenset({7, 8, 9, 10, 13})


@dataclass(frozen=True)
class PdpContext:
    cid: int
    pdpType: str
    apn: str  # empty: given by the operator


@dataclass(frozen=True)
class ApnReading:
    """ Result of ``Modem.readApn`` """
    configured: PdpContext | None  # context 1 in the memory of the modem, None when it is not defined
    network: str | None  # APN given by the network to the connection in use, None when there is none (not in LTE...)
    accessTechnology: int | None  # <AcT> of +COPS, None when unknown (not registered...)

    @property
    def lte(self) -> bool | None:
        """ Whether the modem is registered in LTE, None when unknown """
        return None if self.accessTechnology is None else self.accessTechnology in LTE_ACCESS_TECHNOLOGIES


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


def parseNetworkApn(lines: list[str]) -> tuple[str | None, list[str]]:
    """ ``+CGCONTRDP: <cid>,<bearer id>,<apn>,...`` lines (the APN with or without quotes, depending on the modem).
    :return: the APN of context 1, else the first one given, None when none; and the lines that could not be read """
    found: list[tuple[int, str]] = []
    unreadable: list[str] = []
    for line in _answerLines('+CGCONTRDP:', lines):
        fields = splitFields(line[len('+CGCONTRDP:'):])
        if len(fields) < 3 or not fields[0].isdigit():
            unreadable.append(line)
            continue
        if fields[2]:
            found.append((int(fields[0]), fields[2]))
    apn = next((apn for cid, apn in found if cid == CONTEXT), found[0][1] if found else None)
    return apn, unreadable


def parseAccessTechnology(lines: list[str]) -> int | None:
    """ <AcT> of ``+COPS: <mode>,<format>,"<operator>",<AcT>``, None when the modem is not registered or does not say """
    line = next(iter(_answerLines('+COPS:', lines)), None)
    if line is None:
        return None
    fields = splitFields(line[len('+COPS:'):])
    return int(fields[3]) if len(fields) >= 4 and fields[3].isdigit() else None


def describeContext(context: PdpContext | None) -> str:
    """ Context 1 as the logs show it: never the raw answer, which holds IP addresses """
    if context is None:
        return 'context 1 not defined'
    return f'context {context.cid}: {context.pdpType}, APN "{context.apn}"' + (' (given by the operator)' if not context.apn else '')
