# main.py
# Versión final, refactorizada y corregida para Twilio Voice con el SDK `google-genai`.

import os
import json
import base64
import asyncio
import logging

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.websockets import WebSocketState
from dotenv import load_dotenv
from pydantic import BaseModel

# ================================================
# --- SDK de Google y Dependencias del Agente ---
# ================================================
import google.genai as genai
# 'generativelanguage_types' es el alias que usaremos para todos los tipos de genai
from google.genai import types as generativelanguage_types

from google.cloud import secretmanager
# IMPORTACIÓN CORREGIDA: Solo importamos RunConfig desde aquí
from google.adk.agents.run_config import RunConfig
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.agents import LiveRequestQueue
from google.adk.runners import Runner

# Dependencias de Google Calendar
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
import googleapiclient.discovery

# Agente Jarvis (Asegúrate de que la ruta sea correcta para tu estructura)
try:
    from app.jarvis.agent import root_agent
except ImportError:
    root_agent = None
    logging.error("No se pudo importar root_agent. La funcionalidad de voz no funcionará.")

# Twilio
from twilio.twiml.voice_response import VoiceResponse, Start

# ================================================
# 0. Configuración de Logging y Entorno
# ================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

GCP_PROJECT_ID_ENV = os.getenv("GCP_PROJECT_ID")
SERVER_BASE_URL = os.getenv("SERVER_BASE_URL")
APP_NAME = "Twilio Voice Agent Gemini"

google_calendar_creds = None
google_calendar_service = None
gemini_api_key_loaded = False

def get_project_id_for_secrets():
    effective_project_id = GCP_PROJECT_ID_ENV
    if not effective_project_id and not os.getenv("K_SERVICE"):
        logger.info("Desarrollo local: Cargando .env")
        load_dotenv()
        effective_project_id = os.getenv("GCP_PROJECT_ID_LOCAL")
    return effective_project_id

def get_secret_from_manager(secret_id, project_id):
    if not project_id:
        logger.error(f"No se puede obtener el secreto '{secret_id}' porque el ID del proyecto es nulo.")
        return None
    try:
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
        response = client.access_secret_version(name=name)
        return response.payload.data.decode("UTF-8")
    except Exception as e:
        logger.error(f"Error al obtener secreto '{secret_id}': {e}", exc_info=True)
        return None

def initialize_global_services():
    global google_calendar_creds, google_calendar_service, gemini_api_key_loaded
    project_id = get_project_id_for_secrets()
    if not project_id:
        logger.critical("ID de proyecto de GCP no disponible. No se pueden inicializar servicios.")
        return

    gemini_api_key = get_secret_from_manager("gemini-api-key", project_id)
    if gemini_api_key:
        genai.configure(api_key=gemini_api_key)
        gemini_api_key_loaded = True
        logger.info("API Key de Gemini configurada exitosamente.")
    else:
        logger.critical("GEMINI_API_KEY no se pudo cargar.")

    # (Lógica para inicializar el servicio de Google Calendar)
    # ...

