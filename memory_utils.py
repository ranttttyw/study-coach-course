"""课程版显式记忆：SQL 是事实来源，LLM 不负责猜测或恢复档案。"""
import json
import re
import sqlite3
import uuid
from difflib import SequenceMatcher
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")

class MemoryStore:
    def __init__(self, path, user_id, goal_id=None):
        self.path = str(path)
        self.user_id = user_id
        self.goal_id = goal_id
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS profiles (
              user_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (
              id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
              attempt_id TEXT NOT NULL, topic TEXT NOT NULL, score INTEGER,
              is_demo INTEGER NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
              UNIQUE(user_id, attempt_id));
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, id);
            CREATE TABLE IF NOT EXISTS attempts (
              id TEXT PRIMARY KEY, user_id TEXT NOT NULL, status TEXT NOT NULL,
              payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              CHECK(status IN ('draft','submitted','graded')));
            CREATE INDEX IF NOT EXISTS idx_attempts_user ON attempts(user_id, status, updated_at);
            CREATE TABLE IF NOT EXISTS goals (
              id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL,
              timeframe_weeks INTEGER NOT NULL, start_date TEXT NOT NULL,
              weekly_hours INTEGER NOT NULL, subjects TEXT NOT NULL, status TEXT NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_goals_user ON goals(user_id, status, updated_at);
            CREATE TABLE IF NOT EXISTS chapters (
              id INTEGER PRIMARY KEY AUTOINCREMENT, goal_id TEXT NOT NULL, title TEXT NOT NULL,
              planned_week INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
              completed_at TEXT, UNIQUE(goal_id, title));
            CREATE INDEX IF NOT EXISTS idx_chapters_goal ON chapters(goal_id, planned_week, id);
            CREATE TABLE IF NOT EXISTS chapter_sources (
              chapter_id INTEGER NOT NULL, document_id TEXT NOT NULL,
              start_slide INTEGER NOT NULL, end_slide INTEGER NOT NULL,
              UNIQUE(chapter_id, document_id, start_slide, end_slide));
            CREATE INDEX IF NOT EXISTS idx_chapter_sources_chapter ON chapter_sources(chapter_id);
            ''')
            self._add_column(db, "sessions", "goal_id", "TEXT")
            self._add_column(db, "sessions", "chapter_id", "INTEGER")
            self._add_column(db, "attempts", "goal_id", "TEXT")
            self._add_column(db, "attempts", "chapter_id", "INTEGER")
            self._add_column(db, "chapters", "document_id", "TEXT")
            self._add_column(db, "chapters", "start_slide", "INTEGER")
            self._add_column(db, "chapters", "end_slide", "INTEGER")
            self._add_column(db, "chapters", "confirmed", "INTEGER NOT NULL DEFAULT 0")
            self._add_column(db, "chapters", "included", "INTEGER NOT NULL DEFAULT 1")
            self._add_column(db, "chapters", "learning_level", "TEXT NOT NULL DEFAULT '理解'")
            self._add_column(db, "chapters", "estimated_minutes", "INTEGER NOT NULL DEFAULT 45")
            self._add_column(db, "chapters", "mastery_status", "TEXT NOT NULL DEFAULT '未开始'")

    @staticmethod
    def _add_column(db, table, column, declaration):
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def save_profile(self, profile):
        with self.connect() as db:
            db.execute("INSERT INTO profiles VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
                       (self.user_id, json.dumps(profile, ensure_ascii=False), now()))

    def get_profile(self):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM profiles WHERE user_id=?", (self.user_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def create_goal(self, title, timeframe_weeks, start_date, weekly_hours, subjects):
        title = title.strip()
        if not title or not 1 <= timeframe_weeks <= 104 or not 1 <= weekly_hours <= 80:
            raise ValueError("请填写目标名称，并检查周期和每周小时数")
        goal = {"id": str(uuid.uuid4()), "user_id": self.user_id, "title": title,
                "timeframe_weeks": int(timeframe_weeks), "start_date": str(start_date),
                "weekly_hours": int(weekly_hours), "subjects": [x for x in subjects if x]}
        with self.connect() as db:
            db.execute("INSERT INTO goals VALUES (?,?,?,?,?,?,?,?,?,?)", (goal["id"], goal["user_id"], goal["title"],
                       goal["timeframe_weeks"], goal["start_date"], goal["weekly_hours"], json.dumps(goal["subjects"], ensure_ascii=False),
                       "active", now(), now()))
        return goal

    def list_goals(self):
        with self.connect() as db:
            rows = db.execute("""SELECT g.*,COUNT(CASE WHEN c.confirmed=1 AND c.included=1 THEN 1 END) AS knowledge_total,
                COALESCE(SUM(CASE WHEN c.confirmed=1 AND c.included=1 AND c.status='done' THEN 1 ELSE 0 END), 0) AS knowledge_completed
                FROM goals g LEFT JOIN chapters c ON c.goal_id=g.id
                WHERE g.user_id=? AND g.status='active'
                GROUP BY g.id""", (self.user_id,)).fetchall()
        goals = []
        for row in rows:
            goal = dict(row) | {"subjects": json.loads(row["subjects"])}
            goal["is_completed"] = goal["knowledge_total"] > 0 and goal["knowledge_completed"] == goal["knowledge_total"]
            goals.append(goal)
        return sorted(goals, key=lambda goal: (goal["is_completed"], goal["updated_at"]), reverse=False)

    def get_goal(self, goal_id=None):
        goal_id = goal_id or self.goal_id
        if not goal_id:
            return None
        with self.connect() as db:
            row = db.execute("SELECT * FROM goals WHERE id=? AND user_id=? AND status='active'", (goal_id, self.user_id)).fetchone()
        return (dict(row) | {"subjects": json.loads(row["subjects"])}) if row else None

    def delete_goal(self, goal_id):
        """永久移除一个目标及与之关联的章节、测验和学习记录。"""
        with self.connect() as db:
            exists = db.execute("SELECT 1 FROM goals WHERE id=? AND user_id=?", (goal_id, self.user_id)).fetchone()
            if not exists:
                return False
            db.execute("DELETE FROM sessions WHERE user_id=? AND goal_id=?", (self.user_id, goal_id))
            db.execute("DELETE FROM attempts WHERE user_id=? AND goal_id=?", (self.user_id, goal_id))
            db.execute("DELETE FROM chapter_sources WHERE chapter_id IN (SELECT id FROM chapters WHERE goal_id=?)", (goal_id,))
            db.execute("DELETE FROM chapters WHERE goal_id=?", (goal_id,))
            db.execute("DELETE FROM goals WHERE id=? AND user_id=?", (goal_id, self.user_id))
        return True

    def add_chapter(self, title, planned_week):
        if not self.goal_id or not title.strip():
            raise ValueError("请先选择学习目标并填写章节名称")
        goal = self.get_goal()
        if not 1 <= int(planned_week) <= goal["timeframe_weeks"]:
            raise ValueError("章节周次必须在计划周期内")
        with self.connect() as db:
            db.execute("INSERT INTO chapters(goal_id,title,planned_week,status,created_at,confirmed,included) VALUES(?,?,?,'todo',?,1,1)",
                       (self.goal_id, title.strip(), int(planned_week), now()))

    def update_knowledge_point(self, chapter_id, title, learning_level, estimated_minutes, included):
        if learning_level not in {"了解", "理解", "应用"} or not title.strip() or not 10 <= int(estimated_minutes) <= 600:
            raise ValueError("请填写知识点名称，并检查学习层级和预计时长")
        with self.connect() as db:
            db.execute("UPDATE chapters SET title=?,learning_level=?,estimated_minutes=?,included=?,confirmed=1 WHERE id=? AND goal_id=?",
                       (title.strip(), learning_level, int(estimated_minutes), int(bool(included)), chapter_id, self.goal_id))

    def delete_knowledge_point(self, chapter_id):
        with self.connect() as db:
            db.execute("DELETE FROM chapter_sources WHERE chapter_id=?", (chapter_id,))
            db.execute("DELETE FROM chapters WHERE id=? AND goal_id=?", (chapter_id, self.goal_id))

    def merge_knowledge_points(self, source_id, target_id):
        if source_id == target_id:
            return
        with self.connect() as db:
            target = db.execute("SELECT id FROM chapters WHERE id=? AND goal_id=?", (target_id, self.goal_id)).fetchone()
            if not target:
                raise ValueError("请选择同一学习目标内的合并目标")
            rows = db.execute("SELECT document_id,start_slide,end_slide FROM chapter_sources WHERE chapter_id=?", (source_id,)).fetchall()
            for row in rows:
                db.execute("INSERT OR IGNORE INTO chapter_sources(chapter_id,document_id,start_slide,end_slide) VALUES(?,?,?,?)",
                           (target_id, row["document_id"], row["start_slide"], row["end_slide"]))
            db.execute("UPDATE sessions SET chapter_id=? WHERE goal_id=? AND chapter_id=?", (target_id, self.goal_id, source_id))
            db.execute("UPDATE attempts SET chapter_id=? WHERE goal_id=? AND chapter_id=?", (target_id, self.goal_id, source_id))
            db.execute("DELETE FROM chapter_sources WHERE chapter_id=?", (source_id,))
            db.execute("DELETE FROM chapters WHERE id=? AND goal_id=?", (source_id, self.goal_id))

    def confirm_knowledge_plan(self):
        goal = self.get_goal()
        with self.connect() as db:
            rows = db.execute("SELECT id FROM chapters WHERE goal_id=? AND included=1 ORDER BY planned_week,id", (self.goal_id,)).fetchall()
            for index, row in enumerate(rows):
                week = min(goal["timeframe_weeks"], 1 + index * goal["timeframe_weeks"] // max(len(rows), 1))
                db.execute("UPDATE chapters SET confirmed=1,planned_week=? WHERE id=?", (week, row["id"]))

    def update_mastery(self, chapter_id, score, question_feedback):
        labels = [item.get("mastery") for item in question_feedback]
        if score is None:
            mastery = "学习中"
        elif score >= 90 and labels and all(label == "掌握" for label in labels):
            mastery = "已掌握"
        elif score >= 60 and "需复习" not in labels:
            mastery = "基本掌握"
        else:
            mastery = "需复习"
        done = mastery == "已掌握"
        with self.connect() as db:
            db.execute("UPDATE chapters SET mastery_status=?,status=?,completed_at=? WHERE id=? AND goal_id=?",
                       (mastery, "done" if done else "todo", now() if done else None, chapter_id, self.goal_id))
        return mastery

    @staticmethod
    def _knowledge_key(title):
        return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", title.lower())

    def merge_detected_chapters(self, outline):
        """按知识点合并新教材来源；已有知识点的完成状态不被修改。"""
        if not self.goal_id:
            raise ValueError("请先选择学习目标")
        goal = self.get_goal()
        with self.connect() as db:
            existing = [dict(row) for row in db.execute("SELECT * FROM chapters WHERE goal_id=? ORDER BY planned_week,id", (self.goal_id,))]
            for chapter in existing:
                if chapter.get("document_id") and chapter.get("start_slide"):
                    db.execute("INSERT OR IGNORE INTO chapter_sources(chapter_id,document_id,start_slide,end_slide) VALUES(?,?,?,?)",
                               (chapter["id"], chapter["document_id"], chapter["start_slide"], chapter["end_slide"]))
            added = 0
            for item in outline:
                title = item["title"].strip()
                key = self._knowledge_key(title)
                match = next((chapter for chapter in existing if self._knowledge_key(chapter["title"]) == key), None)
                if not match:
                    match = next((chapter for chapter in existing if SequenceMatcher(None, self._knowledge_key(chapter["title"]), key).ratio() >= 0.72), None)
                if match:
                    chapter_id = match["id"]
                else:
                    total = len(existing) + added + 1
                    week = min(goal["timeframe_weeks"], 1 + (total - 1) * goal["timeframe_weeks"] // max(total, 1))
                    cursor = db.execute("INSERT INTO chapters(goal_id,title,planned_week,status,created_at,document_id,start_slide,end_slide) VALUES(?,?,?,'todo',?,?,?,?)",
                                        (self.goal_id, title, week, now(), item["document_id"], item["start_slide"], item["end_slide"]))
                    chapter_id = cursor.lastrowid
                    existing.append({"id": chapter_id, "title": title})
                    added += 1
                db.execute("INSERT OR IGNORE INTO chapter_sources(chapter_id,document_id,start_slide,end_slide) VALUES(?,?,?,?)",
                           (chapter_id, item["document_id"], item["start_slide"], item["end_slide"]))
        return {"added": added, "total": len(existing)}

    def replace_detected_chapters(self, outline):
        """旧接口兼容：新版本不会删除既有进度。"""
        return self.merge_detected_chapters(outline)

    def add_detected_chapters(self, document_id, outline):
        """兼容旧调用：把单份教材大纲替换为当前计划。"""
        return self.merge_detected_chapters([{**item, "document_id": document_id} for item in outline])

    def list_chapters(self, confirmed_only=False):
        if not self.goal_id:
            return []
        with self.connect() as db:
            has_documents = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='course_documents'").fetchone()
            if has_documents:
                condition = " AND c.confirmed=1 AND c.included=1" if confirmed_only else ""
                rows = db.execute("""SELECT c.*,d.original_filename,
                    GROUP_CONCAT(d2.original_filename || '|' || cs.start_slide || '|' || cs.end_slide, '；') AS source_text
                    FROM chapters c
                    LEFT JOIN course_documents d ON c.document_id=d.id
                    LEFT JOIN chapter_sources cs ON cs.chapter_id=c.id
                    LEFT JOIN course_documents d2 ON cs.document_id=d2.id
                    WHERE c.goal_id=?""" + condition + " GROUP BY c.id ORDER BY c.planned_week,c.id", (self.goal_id,)).fetchall()
            else:
                condition = " AND confirmed=1 AND included=1" if confirmed_only else ""
                rows = db.execute("SELECT *,NULL AS original_filename FROM chapters WHERE goal_id=?" + condition + " ORDER BY planned_week,id", (self.goal_id,)).fetchall()
        return [dict(row) for row in rows]

    def complete_chapter(self, chapter_id, completed=True):
        with self.connect() as db:
            db.execute("UPDATE chapters SET status=?,completed_at=? WHERE id=? AND goal_id=?",
                       ("done" if completed else "todo", now() if completed else None, chapter_id, self.goal_id))

    def save_session(self, attempt_id, log, result, answers, explanation, is_demo, model, goal_id=None, chapter_id=None):
        payload = {"log": log, "result": result, "answers": answers,
                   "explanation": explanation, "model": model, "prompt_version": "course-v1"}
        with self.connect() as db:
            cur = db.execute("INSERT OR IGNORE INTO sessions(user_id,attempt_id,topic,score,is_demo,payload,created_at,goal_id,chapter_id) VALUES(?,?,?,?,?,?,?,?,?)",
                             (self.user_id, attempt_id, log["topic"], result["score"], int(is_demo),
                              json.dumps(payload, ensure_ascii=False), now(), goal_id or self.goal_id, chapter_id))
        return cur.rowcount == 1

    def save_attempt(self, attempt):
        """保存草稿。题目、档案和学习记录均作为不可变快照留在 payload 中。"""
        if attempt.get("status") not in {"draft", "submitted", "graded"}:
            raise ValueError("测验状态不合法")
        with self.connect() as db:
            db.execute("""INSERT INTO attempts(id,user_id,status,payload,created_at,updated_at,goal_id,chapter_id) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET status=excluded.status,payload=excluded.payload,updated_at=excluded.updated_at,goal_id=excluded.goal_id,chapter_id=excluded.chapter_id
                WHERE attempts.user_id=excluded.user_id""",
                (attempt["id"], self.user_id, attempt["status"], json.dumps(attempt, ensure_ascii=False), now(), now(),
                 attempt.get("goal_id", self.goal_id), attempt.get("chapter_id")))

    def get_attempt(self, attempt_id):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM attempts WHERE id=? AND user_id=?", (attempt_id, self.user_id)).fetchone()
        return json.loads(row["payload"]) if row else None

    def latest_draft(self):
        with self.connect() as db:
            if self.goal_id:
                row = db.execute("SELECT payload FROM attempts WHERE user_id=? AND goal_id=? AND status IN ('draft','submitted') ORDER BY updated_at DESC LIMIT 1", (self.user_id, self.goal_id)).fetchone()
            else:
                row = db.execute("SELECT payload FROM attempts WHERE user_id=? AND status IN ('draft','submitted') ORDER BY updated_at DESC LIMIT 1", (self.user_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def mark_attempt_graded(self, attempt_id, result, answers, explanation):
        """只允许草稿进入已评分状态，避免重复评分和重复写入。"""
        with self.connect() as db:
            row = db.execute("SELECT payload,status FROM attempts WHERE id=? AND user_id=?", (attempt_id, self.user_id)).fetchone()
            if not row:
                raise ValueError("未找到当前学习者的测验")
            if row["status"] == "graded":
                raise ValueError("该测验已评分，不能重复提交")
            attempt = json.loads(row["payload"])
            attempt.update(status="graded", result=result, answers=answers, explanation=explanation)
            db.execute("UPDATE attempts SET status='graded',payload=?,updated_at=? WHERE id=? AND user_id=?",
                       (json.dumps(attempt, ensure_ascii=False), now(), attempt_id, self.user_id))
        return attempt

    def recent_sessions(self, limit=6):
        limit = max(1, min(20, int(limit)))
        with self.connect() as db:
            if self.goal_id:
                rows = db.execute("SELECT id,topic,score,is_demo,payload,created_at FROM sessions WHERE user_id=? AND goal_id=? ORDER BY id DESC LIMIT ?", (self.user_id, self.goal_id, limit)).fetchall()
            else:
                rows = db.execute("SELECT id,topic,score,is_demo,payload,created_at FROM sessions WHERE user_id=? ORDER BY id DESC LIMIT ?", (self.user_id, limit)).fetchall()
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]

    def topic_stats(self):
        with self.connect() as db:
            clause, params = ("user_id=? AND goal_id=?", (self.user_id, self.goal_id)) if self.goal_id else ("user_id=?", (self.user_id,))
            rows = db.execute(f"SELECT topic,COUNT(*) AS attempts,ROUND(AVG(score),1) AS average_score FROM sessions WHERE {clause} AND is_demo=0 AND score IS NOT NULL GROUP BY topic ORDER BY average_score ASC,topic LIMIT 20", params).fetchall()
        return [dict(r) for r in rows]

    def export(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM sessions WHERE user_id=? ORDER BY id", (self.user_id,)).fetchall()
        return {"user_id": self.user_id, "profile": self.get_profile(), "sessions": [dict(r) for r in rows]}

    def clear_user(self):
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE user_id=?", (self.user_id,))
            db.execute("DELETE FROM profiles WHERE user_id=?", (self.user_id,))
            db.execute("DELETE FROM attempts WHERE user_id=?", (self.user_id,))
            db.execute("DELETE FROM chapters WHERE goal_id IN (SELECT id FROM goals WHERE user_id=?)", (self.user_id,))
            db.execute("DELETE FROM goals WHERE user_id=?", (self.user_id,))
