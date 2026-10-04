""" Tests of the filter of the AT commands (no modem needed). Run from resources/sms4gd:
python -m unittest discover -s tests -t . """

import unittest

from atfilter import MAX_LENGTH, checkAtCommand, normalize

ALLOWED = [
    # (what is typed, what is sent)
    ('AT', 'AT'),
    ('ATI', 'ATI'),
    ('ATI9', 'ATI9'),
    ('at+csq', 'AT+CSQ'),
    (' at + csq ', 'AT+CSQ'),
    ('  AT+CPIN?\r\n', 'AT+CPIN?'),  # a copy-paste with a line break around: only the normalized command is sent
    ('AT+CGMI', 'AT+CGMI'),
    ('AT+CGMM', 'AT+CGMM'),
    ('AT+CGMR', 'AT+CGMR'),
    ('AT+CNUM', 'AT+CNUM'),
    # reads
    ('AT+CPSI?', 'AT+CPSI?'),
    ('AT+CNMI?', 'AT+CNMI?'),
    ('AT+CNMI=?', 'AT+CNMI=?'),
    ('AT+COPS=?', 'AT+COPS=?'),
    ('AT+CPMS?', 'AT+CPMS?'),
    ('AT+CPMS=?', 'AT+CPMS=?'),
    ('AT+CPIN?', 'AT+CPIN?'),
    ('AT+CSCA?', 'AT+CSCA?'),
    ('AT+CSMS?', 'AT+CSMS?'),
    ('AT^SYSINFO?', 'AT^SYSINFO?'),
    # whitelisted writes
    ('AT+CNMI=2,1,0,2', 'AT+CNMI=2,1,0,2'),
    ('AT+CNMI=1,1,0,1,0', 'AT+CNMI=1,1,0,1,0'),
    ('AT+CSMS=0', 'AT+CSMS=0'),
    ('AT+CSMS=1', 'AT+CSMS=1'),
    ('AT+CFUN=1', 'AT+CFUN=1'),
    ('AT+CFUN=4', 'AT+CFUN=4'),
    ('AT+CFUN=1,1', 'AT+CFUN=1,1'),
    ('AT+CRESET', 'AT+CRESET'),
    ('at+cfun=1,1', 'AT+CFUN=1,1'),
]

REFUSED = [
    # (command, fragment of the reason)
    ('ATZ', 'resetting'),
    ('AT&F', 'resetting'),
    ('AT&W', 'saving'),
    ('ATE0', 'echo'),
    ('ATD0612345678', 'calls'),
    ('ATA', 'calls'),
    ('ATH', 'calls'),
    ('AT+CMGF=1', 'SMS mode'),
    ('AT+CMEE=0', 'error reporting'),
    ('AT+CPIN="0000"', 'PIN'),
    ('AT+CPMS="ME"', 'memory selection'),
    ('AT+CPMS="ME","ME","ME"', 'memory selection'),
    ('AT+CSCA="+33695000695"', 'SMS center'),
    ('AT+CSMP=17,167,0,0', 'SMS parameters'),
    ('AT+IPR=9600', 'port speed'),
    ('AT+CMGS=5', 'sending'),
    ('AT+CMGS=?', 'sending'),
    ('AT+CMGW', 'writing'),
    ('AT+CMSS=1', 'sending'),
    ('AT+CMGD=1,4', 'deleting'),
    ('AT+CLCK="SC",1,"1234"', 'locking'),
    ('AT+CPWD="SC","1","2"', 'passwords'),
    ('AT+CFUN=4,1', 'not allowed'),
    ('AT+CFUN=0,1', 'not allowed'),
    ('AT+CFUN=1,1,1', 'not allowed'),
    ('AT+CRESET=1', 'not allowed'),
    ('AT+CFUN=0', 'PIN'),
    ('AT+CFUN=7', 'not allowed'),
    ('AT+COPS=0', 'not allowed'),
    ('AT+COPS=2', 'not allowed'),
    ('AT+CSMS=2', 'not allowed'),
    ('AT+CNMI=2,1,0,2,0,9', 'not allowed'),
    ('AT+CNMI=a', 'not allowed'),
    ('AT+CNMI=2;', 'single printable'),
    ('AT+CGSN', 'not allowed'),
    ('AT+CIMI', 'not allowed'),
    ('AT+CMGL=4', 'not allowed'),
    ('AT+CMGR=0', 'not allowed'),
    ('AT+CSQ=1', 'not allowed'),
    ('ATV1', 'not allowed'),
    ('AT^SYSINFO', 'not allowed'),
    ('HELLO', 'not an AT command'),
]

