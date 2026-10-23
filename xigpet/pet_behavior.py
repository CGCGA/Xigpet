"""Clock-driven pet actions, independent of Tk and GIF playback."""

from __future__ import annotations

import math
import random


FLOAT_RADIUS_X, FLOAT_RADIUS_Y = 80, 52
FLOAT_INTERVAL = 60
PLAY_INTERVAL = 180
PLAY_DURATION = 30
FLOAT_SPEED = 14


class PetBehavior:
    def __init__(self, rng=None):
        self.rng = random if rng is None else rng
        self.state = "relax"
        self.x = self.y = self.clock = 0.0
        self.next_float = FLOAT_INTERVAL
        self.next_play = PLAY_INTERVAL
        self.play_until = 0.0
        self.steps_remaining = 0
        self.target = (0.0, 0.0)
        self.bounds = (-FLOAT_RADIUS_X, -FLOAT_RADIUS_Y, FLOAT_RADIUS_X, FLOAT_RADIUS_Y)

    def _relax(self):
        self.state = "relax"
        self.steps_remaining = 0
        self.next_float = self.clock + FLOAT_INTERVAL

    def _pick_target(self):
        left, top, right, bottom = self.bounds
        for _ in range(12):
            angle, distance = self.rng.uniform(0, math.tau), self.rng.uniform(36, 84)
            x = max(left, min(right, self.x + math.cos(angle) * distance))
            y = max(top, min(bottom, self.y + math.sin(angle) * distance))
            minimum = min(24, max(3, math.hypot(right - left, bottom - top) * .35))
            if math.hypot(x - self.x, y - self.y) >= minimum:
                self.target = (x, y)
                return
        self.target = max(((x, y) for x in (left, right) for y in (top, bottom)),
                          key=lambda p: math.hypot(p[0] - self.x, p[1] - self.y))

    def advance(self, elapsed, *, paused=False, allow_float=True, bounds=None):
        elapsed = max(0.0, elapsed)
        self.clock += elapsed
        if self.state == "play":
            if self.clock >= self.play_until:
                self._relax()
            return
        if paused:
            return
        if bounds is not None:
            self.bounds = bounds
            left, top, right, bottom = bounds
            self.x, self.y = max(left, min(right, self.x)), max(top, min(bottom, self.y))
            self.target = (max(left, min(right, self.target[0])), max(top, min(bottom, self.target[1])))
        if self.state == "float":
            if not allow_float:
                self._relax()
                return
            dx, dy = self.target[0] - self.x, self.target[1] - self.y
            distance = math.hypot(dx, dy)
            step = FLOAT_SPEED * min(.1, elapsed)
            if distance <= step:
                self.x, self.y = self.target
                self.steps_remaining -= 1
                if self.steps_remaining:
                    self._pick_target()
                else:
                    self._relax()
            elif distance:
                self.x += dx / distance * step
                self.y += dy / distance * step
            return
        # Only roll once per deadline, rather than once per rendering frame.
        if self.clock >= self.next_play:
            self.next_play = self.clock + PLAY_INTERVAL
            if self.rng.random() < .5:
                self.state = "play"
                self.play_until = self.clock + PLAY_DURATION
                return
        if self.clock >= self.next_float:
            self.next_float = self.clock + FLOAT_INTERVAL
            if allow_float and self.rng.random() < .5:
                self.state = "float"
                self.steps_remaining = self.rng.randint(1, 4)
                self._pick_target()
