# GCP Audio Transcriber

A robust Python toolset to convert audio into text using Google Cloud Platform's Speech-to-Text API. Supports both file-based (batch) and live (streaming) transcription.

## ⚠️ Audio Format Notice
**GCP is strictly optimized for WAV (Linear16) files.**
* **MP3 Files:** `transcribe_file.py` will automatically convert them to the correct WAV format using `ffmpeg`.
* **Manual WAVs:** If you provide your own WAV, ensure it is **16kHz, Mono**.

## ☁️ GCP Setup

1.  **Project:** Create a Google Cloud Project with Billing Enabled.
2.  **API:** Enable "Cloud Speech-to-Text API".
3.  **Storage:** Create a Bucket (e.g., `bwai-stt-audio`).
4.  **Auth:** * Create a Service Account with **Storage Admin** and **Cloud Speech Client** roles.
    * Download `key.json`.
    * Convert to base64: `cat key.json | base64 -w 0`

## ⚙️ Installation

1.  **System Requirements (Required for conversion):**
    * **Ubuntu/Debian:** `sudo apt install ffmpeg portaudio19-dev`
    * **Arch:** `sudo pacman -S ffmpeg`
    * **Mac:** `brew install ffmpeg portaudio`

2.  **Python Dependencies:**
    ```bash
    uv sync
    # Or manually:
    uv add google-cloud-speech google-cloud-storage python-dotenv pyaudio
    ```

3.  **Configuration (.env):**
    ```env
    GOOGLE_BUCKET_NAME=your-bucket-name
    GOOGLE_CREDENTIALS_BASE64=your_base64_string_here
    ```

## 🏃‍♂️ Usage

### 1. File Transcription (Recommended)
Best for accuracy and long recordings. Automatically handles MP3->WAV conversion.

```bash
uv run transcribe_file.py my_podcast.mp3

```

* **Output:** Prints text to console and saves `my_podcast.mp3.txt`.
* **Features:** Speaker Detection, Auto-Cleanup of Cloud Storage.

### 2. Live Transcription

Listens to your microphone in real-time.

```bash
uv run transcribe_live.py

```

* **Limit:** Google restricts live streams to ~5 minutes per session.
* **Note:** Speaker detection is available but less stable than file mode.

```
