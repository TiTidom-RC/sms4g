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

if (isset($result['number']) && $result['number'] == 'signalStrength' && isset($result['message'])) {
	config::save('signalStrength', $result['message'], 'sms4g');
	foreach (eqLogic::byType('sms4g') as $eqLogic) {
		$cmd = $eqLogic->getCmd(null, 'signal');
		if (is_object($cmd)) {
			$cmd->event($result['message']);
		}
	}
	die();
}

if (isset($result['number']) && $result['number'] == 'networkName' && isset($result['message'])) {
	config::save('networkName', $result['message'], 'sms4g');
	die();
}

if (isset($result['number']) && $result['number'] == 'modemStatus' && isset($result['status'])) {
	// connectionState : échelle de 0 (déconnecté) à 4 (connecté) ; repeatEventManagement=always sur la cmd fait historiser chaque changement d'état même à valeur identique (ex : plusieurs tentatives de reconnexion)
	[$message, $connectionState, $online] = match ($result['status']) {
		'connecting' => [__('Connexion en cours', __FILE__), 3, 0],
		'connected' => [__('Connecté', __FILE__), 4, 1],
		'searching' => [__('Recherche opérateur', __FILE__), 2, 0],
		'reconnecting' => [__('Reconnexion', __FILE__) . ' ' . $result['attempt'] . '/' . $result['max_attempts'], 1, 0],
		'disconnected' => [__('Déconnecté', __FILE__), 0, 0],
		default => [$result['status'], null, null],
	};
	foreach (eqLogic::byType('sms4g') as $eqLogic) {
		$eqLogic->checkAndUpdateCmd('connection', $message);
		if ($connectionState !== null && $online !== null) {
			$eqLogic->checkAndUpdateCmd('connection_state', $connectionState);
			$eqLogic->checkAndUpdateCmd('online', $online);
		}
	}
	die();
}

if (isset($result['number']) && $result['number'] == 'deliveryReport' && isset($result['destination']) && isset($result['status'])) {
	[$destination, $formattedDestination] = formatSmsNumber($result['destination']);
	$label = ($result['status'] == 'delivered') ? __('Livré', __FILE__) : __('Échec', __FILE__);
	$statusText = $label . ' : ' . $destination . ' (' . date('d/m/Y H:i:s') . ')';
	$success = ($result['status'] == 'delivered') ? 1 : 0;
	$found = false;
	foreach (eqLogic::byType('sms4g', true) as $eqLogic) {
		/** @var cmd $cmd */
		foreach ($eqLogic->getCmd('action') as $cmd) {
			if ($cmd->getSubType() != 'message' || $cmd->getLogicalId() == 'send_to_custom_number') {
				continue;
			}
			if (!str_contains($cmd->getConfiguration('phonenumber'), $destination) && !str_contains($cmd->getConfiguration('phonenumber'), $formattedDestination)) {
				continue;
			}
			$found = true;
			$eqLogic->checkAndUpdateCmd('delivery_status_' . $cmd->getId(), $statusText);
			$eqLogic->checkAndUpdateCmd('delivery_success_' . $cmd->getId(), $success);
			log::add('sms4g', 'info', __('Accusé de réception reçu : ', __FILE__) . secureXSS($statusText));
		}
	}
	if (!$found) {
		// Numéro ne correspondant à aucun contact connu : le message a forcément été envoyé
		// via la commande "Envoyer message à" (numéro personnalisé), on y route l'accusé
		foreach (eqLogic::byType('sms4g', true) as $eqLogic) {
			$customNumberCmd = $eqLogic->getCmd(null, 'send_to_custom_number');
			if (is_object($customNumberCmd)) {
				$eqLogic->checkAndUpdateCmd('delivery_status_' . $customNumberCmd->getId(), $statusText);
				$eqLogic->checkAndUpdateCmd('delivery_success_' . $customNumberCmd->getId(), $success);
				$found = true;
				log::add('sms4g', 'info', __('Accusé de réception reçu : ', __FILE__) . secureXSS($statusText));
				break;
			}
		}
	}
	if (!$found) {
		log::add('sms4g', 'info', __('Accusé de réception reçu pour un numéro non reconnu : ', __FILE__) . secureXSS($destination));
	}
	die();
}

