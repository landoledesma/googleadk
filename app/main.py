# main.py
# VERSIÓN CON RESPONSE_MODALITIES COMO ENUM Y SPEECH_CONFIG BÁSICO

import os
import json
import base64
import asyncio
import logging
import websockets
import audioop

# --- HABILITAR LOGGING DEBUG PARA LIBRERÍAS DE GOOGLE ---
logging.getLogger('google_adk').setLevel(logging.DEBUG)
logging.getLogger('google.adk').setLevel(logging.DEBUG)
logging.getLogger('google.genai').setLevel(logging.DEBUG)
# --- FIN HABILITAR LOGGING DEBUG ---

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.websockets import WebSocketState
from dotenv import load_dotenv

from typing import AsyncIterable, List, Optional # Añadido Optional para speech_synthesis_config

from google.adk.events.event import Event
from google.genai import types as generativelanguage_types
from google.adk.sessions.session import Session 

from google.adk.agents.run_config import RunConfig
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.agents import LiveRequestQueue
from google.adk.runners import Runner

from twilio.twiml.voice_response import VoiceResponse, Connect

# Agente Jarvis (con herramientas)
try:
    from app.jarvis.agent import root_agent
except ImportError:
    try:
        from jarvis.agent import root_agent
    except ImportError:
        root_agent = None
        logging.error("No se pudo importar root_agent. La funcionalidad de voz no funcionará.")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

if not os.getenv("K_SERVICE"):
    load_dotenv()

APP_NAME = "Twilio Voice Agent Gemini"
SERVER_BASE_URL = os.getenv("SERVER_BASE_URL")

if not os.getenv("GOOGLE_API_KEY"):
    logger.warning("ADVERTENCIA: La variable de entorno GOOGLE_API_KEY no está configurada.")
if not SERVER_BASE_URL and root_agent:
    logger.error("ERROR CRÍTICO: La variable de entorno SERVER_BASE_URL no está configurada.")

