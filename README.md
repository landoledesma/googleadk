# Google Calendar Integration for ADK Voice Assistant

This document explains how to set up and use the Google Calendar integration with your ADK Voice Assistant.

## Setup Instructions

### 1. Install Dependencies

First, create a virtual environment:

```bash
# Create a virtual environment
python -m venv .venv
```

Activate the virtual environment:

On Windows:
```bash
# Activate virtual environment on Windows
.venv\Scripts\activate
```

On macOS/Linux:
```bash
# Activate virtual environment on macOS/Linux
source .venv/bin/activate
```

Then, install all required Python packages using pip:

```bash
# Install all dependencies
pip install -r requirements.txt
```

### 2. Set Up Gemini API Key

1. Create or use an existing [Google AI Studio](https://aistudio.google.com/) account
2. Get your Gemini API key from the [API Keys section](https://aistudio.google.com/app/apikeys)
3. Set the API key as an environment variable:

Create a `.env` file in the project root with:

```
GOOGLE_API_KEY=your_api_key_here
```

### 3. Create a Google Cloud Project

1. Go to the [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select an existing one
3. Enable the Google Calendar API for your project:
   - In the sidebar, navigate to "APIs & Services" > "Library"
   - Search for "Google Calendar API" and enable it

### 4. Create OAuth 2.0 Credentials

1. In the Google Cloud Console, navigate to "APIs & Services" > "Credentials"
2. Click "Create Credentials" and select "OAuth client ID"
3. For application type, select "Desktop application"
4. Name your OAuth client (e.g., "ADK Voice Calendar Integration")
5. Click "Create"
6. Download the credentials JSON file
7. Save the file as `credentials.json` in the root directory of this project

### 5. Run the Setup Script

Run the setup script to authenticate with Google Calendar:

```bash
python setup_calendar_auth.py
```

This will:
1. Start the OAuth 2.0 authorization flow
2. Open your browser to authorize the application
3. Save the access token securely for future use
4. Test the connection to your Google Calendar

## Working with Multiple Calendars

The Google Calendar integration supports working with multiple calendars. The OAuth flow will grant access to all calendars associated with your Google account. You can:

1. List all available calendars using the voice command "What calendars do I have access to?"
2. Specify which calendar to use for operations by name or ID
3. Use your primary calendar by default if no calendar is specified

Examples:
- "Show me all my calendars"
- "Create a meeting in my Work calendar" 
- "What's on my Family calendar this weekend?"

## Using the Calendar Integration

Once set up, you can interact with your Google Calendar through the voice assistant:

### Examples:

- "What's on my calendar today?"
- "Show me my schedule for next week"
- "Create a meeting with John tomorrow at 2 PM"
- "Schedule a doctor's appointment for next Friday at 10 AM"
- "Find a free time slot for a 30-minute meeting tomorrow"
- "Delete my 3 PM meeting today"
- "Reschedule my meeting with Sarah to Thursday at 11 AM"
- "Change the title of my dentist appointment to 'Dental Cleaning'"

## Running the Application

After completing the setup, you can run the application using the following command:

```bash
# Start the ADK Voice Assistant with hot-reloading enabled
uvicorn main:app --reload
```

This will start the application server, and you can interact with your voice assistant through the provided interface.

## Troubleshooting

### Token Errors

If you encounter authentication errors:

1. Delete the token file at `~/.credentials/calendar_token.json`
2. Run the setup script again

### Permission Issues

If you need additional calendar permissions:

1. Delete the token file at `~/.credentials/calendar_token.json`
2. Edit the `SCOPES` variable in `app/jarvis/tools/calendar_utils.py`
3. Run the setup script again

### API Quota

Google Calendar API has usage quotas. If you hit quota limits:

1. Check your [Google Cloud Console](https://console.cloud.google.com/)
2. Navigate to "APIs & Services" > "Dashboard"
3. Select "Google Calendar API"
4. View your quota usage and consider upgrading if necessary

### Package Installation Issues

If you encounter issues installing the required packages:

1. Make sure you're using Python 3.8 or newer
2. Try upgrading pip: `pip install --upgrade pip`
3. Install packages individually if a specific package is causing problems

## Security Considerations

- The OAuth token is stored securely in your user directory
- Never share your `credentials.json` file or the generated token
- The application only requests the minimum permissions needed for calendar operations


---
¡Claro! Aquí tienes un resumen conciso para tu jefe senior, explicando el contexto, lo que se ha hecho, el objetivo y el problema actual:

Resumen del Proyecto: Asistente de Voz con Gemini, Google Calendar y Twilio en GCP

Objetivo Final:
Implementar un asistente de voz. Un usuario llama a un número de teléfono de Twilio, interactúa con un agente de Gemini (usando el Gemini ADK - Agent Development Kit), y este agente puede leer, crear y modificar eventos en Google Calendar según los comandos de voz del usuario. Todo esto desplegado en Google Cloud Platform (GCP).

Arquitectura y Servicios Clave:

Frontend de Voz: Twilio (recibe la llamada y gestiona el stream de audio).

Backend: Aplicación FastAPI en Python desplegada en Cloud Run (GCP).

Lógica del Agente: Gemini ADK (para definir el agente, sus herramientas y manejar la conversación).

Herramientas: Funciones Python personalizadas para interactuar con la API de Google Calendar.

Gestión de Secretos: Google Secret Manager (para API keys de Gemini, credenciales de Google Calendar).

Despliegue: Repositorio en GitHub, Docker para empaquetar la aplicación, y Cloud Run para la ejecución. Se está usando IDX (IDE en la nube de Google) que facilita ciertos despliegues a Cloud Run.

Qué se ha Hecho y Funciona (o Debería Funcionar):

Código Base: Se tiene una aplicación FastAPI (main.py) y la definición del agente (jarvis/agent.py con sus herramientas en jarvis/tools/).

Dockerfile: Creado para empaquetar la aplicación.

Secretos en GCP: La API Key de Gemini y las credenciales de Google Calendar (credentials.json, token.json) están almacenadas en Google Secret Manager.

Cloud Run:

La aplicación se ha desplegado en Cloud Run usando IDX o gcloud run deploy.

El servicio Cloud Run está configurado para ser públicamente accesible.

Las variables de entorno necesarias (GCP_PROJECT_ID, SERVER_BASE_URL) están (o deberían estar) configuradas.

La cuenta de servicio de Cloud Run tiene el permiso Secret Manager Secret Accessor para acceder a los secretos.

Uvicorn y FastAPI parecen estar arrancando (el servicio responde, aunque con errores).

Configuración de Twilio: El número de Twilio está configurado para hacer un HTTP POST al endpoint /voice del servicio Cloud Run cuando entra una llamada.

Problema Actual y Dónde Estamos Atascados:
Cuando se llama al número de Twilio:

Twilio llama correctamente al endpoint /voice de la aplicación en Cloud Run.

Sin embargo, la aplicación Cloud Run responde con un error 404 Not Found a esta solicitud de Twilio.

Los logs de la aplicación en Cloud Run revelan la causa raíz:

ERROR:root:No se pudo importar root_agent desde jarvis.agent. La funcionalidad de voz no funcionará.
WARNING:app.main:root_agent no está definido. Los endpoints /voice y /stream no estarán disponibles.


Esto significa que el main.py no puede importar el root_agent desde app/jarvis/agent.py. Como consecuencia, los endpoints @app.post("/voice") y @app.websocket("/stream/{call_sid}") (que están dentro de una condición if root_agent:) no se definen, y por eso la aplicación devuelve un 404.

Investigación Actual del Problema (ImportError):
Se ha verificado:

La estructura del proyecto (Project Root/app/main.py, Project Root/app/jarvis/agent.py, etc.).

El Dockerfile (cómo copia los archivos y cómo ejecuta Uvicorn: CMD ["uvicorn", "app.main:app", ...]). Esta parte parece correcta para la estructura dada.

La presencia de archivos __init__.py en las carpetas app/, app/jarvis/, y app/jarvis/tools/ para que Python las trate como paquetes.

La hipótesis principal es que el ImportError de jarvis.agent se debe a un problema dentro de app/jarvis/agent.py o de los módulos que este importa (principalmente de app/jarvis/tools/...). Por ejemplo:

Un error de sintaxis en agent.py o en algún archivo de tools.

Una importación fallida dentro de agent.py o los archivos de tools (ej. una dependencia que falta en requirements.txt pero que una herramienta necesita, o un problema en cómo las herramientas se importan entre sí o desde tools/__init__.py).

Un problema en cómo las herramientas definidas en app/jarvis/tools/ están siendo exportadas por app/jarvis/tools/__init__.py y luego importadas en app/jarvis/agent.py.

Siguiente Paso Sugerido para Diagnóstico:
Realizar pruebas de importación local detalladas (desde Project Root/, con el entorno virtual activado) para aislar exactamente qué módulo o archivo está causando el fallo de importación en la cadena app.main -> app.jarvis.agent -> app.jarvis.tools.

Espero que este resumen sea útil para tu jefe. Cubre el qué, el cómo, el porqué, y dónde está el bloqueo actual.