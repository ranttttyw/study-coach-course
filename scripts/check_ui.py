import json
import os
import sys
import tempfile
from pathlib import Path

from streamlit.testing.v1 import AppTest
from knowledge_store import KnowledgeStore
from memory_utils import MemoryStore

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

with tempfile.TemporaryDirectory() as directory:
    os.environ["COACH_MODE"] = "demo"
    os.environ["COACH_DB_PATH"] = str(Path(directory) / "ui.db")
    at = AppTest.from_file(str(root / "app.py"), default_timeout=20).run()
    assert not at.exception, at.exception
    next(item for item in at.text_input if item.label == "新用户名").input("learner001")
    next(item for item in at.text_input if item.label == "新密码 至少 8 个字符").input("password123")
    next(item for item in at.text_input if item.label == "确认密码").input("password123")
    next(item for item in at.button if item.label == "创建账号").click().run()
    assert not at.exception, at.exception
    next(item for item in at.text_input if item.label == "目标名称").input("掌握 Agent Memory")
    next(item for item in at.button if item.label == "创建学习目标").click().run()
    assert not at.exception, at.exception
    goal_id = at.session_state["active_goal_id"]
    memory = MemoryStore(os.environ["COACH_DB_PATH"], at.session_state["user"]["id"], goal_id)
    materials = KnowledgeStore(os.environ["COACH_DB_PATH"], at.session_state["user"]["id"], goal_id)
    document_id = materials.create_document("memory.pptx", b"test material", "/tmp/memory.pptx", 10)
    materials.mark_ready(document_id, [{"slide_number": 1, "title": "工作记忆", "body_text": "工作记忆用于暂时保持信息。", "speaker_notes": ""}])
    memory.merge_detected_chapters([{"title": "工作记忆", "document_id": document_id, "start_slide": 1, "end_slide": 1}])
    memory.confirm_knowledge_plan()
    at.run()
    chapter = next(item for item in at.selectbox if item.label == "关联章节")
    chapter.select(memory.list_chapters()[0]["id"]).run()
    next(item for item in at.button if item.label == "生成本次题目").click().run()
    assert not at.exception, at.exception
    attempt = at.session_state["attempt"]
    original = attempt["quiz"]["quiz"]
    assert len(original) == 3 and attempt["goal_id"]
    for area in at.text_area:
        if area.key and str(area.key).startswith("answer_"):
            area.input("这是我针对原题的解释、例子和验证方法。")
    next(area for area in at.text_area if area.label.startswith("请用自己的话概括")).input("工作记忆在会话中，长期档案与事件写入数据库。")
    next(item for item in at.button if item.label == "提交作答并保存反馈").click().run()
    assert not at.exception, at.exception
    assert at.session_state["attempt"]["status"] == "graded"
    at.chat_input[0].set_value("我最近学了什么？").run()
    assert not at.exception, at.exception
    assert at.session_state["latest_sources"]
    print(json.dumps({"login":"PASS","goal":"PASS","draft_and_grade":"PASS","progress_sources":"PASS"}, ensure_ascii=False))
