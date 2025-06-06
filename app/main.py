# main.py

import os
import json
import base64
import asyncio
import logging

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.websockets import WebSocketState # Added import
from dotenv import load_dotenv # Solo para desarrollo local
from pydantic import BaseModel

# Dependencias de Google Cloud y ADK
from google.cloud import secretmanager
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.agents.run_config import RunConfig
from google.adk.agents import LiveRequestQueue
from google.adk.runners import Runner
from google.generativeai import types as generativelanguage_types
import google.generativeai as genai

# Dependencias de Google Calendar
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest # Renombrado para evitar colisión
import googleapiclient.discovery

# Agente Jarvis (asegúrate que la ruta sea correcta)
try:
    from app.jarvis.agent import root_agent

except ImportError:
    root_agent = None
    logging.error("No se pudo importar root_agent desde jarvis.agent. La funcionalidad de voz no funcionará.")

# Twilio
from twilio.twiml.voice_response import VoiceResponse, Start

# ================================================
# 0. Configuración de Logging y Carga de Entorno
# ================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- INICIO: Configuración de Secretos y Variables de Entorno ---
GCP_PROJECT_ID_ENV = os.getenv("GCP_PROJECT_ID") # Configurado en Cloud Run
SERVER_BASE_URL = os.getenv("SERVER_BASE_URL") # Para la URL del WebSocket de Twilio
APP_NAME = "Twilio Voice Agent Gemini" # Nombre de la aplicación para ADK

# Variables globales para servicios de Google
google_calendar_creds = None
google_calendar_service = None
gemini_api_key_loaded = False

# Este es un chunk de 200ms de silencio en audio mu-law 8kHz (estándar de Twilio).
# Se puede usar un WAV convertido a mu-law con sox o ffmpeg.
# Te dejo uno codificado en base64 para usar como ejemplo:
SILENCE_PAYLOAD = (
    "Qk2uAAAAAAAAADYAAAAoAAAAAAAoAAACAAAAAAAAAAAAAAQAAAAEAAAABAAAAAAAAAAAAAAAAAAAAAAAA"
)

def get_project_id_for_secrets():
    effective_project_id = GCP_PROJECT_ID_ENV
    if not effective_project_id and not os.getenv("K_SERVICE"):
        logger.info("Desarrollo local: GCP_PROJECT_ID no en env, cargando de .env como GCP_PROJECT_ID_LOCAL")
        load_dotenv()
        effective_project_id = os.getenv("GCP_PROJECT_ID_LOCAL")
        if not effective_project_id:
            logger.error("GCP_PROJECT_ID_LOCAL no encontrado en .env para desarrollo local.")
            return None
    elif not effective_project_id and os.getenv("K_SERVICE"):
        logger.error("Cloud Run: GCP_PROJECT_ID no configurado como variable de entorno.")
        return None
    return effective_project_id

def get_secret_from_manager(secret_id, project_id):
    if not project_id:
        logger.error(f"No se puede obtener el secreto '{secret_id}' porque project_id es nulo.")
        return None
    try:
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
        response = client.access_secret_version(name=name)
        return response.payload.data.decode("UTF-8")
    except Exception as e:
        logger.error(f"Error al obtener secreto '{secret_id}' (Proyecto: {project_id}): {e}", exc_info=True)
        return None

