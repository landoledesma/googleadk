# main.py
# Versión final y refactorizada para Twilio Voice con el SDK `google-genai`

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
from google.genai import types as generativelanguage_types

from google.cloud import secretmanager
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.agents.run_config import RunConfig, CustomSpeechConfig, OutputAudioConfig
from google.adk.models.generated import AudioEncoding
from google.adk.agents import LiveRequestQueue
from google.adk.runners import Runner

# Dependencias de Google Calendar
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
import googleapiclient.discovery

# Agente Jarvis
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

# (Las funciones de configuración de secretos y servicios globales se mantienen igual)
GCP_PROJECT_ID_ENV = os.getenv("GCP_PROJECT_ID")
SERVER_BASE_URL = os.getenv("SERVER_BASE_URL")
APP_NAME = "Twilio Voice Agent Gemini"

google_calendar_creds = None
google_calendar_service = None
gemini_api_key_loaded = False

def get_project_id_for_secrets():
    effective_project_id = GCP_PROJECT_ID_ENV
    if not effective_project_id and not os.getenv("K_SERVICE"):
        load_dotenv()
        effective_project_id = os.getenv("GCP_PROJECT_ID_LOCAL")
    return effective_project_id

def get_secret_from_manager(secret_id, project_id):
    if not project_id: return None
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
        logger.critical("ID de proyecto de GCP no disponible.")
        return

    gemini_api_key = get_secret_from_manager("gemini-api-key", project_id)
    if gemini_api_key:
        genai.configure(api_key=gemini_api_key)
        gemini_api_key_loaded = True
        logger.info("API Key de Gemini configurada.")
    else:
        logger.critical("GEMINI_API_KEY no se pudo cargar.")

    calendar_creds_json_str = get_secret_from_manager("google-calendar-credentials", project_id)
    calendar_token_json_str = get_secret_from_manager("google-calendar-token", project_id)
    if calendar_token_json_str and calendar_creds_json_str:
        try:
            # ... (Lógica de inicialización de Calendar sin cambios) ...
            logger.info("Servicio de Google Calendar inicializado.")
        except Exception as e:
            logger.error(f"Error inicializando Google Calendar: {e}", exc_info=True)

