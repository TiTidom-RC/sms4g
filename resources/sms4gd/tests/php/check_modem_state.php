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

// Erreur définitive : la ligne Déconnecté reste en info, l'erreur a sa propre ligne et coupe la gestion automatique
[$lines, $values, $added, $saved] = receive(['state' => 'disconnected', 'reason' => 'Incorrect PIN', 'fatal' => true, 'errorType' => 'IncorrectPinError']);
check($lines === [
    ['info', '[MODEM] Déconnecté (Incorrect PIN)'],
    ['error', '[MODEM] Erreur définitive (code PIN, mode PDU) : redémarrage automatique du démon désactivé'],
], 'erreur définitive', $lines);
check($values === $disconnected, 'erreur définitive : commandes', $values);
check($added === [['Erreur du modem : Incorrect PIN', 'sms4gcmderror']], 'erreur définitive : message', $added);
check($saved === ['deamonAutoMode' => 0], 'erreur définitive : gestion automatique désactivée', $saved);

// Les autres états ne changent pas
[$lines, $values] = receive(['state' => 'connected']);
check($lines === [['info', '[MODEM] Connecté']], 'connecté', $lines);
check($values === ['connection' => 'Connecté', 'connection_state' => 4, 'online' => 1], 'connecté : commandes', $values);
[$lines] = receive(['state' => 'reconnecting', 'attempt' => 2, 'maxAttempts' => 10]);
check($lines === [['info', '[MODEM] Reconnexion 2/10']], 'reconnexion', $lines);

echo 'OK' . PHP_EOL;
