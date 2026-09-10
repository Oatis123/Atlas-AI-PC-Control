import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
from langchain_core.messages import HumanMessage, BaseMessage

from utils.logging_setup import setup_logging
setup_logging()

from agent.agent import request_to_agent_async, request_to_agent_sync
from datasama_client import DataSamaClient

datasama_client = DataSamaClient()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Start WebSocket connection loop to Data-Sama
    logging.info("Starting Data-Sama integration client...")
    await datasama_client.start()
    
    # Warm up OmniParser vision engine in background thread
    try:
        import asyncio
        from agent.vision.omniparser_engine import OmniParserEngine
        logging.info("Warming up OmniParser Vision Engine...")
        asyncio.get_event_loop().run_in_executor(None, OmniParserEngine)
    except Exception as e:
        logging.warning(f"Could not pre-load OmniParser engine: {e}")
        
    yield
    # Shutdown: Stop WebSocket connection
    logging.info("Stopping Data-Sama integration client...")
    await datasama_client.stop()

app = FastAPI(title="AI-PC-Control Server Mode", lifespan=lifespan)


class ToolInvocation(BaseModel):
    tool_name: str | None = "execute_pc_task"
    arguments: dict | None = None


class CommandRequest(BaseModel):
    commands: list[str]


# Sliding session history window (up to 15 user-agent request/response pairs)
MAX_HISTORY_TURNS = 15
SESSION_HISTORY: list[BaseMessage] = []


def get_session_messages(prompt_text: str) -> list[BaseMessage]:
    """Builds full message list combining past session history and the new user prompt."""
    return list(SESSION_HISTORY) + [HumanMessage(content=prompt_text)]


def record_session_turn(prompt_text: str, response_text: str):
    """Saves a completed user prompt and agent response pair into the sliding session memory."""
    global SESSION_HISTORY
    from langchain_core.messages import AIMessage
    SESSION_HISTORY.append(HumanMessage(content=prompt_text))
    SESSION_HISTORY.append(AIMessage(content=response_text))
    
    # Keep only the last N turns (each turn = 1 HumanMessage + 1 AIMessage)
    max_messages = MAX_HISTORY_TURNS * 2
    if len(SESSION_HISTORY) > max_messages:
        SESSION_HISTORY = SESSION_HISTORY[-max_messages:]
    logging.info(f"Updated session history (Current size: {len(SESSION_HISTORY)} messages / {len(SESSION_HISTORY)//2} turns)")


async def _execute_pc_task_background(prompt_text: str):
    """Background task running the LangGraph agent and sending WebSocket updates."""
    try:
        logging.info(f"--- [TASK START] Processing prompt: '{prompt_text}' ---")
        
        # 1. Send single background state update over WS
        await datasama_client.send_background_update(f"Executing task: {prompt_text}")

        # 2. Execute local PC control agent with session memory
        messages = get_session_messages(prompt_text)
        agent_response_messages = await request_to_agent_async(messages)

        final_content = ""
        if agent_response_messages:
            # Extract last meaningful AIMessage text content
            from langchain_core.messages import AIMessage
            for msg in reversed(agent_response_messages):
                if isinstance(msg, AIMessage) and msg.content and msg.content != "Calling tools...":
                    final_content = msg.content
                    break
            if not final_content and agent_response_messages:
                final_content = str(agent_response_messages[-1].content)

        if not final_content:
            final_content = "Task completed (no text response from agent)."

        # Record this turn into session history
        record_session_turn(prompt_text, final_content)

        logging.info(f"--- [TASK FINISHED] Sending tool_result to Data-Sama: '{final_content}' ---")

        # 3. Send final tool_result over WS
        await datasama_client.send_tool_result(
            tool_name="execute_pc_task",
            status="success",
            output=final_content
        )
        logging.info("--- [WS SENT] tool_result successfully sent to Data-Sama! ---")
    except Exception as e:
        logging.error(f"Error executing agent task '{prompt_text}': {e}", exc_info=True)
        await datasama_client.send_tool_result(
            tool_name="execute_pc_task",
            status="error",
            output=f"Error executing task on PC: {str(e)}"
        )


