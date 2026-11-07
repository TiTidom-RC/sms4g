""" Tests of the masking of the secrets and of the phone numbers in the logs (no modem needed). Run from
resources/sms4gd:  python -m unittest discover -s tests -t . """

import logging
import unittest

import sms4gd
from tests.test_apn import SIM7600_CGCONTRDP, SIM7600_CGDCONT
from wwanlib import maskPduNumbers
from wwanlib.pdu import decodeSmsPdu, encodeSmsSubmitPdu

SUBMIT = '0011000B913316325476F80000AA04D4F29C0E'  # to +33612345678
DELIVER = '07913366003000F0040A81307723745500006201611141928002C830'  # from 0377324755
STATUS_REPORT = '07913366003000F0060A0B913316325476F862016111419280620161114192800000'  # for +33612345678
# The same digits as in the text (+336, 0377, and the last two), zeros in the middle
SUBMIT_MASKED = '0011000B913306000070F80000AA04D4F29C0E'  # +33600000078
DELIVER_MASKED = '07913366003000F0040A81307700005500006201611141928002C830'  # 0377000055
STATUS_REPORT_MASKED = '07913366003000F0060A0B913306000070F862016111419280620161114192800000'
FROM_A_NAME = '000407D0C6F75B0E00006201611141928000'  # alphanumeric sender (type D0)
FROM_A_SHORT_NUMBER = '0004058136015100006201611141928000'  # 5 digits: 36105


def masked(message: str, maskPdu: bool = False) -> str:
    record = logging.LogRecord('test', logging.DEBUG, '', 0, message, (), None)
    sms4gd.SecretMaskFilter(maskPdu).filter(record)
    return record.getMessage()


class PhoneNumberTest(unittest.TestCase):
    def testNumberWithAPlus(self):
        self.assertEqual(masked('SMS to +33612345678 sent'), 'SMS to +336XXXXXX78 sent')

    def testNationalNumberInAPayload(self):
        self.assertEqual(masked("{'type': 'smsReceived', 'number': '0377324755', 'parts': 1}"),
                         "{'type': 'smsReceived', 'number': '0377XXXX55', 'parts': 1}")

    def testNumbersWithSeparators(self):
        for typed, shown in (('06 12 34 56 78', '06 1XXXXXXXX78'), ('06.12.34.56.78', '06.1XXXXXXXX78'),
                             ('06-12-34-56-78', '06-1XXXXXXXX78')):
            self.assertEqual(masked(f'from {typed}.'), f'from {shown}.', typed)

    def testInternationalNumbersWithoutAPlus(self):
        self.assertEqual(masked('from 33612345678'), 'from 3361XXXXX78')
        self.assertEqual(masked('from 0033612345678'), 'from 0033XXXXXXX78')

    def testWhatIsNotANumberIsLeftAlone(self):
        for line in (
                "'id': '8aee3cab28e04fa39eea8c3418f6cce1', 'time': 1792142069.519",
                'Device : /dev/serial/by-id/usb-SimTech__Incorporated_0123456789ABCDEF-if02-port0',
                'Writing PID 1830653 to /tmp/jeedom/sms4g/deamon.pid',
                'Opening port /dev/ttyUSB_GSM at 115200 bps',
                'SMS to +336XXXXXX78: part 1/26 accepted (TP-MR 65)',
                'SMS to 0377XXXX55 sent',
                'Self-test every : 4.0 h (0 = none), pause 0.123456789 s',
                "response: ['+CPMS: 0,23,0,23,0,23', 'OK']",
                'write: AT^SYSCFGEX="03",40000000,2,4,40000000,,',
                'write: AT^SYSCFGEX="03",3FFFFFFF,2,4,7FFFFFFFFFFFFFFF,,'):
            self.assertEqual(masked(line), line)

    def testTheSmsCenterStaysReadableInTheConnectionDiagnostic(self):
        self.assertEqual(masked('Diagnostic AT+CSCA? : +CSCA: "+33695000695",145'), 'Diagnostic AT+CSCA? : +CSCA: "+33695000695",145')
        self.assertEqual(masked('Diagnostic AT+CNUM : +CNUM: "","+33612345678",145'), 'Diagnostic AT+CNUM : +CNUM: "","+336XXXXXX78",145')
        self.assertEqual(masked('response: [\'+CSCA: "+33695000695",145\', \'OK\']'), 'response: [\'+CSCA: "+336XXXXXX95",145\', \'OK\']')

    def testApiKeyAndPin(self):
        self.assertEqual(masked('GET /x?apikey=abc123 HTTP/1.1'), 'GET /x?apikey=sEcReT HTTP/1.1')
        self.assertEqual(masked('write: AT+CPIN="1234"'), 'write: AT+CPIN=****')

    def testPinInEveryFormTypedInTheDiagnosticMode(self):
        # The diagnostic mode refuses AT+CPIN=, and logs the command it refuses: the PIN never shows, whatever its form
        for typed in ('AT+CPIN=1234', 'at+cpin="1234"', 'AT + CPIN = 1234', 'AT+CPIN="12345678","0000"'):
            with self.subTest(typed):
                line = masked(f'AT command refused (entering a PIN is forbidden): {typed}')
                self.assertNotIn('1234', line)
                self.assertNotIn('0000', line)
                self.assertTrue(line.endswith('=****'), line)
        self.assertEqual(masked("response: ['+CPIN: READY', 'OK']"), "response: ['+CPIN: READY', 'OK']")
        for read in ('write: AT+CPIN?', 'AT command accepted: AT+CPIN=? (timeout 15s)'):
            self.assertEqual(masked(read), read)


