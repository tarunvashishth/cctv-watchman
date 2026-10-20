"""Listens to Frigate over MQTT and sends parking / door alerts to Telegram."""
import html
import json
import logging
import os
import queue
import re
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import paho.mqtt.client as mqtt
import requests
import yaml

import sipcall
from matcher import CarMatcher
from tracker import Alert, Tracker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("notifier")

with open(os.environ.get("CONFIG", "/app/config.yml")) as f:
    CFG = yaml.safe_load(f)

CAMERAS = CFG["cameras"]
ZONE_CAMERA = {}  # filled from Frigate's config at startup
FRIGATE = os.environ.get("FRIGATE_URL", "http://frigate:5000").rstrip("/")
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])
TG = f"https://api.telegram.org/bot{TOKEN}"


class _HideSecrets(logging.Filter):
    """Network errors include the request URL, which contains the bot token."""
    def filter(self, record):
        msg = record.getMessage()
        if TOKEN in msg:
            record.msg, record.args = msg.replace(TOKEN, "<bot-token>"), ()
        return True


for _h in logging.getLogger().handlers:
    _h.addFilter(_HideSecrets())
SIP_USER = os.environ.get("SIP_USER", "")
SIP_PASSWORD = os.environ.get("SIP_PASSWORD", "")
SIP_CALL_TO = os.environ.get("SIP_CALL_TO", "")
NIGHT = CFG.get("night_watch") or {}
TZ = ZoneInfo(NIGHT.get("timezone", "Asia/Kolkata"))


def is_night():
    if not NIGHT.get("enabled", True):
        return False
    start, end, hour = NIGHT.get("start_hour", 0), NIGHT.get("end_hour", 5), datetime.now(TZ).hour
    return start <= hour < end if start < end else hour >= start or hour < end

tracker = Tracker(
    areas=CFG["parking"],
    door_zones=CFG.get("doors") or {},
    my_car_name=CFG["my_car_name"],
    stable_seconds=CFG.get("stable_seconds", 30),
    leave_seconds=CFG.get("leave_seconds", 120),
    door_cooldown=CFG.get("door_cooldown_seconds", 60),
    notify_all_changes=CFG.get("notify_all_changes", False),
    watch_zones=NIGHT.get("zones") or [],
    watch_seconds=NIGHT.get("linger_seconds", 20),
    alarm_repeat_seconds=NIGHT.get("repeat_seconds", 60),
    is_night=is_night,
)
matcher = CarMatcher(
    os.environ.get("REFERENCE_DIR", "/app/references/my_car"),
    CFG.get("appearance_match", {}).get("threshold", 0.75),
)
outbox: "queue.Queue[Alert]" = queue.Queue()


# ---- Frigate ----------------------------------------------------------------

def frigate_jpeg(path, **params):
    try:
        r = requests.get(f"{FRIGATE}{path}", params=params, timeout=10)
        r.raise_for_status()
        return r.content
    except requests.RequestException as e:
        log.warning("Frigate %s failed: %s", path, e)
        return None


def event_snapshot(event_id, crop=False):
    return frigate_jpeg(f"/api/events/{event_id}/snapshot.jpg", bbox=0 if crop else 1, crop=int(crop))


def live_frame(camera):
    return frigate_jpeg(f"/api/{camera}/latest.jpg", bbox=1)


def sync_with_frigate():
    """Learn which camera each zone is on, and pick up cars that were already parked."""
    for attempt in range(30):  # Frigate can take a minute to start
        try:
            cfg = requests.get(f"{FRIGATE}/api/config", timeout=10).json()
            for cam, c in cfg["cameras"].items():
                ZONE_CAMERA.update({z: cam for z in (c.get("zones") or {})})
            r = requests.get(f"{FRIGATE}/api/events",
                             params={"cameras": ",".join(CAMERAS), "in_progress": 1, "limit": 100}, timeout=10)
            r.raise_for_status()
            events = r.json()
            break
        except (requests.RequestException, ValueError, KeyError) as e:
            if attempt == 29:
                log.warning("Could not load in-progress events: %s", e)
                return
            time.sleep(5)
    for ev in events:
        if ev.get("camera") in CAMERAS and ev.get("label") == "car" and not ev.get("false_positive"):
            tracker.load_existing(ev["id"], ev.get("zones"), ev.get("sub_label"))
    log.info("Loaded %d in-progress events", len(events))


# ---- Telegram ---------------------------------------------------------------

def tg_send(text, photo=None):
    try:
        if photo:
            r = requests.post(f"{TG}/sendPhoto", data={"chat_id": CHAT_ID, "caption": text},
                              files={"photo": ("snapshot.jpg", photo, "image/jpeg")}, timeout=30)
        else:
            r = requests.post(f"{TG}/sendMessage", data={"chat_id": CHAT_ID, "text": text}, timeout=30)
        if not r.ok:
            log.error("Telegram rejected message: %s %s", r.status_code, r.text[:200])
    except requests.RequestException as e:
        log.error("Telegram send failed: %s", e)


call_lock = threading.Lock()
called = set()


