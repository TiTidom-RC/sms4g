""" Tests of Config. Run from resources/sms4gd:
python -m unittest discover -s tests -t . """

import unittest

from utils import Config


class ConfigTest(unittest.TestCase):
    def testDefaults(self):
        config = Config.fromArgs([])
        self.assertEqual((config.device, config.socketPort, config.cycle, config.pin, config.smsc), (None, 55115, 30.0, None, None))
        self.assertEqual((config.force4g, config.deliveryReport, config.diagnostic, config.smsTtl), (False, False, False, 3600.0))

    def testCommandLineOfDeamonStart(self):
        config = Config.fromArgs([
            '--device', '/dev/serial/by-id/x', '--loglevel', 'debug', '--socketport', '55116', '--serialrate', '115200',
            '--pin', 'None', '--smsc', 'None', '--force4g', 'yes', '--cycle', '45',
            '--deliveryreport', 'yes', '--reconnectbasedelay', '2', '--reconnectmaxdelay', '60', '--reconnectmaxattempts', '4',
            '--concatpartsttl', '120', '--smsttl', '1800', '--diagnostic', 'yes', '--callback', 'http://127.0.0.1:80/cb.php', '--apikey', 'K',
            '--pid', '/tmp/p.pid'])
        self.assertEqual(config.device, '/dev/serial/by-id/x')
        self.assertEqual((config.logLevel, config.socketPort, config.serialRate), ('debug', 55116, 115200))
        self.assertEqual((config.pin, config.smsc, config.force4g, config.deliveryReport), (None, None, True, True))
        self.assertEqual((config.cycle, config.reconnectBaseDelay, config.reconnectMaxDelay, config.reconnectMaxAttempts, config.concatPartsTtl),
                         (45.0, 2.0, 60.0, 4, 120.0))
        self.assertEqual((config.diagnostic, config.callback, config.apikey, config.pidFile), (True, 'http://127.0.0.1:80/cb.php', 'K', '/tmp/p.pid'))
        self.assertEqual(config.smsTtl, 1800.0)

    def testSupervisionSettings(self):
        config = Config.fromArgs([])
        self.assertEqual((config.messagePause, config.selfTestInterval, config.ownNumber, config.autoRestart), (0.0, 0.0, None, False))
        config = Config.fromArgs(['--messagepause', '3', '--selftest', '24', '--ownnumber', '+33767923801', '--autorestart', 'yes'])
        self.assertEqual((config.messagePause, config.selfTestInterval, config.ownNumber, config.autoRestart),
                         (3.0, 24 * 3600.0, '+33767923801', True))

    def testAnSelfTestIntervalBelowTheMinimumIsRaised(self):
        self.assertEqual(Config.fromArgs(['--selftest', '0.2']).selfTestInterval, 3600.0)  # an SMS costs money
        self.assertEqual(Config.fromArgs(['--selftest', '0']).selfTestInterval, 0.0)
        self.assertEqual(Config.fromArgs(['--selftest', '-5']).selfTestInterval, 0.0)

    def testNoSimNumberIsTheTextNone(self):
        self.assertIsNone(Config.fromArgs(['--ownnumber', 'None']).ownNumber)

    def testPinAndSmscAreKeptWhenGiven(self):
        config = Config.fromArgs(['--pin', '1234', '--smsc', '+33695000695', '--diagnostic', 'no'])
        self.assertEqual((config.pin, config.smsc, config.diagnostic), ('1234', '+33695000695', False))


if __name__ == '__main__':
    unittest.main()
