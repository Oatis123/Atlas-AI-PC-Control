"""Screenshot-driven window agent (single VLM model).

The orchestrator delegates here via `interact_with_window_visual` for windows the
structured OmniParser agent can't handle — browsers, Electron/Chromium apps
(Spotify, Discord, VS Code), games, canvas UIs.

The agent loops: capture_window -> look at the image -> one action -> repeat.
"""

import asyncio
import json
import logging
import time

from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.graph import StateGraph, END
from typing import TypedDict

from agent.models.openrouter_models import vlm_vision
from agent.tools.vision_control_tools import capture_window, click_window, scroll_window
from agent.tools.pc_control_tools import simulate_keyboard
from agent.tools.useful_tools import waiting
from agent.prompts.visual_window_prompt import visual_window_agent_prompt as prompt

# Logging is configured once by utils.logging_setup.setup_logging() at process
# start (see main.py / server.py). Fall back to a basic console config only when
# this module is imported standalone.
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

tools = [capture_window, click_window, scroll_window, simulate_keyboard, waiting]
tools_by_name = {t.name: t for t in tools}
model_with_tools = vlm_vision.bind_tools(tools)


class AgentState(TypedDict):
    messages: list[BaseMessage]


def _short(text, limit: int = 800) -> str:
    s = str(text).replace("\n", " ⏎ ")
    return s if len(s) <= limit else f"{s[:limit]}… (+{len(s) - limit} символов)"


def _is_image_msg(m) -> bool:
    return (
        isinstance(m, HumanMessage)
        and isinstance(m.content, list)
        and any(isinstance(c, dict) and c.get("type") == "image_url" for c in m.content)
    )


async def agent_node(state: AgentState) -> dict:
    logging.info("--- [Visual Agent] Вход в agent_node ---")
    for msg in state["messages"]:
        if getattr(msg, "type", "") == "ai" and not getattr(msg, "content", "") and getattr(msg, "tool_calls", None):
            msg.content = "Вызываю инструменты..."

    logging.info(f"[Visual Agent] Сообщений в истории: {len(state['messages'])}")
    t0 = time.time()

    response = None
    for attempt in range(3):
        try:
            response = await model_with_tools.ainvoke(state["messages"])
            break
        except Exception as e:
            if "429" in str(e) or "rate" in str(e).lower():
                logging.warning(f"⚠️ [Visual Agent 429] Лимит запросов. Ожидание 3 сек ({attempt+1}/3)...")
                await asyncio.sleep(3)
            else:
                raise
    if response is None:
        raise RuntimeError("Ошибка OpenRouter: превышен лимит запросов (429).")

    logging.info(
        f"⏱️ [Visual Agent LLM TIME] Ответ за {time.time() - t0:.2f}с. tool_calls: "
        f"{bool(getattr(response, 'tool_calls', None))}"
    )
    reasoning = (getattr(response, "additional_kwargs", {}) or {}).get("reasoning_content")
    if reasoning:
        logging.info(f"[Visual Agent] Рассуждение: {_short(reasoning)}")
    if getattr(response, "content", ""):
        logging.info(f"[Visual Agent] Ответ: {_short(response.content)}")
    for tc in getattr(response, "tool_calls", None) or []:
        logging.info(f"[Visual Agent] → план: {tc['name']}({tc.get('args', {})})")

    return {"messages": state["messages"] + [response]}


