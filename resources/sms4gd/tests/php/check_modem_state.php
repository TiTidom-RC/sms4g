<?php
// Vrai sms4g::onModemState, avec de faux objets Jeedom. Lancé par test_phpChecks.py.
// Le fichier de la classe est lu en entier ; seul son éventuel require_once du Core (absent sans Jeedom) est retiré de la
// copie passée à eval : le fichier lui-même n'est pas modifié.

class eqLogic {}
class cmd {}

class log {
    public static $lines = [];
    public static function add($_log, $_level, $_message, $_logicalId = '') {
        self::$lines[] = [$_level, $_message];
    }
}

class message {
    public static $added = [];
    public static function add($_plugin, $_message, $_action = '', $_logicalId = '') {
        self::$added[] = [$_message, $_logicalId];
    }
    public static function byPluginLogicalId($_plugin, $_logicalId) {
        return array_values(array_filter(self::$added, fn($_added) => $_added[1] == $_logicalId));
    }
    public static function removeByPluginLogicalId($_plugin, $_logicalId) {
        self::$added = array_values(array_filter(self::$added, fn($_added) => $_added[1] != $_logicalId));
    }
}

class config {
    public static $saved = [];
    public static function save($_key, $_value, $_plugin = 'core') {
        self::$saved[$_key] = $_value;
    }
    public static function byKey($_key, $_plugin = 'core', $_default = '') {
        return array_key_exists($_key, self::$saved) ? self::$saved[$_key] : $_default;
    }
    public static function remove($_key, $_plugin = 'core') {
        unset(self::$saved[$_key]);
    }
}

class jeedom {
    public static function getTmpFolder($_plugin) {
        return __DIR__ . '/absent';
    }
    // Un port déjà résolu par le Core (tableau) : deamon_info ne teste pas le fichier et ne lance pas chmod
    public static function getUsbMapping($_port) {
        return ['resolved' => $_port];
    }
}

function __($_text, $_file = '') {
    return $_text;
}

function secureXSS($_text) {
    return $_text;
}

class fakeModem {
    public $values = [];
    public function checkAndUpdateCmd($_logicalId, $_value, $_time = null) {
        $this->values[$_logicalId] = $_value;
    }
}

$source = file_get_contents(__DIR__ . '/../../../../core/class/sms4g.class.php');
$source = preg_replace('/^<\?php/', '', $source);
// Pas de « $ » en fin d'expression : il ne correspond pas avant le \r d'un fichier aux fins de ligne Windows
$source = preg_replace('/^[ \t]*require_once\b[^\r\n]*core\.inc\.php[^\r\n]*/m', '', $source, -1, $removed);
check($removed <= 1, 'une seule ligne require_once du Core attendue', $removed);
eval($source);

function check(bool $ok, string $label, $actual = null) {
    if (!$ok) {
        echo "KO $label" . ($actual !== null ? ' : ' . json_encode($actual, JSON_UNESCAPED_UNICODE) : '') . PHP_EOL;
        exit(1);
    }
}

/**
 * Un état reçu du démon, sur un Jeedom sans message ni configuration enregistrés.
 * Renvoie les lignes de log, les valeurs des commandes du Modem, les messages et la configuration enregistrés.
 */
function receive(array $_message) {
    log::$lines = [];
    message::$added = [];
    config::$saved = [];
    $modem = new fakeModem();
    sms4g::onModemState($modem, $_message, null);
    return [log::$lines, $modem->values, message::$added, config::$saved];
}

$disconnected = ['connection' => 'Déconnecté', 'connection_state' => 0, 'online' => 0];

// Arrêt du démon (par Jeedom, l'utilisateur ou le heartbeat) : voulu, donc info
[$lines, $values, $added, $saved] = receive(['state' => 'disconnected', 'reason' => 'daemon stopped', 'fatal' => false]);
check($lines === [['info', '[MODEM] Déconnecté (arrêt du démon)']], 'arrêt du démon', $lines);
check($values === $disconnected, 'arrêt du démon : commandes', $values);
check($added === [] && $saved === [], 'arrêt du démon : ni message ni configuration', [$added, $saved]);

// Reconnexion abandonnée : panne, donc error, avec la cause
[$lines, $values, $added, $saved] = receive(['state' => 'disconnected', 'reason' => 'Port /dev/ttyUSB2 not found', 'fatal' => false, 'errorType' => 'SerialException']);
check($lines === [['error', '[MODEM] Déconnecté (Port /dev/ttyUSB2 not found)']], 'reconnexion abandonnée', $lines);
check($values === $disconnected, 'reconnexion abandonnée : commandes', $values);
check($added === [] && $saved === [], 'reconnexion abandonnée : ni message ni configuration', [$added, $saved]);

// La cause vient du démon : un numéro y est masqué
[$lines] = receive(['state' => 'disconnected', 'reason' => 'No answer from +33612345678', 'fatal' => false]);
check($lines === [['error', '[MODEM] Déconnecté (No answer from +336XXXXXX78)']], 'numéro masqué dans la cause', $lines);

// Reconnexion abandonnée sans cause connue (absente ou null)
foreach ([['state' => 'disconnected', 'fatal' => false], ['state' => 'disconnected', 'reason' => null, 'fatal' => false]] as $message) {
    [$lines] = receive($message);
    check($lines === [['error', '[MODEM] Déconnecté']], 'reconnexion abandonnée sans cause', $lines);
}