session_service = InMemorySessionService()
app = FastAPI(title=APP_NAME, version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
async def startup_event_handler():
    logger.info("Iniciando aplicación FastAPI y configurando servicios globales...")
    initialize_global_services()

# ================================================
# SECCIÓN DE VOZ CON TWILIO Y GEMINI ADK
# ================================================
active_streams_sids = {}

if root_agent:
    @app.post("/voice", response_class=PlainTextResponse)
    async def voice_webhook(request: Request):
        try:
            form = await request.form()
            call_sid = form.get("CallSid")
            logger.info(f"📞 Llamada entrante de Twilio - SID: {call_sid}")
            response = VoiceResponse()
            
            # Asegúrate que la URL base del servidor esté configurada en las variables de entorno
            if not SERVER_BASE_URL:
                 logger.error("SERVER_BASE_URL no está configurada. No se puede iniciar el stream.")
                 response.say("Lo siento, hay un error de configuración en el servidor.", language="es-ES")
                 return PlainTextResponse(str(response), media_type="application/xml")

            # La URL del WebSocket debe usar wss:// y no incluir el protocolo en el dominio
            websocket_url = f"wss://{SERVER_BASE_URL.replace('https://', '')}/stream/{call_sid}"
            logger.info(f"Iniciando stream de Twilio a: {websocket_url}")
            
            start = Start()
            start.stream(url=websocket_url)
            response.append(start)
            response.say("Conectando con el asistente. Un momento, por favor.", language="es-ES")
            
            return PlainTextResponse(str(response), media_type="application/xml")
        except Exception as e:
            logger.error(f"Error crítico en el webhook /voice: {e}", exc_info=True)
            return PlainTextResponse("<Response><Say>Ha ocurrido un error inesperado al procesar la llamada.</Say></Response>", status_code=500, media_type="application/xml")

    # --- FUNCIÓN CORREGIDA Y FINAL ---
    def start_agent_session(session_id: str):
        logger.info(f"Iniciando sesión ADK para: {session_id}")
        session = session_service.create_session(app_name=APP_NAME, user_id=session_id, session_id=session_id)
        runner = Runner(app_name=APP_NAME, agent=root_agent, session_service=session_service)

        # --- CONFIGURACIÓN DE AUDIO CORRECTA PARA EL SDK MODERNO Y TWILIO ---
        # Se usan 'input_audio_config' y 'output_audio_config' que esperan un objeto 'AudioConfig'.
        # Esto reemplaza a las antiguas clases 'CustomSpeechConfig' y 'OutputAudioConfig'
        # y es la forma correcta de manejar streams de audio personalizados.
        run_config = RunConfig(
            response_modalities=["AUDIO", "TEXT"],
            input_audio_config=generativelanguage_types.AudioConfig(
                audio_encoding="MULAW", # Twilio envía audio en este formato
                sample_rate_hertz=8000
            ),
            output_audio_config=generativelanguage_types.AudioConfig(
                audio_encoding="MULAW", # Le pedimos a Gemini que responda en el mismo formato
                sample_rate_hertz=8000
            )
        )
        
        live_request_queue = LiveRequestQueue()
        live_events = runner.run_live(session=session, live_request_queue=live_request_queue, run_config=run_config)
        logger.info(f"Sesión ADK y runner iniciados exitosamente para {session_id}")
        return live_events, live_request_queue

    async def process_gemini_responses(websocket: WebSocket, call_sid: str, live_events):
        try:
            async for event in live_events:
                if hasattr(event, 'type') and event.type == generativelanguage_types.LiveEventType.OUTPUT_DATA:
                    if event.output_data and event.output_data.text_data:
                        logger.info(f"Texto de Gemini [{call_sid}]: {event.output_data.text_data.text}")
                    if event.output_data and event.output_data.audio_data:
                        audio_chunk = event.output_data.audio_data.data
                        logger.info(f"🔊 Audio de Gemini a Twilio [{call_sid}]: {len(audio_chunk)} bytes")
                        payload = base64.b64encode(audio_chunk).decode("utf-8")
                        stream_sid = active_streams_sids.get(call_sid)
                        if stream_sid:
                            await websocket.send_json({"event": "media", "streamSid": stream_sid, "media": {"payload": payload}})
                elif hasattr(event, 'type') and event.type == generativelanguage_types.LiveEventType.SESSION_ENDED:
                    logger.info(f"Sesión ADK finalizada por el agente para {call_sid}.")
                    break
                elif hasattr(event, 'type') and event.type == generativelanguage_types.LiveEventType.ERROR:
                    err_msg = event.error.message if hasattr(event, 'error') and hasattr(event.error, 'message') else 'Error desconocido en sesión ADK'
                    logger.error(f"Error en sesión ADK [{call_sid}]: {err_msg}")
                    break
        except WebSocketDisconnect:
            logger.info(f"WebSocket desconectado mientras se procesaban respuestas de Gemini para {call_sid}.")
        except Exception as e:
            logger.error(f"Error procesando respuestas de Gemini [{call_sid}]: {e}", exc_info=True)

    async def process_twilio_audio(websocket: WebSocket, call_sid: str, live_request_queue: LiveRequestQueue):
        try:
            while True:
                message_json = await websocket.receive_json()
                event_type = message_json.get("event")

                if event_type == "start":
                    stream_sid = message_json.get("start", {}).get("streamSid")
                    active_streams_sids[call_sid] = stream_sid
                    logger.info(f"🎙️ Stream de Twilio iniciado [{call_sid}]. streamSid: {stream_sid}")
                
                elif event_type == "media":
                    payload = message_json["media"]["payload"]
                    audio_data_raw = base64.b64decode(payload)
                    
                    if live_request_queue:
                        blob_data = generativelanguage_types.Blob(data=audio_data_raw, mime_type="audio/x-mulaw")
                        live_request_queue.send_realtime(blob_data)

                elif event_type == "stop":
                    logger.info(f"🔴 Stream de Twilio detenido por el cliente para {call_sid}. Cerrando cola.")
                    if live_request_queue:
                        live_request_queue.close()
                    break # Salir del bucle, ya que no habrá más audio.
        
        except WebSocketDisconnect:
            logger.info(f"WebSocket desconectado por Twilio para {call_sid}.")
        except Exception as e:
            logger.error(f"Error procesando audio de Twilio [{call_sid}]: {e}", exc_info=True)

    @app.websocket("/stream/{call_sid}")
    async def websocket_audio_endpoint(websocket: WebSocket, call_sid: str):
        await websocket.accept()
        logger.info(f"🔗 WebSocket aceptado para la llamada {call_sid}")
        live_events, live_request_queue = None, None
        
        try:
            live_events, live_request_queue = start_agent_session(call_sid)
            
            # Crear y ejecutar las dos tareas en paralelo
            twilio_task = asyncio.create_task(process_twilio_audio(websocket, call_sid, live_request_queue))
            gemini_task = asyncio.create_task(process_gemini_responses(websocket, call_sid, live_events))
            
            await asyncio.gather(twilio_task, gemini_task)

        except Exception as e:
            logger.error(f"Error en el manejador principal del WebSocket para {call_sid}: {e}", exc_info=True)
        finally:
            logger.info(f"🧹 Limpiando recursos para {call_sid}")
            if call_sid in active_streams_sids:
                del active_streams_sids[call_sid]
            
            if live_request_queue and not live_request_queue.is_closed():
                logger.info(f"Cerrando live_request_queue en el bloque finally para {call_sid}")
                live_request_queue.close()

            if websocket.client_state != WebSocketState.DISCONNECTED:
                await websocket.close(code=1000)
            logger.info(f"🔚 Conexión WebSocket cerrada para {call_sid}")

# ================================================
# Rutas de Estado y Raíz
# ================================================
@app.get("/", response_class=PlainTextResponse)
async def read_root():
    return "Servidor del agente de voz activo."

@app.get("/_ah/health", response_class=PlainTextResponse)
async def health_check():
    return "OK"

# ================================================
# Ejecución para Desarrollo Local
# ================================================
if __name__ == "__main__":
    if not os.getenv("K_SERVICE"):
        logger.info("Iniciando Uvicorn en modo de desarrollo local...")
        load_dotenv()
    
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)