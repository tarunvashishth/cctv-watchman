"""Run with: python -m unittest test_tracker"""
import unittest

from tracker import Tracker


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def ev(type_, id_, label, zones, sub_label=None, stationary=False):
    return {"type": type_, "after": {"id": id_, "label": label, "camera": "cam3",
                                     "current_zones": zones, "sub_label": sub_label,
                                     "stationary": stationary}}


class TrackerTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.t = Tracker([{"name": "the lane", "capacity": 3, "zones": ["z2", "z3"]}],
                         {"door3": "front door"}, "my_car",
                         stable_seconds=30, leave_seconds=120, door_cooldown=60, clock=self.clock)
        self.assertEqual(self.advance(121), [])  # startup values are learned silently

    def advance(self, s):
        self.clock.t += s
        return [a.text for a in self.t.tick()]

    def car(self, id_, zones, **kw):
        self.t.handle_event(ev("update", id_, "car", zones, **kw))

    def test_my_car_arrives_and_leaves(self):
        self.car("c1", ["z3"])
        self.assertEqual(self.advance(10), [])
        self.car("c1", ["z3"], sub_label=["my_car", 0.9], stationary=True)  # plate read
        self.assertEqual(self.advance(25), [])
        out = self.advance(10)
        self.assertEqual(out, ["🚗 Your car has arrived at the lane.\n🅿️ 2 of 3 spaces free in the lane."])

        self.t.handle_event(ev("end", "c1", "car", []))
        self.assertEqual(self.advance(60), [])  # must be gone for leave_seconds
        out = self.advance(61)
        self.assertEqual(out, ["🚙 Your car has left the lane.\n🅿️ 3 of 3 spaces free in the lane."])

    def test_brief_dropout_of_my_car_is_ignored(self):
        self.car("c1", ["z3"], sub_label=["my_car", 0.9])
        self.advance(31)
        self.t.handle_event(ev("end", "c1", "car", []))
        self.advance(50)
        self.car("c2", ["z3"], sub_label=["my_car", 0.9])  # re-detected, plate read again
        self.assertEqual(self.advance(200), [])

    def test_same_car_on_both_cameras_counts_once(self):
        self.car("a", ["z2"])
        self.car("b", ["z3"])  # same physical car seen by the other camera
        self.advance(31)
        self.assertIn("2 of 3 spaces free", self.t.summary())

    def test_full_and_space_free(self):
        for i in range(3):
            self.car(f"c{i}", ["z2"])
        self.assertEqual(self.advance(31), ["🚫 the lane is full now."])
        self.t.handle_event(ev("end", "c0", "car", []))
        self.assertEqual(self.advance(31), ["✅ A space is free in the lane.\n🅿️ 1 of 3 spaces free in the lane."])

    def test_small_changes_quiet_unless_enabled(self):
        self.car("c0", ["z2"])
        self.assertEqual(self.advance(31), [])
        self.t.notify_all_changes = True
        self.car("c1", ["z2"])
        self.assertEqual(self.advance(31), ["🅿️ 1 of 3 spaces free in the lane."])

    def test_flicker_does_not_alert(self):
        for i in range(3):
            self.car(f"c{i}", ["z2"])
        self.advance(31)
        for _ in range(5):  # detection drops and comes back every few seconds
            self.t.handle_event(ev("end", "c0", "car", []))
            self.advance(5)
            self.car("c0", ["z2"])
            self.advance(5)
        self.assertEqual(self.advance(31), [])

    def test_door_alert_with_cooldown(self):
        self.assertEqual(self.t.handle_event(ev("new", "p1", "person", [])), [])
        alerts = self.t.handle_event(ev("update", "p1", "person", ["door3"]))
        self.assertEqual([(a.text, a.zone) for a in alerts], [("🚪 Someone is at the front door.", "door3")])
        self.assertEqual(self.t.handle_event(ev("update", "p1", "person", ["door3"])), [])
        self.clock.t += 10
        self.assertEqual(self.t.handle_event(ev("update", "p2", "person", ["door3"])), [])
        self.clock.t += 60
        self.assertEqual(len(self.t.handle_event(ev("update", "p3", "person", ["door3"]))), 1)

    def test_unidentified_parked_car_gets_checked(self):
        self.car("c1", ["z3"], stationary=True)
        self.assertEqual(self.t.cars_to_check(), ["c1"])
        self.t.mark_checked("c1")
        self.assertEqual(self.t.cars_to_check(), [])

    def test_reset_after_frigate_restart(self):
        self.car("old1", ["z2"])
        self.car("old2", ["z2"])
        self.t.reset_cars()
        self.car("new1", ["z2"])  # the same parked car, re-detected with a new id
        self.assertIn("2 of 3 spaces free", self.t.summary())

    def test_newest_car(self):
        self.car("1700000005.1-abc", ["z3"])
        self.car("1700000009.2-def", ["z2"])
        self.car("1700000020.0-ghi", [])  # not in the lane
        self.assertEqual(self.t.newest_car(), "1700000009.2-def")


class NightWatchTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.night = True
        self.t = Tracker([{"name": "the lane", "capacity": 6, "zones": ["z3"]}],
                         {"door3": "front door"}, "my_car", watch_zones=["w3"], watch_seconds=20,
                         alarm_repeat_seconds=60, is_night=lambda: self.night, clock=self.clock)
        self.clock.t += 121
        self.t.tick()

    def advance(self, s):
        self.clock.t += s
        return self.t.tick()

    def test_lurker_among_cars_alarms_and_repeats(self):
        self.t.handle_event(ev("new", "p1", "person", ["w3"]))
        self.assertEqual(self.advance(10), [])
        alarms = self.advance(11)
        self.assertEqual(len(alarms), 1)
        self.assertTrue(alarms[0].urgent)
        self.assertIn("Someone is among the parked cars (21 s)", alarms[0].text)
        self.assertEqual(self.advance(30), [])
        self.assertIn("still among", self.advance(31)[0].text)
        self.t.handle_event(ev("end", "p1", "person", []))
        self.assertEqual(self.advance(120), [])

    def test_passer_by_and_daytime_ignored(self):
        self.t.handle_event(ev("new", "p1", "person", ["w3"]))
        self.advance(5)
        self.t.handle_event(ev("update", "p1", "person", []))  # walked on
        self.assertEqual(self.advance(60), [])
        self.night = False
        self.t.handle_event(ev("new", "p2", "person", ["w3"]))
        self.assertEqual(self.advance(60), [])

    def test_my_car_moving_at_night(self):
        self.t.handle_event(ev("update", "c1", "car", ["z3"], ["my_car", 0.9], True))
        self.advance(31)
        alerts = self.t.handle_event(ev("update", "c1", "car", [], ["my_car", 0.9], False))
        self.assertEqual(len(alerts), 1)
        self.assertTrue(alerts[0].urgent)
        self.assertIn("YOUR CAR IS MOVING", alerts[0].text)
        self.assertEqual(self.t.handle_event(ev("update", "c1", "car", [], ["my_car", 0.9], False)), [])
        out = self.advance(121)  # then the normal "left" message, also urgent at night
        self.assertTrue(out[0].urgent and "Your car has left" in out[0].text)

    def test_door_urgent_only_at_night(self):
        self.assertTrue(self.t.handle_event(ev("update", "p1", "person", ["door3"]))[0].urgent)
        self.night = False
        self.clock.t += 61
        alert = self.t.handle_event(ev("update", "p2", "person", ["door3"]))[0]
        self.assertFalse(alert.urgent)
        self.assertEqual(alert.text, "🚪 Someone is at the front door.")


if __name__ == "__main__":
    unittest.main()
