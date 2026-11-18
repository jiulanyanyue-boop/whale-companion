from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from contextlib import contextmanager


def _tokens(text: str) -> set[str]:
    """ASCII words plus overlapping Chinese bigrams, so substrings remain searchable."""
    parts = re.findall(r"[a-zA-Z0-9_]+|[\u3400-\u9fff]+", text.lower())
    tokens: set[str] = set()
    for part in parts:
        if re.fullmatch(r"[\u3400-\u9fff]+", part):
            if len(part) == 1:
                tokens.add(part)
            else:
                tokens.update(part[i:i + 2] for i in range(len(part) - 1))
        elif len(part) > 1:
            tokens.add(part)
    return tokens


class MemeLibrary:
    """Hash-addressed local images with keyword retrieval and injectable vector ranking."""
    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db, timeout=5)
        try:
            with connection:
                yield connection
        finally:
            connection.close()
    def __init__(self, root: str | Path, vector_search: Any = None, *, daily_generation_limit: int = 5, cooldown_seconds: int = 90):
        self.root = Path(root)
        self.images = self.root / "images"
        self.images.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "index.sqlite3"
        self.vector_search = vector_search
        self.daily_generation_limit = max(0, min(int(daily_generation_limit), 100))
        self.cooldown_seconds = max(1, int(cooldown_seconds))
        self._generation_lock = threading.Lock()
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS memes (
                digest TEXT PRIMARY KEY, path TEXT NOT NULL, description TEXT NOT NULL,
                image_text TEXT NOT NULL, meaning TEXT NOT NULL, emotion TEXT NOT NULL,
                scene TEXT NOT NULL, source TEXT NOT NULL, tags TEXT NOT NULL,
                created REAL NOT NULL, last_used REAL NOT NULL DEFAULT 0,
                role TEXT NOT NULL DEFAULT 'searchable')""")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(memes)")}
            if "role" not in columns:
                conn.execute("ALTER TABLE memes ADD COLUMN role TEXT NOT NULL DEFAULT 'searchable'")
            conn.execute("CREATE TABLE IF NOT EXISTS generation_log (created REAL NOT NULL, prompt_hash TEXT NOT NULL)")

    def add(self, image: str | Path | bytes, *, description: str, image_text: str = "", meaning: str = "",
            emotion: str = "", scene: str = "", source: str = "generated", tags: list[str] | None = None,
            role: str = "searchable") -> str:
        if isinstance(image, bytes):
            content, suffix = image, ".img"
        else:
            source_path = Path(image)
            content, suffix = source_path.read_bytes(), source_path.suffix.lower() or ".img"
        # A real suffix is needed by QQ clients when sending a cached image.
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            suffix = ".png"
        elif content.startswith(b"\xff\xd8\xff"):
            suffix = ".jpg"
        elif content.startswith((b"GIF87a", b"GIF89a")):
            suffix = ".gif"
        elif content[:4] == b"RIFF" and content[8:12] == b"WEBP":
            suffix = ".webp"
        digest = hashlib.sha256(content).hexdigest()
        try:
            source_path = Path(image).resolve() if not isinstance(image, bytes) else None
            in_library = source_path is not None and source_path.parent == self.images.resolve()
        except OSError:
            in_library = False
        target = source_path if in_library else self.images / f"{digest}{suffix}"
        if not target.exists():
            target.write_bytes(content)
        with self._connect() as conn:
            conn.execute("""INSERT OR IGNORE INTO memes
                          (digest,path,description,image_text,meaning,emotion,scene,source,tags,created,last_used,role)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
                         (digest, target.name, description, image_text, meaning, emotion, scene, source,
                          json.dumps(tags or [], ensure_ascii=False), time.time(), role if role in {"searchable", "reference_only"} else "searchable"))
        return digest

    def get(self, digest: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM memes WHERE digest=?", (digest,)).fetchone()
        return dict(row) | {"path": str(self.images / row["path"])} if row else None

    def catalog(self, digest: str, *, description: str, image_text: str = "", meaning: str = "", emotion: str = "", scene: str = "", role: str | None = None) -> None:
        if not description.strip() or not meaning.strip():
            raise ValueError("visual description and meaning required")
        with self._connect() as conn:
            if role is None:
                conn.execute("UPDATE memes SET description=?, image_text=?, meaning=?, emotion=?, scene=?, tags=? WHERE digest=?",
                             (description[:1200], image_text[:1200], meaning[:500], emotion[:200], scene[:500], "[]", digest))
            else:
                conn.execute("UPDATE memes SET description=?, image_text=?, meaning=?, emotion=?, scene=?, tags=?, role=? WHERE digest=?",
                             (description[:1200], image_text[:1200], meaning[:500], emotion[:200], scene[:500], "[]", role, digest))

    def search(self, query: str, *, limit: int = 5, minimum_score: float = 0.08, include_reference: bool = False) -> list[dict[str, Any]]:
        words = _tokens(query)
        if not words:
            return []
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM memes").fetchall()
            scored = []
            for row in rows:
                if (not include_reference and row["role"] == "reference_only") or "pending_visual_review" in json.loads(row["tags"]) or not row["description"]:
                    continue
                text = " ".join((row["description"], row["image_text"], row["meaning"], row["emotion"], row["scene"], row["tags"]))
                overlap = len(words & _tokens(text))
                score = overlap / max(1, len(words))
                item = dict(row)
                scored.append((score, item))
            if self.vector_search:
                try:
                    vector = self.vector_search(query, [item for _, item in scored])
                    if vector:
                        ranks = {x["digest"]: x.get("score", 0) for x in vector}
                        scored = [(max(score, ranks.get(item["digest"], 0)), item) for score, item in scored]
                except Exception:
                    pass
            return [item | {"score": score, "path": str(self.images / item["path"])}
                    for score, item in sorted(scored, key=lambda pair: pair[0], reverse=True)
                    if score >= minimum_score][:max(1, min(limit, 10))]

    def mark_used(self, digest: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE memes SET last_used=? WHERE digest=?", (time.time(), digest))

    def import_shared(self, image: str | Path, **metadata: Any) -> str:
        """Explicit administrative/import action; incoming private chat media is never auto-added."""
        return self.add(image, **metadata)

    def generation_allowed(self, prompt: str, *, now: float | None = None) -> tuple[bool, str]:
        current = time.time() if now is None else now
        utc_offset = -time.altzone if time.localtime(current).tm_isdst > 0 else -time.timezone
        day_start = ((current + utc_offset) // 86400) * 86400 - utc_offset
        normalized = " ".join(sorted(_tokens(prompt)))
        prompt_hash = hashlib.sha256(normalized.encode()).hexdigest()
        with self._generation_lock, self._connect() as conn:
            conn.execute("DELETE FROM generation_log WHERE created < ?", (day_start - 7 * 86400,))
            recent = conn.execute("SELECT created FROM generation_log WHERE prompt_hash=? ORDER BY created DESC LIMIT 1", (prompt_hash,)).fetchone()
            if recent and current - recent[0] < self.cooldown_seconds:
                return False, "duplicate_cooldown"
            count = conn.execute("SELECT COUNT(*) FROM generation_log WHERE created >= ?", (day_start,)).fetchone()[0]
            if count >= self.daily_generation_limit:
                return False, "daily_generation_limit"
            conn.execute("INSERT INTO generation_log(created,prompt_hash) VALUES (?,?)", (current, prompt_hash))
            return True, "reserved"
