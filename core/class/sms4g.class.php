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
		$port = config::byKey('port', 'sms4g');
		$port = jeedom::getUsbMapping($port);
		$sms_path = realpath(__DIR__ . '/../../resources/sms4gd');
		$cmd = self::PYTHON3_PATH . " {$sms_path}/sms4gd.py";
		$cmd .= ' --device ' . $port;
		$cmd .= ' --loglevel ' . log::convertLogLevel(log::getLogLevel('sms4g'));
		$cmd .= ' --socketport ' . config::byKey('socketport', 'sms4g');
		$cmd .= ' --serialrate ' . config::byKey('serialRate', 'sms4g');
		$cmd .= ' --pin ' . config::byKey('pin', 'sms4g', 'None');
		$cmd .= ' --textmode ' . ((config::byKey('textMode', 'sms4g') == 1) ? 'yes' : 'no');
		$cmd .= ' --smsc ' . config::byKey('smsc', 'sms4g', 'None');
		$cmd .= ' --force4g ' . ((config::byKey('force4gOnly', 'sms4g') == 1) ? 'yes' : 'no');
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

	/*     * *********************Méthode d'instance************************* */
	public function preSave() {
		if ($this->getConfiguration('allowUnknownOrigin', 0) == 0) {
			$this->setConfiguration('autoAddNewNumber', 0);
		}
	}

	public function postSave() {
		$signal = $this->getCmd(null, 'signal');
		if (!is_object($signal)) {
			$signal = new sms4gCmd();
			$signal->setEqLogic_id($this->getId());
			$signal->setLogicalId('signal');
			$signal->setIsVisible(0);
			$signal->setName(__('Signal', __FILE__));
			$signal->setTemplate('dashboard', 'core::tile');
			$signal->setTemplate('mobile', 'core::tile');
		}
		$signal->setType('info');
		$signal->setSubType('numeric');
		$signal->save();

		$connection = $this->getCmd(null, 'connection');
		if (!is_object($connection)) {
			$connection = new sms4gCmd();
			$connection->setEqLogic_id($this->getId());
			$connection->setLogicalId('connection');
			$connection->setIsVisible(0);
			$connection->setName(__('Connexion', __FILE__));
			$connection->setTemplate('dashboard', 'core::line');
			$connection->setTemplate('mobile', 'core::line');
			$connection->setDisplay('forceReturnLineBefore', 1);
			$connection->setDisplay('forceReturnLineAfter', 1);
		}
		$connection->setType('info');
		$connection->setSubType('string');
		$connection->save();

		$connectionState = $this->getCmd(null, 'connection_state');
		if (!is_object($connectionState)) {
			$connectionState = new sms4gCmd();
			$connectionState->setEqLogic_id($this->getId());
			$connectionState->setLogicalId('connection_state');
			$connectionState->setIsVisible(0);
			$connectionState->setName(__('Connexion (Code)', __FILE__));
			$connectionState->setIsHistorized(1);
			$connectionState->setConfiguration('repeatEventManagement', 'always');
			$connectionState->setTemplate('dashboard', 'core::tile');
			$connectionState->setTemplate('mobile', 'core::tile');
		}
		$connectionState->setType('info');
		$connectionState->setSubType('numeric');
		$connectionState->save();

		$online = $this->getCmd(null, 'online');
		if (!is_object($online)) {
			$online = new sms4gCmd();
			$online->setEqLogic_id($this->getId());
			$online->setLogicalId('online');
			$online->setIsVisible(0);
			$online->setName(__('En Ligne', __FILE__));
			$online->setIsHistorized(1);
			$online->setConfiguration('repeatEventManagement', 'always');
		}
		$online->setType('info');
		$online->setSubType('binary');
		$online->save();

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

	public static function cleanSMS(string $_message) {
		$characterMap = array(
			'À' => 'a', 'Á' => 'a', 'Â' => 'a', 'Ä' => 'a', 'à' => 'a', 'á' => 'a', 'â' => 'a', 'ä' => 'a', '@' => 'a',
			'Ç' => 'c', 'ç' => 'c',
			'È' => 'e', 'É' => 'e', 'Ê' => 'e', 'Ë' => 'e', 'è' => 'e', 'é' => 'e', 'ê' => 'e', 'ë' => 'e', '€' => 'e',
			'Ì' => 'i', 'Í' => 'i', 'Î' => 'i', 'Ï' => 'i', 'ì' => 'i', 'í' => 'i', 'î' => 'i', 'ï' => 'i',
			'Ñ' => 'n', 'ñ' => 'n',
			'Ò' => 'o', 'Ó' => 'o', 'Ô' => 'o', 'Ö' => 'o', 'ò' => 'o', 'ó' => 'o', 'ô' => 'o', 'ö' => 'o',
			'Ù' => 'u', 'Ú' => 'u', 'Û' => 'u', 'Ü' => 'u', 'ù' => 'u', 'ú' => 'u', 'û' => 'u', 'ü' => 'u', 'µ' => 'u',
			'Ý' => 'y', 'ý' => 'y', 'Ÿ' => 'y', 'ÿ' => 'y',
			'Œ' => 'oe', 'œ' => 'oe',
			'$' => 's'
		);
		return preg_replace('#[^A-Za-z0-9 \n\.\'=\*:]+#', '', strtr($_message, $characterMap));
	}

	/*     * *********************Méthode d'instance************************* */

	public function dontRemoveCmd() {
		if (in_array($this->getLogicalId(), array('signal', 'connection', 'connection_state', 'online'))) {
			return true;
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
		if ($this->getType() != 'action' || $this->getSubType() != 'message') {
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
		if ($this->getType() != 'action' || $this->getSubType() != 'message') {
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
		if ($this->getSubtype() == 'message' && $this->getLogicalId() != 'send_to_custom_number') {
			$this->setDisplay('title_disable', 1);
		}
	}

	public function execute($_options = null) {
		$number = $this->getConfiguration('phonenumber');
		if ($this->getLogicalId() == 'send_to_custom_number' && isset($_options['title'])) {
			$number = $_options['title'];
		}
		if (isset($_options['number'])) {
			$number = $_options['number'];
		}
		if (isset($_options['answer'])) {
			$_options['message'] .= ' (' . implode(';', $_options['answer']) . ')';
		}
		$values = array();
		if (isset($_options['message']) && $_options['message'] != '') {
			$message = trim($_options['message']);
		} else {
			$message = trim($_options['title'] . ' ' . $_options['message']);
		}
		if (config::byKey('textMode', 'sms4g') == 1) {
			$message = self::cleanSMS(trim($message));
		}
		if (isset($_options['number'])) {
			$phonenumbers = array($number);
		} else {
			$phonenumbers = explode(';', $this->getConfiguration('phonenumber'));
		}
		// Le découpage en parties/groupes SMS (encodage GSM-7 ou UCS-2, limite de parties liées) est entièrement
		// géré côté démon Python, seul à connaître l'encodage réel du message ; on transmet juste le texte complet
		$maxPartsPerGroup = (int) config::byKey('maxSmsPartsPerGroup', 'sms4g');
		foreach ($phonenumbers as $phonenumber) {
			$values[] = json_encode(array('apikey' => jeedom::getApiKey('sms4g'), 'number' => $phonenumber, 'message' => $message, 'maxPartsPerGroup' => $maxPartsPerGroup));
		}
		foreach ($values as $value) {
			$socket = socket_create(AF_INET, SOCK_STREAM, SOL_TCP);
			if ($socket === false) {
				log::add('sms4g', 'error', '[Envoi SMS] socket_create : ' . socket_strerror(socket_last_error()));
				return false;
			}
			if (@socket_connect($socket, '127.0.0.1', config::byKey('socketport', 'sms4g')) === false) {
				$err = socket_last_error($socket);
				socket_close($socket);
				log::add('sms4g', 'error', '[Envoi SMS] socket_connect (démon arrêté ?) : ' . socket_strerror($err));
				return false;
			}
			if (@socket_write($socket, $value, strlen($value)) === false) {
				$err = socket_last_error($socket);
				socket_close($socket);
				log::add('sms4g', 'error', '[Envoi SMS] socket_write : ' . socket_strerror($err));
				return false;
			}
			socket_close($socket);
		}
		return true;
	}
}
