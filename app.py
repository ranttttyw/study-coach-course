"""Streamlit 中文课程入口：登录、测验、教材检索和可检查记忆。"""
import json
import uuid
from pathlib import Path

import streamlit as st

from auth import AuthStore
from coach_agent import answer_progress
from config import Settings
from knowledge_store import KnowledgeStore
from llm_gateway import CoachModel
from memory_utils import MemoryStore
from outline_generator import create_chapter_outline
from pdf_parser import parse_pdf
from ppt_parser import parse_pptx
from study_graph import LearnerProfile, QuizQuestion, StudyLog, run_full_evaluation, run_initial_verification


def model_error_message(exc, settings):
    """诊断不包含 API Key、请求正文或模型返回原文。"""
    kind = type(exc).__name__
    hints = {
        "AuthenticationError": "API Key 无效、已撤销，或当前项目没有权限。",
        "PermissionDeniedError": "API Key 没有访问该模型的权限。",
        "NotFoundError": "模型名或 OPENAI_BASE_URL 不正确。",
        "RateLimitError": "额度不足、达到限速，或账户未启用 API 计费。",
        "APIConnectionError": "无法连接模型服务，请检查网络或 OPENAI_BASE_URL。",
        "APITimeoutError": "模型服务响应超时，请稍后重试。",
    }
    return f"模型请求失败（{kind}）。模型：{settings.model}。{hints.get(kind, '请运行 scripts/doctor.py --online 检查配置；不要在聊天中发送 API Key。')}"


st.set_page_config(page_title="学习教练 Agent", layout="wide")
st.title("学习教练 Agent")
st.caption("登录后建立档案 → 学习与作答 → 教材检索 → 根据证据复习")
try:
    secrets = dict(st.secrets)
except Exception:
    secrets = {}
try:
    settings = Settings.load(secrets)
    model = CoachModel(settings)
except Exception:
    st.error("配置初始化失败。请检查 .env 或部署平台 Secrets，参阅 README 故障排查。")
    st.stop()

auth = AuthStore(settings.db_path)
if "user" not in st.session_state:
    st.subheader("登录学习空间")
    login_tab, register_tab = st.tabs(["登录", "注册"])
    with login_tab:
        with st.form("login"):
            username = st.text_input("用户名", max_chars=50)
            password = st.text_input("密码", type="password", max_chars=128)
            submitted = st.form_submit_button("登录")
        if submitted:
            user = auth.authenticate(username, password)
            if user:
                st.session_state.user = user
                st.rerun()
            st.error("用户名或密码不正确")
    with register_tab:
        with st.form("register"):
            username = st.text_input("新用户名", max_chars=50)
            password = st.text_input("新密码 至少 8 个字符", type="password", max_chars=128)
            confirm = st.text_input("确认密码", type="password", max_chars=128)
            submitted = st.form_submit_button("创建账号")
        if submitted:
            if password != confirm:
                st.error("两次输入的密码不一致")
            else:
                try:
                    st.session_state.user = auth.register(username, password)
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
    st.caption("本地教学版账号仅用于本机访问隔离；部署到公网前应接入经过验证的身份提供方。")
    st.stop()

user = st.session_state.user
if "messages" not in st.session_state:
    st.session_state.messages = []
for key in ["attempt", "latest_trace", "latest_sources"]:
    st.session_state.setdefault(key, None)

account_store = MemoryStore(settings.db_path, user["id"])
goals = account_store.list_goals()
goal_by_id = {goal["id"]: goal for goal in goals}
# A newly-created goal is selected on the next rerun.  This must happen before
# the sidebar selectbox is created; Streamlit does not permit changing a widget
# key after that widget has been instantiated in the same run.
pending_goal_id = st.session_state.pop("pending_goal_id", None)
if pending_goal_id in goal_by_id:
    st.session_state.active_goal_id = pending_goal_id
elif st.session_state.get("active_goal_id") not in goal_by_id:
    st.session_state.active_goal_id = goals[0]["id"] if goals else None

