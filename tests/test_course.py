import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from pydantic import ValidationError
from config import Settings
from llm_gateway import CoachModel
from memory_utils import MemoryStore
from coach_agent import answer_progress
from study_graph import LearnerProfile, StudyLog, run_initial_verification, run_full_evaluation

class FakeModel:
    def __init__(self):
        self.tasks = []
        self.settings = SimpleNamespace(mode="online")
    def ask_json(self, system, payload):
        self.tasks.append(payload)
        if payload["task"] == "generate":
            return {"quiz": [{"question": f"原题{i}", "reference_answer": f"参考{i}", "sources": ["《教材》第 1 页"]} for i in range(3)], "explanation_prompt": "解释原因"}
        return {"score": 78, "feedback": "概念需要补充", "next_step_recommendation": "做一个例子",
                "question_feedback": [{"mastery": "部分掌握", "feedback": "补充细节"} for _ in payload["quiz"]]}

class CourseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test.db"
        self.store = MemoryStore(self.path, "alice")
        self.profile = LearnerProfile(name="A", main_goal="学习记忆", timeframe="4周", weekly_hours=4)
        self.log = StudyLog(topic="记忆", duration_minutes=30, resource_type="代码", perceived_difficulty="中等")
    def tearDown(self):
        self.tmp.cleanup()
    def result(self):
        return {"score": 78, "feedback": "反馈", "next_step_recommendation": "练习", "quiz": []}
    def test_generate_does_not_grade(self):
        m = FakeModel()
        run_initial_verification(self.profile, self.log, m, [{"source": "《教材》第 1 页", "text": "内容"}])
        self.assertEqual([x["task"] for x in m.tasks], ["generate"])
    def test_grade_uses_original_quiz_without_regeneration(self):
        m = FakeModel()
        initial = run_initial_verification(self.profile, self.log, m, [{"source": "《教材》第 1 页", "text": "内容"}])
        run_full_evaluation(self.profile, self.log, initial.quiz, ["a", "b", "c"], "复述", m)
        self.assertEqual([x["task"] for x in m.tasks], ["generate", "evaluate"])
        self.assertEqual(m.tasks[1]["quiz"], [q.model_dump() for q in initial.quiz])
    def test_blank_answers_rejected(self):
        m = FakeModel(); q = run_initial_verification(self.profile, self.log, m, [{"source": "《教材》第 1 页", "text": "内容"}]).quiz
        with self.assertRaises(ValueError):
            run_full_evaluation(self.profile, self.log, q, ["", "a", "b"], "解释", m)
    def test_bad_model_score_rejected(self):
        m = FakeModel(); q = run_initial_verification(self.profile, self.log, m, [{"source": "《教材》第 1 页", "text": "内容"}]).quiz
        m.ask_json = lambda *a: {"score": 300, "feedback": "x", "next_step_recommendation": "y", "question_feedback": [{"mastery": "掌握", "feedback": "x"}] * 3}
        with self.assertRaises(ValidationError):
            run_full_evaluation(self.profile, self.log, q, ["a", "b", "c"], "解释", m)
    def test_profile_survives_new_store(self):
        self.store.save_profile(self.profile.model_dump())
        self.assertEqual(MemoryStore(self.path, "alice").get_profile()["name"], "A")
    def test_user_filter_and_deletion_scope(self):
        self.store.save_profile({"name": "A"})
        bob = MemoryStore(self.path, "bob"); bob.save_profile({"name": "B"})
        self.store.save_session("same", self.log.model_dump(), self.result(), [], "", False, "fake")
        self.assertEqual(bob.recent_sessions(), [])
        self.store.clear_user()
        self.assertEqual(bob.get_profile(), {"name": "B"})
        self.assertEqual(self.store.recent_sessions(), [])
    def test_duplicate_submit_is_idempotent(self):
        args = ("id", self.log.model_dump(), self.result(), [], "", False, "fake")
        self.assertTrue(self.store.save_session(*args))
        self.assertFalse(self.store.save_session(*args))
        self.assertEqual(len(self.store.recent_sessions()), 1)
    def test_demo_does_not_affect_score_stats(self):
        self.store.save_session("a", self.log.model_dump(), self.result(), [], "", True, "demo")
        self.assertEqual(self.store.topic_stats(), [])
    def test_demo_has_no_score(self):
        m = CoachModel(Settings(mode="demo"))
        q = run_initial_verification(self.profile, self.log, m, [{"source": "《教材》第 1 页", "text": "内容"}]).quiz
        r = run_full_evaluation(self.profile, self.log, q, ["a", "b", "c"], "说明", m)
        self.assertIsNone(r.score)
    def test_demo_agent_returns_visible_evidence(self):
        out = answer_progress("目标？", [], CoachModel(Settings(mode="demo")), self.store)
        self.assertEqual(len(out["trace"]), 3)
        self.assertIn("演示模式", out["answer"])

class FakeCompletions:
    def __init__(self): self.requests = []
    def create(self, **kwargs):
        self.requests.append(kwargs)
        if len(self.requests) == 1:
            call = SimpleNamespace(id="call1", function=SimpleNamespace(name="get_profile", arguments="{}"))
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[call],
                model_dump=lambda **k: {"role": "assistant", "tool_calls": [{"id": "call1", "type": "function", "function": {"name": "get_profile", "arguments": "{}"}}]}))])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=None, content="目标是学习记忆"))])

class AgentLoopTests(unittest.TestCase):
    def test_tool_result_is_returned_to_model(self):
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d)/"db", "a"); store.save_profile({"main_goal": "学习记忆"})
            f = FakeCompletions()
            model = SimpleNamespace(settings=Settings(mode="online"), client=SimpleNamespace(chat=SimpleNamespace(completions=f)))
            out = answer_progress("目标是什么", [], model, store)
            self.assertEqual(out["trace"][0]["tool"], "get_profile")
            self.assertTrue(any(m["role"] == "tool" for m in f.requests[1]["messages"]))
            self.assertEqual(out["answer"], "目标是学习记忆")

    def test_missing_required_tool_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d)/"db", "a")
            f = SimpleNamespace(create=lambda **k: SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(tool_calls=None, content="我猜你的目标是考研"))]))
            model = SimpleNamespace(settings=Settings(mode="online"), client=SimpleNamespace(chat=SimpleNamespace(completions=f)))
            with self.assertRaises(ValueError):
                answer_progress("目标是什么", [], model, store)

if __name__ == "__main__": unittest.main()
