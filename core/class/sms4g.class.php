<?php

/* This file is part of Jeedom.
*
* Jeedom is free software: you can redistribute it and/or modify
* it under the terms of the GNU General Public License as published by
* the Free Software Foundation, either version 3 of the License, or
* (at your option) any later version.
*
* Jeedom is distributed in the hope that it will be useful,
* but WITHOUT ANY WARRANTY; without even the implied warranty of
* MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
* GNU General Public License for more details.
*
* You should have received a copy of the GNU General Public License
* along with Jeedom. If not, see <http://www.gnu.org/licenses/>.
*/

/* * ***************************Includes********************************* */

class sms4g extends eqLogic {
	/*     * ***********************Méthode static*************************** */

	const PYTHON3_PATH = __DIR__ . '/../../resources/venv/bin/python3';
	const PYENV_PATH = '/opt/pyenv/bin/pyenv';

	public static $_encryptConfigKey = array('pin');

	public static function backupExclude() {
		return [
			'resources/venv'
		];
	}

	public static function getPluginVersion() {
		$pluginVersion = '0.0.0';
		$infoFile = dirname(__FILE__) . '/../../plugin_info/info.json';
		if (!file_exists($infoFile)) {
			log::add('sms4g', 'warning', '[Plugin-Version] fichier info.json manquant');
			return $pluginVersion;
		}
		$data = json_decode(file_get_contents($infoFile), true);
		if (is_array($data) && isset($data['pluginVersion'])) {
			$pluginVersion = $data['pluginVersion'];
		}
		return $pluginVersion;
	}

	public static function getPythonVersion() {
		$pythonVersion = '0.0.0';
		try {
			if (file_exists(self::PYTHON3_PATH)) {
				$pythonVersion = exec(system::getCmdSudo() . self::PYTHON3_PATH . " --version 2>&1 | awk '{ print $2 }'");
				config::save('pythonVersion', $pythonVersion, 'sms4g');
			} else {
				log::add('sms4g', 'error', '[Python-Version] Python File :: KO');
			}
		} catch (\Exception $e) {
			log::add('sms4g', 'error', '[Python-Version] Erreur : ' . $e->getMessage());
		}
		log::add('sms4g', 'info', '[Python-Version] PythonVersion :: ' . $pythonVersion);
		return $pythonVersion;
	}

	public static function getPyEnvVersion() {
		$pyenvVersion = '0.0.0';
		try {
			if (file_exists(self::PYENV_PATH)) {
				$pyenvVersion = exec(system::getCmdSudo() . self::PYENV_PATH . " --version | awk '{ print $2 }'");
				config::save('pyenvVersion', $pyenvVersion, 'sms4g');
			} elseif (file_exists(self::PYTHON3_PATH)) {
				$pythonPyEnvInUse = (exec(system::getCmdSudo() . 'dirname $(readlink ' . self::PYTHON3_PATH . ') | grep -Ewc "opt/pyenv"') == 1) ? true : false;
				if (!$pythonPyEnvInUse) {
					$pyenvVersion = "-";
					config::save('pyenvVersion', $pyenvVersion, 'sms4g');
				}
			} else {
				log::add('sms4g', 'error', '[PyEnv-Version] PyEnv File :: KO');
			}
		} catch (\Exception $e) {
			log::add('sms4g', 'error', '[PyEnv-Version] Erreur : ' . $e->getMessage());
		}
		log::add('sms4g', 'info', '[PyEnv-Version] PyEnvVersion :: ' . $pyenvVersion);
		return $pyenvVersion;
	}

	/**
	 * Vérifie l'état de ModemManager (service système de gestion des modems pour l'accès Internet,
	 * sans rapport avec l'envoi/réception de SMS, mais qui peut
	 * entrer en conflit avec l'accès au port série du modem lors d'un (re)branchement USB).
	 *
	 * @return array{installed: bool, active: bool, enabled: bool}
	 */
	public static function checkModemManagerStatus() {
		$result = array('installed' => false, 'active' => false, 'enabled' => false);
		$unitFiles = exec(system::getCmdSudo() . 'systemctl list-unit-files ModemManager.service 2>/dev/null | grep -c ModemManager');
		$result['installed'] = (trim((string) $unitFiles) !== '0' && $unitFiles !== false);
		if (!$result['installed']) {
			return $result;
		}
		$activeState = trim((string) exec(system::getCmdSudo() . 'systemctl is-active ModemManager 2>/dev/null'));
		$result['active'] = ($activeState === 'active');
		$enabledState = trim((string) exec(system::getCmdSudo() . 'systemctl is-enabled ModemManager 2>/dev/null'));
		$result['enabled'] = ($enabledState === 'enabled');
		return $result;
	}

	/**
	 * Désactive et arrête ModemManager (systemctl disable --now).
	 *
	 * @return bool true si la commande s'est terminée avec succès
	 */
	public static function disableModemManager() {
		exec(system::getCmdSudo() . 'systemctl disable --now ModemManager 2>&1', $output, $returnVar);
		log::add('sms4g', 'info', '[ModemManager][Désactivation] ' . implode(' | ', $output));
		return $returnVar === 0;
	}

