"""OpenCV image I/O that also works with Chinese Windows paths."""

from pathlib import Path

import cv2
import numpy as np


def read_image(path: Path | str, flags: int = cv2.IMREAD_COLOR) -> np.ndarray | None:
    try:
        data = np.fromfile(path, dtype=np.uint8)
        return cv2.imdecode(data, flags) if data.size else None
    except OSError:
        return None


def write_image(path: Path | str, frame: np.ndarray) -> bool:
    path = Path(path)
    ok, data = cv2.imencode(path.suffix or ".png", frame)
    if ok:
        path.parent.mkdir(parents=True, exist_ok=True)
        data.tofile(path)
    return bool(ok)