class TaskRequest(BaseModel):
    prompt: str
    async_mode: bool = False


@app.get("/")
async def root():
    """Server status endpoint for Atlas AI-PC-Control."""
    return {
        "name": "Atlas AI-PC-Control API",
        "status": "running",
        "session_history_turns": len(SESSION_HISTORY) // 2,
        "endpoints": {
            "POST /api/execute": "Execute PC task (synchronously or asynchronously)",
            "POST /api/clear-history": "Clear session message history",
            "POST /run": "Direct command list execution (legacy)",
            "POST /tools/run-pc-agent": "Data-Sama integration endpoint"
        }
    }


@app.post("/api/clear-history")
async def clear_history():
    """Clears the session message history."""
    global SESSION_HISTORY
    count = len(SESSION_HISTORY) // 2
    SESSION_HISTORY.clear()
    logging.info("Session history cleared by user API request.")
    return {"status": "success", "message": f"Cleared {count} history turns."}


@app.post("/api/execute")
async def execute_user_task(data: TaskRequest, background_tasks: BackgroundTasks):
    """
    Universal REST API endpoint for clients.
    
    Accepts JSON:
    {
      "prompt": "Open notepad and type Hello",
      "async_mode": false
    }
    """
    if not data.prompt or not data.prompt.strip():
        return {"status": "error", "message": "The 'prompt' field cannot be empty."}

    prompt_text = data.prompt.strip()

    if data.async_mode:
        background_tasks.add_task(_execute_pc_task_background, prompt_text)
        return {
            "status": "queued",
            "message": "Task queued and executing in background.",
            "prompt": prompt_text
        }

    try:
        logging.info(f"--- [API TASK START] Synchronous processing: '{prompt_text}' ---")
        messages = get_session_messages(prompt_text)
        agent_response_messages = await request_to_agent_async(messages)
        
        final_content = ""
        if agent_response_messages:
            from langchain_core.messages import AIMessage
            for msg in reversed(agent_response_messages):
                if isinstance(msg, AIMessage) and msg.content and msg.content != "Calling tools...":
                    final_content = msg.content
                    break
            if not final_content and agent_response_messages:
                final_content = str(agent_response_messages[-1].content)

        if not final_content:
            final_content = "Task executed successfully."

        # Record this turn into session history
        record_session_turn(prompt_text, final_content)

        return {
            "status": "success",
            "prompt": prompt_text,
            "response": final_content
        }
    except Exception as e:
        logging.error(f"Error executing API task '{prompt_text}': {e}", exc_info=True)
        return {
            "status": "error",
            "prompt": prompt_text,
            "error": str(e)
        }


@app.post("/tools/run-pc-agent")
async def run_pc_agent(payload: dict, background_tasks: BackgroundTasks):
    """
    Data-Sama async tool execution endpoint.
    Expects payload format from Data-Sama:
    {"tool_name": "execute_pc_task", "arguments": {"prompt": "..."}}
    """
    arguments = payload.get("arguments", {})
    if not isinstance(arguments, dict):
        arguments = {}

    prompt_text = arguments.get("prompt") or payload.get("prompt") or "Run system health check"

    # Dispatch background execution
    background_tasks.add_task(_execute_pc_task_background, prompt_text)

    # Immediately respond with 200 OK
    return "Task accepted for processing by PC agent."


@app.post("/run")
async def run_agent_command(data: CommandRequest):
    """Legacy endpoint for direct asynchronous command execution."""
    messages = [HumanMessage(content=c) for c in data.commands]
    agent_response_messages = await request_to_agent_async(messages)
    final_content = ""
    if agent_response_messages:
        final_content = str(agent_response_messages[-1].content)
    return {"response": final_content}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=5050)