from langchain_openrouter import ChatOpenRouter
from dotenv import load_dotenv
import os

load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
BASE_URL = "https://openrouter.ai/api/v1"

gpt_oss_120b = ChatOpenRouter(
    model="openai/gpt-oss-120b",
    temperature=0.6,
    openrouter_provider={"sort": "latency", "ignore": ["google-vertex", "groq", "nebius/fp4"]},
    reasoning={"effort": "high"}
)

deepseek_v4_pro = ChatOpenRouter(
    model="deepseek/deepseek-v4-pro",
    temperature=0.6,
    openrouter_provider={"sort": "price", "ignore": ["google-vertex"]},
    reasoning={"effort": "high"}
)

gemma4_31b = ChatOpenRouter(
    model="google/gemma-4-31b-it",
    temperature=0.6,
    openrouter_provider={"sort": "latency", "ignore": ["google-vertex"]},
    reasoning={"effort": "none"}
)

qwen37_plus = ChatOpenRouter(
    model="qwen/qwen3.7-plus",
    temperature=0.6,
    openrouter_provider={"order": ["alibaba"], "ignore": ["google-vertex"]},
    reasoning={"effort": "high"}
)

mimov25_pro = ChatOpenRouter(
    model="xiaomi/mimo-v2.5-pro",
    temperature=0.6,
    openrouter_provider={"order": ["xiaomi/fp8"]},
    reasoning={"effort": "high"}
)

laguna_xs_21 = ChatOpenRouter(
    model="poolside/laguna-xs-2.1",
    temperature=0.7,
    openrouter_provider={"sort": "latency"},
    reasoning={"effort": "none"}
)

laguna_s_21 = ChatOpenRouter(
    model="poolside/laguna-s-2.1",
    temperature=0.7,
    openrouter_provider={"sort": "latency"},
    reasoning={"effort": "none"}
)

vlm_vision = ChatOpenRouter(
    model="qwen/qwen3.7-plus",
    temperature=0.6,
    openrouter_provider={"order": ["alibaba"], "ignore": ["google-vertex"]},
    reasoning={"effort": "high"}
)