with st.sidebar:
    st.subheader("当前学习空间")
    st.write("已登录：" + user["username"])
    st.write("当前模式：" + ("离线流程演示" if settings.mode == "demo" else "在线模型"))
    if goals:
        selected_goal_id = st.selectbox("当前学习目标", options=list(goal_by_id), format_func=lambda value: goal_by_id[value]["title"], key="active_goal_id")
    else:
        selected_goal_id = None
        st.info("请先在“学习目标”创建第一项计划。")
    st.caption("课件、学习记录和测验均绑定到当前账号与所选学习目标。")
    if st.button("退出登录"):
        for key in ["user", "attempt", "messages", "latest_trace", "latest_sources"]:
            st.session_state.pop(key, None)
        st.rerun()
    if st.button("清空当前页面显示"):
        for key in ["attempt", "messages", "latest_trace", "latest_sources"]:
            st.session_state.pop(key, None)
        st.rerun()

active_goal = goal_by_id.get(selected_goal_id)
store = MemoryStore(settings.db_path, user["id"], selected_goal_id)
knowledge = KnowledgeStore(settings.db_path, user["id"], selected_goal_id)


def choose_goal_for_section(section_key, label):
    """在功能区显式选择操作范围，确认后安全切换学习空间。"""
    if not goals:
        return None
    key = f"{section_key}_goal_choice"
    if st.session_state.get(key) not in goal_by_id:
        st.session_state[key] = selected_goal_id
    left, right = st.columns([4, 1])
    with left:
        choice = st.selectbox(label, options=list(goal_by_id), format_func=lambda value: goal_by_id[value]["title"], key=key)
    with right:
        st.write("")
        if st.button("切换", key=f"switch_{section_key}") and choice != selected_goal_id:
            st.session_state.pending_goal_id = choice
            st.session_state.attempt = None
            st.session_state.messages = []
            st.session_state.latest_trace = None
            st.session_state.latest_sources = None
            st.rerun()
    return choice

profile_data = store.get_profile()
if active_goal:
    profile = LearnerProfile(name=(profile_data or {}).get("name", user["username"]), main_goal=active_goal["title"],
        timeframe=f"{active_goal['timeframe_weeks']}周", weekly_hours=active_goal["weekly_hours"],
        subjects=active_goal["subjects"], preferred_formats=(profile_data or {}).get("preferred_formats", []))
else:
    profile = None
if settings.mode == "demo":
    st.info("当前为离线流程演示：不调用模型、不产生真实评分；在线模式见 README。")

tabs = st.tabs(["学习目标", "学习与测验", "教材资料", "进度问答", "记忆检查"])

