
# main.py

import os
import json
import base64
import asyncio
import logging

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException # Asegúrate que HTTPException está importado
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv # Solo para desarrollo local, no para Cloud Run
from pydantic import BaseModel # Importar BaseModel

# Dependencias de Google Cloud y ADK
from google.cloud import secretmanager
from google.adk.sessions.in_memory_session_service import InMemorySessionService # SCALABILITY_NOTE: Producción usaría algo persistente
from google.adk.agents.run_config import RunConfig
from google.adk.agents import LiveRequestQueue
from google.adk.runners import Runner
from google.generativeai import types as generativelanguage_types # O google.ai.generativelanguage_v1beta.types
import google.generativeai as genai # Para configurar la API Key de Gemini

# Configuración del logger para la aplicación
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Inicialización de la aplicación FastAPI
app = FastAPI()

# Configuración de CORS para permitir solicitudes desde cualquier origen (útil para desarrollo)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Permite todos los orígenes
    allow_credentials=True,
    allow_methods=["*"],  # Permite todos los métodos
    allow_headers=["*"],  # Permite todas las cabeceras
)

# Configuración global para las credenciales y el ID del proyecto de Google Cloud
# Estas variables se cargarán desde el entorno o un archivo .env para desarrollo local
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID")

def get_project_id():
    """Retorna el ID del proyecto de Google Cloud, cargándolo desde .env si es necesario para desarrollo local."""
    effective_project_id = GCP_PROJECT_ID
    if not effective_project_id:
            # Intentar cargar de .env para desarrollo local SI NO ESTAMOS EN CLOUD RUN
            # Cloud Run no usa .env; las variables se inyectan directamente.
            # Una forma de detectar si estamos en Cloud Run es verificar K_SERVICE
            if not os.getenv("K_SERVICE"): # K_SERVICE es una variable de entorno estándar en Cloud Run
                logger.info("No estamos en Cloud Run y GCP_PROJECT_ID no está configurado. Intentando cargar desde .env para secretos locales.")
                load_dotenv()
                effective_project_id = os.getenv("GCP_PROJECT_ID_LOCAL") # Espera esta variable en tu .env local
                if not effective_project_id:
                    logger.error("GCP_PROJECT_ID_LOCAL no encontrado en .env para cargar secretos locales.")
                    return None
            else:
                logger.error("GCP_PROJECT_ID no está configurado como variable de entorno en Cloud Run.")
                return None
    return effective_project_id

def get_secret(secret_id, project_id):
    """Recupera un secreto de Google Secret Manager."""
    client = secretmanager.SecretManagerServiceClient()
    secret_name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    try:
        response = client.access_secret_version(name=secret_name)
        return response.payload.data.decode("UTF-8")
    except Exception as e:
        logger.error(f"Error al acceder al secreto {secret_id}: {e}")
        return None

# En `main.py` o un archivo de configuración apropiado
def configure_global_services():
    """Configura servicios globales como la API Key de Gemini."""
    project_id = get_project_id()
    if not project_id:
        logger.error("ID del proyecto no configurado. No se pueden cargar las configuraciones globales.")
        return

    # Cargar y configurar la API Key de Gemini desde Secret Manager
    gemini_api_key = get_secret("GEMINI_API_KEY", project_id)
    if gemini_api_key:
        genai.configure(api_key=gemini_api_key)
        logger.info("API Key de Gemini configurada exitosamente.")
    else:
        logger.error("No se pudo configurar la API Key de Gemini. Verifica que GEMINI_API_KEY esté en Secret Manager.")

    # NOTA_SCALABILIDAD: Reemplaza InMemorySessionService con una solución persistente para producción.
    # Esta configuración es solo para demostración y desarrollo.
    # TODO: Configurar un SessionService persistente para producción (ej. Firestore, Cloud Memorystore).
    # from google.adk.sessions.firestore_session_service import FirestoreSessionService
    # Runner.config_session_service(FirestoreSessionService(project_id=project_id))
    # Runner.config_session_service(InMemorySessionService()) # Para desarrollo
    logger.info("Session service configurado para usar InMemorySessionService (desarrollo).")

# Llama a la función para configurar servicios al iniciar la aplicación.
configure_global_services()

# Modelo para las solicitudes que contienen un prompt para el agente
class PromptRequest(BaseModel):
    prompt: str

