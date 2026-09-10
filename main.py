import vosk
import pyaudio
import json
import os
import torch
import pygame
import threading
import queue
import re
import numpy as np
import webrtcvad
import time
import asyncio
from collections import deque
from faster_whisper import WhisperModel
from openai import BadRequestError
from gtts import gTTS

from langchain_core.messages import HumanMessage

from utils.logging_setup import setup_logging
setup_logging()

from agent.agent import request_to_agent_async
from gui.overlay import SubtitleOverlay
from utils.media_utils import *
from utils.model_setup import ensure_vosk_model, ensure_silero_model

ASR_ENGINE = 'whisper'
TTS_ENGINE = 'silero'

WAKE_WORD = "атлас"
SOUND_MINUS_WORD = "тише"
SOUND_PLUS_WORD = "громче"
PAUSE_WORD = "пауза"
PLAY_WORD = "продолжи"
NEXT_WORD = "дальше"
BACK_WORD = "назад"
UP_WORD = "вверх"
DOWN_WORD = "вниз"

MODEL_FOLDER_NAME = "vosk-model-small-ru-0.22"
WHISPER_MODEL_NAME = "small"
SOUNDS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "sounds")
WAITING_SOUND = os.path.join(SOUNDS_DIR, "4115442.mp3")
INPUT_SAMPLE_RATE = 16000
INPUT_CHANNELS = 1
INPUT_FORMAT = pyaudio.paInt16
VAD_AGGRESSIVENESS = 3
VAD_FRAME_MS = 30
VAD_CHUNK_SIZE = int(INPUT_SAMPLE_RATE * (VAD_FRAME_MS / 1000.0))
VAD_SILENCE_TIMEOUT_MS = 1500
VAD_PRE_BUFFER_MS = 300
MAX_RETRIES = 20
RETRY_DELAY_SECONDS = 2
FOLLOW_UP_TIMEOUT_SECONDS = 5

chat_history = []
gui_queue = queue.Queue()
stop_event = threading.Event()

pygame.mixer.init()
activate_sound = pygame.mixer.Sound(WAITING_SOUND)

try:
    MODEL_FOLDER_NAME = ensure_vosk_model()
except Exception as e:
    print(f"Ошибка: не удалось подготовить модель Vosk: {e}")
    exit()
vosk_model = vosk.Model(MODEL_FOLDER_NAME)

silero_model = None
silero_sample_rate = 48000
silero_speaker = 'aidar'

if TTS_ENGINE == 'gtts':
    print("Движок TTS (gTTS) готов к работе.")
elif TTS_ENGINE == 'silero':
    print("Загрузка модели Silero TTS...")
    device = torch.device('cuda')
    try:
        local_file = ensure_silero_model()
        silero_model = torch.package.PackageImporter(local_file).load_pickle("tts_models", "model")
        silero_model.to(device)
        print("Модель Silero TTS успешно загружена.")
    except Exception as e:
        print(f"Ошибка при загрузке Silero TTS: {e}")
        exit()
else:
    print(f"Ошибка: Неизвестный движок TTS '{TTS_ENGINE}'.")
    exit()

whisper_model = None
if ASR_ENGINE == 'whisper':
    print(f"Загрузка модели faster-whisper ({WHISPER_MODEL_NAME})...")
    try:
        device_type = "cuda" if torch.cuda.is_available() else "cpu"
        compute_type = "float16" if torch.cuda.is_available() else "int8"
        whisper_model = WhisperModel(WHISPER_MODEL_NAME, device=device_type, compute_type=compute_type)
        print("Модель faster-whisper успешно загружена.")
    except Exception as e:
        print(f"Ошибка при загрузке модели faster-whisper: {e}")
        exit()

pa = pyaudio.PyAudio()
stream = pa.open(
    format=INPUT_FORMAT,
    channels=INPUT_CHANNELS,
    rate=INPUT_SAMPLE_RATE,
    input=True,
    frames_per_buffer=VAD_CHUNK_SIZE
)

def sentence_chunks(text):
    pat = re.compile(r'[^\.!\?…]+[\.!?…]+(?:["»)]?)(?:\s*)', re.DOTALL)
    pos = 0
    for m in pat.finditer(text):
        yield m.group(0)
        pos = m.end()
    if pos < len(text):
        yield text[pos:]

