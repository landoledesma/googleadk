# main.py
# VERSIÓN CON CORRECCIÓN DE TIPOS DE EVENTO DEL ADK Y TRANSCODIFICACIÓN

import os
import json
import base64
import asyncio
import logging
import websockets
import audioop

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.websockets import WebSocketState
from dotenv import load_dotenv

from typing import AsyncIterable # Para la pista de tipo

# --- IMPORTACIONES CORREGIDAS ---
from google.adk.events.event import Event # Asumiendo que esta es la ruta correcta para el Evento del ADK
from google.genai import types as generativelanguage_types # Para LiveEventType, Blob, Content, etc.
# --- FIN IMPORTACIONES CORREGIDAS ---

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

    async def start_agent_session(session_id: str):
        logger.info(f"Iniciando sesión ADK para: {session_id}")
        # session = await session_service.create_session( # Esto es si quieres crearla explícitamente
        #     app_name=APP_NAME, user_id=session_id, session_id=session_id
        # )
        # El Runner puede obtener o crear la sesión internamente si se le pasan user_id y session_id
        runner = Runner(app_name=APP_NAME, agent=root_agent, session_service=session_service)
        run_config = RunConfig(response_modalities=["AUDIO", "TEXT"])
        live_request_queue = LiveRequestQueue()
        
        # El Runner maneja la obtención/creación de la sesión si se le da user_id y session_id
        live_events_generator = runner.run_live(
            user_id=session_id, # Usar call_sid como user_id
            session_id=session_id, # Usar call_sid como session_id
            live_request_queue=live_request_queue,
            run_config=run_config
        )
        logger.info(f"Sesión ADK y runner.run_live iniciados para CallSid: {session_id}")
        return live_events_generator, live_request_queue

    # --- FIRMA DE FUNCIÓN CORREGIDA ---
    async def process_gemini_responses(websocket: WebSocket, call_sid: str, live_events: AsyncIterable[Event]):
    # --- FIN FIRMA DE FUNCIÓN CORREGIDA ---
        logger.info(f"Iniciando `process_gemini_responses` para CallSid: {call_sid}")
        try:
            # Ahora 'event' será de tipo 'google.adk.events.event.Event'
            async for event in live_events: # Quitando la anotación de tipo aquí para simplicidad si causa problemas de importación en tiempo de ejecución
                if websocket.client_state == WebSocketState.DISCONNECTED:
                    logger.warning(f"WebSocket desconectado mientras se procesaban respuestas de Gemini para CallSid: {call_sid}. Terminando.")
                    break

                # Asumiendo que el objeto 'Event' del ADK tiene una estructura similar a lo que esperábamos:
                # event.type debería ser un generativelanguage_types.LiveEventType
                # event.output_data debería existir para eventos de datos
                # event.error debería existir para eventos de error

                if not hasattr(event, 'type'):
                    logger.warning(f"Evento de ADK sin atributo 'type' para CallSid: {call_sid}. Evento: {event}")
                    continue

                if event.type == generativelanguage_types.LiveEventType.OUTPUT_DATA:
                    if hasattr(event, 'output_data') and event.output_data and hasattr(event.output_data, 'audio_data') and event.output_data.audio_data:
                        pcm_audio_from_gemini = event.output_data.audio_data.data
                        logger.debug(f"Recibido chunk de audio PCM de Gemini ({len(pcm_audio_from_gemini)} bytes) para CallSid: {call_sid}")
                        try:
                            mulaw_audio_for_twilio = audioop.lin2ulaw(pcm_audio_from_gemini, 2)
                            logger.debug(f"Audio PCM de Gemini convertido a µ-law ({len(mulaw_audio_for_twilio)} bytes) para CallSid: {call_sid}")
                        except audioop.error as e:
                            logger.error(f"Error al convertir PCM (Gemini) a µ-law (Twilio) para CallSid: {call_sid}: {e}. Saltando este chunk de audio.")
                            continue

                        payload_b64 = base64.b64encode(mulaw_audio_for_twilio).decode("utf-8")
                        stream_sid = active_streams_sids.get(call_sid)
                        if stream_sid:
                            media_message = {"event": "media", "streamSid": stream_sid, "media": {"payload": payload_b64}}
                            await websocket.send_json(media_message)
                            logger.debug(f"Enviado chunk de audio µ-law ({len(payload_b64)} chars Base64) a Twilio para CallSid: {call_sid}, StreamSid: {stream_sid}")
                        else:
                            logger.warning(f"No se encontró stream_sid para CallSid: {call_sid} al intentar enviar audio de Gemini.")
                    
                    if hasattr(event, 'output_data') and event.output_data and hasattr(event.output_data, 'text_data') and event.output_data.text_data:
                        logger.info(f"Texto de Gemini para CallSid {call_sid}: {event.output_data.text_data.text}")

                elif event.type == generativelanguage_types.LiveEventType.SESSION_ENDED:
                    logger.info(f"Evento SESSION_ENDED de ADK recibido para CallSid: {call_sid}.")
                    break
                elif event.type == generativelanguage_types.LiveEventType.ERROR:
                    error_details = getattr(event, 'error', 'Error desconocido en evento ADK')
                    logger.error(f"Error en evento de ADK para CallSid: {call_sid}: {error_details}")
                    break
                else:
                    logger.debug(f"Evento ADK de tipo no manejado explícitamente recibido para CallSid {call_sid}: Type: {event.type}")


        except websockets.exceptions.ConnectionClosed as e:
            logger.info(f"WebSocket cerrado por el cliente (Twilio) mientras se procesaban respuestas de Gemini para CallSid: {call_sid}. Código: {e.code}, Razón: {e.reason}")
        except asyncio.CancelledError:
            logger.info(f"`process_gemini_responses` cancelado para CallSid: {call_sid}")
        except Exception as e:
            logger.error(f"Error inesperado en `process_gemini_responses` para CallSid: {call_sid}: {e}", exc_info=True)
        finally:
            logger.info(f"Finalizando `process_gemini_responses` para CallSid: {call_sid}")

    async def process_twilio_audio(websocket: WebSocket, call_sid: str, live_request_queue: LiveRequestQueue):
        logger.info(f"Iniciando `process_twilio_audio` para CallSid: {call_sid}")
        try:
            while True:
                if websocket.client_state == WebSocketState.DISCONNECTED:
                    logger.warning(f"WebSocket desconectado mientras se esperaba audio de Twilio para CallSid: {call_sid}. Terminando.")
                    break
                message_str = await websocket.receive_text()
                message_json = json.loads(message_str)
                event_type = message_json.get("event")

                if event_type == "start":
                    stream_sid = message_json.get("start", {}).get("streamSid")
                    if stream_sid:
                        active_streams_sids[call_sid] = stream_sid
                        logger.info(f"Evento 'start' de Twilio. StreamSid: {stream_sid} asociado a CallSid: {call_sid}")
                    else:
                        logger.error(f"Evento 'start' de Twilio sin streamSid para CallSid: {call_sid}. Msg: {message_json}")
                elif event_type == "media":
                    if live_request_queue and not live_request_queue.is_closed:
                        payload_b64 = message_json.get("media", {}).get("payload")
                        if not payload_b64:
                            logger.warning(f"Mensaje 'media' sin payload para CallSid: {call_sid}. Msg: {message_json}")
                            continue
                        mulaw_data_bytes = base64.b64decode(payload_b64)
                        logger.debug(f"Recibido chunk µ-law ({len(mulaw_data_bytes)} bytes) de Twilio para CallSid: {call_sid}")
                        try:
                            pcm_data_bytes = audioop.ulaw2lin(mulaw_data_bytes, 2)
                            logger.debug(f"Audio µ-law ({len(mulaw_data_bytes)}) -> PCM ({len(pcm_data_bytes)}) para CallSid: {call_sid}")
                        except audioop.error as e:
                            logger.error(f"Error al convertir µ-law a PCM para CallSid: {call_sid}: {e}. Saltando chunk.")
                            continue
                        blob_data = generativelanguage_types.Blob(
                            data=pcm_data_bytes,
                            mime_type='audio/pcm;rate=8000'
                        )
                        live_request_queue.send_realtime(blob_data)
                        logger.debug(f"Enviado chunk PCM a Gemini para CallSid: {call_sid}")
                    elif live_request_queue and live_request_queue.is_closed:
                        logger.warning(f"Audio de Twilio para CallSid: {call_sid}, pero live_request_queue cerrada.")
                elif event_type == "stop":
                    logger.info(f"Evento 'stop' de Twilio para CallSid: {call_sid}.")
                    if live_request_queue and not live_request_queue.is_closed:
                        logger.info(f"Cerrando live_request_queue para CallSid: {call_sid} por 'stop' de Twilio.")
                        live_request_queue.close()
                    break
                elif event_type == "mark":
                    logger.info(f"Evento 'mark' de Twilio para CallSid: {call_sid}. Nombre: {message_json.get('mark', {}).get('name')}")
        except WebSocketDisconnect as e:
            logger.info(f"WS desconectado por Twilio para CallSid: {call_sid}. Código: {e.code}, Razón: {e.reason}")
        except websockets.exceptions.ConnectionClosed as e:
             logger.info(f"WS cerrado por Twilio para CallSid: {call_sid}. Código: {e.code}, Razón: {e.reason}")
        except asyncio.CancelledError:
            logger.info(f"`process_twilio_audio` cancelado para CallSid: {call_sid}")
        except json.JSONDecodeError as e:
            logger.error(f"Error JSON en Twilio para CallSid: {call_sid}: {e}. Msg: {message_str[:200]}...")
        except Exception as e:
            logger.error(f"Error en `process_twilio_audio` para CallSid: {call_sid}: {e}", exc_info=True)
        finally:
            logger.info(f"Finalizando `process_twilio_audio` para CallSid: {call_sid}")
            if live_request_queue and not live_request_queue.is_closed:
                logger.info(f"Asegurando cierre de live_request_queue al final de `process_twilio_audio` para CallSid: {call_sid}")
                live_request_queue.close()

    @app.websocket("/stream/{call_sid}")
    async def websocket_audio_endpoint(websocket: WebSocket, call_sid: str):
        await websocket.accept()
        logger.info(f"🔗 WebSocket aceptado de Twilio para CallSid: {call_sid}")
        live_events_generator: AsyncIterable[Event] = None
        live_request_queue: LiveRequestQueue = None
        try:
            live_events_generator, live_request_queue = await start_agent_session(call_sid)
            initial_greeting_text = "Hola, soy Jarvis, tu asistente de inteligencia artificial. ¿En qué puedo ayudarte hoy?"
            logger.info(f"Enviando saludo inicial '{initial_greeting_text}' a Gemini para CallSid: {call_sid}")
            initial_content = generativelanguage_types.Content(
                parts=[generativelanguage_types.Part(text=initial_greeting_text)]
            )
            live_request_queue.send_content(content=initial_content)
            
            twilio_task = asyncio.create_task(
                process_twilio_audio(websocket, call_sid, live_request_queue), name=f"TwilioProcessor-{call_sid}"
            )
            gemini_task = asyncio.create_task(
                process_gemini_responses(websocket, call_sid, live_events_generator), name=f"GeminiProcessor-{call_sid}"
            )
            done, pending = await asyncio.wait([twilio_task, gemini_task], return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                logger.info(f"Cancelando tarea pendiente: {task.get_name()} para CallSid: {call_sid}")
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
            logger.info(f"Tareas principales para CallSid: {call_sid} finalizadas o canceladas.")
        except Exception as e:
            logger.error(f"Error crítico en `websocket_audio_endpoint` para CallSid: {call_sid}: {e}", exc_info=True)
        finally:
            logger.info(f"🧹 Limpiando recursos para CallSid: {call_sid} en `websocket_audio_endpoint`")
            if live_request_queue and not live_request_queue.is_closed:
                logger.info(f"Cerrando live_request_queue al final de `websocket_audio_endpoint` para CallSid: {call_sid}")
                live_request_queue.close()
            if call_sid in active_streams_sids:
                del active_streams_sids[call_sid]
                logger.info(f"Eliminado StreamSid de `active_streams_sids` para CallSid: {call_sid}")
            if websocket.client_state != WebSocketState.DISCONNECTED:
                try:
                    logger.info(f"Cerrando WebSocket para CallSid: {call_sid}")
                    await websocket.close(code=1000)
                except Exception as e:
                    logger.error(f"Error al cerrar WebSocket para CallSid: {call_sid}: {e}", exc_info=True)
            logger.info(f"Finalizado `websocket_audio_endpoint` para CallSid: {call_sid}")

@app.get("/", response_class=PlainTextResponse)
async def read_root(): return "Servidor del agente de voz Gemini-Twilio (ADK Event Type Fix) activo."

@app.get("/_ah/health", response_class=PlainTextResponse)
async def health_check(): return "OK"

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    reload_flag = os.getenv("K_SERVICE") is None
    logger.info(f"Iniciando Uvicorn en http://0.0.0.0:{port} reload={'on' if reload_flag else 'off'}")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=reload_flag)