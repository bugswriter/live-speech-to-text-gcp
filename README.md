# GCP Audio Transcriber

A simple Python tool to convert any audio file (short or long) into text using Google Cloud Platform's Speech-to-Text API. It handles file uploads to GCS automatically for long-form audio processing.

## ☁️ GCP Setup

1.  **GCP Account**: Ensure you have a Google Cloud Platform account with **Billing Enabled**.
2.  **Enable API**: Search for and enable the **Cloud Speech-to-Text API**.
3.  **Storage**: Create a **Cloud Storage Bucket** (e.g., `bwai-stt-audio`).
4.  **Credentials**:
    * Create a **Service Account** with *Storage Admin* and *Cloud Speech Client* roles.
    * Download the key as `key.json`.
    * Convert the key to a base64 string for your environment variables:
        ```bash
        cat key.json | base64 -w 0
        ```

## ⚙️ Configuration

Create a `.env` file in the root directory:

```env
GOOGLE_BUCKET_NAME=your-bucket-name
GOOGLE_CREDENTIALS_BASE64=your_base64_string_here

```

## 🚀 Installation

This project uses `uv` for dependency management.

```bash
uv sync

```

## 🏃‍♂️ Usage

Run the script by providing the path to your audio file.

**Test with a sample:**

```bash
uv run main.py test/sample.wav

```

**Run on any file:**

```bash
uv run main.py path/to/your/audio.mp3

```

The script will:

1. Upload the audio to your GCS bucket.
2. Transcribe it using the LongRunningRecognize method.
3. Print the text and save it to a `.txt` file.

```

### Next Step
Since you are using `uv`, would you like me to generate the `pyproject.toml` file content as well to ensure the dependencies (`google-cloud-speech`, `google-cloud-storage`, `python-dotenv`) are defined correctly?
