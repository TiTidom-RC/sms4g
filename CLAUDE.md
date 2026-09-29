<!-- Créé avec l'assistance de l'IA (Claude Code) -->

# SMS 4G — Instructions Claude Code

## Règle bloquante — Discussion et validation avant implémentation

- Quand un sujet est évoqué, c'est **d'abord pour en discuter** : analyser, challenger les choix (y compris ceux de l'utilisateur), proposer des alternatives et construire un plan. Ce n'est pas une demande implicite d'implémentation.
- **Aucune modification, aucun ajout ni aucune suppression de code** (dans `sms4g` comme dans `../Documentation`) sans **validation explicite** de l'utilisateur sur le plan proposé, même si la solution paraît évidente ou triviale.
- Déroulé attendu : exploration/lecture du code → plan détaillé (fichiers touchés, approche, points ouverts) → **attente de la confirmation** → implémentation.
- Une validation porte sur le plan présenté uniquement : tout changement de périmètre en cours de route (fichier supplémentaire, approche différente, refactoring annexe) doit être de nouveau soumis avant d'être codé.

## Contexte du projet

`sms4g` est un plugin **Jeedom** pour l'envoi/réception de SMS via un modem GSM/4G (clé USB Huawei ou modem SimCom type SIM7600G-H). Fork indépendant (non lié au dépôt communautaire officiel Jeedom) intégrant : conversion robuste des numéros, gestion des SMS rapprochés, réassemblage des SMS concaténés (y compris via les notifications temps réel `+CMTI` des modems SimCom/LTE), harmonisation des logs.

### Architecture

- **PHP** ([core/class/sms4g.class.php](core/class/sms4g.class.php) — étend `eqLogic`/`cmd`) : cycle de vie du démon (`deamon_info`/`deamon_start`/`deamon_stop`), configuration des contacts, envoi de SMS via socket TCP vers le démon.
- **Démon Python** ([resources/sms4gd/sms4gd.py](resources/sms4gd/sms4gd.py)) : pilote le modem en commandes AT via port série, s'appuie sur un fork de `python-gsmmodem` ([resources/sms4gd/gsmmodem/](resources/sms4gd/gsmmodem/)) et sur la lib démon Jeedom ([resources/sms4gd/jeedom/jeedom.py](resources/sms4gd/jeedom/jeedom.py)).
- **Callback HTTP** ([core/php/jeesms4g.php](core/php/jeesms4g.php)) : point d'entrée appelé par le démon pour remonter messages reçus, accusés de réception, état de connexion. Contrôle d'accès via `jeedom::apiAccess(init('apikey'), 'sms4g')`.
- **Ajax / UI** : [core/ajax/sms4g.ajax.php](core/ajax/sms4g.ajax.php), [desktop/php/sms4g.php](desktop/php/sms4g.php), [desktop/js/sms4g.js](desktop/js/sms4g.js), configuration plugin dans [plugin_info/configuration.php](plugin_info/configuration.php).
- **Communication** :
  - PHP → Python : socket TCP sur `127.0.0.1:<socketport>` (défaut `55115`), payload JSON contenant toujours `apikey` (vérifiée côté démon).
  - Python → PHP : POST HTTP vers `jeesms4g.php?apikey=...` (URL interne `http:127.0.0.1:port:comp`).

### Workspace de dév

Le workspace contient, à côté de ce dépôt, des dépôts voisins directement accessibles (chemins relatifs à la racine de `sms4g`) :

- **Core Jeedom** (`../Jeedom/core`) — référence en lecture seule : le consulter pour vérifier la signature/le comportement d'une méthode du Core (`eqLogic`, `cmd`, `config`, `log`, `jeedom`, `network`, `system`...) plutôt que de supposer. Ne jamais le modifier.
- **Documentation** (`../Documentation`) — doc utilisateur et changelogs du plugin (voir section dédiée ci-dessous), modifiable.
- **Autres plugins Jeedom du même auteur** — **références de style** en lecture seule (ne jamais les modifier depuis ce dépôt) :

  | Plugin | Chemin | À consulter en priorité pour |
  | --- | --- | --- |
  | TVRemote | `../TVRemote` | Démon Python (`tvremoted`, lib `jeedom/`) : chaîne pyenv/venv (`install_apt.sh`), `deamon_start`/`deamon_stop`, socket PHP↔Python, heartbeat, `getPythonVersion`/`getPyEnvVersion` |
  | TTSCast | `../TTSCast` | Démon Python (`ttscastd` + `utils.py`), même chaîne pyenv/venv, gestion de plusieurs `requirements*.txt`, imports optionnels en try/except |
  | NUT_Free | `../NUT_Free` | Démon Python récent (`nutfreed` + `utils.py`), supervision d'équipement, intégration SSH-Manager |
  | Monitoring | `../Monitoring` | PHP pur (pas de démon) : structure `eqLogic`/`cmd`, UI desktop, configuration, logs, CI PHP/JS |
  | SSH-Manager | `../SSH-Manager` | PHP pur : plugin passerelle utilisé par d'autres plugins, templates de commandes |
  | Discordlink | `../Discordlink` | Démon **Node.js** (pas Python), envoi de messages, interactions Jeedom — utile pour le routage des messages reçus vers les interactions |
  | mcpIA | `../mcpIA` | Démon Python **asyncio** (MCP) : architecture différente, à ne pas prendre comme modèle de démon pour `sms4g` ; il a son propre `CLAUDE.md` |

  Règles d'usage :
  - avant d'écrire une fonctionnalité, vérifier si un équivalent existe dans l'un de ces plugins (en priorité ceux à démon Python classique : TVRemote, TTSCast, NUT_Free) et s'en inspirer ;
  - les workflows CI (`checkPHP.yml`, `checkPHPCompat.yml`, `checkPython.yml`, `js-check.yml`, `translations.yml`) sont communs à ces dépôts : s'y référer pour toute évolution de la CI de `sms4g` ;
  - reproduire dans le code **nouveau** de `sms4g` les habitudes de l'auteur (nommage, structure des méthodes, format des logs, gestion d'erreurs, commentaires) ; si les plugins divergent entre eux, privilégier le plus récent / le plus proche fonctionnellement ;
  - en cas de divergence avec le code existant de `sms4g`, garder la cohérence locale du fichier modifié (pas de reformatage massif) ; le fork `gsmmodem/` conserve son style d'origine.

