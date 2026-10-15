"""Pull other members' audio out of a Discord voice channel and play it on the
local machine through the Windows waveOut device.

There's no "receive" API in discord.py-self, so this borrows the pieces its
voice client already sets up:

  * VoiceConnectionState.add_socket_listener hands us every UDP packet the
    socket sees, not just the ones we send.
  * the voice websocket hook gives us SPEAKING, which is the only place the
    server tells us which ssrc belongs to which user.
  * discord.opus ships a copy of libopus we can decode with.
  * davey decrypts the DAVE (E2EE) frames on channels that use it.

Other than those, this is all standard library. Playback goes through winmm via
ctypes so there's nothing extra to install.
"""
from __future__ import annotations

import ctypes
import os
import struct
import threading
import time
import wave
from array import array
from collections import OrderedDict
from typing import Callable, Dict, Optional

try:
    import discord.opus as dopus
    OPUS_MODULE = True
except Exception:  # pragma: no cover
    dopus = None
    OPUS_MODULE = False

try:
    import davey  # type: ignore
    DAVEY_OK = True
except Exception:  # pragma: no cover
    davey = None
    DAVEY_OK = False


_TRANSPORT_MODES = ("aead_xchacha20_poly1305_rtpsize", "xsalsa20_poly1305",
                    "xsalsa20_poly1305_suffix", "xsalsa20_poly1305_lite")


def _decrypt_transport(key: bytes, mode: str, header: bytes,
                       body: bytes) -> bytes:
    """Try one transport cipher over ``body`` with ``header`` as AAD.

    Returns the plaintext, or ``b""`` when this framing/cipher does not
    match (the AEAD tag makes a wrong guess fail deterministically, so the
    caller can safely try several candidates).
    """
    import nacl.secret
    try:
        if mode == "aead_xchacha20_poly1305_rtpsize":
            nonce = bytearray(24)
            nonce[:4] = body[-4:]
            return nacl.secret.Aead(key).decrypt(body[:-4], header,
                                                 bytes(nonce))
        if mode == "xsalsa20_poly1305":
            nonce = bytearray(24)
            nonce[:min(len(header), 24)] = header[:24]
            return nacl.secret.SecretBox(key).decrypt(body, bytes(nonce))
        if mode == "xsalsa20_poly1305_suffix":
            return nacl.secret.SecretBox(key).decrypt(body[:-24], body[-24:])
        if mode == "xsalsa20_poly1305_lite":
            nonce = bytearray(24)
            nonce[:4] = body[-4:]
            return nacl.secret.SecretBox(key).decrypt(body[:-4], bytes(nonce))
    except Exception:
        return b""
    return b""


_opus_lock = threading.Lock()


def ensure_opus() -> bool:
    """Load the libopus DLL bundled with discord.py-self (once)."""
    if not OPUS_MODULE:
        return False
    with _opus_lock:
        try:
            if dopus.is_loaded():
                return True
            base = os.path.join(os.path.dirname(dopus.__file__), "bin")
            for name in ("libopus-0.x64.dll", "libopus-0.x86.dll"):
                path = os.path.join(base, name)
                if os.path.isfile(path):
                    dopus.load_opus(path)
                    if dopus.is_loaded():
                        return True
        except Exception:
            return False
    return bool(OPUS_MODULE and dopus is not None and dopus.is_loaded())


def speaker_available() -> bool:
    return os.name == "nt"


# ----------------------------------------------------------------------
# Windows waveOut streaming speaker (no external dependency)
# ----------------------------------------------------------------------
class _WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", ctypes.c_uint16),
        ("nChannels", ctypes.c_uint16),
        ("nSamplesPerSec", ctypes.c_uint32),
        ("nAvgBytesPerSec", ctypes.c_uint32),
        ("nBlockAlign", ctypes.c_uint16),
        ("wBitsPerSample", ctypes.c_uint16),
        ("cbSize", ctypes.c_uint16),
    ]


