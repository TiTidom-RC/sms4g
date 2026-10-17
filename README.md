# SMS 4G Plugin for Jeedom

## 🎯 Description

Plugin Jeedom pour l'envoi et la réception de SMS via un modem ou une clé GSM/4G branché en USB : modems **SimCom** (ex : SIM7600G-H), **Quectel** (ex : BG96) et clés **Huawei** 4G en mode série (ex : E3372h-153).

Avec ce plugin vous pouvez être notifié par SMS, poser une question ou déclencher une action via SMS grâce au moteur d'interactions natif de Jeedom.

## 🚀 Fonctionnalités principales

- 📤 **Envoi de SMS** : notifications, réponses aux scénarios, envoi vers un ou plusieurs contacts, SMS concaténés standard (norme GSM) pour les messages longs, mise en attente et nouvel essai automatique si le modem est indisponible
- 📥 **Réception de SMS** : messages courts et **longs (multi-parties/concaténés)**, avec reconstruction fiable même sur les notifications temps réel (`+CMTI`)
- ✅ **Accusés de réception** : suivi livré/échec par contact (commandes « Statut » et « Remis »)
- 🔄 **Reconnexion automatique** en cas de perte du modem (backoff configurable)
- 📶 **Forcer le mode 4G uniquement** (modems SimCom, Huawei et Quectel) pour éviter les recherches inutiles en 2G/3G
- 📡 **Équipement « Modem »** : signal, connexion, opérateur, redémarrage du modem
- 🩺 **Auto-test** optionnel : le modem s'envoie un SMS pour vérifier qu'il reçoit les SMS et leurs accusés, avec redémarrage automatique possible
- 🔧 **Mode diagnostic** : commandes AT de consultation envoyées depuis Jeedom (les autres commandes sont refusées)
- 🔐 **Numéros inconnus** : autorisation et/ou création automatique de commande à la réception
- 🤖 **Moteur d'interactions Jeedom** : réponses automatiques aux SMS entrants
- 🛡️ **Journaux** : clé API, code PIN et numéros de téléphone masqués

## 📋 Prérequis matériel et système

- Une clé ou un modem GSM/4G compatible (SimCom, Quectel, Huawei...) avec une carte SIM active
- Accès série exposé sous `/dev/serial/by-id/...` ou `/dev/ttyUSB*`
- **Jeedom 4.6.1 minimum**
- **Debian 12 (Bookworm) minimum** — ce plugin ne prend pas en charge Debian 11

## ⚙️ Architecture

- **PHP** (`core/class/sms4g.class.php`) : intégration Jeedom Core (`eqLogic`/`cmd`), gestion du cycle de vie du démon
- **Python** (`resources/sms4gd/`) : démon à base de threads, communication AT via port série (bibliothèque `wwanlib`, dont le module d'encodage PDU est dérivé de `python-gsmmodem`, LGPL-3.0, © Francois Aucamp), canal socket TCP + callback HTTP vers Jeedom

## 📚 Documentation

Documentation complète disponible sur [le site de documentation](https://titidom-rc.github.io/Documentation/fr_FR/sms4g/). Support et discussions : [forum Community Jeedom](https://community.jeedom.com/tag/plugin-sms4g).

## 📄 Licence et crédits

Ce plugin est distribué sous licence **GNU GPL v3** (voir [LICENSE](LICENSE)) ; les fichiers portent l'en-tête « version 3 ou ultérieure ».

- Plugin issu d'une réécriture du plugin SMS officiel de Jeedom (Jeedom SAS).
- `resources/sms4gd/wwanlib/pdu.py` dérive de [python-gsmmodem](https://github.com/faucamp/python-gsmmodem) (© 2013 Francois Aucamp et contributeurs, GNU LGPL v3 ou ultérieure).
