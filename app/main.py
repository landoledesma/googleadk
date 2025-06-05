import asyncio
import base64
import json
import os
from pathlib import Path
from typing import AsyncIterable, Dict # Añadido Dict

from dotenv import load_dotenv
from fastapi import FastAPI, Query, WebSocket, Request, Response # Añadido Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from google.adk.agents import LiveRequestQueue
from google.adk.agents.run_config import RunConfig
from google.adk.events.event import Event
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.genai import types
from jarvis.agent import root_agent

from twilio.twiml.voice_response import VoiceResponse, Connect # Añadido
from twilio.request_validator import RequestValidator # Opcional, para seguridad

# Load Gemini API Key
load_dotenv()

APP_NAME = "ADK Twilio Voice Agent" # Nombre actualizado
session_service = InMemorySessionService()

# Opcional: Para validar las solicitudes de Twilio
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

def start_agent_session(session_id, is_audio=True): # is_audio siempre True para Twilio
    """Starts an agent session"""
    session = session_service.create_session(
        app_name=APP_NAME,
        user_id=session_id, # Usar CallSid de Twilio como user_id y session_id
        session_id=session_id,
    )
    runner = Runner(
        app_name=APP_NAME,
        agent=root_agent,
        session_service=session_service,
    )
    modality = "AUDIO" # Siempre audio para Twilio

    # Usar el modelo TTS que especificaste (Gemini 1.5 Flash Preview)
    # El nombre del modelo en ADK puede variar. "Puck" es una voz preconstruida.
    # Si Gemini 1.5 Flash es un modelo de generación de voz, la configuración sería diferente.
    # ADK usa las voces de Google Cloud TTS. "Puck" es una de ellas.
    # Si quieres usar un modelo Gemini para TTS, verifica la documentación de ADK para la configuración correcta.
    # Por ahora, mantendremos "Puck" como ejemplo, asumiendo que es una voz de calidad.
    # Si el modelo "gemini-1.5-flash-preview-0514" (o similar) es usado por ADK para TTS,
    # la configuración de speech_config sería distinta.
    # El modelo `gemini-2.0-flash-exp` en `agent.py` es para la lógica del agente, no necesariamente TTS.
    speech_config = types.SpeechConfig(
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Puck")
        ),
        # Asegúrate de que el formato de salida sea compatible con Twilio (e.g., mulaw)
        # ADK por defecto podría dar PCM. Twilio prefiere mulaw para streams.
        # output_audio_encoding=types.OutputAudioEncoding.OUTPUT_AUDIO_ENCODING_MULAW # Ejemplo
    )
    config = {"response_modalities": [modality], "speech_config": speech_config}
    config["output_audio_transcription"] = {} # Para obtener el texto también (útil para logs)
    
    # Configuración para la transcripción del audio de entrada
    config["input_audio_transcription"] = {
        # "model": "chirp", # O el modelo de STT que prefieras/esté disponible
        # "language_codes": ["es-US"] # Ajusta el idioma
    }

    run_config = RunConfig(**config)
    live_request_queue = LiveRequestQueue()
    live_events = runner.run_live(
        session=session,
        live_request_queue=live_request_queue,
        run_config=run_config,
    )
    return live_events, live_request_queue

