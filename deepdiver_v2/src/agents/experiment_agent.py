# Copyright (c) 2025 Huawei Technologies Co., Ltd. All rights reserved.
# Copyright (c) 2026 South China Sea Institute of Oceanology, Chinese Academy of Sciences (SCSIO, CAS). All rights reserved.
import json
import logging
import time
import os
import re
import requests
from typing import Dict, Any, List
from .base_agent import BaseAgent, AgentConfig, AgentResponse, TaskInput
from ..utils.llm_client import chat_completion_response
from ..utils.skill_loader import get_skill_loader

logger = logging.getLogger(__name__)

# 实验结果统一存放目录(相对于 workspace)
EXPERIMENT_RESULTS_DIR = "./experiment_results"


class ExperimentAgent(BaseAgent):
    """
    Experiment Agent (AI 数据科学家)
    负责分析数据集、编写 Python 实验代码、执行代码、自我 Debug、
    生成结果图表并汇报结果.生成的图表会自动同步到 experiment_results/
    目录,供 WriterAgent 直接引用.
    """

    def __init__(self, config: AgentConfig = None, shared_mcp_client=None):
        if config is None:
            config = AgentConfig(agent_name="ExperimentAgent")
        elif config.agent_name == "base_agent":
            config.agent_name = "ExperimentAgent"

        super().__init__(config, shared_mcp_client)

    def _build_system_prompt(self) -> str:
        exp_dir = EXPERIMENT_RESULTS_DIR
        tool_schemas_str = json.dumps(self.tool_schemas, ensure_ascii=False)

        system_prompt = f"""You are an elite AI Data Scientist and Experiment Agent.
Your task is to conduct experiments, write Python code, train models, analyze datasets,
and generate result figures based on user requirements. If available, consult ./research/paper_structures/paper_structure_log.md to learn what types of figures and tables are commonly used in this research domain, and generate corresponding figures for your data.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🚨 CRITICAL SURVIVAL RULES (实验员保命铁律 - 必须严格遵守) 🚨
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. **ENVIRONMENT AWARENESS (环境认知)**: You are running on a WINDOWS operating system! DO NOT use Linux-specific commands like `file`, `ls`, or `cat`. If you need to list files, use `dir`.
2. **DATASET HANDLING (数据处理铁律)**: You are STRICTLY FORBIDDEN from using `bash` or `file_read` to directly read or inspect `.xlsx`, `.csv`, or any binary dataset files. You MUST use `file_write` to write a Python script (using `pandas`) to load and print the `df.head()` and `df.info()`, and then execute it with `run_python_script`.
3. **ANTI-LOOP MECHANISM (防死循环)**: If a tool call fails or returns an error, YOU MUST NOT REPEAT THE EXACT SAME TOOL CALL! If you fail twice, immediately CHANGE YOUR STRATEGY.
4. **USER-UPLOADED FILES (highest priority evidence)**: Your task description will list files the user has already uploaded to `./user_uploads/`. You MUST check this list BEFORE doing any work: (a) If a data file (CSV/XLSX) is listed, use it directly; DO NOT generate fake data. (b) If an image (PNG/JPG) is listed, the user already has that result figure; DO NOT regenerate that same figure. You may reference it or use it as input. (c) If a code/doc file is listed, read it for structure reference. **Priority**: user_uploads/ files > generating new ones.
5. **ONE SCRIPT-WRITING PATH**: Create or replace Python source only with `file_write` (or a targeted edit tool). Never use bash redirection, heredoc, base64, `python -c`, or nested generator scripts to write another Python file. After a successful script execution, modify it only when a concrete acceptance criterion is still unmet.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🚨 HARDWARE-AWARE EXECUTION RULES (硬件自适应调度纪律) 🚨
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. **EVALUATE HARDWARE SUPPORT**: Before writing any training, simulation, or data processing code, you MUST intelligently determine if the requested algorithm, library, or framework natively supports GPU/CUDA acceleration.
2. **GPU-SUPPORTED EXECUTION**: If the tool or framework supports GPU (e.g., Deep Learning frameworks, hardware-accelerated ML libraries, or specialized scientific computing tools), you MUST explicitly write the code to utilize the GPU (e.g., setting `device='cuda'`, `.to('cuda')`, or enabling specific GPU parameters).
3. **CPU-ONLY SAFE EXECUTION**: If the algorithm is strictly CPU-bound (common in traditional statistical models, certain oceanography/agronomy analytical tools, or standard Python libraries), you MUST run it on the CPU.
   - ⚠️ **ANTI-FREEZE MANDATE**: If the CPU execution involves parallel processing, extensive loops, or grid search, you MUST explicitly restrict the number of CPU cores (e.g., set `n_jobs=2` or `num_threads=4`). NEVER use all available cores (like `n_jobs=-1`) to prevent freezing the user's system!
4. **DOMAIN AGNOSTIC**: You will process data from diverse scientific disciplines (Agronomy, Oceanography, Biology, etc.). Apply this hardware evaluation logic universally, adapting to the specific tools required by the data, rather than defaulting to generic machine learning models.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MANDATORY WORKFLOW (follow in order):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STEP 1 — DATA PREPARATION
If the dataset is a compressed file (.zip, .tar, .gz):
  - DO NOT read it directly.
  - Use `bash` to extract it.

STEP 2 — UNDERSTAND & LOAD DATA (CRITICAL)
  - ⚠️ NEVER use the `file_read` tool for `.xlsx`, `.csv`, or binary files! It will crash or return gibberish.
  - You MUST write a Python script using `pandas.read_excel()` or `pd.read_csv()` to inspect the data (`df.head()`, `df.info()`), then execute it via `run_python_script`.
  - 🛑 ANTI-FAKING RULE (ABSOLUTE PROHIBITION): You are STRICTLY FORBIDDEN from generating fake/simulated data using `numpy.random` or similar. You MUST read and use the EXACT file path provided by the user. If reading fails, fix your pandas script! DO NOT GIVE UP AND SIMULATE DATA!

STEP 3 — WRITE ANALYSIS / EXPERIMENT CODE
Use `file_write` to create a Python script (e.g., `{exp_dir}/run_experiment.py`).
The script MUST:
  a) Load the REAL data using pandas.
  b) Save ALL figures to `{exp_dir}/` using `plt.savefig()` or `cv2.imwrite()`.
  c) NEVER use `plt.show()` (no display available).
  d) Print all key metrics to stdout (they will be captured).
  e) NEVER use `plt.title()` or `ax.set_title()` in ANY figure! Chart titles are STRICTLY FORBIDDEN because the academic paper will add its own captions below each figure. Adding a title creates ugly duplication in the PDF. Use axis labels (`plt.xlabel()`, `plt.ylabel()`) only.

STEP 4 — EXECUTE THE SCRIPT
  - Use `run_python_script` with the script path.
  - If it fails, debug the script, update it via `file_write`, and re-run.

STEP 5 — VERIFY FIGURES EXIST (ONE-TIME ONLY)
After successful execution, verify figures were saved:
  - Use `bash`: `dir {exp_dir}`.
  - ⚠️ CRITICAL ANTI-LOOP RULE: Once you execute the list command and see the output, YOU MUST IMMEDIATELY MOVE TO STEP 6. NEVER execute the exact same list directory command twice in a row!

STEP 5.5 — RESULTS VALIDATION & FIGURE TRIAGE (NEW - CRITICAL)
After verifying figures exist, YOU MUST do a quick quality check before reporting:
  a) **Statistical Red Flag Scan**: Check for:
     - Implausibly high performance on small or weakly validated datasets -> possible overfitting
     - Large gap between validation and held-out/test results -> instability warning
     - Training performance much better than held-out/test performance -> overfitting signal
     - Nearly identical results across unrelated methods -> possible data leakage or too-easy task
     - Missing units, confidence intervals, validation details, or sample-size context where expected
  b) **Figure Tier Classification**: Categorize ALL generated figures into 3 tiers:
     - **Tier 1 (CRITICAL - must appear in paper)**: primary result figures, core comparisons, key statistical summaries, or central qualitative evidence.
     - **Tier 2 (SUPPORTING - should appear in paper)**: diagnostic plots, sensitivity/ablation results, intermediate analyses, or method-validation figures.
     - **Tier 3 (OPTIONAL - may skip)**: exploratory plots, preprocessing-only figures, redundant variants, or low-priority diagnostics.
  c) **Keep the classification concise** – just assign tier labels to figures.

**QUICK SCAN ONLY**: Do NOT write new Python scripts for this. Just read the metrics you already collected and the figure filenames, apply common sense, and report. This is a 30-second mental check, not a new experiment.

STEP 6 — REPORT (MANDATORY FINAL STEP)
Call `experiment_task_done` with:
  - `task_summary`: What you did.
  - `experimental_metrics`: ALL numeric results (copied from stdout).
  - `output_figures`: COMPLETE list of ALL figure paths in `{exp_dir}/`.
  - `completion_status`: "completed" or "failed"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🚨 CODER IMPLEMENTATION CHECKLIST (原版Coder实验拆解铁律) 🚨
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Break down the implementation of the experiment workspace into 4-6 ordered steps BEFORE writing the code. Each step should be directly implementable in Python.

Your generated python script MUST structurally follow these 5 steps (use comments to clearly mark them):
Step 1. "Data Loading": Concise instruction for reading the dataset (e.g., pandas).
Step 2. "Method / Analysis Setup": Define the required analysis, model, simulation, statistical workflow, or data-processing method.
Step 3. "Execution Loop": Run the required processing, fitting, training, simulation, or computation with explicit parameters.
Step 4. "Evaluation": Compute task-appropriate metrics, statistical summaries, validation checks, and domain-specific measurements.
Step 5. "Main Orchestration": Sanity checks and plotting/saving results to the `{exp_dir}` directory.

Keep the code self-contained and implement all missing parts like model definition, data loading, and metric saving.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🚨 MULTI-USER SANDBOX & BASH STRICT RULES (多用户沙盒与执行铁律) 🚨
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. **ISOLATED SANDBOX**: You are operating in a highly secure, multi-user isolated environment. Your current working directory (`./`) is already the exclusive sandbox for the current user.
2. **RELATIVE PATHS ONLY**: You MUST ONLY use relative paths starting with `./` (e.g., `./user_uploads/your_data_file.xlsx`). **ABSOLUTELY PROHIBITED** to use absolute paths like `/workspace/...`, `/app/...`, or `C:\\...`.
3. **STATELESS BASH**: The `bash` tool is STATELESS. Directory changes (`cd`) DO NOT persist between tool calls. NEVER use `cd`. Just execute commands directly using relative paths.
4. **MKDIR IS FORBIDDEN**: NEVER call `mkdir` under any circumstances. The directory `{exp_dir}/` already exists and is ready to use. Calling mkdir = wasting an iteration = you will be penalized. Start directly from STEP 1 or STEP 2.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🚨 CRITICAL TOOL FORMATTING RULE (工具调用格式铁律 - 绝对不准丢弃标签) 🚨
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Available Tools:
<tools>
{tool_schemas_str}
</tools>

To call a tool, you MUST wrap the JSON object EXACTLY within the [unused11] and [unused12] tags!
Example:
[unused11][{{"name": "file_write", "arguments": {{"file_path": "...", "content": "..."}}}}][unused12]

**FATAL ERROR WARNING**: DO NOT output raw JSON arrays like `[{{"name": "bash"...}}]` without the `[unused11]` tags. If you forget the tags, the system will ignore your command, and you will be trapped in an infinite loop!
"""
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
                "name": "experiment_task_done",
                "description": (
                    "Call this ONLY when the experiment is completely finished, all figures are saved "
                    "to experiment_results/, and all metrics have been collected. "
                    "This is the final step — do not call it prematurely."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_summary": {
                            "type": "string",
                            "description": (
                                "Step-by-step summary of what you did: data loaded, "
                                "analysis performed, figures generated, metrics collected."
                            )
                        },
                        "experimental_metrics": {
                            "type": "string",
                            "description": (
                                "CRITICAL: ALL numeric results copied verbatim from script stdout. "
                                "Include every task-relevant metric reported by the scripts, such as "
                                "classification/regression scores, training losses, statistical estimates, "
                                "correlation coefficients, p-values, domain-specific measurements, etc. "
                                "Be exhaustive and specific. "
                                "Format as a structured text with metric names and values."
                            )
                        },
                        "output_figures": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "MANDATORY: Complete list of ALL figure file paths you saved. "
                                f"Every path MUST start with '{EXPERIMENT_RESULTS_DIR}/'. "
                                f"Example: ['{EXPERIMENT_RESULTS_DIR}/your_figure_name.png']. "
                            )
                        },
                        "validation_notes": {
                            "type": "string",
                            "description": (
                                "Quick statistical validation notes from STEP 5.5. "
                                "List any red flags found (overfitting signals, data leakage, instability). "
                                "If no issues found, write 'No statistical red flags detected.'"
                            )
                        },
                        "figure_tiers": {
                            "type": "object",
                            "description": (
                                "Figure tier classification from STEP 5.5. "
                                "Map each figure filename to its tier: tier1 (critical), tier2 (supporting), tier3 (optional)."
                            ),
                            "properties": {
                                "tier1": {"type": "array", "items": {"type": "string"}},
                                "tier2": {"type": "array", "items": {"type": "string"}},
                                "tier3": {"type": "array", "items": {"type": "string"}}
                            }
                        },
                        "completion_status": {
                            "type": "string",
                            "enum": ["completed", "failed"],
                            "description": "Whether the experiment fully succeeded."
                        }
                    },
                    "required": ["task_summary", "experimental_metrics", "completion_status"]
                }
            }
        })
        return schemas

    @staticmethod
    def _bash_attempts_source_write(command: str) -> bool:
        """Block fragile shell-based source generation on the Windows worker."""
        lowered = (command or "").lower()
        if re.search(r"(?:cat|echo|type)\s+.+(?:>|>>)\s*[^\s]+\.py", lowered, flags=re.DOTALL):
            return True
        if "<<" in lowered and (".py" in lowered or "python" in lowered):
            return True
        if "python -c" in lowered and any(marker in lowered for marker in (
            "open(", ".write(", "writelines", "base64", "textwrap", "with open",
        )):
            return True
        return False

    @classmethod
    def _experiment_tool_signature(cls, tool_name: str, arguments: Dict[str, Any]) -> str:
        normalized = dict(arguments or {})
        if tool_name == "bash" and isinstance(normalized.get("command"), str):
            command = normalized["command"].lower().replace("\\", "/").replace("./", "")
            normalized["command"] = re.sub(r"\s+", " ", command).strip().rstrip("/")
        return cls.tool_call_signature(tool_name, normalized)

    def _ensure_experiment_results_dir(self) -> None:
        """Ensure experiment_results exists inside the current workspace."""
        try:
            placeholder_tool = {
                "name": "file_write",
                "arguments": {
                    "file_path": f"{EXPERIMENT_RESULTS_DIR}/.gitkeep",
                    "content": "",
                    "create_dirs": True
                }
            }
            self.execute_tool_call(placeholder_tool)
            self.logger.info(f"Ensured results directory exists: {EXPERIMENT_RESULTS_DIR}")
        except Exception as e:
            self.logger.warning(f"Failed to ensure results directory (non-fatal): {e}")

    def _collect_figures_from_results_dir(self) -> List[str]:
        """
        扫描 experiment_results/ 目录,收集所有图片文件路径.
        作为 output_figures 的兜底补充,防止模型漏报.
        """
        figures = []
        try:
            result = self.execute_tool_call({
                "name": "bash",
                "arguments": {"command": f"find {EXPERIMENT_RESULTS_DIR} -name '*.png' -o -name '*.jpg' -o -name '*.jpeg' 2>/dev/null"}
            })
            if result.get("success") and result.get("data", {}).get("stdout"):
                lines = result["data"]["stdout"].strip().split("\n")
                figures = [l.strip() for l in lines if l.strip() and not l.strip().endswith(".gitkeep")]
        except Exception as e:
            self.logger.warning(f"扫描图片目录失败: {e}")
        return figures

    def execute_task(self, task_input: TaskInput) -> AgentResponse:
        start_time = time.time()
        self.logger.info(f"Starting Experiment task: {task_input.task_content}")
        self.reset_trace()

        # 给实验留足够的时间和轮数
        if self.config.max_iterations < 20:
            self.config.max_iterations = 30

        # 在任务开始前确保输出目录存在
        self._ensure_experiment_results_dir()

        conversation_history = [
            {"role": "system", "content": self._build_system_prompt()},
            {"role": "user", "content": (
                f"Experiment Task:\n{task_input.task_content}\n\n"
                f"REMINDER: Save ALL figures to `{EXPERIMENT_RESULTS_DIR}/` "
                f"and list them ALL in `output_figures` when calling `experiment_task_done`.\n\n"
                "/no_think"
            )}
        ]

        iteration = 0
        task_completed = False
        format_error_streak = 0
        last_tool_signature = None
        repeated_tool_call_count = 0
        last_successful_tool = ""
        last_successful_write_path = ""
        verification_call_count = 0

        # 获取模型配置
        from config.config import get_config
        config_obj = get_config()
        model_config = config_obj.get_custom_llm_config()

        while iteration < self.config.max_iterations and not task_completed:
            iteration += 1
            self.logger.info(f"Experiment iteration {iteration}")

            checkpoint = self._agent_intervention_checkpoint("experiment_iteration", iteration)
            checkpoint_message = self._intervention_message(
                checkpoint["instructions"], checkpoint["requested_stage"]
            )
            if checkpoint_message:
                conversation_history.append({"role": "user", "content": checkpoint_message})
            requested_stage = checkpoint.get("requested_stage")
            if requested_stage and requested_stage.get("stage") != "experiment":
                controlled_result = {
                    "task_summary": f"实验在安全检查点按用户指导转入 {requested_stage.get('stage')}。",
                    "completion_status": "stopped_by_user_guidance",
                    "experimental_metrics": "",
                    "output_figures": self._collect_figures_from_results_dir(),
                    "requested_stage": requested_stage.get("stage"),
                }
                self._write_experiment_report(controlled_result)
                self.log_action(iteration, "experiment_task_done", controlled_result, controlled_result)
                task_completed = True
                break

            # Check cancellation before each LLM/tool iteration.
            if self._check_cancellation():
                self.logger.info(f"Experiment cancelled by user at iteration {iteration}")
                raise Exception("Task cancelled by user")

            # 接近结束时催促汇报
            if iteration == self.config.max_iterations - 3:
                conversation_history.append({
                    "role": "user",
                    "content": (
                        f"⚠️ FINAL WARNING: Only 3 iterations remaining. "
                        f"Verify figures exist in `{EXPERIMENT_RESULTS_DIR}/` using bash, "
                        f"then call `experiment_task_done` NOW with all metrics and figure paths. /no_think"
                    )
                })

            try:
                payload = {
                    "model": self.config.model if hasattr(self.config, 'model') and self.config.model else get_config().model_name,
                    "messages": conversation_history,
                    "temperature": self.config.temperature if hasattr(self.config, 'temperature') else 0.1,
                    "max_tokens": self.config.max_tokens if hasattr(self.config, 'max_tokens') else 4096,
                    "chat_template": (
                        "{% for message in messages %}"
                        "{% if loop.first and messages[0]['role'] != 'system' %}{{ '<s>[unused9]系统:[unused10]' }}{% endif %}"
                        "{% if message['role'] == 'system' %}{{'<s>[unused9]系统:' + message['content'] + '[unused10]'}}{% endif %}"
                        "{% if message['role'] == 'assistant' %}{{'[unused9]助手:' + message['content'] + '[unused10]'}}{% endif %}"
                        "{% if message['role'] == 'tool' %}{{'[unused9]工具:' + message['content'] + '[unused10]'}}{% endif %}"
                        "{% if message['role'] == 'function' %}{{'[unused9]方法:' + message['content'] + '[unused10]'}}{% endif %}"
                        "{% if message['role'] == 'user' %}{{'[unused9]用户:' + message['content'] + '[unused10]'}}{% endif %}"
                        "{% endfor %}"
                        "{% if add_generation_prompt %}{{ '[unused9]助手:' }}{% endif %}"
                    ),
                    "spaces_between_special_tokens": False
                }

                retry_num = 1
                max_retry_num = 10
                response_json = None

                while retry_num <= max_retry_num:
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
                        except Exception:
                            response_json = {"error": {"message": response.text, "code": status_code}}

                        if status_code in [429, 500] or (isinstance(response_json, dict) and "error" in response_json):
                            err = response_json.get("error", {})
                            err_code = str(err.get("code", status_code))
                            err_msg = str(err.get("message", "")).lower()

                            # 扩大匹配范围:加入 throttl, capacity 等,包揽阿里云所有拥挤话术
                            if err_code in ["429", "500"] or any(k in err_msg for k in
                                                                 ["rate limit", "429", "quota", "throttl",
                                                                  "too many requests", "capacity"]):
                                self.logger.warning(
                                    f"⚠️ 触发 API 限流或官方服务器拥挤 ({err_code}).Agent 将沉睡 30 秒后避峰继续... (第 {retry_num}/{max_retry_num} 次尝试)")

                                time.sleep(30)
                                retry_num += 1
                                continue

                        if "choices" not in response_json or not response_json["choices"]:
                            error_info = response_json.get("error", "未知 API 错误")
                            self.logger.error(f"API 响应异常: {response_json}")
                            # ❌ 绝对不要在这里 raise ValueError 导致自杀!
                            # 遇到其他网络错误,沉睡3秒重试即可
                            self.logger.warning(
                                f"⚠️ 遇到未知 API 错误,沉睡3秒后重试... (第 {retry_num}/{max_retry_num} 次尝试)")

                            time.sleep(3)
                            retry_num += 1
                            continue
                        break

                    except Exception as e:
                        if "Quota Exceeded" in str(e):
                            raise e
                        self.logger.warning(f"网络请求失败: {e},3秒后重试...")
                        time.sleep(3)
                        retry_num += 1

                if not response_json or "choices" not in response_json:
                    raise Exception(f"API 请求彻底失败: {response_json}")

                assistant_message = response_json["choices"][0]["message"]
                content = assistant_message.get("content", "")

                conversation_history.append({"role": "assistant", "content": content})

                reasoning_content = self._safe_extract_reasoning(content)
                if reasoning_content:
                    self.log_reasoning(iteration, reasoning_content)

                tool_calls = self.extract_tool_calls(content)

                if len(tool_calls) > 0 and tool_calls[0].get("name") == "system_error_feedback":
                    format_error_streak += 1
                    if format_error_streak >= 2:
                        error = "连续两次工具调用格式错误，已停止当前实验子任务，避免无效循环。"
                        self.logger.error(error)
                        return self.create_response(
                            success=False,
                            error=error,
                            iterations=iteration,
                            execution_time=time.time() - start_time,
                        )
                    tool_result = {"success": False, "error": tool_calls[0]["arguments"]["error"]}
                    conversation_history.append({
                        "role": "user",
                        "content": json.dumps(tool_result, ensure_ascii=False) + " /no_think"
                    })
                    continue
                format_error_streak = 0

                intervention_deferred_tools = False
                tool_blocked = False
                for tool_call in tool_calls:
                    arguments = tool_call.get("arguments", {})
                    tool_name = tool_call.get("name", "")

                    tool_checkpoint = self._agent_intervention_checkpoint(
                        "experiment_tool_boundary", iteration, tool_name
                    )
                    tool_checkpoint_message = self._intervention_message(
                        tool_checkpoint["instructions"], tool_checkpoint["requested_stage"]
                    )
                    if tool_checkpoint_message:
                        conversation_history.append({"role": "user", "content": tool_checkpoint_message})
                        intervention_deferred_tools = True
                        requested_stage = tool_checkpoint.get("requested_stage")
                        if requested_stage and requested_stage.get("stage") != "experiment":
                            controlled_result = {
                                "task_summary": f"实验在当前工具完成后按用户指导转入 {requested_stage.get('stage')}。",
                                "completion_status": "stopped_by_user_guidance",
                                "experimental_metrics": "",
                                "output_figures": self._collect_figures_from_results_dir(),
                                "requested_stage": requested_stage.get("stage"),
                            }
                            self._write_experiment_report(controlled_result)
                            self.log_action(iteration, "experiment_task_done", controlled_result, controlled_result)
                            task_completed = True
                        break

                    if tool_name == "experiment_task_done":
                        # ── 兜底:扫描目录补充漏报的图片 ──────────────────
                        reported_figures = arguments.get("output_figures") or []
                        scanned_figures = self._collect_figures_from_results_dir()

                        # 合并去重,保留模型报告的顺序,再追加扫描到但未报告的
                        reported_set = set(reported_figures)
                        for fig in scanned_figures:
                            if fig not in reported_set:
                                reported_figures.append(fig)
                                self.logger.info(f"🔍 自动补充漏报图片: {fig}")

                        arguments["output_figures"] = reported_figures

                        # 同步写入 experiment_results.md 供 Planner/Writer 读取
                        self._write_experiment_report(arguments)

                        task_completed = True
                        self.log_action(iteration, tool_name, arguments, arguments)
                        break

                    if tool_name not in ["think", "reflect"]:
                        signature = self._experiment_tool_signature(tool_name, arguments)
                        if signature == last_tool_signature:
                            repeated_tool_call_count += 1
                        else:
                            last_tool_signature = signature
                            repeated_tool_call_count = 1
                        if repeated_tool_call_count >= 2:
                            tool_result = {
                                "success": False,
                                "error": (
                                    f"Duplicate experiment tool call blocked: {tool_name} already ran with the same "
                                    "arguments. Inspect its existing output, change a concrete failed input, or call "
                                    "experiment_task_done if acceptance criteria are met."
                                ),
                            }
                            self.logger.warning("已拦截完全相同的实验工具调用: %s", tool_name)
                            self.log_action(iteration, tool_name, arguments, tool_result)
                            conversation_history.append({
                                "role": "tool",
                                "content": json.dumps(tool_result, ensure_ascii=False) + " /no_think",
                            })
                            tool_blocked = True
                            break

                    if tool_name == "bash" and self._bash_attempts_source_write(str(arguments.get("command") or "")):
                        tool_result = {
                            "success": False,
                            "error": (
                                "Shell-based source generation is blocked on Windows. Use file_write once to create "
                                "the .py file, then run_python_script. Do not use redirection, heredoc, base64, "
                                "python -c, or a generator script."
                            ),
                        }
                        self.logger.warning("已拦截通过 shell 生成 Python 源文件的操作")
                        self.log_action(iteration, tool_name, arguments, tool_result)
                        conversation_history.append({
                            "role": "tool",
                            "content": json.dumps(tool_result, ensure_ascii=False) + " /no_think",
                        })
                        tool_blocked = True
                        break

                    write_path = str(arguments.get("file_path") or "")
                    if (
                        tool_name == "file_write" and write_path
                        and last_successful_tool == "file_write"
                        and write_path.replace("\\", "/").lower()
                        == last_successful_write_path.replace("\\", "/").lower()
                    ):
                        tool_result = {
                            "success": False,
                            "error": (
                                f"Consecutive rewrite blocked for {write_path}. Execute the current script first and "
                                "use its stdout/stderr to justify a targeted correction."
                            ),
                        }
                        self.logger.warning("已拦截未执行前连续改写同一脚本: %s", write_path)
                        self.log_action(iteration, tool_name, arguments, tool_result)
                        conversation_history.append({
                            "role": "tool",
                            "content": json.dumps(tool_result, ensure_ascii=False) + " /no_think",
                        })
                        tool_blocked = True
                        break

                    command = str(arguments.get("command") or "")
                    normalized_command = re.sub(
                        r"\s+", " ", command.lower().replace("\\", "/").replace("./", "")
                    ).strip()
                    is_result_verification = tool_name == "bash" and normalized_command.startswith("dir ") and "experiment_results" in normalized_command
                    if is_result_verification and verification_call_count >= 1:
                        tool_result = {
                            "success": False,
                            "error": (
                                "The experiment output directory was already verified once. Do not list it again; "
                                "call experiment_task_done with the collected metrics and figure paths."
                            ),
                        }
                        self.logger.warning("已拦截重复实验目录校验")
                        self.log_action(iteration, tool_name, arguments, tool_result)
                        conversation_history.append({
                            "role": "tool",
                            "content": json.dumps(tool_result, ensure_ascii=False) + " /no_think",
                        })
                        tool_blocked = True
                        break

                    if tool_name in ["think", "reflect"]:
                        tool_result = {"tool_results": "Thought logged. Proceed."}
                    else:
                        tool_result = self.execute_tool_call(tool_call)

                    self.log_action(iteration, tool_name, arguments, tool_result)
                    if isinstance(tool_result, dict) and tool_result.get("success"):
                        last_successful_tool = tool_name
                        if tool_name == "file_write":
                            last_successful_write_path = write_path
                        if is_result_verification:
                            verification_call_count += 1
                    conversation_history.append({
                        "role": "tool",
                        "content": json.dumps(tool_result, ensure_ascii=False) + " /no_think"
                    })

                if task_completed:
                    break
                if tool_blocked:
                    continue
                if intervention_deferred_tools:
                    continue

                if len(tool_calls) == 0 and not task_completed:
                    # Track no-tool-call streak to break infinite loops
                    if not hasattr(self, '_no_tool_streak'):
                        self._no_tool_streak = 0
                    self._no_tool_streak += 1

                    if self._no_tool_streak >= 3:
                        # Force completion after 3 consecutive no-tool responses
                        conversation_history.append({
                            "role": "user",
                            "content": (
                                "You have not used any tools for 3 consecutive responses. "
                                "If the experiment is done, call experiment_task_done NOW with all metrics and figure paths. "
                                "If errors occurred, report them in experiment_task_done. "
                                "Do NOT output more text without calling a tool. /no_think"
                            )
                        })
                    else:
                        conversation_history.append({
                            "role": "user",
                            "content": (
                                "Please continue. Use tools to analyze the dataset or run the experiment. "
                                f"Remember to save figures to `{EXPERIMENT_RESULTS_DIR}/`. "
                                "Call experiment_task_done when finished. /no_think"
                            )
                        })
                else:
                    # Reset streak when tool calls were made
                    self._no_tool_streak = 0

            except Exception as e:
                # Re-raise cancellation exceptions to stop the experiment
                if "cancelled" in str(e).lower():
                    self.logger.info(f"Experiment cancelled by user at iter {iteration}")
                    raise
                self.logger.error(f"Experiment Agent Error at iter {iteration}: {e}", exc_info=True)
                conversation_history.append({
                    "role": "user",
                    "content": f"Network or execution error: {str(e)}. Please retry."
                })

        # 从 trace 中取出最终结果
        task_done_result = None
        for step in reversed(self.reasoning_trace):
            if step.get("type") == "action" and step.get("tool") == "experiment_task_done":
                task_done_result = step.get("result")
                break

        return self.create_response(
            success=task_completed,
            result=task_done_result,
            iterations=iteration,
            execution_time=time.time() - start_time
        )

    def _write_experiment_report(self, arguments: Dict[str, Any]) -> None:
        """
        将实验结果写入 experiment_results/experiment_results.md.
        这个文件会被 planner_agent 的 assign_task_to_experimenter 方法
        自动检测并注入到 WriterAgent 的 key_files 中.
        """
        task_summary = arguments.get("task_summary", "")
        experimental_metrics = arguments.get("experimental_metrics", "")
        output_figures = arguments.get("output_figures") or []
        completion_status = arguments.get("completion_status", "completed")
        validation_notes = arguments.get("validation_notes", "")
        figure_tiers = arguments.get("figure_tiers", {})

        tier_md = ""
        if figure_tiers:
            tier_md = "\n\n## Figure Tier Classification\n\n"
            for tier_name, tier_label in [("tier1", "Critical"), ("tier2", "Supporting"), ("tier3", "Optional")]:
                figs = figure_tiers.get(tier_name, [])
                if figs:
                    tier_md += f"**Tier - {tier_label}**:\n"
                    for f in figs:
                        tier_md += f"  - {f}\n"
                    tier_md += "\n"
        validation_md = ""
        if validation_notes:
            validation_md = f"\n\n## Statistical Validation\n\n{validation_notes}\n"

        figures_md = ""
        if output_figures:
            figures_md = "\n\n## Generated Figures\n\n"
            for fig_path in output_figures:
                fname = os.path.basename(fig_path)
                figures_md += f"![{fname}](../experiment_results/{fname})\n\n"

        report_content = (
            f"# Experiment Results\n\n"
            f"**Status**: {completion_status}\n\n"
            f"## Task Summary\n\n{task_summary}\n\n"
            f"## Experimental Metrics\n\n{experimental_metrics}"
            f"{validation_md}"
            f"{tier_md}"
            f"{figures_md}"
        )

        report_path = f"{EXPERIMENT_RESULTS_DIR}/experiment_results.md"
        try:
            write_result = self.execute_tool_call({
                "name": "file_write",
                "arguments": {
                    "file_path": report_path,
                    "content": report_content,
                    "create_dirs": True
                }
            })
            if write_result.get("success"):
                self.logger.info(f"✅ 实验报告已写入: {report_path}")
            else:
                self.logger.warning(f"实验报告写入失败: {write_result.get('error')}")
        except Exception as e:
            self.logger.warning(f"实验报告写入异常(非致命): {e}")


def create_experiment_agent(
    model: Any = None,
    max_iterations: int = 30,
    shared_mcp_client=None,
    **kwargs
) -> ExperimentAgent:
    from .base_agent import create_agent_config
    config = create_agent_config(
        agent_name="ExperimentAgent",
        model=model,
        max_iterations=max_iterations,
        **kwargs
    )
    return ExperimentAgent(config=config, shared_mcp_client=shared_mcp_client)
