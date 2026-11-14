"""Command-line wrapper kept for compatibility: python3 telegram_alert.py "message"."""
import sys

from sari_eyes.telegram import send_message as enviar_alerta_telegram

__all__ = ["enviar_alerta_telegram"]

if __name__ == "__main__":
    if len(sys.argv) > 1:
        if enviar_alerta_telegram(sys.argv[1]):
            print("[TELEGRAM] Alerta enviada con éxito.")
        else:
            print("[TELEGRAM] Error al enviar alerta.")
    else:
        print("Uso: python3 telegram_alert.py 'Mensaje de prueba'")