# Adaptado para Twilio Media Streams
async def agent_to_twilio_messaging(
    websocket: WebSocket, live_events: AsyncIterable[Event | None], stream_sid: str
):
    """Agent to Twilio communication via WebSocket"""
    async for event in live_events:
        if event is None:
            continue

        if event.turn_complete or event.interrupted:
            print(f"[AGENT TO TWILIO {stream_sid}]: Turn complete or interrupted")
            # Podrías enviar un mensaje de 'stop' si es necesario, o Twilio lo maneja al cerrar el WS.
            # O un mensaje de <Hangup/> si el agente decide terminar la llamada.
            continue

        part = event.content and event.content.parts and event.content.parts[0]
        if not part or not isinstance(part, types.Part):
            continue

        # Audio del agente para Twilio
        is_audio_response = (
            part.inline_data
            and part.inline_data.mime_type
            and part.inline_data.mime_type.startswith("audio/") # ADK usualmente da audio/pcm o audio/opus
        )
        if is_audio_response:
            audio_data = part.inline_data.data
            if audio_data:
                # Twilio Streams espera audio en 'μ-law' (PCMU) codificado en base64.
                # ADK podría dar PCM. Si es así, necesitarías convertirlo.
                # Por simplicidad, asumamos que ADK da audio/pcm y lo enviamos.
                # Si ADK da audio/opus, Twilio lo soporta.
                # Si es audio/pcm, Twilio prefiere que sea μ-law.
                # Python tiene el módulo `audioop` para convertir PCM lineal a μ-law.
                # Ejemplo (si audio_data es PCM lineal 16-bit):
                # import audioop
                # mulaw_data = audioop.lin2ulaw(audio_data, 2) # 2 bytes por muestra (16-bit)
                # payload = base64.b64encode(mulaw_data).decode("utf-8")
                
                # Por ahora, enviamos lo que ADK nos da, asumiendo que es compatible o Twilio lo maneja
                payload = base64.b64encode(audio_data).decode("utf-8")
                
                message = {
                    "event": "media",
                    "streamSid": stream_sid,
                    "media": {
                        "payload": payload,
                        # "mimeType": "audio/x-mulaw" # Si convertiste a mulaw
                    },
                }
                await websocket.send_text(json.dumps(message))
                print(f"[AGENT TO TWILIO {stream_sid}]: Sent audio data {len(audio_data)} bytes.")
        
        # Texto del agente (para logging o si queremos mostrarlo en algún lado)
        if part.text and event.partial: # O event.final si solo quieres el final
             print(f"[AGENT TRANSCRIPTION {stream_sid}]: {part.text}")


# Adaptado para Twilio Media Streams
async def twilio_to_agent_messaging(
    websocket: WebSocket, live_request_queue: LiveRequestQueue
):
    """Twilio to agent communication via WebSocket"""
    media_stream_sid = None
    while True:
        message_json = await websocket.receive_text()
        message = json.loads(message_json)
        event_type = message.get("event")

        if event_type == "connected":
            print(f"[TWILIO TO AGENT]: Connected event: {message}")
        
        elif event_type == "start":
            media_stream_sid = message.get("streamSid")
            start_info = message.get("start", {})
            call_sid = start_info.get("callSid")
            print(f"[TWILIO TO AGENT {media_stream_sid}]: Start event from CallSid {call_sid}. Config: {start_info}")
            # Aquí podrías pasar información de 'start' al agente si es necesario
            # Por ejemplo, el CallSid o custom parameters.

        elif event_type == "media":
            if not media_stream_sid:
                print("[TWILIO TO AGENT]: Error: Received media before start event.")
                continue

            payload = message["media"]["payload"]
            # El audio de Twilio es base64 encoded, usualmente μ-law (PCMU)
            audio_data = base64.b64decode(payload)
            
            # ADK espera los bytes crudos. El tipo MIME ayuda al ADK a interpretarlo.
            # Twilio envía audio/x-mulaw (8kHz) por defecto.
            live_request_queue.send_realtime(
                types.Blob(data=audio_data, mime_type="audio/x-mulaw") # Especificar el tipo de Twilio
            )
            # print(f"[TWILIO TO AGENT {media_stream_sid}]: Sent audio chunk {len(audio_data)} bytes to ADK.")

        elif event_type == "stop":
            print(f"[TWILIO TO AGENT {media_stream_sid}]: Stop event: {message}")
            # La llamada terminó o el stream se detuvo. Limpiar.
            live_request_queue.close() # Avisa al ADK que no hay más input
            break # Salir del bucle de escucha

        elif event_type == "mark":
            print(f"[TWILIO TO AGENT {media_stream_sid}]: Mark event: {message}")

        else:
            print(f"[TWILIO TO AGENT {media_stream_sid}]: Unknown event: {message}")


app = FastAPI()

# Sirve archivos estáticos solo si todavía necesitas el frontend web para pruebas
STATIC_DIR = Path(__file__).parent.parent / "static" # Ajusta la ruta si es necesario
if STATIC_DIR.exists():
     app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
     @app.get("/")
     async def root():
         return FileResponse(os.path.join(STATIC_DIR, "index.html"))
else:
    @app.get("/")
    async def root_message():
        return {"message": "ADK Twilio Voice Agent is running. Configure your Twilio number to POST to /twilio/voice."}

