# Guía de Configuración de Odysseus para SARI

Esta guía explica cómo configurar **Odysseus** para actuar como el "Cerebro" de SARI, procesar las alertas de intrusión del Módulo Ojo (vía el middleware `cerebro.py`), utilizar `llama3.1` localmente y conectar servidores MCP para actuar físicamente.

---

## 1. Configuración del LLM Local (Ollama)

Como ya tienes `llama3.1:latest` cargado (se visualiza en tu interfaz de Odysseus), asegúrate de que esté configurado como el modelo por defecto para tu Agente:

1. Ve a **Settings (Ajustes)** haciendo clic en el engranaje del panel inferior izquierdo (junto a tu usuario `sariat`).
2. En la sección **Models (Modelos)**, asegúrate de que el proveedor de Ollama apunte a tu servidor local de Ollama (por defecto `http://localhost:11434` o `http://host.docker.internal:11434`).
3. Selecciona `llama3.1:latest` como modelo preferido para tareas de Agente.

---

## 2. Creación del Agente de Seguridad "Cerebro"

Debes configurar un **Agente Persona** específico en Odysseus para procesar intrusiones:

1. Ve a **Settings → Agents / Personas**.
2. Haz clic en **Create Persona (Crear Persona)** y nómbralo `SARI Cerebro`.
3. Configura el **System Prompt** con las siguientes instrucciones:
   ```text
   Eres el "Cerebro" del Sistema de Alerta y Reconocimiento Inteligente (SARI).
   Tu trabajo es recibir reportes de intrusiones del middleware (Ojo PTZ) y tomar decisiones críticas.
   Dispones de herramientas MCP para controlar dispositivos físicos.
   
   Protocolo de actuación:
   1. Evalúa el nivel de amenaza (cantidad de personas, hora, persistencia).
   2. Si la amenaza es real:
      a. Envía una alerta a Telegram usando el módulo de emergencias.
      b. Activa las sirenas de alerta utilizando la herramienta MCP disponible.
      c. Si es necesario, indica a la cámara que congele su tracking (set_tracking = False) para no perder el ángulo de visión de la zona.
   3. Documenta cada incidente de forma ejecutiva en la base de conocimientos.
   ```
4. Asigna el modelo `llama3.1:latest` a este Agente.

---

## 3. Integración del Webhook (Middleware → Odysseus)

Para que el middleware `cerebro.py` pueda "despertar" a tu agente en Odysseus ante una intrusión:

1. Ve a **Settings → Integrations (Integraciones)**.
2. Busca la sección de **Webhooks / API Tokens** y genera un API Token de administrador.
3. Copia el token y colócalo en el script `cerebro.py` como encabezado de autorización (`Authorization: Bearer <TU_TOKEN>`).
4. Si Odysseus soporta webhooks entrantes nativos (por ejemplo para disparar chats/flujos de agentes), toma la URL de la integración (por defecto suele ser `http://<IP_ODYSSEUS>:7000/api/v1/webhook` o similar según tus configuraciones de contenedores) y actualiza la variable `ODYSSEUS_WEBHOOK_URL` en `cerebro.py`.

---

## 4. Configuración del Servidor MCP (IoT y Sensores)

Una vez que tu compañero de IoT desarrolle el script `iot_mcp_server.py`, deberás agregarlo en Odysseus para que el Agente IA pueda invocar los sensores y actuadores físicos:

1. Ve a la barra lateral izquierda y haz clic en **Tools (Herramientas)** o navega a **Settings → Integrations → Add MCP Server**.
2. Configura el servidor:
   * **Nombre**: `SARI IoT`
   * **Tipo de Transporte**: `stdio` (si corre en la misma máquina/contenedor) o `sse` (si corre de forma remota).
   * **Comando** (para stdio): `python3 /ruta/a/iot_mcp_server.py`
3. Guarda la configuración.
4. El agente `SARI Cerebro` detectará automáticamente herramientas disponibles como `activar_sirena` o `llamar_policia` y las invocará bajo su propio criterio cuando ocurra una intrusión.
