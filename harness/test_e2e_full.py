"""
MemCore Full End-to-End (E2E) Verification Harness.

Exercises the entire lifecycle on a real disk-backed SQLite database:
1. Store initialization & WAL pragmas
2. Project & Agent membership provisioning
3. Governed Triples with canonical parent claim binding
4. 1-Bit Binary Vector Quantization & Two-Stage Recall
5. Multilingual Hybrid Recall (Thai + English Unicode matching)
6. Bitemporal Versioning & Supersede (cascade invalidation)
7. Cryptographic Tombstone Refusal Guards
8. Governed Compaction & Batch Synthesis
9. Git-Backed Project Snapshot Export & Idempotent Import into a clean Node
10. System Health & Integrity Doctor Verification
"""
import os
import sys
import json
import pathlib
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from memcore.store import open_store
from memcore.write import create_memory, supersede, reject_value, list_tombstones, TombstoneBlocked
from memcore.recall import search, recall, query_triples
from memcore.compaction import compact_project_memories
from memcore.export_project import export_project_snapshot, import_project_snapshot
from memcore.store_vectors import pack_binary_vector, hamming_distance


def run_e2e():
    print("=" * 70)
    print("           MEMCORE FULL END-TO-END (E2E) VERIFICATION")
    print("=" * 70)

    import shutil
    tmpdir = tempfile.mkdtemp()
    conn = None
    conn2 = None
    try:
        db_path = os.path.join(tmpdir, "primary_node.db")
        conn = open_store(db_path)
        print(f"[Phase 1] Initialized primary database at:\n          {db_path}")

        # 1. Provision Project and Agents
        conn.execute("INSERT INTO project(id, name) VALUES ('proj-prod', 'Production Fleet')")
        conn.execute("INSERT INTO agent(id, name, profile_key) VALUES ('mika', 'Mika', 'mika')")
        conn.execute("INSERT INTO agent(id, name, profile_key) VALUES ('choke', 'Choke', 'choke')")
        conn.execute("INSERT INTO project_membership(project_id, agent_id, role) VALUES ('proj-prod', 'mika', 'owner')")
        conn.execute("INSERT INTO project_membership(project_id, agent_id, role) VALUES ('proj-prod', 'choke', 'member')")
        conn.commit()
        print("[Phase 1] Provisioned 'proj-prod' with agents 'mika' (owner) & 'choke' (member).")

        # 2. Ingest Memories with Governed Triples
        print("\n[Phase 2] Ingesting Canonical Memories with Governed Triples...")
        m1_id, m1_v1 = create_memory(
            conn,
            project_id="proj-prod",
            agent_id="mika",
            content="Analytics backend microservice listens on port 9440 using TLS",
            scope="project",
            memory_type="fact",
            lifecycle="accepted",
            triples=[("analytics_backend", "port", "9440"), ("analytics_backend", "protocol", "TLS")]
        )
        print(f"  -> Created m1 (id: {m1_id}, v: {m1_v1}) with 2 triples.")

        m2_id, m2_v1 = create_memory(
            conn,
            project_id="proj-prod",
            agent_id="choke",
            content="ห้ามแตะต้องฐานข้อมูล production เด็ดขาดหากไม่ได้รับอนุมัติ",
            scope="project",
            memory_type="constraint",
            lifecycle="accepted",
            triples=[("production_db", "deletion", "forbidden")]
        )
        print(f"  -> Created m2 (id: {m2_id}, v: {m2_v1}) [type=constraint, Thai text].")

        # Query Triples
        triples_found = query_triples(conn, project_id="proj-prod", subject="analytics_backend", active_only=True)
        print(f"  -> query_triples(analytics_backend): returned {len(triples_found)} active triples.")
        assert len(triples_found) == 2, f"Expected 2 triples, found {len(triples_found)}"

        # 3. 1-Bit Binary Vector Quantization Verification
        print("\n[Phase 3] Verifying 1-Bit Binary Vector Quantization...")
        raw_float_vector = [1.0 if (i % 2 == 0) else -1.0 for i in range(384)]
        bin_vector = pack_binary_vector(raw_float_vector)
        print(f"  -> Packed 384-dimensional float32 vector into {len(bin_vector)} bytes (target: 48 bytes).")
        assert len(bin_vector) == 48, f"Expected 48 bytes, got {len(bin_vector)}"
        self_dist = hamming_distance(bin_vector, bin_vector)
        assert self_dist == 0, f"Expected self Hamming distance 0, got {self_dist}"

        inverted_float = [-v for v in raw_float_vector]
        bin_inverted = pack_binary_vector(inverted_float)
        inv_dist = hamming_distance(bin_vector, bin_inverted)
        print(f"  -> Self Hamming distance = {self_dist}, Inverted Hamming distance = {inv_dist}/384 bits.")
        assert inv_dist == 384, f"Expected 384 bit difference, got {inv_dist}"

        # 4. Multilingual Hybrid Recall
        print("\n[Phase 4] Testing Multilingual Hybrid Recall (English & Thai)...")
        en_hits = recall(conn, project_id="proj-prod", agent_id="mika", query="analytics port 9440", limit=5)
        print(f"  -> Recall English ('analytics port 9440'): found {len(en_hits)} candidates.")
        assert any(h[0] == m1_id for h in en_hits), "m1 not found in English recall"

        th_hits = recall(conn, project_id="proj-prod", agent_id="mika", query="ห้ามแตะต้องฐานข้อมูล", limit=5)
        print(f"  -> Recall Thai ('ห้ามแตะต้องฐานข้อมูล'): found {len(th_hits)} candidates.")
        assert any(h[0] == m2_id for h in th_hits), "m2 not found in Thai recall"

        # 5. Bitemporal Supersede & Cascade Invalidation
        print("\n[Phase 5] Testing Bitemporal Supersede & Invalidation...")
        m1_v2 = supersede(
            conn,
            memory_id=m1_id,
            agent_id="mika",
            new_content="Analytics backend microservice migrated to port 9443 using mTLS",
            reason="Port conflict with telemetry worker",
            triples=[("analytics_backend", "port", "9443"), ("analytics_backend", "protocol", "mTLS")]
        )
        print(f"  -> Superseded m1 -> new active version {m1_v2}")

        active_triples = query_triples(conn, project_id="proj-prod", subject="analytics_backend", active_only=True)
        active_ports = [t["object"] for t in active_triples if t["predicate"] == "port"]
        print(f"  -> Active port triples after supersede: {active_ports}")
        assert active_ports == ["9443"], f"Expected ['9443'], got {active_ports}"

        # 6. Tombstone Refusal Guard
        print("\n[Phase 6] Testing Cryptographic Tombstone Refusal Guard...")
        reject_value(
            conn,
            project_id="proj-prod",
            agent_id="mika",
            content="Deploy Kafka cluster on edge nodes",
            reason="Architecture decision: RabbitMQ only on edge nodes"
        )
        t_list = list_tombstones(conn, project_id="proj-prod", agent_id="mika")
        print(f"  -> Active project tombstones installed: {len(t_list)}")

        tombstone_triggered = False
        try:
            create_memory(
                conn,
                project_id="proj-prod",
                agent_id="choke",
                content="Deploy Kafka cluster on edge nodes",
                scope="project"
            )
        except TombstoneBlocked as exc:
            tombstone_triggered = True
            print(f"  -> VETO CONFIRMED: Agent prevented from remembering rejected claim.")

        assert tombstone_triggered, "Tombstone failed to block debunked claim"

        # 7. Governed Compaction
        print("\n[Phase 7] Testing Governed Bitemporal Compaction...")
        for i in range(3):
            create_memory(
                conn,
                project_id="proj-prod",
                agent_id="mika",
                content=f"Historical telemetry gateway metric configuration rule {chr(65+i)}",
                scope="project",
                memory_type="fact",
                lifecycle="accepted"
            )
        # Backdate the 3 memories to 45 days ago
        conn.execute("UPDATE memory SET created_at = datetime('now', '-45 days') WHERE id IN (SELECT memory_id FROM memory_version WHERE content LIKE 'Historical telemetry%')")
        conn.execute("UPDATE memory_version SET created_at = datetime('now', '-45 days') WHERE content LIKE 'Historical telemetry%'")
        conn.commit()

        comp_res = compact_project_memories(conn, project_id="proj-prod", agent_id="mika", older_than_days=14)
        print(f"  -> Compaction executed: {comp_res['compacted_count']} memories compacted into {comp_res['new_memory_id']}.")
        assert comp_res["compacted_count"] == 3

        # 8. Git-Backed Project Snapshot Sync
        print("\n[Phase 8] Testing Git-Backed Project Snapshot Export & Import...")
        snapshot_file = os.path.join(tmpdir, ".memcore.jsonl")
        exp = export_project_snapshot(conn, project_id="proj-prod", out_path=snapshot_file)
        print(f"  -> Exported snapshot to: {snapshot_file}")
        print(f"     Canonical Memories: {exp['memory_count']}, Tombstones: {exp['tombstone_count']}, Seal: {exp['seal_sha256'][:16]}...")

        # Initialize secondary node
        node2_db = os.path.join(tmpdir, "secondary_node.db")
        conn2 = open_store(node2_db)
        conn2.execute("INSERT INTO project(id, name) VALUES ('proj-prod', 'Production Fleet')")
        conn2.execute("INSERT INTO agent(id, name, profile_key) VALUES ('node2_agent', 'Node 2 Agent', 'node2_agent')")
        conn2.execute("INSERT INTO project_membership(project_id, agent_id, role) VALUES ('proj-prod', 'node2_agent', 'owner')")
        conn2.commit()

        imp1 = import_project_snapshot(conn2, project_id="proj-prod", agent_id="node2_agent", in_path=snapshot_file)
        print(f"  -> Node 2 Import: {imp1['imported_memories']} memories, {imp1['imported_tombstones']} tombstones.")
        assert imp1["imported_memories"] == exp["memory_count"]
        assert imp1["imported_tombstones"] == exp["tombstone_count"]

        # Re-import test (Idempotency)
        imp2 = import_project_snapshot(conn2, project_id="proj-prod", agent_id="node2_agent", in_path=snapshot_file)
        print(f"  -> Node 2 Re-Import (Idempotency check): {imp2['imported_memories']} new, {imp2['skipped_existing']} skipped.")
        assert imp2["imported_memories"] == 0
        assert imp2["skipped_existing"] == exp["memory_count"]

        # 9. Verify Foreign Key and Integrity Checks on both databases
        print("\n[Phase 9] Verifying Store Health & Invariants...")
        for name, c in [("Primary", conn), ("Secondary", conn2)]:
            fk_errs = c.execute("PRAGMA foreign_key_check").fetchall()
            integrity = c.execute("PRAGMA integrity_check").fetchall()
            print(f"  -> {name} DB: FK violations = {len(fk_errs)}, Integrity check = {integrity[0][0]}")
            assert len(fk_errs) == 0, f"{name} had FK violations: {fk_errs}"
            assert integrity[0][0] == "ok", f"{name} integrity failed: {integrity}"

        print("\n" + "=" * 70)
        print("      ALL MEMCORE E2E PHASES PASSED WITH ZERO VIOLATIONS!      ")
        print("=" * 70)
    finally:
        if conn:
            conn.close()
        if conn2:
            conn2.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    run_e2e()
