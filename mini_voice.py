"""A tiny standalone voice client, mostly so I could test voice_listen.py
without clicking around the GUI.

Each account in here is just a plain discord.Client that joins a channel with
channel.connect(). Once it's in, we attach voice_listen.VoiceListener - the
same class the app uses - so whatever this prints is what the app would hear.

Usage:
    py -3.13 mini_voice.py listen     join and print the counters every second
    py -3.13 mini_voice.py tone       one account plays a 440 Hz tone while the
                                      other records it and checks the tone
                                      actually came out the other end
    py -3.13 mini_voice.py analyze    re-run that check on the saved capture

Add --guild <id> and --channel <id> to point it somewhere other than the
default test server.

It reads tokens from accounts.json but never writes to it.
"""
from __future__ import annotations

import array
import asyncio
import json
import math
import os
import sys
import threading
import wave

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import discord  # noqa: E402
import voice_listen as vl  # noqa: E402
from main import build_audio_source  # noqa: E402

TONE_WAV = os.path.join(os.environ.get("TEMP", "."), "voicehive_tone440.wav")
CAPTURE_WAV = os.path.join(os.environ.get("TEMP", "."), "voicehive_capture.wav")

# Socialize by default; override with --guild/--channel.
GUILD_ID = 505974446914535426
CHANNEL_ID = 1122343336934658158
TONE_HZ = 440.0


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def load_accounts():
    with open(os.path.join(ROOT, "accounts.json"), "r", encoding="utf-8") as fh:
        return json.load(fh)


def make_tone(path, freq=TONE_HZ, secs=10.0, rate=48000):
    """Write a stereo 48 kHz 16-bit pure sine - what we will try to hear."""
    buf = array.array("h")
    for i in range(int(rate * secs)):
        s = int(11000 * math.sin(2 * math.pi * freq * i / rate))
        buf.append(s)
        buf.append(s)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(buf.tobytes())


def frames_to_wav(frames, path):
    pcm = b"".join(frames)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(pcm)


def goertzel(samples, rate, freq):
    """Magnitude of one frequency - a tiny single-bin DFT."""
    n = len(samples)
    if not n:
        return 0.0
    k = int(0.5 + n * freq / rate)
    w = 2.0 * math.pi * k / n
    coeff = 2.0 * math.cos(w)
    s0 = s1 = s2 = 0.0
    for v in samples:
        s0 = v + coeff * s1 - s2
        s2 = s1
        s1 = s0
    return math.sqrt(max(0.0, s1 * s1 + s2 * s2 - coeff * s1 * s2))


def rms(samples):
    if not samples:
        return 0.0
    return math.sqrt(sum(float(v) * v for v in samples) / len(samples))


# ----------------------------------------------------------------------
# the mini client
# ----------------------------------------------------------------------
class MiniVoice:
    def __init__(self, listen_acc, speak_acc, guild_id, channel_id,
                 mode="listen"):
        self.listen_acc = listen_acc
        self.speak_acc = speak_acc
        self.guild_id = int(guild_id)
        self.channel_id = int(channel_id)
        self.mode = mode
        self.listen_client = discord.Client()
        self.speak_client = discord.Client() if speak_acc else None
        self.listener = None
        self.captured = []
        self._cap_lock = threading.Lock()
        self._done = asyncio.Event()

    # -- voice plumbing ------------------------------------------------
    async def _join(self, client, name):
        guild = None
        for _ in range(60):
            guild = client.get_guild(self.guild_id)
            if guild is not None:
                break
            await asyncio.sleep(0.5)
        if guild is None:
            raise RuntimeError(f"{name}: guild {self.guild_id} not found")
        channel = guild.get_channel(self.channel_id)
        if channel is None:
            raise RuntimeError(f"{name}: channel {self.channel_id} not found")
        vc = await channel.connect(self_mute=True, self_deaf=False,
                                   reconnect=True, timeout=25)
        print(f"[mini] {name} joined {channel.name}")
        return vc

    async def run(self):
        try:
            await self._run()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            print(f"[mini] FAILED: {exc}")
        finally:
            try:
                await self.listen_client.close()
            except Exception:
                pass
            try:
                await self.speak_client.close()
            except Exception:
                pass

    async def _run(self):
        print("[mini] logging in ...")
        await asyncio.gather(
            self.listen_client.login(LISTEN_TOKEN),
            self.speak_client.login(SPEAKER_TOKEN))
        # login() only does the handshake - connect() runs the gateway and
        # fills the guild cache we need to find the voice channel
        asyncio.create_task(self.listen_client.connect())
        asyncio.create_task(self.speak_client.connect())
        await asyncio.gather(
            self.listen_client.wait_until_ready(),
            self.speak_client.wait_until_ready())
        print("[mini] logged in")

        vc_listen = await self._join(self.listen_client, LISTEN_NAME)

        # attach the app's own listener, and tap what it sends to the speaker
        listener = vl.VoiceListener(vc_listen)
        captured = []
        lock = threading.Lock()
        orig_feed = listener._speaker.feed

        def tap(pcm):
            with lock:
                captured.append(bytes(pcm))
            return orig_feed(pcm)

        listener._speaker.feed = tap
        ok = listener.start(timeout=10)
        print(f"[mini] listener started={ok}")
        if not ok:
            print("[mini] listener could not attach")
            return

        if MODE == "listen":
            for _ in range(30):
                await asyncio.sleep(2)
                print("[mini]", listener.stats())
            return

        vc_speak = await self._join(self.speak_client, SPEAK_NAME)
        src = voice_listen_source(TONE_WAV)
        if src is None:
            print("[mini] could not build the tone source")
            return
        vc_speak.play(src)
        print(f"[mini] playing {TONE_HZ:.0f} Hz tone")

        # let the pipeline settle, then record for a while
        await asyncio.sleep(1.0)
        with lock:
            captured.clear()
        for _ in range(12):
            await asyncio.sleep(2)
            st = listener.stats()
            with lock:
                n = len(captured)
            print(f"[mini] {st}  speaker_frames={n}")

        with lock:
            frames = list(captured)
        frames_to_wav(frames, CAPTURE_WAV)
        analyze_tone(frames)


