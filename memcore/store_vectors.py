"""MemCore storage — dense vector persistence (packed float32 and 1-bit binary blobs)."""
import sqlite3
import struct

from . import embedding




#: Pass-through aliases kept on the public store surface (SDK and tests import
#: ``store.pack_vector`` / ``store.unpack_vector``); the implementation lives in
#: the embedding seam.
pack_vector = embedding.pack_vector

unpack_vector = embedding.unpack_vector


def pack_binary_vector(floats: list[float]) -> bytes:
    """Quantize float vector into 1-bit sign representation.

    Each float > 0.0 becomes bit 1, <= 0.0 becomes bit 0.
    Packs 8 dimensions per byte, little-endian bit order.
    384 dimensions -> 48 bytes.
    """
    if not floats:
        return b''
    byte_vals = bytearray((len(floats) + 7) // 8)
    for i, val in enumerate(floats):
        if val > 0.0:
            byte_vals[i // 8] |= (1 << (i % 8))
    return bytes(byte_vals)


def hamming_distance(b1: bytes, b2: bytes) -> int:
    """Compute Hamming distance (count of differing bits) between two binary vectors.

    Fastest path using 64-bit integer bit_count (POPCNT).
    """
    if not b1 or not b2 or len(b1) != len(b2):
        return 999999
    dist = 0
    full_words = len(b1) // 8
    for i in range(full_words):
        offset = i * 8
        w1 = struct.unpack_from('<Q', b1, offset)[0]
        w2 = struct.unpack_from('<Q', b2, offset)[0]
        dist += (w1 ^ w2).bit_count()
    for i in range(full_words * 8, len(b1)):
        dist += (b1[i] ^ b2[i]).bit_count()
    return dist


def store_embedding(conn: sqlite3.Connection, memory_id: str, version_id: str,
                    model: str, vector: list[float]) -> None:
    """Persist an embedding vector and its 1-bit binary representation."""
    blob = pack_vector(vector)
    bin_blob = pack_binary_vector(vector)
    dim = len(vector)
    try:
        conn.execute(
            'INSERT OR REPLACE INTO memory_embedding '
            '(memory_id, version_id, model, dim, vector, binary_vector) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (memory_id, version_id, model, dim, blob, bin_blob)
        )
    except sqlite3.OperationalError:
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
