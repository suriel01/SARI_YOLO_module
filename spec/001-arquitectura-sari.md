# SPEC-001: Arquitectura General del Ecosistema SARI

- **ID**: `SPEC-001`
- **Título**: Arquitectura y Separación de Responsabilidades SARI
- **Estado**: `Aprobado`
- **Módulos**: `Módulo Ojos (Jetson Orin)` / `Módulo Cerebro (Servidor Central)`
- **Última Actualización**: 2026-09-14

---

## 1. Propósito
Definir de manera formal y vinculante la separación de responsabilidades entre el **Módulo Ojos** (borde físico / percepción sensorial) y el **Módulo Cerebro** (núcleo analítico / agente de toma de decisiones).

---

## 2. Definición de Roles y Fronteras

### 2.1 Módulo Ojos (Edge AI — NVIDIA Jetson)
- **Rol**: Percepción sensorial periférica, aceleración de red neuronal en hardware, control cinemático de actuadores PTZ y streaming de video.
- **Límites de decisión**: El Módulo Ojos **NO** decide si activar sirenas generales, contactar a la policía o coordinar respuestas complejas. Su tarea es clasificar personas en tiempo real, rastrearlas espacialmente y emitir telemetría y eventos con evidencia visual al Módulo Cerebro.

### 2.2 Módulo Cerebro (Servidor Central — SARI Brain Agent)
- **Rol**: Razonamiento, evaluación táctica de incidentes, almacenamiento de evidencias y orquestación física (sirenas, reflectores, cerraduras).
- **Límites de decisión**: El Módulo Cerebro actúa como el decisor autónomo. Recibe las alertas enriquecidas y la telemetría continua de los nodos perimetrales, determina el nivel de escalamiento y puede comandar dinámicamente al Módulo Ojos (por ejemplo: congelar tracking para mantener ángulo general).

---

## 3. Contratos de Comunicación

```
[Módulo Ojos (Jetson)] ────────────── (MQTT / HTTP) ─────────────► [Módulo Cerebro]
        │                                                                │
        ├─► sari/nodes/{id}/status (LWT & Birth) ────────────────────────┤
        ├─► sari/nodes/{id}/telemetry (Cada 3s) ─────────────────────────┤
        ├─► sari/alerts (Eventos con Base64 Snapshot) ───────────────────┤
        ├─◄ sari/nodes/{id}/config (Comandos de tracking y ajustes) ─────┤
        └─◄ HTTP GET :8080/snapshot (Extracción fotográfica bajo demanda)┤
```

---

## 4. Criterios de Aceptación
1. El Módulo Ojos debe poder operar autónomamente en su bucle local de visión/PTZ incluso si el enlace con el Módulo Cerebro se interrumpe temporalmente.
2. Toda comunicación con el Módulo Cerebro debe implementar reintentos continuos sin bloquear el hilo de inferencia YOLO.
3. El sistema no debe contener referencias a herramientas externas no aprobadas (e.g. Odysseus).
