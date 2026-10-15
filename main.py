"""
VoiceHive - Multi-Account Discord Voice Control
===============================================

A dark Tkinter panel for driving several Discord accounts at the same time.
Add them, join servers, drop them into voice channels, mute or deafen them one
by one or all at once.

Two more windows open from the top bar:

  Live Listen & Members - pick an account that is already sitting in a voice
  channel, see who else is in there, mute whoever you like, and hear the
  channel through your speakers.  The audio side lives in voice_listen.py.

  Route accounts to channels - spread the accounts over different servers and
  channels instead of dumping them all in the same room.

voice_listen.py does the voice receiving, mini_voice.py is a small standalone
client I use to test that pipeline without the GUI.

Needs Python 3.13 with discord.py-self installed (on Windows just double-click
run.bat). This is the v0.0.1-beta build, so expect a few rough edges.
"""

from __future__ import annotations

import os
import re
import sys
import json
import time
import uuid
import queue
import shutil
import base64
import asyncio
import subprocess
import threading
import traceback
import urllib.request
import webbrowser
import tkinter as tk
from tkinter import ttk, simpledialog, messagebox, filedialog
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

# ----------------------------------------------------------------------
# discord import (guard so the UI can still open and explain the problem)
# ----------------------------------------------------------------------
try:
    import discord
    DISCORD_OK = getattr(discord, "Client", None) is not None
    DISCORD_ERR = "" if DISCORD_OK else "discord.Client is missing (broken install)"
except Exception as exc:  # pragma: no cover
    discord = None
    DISCORD_OK = False
    DISCORD_ERR = str(exc)

# Discord voice now uses the DAVE (E2EE) protocol. Without the 'davey'
# package the client joins voice but gets kicked right away, which looks
# like an endless join/disconnect loop.
try:
    import davey as _davey  # noqa: F401
    DAVEY_OK = True
except Exception:
    DAVEY_OK = False

# Voice receiving (hear what the other members say). A separate module so
# the main file stays readable; it only uses what discord.py-self already
# installs.
try:
    import voice_listen
    LISTEN_OK = True
    LISTEN_ERR = ""
except Exception as exc:  # pragma: no cover
    voice_listen = None
    LISTEN_OK = False
    LISTEN_ERR = str(exc)


# ======================================================================
# Paths & theme
# ======================================================================
APP_DIR = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_FILE = os.path.join(APP_DIR, "accounts.json")

# Who this is and where the code lives (shown in the header + Dashboard).
PROJECT_NAME = "VoiceHive"
PROJECT_VERSION = "0.0.1-beta"
PROJECT_TAGLINE = "Multi-Account Discord Voice Control"
PROJECT_URL = "https://github.com/xavort/VoiceHive"

C_BG        = "#0f1115"
C_PANEL     = "#181b22"
C_PANEL2    = "#1f232c"
C_PANEL3    = "#2a2f3a"
C_BORDER    = "#262b36"
C_TXT       = "#e6e9ef"
C_MUTED     = "#8a93a6"
C_PRIMARY   = "#5865f2"
C_PRIMARY_H = "#4752c4"
C_DANGER    = "#ed4245"
C_DANGER_H  = "#c93b3e"
C_OK        = "#3ba55c"
C_OK_H      = "#2d7d46"
C_OFFLINE   = "#747f8d"
C_WARN      = "#faa61a"
C_WARN_H    = "#d68a12"
C_BOOST_BG  = "#3a2028"   # reddish panel so BOOST is clearly separate
C_BOOST_TXT = "#ff9aa2"
C_GH_BG     = "#24292f"   # GitHub's own dark button colour
C_GH_HOVER  = "#32383f"

FONT        = ("Segoe UI", 10)
FONT_BOLD   = ("Segoe UI", 10, "bold")
FONT_TITLE  = ("Segoe UI", 17, "bold")
FONT_H      = ("Segoe UI", 12, "bold")
FONT_SMALL  = ("Segoe UI", 9)


# ======================================================================
# Data model
# ======================================================================
@dataclass
class Account:
    id: str
    name: str
    token: str

    # runtime
    logged_in: bool = False
    tag: str = ""
    user_id: str = ""
    guilds: Dict[str, str] = field(default_factory=dict)
    error: str = ""

    # voice state
    voice_guild_id: Optional[str] = None
    voice_channel_id: Optional[str] = None
    voice_channel_name: str = ""
    voice_member_count: int = 0
    self_mute: bool = False
    self_deaf: bool = False
    playing: bool = False

    # auto rejoin
    auto_rejoin: bool = False
    last_intent_guild: Optional[str] = None
    last_intent_channel: Optional[str] = None
    last_intent_mute: bool = False
    last_intent_deaf: bool = False

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "token": self.token}


def load_accounts() -> List[Account]:
    if not os.path.exists(ACCOUNTS_FILE):
        return []
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as fh:
            txt = fh.read().strip()
        if not txt:
            return []
        data = json.loads(txt)
        out: List[Account] = []
        for d in data:
            acc = Account(
                id=str(d.get("id") or uuid.uuid4().hex[:8]),
                name=str(d.get("name") or "Account"),
                token=str(d.get("token") or "").strip(),
            )
            if acc.token:
                out.append(acc)
        return out
    except Exception as exc:
        print(f"[!] could not read accounts.json: {exc}")
        return []


def save_accounts(accounts: List[Account]) -> None:
    try:
        tmp = ACCOUNTS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump([a.to_dict() for a in accounts], fh, indent=2)
        os.replace(tmp, ACCOUNTS_FILE)
    except Exception as exc:
        print(f"[!] could not write accounts.json: {exc}")


def parse_id(text: str) -> str:
    """Pull a snowflake id out of 'Name (123456789)' or '123456789'."""
    if not text:
        return ""
    m = re.search(r"(\d{5,})", text)
    return m.group(1) if m else ""


# ----------------------------------------------------------------------
# Audio playback helpers
# ----------------------------------------------------------------------
_ffmpeg_cache: dict = {"checked": False, "path": None}

AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus",
              ".flac", ".wma", ".mp4", ".mkv", ".webm", ".mov", ".aiff")


def find_ffmpeg() -> Optional[str]:
    """Locate the ffmpeg binary (PATH, common folders, imageio-ffmpeg)."""
    if _ffmpeg_cache["checked"]:
        return _ffmpeg_cache["path"]
    _ffmpeg_cache["checked"] = True

    exe = shutil.which("ffmpeg")
    if exe:
        _ffmpeg_cache["path"] = exe
        return exe

    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local, "Microsoft", "WinGet", "Links", "ffmpeg.exe"),
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        os.path.join(APP_DIR, "ffmpeg.exe"),
        os.path.join(APP_DIR, "bin", "ffmpeg.exe"),
    ]
    for cand in candidates:
        if cand and os.path.isfile(cand):
            _ffmpeg_cache["path"] = cand
            return cand

    try:
        import imageio_ffmpeg  # type: ignore
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            _ffmpeg_cache["path"] = exe
            return exe
    except Exception:
        pass

    return None


_AudioSourceBase = discord.AudioSource if discord is not None else object


def _clamp16(value: int) -> int:
    if value > 32767:
        return 32767
    if value < -32768:
        return -32768
    return value


def _apply_gain(data: bytes, gain: float) -> bytes:
    """Scale 16-bit stereo PCM by ``gain`` with clipping.

    Standard library only (``audioop`` was removed from modern Python).
    ``gain`` above 1.0 makes the audio louder / clipped, which is exactly
    what the BOOST control does.
    """
    if not data or gain == 1.0:
        return data
    if gain <= 0.0:
        return bytes(len(data))
    import array
    samples = array.array("h")
    samples.frombytes(data)
    swap = sys.byteorder != "little"
    if swap:
        samples.byteswap()
    out = array.array("h", (_clamp16(int(s * gain)) for s in samples))
    if swap:
        out.byteswap()
    return out.tobytes()


class _GainSource(_AudioSourceBase):
    """Live ``volume`` (0..2) + ``boost`` (1..8) applied to any source.

    This is the *single* place the audio level is applied, so both the
    Volume slider and the separate BOOST slider act on a stream that is
    already playing, with no restart, and with no 2x gain cap.
    """

    def __init__(self, base, volume: float = 1.0, boost: float = 1.0) -> None:
        self.base = base
        self.volume = max(0.0, float(volume))
        self.boost = max(0.0, float(boost))

    @property
    def gain(self) -> float:
        return self.volume * self.boost

    def read(self) -> bytes:
        data = self.base.read()
        gain = self.gain
        if data and gain != 1.0:
            data = _apply_gain(data, gain)
        return data

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        try:
            self.base.cleanup()
        except Exception:
            pass


class _WavFileSource(_AudioSourceBase):
    """Minimal 48kHz 16-bit WAV reader (fallback when ffmpeg is absent)."""

    def __init__(self, path: str, loop: bool = False,
                 start: float = 0.0) -> None:
        import wave
        self._wave = wave.open(path, "rb")
        if self._wave.getsampwidth() != 2 or self._wave.getframerate() != 48000:
            self._wave.close()
            raise ValueError("WAV must be 16-bit / 48000 Hz (install ffmpeg)")
        self._channels = self._wave.getnchannels()
        self._loop = bool(loop)
        if start and start > 0.02:
            try:
                self._wave.setpos(int(start * 48000))
            except Exception:
                pass

    def read(self) -> bytes:
        import array
        frames = self._wave.readframes(960)
        if not frames:
            if not self._loop:
                return b""
            try:
                self._wave.rewind()
            except Exception:
                return b""
            frames = self._wave.readframes(960)
            if not frames:
                return b""
        if self._channels == 1:
            mono = array.array("h")
            mono.frombytes(frames)
            stereo = array.array("h")
            for sample in mono:
                stereo.append(sample)
                stereo.append(sample)
            return stereo.tobytes()
        return frames

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        try:
            self._wave.close()
        except Exception:
            pass


def has_audio_support() -> bool:
    return discord is not None and (find_ffmpeg() is not None)


_duration_cache: Dict[str, float] = {}


def probe_duration(path: str) -> float:
    """Length of a media file in seconds (0.0 when unknown). Cached."""
    if path in _duration_cache:
        return _duration_cache[path]
    duration = 0.0
    probe = shutil.which("ffprobe")
    if not probe:
        exe = find_ffmpeg()
        if exe:
            guess = os.path.join(os.path.dirname(exe), "ffprobe.exe")
            if os.path.isfile(guess):
                probe = guess
    if probe:
        try:
            res = subprocess.run(
                [probe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", path],
                capture_output=True, text=True, timeout=8)
            duration = float((res.stdout or "").strip() or 0.0)
        except Exception:
            duration = 0.0
    _duration_cache[path] = duration
    return duration


# ----------------------------------------------------------------------
# The single audio factory: seek + loop + volume + boost in one place
# ----------------------------------------------------------------------
def build_audio_source(path: str, volume: float = 1.0, boost: float = 1.0,
                       loop: bool = False, start: float = 0.0):
    """Build one :class:`discord.AudioSource` for ``path``.

    ``start``  - seconds to skip so every account plays the same part
    ``loop``   - restart the file when it ends
    ``volume`` - normal 0..2 level   (Volume control)
    ``boost``  - extra 1..8 gain     (BOOST control)

    Returns ``None`` when the file cannot be opened.
    """
    if discord is None or not path or not os.path.isfile(path):
        return None
    start = max(0.0, float(start))

    base = None
    exe = find_ffmpeg()
    if exe:
        parts = []
        if loop:
            parts.append("-stream_loop -1")
        if start > 0.05:
            parts.append(f"-ss {start:.3f}")
        before = " ".join(parts) if parts else None
        try:
            base = discord.FFmpegPCMAudio(path, executable=exe,
                                          before_options=before)
        except Exception as exc:
            print(f"[!] ffmpeg source failed: {exc}")
            base = None

    if base is None:
        try:
            base = _WavFileSource(path, loop=loop, start=start)
        except Exception:
            base = None

    if base is None:
        return None
    return _GainSource(base, volume, boost)


# ======================================================================
# Playback clock: knows which second of the song everyone should be on
# ======================================================================
class PlaybackSession:
    """Keeps every account on the same part of the same song.

    The song timeline keeps running while anyone is playing, so:
      * starting it on another account continues from the same second,
      * stopping everyone freezes it, and the next play continues there.
    """

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.path = ""
        self.base = 0.0
        self.anchor = time.monotonic()
        self.running = False
        self.duration = 0.0
        self.volume = 1.0
        self.boost = 1.0
        self.loop = False

    def position(self) -> float:
        with self.lock:
            if self.running:
                pos = self.base + (time.monotonic() - self.anchor)
            else:
                pos = self.base
            if self.loop and self.duration > 0.5:
                pos = pos % self.duration
            return max(0.0, pos)

    def load(self, path: str, volume: float, boost: float, loop: bool,
             start: float = 0.0) -> None:
        # Probe first: it can take a moment, and the clock must start only
        # when the fields below are set (otherwise accounts would seek ahead).
        duration = probe_duration(path)
        with self.lock:
            self.path = path
            self.base = max(0.0, float(start))
            self.volume = float(volume)
            self.boost = float(boost)
            self.loop = bool(loop)
            self.duration = duration
            self.anchor = time.monotonic()
            self.running = True

    def resume(self) -> None:
        with self.lock:
            if not self.running:
                self.anchor = time.monotonic()
                self.running = True

    def restart(self) -> None:
        with self.lock:
            self.base = 0.0
            self.anchor = time.monotonic()
            self.running = True

    def freeze(self) -> None:
        with self.lock:
            if self.running:
                self.base = self.position()
                self.anchor = time.monotonic()
                self.running = False

    def clear(self) -> None:
        with self.lock:
            self.path = ""
            self.base = 0.0
            self.running = False
            self.duration = 0.0



# ======================================================================
# Background async loop
# ======================================================================
class AsyncLoop:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, name="async-loop", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)


LOOP = AsyncLoop()


def make_bot():
    if discord is None:
        raise RuntimeError("discord library is not available")
    try:
        return discord.Client(self_bot=True)
    except TypeError:
        return discord.Client()