	public static function getPythonDepFromRequirements() {
		$pythonDepString = '';
		$pythonDepNum = 0;
		try {
			if (!file_exists(dirname(__FILE__) . '/../../resources/requirements.txt')) {
				log::add('sms4g', 'error', '[Python-Dep] Fichier requirements.txt manquant');
				config::save('pythonDepString', $pythonDepString, 'sms4g');
				config::save('pythonDepNum', $pythonDepNum, 'sms4g');
				return false;
			}
			$data = file_get_contents(dirname(__FILE__) . '/../../resources/requirements.txt');
			if (!is_string($data)) {
				log::add('sms4g', 'error', '[Python-Dep] Impossible de lire le fichier requirements.txt');
				config::save('pythonDepString', $pythonDepString, 'sms4g');
				config::save('pythonDepNum', $pythonDepNum, 'sms4g');
				return false;
			}
			$lines = explode("\n", $data);
			$packages = array();
			foreach ($lines as $line) {
				$line = trim($line);
				if ($line === '' || str_starts_with($line, '#')) {
					continue; // Ignore les lignes vides et les commentaires
				}
				// Retire les extras [async], [dev], etc.
				$line = preg_replace('/\[[^\]]*\]/', '', $line);
				// Normalise le nom du package pour regex : remplace - ou _ par [-_] (pour supporter les incohérences pip)
				if (preg_match('/^([a-zA-Z0-9_-]+)(.*)$/', $line, $matches)) {
					$packageName = preg_replace('/[-_]/', '[-_]', $matches[1]);
					$versionPart = $matches[2]; // ==0.4.4, >=1.0, etc.
					$packages[] = $packageName . $versionPart;
				}
			}
			$packages = array_unique($packages);
			$pythonDepString = join("|", $packages);
			$pythonDepNum = count($packages);

			if (config::byKey('pythonDepString', 'sms4g') != $pythonDepString) {
				config::save('pythonDepString', $pythonDepString, 'sms4g');
			}
			if (config::byKey('pythonDepNum', 'sms4g', 0) != $pythonDepNum) {
				config::save('pythonDepNum', $pythonDepNum, 'sms4g');
			}
		} catch (\Exception $e) {
			log::add('sms4g', 'debug', '[Python-Dep] Get requirements.txt ERROR :: ' . $e->getMessage());
			return false;
		}
		log::add('sms4g', 'debug', '[Python-Dep] Validated dependencies regex : ' . $pythonDepString . " / Expected packages count : " . $pythonDepNum);

		return true;
	}

	public static function dependancy_install() {
		log::remove(__CLASS__ . '_update');

		$script_sysUpdates = 0;
		$script_restorePyEnv = 0;
		$script_restoreVenv = 0;

		if (config::byKey('debugInstallUpdates', 'sms4g') === '1') {
			$script_sysUpdates = 1;
			config::save('debugInstallUpdates', '0', 'sms4g');
		}
		if (config::byKey('debugRestorePyEnv', 'sms4g') === '1') {
			$script_restorePyEnv = 1;
			config::save('debugRestorePyEnv', '0', 'sms4g');
		}
		if (config::byKey('debugRestoreVenv', 'sms4g') === '1') {
			$script_restoreVenv = 1;
			config::save('debugRestoreVenv', '0', 'sms4g');
		}

		return array('script' => __DIR__ . '/../../resources/install_#stype#.sh ' . jeedom::getTmpFolder(__CLASS__) . '/dependency' . ' ' . $script_sysUpdates . ' ' . $script_restorePyEnv . ' ' . $script_restoreVenv, 'log' => log::getPathToLog(__CLASS__ . '_update'));
	}

	public static function dependancy_info() {
		$return = array();
		$return['log'] = log::getPathToLog(__CLASS__ . '_update');
		$return['progress_file'] = jeedom::getTmpFolder(__CLASS__) . '/dependency';

		if (file_exists(jeedom::getTmpFolder(__CLASS__) . '/dependency')) {
			$return['state'] = 'in_progress';
		} else {
			if (exec(system::getCmdSudo() . system::get('cmd_check') . '-Ec "python3\-requests|python3\-setuptools|python3\-dev|python3\-venv"') < 4) {
				$return['state'] = 'nok';
				log::add(__CLASS__, 'debug', '[Python-Dep] System packages missing (python3-requests, python3-setuptools, python3-dev, or python3-venv)');
			} elseif (!file_exists(self::PYTHON3_PATH)) {
				$return['state'] = 'nok';
				log::add(__CLASS__, 'debug', '[Python-Dep] Python venv executable not found at: ' . self::PYTHON3_PATH);
			} else {
				$expectedCount = config::byKey('pythonDepNum', 'sms4g', 0, true);
				$pythonDepString = config::byKey('pythonDepString', 'sms4g', '', true);

				$cmd = self::PYTHON3_PATH . ' -m pip --no-cache-dir freeze | grep -Ewci "' . $pythonDepString . '"';
				$foundCount = exec($cmd);

				if ($foundCount < $expectedCount) {
					$return['state'] = 'nok';
					log::add(__CLASS__, 'debug', '[Python-Dep] Missing Dependencies. Found: ' . $foundCount . ' / Expected: ' . $expectedCount);
					log::add(__CLASS__, 'debug', '[Python-Dep] Regex used: ' . $pythonDepString);
					$pipFreeze = shell_exec(self::PYTHON3_PATH . ' -m pip --no-cache-dir freeze');
					log::add(__CLASS__, 'debug', '[Python-Dep] Pip Freeze Output: ' . str_replace(PHP_EOL, ' | ', trim($pipFreeze)));
				} else {
					$return['state'] = 'ok';
					log::add(__CLASS__, 'debug', '[Python-Dep] Dependencies installed. State : OK');
				}
			}
		}
		return $return;
	}

