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

require_once dirname(__FILE__) . '/../../../core/php/core.inc.php';

function sms4g_install() {
	$pluginVersion = sms4g::getPluginVersion();
	config::save('pluginVersion', $pluginVersion, 'sms4g');
	message::removeAll('sms4g');
	message::add('sms4g', 'Installation du plugin SMS 4G (Version : ' . $pluginVersion . ')', null, null);

	sms4g::getPythonDepFromRequirements();

	if (config::byKey('api::sms::mode') == '') {
		config::save('api::sms::mode', 'localhost');
	}
	if (config::byKey('pythonVersion', 'sms4g') == '') {
		config::save('pythonVersion', '?.?.?', 'sms4g');
	}
	if (config::byKey('pyenvVersion', 'sms4g') == '') {
		config::save('pyenvVersion', '?.?.?', 'sms4g');
	}
	if (config::byKey('port', 'sms4g') == '') {
		config::save('port', 'none', 'sms4g');
	}
	if (config::byKey('socketport', 'sms4g') == '') {
		config::save('socketport', '55115', 'sms4g');
	}
	if (config::byKey('serialRate', 'sms4g') == '') {
		config::save('serialRate', '115200', 'sms4g');
	}
	if (config::byKey('cycle', 'sms4g') == '') {
		config::save('cycle', '30', 'sms4g');
	}
	if (config::byKey('maxChartByMessage', 'sms4g') == '') {
		config::save('maxChartByMessage', '612', 'sms4g');
	}
	if (config::byKey('concatPartsTtl', 'sms4g') == '') {
		config::save('concatPartsTtl', '300', 'sms4g');
	}
	if (config::byKey('reconnectBaseDelay', 'sms4g') == '') {
		config::save('reconnectBaseDelay', '5', 'sms4g');
	}
	if (config::byKey('reconnectMaxDelay', 'sms4g') == '') {
		config::save('reconnectMaxDelay', '300', 'sms4g');
	}
	if (config::byKey('reconnectMaxAttempts', 'sms4g') == '') {
		config::save('reconnectMaxAttempts', '10', 'sms4g');
	}
	if (config::byKey('debugInstallUpdates', 'sms4g') == '') {
		config::save('debugInstallUpdates', '0', 'sms4g');
	}
	if (config::byKey('debugRestorePyEnv', 'sms4g') == '') {
		config::save('debugRestorePyEnv', '0', 'sms4g');
	}
	if (config::byKey('debugRestoreVenv', 'sms4g') == '') {
		config::save('debugRestoreVenv', '0', 'sms4g');
	}
	if (config::byKey('disableUpdateMsg', 'sms4g') == '') {
		config::save('disableUpdateMsg', '0', 'sms4g');
	}

	$dependencyInfo = sms4g::dependancy_info();
	if (!isset($dependencyInfo['state'])) {
		message::add('sms4g', __('Veuillez vérifier les dépendances', __FILE__));
	} elseif ($dependencyInfo['state'] === 'nok') {
		try {
			$plugin = plugin::byId('sms4g');
			$plugin->dependancy_install();
		} catch (\Throwable $th) {
			message::add('sms4g', __('Une erreur est survenue à l\'installation automatique des dépendances. Vérifiez les logs et relancez les dépendances manuellement', __FILE__));
		}
	}
}

