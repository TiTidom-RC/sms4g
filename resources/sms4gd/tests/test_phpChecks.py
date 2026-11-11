""" Code PHP du plugin exécuté sans Jeedom, avec de faux objets Jeedom (scripts de tests/php). Ignorés si php n'est pas
installé. Run from resources/sms4gd:
python -m unittest discover -s tests -t . """

import os
import shutil
import subprocess
import unittest

PHP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'php')


@unittest.skipUnless(shutil.which('php'), 'php non installé')
class PhpChecksTest(unittest.TestCase):
    def assertScriptPasses(self, name):
        result = subprocess.run(['php', os.path.join(PHP_DIR, name)], capture_output=True, text=True, encoding='utf-8',
                                timeout=30, check=False)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, 'OK'), result.stdout + result.stderr)

    def testModemStateLogsTheCauseOfADisconnection(self):
        """ Déconnexion : cause et niveau du log (arrêt du démon, reconnexion abandonnée, erreur définitive), commandes inchangées """
        self.assertScriptPasses('check_modem_state.php')


if __name__ == '__main__':
    unittest.main()