	public static function deamon_info() {
		$return = array();
		$return['log'] = 'sms4g';
		$return['state'] = 'nok';
		$pid_file = jeedom::getTmpFolder('sms4g') . '/deamon.pid';
		if (file_exists($pid_file)) {
			if (@posix_getsid(trim(file_get_contents($pid_file)))) {
				$return['state'] = 'ok';
			} else {
				shell_exec(system::getCmdSudo() . 'rm -rf ' . $pid_file . ' 2>&1 > /dev/null');
			}
		}
		$return['launchable'] = 'ok';
		$port = config::byKey('port', 'sms4g');
		if ($port == 'none' || $port == '') {
			$return['launchable'] = 'nok';
			$return['launchable_message'] = __("Le port n'est pas configuré", __FILE__);
		} else {
			$port = jeedom::getUsbMapping($port);
			if (is_string($port)) {
				if (@!file_exists($port)) {
					$return['launchable'] = 'nok';
					$return['launchable_message'] = __("Le port n'est pas configuré", __FILE__);
				}
				exec(system::getCmdSudo() . 'chmod 777 ' . $port . ' > /dev/null 2>&1');
			}
		}
		return $return;
	}

	public static function deamon_start() {
		self::deamon_stop();
		self::getPyEnvVersion();
		self::getPythonVersion();
		$deamon_info = self::deamon_info();
		if ($deamon_info['launchable'] != 'ok') {
			throw new Exception(__('Veuillez vérifier la configuration', __FILE__));
		}
		try {
			self::manageModemEquipment();
		} catch (\Exception $e) {
			log::add('sms4g', 'error', '[MODEM] Création de l\'équipement virtuel Modem impossible : ' . $e->getMessage());
		}
		$port = config::byKey('port', 'sms4g');
		$port = jeedom::getUsbMapping($port);
		$sms_path = realpath(__DIR__ . '/../../resources/sms4gd');
		$cmd = self::PYTHON3_PATH . " {$sms_path}/sms4gd.py";
		$cmd .= ' --device ' . $port;
		$cmd .= ' --loglevel ' . log::convertLogLevel(log::getLogLevel('sms4g'));
		$cmd .= ' --socketport ' . config::byKey('socketport', 'sms4g');
		$cmd .= ' --serialrate ' . config::byKey('serialRate', 'sms4g');
		$cmd .= ' --pin ' . config::byKey('pin', 'sms4g', 'None');
		$cmd .= ' --smsc ' . config::byKey('smsc', 'sms4g', 'None');
		$cmd .= ' --force4g ' . ((config::byKey('force4gOnly', 'sms4g') == 1) ? 'yes' : 'no');
		$cmd .= ' --diagnostic ' . ((config::byKey('diagMode', 'sms4g', 0) == 1) ? 'yes' : 'no');
		$cmd .= ' --cycle ' . config::byKey('cycle', 'sms4g');
		$cmd .= ' --deliveryreport ' . ((config::byKey('deliveryReport', 'sms4g', 0) == 1) ? 'yes' : 'no');
		$cmd .= ' --reconnectbasedelay ' . config::byKey('reconnectBaseDelay', 'sms4g', 5);
		$cmd .= ' --reconnectmaxdelay ' . config::byKey('reconnectMaxDelay', 'sms4g', 300);
		$cmd .= ' --reconnectmaxattempts ' . config::byKey('reconnectMaxAttempts', 'sms4g', 10);
		$cmd .= ' --concatpartsttl ' . config::byKey('concatPartsTtl', 'sms4g', 300);
		$cmd .= ' --callback ' . network::getNetworkAccess('internal', 'http:127.0.0.1:port:comp') . '/plugins/sms4g/core/php/jeesms4g.php';
		$cmd .= ' --apikey ' . jeedom::getApiKey('sms4g');
		$cmd .= ' --pid ' . jeedom::getTmpFolder('sms4g') . '/deamon.pid';
		// Masque apikey/pin uniquement dans le log (la commande exécutée ci-dessous garde les vraies valeurs)
		log::add('sms4g', 'info', 'Lancement démon sms4g : ' . preg_replace('/(--apikey|--pin)\s+\S+/', '$1 ***', $cmd));
		$result = exec($cmd . ' >> ' . log::getPathToLog('sms4gd') . ' 2>&1 &');
		$i = 0;
		while ($i < 30) {
			$deamon_info = self::deamon_info();
			if ($deamon_info['state'] == 'ok') {
				break;
			}
			sleep(1);
			$i++;
		}
		if ($i >= 30) {
			log::add('sms4g', 'error', 'Impossible de lancer le démon sms4g, vérifiez le port', 'unableStartDeamon');
			return false;
		}
		message::removeAll('sms4g', 'unableStartDeamon');
		return true;
	}

	public static function deamon_stop() {
		$pid_file = jeedom::getTmpFolder('sms4g') . '/deamon.pid';
		if (file_exists($pid_file)) {
			$pid = intval(trim(file_get_contents($pid_file)));
			if ($pid > 0) {
				system::kill($pid, false); // SIGTERM seul, sans SIGKILL immédiat
				for ($i = 0; $i < 30 && file_exists($pid_file); $i++) {
					usleep(100000); // Attend max 3s que le processus meure
				}
			}
			@unlink($pid_file);
		}
		system::kill('sms4gd.py'); // SIGKILL de sécurité si zombie
		system::fuserk(config::byKey('socketport', 'sms4g'));
		$port = config::byKey('port', 'sms4g');
		if ($port != 'none' && $port != '') {
			system::fuserk(jeedom::getUsbMapping($port));
		}
	}