SYNTAX = [
    'AT+CSQ;E0',  # chaining would bypass the checks
    'AT+CSQ;+CPIN="1234"',
    'AT+CSQ ; ATZ',
    'AT\r\nATZ',
    'AT+CSQ\x1a',
    'AT+CSQ\x1b',
    'AT+CÉ',
    'AT+CPIN="12',
    '',
    '   ',
    'A' * (MAX_LENGTH + 1),
]


class AtFilterTest(unittest.TestCase):
    def testAllowed(self):
        for typed, sent in ALLOWED:
            verdict = checkAtCommand(typed)
            self.assertTrue(verdict.allowed, f'{typed!r}: {verdict.reason}')
            self.assertEqual(verdict.command, sent, typed)
            self.assertEqual(verdict.reason, '')

    def testRefused(self):
        for typed, fragment in REFUSED:
            verdict = checkAtCommand(typed)
            self.assertFalse(verdict.allowed, typed)
            self.assertIn(fragment, verdict.reason, typed)

    def testSyntax(self):
        for typed in SYNTAX:
            verdict = checkAtCommand(typed)
            self.assertFalse(verdict.allowed, repr(typed))
            self.assertTrue(verdict.reason, repr(typed))

    def testNotAString(self):
        for value in (None, 123, b'AT+CSQ', ['AT'], {}):
            self.assertFalse(checkAtCommand(value).allowed, repr(value))

    def testLongestAllowedCommand(self):
        command = 'AT+CNMI=' + ','.join(['2'] * 5)
        self.assertTrue(len(command) <= MAX_LENGTH)
        self.assertTrue(checkAtCommand(command).allowed)

    def testWhatIsCheckedIsWhatIsSent(self):
        for typed, _ in ALLOWED:
            verdict = checkAtCommand(typed)
            self.assertEqual(normalize(verdict.command), verdict.command)  # nothing changes a second time
            self.assertTrue(checkAtCommand(verdict.command).allowed)

    def testSwitchingTheSimOffNeedsAnSimWithoutPin(self):
        self.assertTrue(checkAtCommand('AT+CFUN=0', pinConfigured=False).allowed)
        self.assertTrue(checkAtCommand('at+cfun = 0', pinConfigured=False).allowed)
        verdict = checkAtCommand('AT+CFUN=0', pinConfigured=True)
        self.assertFalse(verdict.allowed)
        self.assertIn('PIN', verdict.reason)
        self.assertFalse(checkAtCommand('AT+CFUN=0').allowed)  # a PIN is assumed by default
        self.assertFalse(checkAtCommand('AT+CFUN=0,1', pinConfigured=False).allowed)  # no restart into the off state

    def testLowerCaseAndSpacesDoNotBypassTheForbiddenList(self):
        for typed in ('at+cpin="1234"', 'AT + CPIN = "1234"', 'at+cmgs=5', 'AT+ CMGD =1,4', 'atz', 'ate0', 'AT+cfun=4,1'):
            self.assertFalse(checkAtCommand(typed).allowed, typed)

    def testNormalizeKeepsQuotedText(self):
        self.assertEqual(normalize('at+x = "a b;c"'), 'AT+X="a b;c"')  # ';' and spaces are kept inside quotes
        self.assertIsNone(normalize('at+x="a";b'))
        self.assertIsNone(normalize('at+x="a'))


if __name__ == '__main__':
    unittest.main()
