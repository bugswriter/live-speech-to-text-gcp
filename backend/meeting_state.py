"""
Meeting Note Processor with Gemini Integration

Architecture:
1. Transcripts accumulate in a buffer (from Google Speech API)
2. Every 30 seconds, buffer is sent to Gemini for processing
3. Gemini receives: current transcript batch + previous summary (for context)
4. Gemini returns: updated notes in structured JSON
5. Notes are merged into meeting state and broadcast to clients

Edge Case Handling:
- Incomplete sentences: Previous summary provides context
- Opinion changes: Gemini tracks speaker positions across batches
- Continuous listening: Processing runs in background, doesn't block audio
"""

import asyncio
import json
import os
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import Optional, Callable
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()


@dataclass
class MeetingNote:
    """The meeting state that gets pushed to frontend."""
    id: str
    title: str = "Untitled Meeting"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    
    # Raw transcript (append-only)
    transcript: list[dict] = field(default_factory=list)
    
    # AI-generated structured notes (replaced each interval)
    summary: str = ""
    key_points: list[str] = field(default_factory=list)
    action_items: list[dict] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    
    # Participant tracking
    participants: list[str] = field(default_factory=list)
    
    # For context continuity between intervals
    _previous_summary: str = field(default="", repr=False)


class GeminiProcessor:
    """
    Handles Gemini API calls for note generation.
    
    Key design: We pass the previous summary to maintain context
    across 30-second intervals, handling incomplete sentences.
    """
    
    def __init__(self):
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY not set in environment")
        
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel("gemini-2.5-flash")
    
    async def process_transcript_batch(
        self,
        new_transcript: list[dict],
        previous_summary: str,
        full_transcript: list[dict]
    ) -> dict:
        """
        Process a batch of transcripts and generate/update notes.
        
        Args:
            new_transcript: Latest 30-sec batch of transcript entries
            previous_summary: Summary from last processing (for context)
            full_transcript: Complete transcript so far (for reference)
        
        Returns:
            Structured notes dict with summary, key_points, action_items, etc.
        """
        
        # Format transcript for the prompt
        transcript_text = self._format_transcript(new_transcript)
        
        prompt = f"""You are a meeting note assistant. Analyze the following conversation transcript and generate structured meeting notes.

IMPORTANT CONTEXT:
- This is a CONTINUATION of an ongoing meeting
- Previous summary (use this to understand incomplete sentences or references): 
{previous_summary if previous_summary else "This is the start of the meeting."}

NEW TRANSCRIPT TO PROCESS:
{transcript_text}

Generate meeting notes in the following JSON format. Be concise but capture all important information:

{{
    "summary": "A 2-3 sentence summary of what was discussed in this segment, connecting to previous context if relevant",
    "key_points": [
        "Important point 1",
        "Important point 2"
    ],
    "action_items": [
        {{"task": "Description of action", "assignee": "Person name or null", "context": "Why this was decided"}}
    ],
    "decisions": [
        {{"decision": "What was decided", "rationale": "Why", "participants_involved": ["names"]}}
    ],
    "open_questions": [
        "Unresolved question or topic that needs follow-up"
    ],
    "opinion_changes": [
        {{"speaker": "name", "from": "previous stance", "to": "new stance", "topic": "what changed"}}
    ]
}}

Rules:
1. Only include sections that have actual content (empty arrays are fine)
2. If a sentence seems incomplete, use the previous summary context to infer meaning
3. Track when someone changes their opinion on a topic
4. Be specific about WHO said WHAT
5. Return ONLY valid JSON, no markdown or explanation

JSON:"""

        response_text = ""
        try:
            # Run Gemini in executor to not block async loop
            response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.model.generate_content(prompt)
            )
            
            # Parse JSON from response
            response_text = response.text.strip()
            
            # Handle potential markdown code blocks
            if response_text.startswith("```"):
                response_text = response_text.split("```")[1]
                if response_text.startswith("json"):
                    response_text = response_text[4:]
                response_text = response_text.strip()
            
            return json.loads(response_text)
            
        except json.JSONDecodeError as e:
            print(f"Failed to parse Gemini response as JSON: {e}")
            print(f"Response was: {response_text[:500] if response_text else 'empty'}")
            return {
                "summary": "Error processing transcript",
                "key_points": [],
                "action_items": [],
                "decisions": [],
                "open_questions": []
            }
        except Exception as e:
            print(f"Gemini API error: {e}")
            return {
                "summary": f"Error: {str(e)}",
                "key_points": [],
                "action_items": [],
                "decisions": [],
                "open_questions": []
            }
    
    def _format_transcript(self, transcript: list[dict]) -> str:
        """Format transcript entries for the prompt."""
        lines = []
        for entry in transcript:
            speaker = entry.get("speaker", "Unknown")
            text = entry.get("text", "")
            lines.append(f"[{speaker}]: {text}")
        return "\n".join(lines)


