# jarvis/agent.py (LA VERSIÓN CORRECTA PARA EL ADK)
from google.adk.agents import Agent

root_agent = Agent(
    # Este es el modelo que el ADK recomienda para este caso de uso
    model="gemini-2.0-flash", 
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