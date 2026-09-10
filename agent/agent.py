import time
from langchain_core.messages import HumanMessage, SystemMessage, BaseMessage, ToolMessage, AIMessage
from langgraph.graph import StateGraph, END
from agent.prompts.main_system_prompt import prompt
from agent.models.openrouter_models import *
from typing import TypedDict, Annotated
import operator
import logging
from typing import List
from agent.tools.pc_control_tools import *
from agent.tools.web_tools import search_web
from agent.tools.useful_tools import waiting
from agent.tools.screen_tools import get_screenshot_tool
from agent.window_interaction_agent import interact_with_window
import langchain
import json

tools = [
         find_application_name, 
         start_application, 
         get_open_windows,
         execute_bash_command, 
         waiting, 
        #  get_screenshot_tool, 
         search_web,
         interact_with_window]

tools_by_name = {tool.name: tool for tool in tools}
model_with_tools = laguna_s_21.bind_tools(tools)


# Logging is configured once by utils.logging_setup.setup_logging() at process
# start (see main.py / server.py). Fall back to a basic console config only when
# this module is imported standalone.
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )


class AgentState(TypedDict):
    messages: list[BaseMessage]
    ids_to_hide: Annotated[list[str], operator.add]
    screenshot_ids_to_hide: Annotated[list[str], operator.add]
    last_search_web_id: str | None


def _short(text, limit: int = 800) -> str:
    """One-line, length-capped preview of a value for log output."""
    s = str(text).replace("\n", " ⏎ ")
    return s if len(s) <= limit else f"{s[:limit]}… (+{len(s) - limit} символов)"


def _fmt_msg(m) -> str:
    """Compact one-line render of a single message (no system prompt, no reasoning dump)."""
    kind = type(m).__name__
    tool_calls = getattr(m, "tool_calls", None) or []
    if tool_calls:
        calls = "; ".join(f"{tc['name']}({tc.get('args', {})})" for tc in tool_calls)
        return f"{kind} → {calls}"
    if isinstance(m, ToolMessage):
        tag = getattr(m, "name", "") or getattr(m, "tool_call_id", "")
        return f"ToolMessage[{tag}]: {_short(m.content)}"
    return f"{kind}: {_short(getattr(m, 'content', ''))}"


def _summarize_chunk(chunk) -> str:
    """Log only the last message produced by each node this step, not the whole history."""
    parts = []
    for node, payload in chunk.items():
        msgs = payload.get("messages") if isinstance(payload, dict) else None
        parts.append(f"[{node}] {_fmt_msg(msgs[-1])}" if msgs else f"[{node}]")
    return " | ".join(parts)


def _detect_stuck_loop(messages, threshold=3):
    """Возвращает сигнатуру повторяющегося вызова, если последние `threshold`
    вызовов инструментов идентичны и каждый вернул ошибку, иначе None."""
    sigs = []
    i = len(messages) - 1
    while i >= 0 and len(sigs) < threshold:
        msg = messages[i]
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            errored = False
            for j in range(i + 1, len(messages)):
                nxt = messages[j]
                if isinstance(nxt, AIMessage):
                    break
                if isinstance(nxt, ToolMessage) and (
                    "шибка" in str(nxt.content) or "rror" in str(nxt.content)
                ):
                    errored = True
            sig = json.dumps(
                [(tc["name"], tc.get("args", {})) for tc in msg.tool_calls],
                sort_keys=True, ensure_ascii=False,
            )
            sigs.append((sig, errored))
        i -= 1

    if len(sigs) < threshold:
        return None
    first = sigs[0][0]
    if all(sig == first and errored for sig, errored in sigs):
        return first
    return None


