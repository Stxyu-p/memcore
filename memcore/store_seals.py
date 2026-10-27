"""MemCore storage — provenance seal chain (HMAC over corroboration events)."""
import hashlib
import hmac
import os




def corroboration_funnel(conn, accept_n: int = 3, golden_n: int = 5) -> dict:
    """Content-free corroboration funnel for one store.

    Reports how live (candidate/accepted/conflict) claim fingerprints are
    distributed by distinct writer count: at_1, at_2 .., at_accept,
    at_golden. Never reads memory text — only fingerprint group sizes and
    owner-agent counts.

    This is the number that tells whether ADR-0018's Golden Rule can ever
    fire. On 2026-10-03 the live store held 73 fingerprints, ALL at sources=1,
    which is why `corroborate` had never promoted anything. The mechanism was
    correct; it had no reachable inputs.
    """
    rows = conn.execute(
        "SELECT fp, sources FROM ("
        "SELECT claim_fingerprint AS fp, COUNT(DISTINCT owner_agent_id) AS sources "
        "FROM memory WHERE claim_fingerprint IS NOT NULL "
        "AND lifecycle IN ('candidate','accepted','conflict') "
        "GROUP BY claim_fingerprint)"
    ).fetchall()
    at_1 = sum(1 for _, s in rows if s == 1)
    at_2 = sum(1 for _, s in rows if s == 2)
    at_accept = sum(1 for _, s in rows if s >= accept_n)
    at_golden = sum(1 for _, s in rows if s >= golden_n)
    return {
        'fingerprints': len(rows),
        'at_1': at_1,
        'at_2': at_2,
        'at_accept': at_accept,
        'at_golden': at_golden,
        'accept_n': accept_n,
        'golden_n': golden_n,
        'reachable': at_accept > 0,
    }



def _active_provenance_key(conn) -> tuple:
    """Return (key_id, key_material bytes), creating the key on first use.

    The key is store-local and random (os.urandom 32 bytes, hex-encoded). It
    is not a high-grade secret — it lives next to the data — but it makes
    post-write attribution tampering detectable, which is the threat that
    matters here (a writer claiming another agent's identity after the fact).
    """
    row = conn.execute(
        'SELECT key_id, key_material FROM provenance_key '
        "ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if row:
        return row[0], bytes.fromhex(row[1])
    key_id = 'pk-' + os.urandom(8).hex()
    key_material = os.urandom(32).hex()
    conn.execute(
        'INSERT INTO provenance_key (key_id, key_material) VALUES (?, ?)',
        (key_id, key_material),
    )
    return key_id, bytes.fromhex(key_material)



def seal_event(project_id: str, agent_id: str, content_hash: str,
               key_material: bytes) -> str:
    """HMAC-SHA256 seal over the attribution triple. Stdlib only."""
    msg = f'{project_id}\0{agent_id}\0{content_hash}'.encode('utf-8')
    return hmac.new(key_material, msg, hashlib.sha256).hexdigest()



def verify_event_seal(conn, event_id: str) -> dict:
    """Check one event's seal. Returns {sealed, valid, key_id}.

    Unsealed (pre-migration) rows report sealed=False, valid=None — they are
    historical, not failures.
    """
    row = conn.execute(
        'SELECT project_id, agent_id, content_hash, provenance_seal '
        'FROM ingest_event WHERE id=?', (event_id,),
    ).fetchone()
    if row is None:
        return {'sealed': False, 'valid': False, 'key_id': None,
                'reason': 'event_not_found'}
    project_id, agent_id, content_hash, seal = row
    if not seal:
        return {'sealed': False, 'valid': None, 'key_id': None,
                'reason': 'pre_migration_row'}
    key_row = conn.execute(
        'SELECT key_id, key_material FROM provenance_key '
        'ORDER BY created_at DESC LIMIT 1'
    ).fetchone()
    if key_row is None:
        return {'sealed': True, 'valid': False, 'key_id': None,
                'reason': 'no_key'}
    key_id, key_material = key_row[0], bytes.fromhex(key_row[1])
    expected = seal_event(project_id, agent_id, content_hash, key_material)
    valid = hmac.compare_digest(expected, seal)
    return {'sealed': True, 'valid': valid, 'key_id': key_id,
            'reason': 'ok' if valid else 'mismatch'}



def verify_all_seals(conn, limit: int = 10000) -> dict:
    """Content-free seal census: {sealed, valid, invalid, unsealed}.

    Never reads event text — only IDs and seal columns.
    """
    rows = conn.execute(
        'SELECT id FROM ingest_event ORDER BY created_at DESC LIMIT ?',
        (limit,),
    ).fetchall()
    report = {'checked': 0, 'sealed': 0, 'valid': 0, 'invalid': 0,
              'unsealed': 0, 'invalid_ids': []}
    for (event_id,) in rows:
        result = verify_event_seal(conn, event_id)
        report['checked'] += 1
        if not result['sealed']:
            report['unsealed'] += 1
        elif result['valid']:
            report['sealed'] += 1
            report['valid'] += 1
        else:
            report['sealed'] += 1
            report['invalid'] += 1
            if len(report['invalid_ids']) < 10:
                report['invalid_ids'].append(event_id)
    return report