function sms4g_update() {
	$pluginVersion = sms4g::getPluginVersion();
	config::save('pluginVersion', $pluginVersion, 'sms4g');
	if (config::byKey('disableUpdateMsg', 'sms4g', '0') === '0') {
		message::removeAll('sms4g');
		message::add('sms4g', 'Mise à jour du plugin SMS 4G (Version : ' . $pluginVersion . ')', null, null);
	}

	sms4g::getPythonDepFromRequirements();

	if (config::byKey('api::sms::mode') == '') {
		config::save('api::sms::mode', 'localhost');
	}
	if (config::byKey('pythonVersion', 'sms4g') == '') {
		config::save('pythonVersion', '?.?.?', 'sms4g');
	}
	if (config::byKey('pyenvVersion', 'sms4g') == '') {
		config::save('pyenvVersion', '?.?.?', 'sms4g');
	}
	if (config::byKey('port', 'sms4g') == '') {
		config::save('port', 'none', 'sms4g');
	}
	if (config::byKey('socketport', 'sms4g') == '') {
		config::save('socketport', '55115', 'sms4g');
	}
	if (config::byKey('serialRate', 'sms4g') == '') {
		config::save('serialRate', '115200', 'sms4g');
	}
	if (config::byKey('cycle', 'sms4g') == '') {
		config::save('cycle', '30', 'sms4g');
	}
	if (config::byKey('maxChartByMessage', 'sms4g') == '') {
		config::save('maxChartByMessage', '612', 'sms4g');
	}
	if (config::byKey('concatPartsTtl', 'sms4g') == '') {
		config::save('concatPartsTtl', '300', 'sms4g');
	}
	if (config::byKey('reconnectBaseDelay', 'sms4g') == '') {
		config::save('reconnectBaseDelay', '5', 'sms4g');
	}
	if (config::byKey('reconnectMaxDelay', 'sms4g') == '') {
		config::save('reconnectMaxDelay', '300', 'sms4g');
	}
	if (config::byKey('reconnectMaxAttempts', 'sms4g') == '') {
		config::save('reconnectMaxAttempts', '10', 'sms4g');
	}
	if (config::byKey('debugInstallUpdates', 'sms4g') == '') {
		config::save('debugInstallUpdates', '0', 'sms4g');
	}
	if (config::byKey('debugRestorePyEnv', 'sms4g') == '') {
		config::save('debugRestorePyEnv', '0', 'sms4g');
	}
	if (config::byKey('debugRestoreVenv', 'sms4g') == '') {
		config::save('debugRestoreVenv', '0', 'sms4g');
	}
	if (config::byKey('disableUpdateMsg', 'sms4g') == '') {
		config::save('disableUpdateMsg', '0', 'sms4g');
	}

	$dependencyInfo = sms4g::dependancy_info();
	if (!isset($dependencyInfo['state'])) {
		message::add('sms4g', __('Veuillez vérifier les dépendances', __FILE__));
	} elseif ($dependencyInfo['state'] === 'nok') {
		try {
			$plugin = plugin::byId('sms4g');
			$plugin->dependancy_install();
		} catch (\Throwable $th) {
			message::add('sms4g', __('Une erreur est survenue à la mise à jour automatique des dépendances. Vérifiez les logs et relancez les dépendances manuellement', __FILE__));
		}
	}

	// Crée les commandes manquantes sur les équipements/contacts créés avant l'ajout de ces fonctionnalités
	foreach (eqLogic::byType('sms4g') as $eqLogic) {
		foreach ($eqLogic->getCmd('action') as $cmd) {
			if ($cmd->getSubType() == 'message' && (!is_object($eqLogic->getCmd(null, 'delivery_status_' . $cmd->getId())) || !is_object($eqLogic->getCmd(null, 'delivery_success_' . $cmd->getId())))) {
				$cmd->save();
			}
		}
		if (!is_object($eqLogic->getCmd(null, 'connection')) || !is_object($eqLogic->getCmd(null, 'connection_state')) || !is_object($eqLogic->getCmd(null, 'online'))) {
			$eqLogic->save();
		}
	}
	$paths = array(
		'resources/sms4gd/gsmmodem/compat.py',
	);
	foreach ($paths as $path) {
		$file = dirname(__FILE__) . '/../' . $path;
		if (file_exists($file)) {
			if (is_dir($file)) {
				exec('rm -rf ' . escapeshellarg($file), $output, $return_var);
				if ($return_var != 0) {
					log::add('sms4g', 'error', 'Failed to remove ' . $file . ' (return code: ' . $return_var . ')');
				}
			} else {
				if (!unlink($file)) {
					log::add('sms4g', 'error', 'Failed to remove ' . $file);
				}
			}
		}
	}
}

function sms4g_remove() {

}

?>
