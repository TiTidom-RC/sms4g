""" Tests of the reading of the APN answers (no modem needed). Run from resources/sms4gd:
python -m unittest discover -s tests -t .

The answers follow 3GPP 27.007 and the AT manuals of SimCom and Quectel (no capture of AT+CGCONTRDP on a real modem yet).
"""

import unittest

from wwanlib.apn import (ApnReading, Connection, PdpContext, addressFamilies, contextOf, describeContext, describeReading,
                         familiesLabel, isValidApn, parseConnections, parseContexts, sameApn, splitFields)

# Answers of a SIM7600G-H (LE20B04SIM7600G22): same layout, the addresses replaced by documentation ones (192.0.2.0/24,
# 198.51.100.0/24 and 2001:db8::/32, written as the modem writes them, 16 numbers)
_IPV6 = '32.1.13.184.0.0.0.0.0.0.0.0.0.0.0.{}'
SIM7600_CGDCONT = ['+CGDCONT: 1,"IP","","0.0.0.0",0,0,0,0',
                   '+CGDCONT: 2,"IPV4V6","ims","0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0",0,0,0,0',
                   '+CGDCONT: 3,"IPV4V6","","0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0",0,0,0,1', 'OK']
SIM7600_CGCONTRDP = ['+CGCONTRDP: 1,5,free,192.0.2.10,,198.51.100.1,198.51.100.2',
                     f'+CGCONTRDP: 2,6,ims,{_IPV6.format(10)}, {_IPV6.format(1)}, {_IPV6.format(2)}, {_IPV6.format(3)},'
                     f'{_IPV6.format(4)},{_IPV6.format(5)}', 'OK']


class ApnValueTest(unittest.TestCase):
    def testValidApn(self):
        for apn in ('', 'free', 'orange', 'sl2sfr', 'mmsbouygtel.com', 'free.mnc015.mcc208.gprs', 'my-apn', 'a' * 100):
            self.assertTrue(isValidApn(apn), apn)

    def testInvalidApn(self):
        # Nothing that could leave the quotes of the AT command, nor anything else than a domain-like name
        for apn in ('free"', 'free";AT+CFUN=0', 'free,1', 'fr ee', 'free\r', 'frée', 'a' * 101, None, 12, ['free']):
            self.assertFalse(isValidApn(apn), repr(apn))

    def testTheCaseDoesNotMatter(self):
        self.assertTrue(sameApn('FREE', 'free'))
        self.assertFalse(sameApn('free', 'orange'))


