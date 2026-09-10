import os
import time
import logging
import threading
import torch
from PIL import Image
import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

class OmniParserEngine:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(OmniParserEngine, cls).__new__(cls)
                cls._instance._initialized = False
        return cls._instance

    def __init__(self, weights_dir=None):
        # Guard against a concurrent warm-up thread and the first real scrape
        # both running heavy model loading at the same time.
        with self._lock:
            if self._initialized:
                return

            if weights_dir is None:
                weights_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "weights")
            self.weights_dir = weights_dir

            self.device = "cuda" if torch.cuda.is_available() else "cpu"
            logging.info(f"Initializing OmniParserEngine on device: {self.device}")

            self.yolo_model = None
            self.ocr_reader = None
            self._load_models()
            self._initialized = True

    def _load_models(self):
        # 0. Make sure the weights are on disk (download on first run)
        try:
            from utils.model_setup import ensure_omniparser_weights
            ensure_omniparser_weights(self.weights_dir)
        except Exception as e:
            logging.warning(f"Could not auto-provision OmniParser weights: {e}")

        # 1. Load YOLO icon detection model if weights exist
        icon_detect_path = os.path.join(self.weights_dir, "icon_detect", "model.pt")
        if not os.path.exists(icon_detect_path):
            # Alternative locations
            icon_detect_path = os.path.join(self.weights_dir, "model.pt")

        if os.path.exists(icon_detect_path):
            try:
                from ultralytics import YOLO
                logging.info(f"Loading YOLO icon detect model from: {icon_detect_path}")
                self.yolo_model = YOLO(icon_detect_path)
                self.yolo_model.to(self.device)
                logging.info("YOLO icon detect model loaded successfully.")
            except Exception as e:
                logging.warning(f"Could not load YOLO model: {e}")

        # 2. Load EasyOCR or RapidOCR if available
        try:
            import easyocr
            logging.info("Loading EasyOCR reader for text detection...")
            self.ocr_reader = easyocr.Reader(['en', 'ru'], gpu=(self.device == "cuda"))
            logging.info("EasyOCR initialized.")
        except Exception as e:
            logging.info(f"EasyOCR not available ({e}), falling back to OpenCV/basic OCR processing.")

    def parse_image(self, image: Image.Image, confidence_threshold: float = 0.15) -> list:
        """
        Parses a PIL Image of a window/screen and returns a list of detected interactive UI elements.
        
        Returns:
            list of dicts: [
                {
                    "left": int, "top": int, "right": int, "bottom": int,
                    "name": str, "control_type": str
                }, ...
            ]
        """
        start_t = time.time()
        w, h = image.size
        results = []

        # 1. OCR text detection
        ocr_boxes = []
        if self.ocr_reader:
            try:
                img_np = np.array(image)
                ocr_results = self.ocr_reader.readtext(img_np)
                for bbox, text, prob in ocr_results:
                    if prob > 0.2 and text.strip():
                        # Fix common OCR misreadings of single digits
                        clean_text = text.strip()
                        if clean_text in ['l', 'I', '|', 'i']:
                            clean_text = '1'
                        elif clean_text in ['O', 'o']:
                            clean_text = '0'

                        xs = [p[0] for p in bbox]
                        ys = [p[1] for p in bbox]
                        ocr_boxes.append({
                            "left": int(min(xs)),
                            "top": int(min(ys)),
                            "right": int(max(xs)),
                            "bottom": int(max(ys)),
                            "name": clean_text,
                            "control_type": "Text/Button"
                        })
            except Exception as e:
                logging.warning(f"Error during OCR detection: {e}")

        # 2. YOLO Icon detection
        icon_boxes = []
        if self.yolo_model:
            try:
                yolo_results = self.yolo_model.predict(source=image, conf=confidence_threshold, verbose=False)
                for r in yolo_results:
                    for box in r.boxes:
                        xyxy = box.xyxy[0].cpu().numpy()
                        cls_id = int(box.cls[0].cpu().numpy())
                        icon_boxes.append({
                            "left": int(xyxy[0]),
                            "top": int(xyxy[1]),
                            "right": int(xyxy[2]),
                            "bottom": int(xyxy[3]),
                            "name": f"Icon_{cls_id}",
                            "control_type": "Icon"
                        })
            except Exception as e:
                logging.warning(f"Error during YOLO detection: {e}")

        # Merge OCR text into YOLO button boxes (so full button area is used + OCR label)
        final_elements = []
        for ib in icon_boxes:
            matched_text = []
            for ob in ocr_boxes:
                cx = (ob["left"] + ob["right"]) / 2
                cy = (ob["top"] + ob["bottom"]) / 2
                if (ib["left"] <= cx <= ib["right"]) and (ib["top"] <= cy <= ib["bottom"]):
                    matched_text.append(ob["name"])
            
            if matched_text:
                ib["name"] = " ".join(matched_text)
                ib["control_type"] = "Button"
            final_elements.append(ib)

        # Add remaining standalone OCR text boxes not covered by YOLO boxes
        for ob in ocr_boxes:
            inside = False
            cx = (ob["left"] + ob["right"]) / 2
            cy = (ob["top"] + ob["bottom"]) / 2
            for fe in final_elements:
                if (fe["left"] <= cx <= fe["right"]) and (fe["top"] <= cy <= fe["bottom"]):
                    inside = True
                    break
            if not inside:
                final_elements.append(ob)

        # Sort elements spatially: row by row (top-to-bottom), then left-to-right
        # Use 40px bucket to group buttons on the same row properly
        def spatial_sort_key(elem):
            row_bucket = elem["top"] // 40
            return (row_bucket, elem["left"])

        final_elements.sort(key=spatial_sort_key)

        elapsed = time.time() - start_t
        logging.info(f"⏱️ [OmniParserEngine] Image parsed in {elapsed:.4f}s. Detected {len(final_elements)} UI elements (Spatially Sorted).")
        return final_elements
