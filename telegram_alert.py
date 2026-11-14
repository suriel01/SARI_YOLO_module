"""Command-line wrapper kept for compatibility: python3 telegram_alert.py "message"."""
import sys

from sari_eyes.telegram import send_message as enviar_alerta_telegram

__all__ = ["enviar_alerta_telegram"]

if __name__ == "__main__":
    if len(sys.argv) > 1:
        sys.exit(0 if enviar_alerta_telegram(sys.argv[1]) else 1)
    print("Usage: python3 telegram_alert.py 'test message'")
