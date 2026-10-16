"""Stabilization hardening regression tests (H1-H9 + C1/C3).

Every test isolates itself in a TemporaryDirectory store that is never the
live ~/.memcore/memory.db. Network is never touched: embedding calls are
mocked, and setup-only writes run with get_embedding patched to None.

Covered:
- H1  out-of-lock embedding (create/supersede/batch lanes)
- H2  contradiction pair guards, CLI clean errors, cmd_embed filters
- H3  read-only survival on lazy fingerprint repair
- H4  bounded per-connection schema cache + migration invalidation
- H6/R1 append_event scrub-before-hash identity
- H7  thread-safe circuit breaker + fastembed cache
- H8  atomic export/restore writes with tmp cleanup
- C1  dry-run/apply parity (decay + journal sweep), both API and CLI
- C3  demotion promotes a compatible hit from the overfetch window
"""
import io
import os
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from memcore import core  # noqa: E402
from memcore import embedding  # noqa: E402
from memcore import export as export_mod  # noqa: E402
from memcore import ingest, store  # noqa: E402
from memcore import __main__ as cli  # noqa: E402


def _make_store(tmpdir, name='hardening.db', project='p', agent='a'):
    db_path = os.path.join(tmpdir, name)
    conn = store.open_store(db_path)
    conn.execute('INSERT INTO project (id, name) VALUES (?, ?)', (project, project))
    conn.execute(
        'INSERT INTO agent (id, name, profile_key) VALUES (?, ?, ?)',
        (agent, agent, agent))
    conn.execute(
        "INSERT INTO project_membership (project_id, agent_id, role) "
        "VALUES (?, ?, 'owner')", (project, agent))
    conn.commit()
    return conn, db_path


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='memcore_hardening_')
        self.conn, self.db = _make_store(self.tmp.name)

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def _create_quiet(self, content, **kwargs):
        with mock.patch('memcore.embedding.get_embedding', return_value=None):
            return core.create_memory(self.conn, 'p', 'a', content, **kwargs)


# ── H1: out-of-lock embedding ────────────────────────────────────────────────