def call_alarm(text, key=None):
    """Ring the Linphone app on your phone over the internet. One call per intruder (key)."""
    if not (SIP_USER and SIP_PASSWORD and SIP_CALL_TO) or (key and key in called):
        return
    if key:
        called.add(key)
    spoken = "".join(ch for ch in text.split("\n")[0] if ch.isascii()).strip()  # drop emoji
    with call_lock:  # one call at a time
        try:
            outcome = sipcall.call(SIP_USER, SIP_PASSWORD, SIP_CALL_TO, f"Parking alarm. {spoken}")
        except Exception as e:
            outcome = f"failed: {e}"
    (log.info if not outcome.startswith("failed") else log.error)("Alarm call %s", outcome)
    return outcome


def sender_loop():
    while True:
        alert = outbox.get()
        if alert.urgent:
            key = alert.event_id or f"{alert.zone}-{int(time.time() // 600)}"  # one incident per intruder
            threading.Thread(target=call_alarm, args=(alert.text, key), daemon=True).start()
        photo = event_snapshot(alert.event_id) if alert.event_id else None
        if not photo and alert.zone in ZONE_CAMERA:
            photo = live_frame(ZONE_CAMERA[alert.zone])
        tg_send(alert.text, photo)
        log.info("Sent: %s", alert.text.replace("\n", " | "))


HELP = (
    "/status – which spots are free + live pictures\n"
    "/snapshot – live pictures\n"
    "/testcall – get an alarm call in the Linphone app to test it\n"
    "/mycar – right after you park: teach me what your car looks like "
    "(uses the car that arrived most recently)"
)


def send_live(caption):
    for camera in CAMERAS:
        tg_send(f"{caption} – {camera}", live_frame(camera))


def handle_command(text):
    cmd, *args = text.strip().split()
    cmd = cmd.split("@")[0].lower()
    if cmd == "/status":
        tg_send(tracker.summary())
        send_live("📷 Live")
    elif cmd == "/snapshot":
        send_live("📷 Live")
    elif cmd == "/testcall":
        if not (SIP_USER and SIP_PASSWORD and SIP_CALL_TO):
            return tg_send("Set SIP_USER, SIP_PASSWORD and SIP_CALL_TO in .env first.")
        tg_send("Calling your Linphone app now…")
        outcome = call_alarm("This is a test call. Night alarms will call you like this.")
        tg_send(f"Test call {outcome}.")
    elif cmd == "/mycar":
        event_id = tracker.newest_car()
        crop = event_snapshot(event_id, crop=True) if event_id else None
        if not crop:
            return tg_send("I don't see a parked car right now.")
        path = matcher.add_reference(crop)
        tracker.mark_mine(event_id)
        tg_send(f"Saved this as your car ({len(matcher.refs)} reference picture(s)). "
                "If it's the wrong car, delete the latest file in notifier/references/my_car.", crop)
        log.info("Saved reference %s", path)
    else:
        tg_send(HELP)


def telegram_loop():
    offset = None
    while True:
        try:
            r = requests.get(f"{TG}/getUpdates", params={"timeout": 30, "offset": offset}, timeout=40)
            updates = r.json().get("result", [])
        except (requests.RequestException, ValueError) as e:
            log.warning("Telegram poll failed: %s", e)
            time.sleep(5)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != CHAT_ID or not msg.get("text"):
                continue  # ignore strangers who find the bot
            try:
                handle_command(msg["text"])
            except Exception:
                log.exception("Command failed")
                tg_send("Sorry, that didn't work. Check the notifier logs.")


# ---- MQTT -------------------------------------------------------------------

def on_connect(client, userdata, flags, reason_code, properties):
    log.info("MQTT connected (%s)", reason_code)
    client.subscribe([("frigate/events", 0), ("frigate/tracked_object_update", 0), ("frigate/available", 0)])


def on_message(client, userdata, msg):
    if msg.topic == "frigate/available":
        if msg.payload == b"online":
            # Frigate (re)started: drop cars it no longer knows about and reload the current ones
            tracker.reset_cars()
            threading.Thread(target=sync_with_frigate, daemon=True).start()
        return
    try:
        payload = json.loads(msg.payload)
    except ValueError:
        return
    if msg.topic == "frigate/events":
        if (payload.get("after") or {}).get("camera") not in CAMERAS:
            return
        for alert in tracker.handle_event(payload):
            outbox.put(alert)
    elif msg.topic == "frigate/tracked_object_update":
        if payload.get("type") == "lpr" and payload.get("name") == CFG["my_car_name"]:
            tracker.mark_mine(payload["id"])


def main():
    threading.Thread(target=sender_loop, daemon=True).start()
    threading.Thread(target=telegram_loop, daemon=True).start()

    sync_with_frigate()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect_async(os.environ.get("MQTT_HOST", "mqtt"), 1883)
    client.loop_start()

    tg_send("👀 Parking watcher started.\n" + HELP)
    while True:
        for event_id in tracker.cars_to_check():
            tracker.mark_checked(event_id)
            crop = event_snapshot(event_id, crop=True)
            if crop and matcher.is_mine(crop):
                log.info("Appearance match: %s is my car", event_id)
                tracker.mark_mine(event_id)
        for alert in tracker.tick():
            outbox.put(alert)
        time.sleep(1)


if __name__ == "__main__":
    main()