# ======================================================================
# Account worker (one per account)
# ======================================================================
class AccountWorker:
    def __init__(self, account: Account, on_change: Callable[[], None]) -> None:
        self.acc = account
        self.on_change = on_change
        self.bot = None
        self._busy = False           # a connect/disconnect is in progress
        self._closing = False
        self._source = None          # current audio source (for volume)
        self._playing_path = None    # file currently broadcasting (for retry)
        self._playing_volume = 1.0   # last requested volume
        self._playing_boost = 1.0    # last requested boost
        self._loop = False
        self._log_sig = None
        # Serialises voice operations for this account so a watchdog
        # rejoin can never race a manual join (which causes the
        # "Already connected to a voice channel" error).
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        LOOP.submit(self._run())

    async def _run(self) -> None:
        try:
            self.bot = make_bot()
            self.acc.bot = self.bot
            self._bind(self.bot)
            await self.bot.start(self.acc.token)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.acc.logged_in = False
            self.acc.error = self._friendly_error(exc)
            print(f"[!] {self.acc.name} login failed: {exc}")
            self.on_change()

    def stop(self) -> None:
        self._closing = True
        self.acc.auto_rejoin = False
        LOOP.submit(self._shutdown())

    async def _shutdown(self) -> None:
        bot = self.bot
        if bot is None:
            return
        try:
            for guild in list(bot.guilds):
                vc = guild.voice_client
                if vc is not None and vc.is_connected():
                    await self._safe_disconnect(vc)
            await bot.close()
        except Exception as exc:
            print(f"[!] close {self.acc.name}: {exc}")
        finally:
            self.acc.logged_in = False

    @staticmethod
    def _friendly_error(exc: Exception) -> str:
        txt = str(exc)
        low = txt.lower()
        if "captcha" in low:
            return "Captcha required - verify the account (phone/email)"
        if "unauthorized" in low or "401" in txt:
            return "Token invalid or expired"
        if "rate" in low or "429" in txt:
            return "Rate limited - wait a bit and reconnect"
        if "banned" in low:
            return "Banned from this guild"
        if "forbidden" in low or "403" in txt:
            return "Forbidden (no permission)"
        return txt[:200]

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------
    def _bind(self, bot) -> None:
        acc = self.acc

        @bot.event
        async def on_ready():
            acc.logged_in = True
            acc.tag = str(bot.user)
            acc.user_id = str(bot.user.id)
            acc.guilds = {str(g.id): g.name for g in bot.guilds}
            acc.error = ""
            print(f"[+] {acc.name} -> {acc.tag}")
            self.on_change()

        @bot.event
        async def on_guild_join(guild):
            acc.guilds[str(guild.id)] = guild.name
            self.on_change()

        @bot.event
        async def on_guild_remove(guild):
            acc.guilds.pop(str(guild.id), None)
            self.on_change()

        @bot.event
        async def on_voice_state_update(member, before, after):
            if bot.user is None:
                return
            if member.id != bot.user.id:
                # somebody entered/left the channel we sit in -> keep the
                # member count shown in the list fresh
                if acc.voice_channel_id:
                    for chan in (before.channel, after.channel):
                        g = getattr(chan, "guild", None)
                        if g is not None and str(g.id) == str(acc.voice_guild_id):
                            vc = g.get_channel(int(acc.voice_channel_id))
                            if vc is not None:
                                acc.voice_member_count = len(
                                    getattr(vc, "members", []) or [])
                                self.on_change()
                            break
                return

            if after.channel is not None:
                acc.voice_guild_id = str(after.channel.guild.id)
                acc.voice_channel_id = str(after.channel.id)
                acc.voice_channel_name = after.channel.name
                acc.voice_member_count = len(
                    getattr(after.channel, "members", []) or [])
                acc.self_mute = bool(after.self_mute)
                acc.self_deaf = bool(after.self_deaf)
                acc.error = ""
            else:
                acc.voice_guild_id = None
                acc.voice_channel_id = None
                acc.voice_channel_name = ""
                acc.self_mute = False
                acc.self_deaf = False
                if (acc.auto_rejoin and acc.last_intent_guild
                        and acc.last_intent_channel and not self._busy
                        and not self._closing):
                    asyncio.create_task(self._delayed_rejoin())

            self.on_change()

    async def _delayed_rejoin(self, delay: float = 2.0) -> None:
        await asyncio.sleep(delay)
        await self.rejoin_if_needed()

    # ------------------------------------------------------------------
    # voice actions (all must run inside LOOP)
    # ------------------------------------------------------------------
    async def connect_voice(self, guild_id: str, channel_id: str,
                            mute: bool, deaf: bool) -> None:
        async with self._lock:
            await self._connect_voice(guild_id, channel_id, mute, deaf)

    async def _connect_voice(self, guild_id: str, channel_id: str,
                             mute: bool, deaf: bool) -> None:
        acc = self.acc
        if self.bot is None:
            return
        acc.last_intent_guild = guild_id
        acc.last_intent_channel = channel_id
        acc.last_intent_mute = mute
        acc.last_intent_deaf = deaf

        self._busy = True
        channel = None
        try:
            try:
                guild = self.bot.get_guild(int(guild_id))
            except (TypeError, ValueError):
                guild = None
            if guild is None:
                acc.error = "Guild not found (is the account in it?)"
                return
            channel = guild.get_channel(int(channel_id))
            if channel is None:
                acc.error = "Voice channel not found"
                return

            vc = guild.voice_client
            already_there = (str(acc.voice_channel_id) == str(channel_id)
                             and str(acc.voice_guild_id) == str(guild_id))
            if vc is not None and vc.is_connected() and already_there:
                await self._apply(guild, channel, mute, deaf)
                acc.voice_channel_name = channel.name
                acc.voice_member_count = len(getattr(channel, "members", []) or [])
                acc.self_mute = mute
                acc.self_deaf = deaf
                acc.error = ""
                return

            # Fail fast (with a clear reason) for the two cases we can decide
            # locally - being denied Connect / View Channel, or the channel
            # being full - instead of waiting out a handshake before saying
            # anything useful.
            blocked = self._voice_blocked(channel)
            if blocked:
                acc.error = blocked
                print(f"[!] {acc.name} voice refused: {blocked}")
                return

            started = time.monotonic()

            # FAST PATH - already connected somewhere in this same guild:
            # Discord can simply move us to the new channel, which keeps the
            # same voice server, session and encryption keys, so it is nearly
            # instant.  `change_voice_state` just sends the voice-state update;
            # discord.py moves the voice client and rebinds the socket itself.
            # Disconnecting first and connecting from scratch is what made
            # joining feel slow.
            if (vc is not None and vc.is_connected()
                    and getattr(vc, "channel", None) is not None
                    and vc.channel.guild.id == guild.id):
                acc.error = "moving voice…"
                self._notify_safe()
                try:
                    await self._apply(guild, channel, mute, deaf)
                    await self._disconnect_other_guilds(guild)
                except Exception as exc:
                    print(f"[.] {acc.name} move failed "
                          f"({exc.__class__.__name__}), reconnecting…")
                else:
                    acc.error = ""
                    acc.voice_guild_id = str(guild.id)
                    acc.voice_channel_id = str(channel.id)
                    acc.voice_channel_name = channel.name
                    acc.voice_member_count = len(
                        getattr(channel, "members", []) or [])
                    acc.self_mute = mute
                    acc.self_deaf = deaf
                    print(f"[v] {acc.name} moved to {channel.name} "
                          f"({time.monotonic() - started:.1f}s)")
                    return

            # A user can only be in ONE voice channel at a time, so drop every
            # other voice connection first.  This also prevents the "Already
            # connected to a voice channel" error.
            had_voice = await self._disconnect_everywhere()
            if had_voice:
                # let Discord process the leave before asking to join again
                await asyncio.sleep(0.4)

            # Discord never answers when a join is refused, and a big E2EE
            # channel can genuinely need a while to negotiate - so try twice
            # with a generous window before reporting a failure.  (A really
            # private/full channel was already ruled out above.)
            connected = False
            last_exc: Optional[Exception] = None
            for attempt, conn_timeout in ((1, 20.0), (2, 35.0)):
                acc.error = "joining voice…"
                self._notify_safe()
                try:
                    await channel.connect(self_mute=mute, self_deaf=deaf,
                                          reconnect=True, timeout=conn_timeout)
                    connected = True
                    break
                except Exception as exc:
                    last_exc = exc
                    if "already" in str(exc).lower():
                        await self._disconnect_everywhere()
                        await asyncio.sleep(0.6)
                    if attempt < 2:
                        acc.error = ("joining voice… (retry, "
                                     f"{exc.__class__.__name__})")
                        self._notify_safe()
                        await asyncio.sleep(1.5)
                        continue
            if not connected:
                reason = self._voice_failure(channel, last_exc)
                acc.error = reason
                print(f"[!] {acc.name} voice failed: {reason}")
                return

            acc.error = ""
            acc.voice_guild_id = str(guild.id)
            acc.voice_channel_id = str(channel.id)
            acc.voice_channel_name = channel.name
            acc.voice_member_count = len(getattr(channel, "members", []) or [])
            acc.self_mute = mute
            acc.self_deaf = deaf
            print(f"[v] {acc.name} joined {channel.name} "
                  f"({time.monotonic() - started:.1f}s)")
        except Exception as exc:
            reason = self._voice_failure(channel, exc)
            acc.error = reason
            print(f"[!] {acc.name} voice failed: {reason}")
            try:
                traceback.print_exc()
            except Exception:
                pass
        finally:
            self._busy = False
            self.on_change()

    # -- voice failure helpers ---------------------------------------
    def _voice_blocked(self, channel) -> str:
        """Return a human reason when we already know joining cannot work.

        Discord does not send an error when it refuses to move us into a voice
        channel - the handshake simply never completes - so we look at the
        channel itself (permissions and the user limit) to say *why* instead of
        just timing out with a cryptic message.
        """
        if channel is None:
            return ""
        guild = getattr(channel, "guild", None)
        me = getattr(guild, "me", None)
        try:
            perms = channel.permissions_for(me)
        except Exception:
            perms = None
        if perms is not None:
            if not getattr(perms, "view_channel", True):
                return "Cannot join: this voice channel is hidden (no View Channel permission)"
            if not getattr(perms, "connect", True):
                return "Cannot join: no Connect permission for this voice channel"
        try:
            limit = int(getattr(channel, "user_limit", 0) or 0)
        except (TypeError, ValueError):
            limit = 0
        me_id = getattr(me, "id", None)
        members = [m for m in (getattr(channel, "members", None) or [])
                   if getattr(m, "id", None) != me_id]
        if limit and len(members) >= limit:
            return (f"Cannot join: channel is full "
                    f"({len(members)}/{limit} users)")
        return ""

    def _voice_failure(self, channel, exc) -> str:
        """Explain a voice-connect failure in plain language."""
        text = str(exc).strip()
        low = text.lower()
        name = getattr(channel, "name", None) or "the voice channel"

        if channel is not None:
            blocked = self._voice_blocked(channel)
            if blocked:
                return blocked

        if isinstance(exc, asyncio.TimeoutError) or "timeout" in low:
            return (f"Cannot join {name}: Discord never let us in "
                    "(private channel, full, or you are blocked)")
        if isinstance(exc, getattr(discord, "Forbidden", ())) or "403" in text \
                or "forbidden" in low:
            return "Cannot join: no permission for this voice channel"
        if "full" in low or "user limit" in low:
            return "Cannot join: voice channel is full"
        if "already connected" in low:
            return "Already connected to another voice channel"
        if "not found" in low:
            return "Voice channel not found (were you removed from the server?)"
        if "closed" in low or "connection" in low or "websocket" in low:
            return f"Voice connection dropped ({text or 'connection closed'})"
        return f"Voice error: {text or exc.__class__.__name__}"

    async def _disconnect_everywhere(self, exclude_guild=None) -> bool:
        """Drop every voice connection for this account (one channel max).

        Returns True when something was actually disconnected.  Each disconnect
        is capped with a short wait so a slow gateway can never stall a join for
        the whole (30s+) connection timeout.
        """
        if self.bot is None:
            return False
        keep = getattr(exclude_guild, "id", None)
        had_voice = False
        for guild in list(self.bot.guilds):
            if exclude_guild is not None and guild.id == keep:
                continue
            vc = guild.voice_client
            if vc is None:
                continue
            await self._safe_disconnect(vc)
            had_voice = True
        return had_voice

    async def _disconnect_other_guilds(self, keep_guild) -> None:
        """Drop voice connections in every guild except ``keep_guild``."""
        await self._disconnect_everywhere(exclude_guild=keep_guild)

    async def _safe_disconnect(self, vc) -> None:
        """Disconnect one voice client without blocking on a slow gateway."""
        conn = getattr(vc, "_connection", None)
        if conn is not None:
            try:
                # VoiceConnectionState.disconnect() waits up to ``timeout``
                # for the confirming voice_state_update; shorten it so a
                # silent gateway cannot hold the join for half a minute.
                conn.timeout = min(5.0, float(conn.timeout or 5.0))
            except Exception:
                pass
        try:
            await asyncio.wait_for(vc.disconnect(force=True), timeout=8.0)
        except Exception:
            pass

    def _notify_safe(self) -> None:
        """Refresh the UI without ever letting a callback error escape."""
        try:
            self.on_change()
        except Exception:
            pass

    async def _apply(self, guild, channel, mute: bool, deaf: bool) -> None:
        await guild.change_voice_state(channel=channel,
                                       self_mute=bool(mute),
                                       self_deaf=bool(deaf))

    async def apply_state(self, mute: Optional[bool], deaf: Optional[bool]) -> None:
        acc = self.acc
        if self.bot is None:
            return
        async with self._lock:
            new_mute = acc.self_mute if mute is None else bool(mute)
            new_deaf = acc.self_deaf if deaf is None else bool(deaf)
            changed = False
            for guild in list(self.bot.guilds):
                vc = guild.voice_client
                if vc is None or not vc.is_connected():
                    continue
                cid = acc.voice_channel_id
                try:
                    channel = guild.get_channel(int(cid)) if cid else None
                except (TypeError, ValueError):
                    channel = None
                if channel is None:
                    continue
                try:
                    await self._apply(guild, channel, new_mute, new_deaf)
                    changed = True
                except Exception as exc:
                    acc.error = f"State: {exc}"
            if changed:
                acc.self_mute = new_mute
                acc.self_deaf = new_deaf
        self.on_change()

    async def leave_voice(self) -> None:
        acc = self.acc
        acc.auto_rejoin = False
        acc.last_intent_guild = None
        acc.last_intent_channel = None
        if self.bot is None:
            return
        async with self._lock:
            self._busy = True
            try:
                self.stop_playback()
                await self._disconnect_everywhere()
            finally:
                self._busy = False
        acc.voice_guild_id = None
        acc.voice_channel_id = None
        acc.voice_channel_name = ""
        acc.voice_member_count = 0
        acc.self_mute = False
        acc.self_deaf = False
        self.on_change()

    async def rejoin_if_needed(self) -> None:
        acc = self.acc
        if not acc.auto_rejoin or self._busy or self._closing or self.bot is None:
            return
        if not acc.last_intent_guild or not acc.last_intent_channel:
            return
        try:
            guild = self.bot.get_guild(int(acc.last_intent_guild))
        except (TypeError, ValueError):
            guild = None
        if guild is None:
            return
        vc = guild.voice_client
        if vc is not None and vc.is_connected():
            return
        await self.connect_voice(acc.last_intent_guild, acc.last_intent_channel,
                                 acc.last_intent_mute, acc.last_intent_deaf)

    async def join_invite(self, invite: str) -> None:
        acc = self.acc
        if self.bot is None:
            return
        code = invite.strip().rstrip("/").split("/")[-1]
        try:
            await self.bot.accept_invite(code)
            acc.error = ""
            acc.guilds = {str(g.id): g.name for g in self.bot.guilds}
            print(f"[+] {acc.name} joined invite {code}")
        except Exception as exc:
            acc.error = f"Join: {self._friendly_error(exc)}"
            print(f"[!] {acc.name} join failed: {exc}")
        self.on_change()

    # ------------------------------------------------------------------
    # audio playback
    # ------------------------------------------------------------------
    def _voice_client(self):
        if self.bot is None:
            return None
        for guild in list(self.bot.guilds):
            try:
                vc = guild.voice_client
            except Exception:
                vc = None
            if vc is not None and vc.is_connected():
                return vc
        return None

    def voice_client(self):
        """Public accessor used by the listen / members window."""
        return self._voice_client()

    def build_playback(self, path: str, volume: float = 1.0,
                       boost: float = 1.0, loop: bool = False,
                       start: float = 0.0):
        """Create (but don't start) an audio source if in voice."""
        if discord is None or self._voice_client() is None:
            return None
        self._loop = bool(loop)
        return build_audio_source(path, volume, boost, loop=loop, start=start)

    def start_playback(self, source, path=None, volume: float = 1.0,
                       boost: float = 1.0) -> bool:
        acc = self.acc
        vc = self._voice_client()
        if vc is None or source is None:
            return False
        try:
            if vc.is_playing():
                vc.stop()
            self._source = source
            self._playing_path = path
            self._playing_volume = max(0.0, float(volume))
            self._playing_boost = max(0.0, float(boost))
            vc.play(source, after=lambda _e: self._after_play())
            acc.playing = True
            acc.error = ""
            return True
        except Exception as exc:
            acc.playing = False
            self._source = None
            acc.error = f"Play: {exc}"
            print(f"[!] {acc.name} playback: {exc}")
            return False

    def _log_level(self) -> None:
        sig = (int(round(self._playing_volume * 100)),
               int(round(self._playing_boost * 100)))
        if getattr(self, "_log_sig", None) == sig:
            return
        self._log_sig = sig
        tag = " (live)" if self._source is not None else ""
        print(f"[vol] {self.acc.name}: volume {sig[0]}%  "
              f"boost {sig[1]}%{tag}")

    def set_volume(self, volume: float) -> None:
        """Change the live Volume of this account's stream."""
        self._playing_volume = max(0.0, float(volume))
        self._log_level()
        src = self._source
        if src is None:
            return
        try:
            src.volume = self._playing_volume
        except Exception as exc:
            print(f"[!] {self.acc.name} volume: {exc}")

    def set_boost(self, boost: float) -> None:
        """Change the live BOOST (extra gain) of this account's stream."""
        self._playing_boost = max(0.0, float(boost))
        self._log_level()
        src = self._source
        if src is None:
            return
        try:
            src.boost = self._playing_boost
        except Exception as exc:
            print(f"[!] {self.acc.name} boost: {exc}")

    def stop_playback(self) -> None:
        acc = self.acc
        acc.playing = False
        self._source = None
        vc = self._voice_client()
        if vc is None:
            return
        try:
            if vc.is_playing():
                vc.stop()
        except Exception:
            pass

    def _after_play(self) -> None:
        self.acc.playing = False
        try:
            self.on_change()
        except Exception:
            pass


