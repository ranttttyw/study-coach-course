"""不输出密钥的环境检查。加 --online 才发送真实模型请求。"""
import argparse
import pathlib
import sys
import tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from config import Settings
from llm_gateway import CoachModel
from memory_utils import MemoryStore

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--online", action="store_true")
    args = parser.parse_args()
    s = Settings.load()
    print("Python:", sys.version.split()[0])
    print("模式:", s.mode, "模型:", s.model)
    print("API Key:", "已设置" if s.api_key else "未设置")
    with tempfile.TemporaryDirectory() as d:
        store = MemoryStore(pathlib.Path(d) / "check.db", "check")
        store.save_profile({"check": True})
        assert store.get_profile() == {"check": True}
    print("SQLite临时读写: 通过")
    if args.online:
        if s.mode != "online":
            raise SystemExit("先设置 COACH_MODE=online")
        m = CoachModel(s)
        from study_graph import LearnerProfile, StudyLog, run_initial_verification
        p = LearnerProfile(name="配置检查", main_goal="理解记忆", timeframe="1周", weekly_hours=2)
        log = StudyLog(topic="工作记忆", duration_minutes=5, resource_type="文档", perceived_difficulty="中等")
        result = run_initial_verification(p, log, m, [{"source": "《示例教材》第 1 页", "title": "工作记忆", "text": "工作记忆用于暂时保持并处理当前任务所需的信息。"}])
        print("真实模型出题与JSON校验: 通过; 题数:", len(result.quiz))
        print("注意: 这一步尚未验证在线工具调用、评分或 Memori。")
    else:
        print("未发送模型请求。在线检查: uv run python scripts/doctor.py --online")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("检查失败类型:", type(exc).__name__)
        raise SystemExit("检查未通过。请检查模型名、API Key 权限/额度、网络与数据库权限；不要公开密钥或完整错误请求。")