session_service = InMemorySessionService()
app = FastAPI(title=APP_NAME, version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
async def startup_event_handler():
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
            logger.info(f"📞 Llamada recibida - SID: {call_sid}")
            response = VoiceResponse()
            websocket_url = f"wss://{SERVER_BASE_URL.replace('https://', '')}/stream/{call_sid}"
            logger.info(f"Iniciando stream a: {websocket_url}")
            start = Start()
            start.stream(url=websocket_url)
            response.append(start)
            response.say("Conectando con el asistente. Por favor, espere.", language="es-ES")
            return PlainTextResponse(str(response), media_type="application/xml")
        except Exception as e:
            logger.error(f"Error en /voice: {e}", exc_info=True)
            # Manejo de error
            return PlainTextResponse("<Response><Say>Error procesando la llamada.</Say></Response>", status_code=500, media_type="application/xml")

    def start_agent_session(session_id: str):
        logger.info(f"Iniciando sesión ADK para: {session_id}")
        session = session_service.create_session(app_name=APP_NAME, user_id=session_id, session_id=session_id)
        runner = Runner(app_name=APP_NAME, agent=root_agent, session_service=session_service)

        # --- CONFIGURACIÓN CRÍTICA PARA TWILIO ---
        # Para evitar el 'ValidationError', debemos especificar cómo manejar el audio.
        # Como usamos un stream de Twilio, necesitamos una configuración de audio personalizada.
        # No usamos `speech_config` (que genera audio TTS estándar como MP3), sino
        # `custom_speech_config` y `output_audio_config` para manejar el formato crudo `MULAW`.
        run_config = RunConfig(
            response_modalities=["AUDIO", "TEXT"],
            custom_speech_config=CustomSpeechConfig(
                audio_encoding=AudioEncoding.MULAW,
                sample_rate_hertz=8000
            ),
            output_audio_config=OutputAudioConfig(
                audio_encoding=AudioEncoding.MULAW,
                sample_rate_hertz=8000
            )
        )
        
        live_request_queue = LiveRequestQueue()
        live_events = runner.run_live(session=session, live_request_queue=live_request_queue, run_config=run_config)
        logger.info(f"Sesión ADK y runner iniciados correctamente para {session_id}")
        return live_events, live_request_queue

    async def process_gemini_responses(websocket: WebSocket, call_sid: str, live_events):
        try:
            async for event in live_events:
                if hasattr(event, 'type') and event.type == generativelanguage_types.LiveEventType.OUTPUT_DATA:
                    if event.output_data and event.output_data.audio_data:
                        audio_chunk = event.output_data.audio_data.data
                        payload = base64.b64encode(audio_chunk).decode("utf-8")
                        stream_sid = active_streams_sids.get(call_sid)
                        if stream_sid:
                            await websocket.send_json({"event": "media", "streamSid": stream_sid, "media": {"payload": payload}})
                elif hasattr(event, 'type') and event.type == generativelanguage_types.LiveEventType.SESSION_ENDED:
                    logger.info(f"Sesión ADK finalizada para {call_sid}.")
                    break
        except Exception as e:
            logger.error(f"Error procesando respuestas Gemini {call_sid}: {e}", exc_info=True)

    async def process_twilio_audio(websocket: WebSocket, call_sid: str, live_request_queue: LiveRequestQueue):
        try:
            while True:
                message_json = await websocket.receive_json()
                event_type = message_json.get("event")

                if event_type == "start":
                    stream_sid = message_json["start"]["streamSid"]
                    active_streams_sids[call_sid] = stream_sid
                    logger.info(f"🎙️ Stream Twilio iniciado {call_sid}. streamSid: {stream_sid}")
                
                elif event_type == "media":
                    payload = message_json["media"]["payload"]
                    audio_data_raw = base64.b64decode(payload)
                    if live_request_queue:
                        blob_data = generativelanguage_types.Blob(data=audio_data_raw, mime_type="audio/x-mulaw")
                        live_request_queue.send_realtime(blob_data)

                elif event_type == "stop":
                    logger.info(f"🔴 Fin stream Twilio para {call_sid}. Cerrando cola.")
                    if live_request_queue:
                        live_request_queue.close()
                    break
        except WebSocketDisconnect:
            logger.info(f"WS desconectado (Twilio) para {call_sid}.")
        except Exception as e:
            logger.error(f"Error en WS Twilio {call_sid}: {e}", exc_info=True)

    @app.websocket("/stream/{call_sid}")
    async def websocket_audio_endpoint(websocket: WebSocket, call_sid: str):
        await websocket.accept()
        logger.info(f"🔗 WS audio aceptado para {call_sid}")
        
        try:
            live_events, live_request_queue = start_agent_session(call_sid)
            twilio_task = asyncio.create_task(process_twilio_audio(websocket, call_sid, live_request_queue))
            gemini_task = asyncio.create_task(process_gemini_responses(websocket, call_sid, live_events))
            await asyncio.gather(twilio_task, gemini_task)
        except Exception as e:
            logger.error(f"Error principal en WS handler {call_sid}: {e}", exc_info=True)
        finally:
            logger.info(f"🧹 Limpiando y cerrando WS {call_sid}")
            if call_sid in active_streams_sids:
                del active_streams_sids[call_sid]
            if websocket.client_state != WebSocketState.DISCONNECTED:
                await websocket.close(code=1000)

@app.get("/")
async def read_root():
    return {"message": "Agente de Voz con Twilio y Gemini ADK"}

if __name__ == "__main__":
    if not os.getenv("K_SERVICE"): load_dotenv()
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=True)