"""
Clip Watcher - records live streams or scans pre-recorded videos / VODs,
detects exciting spikes (chat activity or audio loudness highlights),
and automatically extracts short clips into a folder.

Features:
  - Mode Switcher: "Live Stream" or "Video / VOD"
  - Live Stream: Twitch, YouTube Live, Kick, Custom stream URLs + "CLIP NOW" button
  - Video / VOD: Local video files (.mp4, .mkv, .mov, .ts, etc.) or Video URLs
    with ultra-fast FFmpeg audio energy / loudness spike detection
  - Lossless cutting with FFmpeg copy
  - Optional Whisper AI subtitles (.srt)

Requirements:
  Python 3.9+, streamlink, ffmpeg, customtkinter, pytchat, websocket-client, numpy
"""
import json
import os
import queue
import random
import re
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

# Ensure FFmpeg and user Python scripts are in PATH
user_path = os.environ.get("PATH", "")
extra_paths = [
    str(Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages" / "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe" / "ffmpeg-9.0.2-full_build" / "bin"),
    str(Path.home() / "AppData" / "Local" / "Packages" / "PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0" / "LocalCache" / "local-packages" / "Python313" / "Scripts"),
    r"C:\ffmpeg\bin",
    r"C:\Program Files\ffmpeg\bin",
]

# Auto-detect WinGet Gyan.FFmpeg bin paths if present
winget_pkgs = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
if winget_pkgs.is_dir():
    try:
        for ffmpeg_bin in winget_pkgs.glob("Gyan.FFmpeg*/**/bin"):
            if ffmpeg_bin.is_dir():
                extra_paths.append(str(ffmpeg_bin))
    except Exception:
        pass

# Auto-detect Windows Store Python scripts
store_pkgs = Path.home() / "AppData" / "Local" / "Packages"
if store_pkgs.is_dir():
    try:
        for py_scripts in store_pkgs.glob("PythonSoftwareFoundation.Python*/**/Scripts"):
            if py_scripts.is_dir():
                extra_paths.append(str(py_scripts))
    except Exception:
        pass

for p in extra_paths:
    if os.path.exists(p) and p not in user_path:
        os.environ["PATH"] = p + os.pathsep + os.environ["PATH"]

try:
    import numpy as np
except ImportError:
    np = None

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox
    import customtkinter as ctk
except ImportError:
    tk = None
    ctk = None

APP_NAME = "Clip Watcher"
CONFIG_PATH = Path.home() / ".clipwatcher.json"
BUCKET_SECONDS = 10
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DEFAULTS = {
    "mode": "Live Stream",
    "platform": "Auto-detect",
    "channel": "",
    "video_source": "",
    "folder": str(Path.home() / "Videos" / "ClipWatcher"),
    "quality": "best",
    "sensitivity": 3.0,
    "video_sens": 2.5,
    "min_messages": 15,
    "clip_preset": "30s",
    "clip_seconds": 30,
    "before": 20,
    "after": 10,
    "max_clips": "Top 10 Clips",
    "captions": False,
    "keep_full": False,
    "soft_cuts": True,
}

PLATFORMS = ["Auto-detect", "Twitch", "YouTube", "Kick", "Custom URL"]
MAX_CLIPS_OPTIONS = ["Top 5 Clips", "Top 10 Clips", "Top 15 Clips", "Top 25 Clips", "All Spikes"]

PLACEHOLDERS_LIVE = {
    "Auto-detect": "channel name, @handle, or stream URL",
    "Twitch": "channel name (e.g. shroud) or twitch.tv/...",
    "YouTube": "@channel, watch?v=..., or live URL",
    "Kick": "channel name (e.g. xqc) or kick.com/...",
    "Custom URL": "https://... (HLS, RTMP, or direct stream)",
}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def have(tool):
    from shutil import which
    return which(tool) is not None


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True,
                          creationflags=NO_WINDOW, **kw)


def media_duration(path):
    """Duration (seconds) of a media file or URL."""
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)])
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def check_has_audio(path):
    """Check if media has an audio stream."""
    r = run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", str(path)])
    return "audio" in (r.stdout or "").lower()


def cut_clip(raw, spike_pos, before, after, out, soft_cut=True):
    """Cut [spike_pos-before, spike_pos+after] from raw into out with frame-accuracy and soft transitions."""
    start = max(0.0, spike_pos - before)
    length = before + after

    if soft_cut:
        has_audio = check_has_audio(raw)
        afade_out_st = max(0.05, length - 0.40)
        vfade_out_st = max(0.05, length - 0.20)
        vf_filter = f"fade=t=in:st=0:d=0.15,fade=t=out:st={vfade_out_st:.2f}:d=0.20"

        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-ss", f"{start:.2f}",
            "-i", str(raw),
            "-t", f"{length:.2f}",
            "-vf", vf_filter,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
        ]
        if has_audio:
            af_filter = f"afade=t=in:st=0:d=0.25,afade=t=out:st={afade_out_st:.2f}:d=0.40"
            cmd.extend(["-af", af_filter, "-c:a", "aac", "-b:a", "192k"])
        else:
            cmd.extend(["-an"])
        cmd.append(str(out))

        r = run(cmd)
        if r.returncode == 0 and Path(out).exists() and Path(out).stat().st_size > 0:
            return True

    # Fast stream copy mode / fallback
    r = run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}", "-i", str(raw),
             "-t", f"{length:.2f}", "-c", "copy", "-movflags", "+faststart", str(out)])
    return r.returncode == 0 and Path(out).exists() and Path(out).stat().st_size > 0


