"""Scoped topic summaries, independent of vector long-term retrieval."""
from __future__ import annotations
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

@contextmanager
def _db():
    root = os.environ.get('ASTRBOT_ROOT')
    path = (Path(root) / 'data/plugin_data/astrbot_plugin_livingmemory/whale_short.sqlite3') if root else (Path(__file__).resolve().parents[1] / 'data/whale_short.sqlite3')
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    try:
        with connection:
            connection.execute('CREATE TABLE IF NOT EXISTS topics(scope TEXT,persona TEXT,summary TEXT,updated REAL,PRIMARY KEY(scope,persona))')
            yield connection
    finally:
        connection.close()

def save_topic(scope, persona, summary):
    if not scope or not summary:
        return
    with _db() as connection:
        connection.execute('INSERT OR REPLACE INTO topics VALUES(?,?,?,?)', (scope, str(persona or ''), str(summary)[:1400], time.time()))

def read_topics(scopes, persona, max_chars=1000):
    with _db() as connection:
        connection.execute('DELETE FROM topics WHERE updated<?', (time.time()-7*86400,))
        rows=[]
        for scope in scopes:
            rows.extend(connection.execute('SELECT summary FROM topics WHERE scope=? AND persona=? AND updated>?', (scope, str(persona or ''), time.time()-7*86400)).fetchall())
    return '\n'.join(row[0] for row in rows)[:max_chars]

def clear_topics(scope, persona=None):
    with _db() as connection:
        if persona is None:
            connection.execute('DELETE FROM topics WHERE scope=?', (scope,))
        else:
            connection.execute('DELETE FROM topics WHERE scope=? AND persona=?', (scope, str(persona)))
