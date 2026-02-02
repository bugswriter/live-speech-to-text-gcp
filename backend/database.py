"""
SQLite Database for Meeting Persistence

Handles:
- Creating and updating meetings
- Storing transcripts, notes, and AI-generated content
- Listing past meetings
- Loading meetings for continuation
"""

import sqlite3
import json
from datetime import datetime
from pathlib import Path
from typing import Optional
from contextlib import contextmanager
from dataclasses import asdict

# Database file location
DB_PATH = Path(__file__).parent / "meetings.db"


def get_connection() -> sqlite3.Connection:
    """Get a database connection with row factory."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_db():
    """Context manager for database connections."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Initialize the database schema."""
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meetings (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT 'Untitled Meeting',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                transcript TEXT DEFAULT '[]',
                summary TEXT DEFAULT '',
                key_points TEXT DEFAULT '[]',
                action_items TEXT DEFAULT '[]',
                decisions TEXT DEFAULT '[]',
                open_questions TEXT DEFAULT '[]',
                participants TEXT DEFAULT '[]',
                previous_summary TEXT DEFAULT '',
                is_active INTEGER DEFAULT 0
            )
        """)
        
        # Create index for listing meetings
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_meetings_updated 
            ON meetings(updated_at DESC)
        """)


def create_meeting(meeting_id: str, title: str = "Untitled Meeting") -> dict:
    """Create a new meeting."""
    now = datetime.now().isoformat()
    
    with get_db() as conn:
        conn.execute("""
            INSERT INTO meetings (id, title, created_at, updated_at)
            VALUES (?, ?, ?, ?)
        """, (meeting_id, title, now, now))
    
    return get_meeting(meeting_id)


def get_meeting(meeting_id: str) -> Optional[dict]:
    """Get a meeting by ID."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM meetings WHERE id = ?",
            (meeting_id,)
        ).fetchone()
    
    if not row:
        return None
    
    return _row_to_dict(row)


def update_meeting(meeting_id: str, data: dict) -> Optional[dict]:
    """Update a meeting with new data."""
    now = datetime.now().isoformat()
    
    # Serialize JSON fields
    updates = {
        "updated_at": now,
        "title": data.get("title"),
        "summary": data.get("summary", ""),
        "transcript": json.dumps(data.get("transcript", [])),
        "key_points": json.dumps(data.get("key_points", [])),
        "action_items": json.dumps(data.get("action_items", [])),
        "decisions": json.dumps(data.get("decisions", [])),
        "open_questions": json.dumps(data.get("open_questions", [])),
        "participants": json.dumps(data.get("participants", [])),
        "previous_summary": data.get("_previous_summary", ""),
    }
    
    # Build SET clause
    set_parts = []
    values = []
    for key, value in updates.items():
        if value is not None:
            set_parts.append(f"{key} = ?")
            values.append(value)
    
    if not set_parts:
        return get_meeting(meeting_id)
    
    values.append(meeting_id)
    
    with get_db() as conn:
        conn.execute(
            f"UPDATE meetings SET {', '.join(set_parts)} WHERE id = ?",
            values
        )
    
    return get_meeting(meeting_id)


def set_meeting_active(meeting_id: str, is_active: bool):
    """Set meeting active status."""
    with get_db() as conn:
        conn.execute(
            "UPDATE meetings SET is_active = ? WHERE id = ?",
            (1 if is_active else 0, meeting_id)
        )


def list_meetings(limit: int = 50, offset: int = 0) -> list[dict]:
    """List meetings ordered by last updated."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT id, title, created_at, updated_at, 
                   json_array_length(transcript) as transcript_count,
                   is_active
            FROM meetings
            ORDER BY updated_at DESC
            LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()
    
    return [dict(row) for row in rows]


def delete_meeting(meeting_id: str) -> bool:
    """Delete a meeting."""
    with get_db() as conn:
        cursor = conn.execute(
            "DELETE FROM meetings WHERE id = ?",
            (meeting_id,)
        )
    return cursor.rowcount > 0


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a database row to a meeting dict."""
    return {
        "id": row["id"],
        "title": row["title"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "transcript": json.loads(row["transcript"]),
        "summary": row["summary"],
        "key_points": json.loads(row["key_points"]),
        "action_items": json.loads(row["action_items"]),
        "decisions": json.loads(row["decisions"]),
        "open_questions": json.loads(row["open_questions"]),
        "participants": json.loads(row["participants"]),
        "_previous_summary": row["previous_summary"],
        "is_active": bool(row["is_active"]),
    }


# Initialize database on module import
init_db()