def speak_gtts(text):
    try:
        tts = gTTS(text=text, lang='ru')
        filename = "temp_speech.mp3"
        tts.save(filename)
        tts_temp = pygame.mixer.Sound(filename)
        tts_temp_lenght = tts_temp.get_length()
        tts_temp.play()
        pygame.time.wait(int(tts_temp_lenght * 1000))
        os.remove(filename)
    except Exception as e:
        print(f"Ошибка при синтезе речи с помощью gTTS: {e}")

def _sanitize_for_tts(text):
    # экзотические пробелы/дефисы и markdown ломают Silero (ru v4) — нормализуем
    for bad, good in ((" ", " "), (" ", " "), (" ", " "),
                      ("—", " - "), ("–", "-"), ("…", "...")):
        text = text.replace(bad, good)
    text = re.sub(r"[*_`#>]+", "", text)
    return re.sub(r"\s+", " ", text).strip()

def speak_silero(text):
    text = _sanitize_for_tts(text)
    try:
        out = pa.open(format=pyaudio.paInt16, channels=1, rate=silero_sample_rate, output=True)
        spoke_any = False
        for sent in sentence_chunks(text):
            if not sent.strip():
                continue
            try:
                audio = silero_model.apply_tts(text=sent,
                                               speaker=silero_speaker,
                                               sample_rate=silero_sample_rate)
            except Exception as chunk_err:
                print(f"Silero пропустил фрагмент ({chunk_err!r}): {sent!r}")
                continue
            audio_np = audio.numpy() * 32767
            out.write(audio_np.astype(np.int16).tobytes())
            spoke_any = True
        out.stop_stream()
        out.close()
        if not spoke_any:
            raise RuntimeError("не удалось синтезировать ни одного фрагмента")
    except Exception as e:
        print(f"Ошибка при синтезе Silero: {e!r} — переключаюсь на gTTS")
        speak_gtts(text)

def listen_with_vad_whisper(audio_stream, model, activation_timeout=None):
    if activation_timeout:
        print(f"Ожидание команды ({activation_timeout} сек)...")
    else:
        print("Слушаю команду (faster-whisper)...")

    vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
    frames_per_second = int(INPUT_SAMPLE_RATE / VAD_CHUNK_SIZE)
    silence_frames_needed = int(frames_per_second * (VAD_SILENCE_TIMEOUT_MS / 1000.0))
    pre_buffer_size = int(frames_per_second * (VAD_PRE_BUFFER_MS / 1000.0))
    pre_buffer = deque(maxlen=pre_buffer_size)
    speech_frames = []
    is_speaking = False
    silent_frames_count = 0
    start_time = time.time()

    # Vosk крутится параллельно Whisper только ради живого превью надиктовки в GUI
    live_rec = vosk.KaldiRecognizer(vosk_model, INPUT_SAMPLE_RATE)
    live_parts = []
    last_preview = None

    def _push_preview(chunk):
        nonlocal last_preview
        try:
            if live_rec.AcceptWaveform(chunk):
                seg = json.loads(live_rec.Result()).get("text", "").strip()
                if seg:
                    live_parts.append(seg)
                shown = " ".join(live_parts)
            else:
                part = json.loads(live_rec.PartialResult()).get("partial", "").strip()
                shown = (" ".join(live_parts) + " " + part).strip()
            if shown and shown != last_preview:
                last_preview = shown
                gui_queue.put({'type': 'user_speech', 'text': shown})
        except Exception:
            pass

    audio_stream.stop_stream()
    audio_stream.start_stream()

    while not stop_event.is_set():
        try:
            if not is_speaking and activation_timeout and (time.time() - start_time > activation_timeout):
                print("Таймаут ожидания речи.")
                return "время вышло"

            chunk = audio_stream.read(VAD_CHUNK_SIZE, exception_on_overflow=False)
            if len(chunk) < VAD_CHUNK_SIZE * 2: continue

            is_speech = vad.is_speech(chunk, INPUT_SAMPLE_RATE)

            if is_speech:
                if not is_speaking:
                    print("Обнаружена речь...")
                    gui_queue.put({'type': 'phase', 'name': 'listening'})
                    is_speaking = True
                    speech_frames.extend(list(pre_buffer))
                speech_frames.append(chunk)
                _push_preview(chunk)
                silent_frames_count = 0
            else:
                if not is_speaking:
                    pre_buffer.append(chunk)
                else:
                    speech_frames.append(chunk)
                    _push_preview(chunk)
                    silent_frames_count += 1
                    if silent_frames_count > silence_frames_needed:
                        print("Конец фразы (таймаут по тишине).")
                        break
        except IOError as e:
            print(f"Ошибка чтения потока: {e}")
            break

    if stop_event.is_set(): return "ошибка"

    if not speech_frames or not is_speaking:
        return "время вышло"

    print("Обработка запроса моделью faster-whisper...")
    audio_data = b''.join(speech_frames)
    audio_np = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0

    segments, _ = model.transcribe(audio_np, language="ru")
    command = "".join([segment.text for segment in segments]).strip()

    if command:
        gui_queue.put({'type': 'user_speech', 'text': command})

    return command if command else "время вышло"