// Carte SIM absente jusqu'à l'abandon : la mention de la carte reste, la cause est ajoutée
[$lines, $values, $added] = receive(['state' => 'disconnected', 'reason' => 'SIM not inserted', 'fatal' => false, 'errorType' => 'SimNotInsertedError']);
check($lines === [
    ['error', '[MODEM] Déconnecté : carte SIM absente (SIM not inserted)'],
    ['error', '[MODEM] Carte SIM absente ou mal insérée'],
], 'carte SIM absente', $lines);
check($values['connection'] === 'Déconnecté : carte SIM absente', 'carte SIM absente : commande', $values);
check(array_column($added, 1) === ['sms4gSimMissing'], 'carte SIM absente : message', $added);

// Erreurs définitives : la ligne Déconnecté reste en info, l'erreur a sa propre ligne, la cause est enregistrée pour
// bloquer la relance, et la gestion automatique du démon n'est jamais touchée
$fatalErrors = [
    'IncorrectPinError' => 'Code PIN refusé par la carte SIM : corrigez-le dans la configuration, puis enregistrez',
    'PinRequiredError' => 'La carte SIM demande un code PIN : renseignez-le dans la configuration, puis enregistrez',
    'PukRequiredError' => 'Carte SIM bloquée (code PUK requis) : débloquez-la dans un téléphone, puis enregistrez la configuration',
    'PduModeNotSupportedError' => 'Modem incompatible (mode PDU non pris en charge) : changez de modem, puis enregistrez la configuration',
];
foreach ($fatalErrors as $errorType => $text) {
    [$lines, $values, $added, $saved] = receive(['state' => 'disconnected', 'reason' => 'modem error', 'fatal' => true, 'errorType' => $errorType]);
    check($lines === [
        ['info', '[MODEM] Déconnecté (modem error)'],
        ['error', '[MODEM] ' . $text . ' (relance du démon bloquée)'],
    ], "erreur définitive $errorType", $lines);
    check($values === $disconnected, "erreur définitive $errorType : commandes", $values);
    check($added === [[$text, 'sms4gcmderror']], "erreur définitive $errorType : message", $added);
    check($saved === ['fatalModemError' => $errorType], "erreur définitive $errorType : cause enregistrée, gestion automatique intacte", $saved);
}

// Erreur définitive inconnue : signalée, mais la relance n'est pas bloquée
[$lines, , $added, $saved] = receive(['state' => 'disconnected', 'reason' => 'Other', 'fatal' => true, 'errorType' => 'OtherError']);
check($lines === [['info', '[MODEM] Déconnecté (Other)']], 'erreur définitive inconnue', $lines);
check($added === [['Erreur du modem : Other', 'sms4gcmderror']], 'erreur définitive inconnue : message', $added);
check($saved === [], 'erreur définitive inconnue : rien de bloqué', $saved);

// deamon_info : lançable sans cause enregistrée, bloqué avec le texte de la cause, le port non configuré passe avant
config::$saved = ['port' => '/dev/ttyUSB2'];
$info = sms4g::deamon_info();
check($info['launchable'] === 'ok' && !isset($info['launchable_message']), 'deamon_info sans cause', $info);
foreach ($fatalErrors as $errorType => $text) {
    config::$saved = ['port' => '/dev/ttyUSB2', 'fatalModemError' => $errorType];
    $info = sms4g::deamon_info();
    check([$info['launchable'], $info['launchable_message']] === ['nok', $text], "deamon_info bloqué par $errorType", $info);
}
config::$saved = ['port' => 'none', 'fatalModemError' => 'IncorrectPinError'];
$info = sms4g::deamon_info();
check([$info['launchable'], $info['launchable_message']] === ['nok', "Le port n'est pas configuré"], 'deamon_info : port avant la cause', $info);
config::$saved = ['port' => '/dev/ttyUSB2', 'fatalModemError' => 'OtherError'];
check(sms4g::deamon_info()['launchable'] === 'ok', 'deamon_info : cause inconnue ignorée');

// Enregistrement de la configuration : la cause et son message sont effacés, le démon est de nouveau lançable
config::$saved = ['port' => '/dev/ttyUSB2', 'fatalModemError' => 'IncorrectPinError'];
message::$added = [[$fatalErrors['IncorrectPinError'], 'sms4gcmderror'], ['autre', 'sms4gSimMissing']];
sms4g::postConfig_pin('1234');
check(config::$saved === ['port' => '/dev/ttyUSB2'], 'postConfig_pin : cause effacée', config::$saved);
check(message::$added === [['autre', 'sms4gSimMissing']], 'postConfig_pin : message effacé, les autres gardés', message::$added);
check(sms4g::deamon_info()['launchable'] === 'ok', 'postConfig_pin : démon lançable');

// Les autres états ne changent pas
[$lines, $values] = receive(['state' => 'connected']);
check($lines === [['info', '[MODEM] Connecté']], 'connecté', $lines);
check($values === ['connection' => 'Connecté', 'connection_state' => 4, 'online' => 1], 'connecté : commandes', $values);
[$lines] = receive(['state' => 'reconnecting', 'attempt' => 2, 'maxAttempts' => 10]);
check($lines === [['info', '[MODEM] Reconnexion 2/10']], 'reconnexion', $lines);

echo 'OK' . PHP_EOL;
