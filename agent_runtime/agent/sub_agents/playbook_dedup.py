from __future__ import annotations

import json
import re
from typing import Any

from agent_runtime.agent.sub_agents.base import SubAgent


DEDUP_DECISION_SCHEMA = """\
{
  "has_duplicates": true/false,
  "reason": "简短说明",
  "merge_groups": [
    {
      "canonical_id": "pb_xxx",
      "archive_ids": ["pb_yyy", "pb_zzz"],
      "merged_problem_type": "规范题型名",
      "merged_approach": "合并后的解法描述",
      "merged_trigger_terms": ["关键词1", "关键词2"]
    }
  ]
}
"""

DEDUP_RULES = """\
1) 只合并语义高度相似的题型（核心含义相同，如"最短路径""单源最短路径""无向图最短路径"视为相近）；本质不同的题型不合并。
2) merged_problem_type 选更通用的名称，或置信度更高条目的名称。
3) merged_approach 取最完整清晰的解法描述，必要时融合多条要点，保持 2-5 步结构。
4) merged_trigger_terms 取所有待合并条目 trigger_terms 的并集（去重，保留有意义的词）。
5) 若所有题型已足够独立，输出 has_duplicates: false，merge_groups 为空数组。
6) 不合并 confidence < 0.4 的低置信度条目（保留原样，让其自然淘汰）。
7) 每个 merge_group 的 canonical_id 必须是输入列表中真实存在的 playbook_id。
8) archive_ids 不能包含 canonical_id 本身。
"""

DEDUP_SYSTEM_PROMPT = f"""\
你是 Playbook 去重子代理（playbook dedup sub-agent）。
你的职责是识别语义重复的题型求解经验并输出合并方案。

输出必须是一个严格 JSON 对象，不要输出 markdown 或解释文字。
JSON schema:
{DEDUP_DECISION_SCHEMA}
规则:
{DEDUP_RULES}"""

DEDUP_USER_PROMPT_TEMPLATE = """\
请对以下题型经验列表进行去重分析，输出决策 JSON（禁止 markdown，禁止额外解释）：
{payload_json}
"""


class PlaybookDeduplicateSubAgent(SubAgent):
    name = "playbook_dedup"
    system_prompt = DEDUP_SYSTEM_PROMPT
    default_max_tokens = None
    default_temperature = None

    @staticmethod
    def build_user_prompt(playbooks: list[dict[str, Any]]) -> str:
        return DEDUP_USER_PROMPT_TEMPLATE.format(
            payload_json=json.dumps(playbooks, ensure_ascii=False),
        )

    async def decide(
        self,
        *,
        provider_gateway: Any,
        playbooks: list[dict[str, Any]],
        step: int,
    ) -> tuple[str, dict[str, Any]]:
        model_msgs = self.build_messages(self.build_user_prompt(playbooks))
        decision_text = await provider_gateway.invoke_text(model_msgs, step=step)
        return decision_text, self.parse_decision_json(decision_text)

    @staticmethod
    def parse_decision_json(raw: str) -> dict[str, Any]:
        text = (raw or "").strip()
        if not text:
            return {"has_duplicates": False, "reason": "empty", "merge_groups": []}

        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match:
            text = match.group(0)
        try:
            data = json.loads(text)
        except Exception:
            return {"has_duplicates": False, "reason": "invalid_json", "merge_groups": []}
        if not isinstance(data, dict):
            return {"has_duplicates": False, "reason": "not_object", "merge_groups": []}
        data.setdefault("merge_groups", [])
        return data