async def agent_node(state):
    logging.info("--- Вход в agent_node ---")

    stuck_signature = _detect_stuck_loop(state["messages"])
    if stuck_signature:
        logging.warning(
            f"⚠️ [LOOP GUARD] Инструмент повторно вызывается с теми же аргументами и "
            f"возвращает ошибку: {stuck_signature}. Прерываю цикл."
        )
        return {"messages": state["messages"] + [AIMessage(content=(
            "Failed. Не удалось выполнить задачу: инструмент несколько раз подряд "
            "вызван с одними и теми же неверными аргументами и вернул ошибку."
        ))]}

    # 1. Заглушка для пустого контента (чтобы Xiaomi не крашился)
    for msg in state["messages"]:
        if getattr(msg, "type", "") == "ai" and not getattr(msg, "content", "") and getattr(msg, "tool_calls", None):
            msg.content = "Вызываю инструменты..."

    logging.info(f"Количество сообщений в истории: {len(state['messages'])}")
    model_start_time = time.time()
    
    response = None
    for attempt in range(3):
        try:
            response = await model_with_tools.ainvoke(state["messages"])
            break
        except Exception as e:
            if "429" in str(e) or "TooManyRequests" in str(e) or "rate" in str(e).lower():
                logging.warning(f"⚠️ [Rate Limit 429] OpenRouter лимит запросов. Ожидание 3 сек (попытка {attempt+1}/3)...")
                await asyncio.sleep(3)
            else:
                raise e

    if response is None:
        raise RuntimeError("Ошибка OpenRouter: превышен лимит запросов (429). Попробуйте позже.")

    elapsed = time.time() - model_start_time
    logging.info(f"⏱️ [LLM TIME] Ответ модели получен за {elapsed:.4f} сек. Содержит tool_calls: {bool(getattr(response, 'tool_calls', None))}")
    
    # 2. КРИТИЧЕСКИ ВАЖНО: склеиваем старую историю с новым ответом!
    return {"messages": state["messages"] + [response]}


async def tool_node(state: AgentState) -> dict:
    logging.info("--- Вход в tool_node ---")
    tools_to_hide = ["scrape_application", "get_installed_software", "get_screenshot_tool"]
    last_message = state["messages"][-1]
    
    is_search_web_called_now = any(tc["name"] == "search_web" for tc in last_message.tool_calls)
    previous_search_web_id = state.get("last_search_web_id")
    is_heavy_tool_called = any(tc["name"] in tools_to_hide for tc in last_message.tool_calls)
    should_clean_history = is_heavy_tool_called or (is_search_web_called_now and previous_search_web_id)
    
    previous_ids_to_hide = state.get("ids_to_hide", [])
    screenshot_ids_to_hide = state.get("screenshot_ids_to_hide", [])
    
    cleaned_messages = []
    if should_clean_history:
        logging.info("Выполняется очистка контекста от тяжелых результатов...")
        skip_next = False
        for i, msg in enumerate(state["messages"]):
            if skip_next:
                skip_next = False
                continue
            
            if isinstance(msg, ToolMessage):
                should_hide = msg.tool_call_id in previous_ids_to_hide or \
                             (is_search_web_called_now and msg.tool_call_id == previous_search_web_id)
                if should_hide:
                    is_error = "Ошибка" in msg.content or "ошибка" in msg.content
                    new_content = msg.content if is_error else "Результат выполнения предыдущего инструмента скрыт для экономии контекста."
                    cleaned_messages.append(ToolMessage(content=new_content, tool_call_id=msg.tool_call_id))
                    
                    if msg.tool_call_id in screenshot_ids_to_hide and i + 1 < len(state["messages"]):
                        next_msg = state["messages"][i + 1]
                        if isinstance(next_msg, HumanMessage) and isinstance(next_msg.content, list):
                            if any(isinstance(c, dict) and c.get("type") == "image_url" for c in next_msg.content):
                                skip_next = True
                else:
                    cleaned_messages.append(msg)
            else:
                 cleaned_messages.append(msg)
    else:
        cleaned_messages = state["messages"]

    new_tool_results = []
    current_ids_to_hide = list(previous_ids_to_hide)
    current_screenshot_ids = list(screenshot_ids_to_hide)
    current_search_web_id = None

    logging.info(f"Вызов инструментов: {[tc['name'] for tc in last_message.tool_calls]}")
    for tool_call in last_message.tool_calls:
        tool = tools_by_name[tool_call["name"]]
        tool_start_time = time.time()
        logging.info(f"Выполнение инструмента: {tool_call['name']} с аргументами: {tool_call['args']}")
        
        if tool_call["name"] == "search_web":
            current_search_web_id = tool_call["id"]

        try:
            if tool_call["name"] == "get_screenshot_tool":
                screenshot = await tool.ainvoke(tool_call["args"])
                elapsed = time.time() - tool_start_time
                logging.info(f"⏱️ [TOOL TIME] Инструмент '{tool_call['name']}' выполнен за {elapsed:.4f} сек.")
                mime_type = screenshot["mime_type"]
                screenshot_data = screenshot["screenshot_data"]
                
                human_message_content = [
                    {"type": "text", "text": "Вот запрошенный скриншот для анализа."},
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{screenshot_data}"}}
                ]
                new_tool_results.append(HumanMessage(content=human_message_content))
                
                tool_confirmation = json.dumps({"status": "success", "message": "Image provided in a new message."})
                new_tool_results.append(ToolMessage(content=tool_confirmation, tool_call_id=tool_call["id"]))
                
                current_screenshot_ids.append(tool_call["id"])
            else:
                observation = await tool.ainvoke(tool_call["args"])
                elapsed = time.time() - tool_start_time
                logging.info(f"⏱️ [TOOL TIME] Инструмент '{tool_call['name']}' выполнен за {elapsed:.4f} сек.")
                logging.info(f"Результат '{tool_call['name']}': {_short(observation)}")
                new_tool_results.append(ToolMessage(content=str(observation), tool_call_id=tool_call["id"]))
        except Exception as tool_err:
            logging.error(f"Ошибка вызова инструмента {tool_call['name']}: {tool_err}")
            new_tool_results.append(ToolMessage(content=f"Ошибка вызова инструмента: {tool_err}", tool_call_id=tool_call["id"]))
        
        if tool_call["name"] in tools_to_hide:
            current_ids_to_hide.append(tool_call["id"])
    
    logging.info("Все инструменты выполнены.")
    return {
        "messages": cleaned_messages + new_tool_results,
        "ids_to_hide": current_ids_to_hide,
        "screenshot_ids_to_hide": current_screenshot_ids,
        "last_search_web_id": current_search_web_id,
    }




