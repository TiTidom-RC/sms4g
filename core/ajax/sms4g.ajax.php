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

try {
	require_once dirname(__FILE__) . '/../../../../core/php/core.inc.php';
	include_file('core', 'authentification', 'php');

	if (!isConnect('admin')) {
		throw new Exception(__('401 - Accès non autorisé', __FILE__));
	}

	// Envoie l'entête Content-Type: application/json et interdit toutes les actions en GET (tableau vide = POST obligatoire).
	ajax::init(array());

	if (init('action') == 'checkModemManagerStatus') {
		$status = sms4g::checkModemManagerStatus();
		log::add('sms4g', 'info', '[ModemManager][Vérification] installed=' . ($status['installed'] ? 'OK' : 'KO') . ' | active=' . ($status['active'] ? 'OK' : 'KO') . ' | enabled=' . ($status['enabled'] ? 'OK' : 'KO'));
		ajax::success($status);
	}

	if (init('action') == 'disableModemManager') {
		$success = sms4g::disableModemManager();
		if (!$success) {
			throw new Exception(__('Échec de la désactivation de ModemManager — consultez les logs sms4g', __FILE__));
		}
		ajax::success();
	}

	throw new Exception(__('Aucune méthode correspondante à', __FILE__) . ' : ' . init('action'));
	/*     * *********Catch exception*************** */
} catch (\Throwable $e) {
	log::add('sms4g', 'error', '[AJAX] ' . $e->getMessage() . ' — ' . $e->getFile() . ':' . $e->getLine());
	ajax::error(displayException($e), $e->getCode());
}