def srt_time(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def resolve_youtube_video_id(target):
    """Extract or resolve YouTube video ID from URL or channel handle."""
    target = target.strip()
    if re.fullmatch(r"[a-zA-Z0-9_-]{11}", target):
        return target
    m = re.search(r"[?&]v=([a-zA-Z0-9_-]{11})", target)
    if m:
        return m.group(1)
    m = re.search(r"(?:youtu\.be/|youtube\.com/live/|youtube\.com/shorts/)([a-zA-Z0-9_-]{11})", target)
    if m:
        return m.group(1)

    channel_url = target
    if not channel_url.startswith(("http://", "https://")):
        if not channel_url.startswith("@"):
            channel_url = f"@{channel_url}"
        channel_url = f"https://www.youtube.com/{channel_url}/live"
    elif not channel_url.endswith("/live"):
        channel_url = channel_url.rstrip("/") + "/live"

    req = urllib.request.Request(channel_url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            final_url = resp.geturl()
            m = re.search(r"[?&]v=([a-zA-Z0-9_-]{11})", final_url)
            if m:
                return m.group(1)
            m = re.search(r"/live/([a-zA-Z0-9_-]{11})", final_url)
            if m:
                return m.group(1)
            html = resp.read().decode("utf-8", errors="ignore")
            m = re.search(r'"videoId":"([a-zA-Z0-9_-]{11})"', html)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


def parse_platform_and_target(platform_choice, raw_input):
    """Normalize input into platform, streamlink URL, clean identifier, and display name."""
    raw = raw_input.strip()
    if platform_choice == "Auto-detect" or not platform_choice:
        low = raw.lower()
        if "twitch.tv" in low:
            platform = "Twitch"
        elif "kick.com" in low:
            platform = "Kick"
        elif "youtube.com" in low or "youtu.be" in low or raw.startswith("@"):
            platform = "YouTube"
        elif low.startswith(("http://", "https://", "rtmp://", "rtsp://", "hls://")):
            platform = "Custom URL"
        else:
            platform = "Twitch"
    else:
        platform = platform_choice

    if platform == "Twitch":
        m = re.search(r"twitch\.tv/([a-zA-Z0-9_]+)", raw, re.I)
        channel = m.group(1).lower() if m else raw.lstrip("#").lower()
        stream_url = f"twitch.tv/{channel}"
        clean_name = channel
        display_name = f"Twitch / {channel}"
    elif platform == "Kick":
        m = re.search(r"kick\.com/([a-zA-Z0-9_]+)", raw, re.I)
        channel = m.group(1).lower() if m else raw.lower()
        stream_url = f"kick.com/{channel}"
        clean_name = channel
        display_name = f"Kick / {channel}"
    elif platform == "YouTube":
        if raw.startswith(("http://", "https://")):
            stream_url = raw
        elif raw.startswith("@"):
            stream_url = f"https://www.youtube.com/{raw}/live"
        else:
            stream_url = f"https://www.youtube.com/@{raw}/live"
        vid = resolve_youtube_video_id(raw)
        if vid:
            clean_name = f"yt_{vid}"
        elif raw.startswith("@"):
            clean_name = raw.lstrip("@")
        else:
            clean_name = re.sub(r"[^a-zA-Z0-9_-]", "_", raw)[:25].strip("_")
        display_name = f"YouTube / {clean_name}"
    else:
        stream_url = raw
        m = re.search(r"/([a-zA-Z0-9_-]+)(?:\.[a-zA-Z0-9]+)?(?:[?#]|$)", raw)
        clean_name = m.group(1) if m else re.sub(r"[^a-zA-Z0-9_-]", "_", raw)[:25].strip("_")
        if not clean_name:
            clean_name = "stream"
        display_name = f"Custom / {clean_name}"

    return platform, stream_url, clean_name, display_name


# --------------------------------------------------------------------------
# spike detection
# --------------------------------------------------------------------------
class SpikeDetector:
    """Flags a 10-second bucket whose message count is far above the recent norm with surge velocity & cooldown."""

    def __init__(self, sensitivity=3.0, min_messages=15, history=30, warmup=6):
        self.sensitivity = float(sensitivity)
        self.min_messages = int(min_messages)
        self.history = deque(maxlen=history)
        self.warmup = warmup
        self.prev_count = 0
        self.cooldown = 0

    def update(self, count):
        spike = False
        if self.cooldown > 0:
            self.cooldown -= 1

        if len(self.history) >= self.warmup:
            mean = statistics.fmean(self.history)
            std = statistics.pstdev(self.history)

            surge = count - self.prev_count
            z = (count - mean) / max(std, 1.5)
            sens_thresh = max(1.5, self.sensitivity)

            is_huge_surge = (count >= self.min_messages and surge >= max(mean * 0.75, 8.0) and z >= sens_thresh * 0.75)
            is_statistical_spike = (count >= self.min_messages and z >= sens_thresh and count >= 1.5 * mean)

            if (is_huge_surge or is_statistical_spike) and self.cooldown == 0:
                spike = True
                self.cooldown = 2  # prevent duplicate clips on consecutive buckets of same peak

        self.prev_count = count
        self.history.append(count)
        return spike


# --------------------------------------------------------------------------
# chat readers (Live)
# --------------------------------------------------------------------------
class BaseChatReader(threading.Thread):
    def __init__(self, log):
        super().__init__(daemon=True)
        self.log = log
        self.stop_event = threading.Event()
        self.counts = defaultdict(int)
        self.lock = threading.Lock()

    def record_message(self, count=1):
        bucket = int(time.time() // BUCKET_SECONDS)
        with self.lock:
            self.counts[bucket] += count

    def pop(self, bucket):
        with self.lock:
            return self.counts.pop(bucket, 0)

    def stop(self):
        self.stop_event.set()


class TwitchChatReader(BaseChatReader):
    def __init__(self, channel, log):
        super().__init__(log)
        self.channel = channel.lower().lstrip("#")
        self.sock = None

    def stop(self):
        super().stop()
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass

    def run(self):
        while not self.stop_event.is_set():
            try:
                self._session()
            except OSError as e:
                if not self.stop_event.is_set():
                    self.log(f"[Twitch] Chat connection interrupted ({e}); reconnecting in 5 s")
            self.stop_event.wait(5)

    def _session(self):
        self.sock = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=30)
        s = self.sock
        s.sendall(b"PASS SCHMOOPIIE\r\n")
        s.sendall(f"NICK justinfan{random.randint(10000, 99999)}\r\n".encode())
        s.sendall(f"JOIN #{self.channel}\r\n".encode())
        self.log(f"[Twitch] Connected to chat of #{self.channel}")
        buf = b""
        while not self.stop_event.is_set():
            data = s.recv(4096)
            if not data:
                raise OSError("server closed connection")
            buf += data
            *lines, buf = buf.split(b"\r\n")
            for line in lines:
                if line.startswith(b"PING"):
                    s.sendall(b"PONG :tmi.twitch.tv\r\n")
                elif b" PRIVMSG " in line:
                    self.record_message()


class KickChatReader(BaseChatReader):
    PUSHER_KEY = "34bf7a0ff419a2a775b9"
    PUSHER_CLUSTER = "us2"

    def __init__(self, channel, log):
        super().__init__(log)
        self.channel = channel.lower().strip()
        self.ws = None

    def stop(self):
        super().stop()
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass

    def _get_chatroom_id(self):
        req = urllib.request.Request(
            f"https://kick.com/api/v2/channels/{self.channel}",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                return data.get("chatroom", {}).get("id")
        except Exception as e:
            self.log(f"[Kick] Could not resolve chatroom ID for '{self.channel}': {e}")
            return None

    def run(self):
        try:
            import websocket
        except ImportError:
            self.log("[Kick] websocket-client not installed. Run 'pip install websocket-client'")
            return

        while not self.stop_event.is_set():
            try:
                chatroom_id = self._get_chatroom_id()
                if not chatroom_id:
                    self.stop_event.wait(15)
                    continue

                url = f"wss://ws-{self.PUSHER_CLUSTER}.pusher.com/app/{self.PUSHER_KEY}?protocol=7&client=js&version=8.4.0-rc2&flash=false"
                self.ws = websocket.create_connection(url, timeout=15)
                self.ws.recv()  # connection_established
                sub = json.dumps({
                    "event": "pusher:subscribe",
                    "data": {"auth": "", "channel": f"chatrooms.{chatroom_id}.v2"}
                })
                self.ws.send(sub)
                self.log(f"[Kick] Connected to chat of '{self.channel}'")

                while not self.stop_event.is_set():
                    raw = self.ws.recv()
                    data = json.loads(raw)
                    event = data.get("event", "")
                    if event == "pusher:ping":
                        self.ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
                    elif event == "App\\Events\\ChatMessageEvent":
                        self.record_message()
            except Exception as e:
                if not self.stop_event.is_set():
                    self.log(f"[Kick] Chat connection interrupted ({e}); reconnecting in 5 s")
            self.stop_event.wait(5)


class YouTubeChatReader(BaseChatReader):
    def __init__(self, target, log):
        super().__init__(log)
        self.target = target
        self.chat = None

    def stop(self):
        super().stop()
        if self.chat:
            try:
                self.chat.terminate()
            except Exception:
                pass

    def run(self):
        try:
            import pytchat
        except ImportError:
            self.log("[YouTube] pytchat not installed. Run 'pip install pytchat'")
            return

        while not self.stop_event.is_set():
            try:
                video_id = resolve_youtube_video_id(self.target)
                if not video_id:
                    self.log(f"[YouTube] Waiting to resolve live video ID for '{self.target}'...")
                    self.stop_event.wait(15)
                    continue

                self.log(f"[YouTube] Connected to chat for video ID {video_id}")
                self.chat = pytchat.create(video_id=video_id)
                while not self.stop_event.is_set() and self.chat.is_alive():
                    items = self.chat.get().sync_items()
                    if items:
                        self.record_message(len(items))
                    time.sleep(1)
            except Exception as e:
                if not self.stop_event.is_set():
                    self.log(f"[YouTube] Chat reader notice: {e}; retrying in 10 s")
            self.stop_event.wait(10)


class DummyChatReader(BaseChatReader):
    def __init__(self, log):
        super().__init__(log)

    def run(self):
        self.log("[Stream] Live stream active. Use '⚡ CLIP NOW' to manually capture clips.")
        while not self.stop_event.is_set():
            self.stop_event.wait(5)


# --------------------------------------------------------------------------
# captions (optional Whisper)
# --------------------------------------------------------------------------
class Captioner(threading.Thread):
    def __init__(self, log):
        super().__init__(daemon=True)
        self.q = queue.Queue()
        self.log = log
        self.model = None

    def add(self, clip):
        self.q.put(Path(clip))

    def run(self):
        while True:
            clip = self.q.get()
            if clip is None:
                return
            try:
                if self.model is None:
                    from faster_whisper import WhisperModel
                    self.log("Loading Whisper model (first time downloads it)...")
                    self.model = WhisperModel("base", compute_type="int8")
                segments, _ = self.model.transcribe(str(clip))
                lines = []
                for i, seg in enumerate(segments, 1):
                    lines.append(f"{i}\n{srt_time(seg.start)} --> {srt_time(seg.end)}\n"
                                 f"{seg.text.strip()}\n")
                clip.with_suffix(".srt").write_text("\n".join(lines), encoding="utf-8")
                self.log(f"Captions saved: {clip.with_suffix('.srt').name}")
            except ImportError:
                self.log("Captions skipped: run  pip install faster-whisper")
            except Exception as e:  # noqa: BLE001
                self.log(f"Captions failed for {clip.name}: {e}")


# --------------------------------------------------------------------------
# Live Stream Engine
# --------------------------------------------------------------------------
class Engine(threading.Thread):
    def __init__(self, cfg, log, status, on_clip=None):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.log = log
        self.status = status
        self.on_clip = on_clip or (lambda path: None)
        self.stop_event = threading.Event()
        self.manual_clip_event = threading.Event()

        self.platform, self.stream_url, self.clean_name, self.display_name = parse_platform_and_target(
            cfg.get("platform", "Auto-detect"), cfg["channel"]
        )

        if self.platform == "Twitch":
            self.chat = TwitchChatReader(self.clean_name, log)
        elif self.platform == "Kick":
            self.chat = KickChatReader(self.clean_name, log)
        elif self.platform == "YouTube":
            self.chat = YouTubeChatReader(cfg["channel"], log)
        else:
            self.chat = DummyChatReader(log)

        self.captioner = Captioner(log) if cfg.get("captions") else None
        self.clips = 0
        self.out_dir = Path(cfg["folder"])
        self.raw_dir = self.out_dir / "_recordings"

    def stop(self):
        self.stop_event.set()

    def request_clip(self):
        self.manual_clip_event.set()

    def is_live(self):
        r = run(["streamlink", "--stream-url", self.stream_url, self.cfg["quality"]])
        return r.returncode == 0

    def wait_until_live(self):
        while not self.stop_event.is_set():
            self.status(f"Offline - waiting for stream to go live ({self.display_name})")
            if self.is_live():
                return True
            self.stop_event.wait(25)
        return False

    def run(self):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.log(f"Starting {self.display_name}...")
        self.chat.start()
        if self.captioner:
            self.captioner.start()
        try:
            while not self.stop_event.is_set():
                if not self.wait_until_live():
                    break
                self.record_session()
        finally:
            self.chat.stop()
            if self.captioner:
                self.captioner.q.put(None)
            self.status("Stopped", clips=self.clips)

    def record_session(self):
        cfg = self.cfg
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_prefix = f"{self.platform.lower()}_{self.clean_name}"
        raw = self.raw_dir / f"{safe_prefix}_{stamp}.ts"
        self.log(f"LIVE ({self.display_name}) - recording to {raw.name}")
        proc = subprocess.Popen(
            ["streamlink", "--quiet", self.stream_url, cfg["quality"], "-o", str(raw)],
            creationflags=NO_WINDOW)

        det = SpikeDetector(cfg["sensitivity"], cfg["min_messages"])
        pending = []
        last_spike = 0.0
        next_bucket = int(time.time() // BUCKET_SECONDS)
        window = cfg["before"] + cfg["after"]

        while not self.stop_event.is_set() and proc.poll() is None:
            now = time.time()
            current = int(now // BUCKET_SECONDS)

            if self.manual_clip_event.is_set():
                self.manual_clip_event.clear()
                end = now
                if end - last_spike >= 4:
                    last_spike = end
                    pending.append(end)
                    self.log("⚡ Manual clip requested!")
                    self.status("LIVE - manual clip queued", clips=self.clips, spike=True)

            while next_bucket < current:
                count = self.chat.pop(next_bucket)
                end = (next_bucket + 1) * BUCKET_SECONDS
                is_spike = False
                if det.update(count) and end - last_spike >= window:
                    last_spike = end
                    pending.append(end)
                    is_spike = True
                    self.log(f"Chat spike: {count} msgs / {BUCKET_SECONDS}s")
                self.status(f"LIVE ({self.display_name})", msgs=count, clips=self.clips, spike=is_spike)
                next_bucket += 1

            ready = [t for t in pending if now >= t + cfg["after"] + 3]
            for t in ready:
                pending.remove(t)
                self.save_clip(raw, t)
            self.stop_event.wait(1)

        for t in pending:
            self.save_clip(raw, t)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.log(f"Recording ended for {self.display_name}")
        if not cfg["keep_full"]:
            try:
                raw.unlink()
            except OSError:
                pass

    def save_clip(self, raw, spike_wall_time):
        dur = media_duration(raw)
        if dur is None:
            self.log("Could not read recording duration yet; clip skipped")
            return
        spike_pos = dur - (time.time() - spike_wall_time)
        stamp = datetime.fromtimestamp(spike_wall_time).strftime("%Y%m%d_%H%M%S")
        safe_prefix = f"{self.platform.lower()}_{self.clean_name}"
        out = self.out_dir / f"{safe_prefix}_{stamp}.mp4"
        soft_cut = self.cfg.get("soft_cuts", True)
        if cut_clip(raw, spike_pos, self.cfg["before"], self.cfg["after"], out, soft_cut=soft_cut):
            self.clips += 1
            self.log(f"Saved clip: {out.name}")
            self.status(f"LIVE ({self.display_name})", clips=self.clips)
            self.on_clip(out)
            if self.captioner:
                self.captioner.add(out)
        else:
            self.log("ffmpeg failed to cut clip")


# --------------------------------------------------------------------------
# Video File / VOD Scanner (Fast Audio Peak Highlight Detector)
# --------------------------------------------------------------------------
class VideoScanner(threading.Thread):
    def __init__(self, cfg, log, status, on_clip=None, on_timeline=None):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.log = log
        self.status = status
        self.on_clip = on_clip or (lambda path: None)
        self.on_timeline = on_timeline or (lambda timeline: None)
        self.stop_event = threading.Event()
        self.captioner = Captioner(log) if cfg.get("captions") else None

    def stop(self):
        self.stop_event.set()

    def run(self):
        if np is None:
            self.log("numpy is required for video scanning. Run 'pip install numpy'")
            self.status("Error: numpy missing")
            return

        src = self.cfg.get("video_source", "").strip()
        if not src:
            src = self.cfg.get("channel", "").strip()
        if not src:
            self.log("No video file or URL provided.")
            self.status("Error: No video specified")
            return

        out_dir = Path(self.cfg["folder"])
        out_dir.mkdir(parents=True, exist_ok=True)

        is_local_file = os.path.exists(src)
        if is_local_file:
            video_input = src
            base_name = Path(src).stem
            self.log(f"Processing local video: {Path(src).name}")
        else:
            self.log(f"Resolving video stream URL for '{src}'...")
            self.status("Resolving video URL...")
            r = run(["streamlink", "--stream-url", src, self.cfg.get("quality", "best")])
            if r.returncode == 0 and r.stdout.strip():
                video_input = r.stdout.strip()
                m_yt = resolve_youtube_video_id(src)
                if m_yt:
                    base_name = f"yt_{m_yt}"
                else:
                    base_name = re.sub(r"[^a-zA-Z0-9_-]", "_", src)[:25].strip("_") or "video"
                self.log("Stream URL resolved successfully.")
            else:
                video_input = src
                base_name = "video"

        self.status("Reading video duration...")
        total_duration = media_duration(video_input)
        if total_duration and total_duration > 0:
            mins, secs = divmod(int(total_duration), 60)
            hours, mins = divmod(mins, 60)
            dur_str = f"{hours}h {mins}m {secs}s" if hours else f"{mins}m {secs}s"
            self.log(f"Video duration: {dur_str} ({total_duration:.1f}s)")
        else:
            self.log("Probing duration: proceeding with audio analysis...")

        bucket_sec = 0.5
        self.status("Scanning audio energy & highlights...")
        self.log("Scanning audio waveform with high-precision sub-second tracking...")

        cmd = [
            "ffmpeg", "-v", "error", "-i", str(video_input),
            "-vn", "-ac", "1", "-ar", "1000", "-f", "f32le", "-"
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        chunk_bytes = int(bucket_sec * 1000 * 4)
        rms_values = []
        last_log_time = time.time()

        while not self.stop_event.is_set():
            data = proc.stdout.read(chunk_bytes)
            if not data:
                break
            samples = np.frombuffer(data, dtype=np.float32)
            if len(samples) > 0:
                rms = float(np.sqrt(np.mean(samples ** 2)))
                rms_values.append(rms)
                current_time = len(rms_values) * bucket_sec
                if time.time() - last_log_time >= 1.5:
                    last_log_time = time.time()
                    if total_duration and total_duration > 0:
                        pct = min(100, int((current_time / total_duration) * 100))
                        self.status(f"Scanning audio: {pct}% ({int(current_time)}s / {int(total_duration)}s)")
                    else:
                        self.status(f"Scanning audio: {int(current_time)}s processed")

        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()

        if self.stop_event.is_set():
            self.log("Scanning cancelled by user.")
            self.status("Scan cancelled")
            return

        if not rms_values:
            self.log("No audio track detected or video could not be read.")
            self.status("Error: No audio found")
            return

        self.status("Analyzing highlight moments...")
        rms_arr = np.array(rms_values, dtype=np.float32)
        n_chunks = len(rms_arr)

        # Adaptive rolling baseline (31-step window = ~15s) to detect sudden spikes above ambient sound
        win_size = min(n_chunks, 31)
        if win_size % 2 == 0:
            win_size += 1
        kernel = np.ones(win_size, dtype=np.float32) / win_size
        baseline = np.convolve(rms_arr, kernel, mode="same")
        prominence = (rms_arr - baseline) / np.maximum(baseline, 0.004)

        mean_rms = float(np.mean(rms_arr))
        std_rms = float(np.std(rms_arr))
        sens = float(self.cfg.get("video_sens", 2.5))
        prom_thresh = max(0.18, 1.8 - (sens * 0.35))
        rms_thresh = mean_rms + max(std_rms * (sens * 0.22), 0.002)

        candidates = []
        for i in range(1, n_chunks - 1):
            if rms_arr[i] > rms_arr[i - 1] and rms_arr[i] >= rms_arr[i + 1]:
                if prominence[i] >= prom_thresh or rms_arr[i] >= rms_thresh:
                    t = i * bucket_sec + (bucket_sec / 2.0)
                    score = float(prominence[i] * np.sqrt(max(0.0, rms_arr[i])))
                    candidates.append((score, t, i))

        if len(candidates) < 3 and n_chunks >= 20:
            p85 = float(np.percentile(rms_arr, 85))
            for i in range(1, n_chunks - 1):
                if rms_arr[i] >= p85 and rms_arr[i] >= rms_arr[i - 1] and rms_arr[i] >= rms_arr[i + 1]:
                    t = i * bucket_sec + (bucket_sec / 2.0)
                    candidates.append((float(rms_arr[i]), t, i))

        candidates.sort(key=lambda x: x[0], reverse=True)

        max_str = str(self.cfg.get("max_clips", "Top 10"))
        m = re.search(r"\d+", max_str)
        max_clips = int(m.group(0)) if m else (len(candidates) if "All" in max_str else 10)

        before = int(self.cfg.get("before", 15))
        after = int(self.cfg.get("after", 10))
        window = before + after

        selected_spikes = []
        spike_indices = set()
        for score, t, idx in candidates:
            if all(abs(t - st) >= window for st in selected_spikes):
                selected_spikes.append(t)
                spike_indices.add(idx)
                if len(selected_spikes) >= max_clips:
                    break

        selected_spikes.sort()
        self.log(f"Found {len(selected_spikes)} highlight moments across video with sub-second accuracy!")
        self.status(f"Found {len(selected_spikes)} highlights", msgs=len(selected_spikes), clips=0)

        # Send full timeline to UI chart (downsampled to 36 slots)
        num_slots = CHART_SLOTS
        slot_size = max(1, n_chunks // num_slots)
        timeline_bars = []
        for s_idx in range(num_slots):
            start_i = s_idx * slot_size
            end_i = min(n_chunks, (s_idx + 1) * slot_size)
            chunk = rms_arr[start_i:end_i] if start_i < n_chunks else [0]
            val = float(np.mean(chunk)) if len(chunk) else 0.0
            is_peak = any(idx in spike_indices for idx in range(start_i, end_i))
            timeline_bars.append((int(val * 1000), is_peak))
        self.on_timeline(timeline_bars)

        # 4. Cut Clips
        if self.captioner:
            self.captioner.start()

        clips_saved = 0
        soft_cut = self.cfg.get("soft_cuts", True)
        for idx, spike_time in enumerate(selected_spikes, 1):
            if self.stop_event.is_set():
                break
            m, s = divmod(int(spike_time), 60)
            h, m = divmod(m, 60)
            time_tag = f"{h:02d}h{m:02d}m{s:02d}s" if h else f"{m:02d}m{s:02d}s"
            out_name = f"{base_name}_clip{idx:02d}_{time_tag}.mp4"
            out_path = out_dir / out_name

            self.status(f"Cutting clip {idx}/{len(selected_spikes)} ({time_tag})...", msgs=len(selected_spikes), clips=clips_saved)
            self.log(f"Cutting clip {idx}/{len(selected_spikes)} at {time_tag} -> {out_name}")

            if cut_clip(video_input, spike_time, before, after, out_path, soft_cut=soft_cut):
                clips_saved += 1
                self.log(f"Saved: {out_name}")
                self.on_clip(out_path)
                if self.captioner:
                    self.captioner.add(out_path)
            else:
                self.log(f"FFmpeg failed to cut clip at {time_tag}")

        if self.captioner:
            self.captioner.q.put(None)

        self.status(f"Completed! {clips_saved} clips saved", msgs=len(selected_spikes), clips=clips_saved)
        self.log(f"Finished processing! Successfully created {clips_saved} clips in {out_dir}")


# --------------------------------------------------------------------------
# GUI (customtkinter)
# --------------------------------------------------------------------------
ACCENT = "#FFD400"
BG = "#0d0e11"
CARD = "#17181d"
CARD2 = "#1f2026"
MUTED = "#8b8f98"
RED = "#ff4d4f"
AMBER = "#f5a623"
BLUE = "#3b82f6"
BAR = "#4b4f5c"
CHART_SLOTS = 36


def open_path(p):
    p = str(p)
    if sys.platform.startswith("win"):
        os.startfile(p)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p])


class App(ctk.CTk if ctk else object):
    def __init__(self):
        ctk.set_appearance_mode("dark")
        super().__init__(fg_color=BG)
        self.title(f"{APP_NAME} - Multi-Platform Live & Video Clipper")
        self.geometry("1040x800")
        self.minsize(920, 720)

        self.cfg = dict(DEFAULTS)
        if CONFIG_PATH.exists():
            try:
                self.cfg.update(json.loads(CONFIG_PATH.read_text()))
            except (OSError, ValueError):
                pass

        self.engine = None
        self.q = queue.Queue()
        self.history = deque(maxlen=CHART_SLOTS)
        self.clip_rows = []
        self.clip_count = 0

        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)
        self._sidebar()
        self._main()
        self._load_existing_clips()
        self._on_mode_change(self.v_mode.get())
        self.after(200, self._pump)
        self.protocol("WM_DELETE_WINDOW", self._close)

    # ---- sidebar -----------------------------------------------------------
    def _label(self, parent, text):
        return ctk.CTkLabel(parent, text=text, text_color=MUTED,
                            font=ctk.CTkFont(size=11, weight="bold"))

    def _slider(self, parent, text, lo, hi, steps, value, fmt, on_change=None):
        var = tk.DoubleVar(value=value)
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(3, 0))
        ctk.CTkLabel(row, text=text, font=ctk.CTkFont(size=12)).pack(side="left")
        val = ctk.CTkLabel(row, text=fmt(value), text_color=ACCENT,
                           font=ctk.CTkFont(size=12, weight="bold"))
        val.pack(side="right")

        def _cmd(v):
            val.configure(text=fmt(v))
            if on_change:
                on_change(v)

        slider = ctk.CTkSlider(parent, from_=lo, to=hi, number_of_steps=steps, variable=var,
                               button_color=ACCENT, button_hover_color="#ffe14d",
                               progress_color=ACCENT,
                               command=_cmd)
        slider.pack(fill="x", padx=14, pady=(1, 2))
        var._val_lbl = val
        var._slider = slider
        var._fmt = fmt
        return var

    def _set_slider_val(self, var, new_val):
        var.set(new_val)
        if hasattr(var, "_val_lbl") and hasattr(var, "_fmt"):
            var._val_lbl.configure(text=var._fmt(new_val))

    def _split_for_total(self, total):
        total = int(round(float(total)))
        if total == 30:
            return 20, 10
        elif total == 35:
            return 24, 11
        elif total == 40:
            return 26, 14
        elif total == 60:
            return 40, 20
        else:
            after = max(4, int(round(total * 0.33)))
            before = max(5, total - after)
            return before, after

    def _on_preset_change(self, choice):
        if getattr(self, "_updating_duration", False):
            return
        if choice in ("30s", "35s", "40s", "60s"):
            tot = int(choice.rstrip("s"))
            self._updating_duration = True
            try:
                self._set_slider_val(self.v_total_sec, tot)
                b, a = self._split_for_total(tot)
                self._set_slider_val(self.v_before, b)
                self._set_slider_val(self.v_after, a)
            finally:
                self._updating_duration = False

    def _on_total_slider_change(self, v):
        if getattr(self, "_updating_duration", False):
            return
        tot = int(round(float(v)))
        self._updating_duration = True
        try:
            preset = f"{tot}s" if tot in (30, 35, 40, 60) else "Custom"
            if self.v_clip_preset.get() != preset:
                self.v_clip_preset.set(preset)
            b, a = self._split_for_total(tot)
            self._set_slider_val(self.v_before, b)
            self._set_slider_val(self.v_after, a)
        finally:
            self._updating_duration = False

    def _on_sub_slider_change(self, _v):
        if getattr(self, "_updating_duration", False):
            return
        self._updating_duration = True
        try:
            b = int(round(float(self.v_before.get())))
            a = int(round(float(self.v_after.get())))
            tot = b + a
            self._set_slider_val(self.v_total_sec, tot)
            preset = f"{tot}s" if tot in (30, 35, 40, 60) else "Custom"
            if self.v_clip_preset.get() != preset:
                self.v_clip_preset.set(preset)
        finally:
            self._updating_duration = False

    def _on_platform_change(self, choice):
        hint = PLACEHOLDERS_LIVE.get(choice, "channel name, @handle, or URL")
        self.entry_channel.configure(placeholder_text=hint)

    def _on_mode_change(self, mode):
        is_live = (mode == "Live Stream")
        if is_live:
            self.lbl_src.configure(text="CHANNEL OR LIVE URL")
            self.frame_platform.pack(fill="x", padx=14, before=self.lbl_src)
            self.btn_browse_vid.pack_forget()
            self.frame_live_sliders.pack(fill="x", before=self.frame_shared_sliders)
            self.frame_video_sliders.pack_forget()
            self.switch_keep.pack(anchor="w", padx=14, pady=(4, 0))
            self.btn_clip.pack(side="bottom", fill="x", padx=18, pady=(0, 8), before=self.btn)
            self.btn.configure(text="START WATCHING", fg_color=ACCENT, hover_color="#ffe14d", text_color="#000000")
            self.stat1_title.configure(text="CHAT / 10 S")
            self.chart_title.configure(text="CHAT ACTIVITY  (yellow = spike)")
            self._on_platform_change(self.v_platform.get())
        else:
            self.lbl_src.configure(text="VIDEO FILE OR URL")
            self.frame_platform.pack_forget()
            self.btn_browse_vid.pack(side="right", padx=(6, 0))
            self.frame_live_sliders.pack_forget()
            self.frame_video_sliders.pack(fill="x", before=self.frame_shared_sliders)
            self.switch_keep.pack_forget()
            self.btn_clip.pack_forget()
            self.btn.configure(text="SCAN & AUTO-CLIP VIDEO", fg_color=ACCENT, hover_color="#ffe14d", text_color="#000000")
            self.stat1_title.configure(text="HIGHLIGHTS FOUND")
            self.chart_title.configure(text="AUDIO LOUDNESS TIMELINE  (yellow = clips)")
            self.entry_channel.configure(placeholder_text="C:\\path\\video.mp4 or YouTube URL")

    def _browse_video(self):
        f = filedialog.askopenfilename(
            title="Select Video File",
            filetypes=[("Video Files", "*.mp4 *.mkv *.mov *.avi *.ts *.webm *.flv *.m4v"), ("All Files", "*.*")]
        )
        if f:
            self.v_channel.set(f)

    def _sidebar(self):
        sb = ctk.CTkFrame(self, width=340, corner_radius=0, fg_color=CARD)
        sb.grid(row=0, column=0, sticky="nsew")
        sb.grid_propagate(False)

        # Bottom action buttons (pinned to the bottom)
        self.btn = ctk.CTkButton(sb, text="START WATCHING", height=44,
                                 fg_color=ACCENT, hover_color="#ffe14d",
                                 text_color="#000000",
                                 font=ctk.CTkFont(size=14, weight="bold"),
                                 command=self._toggle)
        self.btn.pack(side="bottom", fill="x", padx=14, pady=(0, 16))

        self.btn_clip = ctk.CTkButton(sb, text="⚡ CLIP NOW", height=36,
                                     fg_color=BLUE, hover_color="#2563eb",
                                     text_color="#ffffff",
                                     font=ctk.CTkFont(size=13, weight="bold"),
                                     command=self._clip_now,
                                     state="disabled")
        self.btn_clip.pack(side="bottom", fill="x", padx=14, pady=(0, 8))

        sb_body = ctk.CTkScrollableFrame(sb, fg_color="transparent")
        sb_body.pack(fill="both", expand=True, padx=4, pady=(0, 6))

        ctk.CTkLabel(sb_body, text="CLIP WATCHER", text_color=ACCENT,
                     font=ctk.CTkFont(size=22, weight="bold")
                     ).pack(anchor="w", padx=14, pady=(12, 0))
        ctk.CTkLabel(sb_body, text="Auto-clips moments when chat or audio goes wild",
                     text_color=MUTED, font=ctk.CTkFont(size=11)
                     ).pack(anchor="w", padx=14, pady=(0, 10))

        # Mode Selector
        self.v_mode = tk.StringVar(value=self.cfg.get("mode", "Live Stream"))
        self.seg_mode = ctk.CTkSegmentedButton(
            sb_body, values=["Live Stream", "Video / VOD"],
            variable=self.v_mode,
            command=self._on_mode_change,
            selected_color=ACCENT, selected_hover_color="#ffe14d",
            unselected_color=CARD2, unselected_hover_color="#2a2c34",
            text_color="#000000",
            font=ctk.CTkFont(size=12, weight="bold")
        )
        self.seg_mode.pack(fill="x", padx=14, pady=(0, 8))

        # Platform selector (Live mode)
        self.frame_platform = ctk.CTkFrame(sb_body, fg_color="transparent")
        self._label(self.frame_platform, "PLATFORM").pack(anchor="w", pady=(4, 2))
        self.v_platform = tk.StringVar(value=self.cfg.get("platform", "Auto-detect"))
        self.opt_platform = ctk.CTkOptionMenu(
            self.frame_platform, variable=self.v_platform, height=34, fg_color=CARD2,
            button_color=CARD2, button_hover_color="#2a2c34",
            values=PLATFORMS, command=self._on_platform_change
        )
        self.opt_platform.pack(fill="x")
        self.frame_platform.pack(fill="x", padx=14)

        # Source input (Channel or Video path)
        self.lbl_src = self._label(sb_body, "CHANNEL OR LIVE URL")
        self.lbl_src.pack(anchor="w", padx=14, pady=(8, 2))

        row_src = ctk.CTkFrame(sb_body, fg_color="transparent")
        row_src.pack(fill="x", padx=14)
        self.v_channel = tk.StringVar(value=self.cfg.get("channel", ""))
        self.entry_channel = ctk.CTkEntry(
            row_src, textvariable=self.v_channel,
            placeholder_text=PLACEHOLDERS_LIVE["Auto-detect"],
            height=36, fg_color=CARD2, border_width=0
        )
        self.entry_channel.pack(side="left", fill="x", expand=True)

        self.btn_browse_vid = ctk.CTkButton(
            row_src, text="Browse", width=65, height=36, fg_color=CARD2,
            hover_color="#2a2c34", command=self._browse_video
        )

        # Save Clips To
        self._label(sb_body, "SAVE CLIPS TO").pack(anchor="w", padx=14, pady=(8, 2))
        self.v_folder = tk.StringVar(value=self.cfg["folder"])
        row_f = ctk.CTkFrame(sb_body, fg_color="transparent")
        row_f.pack(fill="x", padx=14)
        ctk.CTkEntry(row_f, textvariable=self.v_folder, height=36,
                     fg_color=CARD2, border_width=0).pack(side="left", fill="x", expand=True)
        ctk.CTkButton(row_f, text="Browse", width=65, height=36, fg_color=CARD2,
                       hover_color="#2a2c34", command=self._browse
                       ).pack(side="left", padx=(6, 0))

        # Stream Quality
        self._label(sb_body, "STREAM QUALITY").pack(anchor="w", padx=14, pady=(8, 2))
        self.v_quality = tk.StringVar(value=self.cfg["quality"])
        ctk.CTkOptionMenu(sb_body, variable=self.v_quality, height=32, fg_color=CARD2,
                          button_color=CARD2, button_hover_color="#2a2c34",
                          values=["best", "1080p60", "720p60", "720p", "480p", "worst"]
                          ).pack(fill="x", padx=14)

        # Live mode sliders
        self.frame_live_sliders = ctk.CTkFrame(sb_body, fg_color="transparent")
        self._label(self.frame_live_sliders, "LIVE SPIKE DETECTION").pack(anchor="w", padx=14, pady=(8, 2))
        self.v_sens = self._slider(self.frame_live_sliders, "Sensitivity (lower = more clips)", 1, 10, 18,
                                   self.cfg["sensitivity"], lambda v: f"{float(v):.1f}")
        self.v_min = self._slider(self.frame_live_sliders, "Min messages / 10 s", 3, 200, 197,
                                  self.cfg["min_messages"], lambda v: f"{int(float(v))}")
        self.frame_live_sliders.pack(fill="x")

        # Video mode controls
        self.frame_video_sliders = ctk.CTkFrame(sb_body, fg_color="transparent")
        self._label(self.frame_video_sliders, "VIDEO HIGHLIGHT DETECTION").pack(anchor="w", padx=14, pady=(8, 2))
        self.v_video_sens = self._slider(self.frame_video_sliders, "Audio peak sensitivity", 1, 5, 20,
                                         self.cfg.get("video_sens", 2.5), lambda v: f"{float(v):.1f}")
        self._label(self.frame_video_sliders, "MAX CLIPS TO EXTRACT").pack(anchor="w", padx=14, pady=(4, 2))
        self.v_max_clips = tk.StringVar(value=self.cfg.get("max_clips", "Top 10 Clips"))
        ctk.CTkOptionMenu(self.frame_video_sliders, variable=self.v_max_clips, height=32, fg_color=CARD2,
                          button_color=CARD2, button_hover_color="#2a2c34",
                          values=MAX_CLIPS_OPTIONS).pack(fill="x", padx=14)

        # Shared clip duration controls
        self.frame_shared_sliders = ctk.CTkFrame(sb_body, fg_color="transparent")
        self._label(self.frame_shared_sliders, "CLIP DURATION (SECONDS)").pack(anchor="w", padx=14, pady=(8, 2))

        # Quick preset buttons: 30s, 35s, 40s, 60s, Custom
        self.v_clip_preset = tk.StringVar(value=self.cfg.get("clip_preset", "30s"))
        self.seg_clip_len = ctk.CTkSegmentedButton(
            self.frame_shared_sliders, values=["30s", "35s", "40s", "60s", "Custom"],
            variable=self.v_clip_preset,
            command=self._on_preset_change,
            selected_color=ACCENT, selected_hover_color="#ffe14d",
            unselected_color=CARD2, unselected_hover_color="#2a2c34",
            text_color="#000000", font=ctk.CTkFont(size=12, weight="bold")
        )
        self.seg_clip_len.pack(fill="x", padx=14, pady=(2, 4))

        # Total duration slider
        initial_total = int(self.cfg.get("clip_seconds", self.cfg.get("before", 20) + self.cfg.get("after", 10)))
        self.v_total_sec = self._slider(
            self.frame_shared_sliders, "Target clip length", 10, 120, 110,
            initial_total, lambda v: f"{int(float(v))}s",
            on_change=self._on_total_slider_change
        )

        # Before & After fine-tuning sliders
        self.v_before = self._slider(
            self.frame_shared_sliders, "  ↳ Buildup (before peak)", 5, 120, 115,
            self.cfg["before"], lambda v: f"{int(float(v))}s",
            on_change=self._on_sub_slider_change
        )
        self.v_after = self._slider(
            self.frame_shared_sliders, "  ↳ Reaction (after peak)", 0, 60, 60,
            self.cfg["after"], lambda v: f"{int(float(v))}s",
            on_change=self._on_sub_slider_change
        )
        self.frame_shared_sliders.pack(fill="x")

        # Switches
        self.v_soft = tk.BooleanVar(value=self.cfg.get("soft_cuts", True))
        self.v_caps = tk.BooleanVar(value=self.cfg["captions"])
        self.v_keep = tk.BooleanVar(value=self.cfg["keep_full"])
        ctk.CTkSwitch(sb_body, text="Soft transitions & accurate cuts", variable=self.v_soft, progress_color=ACCENT,
                      font=ctk.CTkFont(size=12)).pack(anchor="w", padx=14, pady=(8, 0))
        ctk.CTkSwitch(sb_body, text="Captions with Whisper (.srt)", variable=self.v_caps, progress_color=ACCENT,
                      font=ctk.CTkFont(size=12)).pack(anchor="w", padx=14, pady=(4, 0))
        self.switch_keep = ctk.CTkSwitch(sb_body, text="Keep full stream recording", variable=self.v_keep, progress_color=ACCENT,
                                         font=ctk.CTkFont(size=12))
        self.switch_keep.pack(anchor="w", padx=14, pady=(4, 8))

    # ---- main area ---------------------------------------------------------
    def _main(self):
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew", padx=18, pady=18)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)

        # status card
        card = ctk.CTkFrame(main, fg_color=CARD, corner_radius=14)
        card.grid(row=0, column=0, sticky="ew")
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=18, pady=(16, 6))
        self.dot = ctk.CTkLabel(top, text="●", text_color=MUTED, font=ctk.CTkFont(size=18))
        self.dot.pack(side="left")
        self.status_lbl = ctk.CTkLabel(top, text="Idle", font=ctk.CTkFont(size=18, weight="bold"))
        self.status_lbl.pack(side="left", padx=8)

        stats = ctk.CTkFrame(card, fg_color="transparent")
        stats.pack(fill="x", padx=18)
        self.stat1_box, self.stat1_title, self.stat_chat = self._stat(stats, "CHAT / 10 S", "0")
        self.stat2_box, self.stat2_title, self.stat_clips = self._stat(stats, "CLIPS SAVED", "0")

        self.chart_title = ctk.CTkLabel(card, text="CHAT ACTIVITY  (yellow = spike)", text_color=MUTED,
                                        font=ctk.CTkFont(size=11, weight="bold"))
        self.chart_title.pack(anchor="w", padx=18, pady=(12, 4))
        self.chart = tk.Canvas(card, height=90, bg=CARD, highlightthickness=0)
        self.chart.pack(fill="x", padx=18, pady=(0, 16))
        self.chart.bind("<Configure>", lambda e: self._draw_chart())

        # clips
        clips = ctk.CTkFrame(main, fg_color=CARD, corner_radius=14)
        clips.grid(row=1, column=0, sticky="nsew", pady=14)
        clips.columnconfigure(0, weight=1)
        clips.rowconfigure(1, weight=1)
        head = ctk.CTkFrame(clips, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 4))
        ctk.CTkLabel(head, text="Recent clips", font=ctk.CTkFont(size=15, weight="bold")
                     ).pack(side="left")
        ctk.CTkButton(head, text="Open folder", width=100, height=28, fg_color=CARD2,
                      hover_color="#2a2c34", command=self._open_folder).pack(side="right")
        ctk.CTkButton(head, text="Rescan", width=80, height=28, fg_color=CARD2,
                      hover_color="#2a2c34", command=self._rescan_clips).pack(side="right", padx=(0, 8))
        self.clip_list = ctk.CTkScrollableFrame(clips, fg_color="transparent")
        self.clip_list.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 10))
        self.empty_lbl = ctk.CTkLabel(
            self.clip_list, text="No clips yet.\nPress START WATCHING / SCAN VIDEO and they'll show up here.",
            text_color=MUTED)
        self.empty_lbl.pack(pady=30)

        # log
        self.log = ctk.CTkTextbox(main, height=110, fg_color=CARD, corner_radius=14,
                                  font=ctk.CTkFont(family="Consolas", size=11),
                                  text_color=MUTED)
        self.log.grid(row=2, column=0, sticky="ew")
        self.log.configure(state="disabled")

    def _stat(self, parent, title, value):
        box = ctk.CTkFrame(parent, fg_color=CARD2, corner_radius=10)
        box.pack(side="left", padx=(0, 10), ipadx=10)
        t_lbl = ctk.CTkLabel(box, text=title, text_color=MUTED,
                             font=ctk.CTkFont(size=10, weight="bold"))
        t_lbl.pack(anchor="w", padx=12, pady=(8, 0))
        lbl = ctk.CTkLabel(box, text=value, text_color=ACCENT,
                           font=ctk.CTkFont(size=24, weight="bold"))
        lbl.pack(anchor="w", padx=12, pady=(0, 8))
        return box, t_lbl, lbl

    # ---- chart & clip list ---------------------------------------------------
    def _draw_chart(self):
        c = self.chart
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 10 or not self.history:
            return
        slot = w / CHART_SLOTS
        peak = max([x for x, _ in self.history] + [10])
        start = CHART_SLOTS - len(self.history)
        for i, (count, spike) in enumerate(self.history):
            bar_h = max(2, (h - 4) * count / peak)
            x0 = (start + i) * slot + 1
            c.create_rectangle(x0, h - bar_h, x0 + slot - 3, h,
                               fill=ACCENT if spike else BAR, outline="")

    def _show_empty_label(self):
        if self.empty_lbl is None:
            self.empty_lbl = ctk.CTkLabel(
                self.clip_list,
                text="No clips yet.\nPress START WATCHING / SCAN VIDEO and they'll show up here.",
                text_color=MUTED)
            self.empty_lbl.pack(pady=30)

    def _log_msg(self, msg):
        self.q.put(("log", msg))

    def _add_clip(self, path, at_top=True):
        path = Path(path)
        if not path.exists():
            return
        if self.empty_lbl is not None:
            self.empty_lbl.destroy()
            self.empty_lbl = None
        row = ctk.CTkFrame(self.clip_list, fg_color=CARD2, corner_radius=8)
        row._file_path = path
        if at_top and self.clip_rows:
            row.pack(fill="x", pady=3, padx=4, before=self.clip_rows[0])
        else:
            row.pack(fill="x", pady=3, padx=4)
        when = datetime.fromtimestamp(path.stat().st_mtime).strftime("%b %d  %H:%M:%S")
        ctk.CTkLabel(row, text=path.name, anchor="w",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(side="left", padx=12, pady=8)
        ctk.CTkLabel(row, text=when, text_color=MUTED, font=ctk.CTkFont(size=11)
                     ).pack(side="left")

        def _play(p=path, r=row):
            if not p.exists():
                self._log_msg(f"Missing file removed: {p.name}")
                if r in self.clip_rows:
                    self.clip_rows.remove(r)
                r.destroy()
                if not self.clip_rows:
                    self._show_empty_label()
                return
            open_path(p)

        ctk.CTkButton(row, text="Play", width=60, height=26, fg_color=ACCENT,
                      hover_color="#ffe14d", text_color="#000000",
                      command=_play).pack(side="right", padx=10)
        self.clip_rows.insert(0, row) if at_top else self.clip_rows.append(row)
        while len(self.clip_rows) > 60:
            self.clip_rows.pop().destroy()

    def _rescan_clips(self, verbose=True):
        folder_str = self.v_folder.get().strip() if hasattr(self, "v_folder") else self.cfg.get("folder", "")
        folder = Path(folder_str) if folder_str else Path.cwd()

        # Count missing files currently in clip_rows before clearing
        missing_count = 0
        for r in self.clip_rows:
            fpath = getattr(r, "_file_path", None)
            if fpath is not None:
                try:
                    if not fpath.exists():
                        missing_count += 1
                except OSError:
                    missing_count += 1

        # Clear existing rows
        for r in self.clip_rows:
            try:
                r.destroy()
            except Exception:
                pass
        self.clip_rows.clear()

        if not folder.exists():
            self._show_empty_label()
            if verbose:
                self._log_msg(f"Rescan: folder not found '{folder}'")
            return

        # Scan for existing video files
        vid_exts = {".mp4", ".mkv", ".mov", ".avi", ".ts", ".webm", ".m4v"}
        found = []
        try:
            for item in folder.iterdir():
                try:
                    if item.is_file() and item.suffix.lower() in vid_exts and item.stat().st_size > 0:
                        found.append(item)
                except OSError:
                    continue
        except OSError as e:
            if verbose:
                self._log_msg(f"Rescan error reading folder: {e}")
            self._show_empty_label()
            return

        found.sort(key=lambda f: f.stat().st_mtime, reverse=True)

        for f in found[:50]:
            self._add_clip(f, at_top=False)

        if not self.clip_rows:
            self._show_empty_label()

        if verbose:
            msg = f"Rescanned '{folder.name}': {len(self.clip_rows)} clip(s) listed"
            if missing_count > 0:
                msg += f" (purged {missing_count} missing video{'s' if missing_count != 1 else ''})"
            self._log_msg(msg)

    def _load_existing_clips(self):
        self._rescan_clips(verbose=False)

    # ---- actions ---------------------------------------------------------------
    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.v_folder.get() or str(Path.home()))
        if d:
            self.v_folder.set(d)
            self._rescan_clips(verbose=True)

    def _open_folder(self):
        p = Path(self.v_folder.get())
        p.mkdir(parents=True, exist_ok=True)
        open_path(p)

    def _clip_now(self):
        if self.engine and self.engine.is_alive() and hasattr(self.engine, "request_clip"):
            self.engine.request_clip()

    def _collect(self):
        return {
            "mode": self.v_mode.get(),
            "platform": self.v_platform.get(),
            "channel": self.v_channel.get().strip(),
            "video_source": self.v_channel.get().strip(),
            "folder": self.v_folder.get().strip(),
            "quality": self.v_quality.get(),
            "sensitivity": round(float(self.v_sens.get()), 1),
            "video_sens": round(float(self.v_video_sens.get()), 1),
            "min_messages": int(float(self.v_min.get())),
            "clip_preset": self.v_clip_preset.get(),
            "clip_seconds": int(float(self.v_total_sec.get())),
            "before": int(float(self.v_before.get())),
            "after": int(float(self.v_after.get())),
            "max_clips": self.v_max_clips.get(),
            "captions": bool(self.v_caps.get()),
            "keep_full": bool(self.v_keep.get()),
            "soft_cuts": bool(self.v_soft.get()),
        }

    def _toggle(self):
        if self.engine and self.engine.is_alive():
            self.engine.stop()
            self.btn.configure(state="disabled", text="STOPPING...")
            self.btn_clip.configure(state="disabled")
            return

        missing = [t for t in ("streamlink", "ffmpeg", "ffprobe") if not have(t)]
        if missing:
            messagebox.showerror(APP_NAME, "Not found on PATH: " + ", ".join(missing)
                                 + "\n\nSee README.md for install steps.")
            return

        cfg = self._collect()
        if not cfg["channel"] or not cfg["folder"]:
            msg = "Enter a stream or channel name, and a clips folder." if cfg["mode"] == "Live Stream" else "Select a video file or enter a video URL, and a clips folder."
            messagebox.showerror(APP_NAME, msg)
            return

        self.cfg = cfg
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
        self.history.clear()

        if cfg["mode"] == "Live Stream":
            self.engine = Engine(
                cfg,
                log=lambda m: self.q.put(("log", m)),
                status=lambda t, msgs=None, clips=None, spike=False:
                    self.q.put(("status", (t, msgs, clips, spike))),
                on_clip=lambda p: self.q.put(("clip", p)))
            self.btn.configure(text="STOP", fg_color=RED, hover_color="#ff7375", text_color="#ffffff")
            self.btn_clip.configure(state="normal")
        else:
            self.engine = VideoScanner(
                cfg,
                log=lambda m: self.q.put(("log", m)),
                status=lambda t, msgs=None, clips=None, spike=False:
                    self.q.put(("status", (t, msgs, clips, spike))),
                on_clip=lambda p: self.q.put(("clip", p)),
                on_timeline=lambda bars: self.q.put(("timeline", bars)))
            self.btn.configure(text="CANCEL SCAN", fg_color=RED, hover_color="#ff7375", text_color="#ffffff")
            self.btn_clip.configure(state="disabled")

        self.engine.start()

    # ---- UI pump -----------------------------------------------------------------
    def _set_status(self, text):
        color = MUTED
        if text.startswith("LIVE") or "Cutting" in text:
            color = RED
            if self.v_mode.get() == "Live Stream":
                self.btn_clip.configure(state="normal")
        elif text.startswith("Offline") or "Scanning" in text:
            color = AMBER
        elif text.startswith("Completed") or text.startswith("Done"):
            color = "#52c41a"
        self.dot.configure(text_color=color)
        self.status_lbl.configure(text=text.replace(" - ", "  -  "))

    def _pump(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.log.configure(state="normal")
                    self.log.insert("end", f"{datetime.now():%H:%M:%S}  {val}\n")
                    self.log.see("end")
                    self.log.configure(state="disabled")
                elif kind == "clip":
                    self._add_clip(val)
                elif kind == "timeline":
                    self.history.clear()
                    self.history.extend(val)
                    self._draw_chart()
                else:
                    text, msgs, clips, spike = val
                    self._set_status(text)
                    if msgs is not None:
                        self.stat_chat.configure(text=str(msgs))
                        if self.v_mode.get() == "Live Stream":
                            self.history.append((msgs, spike))
                            self._draw_chart()
                    if clips is not None:
                        self.stat_clips.configure(text=str(clips))
        except queue.Empty:
            pass

        if self.engine and not self.engine.is_alive():
            is_live = (self.v_mode.get() == "Live Stream")
            btn_txt = "START WATCHING" if is_live else "SCAN & AUTO-CLIP VIDEO"
            if self.btn.cget("text") != btn_txt:
                self.btn.configure(state="normal", text=btn_txt, fg_color=ACCENT,
                                   hover_color="#ffe14d", text_color="#000000")
                self.btn_clip.configure(state="disabled")

        self.after(200, self._pump)

    def _close(self):
        if self.engine and self.engine.is_alive():
            self.engine.stop()
        self.destroy()


def main():
    if ctk is None:
        print("Missing GUI library. Run:  pip install customtkinter")
        sys.exit(1)
    App().mainloop()


if __name__ == "__main__":
    main()
