"""YOLO person detection, preferring a cached TensorRT FP16 engine on the Jetson GPU."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

try:
    from ultralytics import YOLO
except ImportError:  # host tests and non-GPU environments
    YOLO = None

try:
    import torch
except ImportError:
    torch = None

PERSON_CLASS = 0
MODEL_NAME = "yolo26n"


def cuda_available() -> bool:
    return bool(torch is not None and torch.cuda.is_available())


def load_model(cache_dir="model_cache"):
    """Load the cached .engine, else the .pt (exporting a TensorRT engine when CUDA exists)."""
    if YOLO is None:
        logger.warning("ultralytics is not installed; detection disabled")
        return None

    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    engine_path, pt_path = cache / f"{MODEL_NAME}.engine", cache / f"{MODEL_NAME}.pt"

    if engine_path.exists():
        try:
            model = YOLO(str(engine_path), task="detect")
            logger.info("TensorRT engine loaded from %s", engine_path)
            return model
        except Exception as exc:
            logger.warning("Could not load the TensorRT engine (%s); falling back to PyTorch", type(exc).__name__)

    if pt_path.exists():
        model = YOLO(str(pt_path))
    else:
        logger.info("Downloading base weights %s.pt", MODEL_NAME)
        model = YOLO(f"{MODEL_NAME}.pt")
        if os.path.exists(f"{MODEL_NAME}.pt"):
            os.replace(f"{MODEL_NAME}.pt", pt_path)
            model = YOLO(str(pt_path))

    if not cuda_available():
        logger.warning("CUDA not available; running inference on CPU")
        return model

    try:
        model.export(format="engine", half=True, device=0, workspace=4)
        if engine_path.exists():
            logger.info("TensorRT FP16 engine exported; reloading")
            return YOLO(str(engine_path), task="detect")
    except Exception as exc:
        logger.error("TensorRT export failed (%s); using CUDA PyTorch", type(exc).__name__)
    model.to("cuda")
    return model


@dataclass(frozen=True)
class Detection:
    confidence: float
    bbox: Tuple[int, int, int, int]

    @property
    def center(self) -> Tuple[int, int]:
        x1, y1, x2, y2 = self.bbox
        return (x1 + x2) // 2, (y1 + y2) // 2


class PersonDetector:
    def __init__(self, model, device: Optional[str] = None) -> None:
        self.model = model
        self.device = device or ("cuda:0" if cuda_available() else "cpu")

    def detect(self, frame, threshold: float) -> List[Detection]:
        results = self.model(frame, device=self.device, classes=[PERSON_CLASS], verbose=False)
        boxes = results[0].boxes
        if boxes is None:
            return []
        detections = []
        for box in boxes:
            confidence = float(box.conf[0])
            if confidence >= threshold:
                x1, y1, x2, y2 = (int(v) for v in box.xyxy[0].tolist())
                detections.append(Detection(confidence, (x1, y1, x2, y2)))
        return detections


def pick_target(detections: Sequence[Detection], cx: float, cy: float) -> Optional[Detection]:
    """The detection closest to the image centre (the one the PTZ keeps following)."""
    if not detections:
        return None
    return min(detections, key=lambda d: (d.center[0] - cx) ** 2 + (d.center[1] - cy) ** 2)
