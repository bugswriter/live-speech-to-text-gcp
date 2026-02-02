# Meeting Notes - Technical Architecture

## Overview

A real-time meeting transcription and note-taking system that converts speech to structured notes using Google Cloud Speech-to-Text for transcription and Google Gemini for intelligent note generation.

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              BROWSER                                         │
│                                                                              │
│  ┌──────────────┐     ┌─────────────────┐     ┌──────────────────┐         │
│  │ getUserMedia │────▶│  AudioWorklet   │────▶│    WebSocket     │         │
│  │ (microphone) │     │  (PCM capture)  │     │  (binary frames) │         │
│  └──────────────┘     └─────────────────┘     └────────┬─────────┘         │
│                                                         │                    │
│  Audio: 16kHz, Mono, Int16 PCM                         │                    │
│                                                         │                    │
│  ┌─────────────────────────────────────────────────────┼──────────────────┐ │
│  │                         UI                          │                  │ │
│  │  ┌─────────┐  ┌──────────┐  ┌─────────┐            │                  │ │
│  │  │ Summary │  │Key Points│  │ Actions │  ◀─────────┘                  │ │
│  │  └─────────┘  └──────────┘  └─────────┘   (JSON state updates)        │ │
│  │  ┌──────────────────────────────────────┐                             │ │
│  │  │           Live Transcript            │                             │ │
│  │  └──────────────────────────────────────┘                             │ │
│  └───────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    │ WebSocket (ws://)
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         BACKEND (FastAPI)                                    │
│                                                                              │
│  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │                        MeetingSession                                   │ │
│  │                                                                         │ │
│  │  ┌─────────────────┐                                                   │ │
│  │  │  Audio Queue    │  Thread-safe queue.Queue                          │ │
│  │  │  (thread-safe)  │  Bridges async WebSocket ←→ sync Speech API       │ │
│  │  └────────┬────────┘                                                   │ │
│  │           │                                                             │ │
│  │           ▼                                                             │ │
│  │  ┌─────────────────────────────────────────────────────────────────┐   │ │
│  │  │              Google Speech API Thread                            │   │ │
│  │  │                                                                  │   │ │
│  │  │  def audio_generator():                                          │   │ │
│  │  │      while streaming:                                            │   │ │
│  │  │          chunk = queue.get()  # blocks until audio available     │   │ │
│  │  │          yield StreamingRecognizeRequest(audio_content=chunk)    │   │ │
│  │  │                                                                  │   │ │
│  │  │  responses = client.streaming_recognize(requests=audio_generator)│   │ │
│  │  │  for response in responses:                                      │   │ │
│  │  │      # Process interim and final results                         │   │ │
│  │  └─────────────────────────────────────────────────────────────────┘   │ │
│  │           │                                                             │ │
│  │           │ Final transcripts                                           │ │
│  │           ▼                                                             │ │
│  │  ┌─────────────────────────────────────────────────────────────────┐   │ │
│  │  │              MeetingNoteManager                                  │   │ │
│  │  │                                                                  │   │ │
│  │  │  _transcript_buffer: []  ← Accumulates transcripts               │   │ │
│  │  │                                                                  │   │ │
│  │  │  Every 30 seconds:                                               │   │ │
│  │  │  ┌─────────────────────────────────────────────────────────┐    │   │ │
│  │  │  │                    GEMINI API                            │    │   │ │
│  │  │  │                                                          │    │   │ │
│  │  │  │  Input:                                                  │    │   │ │
│  │  │  │  - new_transcript (last 30 sec of conversation)          │    │   │ │
│  │  │  │  - previous_summary (context for incomplete sentences)   │    │   │ │
│  │  │  │                                                          │    │   │ │
│  │  │  │  Output (JSON):                                          │    │   │ │
│  │  │  │  - summary                                               │    │   │ │
│  │  │  │  - key_points[]                                          │    │   │ │
│  │  │  │  - action_items[{task, assignee, context}]               │    │   │ │
│  │  │  │  - decisions[{decision, rationale, participants}]        │    │   │ │
│  │  │  │  - open_questions[]                                      │    │   │ │
│  │  │  └─────────────────────────────────────────────────────────┘    │   │ │
│  │  └─────────────────────────────────────────────────────────────────┘   │ │
│  │           │                                                             │ │
│  │           │ Broadcast state update                                      │ │
│  │           ▼                                                             │ │
│  │  ┌─────────────────┐                                                   │ │
│  │  │ SQLite Database │  Persists meetings for later access               │ │
│  │  └─────────────────┘                                                   │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Component Details

### 1. Browser Audio Capture

**Technology**: Web Audio API + AudioWorklet

```javascript
// AudioWorklet runs in separate audio thread for low latency
class AudioProcessor extends AudioWorkletProcessor {
    process(inputs) {
        // Convert Float32 [-1, 1] to Int16 [-32768, 32767]
        const int16 = new Int16Array(samples.length);
        for (let i = 0; i < samples.length; i++) {
            int16[i] = samples[i] * 0x7FFF;
        }
        this.port.postMessage(int16.buffer);
        return true;
    }
}
```

**Why AudioWorklet instead of MediaRecorder?**
- MediaRecorder outputs WebM/Opus (compressed)
- Google Speech API works best with LINEAR16 (uncompressed PCM)
- AudioWorklet gives us raw samples we can convert to exact format needed

**Audio Format**:
| Parameter | Value |
|-----------|-------|
| Sample Rate | 16,000 Hz |
| Channels | 1 (Mono) |
| Bit Depth | 16-bit signed integer |
| Encoding | LINEAR16 (raw PCM) |

### 2. WebSocket Communication

**Protocol**:
- **Binary frames**: Raw PCM audio chunks (~4096 samples = 256ms)
- **Text frames**: JSON commands and state updates

**Commands** (Client → Server):
```json
{"type": "start_recording"}
{"type": "stop_recording"}
{"type": "update_title", "title": "Q4 Planning Meeting"}
```

**Events** (Server → Client):
```json
{"type": "state_sync", "meeting": {...}}
{"type": "state_update", "meeting": {...}}
{"type": "interim_transcript", "text": "...", "speaker": "Speaker 1"}
{"type": "recording_started"}
{"type": "recording_stopped", "meeting": {...}}
```

### 3. Google Speech-to-Text API

**Integration Pattern**: Synchronous streaming in a separate thread

```python
def _run_speech_streaming(self):
    """Runs in separate thread to not block async event loop."""
    
    # Configuration matching transcribe_live.py
    diarization_config = speech.SpeakerDiarizationConfig(
        enable_speaker_diarization=True,
        min_speaker_count=1,
        max_speaker_count=4
    )
    
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=16000,
        language_code="en-US",
        enable_automatic_punctuation=True,
        diarization_config=diarization_config
    )
    
    # Generator yields audio from thread-safe queue
    def audio_generator():
        while self.is_streaming:
            chunk = self.audio_queue.get(timeout=0.1)
            yield speech.StreamingRecognizeRequest(audio_content=chunk)
    
    # Blocking call - runs until stream ends
    responses = client.streaming_recognize(
        config=streaming_config,
        requests=audio_generator()
    )
    
    for response in responses:
        # Process results...
```

**Key Behaviors**:
- **Interim results**: Partial transcriptions while speaking (for UI feedback)
- **Final results**: Complete sentences with punctuation
- **Speaker diarization**: Identifies different speakers (Speaker 1, Speaker 2, etc.)
- **5-minute limit**: Google limits streaming sessions; we auto-restart

### 4. Gemini Note Generation

**Trigger**: Every 30 seconds (configurable via `PROCESS_INTERVAL`)

**Context Handling** (Edge Case: Incomplete Sentences):
```python
prompt = f"""
IMPORTANT CONTEXT:
Previous summary (use this to understand incomplete sentences): 
{previous_summary if previous_summary else "This is the start of the meeting."}

NEW TRANSCRIPT TO PROCESS:
{transcript_text}
"""
```

**Why pass previous summary?**
- User might say "...and that's why I think we should do it" at interval boundary
- Without context, "it" is ambiguous
- Previous summary provides the referent

**Output Schema**:
```json
{
    "summary": "2-3 sentence summary of this segment",
    "key_points": ["Important point 1", "Important point 2"],
    "action_items": [
        {"task": "Description", "assignee": "Person", "context": "Why"}
    ],
    "decisions": [
        {"decision": "What", "rationale": "Why", "participants_involved": ["names"]}
    ],
    "open_questions": ["Unresolved question"],
    "opinion_changes": [
        {"speaker": "name", "from": "old stance", "to": "new stance", "topic": "what"}
    ]
}
```

### 5. Data Persistence (SQLite)

**Schema**:
```sql
CREATE TABLE meetings (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    transcript JSON,      -- Array of {speaker, text, timestamp}
    summary TEXT,
    key_points JSON,      -- Array of strings
    action_items JSON,    -- Array of objects
    decisions JSON,       -- Array of objects
    open_questions JSON,  -- Array of strings
    participants JSON,    -- Array of strings
    is_active BOOLEAN DEFAULT FALSE
);
```

**Operations**:
- **Create**: New meeting on "New Meeting" click
- **Update**: After each Gemini processing cycle and on stop
- **Load**: Resume existing meeting
- **List**: Show all past meetings

## Data Flow Timeline

```
T=0s    User clicks "Start Recording"
        → getUserMedia() gets microphone
        → AudioWorklet starts capturing
        → WebSocket sends "start_recording"
        → Server starts Speech API thread

T=0.1s  First audio chunk arrives
        → Queued for Speech API
        → Speech API starts processing

T=0.5s  First interim result
        → Server broadcasts to UI
        → UI shows "Speaking: hello..."

T=2s    First final result
        → "Hello everyone, let's discuss the project."
        → Added to transcript buffer
        → UI updates transcript section

T=30s   Processing interval triggered
        → Buffer has 15 transcript entries
        → Sent to Gemini with previous context
        → Gemini returns structured notes
        → State saved to SQLite
        → UI updates all sections

T=35s   Continue accumulating...

T=5m    Speech API limit reached
        → Auto-restart streaming
        → No audio lost (queue persists)

T=45m   User clicks "Stop Recording"
        → Final buffer processed
        → Meeting saved to database
        → UI shows final state
```

## File Structure

```
backend/
├── server.py              # FastAPI app, WebSocket handling, Speech API integration
├── meeting_state.py       # MeetingNoteManager, Gemini integration
├── database.py            # SQLite operations
└── static/
    ├── index.html         # Main UI
    ├── meeting-client.js  # WebSocket client, audio capture
    └── audio-processor.js # AudioWorklet for PCM conversion
```

## Environment Variables

```bash
# Google Cloud credentials (base64 encoded service account JSON)
GOOGLE_CREDENTIALS_BASE64=eyJ0eXBlIjoi...

# Google Cloud Storage bucket (for file transcription)
GOOGLE_BUCKET_NAME=my-bucket

# Gemini API key (from https://aistudio.google.com/app/apikey)
GEMINI_API_KEY=AIzaSy...
```

## Running the Application

```bash
# Install dependencies
uv sync

# Start server
cd backend && uv run python server.py

# Open http://localhost:8000
```

## Limitations & Considerations

1. **Google Speech 5-min limit**: Streaming sessions auto-restart, but there may be a brief gap
2. **Speaker diarization accuracy**: Works best with distinct speakers and clear audio
3. **Gemini rate limits**: Free tier has request limits
4. **Browser compatibility**: AudioWorklet requires modern browsers (Chrome 66+, Firefox 76+)
5. **HTTPS required**: getUserMedia requires secure context in production