def voice_listen_source(path):
    return build_audio_source(path)


def analyze_tone(frames):
    pcm = b"".join(frames)
    n = len(pcm) // 2
    if n == 0:
        print("[mini] !! nothing reached the speaker")
        return
    samples = array.array("h")
    samples.frombytes(pcm[:n * 2])
    # de-interleave: analyse the left channel only (stereo shares the tone)
    left = samples[0::2]
    # take a steady middle chunk (skip the first second)
    mid = left[48000:48000 + 48000 * 4]
    mono = [float(mid[i]) for i in range(0, len(mid), 2)]
    rms = math.sqrt(sum(v * v for v in mid) / max(1, len(mid)))
    print(f"[mini] wrote {CAPTURE_WAV}  frames={len(frames)}  "
          f"samples={n}  rms={rms:.0f}")
    cands = [110, 220, 330, 440, 550, 660, 880, 1200]
    mags = {f: goertzel(mid, 48000, f) for f in cands}
    best = max(mags, key=lambda k: mags[k])
    print("[mini] tone energy: " +
          "  ".join(f"{f:.0f}Hz={m / 1000:.0f}k" for f, m in sorted(mags.items())))
    if abs(best - TONE_HZ) < 1.0:
        print(f"[mini] RESULT: PASS - heard the {TONE_HZ:.0f} Hz tone "
              f"(mag={goertzel(mid, 48000, TONE_HZ):.0f})")
    else:
        print(f"[mini] !! dominant frequency is {best:.0f} Hz, not "
              f"{TONE_HZ:.0f} Hz - the audio was NOT clean")


# ----------------------------------------------------------------------
# entry point
# ----------------------------------------------------------------------
MODE = "listen"
LISTEN_TOKEN = ""
SPEAKER_TOKEN = ""
LISTEN_NAME = ""
SPEAK_NAME = ""


def main():
    global MODE, LISTEN_TOKEN, SPEAKER_TOKEN, LISTEN_NAME, SPEAK_NAME
    global GUILD_ID, CHANNEL_ID
    args = sys.argv[1:]
    MODE = args[0] if args and args[0] in ("listen", "tone", "analyze") else "listen"

    # optional --guild / --channel override (Socialize by default)
    for i, a in enumerate(args):
        if a == "--guild" and i + 1 < len(args):
            GUILD_ID = int(args[i + 1])
        elif a == "--channel" and i + 1 < len(args):
            CHANNEL_ID = int(args[i + 1])

    if MODE == "analyze":
        with wave.open(CAPTURE_WAV, "rb") as w:
            pcm = w.readframes(w.getnframes())
        print(f"[mini] analysing {CAPTURE_WAV} ({len(pcm) // 4} stereo frames)")
        analyze_tone([pcm])
        return

    accs = load_accounts()
    if MODE == "tone":
        make_tone(TONE_WAV, TONE_HZ, 25.0)

    by = {a.get("name", "").lower(): a for a in accs}
    listen = by.get("redbull") or accs[0]
    speak = next((a for a in accs if a is not listen), accs[0])
    LISTEN_TOKEN, LISTEN_NAME = listen["token"], listen.get("name", "?")
    SPEAKER_TOKEN, SPEAK_NAME = speak["token"], speak.get("name", "?")

    print(f"[mini] mode={MODE} listener={LISTEN_NAME} speaker={SPEAK_NAME} "
          f"guild={GUILD_ID} channel={CHANNEL_ID}")

    mini = MiniVoice(listen, speak, GUILD_ID, CHANNEL_ID)
    try:
        asyncio.run(mini.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
