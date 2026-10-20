"""Phone you over the internet (SIP) with baresip; answer to hear the alarm read out plus a siren.

Works with free sip.linphone.org accounts: one for this caller, one signed in to the
Linphone app on your phone.
"""
import math
import re
import struct
import subprocess
import tempfile
import wave
from pathlib import Path

RATE = 22050  # espeak-ng's output rate


def _siren(seconds=3.0):
    frames = bytearray()
    for i in range(int(RATE * seconds)):
        t = i / RATE
        freq = 700 + 500 * (0.5 + 0.5 * math.sin(2 * math.pi * 1.5 * t))  # wailing sweep
        frames += struct.pack("<h", int(20000 * math.sin(2 * math.pi * freq * t)))
    return bytes(frames)


def _speech(text, path):
    subprocess.run(["espeak-ng", "-s", "140", "-w", str(path), text], check=True, capture_output=True)
    with wave.open(str(path)) as w:
        return w.readframes(w.getnframes())


def _alarm_wav(text, path, repeats=4):
    speech = _speech(text, path.with_name("speech.wav"))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((speech + _siren()) * repeats)


def call(user, password, to, text, domain="sip.linphone.org", seconds=60):
    """Ring `to` and play the alarm. Returns a short human-readable outcome."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _alarm_wav(text, d / "alarm.wav")
        (d / "config").write_text(
            "module_path /usr/local/lib/baresip/modules\n"
            "sip_listen 0.0.0.0:0\n"
            "sip_cafile /etc/ssl/certs/ca-certificates.crt\n"
            f"audio_source aufile,{d / 'alarm.wav'}\n"
            f"audio_player aufile,{d / 'heard.wav'}\n"
            "module g711.so\nmodule opus.so\nmodule aufile.so\nmodule srtp.so\n"
            "module_app account.so\nmodule_app menu.so\n")
        (d / "accounts").write_text(
            f"<sip:{user}@{domain};transport=tcp>;auth_pass={password};regint=0;mediaenc=srtp-mand\n")  # Linphone requires encrypted audio
        target = to if to.startswith("sip:") else f"sip:{to}@{domain}"
        try:
            out = subprocess.run(["baresip", "-s", "-f", str(d), "-t", str(seconds), "-e", f"/dial {target}"],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 timeout=seconds + 20).stdout
        except subprocess.TimeoutExpired:
            return "baresip did not finish"
    status = re.findall(r"^SIP/2\.0 (\d{3} [^\r\n]*)", out, re.M)
    closed = re.search(r"session closed: ([^\r\n]*)", out)
    rang = any(s.startswith(("180", "183")) for s in status)
    answered = "Call established" in out or any(s.startswith("200") for s in status[1:])
    if answered:
        return "rang and was answered"
    if rang:
        return "rang (not answered)"
    return f"failed: {closed.group(1) if closed else (status[-1] if status else 'no reply')}"