class ParsingTest(unittest.TestCase):
    def testSplitFieldsKeepsTheCommasBetweenQuotes(self):
        self.assertEqual(splitFields(' 1,"IPV4V6","","0.0.0.0,0:0:0:0:0:0:0:0",0'), ['1', 'IPV4V6', '', '0.0.0.0,0:0:0:0:0:0:0:0', '0'])

    def testContextsOfASimcom(self):
        lines = ['+CGDCONT: 1,"IP","CMNET","0.0.0.0",0,0,0,0', '+CGDCONT: 2,"IPV4V6","ims","0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0",0,0,0,0', 'OK']
        contexts, unreadable = parseContexts(lines)
        self.assertEqual(contexts, [PdpContext(1, 'IP', 'CMNET'), PdpContext(2, 'IPV4V6', 'ims')])
        self.assertEqual(unreadable, [])
        self.assertEqual(contextOf(contexts), PdpContext(1, 'IP', 'CMNET'))

    def testEmptyApnOfAQuectel(self):
        contexts, _ = parseContexts(['+CGDCONT: 1,"IPV4V6","","0.0.0.0,0:0:0:0:0:0:0:0",0,0,0,0', 'OK'])
        self.assertEqual(contextOf(contexts), PdpContext(1, 'IPV4V6', ''))

    def testContextOneNotDefined(self):
        contexts, _ = parseContexts(['+CGDCONT: 3,"IP","internet"', 'OK'])
        self.assertIsNone(contextOf(contexts))
        self.assertEqual(parseContexts(['OK']), ([], []))

    def testUnreadableLinesAreSetApart(self):
        contexts, unreadable = parseContexts(['+CGDCONT: x,"IP","free"', '+CGDCONT: 1', '+CGDCONT: 1,"IP","free"', 'OK'])
        self.assertEqual(contexts, [PdpContext(1, 'IP', 'free')])
        self.assertEqual(unreadable, ['+CGDCONT: x,"IP","free"', '+CGDCONT: 1'])

    def testConnectionsOfASim7600(self):
        """ Answer of the SIM7600G-H (LE20B04SIM7600G22, SIM Free) captured on 2026-11-07: no quotes, IPv4 without mask
        for context 1, IPv6 written as 16 numbers for context 2 """
        connections, unreadable = parseConnections(SIM7600_CGCONTRDP)
        self.assertEqual(connections, (Connection(1, 5, 'free', ('IPv4',)), Connection(2, 6, 'ims', ('IPv6',))))
        self.assertEqual(unreadable, [])

    def testConnectionWithQuotesAndMask(self):
        lines = ['+CGCONTRDP: 1,5,"free.mnc015.mcc208.gprs","10.12.34.56.255.255.255.0","10.12.34.1","212.27.40.240"', 'OK']
        self.assertEqual(parseConnections(lines)[0], (Connection(1, 5, 'free.mnc015.mcc208.gprs', ('IPv4',)),))

    def testOneLinePerAddressFamilyIsPutTogether(self):
        lines = ['+CGCONTRDP: 1,5,"free","10.1.2.3"', '+CGCONTRDP: 1,5,"free","2a01:e0a:1::1"', 'OK']
        self.assertEqual(parseConnections(lines)[0], (Connection(1, 5, 'free', ('IPv4', 'IPv6')),))

    def testBothFamiliesInOneField(self):
        self.assertEqual(addressFamilies('10.1.2.3 2a01:e0a::1'), ('IPv4', 'IPv6'))

    def testAddressFamilies(self):
        self.assertEqual(addressFamilies('10.175.25.120'), ('IPv4',))
        self.assertEqual(addressFamilies('10.1.2.3.255.0.0.0'), ('IPv4',))
        self.assertEqual(addressFamilies('.'.join(['0'] * 16)), ('IPv6',))
        self.assertEqual(addressFamilies('.'.join(['0'] * 32)), ('IPv6',))
        self.assertEqual(addressFamilies('fe80::1'), ('IPv6',))
        for unknown in ('', '1.2.3', 'abc', '1.2.3.4.5'):
            self.assertEqual(addressFamilies(unknown), (), unknown)
        self.assertEqual(familiesLabel(('IPv4', 'IPv6')), 'IPv4v6')
        self.assertEqual(familiesLabel(()), '')

    def testConnectionWithoutAddressNorBearer(self):
        self.assertEqual(parseConnections(['+CGCONTRDP: 3,,"internet"', 'OK'])[0], (Connection(3, None, 'internet', ()),))

    def testNoConnection(self):
        self.assertEqual(parseConnections(['OK']), ((), []))
        self.assertEqual(parseConnections(['+CGCONTRDP: 1', 'OK']), ((), ['+CGCONTRDP: 1']))

    def testReadingOfLastNightAndOfThisMorning(self):
        contexts = tuple(parseContexts(SIM7600_CGDCONT)[0])
        morning = ApnReading(contexts, parseConnections(SIM7600_CGCONTRDP)[0])
        self.assertEqual(morning.configured, PdpContext(1, 'IP', ''))
        self.assertEqual(morning.connection(), Connection(1, 5, 'free', ('IPv4',)))
        self.assertEqual(describeReading(morning), '1 IP "" -> free IPv4 #5 | 2 IPV4V6 "ims" -> ims IPv6 #6 | 3 IPV4V6 "" inactive')
        # 2026-11-06 evening: only the connection of context 2 was in use (rebuilt from the summary of the log)
        evening = ApnReading(contexts, parseConnections([SIM7600_CGCONTRDP[1], 'OK'])[0])
        self.assertIsNone(evening.connection())
        self.assertEqual(describeReading(evening), '1 IP "" inactive | 2 IPV4V6 "ims" -> ims IPv6 #6 | 3 IPV4V6 "" inactive')

    def testReadingWhenTheConnectionsWereNotRead(self):
        reading = ApnReading((PdpContext(1, 'IP', 'CMNET'),), (), connectionsRead=False)
        self.assertEqual(describeReading(reading), '1 IP "CMNET" | connections not read (no answer from the modem)')
        refused = ApnReading((PdpContext(1, 'IP', 'CMNET'),), (), connectionsRead=False, connectionsRefused=True)
        self.assertEqual(describeReading(refused), '1 IP "CMNET" | connections not read (AT+CGCONTRDP refused)')

    def testReadingWithoutAnyConnection(self):
        # SIM7600G-H with its radio off: OK without any line to both forms (2026-11-07)
        reading = ApnReading(tuple(parseContexts(SIM7600_CGDCONT)[0]), parseConnections(['OK'])[0])
        self.assertEqual(describeReading(reading), '1 IP "" inactive | 2 IPV4V6 "ims" inactive | 3 IPV4V6 "" inactive')

    def testConnectionOfAContextThatIsNotInTheMemory(self):
        reading = ApnReading((), (Connection(4, 7, 'internet', ('IPv4',)),))
        self.assertEqual(describeReading(reading), '4 (not in the memory) -> internet IPv4 #7')
        self.assertEqual(describeReading(ApnReading((), ())), 'no context in the memory')

    def testReadingOfContextOneOnly(self):
        # The modem refused AT+CGCONTRDP without a parameter: AT+CGCONTRDP=1 tells nothing about the other contexts
        contexts = tuple(parseContexts(SIM7600_CGDCONT)[0])
        reading = ApnReading(contexts, (Connection(1, 5, 'free', ('IPv4',)),), onlyContext=1)
        self.assertEqual(describeReading(reading), '1 IP "" -> free IPv4 #5 | 2 IPV4V6 "ims" not read | 3 IPV4V6 "" not read | '
                                                   'connections read for context 1 only (AT+CGCONTRDP without a parameter refused)')
        inactive = ApnReading(contexts, (), onlyContext=1)
        self.assertTrue(describeReading(inactive).startswith('1 IP "" inactive | 2 IPV4V6 "ims" not read'))
        self.assertEqual([inactive.connectionKnown(cid) for cid in (1, 2, 3)], [True, False, False])

    def testDescriptionNeverHoldsTheAddresses(self):
        self.assertEqual(describeContext(PdpContext(1, 'IP', 'CMNET')), 'context 1: IP, APN "CMNET"')
        self.assertEqual(describeContext(PdpContext(1, 'IPV4V6', '')), 'context 1: IPV4V6, APN "" (given by the operator)')
        self.assertEqual(describeContext(None), 'context 1 not defined')
