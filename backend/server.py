"""
WebSocket server for handling WebRTC audio streams and managing meeting state.

Key insight: Google Speech API streaming works synchronously.
We need to bridge async WebSocket with sync Speech API properly.

Audio Format:
- Browser sends raw PCM (Int16, 16kHz, mono) via AudioWorklet
- This matches Google Speech API LINEAR16 format exactly
"""

import asyncio
import json
import base64
import os
import queue
import threading
from datetime import datetime
from typing import Optional
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from google.cloud import speech
from google.oauth2 import service_account
from dotenv import load_dotenv

from meeting_state import MeetingNoteManager

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

# Audio parameters - must match browser AudioWorklet
SAMPLE_RATE = 16000


def get_credentials() -> service_account.Credentials:
    """Decode base64 credentials from environment."""
    creds_base64 = os.getenv("GOOGLE_CREDENTIALS_BASE64")
    if not creds_base64:
        raise ValueError("GOOGLE_CREDENTIALS_BASE64 not set")
    creds_json = base64.b64decode(creds_base64).decode("utf-8")
    creds_info = json.loads(creds_json)
    return service_account.Credentials.from_service_account_info(creds_info)


# Store active meetings
active_meetings: dict[str, "MeetingSession"] = {}


class MeetingSession:
    """Manages a single meeting session."""

    def __init__(self, meeting_id: str, loop: asyncio.AbstractEventLoop):
        self.meeting_id = meeting_id
        self.loop = loop
        self.clients: list[WebSocket] = []
        
        # Thread-safe queue for audio chunks
        self.audio_queue: queue.Queue[bytes | None] = queue.Queue()
        
        self.is_streaming = False
        self._stream_thread: Optional[threading.Thread] = None
        
        # Note manager with callback for broadcasting state updates
        self.note_manager = MeetingNoteManager(
            meeting_id=meeting_id,
            on_state_update=self._sync_broadcast_state
        )

    def _sync_broadcast_state(self):
        """Sync wrapper to call async broadcast from thread."""
        asyncio.run_coroutine_threadsafe(self.broadcast_state(), self.loop)

    async def add_client(self, websocket: WebSocket):
        """Add a client and send current state."""
        self.clients.append(websocket)
        await websocket.send_json({
            "type": "state_sync",
            "meeting": self.note_manager.to_dict()
        })

    def remove_client(self, websocket: WebSocket):
        """Remove a disconnected client."""
        if websocket in self.clients:
            self.clients.remove(websocket)

    async def broadcast_state(self):
        """Push updated meeting state to all clients."""
        state = {
            "type": "state_update",
            "meeting": self.note_manager.to_dict()
        }
        disconnected = []
        for client in self.clients:
            try:
                await client.send_json(state)
            except Exception:
                disconnected.append(client)
        for client in disconnected:
            self.remove_client(client)

    async def broadcast_interim(self, text: str, speaker: Optional[str]):
        """Send interim transcript to all clients."""
        message = {
            "type": "interim_transcript",
            "text": text,
            "speaker": speaker
        }
        for client in self.clients:
            try:
                await client.send_json(message)
            except Exception:
                pass

    def process_audio_chunk(self, chunk: bytes):
        """Queue audio chunk (called from async context)."""
        self.audio_queue.put(chunk)

    async def start_streaming(self):
        """Start Google Speech streaming and Gemini processing loop."""
        if self.is_streaming:
            return
        
        self.is_streaming = True
        
        # Start the note manager's 30-second processing loop
        await self.note_manager.start()
        
        # Start Google Speech streaming in a separate thread
        self._stream_thread = threading.Thread(
            target=self._run_speech_streaming,
            daemon=True
        )
        self._stream_thread.start()
        
        print(f"[{self.meeting_id}] Streaming started")

    async def stop_streaming(self):
        """Stop streaming and do final processing."""
        if not self.is_streaming:
            return
            
        self.is_streaming = False
        print(f"[{self.meeting_id}] Stopping streaming...")
        
        # Signal the audio generator to stop
        self.audio_queue.put(None)
        
        # Wait for thread to finish
        if self._stream_thread:
            self._stream_thread.join(timeout=5)
        
        # Stop note manager (processes any remaining buffer)
        await self.note_manager.stop()
        
        # Broadcast final state
        await self.broadcast_state()
        print(f"[{self.meeting_id}] Streaming stopped")

    def _run_speech_streaming(self):
        """
        Run Google Speech API streaming (in thread).
        
        This is based on transcribe_live.py which works.
        """
        credentials = get_credentials()
        client = speech.SpeechClient(credentials=credentials)

        # Speaker diarization config
        diarization_config = speech.SpeakerDiarizationConfig(
            enable_speaker_diarization=True,
            min_speaker_count=1,
            max_speaker_count=4
        )

        # Recognition config - LINEAR16 format
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=SAMPLE_RATE,
            language_code="en-US",
            enable_automatic_punctuation=True,
            diarization_config=diarization_config
        )

        streaming_config = speech.StreamingRecognitionConfig(
            config=config,
            interim_results=True,
        )

        def audio_generator():
            """Yield audio chunks from queue."""
            while self.is_streaming:
                try:
                    chunk = self.audio_queue.get(timeout=0.1)
                    if chunk is None:
                        break
                    yield speech.StreamingRecognizeRequest(audio_content=chunk)
                except queue.Empty:
                    continue

        while self.is_streaming:
            try:
                print(f"[{self.meeting_id}] Starting Speech API stream...")
                requests = audio_generator()
                responses = client.streaming_recognize(
                    config=streaming_config,
                    requests=requests
                )
                
                for response in responses:
                    if not self.is_streaming:
                        break
                        
                    if not response.results:
                        continue

                    result = response.results[0]
                    if not result.alternatives:
                        continue

                    transcript = result.alternatives[0].transcript
                    is_final = result.is_final
                    
                    # Get speaker tag
                    speaker = None
                    if result.alternatives[0].words:
                        tag = result.alternatives[0].words[-1].speaker_tag
                        speaker = f"Speaker {tag}"

                    if is_final:
                        print(f"[FINAL] {speaker}: {transcript}")
                        # Schedule async task to add transcript
                        asyncio.run_coroutine_threadsafe(
                            self._handle_final_transcript(transcript, speaker),
                            self.loop
                        )
                    else:
                        # Schedule async task to broadcast interim
                        asyncio.run_coroutine_threadsafe(
                            self.broadcast_interim(transcript, speaker),
                            self.loop
                        )
                        
            except Exception as e:
                if not self.is_streaming:
                    break
                error_str = str(e)
                if "Exceeded" in error_str or "deadline" in error_str.lower():
                    print(f"[{self.meeting_id}] Google 5-min limit, restarting...")
                else:
                    print(f"[{self.meeting_id}] Speech API error: {e}")
                # Brief pause before restart
                import time
                time.sleep(0.5)

        print(f"[{self.meeting_id}] Speech streaming thread ended")

    async def _handle_final_transcript(self, transcript: str, speaker: Optional[str]):
        """Handle a final transcript result."""
        await self.note_manager.add_transcript(
            text=transcript,
            speaker=speaker,
            timestamp=datetime.now().isoformat()
        )
        await self.broadcast_state()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Cleanup on shutdown."""
    yield
    for session in active_meetings.values():
        await session.stop_streaming()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static files
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def root():
    """Serve the meeting page."""
    from fastapi.responses import FileResponse
    return FileResponse(static_dir / "index.html")


@app.websocket("/ws/meeting/{meeting_id}")
async def websocket_meeting(websocket: WebSocket, meeting_id: str):
    """
    WebSocket endpoint for meeting audio streaming.
    
    Protocol:
    - Binary frames: Raw PCM audio (Int16, 16kHz, mono)
    - Text frames: JSON commands
    """
    await websocket.accept()
    
    loop = asyncio.get_event_loop()
    
    # Get or create meeting session
    if meeting_id not in active_meetings:
        active_meetings[meeting_id] = MeetingSession(meeting_id, loop)
    
    session = active_meetings[meeting_id]
    await session.add_client(websocket)
    
    try:
        while True:
            message = await websocket.receive()
            
            if "bytes" in message:
                # Audio chunk - add to queue (sync call is fine here)
                session.process_audio_chunk(message["bytes"])
            
            elif "text" in message:
                data = json.loads(message["text"])
                command = data.get("type")
                
                if command == "start_recording":
                    await session.start_streaming()
                    await websocket.send_json({"type": "recording_started"})
                
                elif command == "stop_recording":
                    await session.stop_streaming()
                    await websocket.send_json({
                        "type": "recording_stopped",
                        "meeting": session.note_manager.to_dict()
                    })
                
                elif command == "update_title":
                    session.note_manager.meeting.title = data.get("title", "")
                    await session.broadcast_state()

    except WebSocketDisconnect:
        session.remove_client(websocket)
        if not session.clients:
            await session.stop_streaming()
            if meeting_id in active_meetings:
                del active_meetings[meeting_id]


@app.get("/api/meeting/{meeting_id}")
async def get_meeting(meeting_id: str):
    """Get meeting state."""
    if meeting_id in active_meetings:
        return active_meetings[meeting_id].note_manager.to_dict()
    return {"error": "Meeting not found"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