	/**
	 * Équipement virtuel « Modem » : crée l'équipement s'il n'existe pas, puis ses commandes manquantes
	 * (installation, mise à jour, démarrage du démon). Le Modem n'est pas un équipement SMS : il n'a ni numéros ni
	 * interactions, ses commandes sont toutes créées ici.
	 */
	public static function manageModemEquipment() {
		$modem = self::byLogicalId('modem', 'sms4g');
		if (!is_object($modem)) {
			log::add('sms4g', 'info', '[MODEM] Création de l\'équipement virtuel Modem');
			$modem = new sms4g();
			$modem->setLogicalId('modem');
			$modem->setName('Modem');
			$modem->setEqType_name('sms4g');
			$modem->setIsEnable(1);
			$modem->setIsVisible(1);
			$modem->save();
		}

		$orderCmd = 1;

		// online (info/binary) : 1 seulement quand le modem est connecté et enregistré sur le réseau
		$cmd = $modem->getCmd(null, 'online');
		if (!is_object($cmd)) {
			$cmd = new sms4gCmd();
			$cmd->setName(__('En Ligne', __FILE__));
			$cmd->setEqLogic_id($modem->getId());
			$cmd->setLogicalId('online');
			$cmd->setType('info');
			$cmd->setSubType('binary');
			$cmd->setIsVisible(1);
			$cmd->setIsHistorized(1);
			$cmd->setConfiguration('repeatEventManagement', 'always');
			$cmd->setOrder($orderCmd++);
			$cmd->save();
		} else {
			$orderCmd++;
		}

		// signal (info/numeric)
		$cmd = $modem->getCmd(null, 'signal');
		if (!is_object($cmd)) {
			$cmd = new sms4gCmd();
			$cmd->setName(__('Signal', __FILE__));
			$cmd->setEqLogic_id($modem->getId());
			$cmd->setLogicalId('signal');
			$cmd->setType('info');
			$cmd->setSubType('numeric');
			$cmd->setIsVisible(1);
			$cmd->setTemplate('dashboard', 'core::tile');
			$cmd->setTemplate('mobile', 'core::tile');
			$cmd->setOrder($orderCmd++);
			$cmd->save();
		} else {
			$orderCmd++;
		}

		// connection (info/string)
		$cmd = $modem->getCmd(null, 'connection');
		if (!is_object($cmd)) {
			$cmd = new sms4gCmd();
			$cmd->setName(__('Connexion', __FILE__));
			$cmd->setEqLogic_id($modem->getId());
			$cmd->setLogicalId('connection');
			$cmd->setType('info');
			$cmd->setSubType('string');
			$cmd->setIsVisible(1);
			$cmd->setTemplate('dashboard', 'core::line');
			$cmd->setTemplate('mobile', 'core::line');
			$cmd->setDisplay('forceReturnLineBefore', 1);
			$cmd->setDisplay('forceReturnLineAfter', 1);
			$cmd->setOrder($orderCmd++);
			$cmd->save();
		} else {
			$orderCmd++;
		}

		// operator (info/string)
		$cmd = $modem->getCmd(null, 'operator');
		if (!is_object($cmd)) {
			$cmd = new sms4gCmd();
			$cmd->setName(__('Opérateur', __FILE__));
			$cmd->setEqLogic_id($modem->getId());
			$cmd->setLogicalId('operator');
			$cmd->setType('info');
			$cmd->setSubType('string');
			$cmd->setIsVisible(0);
			$cmd->setTemplate('dashboard', 'core::line');
			$cmd->setTemplate('mobile', 'core::line');
			$cmd->setDisplay('forceReturnLineBefore', 1);
			$cmd->setDisplay('forceReturnLineAfter', 1);
			$cmd->setOrder($orderCmd++);
			$cmd->save();
		} else {
			$orderCmd++;
		}

		// connection_state (info/numeric) : 0 déconnecté, 1 reconnexion, 2 recherche opérateur, 3 connexion, 4 connecté
		$cmd = $modem->getCmd(null, 'connection_state');
		if (!is_object($cmd)) {
			$cmd = new sms4gCmd();
			$cmd->setName(__('Connexion (Code)', __FILE__));
			$cmd->setEqLogic_id($modem->getId());
			$cmd->setLogicalId('connection_state');
			$cmd->setType('info');
			$cmd->setSubType('numeric');
			$cmd->setIsVisible(0);
			$cmd->setIsHistorized(1);
			// Chaque changement d'état est historisé, même à valeur identique (plusieurs tentatives de reconnexion)
			$cmd->setConfiguration('repeatEventManagement', 'always');
			$cmd->setTemplate('dashboard', 'core::tile');
			$cmd->setTemplate('mobile', 'core::tile');
			$cmd->setOrder($orderCmd++);
			$cmd->save();
		} else {
			$orderCmd++;
		}

		// at_command (action/message) : le message est la commande AT, le titre est le délai en secondes (facultatif)
		$cmd = $modem->getCmd(null, 'at_command');
		if (!is_object($cmd)) {
			$cmd = new sms4gCmd();
			$cmd->setName(__('Commande AT', __FILE__));
			$cmd->setEqLogic_id($modem->getId());
			$cmd->setLogicalId('at_command');
			$cmd->setType('action');
			$cmd->setSubType('message');
			$cmd->setIsVisible(0);
			$cmd->setConfiguration('interact::auto::disable', 1);
			$cmd->setDisplay('title_placeholder', __('Délai en secondes (facultatif)', __FILE__));
			$cmd->setDisplay('message_placeholder', __('Commande AT', __FILE__));
			$cmd->setOrder($orderCmd++);
			$cmd->save();
		} else {
			$orderCmd++;
		}

		// at_status (info/string) : commande et code de fin de la console de diagnostic AT, toujours notifié même identique
		$cmd = $modem->getCmd(null, 'at_status');
		if (!is_object($cmd)) {
			$cmd = new sms4gCmd();
			$cmd->setName(__('Statut AT', __FILE__));
			$cmd->setEqLogic_id($modem->getId());
			$cmd->setLogicalId('at_status');
			$cmd->setType('info');
			$cmd->setSubType('string');
			$cmd->setIsVisible(0);
			$cmd->setConfiguration('repeatEventManagement', 'always');
			$cmd->setConfiguration('interact::auto::disable', 1);
			$cmd->setDisplay('forceReturnLineBefore', 1);
			$cmd->setDisplay('forceReturnLineAfter', 1);
			$cmd->setOrder($orderCmd++);
			$cmd->save();
		} else {
			$orderCmd++;
		}
		// at_response (info/string) : lignes d'information de la réponse du modem (sans le code de fin), toujours notifiées même identiques
		$cmd = $modem->getCmd(null, 'at_response');
		if (!is_object($cmd)) {
			$cmd = new sms4gCmd();
			$cmd->setName(__('Réponse AT', __FILE__));
			$cmd->setEqLogic_id($modem->getId());
			$cmd->setLogicalId('at_response');
			$cmd->setType('info');
			$cmd->setSubType('string');
			$cmd->setIsVisible(0);
			$cmd->setConfiguration('repeatEventManagement', 'always');
			$cmd->setConfiguration('interact::auto::disable', 1);
			$cmd->setDisplay('forceReturnLineBefore', 1);
			$cmd->setDisplay('forceReturnLineAfter', 1);
			$cmd->setOrder($orderCmd++);
			$cmd->save();
		} else {
			$orderCmd++;
		}
	}