session_service = InMemorySessionService()
app = FastAPI(title=APP_NAME, version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

active_streams_sids = {}

if root_agent:
    @app.post("/voice", response_class=PlainTextResponse)
    async def voice_webhook(request: Request):
        try:
            form = await request.form()
            call_sid = form.get("CallSid")
            logger.info(f"📞 Llamada entrante de Twilio - CallSid: {call_sid}")
            response = VoiceResponse()
            if not SERVER_BASE_URL:
                 logger.error(f"SERVER_BASE_URL no configurado para CallSid: {call_sid}")
                 response.say("Error de configuración del servidor.", language="es-ES")
                 return PlainTextResponse(str(response), media_type="application/xml")
            websocket_url_host = SERVER_BASE_URL.replace("https://", "").replace("http://", "")
            websocket_url = f"wss://{websocket_url_host}/stream/{call_sid}"
            logger.info(f"Instruyendo a Twilio que se conecte a: {websocket_url} para CallSid: {call_sid}")
            connect = Connect()
            connect.stream(url=websocket_url)
            response.append(connect)
            response.pause(length=60)
            logger.info(f"Respondiendo a Twilio con TwiML para CallSid {call_sid}: {str(response)}")
            return PlainTextResponse(str(response), media_type="application/xml")
        except Exception as e:
            logger.error(f"Error en /voice para CallSid: {form.get('CallSid', 'Desconocido')}: {e}", exc_info=True)
            error_response = VoiceResponse()
            error_response.say("Error al procesar la llamada.", language="es-ES")
            return PlainTextResponse(str(error_response), status_code=500, media_type="application/xml")


    async def start_agent_session(session_id_str: str, is_audio_mode: bool = True):
        logger.info(f"Iniciando y creando sesión ADK para CallSid: {session_id_str}, Audio Mode: {is_audio_mode}")
        
        session_obj: Session = await session_service.create_session(
            app_name=APP_NAME, user_id=session_id_str, session_id=session_id_str
        )
        logger.info(f"Sesión ADK creada explícitamente para CallSid: {session_id_str}")

        runner = Runner(
            app_name=APP_NAME, agent=root_agent, session_service=session_service
        )
        
        active_modalities: List[generativelanguage_types.Modality] = []
        speech_synthesis_config: Optional[generativelanguage_types.SpeechConfig] = None

        if is_audio_mode:
            active_modalities.append(generativelanguage_types.Modality.AUDIO)
            speech_synthesis_config = generativelanguage_types.SpeechConfig(
                language_code="es-ES"
            )
            logger.info(f"Modo audio activado. SpeechConfig: {speech_synthesis_config}")
        
        if not active_modalities:
             active_modalities.append(generativelanguage_types.Modality.TEXT)

        run_config = RunConfig(
            response_modalities=active_modalities,
            speech_config=speech_synthesis_config
        )
        logger.info(f"Usando RunConfig: {run_config}")
        
        live_request_queue = LiveRequestQueue()
        
        live_events_generator = runner.run_live(
            session=session_obj,
            live_request_queue=live_request_queue,
            run_config=run_config
        )
        logger.info(f"Runner.run_live invocado para CallSid: {session_id_str} pasando objeto Session.")
        return live_events_generator, live_request_queue

    # --- INICIO DEL CÓDIGO MODIFICADO ---
    async def process_gemini_responses(websocket: WebSocket, call_sid: str, live_events: AsyncIterable[Event]):
        logger.info(f"Iniciando `process_gemini_responses` para CallSid: {call_sid}")
        try:
            async for event in live_events:
                if websocket.client_state == WebSocketState.DISCONNECTED:
                    logger.warning(f"WebSocket desconectado (Gemini->Twilio) para CallSid: {call_sid}. Terminando.")
                    break

                # Por defecto, asumimos que no hay datos de audio o texto en este evento
                audio_data_to_send = None
                text_data_to_log = None

                # Escenario 1: Evento con atributo 'type' (estructura esperada anteriormente)
                if hasattr(event, 'type'):
                    logger.debug(f"Evento ADK con 'type': {event.type} para CallSid: {call_sid}")
                    if event.type == generativelanguage_types.LiveEventType.OUTPUT_DATA:
                        if hasattr(event, 'output_data') and event.output_data:
                            if hasattr(event.output_data, 'audio_data') and event.output_data.audio_data:
                                pcm_audio_from_gemini = event.output_data.audio_data.data
                                audio_data_to_send = pcm_audio_from_gemini
                            if hasattr(event.output_data, 'text_data') and event.output_data.text_data:
                                text_data_to_log = event.output_data.text_data.text
                    elif event.type == generativelanguage_types.LiveEventType.SESSION_ENDED:
                        logger.info(f"Evento SESSION_ENDED de ADK recibido para CallSid: {call_sid}.")
                        break 
                    elif event.type == generativelanguage_types.LiveEventType.ERROR:
                        logger.error(f"Error en evento ADK para CallSid: {call_sid}: {getattr(event, 'error', 'Error desconocido')}")
                        break
                
                # Escenario 2: Evento que contiene 'content' directamente (nuevo caso observado)
                elif hasattr(event, 'content') and event.content and hasattr(event.content, 'parts') and event.content.parts:
                    logger.debug(f"Evento ADK con 'content' directo para CallSid: {call_sid}")
                    for part in event.content.parts:
                        if hasattr(part, 'inline_data') and part.inline_data and part.inline_data.mime_type and part.inline_data.mime_type.startswith("audio/"):
                            pcm_audio_from_gemini = part.inline_data.data
                            audio_data_to_send = pcm_audio_from_gemini
                        if hasattr(part, 'text') and part.text:
                            text_data_to_log = part.text
                else:
                    logger.warning(f"Evento ADK no reconocido o sin datos procesables para CallSid: {call_sid}. Evento: {event}")
                    continue # Saltar al siguiente evento

                # Procesar audio si se encontró
                if audio_data_to_send:
                    logger.debug(f"Procesando audio PCM de Gemini ({len(audio_data_to_send)} bytes) para CallSid: {call_sid}")
                    try:
                        # Asume que el audio de Gemini es PCM de 16-bit. La frecuencia de muestreo puede ser un problema (24kHz vs 8kHz).
                        mulaw_audio_for_twilio = audioop.lin2ulaw(audio_data_to_send, 2)
                        logger.debug(f"Audio PCM de Gemini convertido a µ-law ({len(mulaw_audio_for_twilio)} bytes) para CallSid: {call_sid}")
                    except audioop.error as e:
                        logger.error(f"Error al convertir PCM (Gemini) a µ-law (Twilio) para CallSid: {call_sid}: {e}. Saltando este chunk de audio.")
                        continue 
                    
                    payload_b64 = base64.b64encode(mulaw_audio_for_twilio).decode("utf-8")
                    stream_sid = active_streams_sids.get(call_sid)
                    if stream_sid:
                        media_message = {"event": "media", "streamSid": stream_sid, "media": {"payload": payload_b64}}
                        await websocket.send_json(media_message)
                        logger.debug(f"Enviado chunk de audio µ-law a Twilio para CallSid: {call_sid}")
                    else:
                        logger.warning(f"No se encontró stream_sid para CallSid: {call_sid} al intentar enviar audio de Gemini.")
                
                # Loguear texto si se encontró
                if text_data_to_log:
                    logger.info(f"Texto de Gemini para CallSid {call_sid}: {text_data_to_log}")

        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"Conexión WebSocket con ADK cerrada para CallSid: {call_sid}. Código: {e.code}, Razón: {e.reason}", exc_info=True)
        except asyncio.CancelledError:
            logger.info(f"`process_gemini_responses` cancelado para CallSid: {call_sid}")
        except ValueError as e: 
            if "Session not found" in str(e):
                logger.error(f"Error de sesión en ADK (process_gemini_responses) para CallSid {call_sid}: {e}", exc_info=True)
            else:
                logger.error(f"ValueError en `process_gemini_responses` para CallSid: {call_sid}: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Error inesperado en `process_gemini_responses` para CallSid: {call_sid}: {e}", exc_info=True)
        finally:
            logger.info(f"Finalizando `process_gemini_responses` para CallSid: {call_sid}")
    # --- FIN DEL CÓDIGO MODIFICADO ---


    async def process_twilio_audio(websocket: WebSocket, call_sid: str, live_request_queue: LiveRequestQueue):
        logger.info(f"Iniciando `process_twilio_audio` para CallSid: {call_sid}")
        queue_open_for_sending = True
        try:
            while True:
                if websocket.client_state == WebSocketState.DISCONNECTED:
                    logger.warning(f"WebSocket desconectado (Twilio->App) para CallSid: {call_sid}. Terminando.")
                    break
                message_str = await websocket.receive_text()
                message_json = json.loads(message_str)
                event_type = message_json.get("event")

                if event_type == "start":
                    stream_sid = message_json.get("start", {}).get("streamSid")
                    if stream_sid: active_streams_sids[call_sid] = stream_sid
                    logger.info(f"Evento 'start' Twilio. StreamSid: {stream_sid} para CallSid: {call_sid}")
                elif event_type == "media":
                    if queue_open_for_sending: 
                        payload_b64 = message_json.get("media", {}).get("payload")
                        if not payload_b64: continue
                        mulaw_data_bytes = base64.b64decode(payload_b64)
                        try:
                            pcm_data_bytes = audioop.ulaw2lin(mulaw_data_bytes, 2)
                        except audioop.error as e:
                            logger.error(f"Error µ-law->PCM para CallSid: {call_sid}: {e}. Saltando.")
                            continue
                        blob_data = generativelanguage_types.Blob(data=pcm_data_bytes, mime_type='audio/pcm;rate=8000')
                        try:
                            live_request_queue.send_realtime(blob_data)
                            logger.debug(f"Enviado chunk PCM a Gemini para CallSid: {call_sid}")
                        except Exception as e: 
                            logger.warning(f"Error al enviar a live_request_queue para CallSid {call_sid}: {e}")
                            queue_open_for_sending = False 
                    else:
                        logger.debug(f"Audio de Twilio para CallSid: {call_sid}, pero cola ADK no se usa para enviar.")
                elif event_type == "stop":
                    logger.info(f"Evento 'stop' de Twilio para CallSid: {call_sid}.")
                    if queue_open_for_sending:
                        try:
                            live_request_queue.close() 
                            logger.info(f"live_request_queue.close() llamado para CallSid: {call_sid}")
                        except Exception as e:
                            logger.warning(f"Error al llamar a live_request_queue.close() para CallSid {call_sid}: {e}")
                        queue_open_for_sending = False 
                    break
                elif event_type == "mark":
                    mark_name = message_json.get('mark', {}).get('name')
                    logger.info(f"Evento 'mark' de Twilio para CallSid: {call_sid}. Nombre: {mark_name}")
        except WebSocketDisconnect as e:
            logger.info(f"WS desconectado (Twilio->App) por Twilio para CallSid: {call_sid}. Code: {e.code}")
        except websockets.exceptions.ConnectionClosed as e:
             logger.info(f"WS cerrado (Twilio->App) por Twilio para CallSid: {call_sid}. Code: {e.code}")
        except asyncio.CancelledError:
            logger.info(f"`process_twilio_audio` cancelado para CallSid: {call_sid}")
        except json.JSONDecodeError as e:
            logger.error(f"Error JSON en Twilio para CallSid: {call_sid}: {e}. Msg: {message_str[:200]}...")
        except Exception as e:
            logger.error(f"Error inesperado en `process_twilio_audio` para CallSid: {call_sid}: {e}", exc_info=True)
        finally:
            logger.info(f"Finalizando `process_twilio_audio` para CallSid: {call_sid}")
            if queue_open_for_sending: 
                try:
                    live_request_queue.close()
                    logger.info(f"Asegurando cierre de live_request_queue al final de `process_twilio_audio` para CallSid: {call_sid}")
                except Exception as e:
                    logger.warning(f"Error al asegurar cierre de live_request_queue para CallSid {call_sid}: {e}")


    @app.websocket("/stream/{call_sid}")
    async def websocket_audio_endpoint(websocket: WebSocket, call_sid: str):
        await websocket.accept()
        logger.info(f"🔗 WebSocket aceptado de Twilio para CallSid: {call_sid}")
        live_events_generator: AsyncIterable[Event] = None
        live_request_queue: LiveRequestQueue = None
        queue_open_for_sending_ws_level = True
        try:
            live_events_generator, live_request_queue = await start_agent_session(call_sid) 
            
            initial_greeting_text = "Hola, soy Jarvis, tu asistente de inteligencia artificial. ¿En qué puedo ayudarte hoy?"
            logger.info(f"Enviando saludo inicial '{initial_greeting_text}' a Gemini para CallSid: {call_sid}")
            initial_content = generativelanguage_types.Content(parts=[generativelanguage_types.Part(text=initial_greeting_text)])
            try:
                live_request_queue.send_content(content=initial_content)
            except Exception as e:
                 logger.warning(f"Error al enviar saludo inicial a live_request_queue para CallSid {call_sid}: {e}")
                 queue_open_for_sending_ws_level = False

            twilio_task = asyncio.create_task(
                process_twilio_audio(websocket, call_sid, live_request_queue), name=f"TwilioProcessor-{call_sid}"
            )
            gemini_task = asyncio.create_task(
                process_gemini_responses(websocket, call_sid, live_events_generator), name=f"GeminiProcessor-{call_sid}"
            )
            done, pending = await asyncio.wait([twilio_task, gemini_task], return_when=asyncio.FIRST_COMPLETED)
            for task in pending: task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
            logger.info(f"Tareas principales para CallSid: {call_sid} finalizadas o canceladas.")
        except ValueError as e: 
            if "Session not found" in str(e) or "Error al crear la sesión ADK" in str(e):
                logger.error(f"Fallo crítico al iniciar la sesión ADK para CallSid {call_sid}: {e}", exc_info=True)
            else:
                logger.error(f"ValueError en `websocket_audio_endpoint` para CallSid: {call_sid}: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Error crítico en `websocket_audio_endpoint` para CallSid: {call_sid}: {e}", exc_info=True)
        finally:
            logger.info(f"🧹 Limpiando recursos para CallSid: {call_sid} en `websocket_audio_endpoint`")
            if live_request_queue and queue_open_for_sending_ws_level:
                try:
                    live_request_queue.close()
                    logger.info(f"Cerrando live_request_queue (si no se hizo ya) al final de `websocket_audio_endpoint` para CallSid: {call_sid}")
                except Exception as e: 
                    logger.warning(f"Error (esperado si ya cerrada) al cerrar live_request_queue en finally para CallSid {call_sid}: {e}")
            if call_sid in active_streams_sids: del active_streams_sids[call_sid]
            if websocket.client_state != WebSocketState.DISCONNECTED:
                try: await websocket.close(code=1000)
                except Exception: pass
            logger.info(f"Finalizado `websocket_audio_endpoint` para CallSid: {call_sid}")

@app.get("/", response_class=PlainTextResponse)
async def read_root(): return "Servidor del agente de voz Gemini-Twilio (Enum Modalities) activo."

@app.get("/_ah/health", response_class=PlainTextResponse)
async def health_check(): return "OK"

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    reload_flag = os.getenv("K_SERVICE") is None
    logger.info(f"Iniciando Uvicorn en http://0.0.0.0:{port} reload={'on' if reload_flag else 'off'}")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=reload_flag)