# ======================================================================
# Controller
# ======================================================================
class Controller:
    def __init__(self, on_change: Callable[[], None]) -> None:
        self.accounts: List[Account] = load_accounts()
        self.workers: Dict[str, AccountWorker] = {}
        self.on_change = on_change
        self._stop = False
        self._join_lock: Optional[asyncio.Lock] = None
        self.session = PlaybackSession()
        threading.Thread(target=self._watchdog, name="watchdog", daemon=True).start()

    # ---------------- lookups ----------------
    def find(self, acc_id: str) -> Optional[Account]:
        return next((a for a in self.accounts if a.id == acc_id), None)

    def voice_channels(self, guild_id: str) -> List[Tuple[str, str]]:
        out: Dict[str, str] = {}
        try:
            gid = int(guild_id)
        except (TypeError, ValueError):
            return []
        for w in list(self.workers.values()):
            bot = w.bot
            if bot is None:
                continue
            try:
                guild = bot.get_guild(gid)
            except Exception:
                continue
            if guild is None:
                continue
            for ch in list(getattr(guild, "voice_channels", [])):
                out[str(ch.id)] = ch.name
        return sorted(out.items(), key=lambda kv: kv[1].lower())

    def known_guilds(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for a in self.accounts:
            for gid, name in a.guilds.items():
                out.setdefault(gid, name)
        return out

    # ---------------- account mgmt ----------------
    def add_account(self, name: str, token: str) -> Account:
        acc = Account(id=uuid.uuid4().hex[:8], name=name, token=token)
        self.accounts.append(acc)
        save_accounts(self.accounts)
        self.start_account(acc.id)
        return acc

    def update_account(self, acc_id: str, name: str, token: str) -> None:
        acc = self.find(acc_id)
        if not acc:
            return
        self.stop_account(acc_id)
        acc.name = name
        acc.token = token
        acc.error = ""
        save_accounts(self.accounts)
        self.start_account(acc_id)

    def delete_account(self, acc_id: str) -> None:
        self.stop_account(acc_id)
        self.accounts = [a for a in self.accounts if a.id != acc_id]
        save_accounts(self.accounts)

    def start_account(self, acc_id: str) -> None:
        acc = self.find(acc_id)
        if not acc or acc_id in self.workers:
            return
        try:
            w = AccountWorker(acc, self.on_change)
            self.workers[acc_id] = w
            w.start()
        except Exception as exc:
            acc.error = f"Start failed: {exc}"
            print(f"[!] start {acc.name}: {exc}")
            self.on_change()

    def stop_account(self, acc_id: str) -> None:
        w = self.workers.pop(acc_id, None)
        if w:
            w.stop()
        acc = self.find(acc_id)
        if acc:
            acc.logged_in = False
            acc.voice_channel_id = None
            acc.voice_guild_id = None
            acc.voice_channel_name = ""

    def start_all(self) -> None:
        LOOP.submit(self._start_all_staggered())

    async def _start_all_staggered(self) -> None:
        # Log accounts in one after another instead of all at the same
        # instant - hitting Discord with N gateways at once gets them
        # rate limited / dropped.
        for a in list(self.accounts):
            self.start_account(a.id)
            await asyncio.sleep(1.2)

    def stop_all(self) -> None:
        for aid in list(self.workers.keys()):
            self.stop_account(aid)

    def shutdown(self) -> None:
        self._stop = True
        self.stop_all()

    # ---------------- invites ----------------
    def join_invite(self, acc_ids: List[str], invite: str) -> None:
        LOOP.submit(self._join_invite_many(list(acc_ids), invite))

    async def _join_invite_many(self, acc_ids: List[str], invite: str) -> None:
        async def one(i, aid):
            await asyncio.sleep(i * 0.6)   # tiny stagger, then all together
            w = self.workers.get(aid)
            if w is not None:
                await w.join_invite(invite)
        await asyncio.gather(*(one(i, aid) for i, aid in enumerate(acc_ids)))

    # ---------------- voice ----------------
    def join_voice(self, acc_ids: List[str], guild_id: str, channel_id: str,
                   mute: bool = False, deaf: bool = False,
                   auto_rejoin: bool = True) -> None:
        LOOP.submit(self._join_voice_many(list(acc_ids), guild_id, channel_id,
                                          mute, deaf, auto_rejoin))

    async def _join_voice_many(self, acc_ids, guild_id, channel_id,
                               mute, deaf, auto_rejoin) -> None:
        if self._join_lock is None:
            self._join_lock = asyncio.Lock()

        async def one(i, aid):
            await asyncio.sleep(i * 0.25)  # tiny stagger, then all together
            w = self.workers.get(aid)
            if w is None:
                return
            acc = self.find(aid)
            if acc is not None:
                acc.auto_rejoin = auto_rejoin
            await w.connect_voice(guild_id, channel_id, mute, deaf)

        async with self._join_lock:
            await asyncio.gather(*(one(i, aid) for i, aid in enumerate(acc_ids)))

    def leave_voice(self, acc_ids: List[str]) -> None:
        LOOP.submit(self._leave_voice_many(list(acc_ids)))

    async def _leave_voice_many(self, acc_ids) -> None:
        async def one(aid):
            w = self.workers.get(aid)
            if w is not None:
                await w.leave_voice()
        await asyncio.gather(*(one(aid) for aid in acc_ids))

    def set_voice_state(self, acc_ids: List[str],
                        mute: Optional[bool] = None,
                        deaf: Optional[bool] = None) -> None:
        LOOP.submit(self._set_state_many(list(acc_ids), mute, deaf))

    async def _set_state_many(self, acc_ids, mute, deaf) -> None:
        async def one(aid):
            w = self.workers.get(aid)
            if w is not None:
                await w.apply_state(mute, deaf)
        await asyncio.gather(*(one(aid) for aid in acc_ids))

    def move_account(self, acc_id: str, guild_id: str, channel_id: str) -> None:
        """Send one account to a (possibly different) voice channel."""
        acc = self.find(acc_id)
        mute = acc.self_mute if acc else False
        deaf = acc.self_deaf if acc else False
        self.join_voice([acc_id], guild_id, channel_id, mute, deaf, True)

    def dispatch_route(self, intents: List[Tuple[str, str, str]]) -> None:
        """Scatter accounts: each item is (acc_id, guild_id, channel_id)."""
        LOOP.submit(self._dispatch_route(list(intents)))

    async def _dispatch_route(self, intents) -> None:
        if self._join_lock is None:
            self._join_lock = asyncio.Lock()

        async def one(i, aid, gid, cid):
            await asyncio.sleep(i * 0.5)
            w = self.workers.get(aid)
            if w is None:
                return
            acc = self.find(aid)
            mute = acc.self_mute if acc else False
            deaf = acc.self_deaf if acc else False
            if acc is not None:
                acc.auto_rejoin = True
            await w.connect_voice(gid, cid, mute, deaf)

        async with self._join_lock:
            await asyncio.gather(*(one(i, a, g, c)
                                   for i, (a, g, c) in enumerate(intents)))

    # ---------------- audio playback ----------------
    def play_song(self, acc_ids, path: str, volume: float, boost: float,
                  loop: bool, restart: bool = False) -> None:
        """Play ``path`` on ``acc_ids``, keeping everyone on the same part.

        If the same file is already loaded, the accounts start where the
        song currently is (nothing restarts from the top). ``restart``
        forces it back to 0:00.
        """
        sess = self.session
        with sess.lock:
            same = (path == sess.path)
            if restart or not same:
                sess.load(path, volume, boost, loop, start=0.0)
            else:
                sess.resume()
                sess.volume = float(volume)
                sess.boost = float(boost)
                sess.loop = bool(loop)
            start = sess.position()
            # the track already finished -> start it over instead of
            # seeking past the end into silence
            if sess.duration > 1.0 and start >= sess.duration - 0.4:
                sess.restart()
                start = 0.0
            vol, bst, lp = sess.volume, sess.boost, sess.loop
            song = sess.path
        LOOP.submit(self._play_many(list(acc_ids) if acc_ids else None,
                                    song, vol, bst, lp, start))

    async def _play_many(self, acc_ids, path, volume, boost, loop,
                         start: float = 0.0) -> None:
        if not acc_ids:
            acc_ids = [a.id for a in self.accounts if a.voice_channel_id]
        # 1) build every source first (this spawns ffmpeg)
        sources = []
        for aid in acc_ids:
            w = self.workers.get(aid)
            if w is None:
                continue
            src = w.build_playback(path, volume, boost, loop=loop, start=start)
            if src is not None:
                sources.append((w, src))
        # 2) start them in one tight loop -> all accounts start together
        started = 0
        for w, src in sources:
            if w.start_playback(src, path, volume, boost):
                started += 1
        print(f"[♪] '{os.path.basename(path)}' @ {start:0.1f}s on "
              f"{started}/{len(acc_ids)} account(s) "
              f"(vol {int(volume * 100)}% boost {int(boost * 100)}%)")
        try:
            self.on_change()
        except Exception:
            pass

    def stop_audio(self, acc_ids: Optional[List[str]] = None) -> None:
        LOOP.submit(self._stop_many(list(acc_ids) if acc_ids else None))

    async def _stop_many(self, acc_ids) -> None:
        if not acc_ids:
            acc_ids = [a.id for a in self.accounts if a.voice_channel_id]
        for aid in acc_ids:
            w = self.workers.get(aid)
            if w is not None:
                w.stop_playback()
        # nobody is playing any more -> remember the spot so the next play
        # continues from here instead of starting over
        await asyncio.sleep(0.1)
        if not any(getattr(a, "playing", False) for a in self.accounts):
            self.session.freeze()
        try:
            self.on_change()
        except Exception:
            pass

    def set_playback_volume(self, volume: float,
                            acc_ids: Optional[List[str]] = None) -> None:
        self.session.volume = max(0.0, float(volume))
        LOOP.submit(self._volume_many(list(acc_ids) if acc_ids else None,
                                      volume))

    async def _volume_many(self, acc_ids, volume: float) -> None:
        if not acc_ids:
            acc_ids = [a.id for a in self.accounts]
        for aid in acc_ids:
            w = self.workers.get(aid)
            if w is not None:
                w.set_volume(volume)

    def set_playback_boost(self, boost: float,
                           acc_ids: Optional[List[str]] = None) -> None:
        self.session.boost = max(0.0, float(boost))
        LOOP.submit(self._boost_many(list(acc_ids) if acc_ids else None, boost))

    async def _boost_many(self, acc_ids, boost: float) -> None:
        if not acc_ids:
            acc_ids = [a.id for a in self.accounts]
        for aid in acc_ids:
            w = self.workers.get(aid)
            if w is not None:
                w.set_boost(boost)

    def seek_relative(self, delta: float,
                      acc_ids: Optional[List[str]] = None) -> None:
        """Jump ``delta`` seconds (negative = back) and keep playing."""
        sess = self.session
        with sess.lock:
            if not sess.path:
                return
            pos = max(0.0, sess.position() + float(delta))
            duration = sess.duration
            if duration > 1.0 and pos >= duration - 0.3:
                pos = 0.0 if sess.loop else max(0.0, duration - 1.0)
            sess.load(sess.path, sess.volume, sess.boost, sess.loop, start=pos)
            song, vol, bst, lp = sess.path, sess.volume, sess.boost, sess.loop
        LOOP.submit(self._play_many(list(acc_ids) if acc_ids else None,
                                    song, vol, bst, lp, pos))

    # ---------------- watchdog ----------------
    def _watchdog(self) -> None:
        import time
        while not self._stop:
            time.sleep(4)
            if self._stop:
                break
            for acc in list(self.accounts):
                if not acc.auto_rejoin or not acc.logged_in:
                    continue
                if not acc.last_intent_guild or not acc.last_intent_channel:
                    continue
                w = self.workers.get(acc.id)
                if w is not None:
                    LOOP.submit(w.rejoin_if_needed())


# ======================================================================
# Small UI toolkit
# ======================================================================
class PillButton(tk.Button):
    def __init__(self, parent, command, padx: int = 12, pady: int = 5,
                 width: Optional[int] = None, font=FONT_BOLD):
        super().__init__(parent, command=command, relief="flat", bd=0,
                         font=font, cursor="hand2", highlightthickness=0,
                         padx=padx, pady=pady)
        self._bg = C_PANEL3
        self._hover = C_PANEL3
        self._enabled = True
        if width:
            self.configure(width=width)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)

    def _enter(self, _e):
        if self._enabled:
            self.configure(bg=self._hover)

    def _leave(self, _e):
        if self._enabled:
            self.configure(bg=self._bg)

    def style(self, text: str, bg: str = C_PANEL3, hover: Optional[str] = None,
              fg: str = C_TXT, enabled: bool = True) -> None:
        self._bg = bg
        self._hover = hover or bg
        self._enabled = enabled
        self.configure(
            text=text,
            fg=fg if enabled else C_MUTED,
            disabledforeground=C_MUTED,
            bg=bg if enabled else C_PANEL,
            activebackground=self._hover,
            activeforeground=fg,
            state="normal" if enabled else "disabled",
            cursor="hand2" if enabled else "arrow",
        )


def flat_btn(parent, text, cmd, bg=C_PANEL3, fg=C_TXT, hover=None,
             padx: int = 12, pady: int = 5):
    b = PillButton(parent, cmd, padx=padx, pady=pady)
    b.style(text, bg=bg, hover=hover or bg, fg=fg)
    return b


def card(parent, **kw):
    return tk.Frame(parent, bg=C_PANEL, highlightbackground=C_BORDER,
                    highlightthickness=1, **kw)


class ScrollArea(tk.Frame):
    """A vertically scrollable area with an inner frame children go into."""

    def __init__(self, parent, bg=C_PANEL):
        super().__init__(parent, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vsb.pack(side="right", fill="y")
        self.canvas.bind("<Enter>", lambda _e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda _e: self.canvas.unbind_all("<MouseWheel>"))

    def _on_inner(self, _e):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, e):
        self.canvas.itemconfigure(self._win, width=e.width)

    def _wheel(self, e):
        self.canvas.yview_scroll(int(-e.delta / 120), "units")


@dataclass
class RowAction:
    key: str
    text: Callable[[Account], str]
    command: Callable[[str], None]
    style: Optional[Callable[[Account], Tuple[str, Optional[str], str, bool]]] = None
    width: Optional[int] = None


class AccountRow:
    """One account = one row with live status + action buttons."""

    def __init__(self, parent, app, tab_key: str, acc_id: str,
                 actions_builder: Callable[["AccountRow"], List[RowAction]]):
        self.app = app
        self.tab_key = tab_key
        self.acc_id = acc_id
        self.buttons: List[Tuple[RowAction, PillButton]] = []

        self.frame = tk.Frame(parent, bg=C_PANEL2, highlightbackground=C_BORDER,
                              highlightthickness=1)
        self.sel_btn = PillButton(self.frame, lambda: app.toggle_selected(tab_key, acc_id),
                                  padx=6, pady=3, width=3)
        self.sel_btn.pack(side="left", padx=(6, 6))

        self.dot = tk.Label(self.frame, text="⚪", bg=C_PANEL2, fg=C_OFFLINE, font=FONT)
        self.dot.pack(side="left")

        self.name = tk.Label(self.frame, text="", bg=C_PANEL2, fg=C_TXT,
                             font=FONT_BOLD, width=16, anchor="w")
        self.name.pack(side="left", padx=(6, 4))

        self.tag = tk.Label(self.frame, text="", bg=C_PANEL2, fg=C_MUTED,
                            font=FONT_SMALL, width=18, anchor="w")
        self.tag.pack(side="left")

        self.actions = tk.Frame(self.frame, bg=C_PANEL2)
        self.actions.pack(side="right", padx=6, pady=4)

        self.status = tk.Label(self.frame, text="", bg=C_PANEL2, fg=C_MUTED,
                               font=FONT_SMALL, anchor="w")
        self.status.pack(side="left", fill="x", expand=True, padx=(6, 6))

        for act in actions_builder(self):
            btn = PillButton(self.actions, lambda a=act: a.command(acc_id),
                             padx=10, pady=3, width=act.width)
            btn.pack(side="left", padx=3)
            self.buttons.append((act, btn))

    def update(self, acc: Account, selected: bool) -> None:
        # selection chip
        self.sel_btn.style("☑" if selected else "☐",
                           bg=C_PRIMARY if selected else C_PANEL3,
                           hover=C_PRIMARY_H if selected else C_PANEL3,
                           fg="#ffffff" if selected else C_MUTED)

        online = acc.logged_in
        in_voice = bool(acc.voice_channel_id)

        if online:
            self.dot.configure(text="🟢", fg=C_OK)
            self.frame.configure(highlightbackground=C_OK if in_voice else C_BORDER)
        elif acc.error:
            self.dot.configure(text="🔴", fg=C_DANGER)
            self.frame.configure(highlightbackground=C_DANGER)
        else:
            self.dot.configure(text="⚪", fg=C_OFFLINE)
            self.frame.configure(highlightbackground=C_BORDER)

        self.name.configure(text=acc.name or "?")
        self.tag.configure(text=(acc.tag if online else "offline"))

        parts: List[str] = ["Online" if online else "Offline"]
        if in_voice:
            parts.append(f"🔊 {acc.voice_channel_name or 'voice'}")
            if acc.voice_member_count:
                parts.append(f"👥 {acc.voice_member_count}")
            parts.append("🔇 muted" if acc.self_mute else "🎙️ unmuted")
            parts.append("🔈 deaf" if acc.self_deaf else "🔊 audio on")
            if acc.playing:
                parts.append("♪ playing")
        if acc.error:
            parts.append(f"⚠️ {acc.error}")
        self.status.configure(text="  ·  ".join(parts))

        for act, btn in self.buttons:
            if act.style is not None:
                bg, hover, fg, enabled = act.style(acc)
            else:
                bg, hover, fg, enabled = C_PANEL3, C_PANEL3, C_TXT, True
            btn.style(act.text(acc), bg=bg, hover=hover, fg=fg, enabled=enabled)

    def destroy(self) -> None:
        self.frame.destroy()


class AccountList:
    """Keeps a live list of AccountRow widgets in sync with the model."""

    def __init__(self, parent, app, tab_key: str,
                 actions_builder: Callable[[AccountRow], List[RowAction]],
                 empty_text: str = "No accounts yet"):
        self.app = app
        self.tab_key = tab_key
        self.actions_builder = actions_builder
        self.rows: Dict[str, AccountRow] = {}
        self._order: List[str] = []

        self.wrap = tk.Frame(parent, bg=C_PANEL)
        self.scroll = ScrollArea(self.wrap, bg=C_PANEL)
        self.scroll.pack(fill="both", expand=True)
        self.empty = tk.Label(self.wrap, text=empty_text, bg=C_PANEL,
                              fg=C_MUTED, font=FONT)
        self._empty_shown = False

    def widget(self) -> tk.Frame:
        return self.wrap

    def _show_empty(self, show: bool) -> None:
        if show == self._empty_shown:
            return
        self._empty_shown = show
        if show:
            self.scroll.pack_forget()
            self.empty.pack(pady=24)
        else:
            self.empty.pack_forget()
            self.scroll.pack(fill="both", expand=True)

    def sync(self, accounts: List[Account]) -> None:
        ids = [a.id for a in accounts]
        self._show_empty(not accounts)

        for acc in accounts:
            row = self.rows.get(acc.id)
            if row is None:
                row = AccountRow(self.scroll.inner, self.app, self.tab_key,
                                 acc.id, self.actions_builder)
                self.rows[acc.id] = row
            row.update(acc, self.app.is_selected(self.tab_key, acc.id))

        for aid in list(self.rows.keys()):
            if aid not in ids:
                self.rows.pop(aid).destroy()

        if ids != self._order:
            self._order = ids
            for aid in ids:
                self.rows[aid].frame.pack(fill="x", padx=4, pady=3)


