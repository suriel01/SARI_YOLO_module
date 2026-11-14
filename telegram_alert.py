"""Command-line wrapper kept for compatibility: python3 telegram_alert.py "mensaje"."""
import logging
import sys

from sari_eyes.telegram import send_message as enviar_alerta_telegram

__all__ = ["enviar_alerta_telegram"]

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[TELEGRAM] %(message)s", stream=sys.stdout)
    if len(sys.argv) > 1:
        if enviar_alerta_telegram(sys.argv[1]):
            print("[TELEGRAM] Alerta enviada con éxito.")
        else:
            print("[TELEGRAM] Error al enviar alerta.")
    else:
        print("Uso: python3 telegram_alert.py 'Mensaje de prueba'")
