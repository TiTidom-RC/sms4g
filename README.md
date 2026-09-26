# SMS 4G Plugin for Jeedom

## 🎯 Description

Plugin Jeedom pour l'envoi et la réception de SMS via une clé/modem 4G (USB ou intégré), avec prise en charge spécifique des modems **SimCom** (ex : SIM7600G-H) en plus des clés Huawei classiques.

Avec ce plugin vous pouvez être notifié par SMS, poser une question ou déclencher une action via SMS grâce au moteur d'interactions natif de Jeedom.

## 🚀 Fonctionnalités principales

- 📤 **Envoi de SMS** : notifications, réponses aux scénarios, envoi vers un ou plusieurs contacts, avec SMS concaténés standard (norme GSM) pour les messages longs
- 📥 **Réception de SMS** : messages courts et **longs (multi-parties/concaténés)**, avec reconstruction fiable même sur les notifications temps réel (`+CMTI`)
- ✅ **Accusés de réception** : suivi livré/échec par contact
- 🔄 **Reconnexion automatique** en cas de perte du modem (backoff configurable)
- 📶 **Forcer le mode 4G uniquement** (modems SimCom) pour éviter les recherches inutiles en 2G/3G
- 🔐 **Numéros inconnus** : autorisation et/ou création automatique de commande à la réception
- 🤖 **Moteur d'interactions Jeedom** : réponses automatiques aux SMS entrants

## 📋 Prérequis matériel et système

- Une clé ou un modem GSM/4G compatible (Huawei, SimCom...) avec une carte SIM active
- Accès série exposé sous `/dev/serial/by-id/...` ou `/dev/ttyUSB*`
- **Debian 12 (Bookworm) minimum** — ce plugin ne prend pas en charge Debian 11

## ⚙️ Architecture

- **PHP** (`core/class/sms4g.class.php`) : intégration Jeedom Core (`eqLogic`/`cmd`), gestion du cycle de vie du démon
- **Python** (`resources/sms4gd/`) : démon asyncio, communication AT via port série (fork de `python-gsmmodem`), canal socket TCP + callback HTTP vers Jeedom

## 📚 Documentation

Documentation complète disponible sur [le site de documentation](https://titidom-rc.github.io/Documentation/fr_FR/sms4g/).
