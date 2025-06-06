# Twilio
from twilio.twiml.voice_response import VoiceResponse, Start

# ================================================
# 0. Configuración de Logging y Carga de Entorno
# ================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- INICIO: Configuración de Secretos y Variables de Entorno ---
GCP_PROJECT_ID_ENV = os.getenv("GCP_PROJECT_ID")
SERVER_BASE_URL = os.getenv("SERVER_BASE_URL")
APP_NAME = "Twilio Voice Agent Gemini"

google_calendar_creds = None
google_calendar_service = None
gemini_api_key_loaded = False

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

    calendar_creds_json_str = get_secret_from_manager("google-calendar-credentials", project_id)
    calendar_token_json_str = get_secret_from_manager("google-calendar-token", project_id)

    if calendar_token_json_str and calendar_creds_json_str:
        try:
            token_info = json.loads(calendar_token_json_str)
            client_secrets_dict = json.loads(calendar_creds_json_str)
            scopes = token_info.get('scopes', ['https://www.googleapis.com/auth/calendar'])
            google_calendar_creds = Credentials(
                token=token_info.get('token'), refresh_token=token_info.get('refresh_token'),
                token_uri=token_info.get('token_uri', 'https://oauth2.googleapis.com/token'),
                client_id=client_secrets_dict.get('installed', {}).get('client_id'),
                client_secret=client_secrets_dict.get('installed', {}).get('client_secret'),
                scopes=scopes
            )
            if google_calendar_creds and google_calendar_creds.expired and google_calendar_creds.refresh_token:
                logger.info("Token de Google Calendar expirado, refrescando...")
                google_calendar_creds.refresh(GoogleAuthRequest())
                logger.info("Token de Google Calendar refrescado.")
            google_calendar_service = googleapiclient.discovery.build('calendar', 'v3', credentials=google_calendar_creds)
            logger.info("Servicio de Google Calendar inicializado.")
        except Exception as e:
            logger.error(f"Error inicializando Google Calendar: {e}", exc_info=True)
    else:
        if not calendar_creds_json_str: logger.critical("CALENDAR_CREDENTIALS_JSON_STR no cargado.")
        if not calendar_token_json_str: logger.critical("CALENDAR_TOKEN_JSON_STR no cargado.")

session_service = InMemorySessionService()
# --- FIN: Configuración ---