with tabs[0]:
    st.subheader("学习目标与章节计划")
    if notice := st.session_state.pop("knowledge_notice", None):
        st.success(notice)
    if goals:
        st.caption(f"你已有 {len(goals)} 个学习目标。进行中的目标排在前面，全部完成的目标排在最下方。")
        for goal_item in goals:
            total = goal_item["knowledge_total"]
            completed_count = goal_item["knowledge_completed"]
            state = "已完成" if goal_item["is_completed"] else "进行中"
            with st.container(border=True):
                left, middle, right = st.columns([5, 2, 2])
                with left:
                    current = " · 当前查看" if goal_item["id"] == selected_goal_id else ""
                    st.write(f"**{goal_item['title']}** · {state}{current}")
                    st.caption(f"{goal_item['timeframe_weeks']} 周 · 每周 {goal_item['weekly_hours']} 小时 · 知识点 {completed_count}/{total} 已完成")
                    st.progress(completed_count / total if total else 0)
                with middle:
                    if st.button("切换到此目标", key=f"overview_switch_{goal_item['id']}") and goal_item["id"] != selected_goal_id:
                        st.session_state.pending_goal_id = goal_item["id"]
                        st.session_state.attempt = None
                        st.session_state.messages = []
                        st.rerun()
                with right:
                    confirm_delete = st.checkbox("确认删除", key=f"confirm_delete_goal_{goal_item['id']}")
                    if st.button("删除目标", key=f"delete_goal_{goal_item['id']}", disabled=not confirm_delete):
                        paths = KnowledgeStore(settings.db_path, user["id"]).delete_goal_documents(goal_item["id"])
                        account_store.delete_goal(goal_item["id"])
                        for stored_path in paths:
                            Path(stored_path).unlink(missing_ok=True)
                        remaining = [goal["id"] for goal in goals if goal["id"] != goal_item["id"]]
                        st.session_state.pending_goal_id = remaining[0] if remaining else None
                        st.session_state.attempt = None
                        st.session_state.messages = []
                        st.session_state.latest_trace = None
                        st.session_state.latest_sources = None
                        st.rerun()
    profile_data = profile_data or {}
    with st.expander("个人学习偏好", expanded=not profile_data):
        with st.form("profile"):
            name = st.text_input("姓名或昵称", value=profile_data.get("name", "学员A"), max_chars=80)
            formats = st.text_input("偏好形式 使用逗号分隔", value="，".join(profile_data.get("preferred_formats", ["代码练习", "图解"])))
            save = st.form_submit_button("保存个人偏好")
        if save:
            store.save_profile({"name": name.strip(), "preferred_formats": [x.strip() for x in formats.replace("，", ",").split(",") if x.strip()]})
            st.success("个人偏好已保存。")
    with st.expander("新建学习目标", expanded=not goals):
        with st.form("goal"):
            title = st.text_input("目标名称", placeholder="例如：四周掌握 Agent Memory")
            left, right = st.columns(2)
            weeks = left.number_input("计划周期 周", min_value=1, max_value=104, value=4)
            start = right.date_input("开始日期")
            hours = left.number_input("每周学习小时", min_value=1, max_value=80, value=6)
            subjects = st.text_input("目标主题 使用中文或英文逗号分隔", placeholder="Memory，上下文工程，LangGraph")
            create = st.form_submit_button("创建学习目标")
        if create:
            try:
                goal = account_store.create_goal(title, weeks, start, hours, [x.strip() for x in subjects.replace("，", ",").split(",") if x.strip()])
                st.session_state.pending_goal_id = goal["id"]
                st.session_state.attempt = None
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
    if active_goal:
        candidates = store.list_chapters()
        unconfirmed = [chapter for chapter in candidates if not chapter.get("confirmed")]
        with st.expander("知识点确认", expanded=bool(unconfirmed)):
            st.caption("教材识别结果不会直接成为计划。点击“保存并确认”后，该知识点才会纳入正式计划。")
            if not candidates:
                st.info("先在“教材资料”上传并解析教材，系统会在这里列出候选知识点。")
            for candidate in candidates:
                with st.form(f"knowledge_{candidate['id']}"):
                    st.write(f"**{'待确认' if not candidate.get('confirmed') else '已纳入计划'}** · {candidate['title']}")
                    new_title = st.text_input("知识点名称", value=candidate["title"], key=f"title_{candidate['id']}")
                    c1, c2 = st.columns(2)
                    level = c1.selectbox("达成标准", ["了解", "理解", "应用"], index=["了解", "理解", "应用"].index(candidate.get("learning_level", "理解")), key=f"level_{candidate['id']}")
                    minutes = c2.number_input("预计学习分钟", min_value=10, max_value=600, value=int(candidate.get("estimated_minutes", 45)), key=f"minutes_{candidate['id']}")
                    included = st.checkbox("纳入本次学习目标", value=bool(candidate.get("included", 1)), key=f"included_{candidate['id']}")
                    targets = [chapter for chapter in candidates if chapter["id"] != candidate["id"]]
                    merge_target = st.selectbox("合并到已有知识点（可选）", [None] + [chapter["id"] for chapter in targets],
                                               format_func=lambda value: "不合并" if value is None else next(chapter["title"] for chapter in targets if chapter["id"] == value), key=f"merge_{candidate['id']}")
                    save, merge, remove = st.columns(3)
                    save_clicked = save.form_submit_button("更新设置" if candidate.get("confirmed") else "保存并确认")
                    merge_clicked = merge.form_submit_button("合并")
                    delete_clicked = remove.form_submit_button("删除")
                if save_clicked:
                    store.update_knowledge_point(candidate["id"], new_title, level, minutes, included)
                    st.session_state.knowledge_notice = f"已保存“{new_title.strip()}”并纳入学习计划。" if included else f"已保存“{new_title.strip()}”，但未纳入本次学习目标。"
                    st.rerun()
                if merge_clicked:
                    if merge_target is None:
                        st.error("请先选择要合并到的知识点。")
                    else:
                        store.merge_knowledge_points(candidate["id"], merge_target)
                        st.rerun()
                if delete_clicked:
                    store.delete_knowledge_point(candidate["id"])
                    st.rerun()
            if candidates and st.button("按计划周期重新安排已确认知识点"):
                store.confirm_knowledge_plan()
                st.success("已按计划周期重新安排已确认知识点。")
                st.rerun()

        chapters = store.list_chapters(confirmed_only=True)
        completed = sum(chapter["status"] == "done" for chapter in chapters)
        st.write(f"**当前目标：{active_goal['title']}** · {active_goal['timeframe_weeks']} 周 · 每周 {active_goal['weekly_hours']} 小时")
        st.progress(completed / len(chapters) if chapters else 0, text=f"知识点进度：{completed}/{len(chapters)} 已掌握")
        if not chapters:
            st.info("请在上方确认知识点后生成正式计划。")
        next_item = next((chapter for chapter in chapters if chapter.get("mastery_status") == "需复习"), None) or next((chapter for chapter in chapters if chapter["status"] != "done"), None)
        if next_item:
            st.info(f"下一步建议：学习“{next_item['title']}” · 达成标准：{next_item.get('learning_level', '理解')} · 预计 {next_item.get('estimated_minutes', 45)} 分钟")
        for chapter in chapters:
            left, right = st.columns([5, 1])
            with left:
                sources = chapter.get("source_text")
                if sources:
                    pages = " · " + "；".join(f"《{name}》第 {start}-{end} 页" for name, start, end in (source.split("|", 2) for source in sources.split("；")))
                else:
                    filename = chapter.get("original_filename") or "教材"
                    pages = f" · 《{filename}》第 {chapter['start_slide']}-{chapter['end_slide']} 页" if chapter.get("start_slide") else ""
                st.write(f"第 {chapter['planned_week']} 周 · {chapter['title']}{pages} · {chapter.get('mastery_status', '未开始')}")
            with right:
                if st.button("完成" if chapter["status"] != "done" else "重开", key=f"chapter_{chapter['id']}"):
                    store.complete_chapter(chapter["id"], chapter["status"] != "done")
                    st.rerun()

