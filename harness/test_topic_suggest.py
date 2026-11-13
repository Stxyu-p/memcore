"""Topic suggestions: lexical coverage ranking, isolation, and the remember surface."""
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from memcore import mcp_server, recall, store
from memcore.topic_suggest import suggest_topics
from memcore.write import create_memory


class TopicSuggestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self.tmp.name) / "m.db"
        self.conn = store.open_store(str(self.db_path))
        self.pid, self.aid = "proj-s", "suggest-agent"
        self.conn.execute("INSERT INTO project (id,name) VALUES (?,?)", (self.pid, "S"))
        self.conn.execute("INSERT INTO project (id,name) VALUES (?,?)", ("proj-other", "O"))
        for aid in (self.aid, "other-agent"):
            self.conn.execute(
                "INSERT INTO agent (id,name,profile_key) VALUES (?,?,?)",
                (aid, aid, "default" if aid == self.aid else "other-profile"),
            )
            self.conn.execute(
                "INSERT INTO project_membership (project_id,agent_id,role) VALUES (?,?,?)",
                (self.pid, aid, "owner"),
            )
        self.conn.execute(
            "INSERT INTO project_membership (project_id,agent_id,role) VALUES (?,?,?)",
            ("proj-other", self.aid, "owner"),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def remember(self, content, *, topics=None, project=None, agent=None, scope="project"):
        mid, _ = create_memory(
            self.conn, project or self.pid, agent or self.aid, content,
            scope=scope, lifecycle="accepted", topics=topics,
        )
        self.conn.commit()
        return mid

    def topics_of(self, mem_id):
        return [
            r[0] for r in self.conn.execute(
                "SELECT topic_id FROM memory_topic WHERE memory_id = ? ORDER BY topic_id",
                (mem_id,),
            )
        ]


class TestSuggestRanking(TopicSuggestBase):
    def test_ranks_the_covering_topic_first(self):
        self.remember(
            "sqlite wal checkpoint tuning prevents writer starvation",
            topics=["db/sqlite"],
        )
        self.remember(
            "thai bigram tokenization for fts5 unicode61 search",
            topics=["search/thai"],
        )
        hits = suggest_topics(
            self.conn, self.pid, self.aid,
            "sqlite wal checkpoint writer starvation under load",
        )
        self.assertEqual([h["topic_id"] for h in hits], ["db/sqlite"])
        # 5 of the query's 7 salient tokens appear in that topic's memory
        self.assertAlmostEqual(hits[0]["coverage"], 0.714, places=3)
        self.assertEqual(hits[0]["matched_memories"], 1)

    def test_unrelated_content_suggests_nothing(self):
        self.remember(
            "sqlite wal checkpoint tuning prevents writer starvation",
            topics=["db/sqlite"],
        )
        self.assertEqual(
            suggest_topics(self.conn, self.pid, self.aid, "bake sourdough bread overnight"),
            [],
        )

    def test_min_coverage_filters_weak_overlap(self):
        self.remember(
            "sqlite wal checkpoint tuning prevents writer starvation",
            topics=["db/sqlite"],
        )
        # one shared token out of five is far below a 0.9 bar
        hits = suggest_topics(
            self.conn, self.pid, self.aid,
            "sqlite kubernetes ingress certificate rotation policy",
            min_coverage=0.9,
        )
        self.assertEqual(hits, [])

    def test_no_topics_table_yields_nothing(self):
        empty_conn = store.open_store(str(pathlib.Path(self.tmp.name) / "empty.db"))
        try:
            empty_conn.execute("DROP TABLE IF EXISTS memory_topic")
            empty_conn.commit()
            self.assertEqual(suggest_topics(empty_conn, self.pid, self.aid, "anything at all"), [])
        finally:
            empty_conn.close()


class TestSuggestIsolation(TopicSuggestBase):
    def test_other_project_memories_never_score(self):
        self.remember(
            "sqlite wal checkpoint tuning prevents writer starvation",
            topics=["db/sqlite"], project="proj-other",
        )
        self.assertEqual(
            suggest_topics(
                self.conn, self.pid, self.aid,
                "sqlite wal checkpoint writer starvation",
            ),
            [],
        )

    def test_other_agents_private_memory_never_scores(self):
        self.remember(
            "sqlite wal checkpoint tuning prevents writer starvation",
            topics=["db/sqlite"], agent="other-agent", scope="private",
        )
        self.assertEqual(
            suggest_topics(
                self.conn, self.pid, self.aid,
                "sqlite wal checkpoint writer starvation",
            ),
            [],
        )

    def test_own_private_memory_does_score(self):
        self.remember(
            "sqlite wal checkpoint tuning prevents writer starvation",
            topics=["db/sqlite"], scope="private",
        )
        hits = suggest_topics(
            self.conn, self.pid, self.aid,
            "sqlite wal checkpoint writer starvation",
        )
        self.assertEqual([h["topic_id"] for h in hits], ["db/sqlite"])


class TestSuggestTelemetry(TopicSuggestBase):
    def test_suggestion_probe_writes_no_recall_telemetry(self):
        self.remember(
            "sqlite wal checkpoint tuning prevents writer starvation",
            topics=["db/sqlite"],
        )
        with patch.object(
            recall, "_log_recall_telemetry",
            side_effect=AssertionError("suggestions must not pollute recall telemetry"),
        ):
            hits = suggest_topics(
                self.conn, self.pid, self.aid,
                "sqlite wal checkpoint writer starvation",
            )
        self.assertTrue(hits)


class TestRememberTopicMode(TopicSuggestBase):
    def setUp(self):
        super().setUp()
        self.remember(
            "sqlite wal checkpoint tuning prevents writer starvation",
            topics=["db/sqlite"],
        )
        self.env = patch.dict(os.environ, {"MEMCORE_DB_PATH": str(self.db_path)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        super().tearDown()

    def call(self, args):
        with patch.object(mcp_server, "DEFAULT_AGENT", self.aid):
            return mcp_server._tool_remember({"project": self.pid, "scope": "project", **args})

    def test_suggest_mode_reports_without_attaching(self):
        res = self.call({"content": "sqlite wal checkpoint writer starvation tuning notes"})
        self.assertTrue(res["success"])
        self.assertEqual(res["suggested_topics"][0]["topic_id"], "db/sqlite")
        self.assertEqual(self.topics_of(res["memory_id"]), [])

    def test_auto_mode_attaches_best_match(self):
        res = self.call({
            "content": "sqlite wal checkpoint writer starvation tuning notes",
            "topic_mode": "auto",
        })
        self.assertEqual(res["auto_topics"], ["db/sqlite"])
        self.assertEqual(self.topics_of(res["memory_id"]), ["db/sqlite"])
        self.assertNotIn("suggested_topics", res)

    def test_off_mode_skips_the_check(self):
        res = self.call({
            "content": "sqlite wal checkpoint writer starvation tuning notes",
            "topic_mode": "off",
        })
        self.assertNotIn("suggested_topics", res)
        self.assertNotIn("auto_topics", res)

    def test_explicit_topics_bypass_suggestions(self):
        res = self.call({
            "content": "sqlite wal checkpoint writer starvation tuning notes",
            "topics": ["db/sqlite"],
        })
        self.assertNotIn("suggested_topics", res)
        self.assertEqual(self.topics_of(res["memory_id"]), ["db/sqlite"])

    def test_invalid_topic_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            self.call({"content": "anything", "topic_mode": "always"})


if __name__ == "__main__":
    unittest.main()
