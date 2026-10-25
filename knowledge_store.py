"""教材资料的确定性存储与可解释关键词检索。"""
import hashlib
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _terms(query):
    ascii_terms = re.findall(r"[A-Za-z0-9_]{2,}", query.lower())
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", query))
    pairs = [chinese[i:i + 2] for i in range(max(0, len(chinese) - 1))]
    return list(dict.fromkeys(ascii_terms + pairs + ([chinese] if len(chinese) >= 2 else [])))[:20]


class KnowledgeStore:
    def __init__(self, path, user_id, goal_id=None):
        self.path, self.user_id, self.goal_id = str(path), user_id, goal_id
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS course_documents (
              id TEXT PRIMARY KEY, user_id TEXT NOT NULL, original_filename TEXT NOT NULL,
              stored_filename TEXT NOT NULL, sha256 TEXT NOT NULL, status TEXT NOT NULL,
              slide_count INTEGER NOT NULL DEFAULT 0, error_message TEXT, created_at TEXT NOT NULL,
              UNIQUE(user_id, sha256));
            CREATE TABLE IF NOT EXISTS document_slides (
              id INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL,
              slide_number INTEGER NOT NULL, title TEXT NOT NULL, body_text TEXT NOT NULL,
              speaker_notes TEXT NOT NULL, search_text TEXT NOT NULL,
              UNIQUE(document_id, slide_number));
            CREATE INDEX IF NOT EXISTS idx_documents_user ON course_documents(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_slides_document ON document_slides(document_id, slide_number);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(course_documents)")}
            if "goal_id" not in columns:
                db.execute("ALTER TABLE course_documents ADD COLUMN goal_id TEXT")
            db.execute("CREATE INDEX IF NOT EXISTS idx_documents_goal ON course_documents(user_id, goal_id, created_at)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def count_documents(self):
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM course_documents WHERE user_id=? AND goal_id IS ?", (self.user_id, self.goal_id)).fetchone()[0]

    def create_document(self, filename, data, stored_filename, max_documents):
        if self.count_documents() >= max_documents:
            raise ValueError("已达到当前学习者的教材数量上限")
        digest = hashlib.sha256(data).hexdigest()
        document_id = str(uuid.uuid4())
        try:
            with self.connect() as db:
                db.execute("INSERT INTO course_documents(id,user_id,original_filename,stored_filename,sha256,status,slide_count,error_message,created_at,goal_id) VALUES(?,?,?,?,?,'processing',0,NULL,?,?)",
                           (document_id, self.user_id, filename, stored_filename, digest, _now(), self.goal_id))
        except sqlite3.IntegrityError as exc:
            raise ValueError("这份教材已上传，无需重复添加") from exc
        return document_id

    def mark_ready(self, document_id, slides):
        with self.connect() as db:
            for slide in slides:
                search = "\n".join([slide["title"], slide["body_text"], slide["speaker_notes"]])
                db.execute("INSERT INTO document_slides(document_id,slide_number,title,body_text,speaker_notes,search_text) VALUES(?,?,?,?,?,?)",
                           (document_id, slide["slide_number"], slide["title"], slide["body_text"], slide["speaker_notes"], search))
            db.execute("UPDATE course_documents SET status='ready',slide_count=? WHERE id=? AND user_id=?",
                       (len(slides), document_id, self.user_id))

    def mark_failed(self, document_id, message):
        with self.connect() as db:
            db.execute("UPDATE course_documents SET status='failed',error_message=? WHERE id=? AND user_id=?",
                       (message[:500], document_id, self.user_id))

    def list_documents(self):
        with self.connect() as db:
            rows = db.execute("SELECT id,original_filename,status,slide_count,error_message,created_at FROM course_documents WHERE user_id=? AND goal_id IS ? ORDER BY created_at DESC", (self.user_id, self.goal_id)).fetchall()
        return [dict(row) for row in rows]

    def ready_pages(self):
        """返回当前目标全部已解析教材的页面，用于重建统一学习计划。"""
        with self.connect() as db:
            rows = db.execute("""SELECT d.id AS document_id,d.original_filename,s.slide_number,s.title,s.body_text,s.speaker_notes
                FROM document_slides s JOIN course_documents d ON s.document_id=d.id
                WHERE d.user_id=? AND d.goal_id IS ? AND d.status='ready'
                ORDER BY d.created_at,s.slide_number""", (self.user_id, self.goal_id)).fetchall()
        return [dict(row) for row in rows]

    def chapter_material(self, chapter_id, max_pages=12, chars_per_page=900):
        """取得一个知识点关联的教材证据，供出题和逐题反馈使用。"""
        with self.connect() as db:
            rows = db.execute("""SELECT d.original_filename,s.slide_number,s.title,s.body_text,s.speaker_notes
                FROM chapter_sources cs
                JOIN chapters c ON c.id=cs.chapter_id
                JOIN course_documents d ON d.id=cs.document_id
                JOIN document_slides s ON s.document_id=cs.document_id
                WHERE cs.chapter_id=? AND c.goal_id=? AND d.user_id=? AND d.goal_id IS ?
                  AND s.slide_number BETWEEN cs.start_slide AND cs.end_slide
                ORDER BY d.created_at,s.slide_number LIMIT ?""",
                (chapter_id, self.goal_id, self.user_id, self.goal_id, max_pages)).fetchall()
            if not rows:
                rows = db.execute("""SELECT d.original_filename,s.slide_number,s.title,s.body_text,s.speaker_notes
                    FROM chapters c JOIN course_documents d ON d.id=c.document_id
                    JOIN document_slides s ON s.document_id=c.document_id
                    WHERE c.id=? AND c.goal_id=? AND d.user_id=? AND d.goal_id IS ?
                      AND s.slide_number BETWEEN c.start_slide AND c.end_slide
                    ORDER BY s.slide_number LIMIT ?""",
                    (chapter_id, self.goal_id, self.user_id, self.goal_id, max_pages)).fetchall()
        return [{"source": f"《{row['original_filename']}》第 {row['slide_number']} 页",
                 "title": row["title"],
                 "text": "\n".join([row["body_text"], row["speaker_notes"]]).strip()[:chars_per_page]}
                for row in rows]

    def delete_document(self, document_id):
        with self.connect() as db:
            row = db.execute("SELECT stored_filename FROM course_documents WHERE id=? AND user_id=? AND goal_id IS ?", (document_id, self.user_id, self.goal_id)).fetchone()
            if not row:
                return None
            db.execute("DELETE FROM document_slides WHERE document_id=?", (document_id,))
            db.execute("DELETE FROM course_documents WHERE id=? AND user_id=? AND goal_id IS ?", (document_id, self.user_id, self.goal_id))
        return row["stored_filename"]

    def delete_goal_documents(self, goal_id):
        """返回已删除教材的本地路径，供界面安全清理文件。"""
        with self.connect() as db:
            rows = db.execute("SELECT id,stored_filename FROM course_documents WHERE user_id=? AND goal_id=?", (self.user_id, goal_id)).fetchall()
            db.execute("DELETE FROM document_slides WHERE document_id IN (SELECT id FROM course_documents WHERE user_id=? AND goal_id=?)", (self.user_id, goal_id))
            db.execute("DELETE FROM course_documents WHERE user_id=? AND goal_id=?", (self.user_id, goal_id))
        return [row["stored_filename"] for row in rows]

    def search(self, query, limit=3, excerpt_chars=1200):
        terms = _terms(query)
        if not terms:
            return []
        with self.connect() as db:
            rows = db.execute("""SELECT d.id AS document_id,d.original_filename,s.slide_number,s.title,s.search_text
                FROM document_slides s JOIN course_documents d ON s.document_id=d.id
                WHERE d.user_id=? AND d.goal_id IS ? AND d.status='ready'""", (self.user_id, self.goal_id)).fetchall()
        ranked = []
        for row in rows:
            title, text = row["title"].lower(), row["search_text"].lower()
            score = sum((5 if term in title else 0) + (1 if term in text else 0) for term in terms)
            if score:
                excerpt = row["search_text"].replace("\n", " ")[:excerpt_chars]
                ranked.append((score, dict(document_id=row["document_id"], filename=row["original_filename"],
                                            slide_number=row["slide_number"], title=row["title"], excerpt=excerpt,
                                            source=f"《{row['original_filename']}》第 {row['slide_number']} 页")))
        return [item for _, item in sorted(ranked, key=lambda x: (-x[0], x[1]["slide_number"]))[:limit]]

    def clear_user(self):
        with self.connect() as db:
            rows = db.execute("SELECT stored_filename FROM course_documents WHERE user_id=?", (self.user_id,)).fetchall()
            db.execute("DELETE FROM document_slides WHERE document_id IN (SELECT id FROM course_documents WHERE user_id=?)", (self.user_id,))
            db.execute("DELETE FROM course_documents WHERE user_id=?", (self.user_id,))
        return [row["stored_filename"] for row in rows]
