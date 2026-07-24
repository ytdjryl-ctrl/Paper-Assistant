# Copyright (c) 2025 Huawei Technologies Co., Ltd. All rights reserved.
import json
from typing import Dict, Any, List
import time
import requests
import os
from .base_agent import BaseAgent, AgentConfig, AgentResponse, TaskInput
from ..utils.llm_client import chat_completion_response
from ..utils.skill_loader import get_skill_loader
from .information_context import LiteratureContextLedger
import logging
logger = logging.getLogger(__name__)

DEFAULT_INFO_SEEKER_MAX_ITERATIONS = 30


def resolve_info_seeker_max_iterations(configured_max_iterations: int) -> int:
    """Resolve iteration limit while accepting both old and V2 env names."""
    env_value = (
        os.getenv("INFO_SEEKER_MAX_ITERATIONS")
        or os.getenv("INFORMATION_SEEKER_MAX_ITERATION")
    )
    if env_value:
        return max(1, int(env_value))
    configured = int(configured_max_iterations or DEFAULT_INFO_SEEKER_MAX_ITERATIONS)
    return max(DEFAULT_INFO_SEEKER_MAX_ITERATIONS, configured)


class InformationSeekerAgent(BaseAgent):
    """
    Information Seeker Agent that follows ReAct pattern (Reasoning + Acting)
    
    This agent takes decomposed sub-questions or tasks from parent agents,
    thinks interleaved (reasoning -> action -> reasoning -> action),
    uses MCP tools to gather information, and returns structured results.
    """
    
    def __init__(self, config: AgentConfig = None, shared_mcp_client=None):
        # Set default agent name if not specified
        if config is None:
            config = AgentConfig(agent_name="InformationSeekerAgent")
        elif config.agent_name == "base_agent":
            config.agent_name = "InformationSeekerAgent"
            
        super().__init__(config, shared_mcp_client)

    def _build_system_prompt(self) -> str:
        """Build the system prompt for the ReAct agent"""
        tool_schemas_str = json.dumps(self.tool_schemas, ensure_ascii=False)
        system_prompt_template = """You are an Information Seeker Agent that follows the ReAct pattern (Reasoning + Acting).
        
        Your role is to:
        1. Take decomposed sub-questions or tasks from parent agents
        2. Think step-by-step through reasoning 
        3. Use available tools to gather information when needed
        4. Continue reasoning based on tool results
        5. Repeat this process until you have sufficient information
        6. Call info_seeker_subjective_task_done to provide a structured summary and key files
        
        # 🚨🚨🚨 CRITICAL RULES (致命纪律 - 绝对不许偷懒) 🚨🚨🚨
        1. NO HALLUCINATION: You must NEVER invent, fabricate, or guess academic references.
        2. SOURCE HANDLING: Use `academic_search`, PubMed, or arXiv to identify papers first. Retrieve full text only for a small set of highly relevant candidates. If a tool returns `metadata_abstract_only`, treat it as usable evidence with a stated limitation and never retry that paper's full text.
        3. NO LAZINESS: Do not return only unexplained paper titles. For each selected source, capture citation metadata, evidence, task relevance, and any accessible findings.
        4. VERIFICATION: Complete the task when the requested source count and evidence depth are reasonably satisfied, or when available sources/tool access have been exhausted and limitations are documented.
        5. DURABLE SEARCH MEMORY: Search results may be returned as a compact ledger using IDs such as L001. Full raw batches are saved at the reported path. Use the ledger to avoid repeating queries and reopen a batch only when a particular record needs verification.
        
        TOOL USAGE STRATEGY:
        Follow this optimized workflow for information gathering:
        
        1. INITIAL RESEARCH:
           - Generate 3-8 focused queries for unresolved Claims-Evidence gaps. Expand only when a result identifies a new gap.
           - **QUERY FORMAT WARNING**: When writing search queries in the JSON arguments,
           - DO NOT wrap individual query strings with extra quotes or escape characters.
             Wrong: ["\"set.seed(10)\" genomic selection"]
             Correct: ["set.seed 10 genomic selection"]
             If a query needs to express an exact phrase, just write it plainly without 
             inner quotation marks.
           - **ACADEMIC SOURCE RULE:** a) Use `academic_search` for journal metadata from Crossref, OpenAlex, and configured ScienceDirect.
                b) For AI/Computer Science topics, also prioritize `arxiv_search` to find open-access papers.
                c) For Biology/Agriculture topics, also prioritize `search_pubmed_key_words`; its results already contain batched metadata and abstracts.
                d) Use `batch_web_search` for general news, blogs, or finding a specific paper title.
           - Do not send generic ML/CV architecture terms to PubMed. After three empty, irrelevant, or blocked calls in one query family, switch source or stop that branch.
           - Analyze the search results to identify promising sources.
           
        
        2. CONTENT EXTRACTION (CRITICAL):  
           - **CRAWLER RESTRICTIONS:** **STRICTLY PROHIBIT** using `url_crawler` for the following domains: `mdpi.com`, `ieeexplore.ieee.org`, `sciencedirect.com`, `springer.com`, `wiley.com`. These sites will block the crawler and hang the system.
           - **ALTERNATIVE FOR RESTRICTED SITES:** For ScienceDirect, use `get_sciencedirect_article` only for a selected relevant result when Elsevier access is configured. For other restricted publishers, search the title via `arxiv_search` for an open version. Otherwise use verified metadata/abstract and do not crawl or retry the publisher page.
           - **FULL-TEXT BUDGET:** Call `get_pubmed_article` or `get_sciencedirect_article` only for the top 3-5 candidates across one assigned task. Do not call them for every search result.
           - For accessible URLs, use `url_crawler` and save to `./url_crawler_save_files/research/`.
           - **EFFICIENCY RULE**: Maximum 3 url_crawler calls total. If a URL returns 403 or any error, immediately skip it and move to the next one. NEVER retry failed URLs.
           - **SOURCE COUNT**: Respect the source count requested by the task. If no count is specified, collect a focused set of about 3-8 high-quality sources instead of chasing volume.
           - **NETWORK SEARCH**: If user-uploaded files are provided, perform task-relevant web or academic search when the task requires recent external support. Start from `./research/references.json` and `./research/literature_online/`, then expand only unresolved Claims-Evidence gaps. Do not force extra sources when evidence is sufficient or the task explicitly limits sources.
           - **FOR ACADEMIC TASKS**: Prefer recent, relevant, authoritative papers. Continue planning/searching until the assigned acceptance criteria and evidence gaps are addressed, within the configured iteration budget. Save new structured summaries under `./research/autonomous_search/`; never stop at an arbitrary source count and never fabricate missing metadata.
        3. CONTENT ANALYSIS:
           - Use `document_extract` for multi-dimensional analysis of saved files.
           - For research papers (PDFs), **ALWAYS** use `document_extract` instead of `file_read`.
           - **PAPER STRUCTURE EXTRACTION**: After reading each paper, identify and record in paper_structures:
             * paper_summary: A 200-500 word Chinese summary of the paper content (research problem, method, key results, conclusions)
             * file_path: The local file path where the crawled paper content is saved
             * figure_types: What types of figures does the paper use? (trend curves, distribution plots, comparison charts, heatmaps, workflow diagrams, architecture diagrams, qualitative examples, etc.)
             * table_types: What types of tables does the paper use? (performance/summary tables, statistical test tables, ablation or sensitivity tables, parameter tables, dataset or sample statistics tables, etc.)
             * figure_count and table_count: How many of each?
             * This data helps the experiment agent learn what content+figures+tables are commonly expected in this domain.
        
        4. FILE MANAGEMENT & SAFETY:
           - **BINARY FILE WARNING:** NEVER use `file_read` for PDF, ZIP, or other binary files. It will return garbled text and cause token overflow/crashes.
           - For reviewing saved content:
                a) Prefer `document_qa` for specific insights.
                b) Prefer `document_extract` for structured summaries.
                c) Use `file_read` **ONLY** for small text files (<1000 tokens) that you are certain are plain text.
        
        
        ### Usage of Systematic Tool:
            - `think` is a systematic tool. After receiving the response from the complex tool or before invoking any other tools, you must **first invoke the `think` tool**: to deeply reflect on the results of previous tool invocations (if any), and to thoroughly consider and plan the user's task. The `think` tool does not acquire new information; it only saves your thoughts into memory.
        
        Always provide clear reasoning for your actions and synthesize information effectively.

Below, within the <tools></tools> tags, are the descriptions of each tool and the required fields for invocation:
<tools>
$tool_schemas
</tools>
For each function call, return a JSON object placed within the [unused11][unused12] tags, which includes the function name and the corresponding function arguments:
[unused11][{\"name\": <function name>, \"arguments\": <args json object>}][unused12]
CRITICAL: Do not output loose tool syntax such as `<tool_call>tool_name` followed by JSON. Always use the exact [unused11] JSON wrapper above, and keep `arguments` as a JSON object, not a quoted JSON string.
"""
        system_prompt = system_prompt_template.replace("$tool_schemas", tool_schemas_str)
        return get_skill_loader().inject_agent_skills(
            system_prompt,
            self.config.agent_name,
            compact=True
        )

    @staticmethod
    def _build_initial_message_from_task_input(task_input: TaskInput) -> str:
        """Build the initial user message from TaskInput"""
        message = task_input.format_for_prompt()
        
        message += "\nPlease analyze this task and start your ReAct process:\n"
        message += "1. Reason about what information you need to gather\n"
        message += "2. Use appropriate tools to get that information\n"
        message += "3. Continue reasoning and acting until you have sufficient information\n"
        message += "4. Call info_seeker_subjective_task_done when ready to provide your complete findings\n\n"
        message += "Begin with your initial reasoning about the task."
        
        return message

    @staticmethod
    def _safe_short_text(value: Any, limit: int = 1200) -> str:
        try:
            text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
        except Exception:
            text = str(value)
        return text[:limit]

    def _build_partial_completion(
        self,
        task_input: TaskInput,
        successful_actions: List[Dict[str, Any]],
        reason: str
    ) -> Dict[str, Any]:
        action_lines = []
        for item in successful_actions[-8:]:
            action_lines.append(
                f"- {item.get('tool', 'unknown_tool')}: "
                f"{self._safe_short_text(item.get('arguments', {}), 180)}"
            )
        if not action_lines:
            action_lines.append("- No successful external tool action was recorded before fallback.")

        return {
            "task_summary": (
                f"Information gathering ended with a controlled partial completion.\n\n"
                f"Reason: {reason}\n\n"
                f"Original task: {task_input.task_content}\n\n"
                f"Recorded successful actions:\n" + "\n".join(action_lines)
            ),
            "completion_status": "completed_with_partial_sources",
            "key_files": [],
            "paper_structures": []
        }
    
    def execute_task(self, task_input: TaskInput) -> AgentResponse:
        """
        Execute a task using ReAct pattern (Reasoning + Acting)
        
        Args:
            task_input: TaskInput object with standardized task information
            
        Returns:
            AgentResponse with results and process trace
        """
        start_time = time.time()
        
        try:
            self.logger.info(f"Starting information seeker task: {task_input.task_content}")
            
            # Reset trace for new task
            self.reset_trace()
            
            # Initialize conversation history
            conversation_history = []
            
            # Build initial system prompt for ReAct
            system_prompt = self._build_system_prompt()
            
            # Build initial user message from TaskInput
            user_message = self._build_initial_message_from_task_input(task_input)

            # Add to conversation
            conversation_history.append({"role": "system", "content": system_prompt})
            conversation_history.append({"role": "user", "content": user_message + " /no_think"})

            iteration = 0
            task_completed = False
            task_done_result = None
            completion_tool = None  # 记录实际触发完成的 done 工具名,用于精确回溯结果
            successful_actions = []
            no_tool_streak = 0
            format_error_streak = 0
            last_tool_signature = None
            repeated_tool_call_count = 0
            literature_context = LiteratureContextLedger(self, task_input, "subjective")
            # Get model configuration from config
            from config.config import get_config
            config = get_config()
            model_config = config.get_custom_llm_config()


            # ReAct Loop: Reasoning -> Acting -> Reasoning -> Acting...
            configured_max_iterations = getattr(self.config, "max_iterations", DEFAULT_INFO_SEEKER_MAX_ITERATIONS)
            self.config.max_iterations = resolve_info_seeker_max_iterations(configured_max_iterations)
            while iteration < self.config.max_iterations and not task_completed:
                iteration += 1
                self.logger.info(f"Planning iteration {iteration}")

                checkpoint = self._agent_intervention_checkpoint(
                    "information_seeker_iteration", iteration
                )
                checkpoint_message = self._intervention_message(
                    checkpoint["instructions"], checkpoint["requested_stage"]
                )
                if checkpoint_message:
                    conversation_history.append({"role": "user", "content": checkpoint_message})
                requested_stage = checkpoint.get("requested_stage")
                if requested_stage and requested_stage.get("stage") != "information_search":
                    task_done_result = self._build_partial_completion(
                        task_input,
                        successful_actions,
                        f"user requested transition to {requested_stage.get('stage')} at a safe checkpoint",
                    )
                    self.log_action(
                        iteration, "info_seeker_subjective_task_done", task_done_result, task_done_result
                    )
                    completion_tool = "info_seeker_subjective_task_done"
                    task_completed = True
                    break

                if self._check_cancellation():
                    return self.create_response(
                        success=False,
                        error="Task cancelled by user",
                        iterations=iteration,
                        execution_time=time.time() - start_time
                    )

                try:
                    # Get LLM response (reasoning + potential tool calls)
                    has_vision = False
                    for msg in conversation_history:
                        if isinstance(msg.get("content"), list):
                            has_vision = True
                            break

                    payload = {
                        "model": self.config.model if hasattr(self.config, 'model') else "pangu_auto",
                        "messages": literature_context.payload_messages(conversation_history),
                        "temperature": self.config.temperature if hasattr(self.config, 'temperature') else 0.3,
                        "max_tokens": self.config.max_tokens if hasattr(self.config, 'max_tokens') else 4096,
                    }

                    # 2. 如果没有图片,才使用文本专属的 chat_template
                    if not has_vision:
                        payload[
                            "chat_template"] = "{% for message in messages %}{% if loop.first and messages[0]['role'] != 'system' %}{{ '<s>[unused9]系统:[unused10]' }}{% endif %}{% if message['role'] == 'system' %}{{'<s>[unused9]系统:' + message['content'] + '[unused10]'}}{% endif %}{% if message['role'] == 'assistant' %}{{'[unused9]助手:' + message['content'] + '[unused10]'}}{% endif %}{% if message['role'] == 'tool' %}{{'[unused9]工具:' + message['content'] + '[unused10]'}}{% endif %}{% if message['role'] == 'function' %}{{'[unused9]方法:' + message['content'] + '[unused10]'}}{% endif %}{% if message['role'] == 'user' %}{{'[unused9]用户:' + message['content'] + '[unused10]'}}{% endif %}{% endfor %}{% if add_generation_prompt %}{{ '[unused9]助手:' }}{% endif %}"
                        payload["spaces_between_special_tokens"] = False

                    retry_num = 1
                    max_retry_num = 10
                    while retry_num < max_retry_num:
                        try:
                            response = chat_completion_response(
                                payload,
                                model_config=model_config,
                                agent_name=self.config.agent_name,
                                request_logger=self.logger,
                            )

                            status_code = response.status_code
                            try:
                                response_json = response.json()
                            except:
                                response_json = {"error": {"message": response.text, "code": status_code}}

                            # 3. 精准捕获限流 (429 / Quota)
                            if status_code == 429 or (isinstance(response_json, dict) and "error" in response_json):
                                err = response_json.get("error", {})
                                err_code = str(err.get("code", status_code))
                                err_msg = str(err.get("message", "")).lower()

                                if err_code == "429" or "rate limit" in err_msg or "429" in err_msg or "quota" in err_msg or "throttling" in err_msg:
                                    self.logger.warning(
                                        f"⚠️ 触发 API 限流或额度超限 (429).Agent 将沉睡 30 秒后继续... (第 {retry_num}/{max_retry_num} 次尝试)")

                                    time.sleep(30)
                                    retry_num += 1
                                    continue

                            # 正常错误检查
                            if "choices" not in response_json or not response_json["choices"]:
                                error_info = response_json.get("error", "未知 API 错误")
                                self.logger.error(f"API 响应异常: {response_json}")
                                assistant_message = {
                                    "role": "assistant",
                                    "content": f"[unused16][unused17] 错误:API 请求失败({error_info}).请尝试调整策略."
                                }
                            else:
                                assistant_message = response_json["choices"][0]["message"]

                            self.logger.debug("API response received successfully")
                            break  # 成功,跳出重试循环

                        except Exception as e:
                            err_msg = str(e).lower()
                            if "429" in err_msg or "rate limit" in err_msg or "quota" in err_msg or "throttling" in err_msg:
                                self.logger.warning(
                                    f"⚠️ 捕获到网络层限流异常.Agent 将沉睡 60 秒后继续... (第 {retry_num}/{max_retry_num} 次尝试)")

                                time.sleep(60)
                            else:
                                self.logger.warning(f"API 请求失败: {e},3秒后重试...")

                                time.sleep(3)

                            retry_num += 1
                            if retry_num == max_retry_num:
                                raise ValueError(str(e))
                            continue


                    # Log the reasoning
                    assistant_content = assistant_message.get("content") or ""
                    if not assistant_content.strip():
                        no_tool_streak += 1
                        self.logger.warning(
                            f"Empty assistant response in information seeker iteration {iteration} "
                            f"(streak={no_tool_streak})"
                        )
                        if no_tool_streak >= 3:
                            task_done_result = self._build_partial_completion(
                                task_input,
                                successful_actions,
                                "the model returned empty responses repeatedly"
                            )
                            self.log_action(
                                iteration,
                                "info_seeker_subjective_task_done",
                                task_done_result,
                                task_done_result
                            )
                            task_completed = True
                            break
                        conversation_history.append({
                            "role": "user",
                            "content": (
                                "Your last response was empty. Return exactly one executable tool call in "
                                "[unused11] JSON format, or call info_seeker_subjective_task_done with a concise "
                                "summary of the information already gathered. /no_think"
                            )
                        })
                        continue

                    try:
                        reasoning_content = self._safe_extract_reasoning(assistant_content)
                        if reasoning_content:
                            self.log_reasoning(iteration, reasoning_content)
                    except Exception as e:
                        self.logger.warning(f"Tool call parsing error: {e}")
                        # Parse error, rerun
                        followup_prompt = f"There is a problem with the format of model generation: {e}. Please try again."
                        conversation_history.append({"role": "user", "content": followup_prompt + " /no_think"})
                        continue


                    # def extract_tool_calls(content):
                    #     import re
                    #     if not content:
                    #         return []
                    #     tool_call_str = re.findall(r"\[unused11\]([\s\S]*?)\[unused12\]", content)
                    #     if len(tool_call_str) > 0:
                    #         try:
                    #             tool_calls = json.loads(tool_call_str[0].strip())
                    #         except Exception as ee:
                    #             return ["fail_tools_load", ee]
                    #     else:
                    #         return []
                    #     return tool_calls
                    # def extract_tool_calls(content):
                    #     import re
                    #     if not content:
                    #         return []
                    #
                    #     # 查找所有匹配的内容
                    #     tool_call_matches = re.findall(r"\[unused11\]([\s\S]*?)\[unused12\]", content)
                    #
                    #     # 增加判空校验,防止 index out of range
                    #     if not tool_call_matches:
                    #         return []
                    #
                    #     try:
                    #         # 取第一个匹配项并尝试解析 JSON
                    #         first_match = tool_call_matches[0].strip()
                    #         tool_calls = json.loads(first_match)
                    #         return tool_calls if isinstance(tool_calls, list) else [tool_calls]
                    #     except Exception as e:
                    #         logger.error(f"解析工具调用 JSON 失败: {e}")
                    #         return []

                    # Add assistant message to conversation
                    conversation_history.append({
                        "role": "assistant",
                        "content": assistant_content
                    })

                    tool_calls = self.extract_tool_calls(assistant_content)

                    if (
                        tool_calls
                        and isinstance(tool_calls[0], dict)
                        and tool_calls[0].get("name") == "system_error_feedback"
                    ):
                        raw_content = assistant_message.get("content", "")
                        for tdn in ["info_seeker_subjective_task_done", "info_seeker_objective_task_done"]:
                            if tdn in raw_content:
                                recovered_args = {
                                    "task_summary": raw_content[:2000],
                                    "completion_status": "completed_with_json_recovery",
                                }
                                self.logger.warning(f"Recovered malformed {tdn} call and marked task complete")
                                self.log_action(iteration, tdn, recovered_args, recovered_args)
                                task_completed = True
                                task_done_result = recovered_args
                                break
                        if task_completed:
                            break
                        format_error_streak += 1
                        if format_error_streak >= 2:
                            error = "连续两次工具调用格式错误，已停止当前调研子任务，避免无效循环。"
                            if successful_actions:
                                task_done_result = self._build_partial_completion(
                                    task_input, successful_actions,
                                    "two later tool calls had invalid formatting; earlier successful evidence was preserved",
                                )
                                self.logger.warning("%s 已保留前面成功取得的检索结果。", error)
                                self.log_action(
                                    iteration, "info_seeker_subjective_task_done",
                                    task_done_result, task_done_result,
                                )
                                return self.create_response(
                                    success=True,
                                    result=task_done_result,
                                    iterations=iteration,
                                    execution_time=time.time() - start_time,
                                )
                            self.logger.error(error)
                            return self.create_response(
                                success=False,
                                error=error,
                                iterations=iteration,
                                execution_time=time.time() - start_time,
                            )
                    else:
                        format_error_streak = 0

                    if len(tool_calls) > 0 and tool_calls[0] == "fail_tools_load":
                        # Parse error, rerun
                        followup_prompt = f"There was a parsing error in the format of the tool call" \
                                          f" you generated:{tool_calls[1]} Please regenerate it."
                        conversation_history.append({"role": "user", "content": followup_prompt + " /no_think"})
                        continue


                    # Execute tool calls if any (Acting phase)
                    if tool_calls:
                        no_tool_streak = 0
                    intervention_deferred_tools = False
                    duplicate_tool_blocked = False
                    for tool_call in tool_calls:
                        arguments = tool_call.get("arguments", {})
                        tool_name = tool_call.get("name", "")

                        tool_checkpoint = self._agent_intervention_checkpoint(
                            "information_seeker_tool_boundary", iteration, tool_name
                        )
                        tool_checkpoint_message = self._intervention_message(
                            tool_checkpoint["instructions"], tool_checkpoint["requested_stage"]
                        )
                        if tool_checkpoint_message:
                            conversation_history.append({"role": "user", "content": tool_checkpoint_message})
                            intervention_deferred_tools = True
                            requested_stage = tool_checkpoint.get("requested_stage")
                            if requested_stage and requested_stage.get("stage") != "information_search":
                                task_done_result = self._build_partial_completion(
                                    task_input,
                                    successful_actions,
                                    f"user requested transition to {requested_stage.get('stage')} after the previous tool",
                                )
                                self.log_action(
                                    iteration, "info_seeker_subjective_task_done", task_done_result, task_done_result
                                )
                                completion_tool = "info_seeker_subjective_task_done"
                                task_completed = True
                            break

                        # ========== 这里的任务完成判断保留各个 Agent 原有的 ==========
                        # (如果是 Planner,这里可能是 planner_subjective_task_done 等,不要改动这一小段if)
                        if tool_name in ["info_seeker_subjective_task_done", "info_seeker_objective_task_done",
                                         "writer_subjective_task_done", "planner_subjective_task_done",
                                         "planner_objective_task_done", "experiment_task_done"]:
                            task_completed = True
                            completion_tool = tool_name
                            self.log_action(iteration, tool_name, arguments, arguments)
                            break
                        # ============================================================

                        if tool_name not in ["think", "reflect"]:
                            signature = self.tool_call_signature(tool_name, arguments)
                            if signature == last_tool_signature:
                                repeated_tool_call_count += 1
                            else:
                                last_tool_signature = signature
                                repeated_tool_call_count = 1

                            if repeated_tool_call_count >= 3 and successful_actions:
                                task_done_result = self._build_partial_completion(
                                    task_input,
                                    successful_actions,
                                    f"stopped after the exact same {tool_name} call was generated three times"
                                )
                                self.logger.warning(
                                    "相同工具调用连续出现 3 次，已保留现有检索结果并结束当前子任务: %s",
                                    tool_name,
                                )
                                self.log_action(
                                    iteration,
                                    "info_seeker_subjective_task_done",
                                    task_done_result,
                                    task_done_result,
                                )
                                completion_tool = "info_seeker_subjective_task_done"
                                task_completed = True
                                break

                            if repeated_tool_call_count >= 2:
                                self.logger.warning(
                                    "已拦截连续重复工具调用，要求模型更换搜索或结束当前子任务: %s",
                                    tool_name,
                                )
                                conversation_history.append({
                                    "role": "tool",
                                    "content": json.dumps({
                                        "success": False,
                                        "error": (
                                            f"Duplicate call blocked: {tool_name} was already executed with "
                                            "the same arguments. Use a different query/tool, or call "
                                            "info_seeker_subjective_task_done with the evidence already gathered."
                                        ),
                                    }, ensure_ascii=False) + " /no_think",
                                })
                                duplicate_tool_blocked = True
                                break

                        if tool_name in ["think", "reflect"]:
                            tool_result = {"tool_results": "You can proceed to invoke other tools if needed."}
                        else:
                            tool_result = self.execute_tool_call(tool_call)

                        prompt_tool_result = literature_context.capture(tool_name, arguments, tool_result)

                        # Log the action using base class method
                        self.log_action(iteration, tool_name, arguments, tool_result)
                        if not isinstance(tool_result, dict) or tool_result.get("success", True):
                            successful_actions.append({
                                "tool": tool_name,
                                "arguments": arguments,
                                "result": self._safe_short_text(tool_result, 600)
                            })

                        # 4. 识别图片结果,并构造符合 OpenAI 视觉标准的 List
                        is_vision = False
                        image_url = ""
                        if isinstance(tool_result, dict) and "data" in tool_result and isinstance(tool_result["data"],
                                                                                                  dict):
                            if tool_result["data"].get("is_vision_content"):
                                is_vision = True
                                image_url = tool_result["data"].get("image_url", "")

                        if is_vision:
                            # 对于图片,不能直接塞进 tool 角色,需要转交给 user 角色透传
                            conversation_history.append({
                                "role": "tool",
                                "content": json.dumps({"status": "success",
                                                       "message": "Image loaded successfully. Please see the next user message for the image content."},
                                                      ensure_ascii=False) + " /no_think"
                            })
                            conversation_history.append({
                                "role": "user",
                                "content": [
                                    {"type": "text",
                                     "text": f"这是你请求的图片(路径:{arguments.get('file_path')}),请根据当前任务提取图中可见的关键标签、数值、趋势、图例和结论,不要预设某一类指标: /no_think"},
                                    {"type": "image_url", "image_url": {"url": image_url}}
                                ]
                            })
                        else:
                            # 正常文本工具的返回
                            conversation_history.append({
                                "role": "tool",
                                "content": json.dumps(prompt_tool_result, ensure_ascii=False, indent=2) + " /no_think"
                            })

                    if task_completed:
                        break
                    if duplicate_tool_blocked:
                        continue
                    if intervention_deferred_tools:
                        continue

                    # If no tool calls, encourage continued search
                    # ====== BRUTE FORCE: check if model called task_done in reasoning markers ======
                    if len(tool_calls) == 0:
                        raw_content = assistant_message.get("content", "")
                        import re as _re
                        task_done_names = ["info_seeker_subjective_task_done", "info_seeker_objective_task_done", "writer_subjective_task_done", "planner_subjective_task_done", "planner_objective_task_done", "experiment_task_done"]
                        extracted = False
                        for tdn in task_done_names:
                            if tdn in raw_content:
                                # Try to extract JSON
                                json_match = _re.search(r'\{[\s\S]*"task_summary"[\s\S]*\}', raw_content)
                                if not json_match:
                                    json_match = _re.search(r'\{[\s\S]*"summary"[\s\S]*\}', raw_content)
                                if json_match:
                                    try:
                                        args = json.loads(json_match.group(0))
                                        if args and isinstance(args, dict):
                                            self.logger.info(f"Brute-force extracted {tdn} from reasoning content")
                                            self.log_action(iteration, tdn, args, args)
                                            task_completed = True
                                            task_done_result = args
                                            extracted = True
                                            break
                                    except:
                                        pass
                        if extracted:
                            break  # exit while loop
                    # ====== END BRUTE FORCE ======

                    if len(tool_calls) == 0:
                        no_tool_streak += 1

                        if no_tool_streak >= 3:
                            if successful_actions:
                                task_done_result = self._build_partial_completion(
                                    task_input,
                                    successful_actions,
                                    "the model produced no executable tool calls repeatedly"
                                )
                                self.log_action(
                                    iteration,
                                    "info_seeker_subjective_task_done",
                                    task_done_result,
                                    task_done_result
                                )
                                task_completed = True
                                break
                            conversation_history.append({
                                "role": "user",
                                "content": "You have not used any tools for 3 consecutive responses. Call info_seeker_subjective_task_done NOW with your findings. /no_think"
                            })
                        else:
                            followup_prompt = (
                                "Continue your analysis. If you need more information, use available tools. "
                                "If you have enough information to answer the question, call info_seeker_subjective_task_done with your complete context."
                            )
                            conversation_history.append({"role": "user", "content": followup_prompt + " /no_think"})
                    if iteration == self.config.max_iterations-3:
                        followup_prompt = "Due to length and number of rounds restrictions, you must now call the `info_seeker_subjective_task_done` tool to report your findings immediately. /no_think"
                        conversation_history.append({"role": "user", "content": followup_prompt})


                except Exception as e:
                    error_msg = f"Error in planning iteration {iteration}: {e}"
                    self.log_error(iteration, error_msg)
                    break
            
            execution_time = time.time() - start_time
            # Extract final result
            if task_completed:
                # 按实际触发完成的工具名回溯结果,避免只认 info_seeker_subjective_task_done 而丢失结果
                done_tools = [completion_tool] if completion_tool else [
                    "info_seeker_subjective_task_done", "info_seeker_objective_task_done",
                    "writer_subjective_task_done", "planner_subjective_task_done",
                    "planner_objective_task_done", "experiment_task_done"
                ]
                task_done_result = None
                for step in reversed(self.reasoning_trace):
                    if step.get("type") == "action" and step.get("tool") in done_tools:
                        task_done_result = step.get("result")
                        break
                if task_done_result is None:
                    self.logger.warning(
                        f"任务标记完成(via {completion_tool}),但未能从 trace 中回溯到完成结果,result 为空"
                    )

                # ---- Save paper_structures to shared log ----
                if task_done_result and isinstance(task_done_result, dict):
                    paper_structures = task_done_result.get("paper_structures", [])
                    if paper_structures:
                        try:
                            import os as _os, re as _re
                            log_dir = "./research/paper_structures"
                            lit_dir = "./research/literature"
                            _os.makedirs(log_dir, exist_ok=True)
                            _os.makedirs(lit_dir, exist_ok=True)
                            log_path = _os.path.join(log_dir, "paper_structure_log.md")
                            with open(log_path, "a", encoding="utf-8") as _f:
                                session_id = _os.environ.get("CODEX_SESSION_ID", "unknown")
                                _f.write("\n## Session: " + str(session_id) + "\n")
                                for ps in paper_structures:
                                    title = ps.get("paper_title", "Unknown")
                                    summary = ps.get("paper_summary", "")
                                    file_path = ps.get("file_path", "")
                                    figs = ps.get("figure_types", [])
                                    tabs = ps.get("table_types", [])
                                    fig_cnt = ps.get("figure_count", 0)
                                    tab_cnt = ps.get("table_count", 0)

                                    # Save individual paper summary to literature/
                                    safe_name = _re.sub(r'[\\/*?:"<>|]', '_', title)[:80]
                                    lit_path = _os.path.join(lit_dir, safe_name + ".md")
                                    with open(lit_path, "w", encoding="utf-8") as _lf:
                                        _lf.write("# " + str(title) + "\n\n")
                                        _lf.write("**Source**: " + str(file_path) + "\n\n")
                                        _lf.write("## 内容摘要\n\n" + str(summary) + "\n\n")
                                        _lf.write("## 图表结构\n")
                                        _lf.write("- Figures (" + str(fig_cnt) + "): " + (", ".join(figs) if figs else "N/A") + "\n")
                                        _lf.write("- Tables (" + str(tab_cnt) + "): " + (", ".join(tabs) if tabs else "N/A") + "\n")

                                    # Append to aggregate log
                                    _f.write("### " + str(title) + "\n")
                                    if summary:
                                        _f.write("- Summary: " + str(summary)[:300] + "...\n")
                                    if file_path:
                                        _f.write("- Source: " + str(file_path) + "\n")
                                    figs_str = ", ".join(figs) if figs else "N/A"
                                    tabs_str = ", ".join(tabs) if tabs else "N/A"
                                    _f.write("- Figures (" + str(fig_cnt) + "): " + figs_str + "\n")
                                    _f.write("- Tables (" + str(tab_cnt) + "): " + tabs_str + "\n")
                                    _f.write("\n")
                            self.logger.info("Saved " + str(len(paper_structures)) + " paper structures to " + log_dir)
                        except Exception as _e:
                            self.logger.warning("Failed to save paper_structures: " + str(_e))
                # ---- END paper_structures save ----

                return self.create_response(
                    success=True,
                    result=task_done_result,
                    iterations=iteration,
                    execution_time=execution_time
                )
            else:
                if successful_actions:
                    task_done_result = self._build_partial_completion(
                        task_input,
                        successful_actions,
                        f"iteration budget reached after {self.config.max_iterations} rounds"
                    )
                    self.log_action(
                        iteration,
                        "info_seeker_subjective_task_done",
                        task_done_result,
                        task_done_result
                    )
                    return self.create_response(
                        success=True,
                        result=task_done_result,
                        iterations=iteration,
                        execution_time=execution_time
                    )
                return self.create_response(
                    success=False,
                    error=f"Task not completed within {self.config.max_iterations} iterations",
                    iterations=iteration,
                    execution_time=execution_time
                )
                
        except Exception as e:
            execution_time = time.time() - start_time
            self.logger.error(f"Error in execute_task: {e}")
            return self.create_response(
                success=False,
                error=str(e),
                iterations=iteration if 'iteration' in locals() else 0,
                execution_time=execution_time
            )

    def _build_agent_specific_tool_schemas(self) -> List[Dict[str, Any]]:
        """
        Build tool schemas for InformationSeekerAgent using proper MCP architecture.
        Schemas come from MCP server via client, not direct imports.
        """
        # Get MCP tool schemas from server via client (proper MCP architecture)
        schemas = super()._build_agent_specific_tool_schemas()

        # Add schemas for built-in task assignment tools
        builtin_assignment_schemas = [
            {
                "type": "function",
                "function": {
                    "name": "think",
                    "description": "Use the tool to think about something. It will not obtain new information or make any changes to the repository, but just log the thought. Use it when complex reasoning or brainstorming is needed.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "thought": {
                                "type": "string",
                                "description": "Your thoughts."
                            }
                        },
                        "required": ["thought"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "reflect",
                    "description": "When multiple attempts yield no progress, use this tool to reflect on previous reasoning and planning, considering possible overlooked clues and exploring more possibilities. It will not obtain new information or make any changes to the repository.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "reflect": {
                                "type": "string",
                                "description": "The specific content of your reflection"
                            }
                        },
                        "required": ["reflect"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "info_seeker_subjective_task_done",
                    "description": "Information Seeker Agent task completion reporting with information collection summary and related files.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task_summary": {
                                "type": "string",
                                "description": "Simple summary of what information has been collected for the current task and what new discoveries have been made.",
                                "format": "markdown"
                            },
                            "key_files": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "file_path": {
                                            "type": "string",
                                            "description": "Relative path to the file with collected content"
                                        },
                                    },
                                    "required": ["file_path"]
                                },
                                "description": "Collect files highly relevant to this task. "
                            },
                            "completion_status": {
                                "type": "string",
                                "enum": ["completed", "partial", "failed"],
                                "description": "Final status of the information gathering task"
                            },
                            "completion_analysis": {
                                "type": "string",
                                "description": "Brief analysis of task completion quality, information thoroughness, and any limitations or gaps."
                            },
                            "paper_structures": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "paper_title": {"type": "string", "description": "论文标题"},
                                        "paper_summary": {"type": "string", "description": "该论文的核心内容摘要(200-500字中文),包括研究问题、方法、主要结果和结论"},
                                        "file_path": {"type": "string", "description": "爬取保存的论文文件路径,如 ./url_crawler_save_files/research/xxx.md"},
                                        "figure_types": {"type": "array", "items": {"type": "string"}, "description": "论文中使用的图表类型列表"},
                                        "table_types": {"type": "array", "items": {"type": "string"}, "description": "论文中使用的表格类型列表"},
                                        "figure_count": {"type": "integer", "description": "论文中的图表总数"},
                                        "table_count": {"type": "integer", "description": "论文中的表格总数"}
                                    },
                                    "required": ["paper_title", "paper_summary", "figure_types", "table_types"]
                                },
                                "description": "从每篇检索到的论文中提取的内容摘要和图表结构模式.每篇论文一条记录."
                            }
                        },
                        "required": ["task_summary", "key_files", "completion_status", "completion_analysis"]
                    }
                }
            },
        ]

        schemas.extend(builtin_assignment_schemas)

        return schemas


# Factory function for creating the agent
def create_subjective_information_seeker(
    model: str = "pangu_auto",
    max_iterations: int = 20,
    shared_mcp_client=None,
    **kwargs
) -> InformationSeekerAgent:
    """
    Create an InformationSeekerAgent instance with server-managed sessions.
    
    Args:
        model: The LLM model to use
        max_iterations: Maximum number of iterations
        shared_mcp_client: Optional shared MCP client from parent agent (prevents extra sessions)
        **kwargs: Additional configuration options
        
    Returns:
        Configured InformationSeekerAgent instance with appropriate tools
    """
    # Import the enhanced config function
    from .base_agent import create_agent_config
    
    # Create agent configuration (session managed by MCP server)
    config = create_agent_config(
        agent_name="InformationSeekerAgent",
        model=model,
        max_iterations=max_iterations,
        **kwargs
    )
    
    # Create agent instance with shared MCP client (filtered tools for information seeking)
    agent = InformationSeekerAgent(config=config, shared_mcp_client=shared_mcp_client)
    
    return agent
