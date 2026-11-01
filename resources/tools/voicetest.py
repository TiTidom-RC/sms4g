""" Test autonome d'un appel sortant avec diffusion audio sur SIM7600 (démon sms4g arrêté).

Appelle un numéro, attend VOICE CALL: BEGIN, active l'audio USB (AT+CPCMREG=1), diffuse un fichier PCM brut
(8 kHz, mono, 16 bits little-endian), affiche les touches reçues, enregistre l'audio reçu, puis raccroche.
Seule dépendance : pyserial (celle du venv du plugin).
"""

import argparse
import sys
import threading
import time

import serial

FRAME_SIZE = 320  # 20 ms à 8 kHz, 16 bits, mono
FRAME_DURATION = 0.02
BYTES_PER_SECOND = 16000


class AtLink:
    """ Port AT : un thread lit et affiche chaque ligne horodatée, et signale la fin des commandes et l'état de l'appel """

    def __init__(self, port: str):
        self.port = serial.Serial(port=port, baudrate=115200, dsrdtr=True, rtscts=False, timeout=0.2)
        self.port.reset_input_buffer()
        self.startedAt = time.monotonic()
        self.answer: str | None = None
        self.answerReceived = threading.Event()
        self.callBegan = threading.Event()
        self.callEnded = threading.Event()
        self.keys: list[str] = []
        self.alive = True
        self.thread = threading.Thread(target=self._readLoop, daemon=True)
        self.thread.start()

    def log(self, text: str) -> None:
        print(f'[{time.monotonic() - self.startedAt:7.2f}s] {text}', flush=True)

    def _readLoop(self) -> None:
        buffer = b''
        while self.alive:
            buffer += self.port.read(self.port.in_waiting or 1)
            while b'\n' in buffer:
                raw, buffer = buffer.split(b'\n', 1)
                line = raw.decode('ascii', errors='replace').strip()
                if line:
                    self._handleLine(line)

    def _handleLine(self, line: str) -> None:
        self.log(f'<< {line}')
        if line == 'OK' or line == 'ERROR' or line.startswith(('+CME ERROR', '+CMS ERROR')):
            self.answer = line
            self.answerReceived.set()
        elif line.startswith('VOICE CALL: BEGIN'):
            self.callBegan.set()
        elif line.startswith('VOICE CALL: END') or line in ('NO CARRIER', 'BUSY', 'NO ANSWER'):
            self.callEnded.set()
        elif line.startswith('+RXDTMF:'):
            self.keys.append(line.split(':', 1)[1].strip())

    def command(self, text: str, timeout: float = 5.0) -> str | None:
        self.answerReceived.clear()
        self.answer = None
        self.log(f'>> {text}')
        self.port.write(text.encode('ascii') + b'\r')
        self.answerReceived.wait(timeout)
        return self.answer

    def close(self) -> None:
        self.alive = False
        self.thread.join(timeout=1)
        self.port.close()


class AudioLink:
    """ Port audio : un thread enregistre en continu ce que le modem envoie, pour ne pas laisser son tampon se remplir """

    def __init__(self, port: str, outputPath: str):
        self.port = serial.Serial(port=port, baudrate=115200, dsrdtr=False, rtscts=False, timeout=0.05, write_timeout=1)
        self.port.reset_input_buffer()
        self.output = open(outputPath, 'wb')
        self.received = 0
        self.alive = True
        self.thread = threading.Thread(target=self._readLoop, daemon=True)
        self.thread.start()

    def _readLoop(self) -> None:
        while self.alive:
            data = self.port.read(self.port.in_waiting or 1)
            if data:
                self.received += len(data)
                self.output.write(data)

    def play(self, pcm: bytes, callEnded: threading.Event, log) -> None:
        startedAt = time.monotonic()
        deadline = startedAt
        worstDelay = 0.0
        sent = 0
        for offset in range(0, len(pcm), FRAME_SIZE):
            if callEnded.is_set():
                log('Appel terminé pendant la diffusion')
                break
            try:
                self.port.write(pcm[offset:offset + FRAME_SIZE].ljust(FRAME_SIZE, b'\0'))
            except serial.SerialTimeoutException:
                log('Le modem n\'accepte plus l\'audio (écriture bloquée plus d\'une seconde) : diffusion interrompue')
                break
            sent += FRAME_SIZE
            deadline += FRAME_DURATION
            delay = deadline - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                worstDelay = max(worstDelay, -delay)
        log(f'Diffusion : {sent} octets ({sent / BYTES_PER_SECOND:.1f} s d\'audio) en {time.monotonic() - startedAt:.1f} s, '
            f'plus gros retard {worstDelay * 1000:.0f} ms')

    def close(self) -> None:
        self.alive = False
        self.thread.join(timeout=1)
        self.port.close()
        self.output.close()


def enableAudio(at: AtLink) -> bool:
    """ AT+CPCMREG=1 peut échouer juste après le changement d'état de l'appel : quelques essais espacés """
    for _ in range(5):
        time.sleep(0.5)
        if at.command('AT+CPCMREG=1') == 'OK':
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description='Test d\'appel sortant avec diffusion audio sur SIM7600')
    parser.add_argument('--at', required=True, help='port AT')
    parser.add_argument('--audio', required=True, help='port audio')
    parser.add_argument('--number', required=True, help='numéro à appeler')
    parser.add_argument('--pcm', required=True, help='fichier PCM brut 8 kHz, mono, 16 bits little-endian')
    parser.add_argument('--received', default='received.raw', help='fichier où enregistrer l\'audio reçu')
    parser.add_argument('--ring', type=float, default=45.0, help='temps maximal d\'attente du décroché, en secondes')
    parser.add_argument('--hold', type=float, default=20.0, help='temps en ligne après la diffusion, en secondes')
    args = parser.parse_args()

    with open(args.pcm, 'rb') as f:
        pcm = f.read()
    print(f'Fichier : {len(pcm)} octets, soit {len(pcm) / BYTES_PER_SECOND:.1f} s d\'audio', flush=True)

    at = AtLink(args.at)
    audio = AudioLink(args.audio, args.received)
    audioEnabled = False
    code = 1
    try:
        if at.command(f'ATD{args.number};') != 'OK':
            at.log('Composition refusée')
            return code
        at.log('Composition acceptée : note le moment où tu décroches')
        if not at.callBegan.wait(args.ring):
            at.log('Pas de VOICE CALL: BEGIN dans le délai')
            return code
        audioEnabled = enableAudio(at)
        if not audioEnabled:
            at.log('AT+CPCMREG=1 refusé')
            return code
        time.sleep(1.0)
        audio.play(pcm, at.callEnded, at.log)
        at.log(f'Reste en ligne {args.hold:.0f} s : parle et appuie sur des touches')
        at.callEnded.wait(args.hold)
        code = 0
        return code
    except KeyboardInterrupt:
        at.log('Interrompu')
        return code
    finally:
        if audioEnabled:
            at.command('AT+CPCMREG=0')
        if not at.callEnded.is_set():
            at.command('AT+CHUP')
            at.callEnded.wait(5)
        if not at.callEnded.is_set():
            at.log('Appel peut-être encore en cours après AT+CHUP : raccroche sur le téléphone')
        at.command('AT+CEER')
        audio.close()
        at.log(f'Touches reçues : {" ".join(at.keys) or "aucune"}')
        at.log(f'Audio reçu : {audio.received} octets (attendu : {BYTES_PER_SECOND} par seconde en ligne)')
        at.close()


if __name__ == '__main__':
    sys.exit(main())