async def tool_node(state: AgentState) -> dict:
    logging.info("--- [Visual Agent] Вход в tool_node ---")
    last = state["messages"][-1]
    new_capture = any(tc["name"] == "capture_window" for tc in last.tool_calls)

    history = list(state["messages"])
    if new_capture:
        # keep only the freshest screenshot in context — old images just cost tokens
        for i, m in enumerate(history):
            if _is_image_msg(m):
                history[i] = HumanMessage(content="[прошлый скриншот скрыт для экономии контекста]")

    tool_results = []   # ToolMessages — must directly follow the AIMessage
    image_msgs = []      # the screenshot(s) — appended after all ToolMessages
    for tc in last.tool_calls:
        tool = tools_by_name.get(tc["name"])
        t0 = time.time()
        logging.info(f"[Visual Agent] Выполнение: {tc['name']}({tc.get('args', {})})")
        if tool is None:
            tool_results.append(ToolMessage(content=f"Ошибка: неизвестный инструмент '{tc['name']}'.", tool_call_id=tc["id"]))
            continue
        try:
            obs = await tool.ainvoke(tc["args"])
        except Exception as e:
            logging.error(f"[Visual Agent] Ошибка инструмента {tc['name']}: {e}")
            tool_results.append(ToolMessage(content=f"Ошибка вызова инструмента: {e}", tool_call_id=tc["id"]))
            continue
        dt = time.time() - t0

        if tc["name"] == "capture_window" and isinstance(obs, dict) and "screenshot_data" in obs:
            w, h = obs.get("width"), obs.get("height")
            tool_results.append(ToolMessage(
                content=f"Скриншот получен ({w}x{h} px). Координаты для кликов бери прямо с него.",
                tool_call_id=tc["id"],
            ))
            image_msgs.append(HumanMessage(content=[
                {"type": "text", "text": f"Скриншот окна ({w}x{h} px):"},
                {"type": "image_url", "image_url": {"url": f"data:{obs['mime_type']};base64,{obs['screenshot_data']}"}},
            ]))
            logging.info(f"[Visual Agent] capture_window за {dt:.2f}с → {w}x{h}")
        else:
            if isinstance(obs, dict) and "error" in obs:
                obs = f"Ошибка: {obs['error']}"
            logging.info(f"[Visual Agent] {tc['name']} за {dt:.2f}с → {_short(obs)}")
            tool_results.append(ToolMessage(content=str(obs), tool_call_id=tc["id"]))

    return {"messages": history + tool_results + image_msgs}


def should_continue(state: AgentState) -> str:
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None):
        logging.info("[Visual Agent] Цикл продолжается.")
        return "continue"
    logging.info("[Visual Agent] Цикл завершён.")
    return "end"


workflow = StateGraph(AgentState)
workflow.add_node("agent", agent_node)
workflow.add_node("action", tool_node)
workflow.set_entry_point("agent")
workflow.add_conditional_edges("agent", should_continue, {"continue": "action", "end": END})
workflow.add_edge("action", "agent")
graph = workflow.compile()

config = {"recursion_limit": 60}


async def request_to_visual_agent(req: str) -> str:
    logging.info(f"[Visual Agent] Запрос к агенту: {req}")
    try:
        input_data = {"messages": [SystemMessage(prompt), HumanMessage(content=req)]}
        response = await graph.ainvoke(input=input_data, config=config)
        final_answer = response["messages"][-1].content
        logging.info(f"[Visual Agent] Финальный ответ: {final_answer}")
        return final_answer
    except Exception as e:
        logging.error(f"[Visual Agent] Ошибка работы агента: {e}", exc_info=True)
        return f"Ошибка работы визуального агента: {e}"


@tool
async def interact_with_window_visual(win_name: str = "", task: str = "") -> str:
    """
Delegates a task to the VISUAL window agent, which drives a window purely from
screenshots (no accessibility/element tree). Use this INSTEAD of `interact_with_window`
for non-standard UIs: web browsers, Electron/Chromium apps (Spotify, Discord, Slack,
VS Code), games, media players, and anything where `interact_with_window` failed to
find the elements it needed.
Arguments:
- win_name: The exact window title from your window tools (e.g. "Spotify Premium").
- task: What to achieve, described as a GOAL (e.g. "Play the playlist named 'Python'").
Returns the result of the interaction or an error message.
"""
    if not win_name or not task:
        return "Ошибка: обязательны оба аргумента: win_name (название окна) и task (описание задачи)."

    logging.info(f"[Tool: interact_with_window_visual] Окно '{win_name}', задача: '{task}'")
    try:
        req = json.dumps({"window_name": win_name, "task": task}, ensure_ascii=False)
        return await request_to_visual_agent(req)
    except Exception as e:
        logging.error(f"[Tool: interact_with_window_visual] Ошибка: {e}", exc_info=True)
        return f"Ошибка при запросе к визуальному агенту: {e}"
