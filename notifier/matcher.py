"""Fallback "is this my car?" check by colour, for when the plate can't be read.

Compares the body colour of a cropped car against reference crops of your car
(saved with the /mycar Telegram command). Good at telling a red hatchback from
a white SUV; it can't separate two cars of the same colour.
"""
import time
from pathlib import Path

import cv2
import numpy as np

BRIGHTNESS_WEIGHT = 0.5  # daylight vs dusk changes brightness more than colour
MAX_DISTANCE = 30.0      # colour distance (Lab units) at which the score reaches 0


class CarMatcher:
    def __init__(self, ref_dir, threshold=0.75):
        self.ref_dir = Path(ref_dir)
        self.ref_dir.mkdir(parents=True, exist_ok=True)
        self.threshold = threshold
        self.refs = [c for p in sorted(self.ref_dir.glob("*.jpg"))
                     if (c := self._body_colour(cv2.imread(str(p)))) is not None]

    @staticmethod
    def _body_colour(img):
        """Median Lab colour of the middle of the car (skips road edges, roof line, wheels)."""
        if img is None or img.size == 0:
            return None
        h, w = img.shape[:2]
        mid = img[int(h * 0.2):int(h * 0.8), int(w * 0.15):int(w * 0.85)]
        lab = cv2.cvtColor(mid, cv2.COLOR_BGR2LAB).reshape(-1, 3).astype(np.float32)
        lab[:, 0] *= 100 / 255  # OpenCV stores L as 0-255
        lab[:, 1:] -= 128
        return np.median(lab, axis=0)

    @staticmethod
    def _decode(jpeg):
        return cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)

    @staticmethod
    def _similarity(a, b):
        dl, da, db = a - b
        dist = np.sqrt((BRIGHTNESS_WEIGHT * dl) ** 2 + da ** 2 + db ** 2)
        return max(0.0, 1.0 - dist / MAX_DISTANCE)

    def score(self, jpeg):
        colour = self._body_colour(self._decode(jpeg))
        if colour is None or not self.refs:
            return 0.0
        return max(self._similarity(r, colour) for r in self.refs)

    def is_mine(self, jpeg):
        return bool(self.refs) and self.score(jpeg) >= self.threshold

    def add_reference(self, jpeg):
        img = self._decode(jpeg)
        colour = self._body_colour(img)
        if colour is None:
            raise ValueError("could not read image")
        path = self.ref_dir / f"ref_{int(time.time())}.jpg"
        cv2.imwrite(str(path), img)
        self.refs.append(colour)
        return path
