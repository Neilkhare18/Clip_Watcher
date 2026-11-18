# Clip Watcher

Clip Watcher is a desktop application designed to automatically monitor live streams or scan recorded videos/VODs, identify high-energy moments (chat activity surges or audio loudness peaks), and generate short video clips.

---

## Highlights & Features

### 1. Live Stream Mode
- **Multi-Platform Support**:
  - **Twitch**: Anonymous IRC live chat reader.
  - **YouTube Live**: Real-time chat streaming using `pytchat`.
  - **Kick**: Pusher WebSocket protocol listener.
  - **Custom Streams**: Any stream supported by Streamlink (HLS `.m3u8`, RTMP, Dailymotion, etc.).
- **Automated Spike Detection**: Detects chat message volume surges to capture funny, surprising, or clutch stream moments.
- **Manual Clip Button**: Immediate "CLIP NOW" button to cut a highlight on demand while watching.
- **Real-Time Rolling Chart**: Displays live chat message frequency and trigger thresholds as streams progress.

### 2. Video / VOD Mode
- **Flexible Video Input**:
  - **Local Media**: MP4, MKV, MOV, TS, WEBM, AVI, and other common formats.
  - **Online Video Links**: YouTube videos, Twitch VODs, Kick VODs, or direct URLs.
- **Audio Energy Highlight Detection**: Scans audio loudness peaks (reactions, cheering, loud action) using FFmpeg.
- **Waveform Timeline**: Visual graph illustrating the video's audio energy profile with markers where clips are extracted.
- **Highlight Selector**: Choose how many clips to extract (Top 5, Top 10, Top 15, Top 25, or all detected spikes) with customizable pre- and post-event durations.

### 3. General Capabilities
- **Flexible Clip Durations**: Presets (30s, 35s, 40s, 60s) or customizable duration with balanced lead-in and reaction splits.
- **Smooth Audio/Video Transitions**: Built-in micro-fades to avoid harsh cuts and audio clicks.
- **Optional Whisper Subtitles**: Support for local AI caption generation (`.srt`) with `faster-whisper`.
- **Integrated Recent Clips Gallery**: Review and preview your clips directly within the application.

---

## Installation & Setup

### Prerequisites
1. **Python 3.9 or higher**: [python.org](https://www.python.org/downloads/)
2. **FFmpeg installed and available on system PATH**:
   - **Windows**: Install using Winget (`winget install Gyan.FFmpeg`) or Chocolatey (`choco install ffmpeg`).
   - **macOS**: Install using Homebrew (`brew install ffmpeg`).
   - **Linux**: Install using package manager (`sudo apt install ffmpeg`).

### Quick Start (Windows)
1. Clone or download this repository.
2. Double-click `run.bat`.
   - The launcher script checks your Python and FFmpeg paths, installs required libraries, and starts Clip Watcher.

### Manual Setup (Cross-Platform)
1. Clone the repository:
   ```bash
   git clone https://github.com/your-username/clip-watcher.git
   cd clip-watcher
   ```
2. (Optional) Create and activate a virtual environment:
   ```bash
   python -m venv venv
   # On Windows:
   venv\Scripts\activate
   # On macOS/Linux:
   source venv/bin/activate
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Run the application:
   ```bash
   python clipwatcher.py
   ```

### Optional AI Subtitles
To enable automatic subtitle transcription:
```bash
pip install faster-whisper
```

---

## Project Structure

```text
clip-watcher/
├── clipwatcher.py       # Main application (GUI, detection engines, stream & video processing)
├── requirements.txt     # Python package dependencies
├── run.bat              # One-click Windows launch script with environment verification
├── .gitignore           # Git ignore rules for caches, media outputs, and local configs
├── .gitattributes       # Line-ending and binary file normalizations
└── README.md            # Documentation and instructions
```

---

## How Detection Works

1. **Live Chat Velocity**:
   - Calculates message rates over short sliding time windows.
   - Triggers an automated clip when message frequency exceeds a multiple of the recent running average.

2. **VOD Audio Energy**:
   - Analyzes audio volume across the duration of the video.
   - Identifies local maximum loudness events and extracts corresponding video segments.
