<div align="center">

# Atlas AI PC Control

Multi-agent AI system for automating Windows PCs via PowerShell, screenshot-driven VLM GUI control, REST API, and local voice.

![Python](https://img.shields.io/badge/Python-3.10--3.12-111827?style=flat-square)
![Platform](https://img.shields.io/badge/Platform-Windows-374151?style=flat-square)
![LangGraph](https://img.shields.io/badge/Agent-LangGraph-1f2937?style=flat-square)

</div>

<!-- Add screenshot or demo GIF here, e.g.:
![Atlas AI PC Control preview](./docs/preview.png)
-->

## Features

- **Hybrid Terminal & GUI Control** — Prioritizes PowerShell command execution for file, process, and system operations, delegating UI tasks to a dedicated Window Agent.
- **Screenshot-Driven Window Agent (VLM)** — Captures the target window, performs keyboard actions, scrolling, and pixel clicks via a vision-language model (`agent/window_agent.py`), and re-captures to verify results across native Windows, Electron/Chromium apps, and games.
- **Multi-Language Input Emulation** — Handles Unicode text input (including Cyrillic, CJK, and symbols) via clipboard injection to avoid keyboard layout and UWP window constraints.
- **REST API & Background Queue** — Exposes a FastAPI server supporting both synchronous task execution and asynchronous background task dispatch.
- **WebSocket Integration Client** — Connects to external orchestrators (such as Data-Sama) to stream task status updates and return asynchronous tool results.
- **Local Voice Interface & Overlay** — Desktop voice mode with Vosk wake-word detection, faster-whisper transcription, Silero TTS / gTTS output, auto-downloaded voice weights, and a custom desktop subtitle overlay.

## Architecture

```text
                       ┌───────────────────────────┐
                       │       User / Client       │
                       └─────────────┬─────────────┘
                                     │
               ┌─────────────────────┴─────────────────────┐
               ▼                                           ▼
 ┌───────────────────────────┐               ┌───────────────────────────┐
 │   REST API & WebSocket    │               │    Local Voice GUI        │
 │  server.py / FastAPI      │               │  main.py (Vosk + Whisper) │
 └─────────────┬─────────────┘               └─────────────┬─────────────┘
               │                                           │
               └─────────────────────┬─────────────────────┘
                                     ▼
                       ┌───────────────────────────┐
                       │     Atlas Main Agent      │
                       │  LangGraph / OpenRouter   │
                       └─────────────┬─────────────┘
                                     │
              ┌──────────────────────┴──────────────────────┐
              ▼                                             ▼
┌───────────────────────────┐                 ┌───────────────────────────┐
│   Terminal (PowerShell)   │                 │   Window Agent (GUI)      │
│   execute_bash_command    │                 │   interact_with_window    │
└───────────────────────────┘                 └─────────────┬─────────────┘
                                                            │
                                                            ▼
                                              ┌───────────────────────────┐
                                              │    VLM + Screenshots      │
                                              │  keyboard / pixel clicks  │
                                              └───────────────────────────┘
```

## Tech Stack

| Layer | Technology |
| :--- | :--- |
| Agent Orchestration | LangGraph, LangChain, OpenRouter |
| GUI Automation | Vision-language model (OpenRouter), OpenCV, Pillow |
| API & Realtime | FastAPI, Uvicorn, WebSockets |
| Voice & Overlay | Vosk, faster-whisper, Silero TTS / gTTS, WebRTC VAD, Pygame |

## Getting Started

### Prerequisites

- Windows 10 or Windows 11
- Python 3.10 – 3.12
- PyTorch (required for local voice mode / Silero TTS)
- An [OpenRouter](https://openrouter.ai/) API key

### Installation

```bash
git clone https://github.com/Oatis123/Atlas-AI-PC-Control.git
cd Atlas-AI-PC-Control

python -m venv venv
venv\Scripts\activate

pip install -r requirements.txt
```

## Configuration

Create a `.env` file in the project root:

| Variable | Required | Default | Description |
| :--- | :---: | :--- | :--- |
| `OPENROUTER_API_KEY` | Yes | — | API key used by `agent/models/openrouter_models.py` to call LLM and VLM models via OpenRouter |

## Usage

### API Server

Start the FastAPI server (listens on `http://127.0.0.1:5050`):

```bash
python server.py
```

| Endpoint | Method | Description |
| :--- | :--- | :--- |
| `/` | `GET` | Health check and available endpoints |
| `/api/execute` | `POST` | Run an agent prompt synchronously or in the background |
| `/tools/run-pc-agent` | `POST` | Async tool execution endpoint for Data-Sama integration |
| `/run` | `POST` | Legacy endpoint for executing a list of commands |

Synchronous request:

```bash
curl -X POST "http://127.0.0.1:5050/api/execute" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Open calculator and multiply 25 by 4"}'
```

Asynchronous background request:

```bash
curl -X POST "http://127.0.0.1:5050/api/execute" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Create folder Test on desktop", "async_mode": true}'
```

### Local Voice Assistant

Start the voice-controlled desktop assistant (Vosk and Silero TTS weights are downloaded automatically on first run via `utils/model_setup.py`):

```bash
python main.py
```

## Project Structure

```text
Atlas-AI-PC-Control/
├── agent/
│   ├── agent.py                 # Main LangGraph agent and tool router
│   ├── window_agent.py          # Screenshot-driven VLM window agent
│   ├── models/                  # OpenRouter model configurations
│   ├── prompts/                 # System prompts for main and window agents
│   └── tools/                   # PowerShell, vision, screen, and web tools
├── assets/
│   ├── fonts/                   # Overlay fonts
│   └── sounds/                  # Voice activation audio cues
├── gui/
│   └── overlay.py               # Desktop voice and subtitle overlay
├── utils/
│   ├── logging_setup.py         # Logging configuration
│   ├── media_utils.py           # Media and playback helpers
│   └── model_setup.py           # Auto-provisioning for Vosk and Silero models
├── datasama_client.py           # WebSocket client for Data-Sama integration
├── main.py                      # Local voice assistant entry point
├── server.py                    # FastAPI server entry point
└── requirements.txt
```

---

<p align="center">made with 💚</p>
