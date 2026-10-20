"""Turns Frigate tracked-object events into parking and door alerts.

A parking *area* is a stretch of road that holds `capacity` cars. It can be
covered by several Frigate zones (e.g. the same row seen from both ends); the
area's car count is the highest count among its zones, so a car seen by two
cameras is counted once.

At night (`is_night()`), people lingering in the watch zones around the
parked cars, your car moving, and anyone at the door raise *urgent* alarms.

Pure logic (no network), so it can be tested by feeding it fake events.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

VEHICLES = {"car", "truck", "motorcycle", "bus"}


@dataclass
class Alert:
    text: str
    event_id: str | None = None  # whose snapshot to attach
    zone: str | None = None      # for a live frame when there's no event snapshot
    urgent: bool = False         # night-time alarm: also ring the phone


@dataclass
class Car:
    zones: set = field(default_factory=set)
    mine: bool = False
    stationary: bool = False
    checked: bool = False  # appearance fallback already tried
    moving_alarmed: bool = False


@dataclass
class Lurker:
    since: float
    zone: str
    last_alarm: float | None = None


class Debounce:
    """Holds a value that only changes after the new value has been seen steadily."""

    def __init__(self):
        self.value = None  # None = not known yet (startup)
        self.pending = None
        self.since = 0.0

    def update(self, value, now):
        if value == self.value:
            self.pending = None
        elif value != self.pending:
            self.pending, self.since = value, now

    def commit(self, now, wait):
        """Returns (old, new) once the pending value has lasted `wait` seconds."""
        if self.pending is None or now - self.since < wait:
            return None
        old, self.value, self.pending = self.value, self.pending, None
        return old, self.value


@dataclass
class Area:
    name: str
    capacity: int
    zones: list
    mine: Debounce = field(default_factory=Debounce)
    free: Debounce = field(default_factory=Debounce)


def sub_label_name(sub_label):
    """Frigate sends sub_label as ["name", score] in MQTT and as "name" in the API."""
    if isinstance(sub_label, (list, tuple)):
        return sub_label[0] if sub_label else None
    return sub_label


class Tracker:
    def __init__(self, areas, door_zones, my_car_name, stable_seconds=30, leave_seconds=120,
                 door_cooldown=60, notify_all_changes=False, watch_zones=(), watch_seconds=20,
                 alarm_repeat_seconds=60, is_night=lambda: False, clock=time.time):
        self.areas = [Area(a["name"], int(a["capacity"]), list(a["zones"])) for a in areas]
        self.spot_zones = {z for a in self.areas for z in a.zones}
        self.door_zones = dict(door_zones)  # zone name -> friendly name
        self.my_car_name = my_car_name
        self.stable_seconds = stable_seconds
        self.leave_seconds = leave_seconds
        self.door_cooldown = door_cooldown
        self.notify_all_changes = notify_all_changes
        self.watch_zones = set(watch_zones)
        self.watch_seconds = watch_seconds
        self.alarm_repeat_seconds = alarm_repeat_seconds
        self.is_night = is_night
        self.clock = clock
        self.lurkers: dict[str, Lurker] = {}

        self.cars: dict[str, Car] = {}
        self.door_seen = set()
        self.last_door_alert = float("-inf")
        self.lock = threading.Lock()
        self._observe()

    # ---- inputs -------------------------------------------------------------

    def handle_event(self, msg):
        """Feed one `frigate/events` payload. Returns door alerts to send now."""
        after = msg.get("after") or {}
        event_id, label = after.get("id"), after.get("label")
        alerts = []
        with self.lock:
            if label in VEHICLES:
                if msg.get("type") == "end" or after.get("false_positive"):
                    self.cars.pop(event_id, None)
                else:
                    car = self.cars.setdefault(event_id, Car())
                    was_parked_here = bool(car.zones)
                    car.zones = set(after.get("current_zones") or []) & self.spot_zones
                    car.stationary = bool(after.get("stationary"))
                    if sub_label_name(after.get("sub_label")) == self.my_car_name:
                        car.mine = True
                    if (car.mine and was_parked_here and not car.zones and not car.stationary
                            and not car.moving_alarmed and self.is_night()):
                        car.moving_alarmed = True
                        alerts.append(Alert("🚨 YOUR CAR IS MOVING! It just drove out of its parking spot.",
                                            event_id, urgent=True))
                self._observe()
            elif label == "person":
                if msg.get("type") == "end":
                    self.lurkers.pop(event_id, None)
                    return alerts
                zones = after.get("current_zones") or []
                watched = [z for z in zones if z in self.watch_zones]
                if watched:
                    self.lurkers.setdefault(event_id, Lurker(self.clock(), watched[0]))
                else:
                    self.lurkers.pop(event_id, None)
                doors = [z for z in zones if z in self.door_zones]
                if doors and event_id not in self.door_seen:
                    self.door_seen.add(event_id)
                    now = self.clock()
                    if now - self.last_door_alert >= self.door_cooldown:
                        self.last_door_alert = now
                        night = self.is_night()
                        alerts.append(Alert(f"{'🚨' if night else '🚪'} Someone is at the "
                                            f"{self.door_zones[doors[0]]}.", event_id, doors[0], urgent=night))
        return alerts

    def load_existing(self, event_id, zones, sub_label, stationary=True):
        """Seed a car that was already parked when the notifier started."""
        with self.lock:
            self.cars[event_id] = Car(
                zones=set(zones or []) & self.spot_zones,
                mine=sub_label_name(sub_label) == self.my_car_name,
                stationary=stationary,
            )
            self._observe()

    def reset_cars(self):
        """Forget all cars (Frigate restarted: its old tracked objects are gone without "end" events)."""
        with self.lock:
            self.cars.clear()
            self.lurkers.clear()
            self._observe()

    def mark_mine(self, event_id):
        with self.lock:
            car = self.cars.get(event_id)
            if car and not car.mine:
                car.mine = True
                self._observe()

    def mark_checked(self, event_id):
        with self.lock:
            if event_id in self.cars:
                self.cars[event_id].checked = True

    # ---- outputs ------------------------------------------------------------

    def tick(self):
        """Announce changes that have lasted long enough. Returns alerts."""
        alerts = []
        with self.lock:
            now = self.clock()
            night = self.is_night()
            if night:
                for event_id, lurker in self.lurkers.items():
                    stayed = now - lurker.since
                    due = lurker.last_alarm is None or now - lurker.last_alarm >= self.alarm_repeat_seconds
                    if stayed >= self.watch_seconds and due:
                        again = " still" if lurker.last_alarm is not None else ""
                        lurker.last_alarm = now
                        alerts.append(Alert(f"🚨 Someone is{again} among the parked cars "
                                            f"({int(stayed)} s). Possible battery or car theft!",
                                            event_id, lurker.zone, urgent=True))
            for area in self.areas:
                leaving = area.mine.pending is False
                mine = area.mine.commit(now, self.leave_seconds if leaving else self.stable_seconds)
                free = area.free.commit(now, self.stable_seconds)
                free_now = area.free.value if area.free.pending is None else self._free(area)
                status = f"🅿️ {free_now} of {area.capacity} spaces free in {area.name}."

                if mine and mine[0] is not None:
                    text = (f"🚗 Your car has arrived at {area.name}." if mine[1]
                            else f"🚙 Your car has left {area.name}.")
                    urgent = night and not mine[1]
                    if urgent:
                        text = f"🚨 {text}"
                    alerts.append(Alert(f"{text}\n{status}", self._my_car(area), area.zones[0], urgent))
                elif free and free[0] is not None:
                    old, new = free
                    if new == 0:
                        text = f"🚫 {area.name} is full now."
                    elif old == 0:
                        text = f"✅ A space is free in {area.name}.\n{status}"
                    elif self.notify_all_changes:
                        text = status
                    else:
                        continue
                    alerts.append(Alert(text, None, area.zones[0]))
        return alerts

    def cars_to_check(self):
        """Parked, unidentified cars in an area that the appearance matcher should look at."""
        with self.lock:
            return [eid for eid, c in self.cars.items()
                    if c.zones and c.stationary and not c.mine and not c.checked]

    def car_in(self, zone):
        """A car in this zone (yours first), for /mycar."""
        with self.lock:
            here = [(eid, c) for eid, c in self.cars.items() if zone in c.zones]
            here.sort(key=lambda ec: not ec[1].mine)
            return here[0][0] if here else None

    def newest_car(self):
        """The most recently arrived car in a parking area (Frigate ids start with a timestamp)."""
        with self.lock:
            here = [eid for eid, c in self.cars.items() if c.zones]
            return max(here, key=lambda eid: float(eid.split("-")[0]), default=None)

    def summary(self):
        with self.lock:
            lines = []
            for area in self.areas:
                lines.append(f"🅿️ {area.name}: {self._free(area)} of {area.capacity} spaces free")
                lines.append("🚗 Your car is here" if self._my_car(area) else "Your car is not here")
            return "\n".join(lines)

    # ---- internals (call with lock held) ------------------------------------

    def _occupied(self, area):
        return max((sum(1 for c in self.cars.values() if z in c.zones) for z in area.zones), default=0)

    def _free(self, area):
        return max(0, area.capacity - self._occupied(area))

    def _my_car(self, area):
        return next((eid for eid, c in self.cars.items() if c.mine and c.zones & set(area.zones)), None)

    def _observe(self):
        now = self.clock()
        for area in self.areas:
            area.mine.update(self._my_car(area) is not None, now)
            area.free.update(self._free(area), now)
