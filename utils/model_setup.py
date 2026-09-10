"""Auto-provisioning of model weights.

Every helper here is idempotent: it checks whether the weights are already on
disk and only downloads them if they are missing. Call them at startup so a
fresh clone bootstraps itself without a manual download step.
"""

import os
import sys
import logging
import zipfile
import urllib.request

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- Vosk (wake-word / fast ASR) ---
VOSK_MODEL_NAME = "vosk-model-small-ru-0.22"
VOSK_MODEL_URL = f"https://alphacephei.com/vosk/models/{VOSK_MODEL_NAME}.zip"

# --- Silero (local TTS) ---
SILERO_MODEL_FILE = os.path.join(PROJECT_ROOT, "model_silero.pt")
SILERO_MODEL_URL = "https://models.silero.ai/models/tts/ru/v4_ru.pt"

# --- OmniParser (vision UI parsing) ---
OMNIPARSER_WEIGHTS_DIR = os.path.join(PROJECT_ROOT, "agent", "vision", "weights")
OMNIPARSER_REPOS = ["microsoft/OmniParser-v2.0", "microsoft/OmniParser"]


def _download_with_progress(url, dest):
    name = url.rsplit("/", 1)[-1]
    state = {"pct": -1}

    def _hook(count, block_size, total_size):
        if total_size <= 0:
            return
        done = min(total_size, count * block_size)
        pct = done * 100 // total_size
        if pct == state["pct"]:
            return
        state["pct"] = pct
        sys.stdout.write(f"\r  {name}: {pct:3d}%  ({done // (1024 * 1024)} / {total_size // (1024 * 1024)} MB)")
        sys.stdout.flush()

    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    urllib.request.urlretrieve(url, dest, _hook)
    sys.stdout.write("\n")


def ensure_vosk_model(base_dir=PROJECT_ROOT):
    """Return the path to the Vosk model directory, downloading it if absent."""
    model_dir = os.path.join(base_dir, VOSK_MODEL_NAME)
    if os.path.isdir(os.path.join(model_dir, "am")):
        return model_dir

    logger.info("Модель Vosk '%s' не найдена — скачиваю (~45 МБ)...", VOSK_MODEL_NAME)
    zip_path = os.path.join(base_dir, f"{VOSK_MODEL_NAME}.zip")
    try:
        _download_with_progress(VOSK_MODEL_URL, zip_path)
        logger.info("Распаковываю модель Vosk...")
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(base_dir)
    finally:
        if os.path.exists(zip_path):
            os.remove(zip_path)

    if not os.path.isdir(os.path.join(model_dir, "am")):
        raise RuntimeError(f"Не удалось подготовить модель Vosk в {model_dir}")
    logger.info("Модель Vosk готова: %s", model_dir)
    return model_dir


def ensure_silero_model():
    """Return the path to the Silero TTS package, downloading it if absent."""
    if os.path.isfile(SILERO_MODEL_FILE):
        return SILERO_MODEL_FILE

    logger.info("Модель Silero TTS не найдена — скачиваю...")
    tmp = SILERO_MODEL_FILE + ".part"
    try:
        _download_with_progress(SILERO_MODEL_URL, tmp)
        os.replace(tmp, SILERO_MODEL_FILE)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    logger.info("Модель Silero TTS готова: %s", SILERO_MODEL_FILE)
    return SILERO_MODEL_FILE


def ensure_omniparser_weights(weights_dir=OMNIPARSER_WEIGHTS_DIR):
    """Return the OmniParser weights directory, downloading the weights if absent."""
    icon_detect = os.path.join(weights_dir, "icon_detect", "model.pt")
    icon_caption = os.path.join(weights_dir, "icon_caption", "model.safetensors")
    if os.path.exists(icon_detect) and os.path.exists(icon_caption):
        return weights_dir

    from huggingface_hub import snapshot_download

    os.makedirs(weights_dir, exist_ok=True)
    last_err = None
    for repo_id in OMNIPARSER_REPOS:
        try:
            logger.info("Скачиваю веса OmniParser из %s...", repo_id)
            snapshot_download(
                repo_id=repo_id,
                local_dir=weights_dir,
                ignore_patterns=["*.git*", "README.md"],
            )
            logger.info("Веса OmniParser готовы: %s", weights_dir)
            return weights_dir
        except Exception as e:
            logger.warning("Не удалось скачать %s: %s", repo_id, e)
            last_err = e

    raise RuntimeError(f"Не удалось скачать веса OmniParser: {last_err}")


def ensure_all():
    """Provision every model the project needs. Handy for a one-shot setup run."""
    ensure_vosk_model()
    ensure_silero_model()
    ensure_omniparser_weights()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    ensure_all()
