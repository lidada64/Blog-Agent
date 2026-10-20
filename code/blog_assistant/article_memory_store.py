"""Versioned article inventory and queue; runtime mixin owns leases and budgets."""
import hashlib
import json
import sqlite3
from pathlib import Path

from .article_memory_build import GENERATION_VERSION, PARSER_VERSION
from .article_memory_runtime import MemoryRuntime, MIGRATION_V2

SCHEMA_VERSION = 2
_SCHEMA = (
    """CREATE TABLE libraries (
        library_id TEXT PRIMARY KEY, root TEXT NOT NULL, scope TEXT NOT NULL,
        generation_version TEXT NOT NULL, scan_epoch INTEGER NOT NULL DEFAULT 0,
        applied_epoch INTEGER NOT NULL DEFAULT 0, initialized INTEGER NOT NULL DEFAULT 0,
        scan_state TEXT NOT NULL DEFAULT 'not_scanned', issues_json TEXT NOT NULL DEFAULT '[]',
        maintenance_json TEXT NOT NULL DEFAULT '{}', UNIQUE(root, scope))""",
    """CREATE TABLE documents (
        library_id TEXT NOT NULL REFERENCES libraries(library_id), article_id TEXT NOT NULL,
        detected_version TEXT NOT NULL, active_version TEXT, status TEXT NOT NULL,
        seen_epoch INTEGER NOT NULL, PRIMARY KEY(library_id, article_id))""",
    """CREATE TABLE nodes (
        library_id TEXT NOT NULL, article_id TEXT NOT NULL, source_version TEXT NOT NULL,
        generation_version TEXT NOT NULL, node_id TEXT NOT NULL, parent_id TEXT, kind TEXT NOT NULL,
        title TEXT NOT NULL, start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
        citation_id TEXT, parser_version TEXT NOT NULL, summary TEXT,
        state TEXT NOT NULL DEFAULT 'indexed', dependencies_json TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY(library_id, article_id, source_version, generation_version, node_id),
        FOREIGN KEY(library_id, article_id) REFERENCES documents(library_id, article_id))""",
    """CREATE TABLE jobs (
        job_id INTEGER PRIMARY KEY, library_id TEXT NOT NULL, article_id TEXT NOT NULL,
        source_version TEXT NOT NULL, node_id TEXT NOT NULL, generation_version TEXT NOT NULL,
        state TEXT NOT NULL, reason TEXT, lease_token INTEGER NOT NULL DEFAULT 0,
        lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0, next_run_at REAL,
        UNIQUE(library_id, article_id, source_version, node_id, generation_version),
        FOREIGN KEY(library_id, article_id) REFERENCES documents(library_id, article_id))""",
    """CREATE TABLE usage (
        library_id TEXT NOT NULL REFERENCES libraries(library_id), request_id TEXT NOT NULL,
        state TEXT NOT NULL, reserved_requests INTEGER NOT NULL,
        reserved_input_tokens INTEGER NOT NULL, reserved_output_tokens INTEGER NOT NULL,
        reserved_cost_microunits INTEGER NOT NULL, actual_input_tokens INTEGER,
        actual_output_tokens INTEGER, actual_cost_microunits INTEGER,
        PRIMARY KEY(library_id, request_id))""",
    "CREATE INDEX jobs_by_state ON jobs(library_id, state, next_run_at)",
)