def listen_with_vosk(audio_stream, recognizer):
    print("Слушаю команду (Vosk)...")
    recognizer.Reset()
    while not stop_event.is_set():
        try:
            data = audio_stream.read(4096, exception_on_overflow=False)
            if recognizer.AcceptWaveform(data):
                result_json = recognizer.FinalResult()
                result_dict = json.loads(result_json)
                command = result_dict.get("text", "")
                if command:
                    print("Команда распознана.")
                    return command.strip()
        except IOError as e:
            print(f"Ошибка чтения потока: {e}")
            break
    return "время вышло"

def listen_for_command(audio_stream, play_sound=True, activation_timeout=None):
    if play_sound:
        activate_sound.play()
    if ASR_ENGINE == 'whisper':
        return listen_with_vad_whisper(audio_stream, whisper_model, activation_timeout=activation_timeout)
    elif ASR_ENGINE == 'vosk':
        vosk_command_recognizer = vosk.KaldiRecognizer(vosk_model, INPUT_SAMPLE_RATE)
        return listen_with_vosk(audio_stream, vosk_command_recognizer)
    else:
        print(f"Ошибка: Неизвестный движок распознавания '{ASR_ENGINE}'")
        return "ошибка"

def wait_for_wake_word(audio_stream):
    recognizer = vosk.KaldiRecognizer(vosk_model, INPUT_SAMPLE_RATE)
    recognizer.SetWords(True)
    
    print(f"\nОжидание команд ({WAKE_WORD}, {SOUND_PLUS_WORD}, {SOUND_MINUS_WORD}, {PAUSE_WORD}, {PLAY_WORD})...")

    while not stop_event.is_set():
        try:
            data = audio_stream.read(4096, exception_on_overflow=False)
            if recognizer.AcceptWaveform(data):
                result_json = recognizer.Result()
                result_dict = json.loads(result_json)
                text = result_dict.get("text", "")

                if SOUND_MINUS_WORD in text:
                    print(f"Быстрая команда: '{SOUND_MINUS_WORD}'. Уменьшаю громкость.")
                    sound_minus()
                    continue

                if SOUND_PLUS_WORD in text:
                    print(f"Быстрая команда: '{SOUND_PLUS_WORD}'. Увеличиваю громкость.")
                    sound_plus()
                    continue

                if PAUSE_WORD in text:
                    print(f"Быстрая команда: '{PAUSE_WORD}'. Ставлю на паузу.")
                    play_pause()
                    continue

                if PLAY_WORD in text:
                    print(f"Быстрая команда: '{PLAY_WORD}'. Продолжаю воспроизведение.")
                    play_pause()
                    continue

                if NEXT_WORD in text:
                    print(f"Быстрая команда: '{NEXT_WORD}'. Следующий медия.")
                    next_media()
                    continue
                        
                if BACK_WORD in text:
                    print(f"Быстрая команда: '{BACK_WORD}'. Предыдущая медия.")
                    back_media()
                    continue

                if UP_WORD in text:
                    print(f"Быстрая команда: '{UP_WORD}'. Вверх.")
                    up()

                if DOWN_WORD in text:
                    print(f"Быстрая команда: '{UP_WORD}'. Вниз")
                    down()

                if WAKE_WORD in text:
                    print(f"▶️ Кодовое слово '{WAKE_WORD}' обнаружено!")
                    command_part = text.split(WAKE_WORD, 1)[-1].strip()
                    return command_part

        except IOError as e:
            print(f"Ошибка чтения потока в wait_for_wake_word: {e}")
            break
            
    return None