	/**
	 * Envoie un message au démon par sa socket (protocole : apikey + cmd + paramètres).
	 *
	 * @param array $_payload
	 * @return bool false si le démon n'a pas pu être joint
	 */
	public static function sendToDaemon($_payload) {
		try {
			$_payload['apikey'] = jeedom::getApiKey('sms4g');
			$value = json_encode($_payload);
			$socket = socket_create(AF_INET, SOCK_STREAM, 0);
			if (@socket_connect($socket, '127.0.0.1', config::byKey('socketport', 'sms4g', '55115')) === false) {
				throw new Exception('socket_connect (démon arrêté ?) :: ' . socket_strerror(socket_last_error($socket)));
			}
			if (@socket_write($socket, $value, strlen($value)) === false) {
				throw new Exception('socket_write :: ' . socket_strerror(socket_last_error($socket)));
			}
			socket_close($socket);
			return true;
		} catch (\Exception $e) {
			log::add('sms4g', 'error', '[SOCKET][SendToDaemon] Exception :: ' . $e->getMessage());
			return false;
		}
	}

	/**
	 * Envoie un SMS à chaque numéro. Le découpage en parties/groupes SMS (encodage GSM-7 ou UCS-2, limite de parties
	 * liées) est entièrement géré côté démon Python, seul à connaître l'encodage réel du message : on lui transmet
	 * juste le texte complet.
	 *
	 * @param string[] $_phonenumbers
	 * @param string $_message
	 * @return bool false si un envoi au démon a échoué
	 */
	public static function sendSms($_phonenumbers, $_message) {
		$message = trim($_message);
		$phonenumbers = array_filter(array_map('trim', $_phonenumbers), 'strlen');
		if (count($phonenumbers) == 0 || $message == '') {
			log::add('sms4g', 'warning', '[SMS] Envoi ignoré : numéro ou message vide');
			return false;
		}
		$maxPartsPerGroup = (int) config::byKey('maxSmsPartsPerGroup', 'sms4g');
		foreach ($phonenumbers as $phonenumber) {
			if (!self::sendToDaemon(array('cmd' => 'sendSms', 'number' => $phonenumber, 'message' => $message, 'maxPartsPerGroup' => $maxPartsPerGroup))) {
				return false;
			}
		}
		return true;
	}

	/**
	 * Commande AT du mode diagnostic. Rend la main aussitôt : le résultat arrive plus tard dans les commandes
	 * « Statut AT » et « Réponse AT » (onAtResponse). Ici seul le mode diagnostic est contrôlé : le format et le
	 * filtre des commandes autorisées sont ceux du démon, qui fait foi (son refus arrive dans « Statut AT »).
	 *
	 * @param string $_command commande AT
	 * @param mixed $_timeout délai en secondes (facultatif, le démon le borne)
	 * @return bool
	 */
	public static function sendAtCommand($_command, $_timeout = '') {
		$modem = self::byLogicalId('modem', 'sms4g');
		if (!is_object($modem)) {
			log::add('sms4g', 'debug', '[AT] Équipement virtuel Modem non trouvé');
			return false;
		}
		$command = trim((string) $_command);
		if (config::byKey('diagMode', 'sms4g', 0) != 1) {
			log::add('sms4g', 'warning', '[AT] Commande refusée : le mode diagnostic est désactivé');
			self::setAtResult($modem, $command, 'refused', 'diagnostic mode is disabled', '');
			return false;
		}
		$payload = array('cmd' => 'atCommand', 'id' => bin2hex(random_bytes(8)), 'command' => $command);
		if (is_numeric($_timeout) && $_timeout > 0) {
			$payload['timeout'] = (float) $_timeout;
		}
		log::add('sms4g', 'info', '[AT] Commande envoyée au démon : ' . self::maskPhoneNumbers($command));
		if (!self::sendToDaemon($payload)) {
			self::setAtResult($modem, $command, 'notConnected', 'daemon not reachable', '');
			return false;
		}
		return true;
	}

