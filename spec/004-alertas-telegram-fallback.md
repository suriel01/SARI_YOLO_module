# SPEC-004: Canal Directo de Alertas de Emergencia Telegram

- **ID**: `SPEC-004`
- **Título**: Canal de Notificación Directa a Operadores vía Telegram Bot API
- **Estado**: `Implementado`
- **Módulo**: `Módulo Ojos (sari_eyes/telegram.py, sari_eyes/alert_queue.py)`
- **Última Actualización**: 2026-11-13

---

## 1. Propósito
Proveer un canal directo y redundante hacia los operadores a través del Telegram Bot API cuando el Módulo Cerebro no está disponible. El Cerebro sigue siendo el canal principal y el único que decide la sirena (SPEC-001).

---

## 2. Configuración y Credenciales

- **Bot Token**: variable de entorno `TELEGRAM_BOT_TOKEN` (obligatoria; el nodo no arranca sin ella).
- **Chat ID**: variable de entorno `TELEGRAM_CHAT_ID` (obligatoria).
- Los valores viven solo en `.env` (no versionado) o en el gestor de secretos del despliegue. Nunca se escriben en código, specs, `docker-compose.yml` ni logs.
- **Endpoint Telegram**: `POST https://api.telegram.org/bot<TOKEN>/sendMessage` (texto plano, sin `parse_mode`).
- **Timeout**: `5.0` segundos. Los errores de red se registran solo por tipo, porque la URL contiene el token.

---

## 3. Comportamiento en Tiempo de Ejecución
1. **Cola persistente**: toda alerta (intrusión o sabotaje) se escribe primero en la cola en disco (`ALERT_QUEUE_DIR`, por defecto `alert_queue/`) y se reintenta con backoff exponencial (2 s … 60 s) por REST y luego MQTT hasta que el Cerebro la acepte.
2. **Fallback automático**: si una alerta sigue sin entregarse 30 s después de crearse, se envía un único mensaje directo de Telegram indicando que el Cerebro no responde. Los reintentos hacia el Cerebro continúan; si Telegram falla, se reintenta en el siguiente intento.
3. **Sin bloqueo**: el despacho corre en el hilo `AlertDispatcher`; nunca retrasa la inferencia ni los comandos PTZ.

---

## 4. Verificación
```bash
python3 telegram_alert.py "[TEST] Verificación manual de alerta Telegram"
```
Criterio de éxito: salida `[TELEGRAM] Alerta enviada con éxito.` y recepción en el chat configurado. Las pruebas unitarias (`pytest -q`, `tests/test_alert_queue.py`, `tests/test_telegram.py`) cubren el fallback sin red.
