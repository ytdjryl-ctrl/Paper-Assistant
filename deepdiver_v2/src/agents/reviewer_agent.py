import json
import logging
import time
import os
from typing import Dict, Any, List, Optional

from .base_agent import BaseAgent, AgentConfig, AgentResponse, TaskInput
from ..utils.llm_client import chat_completion_response
from ..utils.skill_loader import get_skill_loader
import requests

logger = logging.getLogger(__name__)

REVIEWER_ROLES = {
    "methodology": {
        "name_cn": "方法论审稿人",
        "name_en": "Methodology Reviewer",
        "focus": "实验设计、统计方法、数据预处理、模型选择、超参数调优、评估指标、可复现性、图表质量",
    },
    "domain": {
        "name_cn": "领域专家审稿人",
        "name_en": "Domain Reviewer",
        "focus": "文献覆盖度、理论框架、最新参考文献(2020-2025)、领域贡献、引用准确性、讨论深度",
    },
    "devils_advocate": {
        "name_cn": "魔鬼代言人",
        "name_en": "Devil's Advocate",
        "focus": "质疑核心论点、发现逻辑谬误、识别过度泛化、提出替代解释、检验样本量支持性",
    },
}


class ReviewerAgent(BaseAgent):
    """
    专业审稿 Agent - 多视角同行评审器
    支持 methodology / domain / devils_advocate 三个审稿角色
    由 Planner 并行调度执行
    """

    def __init__(
        self,
        config: AgentConfig = None,
        shared_mcp_client=None,
        reviewer_role: str = "methodology",
    ):
        if config is None:
            config = AgentConfig(agent_name="ReviewerAgent")
        elif config.agent_name == "base_agent":
            config.agent_name = "ReviewerAgent"

        self.reviewer_role = reviewer_role
        role_info = REVIEWER_ROLES.get(reviewer_role, REVIEWER_ROLES["methodology"])
        self.role_name_cn = role_info["name_cn"]
        self.role_name_en = role_info["name_en"]
        self.role_focus = role_info["focus"]

        super().__init__(config, shared_mcp_client)

        self.config.agent_name = f"ReviewerAgent-{reviewer_role}"

    def _build_system_prompt(self) -> str:
        tool_schemas_str = json.dumps(self.tool_schemas, ensure_ascii=False)

        common_header = f"""You are a highly critical senior Academic Peer Reviewer specializing in **{self.role_name_en}** ({self.role_name_cn}).

Your primary review focus: {self.role_focus}

### LANGUAGE MANDATE
You MUST write your ENTIRE review (summary, strengths, weaknesses) strictly in CHINESE (中文)!
只允许使用**中文**撰写审稿意见!

### REVIEWER WORKFLOW
STEP 1 - READ THE PAPER CONTENT: The complete paper is provided in the user message below. Read and understand it carefully.

STEP 2 - CRITICAL ANALYSIS: Analyze the paper's strengths, weaknesses, methodology, and scientific validity from your specific perspective."""

        role_specific = f"""


STEP 3 - SUBMIT YOUR COMPLETE REVIEW: Call reviewer_task_done IMMEDIATELY with ALL fields fully populated:
- summary: A comprehensive summary (in CHINESE, at least 3 sentences)
- strengths: 2-4 specific strengths with detailed explanation (in CHINESE, DO NOT leave empty)
- weaknesses: 2-6 specific weaknesses with improvement suggestions (in CHINESE, MUST BE NON-EMPTY)
- scores: ALL 8 score fields MUST be integers (Originality, Quality, Clarity, Significance, Soundness, Presentation: 1-4; Overall: 1-10; Confidence: 1-5). DO NOT omit any score!
- decision: One of [Accept, Reject, Major Revision, Minor Revision]
- key_critique: Your single most important critique (in CHINESE)

CRITICAL: Every field is REQUIRED. If you submit empty weaknesses or missing scores, your review will be REJECTED."""

        common_footer = """
IMPORTANT: Call reviewer_task_done NOW with your complete peer review report. Do NOT make additional tool calls.
"""

        system_prompt = common_header + role_specific + common_footer
        return get_skill_loader().inject_agent_skills(
            system_prompt,
            self.config.agent_name,
            compact=True
        )

    def _build_agent_specific_tool_schemas(self) -> List[Dict[str, Any]]:
        schemas = super()._build_agent_specific_tool_schemas()

        schemas.append({
            "type": "function",
            "function": {
                "name": "reviewer_task_done",
                "description": f"Submit the structured peer review from {self.role_name_cn}({self.role_name_en}) perspective.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": "用中文简短总结论文的核心贡献与方法.(MUST BE IN CHINESE)"
                        },
                        "strengths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "用中文列出论文的2-3个主要优点.(MUST BE IN CHINESE)"
                        },
                        "weaknesses": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "用中文列出论文的2-3个主要缺点或限制.(MUST BE IN CHINESE)"
                        },
                        "scores": {
                            "type": "object",
                            "properties": {
                                "Originality": {"type": "integer", "description": "1-4"},
                                "Quality": {"type": "integer", "description": "1-4"},
                                "Clarity": {"type": "integer", "description": "1-4"},
                                "Significance": {"type": "integer", "description": "1-4"},
                                "Soundness": {"type": "integer", "description": "1-4"},
                                "Presentation": {"type": "integer", "description": "1-4"},
                                "Overall": {"type": "integer", "description": "1-10"},
                                "Confidence": {"type": "integer", "description": "1-5"}
                            },
                            "required": ["Originality", "Quality", "Clarity", "Significance", "Soundness",
                                         "Presentation", "Overall", "Confidence"]
                        },
                        "decision": {
                            "type": "string",
                            "enum": ["Accept", "Reject", "Major Revision", "Minor Revision"],
                            "description": "Final decision for the paper."
                        },
                        "key_critique": {
                            "type": "string",
                            "description": (
                                "YOUR SINGLE MOST IMPORTANT CRITIQUE. "
                                "Identify the weakest aspect from YOUR perspective, quote the relevant part, "
                                "and explain why it matters. MUST BE IN CHINESE."
                            )
                        },
                    },
                    "required": ["summary", "strengths", "weaknesses", "scores", "decision", "key_critique"]
                }
            }
        })
        return schemas

    def execute_task(self, task_input: TaskInput) -> AgentResponse:
        start_time = time.time()
        self.logger.info(f"[{self.role_name_cn}] Starting Peer Review task...")
        self.reset_trace()

        reviewer_max_iterations = max(3, int(os.getenv("REVIEWER_MAX_ITERATIONS", "12")))
        if self.config.max_iterations > reviewer_max_iterations:
            self.config.max_iterations = reviewer_max_iterations

        conversation_history = [
            {"role": "system", "content": self._build_system_prompt()},
            {"role": "user",
             "content": f"Please review the paper from your perspective as {self.role_name_cn}. Task: {task_input.task_content}\n/no_think"}
        ]

        iteration = 0
        task_completed = False
        task_done_result = None
        consecutive_errors = 0

        from config.config import get_config
        config_obj = get_config()
        model_config = config_obj.get_custom_llm_config()

        while iteration < self.config.max_iterations and not task_completed:
            iteration += 1
            self.logger.info(f"[{self.role_name_cn}] Iteration {iteration}")

            try:
                payload = {
                    "model": self.config.model if hasattr(self.config, 'model') else get_config().model_name,
                    "messages": conversation_history,
                    "temperature": 0.3,
                    "max_tokens": 8192,
                    "chat_template": (
                        "{% for message in messages %}"
                        "{% if loop.first and messages[0]['role'] != 'system' %}{{ '<s>[unused9]系统:[unused10]' }}{% endif %}"
                        "{% if message['role'] == 'system' %}{{'<s>[unused9]系统:' + message['content'] + '[unused10]'}}{% endif %}"
                        "{% if message['role'] == 'assistant' %}{{'[unused9]助手:' + message['content'] + '[unused10]'}}{% endif %}"
                        "{% if message['role'] == 'tool' %}{{'[unused9]工具:' + message['content'] + '[unused10]'}}{% endif %}"
                        "{% if message['role'] == 'user' %}{{'[unused9]用户:' + message['content'] + '[unused10]'}}{% endif %}"
                        "{% endfor %}"
                        "{% if add_generation_prompt %}{{ '[unused9]助手:' }}{% endif %}"
                    ),
                    "spaces_between_special_tokens": False
                }

                response = chat_completion_response(
                    payload,
                    model_config=model_config,
                    agent_name=self.config.agent_name,
                    request_logger=self.logger,
                )
                consecutive_errors = 0
                response_json = response.json()
                if "choices" not in response_json:
                    err_msg = response_json.get("error", {}).get("message", str(response_json)[:200])
                    self.logger.error(f"[{self.role_name_cn}] API returned no choices: {err_msg}")
                    conversation_history.append({"role": "user", "content": f"API error: {err_msg}. Please retry. /no_think"})
                    continue
                message = response_json["choices"][0]["message"]
                content = message.get("content", "")

                conversation_history.append({"role": "assistant", "content": content})

                # Try parsing tool_calls from both [unused11] markers AND native tool_calls field
                tool_calls = self.extract_tool_calls(content)
                if not tool_calls:
                    native_tool_calls = message.get("tool_calls", [])
                    for tc in native_tool_calls:
                        func = tc.get("function", {})
                        tool_calls.append({
                            "name": func.get("name", ""),
                            "arguments": json.loads(func.get("arguments", "{}")) if isinstance(func.get("arguments"), str) else func.get("arguments", {})
                        })

                self._no_tool_streak = 0
                for tool_call in tool_calls:
                    arguments = tool_call.get("arguments", {})
                    tool_name = tool_call.get("name", "")

                    if tool_name == "reviewer_task_done":
                        task_done_result = arguments
                        task_completed = True
                        self._save_review_report(arguments)
                        break

                    if tool_name in ["think", "reflect"]:
                        tool_result = {"tool_results": "Thought logged. Proceed."}
                    else:
                        tool_result = self.execute_tool_call(tool_call)

                    conversation_history.append({
                        "role": "tool",
                        "content": json.dumps(tool_result, ensure_ascii=False) + " /no_think"
                    })

                if len(tool_calls) == 0 and not task_completed:
                    # Last resort: try to brute-force extract reviewer_task_done from raw content
                    import re as _re, ast as _ast
                    done_match = _re.search(r'reviewer_task_done', content)
                    if done_match:
                        args = None

                        # Strategy A: Try to find JSON object with "summary" key
                        json_match = _re.search(r'\{[\s\S]*"summary"[\s\S]*\}', content)
                        if json_match:
                            try:
                                args = json.loads(json_match.group(0))
                            except:
                                pass

                        # Strategy B: Try Python-style reviewer_task_done(summary="...", strengths=[...], ...)
                        if args is None:
                            py_match = _re.search(r'reviewer_task_done\s*\((.*?)\)\s*(?:$|\n)', content, _re.DOTALL)
                            if py_match:
                                py_args_str = py_match.group(1)
                                args = {}
                                # Parse summary (string value)
                                summary_m = _re.search(r'summary\s*=\s*"((?:[^"\\]|\\.)*)"', py_args_str, _re.DOTALL)
                                if summary_m:
                                    args['summary'] = summary_m.group(1).replace('\"', '"').replace('\n', '\n')

                                # Parse strengths (list of strings) - use brace counting
                                strengths_start = py_args_str.find("strengths")
                                if strengths_start >= 0:
                                    eq_pos = py_args_str.find("=", strengths_start)
                                    if eq_pos >= 0:
                                        bracket_start = py_args_str.find("[", eq_pos)
                                        if bracket_start >= 0:
                                            depth = 0
                                            bracket_end = bracket_start
                                            for i in range(bracket_start, len(py_args_str)):
                                                if py_args_str[i] == "[":
                                                    depth += 1
                                                elif py_args_str[i] == "]":
                                                    depth -= 1
                                                    if depth == 0:
                                                        bracket_end = i
                                                        break
                                            list_str = py_args_str[bracket_start:bracket_end+1]
                                            str_items = _re.findall(r'"((?:[^"\\]|\\.)*)"', list_str, _re.DOTALL)
                                            args['strengths'] = [s.replace('\\"', '"').replace('\\n', '\n') for s in str_items]

                                # Parse weaknesses (list of strings) - use brace counting
                                weaknesses_start = py_args_str.find("weaknesses")
                                if weaknesses_start >= 0:
                                    eq_pos = py_args_str.find("=", weaknesses_start)
                                    if eq_pos >= 0:
                                        bracket_start = py_args_str.find("[", eq_pos)
                                        if bracket_start >= 0:
                                            depth = 0
                                            bracket_end = bracket_start
                                            for i in range(bracket_start, len(py_args_str)):
                                                if py_args_str[i] == "[":
                                                    depth += 1
                                                elif py_args_str[i] == "]":
                                                    depth -= 1
                                                    if depth == 0:
                                                        bracket_end = i
                                                        break
                                            list_str = py_args_str[bracket_start:bracket_end+1]
                                            str_items = _re.findall(r'"((?:[^"\\]|\\.)*)"', list_str, _re.DOTALL)
                                            args['weaknesses'] = [s.replace('\\"', '"').replace('\\n', '\n') for s in str_items]

                                # Parse scores (dict) - use brace counting for multi-line
                                scores_start = py_args_str.find("scores")
                                if scores_start >= 0:
                                    eq_pos = py_args_str.find("=", scores_start)
                                    if eq_pos >= 0:
                                        brace_start = py_args_str.find("{", eq_pos)
                                        if brace_start >= 0:
                                            depth = 0
                                            brace_end = brace_start
                                            for i in range(brace_start, len(py_args_str)):
                                                if py_args_str[i] == "{":
                                                    depth += 1
                                                elif py_args_str[i] == "}":
                                                    depth -= 1
                                                    if depth == 0:
                                                        brace_end = i
                                                        break
                                            scores_str = py_args_str[brace_start:brace_end+1]
                                            try:
                                                args["scores"] = json.loads(scores_str)
                                            except:
                                                try:
                                                    args["scores"] = _ast.literal_eval(scores_str)
                                                except:
                                                    pass
                                # Parse decision
                                decision_m = _re.search(r'decision\s*=\s*"([^"]*)"', py_args_str)
                                if decision_m:
                                    args['decision'] = decision_m.group(1)

                                # Parse key_critique
                                critique_m = _re.search(r'key_critique\s*=\s*"((?:[^"\\]|\\.)*)"', py_args_str, _re.DOTALL)
                                if critique_m:
                                    args['key_critique'] = critique_m.group(1).replace('\"', '"')

                        if args and isinstance(args, dict) and 'summary' in args:
                            # If summary looks like nested JSON, unwrap it
                            if isinstance(args.get('summary'), str) and args['summary'].strip().startswith('{'):
                                try:
                                    inner = json.loads(args['summary'])
                                    if isinstance(inner, dict) and 'summary' in inner:
                                        for k in ['summary', 'strengths', 'weaknesses', 'scores', 'decision', 'key_critique']:
                                            if k in inner:
                                                args[k] = inner[k]
                                except:
                                    pass
                            task_done_result = args
                            task_completed = True
                            self._save_review_report(args)
                            self.logger.info(f'[{self.role_name_cn}] Brute-force extracted reviewer_task_done')
                            break
                    # Track no-tool-call streak
                    if not hasattr(self, '_no_tool_streak'):
                        self._no_tool_streak = 0
                    self._no_tool_streak += 1

                    if self._no_tool_streak >= 3:
                        conversation_history.append({"role": "user",
                                                     "content": "You have not called any tool for 3 responses. Call reviewer_task_done NOW with your review. /no_think"})
                        if self._no_tool_streak >= 5:
                            self.logger.warning(f"[{self.role_name_cn}] stopping after {self._no_tool_streak} no-tool responses")
                            break
                    else:
                        conversation_history.append({"role": "user",
                                                     "content": "Please continue. Read the paper and call reviewer_task_done when finished. /no_think"})

            except Exception as e:
                self.logger.error(f"[{self.role_name_cn}] Error: {e}")
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    self.logger.warning(f"[{self.role_name_cn}] stopping after {consecutive_errors} consecutive errors")
                    break
                conversation_history.append({"role": "user", "content": f"Error: {e}. Please retry."})

        # Fallback: if reviewer_task_done was never called, save a failure report
        result = task_done_result or {}
        if not task_completed:
            # Try to salvage partial review content from conversation history
            partial_summary = ""
            partial_strengths = []
            partial_weaknesses = []
            for msg in reversed(conversation_history):
                if msg.get("role") == "assistant":
                    c = msg.get("content", "")
                    # Look for review-like content
                    if "summary" in c.lower() or "strength" in c.lower() or "weakness" in c.lower():
                        partial_summary = c[:500]
                    # Extract bullet points for strengths/weaknesses
                    for line in c.split("\n"):
                        if "strength" in line.lower() or "advantage" in line.lower() or "??" in line or "??" in line:
                            partial_strengths.append(line.strip("- *")[:200])
                        if "weakness" in line.lower() or "limitation" in line.lower() or "??" in line or "??" in line:
                            partial_weaknesses.append(line.strip("- *")[:200])
                    if partial_summary:
                        break

            fallback_data = {
                "summary": partial_summary if partial_summary else f"[{self.role_name_cn}] Review partially completed - the reviewer reached the iteration limit before finalizing. The partial analysis below reflects the reviewer''s preliminary assessment.",
                "strengths": partial_strengths if partial_strengths else ["The paper addresses a relevant research question in its domain."],
                "weaknesses": partial_weaknesses if partial_weaknesses else ["The review could not be fully completed within the allocated iterations. Please refer to the partial analysis above."],
                "scores": {"Overall": 5, "Originality": 2, "Quality": 2, "Clarity": 2, "Significance": 2, "Soundness": 2, "Presentation": 2, "Confidence": 3},
                "decision": "Major Revision",
                "key_critique": partial_summary[:300] if partial_summary else "The reviewer was unable to complete a full assessment. A re-review is recommended."
            }
            result = fallback_data
            try:
                self._save_review_report(fallback_data)
                self.logger.warning(f"[{self.role_name_cn}] Fallback: saved failure report (review incomplete)")
            except:
                pass

        result["reviewer_role"] = self.reviewer_role
        result["reviewer_name"] = self.role_name_cn

        return self.create_response(
            success=task_completed,
            result=result,
            iterations=iteration,
            execution_time=time.time() - start_time
        )

    def _save_review_report(self, review_data: dict):
        """将评审结果保存为人类可读的 Markdown 文件(按角色分文件)"""
        # ---- DEFENSIVE: Unwrap JSON-in-summary ----
        summary_raw = review_data.get("summary", "")
        if isinstance(summary_raw, str) and summary_raw.strip().startswith("{"):
            try:
                unwrapped = json.loads(summary_raw)
                if isinstance(unwrapped, dict) and "summary" in unwrapped:
                    for key in ["summary", "strengths", "weaknesses", "scores", "decision", "key_critique"]:
                        if key in unwrapped and (key not in review_data or not review_data.get(key)):
                            review_data[key] = unwrapped[key]
                    self.logger.info(f"[{self.role_name_cn}] Unwrapped JSON-in-summary")
            except (json.JSONDecodeError, TypeError):
                pass
        # ---- END DEFENSIVE ----

        # ---- SCORE AUGMENTATION: merge and fill defaults ----
        scores = review_data.get("scores", {})
        if not isinstance(scores, dict):
            scores = {}
        SCORE_DEFAULTS = {
            "Originality": 3, "Quality": 3, "Clarity": 3,
            "Significance": 3, "Soundness": 3, "Presentation": 3,
            "Overall": 5, "Confidence": 3
        }
        for score_key, default_val in SCORE_DEFAULTS.items():
            if score_key not in scores or scores.get(score_key) is None:
                scores[score_key] = default_val
        review_data["scores"] = scores
        # ---- END SCORE DEFAULTS ----
        report_filename = f"./report/peer_review_report_{self.reviewer_role}.md"

        lines = []
        lines.append(f"# Peer Review Report -- {self.role_name_cn} ({self.role_name_en})")
        lines.append("")
        lines.append(f"**Reviewer Role**: {self.role_name_cn}")
        lines.append(f"**Decision**: **{review_data.get('decision', 'N/A')}**")
        lines.append("")
        lines.append("## Scores")
        lines.append("- **Originality**: " + str(scores.get("Originality", "N/A")) + " / 4")
        lines.append("- **Quality**: " + str(scores.get("Quality", "N/A")) + " / 4")
        lines.append("- **Clarity**: " + str(scores.get("Clarity", "N/A")) + " / 4")
        lines.append("- **Significance**: " + str(scores.get("Significance", "N/A")) + " / 4")
        lines.append("- **Soundness**: " + str(scores.get("Soundness", "N/A")) + " / 4")
        lines.append("- **Presentation**: " + str(scores.get("Presentation", "N/A")) + " / 4")
        lines.append("- **Overall**: " + str(scores.get("Overall", "N/A")) + " / 10")
        lines.append("- **Confidence**: " + str(scores.get("Confidence", "N/A")) + " / 5")
        lines.append("")
        lines.append("## Summary")
        lines.append(str(review_data.get("summary", "N/A")))
        lines.append("")
        lines.append("## Strengths")
        strengths = review_data.get("strengths", [])
        if isinstance(strengths, list):
            for s in strengths:
                lines.append("- " + str(s))
        elif isinstance(strengths, str) and strengths.strip():
            lines.append("- " + strengths)
        if not strengths:
            lines.append("- (No specific strengths identified)")
        lines.append("")
        lines.append("## Weaknesses")
        weaknesses = review_data.get("weaknesses", [])
        if isinstance(weaknesses, list):
            for w in weaknesses:
                lines.append("- " + str(w))
        elif isinstance(weaknesses, str) and weaknesses.strip():
            lines.append("- " + weaknesses)
        if not weaknesses:
            # Try to extract weaknesses from key_critique
            kc = review_data.get("key_critique", "")
            if kc and len(kc) > 20:
                import re as _re2
                parts = _re2.split(r"(?:\d+[\.\)]\s*)|(?:;|;)", kc)
                parts = [p.strip() for p in parts if len(p.strip()) > 10]
                if len(parts) >= 2:
                    weaknesses = parts
                    review_data["weaknesses"] = weaknesses
                    for w in weaknesses:
                        lines.append("- " + str(w))
                else:
                    lines.append("- " + str(kc))
            else:
                lines.append("- (No specific weaknesses identified)")
        key_critique = review_data.get("key_critique", "")
        if key_critique:
            lines.append("")
            lines.append("## Key Critique")
            lines.append(str(key_critique))

        report_md = chr(10).join(lines)

        try:
            self.execute_tool_call({
                "name": "file_write",
                "arguments": {
                    "file_path": report_filename,
                    "content": report_md,
                    "create_dirs": True
                }
            })
            self.logger.info(f"[{self.role_name_cn}] 评审报告已保存至 {report_filename}")
        except Exception as e:
            self.logger.warning(f"[{self.role_name_cn}] 保存评审报告失败: {e}")

def create_reviewer_agent(
    model=None,
    max_iterations=30,
    shared_mcp_client=None,
    reviewer_role: str = "methodology",
    **kwargs
) -> ReviewerAgent:
    """
    创建指定角色的审稿 Agent.

    Args:
        model: LLM 模型名称
        max_iterations: 最大迭代次数
        shared_mcp_client: 共享 MCP 客户端
        reviewer_role: 审稿角色,可选 "methodology" / "domain" / "devils_advocate"

    Returns:
        配置好的 ReviewerAgent 实例
    """
    from .base_agent import create_agent_config
    config = create_agent_config(
        agent_name=f"ReviewerAgent-{reviewer_role}",
        model=model,
        max_iterations=max_iterations,
        **kwargs
    )
    return ReviewerAgent(
        config=config,
        shared_mcp_client=shared_mcp_client,
        reviewer_role=reviewer_role,
    )