if (isset($result['number']) && $result['number'] == 'none' && isset($result['message'])) {
	message::add('sms4g', 'Error : ' . $result['message'], '', 'sms4gcmderror');
	if (str_contains($result['message'], 'PIN')) {
		config::save('deamonAutoMode', 0, 'sms4g');
	}
}
/** @var array<sms4g> */
$eqLogics = eqLogic::byType('sms4g', true);
if (count($eqLogics) < 1) {
	log::add('sms4g', 'debug', __("Aucun équipement SMS activé", __FILE__));
	die();
}
if (isset($result['devices'])) {
	foreach ($result['devices'] as $key => $datas) {
		$message = trim($datas['message']);
		$number = $datas['number'];
		// Le seul payload 'none' (erreur démon) arrive toujours en top-level, jamais imbriqué sous 'devices' (voir plus haut)
		if ($message == '' || $number == '') {
			continue;
		}
		[$number, $formattedPhoneNumber] = formatSmsNumber($number);
		$reply = '';
		$smsOk = false;
		foreach ($eqLogics as $eqLogic) {
			/** @var cmd $cmd */
			foreach ($eqLogic->getCmd() as $cmd) {
				if (!str_contains($cmd->getConfiguration('phonenumber'), $number) && !str_contains($cmd->getConfiguration('phonenumber'), $formattedPhoneNumber)) {
					continue;
				}
				$smsOk = true;
				log::add('sms4g', 'info', __('Message venant de ', __FILE__) . $formattedPhoneNumber . ' : ' . $message);
				if ($cmd->askResponse($message)) {
					continue (3);
				}
				handleMessage($cmd, $number, $message);
				break;
			}
			if (!$smsOk) {
				if ($eqLogic->getConfiguration('allowUnknownOrigin', 0) == 1) {
					if ($eqLogic->getConfiguration('autoAddNewNumber', 0) == 1) {
						log::add('sms4g', 'info', __('Message venant d\'un numéro inconnu, création auto activée:', __FILE__) . secureXSS($number) . ' (' . secureXSS($formattedPhoneNumber) . ') : ' . secureXSS($message));
						$new_number = new sms4gCmd();
						$new_number->setType('action');
						$new_number->setSubType('message');
						$new_number->setEqLogic_id($eqLogic->getId());
						$new_number->setName($number);
						$new_number->setConfiguration('phonenumber', $number);
						$new_number->save();

						handleMessage($new_number, $number, $message);
					} else {
						log::add('sms4g', 'info', __('Message venant d\'un numéro inconnu mais les numéros inconnus sont autorisés : ', __FILE__) . secureXSS($number) . ' (' . secureXSS($formattedPhoneNumber) . ') : ' . secureXSS($message));
						$eqLogic->checkAndUpdateCmd('sms', $message);
						$eqLogic->checkAndUpdateCmd('sender', $number);
					}
					$smsOk = true;
				}
			}
		}

		if (!$smsOk) {
			log::add('sms4g', 'info', __('Message venant d\'un numéro non autorisé : ', __FILE__) . secureXSS($number) . ' (' . secureXSS($formattedPhoneNumber) . ') : ' . secureXSS($message));
		}
	}
}

/**
 * Convertit un numéro reçu du démon vers ses deux formats de matching (international +33... et national 0...),
 * pour comparer avec le champ "phonenumber" d'une commande quel que soit le format saisi par l'utilisateur.
 * Reconnaît uniquement les formes réelles d'un numéro français (+33/0033/national 0) via des regex ancrées.
 * Toute autre entrée (sender ID alphanumérique, numéro étranger, short code...) est renvoyée inchangée des
 * deux côtés : il n'existe pas de forme alternative pertinente, et on évite ainsi toute conversion hasardeuse.
 *
 * @param string $number
 * @return array{0: string, 1: string}
 */
function formatSmsNumber($number) {
	if (preg_match('/^\+33([0-9]{9})$/', $number, $matches) === 1) {
		return array($number, '0' . $matches[1]);
	}
	if (preg_match('/^(?:00)?33([0-9]{9})$/', $number, $matches) === 1) {
		return array('+33' . $matches[1], '0' . $matches[1]);
	}
	if (preg_match('/^0([0-9]{9})$/', $number) === 1) {
		return array('+33' . substr($number, 1), $number);
	}
	return array($number, $number);
}

/**
 * Handle sms message when sms4gCmd has been found. It will check for interaction and then update 'sms' & 'sender' cmd on the eqLogic
 *
 * @param sms4gCmd $cmd
 * @param string $number
 * @param string $message
 * @return void
 */
function handleMessage($cmd, $number, $message) {
	/** @var eqLogic */
	$eqLogic = $cmd->getEqLogic();
	if ($eqLogic->getConfiguration('disableInteract', '0') == '0') {
		$params = array('plugin' => 'sms4g');
		if ($cmd->getConfiguration('user') != '') {
			$user = user::byId($cmd->getConfiguration('user'));
			if (is_object($user)) {
				$params['profile'] = $user->getLogin();
			}
		}
		$parameters['reply_cmd'] = $cmd;
		$reply = interactQuery::tryToReply($message, $params);
		if (trim($reply['reply']) != '') {
			$cmd->execute(array('title' => $reply['reply'], 'message' => '', 'number' => $number));
			log::add('sms4g', 'info', __("\nRéponse : ", __FILE__) . $reply['reply']);
		}
	} else {
		log::add('sms4g', 'debug', __("Interaction désactivée.", __FILE__));
	}
	$eqLogic->checkAndUpdateCmd('sms', $message);
	$eqLogic->checkAndUpdateCmd('sender', $cmd->getName());
}

