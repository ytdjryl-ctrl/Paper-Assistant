# Copyright (c) 2025 Huawei Technologies Co., Ltd. All rights reserved.
# Copyright (c) 2026 South China Sea Institute of Oceanology, Chinese Academy of Sciences (SCSIO, CAS). All rights reserved.
import json
import logging
from turtle import end_fill
from typing import Dict, Any, List, Optional, Union
from pathlib import Path
import time
import requests
import os
from .base_agent import BaseAgent, AgentConfig, AgentResponse, TaskInput
from ..utils.llm_client import chat_completion_response
from ..utils.skill_loader import get_skill_loader
from .information_context import LiteratureContextLedger

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
    
    def set_cancellation_token(self, cancellation_token):
        """
        Set the cancellation token for this agent
        设置此代理的取消令牌
        
        Args:
            cancellation_token: threading.Event object that will be set when task should be cancelled
        """
        self._cancellation_token = cancellation_token
    
    def _check_cancellation(self) -> bool:
        """
        Check if task has been cancelled
        检查任务是否已被取消
        
        Returns:
            True if task should be cancelled, False otherwise
        """
        if self._cancellation_token and self._cancellation_token.is_set():
            self.logger.info("InformationSeekerAgent task cancellation detected")
            return True
        return False
    
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
        6. Call info_seeker_objective_task_done to provide a structured summary
        
        ### Optimized Workflow:
        Follow this optimized workflow for information gathering:
        
		0. **MANDATORY FIRST STEP - Check Workspace for Existing Files:**
		   - Check `./user_uploads/` directory for user-uploaded files (HIGH PRIORITY)
		   - Check `./library_refs/` directory for user-selected library files (NORMAL PRIORITY)
		   - **CRITICAL REQUIREMENT:** When calling `document_extract`, you MUST include ALL document files from BOTH directories:
		     * Include ALL .pdf, .doc, .docx files (source documents)
		     * Include ALL .txt files that are NOT converted from other documents (e.g., research/*.txt)
		     * The system will automatically skip .pdf.txt, .doc.txt, .docx.txt if the source file exists
		   - In Hybrid mode, read `research/workspace_digest.json` first and open only files needed for unresolved evidence gaps.
		   - **CRITICAL:** Do NOT skip library_refs files even if user_uploads has files
		   - Only proceed to web search after analyzing existing files

		1. INITIAL RESEARCH:
           - Generate 3-8 focused queries for unresolved Claims-Evidence gaps. Add queries only when prior results expose a new gap.
           - **QUERY FORMAT WARNING**: When writing search queries in the JSON arguments,
             DO NOT wrap individual query strings with extra quotes or escape characters.
             Wrong: ["\"set.seed(10)\" genomic selection"]
             Correct: ["set.seed 10 genomic selection"]
             If a query needs to express an exact phrase, just write it plainly without 
             inner quotation marks.
           - Select retrieval tools by source and task. Use `academic_search` for structured Crossref/OpenAlex/configured ScienceDirect journal metadata; PubMed/Medrxiv for biological, medical, agriculture, and nondestructive-assessment evidence; arXiv for computer science; and `batch_web_search` for general web discovery. PubMed search results already contain batched verified metadata and abstracts.
           - Do not use PubMed for generic neural-network architecture terms. Reserve it for biological, medical, agriculture, and nondestructive-assessment evidence.
           - After three empty, irrelevant, or blocked calls in one query family, switch source or stop that branch.
           - Analyze the search results (titles, snippets, URLs, article id, article abstract...) to identify promising sources

        2. CONTENT EXTRACTION (PDF-first strategy):
           a) For PubMed, Medrxiv, and Arxiv papers, use "get_pubmed_article",
              "medrxiv_read_paper", or "arxiv_read_paper" to retrieve full text when available. Call full-text tools only for the top 3-5 candidates in one assigned task, never for every search result. A `metadata_abstract_only` result is successful limited evidence and must not be retried.
           a2) For a selected ScienceDirect result, use `get_sciencedirect_article` through the official Elsevier API when configured; never crawl the ScienceDirect page directly.
           b) For web pages found by batch_web_search:
              - First try to use `download_files` to download the full-text PDF and save it under
                `./url_crawler_save_files/research/pdfs/`.
              - If the PDF cannot be downloaded, use `jina_reader` to read the web page content.
              - If `jina_reader` also fails, save the URL reference under
                `./url_crawler_save_files/research/urls/` as a `.url.txt` file containing the URL.
           c) Save all successfully retrieved content (full-text PDF or web page text) as `.txt`
              under `./url_crawler_save_files/research/txts/` when text conversion is available.
           d) Directory convention:
              - ./url_crawler_save_files/research/pdfs/     <- downloaded PDF files
              - ./url_crawler_save_files/research/txts/     <- converted text files
              - ./url_crawler_save_files/research/urls/     <- URL-only references when content cannot be downloaded
           e) Do not fabricate paper content. If only a URL reference is available, clearly mark it as URL-only evidence.
           f) Respect the source count and evidence depth requested by the task. If no count is specified, collect a focused set of about 3-8 high-quality sources, then summarize limitations instead of chasing volume.
        
        3.CONTENT ANALYSIS:
           - Use `document_qa` to ask specific questions about the saved files:
                a) Formulate focused questions to extract key insights
                b) Use answers to deepen your understanding
           - You can ask multiple questions about the same file
           - Use `document_extract` for multi-dimensional analysis of saved files:
        		a) Provides structured analysis across five key dimensions: doc time, source, authority, core content and task relevance.

        4. FILE MANAGEMENT:
           - Use `file_write` to save important findings or summaries
           - For reviewing saved content:
                a) Prefer `document_qa` to ask specific questions about the content
				b) Prefer `document_extract` to get comprehensive multi-dimensional analysis of saved files
                c) Use `file_read` ONLY for small files (<1000 tokens) when you need the entire content
                d) Avoid reading large files directly as it may exceed context limits
        
        5. TASK COMPLETION:
           - **MANDATORY: Before calling task_done, you MUST create a comprehensive reference summary file!**
           - Save ALL collected references to a task-appropriate markdown file such as `reference_summary.md` in the workspace root directory
           - The reference file MUST include for EACH paper:
             * Full citation (authors, year, title, journal, DOI/PMID)
             * Data, materials, or evidence used
             * Methods, analysis workflow, or experimental design
             * Parameters, settings, assumptions, or protocols when reported
             * Task-relevant findings, metrics, statistical results, limitations, and relevance
           - **DO NOT call task_done without creating this reference summary file!**
           - Search results may be returned as a compact durable literature ledger with IDs such as L001. Full raw batches are saved at the reported path. Use the ledger to avoid duplicate searches; retrieve a saved batch only when a specific record needs verification.
           - When ready to report, call `info_seeker_objective_task_done` with:
                a) Comprehensive markdown summary of your process and findings
                b) List of key files created with descriptions
                c) **MUST include the path to your reference summary file in key_files!**
        
        ### Usage of Systematic Tool:
            - `think` is a systematic tool. After receiving the response from the complex tool or before invoking any other tools, you must **first invoke the `think` tool**: to deeply reflect on the results of previous tool invocations (if any), and to thoroughly consider and plan the user's task. The `think` tool does not acquire new information; it only saves your thoughts into memory.
            - `reflect` is a systematic tool. When encountering a failure in tool execution, it is necessary to invoke the reflect tool to conduct a review and revise the task plan. It does not acquire new information; it only saves your thoughts into memory.
        
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
        message += "4. Call task_done when ready to provide your complete findings\n\n"
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
            "key_files": []
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
            conversation_history.append({"role": "user", "content": user_message+" /no_think"})


            iteration = 0
            task_completed = False
            task_done_result = None
            completion_tool = None  # 记录实际触发完成的 done 工具名,用于精确回溯结果
            successful_actions = []
            no_tool_streak = 0
            format_error_streak = 0
            last_tool_signature = None
            repeated_tool_call_count = 0
            literature_context = LiteratureContextLedger(self, task_input, "objective")
            # Get model endpoint configuration from env-backed config
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
                        iteration, "info_seeker_objective_task_done", task_done_result, task_done_result
                    )
                    completion_tool = "info_seeker_objective_task_done"
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
                                "info_seeker_objective_task_done",
                                task_done_result,
                                task_done_result
                            )
                            task_completed = True
                            break
                        conversation_history.append({
                            "role": "user",
                            "content": (
                                "Your last response was empty. Return exactly one executable tool call in "
                                "[unused11] JSON format, or call info_seeker_objective_task_done with a concise "
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
                    #         except:
                    #             return []
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
                        for tdn in ["info_seeker_objective_task_done", "info_seeker_subjective_task_done"]:
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
                                    iteration, "info_seeker_objective_task_done",
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
                                    iteration, "info_seeker_objective_task_done", task_done_result, task_done_result
                                )
                                completion_tool = "info_seeker_objective_task_done"
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
                                    "info_seeker_objective_task_done",
                                    task_done_result,
                                    task_done_result,
                                )
                                completion_tool = "info_seeker_objective_task_done"
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
                                            "info_seeker_objective_task_done with the evidence already gathered."
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

                    # If no tool calls, encourage continued planning
                    # ====== BRUTE FORCE: check if model called task_done in reasoning markers ======
                    if len(tool_calls) == 0:
                        raw_content = assistant_message.get("content", "")
                        import re as _re
                        task_done_names = ["info_seeker_subjective_task_done", "info_seeker_objective_task_done", "writer_subjective_task_done", "planner_subjective_task_done", "planner_objective_task_done", "experiment_task_done"]
                        extracted = False
                        for tdn in task_done_names:
                            if tdn in raw_content:
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
                        if no_tool_streak >= 3 and successful_actions:
                            task_done_result = self._build_partial_completion(
                                task_input,
                                successful_actions,
                                "the model produced no executable tool calls repeatedly"
                            )
                            self.log_action(
                                iteration,
                                "info_seeker_objective_task_done",
                                task_done_result,
                                task_done_result
                            )
                            task_completed = True
                            break
                        # Add follow-up prompt to encourage action or completion
                        followup_prompt = (
                            "Continue your planning process. Use available tools to assign tasks to agents, "
                            "search for information, or coordinate work. When you have a complete answer, "
                            "call info_seeker_objective_task_done. /no_think"
                        )
                        conversation_history.append({"role": "user", "content": followup_prompt})
                    if iteration == self.config.max_iterations-3:
                        followup_prompt = "Due to length and number of rounds restrictions, you must now call the `info_seeker_objective_task_done` tool to report the completion of your task. /no_think"
                        conversation_history.append({"role": "user", "content": followup_prompt})


                except Exception as e:
                    error_msg = f"Error in planning iteration {iteration}: {e}"
                    self.log_error(iteration, error_msg)
                    break
            
            execution_time = time.time() - start_time
            # Extract final result
            if task_completed:
                # 按实际触发完成的工具名回溯结果,避免只认 info_seeker_objective_task_done 而丢失结果
                done_tools = [completion_tool] if completion_tool else [
                    "info_seeker_objective_task_done", "info_seeker_subjective_task_done",
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
                        "info_seeker_objective_task_done",
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
                    "name": "info_seeker_objective_task_done",
                    "description": "Structured reporting of task completion details including summary, decisions, outputs, and status",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "task_summary": {
                                "type": "string",
                                "description": "Comprehensive markdown covering what the agent was asked to do, steps taken, tools used, key findings, files created, challenges, and final deliverables.",
                                "format": "markdown"
                            },
                            "task_name": {
                                "type": "string",
                                "description": "The name of the task currently assigned to the agent, usually with underscores (e.g., 'web_research_ai_trends')"
                            },
                            "key_files": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "file_path": {
                                            "type": "string",
                                            "description": "Relative path to created/modified file"
                                        },
                                        "desc": {
                                            "type": "string",
                                            "description": "File contents and creation purpose"
                                        },
                                        "is_final_output_file": {
                                            "type": "boolean",
                                            "description": "Whether file is primary deliverable"
                                        }
                                    },
                                    "required": ["file_path", "desc", "is_final_output_file"]
                                },
                                "description": "List of key files generated or modified during the task, with their details."
                            },
                            "completion_status": {
                                "type": "string",
                                "enum": ["completed", "partial", "failed"],
                                "description": "Final task status"
                            }
                        },
                        "required": ["task_summary", "task_name", "key_files", "completion_status"]
                    }
                }
            },
        ]

        schemas.extend(builtin_assignment_schemas)

        return schemas


# Factory function for creating the agent
def create_objective_information_seeker(
    model: Any = None,
    max_iterations: Any = None,
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
    from ..agents.base_agent import create_agent_config
    
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