class TestH1OutOfLockEmbedding(_StoreCase):

    def _slow_embed_side_effect(self):
        started = threading.Event()
        release = threading.Event()

        def slow(content, **kwargs):
            started.set()
            if not release.wait(timeout=5):
                raise RuntimeError('test embed release timeout')
            return [0.25, 0.5, 0.75]

        return started, release, slow

    def _assert_other_writer_not_blocked(self, db_path):
        """BEGIN IMMEDIATE + UPDATE must succeed while the embed mock is held."""
        other = sqlite3.connect(db_path, timeout=5)
        try:
            other.execute('PRAGMA busy_timeout = 300')
            other.execute('BEGIN IMMEDIATE')
            other.execute("UPDATE project SET name = name WHERE id = 'p'")
            other.execute('COMMIT')
        finally:
            other.close()

    def test_write_lock_not_held_during_slow_embedding_io(self):
        started, release, slow = self._slow_embed_side_effect()
        result = {}

        def writer():
            conn2 = store.open_store(self.db)
            try:
                result['ids'] = core.create_memory(
                    conn2, 'p', 'a', 'create lane with slow embedding')
            except Exception as exc:  # pragma: no cover - failure path
                result['error'] = exc
            finally:
                conn2.close()

        with mock.patch('memcore.embedding.get_embedding', side_effect=slow):
            thread = threading.Thread(target=writer, daemon=True)
            thread.start()
            try:
                self.assertTrue(
                    started.wait(timeout=5), 'embedding fetch never started')
                # While the embedding call is still in flight the write lock
                # must be free: another writer takes it immediately.
                self._assert_other_writer_not_blocked(self.db)
            finally:
                release.set()
                thread.join(timeout=10)
        self.assertNotIn('error', result, result.get('error'))
        mem_id, _ver_id = result['ids']
        row = self.conn.execute(
            'SELECT COUNT(*) FROM memory WHERE id=?', (mem_id,)).fetchone()
        self.assertEqual(row[0], 1)

    def test_write_lock_not_held_during_slow_embedding_io_supersede(self):
        mem_id, _ = self._create_quiet('supersede lane initial claim')
        started, release, slow = self._slow_embed_side_effect()
        result = {}

        def writer():
            conn2 = store.open_store(self.db)
            try:
                result['ver'] = core.supersede(
                    conn2, mem_id, 'a', 'supersede lane replacement claim')
            except Exception as exc:  # pragma: no cover - failure path
                result['error'] = exc
            finally:
                conn2.close()

        with mock.patch('memcore.embedding.get_embedding', side_effect=slow):
            thread = threading.Thread(target=writer, daemon=True)
            thread.start()
            try:
                self.assertTrue(started.wait(timeout=5))
                self._assert_other_writer_not_blocked(self.db)
            finally:
                release.set()
                thread.join(timeout=10)
        self.assertNotIn('error', result, result.get('error'))
        self.assertTrue(str(result['ver']).startswith('ver-'))

    def test_create_memory_manage_false_skips_embedding_http(self):
        calls = []

        def fake(content, **kwargs):
            calls.append(content)
            return [0.1, 0.2]

        self.conn.execute('BEGIN IMMEDIATE')
        try:
            with mock.patch('memcore.embedding.get_embedding', side_effect=fake):
                mem_id, ver_id = core.create_memory(
                    self.conn, 'p', 'a', 'batched claim without embedding',
                    _manage_transaction=False)
                self.assertEqual(calls, [])
            self.conn.execute('COMMIT')
        except Exception:
            self.conn.execute('ROLLBACK')
            raise
        row = self.conn.execute(
            'SELECT COUNT(*) FROM memory_embedding WHERE memory_id=?',
            (mem_id,)).fetchone()
        self.assertEqual(row[0], 0)
        self.assertTrue(ver_id.startswith('ver-'))

    def test_import_memories_never_fires_embedding_http(self):
        calls = []

        def fake(content, **kwargs):
            calls.append(content)
            return [0.5]

        items = [{'summary': f'import hardening claim {i}'} for i in range(5)]
        with mock.patch('memcore.embedding.get_embedding', side_effect=fake):
            result = core.import_memories(self.conn, items, 'p', 'a')
        self.assertEqual(calls, [])
        self.assertEqual(result['added'], 5)

    def test_create_memory_still_stores_embedding_when_available(self):
        with mock.patch(
                'memcore.embedding.get_embedding', return_value=[0.5, 0.25]):
            mem_id, ver_id = core.create_memory(
                self.conn, 'p', 'a', 'embedded claim content')
        row = self.conn.execute(
            'SELECT vector, model, dim FROM memory_embedding WHERE version_id=?',
            (ver_id,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(store.unpack_vector(row[0]), [0.5, 0.25])
        self.assertEqual(row[2], 2)

    def test_supersede_stores_precomputed_embedding(self):
        mem_id, _ = self._create_quiet('supersede embed original claim')
        with mock.patch(
                'memcore.embedding.get_embedding', return_value=[0.75, 0.5]):
            new_ver = core.supersede(
                self.conn, mem_id, 'a', 'supersede embed replacement claim')
        row = self.conn.execute(
            'SELECT vector FROM memory_embedding WHERE version_id=?',
            (new_ver,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(store.unpack_vector(row[0]), [0.75, 0.5])


# ── H2: contradiction guards + CLI clean errors ─────────────────────────────

class TestH2ContradictionGuards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='memcore_h2_')
        self.conn, self.db = _make_store(self.tmp.name, name='guard.db')
        # a second project the same agent owns, for the cross-project case
        self.conn.execute("INSERT INTO project (id, name) VALUES ('p2', 'p2')")
        self.conn.execute(
            "INSERT INTO project_membership (project_id, agent_id, role) "
            "VALUES ('p2', 'a', 'owner')")
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def _create(self, content, project='p'):
        with mock.patch('memcore.embedding.get_embedding', return_value=None):
            return core.create_memory(self.conn, project, 'a', content)[0]

    def test_mark_contradiction_rejects_cross_project_and_self_pair(self):
        a = self._create('contradiction guard claim alpha')
        b = self._create('contradiction guard claim bravo')
        c = self._create('contradiction guard claim charlie', project='p2')
        with self.assertRaises(core.MemCoreError):
            core.mark_contradiction(self.conn, a, a, 'a', reason='self pair')
        with self.assertRaises(core.MemCoreError):
            core.mark_contradiction(self.conn, b, c, 'a', reason='cross pair')

    def test_mark_contradiction_no_partial_writes_on_reject(self):
        a = self._create('partial write guard claim alpha')
        b = self._create('partial write guard claim bravo')
        c = self._create('partial write guard claim charlie', project='p2')
        with self.assertRaises(core.MemCoreError):
            core.mark_contradiction(self.conn, b, c, 'a', reason='cross pair')
        with self.assertRaises(core.MemCoreError):
            core.mark_contradiction(self.conn, a, a, 'a', reason='self pair')
        lifecycles = dict(self.conn.execute(
            'SELECT id, lifecycle FROM memory WHERE id IN (?, ?, ?)', (a, b, c)))
        self.assertEqual(lifecycles[a], 'candidate')
        self.assertEqual(lifecycles[b], 'candidate')
        self.assertEqual(lifecycles[c], 'candidate')
        audits = self.conn.execute(
            "SELECT COUNT(*) FROM audit_event WHERE action='mark_conflict'"
        ).fetchone()[0]
        self.assertEqual(audits, 0)

    def test_cli_mark_conflict_clean_error_on_invalid_pair(self):
        a = self._create('cli conflict guard claim alpha')
        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            with self.assertRaises(SystemExit) as caught:
                cli.main(['--db', self.db, 'mark-conflict', '--project', 'p',
                          '--agent', 'a', '--memory-a', a, '--memory-b', a,
                          '--reason', 'self pair', '--confirm'])
        code = caught.exception.code
        self.assertIsInstance(code, str)
        self.assertTrue(code.startswith('error:'), code)
        self.assertNotIn('Traceback', out.getvalue())


# ── H3: read-only survival ──────────────────────────────────────────────────

class TestH3ReadOnlySurvival(_StoreCase):

    def test_readonly_connection_survives_null_fingerprint_repair_paths(self):
        with mock.patch('memcore.embedding.get_embedding', return_value=None):
            mem_id, _ = core.create_memory(
                self.conn, 'p', 'a', 'legacy null fingerprint claim',
                scope='private')
        self.conn.execute(
            'UPDATE memory SET claim_fingerprint=NULL WHERE id=?', (mem_id,))
        self.conn.commit()
        ro = store.open_runtime_store_readonly(self.db)
        try:
            version_id, fp = core._current_claim_identity(ro, mem_id)
            self.assertTrue(version_id)
            self.assertEqual(fp, core.fingerprint('legacy null fingerprint claim'))
            matches = ingest._private_claim_ids(
                ro, 'p', 'a', core.fingerprint('legacy null fingerprint claim'))
            self.assertEqual(matches, [mem_id])
        finally:
            ro.close()
        # the read-only handle must not have repaired (or hidden) anything
        stored = self.conn.execute(
            'SELECT claim_fingerprint FROM memory WHERE id=?', (mem_id,)).fetchone()[0]
        self.assertIsNone(stored)


# ── H4: bounded schema cache ────────────────────────────────────────────────

class TestH4SchemaCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='memcore_h4_')
        core._CONN_TABLE_COLS.clear()

    def tearDown(self):
        core._CONN_TABLE_COLS.clear()
        self.tmp.cleanup()

    def test_table_cols_cache_bounded_and_no_stale_pre_migration_leak(self):
        # (a) a migrated store never loses its columns after id() churn
        migrated = os.path.join(self.tmp.name, 'migrated.db')
        store.open_store(migrated).close()
        for _ in range(140):
            c = sqlite3.connect(migrated)
            try:
                cols = core._table_cols(c, 'memory')
                self.assertIn('lifecycle', cols)
            finally:
                c.close()
        self.assertLessEqual(len(core._CONN_TABLE_COLS), 128)
        self.assertGreater(len(core._CONN_TABLE_COLS), 1)

        # (b) a pre-migration empty result is never cached, so the same
        # connection sees the real columns after migrations run
        fresh = os.path.join(self.tmp.name, 'fresh.db')
        conn = sqlite3.connect(fresh)
        try:
            # Same bootstrap order as open_store: WAL before migrations.
            conn.execute('PRAGMA busy_timeout = 10000')
            conn.execute('PRAGMA journal_mode = WAL')
            self.assertEqual(core._table_cols(conn, 'memory'), set())
            self.assertNotIn((id(conn), 'memory'), core._CONN_TABLE_COLS)
            store.apply_migrations(conn)
            cols = core._table_cols(conn, 'memory')
            self.assertIn('lifecycle', cols)
            self.assertIn('claim_fingerprint', cols)
        finally:
            conn.close()

    def test_table_cols_migration_hook_invalidates_cache(self):
        migrated = os.path.join(self.tmp.name, 'hook.db')
        conn = store.open_store(migrated)
        try:
            self.assertIn('lifecycle', core._table_cols(conn, 'memory'))
            self.assertTrue(core._CONN_TABLE_COLS)
            store.apply_migrations(conn)
            self.assertEqual(core._CONN_TABLE_COLS, {})
        finally:
            conn.close()


# ── H2b: cmd_embed filter ───────────────────────────────────────────────────

class TestH2EmbedFilters(_StoreCase):

    def test_cmd_embed_skips_rejected_disabled_and_superseded_memories(self):
        ids = {}
        with mock.patch('memcore.embedding.get_embedding', return_value=None):
            ids['cand'], _ = core.create_memory(
                self.conn, 'p', 'a', 'embed filter candidate claim')
            ids['acc'], _ = core.create_memory(
                self.conn, 'p', 'a', 'embed filter accepted claim',
                lifecycle='accepted')
            ids['conf'], _ = core.create_memory(
                self.conn, 'p', 'a', 'embed filter conflict claim')
            ids['rej'], _ = core.create_memory(
                self.conn, 'p', 'a', 'embed filter rejected claim')
            ids['dis'], _ = core.create_memory(
                self.conn, 'p', 'a', 'embed filter disabled claim')
            ids['sup'], _ = core.create_memory(
                self.conn, 'p', 'a', 'embed filter superseded claim')
        core.reject(self.conn, ids['rej'], 'a', reason='not wanted')
        core.deactivate(self.conn, ids['dis'], 'a', reason='turned off')
        self.conn.execute(
            "UPDATE memory SET lifecycle='conflict' WHERE id=?", (ids['conf'],))
        self.conn.execute(
            "UPDATE memory SET lifecycle='superseded' WHERE id=?", (ids['sup'],))
        self.conn.commit()

        vec = [0.1, 0.2, 0.3]
        with mock.patch(
                'memcore.embedding.get_embeddings_batch',
                return_value=[vec, vec, vec]):
            out = io.StringIO()
            with mock.patch('sys.stdout', out):
                cli.main(['--db', self.db, 'embed'])
        self.assertIn('Embedded 3 memories', out.getvalue())
        embedded = {r[0] for r in self.conn.execute(
            'SELECT DISTINCT memory_id FROM memory_embedding')}
        self.assertEqual(
            embedded, {ids['cand'], ids['acc'], ids['conf']})


# ── C1: dry-run / apply parity ──────────────────────────────────────────────

def _parse_decay_output(text):
    aged, staled = [], []
    current = aged
    for line in text.splitlines():
        if 'aging → stale' in line:
            current = staled
            continue
        if line.strip().startswith('mem-'):
            current.append(line.strip())
    return aged, staled


def _parse_sweep_output(text):
    builtin, defer = [], []
    current = builtin
    for line in text.splitlines():
        if 'defer cap' in line:
            current = defer
            continue
        if line.strip().startswith('evt-'):
            current.append(line.strip())
    return builtin, defer


class TestC1DryRunParity(_StoreCase):

    def _seed_decay_rows(self):
        old_id, _ = self._create_quiet('decay parity claim alpha')
        reinforced_id, _ = self._create_quiet('decay parity claim bravo')
        fresh_id, _ = self._create_quiet('decay parity claim charlie')
        self.conn.execute(
            "UPDATE memory SET updated_at='2020-01-01T00:00:00Z' "
            'WHERE id IN (?, ?)', (old_id, reinforced_id))
        self.conn.commit()
        core.record_recall(self.conn, [reinforced_id])
        self.conn.commit()
        return old_id, reinforced_id, fresh_id

    def test_decay_dry_run_matches_apply_with_reinforced_memories(self):
        old_id, _reinforced_id, _fresh_id = self._seed_decay_rows()
        aged_dry, staled_dry = core.apply_freshness_decay(
            self.conn, dry_run=True)
        self.assertEqual(aged_dry, [old_id])
        self.assertEqual(staled_dry, [])
        # dry-run performed no writes
        freshness = self.conn.execute(
            'SELECT freshness FROM memory WHERE id=?', (old_id,)).fetchone()[0]
        self.assertEqual(freshness, 'current')
        aged, staled = core.apply_freshness_decay(self.conn)
        self.assertEqual(aged, aged_dry)
        self.assertEqual(staled, staled_dry)
        freshness = self.conn.execute(
            'SELECT freshness FROM memory WHERE id=?', (old_id,)).fetchone()[0]
        self.assertEqual(freshness, 'aging')

    def test_decay_cli_dry_run_matches_apply_lists(self):
        old_id, _reinforced_id, _fresh_id = self._seed_decay_rows()

        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            cli.main(['--db', self.db, 'decay'])
        aged_dry, staled_dry = _parse_decay_output(out.getvalue())
        self.assertEqual(aged_dry, [old_id])
        self.assertEqual(staled_dry, [])
        freshness = self.conn.execute(
            'SELECT freshness FROM memory WHERE id=?', (old_id,)).fetchone()[0]
        self.assertEqual(freshness, 'current', 'dry-run must not write')

        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            cli.main(['--db', self.db, 'decay', '--apply'])
        aged_applied, staled_applied = _parse_decay_output(out.getvalue())
        self.assertEqual(aged_applied, aged_dry)
        self.assertEqual(staled_applied, staled_dry)

    def _seed_sweep_rows(self):
        ev_old, _ = ingest.append_event(
            self.conn, 'p', 'a', 'turn', user_content='stale builtin target')
        self.conn.execute(
            "UPDATE ingest_event SET decision='builtin_memory_replace_unresolved_target', "
            "created_at='2020-01-01T00:00:00Z' WHERE id=?", (ev_old,))
        ev_fresh, _ = ingest.append_event(
            self.conn, 'p', 'a', 'turn', user_content='fresh builtin target')
        self.conn.execute(
            "UPDATE ingest_event SET decision='builtin_memory_replace_unresolved_target' "
            'WHERE id=?', (ev_fresh,))
        ev_defer, _ = ingest.append_event(
            self.conn, 'p', 'a', 'turn', user_content='deferred semantic content')
        self.conn.execute(
            "UPDATE ingest_event SET decision='semantic_deferred' WHERE id=?",
            (ev_defer,))
        for i in range(2):
            self.conn.execute(
                'INSERT INTO ingest_analysis '
                '(id, event_id, analyzer, verdict, candidate_content, '
                ' confidence, rationale, metadata, created_at) '
                "VALUES (?, ?, 'test', 'defer', '', 0.5, 'unsure', '{}', ?)",
                (f'an-{i}-{ev_defer[:8]}', ev_defer, '2026-01-01T00:00:00Z'))
        self.conn.commit()
        return ev_old, ev_fresh, ev_defer

    def test_journal_sweep_dry_run_matches_apply(self):
        ev_old, _ev_fresh, ev_defer = self._seed_sweep_rows()
        dry = ingest.sweep(self.conn, builtin_days=7, max_defers=2,
                           dry_run=True)
        self.assertEqual(dry['stale_builtin'], [ev_old])
        self.assertEqual(dry['defer_cap'], [ev_defer])
        statuses = dict(self.conn.execute(
            'SELECT id, status FROM ingest_event'))
        self.assertEqual(statuses[ev_old], 'pending')
        self.assertEqual(statuses[ev_defer], 'pending')
        applied = ingest.sweep(self.conn, builtin_days=7, max_defers=2)
        self.assertEqual(applied['stale_builtin'], dry['stale_builtin'])
        self.assertEqual(applied['defer_cap'], dry['defer_cap'])
        statuses = dict(self.conn.execute(
            'SELECT id, status FROM ingest_event'))
        self.assertEqual(statuses[ev_old], 'ignored')
        self.assertEqual(statuses[ev_defer], 'ignored')

    def test_journal_sweep_cli_dry_run_matches_apply_lists(self):
        ev_old, _ev_fresh, ev_defer = self._seed_sweep_rows()
        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            cli.main(['--db', self.db, 'journal-sweep',
                      '--builtin-days', '7', '--max-defers', '2'])
        builtin_dry, defer_dry = _parse_sweep_output(out.getvalue())
        self.assertEqual(builtin_dry, [ev_old])
        self.assertEqual(defer_dry, [ev_defer])
        status = self.conn.execute(
            'SELECT status FROM ingest_event WHERE id=?', (ev_old,)).fetchone()[0]
        self.assertEqual(status, 'pending')
        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            cli.main(['--db', self.db, 'journal-sweep',
                      '--builtin-days', '7', '--max-defers', '2', '--apply'])
        builtin_applied, defer_applied = _parse_sweep_output(out.getvalue())
        self.assertEqual(builtin_applied, builtin_dry)
        self.assertEqual(defer_applied, defer_dry)


# ── H7: thread safety ───────────────────────────────────────────────────────

class TestH7ThreadSafety(unittest.TestCase):

    def tearDown(self):
        embedding.reset_circuit_breaker()
        try:
            embedding._fastembed_cache.clear()
        except Exception:
            pass

    def test_embedding_circuit_breaker_thread_safety(self):
        embedding.reset_circuit_breaker()
        errors = []

        def hammer():
            try:
                for _ in range(500):
                    embedding._record_failure()
                    embedding.is_circuit_open()
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        self.assertEqual(errors, [])
        # With the lock, increments are exact: no lost updates.
        self.assertEqual(embedding._consecutive_failures, 8 * 500)
        self.assertTrue(embedding.is_circuit_open())
        embedding.reset_circuit_breaker()
        self.assertEqual(embedding._consecutive_failures, 0)
        self.assertFalse(embedding.is_circuit_open())

    def test_fastembed_cache_single_instantiation_under_threads(self):
        created = []

        class FakeEmbedder:
            def __init__(self, model_name=None):
                created.append(model_name)
                time.sleep(0.05)  # simulate the costly ONNX model load

            def embed(self, texts):
                return [[0.1, 0.2] for _ in texts]

        fake_module = types.ModuleType('fastembed')
        fake_module.TextEmbedding = FakeEmbedder
        embedding._fastembed_cache.clear()
        results = []
        errors = []

        def worker():
            try:
                results.append(
                    embedding._get_fastembed_batch(['x'], model='m-test'))
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        with mock.patch.dict(sys.modules, {'fastembed': fake_module}):
            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)
        self.assertEqual(errors, [])
        self.assertEqual(len(created), 1, created)
        self.assertEqual(len(results), 8)


# ── H8: atomic writes ───────────────────────────────────────────────────────

class TestH8AtomicWrites(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='memcore_h8_')
        self.conn, self.db = _make_store(self.tmp.name, name='atomic.db')

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def _tmp_leftovers(self):
        return [name for name in os.listdir(self.tmp.name) if '.tmp.' in name]

    def test_export_atomic_write_leaves_existing_file_intact_on_failure(self):
        out_path = os.path.join(self.tmp.name, 'AGENTS.md')
        original = ('<!-- generated by MemCore (content abc123) -->\n'
                    '# existing hand-authored content\n')
        with open(out_path, 'w', encoding='utf-8') as handle:
            handle.write(original)
        with mock.patch('memcore.export.os.replace',
                        side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                export_mod.export(self.conn, 'p', 'a', out_path=out_path)
        with open(out_path, encoding='utf-8') as handle:
            self.assertEqual(handle.read(), original)
        self.assertEqual(self._tmp_leftovers(), [])

    def test_restore_backup_atomic_write_cleanup_on_io_error(self):
        target = os.path.join(self.tmp.name, 'restore-target.db')
        store.open_store(target).close()
        snapshot = os.path.join(self.tmp.name, 'snapshot.db')
        snap_conn = store.open_store(snapshot)
        snap_conn.execute(
            "INSERT INTO project (id, name) VALUES ('snap', 'snap')")
        snap_conn.commit()
        snap_conn.close()
        with open(target, 'rb') as handle:
            before = handle.read()
        with mock.patch('memcore.__main__.os.replace',
                        side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                cli.main(['--db', target, 'restore-from-snapshot',
                          '--snapshot', snapshot, '--confirm'])
        with open(target, 'rb') as handle:
            self.assertEqual(handle.read(), before)
        self.assertEqual(self._tmp_leftovers(), [])


# ── C3: demotion overfetch window ───────────────────────────────────────────

class TestC3DemotionOverfetch(unittest.TestCase):

    @staticmethod
    def _row(mid, content):
        return (mid, 'project', 'accepted', 'unverified', 'current', content,
                'a', 0.0, core.fingerprint(' '.join(content.split())))

    def test_demotion_promotes_compatible_hit_from_overfetch_window(self):
        base = [
            self._row('mem-1', 'gateway listens on port 9090'),
            self._row('mem-2', 'gateway listens on port 7070'),
            self._row('mem-3', 'gateway listens on port 6060'),
            self._row('mem-4', 'gateway listens on port 8080'),
        ]
        result = core._merge_exact_fallback(base, [], 3, 'gateway port 8080')
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0][0], 'mem-4',
                         'compatible hit must be promoted from the window')
        # a query without numbers leaves order untouched
        unchanged = core._merge_exact_fallback(base, [], 3, 'gateway listens')
        self.assertEqual(
            [row[0] for row in unchanged], ['mem-1', 'mem-2', 'mem-3'])


# ── H6/R1: journal scrub identity ───────────────────────────────────────────

class TestH6JournalScrubIdentity(_StoreCase):

    def test_append_event_secret_scrub_keeps_retry_identity(self):
        secret = 'hf_' + 'a' * 40
        content = f'please store token {secret} for the test'
        ev1, created1 = ingest.append_event(
            self.conn, 'p', 'a', 'turn', user_content=content)
        ev2, created2 = ingest.append_event(
            self.conn, 'p', 'a', 'turn', user_content=content)
        self.assertEqual(ev1, ev2)
        self.assertTrue(created1)
        self.assertFalse(created2)
        # R1: the stored event carries the scrub of the same input, and the
        # hash is computed OVER THE SCRUBBED payload — re-sending the already
        # scrubbed form keeps the same identity (no duplicate events).
        stored = self.conn.execute(
            'SELECT user_content FROM ingest_event WHERE id=?',
            (ev1,)).fetchone()[0]
        self.assertNotIn(secret, stored)
        self.assertIn('redacted', stored)
        scrubbed = core.scrub_secrets(content)
        ev3, created3 = ingest.append_event(
            self.conn, 'p', 'a', 'turn', user_content=scrubbed)
        self.assertEqual(ev3, ev1)
        self.assertFalse(created3)
        count = self.conn.execute(
            'SELECT COUNT(*) FROM ingest_event WHERE id=?', (ev1,)).fetchone()[0]
        self.assertEqual(count, 1)


if __name__ == '__main__':
    unittest.main()
