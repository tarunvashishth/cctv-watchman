# CCTV Watchman (Hikvision DVR + Frigate)

Watches your Hikvision camera and sends Telegram messages when:

- 🚗 your car arrives or leaves
- ⚠️ another car parks in front of your house, or ✅ a spot becomes free (every message lists which spots are free)
- 🚨 at night: someone lingering among the parked cars, or your car moving, triggers a loud phone alarm (see "Night alarm")

Each alert includes a snapshot. Message the bot `/status` anytime to see the spots and a live picture.

```
Hikvision DVR ──RTSP──▶ Frigate ──MQTT──▶ notifier ──▶ Telegram
                        (detects cars/people,          (spot logic,
                         zones, number plates)          alerts, /status)
```

- **[Frigate](https://frigate.video)** (open source) does the video AI: car/person detection, zones, number-plate reading, recordings, and a web UI.
- **notifier/** is the small part written for this project: it turns Frigate's events into "your car left, Spot A is free" messages.

Hik-Connect keeps working as before; this reads the DVR directly on your home network.

---

## What you need

- A computer that stays on, on the same network as the DVR. Your Mac is fine to start with; an always-on mini PC is better long term.
- **Docker**: on this Mac it's [Colima](https://github.com/abiosoft/colima) (installed with `brew install colima docker docker-compose`). It starts automatically at login. Check it with `colima status`; start it by hand with `colima start`.
- The DVR's IP address, username and password.

## 1. Check the camera stream from the DVR

1. Find the DVR's IP: in Hik-Connect go to *Device → Settings → Network*, or check your router's device list.
2. In the DVR's web page (`http://<DVR-IP>`), check that RTSP is allowed: *Configuration → Network → Advanced Settings → Integration Protocol*. Also set *RTSP Authentication* to *digest/basic*.
3. Recommended: under *Configuration → Video/Audio*, set the camera's **sub stream** to **H.264** at 640×360 or 704×576, 5–10 fps. Frigate runs detection on this stream.
4. Test in VLC (*File → Open Network*), using the channel your front camera is on:
   - Main stream: `rtsp://admin:PASSWORD@192.168.1.64:554/Streaming/Channels/101`
   - Sub stream: `rtsp://admin:PASSWORD@192.168.1.64:554/Streaming/Channels/102`

   Channel 2 is `201`/`202`, channel 3 is `301`/`302`, and so on.

If your camera is on a channel other than 1, change `101`/`102` in [frigate/config/config.yml](frigate/config/config.yml).

## 2. Create the Telegram bot

1. In Telegram, message **@BotFather** → `/newbot` → follow the prompts. Copy the **token**.
2. Send any message to your new bot.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy the number under `"chat":{"id":…}`. That's your **chat ID**.

The bot ignores messages from anyone except this chat.

## 3. Configure

```bash
cp .env.example .env
```

Edit `.env` with your DVR IP, login, Telegram token and chat ID.

In [frigate/config/config.yml](frigate/config/config.yml), your number plate is read from `FRIGATE_MY_PLATE` in `.env` (written without spaces, e.g. `DL3CAB1234`) and used under `lpr → known_plates → my_car`.

## 4. Start it

```bash
docker compose up -d
```

Then open **https://localhost:8971**. The first-time admin password is printed in the logs:

```bash
docker compose logs frigate | grep -i password
```

You should see the camera live. You'll also get a "Parking watcher started" Telegram message.

## 5. Draw the parking zones

In the Frigate UI, go to **Settings → Masks / Zones**, pick the camera and edit the zones:

| Zone name | What to draw | Objects |
|---|---|---|
| `spot_a`, `spot_b`, … | One shape per parking spot, covering where a parked car's body sits | car |

Keep the names exactly as above, or update [notifier/config.yml](notifier/config.yml) to match. Add or remove spots in both places. Save, then restart:

```bash
docker compose restart
```

Tips:
- Draw spot zones a bit smaller than the spot, so cars driving past on the road don't clip them.
- `stable_seconds` (30 by default, in `notifier/config.yml`) is how long a spot must stay changed before you're told. That filters out cars that just drive through.

## 6. Teach it your car

There are two ways it recognises your car, and they work together:

1. **Number plate (best).** Frigate reads plates of cars it detects and tags matches as `my_car`. This needs the plate to be reasonably large and sharp in the camera image (at least about 30×30 px on the detect stream). Check *Explore* in the Frigate UI to see whether plates are being read.
2. **Colour (fallback).** Park your car in a spot and message the bot `/mycar spot_a`. It saves a picture of your car and from then on recognises a parked car of the same colour. Do this once in daylight and once in the evening, since it keeps several references. It can't tell apart two cars of the same colour, so rely on the plate where you can.

## Night alarm (battery / car theft)

Between **1 am and 4 am** (`night_watch` in [notifier/config.yml](notifier/config.yml)), these raise an **urgent alarm**:

- 🚨 someone stays among the parked cars for 20+ seconds (repeats every minute while they're there)
- 🚨 your car drives out of its spot

Each alarm goes to Telegram with a picture, and also rings your phone (below).

**Phone call over the internet (Linphone, free and open source):** night alarms ring the **Linphone** app on your phone like a real call, one call per intruder. If you answer, you hear the alarm read out and a siren. The Mac places the call with [baresip](https://github.com/baresip/baresip), using free accounts from [sip.linphone.org](https://subscribe.linphone.org/).
1. At https://subscribe.linphone.org/ create a free account.
2. Install **Linphone** (Play Store or F-Droid) on the phone and sign in with it. Linphone requires encrypted call audio; the caller uses SRTP for this.
3. On the S23, set **Settings → Apps → Linphone → Battery → Unrestricted**. In Linphone's settings, enable keeping the app running in the background if offered, so it can ring while the phone sleeps. Allow Linphone through Do Not Disturb, and keep the phone on sound, not silent.
4. In `.env` set `SIP_USER`, `SIP_PASSWORD` and `SIP_CALL_TO` to that same account. The Mac only dials and never signs in to receive calls, so calling your own account rings the phone. A separate caller account also works. Then run `docker compose up -d --no-deps notifier`.
5. Send `/testcall` to the Telegram bot. It replies with how the call went (rang / answered / why it failed).

**Watch zones:** `cam2_watch` and `cam3_watch` in Frigate (Settings → Masks / Zones) are where people are watched at night. They start slightly bigger than the parking zones. Make sure each covers the cars **and the strip of road in front of their bonnets**, because Frigate uses a person's feet to decide which zone they're in.

## Cloud mode (runs on the Oracle server, 24/7)

The home connection has no public IP (CGNAT) and the router has no VPN, so a server can't reach the DVR directly. Instead, cameras 2 and 3 are pulled through the **Hik-Connect cloud**, the same path the phone app uses, by [hikcloud2rtc](https://github.com/halamov/hikcloud2rtc) (an unofficial client by halamov). It has no license file, so it is **not bundled** here: clone it next to this project's files first, then start as below.
  ```bash
  git clone https://github.com/halamov/hikcloud2rtc.git hikcloud2rtc
  ```
  Frigate then reads those streams instead of the DVR.

- Server: any always-on Linux box with Docker (`ssh <your-server-ip>`)
- Start or update:
  ```bash
  docker compose -f docker-compose.yml -f docker-compose.cloud.yml up -d --build
  ```
- Frigate config: [frigate/config/config.cloud.yml](frigate/config/config.cloud.yml), generated from `config.yml` with the cameras pointed at the cloud stream.
- Frigate UI: `ssh -L 8971:localhost:8971 <your-server-ip>`, then open http://localhost:8971. It's not exposed to the internet.
- Hik-Connect login: `HIK_USER` / `HIK_PASSWORD` in `.env`. A phone-number login must be written as `91` + number, without `+`.
- Uses ~0.4 Mbit/s of home upload for both cameras. Unofficial: a Hik-Connect app update could break it.

## Disk space (automatic cleanup)

Nothing needs manual deleting:
- **Frigate**: snapshots are deleted after 3 days, alert clips after 3 days, other detection clips after 1 day, and no 24/7 video is kept (`snapshots` / `record` in [frigate/config/config.yml](frigate/config/config.yml)). Frigate also removes the oldest recordings by itself if the disk runs low.
- **Container logs** are capped at 3 × 10 MB per service ([docker-compose.yml](docker-compose.yml)).
- After rebuilding the notifier, run `docker image prune -f` to remove the old image.

## Telegram commands

| Command | Does |
|---|---|
| `/status` | Each spot (free / your car / another car) + live picture |
| `/snapshot` | Live picture |
| `/mycar` | Right after you park: save the newest parked car as "my car" (colour reference) |
| `/testcall` | Ring the Linphone app with a test alarm call |

## Troubleshooting

```bash
docker compose logs -f notifier
docker compose logs -f frigate
```

- **No video in Frigate**: wrong IP/password/channel, or a special character in the password. URL-encode it in `.env`, e.g. `@` → `%40`. Test the URL in VLC first.
- **High CPU**: make sure detection uses the sub stream (it does by default) and keep `detect.fps` at 5. On an Intel mini PC, switch the detector to OpenVINO (link in the config).
- **Your car shows as "another car"**: check whether the plate is being read (Frigate → Explore). If not, use `/mycar`. Messages may first say "another car parked" and then "That's your car" if the plate is read late.

## Running the notifier tests

```bash
cd notifier && python3 -m unittest test_tracker
```

## Credits

CCTV Watchman is glue and alert logic on top of other people's open-source work. Thank you to:

| Project | Used for | License |
|---|---|---|
| [Frigate](https://github.com/blakeblackshear/frigate) (Blake Blackshear and contributors) | Object detection, zones, number-plate reading, recordings, web UI (includes [go2rtc](https://github.com/AlexxIT/go2rtc) for restreaming) | MIT |
| [hikcloud2rtc](https://github.com/halamov/hikcloud2rtc) (halamov) | Pulls Hik-Connect cloud streams (cloud mode, not bundled) | no license published |
| [cloud-cam-viewer](https://github.com/Bahrombekk/cloud-cam-viewer), [hikcloudstream](https://github.com/cristianojmiranda/hikcloudstream), [pyEzvizApi](https://github.com/RenierM26/pyEzvizApi) | The foundations hikcloud2rtc is built on | MIT / Apache-2.0 / Apache-2.0 |
| [baresip](https://github.com/baresip/baresip) and [re](https://github.com/baresip/re) | SIP phone calls for the night alarm (built from source in the notifier image) | BSD-3-Clause |
| [Linphone](https://linphone.org) | Free SIP accounts and the phone app that rings | GPL-3.0 (app) |
| [Eclipse Mosquitto](https://github.com/eclipse-mosquitto/mosquitto) | MQTT broker between Frigate and the notifier | EPL-2.0 / EDL-1.0 |
| [eSpeak NG](https://github.com/espeak-ng/espeak-ng) | Reads the alarm aloud on calls (separate binary in the notifier image) | GPL-3.0 |
| [OpenCV](https://opencv.org), [NumPy](https://numpy.org), [paho-mqtt](https://github.com/eclipse/paho.mqtt.python), [Requests](https://github.com/psf/requests), [PyYAML](https://github.com/yaml/pyyaml) | Colour matching and plumbing in the notifier | Apache-2.0 / BSD / EPL / MIT |
| [Telegram Bot API](https://core.telegram.org/bots/api) | Messages and bot commands | n/a (service) |

Not affiliated with or endorsed by Hikvision, Hik-Connect, Frigate or any project above. The Hik-Connect cloud client is unofficial and may break when Hikvision changes their service; use it only with your own devices and account.

## License

[MIT](LICENSE) © 2026 Tarun Vashishtha. Third-party components keep their own licenses.
