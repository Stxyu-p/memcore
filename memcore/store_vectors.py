"""MemCore storage — dense vector persistence (packed float32 blobs)."""
import sqlite3

from . import embedding




#: Pass-through aliases kept on the public store surface (SDK and tests import
#: ``store.pack_vector`` / ``store.unpack_vector``); the implementation lives in
#: the embedding seam.
pack_vector = embedding.pack_vector

unpack_vector = embedding.unpack_vector



def store_embedding(conn: sqlite3.Connection, memory_id: str, version_id: str,
                    model: str, vector: list[float]) -> None:
    """Persist an embedding vector for a specific memory version."""
    blob = pack_vector(vector)
    dim = len(vector)
    conn.execute(
        'INSERT OR REPLACE INTO memory_embedding '
        '(memory_id, version_id, model, dim, vector) '
        'VALUES (?, ?, ?, ?, ?)',
        (memory_id, version_id, model, dim, blob)
    )



def get_embedding(conn: sqlite3.Connection, memory_id: str,
                  version_id: str) -> list[float] | None:
    """Retrieve and unpack the embedding vector for a memory version, or None if absent."""
    cur = conn.execute(
        'SELECT vector FROM memory_embedding WHERE memory_id=? AND version_id=?',
        (memory_id, version_id)
    )
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    return unpack_vector(row[0])