class _WAVEHDR(ctypes.Structure):
    _fields_ = [
        ("lpData", ctypes.c_void_p),
        ("dwBufferLength", ctypes.c_uint32),
        ("dwBytesRecorded", ctypes.c_uint32),
        ("dwUser", ctypes.c_void_p),
        ("dwFlags", ctypes.c_uint32),
        ("dwLoops", ctypes.c_uint32),
        ("lpNext", ctypes.c_void_p),
        ("reserved", ctypes.c_void_p),
    ]


_WHDR_DONE = 0x00000001
_WAVE_MAPPER = 0xFFFFFFFF


class Speaker:
    """A small blocking streaming player built on ``winmm`` waveOut."""

    def __init__(self, rate: int = 48000, channels: int = 2,
                 frame_bytes: int = 3840, buffers: int = 10) -> None:
        self.rate = rate
        self.channels = channels
        self.frame_bytes = frame_bytes
        self._buffers = max(3, buffers)
        self._lock = threading.Lock()
        self._inflight = []
        self._free = []
        self._winmm = None
        self._device = None
        self.gain = 1.0
        self.error = ""

    def start(self) -> bool:
        if os.name != "nt":
            self.error = "local playback needs Windows"
            return False
        try:
            winmm = ctypes.windll.winmm
        except Exception as exc:  # pragma: no cover
            self.error = f"winmm: {exc}"
            return False

        winmm.waveOutOpen.argtypes = [
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint,
            ctypes.POINTER(_WAVEFORMATEX), ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_uint]
        winmm.waveOutPrepareHeader.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_WAVEHDR), ctypes.c_uint]
        winmm.waveOutWrite.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_WAVEHDR), ctypes.c_uint]
        winmm.waveOutUnprepareHeader.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_WAVEHDR), ctypes.c_uint]
        winmm.waveOutReset.argtypes = [ctypes.c_void_p]
        winmm.waveOutClose.argtypes = [ctypes.c_void_p]

        wfx = _WAVEFORMATEX()
        wfx.wFormatTag = 1
        wfx.nChannels = self.channels
        wfx.nSamplesPerSec = self.rate
        wfx.wBitsPerSample = 16
        wfx.nBlockAlign = self.channels * 2
        wfx.nAvgBytesPerSec = self.rate * wfx.nBlockAlign
        wfx.cbSize = 0

        handle = ctypes.c_void_p()
        rc = winmm.waveOutOpen(ctypes.byref(handle), _WAVE_MAPPER,
                               ctypes.byref(wfx), None, None, 0)
        if rc != 0:
            # 0 = MMSYSERR_NOERROR; anything else means we have no output.
            self.error = f"waveOutOpen failed (MMSYSERR {rc})"
            return False
        self.error = ""
        self._winmm = winmm
        self._device = handle
        for _ in range(self._buffers):
            buf = ctypes.create_string_buffer(self.frame_bytes)
            hdr = _WAVEHDR()
            self._free.append((hdr, buf))
        return True

    def _reap(self) -> None:
        still = []
        for hdr, buf in self._inflight:
            if hdr.dwFlags & _WHDR_DONE:
                try:
                    self._winmm.waveOutUnprepareHeader(
                        self._device, ctypes.byref(hdr),
                        ctypes.sizeof(_WAVEHDR))
                except Exception:
                    pass
                self._free.append((hdr, buf))
            else:
                still.append((hdr, buf))
        self._inflight = still

    def feed(self, pcm: bytes) -> None:
        pcm = _apply_gain(pcm, self.gain)
        pcm = pcm[:self.frame_bytes].ljust(self.frame_bytes, b"\x00")
        if self._winmm is None or self._device is None:
            return
        with self._lock:
            if not self._free:
                self._reap()
            waited = 0.0
            while not self._free and waited < 0.25:
                time.sleep(0.002)
                waited += 0.002
                self._reap()
            if not self._free:
                return
            hdr, buf = self._free.pop()
            ctypes.memmove(buf, pcm, self.frame_bytes)
            hdr.lpData = ctypes.cast(buf, ctypes.c_void_p)
            hdr.dwBufferLength = self.frame_bytes
            hdr.dwFlags = 0
            self._winmm.waveOutPrepareHeader(
                self._device, ctypes.byref(hdr), ctypes.sizeof(_WAVEHDR))
            self._winmm.waveOutWrite(
                self._device, ctypes.byref(hdr), ctypes.sizeof(_WAVEHDR))
            self._inflight.append((hdr, buf))

    def close(self) -> None:
        if self._winmm is None:
            return
        try:
            self._winmm.waveOutReset(self._device)
        except Exception:
            pass
        time.sleep(0.05)
        try:
            self._winmm.waveOutClose(self._device)
        except Exception:
            pass
        self._winmm = None
        self._device = None


