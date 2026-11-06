""" Tests of the reading of the APN answers (no modem needed). Run from resources/sms4gd:
python -m unittest discover -s tests -t .

The answers follow 3GPP 27.007 and the AT manuals of SimCom and Quectel (no capture of AT+CGCONTRDP on a real modem yet).
"""

import unittest

from wwanlib.apn import (ApnReading, PdpContext, contextOf, describeContext, isValidApn, parseAccessTechnology, parseContexts,
                         parseNetworkApn, sameApn, splitFields)


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

    def testNetworkApnWithQuotes(self):
        lines = ['+CGCONTRDP: 1,5,"free.mnc015.mcc208.gprs","10.12.34.56.255.255.255.0","10.12.34.1","212.27.40.240"', 'OK']
        self.assertEqual(parseNetworkApn(lines), ('free.mnc015.mcc208.gprs', []))

    def testNetworkApnWithoutQuotes(self):
        self.assertEqual(parseNetworkApn(['+CGCONTRDP: 1,5,orange,10.1.2.3.255.0.0.0', 'OK']), ('orange', []))

    def testNetworkApnOfContextOneFirst(self):
        lines = ['+CGCONTRDP: 2,6,"ims"', '+CGCONTRDP: 1,5,"free"', 'OK']
        self.assertEqual(parseNetworkApn(lines)[0], 'free')
        self.assertEqual(parseNetworkApn(['+CGCONTRDP: 3,6,"internet"', 'OK'])[0], 'internet')

    def testNoNetworkApn(self):
        self.assertEqual(parseNetworkApn(['OK']), (None, []))
        self.assertEqual(parseNetworkApn(['+CGCONTRDP: 1,5,""', 'OK']), (None, []))
        self.assertEqual(parseNetworkApn(['+CGCONTRDP: 1', 'OK']), (None, ['+CGCONTRDP: 1']))

    def testAccessTechnology(self):
        self.assertEqual(parseAccessTechnology(['+COPS: 0,0,"Free Free",7', 'OK']), 7)
        self.assertEqual(parseAccessTechnology(['+COPS: 0,0,"Orange F",8', 'OK']), 8)
        self.assertEqual(parseAccessTechnology(['+COPS: 0,0,"Free",2', 'OK']), 2)
        self.assertIsNone(parseAccessTechnology(['+COPS: 0', 'OK']))
        self.assertIsNone(parseAccessTechnology(['+COPS: 0,0,"Free"', 'OK']))
        self.assertIsNone(parseAccessTechnology(['OK']))

    def testLte(self):
        self.assertTrue(ApnReading(None, 'free', 7).lte)
        self.assertTrue(ApnReading(None, None, 8).lte)  # LTE-M of a Quectel BG96
        self.assertFalse(ApnReading(None, None, 2).lte)
        self.assertFalse(ApnReading(None, None, 0).lte)
        self.assertIsNone(ApnReading(None, None, None).lte)

    def testDescriptionNeverHoldsTheAddresses(self):
        self.assertEqual(describeContext(PdpContext(1, 'IP', 'CMNET')), 'context 1: IP, APN "CMNET"')
        self.assertEqual(describeContext(PdpContext(1, 'IPV4V6', '')), 'context 1: IPV4V6, APN "" (given by the operator)')
        self.assertEqual(describeContext(None), 'context 1 not defined')
