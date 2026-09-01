import sqlite3
import time
from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id          TEXT PRIMARY KEY,
  session_id  TEXT NOT NULL,
  filename    TEXT NOT NULL,
  sha256      TEXT NOT NULL,
  opts_hash   TEXT NOT NULL,
  status      TEXT NOT NULL,
  page_total  INTEGER,
  started_at  REAL,
  finished_at REAL,
  error       TEXT,
  result_dir  TEXT,
  n_tables    INTEGER,
  n_images    INTEGER,
  attempts    INTEGER NOT NULL DEFAULT 0,
  created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_cache   ON jobs(sha256, opts_hash, status);
CREATE INDEX IF NOT EXISTS idx_jobs_session ON jobs(session_id, created_at);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def create_job(conn, *, id, session_id, filename, sha256, opts_hash,
               status, page_total, result_dir=None) -> None:
    conn.execute(
        "INSERT INTO jobs (id, session_id, filename, sha256, opts_hash, status, "
        "page_total, result_dir, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (id, session_id, filename, sha256, opts_hash, status, page_total, result_dir, time.time()),
    )
    conn.commit()


def find_cached(conn, sha256, opts_hash):
    return conn.execute(
        "SELECT * FROM jobs WHERE sha256=? AND opts_hash=? AND status='done' "
        "ORDER BY created_at DESC LIMIT 1",
        (sha256, opts_hash),
    ).fetchone()


def get_job(conn, job_id):
    return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def list_jobs(conn, session_id, admin: bool):
    if admin:
        return conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT 200").fetchall()
    return conn.execute(
        "SELECT * FROM jobs WHERE session_id=? ORDER BY created_at DESC LIMIT 200",
        (session_id,)).fetchall()


def claim_next_queued(conn):
    # 원자적 선점: 다중 워커로 늘려도 안전.
    cur = conn.execute(
        "UPDATE jobs SET status='running', started_at=? "
        "WHERE id = (SELECT id FROM jobs WHERE status='queued' "
        "            ORDER BY created_at LIMIT 1) RETURNING *",
        (time.time(),),
    )
    row = cur.fetchone()
    conn.commit()
    return row


def finish_job(conn, job_id, *, status, error=None, result_dir=None,
               n_tables=None, n_images=None) -> None:
    conn.execute(
        "UPDATE jobs SET status=?, error=?, result_dir=COALESCE(?, result_dir), "
        "n_tables=COALESCE(?, n_tables), n_images=COALESCE(?, n_images), "
        "finished_at=? WHERE id=?",
        (status, error, result_dir, n_tables, n_images, time.time(), job_id),
    )
    conn.commit()


def count_queued(conn, session_id) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE session_id=? AND status='queued'",
        (session_id,)).fetchone()[0]


def delete_done(conn, session_id, admin: bool) -> int:
    """완료 잡 행을 지운다(관리자면 전체, 아니면 제 세션 것만).

    파일은 건드리지 않는다 — 워커 sweep이 참조 없는 업로드·결과부터 정리한다.
    """
    if admin:
        cur = conn.execute("DELETE FROM jobs WHERE status='done'")
    else:
        cur = conn.execute(
            "DELETE FROM jobs WHERE status='done' AND session_id=?", (session_id,))
    conn.commit()
    return cur.rowcount


def delete_expired(conn) -> int:
    cur = conn.execute("DELETE FROM jobs WHERE created_at < ?",
                       (time.time() - config.RETENTION_SEC,))
    conn.commit()
    return cur.rowcount


def referenced_shas(conn):
    return {r["sha256"] for r in conn.execute("SELECT DISTINCT sha256 FROM jobs")}


def referenced_result_dirs(conn):
    return {r["result_dir"] for r in
            conn.execute("SELECT DISTINCT result_dir FROM jobs WHERE result_dir IS NOT NULL")}


def active_created_ats(conn) -> list[float]:
    # 대기 잡마다 COUNT(*)를 날리는 대신, 한 번 정렬해 받아 bisect로 순번을 매긴다.
    return [r[0] for r in conn.execute(
        "SELECT created_at FROM jobs WHERE status IN ('queued', 'running') "
        "ORDER BY created_at")]


def worker_busy(conn) -> bool:
    return conn.execute(
        "SELECT 1 FROM jobs WHERE status='running' LIMIT 1").fetchone() is not None


def requeue_running(conn) -> int:
    # 워커가 running 중에 죽으면 그 잡은 영원히 running으로 남아 worker_busy가
    # 계속 True가 된다. 워커가 하나뿐이므로 시작 시 남아있는 running은 모두 고아다.
    # attempts를 올려, 반복해서 워커를 죽이는(OOM 등) 문서가 무한 재시도되지 않게 한다.
    cur = conn.execute(
        "UPDATE jobs SET status='queued', started_at=NULL, attempts=attempts+1 "
        "WHERE status='running'")
    conn.commit()
    return cur.rowcount
