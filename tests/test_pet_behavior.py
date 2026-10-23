import math
import random
import unittest
from unittest.mock import Mock

from xigpet.pet_behavior import PetBehavior, FLOAT_RADIUS_X, FLOAT_RADIUS_Y, FLOAT_SPEED


class PetBehaviorTests(unittest.TestCase):
    def rng(self, *, probability=.1, steps=2):
        rng = Mock(wraps=random.Random(17))
        rng.random.return_value = probability
        rng.randint.return_value = steps
        return rng

    def test_float_probability_is_checked_once_per_minute(self):
        rng = self.rng(probability=.5)
        pet = PetBehavior(rng)
        pet.advance(59)
        rng.random.assert_not_called()
        pet.advance(1)
        self.assertEqual(pet.state, "relax")
        rng.random.assert_called_once()
        pet.advance(59)
        rng.random.assert_called_once()
        rng.random.return_value = .499
        pet.advance(1)
        self.assertEqual(pet.state, "float")
        self.assertEqual(rng.random.call_count, 2)

    def test_float_completes_one_to_four_segments_and_remains_local(self):
        for count in range(1, 5):
            pet = PetBehavior(self.rng(steps=count))
            pet.advance(60)
            self.assertEqual(pet.steps_remaining, count)
            completed = 0
            for _ in range(800):
                before = (pet.x, pet.y)
                remaining = pet.steps_remaining
                pet.advance(.05)
                self.assertLessEqual(abs(pet.x), FLOAT_RADIUS_X + .001)
                self.assertLessEqual(abs(pet.y), FLOAT_RADIUS_Y + .001)
                self.assertLessEqual(math.hypot(pet.x - before[0], pet.y - before[1]), FLOAT_SPEED * .05 + .001)
                if pet.steps_remaining < remaining:
                    completed += 1
                if pet.state == "relax":
                    break
            self.assertEqual(pet.state, "relax")
            self.assertEqual(completed, count)

    def test_play_is_checked_at_three_minutes_and_lasts_thirty_seconds(self):
        rng = self.rng()
        pet = PetBehavior(rng)
        pet.advance(179, allow_float=False)
        rng.random.assert_not_called()
        pet.advance(1, allow_float=False)
        self.assertEqual(pet.state, "play")
        pet.advance(29, paused=True)
        self.assertEqual(pet.state, "play")
        pet.advance(1, paused=True)
        self.assertEqual(pet.state, "relax")
        rng.random.assert_called_once()

    def test_play_takes_priority_when_both_deadlines_arrive(self):
        rng = self.rng()
        pet = PetBehavior(rng)
        pet.advance(180)
        self.assertEqual(pet.state, "play")
        rng.random.assert_called_once()
        pet.advance(30)
        self.assertEqual(pet.state, "relax")
        pet.advance(59)
        self.assertEqual(pet.state, "relax")

    def test_float_pauses_and_can_be_disabled(self):
        pet = PetBehavior(self.rng())
        pet.advance(60)
        before = (pet.x, pet.y)
        pet.advance(10, paused=True)
        self.assertEqual((pet.x, pet.y), before)
        pet.advance(.1, allow_float=False)
        self.assertEqual(pet.state, "relax")
        self.assertEqual((pet.x, pet.y), before)

    def test_float_respects_screen_edges(self):
        pet = PetBehavior(self.rng(steps=4))
        bounds = (0, 0, 18, 12)
        pet.advance(60, bounds=bounds)
        for _ in range(500):
            pet.advance(.05, bounds=bounds)
            self.assertGreaterEqual(pet.x, 0)
            self.assertLessEqual(pet.x, 18)
            self.assertGreaterEqual(pet.y, 0)
            self.assertLessEqual(pet.y, 12)