def initialize_global_services():
    global google_calendar_creds, google_calendar_service, gemini_api_key_loaded

    project_id = get_project_id_for_secrets()
    if not project_id:
        logger.critical("ID de proyecto de GCP no disponible. No se pueden inicializar servicios.")
        return

    # Configurar API Key de Gemini
    gemini_api_key = get_secret_from_manager("gemini-api-key", project_id)
    if gemini_api_key:
        try:
            genai.configure(api_key=gemini_api_key)
            gemini_api_key_loaded = True
            logger.info("API Key de Gemini configurada exitosamente.")
        except Exception as e:
            logger.error(f"Error configurando API Key de Gemini: {e}")
    else:
        logger.critical("GEMINI_API_KEY no se pudo cargar.")

    # Configurar credenciales de Google Calendar
    calendar_creds_json_str = get_secret_from_manager("google-calendar-credentials", project_id)
    calendar_token_json_str = get_secret_from_manager("google-calendar-token", project_id)

    if calendar_token_json_str and calendar_creds_json_str:
        try:
            token_info = json.loads(calendar_token_json_str)
            client_secrets_dict = json.loads(calendar_creds_json_str)
            scopes = token_info.get('scopes', ['https://www.googleapis.com/auth/calendar'])

            google_calendar_creds = Credentials(
                token=token_info.get('token'),
                refresh_token=token_info.get('refresh_token'),
                token_uri=token_info.get('token_uri', 'https://oauth2.googleapis.com/token'),
                client_id=client_secrets_dict.get('installed', {}).get('client_id'),
                client_secret=client_secrets_dict.get('installed', {}).get('client_secret'),
                scopes=scopes
            )

            if google_calendar_creds and google_calendar_creds.expired and google_calendar_creds.refresh_token:
                logger.info("Token de Google Calendar expirado, intentando refrescar...")
                google_calendar_creds.refresh(GoogleAuthRequest())
                logger.info("Token de Google Calendar refrescado.")
                # NOTA: Idealmente, guardarías el nuevo token_info en Secret Manager aquí.

            google_calendar_service = googleapiclient.discovery.build('calendar', 'v3', credentials=google_calendar_creds)
            logger.info("Servicio de Google Calendar inicializado.")
        except Exception as e:
            logger.error(f"Error al inicializar credenciales/servicio de Google Calendar: {e}", exc_info=True)
    else:
        if not calendar_creds_json_str: logger.critical("CALENDAR_CREDENTIALS_JSON_STR no se pudo cargar.")
        if not calendar_token_json_str: logger.critical("CALENDAR_TOKEN_JSON_STR no se pudo cargar.")

# SCALABILITY_NOTE: InMemorySessionService no es para producción escalada.
session_service = InMemorySessionService()
# --- FIN: Configuración de Secretos y Variables de Entorno ---