# ======================================================================
# Application
# ======================================================================
class App(tk.Tk):
    REFRESH_MS = 500

    def __init__(self) -> None:
        super().__init__()
        self.title(f"VoiceHive {PROJECT_VERSION}")
        self.geometry("1300x820")
        self.minsize(1080, 640)
        self.configure(bg=C_BG)

        self.event_q: "queue.Queue[bool]" = queue.Queue()
        self.selection: Dict[str, set] = {
            "dash": set(), "accounts": set(), "join": set(), "voice": set(),
        }
        self._chan_values_all: List[str] = []
        self._monitor_win = None
        self._routing_win = None
        self.auto_listen = tk.BooleanVar(value=True)
        self._auto_listened: set = set()
        self.controller = Controller(on_change=self._notify)

        self._build_style()
        self._build_header()
        self._build_warnings()
        self._build_tabs()

        if os.environ.get("DISVBC_NO_AUTOSTART") != "1":
            self.controller.start_all()

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._tick()

    # ------------------------------------------------------------------
    def _notify(self) -> None:
        try:
            self.event_q.put(True)
        except Exception:
            pass

    def _tick(self) -> None:
        try:
            while True:
                self.event_q.get_nowait()
        except queue.Empty:
            pass
        try:
            self.refresh()
        except Exception as exc:
            print(f"[!] refresh error: {exc}")
        self.after(self.REFRESH_MS, self._tick)

    # ------------------------------------------------------------------
    # styling
    # ------------------------------------------------------------------
    def _build_style(self) -> None:
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except Exception:
            pass
        s.configure("TNotebook", background=C_BG, borderwidth=0)
        s.configure("TNotebook.Tab", background=C_PANEL, foreground=C_MUTED,
                    padding=[18, 9], borderwidth=0, font=FONT_BOLD)
        s.map("TNotebook.Tab",
              background=[("selected", C_PRIMARY)],
              foreground=[("selected", "#ffffff")])
        s.configure("Vertical.TScrollbar", background=C_PANEL3, troughcolor=C_PANEL,
                    bordercolor=C_PANEL, arrowcolor=C_MUTED)
        s.configure("Dark.TCombobox",
                    fieldbackground=C_PANEL2, background=C_PANEL2,
                    foreground=C_TXT, arrowcolor=C_TXT,
                    bordercolor=C_BORDER, lightcolor=C_PANEL2, darkcolor=C_PANEL2)
        s.map("Dark.TCombobox",
              fieldbackground=[("readonly", C_PANEL2)],
              foreground=[("readonly", C_TXT)])
        self.option_add("*TCombobox*Listbox.background", C_PANEL2)
        self.option_add("*TCombobox*Listbox.foreground", C_TXT)
        self.option_add("*TCombobox*Listbox.selectBackground", C_PRIMARY)
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")

    def _build_header(self) -> None:
        h = tk.Frame(self, bg=C_BG, height=62)
        h.pack(fill="x", side="top")
        h.pack_propagate(False)
        tk.Label(h, text="🐝  VoiceHive", bg=C_BG, fg=C_TXT,
                 font=FONT_TITLE).pack(side="left", padx=(20, 8))
        tk.Label(h, text="Multi-Account Discord Voice Control", bg=C_BG,
                 fg=C_MUTED, font=FONT).pack(side="left", pady=(4, 0))
        tk.Label(h, text=f"v{PROJECT_VERSION}", bg=C_PANEL3, fg=C_MUTED,
                 font=("Segoe UI", 8)).pack(side="left", padx=(10, 0),
                                            pady=(6, 0))
        self.header_stats = tk.Label(h, text="", bg=C_BG, fg=C_MUTED, font=FONT)
        self.header_stats.pack(side="right", padx=20)

        # GitHub badge - "this project is open source" and a one-click link.
        self.github_btn = PillButton(
            h, self._open_github, padx=14, pady=5,
            font=("Segoe UI", 10, "bold"))
        self.github_btn.style("🐙  Open Source · GitHub",
                              bg=C_GH_BG, hover=C_GH_HOVER, fg="#ffffff")
        self.github_btn.pack(side="right", padx=(0, 8))

    def _open_github(self) -> None:
        """Open the public repository in the default browser."""
        try:
            webbrowser.open(PROJECT_URL, new=2)
        except Exception as exc:
            messagebox.showinfo(
                "VoiceHive",
                f"Open this link in your browser:\n\n{PROJECT_URL}\n\n({exc})",
                parent=self)

    def _build_warnings(self) -> None:
        if not DISCORD_OK:
            self._warning_bar(
                "⚠️  The 'discord' library is not working in this interpreter. "
                "Run the app with Python 3.13 using run.bat.  "
                f"({DISCORD_ERR})", C_DANGER)
        elif not DAVEY_OK:
            self._warning_bar(
                "⚠️  Voice E2EE (davey) is missing, so Discord may kick accounts "
                "out of voice. Fix:  py -3.13 -m pip install \"discord.py-self[voice]\"",
                C_WARN)

    def _warning_bar(self, msg: str, color: str) -> None:
        bar = tk.Frame(self, bg=color)
        bar.pack(fill="x", side="top")
        tk.Label(bar, text=msg, bg=color, fg="#ffffff" if color == C_DANGER else "#111111",
                 font=FONT_BOLD, anchor="w", justify="left"
                 ).pack(fill="x", padx=16, pady=8)

    # ------------------------------------------------------------------
    # tabs
    # ------------------------------------------------------------------
    def _build_tabs(self) -> None:
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=14, pady=(0, 14))

        self.tab_dash = tk.Frame(self.nb, bg=C_BG)
        self.tab_accounts = tk.Frame(self.nb, bg=C_BG)
        self.tab_join = tk.Frame(self.nb, bg=C_BG)
        self.tab_voice = tk.Frame(self.nb, bg=C_BG)

        self.nb.add(self.tab_dash, text="📊  Dashboard")
        self.nb.add(self.tab_accounts, text="👥  Accounts")
        self.nb.add(self.tab_join, text="📨  Join Server")
        self.nb.add(self.tab_voice, text="🔊  Voice")

        self._build_dashboard()
        self._build_accounts()
        self._build_join()
        self._build_voice()

    # ---------- Dashboard ----------
    def _build_dashboard(self) -> None:
        wrap = tk.Frame(self.tab_dash, bg=C_BG)
        wrap.pack(fill="both", expand=True, padx=6, pady=6)

        stats = tk.Frame(wrap, bg=C_BG)
        stats.pack(fill="x", pady=(0, 12))

        self.stat_cards: Dict[str, tk.Label] = {}
        for key, label, color in (
            ("total", "Total Accounts", C_PRIMARY),
            ("online", "Online", C_OK),
            ("voice", "In Voice", C_WARN),
            ("muted", "Muted", C_DANGER),
            ("deaf", "Deafened", C_MUTED),
        ):
            c = card(stats)
            c.pack(side="left", fill="x", expand=True, padx=6)
            tk.Frame(c, bg=color, height=3).pack(fill="x")
            body = tk.Frame(c, bg=C_PANEL)
            body.pack(fill="both", padx=16, pady=14)
            val = tk.Label(body, text="0", bg=C_PANEL, fg=C_TXT,
                           font=("Segoe UI", 24, "bold"))
            val.pack(anchor="w")
            tk.Label(body, text=label, bg=C_PANEL, fg=C_MUTED,
                     font=FONT_SMALL).pack(anchor="w")
            self.stat_cards[key] = val

        # Open-source banner: logos, tagline and a clickable repository link.
        src = card(wrap)
        src.pack(fill="x", pady=(0, 12))
        tk.Frame(src, bg=C_GH_BG, height=3).pack(fill="x")
        srow = tk.Frame(src, bg=C_PANEL)
        srow.pack(fill="x", padx=16, pady=12)

        tk.Label(srow, text="🐙", bg=C_PANEL, fg=C_TXT,
                 font=("Segoe UI Emoji", 20)).pack(side="left")
        info = tk.Frame(srow, bg=C_PANEL)
        info.pack(side="left", padx=(12, 0))
        tk.Label(info, text=f"{PROJECT_NAME} is open source", bg=C_PANEL,
                 fg=C_TXT, font=FONT_H).pack(anchor="w")
        link = tk.Label(info, text=PROJECT_URL.replace("https://", ""),
                        bg=C_PANEL, fg=C_PRIMARY, font=FONT, cursor="hand2")
        link.pack(anchor="w")
        link.bind("<Button-1>", lambda _e: self._open_github())
        link.bind("<Enter>", lambda _e: link.configure(fg="#8891ff"))
        link.bind("<Leave>", lambda _e: link.configure(fg=C_PRIMARY))

        flat_btn(srow, "⭐  Star on GitHub", self._open_github, bg=C_GH_BG,
                 hover=C_GH_HOVER, fg="#ffffff", padx=14, pady=6
                 ).pack(side="right")

        body = card(wrap)
        body.pack(fill="both", expand=True)
        head = tk.Frame(body, bg=C_PANEL)
        head.pack(fill="x", padx=14, pady=(12, 4))
        tk.Label(head, text="Live Accounts", bg=C_PANEL, fg=C_TXT,
                 font=FONT_H).pack(side="left")
        tk.Label(head, text="Quick mute / deafen for everyone in voice",
                 bg=C_PANEL, fg=C_MUTED, font=FONT_SMALL).pack(side="left", padx=12)

        self.dash_list = AccountList(body, self, "dash", self._dash_actions)
        self.dash_list.widget().pack(fill="both", expand=True, padx=8, pady=(0, 12))

    def _dash_actions(self, row: AccountRow) -> List[RowAction]:
        mic_style = self._mic_style
        deaf_style = self._deaf_style
        return [
            RowAction("mic",
                      lambda a: "🎙️ Unmute" if a.self_mute else "🔇 Mute",
                      self.toggle_mute, mic_style, width=10),
            RowAction("deaf",
                      lambda a: "🔊 Undeafen" if a.self_deaf else "🔈 Deafen",
                      self.toggle_deaf, deaf_style, width=11),
        ]

    # ---------- Accounts ----------
    def _build_accounts(self) -> None:
        wrap = tk.Frame(self.tab_accounts, bg=C_BG)
        wrap.pack(fill="both", expand=True, padx=6, pady=6)

        add = card(wrap)
        add.pack(fill="x", pady=(0, 10))
        inner = tk.Frame(add, bg=C_PANEL)
        inner.pack(fill="x", padx=16, pady=14)

        tk.Label(inner, text="Add Account", bg=C_PANEL, fg=C_TXT,
                 font=FONT_H).pack(side="left", padx=(0, 16))

        tk.Label(inner, text="Name", bg=C_PANEL, fg=C_MUTED,
                 font=FONT_SMALL).pack(side="left")
        self.new_name_var = tk.StringVar()
        tk.Entry(inner, textvariable=self.new_name_var, bg=C_PANEL2, fg=C_TXT,
                 insertbackground=C_TXT, relief="flat", width=18, font=FONT
                 ).pack(side="left", padx=(6, 14), ipady=5)

        tk.Label(inner, text="Token", bg=C_PANEL, fg=C_MUTED,
                 font=FONT_SMALL).pack(side="left")
        self.new_token_var = tk.StringVar()
        tk.Entry(inner, textvariable=self.new_token_var, bg=C_PANEL2, fg=C_TXT,
                 insertbackground=C_TXT, relief="flat", width=52, font=FONT
                 ).pack(side="left", padx=(6, 14), ipady=5)

        flat_btn(inner, "➕  Add Account", self.on_add_account,
                 bg=C_PRIMARY, hover=C_PRIMARY_H, fg="#ffffff").pack(side="left")

        bar = tk.Frame(wrap, bg=C_BG)
        bar.pack(fill="x", pady=(0, 10))
        flat_btn(bar, "🔌  Reconnect Selected", self.on_reconnect).pack(side="left", padx=4)
        flat_btn(bar, "✏️  Edit Selected", self.on_edit_account).pack(side="left", padx=4)
        flat_btn(bar, "🗑️  Delete Selected", self.on_delete_account,
                 bg=C_DANGER, hover=C_DANGER_H, fg="#ffffff").pack(side="left", padx=4)
        flat_btn(bar, "Select All", lambda: self.select_all("accounts")).pack(side="left", padx=4)
        flat_btn(bar, "Clear", lambda: self.clear_selection("accounts")).pack(side="left", padx=4)

        self.accounts_status = tk.Label(wrap, text="", bg=C_BG, fg=C_MUTED,
                                        font=FONT_SMALL, anchor="w")
        self.accounts_status.pack(fill="x", padx=6, pady=(0, 8))

        body = card(wrap)
        body.pack(fill="both", expand=True)
        self.acc_list = AccountList(body, self, "accounts", self._acc_actions)
        self.acc_list.widget().pack(fill="both", expand=True, padx=10, pady=10)

    def _acc_actions(self, row: AccountRow) -> List[RowAction]:
        return [
            RowAction("reconnect", lambda a: "🔌 Reconnect",
                      self.on_reconnect_one,
                      lambda a: (C_PANEL3, C_PANEL3, C_TXT, True), width=12),
            RowAction("edit", lambda a: "✏️ Edit", self.on_edit_one,
                      lambda a: (C_PANEL3, C_PANEL3, C_TXT, True), width=8),
            RowAction("delete", lambda a: "🗑️ Delete", self.on_delete_one,
                      lambda a: (C_DANGER, C_DANGER_H, "#ffffff", True), width=9),
        ]

    # ---------- Join ----------
    def _build_join(self) -> None:
        wrap = tk.Frame(self.tab_join, bg=C_BG)
        wrap.pack(fill="both", expand=True, padx=6, pady=6)

        left = card(wrap)
        left.pack(side="left", fill="y", padx=(0, 10))
        lb = tk.Frame(left, bg=C_PANEL)
        lb.pack(fill="both", padx=16, pady=14)

        tk.Label(lb, text="Join Server", bg=C_PANEL, fg=C_TXT,
                 font=FONT_H).pack(anchor="w")
        tk.Label(lb, text="Paste an invite link or code",
                 bg=C_PANEL, fg=C_MUTED, font=FONT_SMALL
                 ).pack(anchor="w", pady=(0, 10))

        self.invite_var = tk.StringVar()
        tk.Entry(lb, textvariable=self.invite_var, bg=C_PANEL2, fg=C_TXT,
                 insertbackground=C_TXT, relief="flat", width=36, font=FONT
                 ).pack(fill="x", pady=(0, 12), ipady=6)

        flat_btn(lb, "📨  Join Selected", self.on_join_invite,
                 bg=C_PRIMARY, hover=C_PRIMARY_H, fg="#ffffff"
                 ).pack(fill="x", pady=3)
        flat_btn(lb, "📨  Join All Online", self.on_join_invite_all
                 ).pack(fill="x", pady=3)

        tk.Frame(lb, bg=C_BORDER, height=1).pack(fill="x", pady=12)
        flat_btn(lb, "Select All", lambda: self.select_all("join")).pack(fill="x", pady=3)
        flat_btn(lb, "Clear", lambda: self.clear_selection("join")).pack(fill="x", pady=3)

        right = card(wrap)
        right.pack(side="left", fill="both", expand=True)
        tk.Label(right, text="Accounts", bg=C_PANEL, fg=C_TXT,
                 font=FONT_H).pack(anchor="w", padx=16, pady=(14, 2))
        tk.Label(right, text="Tick ☐ accounts, then press Join",
                 bg=C_PANEL, fg=C_MUTED, font=FONT_SMALL
                 ).pack(anchor="w", padx=16, pady=(0, 6))
        self.join_list = AccountList(right, self, "join", self._join_actions)
        self.join_list.widget().pack(fill="both", expand=True, padx=8, pady=(0, 10))

    def _join_actions(self, row: AccountRow) -> List[RowAction]:
        return [
            RowAction("join", lambda a: "📨 Join", self.on_join_one,
                      lambda a: (C_PRIMARY, C_PRIMARY_H, "#ffffff", a.logged_in),
                      width=8),
        ]

    # ---------- Voice ----------
    def _build_voice(self) -> None:
        wrap = tk.Frame(self.tab_voice, bg=C_BG)
        wrap.pack(fill="both", expand=True, padx=6, pady=6)

        left = card(wrap)
        left.pack(side="left", fill="y", padx=(0, 10))
        lb = tk.Frame(left, bg=C_PANEL)
        lb.pack(fill="both", padx=16, pady=14)

        tk.Label(lb, text="Join Voice Channel", bg=C_PANEL, fg=C_TXT,
                 font=FONT_H).pack(anchor="w")
        tk.Label(lb, text="Pick a name or paste an ID",
                 bg=C_PANEL, fg=C_MUTED, font=FONT_SMALL
                 ).pack(anchor="w", pady=(0, 10))

        tk.Label(lb, text="🔎  Search by server name", bg=C_PANEL, fg=C_MUTED,
                 font=FONT_SMALL).pack(anchor="w")
        self.guild_search_var = tk.StringVar()
        self.guild_search_var.trace_add("write", lambda *_: self._refresh_guild_combo())
        tk.Entry(lb, textvariable=self.guild_search_var, bg=C_PANEL2, fg=C_TXT,
                 insertbackground=C_TXT, relief="flat", width=32, font=FONT
                 ).pack(fill="x", pady=(2, 10), ipady=5)

        tk.Label(lb, text="Server", bg=C_PANEL, fg=C_MUTED,
                 font=FONT_SMALL).pack(anchor="w")
        self.guild_var = tk.StringVar()
        self.guild_combo = ttk.Combobox(lb, textvariable=self.guild_var,
                                        style="Dark.TCombobox", width=32,
                                        font=FONT, values=[])
        self.guild_combo.pack(fill="x", pady=(2, 8), ipady=4)
        self.guild_combo.bind("<<ComboboxSelected>>", self._on_guild_pick)

        tk.Label(lb, text="Voice Channel", bg=C_PANEL, fg=C_MUTED,
                 font=FONT_SMALL).pack(anchor="w")
        tk.Label(lb, text="🔎  Search by channel name", bg=C_PANEL, fg=C_MUTED,
                 font=FONT_SMALL).pack(anchor="w", pady=(6, 0))
        self.chan_search_var = tk.StringVar()
        self.chan_search_var.trace_add("write",
                                       lambda *_: self._filter_channels())
        tk.Entry(lb, textvariable=self.chan_search_var, bg=C_PANEL2, fg=C_TXT,
                 insertbackground=C_TXT, relief="flat", width=32, font=FONT
                 ).pack(fill="x", pady=(2, 6), ipady=4)
        self.chan_var = tk.StringVar()
        self.chan_combo = ttk.Combobox(lb, textvariable=self.chan_var,
                                       style="Dark.TCombobox", width=32,
                                       font=FONT, values=[])
        self.chan_combo.pack(fill="x", pady=(2, 10), ipady=4)

        self.mute_on_join = tk.BooleanVar(value=True)
        self.deaf_on_join = tk.BooleanVar(value=False)
        self.auto_rejoin_var = tk.BooleanVar(value=True)
        for text, var in (("Self mute on join", self.mute_on_join),
                          ("Self deaf on join", self.deaf_on_join),
                          ("Auto rejoin (keep them in voice)", self.auto_rejoin_var)):
            tk.Checkbutton(lb, text=text, variable=var, bg=C_PANEL, fg=C_TXT,
                           selectcolor=C_PANEL2, activebackground=C_PANEL,
                           activeforeground=C_TXT, font=FONT, bd=0,
                           highlightthickness=0, anchor="w"
                           ).pack(anchor="w")

        tk.Frame(lb, bg=C_BORDER, height=1).pack(fill="x", pady=12)
        self.voice_all_mic = flat_btn(lb, "🔇  Mute All", self.on_mute_all,
                                      bg=C_PANEL3)
        self.voice_all_mic.pack(fill="x", pady=3)
        self.voice_all_deaf = flat_btn(lb, "🔈  Deafen All", self.on_deaf_all,
                                       bg=C_PANEL3)
        self.voice_all_deaf.pack(fill="x", pady=3)

        tk.Frame(lb, bg=C_BORDER, height=1).pack(fill="x", pady=12)
        flat_btn(lb, "🎧  Join Selected", self.on_voice_join,
                 bg=C_PRIMARY, hover=C_PRIMARY_H, fg="#ffffff").pack(fill="x", pady=3)
        flat_btn(lb, "🎧  Join All Online", self.on_voice_join_all).pack(fill="x", pady=3)
        flat_btn(lb, "🚪  Leave Selected", self.on_voice_leave,
                 bg=C_DANGER, hover=C_DANGER_H, fg="#ffffff").pack(fill="x", pady=3)
        flat_btn(lb, "🚪  Leave All", self.on_voice_leave_all).pack(fill="x", pady=3)

        tk.Frame(lb, bg=C_BORDER, height=1).pack(fill="x", pady=12)
        flat_btn(lb, "Select All", lambda: self.select_all("voice")).pack(fill="x", pady=3)
        flat_btn(lb, "Clear", lambda: self.clear_selection("voice")).pack(fill="x", pady=3)

        tk.Frame(lb, bg=C_BORDER, height=1).pack(fill="x", pady=12)
        tk.Label(lb, text="Extra windows", bg=C_PANEL, fg=C_MUTED,
                 font=FONT_SMALL).pack(anchor="w", pady=(0, 4))
        flat_btn(lb, "🎧  Listen / Members", self._open_monitor_window,
                 bg=C_PRIMARY, hover=C_PRIMARY_H, fg="#ffffff"
                 ).pack(fill="x", pady=3)
        tk.Checkbutton(lb, text="🔊  Auto-listen (play their voice here)",
                       variable=self.auto_listen, bg=C_PANEL, fg=C_TXT,
                       selectcolor=C_PANEL2, activebackground=C_PANEL,
                       activeforeground=C_TXT, font=FONT_SMALL, bd=0,
                       highlightthickness=0, anchor="w").pack(anchor="w", pady=(2, 3))
        flat_btn(lb, "🗺️  Routing window", self._open_routing_window,
                 bg=C_PANEL3).pack(fill="x", pady=3)

        right = card(wrap)
        right.pack(side="left", fill="both", expand=True)
        tk.Label(right, text="Live Voice Control", bg=C_PANEL, fg=C_TXT,
                 font=FONT_H).pack(anchor="w", padx=16, pady=(14, 2))
        tk.Label(right, text="Mute / deafen an account, or send it to voice",
                 bg=C_PANEL, fg=C_MUTED, font=FONT_SMALL
                 ).pack(anchor="w", padx=16, pady=(0, 6))

        # ---- audio (the ACCOUNTS broadcast it in voice) ----
        #  four clearly separated blocks: file / playback / volume / BOOST
        playback = tk.Frame(right, bg=C_PANEL2, highlightbackground=C_BORDER,
                            highlightthickness=1)
        playback.pack(fill="x", padx=12, pady=(0, 8))

        # ── 1. the file ─────────────────────────────────────────────
        pb = tk.Frame(playback, bg=C_PANEL2)
        pb.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(pb, text="🎵  Audio file", bg=C_PANEL2, fg=C_TXT,
                 font=FONT_H).pack(side="left")
        tk.Label(pb, text="(the accounts broadcast it in voice)",
                 bg=C_PANEL2, fg=C_MUTED, font=FONT_SMALL).pack(side="left", padx=8)

        self.audio_path_var = tk.StringVar()
        self.last_audio_path = ""
        tk.Entry(pb, textvariable=self.audio_path_var, bg=C_PANEL, fg=C_TXT,
                 insertbackground=C_TXT, relief="flat", font=FONT
                 ).pack(side="left", fill="x", expand=True, padx=(12, 6), ipady=5)
        flat_btn(pb, "📂", self.on_browse_audio, bg=C_PANEL, padx=10).pack(side="left")

        # ── 2. playback control (separate block) ────────────────────
        tk.Frame(playback, bg=C_BORDER, height=1).pack(fill="x", padx=12, pady=(8, 0))
        head = tk.Frame(playback, bg=C_PANEL2)
        head.pack(fill="x", padx=12, pady=(6, 0))
        tk.Label(head, text="▶  PLAYBACK", bg=C_PANEL2, fg=C_PRIMARY,
                 font=FONT_BOLD).pack(side="left")
        tk.Label(head, text="   switching accounts keeps the same second",
                 bg=C_PANEL2, fg=C_MUTED, font=FONT_SMALL).pack(side="left")

        self.loop_var = tk.BooleanVar(value=False)

        row1 = tk.Frame(playback, bg=C_PANEL2)
        row1.pack(fill="x", padx=12, pady=(4, 2))
        flat_btn(row1, "▶️ Play — everyone in voice", self.on_play_all,
                 bg=C_OK, hover=C_OK_H, fg="#ffffff").pack(side="left", padx=(0, 6))
        flat_btn(row1, "▶️ Play — selected", self.on_play_selected,
                 bg=C_OK, hover=C_OK_H, fg="#ffffff").pack(side="left", padx=(0, 6))
        flat_btn(row1, "⛔ Stop everyone", self.on_stop_audio,
                 bg=C_DANGER, hover=C_DANGER_H, fg="#ffffff").pack(side="left")

        row2 = tk.Frame(playback, bg=C_PANEL2)
        row2.pack(fill="x", padx=12, pady=(2, 4))
        flat_btn(row2, "🔀 Move to selected (continue)", self.on_only_selected,
                 bg=C_PANEL3).pack(side="left", padx=(0, 6))
        flat_btn(row2, "⟲ Restart from 0:00", self.on_restart_audio,
                 bg=C_PANEL3).pack(side="left", padx=(0, 6))
        tk.Checkbutton(row2, text="🔁 Repeat", variable=self.loop_var,
                       bg=C_PANEL2, fg=C_TXT, selectcolor=C_PANEL,
                       activebackground=C_PANEL2, activeforeground=C_TXT,
                       font=FONT_SMALL, bd=0, highlightthickness=0,
                       cursor="hand2").pack(side="left", padx=(6, 0))

        # ── 3. volume (normal loudness, separate block) ─────────────
        tk.Frame(playback, bg=C_BORDER, height=1).pack(fill="x", padx=12, pady=(2, 0))
        vol = tk.Frame(playback, bg=C_PANEL3)
        vol.pack(fill="x", padx=12, pady=(6, 4))
        vhead = tk.Frame(vol, bg=C_PANEL3)
        vhead.pack(fill="x", padx=10, pady=(6, 0))
        tk.Label(vhead, text="🔉  VOLUME", bg=C_PANEL3, fg=C_TXT,
                 font=FONT_BOLD).pack(side="left")
        tk.Label(vhead, text="normal loudness   0–200%", bg=C_PANEL3,
                 fg=C_MUTED, font=FONT_SMALL).pack(side="left", padx=8)
        self.volume_value = tk.Label(vhead, text="100%", bg=C_PANEL3, fg=C_OK,
                                     font=FONT_BOLD)
        self.volume_value.pack(side="left", padx=6)
        vrow = tk.Frame(vol, bg=C_PANEL3)
        vrow.pack(fill="x", padx=10, pady=(0, 6))
        flat_btn(vol, "➖", self.on_volume_down, bg=C_PANEL, padx=8, pady=1
                 ).pack(side="left", padx=(10, 0))
        self.volume_var = tk.IntVar(value=100)
        tk.Scale(vol, from_=0, to=200, orient="horizontal",
                 variable=self.volume_var, bg=C_PANEL3, fg=C_TXT,
                 troughcolor=C_PANEL, highlightthickness=0, bd=0,
                 font=FONT_SMALL, length=220, showvalue=False,
                 command=lambda _v=None: self.on_volume_change()
                 ).pack(side="left", padx=6)
        flat_btn(vol, "➕", self.on_volume_up, bg=C_PANEL, padx=8, pady=1
                 ).pack(side="left")

        # ── 4. BOOST — a different, separate control: extra gain ────
        tk.Frame(playback, bg=C_BORDER, height=1).pack(fill="x", padx=12, pady=(2, 0))
        boost = tk.Frame(playback, bg=C_BOOST_BG,
                         highlightbackground=C_DANGER, highlightthickness=1)
        boost.pack(fill="x", padx=12, pady=(6, 4))
        bhead = tk.Frame(boost, bg=C_BOOST_BG)
        bhead.pack(fill="x", padx=10, pady=(6, 0))
        tk.Label(bhead, text="📢  BOOST", bg=C_BOOST_BG, fg=C_BOOST_TXT,
                 font=FONT_BOLD).pack(side="left")
        tk.Label(bhead, text="extra amplification (gets loud / distorted)   "
                             "100–800%", bg=C_BOOST_BG, fg=C_MUTED,
                 font=FONT_SMALL).pack(side="left")
        brow = tk.Frame(boost, bg=C_BOOST_BG)
        brow.pack(fill="x", padx=10, pady=(0, 6))
        flat_btn(brow, "➖", self.on_boost_down, bg=C_PANEL, padx=8, pady=1
                 ).pack(side="left")
        self.boost_var = tk.IntVar(value=100)
        tk.Scale(brow, from_=100, to=800, orient="horizontal",
                 variable=self.boost_var, bg=C_BOOST_BG, fg=C_TXT,
                 troughcolor=C_PANEL, highlightthickness=0, bd=0,
                 font=FONT_SMALL, length=230, showvalue=True,
                 command=lambda _v=None: self.on_boost_change()
                 ).pack(side="left", padx=6)
        flat_btn(brow, "➕", self.on_boost_up, bg=C_PANEL, padx=8, pady=1
                 ).pack(side="left", padx=(0, 8))
        self.boost_value = tk.Label(brow, text="100%", bg=C_BOOST_BG,
                                    fg=C_BOOST_TXT, font=FONT_BOLD, width=5)
        self.boost_value.pack(side="left")

        self.audio_status = tk.Label(playback, text="", bg=C_PANEL2, fg=C_MUTED,
                                     font=FONT_SMALL, anchor="w", wraplength=620,
                                     justify="left")
        self.audio_status.pack(fill="x", padx=12, pady=(2, 8))

        self.voice_list = AccountList(right, self, "voice", self._voice_actions)
        self.voice_list.widget().pack(fill="both", expand=True, padx=8, pady=(0, 10))

    def _voice_actions(self, row: AccountRow) -> List[RowAction]:
        return [
            RowAction("mic",
                      lambda a: "🎙️ Unmute" if a.self_mute else "🔇 Mute",
                      self.toggle_mute, self._mic_style, width=10),
            RowAction("deaf",
                      lambda a: "🔊 Undeafen" if a.self_deaf else "🔈 Deafen",
                      self.toggle_deaf, self._deaf_style, width=11),
            RowAction("play",
                      lambda a: "⏸ Stop" if a.playing else "▶ Play",
                      self.on_play_one, self._play_style, width=8),
            RowAction("join", lambda a: "🎧 Join", self.on_voice_join_one,
                      lambda a: (C_OK, C_OK_H, "#ffffff", a.logged_in), width=7),
            RowAction("leave", lambda a: "🚪 Leave", self.on_voice_leave_one,
                      lambda a: (C_DANGER, C_DANGER_H, "#ffffff", bool(a.voice_channel_id)),
                      width=8),
        ]

    def _play_style(self, acc: Account):
        if acc.playing:
            return (C_WARN, C_WARN_H, "#111111", True)
        if acc.voice_channel_id and getattr(self, "last_audio_path", ""):
            return (C_OK, C_OK_H, "#ffffff", True)
        return (C_PANEL3, C_PANEL3, C_TXT, False)

    @staticmethod
    def _mic_style(acc: Account):
        if not acc.voice_channel_id:
            return (C_PANEL3, C_PANEL3, C_TXT, False)
        if acc.self_mute:
            return (C_WARN, C_WARN_H, "#111111", True)
        return (C_OK, C_OK_H, "#ffffff", True)

    @staticmethod
    def _deaf_style(acc: Account):
        if not acc.voice_channel_id:
            return (C_PANEL3, C_PANEL3, C_TXT, False)
        if acc.self_deaf:
            return (C_WARN, C_WARN_H, "#111111", True)
        return (C_PANEL3, C_PANEL3, C_TXT, True)

    # ------------------------------------------------------------------
    # selection helpers
    # ------------------------------------------------------------------
    def is_selected(self, tab_key: str, acc_id: str) -> bool:
        return acc_id in self.selection[tab_key]

    def toggle_selected(self, tab_key: str, acc_id: str) -> None:
        s = self.selection[tab_key]
        if acc_id in s:
            s.discard(acc_id)
        else:
            s.add(acc_id)
        self.refresh()

    def select_all(self, tab_key: str) -> None:
        self.selection[tab_key] = {a.id for a in self.controller.accounts}
        self.refresh()

    def clear_selection(self, tab_key: str) -> None:
        self.selection[tab_key] = set()
        self.refresh()

    def _selected(self, tab_key: str, online_only: bool = False) -> List[str]:
        out = []
        for a in self.controller.accounts:
            if a.id in self.selection[tab_key]:
                if online_only and not a.logged_in:
                    continue
                out.append(a.id)
        return out

    def _online_ids(self) -> List[str]:
        return [a.id for a in self.controller.accounts if a.logged_in]

    # ------------------------------------------------------------------
    # account actions
    # ------------------------------------------------------------------
    def _set_accounts_status(self, text: str, error: bool = False) -> None:
        self.accounts_status.configure(text=text, fg=C_DANGER if error else C_OK)

    def on_add_account(self) -> None:
        name = self.new_name_var.get().strip()
        token = self.new_token_var.get().strip()
        if not token:
            self._set_accounts_status("Token is required.", error=True)
            return
        if not name:
            name = f"Account {len(self.controller.accounts) + 1}"
        try:
            self.controller.add_account(name, token)
        except Exception as exc:
            self._set_accounts_status(f"Failed to add: {exc}", error=True)
            return
        self.new_name_var.set("")
        self.new_token_var.set("")
        self._set_accounts_status(f"Added \"{name}\". Logging in...")
        self.refresh()

    def on_edit_account(self) -> None:
        ids = self._selected("accounts")
        if len(ids) != 1:
            messagebox.showinfo("Edit", "Select exactly one account first.", parent=self)
            return
        self.on_edit_one(ids[0])

    def _edit_account(self, acc_id: str) -> None:
        acc = self.controller.find(acc_id)
        if not acc:
            return
        name = simpledialog.askstring("Edit Account", "Name:",
                                      initialvalue=acc.name, parent=self)
        if name is None:
            return
        token = simpledialog.askstring("Edit Account", "Token:",
                                       initialvalue=acc.token, parent=self)
        if token is None:
            return
        self.controller.update_account(acc.id, name.strip() or acc.name, token.strip())
        self._set_accounts_status(f"Updated \"{acc.name}\".")
        self.refresh()

    def on_edit_one(self, acc_id: str) -> None:
        self._edit_account(acc_id)

    def on_reconnect(self) -> None:
        ids = self._selected("accounts")
        if not ids:
            self._set_accounts_status("Select account(s) first.", error=True)
            return
        self.on_reconnect_one(ids)

    def on_reconnect_one(self, acc_id) -> None:
        ids = acc_id if isinstance(acc_id, list) else [acc_id]
        for aid in ids:
            self.controller.stop_account(aid)
        for aid in ids:
            self.controller.start_account(aid)
        self._set_accounts_status(f"Reconnecting {len(ids)} account(s)...")
        self.refresh()

    def on_delete_account(self) -> None:
        ids = self._selected("accounts")
        if not ids:
            self._set_accounts_status("Select account(s) first.", error=True)
            return
        if not messagebox.askyesno("Delete", f"Delete {len(ids)} account(s)?", parent=self):
            return
        for aid in ids:
            self.controller.delete_account(aid)
            self.selection["accounts"].discard(aid)
        save_accounts(self.controller.accounts)
        self._set_accounts_status(f"Deleted {len(ids)} account(s).")
        self.refresh()

    def on_delete_one(self, acc_id: str) -> None:
        acc = self.controller.find(acc_id)
        if not acc:
            return
        if not messagebox.askyesno("Delete", f"Delete \"{acc.name}\"?", parent=self):
            return
        self.controller.delete_account(acc_id)
        for key in self.selection:
            self.selection[key].discard(acc_id)
        self._set_accounts_status(f"Deleted \"{acc.name}\".")
        self.refresh()

    # ------------------------------------------------------------------
    # join actions
    # ------------------------------------------------------------------
    def _invite(self) -> str:
        invite = self.invite_var.get().strip()
        if not invite:
            messagebox.showinfo("Join Server", "Paste an invite link or code first.", parent=self)
        return invite

    def on_join_invite(self) -> None:
        invite = self._invite()
        if not invite:
            return
        ids = self._selected("join", online_only=True)
        if not ids:
            messagebox.showinfo("Join Server", "Select at least one online account.", parent=self)
            return
        self.controller.join_invite(ids, invite)

    def on_join_invite_all(self) -> None:
        invite = self._invite()
        if not invite:
            return
        ids = self._online_ids()
        if not ids:
            messagebox.showinfo("Join Server", "No online accounts.", parent=self)
            return
        self.controller.join_invite(ids, invite)

    def on_join_one(self, acc_id: str) -> None:
        invite = self._invite()
        if not invite:
            return
        self.controller.join_invite([acc_id], invite)

    # ------------------------------------------------------------------
    # voice actions
    # ------------------------------------------------------------------
    def _voice_target(self) -> Optional[Tuple[str, str]]:
        gid = parse_id(self.guild_var.get())
        cid = parse_id(self.chan_var.get())
        if not gid or not cid:
            messagebox.showinfo("Voice", "Choose a Guild and a Voice Channel first.", parent=self)
            return None
        return gid, cid

    def on_voice_join(self) -> None:
        target = self._voice_target()
        if not target:
            return
        ids = self._selected("voice", online_only=True)
        if not ids:
            messagebox.showinfo("Voice", "Select at least one online account.", parent=self)
            return
        self.controller.join_voice(ids, target[0], target[1],
                                   self.mute_on_join.get(),
                                   self.deaf_on_join.get(),
                                   self.auto_rejoin_var.get())

    def on_voice_join_all(self) -> None:
        target = self._voice_target()
        if not target:
            return
        ids = self._online_ids()
        if not ids:
            messagebox.showinfo("Voice", "No online accounts.", parent=self)
            return
        self.controller.join_voice(ids, target[0], target[1],
                                   self.mute_on_join.get(),
                                   self.deaf_on_join.get(),
                                   self.auto_rejoin_var.get())

    def on_voice_join_one(self, acc_id: str) -> None:
        target = self._voice_target()
        if not target:
            return
        self.controller.join_voice([acc_id], target[0], target[1],
                                   self.mute_on_join.get(),
                                   self.deaf_on_join.get(),
                                   self.auto_rejoin_var.get())

    def on_voice_leave(self) -> None:
        ids = self._selected("voice")
        ids = [i for i in ids if self.controller.find(i) and self.controller.find(i).voice_channel_id]
        if not ids:
            messagebox.showinfo("Voice", "No selected account is in voice.", parent=self)
            return
        self.controller.leave_voice(ids)

    def on_voice_leave_all(self) -> None:
        ids = [a.id for a in self.controller.accounts if a.voice_channel_id]
        if not ids:
            return
        self.controller.leave_voice(ids)

    def on_voice_leave_one(self, acc_id: str) -> None:
        acc = self.controller.find(acc_id)
        if acc and acc.voice_channel_id:
            self.controller.leave_voice([acc_id])

    # per-account toggles
    def toggle_mute(self, acc_id: str) -> None:
        acc = self.controller.find(acc_id)
        if not acc or not acc.voice_channel_id:
            return
        self.controller.set_voice_state([acc_id], mute=not acc.self_mute)

    def toggle_deaf(self, acc_id: str) -> None:
        acc = self.controller.find(acc_id)
        if not acc or not acc.voice_channel_id:
            return
        self.controller.set_voice_state([acc_id], deaf=not acc.self_deaf)

    # bulk toggles
    def _in_voice_ids(self) -> List[str]:
        return [a.id for a in self.controller.accounts if a.voice_channel_id]

    def on_mute_all(self) -> None:
        ids = self._in_voice_ids()
        if not ids:
            messagebox.showinfo("Voice", "Nobody is in voice.", parent=self)
            return
        target = not all(a.self_mute for a in self.controller.accounts if a.voice_channel_id)
        self.controller.set_voice_state(ids, mute=target)

    def on_deaf_all(self) -> None:
        ids = self._in_voice_ids()
        if not ids:
            messagebox.showinfo("Voice", "Nobody is in voice.", parent=self)
            return
        target = not all(a.self_deaf for a in self.controller.accounts if a.voice_channel_id)
        self.controller.set_voice_state(ids, deaf=target)

    # ------------------------------------------------------------------
    # audio playback
    # ------------------------------------------------------------------
    def on_browse_audio(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title="Choose an audio file",
            filetypes=[("Audio", " ".join("*" + e for e in AUDIO_EXTS)),
                       ("All files", "*.*")])
        if path:
            self.audio_path_var.set(path)
            # warm the duration cache in the background so the first play
            # does not have to wait for ffprobe
            threading.Thread(target=probe_duration, args=(path,),
                             daemon=True).start()
            self._audio_status_text(f"Loaded: {os.path.basename(path)}")

    def _audio_path_or_none(self) -> str:
        path = self.audio_path_var.get().strip().strip('"')
        if path and os.path.isfile(path):
            return path
        path = getattr(self, "last_audio_path", "")
        if path and os.path.isfile(path):
            return path
        return ""

    def _current_audio(self) -> str:
        path = self._audio_path_or_none()
        if not path:
            messagebox.showinfo("Playback", "Choose an audio file first.", parent=self)
        return path

    def _volume(self) -> float:
        try:
            value = int(self.volume_var.get())
        except Exception:
            value = 100
        return max(0.0, min(2.0, value / 100.0))

    def _boost(self) -> float:
        try:
            value = int(self.boost_var.get())
        except Exception:
            value = 100
        return max(1.0, min(8.0, value / 100.0))

    def _loop(self) -> bool:
        try:
            return bool(self.loop_var.get())
        except Exception:
            return False

    # ---------- volume (live, normal loudness) ----------
    def on_volume_up(self) -> None:
        self._bump_volume(10)

    def on_volume_down(self) -> None:
        self._bump_volume(-10)

    def _bump_volume(self, delta: int) -> None:
        value = max(0, min(200, int(self.volume_var.get()) + delta))
        self.volume_var.set(value)
        self.on_volume_change()

    def on_volume_change(self, _event=None) -> None:
        try:
            value = int(self.volume_var.get())
        except Exception:
            value = 100
        try:
            self.volume_value.configure(text=f"{value}%")
        except Exception:
            pass
        self.controller.set_playback_volume(value / 100.0)
        self._audio_status_text(f"🔉 Volume {value}%  (applied live)")

    # ---------- BOOST (live, separate from volume) ----------
    def on_boost_up(self) -> None:
        self._bump_boost(50)

    def on_boost_down(self) -> None:
        self._bump_boost(-50)

    def _bump_boost(self, delta: int) -> None:
        value = max(100, min(800, int(self.boost_var.get()) + delta))
        self.boost_var.set(value)
        self.on_boost_change()

    def on_boost_change(self, _event=None) -> None:
        try:
            value = int(self.boost_var.get())
        except Exception:
            value = 100
        try:
            self.boost_value.configure(text=f"{value}%")
        except Exception:
            pass
        self.controller.set_playback_boost(value / 100.0)
        if value == 100:
            self._audio_status_text("📢 Boost off (100%)")
        else:
            self._audio_status_text(
                f"📢 Boost {value}%  (amplified, may distort)")

    # ---------- play / transfer ----------
    def on_play_all(self) -> None:
        self._play(everyone=True)

    def on_play_selected(self) -> None:
        self._play(everyone=False)

    def on_restart_audio(self) -> None:
        self._play(everyone=True, restart=True)

    def _play(self, everyone: bool, restart: bool = False) -> None:
        path = self._current_audio()
        if not path:
            return
        if find_ffmpeg() is None:
            self._audio_status_text(
                "ffmpeg not found - only simple 48kHz WAV files can play.",
                ok=False)
        if everyone:
            ids = self._in_voice_ids()
        else:
            ids = []
            for aid in self._selected("voice", online_only=True):
                acc = self.controller.find(aid)
                if acc and acc.voice_channel_id:
                    ids.append(aid)
        if not ids:
            messagebox.showinfo("Playback",
                                "No account is in voice for that.", parent=self)
            return
        self.last_audio_path = path
        self.controller.play_song(ids, path, self._volume(), self._boost(),
                                  self._loop(), restart=restart)
        verb = "restarted" if restart else "broadcasting"
        self._audio_status_text(
            f"▶ {len(ids)} account(s) are {verb} "
            f"\"{os.path.basename(path)}\"")

    def on_play_one(self, acc_id: str) -> None:
        acc = self.controller.find(acc_id)
        if not acc or not acc.voice_channel_id:
            return
        if acc.playing:
            self.controller.stop_audio([acc_id])
            self._audio_status_text(f"⏸ {acc.name} stopped.")
            return
        path = self._audio_path_or_none()
        if not path:
            messagebox.showinfo("Playback", "Choose an audio file first.", parent=self)
            return
        self.last_audio_path = path
        self.controller.play_song([acc_id], path, self._volume(), self._boost(),
                                  self._loop())
        self._audio_status_text(
            f"▶ {acc.name} is playing \"{os.path.basename(path)}\"")

    def on_only_selected(self) -> None:
        target = None
        for aid in self._selected("voice", online_only=True):
            acc = self.controller.find(aid)
            if acc and acc.voice_channel_id:
                target = aid
                break
        if target is None:
            messagebox.showinfo("Playback",
                                "Select one account that is in voice.", parent=self)
            return
        path = self._audio_path_or_none()
        if not path:
            messagebox.showinfo("Playback", "Choose an audio file first.", parent=self)
            return
        self.last_audio_path = path
        # stop everyone else, then let the chosen account continue from the
        # same second of the song instead of starting over
        self.controller.stop_audio(None)

        def start():
            self.controller.play_song([target], path, self._volume(),
                                      self._boost(), self._loop())
            acc = self.controller.find(target)
            self._audio_status_text(
                f"🔀 now only {acc.name if acc else 'one account'} keeps playing")
        self.after(350, start)

    def on_stop_audio(self) -> None:
        self.controller.stop_audio(None)
        self._audio_status_text("⛔ stopped on all accounts.")

    def _audio_status_text(self, text: str, ok: bool = True) -> None:
        try:
            self.audio_status.configure(text=text, fg=C_OK if ok else C_WARN)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # combobox pickers
    # ------------------------------------------------------------------
    def _on_guild_pick(self, _event=None) -> None:
        gid = parse_id(self.guild_var.get())
        if not gid:
            return
        chans = self.controller.voice_channels(gid)
        self._chan_values_all = [f"{name} ({cid})" for cid, name in chans]
        self._filter_channels()
        if self._chan_values_all and not parse_id(self.chan_var.get()):
            self.chan_var.set(self._chan_values_all[0])

    def _filter_channels(self) -> None:
        """Live filter of the channel list (matches from the first letter)."""
        values = getattr(self, "_chan_values_all", [])
        try:
            query = self.chan_search_var.get().strip().lower()
        except Exception:
            query = ""
        if query:
            values = [v for v in values if query in v.lower()]
        if list(self.chan_combo.cget("values")) != values:
            self.chan_combo.configure(values=values)
        if values and parse_id(self.chan_var.get()) not in \
                [parse_id(v) for v in values]:
            self.chan_var.set(values[0])

    def _refresh_guild_combo(self) -> None:
        guilds = self.controller.known_guilds()
        try:
            query = self.guild_search_var.get().strip().lower()
        except Exception:
            query = ""
        items = [(name, gid) for gid, name in guilds.items()
                 if not query or query in name.lower() or query in str(gid)]
        items.sort(key=lambda kv: kv[0].lower())
        values = [f"{name} ({gid})" for name, gid in items]
        if list(self.guild_combo.cget("values")) != values:
            self.guild_combo.configure(values=values)
        # typing a server name immediately selects the first match, so its
        # channels appear at once (works from the very first letter)
        if query and values and parse_id(self.guild_var.get()) not in \
                [parse_id(v) for v in values]:
            self.guild_var.set(values[0])
            self._on_guild_pick()

    # ------------------------------------------------------------------
    # extra detached windows (same script)
    # ------------------------------------------------------------------
    def _open_monitor_window(self) -> None:
        win = getattr(self, "_monitor_win", None)
        if win is not None and win.winfo_exists():
            win.deiconify()
            win.lift()
            win.focus_force()
            return
        self._monitor_win = MonitorWindow(self)

    def _open_routing_window(self) -> None:
        win = getattr(self, "_routing_win", None)
        if win is not None and win.winfo_exists():
            win.deiconify()
            win.lift()
            win.focus_force()
            return
        self._routing_win = RoutingWindow(self)

    def browse_audio_into(self, var) -> None:
        p = filedialog.askopenfilename(
            title="Pick an audio file",
            filetypes=[("Audio", "*" + " *".join(AUDIO_EXTS)), ("All", "*.*")])
        if p:
            var.set(p)
            self.last_audio_path = p

    # ------------------------------------------------------------------
    # refresh
    # ------------------------------------------------------------------
    def refresh(self) -> None:
        accs = self.controller.accounts

        total = len(accs)
        online = sum(1 for a in accs if a.logged_in)
        voice = sum(1 for a in accs if a.voice_channel_id)
        muted = sum(1 for a in accs if a.voice_channel_id and a.self_mute)
        deaf = sum(1 for a in accs if a.voice_channel_id and a.self_deaf)

        self.header_stats.configure(
            text=f"Accounts: {total}   ·   Online: {online}   ·   "
                 f"In Voice: {voice}   ·   Muted: {muted}")
        self.stat_cards["total"].configure(text=str(total))
        self.stat_cards["online"].configure(text=str(online))
        self.stat_cards["voice"].configure(text=str(voice))
        self.stat_cards["muted"].configure(text=str(muted))
        self.stat_cards["deaf"].configure(text=str(deaf))

        self.dash_list.sync(accs)
        self.acc_list.sync(accs)
        self.join_list.sync(accs)
        self.voice_list.sync(accs)

        self._refresh_guild_combo()

        playing = sum(1 for a in accs if a.playing)
        try:
            if playing:
                self.audio_status.configure(text=f"♪ Playing on {playing} account(s)")
        except Exception:
            pass

        all_muted = bool(voice) and all(a.self_mute for a in accs if a.voice_channel_id)
        all_deaf = bool(voice) and all(a.self_deaf for a in accs if a.voice_channel_id)
        self.voice_all_mic.style("🔊  Unmute All" if all_muted else "🔇  Mute All",
                                 bg=C_OK if all_muted else C_PANEL3,
                                 hover=C_OK_H if all_muted else C_PANEL3)
        self.voice_all_deaf.style("🔊  Undeafen All" if all_deaf else "🔈  Deafen All",
                                  bg=C_OK if all_deaf else C_PANEL3,
                                  hover=C_OK_H if all_deaf else C_PANEL3)

        self._auto_sync_listen(accs)

    # ------------------------------------------------------------------
    def _auto_sync_listen(self, accs) -> None:
        """Keep the local "listen" playing the channel an account joined."""
        if not LISTEN_OK:
            return
        var = getattr(self, "auto_listen", None)
        if var is None or not var.get():
            return
        target = next((a for a in accs if a.voice_channel_id), None)
        if target is None:
            self._auto_listened.clear()
            return
        if target.id in self._auto_listened:
            return                       # already handled / user stopped it
        win = self._monitor_win
        if win is None or not win.winfo_exists():
            # only bother opening the window while someone is in voice
            self._open_monitor_window()
            win = self._monitor_win
            if win is None:
                return
        if win.listener is not None:
            if win.listener_acc == target.id:
                self._auto_listened.add(target.id)
            # otherwise it is still bound to an account that just left; the
            # window will drop it on its next tick and we restart then
            return
        # start listening to the first account that is in a channel
        win.selected_id = target.id
        win._member_sig = None
        win.toggle_listen()
        self._auto_listened.add(target.id)

    # ------------------------------------------------------------------
    def on_close(self) -> None:
        for attr in ("_monitor_win", "_routing_win"):
            win = getattr(self, attr, None)
            try:
                if win is not None and win.winfo_exists():
                    win.on_close()
            except Exception:
                pass
        try:
            self.controller.shutdown()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass


# ======================================================================
# Detached windows: avatars + AvatarCache
# ======================================================================
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def _avatar_url(member, size: int = 64) -> Optional[str]:
    try:
        asset = getattr(member, "display_avatar", None)
        url = getattr(asset, "url", "") if asset is not None else ""
    except Exception:
        url = ""
    if not url:
        return None
    base = url.split("?", 1)[0]
    for ext in (".gif", ".webp", ".jpg", ".jpeg"):
        if base.lower().endswith(ext):
            base = base[: -len(ext)] + ".png"
            break
    return f"{base}?size={size}"


class AvatarCache:
    """Downloads member avatars in the background and renders them as
    Tk images on the main thread (Tk is not thread-safe)."""

    def __init__(self, widget, on_ready=None):
        self.widget = widget
        self.on_ready = on_ready
        self.images: Dict[str, "tk.PhotoImage"] = {}
        self._pending = set()
        self._q: "queue.Queue" = queue.Queue()
        self._done: "queue.Queue" = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    def get(self, member):
        key = str(getattr(member, "id", "") or "")
        if not key:
            return None
        img = self.images.get(key)
        if img is not None:
            return img
        if key not in self._pending:
            url = _avatar_url(member)
            if url:
                self._pending.add(key)
                self._q.put((key, url))
        return None

    def _worker(self):
        # Never touch Tk here: hand finished downloads to the main thread
        # through a queue and let drain() build the PhotoImage there.
        while True:
            key, url = self._q.get()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": _UA})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = resp.read()
                self._done.put((key, data))
            except Exception:
                self._pending.discard(key)

    def drain(self):
        """Called on the Tk main thread to render finished downloads."""
        while True:
            try:
                key, data = self._done.get_nowait()
            except queue.Empty:
                return
            self._store(key, data)

    def _store(self, key: str, data: bytes) -> None:
        try:
            b64 = base64.b64encode(data).decode("ascii")
            img = tk.PhotoImage(data=b64)
            if img.width() > 64:
                img = img.subsample(max(1, img.width() // 64))
            self.images[key] = img
            if self.on_ready:
                self.on_ready(key, img)
        except Exception:
            pass
        finally:
            self._pending.discard(key)


# ======================================================================
# Live Listen / Members window (detached)
# ======================================================================
class MonitorWindow(tk.Toplevel):
    REFRESH_MS = 1200

    def __init__(self, app: "App") -> None:
        super().__init__(app)
        self.app = app
        self.controller = app.controller
        self.title("🎧  Live Listen & Members")
        self.geometry("1060x700")
        self.minsize(900, 560)
        self.configure(bg=C_BG)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.selected_id: Optional[str] = None
        self.listener = None
        self.listener_acc: Optional[str] = None
        self.local_muted: Dict[str, bool] = {}
        self._side_ids: List[str] = []
        self._member_sig = None
        self._avatar_labels: Dict[str, tk.Label] = {}
        self.avatars = AvatarCache(self, self._on_avatar)
        # a deafened account is never sent audio by Discord, so while we listen
        # we quietly undo that; ``_undeafening`` remembers we asked so we do
        # not spam the gateway every refresh tick.
        self._undeafening = False
        self._undeafen_at = 0.0
        # watchdog state: if packets stop flowing on an otherwise live channel
        # the socket silently died, so we re-attach the listener.
        self._stall_pkts = -1
        self._stall_since = 0.0
        self._stall_cooldown = 0.0
        # "save what I hear"
        self._record_last = ""
        self._record_last_t = 0.0

        self.audio_var = tk.StringVar(
            value=app.audio_path_var.get() if hasattr(app, "audio_path_var") else "")
        self.listen_vol = tk.IntVar(value=100)
        self.listen_boost = tk.IntVar(value=100)
        self.play_vol = tk.IntVar(value=100)
        self.play_boost = tk.IntVar(value=100)
        self.loop_var = tk.BooleanVar(value=False)

        self._build()
        self._do_refresh()
        self.after(self.REFRESH_MS, self._tick)

    # ---------------- layout ----------------
    def _build(self) -> None:
        top = tk.Frame(self, bg=C_BG)
        top.pack(fill="both", expand=True, padx=8, pady=8)

        # ---- sidebar: accounts currently in a voice channel ----
        side = card(top)
        side.pack(side="left", fill="y", padx=(0, 8))
        tk.Label(side, text="In voice", bg=C_PANEL, fg=C_TXT,
                 font=FONT_H).pack(anchor="w", padx=12, pady=(10, 0))
        tk.Label(side, text="pick an account to watch & listen",
                 bg=C_PANEL, fg=C_MUTED, font=FONT_SMALL
                 ).pack(anchor="w", padx=12)
        self.side_scroll = ScrollArea(side, bg=C_PANEL)
        self.side_scroll.canvas.configure(width=230)
        self.side_scroll.pack(fill="both", expand=True, padx=6, pady=8)

        # ---- right: members + controls ----
        right = card(top)
        right.pack(side="left", fill="both", expand=True)

        head = tk.Frame(right, bg=C_PANEL)
        head.pack(fill="x", padx=14, pady=(12, 2))
        self.title_lbl = tk.Label(head, text="No account selected",
                                  bg=C_PANEL, fg=C_TXT, font=FONT_H)
        self.title_lbl.pack(anchor="w")
        self.sub_lbl = tk.Label(head, text="", bg=C_PANEL, fg=C_MUTED,
                                font=FONT_SMALL)
        self.sub_lbl.pack(anchor="w")

        # listen + local monitoring volume
        listen = tk.Frame(right, bg=C_PANEL2, highlightbackground=C_BORDER,
                          highlightthickness=1)
        listen.pack(fill="x", padx=12, pady=(8, 6))
        lrow = tk.Frame(listen, bg=C_PANEL2)
        lrow.pack(fill="x", padx=10, pady=(8, 2))
        self.listen_btn = flat_btn(lrow, "🔊  Start listening",
                                   self.toggle_listen, bg=C_OK, hover=C_OK_H,
                                   fg="#ffffff")
        self.listen_btn.pack(side="left")
        flat_btn(lrow, "🔈  Test my speakers", self._test_speaker,
                 bg=C_PANEL3).pack(side="left", padx=(6, 0))
        flat_btn(lrow, "⏺  Save 10s", self._record_listen,
                 bg=C_PANEL3).pack(side="left", padx=(6, 0))
        self.listen_status = tk.Label(lrow, text="", bg=C_PANEL2, fg=C_MUTED,
                                      font=FONT_SMALL)
        self.listen_status.pack(side="left", padx=10)

        vrow = tk.Frame(listen, bg=C_PANEL2)
        vrow.pack(fill="x", padx=10, pady=(4, 8))
        tk.Label(vrow, text="Listen volume", bg=C_PANEL2, fg=C_MUTED,
                 font=FONT_SMALL).pack(side="left")
        tk.Scale(vrow, from_=0, to=200, orient="horizontal",
                 variable=self.listen_vol, bg=C_PANEL2, fg=C_TXT,
                 troughcolor=C_PANEL, highlightthickness=0, bd=0,
                 showvalue=True, length=150, font=FONT_SMALL,
                 command=lambda _v=None: self._apply_local_gain()
                 ).pack(side="left", padx=6)
        tk.Label(vrow, text="Boost", bg=C_PANEL2, fg=C_MUTED,
                 font=FONT_SMALL).pack(side="left", padx=(12, 0))
        tk.Scale(vrow, from_=100, to=800, orient="horizontal",
                 variable=self.listen_boost, bg=C_BOOST_BG, fg=C_TXT,
                 troughcolor=C_PANEL, highlightthickness=0, bd=0,
                 showvalue=True, length=150, font=FONT_SMALL,
                 command=lambda _v=None: self._apply_local_gain()
                 ).pack(side="left", padx=6)

        # account controls
        act = tk.Frame(right, bg=C_PANEL2, highlightbackground=C_BORDER,
                       highlightthickness=1)
        act.pack(fill="x", padx=12, pady=(0, 6))
        a1 = tk.Frame(act, bg=C_PANEL2)
        a1.pack(fill="x", padx=10, pady=(8, 4))
        self.mic_btn = flat_btn(a1, "🔇  Mute mic", lambda: self._toggle_mic(),
                                bg=C_PANEL3)
        self.mic_btn.pack(side="left", padx=(0, 6))
        self.deaf_btn = flat_btn(a1, "🔈  Deafen", lambda: self._toggle_deaf(),
                                 bg=C_PANEL3)
        self.deaf_btn.pack(side="left", padx=(0, 6))
        flat_btn(a1, "🚪  Leave", self._leave, bg=C_DANGER, hover=C_DANGER_H,
                 fg="#ffffff").pack(side="left", padx=(0, 12))
        tk.Label(a1, text="Move to", bg=C_PANEL2, fg=C_MUTED,
                 font=FONT_SMALL).pack(side="left")
        self.move_var = tk.StringVar()
        self.move_combo = ttk.Combobox(a1, textvariable=self.move_var,
                                       style="Dark.TCombobox", width=24,
                                       font=FONT, values=[])
        self.move_combo.pack(side="left", padx=6)
        flat_btn(a1, "➡️  Move", self._move, bg=C_PRIMARY,
                 hover=C_PRIMARY_H, fg="#ffffff").pack(side="left")

        a2 = tk.Frame(act, bg=C_PANEL2)
        a2.pack(fill="x", padx=10, pady=(0, 8))
        tk.Label(a2, text="🎵", bg=C_PANEL2, fg=C_TXT).pack(side="left")
        tk.Entry(a2, textvariable=self.audio_var, bg=C_PANEL, fg=C_TXT,
                 insertbackground=C_TXT, relief="flat", font=FONT, width=30
                 ).pack(side="left", padx=6, ipady=4)
        flat_btn(a2, "📂", lambda: self.app.browse_audio_into(self.audio_var),
                 bg=C_PANEL, padx=8).pack(side="left", padx=(0, 8))
        flat_btn(a2, "▶️ Play", self._play, bg=C_PRIMARY, hover=C_PRIMARY_H,
                 fg="#ffffff").pack(side="left", padx=(0, 4))
        flat_btn(a2, "⏹ Stop", self._stop, bg=C_DANGER, hover=C_DANGER_H,
                 fg="#ffffff").pack(side="left", padx=(0, 4))
        flat_btn(a2, "⏪ −10s", lambda: self._seek(-10), bg=C_PANEL3
                 ).pack(side="left", padx=(0, 4))
        flat_btn(a2, "⏩ +10s", lambda: self._seek(10), bg=C_PANEL3
                 ).pack(side="left", padx=(0, 4))
        flat_btn(a2, "⟲ 0:00", self._restart, bg=C_PANEL3).pack(side="left")
        tk.Checkbutton(a2, text="🔁", variable=self.loop_var, bg=C_PANEL2,
                       fg=C_TXT, selectcolor=C_PANEL, activebackground=C_PANEL2,
                       activeforeground=C_TXT, font=FONT_SMALL, bd=0,
                       highlightthickness=0).pack(side="left", padx=(6, 0))

        a3 = tk.Frame(act, bg=C_BOOST_BG)
        a3.pack(fill="x", padx=10, pady=(0, 8))
        tk.Label(a3, text="🔊 Volume", bg=C_BOOST_BG, fg=C_TXT,
                 font=FONT_SMALL).pack(side="left")
        tk.Scale(a3, from_=0, to=200, orient="horizontal",
                 variable=self.play_vol, bg=C_BOOST_BG, fg=C_TXT,
                 troughcolor=C_PANEL, highlightthickness=0, bd=0,
                 showvalue=True, length=140, font=FONT_SMALL,
                 command=lambda _v=None: self._apply_play_gain()
                 ).pack(side="left", padx=6)
        tk.Label(a3, text="Boost", bg=C_BOOST_BG, fg=C_BOOST_TXT,
                 font=FONT_SMALL).pack(side="left", padx=(12, 0))
        tk.Scale(a3, from_=100, to=800, orient="horizontal",
                 variable=self.play_boost, bg=C_BOOST_BG, fg=C_BOOST_TXT,
                 troughcolor=C_PANEL, highlightthickness=0, bd=0,
                 showvalue=True, length=140, font=FONT_SMALL,
                 command=lambda _v=None: self._apply_play_gain()
                 ).pack(side="left", padx=6)

        # members
        tk.Label(right, text="Members in this channel", bg=C_PANEL, fg=C_MUTED,
                 font=FONT_SMALL).pack(anchor="w", padx=16)
        self.mem_scroll = ScrollArea(right, bg=C_PANEL)
        self.mem_scroll.pack(fill="both", expand=True, padx=8, pady=(2, 10))

    # ---------------- data helpers ----------------
    def _current_acc(self):
        if not self.selected_id:
            return None
        return self.controller.find(self.selected_id)

    def _current_channel(self):
        acc = self._current_acc()
        if acc is None or not acc.voice_channel_id:
            return None
        gid = acc.voice_guild_id
        for aid, w in self.controller.workers.items():
            bot = getattr(w, "bot", None)
            if bot is None:
                continue
            try:
                guild = bot.get_guild(int(gid)) if gid else None
            except Exception:
                guild = None
            if guild is None:
                continue
            try:
                return guild.get_channel(int(acc.voice_channel_id))
            except Exception:
                continue
        return None

    def _tick(self) -> None:
        if not self.winfo_exists():
            return
        try:
            self._do_refresh()
        except Exception as exc:
            print(f"[!] monitor refresh: {exc}")
        self.after(self.REFRESH_MS, self._tick)

    def _do_refresh(self) -> None:
        if not self.winfo_exists():
            return
        # apply any avatars downloaded since the last tick (main thread only)
        try:
            self.avatars.drain()
        except Exception:
            pass
        accs = [a for a in self.controller.accounts if a.voice_channel_id]
        if self.selected_id not in [a.id for a in accs]:
            if self.selected_id is not None:
                self._stop_listen()
            self.selected_id = accs[0].id if accs else None
            self._member_sig = None

        ids = [a.id for a in accs]
        if ids != self._side_ids:
            self._build_sidebar(accs)

        acc = self._current_acc()
        if acc is None:
            self.title_lbl.configure(text="No account is in a voice channel")
            self.sub_lbl.configure(text="Join a voice channel to watch it here.")
            self._build_members(None, None)
            # keep any diagnostic text (e.g. the speaker-test result)
            self.listen_btn.style("🔊  Start listening", bg=C_PANEL, fg=C_MUTED,
                                  enabled=False)
            if not self.listen_status.cget("text"):
                self.listen_status.configure(
                    text="join a voice channel to start listening")
        else:
            self.title_lbl.configure(text=f"{acc.name}  ·  🔊 "
                                          f"{acc.voice_channel_name or 'voice'}")
            count = acc.voice_member_count
            self.sub_lbl.configure(
                text=f"{count} member(s)   ·   "
                     f"{'🔇 muted' if acc.self_mute else '🎙️ unmuted'}   ·   "
                     f"{'🔈 deafened' if acc.self_deaf else '🔊 hearing'}")
            self.mic_btn.style("🔊  Unmute mic" if acc.self_mute else "🔇  Mute mic",
                               bg=C_OK if acc.self_mute else C_PANEL3,
                               hover=C_OK_H if acc.self_mute else C_PANEL3)
            self.deaf_btn.style("🔊  Undeafen" if acc.self_deaf else "🔈  Deafen",
                                bg=C_OK if acc.self_deaf else C_PANEL3,
                                hover=C_OK_H if acc.self_deaf else C_PANEL3)
            chans = self.controller.voice_channels(acc.voice_guild_id) \
                if acc.voice_guild_id else []
            vals = [f"{name} ({cid})" for cid, name in chans]
            self.move_combo.configure(values=vals)
            if not self.move_var.get() and vals:
                cur = f"{acc.voice_channel_name} ({acc.voice_channel_id})"
                self.move_var.set(cur if cur in vals else vals[0])
            self._sync_listener(acc)
            self._refresh_listen_btn()

        self._refresh_members(acc)

    def _sync_listener(self, acc) -> None:
        """Re-bind when the voice connection was re-made.

        Discord reconnects (or the account is moved/rejoined) create a brand
        new VoiceClient; the old socket goes silent, so the listener would
        just sit there hearing nothing.
        """
        if self.listener is None or acc is None:
            return
        w = self.controller.workers.get(acc.id)
        vc = w.voice_client() if w is not None else None
        if vc is None:
            return
        # A reconnect swaps the inner _connection while the VoiceClient object
        # survives, so comparing the client alone would miss it.
        same = (vc is getattr(self.listener, "vc", None)
                and getattr(vc, "_connection", None)
                is getattr(self.listener, "conn", None))
        st = self.listener.stats()
        now = time.time()
        # Same socket but no audio for a while even though others are in the
        # channel: Discord moved us to a new media server without discord.py
        # noticing.  Re-attach so listening heals itself instead of playing
        # silence forever.
        stalled = (same and st.get("packets", 0) > 0
                   and st.get("idle", 0.0) > 15.0
                   and now > self._stall_cooldown
                   and self._others_in_channel(acc))
        if same and not stalled:
            return
        if stalled:
            self._stall_cooldown = now + 45.0
            print("[listen] no audio for 15s -> re-binding listener")
        else:
            print("[listen] voice connection changed -> re-binding listener")
        muted = dict(self.local_muted)
        self._stop_listen()
        if not LISTEN_OK:
            return
        try:
            lst = voice_listen.VoiceListener(vc)
            if lst.start(timeout=2.0):
                self.listener = lst
                self.listener_acc = acc.id
                for uid, m in muted.items():
                    if m:
                        lst.mute_user(uid, True)
                self._apply_local_gain()
            elif acc.voice_channel_id:
                # new socket not ready yet - retry from the timer
                self.after(600, self._start_listen)
        except Exception as exc:
            print(f"[listen] re-bind failed: {exc}")

    def _others_in_channel(self, acc) -> bool:
        ch = self._current_channel()
        if ch is None:
            return False
        try:
            return len(list(ch.members)) > 1
        except Exception:
            return False

    def _refresh_listen_btn(self) -> None:
        on = self.listener is not None
        self.listen_btn.style("⏹  Stop listening" if on else "🔊  Start listening",
                              bg=C_DANGER if on else C_OK,
                              hover=C_DANGER_H if on else C_OK_H,
                              fg="#ffffff", enabled=True)
        if not on or self.listener is None:
            self._undeafening = False
            return
        acc = self._current_acc()
        # Discord sends no audio at all to a deafened client, so a deafened
        # account hears silence no matter how good the decoder is.  Undo it
        # quietly while listening, then carry on.
        if acc is not None and getattr(acc, "self_deaf", False):
            if not self._undeafening or time.time() - self._undeafen_at > 4.0:
                self._undeafening = True
                self._undeafen_at = time.time()
                self.controller.set_voice_state([acc.id], deaf=False)
                self.listen_status.configure(
                    text="undeafening this account so it can hear…")
            return
        self._undeafening = False

        st = self.listener.stats()
        if not st.get("speaker_ok", True):
            self.listen_status.configure(
                text="⚠ no audio output device — "
                     f"{st.get('speaker_error') or 'device error'}")
            return
        if st.get("recording"):
            name = os.path.basename(st.get("record_path") or "capture.wav")
            self.listen_status.configure(text=f"⏺ recording what you hear → {name}")
            return
        if self._record_last and time.time() - self._record_last_t < 12:
            name = os.path.basename(self._record_last)
            self.listen_status.configure(text=f"✔ saved what you heard → {name}")
            return
        lvl = int(min(8, st.get("peak", 0) / 4096))
        meter = "█" * lvl + "·" * (8 - lvl)
        extra = ""
        if st["decoded"] == 0:
            if st.get("dave_fail"):
                extra = "  ·  E2EE: resolving speaker keys…"
            elif not st.get("packets"):
                if acc is not None and self._others_in_channel(acc):
                    extra = "  ·  the server is not sending audio yet"
                else:
                    extra = "  ·  nobody else is here to hear"
            else:
                extra = "  ·  waiting for someone to speak"
        elif st.get("dave_fail"):
            extra = "  ·  E2EE: resolving speaker keys…"
        self.listen_status.configure(
            text=f"● {meter}  {st['decoded']}f/{st['packets']}p "
                 f"({len(self.listener.speakers())} spk){extra}")

    def _record_listen(self) -> None:
        """Save 10 seconds of exactly what the speakers are given."""
        if self.listener is None:
            self.listen_status.configure(text="start listening first")
            return
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        folder = desktop if os.path.isdir(desktop) else os.environ.get("TEMP",
                                                                      ".")
        out = os.path.join(folder,
                           f"voicehive_hear_{time.strftime('%H%M%S')}.wav")
        if self.listener.record(out, 10.0):
            self._record_last = out
            self._record_last_t = time.time()
            self.listen_status.configure(
                text=f"⏺ recording 10s → {os.path.basename(out)}")
        else:
            self.listen_status.configure(
                text="recording failed: "
                     f"{getattr(self.listener, 'record_error', '') or 'error'}")

    def _test_speaker(self) -> None:
        if not LISTEN_OK:
            self.listen_status.configure(
                text=f"listen module unavailable: {LISTEN_ERR}")
            return
        self.listen_status.configure(text="playing a test tone…")
        self._tone_msg = None

        def run():
            try:
                ok, err = voice_listen.test_tone()
            except Exception as exc:
                ok, err = False, str(exc)
            # only the main thread touches widgets; hand the text over here
            self._tone_msg = ("test tone played 🔊 — did you hear it?" if ok
                              else f"⚠ audio output problem: {err}")

        threading.Thread(target=run, daemon=True).start()
        self.after(200, self._poll_tone)

    def _poll_tone(self) -> None:
        if not self.winfo_exists():
            return
        msg = getattr(self, "_tone_msg", None)
        if msg is None:
            self.after(200, self._poll_tone)
            return
        try:
            self.listen_status.configure(text=msg)
        except Exception:
            pass

    # ---------------- sidebar ----------------
    def _build_sidebar(self, accs) -> None:
        for w in self.side_scroll.inner.winfo_children():
            w.destroy()
        self._side_ids = [a.id for a in accs]
        for a in accs:
            sel = (a.id == self.selected_id)
            b = PillButton(self.side_scroll.inner,
                           lambda i=a.id: self._select(i), padx=10, pady=6)
            b.style(f"{a.name}\n🔊 {a.voice_channel_name or 'voice'}",
                    bg=C_PRIMARY if sel else C_PANEL3,
                    hover=C_PRIMARY_H if sel else C_PANEL3,
                    fg="#ffffff" if sel else C_TXT)
            b.pack(fill="x", padx=4, pady=3)

    def _select(self, acc_id: str) -> None:
        if self.selected_id != acc_id:
            self.selected_id = acc_id
            self._member_sig = None
            self._stop_listen()
            try:
                self._do_refresh()
            except Exception:
                pass

    # ---------------- members ----------------
    def _refresh_members(self, acc) -> None:
        channel = self._current_channel()
        me_id = ""
        if acc is not None:
            w = self.controller.workers.get(acc.id)
            if (w is not None and getattr(w, "bot", None) is not None
                    and w.bot.user is not None):
                me_id = str(w.bot.user.id)
        members = list(getattr(channel, "members", []) or []) if channel else []
        sig = (str(getattr(channel, "id", "")),
               tuple(sorted((str(getattr(m, "id", "")),
                             bool(getattr(getattr(m, "voice", None), "mute", False)),
                             bool(getattr(getattr(m, "voice", None), "self_mute", False)),
                             self.local_muted.get(str(getattr(m, "id", "")), False))
                            for m in members)))
        if sig == self._member_sig:
            return
        self._member_sig = sig
        self._build_members(channel, me_id, members)

    def _build_members(self, channel, me_id, members=None) -> None:
        inner = self.mem_scroll.inner
        for w in inner.winfo_children():
            w.destroy()
        self._avatar_labels = {}
        if channel is None:
            tk.Label(inner, text="No voice channel to show.",
                     bg=C_PANEL, fg=C_MUTED, font=FONT).pack(pady=24)
            return
        if members is None:
            members = list(getattr(channel, "members", []) or [])
        members.sort(key=lambda m: (getattr(m, "display_name", "") or "").lower())
        if not members:
            tk.Label(inner, text="Nobody else is here yet.",
                     bg=C_PANEL, fg=C_MUTED, font=FONT).pack(pady=24)
            return
        for m in members:
            mid = str(getattr(m, "id", ""))
            row = tk.Frame(inner, bg=C_PANEL2, highlightbackground=C_BORDER,
                           highlightthickness=1)
            row.pack(fill="x", padx=4, pady=3)

            av = tk.Label(row, bg=C_PANEL2, text="👤", fg=C_MUTED, font=FONT_H)
            av.pack(side="left", padx=8, pady=6)
            self._avatar_labels[mid] = av
            img = self.avatars.get(m)
            if img is not None:
                av.configure(image=img, text="")
                av.image = img

            info = tk.Frame(row, bg=C_PANEL2)
            info.pack(side="left", fill="x", expand=True)
            tk.Label(info, text=getattr(m, "display_name", "?"), bg=C_PANEL2,
                     fg=C_TXT, font=FONT_BOLD, anchor="w").pack(anchor="w")
            st = getattr(m, "voice", None)
            bits = []
            if st is not None:
                if getattr(st, "mute", False):
                    bits.append("server-muted")
                elif getattr(st, "self_mute", False):
                    bits.append("muted")
                if getattr(st, "self_deaf", False) or getattr(st, "deaf", False):
                    bits.append("deafened")
            if mid == me_id:
                bits.append("this account")
            if self.local_muted.get(mid):
                bits.append("hidden locally")
            tk.Label(info, text=" · ".join(bits) or "in voice", bg=C_PANEL2,
                     fg=C_MUTED, font=FONT_SMALL, anchor="w").pack(anchor="w")

            muted = self.local_muted.get(mid, False)
            btn = PillButton(row, lambda u=mid: self._toggle_member_mute(u),
                             padx=10, pady=4, width=11)
            btn.style("🔈  Unhide" if muted else "🔇  Mute",
                      bg=C_OK if muted else C_PANEL3,
                      hover=C_OK_H if muted else C_PANEL3)
            btn.pack(side="right", padx=8)

    def _on_avatar(self, key: str, img) -> None:
        lbl = self._avatar_labels.get(key)
        try:
            if lbl is not None and lbl.winfo_exists():
                lbl.configure(image=img, text="")
                lbl.image = img
        except Exception:
            pass

    # ---------------- member mute ----------------
    def _toggle_member_mute(self, user_id: str) -> None:
        acc = self._current_acc()
        channel = self._current_channel()
        # try a real (server) mute when this account has the permission
        try:
            if channel is not None and acc is not None:
                w = self.controller.workers.get(acc.id)
                guild = channel.guild
                member = guild.get_member(int(user_id))
                perms = channel.permissions_for(guild.me)
                me_id = None
                if (w is not None and getattr(w, "bot", None) is not None
                        and w.bot.user is not None):
                    me_id = w.bot.user.id
                if (member is not None and perms.mute_members
                        and member.id != me_id):
                    new = not bool(getattr(getattr(member, "voice", None),
                                           "mute", False))
                    LOOP.submit(member.edit(mute=new))
                    self.listen_status.configure(
                        text=f"server-muted {member.display_name}" if new
                        else f"unmuted {member.display_name}")
                    return
        except Exception:
            pass
        # otherwise hide them locally in our own listening
        new = not self.local_muted.get(user_id, False)
        self.local_muted[user_id] = new
        if self.listener is not None:
            self.listener.mute_user(user_id, new)
        self._member_sig = None
        self.listen_status.configure(
            text=("hidden locally (no server-mute permission)"
                  if new else "unhidden locally"))

    # ---------------- listen ----------------
    def toggle_listen(self) -> None:
        if self.listener is not None:
            self._stop_listen()
            self._refresh_listen_btn()
            return
        acc = self._current_acc()
        if acc is None:
            self.listen_status.configure(
                text="pick the account that is in voice from the list on the left")
            return
        if not acc.voice_channel_id:
            self.listen_status.configure(
                text="that account is not in a voice channel yet — join first")
            return
        if not LISTEN_OK:
            self.listen_status.configure(
                text=f"listen module unavailable: {LISTEN_ERR}")
            return
        if acc.self_deaf:
            # Discord stops sending audio to deafened clients
            self.controller.set_voice_state([acc.id], deaf=False)
            self.listen_status.configure(text="undeafening, starting…")
            self.after(900, self._start_listen)
        else:
            self._start_listen()

    def _start_listen(self, attempt: int = 0) -> None:
        if self.listener is not None:
            return
        acc = self._current_acc()
        if acc is None:
            self.listen_status.configure(
                text="pick the account that is in voice from the list on the left")
            return
        w = self.controller.workers.get(acc.id)
        vc = w.voice_client() if w is not None else None
        if vc is None:
            # a fresh join can take a moment to expose the voice client
            if acc.voice_channel_id and attempt < 10:
                self.listen_status.configure(
                    text=f"waiting for voice connection… ({attempt + 1})")
                self.after(600, lambda: self._start_listen(attempt + 1))
                return
            self.listen_status.configure(text="that account is not in voice yet")
            self._refresh_listen_btn()
            return
        try:
            lst = voice_listen.VoiceListener(vc)
            ok = lst.start()
        except Exception as exc:
            lst = None
            ok = False
            self.listen_status.configure(text=f"listen error: {exc}")
        if ok:
            self.listener = lst
            self.listener_acc = acc.id
            for uid, muted in self.local_muted.items():
                if muted:
                    lst.mute_user(uid, True)
            self._apply_local_gain()
            self._refresh_listen_btn()
            return
        self._refresh_listen_btn()
        if acc.voice_channel_id and attempt < 12:
            # The voice client exists but its UDP socket is not up yet (a
            # slow join or DAVE/E2EE negotiation).  Keep retrying instead of
            # failing silently, so listening works on every server.
            self.listen_status.configure(
                text=f"waiting for voice connection… ({attempt + 1})")
            self.after(500, lambda: self._start_listen(attempt + 1))
            return
        # out of retries - say *why* instead of leaving a stale message
        if not LISTEN_OK:
            self.listen_status.configure(
                text=f"listen module unavailable: {LISTEN_ERR}")
        elif not voice_listen.ensure_opus():
            self.listen_status.configure(
                text="opus decoder missing — run: py -3.13 -m pip install "
                     "\"discord.py-self[voice]\"")
        else:
            self.listen_status.configure(
                text="could not attach to the voice socket — use Leave then "
                     "Join again")

    def _stop_listen(self) -> None:
        lst = self.listener
        self.listener = None
        self.listener_acc = None
        if lst is not None:
            try:
                lst.stop()
            except Exception:
                pass
        try:
            self.listen_status.configure(text="")
        except Exception:
            pass

    def _apply_local_gain(self) -> None:
        if self.listener is None:
            return
        try:
            gain = (max(0, self.listen_vol.get()) / 100.0) * \
                   (max(0, self.listen_boost.get()) / 100.0)
            self.listener.set_gain(gain)
        except Exception:
            pass

    # ---------------- account controls ----------------
    def _toggle_mic(self) -> None:
        acc = self._current_acc()
        if acc is None:
            return
        self.controller.set_voice_state([acc.id], mute=not acc.self_mute)

    def _toggle_deaf(self) -> None:
        acc = self._current_acc()
        if acc is None:
            return
        new = not acc.self_deaf
        self.controller.set_voice_state([acc.id], deaf=new)
        if new and self.listener is not None:
            self._stop_listen()
            self._refresh_listen_btn()

    def _leave(self) -> None:
        acc = self._current_acc()
        if acc is None:
            return
        self._stop_listen()
        self.controller.leave_voice([acc.id])

    def _move(self) -> None:
        acc = self._current_acc()
        if acc is None:
            return
        cid = parse_id(self.move_var.get())
        if not cid or not acc.voice_guild_id:
            return
        self.controller.move_account(acc.id, acc.voice_guild_id, cid)

    def _play(self) -> None:
        acc = self._current_acc()
        if acc is None:
            return
        path = self.audio_var.get().strip() or getattr(self.app, "last_audio_path", "")
        if not path or not os.path.exists(path):
            self.listen_status.configure(text="pick a valid audio file first")
            return
        self.app.last_audio_path = path
        p = self.audio_var.get().strip() or path
        self.controller.play_song([acc.id], p,
                                  max(0, self.play_vol.get()) / 100.0,
                                  max(0, self.play_boost.get()) / 100.0,
                                  bool(self.loop_var.get()))

    def _stop(self) -> None:
        acc = self._current_acc()
        if acc is not None:
            self.controller.stop_audio([acc.id])

    def _apply_play_gain(self) -> None:
        """Live volume / boost for the song this account is playing."""
        acc = self._current_acc()
        if acc is None:
            return
        try:
            self.controller.set_playback_volume(
                max(0, self.play_vol.get()) / 100.0, [acc.id])
            self.controller.set_playback_boost(
                max(0, self.play_boost.get()) / 100.0, [acc.id])
        except Exception:
            pass

    def _seek(self, delta: int) -> None:
        acc = self._current_acc()
        if acc is not None:
            self.controller.seek_relative(delta, [acc.id])

    def _restart(self) -> None:
        acc = self._current_acc()
        if acc is None:
            return
        path = self.audio_var.get().strip() or getattr(self.app, "last_audio_path", "")
        if path and os.path.exists(path):
            self.controller.play_song([acc.id], path,
                                      max(0, self.play_vol.get()) / 100.0,
                                      max(0, self.play_boost.get()) / 100.0,
                                      bool(self.loop_var.get()), restart=True)

    # ---------------- close ----------------
    def on_close(self) -> None:
        self._stop_listen()
        try:
            self.destroy()
        except Exception:
            pass


# ======================================================================
# Routing window (detached): send each account to its own channel
# ======================================================================
class RoutingWindow(tk.Toplevel):
    REFRESH_MS = 2500

    def __init__(self, app: "App") -> None:
        super().__init__(app)
        self.app = app
        self.controller = app.controller
        self.title("🗺️  Route accounts to channels")
        self.geometry("940x640")
        self.minsize(760, 460)
        self.configure(bg=C_BG)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.rows: Dict[str, dict] = {}
        self._build()
        self._refresh()

    def _build(self) -> None:
        head = tk.Frame(self, bg=C_BG)
        head.pack(fill="x", padx=12, pady=(12, 4))
        tk.Label(head, text="🗺️  Scatter accounts", bg=C_BG, fg=C_TXT,
                 font=FONT_H).pack(side="left")
        tk.Label(head, text="choose a server + channel for each account, "
                            "then Dispatch",
                 bg=C_BG, fg=C_MUTED, font=FONT_SMALL).pack(side="left", padx=10)
        flat_btn(head, "🚀  Dispatch all", self._dispatch_all, bg=C_PRIMARY,
                 hover=C_PRIMARY_H, fg="#ffffff").pack(side="right")
        flat_btn(head, "🔄  Refresh channels", self._refresh_channels,
                 bg=C_PANEL3).pack(side="right", padx=6)

        bar = tk.Frame(self, bg=C_PANEL, highlightbackground=C_BORDER,
                       highlightthickness=1)
        bar.pack(fill="x", padx=12, pady=(6, 2))
        for text, w in (("Account", 14), ("Server", 34), ("Voice channel", 34), ("", 6)):
            tk.Label(bar, text=text, bg=C_PANEL, fg=C_MUTED, font=FONT_SMALL,
                     width=w, anchor="w").pack(side="left", padx=6, pady=6)

        self.status = tk.Label(self, text="", bg=C_BG, fg=C_MUTED,
                               font=FONT_SMALL, anchor="w")
        self.status.pack(fill="x", padx=14)

        body = card(self)
        body.pack(fill="both", expand=True, padx=12, pady=(4, 12))
        self.scroll = ScrollArea(body, bg=C_PANEL)
        self.scroll.pack(fill="both", expand=True, padx=6, pady=6)

    def _known_guilds(self) -> List[str]:
        g = self.controller.known_guilds()
        return [f"{name} ({gid})" for name, gid in
                sorted(((n, i) for i, n in g.items()), key=lambda kv: kv[0].lower())]

    def _refresh(self) -> None:
        if not self.winfo_exists():
            return
        accs = list(self.controller.accounts)
        ids = [a.id for a in accs]
        for aid in list(self.rows.keys()):
            if aid not in ids:
                self.rows.pop(aid)["frame"].destroy()
        guild_values = self._known_guilds()
        for acc in accs:
            row = self.rows.get(acc.id)
            if row is None:
                row = self._make_row(acc)
                self.rows[acc.id] = row
            row["name"].configure(
                text=("🟢 " if acc.logged_in else "⚪ ") + (acc.name or "?"))
            if list(row["guild_combo"].cget("values")) != guild_values:
                row["guild_combo"].configure(values=guild_values)
        self.after(self.REFRESH_MS, self._refresh)

    def _make_row(self, acc: Account) -> dict:
        frame = tk.Frame(self.scroll.inner, bg=C_PANEL2,
                         highlightbackground=C_BORDER, highlightthickness=1)
        frame.pack(fill="x", padx=4, pady=3)

        name = tk.Label(frame, text=acc.name, bg=C_PANEL2, fg=C_TXT,
                        font=FONT_BOLD, width=14, anchor="w")
        name.pack(side="left", padx=6, pady=5)

        guild_var = tk.StringVar()
        guild_combo = ttk.Combobox(frame, textvariable=guild_var,
                                   style="Dark.TCombobox", width=32, font=FONT,
                                   values=[])
        guild_combo.pack(side="left", padx=6)
        guild_combo.bind("<<ComboboxSelected>>",
                         lambda _e, i=acc.id: self._pick_guild(i))

        chan_var = tk.StringVar()
        chan_combo = ttk.Combobox(frame, textvariable=chan_var,
                                  style="Dark.TCombobox", width=32, font=FONT,
                                  values=[])
        chan_combo.pack(side="left", padx=6)

        go = flat_btn(frame, "Go", lambda i=acc.id: self._go(i),
                      bg=C_PRIMARY, hover=C_PRIMARY_H, fg="#ffffff", padx=10)
        go.pack(side="left", padx=6)

        return {"frame": frame, "name": name, "guild_var": guild_var,
                "guild_combo": guild_combo, "chan_var": chan_var,
                "chan_combo": chan_combo, "chans": []}

    def _pick_guild(self, acc_id: str) -> None:
        row = self.rows.get(acc_id)
        if row is None:
            return
        gid = parse_id(row["guild_var"].get())
        chans = self.controller.voice_channels(gid) if gid else []
        row["chans"] = [f"{name} ({cid})" for cid, name in chans]
        row["chan_combo"].configure(values=row["chans"])
        if row["chans"] and parse_id(row["chan_var"].get()) not in \
                [parse_id(v) for v in row["chans"]]:
            row["chan_var"].set(row["chans"][0])

    def _refresh_channels(self) -> None:
        for acc_id in list(self.rows.keys()):
            if parse_id(self.rows[acc_id]["guild_var"].get()):
                self._pick_guild(acc_id)
        self.status.configure(text="channel lists refreshed")

    def _go(self, acc_id: str) -> None:
        row = self.rows.get(acc_id)
        if row is None:
            return
        gid = parse_id(row["guild_var"].get())
        cid = parse_id(row["chan_var"].get())
        if not gid or not cid:
            self.status.configure(text="pick both a server and a channel")
            return
        self.controller.move_account(acc_id, gid, cid)
        self.status.configure(text=f"sent {self.controller.find(acc_id).name} → channel")

    def _dispatch_all(self) -> None:
        intents = []
        for acc_id, row in self.rows.items():
            gid = parse_id(row["guild_var"].get())
            cid = parse_id(row["chan_var"].get())
            if gid and cid:
                intents.append((acc_id, gid, cid))
        if not intents:
            self.status.configure(text="nothing to dispatch — pick channels first")
            return
        self.controller.dispatch_route(intents)
        self.status.configure(text=f"dispatching {len(intents)} account(s)…")

    def on_close(self) -> None:
        try:
            self.destroy()
        except Exception:
            pass


# ======================================================================
if __name__ == "__main__":
    app = App()
    app.mainloop()