# NUEVO: Endpoint para Twilio Voice Webhook
@app.post("/twilio/voice")
async def twilio_voice_webhook(request: Request):
    # Opcional: Validar que la solicitud viene de Twilio
    # if TWILIO_AUTH_TOKEN:
    #     validator = RequestValidator(TWILIO_AUTH_TOKEN)
    #     form_ = await request.form()
    #     if not validator.validate(str(request.url), form_, request.headers.get('X-Twilio-Signature', '')):
    #         return Response(content="Invalid Twilio Signature", status_code=403)

    response = VoiceResponse()
    connect = Connect()
    
    # Construye la URL del WebSocket. Para Cloud Run, usa el formato wss://<service-url>/ws/...
    # Durante el desarrollo local con ngrok: wss://<ngrok-id>.ngrok.io/ws/...
    # El {CallSid} es una buena forma de identificar la sesión de WebSocket.
    # Cloud Run te dará una URL base como https://my-app-abcdef-uc.a.run.app
    # La URL del WebSocket sería wss://my-app-abcdef-uc.a.run.app/ws/
    # El `request.url.scheme` y `request.url.netloc` pueden no ser correctos detrás de un proxy.
    # Es mejor obtener la URL base de una variable de entorno en Cloud Run.
    
    # Para Cloud Run, la URL pública se puede obtener de K_SERVICE y K_REVISION o construirla.
    # Pero es más simple si la configuras explícitamente o la infieres.
    # Si estás en Cloud Run, `request.base_url` debería ser la URL pública HTTPS.
    # ws_scheme = "wss" if request.url.scheme == "https" else "ws"
    # ws_url_base = f"{ws_scheme}://{request.url.netloc}"
    
    # Asumamos que la URL del servicio se define en una variable de entorno para Cloud Run
    # o que request.base_url es correcta.
    # Para Cloud Run, la URL base ya será https.
    base_url = str(request.base_url).replace("http://", "ws://").replace("https://", "wss://")
    if base_url.endswith('/'): # Asegurar que no haya doble slash
        base_url = base_url[:-1]

    # Usaremos el CallSid como session_id para el WebSocket.
    # Twilio lo envía en los parámetros del POST al webhook.
    form_data = await request.form()
    call_sid = form_data.get("CallSid")
    if not call_sid:
        response.say("Error: No CallSid provided.")
        return Response(content=str(response), media_type="application/xml")

    # El session_id para el WebSocket será el CallSid.
    websocket_url = f"{base_url}/ws/{call_sid}"

    print(f"Incoming call {call_sid}. Connecting to WebSocket: {websocket_url}")
    
    connect.stream(url=websocket_url)
    response.append(connect)
    response.pause(length=60) # Mantener la llamada activa mientras el stream se conecta y procesa. Ajusta si es necesario.
    
    # Podrías añadir un <Say> inicial si quieres, antes de conectar el stream.
    # response.say("Connecting to the assistant.")
    
    return Response(content=str(response), media_type="application/xml")


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    session_id: str,
    # is_audio: str = Query(default="true"), # Para Twilio, is_audio siempre es true
):
    await websocket.accept()
    print(f"Twilio WebSocket client connected for session_id (CallSid): {session_id}")

    # El session_id aquí es el CallSid de Twilio
    # El primer mensaje de Twilio por WebSocket contendrá el streamSid
    # Necesitamos esperar el mensaje 'start' para obtener el streamSid real del media stream.
    
    # Inicializamos el agente ADK aquí
    # is_audio es True por defecto para Twilio
    live_events, live_request_queue = start_agent_session(session_id, is_audio=True)

    # El stream_sid real lo obtendremos del mensaje 'start'
    # Lo pasaremos a agent_to_twilio_messaging cuando lo tengamos.
    # Por ahora, podemos empezar twilio_to_agent_messaging y que él extraiga el stream_sid
    # y luego inicie la otra tarea con el stream_sid correcto.

    # Corrección: `twilio_to_agent_messaging` necesita `live_request_queue`
    # `agent_to_twilio_messaging` necesita `live_events` y el `stream_sid`
    # El `stream_sid` se obtiene dentro de `twilio_to_agent_messaging`
    # Mejor reestructurar ligeramente:

    media_stream_sid = None
    initial_message_json = await websocket.receive_text() # Esperar el primer mensaje
    initial_message = json.loads(initial_message_json)

    if initial_message.get("event") == "start":
        media_stream_sid = initial_message.get("streamSid")
        call_sid_from_start = initial_message.get("start", {}).get("callSid")
        print(f"WebSocket Start event for CallSid {call_sid_from_start}, StreamSid {media_stream_sid}. Session_id from URL was {session_id}")
        # Aquí podrías validar que call_sid_from_start == session_id si es necesario

        # Procesar este primer mensaje de 'start' como si fuera parte del loop
        # Re-enviar el mensaje 'start' a la cola para ser procesado por twilio_to_agent_messaging
        # o manejarlo aquí y luego empezar los loops.

        # Para simplificar, vamos a asumir que `twilio_to_agent_messaging` manejará el 'start'
        # y pasará el stream_sid a `agent_to_twilio_messaging`
        # Esto requiere un poco de refactorización o un objeto compartido.

        # Alternativa: El `session_id` del websocket (CallSid) es suficiente para correlacionar
        # y el `stream_sid` es solo para el mensaje `media` de vuelta a Twilio.

        # Enfoque más simple:
        # `twilio_to_agent_messaging` extrae el stream_sid del evento 'start'
        # y lo usamos para `agent_to_twilio_messaging`.

        # La tarea `twilio_to_agent_messaging` procesará el mensaje 'start' que ya leímos
        # y los subsecuentes. Necesitamos una forma de que envíe el stream_sid a la otra tarea.
        # O, más fácil, que `twilio_to_agent_messaging` también maneje el evento `start` recibido.
        
        # Modificamos cómo se inician las tareas:
        # Primero, aseguramos que el `media_stream_sid` se establece.
        # La función `twilio_to_agent_messaging_wrapper` se encargará de esto.

        async def twilio_to_agent_messaging_wrapper(
            websocket: WebSocket,
            live_request_queue: LiveRequestQueue,
            first_message: Dict
        ):
            nonlocal media_stream_sid # Para actualizar la variable externa
            
            # Procesar el primer mensaje
            if first_message.get("event") == "start":
                media_stream_sid = first_message.get("streamSid")
                print(f"[TWILIO TO AGENT {media_stream_sid}]: Start event (from wrapper).")
                # Aquí puedes enviar el audio si el evento 'start' tiene 'mediaFormat'
                # y quieres que el ADK sepa el formato de entrada.
            
            elif first_message.get("event") == "media": # Si el primer mensaje es media (poco probable)
                 audio_data = base64.b64decode(first_message["media"]["payload"])
                 live_request_queue.send_realtime(
                     types.Blob(data=audio_data, mime_type="audio/x-mulaw")
                 )

            # Continuar con el bucle normal
            await twilio_to_agent_messaging(websocket, live_request_queue)

        # La tarea que envía audio al agente ADK (y obtiene el stream_sid)
        client_to_agent_task = asyncio.create_task(
            twilio_to_agent_messaging_wrapper(websocket, live_request_queue, initial_message)
        )

        # Esperar a que media_stream_sid se establezca (puede ser rápido)
        # O mejor, agent_to_twilio_messaging debe ser capaz de manejar un stream_sid None inicialmente
        # y solo enviar mensajes 'media' cuando stream_sid esté disponible.
        # En la práctica, Twilio envía 'start' primero.
        
        while media_stream_sid is None and not client_to_agent_task.done():
            await asyncio.sleep(0.01) # Espera corta a que se procese el evento 'start'

        if media_stream_sid:
            agent_to_client_task = asyncio.create_task(
                agent_to_twilio_messaging(websocket, live_events, media_stream_sid)
            )
            await asyncio.gather(agent_to_client_task, client_to_agent_task)
        else:
            print(f"Error: Could not get media_stream_sid for session {session_id}. Closing WebSocket.")
            if not client_to_agent_task.done():
                client_to_agent_task.cancel()
    
    else: # El primer mensaje no fue 'start'
        print(f"WebSocket for session {session_id} did not start with a 'start' event. First message: {initial_message}")
        # Aquí puedes decidir cerrar el websocket o intentar manejarlo.

    live_request_queue.close() # Asegurar que se cierre si algo sale mal.
    print(f"Twilio WebSocket client disconnected for session_id: {session_id}")