	/**
	 * Écrit « Réponse AT » puis « Statut AT » (commande, flèche, code de fin en majuscules, détail éventuel entre
	 * parenthèses). La réponse passe en premier : un scénario déclenché par le statut trouve toujours une réponse
	 * complète. Les textes sont protégés des balises HTML : ils peuvent venir du modem, ou d'une commande saisie par
	 * une IA ; les guillemets restent lisibles (+COPS: 0,0,"Free Free",7).
	 */
	private static function setAtResult($_modem, $_command, $_status, $_detail, $_response, $_time = null) {
		$labels = array('ok' => 'OK', 'error' => 'ERROR', 'timeout' => 'TIMEOUT', 'refused' => 'REFUSED', 'notConnected' => 'NOT CONNECTED');
		$label = isset($labels[$_status]) ? $labels[$_status] : strtoupper($_status);
		$status = $_command . ' → ' . $label;
		if ($_detail != '') {
			$status .= ' (' . $_detail . ')';
		}
		// Jamais vide : sans ligne du modem (refus, démon arrêté, délai dépassé sans réponse), la réponse porte le code de fin
		if ($_response === '') {
			$_response = $label;
		}
		$_modem->checkAndUpdateCmd('at_response', htmlspecialchars($_response, ENT_NOQUOTES | ENT_SUBSTITUTE, 'UTF-8'), $_time);
		$_modem->checkAndUpdateCmd('at_status', htmlspecialchars($status, ENT_NOQUOTES | ENT_SUBSTITUTE, 'UTF-8'), $_time);
	}

	/**
	 * @param mixed $_id identifiant du message
	 * @return bool true si ce message a déjà été traité. Il est marqué dès maintenant (et non après son traitement) :
	 *              un renvoi qui arrive pendant que le premier tourne encore est ignoré lui aussi.
	 */
	public static function isDuplicateMessage($_id) {
		if (!is_scalar($_id) || $_id === '') {
			return false;
		}
		$key = 'sms4g::message::' . preg_replace('/[^0-9a-zA-Z]/', '', (string) $_id);
		if (cache::byKey($key)->getValue(null) !== null) {
			return true;
		}
		// Mémorise l'identifiant 10 minutes pour détecter les renvois du démon
		cache::set($key, 1, 600);
		return false;
	}

	/**
	 * Handlers des messages du démon (aiguillés par jeesms4g.php) : chacun met à jour l'équipement virtuel Modem.
	 * $_time : date du message côté démon (pour l'historique), ou null.
	 */
	public static function onModemState($_modem, $_message, $_time) {
		$state = isset($_message['state']) ? $_message['state'] : '';
		$attempt = isset($_message['attempt']) ? (int) $_message['attempt'] : 0;
		$maxAttempts = isset($_message['maxAttempts']) ? (int) $_message['maxAttempts'] : 0;
		// connection_state : échelle de 0 (déconnecté) à 4 (connecté) ; online = 1 seulement quand le modem est connecté et enregistré
		switch ($state) {
			case 'connecting':
				$text = __('Connexion en cours', __FILE__);
				$code = 3;
				$online = 0;
				break;
			case 'connected':
				$text = __('Connecté', __FILE__);
				$code = 4;
				$online = 1;
				break;
			case 'searching':
				$text = __('Recherche opérateur', __FILE__);
				$code = 2;
				$online = 0;
				break;
			case 'reconnecting':
				// Deux états par tentative : l'annonce (avec le délai d'attente), puis la tentative elle-même
				if (isset($_message['retryIn'])) {
					$text = __('Reconnexion dans', __FILE__) . ' ' . (int) round($_message['retryIn']) . ' s (' . $attempt . '/' . $maxAttempts . ')';
				} else {
					$text = __('Reconnexion', __FILE__) . ' ' . $attempt . '/' . $maxAttempts;
				}
				$code = 1;
				$online = 0;
				break;
			case 'disconnected':
				$text = __('Déconnecté', __FILE__);
				$code = 0;
				$online = 0;
				break;
			default:
				log::add('sms4g', 'warning', '[CALLBACK] État du modem inconnu : ' . secureXSS($state));
				return;
		}
		log::add('sms4g', 'info', '[MODEM] ' . $text);
		$_modem->checkAndUpdateCmd('connection', $text, $_time);
		$_modem->checkAndUpdateCmd('connection_state', $code, $_time);
		$_modem->checkAndUpdateCmd('online', $online, $_time);

		if ($state == 'disconnected' && !empty($_message['fatal'])) {
			$reason = isset($_message['reason']) ? secureXSS($_message['reason']) : '';
			message::add('sms4g', __('Erreur du modem', __FILE__) . ' : ' . $reason, '', 'sms4gcmderror');
			// Un nouveau démarrage ne corrigerait pas ces erreurs (un PIN incorrect répété bloque même la SIM)
			if (isset($_message['errorType']) && in_array($_message['errorType'], array('PinRequiredError', 'IncorrectPinError', 'PukRequiredError', 'PduModeNotSupportedError'))) {
				log::add('sms4g', 'error', '[MODEM] Erreur définitive (code PIN, mode PDU) : redémarrage automatique du démon désactivé');
				config::save('deamonAutoMode', 0, 'sms4g');
			}
		}
	}