class ArticleMemoryStore(MemoryRuntime):
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, SCHEMA_VERSION):
                raise ValueError(f"Unsupported article memory schema: {version}")
            if version == 0:
                for statement in _SCHEMA:
                    self.db.execute(statement)
            if version < 2:
                for statement in MIGRATION_V2:
                    self.db.execute(statement)
                self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.db.commit()
        except Exception:
            self.db.rollback()
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def register_library(self, root, scope: str, generation_version: str = GENERATION_VERSION) -> str:
        """Trusted maintenance callers supply scope; a mode name is not authentication."""
        root = Path(root).resolve(strict=True)
        if not root.is_dir() or not scope.strip() or not generation_version.strip():
            raise ValueError("root, scope and generation_version are required")
        identity = json.dumps([str(root), scope], ensure_ascii=False)
        library_id = hashlib.sha256(identity.encode()).hexdigest()
        # Never change generation config implicitly while another scan is in flight.
        self.db.execute("""INSERT OR IGNORE INTO libraries
            (library_id, root, scope, generation_version) VALUES (?, ?, ?, ?)""",
                        (library_id, str(root), scope, generation_version))
        existing = self._library(library_id)
        if generation_version != GENERATION_VERSION and existing["generation_version"] != generation_version:
            raise ValueError("Generation changes require a future explicit rebuild/migration API")
        return library_id

    def _library(self, library_id):
        row = self.db.execute("SELECT * FROM libraries WHERE library_id=?", (library_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown library")
        return row

    def find_library(self, root, scope: str) -> str:
        row = self.db.execute("SELECT library_id FROM libraries WHERE root=? AND scope=?",
                              (str(Path(root).resolve()), scope)).fetchone()
        if row is None:
            raise ValueError("Unknown library; initialize it first")
        return row[0]

    def begin_scan(self, library_id: str, token=None) -> tuple[str, int]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if token is not None:
                self.require_worker(library_id, token)
            library = self._library(library_id)
            epoch = library["scan_epoch"] + 1
            self.db.execute("UPDATE libraries SET scan_epoch=?, scan_state='scanning' WHERE library_id=?",
                            (epoch, library_id))
            self.db.commit()
            return library["root"], epoch
        except Exception:
            self.db.rollback()
            raise

    def apply_scan(self, scan, token=None):
        """Only the latest issued epoch may commit; partial scans never infer deletion."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if token is not None:
                self.require_worker(scan.library_id, token)
            library = self._library(scan.library_id)
            if (library["root"] != scan.root or library["scan_epoch"] != scan.scan_epoch
                    or library["applied_epoch"] >= scan.scan_epoch):
                raise ValueError("Stale or mismatched scan")
            generation = library["generation_version"]
            for article in scan.articles:
                key = (scan.library_id, article.article_id)
                previous = self.db.execute("SELECT * FROM documents WHERE library_id=? AND article_id=?", key).fetchone()
                changed = previous is None or previous["detected_version"] != article.version or previous["status"] == "deleted"
                if changed:
                    self.db.execute("""INSERT INTO documents VALUES (?, ?, ?, NULL, 'stale', ?)
                        ON CONFLICT(library_id, article_id) DO UPDATE SET
                        detected_version=excluded.detected_version, active_version=NULL,
                        status='stale', seen_epoch=excluded.seen_epoch""", (*key, article.version, scan.scan_epoch))
                    self.db.execute("UPDATE jobs SET state='superseded', reason='source_changed' WHERE library_id=? AND article_id=?", key)
                else:
                    self.db.execute("UPDATE documents SET seen_epoch=? WHERE library_id=? AND article_id=?", (scan.scan_epoch, *key))
                for node in article.nodes:
                    self.db.execute("""INSERT OR IGNORE INTO nodes
                        (library_id, article_id, source_version, generation_version, node_id,
                         parent_id, kind, title, start_line, end_line, citation_id, parser_version)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (*key, article.version, generation, node["node_id"], node["parent_id"],
                         node["kind"], node["title"], node["start_line"], node["end_line"],
                         node["citation_id"], PARSER_VERSION))
                # Article-level build intent; Stage C expands a validated dependency DAG.
                self.db.execute("""INSERT INTO jobs
                    (library_id, article_id, source_version, node_id, generation_version, state, reason)
                    VALUES (?, ?, ?, 'article', ?, 'blocked_config', 'generator_and_budget_not_implemented')
                    ON CONFLICT(library_id, article_id, source_version, node_id, generation_version)
                    DO UPDATE SET state='blocked_config', reason=excluded.reason
                    WHERE jobs.state='superseded'""", (*key, article.version, generation))
                if json.loads(library['maintenance_json']):
                    self.db.execute("UPDATE jobs SET state='pending', reason=NULL WHERE library_id=? AND article_id=? AND source_version=? AND generation_version=? AND state='blocked_config'",
                                    (*key, article.version, generation))
            if scan.complete:
                missing = self.db.execute("SELECT article_id FROM documents WHERE library_id=? AND seen_epoch<>? AND status<>'deleted'",
                                          (scan.library_id, scan.scan_epoch)).fetchall()
                for row in missing:
                    key = (scan.library_id, row["article_id"])
                    self.db.execute("UPDATE documents SET status='deleted', active_version=NULL WHERE library_id=? AND article_id=?", key)
                    self.db.execute("UPDATE jobs SET state='superseded', reason='source_deleted' WHERE library_id=? AND article_id=?", key)
            self.db.execute("""UPDATE libraries SET applied_epoch=?, initialized=MAX(initialized, ?),
                scan_state=?, issues_json=? WHERE library_id=?""",
                (scan.scan_epoch, int(scan.complete), "complete" if scan.complete else "partial",
                 json.dumps(scan.issues, ensure_ascii=False), scan.library_id))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def status(self, library_id: str) -> dict:
        library = dict(self._library(library_id))
        library["initialized"] = bool(library["initialized"])
        library["issues"] = json.loads(library.pop("issues_json"))
        library.pop("maintenance_json")
        library["jobs"] = {row["state"]: row["n"] for row in self.db.execute(
            "SELECT state, COUNT(*) AS n FROM jobs WHERE library_id=? GROUP BY state", (library_id,))}
        library["documents"] = {row["status"]: row["n"] for row in self.db.execute(
            "SELECT status, COUNT(*) AS n FROM documents WHERE library_id=? GROUP BY status", (library_id,))}
        return {**library, "schema_version": SCHEMA_VERSION, "worker_state": self.worker(library_id)['state'],
                "worker": self.worker(library_id), "budget": self.budget_status(library_id)}