## Environnement de développement

- **OS dev** : Windows 11 + VS Code. Pour les commandes lancées **pour l'utilisateur** (instructions, exemples), utiliser **PowerShell** ; ne jamais proposer de commandes bash/Linux à exécuter sur le poste de dev.
- **OS cible** : Debian **12 (Bookworm) minimum** (via Jeedom), chaîne `pyenv` + `venv` réutilisée de TVRemote/TTSCast ([resources/install_apt.sh](resources/install_apt.sh), Python 3.12.x pinné, venv dans `resources/venv`). Les scripts shell du dépôt (`install_apt.sh`) sont exécutés sur la cible Linux : bash y est normal.
- Aucun environnement Jeedom local : le plugin ne peut pas être lancé sur le poste de dev. Valider via les linters (voir CI) et une relecture attentive ; signaler explicitement ce qui n'a pas pu être testé sur un vrai modem.

## Workflow Git et CI

- Branches : `dev` (travail courant) → PR vers `beta` → `master`/stable. Les branches `fix/*` partent de `dev`.
- La CI ([.github/workflows/](.github/workflows/)) tourne sur les **PR vers `beta`** :
  - PHP : lint PHP 8.4 + PHPStan niveau 0 en PHP 8.2 et 8.4 (avec le Core Jeedom en `scanDirectories`) sur `core/`, `desktop/`, `plugin_info/`.
  - Python : `ruff check resources/sms4gd/` (config [ruff.toml](ruff.toml), `py312`, règles `E`/`F`).
  - JS : `node --check` sur tous les `.js`.