with tabs[1]:
    st.subheader("记录今天的学习")
    choose_goal_for_section("study", "本次学习与测验归属的学习目标")
    if profile is None:
        st.info("请先在“学习目标”创建或选择一个目标。")
    else:
        if st.session_state.attempt is None:
            st.session_state.attempt = store.latest_draft()
        chapters = store.list_chapters(confirmed_only=True)
        chapter_by_id = {chapter["id"]: chapter for chapter in chapters}
        with st.form("study"):
            chapter_id = st.selectbox("关联章节", options=list(chapter_by_id) + [None], format_func=lambda value: "未关联章节" if value is None else chapter_by_id[value]["title"])
            topic = st.text_input("学习主题", value=chapter_by_id[chapter_id]["title"] if chapter_id else "本次学习主题", max_chars=200)
            c1, c2 = st.columns(2)
            duration = c1.number_input("学习分钟", min_value=5, max_value=600, value=30)
            difficulty = c2.selectbox("主观难度", ["中等", "容易", "困难"])
            resource = st.selectbox("主要学习形式", ["代码练习", "文档", "视频", "课程", "其他"], index=1,
                                   key="study_resource_type", help="默认是文档；可按本次实际使用的学习材料调整。")
            notes = st.text_area("补充笔记", max_chars=3000)
            generate = st.form_submit_button("生成本次题目")
        if generate:
            try:
                if not chapter_id:
                    raise ValueError("请先选择一个已解析的关联章节；测验只会根据该知识点的教材出题")
                material_context = knowledge.chapter_material(chapter_id)
                if not material_context:
                    raise ValueError("该知识点还没有可用教材页。请先上传并解析教材，再生成测验")
                log = StudyLog(topic=topic.strip(), duration_minutes=duration, resource_type=resource, perceived_difficulty=difficulty, free_notes=notes)
                with st.spinner("正在生成题目"):
                    initial = run_initial_verification(profile, log, model, material_context)
                attempt = {"id": str(uuid.uuid4()), "status": "draft", "log": log.model_dump(), "profile": profile.model_dump(),
                           "quiz": initial.model_dump(), "answers": [], "explanation": "", "result": None,
                           "mode": settings.mode, "model": settings.model, "goal_id": selected_goal_id, "chapter_id": chapter_id,
                           "material_context": material_context}
                store.save_attempt(attempt)
                st.session_state.attempt = attempt
            except Exception as exc:
                st.error(model_error_message(exc, settings))
        attempt = st.session_state.attempt
        if attempt:
            legacy_quiz = not attempt.get("material_context")
            quiz = [QuizQuestion.model_validate({"reference_answer": "这是旧草稿，请重新生成以获得教材参考答案。",
                                                 "sources": ["旧草稿未保存教材来源"], **q}) for q in attempt["quiz"]["quiz"]]
            st.caption(("未完成测验已从数据库恢复。题目编号：" if attempt["status"] in {"draft", "submitted"} else "已完成测验。题目编号：") + attempt["id"][:8])
            if legacy_quiz:
                st.warning("这份旧草稿生成时尚未绑定教材知识点。请新建一次基于教材的测验，以获得可追溯题目与逐题参考答案。")
            with st.form("answers_" + attempt["id"]):
                old_answers = attempt.get("answers", [])
                answers = [st.text_area(f"第{i + 1}题  {q.question}", value=old_answers[i] if i < len(old_answers) else "",
                           key=f"answer_{attempt['id']}_{i}", max_chars=4000) for i, q in enumerate(quiz)]
                explanation = st.text_area(attempt["quiz"]["explanation_prompt"], value=attempt.get("explanation", ""), max_chars=6000)
                evaluate = st.form_submit_button("提交作答并保存反馈", disabled=attempt["status"] == "graded")
            if evaluate:
                try:
                    if attempt["mode"] != settings.mode or attempt["model"] != settings.model:
                        raise ValueError("模型配置已变化，请重新生成题目")
                    attempt.update(status="submitted", answers=answers, explanation=explanation)
                    store.save_attempt(attempt)
                    result = run_full_evaluation(LearnerProfile.model_validate(attempt["profile"]), StudyLog.model_validate(attempt["log"]), quiz, answers, explanation, model, attempt.get("material_context"))
                    if attempt.get("chapter_id"):
                        store.update_mastery(attempt["chapter_id"], result.score, [item.model_dump() for item in result.question_feedback])
                    store.save_session(attempt["id"], attempt["log"], result.model_dump(), answers, explanation, settings.mode == "demo", settings.model, attempt["goal_id"], attempt.get("chapter_id"))
                    st.session_state.attempt = store.mark_attempt_graded(attempt["id"], result.model_dump(), answers, explanation)
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc) if "请" in str(exc) or "已" in str(exc) else "模型输出格式不符合要求，请重试。")
                except Exception as exc:
                    st.error(model_error_message(exc, settings))
            if attempt.get("result"):
                result = attempt["result"]
                if result["score"] is not None:
                    st.metric("模型建议评分 仅供参考", f"{result['score']} / 100")
                st.write(result["feedback"])
                st.write("下一步：" + result["next_step_recommendation"])
                feedback_items = result.get("question_feedback", [])
                st.subheader("逐题点评与参考答案")
                for index, question in enumerate(quiz):
                    item = feedback_items[index] if index < len(feedback_items) else {"mastery": "需复习", "feedback": "本次未返回逐题点评，请对照参考答案复习。"}
                    with st.expander(f"第 {index + 1} 题 · {item['mastery']}", expanded=True):
                        st.write("**你的答案**：" + (attempt.get("answers", [""] * len(quiz))[index] or "未作答"))
                        st.write("**参考答案**：" + question.reference_answer)
                        st.write("**点评**：" + item["feedback"])
                        st.caption("教材来源：" + "；".join(question.sources))