def should_continue(state):
    last_message = state["messages"][-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        logging.info("Цикл продолжается: агент запросил вызов инструментов.")
        return "continue"
    else:
        logging.info("Цикл завершен: агент вернул финальный ответ.")
        return "end"


workflow = StateGraph(AgentState)

workflow.add_node("agent", agent_node)
workflow.add_node("action", tool_node)

workflow.set_entry_point("agent")

workflow.add_conditional_edges(
    "agent",
    should_continue,
    {
        "continue": "action",
        "end": END,
    },
)

workflow.add_edge("action", "agent")

graph = workflow.compile()

config = {"recursion_limit": 200}

#for chunk in graph.stream(input_data, stream_mode="values", config=config):
#    print(chunk, end="", flush=True)

async def request_to_agent_async(req: List):
    logging.info(f"Получен новый запрос: {req}")
    
    try:
        input_data = {"messages": [SystemMessage(prompt)] + req}
        logging.info("Данные для графа подготовлены.")
        logging.info("Вызов графа в асинхронном потоковом режиме...")
        
        final_answer = None
        last_chunk = None 

        async for chunk in graph.astream(input_data, config={"recursion_limit": 200}):
            if "__end__" not in chunk:
                logging.info(f"Шаг графа: {_summarize_chunk(chunk)}")
            
            last_chunk = chunk

            if "__end__" in chunk:
                final_answer = chunk["__end__"]

        logging.info("Граф успешно отработал.")

        if final_answer:
            answer = final_answer.get("messages")
            logging.info(f"Ответ извлечён из финального узла: {_fmt_msg(answer[-1]) if answer else None}")
            return answer
        elif last_chunk and "agent" in last_chunk:
            agent_messages = last_chunk["agent"].get("messages", [])
            if agent_messages and isinstance(agent_messages[-1], AIMessage):
                answer = [agent_messages[-1]]
                logging.info(f"Прямой текстовый ответ агента: {_fmt_msg(answer[-1])}")
                return answer
        else:
            logging.warning("Граф завершил работу, но не вернул никакого ответа.")
            return None

    except Exception as e:
        logging.error(f"Произошла ошибка при обработке запроса: {req}", exc_info=True)
        raise e
    
def request_to_agent_sync(req: List):
    logging.info(f"Получен новый запрос (sync): {req}")
    import asyncio
    
    req_messages = [HumanMessage(content=c) if isinstance(c, str) else c for c in req]
    input_data = {"messages": [SystemMessage(prompt)] + req_messages}
    
    response = asyncio.run(graph.ainvoke(input=input_data, config=config))
    
    logging.info(f"Ответ от агента: {response}")
    
    return response["messages"][-1].content