@app.post("/chat") # Corrección: eliminada la barra invertida
async def process_chat(request: PromptRequest):
    """Procesa una solicitud de chat y devuelve la respuesta del agente."""
    logger.info(f"Solicitud de chat recibida con el prompt: {request.prompt}")

    run_config = RunConfig()
    live_request_queue = LiveRequestQueue.create(run_config=run_config)

    # Enviar el prompt al agente a través de la cola de solicitudes
    try:
        live_request_queue.send_request(request.prompt)
        logger.info("Prompt enviado al agente.")
    except Exception as e:
        logger.error(f"Error al enviar el prompt al agente: {e}")
        raise HTTPException(status_code=500, detail=f"Error al enviar prompt: {e}")

    # Esperar la respuesta del agente
    response_parts = []
    try:
        logger.info("Esperando respuesta del agente...")
        for part in live_request_queue:
            if part.payload:
                response_parts.append(part.payload.decode('utf-8'))
                logger.info(f"Parte de la respuesta recibida: {part.payload.decode('utf-8')}")
            if part.is_complete:
                logger.info("Respuesta completa recibida del agente.")
                break
    except Exception as e:
        logger.error(f"Error al recibir la respuesta del agente: {e}")
        # Considera si debes limpiar o cerrar la cola aquí si es necesario
        raise HTTPException(status_code=500, detail=f"Error al recibir respuesta: {e}")
    finally:
        # Asegúrate de cerrar la cola para liberar recursos
        live_request_queue.close()
        logger.info("Cola de solicitudes cerrada.")

    final_response = "".join(response_parts)
    logger.info(f"Respuesta final ensamblada: {final_response}")
    return {"response": final_response}