def _apply_gain(data: bytes, gain: float) -> bytes:
    if not data or gain == 1.0:
        return data
    if gain <= 0.0:
        return bytes(len(data))
    samples = array("h")
    samples.frombytes(data)
    out = array("h", (_clamp(int(s * gain)) for s in samples))
    return out.tobytes()


def _clamp(v: int) -> int:
    if v > 32767:
        return 32767
    if v < -32768:
        return -32768
    return v


def test_tone(seconds: float = 1.0, freq: float = 523.25):
    """Play a short beep locally so the user can check their speakers.

    Returns ``(ok, error)`` so the UI can show why it failed.
    """
    if os.name != "nt":
        return False, "local playback only works on Windows"
    spk = Speaker()
    if not spk.start():
        return False, spk.error or "could not open the audio device"
    import math
    rate = spk.rate
    total = int(rate * max(0.2, seconds))
    per = 960
    try:
        made = 0
        while made < total:
            n = min(per, total - made)
            buf = array("h")
            for i in range(n):
                t = (made + i) / rate
                # fade the ends so it does not click
                env = min(1.0, (made + i) / (rate * 0.03),
                          (total - made - i) / (rate * 0.03))
                s = int(0.35 * 32767 * env * math.sin(2 * math.pi * freq * t))
                buf.append(s)
                buf.append(s)
            made += n
            spk.feed(buf.tobytes())       # already interleaved stereo L,R
    finally:
        spk.close()
    return True, ""


