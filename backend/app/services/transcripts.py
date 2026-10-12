"""
Full conversation transcripts (a Growth feature) — shaping and search over what Retell actually sent us.

Stored shape (services/call_ingestion.py): {"text": str | None, "segments": list | None} where `segments`
is Retell's transcript_object: [{"role": "agent" | "user", "content": str, "words": [{"word", "start", "end"}]}].
Speaker labels come from `role`; a turn's timestamps come from its first and last word when Retell
provided them. Anything not provided is returned as null — nothing is estimated or invented.
"""
import re
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

SPEAKERS = {"agent": "Receptionist", "user": "Caller"}
MIN_QUERY_CHARS = 2
MAX_QUERY_CHARS = 100
SNIPPET_RADIUS = 60


def _seconds(value) -> float | None:
    return round(float(value), 2) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def shape_segments(stored: dict | None) -> list[dict]:
    segments = (stored or {}).get("segments")
    out: list[dict] = []
    if not isinstance(segments, list):
        return out
    for seg in segments:
        if not isinstance(seg, dict) or not isinstance(seg.get("content"), str) or not seg["content"].strip():
            continue
        words = seg.get("words") if isinstance(seg.get("words"), list) else []
        first = next((w for w in words if isinstance(w, dict) and _seconds(w.get("start")) is not None), None)
        last = next((w for w in reversed(words) if isinstance(w, dict) and _seconds(w.get("end")) is not None), None)
        role = seg.get("role")
        out.append(
            {
                "speaker": SPEAKERS.get(role, "Receptionist" if role == "agent" else "Caller"),
                "role": role if role in SPEAKERS else "user",
                "text": seg["content"].strip(),
                "start_seconds": _seconds(first.get("start")) if first else None,
                "end_seconds": _seconds(last.get("end")) if last else None,
            }
        )
    return out


def shape_transcript(stored: dict | None) -> dict:
    return {"segments": shape_segments(stored), "text": (stored or {}).get("text")}


def clean_query(q: str) -> str:
    return re.sub(r"\s+", " ", q).strip()[:MAX_QUERY_CHARS]


def _like_pattern(q: str) -> str:
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _snippet(content: str, needle: str) -> str:
    i = content.lower().find(needle.lower())
    if i < 0:
        return content[: SNIPPET_RADIUS * 2]
    start, end = max(0, i - SNIPPET_RADIUS), min(len(content), i + len(needle) + SNIPPET_RADIUS)
    return ("…" if start else "") + content[start:end] + ("…" if end < len(content) else "")


_SEARCH_SQL = text(
    """
    SELECT c.id, c.direction, c.caller_number, c.started_at, c.summary, t.transcript
    FROM calls c
    JOIN call_transcripts t ON t.call_id = c.id
    WHERE c.organization_id = :org
      AND jsonb_typeof(t.transcript->'segments') = 'array'
      AND EXISTS (
        SELECT 1 FROM jsonb_array_elements(t.transcript->'segments') s
        WHERE jsonb_typeof(s->'content') = 'string' AND s->>'content' ILIKE :pat ESCAPE '\\'
      )
    ORDER BY COALESCE(c.started_at, c.created_at) DESC
    LIMIT :lim
    """
)


def search_transcripts(db: Session, organization_id: uuid.UUID, query: str, limit: int = 20) -> list[dict]:
    """Calls (this organization only) whose stored conversation mentions `query`, newest first, with the matching lines."""
    q = clean_query(query)
    if len(q) < MIN_QUERY_CHARS:
        return []
    rows = db.execute(_SEARCH_SQL, {"org": organization_id, "pat": _like_pattern(q), "lim": limit}).all()
    results = []
    for call_id, direction, caller, started_at, summary, stored in rows:
        matches = [s for s in shape_segments(stored) if q.lower() in s["text"].lower()]
        results.append(
            {
                "call_id": str(call_id),
                "direction": direction.value if hasattr(direction, "value") else str(direction),
                "caller_number": caller,
                "started_at": started_at,
                "summary": summary,
                "matches": [
                    {"speaker": m["speaker"], "snippet": _snippet(m["text"], q), "start_seconds": m["start_seconds"]}
                    for m in matches[:3]
                ],
                "match_count": len(matches),
            }
        )
    return results