with tabs[2]:
    st.subheader("教材资料")
    choose_goal_for_section("materials", "上传教材归属的学习目标")
    st.caption("支持文字型 PPTX 和 PDF：提取可复制文字后自动识别章节。扫描件、图片和复杂图表不保证可读；扫描 PDF 请先 OCR。")
    if not active_goal:
        st.info("请先创建并选择一个学习目标。教材会绑定到该目标。")
    uploaded = st.file_uploader("上传教材 PDF 或 PPTX", type=["pdf", "pptx"], accept_multiple_files=True, disabled=not active_goal,
                                help="可一次选择多份教材。新教材会按知识点合并到既有计划，不会重置已完成进度。")
    if active_goal and uploaded and st.button("解析教材并合并知识点"):
        upload_dir = Path(settings.upload_dir) / user["id"]
        upload_dir.mkdir(parents=True, exist_ok=True)
        failures, uploaded_count = [], 0
        for file in uploaded:
            data = file.getvalue()
            suffix = Path(file.name).suffix.lower()
            is_valid = (suffix == ".pptx" and data.startswith(b"PK")) or (suffix == ".pdf" and data.startswith(b"%PDF"))
            if not is_valid or len(data) > settings.max_ppt_mb * 1024 * 1024:
                failures.append(file.name)
                continue
            stored = upload_dir / f"{uuid.uuid4()}{suffix}"
            doc_id = None
            try:
                doc_id = knowledge.create_document(file.name, data, str(stored), settings.max_documents_per_user)
                stored.write_bytes(data)
                pages = parse_pdf(stored, settings.max_ppt_slides) if suffix == ".pdf" else parse_pptx(stored, settings.max_ppt_slides)
                knowledge.mark_ready(doc_id, pages)
                uploaded_count += 1
            except Exception:
                if doc_id:
                    knowledge.mark_failed(doc_id, "无法解析此文件")
                stored.unlink(missing_ok=True)
                failures.append(file.name)
        all_pages = knowledge.ready_pages()
        if all_pages and uploaded_count:
            try:
                with st.spinner("正在把新教材合并到已有知识点"):
                    outline = create_chapter_outline(all_pages, active_goal["timeframe_weeks"], model, [chapter["title"] for chapter in store.list_chapters()])
                    result = store.merge_detected_chapters(outline)
                st.success(f"已处理 {uploaded_count} 份新教材：新增 {result['added']} 个候选知识点。请到“学习目标”的“知识点确认”中确认后再纳入计划。")
            except Exception as exc:
                st.error(model_error_message(exc, settings))
        if failures:
            st.warning("以下文件未能解析：" + "、".join(failures) + "。请确认文件未加密且 PDF 含可复制文字。")
    documents = knowledge.list_documents()
    if active_goal and not documents:
        st.info("尚未上传教材。上传后，进度问答可按需检索课件并显示页码来源。")
    for document in documents:
        left, right = st.columns([5, 1])
        with left:
            st.write(f"**{document['original_filename']}** · {document['status']} · {document['slide_count']} 页")
            if document["error_message"]:
                st.caption(document["error_message"])
        with right:
            if st.button("删除", key="delete_doc_" + document["id"]):
                stored = knowledge.delete_document(document["id"])
                if stored:
                    Path(stored).unlink(missing_ok=True)
                st.rerun()

