# jarvis/agent.py (LA VERSIÓN CORRECTA PARA EL ADK)
from google.adk.agents import Agent
# from google.adk.tools import google_search  # Import the search tool
from .tools import (
    create_event,
    delete_event,
    edit_event,
    get_current_time,
    list_events,
)
root_agent = Agent(
    # Este es el modelo que el ADK recomienda para este caso de uso
    model="gemini-2.5-flash-preview-native-audio-dialog", 
    name="jarvis",
    description="Agent to help with scheduling and calendar operations.",
    instruction=f"""
    You are Jarvis, a helpful assistant...
    """,
    tools=[
        list_events,
        create_event,
        edit_event,
        delete_event,
    ],
)