class RawPduTest(unittest.TestCase):
    """ By default the PDU of the Debug logs are kept as the modem sees them """

    def testPduLinesAreLeftAsIs(self):
        for line in (f'write: {SUBMIT}', f"response: ['+CMGR: 0,,22', '{DELIVER}', 'OK']", f"Notification: ['+CDS: 25', '{SUBMIT}']"):
            self.assertEqual(masked(line), line)


class MaskedPduTest(unittest.TestCase):
    """ With the option, the middle digits of the number are zeros, as many digits as in the text are kept: the frame is
    still a valid one """

    def testSubmit(self):
        self.assertEqual(maskPduNumbers(SUBMIT), SUBMIT_MASKED)

    def testDeliver(self):
        self.assertEqual(maskPduNumbers(DELIVER), DELIVER_MASKED)

    def testStatusReport(self):
        self.assertEqual(maskPduNumbers(STATUS_REPORT), STATUS_REPORT_MASKED)

    def testTheResultStaysHexadecimalAndMasksOnlyOnce(self):
        for pdu in (SUBMIT, DELIVER, STATUS_REPORT):
            result = maskPduNumbers(pdu)
            int(result, 16)
            self.assertEqual(len(result), len(pdu))
            self.assertEqual(maskPduNumbers(result), result)

    def testTheFrameStillDecodes(self):
        for number, expected in (('+33612345678', '+33600000078'), ('0612345678', '0612000078'), ('+336123456', '+336000056')):
            pdu = encodeSmsSubmitPdu(number, 'Bonjour à tous', reference=7, requestStatusReport=True)[0].data.hex().upper()
            before, after = decodeSmsPdu(pdu), decodeSmsPdu(maskPduNumbers(pdu))
            self.assertEqual(after['number'], expected)
            self.assertEqual((after['text'], after['reference']), (before['text'], before['reference']))

    def testASenderNameAndAShortNumberAreLeftAlone(self):
        self.assertEqual(maskPduNumbers(FROM_A_NAME), FROM_A_NAME)
        self.assertEqual(maskPduNumbers(FROM_A_SHORT_NUMBER), FROM_A_SHORT_NUMBER)

    def testWhatIsNotAPduIsReturnedAsIs(self):
        for text in ('', 'ZZ', '0011', '00110', '0011000B913316', '0013000B913316325476F80000', '00110000'):  # truncated, other type
            self.assertEqual(maskPduNumbers(text), text, text)

    def testLogLines(self):
        self.assertEqual(masked(f'write: {SUBMIT}', True), f'write: {SUBMIT_MASKED}')
        self.assertEqual(masked(f"response: ['+CMGR: 0,,22', '{DELIVER}', 'OK']", True),
                         f"response: ['+CMGR: 0,,22', '{DELIVER_MASKED}', 'OK']")
        self.assertEqual(masked(f"Notification: ['+CDS: 25', '{SUBMIT}']", True), f"Notification: ['+CDS: 25', '{SUBMIT_MASKED}']")

    def testLongHexOutsideOfTheModemTrafficIsLeftAlone(self):
        line = "Send to jeedom : {'id': 'e758eb4e78ba458a896a83906f376425'}"
        self.assertEqual(masked(line, True), line)