# ----------------------------------------------------------------------
# The listener / mixer
# ----------------------------------------------------------------------
class VoiceListener:
    """Listens to one voice connection and plays the mixed audio locally."""

    FRAME_SAMPLES = 960
    FRAME_BYTES = 3840

    def __init__(self, voice_client,
                 on_status: Optional[Callable] = None) -> None:
        self.vc = voice_client
        # capture the connection object now: on a Discord reconnect the same
        # VoiceClient survives but its ``_connection`` is replaced, which
        # would silently leave us listening to a dead socket
        self.conn = getattr(voice_client, "_connection", None)
        self.on_status = on_status
        self._running = False
        self._lock = threading.Lock()
        self._ssrc_user: Dict[int, str] = {}
        self._decoders: Dict[int, object] = {}
        self._mix: "OrderedDict[int, dict]" = OrderedDict()
        self._ssrc_base: Dict[int, tuple] = {}
        self._muted_users = set()
        self._t0: Optional[float] = None
        self._delay_slots = 4           # ~80 ms jitter buffer
        self._speaker = Speaker()
        self.speaker_ok = False
        self.speaker_error = ""
        self._peak = 0                  # recent audio level (0..32767)
        self._bound_packet = self._on_packet
        self._hook_prev = None
        self._stats = {"packets": 0, "decoded": 0, "own": 0,
                       "rtp_skip": 0, "decrypt_fail": 0, "dave_fail": 0,
                       "decode_fail": 0, "muted": 0}
        self._last_err = ""
        self._last_pkt = 0.0            # wall clock of the last packet heard
        self._recorder = None           # WAV writer for "save what I hear"
        self._record_path = ""
        self._record_until = 0.0
        self.record_error = ""

    # -- lifecycle ----------------------------------------------------
    def start(self, timeout: float = 0.0) -> bool:
        """Attach to the voice socket and start decoding.

        ``timeout`` seconds are spent waiting for the UDP socket when the
        voice handshake has not finished yet - a fresh join can take a moment,
        especially while DAVE/E2EE negotiates, and failing immediately there
        is why "listening" used to do nothing.  The GUI passes 0 and retries
        from a timer so the main thread is never blocked.
        """
        if not ensure_opus():
            print("[listen] opus decoder unavailable")
            return False
        if self.vc is None and self.conn is None:
            # the caller grabbed us before the voice client existed; let it
            # retry rather than spinning for the whole timeout
            print("[listen] no voice client")
            return False
        try:
            from discord.utils import MISSING as _MISSING
        except Exception:  # pragma: no cover
            _MISSING = None
        deadline = time.time() + max(0.0, timeout)
        while True:
            conn = getattr(self.vc, "_connection", None) or self.conn
            sock = getattr(conn, "socket", None) if conn is not None else None
            if sock is not None and sock is not _MISSING:
                self.conn = conn
                break
            if time.time() >= deadline:
                print("[listen] voice connection not ready")
                return False
            time.sleep(0.2)

        try:
            ws = conn.ws
            prev = getattr(ws, "_hook", None)

            async def hook(sock, msg, _prev=prev):
                try:
                    self._on_ws(msg)
                except Exception:
                    pass
                if _prev is not None and _prev is not hook:
                    try:
                        await _prev(sock, msg)
                    except Exception:
                        pass

            ws._hook = hook
            self._hook_prev = prev
        except Exception as exc:
            print(f"[listen] hook setup: {exc}")

        try:
            conn.add_socket_listener(self._bound_packet)
        except Exception as exc:
            print(f"[listen] socket listener: {exc}")
            return False

        try:
            self.speaker_ok = bool(self._speaker.start())
            self.speaker_error = self._speaker.error
        except Exception as exc:
            self.speaker_ok = False
            self.speaker_error = str(exc)
        if not self.speaker_ok:
            print(f"[listen] speaker unavailable: {self.speaker_error}")

        self._running = True
        threading.Thread(target=self._mix_loop, name="voice-mix",
                         daemon=True).start()
        print("[listen] started (decoding others' audio)")
        return True

    def stop(self) -> None:
        self._running = False
        self.stop_record()
        conn = self.conn or getattr(self.vc, "_connection", None)
        try:
            conn.remove_socket_listener(self._bound_packet)
        except Exception:
            pass
        try:
            ws = conn.ws
            if self._hook_prev is not None:
                ws._hook = self._hook_prev
            else:
                ws._hook = type(ws)._hook.__get__(ws)
        except Exception:
            pass
        try:
            self._speaker.close()
        except Exception:
            pass
        print("[listen] stopped")

    def set_gain(self, gain: float) -> None:
        self._speaker.gain = max(0.0, float(gain))

    def mute_user(self, user_id: str, muted: bool) -> None:
        uid = str(user_id)
        if muted:
            self._muted_users.add(uid)
        else:
            self._muted_users.discard(uid)

    def is_muted(self, user_id: str) -> bool:
        return str(user_id) in self._muted_users

    def speakers(self):
        return dict(self._ssrc_user)

    def stats(self) -> dict:
        out = dict(self._stats)
        out["users"] = len(self._ssrc_user)
        out["speaker_ok"] = self.speaker_ok
        out["speaker_error"] = self.speaker_error
        out["last_err"] = self._last_err
        out["peak"] = self._peak
        out["idle"] = (time.time() - self._last_pkt) if self._last_pkt else -1.0
        out["recording"] = self._recorder is not None
        out["record_path"] = self._record_path
        out["record_error"] = self.record_error
        return out

    # -- "save what I hear" ------------------------------------------
    def record(self, path: str, seconds: float = 10.0) -> bool:
        """Write exactly what is going to the speakers into a WAV file.

        This is the ground truth: whatever the file holds is what the app
        really heard, so the user can open it and judge the quality without
        trusting a level meter.
        """
        self.stop_record()
        try:
            w = wave.open(path, "wb")
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(48000)
        except Exception as exc:
            self.record_error = str(exc)
            return False
        self._recorder = w
        self._record_path = path
        self._record_until = time.time() + max(1.0, float(seconds))
        self.record_error = ""
        return True

    def stop_record(self) -> str:
        w = self._recorder
        self._recorder = None
        if w is not None:
            try:
                w.close()
            except Exception:
                pass
        return self._record_path

    # -- voice websocket ---------------------------------------------
    def _on_ws(self, msg) -> None:
        try:
            op = msg.get("op")
            data = msg.get("d") or {}
        except Exception:
            return
        if op == 5:  # SPEAKING
            ssrc = data.get("ssrc")
            uid = data.get("user_id")
            if ssrc is not None and uid is not None:
                self._ssrc_user[int(ssrc)] = str(uid)

    # -- udp packets --------------------------------------------------
    def _on_packet(self, data: bytes) -> None:
        try:
            if len(data) < 12 or (data[0] >> 6) != 2:
                self._stats["rtp_skip"] += 1
                return
            if (data[1] & 0x7F) != 0x78:     # opus payload type only
                self._stats["rtp_skip"] += 1
                return
            ssrc = struct.unpack_from(">I", data, 8)[0]
            conn = self.conn or self.vc._connection
            if ssrc == getattr(conn, "ssrc", None):
                self._stats["own"] += 1
                return
            self._stats["packets"] += 1
            self._last_pkt = time.time()
            try:
                payload = self._decrypt(conn, data)
            except Exception as exc:
                self._last_err = f"transport {type(exc).__name__}: {exc}"
                self._stats["decrypt_fail"] += 1
                return
            if not payload:
                self._stats["decrypt_fail"] += 1
                return
            uid = self._ssrc_user.get(ssrc)
            if uid is not None and uid in self._muted_users:
                self._stats["muted"] += 1
                return                       # hidden locally: skip entirely
            try:
                payload, resolved = self._dave_decrypt(conn, ssrc, uid, payload)
            except Exception as exc:
                self._last_err = f"dave {type(exc).__name__}: {exc}"
                self._stats["dave_fail"] += 1
                return
            if payload is None:
                # encrypted (E2EE) and we could not recover the user's key
                self._stats["dave_fail"] += 1
                return
            if resolved is not None and resolved in self._muted_users:
                self._stats["muted"] += 1
                return                       # sender was only identified now
            decoder = self._decoders.get(ssrc)
            if decoder is None:
                decoder = dopus.Decoder()
                self._decoders[ssrc] = decoder
            try:
                pcm = decoder.decode(payload)
            except Exception as exc:
                self._last_err = f"opus {type(exc).__name__}: {exc}"
                self._stats["decode_fail"] += 1
                return
            if not pcm:
                self._stats["decode_fail"] += 1
                return
            self._stats["decoded"] += 1
            rtp_ts = struct.unpack_from(">I", data, 4)[0]
            slot = self._schedule(ssrc, rtp_ts)
            if slot is not None:
                self._mix_frame(slot, ssrc, pcm)
        except Exception as exc:
            self._last_err = f"packet {type(exc).__name__}: {exc}"

    def _decrypt(self, conn, data: bytes) -> bytes:
        """Transport-decrypt an inbound RTP packet and return the *media*
        payload (i.e. the DAVE frame, when the channel is end-to-end
        encrypted).

        Two details here are what make official clients audible at all:

        * the header is **not** always 12 bytes.  When Discord's SFU relays
          media from the real desktop/mobile clients it appends an RFC 8285
          header extension (profile ``0xBEDE``).  With the ``*_rtpsize``
          ciphers only the fixed header, the CSRCs and the 4-byte extension
          *preamble* are authenticated; the extension *elements* are encrypted
          together with the media payload.  The decrypted body is therefore
          ``[extension elements][payload]`` and the payload only starts after
          them (8 bytes in practice).
        * which split is right is not fixed, so we try the plausible ones.
          The AEAD tag makes a wrong guess fail deterministically, so trying a
          handful is safe.
        """
        key = bytes(getattr(conn, "secret_key", b"") or b"")
        if not key or len(data) < 16:
            return b""

        cc = data[0] & 0x0F
        base = 12 + 4 * cc
        extended = bool(data[0] & 0x10) and len(data) >= base + 4
        ext_words = struct.unpack_from(">H", data, base + 2)[0] if extended else 0

        cands = [base]
        if extended:                          # RTP header extension present
            cands.append(base + 4)            # body starts after ext preamble
            cands.append(base + 4 + 4 * ext_words)

        mode = getattr(conn, "mode", None)
        modes = [mode] + [m for m in _TRANSPORT_MODES if m != mode]
        for hlen in cands:
            if hlen < 12 or hlen >= len(data):
                continue
            header, body = data[:hlen], data[hlen:]
            for m in modes:
                out = _decrypt_transport(key, m, header, body)
                if not out:
                    continue
                # The encrypted extension elements sit in front of the media
                # payload; how many depends on which split authenticated them.
                cut = 0
                if extended and ext_words:
                    if hlen == base:          # preamble was part of the body
                        cut = 4 + 4 * ext_words
                    elif hlen == base + 4:    # preamble was authenticated
                        cut = 4 * ext_words
                if 0 < cut < len(out):
                    out = out[cut:]
                self._last_err = ""
                return out
        return b""

    def _dave_decrypt(self, conn, ssrc: int, uid: Optional[str],
                      payload: bytes):
        """Return ``(plain_opus_or_None, resolved_uid_or_None)``.

        Discord voice is end-to-end encrypted (DAVE / E2EE): the sender
        encrypts the opus packet with a key bound to *their* user id, so we
        must know which user a given ``ssrc`` belongs to.  The SPEAKING event
        (op 5) normally tells us, but Discord does **not** resend it for people
        who are already talking when we join.  When we have no mapping we
        therefore probe the peers we know in the DAVE group and remember the
        key that works, which recovers the audio instead of playing ciphertext
        as noise.
        """
        session = getattr(conn, "dave_session", None)
        if not session or not DAVEY_OK:
            return payload, None
        try:
            if not getattr(conn, "can_encrypt", False):
                return payload, None       # E2EE not active: plain opus
        except Exception:
            return payload, None

        order = []
        if uid is not None:
            order.append(str(uid))
        cached = self._ssrc_user.get(ssrc)
        if cached is not None and cached not in order:
            order.append(cached)
        for peer in self._dave_peers(session):
            if str(peer) not in order:
                order.append(str(peer))

        for cand in order:
            out, real = self._dave_try(session, cand, payload)
            if out is None:
                continue
            if real:
                self._ssrc_user[ssrc] = str(cand)
                return out, str(cand)
            return out, None               # not encrypted: pass plain opus on
        if uid is not None:
            self._ssrc_user.pop(ssrc, None)   # stale mapping: re-discover
        return None, None

    @staticmethod
    def _dave_peers(session):
        try:
            return [int(x) for x in session.get_user_ids()]
        except Exception:
            return []

    @staticmethod
    def _dave_try(session, uid, payload):
        """Try one user id.  Returns ``(plain_or_None, really_decrypted)``.

        davey returns a frame unchanged when it was not encrypted (its
        "passthrough" mode), which is how plain opus from clients that do not
        speak DAVE still gets through; that is reported as
        ``really_decrypted=False``.  A wrong user id raises instead, so the
        caller can move on to the next candidate.
        """
        candidates = []
        try:
            candidates.append(int(uid))
        except Exception:
            pass
        if isinstance(uid, str):
            candidates.append(uid)
        for cand in candidates:
            try:
                out = session.decrypt(cand, davey.MediaType.audio, payload)
            except Exception as exc:
                if "UnencryptedWhenPassthroughDisabled" in str(exc):
                    return payload, False  # not encrypted: use it as-is
                continue
            if not out:
                continue
            return out, out != payload
        return None, False

    def _schedule(self, ssrc: int, rtp_ts: int) -> Optional[int]:
        """Map an RTP timestamp onto a wall-clock playback slot.

        Discord sends one opus frame every 20 ms and the RTP timestamp grows
        by exactly ``FRAME_SAMPLES`` per frame, so consecutive frames always
        belong in consecutive slots no matter when they reach us.  The old
        code stamped frames with their *arrival* time instead, so two packets
        that landed in the same 20 ms window were summed together (and a
        packet dragged across a boundary left an empty one) - that is what
        made the audio crackle.  Each speaker is anchored once, then placed
        purely by their own timestamps, which is jitter-proof.
        """
        now = time.perf_counter()
        with self._lock:
            if self._t0 is None:
                self._t0 = now
            t0 = self._t0
            idx = rtp_ts // self.FRAME_SAMPLES
            base = self._ssrc_base.get(ssrc)
            if base is None or idx < base[0] - 50:
                # first frame, or the ssrc was reused / the stream restarted
                base = (idx, int((now - t0) / 0.02))
                self._ssrc_base[ssrc] = base
            slot = base[1] + (idx - base[0])
            min_slot = int((now - t0) / 0.02) - self._delay_slots
        if slot < min_slot:
            return None                     # too late: never replay old audio
        if slot > min_slot + 250:           # timestamp wrap/glitch: clamp
            slot = min_slot + 250
        return slot

    def _mix_frame(self, slot: int, ssrc: int, pcm: bytes) -> None:
        frame = array("h")
        frame.frombytes(pcm)
        with self._lock:
            bucket = self._mix.get(slot)
            if bucket is None:
                bucket = {}
                self._mix[slot] = bucket
            # keyed by ssrc: a duplicate / retransmit of the same frame
            # replaces the old one instead of being added on top of it
            bucket[ssrc] = frame
            if len(self._mix) > 96:
                cutoff = slot - 120
                for key in [k for k in self._mix.keys() if k < cutoff]:
                    self._mix.pop(key, None)

    def _render(self, bucket) -> bytes:
        """Sum every speaker's frame that landed in one 20 ms slot."""
        if not bucket:
            return b"\x00" * self.FRAME_BYTES
        frames = list(bucket.values())
        length = max(len(f) for f in frames)
        out = array("h", bytes(2 * length))
        for frame in frames:
            n = min(length, len(frame))
            for i in range(n):
                out[i] = _clamp(out[i] + frame[i])
        return out.tobytes()

    def _mix_loop(self) -> None:
        period = 0.02
        nxt = time.perf_counter() + period
        while self._running:
            now = time.perf_counter()
            if now < nxt:
                time.sleep(max(0.0, min(nxt - now, 0.005)))
                continue
            nxt += period
            if nxt < now - 0.25:
                nxt = now + period
            with self._lock:
                if self._t0 is None:
                    chunk = b"\x00" * self.FRAME_BYTES
                    bucket_had_audio = False
                else:
                    slot = int((now - self._t0) / 0.02) - self._delay_slots
                    bucket = self._mix.pop(slot, None)
                    bucket_had_audio = bool(bucket)
                    chunk = self._render(bucket)
            if bucket_had_audio:
                peak = 0
                for i in range(0, len(chunk) - 1, 2):
                    v = chunk[i] | (chunk[i + 1] << 8)
                    if v >= 0x8000:
                        v -= 0x10000
                    if v < 0:
                        v = -v
                    if v > peak:
                        peak = v
                # decay so the meter is readable
                self._peak = max(peak, int(self._peak * 0.6))
            else:
                self._peak = int(self._peak * 0.6)
            self._speaker.feed(chunk)
            if self._recorder is not None:
                try:
                    self._recorder.writeframes(chunk)
                except Exception as exc:
                    self.record_error = str(exc)
                    self.stop_record()
                else:
                    if time.time() >= self._record_until:
                        self.stop_record()
