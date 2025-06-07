# main.py
# VERSIÓN CON TRANSCODIFICACIÓN DE AUDIO (audioop) Y MEJORAS

import os
import json
import base64
import asyncio
import logging
import websockets
import audioop # <--- AÑADIDO PARA TRANSCODIFICACIÓN

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.websockets import WebSocketState
from dotenv import load_dotenv

from google.genai import types as generativelanguage_types
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

# Configuración del logging
# Cambia a logging.DEBUG para ver mensajes más detallados sobre el flujo de audio
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

if not os.getenv("K_SERVICE"): # Cargar .env solo si no estamos en un entorno K_SERVICE (como Cloud Run)
    load_dotenv()

APP_NAME = "Twilio Voice Agent Gemini"
SERVER_BASE_URL = os.getenv("SERVER_BASE_URL")

if not os.getenv("GOOGLE_API_KEY"):
    logger.warning("ADVERTENCIA: La variable de entorno GOOGLE_API_KEY no está configurada.")
if not SERVER_BASE_URL and root_agent: # Solo es crítico si el agente de voz está activo
    logger.error("ERROR CRÍTICO: La variable de entorno SERVER_BASE_URL no está configurada. El webhook de voz no funcionará.")


session_service = InMemorySessionService()
app = FastAPI(title=APP_NAME, version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

active_streams_sids = {} # Mantiene un mapeo de call_sid a stream_sid de Twilio

if root_agent:
    @app.post("/voice", response_class=PlainTextResponse)
    async def voice_webhook(request: Request):
        try:
            form = await request.form()
            call_sid = form.get("CallSid")
            logger.info(f"📞 Llamada entrante de Twilio - CallSid: {call_sid}")

            response = VoiceResponse()
            if not SERVER_BASE_URL:
                 logger.error(f"SERVER_BASE_URL no configurado. No se puede conectar el stream para CallSid: {call_sid}")
                 response.say("Lo siento, hay un error de configuración del servidor que impide procesar tu llamada.", language="es-ES")
                 return PlainTextResponse(str(response), media_type="application/xml")

            # Asegurarse de que la URL del WebSocket sea wss://
            websocket_url_host = SERVER_BASE_URL.replace("https://", "").replace("http://", "")
            websocket_url = f"wss://{websocket_url_host}/stream/{call_sid}"

            logger.info(f"Instruyendo a Twilio para conectar WebSocket a: {websocket_url} para CallSid: {call_sid}")
            connect = Connect()
            connect.stream(url=websocket_url)
            response.append(connect)
            # Pausa para dar tiempo a que se establezca la conexión WebSocket y el agente salude.
            # Ajusta esta duración si es necesario.
            response.pause(length=60) # Twilio cortará la llamada si no hay <Say>, <Play>, <Gather>, etc. después de la pausa.
                                      # El agente debe responder dentro de este tiempo.
            logger.info(f"Respondiendo a Twilio con TwiML para CallSid {call_sid}: {str(response)}")
            return PlainTextResponse(str(response), media_type="application/xml")
        except Exception as e:
            logger.error(f"Error en /voice webhook para CallSid: {form.get('CallSid', 'Desconocido')}: {e}", exc_info=True)
            # Devolver una respuesta TwiML de error genérica
            error_response = VoiceResponse()
            error_response.say("Lo siento, ocurrió un error al procesar tu llamada. Intenta de nuevo más tarde.", language="es-ES")
            return PlainTextResponse(str(error_response), status_code=500, media_type="application/xml")

    async def start_agent_session(session_id: str):
        logger.info(f"Iniciando sesión ADK para session_id (CallSid): {session_id}")
        session = await session_service.create_session(
            app_name=APP_NAME, user_id=session_id, session_id=session_id # Usar call_sid como user_id y session_id
        )
        runner = Runner(app_name=APP_NAME, agent=root_agent, session_service=session_service)

        # Configuración de ejecución simple, confiando en que el ADK y Gemini
        # manejen la configuración de audio al recibir PCM.
        run_config = RunConfig(
            response_modalities=["AUDIO", "TEXT"]
            # Podríamos explorar `audio_config` si necesitamos un control más fino y
            # `mime_type` en Blob no es suficiente o queremos especificar 16kHz tras un upsampling.
        )
        
        live_request_queue = LiveRequestQueue()
        live_events = runner.run_live(session=session, live_request_queue=live_request_queue, run_config=run_config)
        logger.info(f"Sesión ADK y runner iniciados. `live_events` y `live_request_queue` listos para CallSid: {session_id}")
        return live_events, live_request_queue

    async def process_gemini_responses(websocket: WebSocket, call_sid: str, live_events: generativelanguage_types.LiveEvents):
        logger.info(f"Iniciando `process_gemini_responses` para CallSid: {call_sid}")
        try:
            async for event in live_events:
                if websocket.client_state == WebSocketState.DISCONNECTED:
                    logger.warning(f"WebSocket desconectado mientras se procesaban respuestas de Gemini para CallSid: {call_sid}. Terminando.")
                    break

                if event.type == generativelanguage_types.LiveEventType.OUTPUT_DATA:
                    if event.output_data and event.output_data.audio_data:
                        pcm_audio_from_gemini = event.output_data.audio_data.data
                        logger.debug(f"Recibido chunk de audio PCM de Gemini ({len(pcm_audio_from_gemini)} bytes) para CallSid: {call_sid}")
                        
                        try:
                            # Asumimos PCM lineal 16-bit (2 bytes por muestra) de Gemini.
                            # Twilio Media Streams espera µ-law.
                            mulaw_audio_for_twilio = audioop.lin2ulaw(pcm_audio_from_gemini, 2)
                            logger.debug(f"Audio PCM de Gemini convertido a µ-law ({len(mulaw_audio_for_twilio)} bytes) para CallSid: {call_sid}")
                        except audioop.error as e:
                            logger.error(f"Error al convertir PCM (Gemini) a µ-law (Twilio) para CallSid: {call_sid}: {e}. Saltando este chunk de audio.")
                            continue # Saltar este chunk de audio si la conversión falla

                        payload_b64 = base64.b64encode(mulaw_audio_for_twilio).decode("utf-8")
                        stream_sid = active_streams_sids.get(call_sid)

                        if stream_sid:
                            media_message = {"event": "media", "streamSid": stream_sid, "media": {"payload": payload_b64}}
                            await websocket.send_json(media_message)
                            logger.debug(f"Enviado chunk de audio µ-law ({len(payload_b64)} chars Base64) a Twilio para CallSid: {call_sid}, StreamSid: {stream_sid}")
                        else:
                            logger.warning(f"No se encontró stream_sid para CallSid: {call_sid} al intentar enviar audio de Gemini. El audio podría no reproducirse.")
                    
                    if event.output_data and event.output_data.text_data:
                        logger.info(f"Texto de Gemini para CallSid {call_sid}: {event.output_data.text_data.text}")


                elif event.type == generativelanguage_types.LiveEventType.SESSION_ENDED:
                    logger.info(f"Evento SESSION_ENDED de ADK recibido para CallSid: {call_sid}.")
                    # Considerar enviar un mensaje "mark" para indicar el fin de la elocución si es necesario,
                    # o simplemente dejar que el WebSocket se cierre.
                    # Ejemplo: await websocket.send_json({"event": "mark", "streamSid": active_streams_sids.get(call_sid), "mark": {"name": "gemini_done"}})
                    break # Salir del bucle async for

                elif event.type == generativelanguage_types.LiveEventType.ERROR:
                    logger.error(f"Error en evento de ADK para CallSid: {call_sid}: {event.error}")
                    # Podríamos necesitar cerrar el WebSocket o la sesión de forma más activa aquí.
                    break

        except websockets.exceptions.ConnectionClosed as e: # Si el WebSocket se cierra desde el lado del cliente (Twilio)
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
            while True: # Bucle para recibir mensajes del WebSocket de Twilio
                if websocket.client_state == WebSocketState.DISCONNECTED:
                    logger.warning(f"WebSocket desconectado mientras se esperaba audio de Twilio para CallSid: {call_sid}. Terminando.")
                    break
                
                message_str = await websocket.receive_text() # Usar receive_text ya que Twilio envía JSON como texto
                message_json = json.loads(message_str)
                event_type = message_json.get("event")

                if event_type == "start":
                    stream_sid = message_json.get("start", {}).get("streamSid")
                    if stream_sid:
                        active_streams_sids[call_sid] = stream_sid
                        logger.info(f"Evento 'start' de Twilio. StreamSid: {stream_sid} asociado a CallSid: {call_sid}")
                    else:
                        logger.error(f"Evento 'start' de Twilio sin streamSid para CallSid: {call_sid}. Mensaje: {message_json}")
                    # Aquí podrías enviar un mensaje de bienvenida inicial si no se hace a través de send_content.
                    # Por ejemplo, un "Hola" TTS para confirmar la conexión.
                    
                elif event_type == "media":
                    if live_request_queue and not live_request_queue.is_closed:
                        payload_b64 = message_json.get("media", {}).get("payload")
                        if not payload_b64:
                            logger.warning(f"Mensaje 'media' sin payload para CallSid: {call_sid}. Mensaje: {message_json}")
                            continue

                        # Paso 1: Decodificar base64
                        mulaw_data_bytes = base64.b64decode(payload_b64)
                        logger.debug(f"Recibido chunk de audio µ-law ({len(mulaw_data_bytes)} bytes) de Twilio para CallSid: {call_sid}")

                        try:
                            # Twilio envía µ-law. Convertimos a PCM lineal 16-bit (2 bytes por muestra).
                            # La tasa de muestreo de µ-law telefónico es 8000 Hz.
                            pcm_data_bytes = audioop.ulaw2lin(mulaw_data_bytes, 2)
                            logger.debug(f"Audio µ-law ({len(mulaw_data_bytes)} bytes) convertido a PCM ({len(pcm_data_bytes)} bytes) para CallSid: {call_sid}")
                        except audioop.error as e:
                            logger.error(f"Error al convertir µ-law (Twilio) a PCM (Gemini) para CallSid: {call_sid}: {e}. Saltando este chunk de audio.")
                            continue # Saltar este chunk si la conversión falla

                        # Paso 3: Construir el Blob con el mime_type adecuado para Gemini
                        blob_data = generativelanguage_types.Blob(
                            data=pcm_data_bytes,
                            mime_type='audio/pcm;rate=8000' # Gemini debería entender PCM a 8kHz
                        )
                        live_request_queue.send_realtime(blob_data)
                        logger.debug(f"Enviado chunk de audio PCM a Gemini para CallSid: {call_sid}")
                    elif live_request_queue and live_request_queue.is_closed:
                        logger.warning(f"Recibido audio de Twilio para CallSid: {call_sid}, pero live_request_queue ya está cerrada.")


                elif event_type == "stop":
                    logger.info(f"Evento 'stop' de Twilio recibido para CallSid: {call_sid}. Stream detenido por Twilio.")
                    if live_request_queue and not live_request_queue.is_closed:
                        logger.info(f"Cerrando live_request_queue para CallSid: {call_sid} debido al evento 'stop' de Twilio.")
                        live_request_queue.close() # Señala al ADK que no habrá más entrada de audio
                    break # Salir del bucle, la tarea terminará

                elif event_type == "mark":
                    mark_name = message_json.get('mark', {}).get('name')
                    logger.info(f"Evento 'mark' de Twilio recibido para CallSid: {call_sid}. Nombre: {mark_name}")

                # Otros eventos como "dtmf" podrían manejarse aquí si es necesario

        except WebSocketDisconnect as e: # Captura específica de FastAPI para desconexiones
            logger.info(f"WebSocket desconectado limpiamente por el cliente (Twilio) para CallSid: {call_sid}. Código: {e.code}, Razón: {e.reason}")
        except websockets.exceptions.ConnectionClosed as e: # Captura más general de websockets
             logger.info(f"Conexión WebSocket cerrada por el cliente (Twilio) para CallSid: {call_sid}. Código: {e.code}, Razón: {e.reason}")
        except asyncio.CancelledError:
            logger.info(f"`process_twilio_audio` cancelado para CallSid: {call_sid}")
        except json.JSONDecodeError as e:
            logger.error(f"Error al decodificar JSON de Twilio para CallSid: {call_sid}: {e}. Mensaje recibido: {message_str[:200]}...") # Loguear parte del mensaje
        except Exception as e:
            logger.error(f"Error inesperado en `process_twilio_audio` para CallSid: {call_sid}: {e}", exc_info=True)
        finally:
            logger.info(f"Finalizando `process_twilio_audio` para CallSid: {call_sid}")
            if live_request_queue and not live_request_queue.is_closed:
                logger.info(f"Asegurando que live_request_queue esté cerrada al finalizar `process_twilio_audio` para CallSid: {call_sid}")
                live_request_queue.close()


    @app.websocket("/stream/{call_sid}")
    async def websocket_audio_endpoint(websocket: WebSocket, call_sid: str):
        await websocket.accept()
        logger.info(f"🔗 WebSocket aceptado de Twilio para CallSid: {call_sid}")
        
        live_events: generativelanguage_types.LiveEvents = None
        live_request_queue: LiveRequestQueue = None
        
        try:
            live_events, live_request_queue = await start_agent_session(call_sid)
            
            # Mensaje inicial para que el agente salude.
            # Esto envía la solicitud a Gemini, y la respuesta de audio se manejará en `process_gemini_responses`.
            initial_greeting_text = "Hola, soy Jarvis, tu asistente de inteligencia artificial. ¿En qué puedo ayudarte hoy?"
            logger.info(f"Enviando saludo inicial '{initial_greeting_text}' a Gemini para CallSid: {call_sid}")
            initial_content = generativelanguage_types.Content(
                # role="user", # El ADK lo interpreta como una instrucción para el modelo si es la primera.
                parts=[generativelanguage_types.Part(text=initial_greeting_text)]
            )
            # No se necesita especificar 'role' aquí, el ADK lo maneja.
            # Si el agente está configurado para responder primero, este content iniciará esa respuesta.
            live_request_queue.send_content(content=initial_content)
            
            # Crear tareas para procesar audio de Twilio y respuestas de Gemini concurrentemente
            twilio_task = asyncio.create_task(
                process_twilio_audio(websocket, call_sid, live_request_queue),
                name=f"TwilioProcessor-{call_sid}"
            )
            gemini_task = asyncio.create_task(
                process_gemini_responses(websocket, call_sid, live_events),
                name=f"GeminiProcessor-{call_sid}"
            )
            
            # Esperar a que una de las tareas principales termine.
            # Si `process_twilio_audio` termina (ej. llamada colgada), cancelamos `process_gemini_responses`.
            # Si `process_gemini_responses` termina (ej. sesión ADK finalizada), podríamos querer cerrar la conexión de Twilio o esperar.
            done, pending = await asyncio.wait(
                [twilio_task, gemini_task],
                return_when=asyncio.FIRST_COMPLETED, # Continuar cuando una tarea termine
            )

            for task in pending: # Cancelar las tareas pendientes
                logger.info(f"Cancelando tarea pendiente: {task.get_name()} para CallSid: {call_sid}")
                task.cancel()
            
            # Esperar a que las tareas pendientes se cancelen y procesar resultados/excepciones de las tareas completadas
            await asyncio.gather(*done, *pending, return_exceptions=True)
            logger.info(f"Todas las tareas principales para CallSid: {call_sid} han finalizado o han sido canceladas.")

        except Exception as e:
            logger.error(f"Error crítico en `websocket_audio_endpoint` para CallSid: {call_sid}: {e}", exc_info=True)
        finally:
            logger.info(f"🧹 Limpiando recursos para CallSid: {call_sid} en `websocket_audio_endpoint`")
            
            if live_request_queue and not live_request_queue.is_closed:
                logger.info(f"Cerrando live_request_queue explícitamente al final de `websocket_audio_endpoint` para CallSid: {call_sid}")
                live_request_queue.close()
            
            # live_events no necesita un close() explícito, se gestiona con la sesión del ADK.

            if call_sid in active_streams_sids:
                del active_streams_sids[call_sid]
                logger.info(f"Eliminado StreamSid de `active_streams_sids` para CallSid: {call_sid}")

            if websocket.client_state != WebSocketState.DISCONNECTED:
                try:
                    logger.info(f"Cerrando conexión WebSocket para CallSid: {call_sid}")
                    await websocket.close(code=1000) # Código 1000 para cierre normal
                except Exception as e: # Podría fallar si ya está cerrada o en un estado anómalo
                    logger.error(f"Error al intentar cerrar WebSocket para CallSid: {call_sid}: {e}", exc_info=True)
            
            logger.info(f"Finalizado `websocket_audio_endpoint` para CallSid: {call_sid}")

@app.get("/", response_class=PlainTextResponse)
async def read_root():
    return "Servidor del agente de voz Gemini-Twilio (con transcodificación de audio) activo."

@app.get("/_ah/health", response_class=PlainTextResponse) # Para chequeos de salud de Cloud Run, etc.
async def health_check():
    return "OK"

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080)) # Puerto estándar para Cloud Run
    reload_flag = os.getenv("K_SERVICE") is None # Activar reload solo localmente
    
    logger.info(f"Iniciando servidor Uvicorn en http://0.0.0.0:{port} con reload={'activado' if reload_flag else 'desactivado'}")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=reload_flag)