with tabs[3]:
    st.subheader("根据学习记录和教材讨论进度")
    st.caption("问题只会读取当前学习目标下的学习记录和教材。")
    messages = st.session_state.messages
    for message in messages:
        with st.chat_message(message["role"]):
            st.write(message["content"])
    prompt = st.chat_input("输入学习进度或教材问题", max_chars=2000, disabled=not active_goal)
    if prompt:
        try:
            with st.spinner("正在读取学习记忆和教材证据"):
                output = answer_progress(prompt, messages, model, store, knowledge)
            messages.extend([{"role": "user", "content": prompt}, {"role": "assistant", "content": output["answer"]}])
            st.session_state.messages = messages[-12:]
            st.session_state.latest_trace = output["trace"]
            st.session_state.latest_sources = output["sources"]
            st.rerun()
        except Exception as exc:
            st.error(model_error_message(exc, settings))
    st.caption("本次回答的可追溯来源")
    st.write(st.session_state.latest_sources or ["尚无来源。提出问题后会显示实际读取的档案、记录或课件页码。"])
    with st.expander("本次工具调用和证据"):
        st.json(st.session_state.latest_trace or [])

with tabs[4]:
    st.subheader("检查和管理当前账号的记忆")
    st.write("长期档案")
    st.json(store.get_profile() or {})
    rows = store.recent_sessions(20)
    st.dataframe([dict(记录ID=row["id"], 主题=row["topic"], 分数=row["score"], 演示=bool(row["is_demo"]), 时间UTC=row["created_at"])
                  for row in rows], width="stretch")
    st.download_button("导出当前账号的学习数据", json.dumps(store.export(), ensure_ascii=False, indent=2), file_name="learning-data.json", mime="application/json")
    confirm = st.checkbox("我确认删除当前账号的档案、测验、学习记录和上传教材")
    if st.button("删除当前账号数据", disabled=not confirm):
        for stored in knowledge.clear_user():
            Path(stored).unlink(missing_ok=True)
        store.clear_user()
        for key in ["attempt", "messages", "latest_trace", "latest_sources"]:
            st.session_state.pop(key, None)
        st.rerun()