class MeetingNoteManager:
    """
    Manages meeting state with interval-based Gemini processing.
    
    Flow:
    1. Transcripts arrive continuously from Google Speech
    2. They're buffered in _transcript_buffer
    3. Every PROCESS_INTERVAL seconds, buffer is sent to Gemini
    4. Results update the meeting state
    5. State is broadcast to clients via callback
    """
    
    PROCESS_INTERVAL = 30  # seconds
    
    def __init__(
        self,
        meeting_id: str,
        on_state_update: Optional[Callable[[], None]] = None,
        initial_state: Optional[dict] = None
    ):
        # Load from initial state if provided (for continuing meetings)
        if initial_state:
            self.meeting = MeetingNote(
                id=meeting_id,
                title=initial_state.get("title", "Untitled Meeting"),
                created_at=initial_state.get("created_at", datetime.now().isoformat()),
                transcript=initial_state.get("transcript", []),
                summary=initial_state.get("summary", ""),
                key_points=initial_state.get("key_points", []),
                action_items=initial_state.get("action_items", []),
                decisions=initial_state.get("decisions", []),
                open_questions=initial_state.get("open_questions", []),
                participants=initial_state.get("participants", []),
                _previous_summary=initial_state.get("_previous_summary", ""),
            )
        else:
            self.meeting = MeetingNote(id=meeting_id)
        
        self._transcript_buffer: list[dict] = []
        self._on_state_update = on_state_update
        self._processor: Optional[GeminiProcessor] = None
        self._processing_task: Optional[asyncio.Task] = None
        self._is_running = False
        self._last_process_time: Optional[datetime] = None
        
        # Try to initialize Gemini processor
        try:
            self._processor = GeminiProcessor()
        except ValueError as e:
            print(f"Warning: Gemini not configured: {e}")
            print("Meeting notes will only show raw transcript.")
    
    def to_dict(self) -> dict:
        """Convert meeting state to JSON-serializable dict."""
        data = asdict(self.meeting)
        # Remove internal fields
        data.pop("_previous_summary", None)
        return data
    
    async def start(self):
        """Start the interval processing loop."""
        if self._is_running:
            return
        self._is_running = True
        self._processing_task = asyncio.create_task(self._processing_loop())
    
    async def stop(self):
        """Stop processing and do final processing of remaining buffer."""
        self._is_running = False
        if self._processing_task:
            self._processing_task.cancel()
            try:
                await self._processing_task
            except asyncio.CancelledError:
                pass
        
        # Final processing of any remaining transcript
        if self._transcript_buffer:
            await self._process_buffer()
    
    async def add_transcript(
        self,
        text: str,
        speaker: Optional[str],
        timestamp: str
    ):
        """
        Add a new transcript entry (called for each Google Speech final result).
        
        This is NON-BLOCKING - just adds to buffer.
        Processing happens in background loop.
        """
        entry = {
            "speaker": speaker or "Unknown",
            "text": text,
            "timestamp": timestamp
        }
        
        # Add to both full transcript and current buffer
        self.meeting.transcript.append(entry)
        self._transcript_buffer.append(entry)
        
        # Track participants
        if speaker and speaker not in self.meeting.participants:
            self.meeting.participants.append(speaker)
    
    async def _processing_loop(self):
        """Background loop that processes buffer every PROCESS_INTERVAL seconds."""
        while self._is_running:
            try:
                await asyncio.sleep(self.PROCESS_INTERVAL)
                
                if self._transcript_buffer:
                    await self._process_buffer()
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Processing loop error: {e}")
    
    async def _process_buffer(self):
        """Process the current transcript buffer with Gemini."""
        if not self._transcript_buffer:
            return
        
        # Skip if Gemini not configured
        if not self._processor:
            print(f"[{datetime.now().isoformat()}] Skipping Gemini processing (not configured)")
            self._transcript_buffer = []
            if self._on_state_update:
                self._on_state_update()
            return
        
        # Snapshot and clear buffer (so new transcripts can accumulate)
        batch = self._transcript_buffer.copy()
        self._transcript_buffer = []
        
        print(f"[{datetime.now().isoformat()}] Processing {len(batch)} transcript entries...")
        
        # Call Gemini with batch + previous summary for context
        result = await self._processor.process_transcript_batch(
            new_transcript=batch,
            previous_summary=self.meeting._previous_summary,
            full_transcript=self.meeting.transcript
        )
        
        # Update meeting state with Gemini's output
        self._merge_result(result)
        
        # Store current summary as context for next batch
        self.meeting._previous_summary = result.get("summary", "")
        
        self._last_process_time = datetime.now()
        
        # Notify clients of state change (sync callback)
        if self._on_state_update:
            self._on_state_update()
        
        print(f"[{datetime.now().isoformat()}] Processing complete. Summary: {result.get('summary', '')[:100]}...")
    
    def _merge_result(self, result: dict):
        """Merge Gemini's output into meeting state."""
        
        # Update summary (cumulative - append to build full meeting summary)
        new_summary = result.get("summary", "")
        if new_summary:
            if self.meeting.summary:
                self.meeting.summary += "\n\n" + new_summary
            else:
                self.meeting.summary = new_summary
        
        # Append key points (deduplicated)
        for point in result.get("key_points", []):
            if point and point not in self.meeting.key_points:
                self.meeting.key_points.append(point)
        
        # Append action items
        for item in result.get("action_items", []):
            if item:
                self.meeting.action_items.append(item)
        
        # Append decisions
        for decision in result.get("decisions", []):
            if decision:
                self.meeting.decisions.append(decision)
        
        # Update open questions (can be resolved, so replace)
        new_questions = result.get("open_questions", [])
        if new_questions:
            # Keep questions that weren't answered, add new ones
            self.meeting.open_questions = new_questions
        
        # Opinion changes are noted in decisions/key_points, no separate tracking needed


# For backward compatibility with server.py imports
__all__ = ["MeetingNote", "MeetingNoteManager", "GeminiProcessor"]
