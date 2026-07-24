# Especificación: Integración Middleware a Odysseus

## Qué hace esta feature
Este módulo sirve como intermediario (middleware) entre las cámaras "Ojo" (SARI) y el "Cerebro" (Odysseus).
Su objetivo es no saturar a Odysseus con datos crudos, realizando un conteo básico de detecciones en la periferia.

## Criterios de Aceptación
1. Recibe telemetría por WebSockets en el puerto 8765.
2. Si se detectan 3 o más personas, se dispara un webhook HTTP a Odysseus.
3. Cuenta con un cooldown de 30 segundos entre envíos para evitar spam.
4. Opcionalmente, bloquea el seguimiento automático (PTZ freeze) enviando un comando de vuelta a la cámara.

## Bucle de Loop Engineering (Verificación)
- **Actuar**: Se levanta el servidor `cerebro.py` localmente.
- **Observar**: Se monitorean los logs al conectar una cámara simulada o real.
- **Corregir**: Si el webhook falla (HTTP 500 o timeout), ajustar URL y timeout en el código antes de mandar a producción.
