# main.py
# VERSIÓN FINAL: Usa la API de Live directa para la comunicación y el ADK Runner solo para la lógica de las herramientas.

import os
import json
import base64
import asyncio
import logging
import websockets

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.websockets import WebSocketState
from dotenv import load_dotenv

# --- SDK de Google ---
from google import genai
from google.genai import types as generativelanguage_types

# --- Componentes del ADK que SÍ usamos ---
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.runners import Runner

from twilio.twiml.voice_response import VoiceResponse, Connect

# Agente Jarvis (con herramientas) - ¡Sigue siendo el cerebro!
try:
    from app.jarvis.agent import root_agent
except ImportError:
    try:
        from jarvis.agent import root_agent
    except ImportError:
        root_agent = None
        logging.error("No se pudo importar root_agent. La funcionalidad de voz no funcionará.")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

if not os.getenv("K_SERVICE"):
    load_dotenv()

APP_NAME = "Twilio Voice Agent Gemini"
SERVER_BASE_URL = os.getenv("SERVER_BASE_URL")
GEMINI_API_KEY = os.getenv("GOOGLE_API_KEY")

if not GEMINI_API_KEY:
    logger.critical("FATAL: La variable de entorno GOOGLE_API_KEY no está configurada.")

# Cliente directo de la API de Gemini
client = genai.Client(api_key=GEMINI_API_KEY)
# Servicio de sesión del ADK para mantener el historial del agente
adk_session_service = InMemorySessionService()

app = FastAPI(title=APP_NAME, version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

active_streams_sids = {}

if root_agent:
    @app.post("/voice", response_class=PlainTextResponse)
    async def voice_webhook(request: Request):
        # Esta parte ya funciona perfectamente.
        form = await request.form()
        call_sid = form.get("CallSid")
        logger.info(f"📞 Llamada entrante de Twilio - SID: {call_sid}")
        response = VoiceResponse()
        websocket_url = f"wss://{SERVER_BASE_URL.replace('https://', '')}/stream/{call_sid}"
        connect = Connect()
        connect.stream(url=websocket_url)
        response.append(connect)
        response.pause(length=30)
        return PlainTextResponse(str(response), media_type="application/xml")

    async def handle_agent_logic(live_session, adk_runner, adk_session, transcribed_text: str):
        """Usa el ADK para procesar texto y devuelve la respuesta al Live API para que la hable."""
        logger.info(f"Texto transcrito para el agente: '{transcribed_text}'")
        
        # Usamos el runner del ADK en modo "un solo turno" para usar las herramientas
        agent_response = await adk_runner.run_query(
            session=adk_session,
            query=transcribed_text,
            run_config={"response_modalities": ["TEXT"]} # Solo queremos la respuesta de texto
        )

        # Extraemos el texto de la respuesta del agente
        response_text = "".join(
            part.text for part in agent_response.parts if part.text
        )
        
        if response_text:
            logger.info(f"Respuesta del agente: '{response_text}'")
            # Enviamos el texto de vuelta a la API de Live para que lo convierta en audio
            await live_session.send_client_content(
                turns=[{"role": "user", "parts": [{"text": response_text}]}],
                turn_complete=True,
                is_final_turn=False # Indicamos que la conversación no ha terminado
            )
        else:
            logger.warning("El agente no generó una respuesta de texto.")

    @app.websocket("/stream/{call_sid}")
    async def websocket_audio_endpoint(websocket: WebSocket, call_sid: str):
        await websocket.accept()
        logger.info(f"🔗 WebSocket aceptado para {call_sid}")

        # Modelo y config para la API de Live directa
        model = "gemini-2.5-flash-preview-native-audio-dialog"
        config = {
            "response_modalities": ["AUDIO"], # Solo queremos audio de la API de Live
            "input_audio_transcription": {}  # Habilitamos la transcripción de entrada
        }

        # Creamos una sesión del ADK para mantener el historial y un runner para la lógica
        adk_session = await adk_session_service.create_session(app_name=APP_NAME, user_id=call_sid, session_id=call_sid)
        adk_runner = Runner(app_name=APP_NAME, agent=root_agent, session_service=adk_session_service)

        try:
            async with client.aio.live.connect(model=model, config=config) as live_session:
                logger.info(f"Sesión con la API de Gemini Live iniciada para {call_sid}")

                # Saludo inicial proactivo
                await handle_agent_logic(live_session, adk_runner, adk_session, "Saluda y preséntate como Jarvis.")

                # Tarea para recibir audio de Gemini y enviarlo a Twilio
                async def gemini_to_twilio():
                    try:
                        async for response in live_session.receive():
                            if response.data:
                                payload = base64.b64encode(response.data).decode("utf-8")
                                if call_sid in active_streams_sids:
                                    await websocket.send_json({"event": "media", "streamSid": active_streams_sids[call_sid], "media": {"payload": payload}})
                            # Cuando recibimos una transcripción del usuario...
                            if response.server_content and response.server_content.input_transcription:
                                transcribed_text = response.server_content.input_transcription.text
                                # ...invocamos la lógica del agente con el texto
                                await handle_agent_logic(live_session, adk_runner, adk_session, transcribed_text)
                    except Exception as e:
                        logger.error(f"Error en el bucle Gemini->Twilio: {e}")

                # Tarea para recibir audio de Twilio y enviarlo a Gemini
                async def twilio_to_gemini():
                    try:
                        while True:
                            message_json = await websocket.receive_json()
                            if message_json.get("event") == "start":
                                active_streams_sids[call_sid] = message_json["start"]["streamSid"]
                            elif message_json.get("event") == "media":
                                audio_bytes = base64.b64decode(message_json["media"]["payload"])
                                await live_session.send_realtime_input(audio=generativelanguage_types.Blob(data=audio_bytes, mime_type="audio/x-mulaw"))
                            elif message_json.get("event") == "stop":
                                break
                    except WebSocketDisconnect:
                        logger.info("Twilio cerró la conexión WebSocket.")
                    except Exception as e:
                        logger.error(f"Error en el bucle Twilio->Gemini: {e}")

                await asyncio.gather(gemini_to_twilio(), twilio_to_gemini())

        except Exception as e:
            logger.error(f"Error crítico en la sesión de Gemini Live: {e}", exc_info=True)
        finally:
            logger.info(f"🧹 Limpiando recursos para {call_sid}")
            if call_sid in active_streams_sids:
                del active_streams_sids[call_sid]

@app.get("/", response_class=PlainTextResponse)
async def read_root(): return "Servidor del agente de voz activo."

@app.get("/_ah/health", response_class=PlainTextResponse)
async def health_check(): return "OK"

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8080)), reload=True)