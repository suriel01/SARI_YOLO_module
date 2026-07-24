import os
import requests

def enviar_alerta_telegram(mensaje: str, token: str = None, chat_id: str = None) -> bool:
    """
    Envía una alerta de texto a un chat de Telegram.
    
    Args:
        mensaje (str): El mensaje a enviar.
        token (str, optional): Token del Bot de Telegram. Si no se provee, intenta leer la variable TELEGRAM_BOT_TOKEN.
        chat_id (str, optional): ID del chat de destino. Si no se provee, intenta leer la variable TELEGRAM_CHAT_ID.
        
    Returns:
        bool: True si se envió correctamente, False en caso de error.
    """
    bot_token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    target_chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    
    if not bot_token or not target_chat_id:
        print("[TELEGRAM] Error: Faltan credenciales (TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID).")
        return False
        
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": target_chat_id,
        "text": mensaje,
        "parse_mode": "Markdown"
    }
    
    try:
        response = requests.post(url, json=payload, timeout=5.0)
        if response.status_code == 200:
            print("[TELEGRAM] Alerta enviada con éxito.")
            return True
        else:
            print(f"[TELEGRAM] Error al enviar alerta HTTP {response.status_code}: {response.text}")
            return False
    except requests.exceptions.RequestException as e:
        print(f"[TELEGRAM] Excepción de red al enviar alerta: {e}")
        return False

# Ejemplo de uso si se llama directamente
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        enviar_alerta_telegram(sys.argv[1])
    else:
        print("Uso: python3 telegram_alert.py 'Mensaje de prueba'")
