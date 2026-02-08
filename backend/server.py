"""
WebSocket server for handling WebRTC audio streams and managing meeting state.

Features:
- Real-time audio streaming to Google Speech API
- 30-second interval Gemini processing
- SQLite persistence for meetings
- Meeting continuation support
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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from google.cloud import speech
from google.oauth2 import service_account
from dotenv import load_dotenv

from meeting_state import MeetingNoteManager, MeetingNote, AgendaItem
import database as db

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

# Audio parameters
SAMPLE_RATE = 16000


def get_credentials() -> service_account.Credentials:
    """Decode base64 credentials from environment."""
    creds_base64 = os.getenv("GOOGLE_CREDENTIALS_BASE64")
    if not creds_base64:
        raise ValueError("GOOGLE_CREDENTIALS_BASE64 not set")
    creds_json = base64.b64decode(creds_base64).decode("utf-8")
    creds_info = json.loads(creds_json)
    return service_account.Credentials.from_service_account_info(creds_info)


# Store active meetings in memory
active_meetings: dict[str, "MeetingSession"] = {}


class MeetingSession:
    """Manages a single meeting session."""

    def __init__(self, meeting_id: str, loop: asyncio.AbstractEventLoop):
        self.meeting_id = meeting_id
        self.loop = loop
        self.clients: list[WebSocket] = []
        self.audio_queue: queue.Queue[bytes | None] = queue.Queue()
        self.is_streaming = False
        self._stream_thread: Optional[threading.Thread] = None
        
        # Load existing meeting or create new
        existing = db.get_meeting(meeting_id)
        if existing:
            self.note_manager = MeetingNoteManager(
                meeting_id=meeting_id,
                on_state_update=self._on_update,
                initial_state=existing
            )
            print(f"[{meeting_id}] Loaded existing meeting: {existing['title']}")
        else:
            db.create_meeting(meeting_id)
            self.note_manager = MeetingNoteManager(
                meeting_id=meeting_id,
                on_state_update=self._on_update
            )
            print(f"[{meeting_id}] Created new meeting")

    def _on_update(self):
        """Called when meeting state changes - save and broadcast."""
        # Save to database
        db.update_meeting(self.meeting_id, self.note_manager.to_dict())
        # Broadcast to clients
        asyncio.run_coroutine_threadsafe(self.broadcast_state(), self.loop)

    async def add_client(self, websocket: WebSocket):
        """Add a client and send current state."""
        self.clients.append(websocket)
        await websocket.send_json({
            "type": "state_sync",
            "meeting": self.note_manager.to_dict()
        })

    def remove_client(self, websocket: WebSocket):
        if websocket in self.clients:
            self.clients.remove(websocket)

    async def broadcast_state(self):
        """Push updated meeting state to all clients."""
        state = {"type": "state_update", "meeting": self.note_manager.to_dict()}
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
        message = {"type": "interim_transcript", "text": text, "speaker": speaker}
        for client in self.clients:
            try:
                await client.send_json(message)
            except Exception:
                pass

    def process_audio_chunk(self, chunk: bytes):
        """Queue audio chunk."""
        self.audio_queue.put(chunk)

    async def start_streaming(self):
        """Start Google Speech streaming and Gemini processing."""
        if self.is_streaming:
            return
        
        self.is_streaming = True
        db.set_meeting_active(self.meeting_id, True)
        
        # Start Gemini processing loop
        await self.note_manager.start()
        
        # Start Speech API thread
        self._stream_thread = threading.Thread(
            target=self._run_speech_streaming,
            daemon=True
        )
        self._stream_thread.start()
        
        print(f"[{self.meeting_id}] Streaming started")

    async def stop_streaming(self):
        """Stop streaming and save final state."""
        if not self.is_streaming:
            return
            
        self.is_streaming = False
        db.set_meeting_active(self.meeting_id, False)
        print(f"[{self.meeting_id}] Stopping streaming...")
        
        # Signal audio generator to stop
        self.audio_queue.put(None)
        
        if self._stream_thread:
            self._stream_thread.join(timeout=5)
        
        # Final processing
        await self.note_manager.stop()
        
        # Save final state
        db.update_meeting(self.meeting_id, self.note_manager.to_dict())
        
        await self.broadcast_state()
        print(f"[{self.meeting_id}] Streaming stopped and saved")

    def _run_speech_streaming(self):
        """Run Google Speech API streaming in thread."""
        credentials = get_credentials()
        client = speech.SpeechClient(credentials=credentials)

        diarization_config = speech.SpeakerDiarizationConfig(
            enable_speaker_diarization=True,
            min_speaker_count=1,
            max_speaker_count=4
        )

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
                responses = client.streaming_recognize(
                    config=streaming_config,
                    requests=audio_generator()
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
                    
                    speaker = None
                    if result.alternatives[0].words:
                        tag = result.alternatives[0].words[-1].speaker_tag
                        speaker = f"Speaker {tag}"

                    if is_final:
                        print(f"[FINAL] {speaker}: {transcript}")
                        asyncio.run_coroutine_threadsafe(
                            self._handle_final_transcript(transcript, speaker),
                            self.loop
                        )
                    else:
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
                import time
                time.sleep(0.5)

        print(f"[{self.meeting_id}] Speech streaming thread ended")

    async def _handle_final_transcript(self, transcript: str, speaker: Optional[str]):
        """Handle final transcript result."""
        await self.note_manager.add_transcript(
            text=transcript,
            speaker=speaker,
            timestamp=datetime.now().isoformat()
        )
        # Save incrementally
        db.update_meeting(self.meeting_id, self.note_manager.to_dict())
        await self.broadcast_state()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Cleanup on shutdown."""
    yield
    for session in active_meetings.values():
        await session.stop_streaming()


