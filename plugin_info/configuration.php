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
include_file('core', 'authentification', 'php');
if (!isConnect('admin')) {
    throw new Exception('{{401 - Accès non autorisé}}');
}
?>
<form class="form-horizontal">
    <fieldset>
        <div>
            <legend><i class="fas fa-info"></i> {{Plugin}}</legend>
            <div class="form-group">
                <label class="col-lg-3 control-label">{{Version Plugin}}
                    <sup><i class="fas fa-question-circle tooltips" title="{{Version du Plugin (A indiquer sur Community)}}"></i></sup>
                </label>
                <div class="col-lg-1">
                    <input class="configKey form-control" data-l1key="pluginVersion" readonly />
                </div>
            </div>
            <div class="form-group">
                <label class="col-lg-3 control-label">{{Version Python}}
                    <sup><i class="fas fa-question-circle tooltips" title="{{Version de Python utilisée par le Plugin (A indiquer sur Community)}}"></i></sup>
                </label>
                <div class="col-lg-1">
                    <input class="configKey form-control" data-l1key="pythonVersion" readonly />
                </div>
            </div>
            <div class="form-group">
                <label class="col-lg-3 control-label">{{Version PyEnv}}
                    <sup><i class="fas fa-question-circle tooltips" title="{{Version de PyEnv utilisée par le Plugin (A indiquer sur Community)}}"></i></sup>
                </label>
                <div class="col-lg-1">
                    <input class="configKey form-control" data-l1key="pyenvVersion" readonly />
                </div>
            </div>
            <div class="form-group">
                <label class="col-lg-3 control-label">{{Désactiver les messages de MàJ}}
                    <sup><i class="fas fa-question-circle tooltips" title="{{Cocher cette case désactivera les messages de mise à jour du plugin dans le centre de message}}"></i></sup>
                </label>
                <div class="col-lg-1">
                    <input type="checkbox" class="configKey" data-l1key="disableUpdateMsg" />
                </div>
            </div>
            <legend><i class="fas fa-code"></i> {{Dépendances}}</legend>
            <div class="form-group">
                <label class="col-lg-3 control-label">{{Force les mises à jour Systèmes}}
                    <sup><i class="fas fa-ban tooltips" style="color:var(--al-danger-color)!important;" title="{{Les dépendances devront être relancées après la sauvegarde de ce paramètre}}"></i></sup>
                    <sup><i class="fas fa-question-circle tooltips" title="{{Permet de forcer l'installation des mises à jour systèmes}}"></i></sup>
                </label>
                <div class="col-lg-1">
                    <input type="checkbox" class="configKey" data-l1key="debugInstallUpdates" />
                </div>
            </div>
            <div class="form-group">
                <label class="col-lg-3 control-label">{{Force la réinitialisation de PyEnv}}
                    <sup><i class="fas fa-ban tooltips" style="color:var(--al-danger-color)!important;" title="{{Les dépendances devront être relancées après la sauvegarde de ce paramètre}}"></i></sup>
                    <sup><i class="fas fa-question-circle tooltips" title="{{Permet de forcer la réinitialisation de l'environnement Python utilisé par le plugin}}"></i></sup>
                </label>
                <div class="col-lg-1">
                    <input type="checkbox" class="configKey" data-l1key="debugRestorePyEnv" />
                </div>
            </div>
            <div class="form-group">
                <label class="col-lg-3 control-label">{{Force la réinitialisation de Venv}}
                    <sup><i class="fas fa-ban tooltips" style="color:var(--al-danger-color)!important;" title="{{Les dépendances devront être relancées après la sauvegarde de ce paramètre}}"></i></sup>
                    <sup><i class="fas fa-question-circle tooltips" title="{{Permet de forcer la réinitialisation de l'environnement Venv utilisé par le plugin}}"></i></sup>
                </label>
                <div class="col-lg-1">
                    <input type="checkbox" class="configKey" data-l1key="debugRestoreVenv" />
                </div>
            </div>
            <legend><i class="fas fa-university"></i> {{Démon}}</legend>
            <div class="form-group">
                <label class="col-lg-3 control-label">{{Port socket interne}}
                    <sup><i class="fas fa-exclamation-triangle tooltips" style="color:var(--al-warning-color)!important;" title="{{Le démon devra être redémarré après la modification de ce paramètre}}"></i></sup>
                    <sup><i class="fas fa-question-circle tooltips" title="{{[ATTENTION] Ne changez ce paramètre qu'en cas de nécessité. (Défaut = 55115)}}"></i></sup>
                </label>
                <div class="col-lg-1">
                    <input class="configKey form-control" data-l1key="socketport" placeholder="55115" />
                </div>
            </div>
            <div class="form-group">
                <label class="col-lg-3 control-label">{{Cycle (s)}}
                    <sup><i class="fas fa-exclamation-triangle tooltips" style="color:var(--al-warning-color)!important;" title="{{Le démon devra être redémarré après la modification de ce paramètre}}"></i></sup>
                    <sup><i class="fas fa-question-circle tooltips" title="{{Intervalle entre deux relevés du signal et de l'enregistrement sur le réseau mobile (5 secondes au minimum). Un chiffre trop bas peut amener à une certaine instabilité.}}"></i></sup>
                </label>
                <div class="col-lg-1">
                    <input class="configKey form-control" data-l1key="cycle" />
                </div>
            </div>
        </div>
        <div>
            <legend><i class="fas fa-server"></i> {{Système}}</legend>
            <div class="form-group">
                <label class="col-lg-3 control-label">ModemManager
                    <sup><i class="fas fa-question-circle tooltips" title="{{Service système de gestion des modems pour l'accès Internet (sans rapport avec l'envoi/réception de SMS) : peut entrer en conflit avec l'accès au port série du modem lors d'un branchement/débranchement}}"></i></sup>
                </label>
                <div class="col-lg-9" style="display:flex; align-items:center; gap:14px; flex-wrap:wrap; padding-top:4px;">
                    <button type="button" id="btn_checkModemManager" class="btn btn-sm btn-info">
                        <i class="fas fa-stethoscope"></i> {{Vérifier}}
                    </button>
                    <button type="button" id="btn_disableModemManager" class="btn btn-sm btn-warning">
                        <i class="fas fa-ban"></i> {{Désactiver}}
                    </button>
                    <span id="modemManagerCheck_active"><i class="fas fa-question-circle" style="color:var(--al-default-color,#95a5a6)"></i> {{Actif}}</span>
                    <span id="modemManagerCheck_enabled"><i class="fas fa-question-circle" style="color:var(--al-default-color,#95a5a6)"></i> {{Activé au démarrage}}</span>
                </div>
            </div>
        </div>
        <div>
            <legend><i class="fas fa-sim-card"></i> {{Modem}}</legend>
            <div class="form-group">
                <label class="col-lg-3 control-label">{{État du modem}}
                    <sup><i class="fas fa-question-circle tooltips" title="{{Affiche la connexion du modem, son enregistrement sur le réseau mobile et la qualité du signal (valeurs de l'équipement Modem)}}"></i></sup>
                </label>
                <div class="col-lg-9" style="display:flex; align-items:center; gap:14px; flex-wrap:wrap; padding-top:4px;">
                    <button type="button" id="btn_checkModem" class="btn btn-sm btn-info">
                        <i class="fas fa-stethoscope"></i> {{Vérifier}}
                    </button>
                    <span id="modemCheck_network"><i class="fas fa-question-circle" style="color:var(--al-default-color,#95a5a6)"></i> {{Réseau}}</span>
                    <span id="modemCheck_signal"><i class="fas fa-question-circle" style="color:var(--al-default-color,#95a5a6)"></i> {{Signal}}</span>
                </div>
            </div>
            <div class="form-group">
                <label class="col-lg-3 control-label">{{Port SMS}}
                    <sup><i class="fas fa-exclamation-triangle tooltips" style="color:var(--al-warning-color)!important;" title="{{Le démon devra être redémarré après la modification de ce paramètre}}"></i></sup>
                    <sup><i class="fas fa-question-circle tooltips" title="{{Privilégiez un port /dev/serial/by-id/... (stable) plutôt qu'un /dev/ttyUSB* (numérotation pouvant changer après un redémarrage ou un rebranchement)}}"></i></sup>
                </label>
                <div class="col-lg-3">
                    <select class="configKey form-control" data-l1key="port">
                        <option value="none">{{Aucun}}</option>
                        <?php
                        foreach (jeedom::getUsbMapping() as $name => $value) {
                            echo '<option value="' . $name . '">' . $name . ' (' . $value . ')</option>';
                        }
                        foreach (ls('/dev/', 'tty*') as $value) {
                            echo '<option value="/dev/' . $value . '">/dev/' . $value . '</option>';
                        }
                        ?>
                    </select>
                </div>
            </div>
            <div class="form-group">
                <label class="col-lg-3 control-label">{{Vitesse (bauds)}}
                    <sup><i class="fas fa-exclamation-triangle tooltips" style="color:var(--al-warning-color)!important;" title="{{Le démon devra être redémarré après la modification de ce paramètre}}"></i></sup>
                    <sup><i class="fas fa-question-circle tooltips" title="{{115200 convient à la plupart des modems ; 9600 peut être utile avec un modem plus ancien en cas de problème de communication}}"></i></sup>
                </label>
                <div class="col-lg-2">
                    <select class="configKey form-control" data-l1key="serialRate">
                        <option value="115200">115200</option>
                        <option value="9600">9600</option>
                    </select>
                </div>
            </div>
            <div class="form-group">
                <label class="col-lg-3 control-label">{{Code PIN}}
                    <sup><i class="fas fa-exclamation-triangle tooltips" style="color:var(--al-warning-color)!important;" title="{{Le démon devra être redémarré après la modification de ce paramètre}}"></i></sup>
                    <sup><i class="fas fa-question-circle tooltips" title="{{Laisser vide si votre carte SIM n'a pas de code PIN}}"></i></sup>
                </label>
                <div class="col-lg-2">
                    <input type="password" class="configKey form-control" data-l1key="pin" />
                </div>
            </div>
            <div class="form-group">
                <label class="col-lg-3 control-label">{{Forcer le mode 4G uniquement}}
                    <sup><i class="fas fa-exclamation-triangle tooltips" style="color:var(--al-warning-color)!important;" title="{{Le démon devra être redémarré après la modification de ce paramètre}}"></i></sup>
                    <sup><i class="fas fa-question-circle tooltips" title="{{Recommandé si votre opérateur a coupé la 2G/3G : évite au modem de perdre du temps à les rechercher. Attention, si la couverture 4G est absente à un endroit, le modem ne se repliera pas sur 2G/3G. Uniquement pris en compte sur les modems SimCom (ex : SIM7600G-H)}}"></i></sup>
                </label>
                <div class="col-lg-1">
                    <input type="checkbox" class="configKey" data-l1key="force4gOnly" />
                </div>
            </div>
        </div>
        <div>
            <legend><i class="fas fa-sms"></i> {{Messages}}</legend>
            <div class="form-group">
                <label class="col-lg-3 control-label">{{SMS multi-segments (max)}}
                    <sup><i class="fas fa-question-circle tooltips" title="{{0 = illimité. Certains opérateurs limitent le nombre de SMS pouvant être liés pour former un message long — indique la limite constatée si besoin}}"></i></sup>
                </label>
                <div class="col-lg-1">
                    <input class="configKey form-control" data-l1key="maxSmsPartsPerGroup" />
                </div>
            </div>
            <div class="form-group">
                <label class="col-lg-3 control-label">{{Passerelle SMS (SMSC)}}
                    <sup><i class="fas fa-exclamation-triangle tooltips" style="color:var(--al-warning-color)!important;" title="{{Le démon devra être redémarré après la modification de ce paramètre}}"></i></sup>
                    <sup><i class="fas fa-question-circle tooltips" title="{{À laisser vide dans le cas normal : le centre SMS de la SIM est utilisé. À renseigner seulement si l'envoi échoue avec l'erreur CMS 330 (centre SMS non défini) : numéro de la passerelle SMS de votre opérateur, par exemple +33695000695 (sur Android, le code #*#*4636#*#* l'affiche). Un numéro invalide est ignoré et signalé dans le log du démon.}}"></i></sup>
                </label>
                <div class="col-lg-2">
                    <input class="configKey form-control" data-l1key="smsc" />
                </div>
            </div>
            <div class="form-group">
                <label class="col-lg-3 control-label">{{Demander un accusé de réception}}
                    <sup><i class="fas fa-exclamation-triangle tooltips" style="color:var(--al-warning-color)!important;" title="{{Le démon devra être redémarré après la modification de ce paramètre}}"></i></sup>
                    <sup><i class="fas fa-question-circle tooltips" title="{{Le démon tentera de récupérer le statut de livraison (livré / échec) de chaque message envoyé}}"></i></sup>
                </label>
                <div class="col-lg-1">
                    <input type="checkbox" class="configKey" data-l1key="deliveryReport" />
                </div>
            </div>
            <div class="form-group">
                <label class="col-lg-3 control-label">{{Durée de vie des SMS en attente (minutes)}}
                    <sup><i class="fas fa-exclamation-triangle tooltips" style="color:var(--al-warning-color)!important;" title="{{Le démon devra être redémarré après la modification de ce paramètre}}"></i></sup>
                    <sup><i class="fas fa-question-circle tooltips" title="{{Durée pendant laquelle le plugin retente l'envoi d'un SMS qui n'a pas pu partir (modem déconnecté, absence de réseau...). Passé ce délai, l'envoi est abandonné et le statut passe à « Expiré ». Minimum : 1 minute. Par défaut : 60 minutes.}}"></i></sup>
                </label>
                <div class="col-lg-1">
                    <input class="configKey form-control" data-l1key="smsTtl" />
                </div>
            </div>
            <div class="form-group">
                <label class="col-lg-3 control-label">{{Attente des parties d'un SMS long (s)}}
                    <sup><i class="fas fa-exclamation-triangle tooltips" style="color:var(--al-warning-color)!important;" title="{{Le démon devra être redémarré après la modification de ce paramètre}}"></i></sup>
                    <sup><i class="fas fa-question-circle tooltips" title="{{Durée pendant laquelle le plugin attend les parties manquantes d'un SMS long. Passé ce délai, le message est abandonné (il n'est pas transmis à moitié) et un message d'erreur apparaît dans Jeedom. Par défaut : 300 secondes.}}"></i></sup>
                </label>
                <div class="col-lg-1">
                    <input class="configKey form-control" data-l1key="concatPartsTtl" />
                </div>
            </div>
            <div class="form-group">
                <label class="col-lg-3 control-label">{{Interactions : ignorer les SMS de plus de (minutes)}}
                    <sup><i class="fas fa-question-circle tooltips" title="{{Un SMS reçu avec plus de retard que ce délai (modem débranché, démon arrêté...) n'est plus exécuté comme une commande : il n'y a ni interaction ni réponse à une question en attente, pour éviter d'exécuter un ordre périmé. Les commandes « Message » et « Expéditeur » sont quand même mises à jour (un scénario déclenché par ces commandes doit donc tenir compte de l'âge du message) et le SMS est écrit dans le log. 0 : jamais ignoré. Par défaut : 10 minutes. L'âge est calculé avec la date du centre SMS : l'horloge de Jeedom doit être à l'heure.}}"></i></sup>
                </label>
                <div class="col-lg-1">
                    <input class="configKey form-control" data-l1key="smsMaxAge" />
                </div>
            </div>
        </div>
        <div>
            <legend><i class="fas fa-sync-alt"></i> {{Reconnexion automatique}}</legend>
            <div class="form-group">
                <label class="col-lg-3 control-label">{{Délai avant la première tentative (s)}}
                    <sup><i class="fas fa-exclamation-triangle tooltips" style="color:var(--al-warning-color)!important;" title="{{Le démon devra être redémarré après la modification de ce paramètre}}"></i></sup>
                    <sup><i class="fas fa-question-circle tooltips" title="{{Ce délai double à chaque nouvel échec, jusqu'au délai maximum ci-dessous}}"></i></sup>
                </label>
                <div class="col-lg-1">
                    <input class="configKey form-control" data-l1key="reconnectBaseDelay" />
                </div>
            </div>
            <div class="form-group">
                <label class="col-lg-3 control-label">{{Délai maximum entre deux tentatives (s)}}
                    <sup><i class="fas fa-exclamation-triangle tooltips" style="color:var(--al-warning-color)!important;" title="{{Le démon devra être redémarré après la modification de ce paramètre}}"></i></sup>
                    <sup><i class="fas fa-question-circle tooltips" title="{{Le délai entre deux tentatives double à chaque échec, sans jamais dépasser cette valeur. Par défaut : 300 secondes}}"></i></sup>
                </label>
                <div class="col-lg-1">
                    <input class="configKey form-control" data-l1key="reconnectMaxDelay" />
                </div>
            </div>
            <div class="form-group">
                <label class="col-lg-3 control-label">{{Nombre maximum de tentatives}}
                    <sup><i class="fas fa-exclamation-triangle tooltips" style="color:var(--al-warning-color)!important;" title="{{Le démon devra être redémarré après la modification de ce paramètre}}"></i></sup>
                    <sup><i class="fas fa-question-circle tooltips" title="{{Au-delà de ce nombre de tentatives infructueuses, le démon redémarre complètement}}"></i></sup>
                </label>
                <div class="col-lg-1">
                    <input class="configKey form-control" data-l1key="reconnectMaxAttempts" />
                </div>
            </div>
        </div>
        <div>
            <legend><i class="fas fa-stethoscope"></i> {{Diagnostic}}</legend>
            <div class="form-group">
                <label class="col-lg-3 control-label">{{Mode diagnostic (commandes AT)}}
                    <sup><i class="fas fa-exclamation-triangle tooltips" style="color:var(--al-warning-color)!important;" title="{{Le démon devra être redémarré après la modification de ce paramètre}}"></i></sup>
                    <sup><i class="fas fa-question-circle tooltips" title="{{Autorise l'envoi de commandes AT au modem depuis Jeedom, avec la commande « Commande AT » de l'équipement Modem (le résultat arrive dans « Statut AT » et « Réponse AT »). Les commandes dangereuses sont refusées par le démon. A activer le temps d'un diagnostic.}}"></i></sup>
                </label>
                <div class="col-lg-1">
                    <input type="checkbox" class="configKey" data-l1key="diagMode" />
                </div>
            </div>
        </div>
    </fieldset>
</form>
<script>
(function () {
    'use strict'
    const AJAX_URL = 'plugins/sms4g/core/ajax/sms4g.ajax.php'
    const unknownHtml = '<i class="fas fa-question-circle" style="color:var(--al-default-color,#95a5a6)"></i> '

    const indicator = (ok, label, warn) => {
        const icon = ok ? 'fa-check-circle' : (warn ? 'fa-exclamation-triangle' : 'fa-times-circle')
        const color = ok ? 'var(--al-success-color,#2ecc71)' : (warn ? 'var(--al-warning-color,#f39c12)' : 'var(--al-danger-color,#e74c3c)')
        return `<i class="fas ${icon}" style="color:${color}"></i> ${label}`
    }

    const refreshModemManagerStatus = (onDone) => {
        domUtils.ajax({
            type: 'POST',
            url: AJAX_URL,
            data: { action: 'checkModemManagerStatus' },
            dataType: 'json',
            error: (request, status, error) => handleAjaxError(request, status, error),
            success: (data) => {
                if (data.state !== 'ok') {
                    jeedomUtils.showAlert({ message: data.result, level: 'danger' })
                    document.getElementById('modemManagerCheck_active').innerHTML = unknownHtml + '{{Actif}}'
                    document.getElementById('modemManagerCheck_enabled').innerHTML = unknownHtml + '{{Activé au démarrage}}'
                } else {
                    const result = data.result
                    if (!result.installed) {
                        document.getElementById('modemManagerCheck_active').innerHTML = indicator(true, '{{Non installé}}')
                        document.getElementById('modemManagerCheck_enabled').innerHTML = ''
                    } else {
                        document.getElementById('modemManagerCheck_active').innerHTML = indicator(!result.active, result.active ? '{{Actif}}' : '{{Inactif}}', result.active)
                        document.getElementById('modemManagerCheck_enabled').innerHTML = indicator(!result.enabled, result.enabled ? '{{Activé au démarrage}}' : '{{Désactivé au démarrage}}', result.enabled)
                    }
                }
                if (onDone) { onDone() }
            }
        })
    }

    document.getElementById('btn_checkModemManager')?.addEventListener('click', (event) => {
        const btn = event.currentTarget
        btn.disabled = true
        refreshModemManagerStatus(() => { btn.disabled = false })
    })

    // Indicateur d'une ligne de résultat : ok / warn / ko, ou inconnu (icône « ? »)
    const modemIndicator = (item, fallback) => {
        if (!item || item.status === 'unknown') {
            return unknownHtml + ((item && item.label) || fallback)
        }
        return indicator(item.status === 'ok', item.label, item.status === 'warn')
    }

    const showModemStatus = (result) => {
        document.getElementById('modemCheck_network').innerHTML = modemIndicator(result && result.network, '{{Réseau}}')
        document.getElementById('modemCheck_signal').innerHTML = modemIndicator(result && result.signal, '{{Signal}}')
    }

    document.getElementById('btn_checkModem')?.addEventListener('click', (event) => {
        const btn = event.currentTarget
        btn.disabled = true
        domUtils.ajax({
            type: 'POST',
            url: AJAX_URL,
            data: { action: 'getModemStatus' },
            dataType: 'json',
            error: (request, status, error) => { handleAjaxError(request, status, error); showModemStatus(null); btn.disabled = false },
            success: (data) => {
                btn.disabled = false
                if (data.state !== 'ok') {
                    jeedomUtils.showAlert({ message: data.result, level: 'danger' })
                    showModemStatus(null)
                } else {
                    jeedomUtils.showAlert({ message: data.result.message, level: data.result.level })
                    showModemStatus(data.result)
                }
            }
        })
    })

    document.getElementById('btn_disableModemManager')?.addEventListener('click', (event) => {
        const btn = event.currentTarget
        jeeDialog.confirm({
            title: '{{Désactivation de}} ModemManager',
            message: '<div class="alert alert-warning"><i class="fas fa-exclamation-triangle"></i> {{ModemManager va être désactivé et arrêté}} (<code>systemctl disable --now</code>).<br>{{Ce changement est global au système.}}</div>{{Confirmer la désactivation ?}}'
        }, (result) => {
            if (!result) {
                return
            }
            btn.disabled = true
            domUtils.ajax({
                type: 'POST',
                url: AJAX_URL,
                data: { action: 'disableModemManager' },
                dataType: 'json',
                error: (request, status, error) => { handleAjaxError(request, status, error); btn.disabled = false },
                success: (data) => {
                    if (data.state !== 'ok') {
                        jeedomUtils.showAlert({ message: data.result, level: 'danger' })
                        btn.disabled = false
                    } else {
                        jeedomUtils.showAlert({ message: '{{ModemManager désactivé avec succès}}', level: 'success' })
                        refreshModemManagerStatus(() => { btn.disabled = false })
                    }
                }
            })
        })
    })
})()
</script>