app = FastAPI(title=APP_NAME, version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
async def startup_event_handler(): # Renombrado para claridad
    logger.info("Iniciando aplicación FastAPI y configurando servicios globales...")
    initialize_global_services()
    if not root_agent: logger.warning("root_agent no disponible. Funcionalidad de voz limitada.")
    if not SERVER_BASE_URL and os.getenv("K_SERVICE"): logger.error("SERVER_BASE_URL no configurado en Cloud Run.")

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
            logger.info(f"📞 Llamada recibida - SID: {call_sid or 'NO_CALL_SID'}")
            if not call_sid:
                logger.error("Llamada sin CallSid.")
                return PlainTextResponse("Error: CallSid no encontrado.", status_code=400, media_type="application/xml")
            
            response = VoiceResponse()
            if not SERVER_BASE_URL:
                logger.error("SERVER_BASE_URL no configurado.")
                response.say("Error de configuración del servidor.", language="es-ES")
                return PlainTextResponse(str(response), media_type="application/xml", status_code=500)

            websocket_url = f"wss://{SERVER_BASE_URL}/stream/{call_sid}"
            logger.info(f"Iniciando stream a: {websocket_url}")
            start = Start()
            start.stream(url=websocket_url)
            response.append(start)
            # El saludo inicial lo dará Gemini después de conectar el WebSocket.
            # Si quieres un saludo inmediato ANTES de que Gemini responda:
            response.say("Conectando con el asistente Gemini. Por favor, espere.", language="es-ES")
            # response.pause(length=1) # Opcional
            return PlainTextResponse(str(response), media_type="application/xml")
        except Exception as e:
            logger.error(f"Error en /voice: {e}", exc_info=True)
            error_response = VoiceResponse()
            error_response.say("Error procesando llamada.", language="es-ES")
            return PlainTextResponse(str(error_response), media_type="application/xml", status_code=500)

    # CORRECCIÓN: start_agent_session no necesita ser async si no usa await internamente después de los cambios
    def start_agent_session(session_id: str): # No es async
        logger.info(f"Iniciando sesión ADK para: {session_id}")
        session = session_service.create_session(
            app_name=APP_NAME, user_id=session_id, session_id=session_id
        )
        runner = Runner(app_name=APP_NAME, agent=root_agent, session_service=session_service)

        # CORRECCIÓN: Simplificar RunConfig, eliminar SpeechConfig y OutputAudioConfig explícitos
        run_config = RunConfig(
            response_modalities=["AUDIO", "TEXT"]
        )
        
        live_request_queue = LiveRequestQueue()
        live_events = runner.run_live(
            session=session, live_request_queue=live_request_queue, run_config=run_config
        )
        # CORRECCIÓN: Eliminar llamada a runner.run_query de aquí
        logger.info(f"Sesión ADK y runner iniciados para {session_id}")
        return live_events, live_request_queue, session

    async def process_gemini_responses(websocket: WebSocket, call_sid: str, live_events):
        try:
            async for event in live_events:
                # (Tu lógica existente para procesar eventos de Gemini, parece bien)
                logger.debug(f"Evento ADK {call_sid}: {event.type if hasattr(event, 'type') else 'Tipo desc.'}")
                if hasattr(event, 'type') and event.type == generativelanguage_types.LiveEventType.OUTPUT_DATA:
                    if event.output_data and event.output_data.text_data:
                        logger.info(f"Texto Gemini {call_sid}: {event.output_data.text_data.text}")
                    if event.output_data and event.output_data.audio_data:
                        audio_chunk = event.output_data.audio_data.data
                        logger.info(f"🔊 Audio Gemini a Twilio {call_sid}: {len(audio_chunk)} bytes")
                        payload = base64.b64encode(audio_chunk).decode("utf-8")
                        stream_sid = active_streams_sids.get(call_sid)
                        if not stream_sid:
                            logger.warning(f"No stream_sid para {call_sid} al enviar audio Gemini.")
                            continue
                        await websocket.send_json({"event": "media", "streamSid": stream_sid, "media": {"payload": payload}})
                elif hasattr(event, 'type') and event.type == generativelanguage_types.LiveEventType.SESSION_ENDED:
                    logger.info(f"Sesión ADK finalizada para {call_sid}.")
                    break
                elif hasattr(event, 'type') and event.type == generativelanguage_types.LiveEventType.ERROR:
                    err_msg = event.error.message if hasattr(event, 'error') and hasattr(event.error, 'message') else 'Error ADK'
                    logger.error(f"Error sesión ADK {call_sid}: {err_msg}")
                    break
        except WebSocketDisconnect: logger.info(f"WS desconectado (Gemini) para {call_sid}.")
        except Exception as e: logger.error(f"Error procesando respuestas Gemini {call_sid}: {e}", exc_info=True)
        finally: logger.info(f"Fin procesamiento respuestas Gemini para {call_sid}.")

    async def process_twilio_audio(websocket: WebSocket, call_sid: str, live_request_queue: LiveRequestQueue):
        try:
            while True:
                message_json = await websocket.receive_json()
                event_type = message_json.get("event")
                if event_type == "connected": logger.info(f"🔌 WS conectado (Twilio) {call_sid}. Protocolo: {message_json.get('protocol')}")
                elif event_type == "start":
                    stream_sid = message_json.get("streamSid")
                    active_streams_sids[call_sid] = stream_sid
                    logger.info(f"🎙️ Stream Twilio iniciado {call_sid}. streamSid: {stream_sid}")
                    # Opcional: Enviar un prompt inicial a Gemini una vez que el stream de Twilio está listo
                    # if live_request_queue and not live_request_queue.is_closed:
                    #     initial_greeting_prompt = "El usuario ha conectado. Salúdalo y pregúntale en qué puedes ayudar."
                    #     live_request_queue.send_request(initial_greeting_prompt) # Envía como texto
                    #     logger.info(f"Prompt de saludo inicial enviado a Gemini para {call_sid}")

                elif event_type == "media":
                    payload = message_json["media"]["payload"]
                    audio_data_raw = base64.b64decode(payload)
                    if live_request_queue and not live_request_queue.is_closed:
                        blob_data = generativelanguage_types.Blob(data=audio_data_raw, mime_type="audio/x-mulaw")
                        live_request_queue.send_realtime(blob_data)
                        logger.debug(f"🔊 Audio Twilio a Gemini {call_sid}: {len(audio_data_raw)} bytes")
                    else: logger.warning(f"live_request_queue no disponible/cerrada para {call_sid}.")
                elif event_type == "stop":
                    logger.info(f"🔴 Fin stream Twilio para {call_sid}")
                    if live_request_queue and not live_request_queue.is_closed: live_request_queue.close()
                    break
                elif event_type == "mark": logger.info(f"✅ Mark Twilio {call_sid}: {message_json.get('name')}")
        except WebSocketDisconnect: logger.info(f"WS desconectado (Twilio) por Twilio para {call_sid}.")
        except Exception as e: logger.error(f"Error en WS Twilio {call_sid}: {e}", exc_info=True)
        finally:
            if live_request_queue and not live_request_queue.is_closed: live_request_queue.close()
            logger.info(f"Fin procesamiento audio Twilio para {call_sid}.")

    @app.websocket("/stream/{call_sid}")
    async def websocket_audio_endpoint(websocket: WebSocket, call_sid: str):
        await websocket.accept()
        logger.info(f"🔗 WS audio aceptado para {call_sid}")
        live_events, live_request_queue, adk_session = None, None, None
        twilio_task, gemini_task = None, None
        try:
            if not gemini_api_key_loaded:
                logger.error(f"API Key Gemini no cargada para {call_sid}.")
                await websocket.close(code=1011, reason="Error servidor: API Key Gemini")
                return
            if not google_calendar_service:
                logger.error(f"Servicio Calendar no inicializado para {call_sid}.")
                await websocket.close(code=1011, reason="Error servidor: Calendar service")
                return

            live_events, live_request_queue, adk_session = start_agent_session(call_sid) # Ya no es async
            
            twilio_task = asyncio.create_task(process_twilio_audio(websocket, call_sid, live_request_queue))
            gemini_task = asyncio.create_task(process_gemini_responses(websocket, call_sid, live_events))
            
            await asyncio.gather(twilio_task, gemini_task)
        except Exception as e:
            logger.error(f"Error principal en WS handler {call_sid}: {e}", exc_info=True)
        finally:
            logger.info(f"🧹 Limpiando WS {call_sid}")
            # Cancelar tareas
            if twilio_task and not twilio_task.done(): twilio_task.cancel()
            if gemini_task and not gemini_task.done(): gemini_task.cancel()
            try:
                if twilio_task: await twilio_task
            except asyncio.CancelledError: logger.info(f"Twilio task cancelada {call_sid}")
            try:
                if gemini_task: await gemini_task
            except asyncio.CancelledError: logger.info(f"Gemini task cancelada {call_sid}")

            if live_request_queue and not live_request_queue.is_closed: live_request_queue.close()
            if call_sid in active_streams_sids: del active_streams_sids[call_sid]
            try:
                if websocket.client_state != WebSocketState.DISCONNECTED:
                    await websocket.close(code=1000)
            except RuntimeError: pass 
            except Exception as e_close: logger.error(f"Error cerrando WS {call_sid}: {e_close}")
            logger.info(f"🔚 WS audio cerrado para {call_sid}")
else:
    logger.warning("root_agent no definido. Endpoints /voice y /stream no disponibles.")

# ================================================
# TUS RUTAS EXISTENTES (CHAT, WS, RUN)
# (Se mantienen como estaban, asumiendo que su lógica es independiente o ya las adaptaste)
# ================================================
class PromptRequest(BaseModel): prompt: str

@app.post("/chat")
async def process_chat(request: PromptRequest):
    # ... (Tu lógica actual para /chat) ...
    # Asegúrate que maneje la no disponibilidad de root_agent si lo usa
    logger.info(f"Chat: {request.prompt}")
    if not root_agent or not gemini_api_key_loaded:
        raise HTTPException(status_code=503, detail="Chat no disponible.")
    # Ejemplo simplificado de uso de run_query (debe ser async)
    try:
        runner = Runner(app_name=APP_NAME, agent=root_agent, session_service=session_service)
        session = session_service.create_session(app_name=APP_NAME, user_id="chat_user", session_id=f"chat_{os.urandom(8).hex()}")
        response = await runner.run_query(session=session, query=request.prompt, run_config=RunConfig(response_modalities=["TEXT"]))
        text_response = "".join([part.text_data.text for part in response.parts if part.text_data])
        return {"response": text_response}
    except Exception as e:
        logger.error(f"Error en /chat: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.websocket("/ws")
async def websocket_chat_endpoint(websocket: WebSocket): # Renombrado para evitar colisión con el de audio
    # ... (Tu lógica actual para /ws) ...
    await websocket.accept()
    logger.info("WS /ws conectado.")
    if not root_agent or not gemini_api_key_loaded:
        await websocket.close(code=1011, reason="Servicio no disponible")
        return
    # (Lógica de ejemplo)
    runner = Runner(app_name=APP_NAME, agent=root_agent, session_service=session_service)
    session = session_service.create_session(app_name=APP_NAME, user_id="ws_user", session_id=f"ws_{os.urandom(8).hex()}")
    run_config = RunConfig(response_modalities=["TEXT"])
    try:
        while True:
            data = await websocket.receive_text()
            response = await runner.run_query(session=session, query=data, run_config=run_config)
            text_response = "".join([part.text_data.text for part in response.parts if part.text_data])
            await websocket.send_text(text_response)
    except WebSocketDisconnect: logger.info("WS /ws desconectado.")
    except Exception as e: logger.error(f"Error en WS /ws: {e}", exc_info=True)

@app.post("/v1/projects/{project_id}/locations/{location_id}/agents/{agent_id}:run")
async def run_agent_endpoint_v1(project_id: str, location_id: str, agent_id: str, request: Request): # Renombrado
    # ... (Tu lógica actual para /run) ...
    logger.info(f"/run: {project_id}/{location_id}/{agent_id}")
    if not root_agent or not gemini_api_key_loaded:
        raise HTTPException(status_code=503, detail="/run no disponible.")
    # (Lógica de ejemplo)
    try:
        body_bytes = await request.body()
        data = json.loads(body_bytes.decode('utf-8'))
        prompt = data.get("prompt") or data.get("input")
        if not prompt: raise HTTPException(status_code=400, detail="Falta prompt/input")
        
        runner = Runner(app_name=APP_NAME, agent=root_agent, session_service=session_service)
        session = session_service.create_session(app_name=APP_NAME, user_id="run_user", session_id=f"run_{os.urandom(8).hex()}")
        response = await runner.run_query(session=session, query=prompt, run_config=RunConfig(response_modalities=["TEXT"]))
        output_text = "".join([part.text_data.text for part in response.parts if part.text_data])
        return PlainTextResponse(content=output_text)
    except Exception as e:
        logger.error(f"Error en /run: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

# ================================================
# RUTAS ESTÁNDAR
# ================================================
@app.get("/")
async def read_root():
    return {"message": "Bienvenido al Agente de Jarvis con Voz - FastAPI Edition"}

@app.get("/_ah/health")
async def health_check():
    return PlainTextResponse("OK")

# ================================================
# Ejecución
# ================================================
if __name__ == "__main__":
    logger.info("Iniciando Uvicorn localmente...")
    if not os.getenv("K_SERVICE"): load_dotenv()
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=True)

