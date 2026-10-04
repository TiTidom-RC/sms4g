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
require_once dirname(__FILE__) . "/../../../../core/php/core.inc.php";

if (!jeedom::apiAccess(init('apikey'), 'sms4g')) {
	echo __('Vous n\'êtes pas autorisé à effectuer cette action', __FILE__);
	die();
}
if (init('test') != '') {
	echo 'OK';
	die();
}
$result = json_decode(file_get_contents("php://input"), true);
if (!is_array($result)) {
	die();
}

// Protocole v2 : {"messages": [{id, type, time, ...}, ...]} (état de connexion, signal, réseau, réponse AT).
// Un message déjà traité (même id : renvoi après une réponse perdue) est ignoré.
if (isset($result['messages']) && is_array($result['messages'])) {
	$modem = sms4g::byLogicalId('modem', 'sms4g');
	if (!is_object($modem)) {
		log::add('sms4g', 'debug', '[CALLBACK] Équipement virtuel Modem non trouvé');
		die();
	}
	foreach ($result['messages'] as $message) {
		if (!is_array($message) || !isset($message['type'])) {
			log::add('sms4g', 'warning', '[CALLBACK] Message du démon sans type, ignoré');
			continue;
		}
		if (isset($message['id']) && sms4g::isDuplicateMessage($message['id'])) {
			log::add('sms4g', 'debug', '[CALLBACK] Message déjà traité, ignoré : ' . secureXSS($message['type']));
			continue;
		}
		$time = isset($message['time']) ? date('Y-m-d H:i:s', (int) $message['time']) : null;
		// \Throwable (et non \Exception) : un message mal formé peut lever une Error (TypeError...), qui ne doit pas perdre le reste du lot
		try {
			switch ($message['type']) {
				case 'modemState':
					sms4g::onModemState($modem, $message, $time);
					break;
				case 'signal':
					sms4g::onSignal($modem, $message, $time);
					break;
				case 'network':
					sms4g::onNetwork($modem, $message, $time);
					break;
				case 'atResponse':
					sms4g::onAtResponse($modem, $message, $time);
					break;
				case 'smsStatus':
					sms4g::onSmsStatus($message, $time);
					break;
				default:
					log::add('sms4g', 'warning', '[CALLBACK] Type de message inconnu : ' . secureXSS($message['type']));
			}
		} catch (\Throwable $e) {
			log::add('sms4g', 'error', '[CALLBACK] Message ' . secureXSS($message['type']) . ' : ' . $e->getMessage() . ' — ' . $e->getFile() . ':' . $e->getLine());
		}
	}
	die();
}