app = FastAPI(title="Meeting Notes", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


# ============ API Routes ============

@app.get("/")
async def root():
    """Serve the main page."""
    return FileResponse(static_dir / "index.html")


@app.get("/api/meetings")
async def list_meetings(limit: int = 50, offset: int = 0):
    """List all meetings."""
    return db.list_meetings(limit, offset)


@app.post("/api/meetings")
async def create_meeting(data: Optional[dict] = None):
    """Create a new meeting."""
    import uuid
    meeting_id = f"meeting-{uuid.uuid4().hex[:8]}"
    title = data.get("title", "Untitled Meeting") if data else "Untitled Meeting"
    initial_agenda = data.get("agenda") if data else None # Pass initial agenda
    return db.create_meeting(meeting_id, title, initial_agenda)


@app.get("/api/meetings/{meeting_id}")
async def get_meeting(meeting_id: str):
    """Get a specific meeting."""
    meeting = db.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting


@app.put("/api/meetings/{meeting_id}")
async def update_meeting(meeting_id: str, data: dict):
    """Update a meeting."""
    meeting = db.update_meeting(meeting_id, data)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    
    # Broadcast to active clients
    if meeting_id in active_meetings:
        await active_meetings[meeting_id].broadcast_state()
    
    return meeting


@app.delete("/api/meetings/{meeting_id}")
async def delete_meeting(meeting_id: str):
    """Delete a meeting."""
    if meeting_id in active_meetings:
        await active_meetings[meeting_id].stop_streaming()
        del active_meetings[meeting_id]
    
    if not db.delete_meeting(meeting_id):
        raise HTTPException(status_code=404, detail="Meeting not found")
    
    return {"status": "deleted"}

# New API endpoint for updating agenda item status
@app.post("/api/meetings/{meeting_id}/agenda/{item_id}/status")
async def update_agenda_item_status(meeting_id: str, item_id: str, data: dict):
    """Update the completion status of an agenda item."""
    session = active_meetings.get(meeting_id)
    if not session:
        # Load from DB if not active, update, then save
        meeting_data = db.get_meeting(meeting_id)
        if not meeting_data:
            raise HTTPException(status_code=404, detail="Meeting not found")
        
        # Create a temporary manager to update agenda
        temp_manager = MeetingNoteManager(meeting_id=meeting_id, initial_state=meeting_data)
        temp_manager.update_agenda_item_status(item_id, data.get("completed", False))
        db.update_meeting(meeting_id, temp_manager.to_dict())
        # No broadcast needed if not active
        return {"status": "success", "meeting": temp_manager.to_dict()}
    
    session.note_manager.update_agenda_item_status(item_id, data.get("completed", False))
    await session.broadcast_state()
    return {"status": "success", "meeting": session.note_manager.to_dict()}

# New API endpoint for adding an agenda item
@app.post("/api/meetings/{meeting_id}/agenda")
async def add_agenda_item(meeting_id: str, data: dict):
    """Add a new agenda item to the meeting."""
    text = data.get("text")
    if not text:
        raise HTTPException(status_code=400, detail="Agenda item text is required.")

    session = active_meetings.get(meeting_id)
    if not session:
        # Load from DB if not active, add, then save
        meeting_data = db.get_meeting(meeting_id)
        if not meeting_data:
            raise HTTPException(status_code=404, detail="Meeting not found")
        
        temp_manager = MeetingNoteManager(meeting_id=meeting_id, initial_state=meeting_data)
        temp_manager.add_agenda_item(text)
        db.update_meeting(meeting_id, temp_manager.to_dict())
        return {"status": "success", "meeting": temp_manager.to_dict()}
    
    session.note_manager.add_agenda_item(text)
    await session.broadcast_state()
    return {"status": "success", "meeting": session.note_manager.to_dict()}


# ============ WebSocket ============

@app.websocket("/ws/meeting/{meeting_id}")
async def websocket_meeting(websocket: WebSocket, meeting_id: str):
    """WebSocket endpoint for meeting audio streaming."""
    await websocket.accept()
    
    loop = asyncio.get_event_loop()
    
    # Get or create session
    if meeting_id not in active_meetings:
        active_meetings[meeting_id] = MeetingSession(meeting_id, loop)
    
    session = active_meetings[meeting_id]
    await session.add_client(websocket)
    
    try:
        while True:
            try:
                message = await websocket.receive()
            except RuntimeError as e:
                # Handle "Cannot call receive once disconnect received"
                if "disconnect" in str(e).lower():
                    break
                raise
            
            # Check for disconnect message
            if message.get("type") == "websocket.disconnect":
                break
            
            if "bytes" in message:
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
                    db.update_meeting(meeting_id, session.note_manager.to_dict())
                    await session.broadcast_state()

    except WebSocketDisconnect:
        pass
    finally:
        session.remove_client(websocket)
        # Keep session around for potential reconnects


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
