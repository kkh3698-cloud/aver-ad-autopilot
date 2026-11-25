"""SQLite 저장소. 순위 이력, 광고 일별 통계, 소재 가설, 실행 로그."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS place_ranks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at TEXT NOT NULL,
    keyword TEXT NOT NULL,
    provider TEXT NOT NULL,
    rank INTEGER,              -- NULL = 조회 범위 안에서 못 찾음
    scanned INTEGER NOT NULL,  -- 몇 위까지 확인했는지
    top_competitors TEXT       -- JSON: 상위 업체명
);
CREATE INDEX IF NOT EXISTS ix_ranks_kw ON place_ranks(keyword, checked_at);

CREATE TABLE IF NOT EXISTS ad_stats_daily (
    entity_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,     -- ad | keyword | adgroup
    adgroup_id TEXT,
    stat_date TEXT NOT NULL,
    imp INTEGER DEFAULT 0,
    clk INTEGER DEFAULT 0,
    cost INTEGER DEFAULT 0,        -- 네이버 salesAmt = 광고비(원)
    conv REAL DEFAULT 0,           -- ccnt
    avg_rank REAL,
    PRIMARY KEY (entity_id, stat_date)
);

CREATE TABLE IF NOT EXISTS entities (
    entity_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    adgroup_id TEXT,
    label TEXT,                    -- 소재: 제목|설명, 키워드: 키워드 텍스트
    status TEXT,                   -- active | paused | pending | rejected
    bid INTEGER,
    created_at TEXT,
    updated_at TEXT,
    meta TEXT
);

CREATE TABLE IF NOT EXISTS variants (
    entity_id TEXT PRIMARY KEY,    -- 생성된 소재 ID
    adgroup_id TEXT,
    headline TEXT,
    description TEXT,
    hypothesis TEXT,               -- 이 소재가 시험하는 것
    source TEXT,                   -- claude | template | manual
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS inspirations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch TEXT NOT NULL,           -- 수집 회차
    source TEXT NOT NULL,          -- youtube | naver_blog | manual | demo
    query TEXT,
    title TEXT,
    snippet TEXT,
    url TEXT,
    metric_label TEXT,             -- 사람이 읽는 근거: "조회수 12만, 하루 4천"
    score REAL,                    -- 소스 내 백분위(0~100)
    published TEXT,
    collected_at TEXT
);

CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    adgroup_id TEXT NOT NULL,
    adgroup_name TEXT,
    headline TEXT NOT NULL,
    description TEXT NOT NULL,
    hypothesis TEXT,
    pattern TEXT,                  -- 참고한 인기 콘텐츠의 소구 패턴
    inspiration_ids TEXT,          -- JSON [id,...]
    replaces_ad_id TEXT,           -- 교체 대상(패자) 소재. NULL 이면 빈 자리 추가
    replaces_label TEXT,
    reason TEXT,
    source TEXT,                   -- claude | template
    status TEXT NOT NULL,          -- pending → approved → applied → live
                                   --   | rejected | expired | naver_rejected | failed
    new_ad_id TEXT,
    created_at TEXT,
    decided_at TEXT,
    decided_by TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS actions_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL,
    plan_id TEXT,
    action_type TEXT NOT NULL,
    target_id TEXT,
    payload TEXT,
    reason TEXT,
    status TEXT NOT NULL,          -- applied | dry_run | skipped | failed
    detail TEXT
);
"""


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # ---- place ranks ----------------------------------------------------
    def add_rank(self, keyword: str, provider: str, rank: int | None, scanned: int,
                 competitors: list[str], checked_at: str | None = None) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO place_ranks(checked_at, keyword, provider, rank, scanned, top_competitors)"
                " VALUES (?,?,?,?,?,?)",
                (checked_at or now(), keyword, provider, rank, scanned,
                 json.dumps(competitors, ensure_ascii=False)),
            )

    def rank_history(self, keyword: str, limit: int = 60) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute(
                "SELECT * FROM place_ranks WHERE keyword=? ORDER BY checked_at DESC LIMIT ?",
                (keyword, limit),
            ).fetchall()[::-1]

    def rank_keywords(self) -> list[str]:
        with self.conn() as c:
            return [r[0] for r in c.execute("SELECT DISTINCT keyword FROM place_ranks ORDER BY keyword")]

    # ---- entities & stats -----------------------------------------------
    def upsert_entity(self, entity_id: str, entity_type: str, adgroup_id: str | None,
                      label: str, status: str, bid: int | None = None,
                      meta: dict[str, Any] | None = None) -> None:
        ts = now()
        with self.conn() as c:
            c.execute(
                """INSERT INTO entities(entity_id, entity_type, adgroup_id, label, status, bid,
                                        created_at, updated_at, meta)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(entity_id) DO UPDATE SET
                     label=excluded.label, status=excluded.status, bid=excluded.bid,
                     adgroup_id=excluded.adgroup_id, updated_at=excluded.updated_at,
                     meta=excluded.meta""",
                (entity_id, entity_type, adgroup_id, label, status, bid, ts, ts,
                 json.dumps(meta or {}, ensure_ascii=False)),
            )

    def entities(self, entity_type: str, adgroup_id: str | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM entities WHERE entity_type=?"
        args: list[Any] = [entity_type]
        if adgroup_id:
            q += " AND adgroup_id=?"
            args.append(adgroup_id)
        with self.conn() as c:
            return c.execute(q + " ORDER BY created_at", args).fetchall()

    def upsert_stat(self, entity_id: str, entity_type: str, adgroup_id: str | None,
                    stat_date: str, imp: int, clk: int, cost: int, conv: float,
                    avg_rank: float | None) -> None:
        with self.conn() as c:
            c.execute(
                """INSERT INTO ad_stats_daily VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(entity_id, stat_date) DO UPDATE SET
                     imp=excluded.imp, clk=excluded.clk, cost=excluded.cost,
                     conv=excluded.conv, avg_rank=excluded.avg_rank""",
                (entity_id, entity_type, adgroup_id, stat_date, imp, clk, cost, conv, avg_rank),
            )

    def stats_window(self, entity_id: str, since: str) -> dict[str, float]:
        with self.conn() as c:
            r = c.execute(
                """SELECT COALESCE(SUM(imp),0) imp, COALESCE(SUM(clk),0) clk,
                          COALESCE(SUM(cost),0) cost, COALESCE(SUM(conv),0) conv,
                          CASE WHEN SUM(imp)>0 THEN SUM(avg_rank*imp)/SUM(imp) END avg_rank,
                          COUNT(*) days
                   FROM ad_stats_daily WHERE entity_id=? AND stat_date>=?""",
                (entity_id, since),
            ).fetchone()
        return dict(r)

    def daily_totals(self, since: str, entity_type: str = "ad") -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute(
                """SELECT stat_date, SUM(imp) imp, SUM(clk) clk, SUM(cost) cost, SUM(conv) conv
                   FROM ad_stats_daily WHERE stat_date>=? AND entity_type=?
                   GROUP BY stat_date ORDER BY stat_date""",
                (since, entity_type),
            ).fetchall()

    # ---- variants & logs ------------------------------------------------
    def add_variant(self, entity_id: str, adgroup_id: str, headline: str, description: str,
                    hypothesis: str, source: str) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO variants VALUES (?,?,?,?,?,?,?)",
                (entity_id, adgroup_id, headline, description, hypothesis, source, now()),
            )

    def variant(self, entity_id: str) -> sqlite3.Row | None:
        with self.conn() as c:
            return c.execute("SELECT * FROM variants WHERE entity_id=?", (entity_id,)).fetchone()

    def log_action(self, plan_id: str, action_type: str, target_id: str | None,
                   payload: dict[str, Any], reason: str, status: str, detail: str = "") -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO actions_log(run_at, plan_id, action_type, target_id, payload, reason, status, detail)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (now(), plan_id, action_type, target_id,
                 json.dumps(payload, ensure_ascii=False), reason, status, detail),
            )

    def recent_actions(self, limit: int = 50) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute("SELECT * FROM actions_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def already_applied(self, plan_id: str, action_type: str, payload: dict[str, Any]) -> bool:
        with self.conn() as c:
            return c.execute(
                "SELECT 1 FROM actions_log WHERE plan_id=? AND action_type=? AND payload=? AND status='applied'",
                (plan_id, action_type, json.dumps(payload, ensure_ascii=False)),
            ).fetchone() is not None

    def last_applied(self, target_id: str, action_type: str) -> str | None:
        with self.conn() as c:
            r = c.execute(
                "SELECT MAX(run_at) FROM actions_log WHERE target_id=? AND action_type=? AND status='applied'",
                (target_id, action_type),
            ).fetchone()
        return r[0] if r else None

    def applied_count_since(self, since_iso: str) -> int:
        with self.conn() as c:
            return c.execute(
                "SELECT COUNT(*) FROM actions_log WHERE status='applied' AND run_at>=?", (since_iso,)
            ).fetchone()[0]


    # ---- inspirations ---------------------------------------------------
    def add_inspirations(self, batch: str, items: list[dict[str, Any]]) -> None:
        with self.conn() as c:
            c.executemany(
                "INSERT INTO inspirations(batch, source, query, title, snippet, url, metric_label, score,"
                " published, collected_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(batch, i["source"], i.get("query"), i.get("title"), i.get("snippet"), i.get("url"),
                  i.get("metric_label"), i.get("score", 0), i.get("published"), now()) for i in items],
            )

    def latest_inspiration_batch(self) -> tuple[str | None, str | None]:
        with self.conn() as c:
            r = c.execute("SELECT batch, MAX(collected_at) FROM inspirations").fetchone()
        return (r[0], r[1]) if r and r[0] else (None, None)

    def top_inspirations(self, limit: int = 12, per_source: int | None = None) -> list[sqlite3.Row]:
        batch, _ = self.latest_inspiration_batch()
        if not batch:
            return []
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM inspirations WHERE batch=? ORDER BY score DESC, id", (batch,)
            ).fetchall()
        if not per_source:
            return rows[:limit]
        # 소스별로 골고루 (유튜브만 잔뜩 뽑히지 않게)
        picked, count = [], {}
        for r in rows:
            if count.get(r["source"], 0) < per_source:
                picked.append(r)
                count[r["source"]] = count.get(r["source"], 0) + 1
        return picked[:limit]

    def inspirations_by_ids(self, ids: list[int]) -> list[sqlite3.Row]:
        if not ids:
            return []
        with self.conn() as c:
            q = ",".join("?" * len(ids))
            return c.execute(f"SELECT * FROM inspirations WHERE id IN ({q})", ids).fetchall()

    # ---- proposals ------------------------------------------------------
    def add_proposal(self, **f: Any) -> int:
        f.setdefault("status", "pending")
        f.setdefault("created_at", now())
        cols = ",".join(f)
        with self.conn() as c:
            cur = c.execute(f"INSERT INTO proposals({cols}) VALUES ({','.join('?' * len(f))})", list(f.values()))
            return int(cur.lastrowid)

    def proposals(self, statuses: list[str] | None = None, adgroup_id: str | None = None,
                  limit: int = 200) -> list[sqlite3.Row]:
        q, args = "SELECT * FROM proposals WHERE 1=1", []
        if statuses:
            q += f" AND status IN ({','.join('?' * len(statuses))})"
            args += statuses
        if adgroup_id:
            q += " AND adgroup_id=?"
            args.append(adgroup_id)
        with self.conn() as c:
            return c.execute(q + " ORDER BY id DESC LIMIT ?", args + [limit]).fetchall()

    def proposal(self, pid: int) -> sqlite3.Row | None:
        with self.conn() as c:
            return c.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()

    def update_proposal(self, pid: int, **f: Any) -> None:
        sets = ",".join(f"{k}=?" for k in f)
        with self.conn() as c:
            c.execute(f"UPDATE proposals SET {sets} WHERE id=?", list(f.values()) + [pid])

    def transition(self, pid: int, from_status: str, **f: Any) -> bool:
        """상태가 from_status 일 때만 바꾼다 (동시 승인/중복 클릭 방지). 바뀌었으면 True."""
        sets = ",".join(f"{k}=?" for k in f)
        with self.conn() as c:
            cur = c.execute(f"UPDATE proposals SET {sets} WHERE id=? AND status=?",
                            list(f.values()) + [pid, from_status])
            return cur.rowcount == 1

    # ---- kv -------------------------------------------------------------
    def kv_get(self, k: str, default: str | None = None) -> str | None:
        with self.conn() as c:
            r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def kv_set(self, k: str, v: str) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")
