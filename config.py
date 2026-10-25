"""课程版配置。模型服务参数只在服务端读取，不从学员表单写入进程环境。"""
import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

@dataclass(frozen=True)
class Settings:
    mode: str = "demo"
    api_key: str = ""
    base_url: str = ""
    model: str = "gpt-4o-mini"
    db_path: str = str(ROOT / "data" / "study_coach.db")
    recent_limit: int = 6
    max_tool_steps: int = 4
    upload_dir: str = str(ROOT / "data" / "uploads")
    max_ppt_mb: int = 30
    max_ppt_slides: int = 100
    max_documents_per_user: int = 10
    material_result_limit: int = 3
    material_chars_per_result: int = 1200

    @classmethod
    def load(cls, secrets=None):
        def value(k, default=""):
            return str((secrets or {}).get(k, os.getenv(k, default)))
        mode = value("COACH_MODE", "demo").strip().lower()
        if mode not in {"demo", "online"}:
            raise ValueError("COACH_MODE 只能是 demo 或 online")
        raw_path = Path(value("COACH_DB_PATH", "data/study_coach.db"))
        path = raw_path if raw_path.is_absolute() else ROOT / raw_path
        s = cls(mode=mode, api_key=value("OPENAI_API_KEY"),
                base_url=value("OPENAI_BASE_URL").strip(), model=value("COACH_MODEL", "gpt-4o-mini"),
                db_path=str(path), recent_limit=int(value("COACH_RECENT_LIMIT", "6")),
                max_tool_steps=int(value("COACH_MAX_TOOL_STEPS", "4")),
                upload_dir=str(Path(value("COACH_UPLOAD_DIR", "data/uploads")) if Path(value("COACH_UPLOAD_DIR", "data/uploads")).is_absolute() else ROOT / value("COACH_UPLOAD_DIR", "data/uploads")),
                max_ppt_mb=int(value("COACH_MAX_PPT_MB", "30")),
                max_ppt_slides=int(value("COACH_MAX_PPT_SLIDES", "100")),
                max_documents_per_user=int(value("COACH_MAX_DOCUMENTS_PER_USER", "10")),
                material_result_limit=int(value("COACH_MATERIAL_RESULT_LIMIT", "3")),
                material_chars_per_result=int(value("COACH_MATERIAL_CHARS_PER_RESULT", "1200")))
        if s.mode == "online" and not s.api_key:
            raise ValueError("在线模式需要设置 OPENAI_API_KEY")
        if not 1 <= s.recent_limit <= 20 or not 1 <= s.max_tool_steps <= 8:
            raise ValueError("近期记录数应为 1 到 20，工具轮数应为 1 到 8")
        if not 1 <= s.max_ppt_mb <= 100 or not 1 <= s.max_ppt_slides <= 500 or not 1 <= s.max_documents_per_user <= 100:
            raise ValueError("教材上传配置超出允许范围")
        return s
