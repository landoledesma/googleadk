# main.py
# VERSIÓN FINAL Y CORRECTA: Corrige el ValidationError y el RuntimeWarning.

import os
import json
import base64
import asyncio
import logging
import io

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.websockets import WebSocketState
from dotenv import load_dotenv

# ================================================
# --- SDK de Google, ADK y Librerías de Audio ---
# ================================================
from google.genai import types as generativelanguage_types
from google.adk.agents.run_config import RunConfig
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.agents import LiveRequestQueue
from google.adk.runners import Runner
from pydub import AudioSegment

# Twilio
from twilio.twiml.voice_response import VoiceResponse, Start

# Agente Jarvis
try:
    from app.jarvis.agent import root_agent
except ImportError:
    try:
        from jarvis.agent import root_agent
    except ImportError:
        root_agent = None
        logging.error("No se pudo importar root_agent. La funcionalidad de voz no funcionará.")

# ================================================
# 0. Configuración de Logging y Entorno
# ================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

if not os.getenv("K_SERVICE"):
    load_dotenv()

APP_NAME = "Twilio Voice Agent Gemini"
SERVER_BASE_URL = os.getenv("SERVER_BASE_URL")

if not os.getenv("GOOGLE_API_KEY"):
    logger.warning("ADVERTENCIA: La variable de entorno GOOGLE_API_KEY no está configurada.")

session_service = InMemorySessionService()
app = FastAPI(title=APP_NAME, version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

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
            if not SERVER_BASE_URL:
                 response.say("Error de configuración del servidor.", language="es-ES")
                 return PlainTextResponse(str(response), media_type="application/xml")
            websocket_url = f"wss://{SERVER_BASE_URL.replace('https://', '')}/stream/{call_sid}"
            start = Start()
            start.stream(url=websocket_url)
            response.append(start)
            response.say("Conectando con el asistente. Un momento.", language="es-ES")
            return PlainTextResponse(str(response), media_type="application/xml")
        except Exception as e:
            logger.error(f"Error en /voice: {e}", exc_info=True)
            return PlainTextResponse("<Response><Say>Error al procesar la llamada.</Say></Response>", status_code=500, media_type="application/xml")

    # --- CORRECCIÓN DE RUNTIMEWARNING: La función debe ser async ---
    async def start_agent_session(session_id: str):
        logger.info(f"Iniciando sesión ADK para: {session_id}")
        
        # --- CORRECCIÓN DE RUNTIMEWARNING: Se añade 'await' ---
        session = await session_service.create_session(
            app_name=APP_NAME, user_id=session_id, session_id=session_id
        )
        runner = Runner(app_name=APP_NAME, agent=root_agent, session_service=session_service)

        # Se crea un `SpeechConfig` para solicitar a Gemini que genere audio.
        speech_config = generativelanguage_types.SpeechConfig(
            voice_config=generativelanguage_types.VoiceConfig(
                prebuilt_voice_config=generativelanguage_types.PrebuiltVoiceConfig(voice_name="Puck")
            )
        )
        
        # --- CORRECCIÓN DE VALIDATIONERROR: Se crea un objeto vacío ---
        run_config = RunConfig(
            response_modalities=["AUDIO", "TEXT"],
            speech_config=speech_config,
            input_audio_transcription=generativelanguage_types.AudioTranscriptionConfig()
        )
        
        live_request_queue = LiveRequestQueue()
        live_events = runner.run_live(session=session, live_request_queue=live_request_queue, run_config=run_config)
        logger.info("Sesión ADK y runner iniciados con la configuración correcta.")
        return live_events, live_request_queue

    async def process_gemini_responses(websocket: WebSocket, call_sid: str, live_events):
        try:
            async for event in live_events:
                if event.type == generativelanguage_types.LiveEventType.OUTPUT_DATA:
                    if event.output_data and event.output_data.audio_data:
                        gemini_audio_chunk = event.output_data.audio_data.data
                        audio_segment = AudioSegment(data=gemini_audio_chunk, sample_width=2, frame_rate=24000, channels=1)
                        buffer = io.BytesIO()
                        audio_segment.export(buffer, format="mulaw", frame_rate=8000)
                        twilio_audio_chunk = buffer.getvalue()
                        payload = base64.b64encode(twilio_audio_chunk).decode("utf-8")
                        stream_sid = active_streams_sids.get(call_sid)
                        if stream_sid:
                            await websocket.send_json({"event": "media", "streamSid": stream_sid, "media": {"payload": payload}})
                elif event.type == generativelanguage_types.LiveEventType.SESSION_ENDED:
                    logger.info(f"Sesión ADK finalizada para {call_sid}.")
                    break
        except Exception as e:
            logger.error(f"Error en process_gemini_responses: {e}", exc_info=True)

    async def process_twilio_audio(websocket: WebSocket, call_sid: str, live_request_queue: LiveRequestQueue):
        try:
            while True:
                message_json = await websocket.receive_json()
                event_type = message_json.get("event")
                if event_type == "start":
                    active_streams_sids[call_sid] = message_json.get("start", {}).get("streamSid")
                elif event_type == "media":
                    if live_request_queue:
                        blob_data = generativelanguage_types.Blob(data=base64.b64decode(message_json["media"]["payload"]), mime_type="audio/x-mulaw")
                        live_request_queue.send_realtime(blob_data)
                elif event_type == "stop":
                    logger.info(f"Stream de Twilio detenido para {call_sid}.")
                    if live_request_queue: live_request_queue.close()
                    break
        except WebSocketDisconnect:
            logger.info(f"WS desconectado por Twilio para {call_sid}.")
        except Exception as e:
            logger.error(f"Error en process_twilio_audio: {e}", exc_info=True)

    @app.websocket("/stream/{call_sid}")
    async def websocket_audio_endpoint(websocket: WebSocket, call_sid: str):
        await websocket.accept()
        logger.info(f"🔗 WebSocket aceptado para {call_sid}")
        try:
            # --- CORRECCIÓN DE RUNTIMEWARNING: Se añade 'await' a la llamada ---
            live_events, live_request_queue = await start_agent_session(call_sid)
            
            twilio_task = asyncio.create_task(process_twilio_audio(websocket, call_sid, live_request_queue))
            gemini_task = asyncio.create_task(process_gemini_responses(websocket, call_sid, live_events))
            await asyncio.gather(twilio_task, gemini_task)
        except Exception as e:
            logger.error(f"Error en websocket_audio_endpoint: {e}", exc_info=True)
        finally:
            logger.info(f"🧹 Limpiando recursos para {call_sid}")
            if call_sid in active_streams_sids: del active_streams_sids[call_sid]
            if websocket.client_state != WebSocketState.DISCONNECTED: await websocket.close(code=1000)

@app.get("/", response_class=PlainTextResponse)
async def read_root(): return "Servidor del agente de voz activo."

@app.get("/_ah/health", response_class=PlainTextResponse)
async def health_check(): return "OK"

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8080)), reload=True)