# ================================================
# 1. Inicializar FastAPI
# ================================================
app = FastAPI(title=APP_NAME, version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Llamar a la inicialización de servicios al inicio de la aplicación
@app.on_event("startup")
async def startup_event():
    logger.info("Iniciando aplicación FastAPI y configurando servicios globales...")
    initialize_global_services()
    if not root_agent:
        logger.warning("root_agent no está disponible. Las funcionalidades de voz que dependen de él no funcionarán.")
    if not SERVER_BASE_URL and os.getenv("K_SERVICE"): # Solo es crítico en Cloud Run para Twilio
        logger.error("SERVER_BASE_URL no está configurada en el entorno de Cloud Run. El webhook de voz de Twilio fallará.")


# ================================================
# SECCIÓN DE VOZ CON TWILIO Y GEMINI ADK
# ================================================

active_streams_sids = {} # Moved here to be accessible by send_keepalive

async def send_keepalive(websocket: WebSocket, call_sid: str):
    try:
        while True:
            stream_sid = active_streams_sids.get(call_sid)
            if stream_sid:
                keepalive_message = {
                    "event": "media",
                    "streamSid": stream_sid,
                    "media": {"payload": SILENCE_PAYLOAD}
                }
                await websocket.send_json(keepalive_message)
                logger.debug(f"Enviado keep-alive de silencio para {call_sid}")
            await asyncio.sleep(5)  # Cada 5 segundos
    except asyncio.CancelledError:
        logger.info(f"Tarea keep-alive cancelada para {call_sid}")
    except Exception as e:
        logger.error(f"Error en tarea keep-alive para {call_sid}: {e}", exc_info=True)

if root_agent: # Solo definir estos endpoints si el root_agent se importó correctamente
    @app.post("/voice", response_class=PlainTextResponse)
    async def voice_webhook(request: Request):
        try:
            form = await request.form()
            call_sid = form.get("CallSid")
            logger.info(f"📞 Nueva llamada recibida - Call SID: {call_sid if call_sid else 'NO_CALL_SID'}")

            if not call_sid:
                logger.error("Llamada recibida sin CallSid.")
                return PlainTextResponse("Error: CallSid no encontrado.", status_code=400, media_type="application/xml")

            response = VoiceResponse()
            start = Start()
            
            if not SERVER_BASE_URL:
                logger.error("SERVER_BASE_URL no está configurado. No se puede iniciar el stream de WebSocket.")
                response.say("Lo sentimos, hay un problema de configuración del servidor. No podemos continuar.", language="es-ES")
                return PlainTextResponse(str(response), media_type="application/xml", status_code=500)

            websocket_url = f"wss://{SERVER_BASE_URL}/stream/{call_sid}"
            logger.info(f"Iniciando stream hacia: {websocket_url}")
            start.stream(url=websocket_url)
            response.append(start)
            response.say(message="Hola, estás hablando con el asistente virtual de Gemini. Por favor, hable después del tono.", voice="alice", language="es-ES") # Initial prompt will handle this
            response.pause(length=1)
            return PlainTextResponse(str(response), media_type="application/xml")
        except Exception as e:
            logger.error(f"Error en /voice webhook: {e}", exc_info=True)
            error_response = VoiceResponse()
            error_response.say("Lo sentimos, ha ocurrido un error al procesar su llamada.", language="es-ES")
            return PlainTextResponse(str(error_response), media_type="application/xml", status_code=500)

    # def start_agent_session(session_id: str): # Changed to async
    async def start_agent_session(session_id: str):
        logger.info(f"Iniciando sesión Gemini ADK para session_id: {session_id}")
        session = session_service.create_session(
            app_name=APP_NAME, user_id=session_id, session_id=session_id,
        )
        runner = Runner(app_name=APP_NAME, agent=root_agent, session_service=session_service)

        # Mandar un mensaje inicial (opcional pero recomendado)
        initial_prompt = "Hola, soy el asistente virtual de Gemini. ¿En qué puedo ayudarte hoy?"
        try:
            # Nota: run_query devuelve una lista de partes de respuesta
            run_config_initial = RunConfig(response_modalities=["AUDIO", "TEXT"]) # Needs audio for initial twilio response
            # Adaptado para usar await en lugar de asyncio.run
            response = await runner.run_query(session=session, query=initial_prompt, run_config=run_config_initial)
            logger.info(f"Respuesta inicial de Gemini generada: {response}")
        except Exception as e:
            logger.error(f"Error generando respuesta inicial de Gemini: {e}", exc_info=True)


        speech_config = generativelanguage_types.SpeechConfig(
            voice_config=generativelanguage_types.VoiceConfig(
                prebuilt_voice_config=generativelanguage_types.PrebuiltVoiceConfig(voice_name="Puck")
            )
        )
        output_audio_config = generativelanguage_types.OutputAudioConfig(
            audio_encoding="OUTPUT_AUDIO_ENCODING_LINEAR_16",
            sample_rate_hertz=8000,
        )
        run_config_live = RunConfig( # Renamed to avoid conflict
            response_modalities=["AUDIO", "TEXT"],
            speech_config=speech_config,
            output_audio_config=output_audio_config,
        )
        live_request_queue = LiveRequestQueue()
        live_events = runner.run_live(
            session=session, live_request_queue=live_request_queue, run_config=run_config_live, # Use new run_config_live
        )
        logger.info(f"Sesión Gemini ADK y runner iniciados para {session_id}")
        return live_events, live_request_queue, session


    async def process_gemini_responses(websocket: WebSocket, call_sid: str, live_events):
        try:
            async for event in live_events:
                logger.debug(f"Evento de Gemini ADK para {call_sid}: {event.type if hasattr(event, 'type') else 'Tipo desconocido'}")
                if hasattr(event, 'type') and event.type == generativelanguage_types.LiveEventType.OUTPUT_DATA:
                    if event.output_data and event.output_data.text_data:
                        logger.info(f"Respuesta de texto de Gemini para {call_sid}: {event.output_data.text_data.text}")
                    if event.output_data and event.output_data.audio_data:
                        audio_chunk = event.output_data.audio_data.data
                        logger.info(f"🔊 Enviando audio de Gemini a Twilio para {call_sid}: {len(audio_chunk)} bytes")
                        payload = base64.b64encode(audio_chunk).decode("utf-8")
                        stream_sid = active_streams_sids.get(call_sid)
                        if not stream_sid:
                            logger.warning(f"No se encontró stream_sid para {call_sid} al enviar audio de Gemini.")
                            continue
                        message_to_twilio = {"event": "media", "streamSid": stream_sid, "media": {"payload": payload}}
                        await websocket.send_json(message_to_twilio)
                elif hasattr(event, 'type') and event.type == generativelanguage_types.LiveEventType.SESSION_ENDED:
                    logger.info(f"Sesión Gemini ADK finalizada para {call_sid} según evento.")
                    break
                elif hasattr(event, 'type') and event.type == generativelanguage_types.LiveEventType.ERROR:
                    err_msg = event.error.message if hasattr(event, 'error') and hasattr(event.error, 'message') else 'Error desconocido en sesión ADK'
                    logger.error(f"Error en sesión Gemini ADK para {call_sid}: {err_msg}")
                    break
        except WebSocketDisconnect:
            logger.info(f"WebSocket desconectado (Gemini responses) para {call_sid}.")
        except Exception as e:
            logger.error(f"Error procesando respuestas de Gemini para {call_sid}: {e}", exc_info=True)
        finally:
            logger.info(f"Finalizado el procesamiento de respuestas de Gemini para {call_sid}.")

    async def process_twilio_audio(websocket: WebSocket, call_sid: str, live_request_queue: LiveRequestQueue):
        try:
            while True:
                message_json = await websocket.receive_json()
                event_type = message_json.get("event")

                if event_type == "connected":
                    logger.info(f"🔌 WebSocket conectado (Twilio) para {call_sid}. Protocolo: {message_json.get('protocol')}")
                elif event_type == "start":
                    stream_sid = message_json.get("streamSid")
                    active_streams_sids[call_sid] = stream_sid
                    logger.info(f"🎙️ Stream de Twilio iniciado para {call_sid}. streamSid: {stream_sid}")
                elif event_type == "media":
                    payload = message_json["media"]["payload"]
                    audio_data_raw = base64.b64decode(payload)
                    # Asegurarse que live_request_queue esté disponible y no cerrado
                    if live_request_queue and not live_request_queue.is_closed:
                        blob_data = generativelanguage_types.Blob(data=audio_data_raw, mime_type="audio/x-mulaw") # Twilio usualmente envía mu-law
                        live_request_queue.send_realtime(blob_data)
                        logger.debug(f"🔊 Audio de Twilio enviado a Gemini para {call_sid}: {len(audio_data_raw)} bytes")
                    else:
                        logger.warning(f"live_request_queue no disponible o cerrada para {call_sid}. No se envía audio a Gemini.")
                elif event_type == "stop":
                    logger.info(f"🔴 Fin del stream de Twilio para {call_sid}")
                    if live_request_queue and not live_request_queue.is_closed: live_request_queue.close()
                    break
                elif event_type == "mark":
                    logger.info(f"✅ Evento Mark de Twilio para {call_sid}: {message_json.get('name')}")
        except WebSocketDisconnect:
            logger.info(f"WebSocket desconectado (Twilio audio) por Twilio para {call_sid}.")
        except Exception as e:
            logger.error(f"Error en WebSocket de Twilio para {call_sid}: {e}", exc_info=True)
        finally:
            if live_request_queue and not live_request_queue.is_closed: live_request_queue.close()
            logger.info(f"Finalizado el procesamiento de audio de Twilio para {call_sid}.")


    @app.websocket("/stream/{call_sid}")
    async def websocket_audio_endpoint(websocket: WebSocket, call_sid: str):
        await websocket.accept()
        logger.info(f"🔗 Conexión WebSocket de audio aceptada para {call_sid}")
        live_events, live_request_queue, adk_session = None, None, None
        keepalive_task = None # Initialize keepalive_task
        twilio_task = None
        gemini_task = None
        try:
            if not gemini_api_key_loaded: 
                logger.error(f"API Key de Gemini no cargada. No se puede iniciar sesión ADK para {call_sid}.")
                await websocket.close(code=1011, reason="Server configuration error: Gemini API Key not loaded")
                return
            if not google_calendar_service: 
                logger.error(f"Servicio de Google Calendar no inicializado. No se puede iniciar sesión ADK para {call_sid}.")
                await websocket.close(code=1011, reason="Server configuration error: Calendar service not initialized")
                return

            # live_events, live_request_queue, adk_session = start_agent_session(call_sid) # Changed to await
            live_events, live_request_queue, adk_session = await start_agent_session(call_sid)
            
            keepalive_task = asyncio.create_task(send_keepalive(websocket, call_sid)) # Create keepalive task
            twilio_task = asyncio.create_task(process_twilio_audio(websocket, call_sid, live_request_queue))
            gemini_task = asyncio.create_task(process_gemini_responses(websocket, call_sid, live_events))
            
            # await asyncio.gather(twilio_task, gemini_task)
            await asyncio.gather(twilio_task, gemini_task, keepalive_task) # Add keepalive_task to gather

        except Exception as e:
            logger.error(f"Error principal en WebSocket handler para {call_sid}: {e}", exc_info=True)
        finally:
            logger.info(f"🧹 Limpiando recursos para WebSocket {call_sid}")
            if keepalive_task: # Cancel keepalive task
                keepalive_task.cancel()
            # Cancel other tasks explicitly before closing queue or session
            if twilio_task and not twilio_task.done():
                twilio_task.cancel()
            if gemini_task and not gemini_task.done():
                gemini_task.cancel()
            
            # Wait for tasks to finish cancellation
            try:
                if twilio_task: await twilio_task
            except asyncio.CancelledError:
                logger.info(f"Twilio task cancelada para {call_sid}")
            try:
                if gemini_task: await gemini_task
            except asyncio.CancelledError:
                logger.info(f"Gemini task cancelada para {call_sid}")
            try:
                if keepalive_task: await keepalive_task
            except asyncio.CancelledError:
                logger.info(f"Keepalive task cancelada para {call_sid}")


            if live_request_queue and not live_request_queue.is_closed: live_request_queue.close()
            # Consider closing ADK session if applicable
            # if adk_session: session_service.close_session(adk_session.session_id)

            if call_sid in active_streams_sids: del active_streams_sids[call_sid]
            try:
                if websocket.client_state != WebSocketState.DISCONNECTED:
                    await websocket.close(code=1000) # Use code 1000 for normal closure
            except RuntimeError: pass 
            except Exception as e_close: logger.error(f"Error al cerrar websocket para {call_sid}: {e_close}")
            logger.info(f"🔚 Conexión WebSocket de audio cerrada para {call_sid}")
else:
    logger.warning("root_agent no está definido. Los endpoints /voice y /stream no estarán disponibles.")

# ================================================
# TUS RUTAS EXISTENTES (CHAT, WS, RUN)
# ================================================
class PromptRequest(BaseModel):
    prompt: str

@app.post("/chat")
async def process_chat(request: PromptRequest):
    logger.info(f"Solicitud de chat recibida con el prompt: {request.prompt}")
    if not root_agent or not gemini_api_key_loaded: 
        logger.error("Chat no disponible: root_agent o API Key de Gemini no configurados.")
        raise HTTPException(status_code=503, detail="Servicio de chat no disponible temporalmente.")

    try:
        run_config = RunConfig(response_modalities=["TEXT"])
        chat_session_id = f"chat_{os.urandom(8).hex()}"
        session = session_service.create_session(app_name=APP_NAME, user_id="chat_user", session_id=chat_session_id)
        runner = Runner(app_name=APP_NAME, agent=root_agent, session_service=session_service)
        
        response = await runner.run_query(session=session, query=request.prompt, run_config=run_config)
        final_text_response = "".join([part.text_data.text for part in response.parts if part.text_data])
        logger.info(f"Respuesta de chat: {final_text_response}")
        return {"response": final_text_response}
    except Exception as e:
        logger.error(f"Error en /chat: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error procesando chat: {str(e)}")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("Cliente WebSocket /ws conectado.")
    if not root_agent or not gemini_api_key_loaded:
        logger.error("WebSocket /ws no disponible: root_agent o API Key de Gemini no configurados.")
        await websocket.close(code=1011, reason="Servicio no disponible")
        return

    chat_session_id = f"ws_{os.urandom(8).hex()}" 
    session = session_service.create_session(app_name=APP_NAME, user_id="ws_user", session_id=chat_session_id)
    runner = Runner(app_name=APP_NAME, agent=root_agent, session_service=session_service)
    run_config = RunConfig(response_modalities=["TEXT"])

    try:
        while True:
            data = await websocket.receive_text()
            logger.info(f"Mensaje de /ws: {data}")
            response = await runner.run_query(session=session, query=data, run_config=run_config)
            final_text_response = "".join([part.text_data.text for part in response.parts if part.text_data])
            await websocket.send_text(final_text_response)
            logger.info(f"Respuesta enviada por /ws: {final_text_response}")
    except WebSocketDisconnect:
        logger.info("Cliente WebSocket /ws desconectado.")
    except Exception as e:
        logger.error(f"Error en WebSocket /ws: {e}", exc_info=True)
        try:
            if websocket.client_state != WebSocketState.DISCONNECTED:
                await websocket.close(code=1011)
        except RuntimeError: pass
    finally:
        logger.info("Conexión WebSocket /ws cerrada.")


@app.post("/v1/projects/{project_id}/locations/{location_id}/agents/{agent_id}:run")
async def run_agent_endpoint(project_id: str, location_id: str, agent_id: str, request: Request): 
    logger.info(f"Solicitud /run: {project_id}/{location_id}/{agent_id}")
    if not root_agent or not gemini_api_key_loaded:
        logger.error("/run no disponible: root_agent o API Key de Gemini no configurados.")
        raise HTTPException(status_code=503, detail="Servicio /run no disponible temporalmente.")
    try:
        request_body_bytes = await request.body()
        if not request_body_bytes: raise HTTPException(status_code=400, detail="Cuerpo vacío")
        request_body_str = request_body_bytes.decode('utf-8')
        data = json.loads(request_body_str)
        prompt_text = data.get("prompt") or data.get("input")
        if not prompt_text: raise HTTPException(status_code=400, detail="Falta prompt/input")
        
        run_session_id = f"run_{os.urandom(8).hex()}"
        session = session_service.create_session(app_name=APP_NAME, user_id="run_user", session_id=run_session_id)
        runner = Runner(app_name=APP_NAME, agent=root_agent, session_service=session_service)
        run_config = RunConfig(response_modalities=["TEXT"])

        response = await runner.run_query(session=session, query=prompt_text, run_config=run_config)
        output_text = "".join([part.text_data.text for part in response.parts if part.text_data])
        
        logger.info(f"Respuesta de /run: {output_text}")
        return PlainTextResponse(content=output_text)

    except json.JSONDecodeError as jde:
        logger.error(f"Error JSON en /run: {jde}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"JSON malformado: {str(jde)}")
    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        logger.error(f"Error inesperado en /run: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error interno del servidor: {str(e)}")


# ================================================
# RUTAS ESTÁNDAR
# ================================================
@app.get("/")
async def read_root():
    logger.info("Solicitud recibida en la ruta raíz (/).")
    return {"message": "Bienvenido al Agente de Jarvis con Voz - FastAPI Edition"}

@app.get("/_ah/health")
async def health_check():
    logger.info("Health check solicitado.")
    return PlainTextResponse("OK")

# ================================================
# Ejecución (para desarrollo local con Uvicorn)
# ================================================
if __name__ == "__main__":
    logger.info("Iniciando servidor FastAPI localmente con Uvicorn en puerto 8000...")
    if not os.getenv("K_SERVICE"):
        load_dotenv()
    
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=True)