# Nueva ruta para el WebSocket, siguiendo el ejemplo de arriba
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Maneja las conexiones WebSocket para la comunicación bidireccional con el agente."""
    await websocket.accept()
    logger.info("Cliente WebSocket conectado.")

    run_config = RunConfig()
    live_request_queue = LiveRequestQueue.create(run_config=run_config)

    try:
        # Tarea para escuchar mensajes del cliente WebSocket
        async def listen_to_client():
            try:
                while True:
                    data = await websocket.receive_text()
                    logger.info(f"Mensaje recibido del cliente WebSocket: {data}")
                    live_request_queue.send_request(data) # Enviar el prompt al agente
                    logger.info("Prompt enviado al agente a través de WebSocket.")
            except WebSocketDisconnect:
                logger.info("Cliente WebSocket desconectado (esperado al cerrar la conexión).")
                # Indicar a la tarea de envío que el cliente se ha desconectado
                # Esto podría hacerse con una variable de evento o cancelando la tarea directamente
                # Por simplicidad, aquí solo registramos el evento.
            except Exception as e:
                logger.error(f"Error al escuchar al cliente WebSocket: {e}")
                # Considerar cerrar la conexión WebSocket si ocurre un error inesperado
                # await websocket.close(code=1011) # Internal Error
            finally:
                # Cuando la escucha termina (desconexión o error), cerramos la cola desde aquí
                # o nos aseguramos de que se cierre en el bloque finally principal.
                if not live_request_queue.is_closed:
                    live_request_queue.close()
                    logger.info("Cola de solicitudes cerrada desde listen_to_client.")

        # Tarea para enviar respuestas del agente al cliente WebSocket
        async def send_to_client():
            try:
                for part in live_request_queue:
                    if part.payload:
                        message = part.payload.decode('utf-8')
                        await websocket.send_text(message)
                        logger.info(f"Respuesta del agente enviada al cliente WebSocket: {message}")
                    if part.is_complete:
                        logger.info("Respuesta completa del agente enviada.")
                        break # Salir del bucle una vez que la respuesta está completa
            except WebSocketDisconnect:
                # Esto puede ocurrir si el cliente se desconecta mientras el agente aún está procesando
                logger.info("Cliente WebSocket desconectado mientras se enviaban respuestas.")
            except Exception as e:
                logger.error(f"Error al enviar datos al cliente WebSocket: {e}")
            finally:
                # Asegurar que la cola se cierre si esta tarea termina inesperadamente.
                if not live_request_queue.is_closed:
                    live_request_queue.close()
                    logger.info("Cola de solicitudes cerrada desde send_to_client.")

        # Ejecutar ambas tareas concurrentemente
        # El `asyncio.gather` esperará a que ambas tareas completen.
        # Si una tarea falla con una excepción, `gather` la propagará.
        await asyncio.gather(
            listen_to_client(),
            send_to_client()
        )

    except Exception as e:
        # Captura excepciones generales que podrían ocurrir fuera de las tareas (ej. al crear la cola)
        logger.error(f"Error en el manejo del WebSocket: {e}")
        # Intenta cerrar el WebSocket con un código de error si aún está abierto
        try:
            await websocket.close(code=1011) # Internal Error
        except RuntimeError as re:
            # Esto puede suceder si el WebSocket ya está cerrado o en un estado inválido
            logger.warning(f"Intento de cerrar WebSocket ya cerrado/inválido: {re}")
    finally:
        # Asegurarse de que la cola de solicitudes esté cerrada al final, sin importar qué.
        if not live_request_queue.is_closed:
            live_request_queue.close()
            logger.info("Cola de solicitudes cerrada en el bloque finally principal del WebSocket.")
        logger.info("Conexión WebSocket cerrada.")


@app.post("/v1/projects/{project_id}/locations/{location_id}/agents/{agent_id}:run")
async def run_agent(project_id: str, location_id: str, agent_id: str, request: Request):
    """Ejecuta el agente con la configuración proporcionada."""
    logger.info(f"Solicitud para ejecutar agente recibida: {project_id}/{location_id}/{agent_id}")

    try:
        # Obtener los datos del cuerpo de la solicitud
        request_body_bytes = await request.body()
        logger.debug(f"Cuerpo de la solicitud (bytes): {request_body_bytes[:500]}...") # Loguea solo una parte

        # Verificar si el cuerpo está vacío
        if not request_body_bytes:
            logger.warning("El cuerpo de la solicitud está vacío.")
            raise HTTPException(status_code=400, detail="El cuerpo de la solicitud no puede estar vacío.")

        # Decodificar el cuerpo de la solicitud de bytes a string (asumiendo UTF-8)
        try:
            request_body_str = request_body_bytes.decode('utf-8')
            logger.debug(f"Cuerpo de la solicitud (str): {request_body_str[:500]}...") # Loguea solo una parte
        except UnicodeDecodeError as ude:
            logger.error(f"Error al decodificar el cuerpo de la solicitud como UTF-8: {ude}")
            raise HTTPException(status_code=400, detail=f"Error de decodificación UTF-8: {ude}")

        # Convertir la cadena JSON a un diccionario Python
        try:
            data = json.loads(request_body_str)
            logger.debug(f"Datos JSON deserializados: {data}")
        except json.JSONDecodeError as jde:
            logger.error(f"Error al deserializar JSON del cuerpo de la solicitud: {jde}")
            logger.error(f"Contenido problemático (primeros 500 caracteres): {request_body_str[:500]}")
            raise HTTPException(status_code=400, detail=f"JSON malformado: {jde}")

        # Extraer el prompt del campo 'prompt' o 'input'
        prompt_text = data.get("prompt") or data.get("input")
        if not prompt_text:
            logger.warning("No se encontró 'prompt' o 'input' en la solicitud.")
            raise HTTPException(status_code=400, detail="La solicitud debe contener un campo 'prompt' o 'input'.")

        logger.info(f"Prompt extraído para el agente: {prompt_text}")

        # Procesar el prompt con el agente (lógica similar a /chat pero adaptada)
        run_config = RunConfig() # Configuración por defecto, ajustar según sea necesario
        live_request_queue = LiveRequestQueue.create(run_config=run_config)

        live_request_queue.send_request(prompt_text)
        logger.info("Prompt enviado al agente a través de la ruta /run.")

        response_parts = []
        output_text = "" # Para almacenar la respuesta completa como texto

        for part in live_request_queue:
            if part.payload:
                decoded_payload = part.payload.decode('utf-8')
                response_parts.append(decoded_payload)
                logger.debug(f"Parte de la respuesta recibida (agente /run): {decoded_payload}")
            if part.is_complete:
                logger.info("Respuesta completa recibida del agente (ruta /run).")
                break

        live_request_queue.close() # Asegurarse de cerrar la cola
        logger.info("Cola de solicitudes cerrada (ruta /run).")

        output_text = "".join(response_parts)
        logger.info(f"Respuesta final del agente (ruta /run): {output_text}")

        # La respuesta debe estar en el formato esperado por el cliente de esta API
        # Por ejemplo, si el cliente espera un JSON con un campo "output":
        # Serializa la respuesta en el formato de google.cloud.aiplatform.v1.PredictionResponse
        # Esto es un ejemplo y puede necesitar ajustarse al esquema exacto.
        # Para el ADK, la respuesta que espera el runner es una cadena de texto simple, no un JSON complejo.
        # Si el cliente es el Test Client del ADK, espera una cadena de texto plano.

        # Si se necesita devolver una estructura JSON específica:
        # return {"output": output_text, "other_fields": "values"}


        # Para compatibilidad con el ADK Test Client que espera PlainTextResponse:
        return PlainTextResponse(content=output_text)

    except HTTPException as http_exc:
        # Re-lanzar HTTPExceptions para que FastAPI las maneje
        raise http_exc
    except Exception as e:
        logger.error(f"Error inesperado en /run: {e}", exc_info=True)
        # Para errores no HTTP, devuelve un error 500 genérico
        raise HTTPException(status_code=500, detail=f"Error interno del servidor: {str(e)}")



# Ruta raíz para verificar que el servidor está funcionando
@app.get("/")
async def read_root():
    """Ruta raíz que devuelve un mensaje de bienvenida."""
    logger.info("Solicitud recibida en la ruta raíz (/).")
    return {"message": "Bienvenido al Agente de Jarvis - FastAPI Edition"}

# Health check endpoint
@app.get("/_ah/health")
async def health_check():
    """Endpoint de health check para Cloud Run."""
    logger.info("Health check solicitado.")
    return PlainTextResponse("OK")

# Para desarrollo local, permite ejecutar el servidor con Uvicorn directamente
if __name__ == "__main__":
    import uvicorn
    logger.info("Iniciando servidor Uvicorn para desarrollo local en el puerto 8080.")
    # El puerto aquí debe coincidir con el EXPOSE del Dockerfile y el CMD
    # Cloud Run espera por defecto el puerto 8080.
    uvicorn.run(app, host="0.0.0.0", port=8080)
