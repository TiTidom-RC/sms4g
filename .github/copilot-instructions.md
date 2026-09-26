# SMS 4G — Instructions Copilot

## Contexte du projet

`sms4g` est un plugin **Jeedom** pour l'envoi/réception de SMS via un modem GSM/4G (clé USB Huawei ou modem SimCom type SIM7600G-H). Fork indépendant (non lié au dépôt communautaire officiel Jeedom) intégrant : conversion robuste des numéros, gestion des SMS rapprochés, réassemblage des SMS concaténés (y compris via les notifications temps réel `+CMTI` des modems SimCom/LTE), harmonisation des logs.

### Architecture
- **PHP** (`core/class/sms4g.class.php` — étend `eqLogic`/`cmd`) : gère le cycle de vie du démon (`deamon_start`/`deamon_stop`), la configuration des contacts, l'envoi de SMS via socket TCP vers le démon.
- **Démon Python** (`resources/sms4gd/sms4gd.py`) : boucle asyncio, pilote le modem en AT commands via port série, fork de `python-gsmmodem` (`resources/sms4gd/gsmmodem/`).
- **Callback HTTP** (`core/php/jeesms4g.php`) : point d'entrée appelé par le démon Python pour remonter messages reçus, accusés de réception, état de connexion.
- **Communication** : PHP → Python via socket TCP (`sendToDaemon`) ; Python → PHP via POST HTTP vers `jeesms4g.php`.

## Environnement de développement

- **OS dev** : Windows 11 + VS Code, terminal **PowerShell uniquement** (jamais de commandes bash/Linux).
- **OS cible** : Debian **12 (Bookworm) minimum** (via Jeedom), chaîne `pyenv` + `venv` réutilisée de TVRemote/TTSCast (`resources/install_apt.sh`, Python 3.12.14 pinné, venv dans `resources/venv`).

## Conventions de code

- Code (variables, clés JSON/config, logicalId) : anglais + camelCase, sauf conventions héritées du Core Jeedom (snake_case toléré, ex. `deamon_start`).
- Textes documentaires, logs, labels UI : français accepté.
- PHP **8.2 minimum, sans repli 7.4** (Debian 12 est la cible minimum supportée — premier plugin de cet auteur à abandonner la compatibilité PHP 7.4/Debian 11). Les syntaxes 8.0+ (`match`, `str_contains`/`str_starts_with`, named arguments, `enum`, `readonly`, union types) sont donc utilisables librement, sans commentaire `// TODO: PHP X.Y`.
- Python : Ruff uniquement pour le lint (`ruff.toml`), pas de flake8.

## Patterns Jeedom à respecter

- Ne jamais réimplémenter une fonction déjà fournie par le Core (`log::add`, `config::byKey`/`config::save`, `eqLogic::byType`, `jeedom::getApiKey`/`getTmpFolder`...).
- `cmd::save()` n'appelle **pas** `postSave()` (contrairement à `eqLogic::save()`) — toute logique dépendant de l'état final des cmds doit passer par `postAjax()` ou une création à la demande, jamais par un hook `cmd::postSave()` seul en cas de dépendance à l'ordre de sauvegarde.
- Toute nouvelle option de configuration du démon doit être ajoutée aux **deux** endroits : `config::byKey(...)` côté PHP (`deamon_start()`) → argument CLI → `argparse` côté `sms4gd.py`, **et** un champ correspondant dans `plugin_info/configuration.php`.
- Suivi de version : `sms4g::getPluginVersion()` (lit `plugin_info/info.json`), `sms4g::getPythonVersion()` et `sms4g::getPyEnvVersion()`, appelés depuis `install.php` et `deamon_start()`, affichés en lecture seule dans `configuration.php`.
- Dépendances Python : `resources/install_apt.sh` (pyenv + venv, chaîne TVRemote/TTSCast) + `resources/requirements.txt` (versions pinnées) — pas de `plugin_info/packages.json` (mécanisme générique Jeedom abandonné au profit du script custom). Toute nouvelle dépendance Python doit être ajoutée à `requirements.txt`, la regex de vérification (`pythonDepString`/`pythonDepNum`) est recalculée automatiquement par `sms4g::getPythonDepFromRequirements()`.

## Documentation utilisateur (repo `Documentation`, branche `beta`)

- Changelog stable : `Documentation/fr_FR/sms4g/changelog.md`
- Changelog beta : `Documentation/fr_FR/sms4g/beta/changelog.md`
- Doc utilisateur : `Documentation/fr_FR/sms4g/index.md`
- Toujours rédigés pour utilisateurs finaux (pas de jargon technique), voir les instructions Copilot du repo `Documentation` pour le format exact (préfixes `[NEW]`/`[UPDATE]`/`[FIX]`/`[SECURITY]`, groupement par thème).