	public static function onSignal($_modem, $_message, $_time) {
		$value = isset($_message['value']) ? (int) $_message['value'] : -1;
		$_modem->checkAndUpdateCmd('signal', $value, $_time);
	}

	public static function onNetwork($_modem, $_message, $_time) {
		$operator = (isset($_message['operator']) && $_message['operator'] !== null) ? (string) $_message['operator'] : '';
		$_modem->checkAndUpdateCmd('operator', $operator, $_time);
	}

	public static function onAtResponse($_modem, $_message, $_time) {
		$command = isset($_message['command']) ? (string) $_message['command'] : '';
		$status = isset($_message['status']) ? (string) $_message['status'] : 'error';
		$lines = (isset($_message['lines']) && is_array($_message['lines'])) ? $_message['lines'] : array();
		$detail = ($status != 'ok' && !empty($_message['error'])) ? (string) $_message['error'] : '';
		$truncated = !empty($_message['truncated']);
		log::add('sms4g', 'info', '[AT] ' . self::maskPhoneNumbers($command) . ' → ' . $status);
		log::add('sms4g', 'debug', '[AT] Réponse : ' . self::maskPhoneNumbers(implode(' | ', $lines)));
		// Délai dépassé : les lignes reçues sont incomplètes, elles restent dans le log (la réponse portera TIMEOUT)
		if ($status == 'timeout') {
			$lines = array();
			$truncated = false;
		}
		// Le code de fin du modem (OK, +CME ERROR…) est dans le statut : la réponse ne le répète que s'il est seul
		if (!$truncated && in_array($status, array('ok', 'error')) && count($lines) > 1) {
			array_pop($lines);
		}
		$response = implode("\n", $lines);
		if ($truncated) {
			$response = trim($response . "\n[…]");
		}
		self::setAtResult($_modem, $command, $status, $detail, $response, $_time);
	}

	/**
	 * Masque les numéros de téléphone d'un texte de log (même règle que le démon : 4 premiers et 2 derniers caractères).
	 */
	private static function maskPhoneNumbers($_text) {
		return preg_replace_callback('/\+\d{6,15}/', function ($match) {
			return substr($match[0], 0, 4) . str_repeat('X', strlen($match[0]) - 6) . substr($match[0], -2);
		}, $_text);
	}

	/*     * *********************Méthode d'instance************************* */
	public function preSave() {
		if ($this->getConfiguration('allowUnknownOrigin', 0) == 0) {
			$this->setConfiguration('autoAddNewNumber', 0);
		}
	}

	public function postSave() {
		// Équipement virtuel Modem : ses commandes sont gérées par manageModemEquipment()
		if ($this->getLogicalId() == 'modem') {
			return;
		}
		$sms = $this->getCmd(null, 'sms');
		if (!is_object($sms)) {
			$sms = new sms4gCmd();
			$sms->setEqLogic_id($this->getId());
			$sms->setLogicalId('sms');
			$sms->setIsVisible(0);
			$sms->setName(__('Message', __FILE__));
			$sms->setTemplate('dashboard', 'core::line');
			$sms->setTemplate('mobile', 'core::line');
			$sms->setDisplay('forceReturnLineBefore', 1);
			$sms->setDisplay('forceReturnLineAfter', 1);
		}
		$sms->setType('info');
		$sms->setSubType('string');
		$sms->save();

		$sender = $this->getCmd(null, 'sender');
		if (!is_object($sender)) {
			$sender = new sms4gCmd();
			$sender->setEqLogic_id($this->getId());
			$sender->setLogicalId('sender');
			$sender->setIsVisible(0);
			$sender->setName(__('Expéditeur', __FILE__));
			$sender->setTemplate('dashboard', 'core::line');
			$sender->setTemplate('mobile', 'core::line');
			$sender->setDisplay('forceReturnLineBefore', 1);
			$sender->setDisplay('forceReturnLineAfter', 1);
		}
		$sender->setType('info');
		$sender->setSubType('string');
		$sender->save();

		$customNumber = $this->getCmd(null, 'send_to_custom_number');
		if (!is_object($customNumber)) {
			$customNumber = new sms4gCmd();
			$customNumber->setEqLogic_id($this->getId());
			$customNumber->setLogicalId('send_to_custom_number');
			$customNumber->setIsVisible(0);
			$customNumber->setName(__('Envoyer message à', __FILE__));
			$customNumber->setType('action');
			$customNumber->setSubType('message');
			$customNumber->setDisplay('title_placeholder', __('Numéro', __FILE__));
			$customNumber->save();
		}
	}
}

class sms4gCmd extends cmd {
	/*     * *************************Attributs****************************** */

	/*     * ***********************Méthode static*************************** */

	/*     * *********************Méthode d'instance************************* */

