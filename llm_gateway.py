"""模型适配层。demo 只验证流程，不能测量真实知识水平。"""
import json
from openai import OpenAI

class CoachModel:
    def __init__(self, settings):
        self.settings = settings
        self.client = None
        if settings.mode == "online":
            # SDK 也会读取 OPENAI_BASE_URL。显式提供官方地址可避免 .env 中
            # OPENAI_BASE_URL= 空值被 SDK 误作无协议 URL。
            kwargs = dict(api_key=settings.api_key, timeout=45.0, max_retries=1,
                          base_url=settings.base_url or "https://api.openai.com/v1")
            self.client = OpenAI(**kwargs)

    def ask_json(self, system, payload):
        if self.client is None:
            if payload["task"] == "generate":
                topic = payload["log"]["topic"]
                sources = [item["source"] for item in payload.get("material_context", [])[:2]] or ["未提供教材来源"]
                return {"quiz": [
                    {"question": f"根据教材解释 {topic} 的核心概念。", "reference_answer": f"应依据教材准确说明 {topic} 的定义、作用和关键要点。", "sources": sources},
                    {"question": f"根据教材给出 {topic} 的一个具体例子，并说明原因。", "reference_answer": f"应给出教材中支持 {topic} 的例子，并解释其与概念的关系。", "sources": sources},
                    {"question": f"学习 {topic} 时容易混淆什么？如何根据教材验证理解？", "reference_answer": f"应指出一个常见混淆点，并用教材中的定义、步骤或例子进行验证。", "sources": sources}],
                    "explanation_prompt": "请串起概念、例子和验证方法。"}
            if payload["task"] == "outline":
                slides = payload["slides"]
                candidates = [item for item in slides if item.get("title")] or slides
                maximum = payload.get("max_chapters", payload.get("timeframe_weeks", 4))
                stride = max(1, -(-len(candidates) // maximum))
                return {"chapters": [{"title": item.get("title") or f"第 {item['page']} 页内容", "document_id": item.get("document_id", "single-document"),
                                      "start_slide": item["page"], "end_slide": item["page"]}
                                      for item in candidates[::stride][:maximum]]}
            answers = payload.get("answers", [])
            return {"score": None, "feedback": "演示模式已收到答案；未调用模型，不评价知识掌握程度。",
                    "next_step_recommendation": "切换在线模式后，对照教师量表验证评分一致性。",
                    "question_feedback": [{"mastery": "需复习" if not answer.strip() else "部分掌握", "feedback": "演示模式不作真实判分；请对照本题参考答案和教材来源复习。"} for answer in answers]}
        response = self.client.chat.completions.create(
            model=self.settings.model, temperature=0.2,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
        content = response.choices[0].message.content
        if not content:
            raise ValueError("模型返回了空内容，请检查模型及 JSON 模式支持情况")
        return json.loads(content)