class AddressTest(unittest.TestCase):
    """ The IP addresses of AT+CGCONTRDP / AT+CGDCONT? (debug level): their form and their start stay, which tells their kind
    (private, public, none) without telling the line; the context, the connection number and the APN stay readable """

    def testAnswerOfTheSim7600(self):
        self.assertEqual(
            masked(f'response: {SIM7600_CGCONTRDP}'),
            "response: ['+CGCONTRDP: 1,5,free,192.x.x.x,,198.x.x.x,198.x.x.x', "
            "'+CGCONTRDP: 2,6,ims,32.1.x.x.x.x.x.x.x.x.x.x.x.x.x.x, 32.1.x.x.x.x.x.x.x.x.x.x.x.x.x.x, "
            "32.1.x.x.x.x.x.x.x.x.x.x.x.x.x.x, 32.1.x.x.x.x.x.x.x.x.x.x.x.x.x.x,32.1.x.x.x.x.x.x.x.x.x.x.x.x.x.x,"
            "32.1.x.x.x.x.x.x.x.x.x.x.x.x.x.x', 'OK']")

    def testContextsOfTheMemory(self):
        self.assertEqual(masked(f'response: {SIM7600_CGDCONT}'), f'response: {SIM7600_CGDCONT}')  # 0.0.0.0: no address
        self.assertEqual(masked('+CGDCONT: 1,"IPV4V6","free","10.12.34.56",0,0'), '+CGDCONT: 1,"IPV4V6","free","10.x.x.x",0,0')

    def testForms(self):
        for address, shown in (('10.175.25.120', '10.x.x.x'), ('10.12.34.56.255.255.255.0', '10.x.x.x.255.255.255.0'),
                               ('2a01:e0a:1::1', '2a01:x:x::x'), ('fe80::1', 'fe80::x'),
                               ('.'.join(['253', '222'] + ['1'] * 30), '.'.join(['253', '222'] + ['x'] * 14 + ['1'] * 16)),
                               ('1.2.3.4.5', '1.x.x.x.x')):
            with self.subTest(address):
                self.assertEqual(masked(f'+CGCONTRDP: 1,5,"free","{address}"'), f'+CGCONTRDP: 1,5,"free","{shown}"')

    def testLinesJoinedByPhpOrInAListAreMaskedOneByOne(self):
        self.assertEqual(masked('+CGCONTRDP: 1,5,"free.mnc015.mcc208.gprs","10.1.2.3" | +CGCONTRDP: 2,6,"ims","fd00:1::2" | OK'),
                         '+CGCONTRDP: 1,5,"free.mnc015.mcc208.gprs","10.x.x.x" | +CGCONTRDP: 2,6,"ims","fd00:x::x" | OK')

    def testWhatIsNotAnAnswerOfTheseCommandsIsLeftAlone(self):
        for line in ('Callback : http://127.0.0.1:80/plugins/sms4g/core/php/jeesms4g.php',
                     'PyEnvVersion :: 2.8.14-17-ge6669f0a', 'Diagnostic APN : 1 IP "" -> free IPv4 #5',
                     "response: ['+CPSI: LTE,Online,208-15,0x23F9,108521043,362,EUTRAN-BAND3,1675,4,4,-130,-1073,-750,12', 'OK']"):
            self.assertEqual(masked(line), line)


if __name__ == '__main__':
    unittest.main()
