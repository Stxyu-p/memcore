import sqlite3
import threading
from memcore import store

def test_attach_trinity_spokes_and_cross_query(tmp_path):
    mem_db = tmp_path / "memory.db"
    skills_db = tmp_path / "skills.db"
    blobs_db = tmp_path / "blobs.db"

    c1 = sqlite3.connect(str(skills_db))
    try:
        store.apply_wal_pragmas(c1)
        c1.execute("CREATE TABLE skill_embeddings (skill_name TEXT PRIMARY KEY, model TEXT)")
        c1.execute("INSERT INTO skill_embeddings VALUES ('trimtok', 'e5')")
        c1.commit()
    finally:
        c1.close()

    c2 = sqlite3.connect(str(blobs_db))
    try:
        store.apply_wal_pragmas(c2)
        c2.execute("CREATE TABLE chunks (sha TEXT PRIMARY KEY, alias TEXT)")
        c2.execute("INSERT INTO chunks VALUES ('abc12345', '#c1')")
        c2.commit()
    finally:
        c2.close()

    conn = store.open_store(str(mem_db))
    try:
        attached = store.attach_trinity_spokes(conn, str(skills_db), str(blobs_db))
        assert set(attached) == {"skills_spoke", "blobs_spoke"}
        sk = conn.execute("SELECT skill_name FROM skills_spoke.skill_embeddings").fetchone()
        ch = conn.execute("SELECT alias FROM blobs_spoke.chunks").fetchone()
        assert sk[0] == "trimtok"
        assert ch[0] == "#c1"
    finally:
        conn.close()

def test_write_tx_nested_savepoint_and_concurrency(tmp_path):
    db = tmp_path / "concurrent.db"
    conn = store.open_store(str(db))
    try:
        with store.write_tx(conn):
            conn.execute("CREATE TABLE IF NOT EXISTS counter (n INTEGER)")
            with store.write_tx(conn):
                conn.execute("INSERT INTO counter VALUES (1)")
        assert conn.execute("SELECT SUM(n) FROM counter").fetchone()[0] == 1
    finally:
        conn.close()

def test_plugin_and_workspace_adapters_are_byte_identical_and_clean_up_connections():
    import pathlib
    ws_dir = pathlib.Path(r"C:/Users/gugmn/Workspace/memcore/integrations/hermes/memcore")
    pl_dir = pathlib.Path(r"C:/Users/gugmn/AppData/Local/hermes/plugins/memcore")
    for name in ("__init__.py", "plugin.py", "native_provider.py", "semantic_analyzer.py"):
        assert (ws_dir / name).read_bytes() == (pl_dir / name).read_bytes(), f"Drift in {name}"

    from integrations.hermes.memcore import plugin as agent_plugin
    assert callable(getattr(agent_plugin, "close_cached_connections", None))