- Traductions : générées automatiquement (workflow `translations.yml` sur push `beta`, DeepL) → **ne jamais éditer à la main** `core/i18n/*.json` ni les descriptions traduites de `plugin_info/info.json` ; seul le `fr_FR` est rédigé manuellement.
- **Commits** : faits exclusivement par l'utilisateur via GitHub Desktop, dans ce dépôt comme dans `../Documentation`. Ne jamais commiter, pousser ni ouvrir de PR, et ne pas le proposer en fin de tâche : lister simplement les fichiers modifiés.
- Messages de commit (si l'utilisateur demande une proposition de message) : anglais, courts, à l'impératif (ex. `Restrict sms4g API mode to localhost`).

## Conventions de code

- Code (variables, clés JSON/config, logicalId) : anglais + camelCase, sauf conventions héritées du Core Jeedom (snake_case toléré, ex. `deamon_start`).
- Textes documentaires, logs, labels UI, commentaires : français accepté (c'est l'usage dans le code existant).
- Chaînes UI traduisibles : `__('...', __FILE__)` en PHP, `{{...}}` dans les templates `desktop/php`. Rédiger en français, les autres langues sont générées.
- Indentation : tabulations en PHP (style Core Jeedom), 4 espaces en Python.
- PHP **8.2 minimum, sans repli 7.4** (Debian 12 est la cible minimum supportée). Les syntaxes 8.0+ (`match`, `str_contains`/`str_starts_with`, named arguments, `enum`, `readonly`, union types) sont utilisables librement, sans commentaire `// TODO: PHP X.Y`. Ne pas utiliser de syntaxe > 8.2 (la CI teste 8.2).
- Python : **3.12**, Ruff uniquement pour le lint, pas de flake8/black. Ne pas reformater massivement les fichiers existants.
- `resources/sms4gd/gsmmodem/` est un fork tiers : modifications ciblées et minimales, en conservant le style d'origine.
- Nouveaux termes techniques (commandes AT, noms d'options) : les ajouter à `cSpell.words` dans [.vscode/settings.json](.vscode/settings.json) si besoin.

## Patterns Jeedom à respecter

- **Helpers du Core en priorité (règle systématique)** : le Core Jeedom fournit de nombreux helpers, et le plugin ne doit jamais réécrire ce qui existe déjà. Avant d'écrire une fonction utilitaire (PHP ou JS), **chercher d'abord dans `../Jeedom/core`** si un équivalent existe et l'utiliser. On ne code une implémentation propre que si rien n'existe, en le signalant explicitement dans le plan. Où chercher :
  - **PHP, classes du Core** (`core/class/`) : `log::add`, `config::byKey`/`config::save`, `eqLogic::byType`, `cmd::byEqLogicIdAndLogicalId`, `jeedom::getApiKey`/`apiAccess`/`getTmpFolder`, `network::getNetworkAccess`, `system::fuserk`/`getCmdSudo`, `message::add`, `cache::set`/`byKey`...
  - **PHP, fonctions globales** ([core/php/utils.inc.php](../Jeedom/core/core/php/utils.inc.php)) : `init`, `sendVarToJS`, `include_file`, `is_json`, `secureXSS`, `sanitizeAccent`, `convertDuration`, `sizeFormat`, `date_fr`, `cleanPath`, `rrmdir`, `getClientIp`, `netMatch`...
  - **JS, manipulation du DOM** ([core/dom/dom.utils.js](../Jeedom/core/core/dom/dom.utils.js), `domUtils`) : sélection, événements, requêtes Ajax (`domUtils.ajax`), plutôt que du JS natif ad hoc.
  - **JS, UI** ([desktop/common/js/utils.js](../Jeedom/core/desktop/common/js/utils.js), `jeedomUtils`) : `showAlert`/`hideAlert`, `sanitizeHTML`, `linkify`, `readableFileSize`, `initTooltips`, `datePickerInit`, `checkPageModified`...
  - **JS, modales** : `jeeDialog` (alert/confirm/prompt/dialog), jamais `alert()`/`confirm()` natifs ni de modale maison.
  - **JS, API métier** ([core/js/*.class.js](../Jeedom/core/core/js/)) : `jeedom.cmd.*`, `jeedom.eqLogic.*`, `jeedom.config.*`, `jeedom.plugin.*`... pour les appels au Core plutôt que des requêtes Ajax écrites à la main.
  - En cas de doute sur la signature ou le comportement d'un helper, lire sa source dans le Core plutôt que de supposer. Les plugins voisins montrent comment l'auteur les utilise en pratique.
- `cmd::save()` n'appelle **pas** `postSave()` (contrairement à `eqLogic::save()`) — toute logique dépendant de l'état final des cmds doit passer par `postAjax()` ou une création à la demande, jamais par un hook `cmd::postSave()` seul en cas de dépendance à l'ordre de sauvegarde.
- **Nouvelle option de configuration du démon** — la chaîne complète doit être mise à jour :
  1. valeur par défaut dans [plugin_info/install.php](plugin_info/install.php), **dans `sms4g_install()` et `sms4g_update()`** (les deux blocs sont dupliqués) ;
  2. champ dans [plugin_info/configuration.php](plugin_info/configuration.php) ;
  3. `config::byKey(...)` → argument CLI dans `deamon_start()` ;
  4. `parser.add_argument(...)` + lecture de `args` dans `sms4gd.py`.
- Suivi de version : `sms4g::getPluginVersion()` (lit `plugin_info/info.json`), `sms4g::getPythonVersion()` et `sms4g::getPyEnvVersion()`, appelés depuis `install.php` et `deamon_start()`, affichés en lecture seule dans `configuration.php`. Un bump de version se fait dans `pluginVersion` de `info.json`.
- Dépendances Python : [resources/install_apt.sh](resources/install_apt.sh) (pyenv + venv) + [resources/requirements.txt](resources/requirements.txt) (versions pinnées) — pas de `plugin_info/packages.json`. Toute nouvelle dépendance Python va dans `requirements.txt` ; la regex de vérification (`pythonDepString`/`pythonDepNum`) est recalculée automatiquement par `sms4g::getPythonDepFromRequirements()`.

## Sécurité et données sensibles

- **Logs** : ne jamais journaliser en clair l'apikey, le code PIN ni les numéros de téléphone complets.
  - Côté Python, le filtre `SecretMaskFilter` (`sms4gd.py`) masque apikey, `AT+CPIN` et numéros `+XXXXXXXX` : toute nouvelle donnée sensible doit y être ajoutée (règle dans `_RULES`) plutôt que masquée au cas par cas.
  - Côté PHP, masquer avant `log::add` (cf. `preg_replace` sur `--apikey|--pin` dans `deamon_start()`), sans altérer la commande réellement exécutée.
- **Accès API** : le mode API du plugin est forcé à `localhost` (`api::sms4g::mode` dans `install.php`) ; ne pas relâcher cette contrainte.
- Toute entrée provenant du socket ou du callback HTTP doit être authentifiée par l'apikey avant traitement.
- Les SMS reçus sont des entrées non fiables (routées vers le moteur d'interactions) : ne jamais les exécuter/interpoler sans contrôle.

## Documentation utilisateur (repo voisin `../Documentation`, branche `beta`)

- Changelog stable : `../Documentation/fr_FR/sms4g/changelog.md`
- Changelog beta : `../Documentation/fr_FR/sms4g/beta/changelog.md`
- Doc utilisateur : `../Documentation/fr_FR/sms4g/index.md`
- Toujours rédigés pour utilisateurs finaux (pas de jargon technique), voir les instructions du repo `Documentation` pour le format exact (préfixes `[NEW]`/`[UPDATE]`/`[FIX]`/`[SECURITY]`, groupement par thème).
- Après une évolution fonctionnelle, mettre à jour le changelog beta (et la doc si besoin) directement dans `../Documentation`, sur sa branche `beta` ; commits séparés de ceux de `sms4g`.

## Règle transverse

- Ajouter la mention « Créé avec l'assistance de l'IA » dans les métadonnées des documents ou en commentaire d'en-tête de tout **nouveau** fichier/programme créé.