	public function dontRemoveCmd() {
		// Équipement virtuel Modem : seules les commandes créées par le plugin sont protégées (les anciennes
		// commandes signal / connexion des équipements SMS se suppriment à la main)
		$eqLogic = $this->getEqLogic();
		if (is_object($eqLogic) && $eqLogic->getLogicalId() == 'modem') {
			return in_array($this->getLogicalId(), array('signal', 'operator', 'connection', 'connection_state', 'online', 'at_status', 'at_response', 'at_command'));
		}
		if (str_starts_with($this->getLogicalId(), 'delivery_status_') || str_starts_with($this->getLogicalId(), 'delivery_success_')) {
			return true;
		}
		return false;
	}

	/**
	 * Crée les commandes compagnon de cette commande d'action de type message, si elles n'existent
	 * pas déjà : "Statut" (texte, pour affichage) et "Remis" (binaire 1/0, pour condition de scénario).
	 *
	 * @return void
	 */
	public function postSave() {
		if ($this->getType() != 'action' || $this->getSubType() != 'message' || $this->getLogicalId() == 'at_command') {
			return;
		}
		$eqLogic = $this->getEqLogic();
		// send_to_custom_number n'a pas de destinataire fixe : la destination réelle est
		// affichée dans la valeur des commandes (cf jeesms4g.php), pas dans leur nom
		$label = ($this->getLogicalId() == 'send_to_custom_number') ? 'Custom' : $this->getName();

		$statusLogicalId = 'delivery_status_' . $this->getId();
		if (!is_object($eqLogic->getCmd(null, $statusLogicalId))) {
			$deliveryStatus = new sms4gCmd();
			$deliveryStatus->setEqLogic_id($this->getEqLogic_id());
			$deliveryStatus->setLogicalId($statusLogicalId);
			$deliveryStatus->setIsVisible(0);
			$deliveryStatus->setType('info');
			$deliveryStatus->setSubType('string');
			$deliveryStatus->setName(__('Statut', __FILE__) . ' - ' . $label);
			$deliveryStatus->setTemplate('dashboard', 'core::line');
			$deliveryStatus->setTemplate('mobile', 'core::line');
			$deliveryStatus->setDisplay('forceReturnLineBefore', 1);
			$deliveryStatus->setDisplay('forceReturnLineAfter', 1);
			$deliveryStatus->save();
		}

		$successLogicalId = 'delivery_success_' . $this->getId();
		if (!is_object($eqLogic->getCmd(null, $successLogicalId))) {
			$deliverySuccess = new sms4gCmd();
			$deliverySuccess->setEqLogic_id($this->getEqLogic_id());
			$deliverySuccess->setLogicalId($successLogicalId);
			$deliverySuccess->setIsVisible(0);
			$deliverySuccess->setType('info');
			$deliverySuccess->setSubType('binary');
			$deliverySuccess->setName(__('Remis', __FILE__) . ' - ' . $label);
			$deliverySuccess->setTemplate('dashboard', 'core::icon');
			$deliverySuccess->setTemplate('mobile', 'core::icon');
			// Sans ça, un envoi qui repasse à la même valeur (souvent 1) ne génère pas d'événement
			$deliverySuccess->setConfiguration('repeatEventManagement', 'always');
			$deliverySuccess->save();
		}
	}

	/**
	 * Supprime la commande compagnon "accusé de réception" lorsque cette commande d'action
	 * de type message est supprimée. Utilise preRemove() (et non postRemove()) car DB::remove()
	 * réinitialise l'id de l'objet à null avant d'appeler postRemove().
	 *
	 * @return void
	 */
	public function preRemove() {
		if ($this->getType() != 'action' || $this->getSubType() != 'message' || $this->getLogicalId() == 'at_command') {
			return;
		}
		$eqLogic = $this->getEqLogic();
		foreach (array('delivery_status_', 'delivery_success_') as $prefix) {
			$companion = $eqLogic->getCmd(null, $prefix . $this->getId());
			if (is_object($companion)) {
				$companion->remove();
			}
		}
	}

	public function preSave() {
		if ($this->getSubtype() == 'message' && !in_array($this->getLogicalId(), array('send_to_custom_number', 'at_command'))) {
			$this->setDisplay('title_disable', 1);
		}
	}

	public function execute($_options = null) {
		if ($this->getLogicalId() == 'at_command') {
			// Commande AT du mode diagnostic : la commande est le message, le délai (facultatif) est le titre
			return sms4g::sendAtCommand(isset($_options['message']) ? $_options['message'] : '', isset($_options['title']) ? $_options['title'] : '');
		}
		$title = isset($_options['title']) ? $_options['title'] : '';
		$message = isset($_options['message']) ? $_options['message'] : '';
		if (isset($_options['answer'])) {
			$message .= ' (' . implode(';', $_options['answer']) . ')';
		}
		$isCustomNumber = ($this->getLogicalId() == 'send_to_custom_number');
		if (isset($_options['number'])) {
			// Réponse à une interaction : le numéro de l'expéditeur
			$phonenumbers = array($_options['number']);
		} elseif ($isCustomNumber) {
			// "Envoyer message à" : le ou les numéros (séparés par ;) sont le titre, le texte est le message
			$phonenumbers = explode(';', $title);
		} else {
			$phonenumbers = explode(';', $this->getConfiguration('phonenumber'));
		}
		$message = trim($message);
		if ($message == '' && !$isCustomNumber) {
			$message = trim($title);
		}
		return sms4g::sendSms($phonenumbers, $message);
	}
}