async def voice_assistant_logic():
    global chat_history
    stream.start_stream()
    print(f"\n✅ Система активирована. Движок ASR: {ASR_ENGINE.upper()}. Движок TTS: {TTS_ENGINE.upper()}.")
    try:
        while not stop_event.is_set():
            gui_queue.put({'type': 'phase', 'name': 'idle', 'reset': True})
            command = await asyncio.to_thread(wait_for_wake_word, stream)
            if stop_event.is_set(): break

            if not command:
                gui_queue.put({'type': 'phase', 'name': 'listening', 'reset': True})
                command = await asyncio.to_thread(listen_for_command, stream)
            else:
                activate_sound.play()
                gui_queue.put({'type': 'user_speech', 'text': command})

            while command and "время вышло" not in command and "ошибка" not in command:
                if stop_event.is_set(): break

                print(f"Выполнение запроса: '{command}'")
                chat_history.append(HumanMessage(content=command))
                gui_queue.put({'type': 'phase', 'name': 'working'})
                response_history = None
                for attempt in range(MAX_RETRIES):
                    if stop_event.is_set(): break
                    try:
                        response_history = await request_to_agent_async(chat_history)
                        break
                    except BadRequestError as e:
                        print(f"Ошибка (попытка {attempt + 1}/{MAX_RETRIES}): {e}")
                        if attempt < MAX_RETRIES - 1:
                            await asyncio.sleep(RETRY_DELAY_SECONDS)
                        else:
                            print("Не удалось получить ответ от LLM.")
                            response_history = None
                    except Exception as e:
                        print(f"Непредвиденная ошибка: {e}")
                        response_history = None
                        break
                
                if stop_event.is_set(): break

                if response_history:
                    # выкидываем chain-of-thought из истории — он раздувает контекст
                    for m in response_history:
                        for k in ("reasoning_content", "reasoning_details", "reasoning"):
                            getattr(m, "additional_kwargs", {}).pop(k, None)
                    chat_history = response_history
                    response_text = response_history[-1].content
                    if isinstance(response_text, list):
                        response_text = response_text[0]["text"]
                else:
                    response_text = "Произошла ошибка при обработке запроса."

                if response_text:
                    gui_queue.put({'type': 'agent_response_chunk', 'text': response_text})
                    print(f"Ответ агента: {response_text}")
                    
                    if TTS_ENGINE == 'gtts':
                        await asyncio.to_thread(speak_gtts, response_text)
                    elif TTS_ENGINE == 'silero':
                        await asyncio.to_thread(speak_silero, response_text)
                else:
                    print("Агент вернул пустой ответ.")
                
                activate_sound.play()
                gui_queue.put({'type': 'phase', 'name': 'listening', 'reset': True})
                command = await asyncio.to_thread(listen_for_command, stream, play_sound=False, activation_timeout=FOLLOW_UP_TIMEOUT_SECONDS)

            print(f"\n🔁 Снова жду кодовое слово '{WAKE_WORD}'...")
    except Exception as e:
        print(f"Критическая ошибка в потоке ассистента: {e}")
    finally:
        print("Поток ассистента завершает работу.")
        if stream.is_active():
            stream.stop_stream()
            stream.close()
        pa.terminate()

def shutdown_app():
    stop_event.set()


def _warm_up_omniparser():
    """Load the OmniParser vision models once at startup, off the hot path,
    so the first `scrape_application` during a conversation is not stalled ~7s."""
    try:
        import time as _t
        from agent.vision.omniparser_engine import OmniParserEngine
        t0 = _t.time()
        OmniParserEngine()
        print(f"OmniParser прогрет за {_t.time() - t0:.1f} с.")
    except Exception as e:
        print(f"Не удалось прогреть OmniParser: {e}")


threading.Thread(target=_warm_up_omniparser, daemon=True).start()

assistant_thread = threading.Thread(target=lambda: asyncio.run(voice_assistant_logic()), daemon=True)
assistant_thread.start()

app = SubtitleOverlay(gui_queue=gui_queue, stop_event_callback=shutdown_app, stop_event=stop_event)
app.mainloop()

print("Основной поток: ожидание завершения рабочего потока...")
assistant_thread.join(timeout=2)
print("Программа полностью завершена.")