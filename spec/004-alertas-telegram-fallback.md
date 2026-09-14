# SPEC-004: Canal Directo de Alertas de Emergencia Telegram

- **ID**: `SPEC-004`
- **Título**: Canal de Notificación Directa a Operadores vía Telegram Bot API
- **Estado**: `Implementado`
- **Módulo**: `Módulo Ojos (telegram_alert.py)`
- **Última Actualización**: 2026-09-14

---

## 1. Propósito
Proveer un canal de comunicación directo y redundante hacia los teléfonos móviles de los operadores a través del Telegram Bot API. Este canal opera como respaldo autónomo en situaciones donde el Módulo Cerebro esté fuera de línea o ante eventos de emergencia calificados.

---

## 2. Configuración y Credenciales

- **Bot Token**: Inyectado por variable de entorno `TELEGRAM_BOT_TOKEN`.
  - Valor actual del sistema: `8990032616:AAFT732S-Q46GAaNXRs3bEs0-JidOZE7tjQ`
- **Chat ID**: Inyectado por variable de entorno `TELEGRAM_CHAT_ID`.
  - Valor actual del sistema: `7170575800`
- **Endpoint Telegram**: `POST https://api.telegram.org/bot<TOKEN>/sendMessage`
- **Formato**: `parse_mode: "Markdown"`
- **Timeout**: `5.0` segundos

---

## 3. Comportamiento en Tiempo de Ejecución
1. **Ejecución Asíncrona**: Cuando `camara_ptz.py` invoca una alerta de Telegram, debe despacharla en un hilo separado (`daemon=True`) para evitar retrasar el procesamiento de frames de video o los comandos PTZ.
2. **Fallback Silencioso**: Si la red WAN no tiene salida a Internet o Telegram falla, el error debe capturarse en logs (`[TELEGRAM] Excepción de red...`) sin propagar excepciones que detengan el servicio de visión.

---

## 4. Verificación
El envío puede ser validado ejecutando:
```bash
python3 /home/jetson/SARI/telegram_alert.py "[TEST] Verificación manual de alerta Telegram"
```
Criterio de éxito: Registro en consola `[TELEGRAM] Alerta enviada con éxito.` y recepción en el chat `7170575800`.
