# Copyright (c) 2025 Huawei Technologies Co., Ltd. All rights reserved.
# Copyright (c) 2026 South China Sea Institute of Oceanology, Chinese Academy of Sciences (SCSIO, CAS). All rights reserved.
"""
Planner Agent for Multi-Agent Task Coordination

This agent serves as a coordinator for complex tasks that require multiple agents
working together. It implements the ReAct pattern for reasoning and action.
"""
import time
import logging
import re
import json
import requests
import os
import glob
import hashlib
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, Any, List, Optional, Union
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed

# Base imports
from .base_agent import BaseAgent, AgentConfig, AgentResponse, WriterAgentTaskInput
from ..utils.llm_client import chat_completion_response
from ..utils.skill_loader import get_skill_loader
# Import agent creators for built-in task assignment
from .writer_agent import create_writer_agent
logger = logging.getLogger(__name__)
from .experiment_agent import create_experiment_agent

DEFAULT_INFO_SEEKER_MAX_ITERATIONS = 30


def _resolve_info_seeker_max_iterations(default_value: int = DEFAULT_INFO_SEEKER_MAX_ITERATIONS) -> int:
    env_value = (
        os.getenv("INFO_SEEKER_MAX_ITERATIONS")
        or os.getenv("INFORMATION_SEEKER_MAX_ITERATION")
    )
    if env_value:
        return max(1, int(env_value))
    return max(1, int(default_value))


class PlannerAgent(BaseAgent):
    """
    PlannerAgent coordinates multiple agents to handle complex user queries.

    The agent uses the ReAct pattern (Reasoning + Acting) to analyze user requests,
    break them down into manageable tasks, and coordinate the appropriate agents
    to complete the work.
    """

    def __init__(self, config: AgentConfig = None, shared_mcp_client=None, task_id: Optional[str] = None):
        # Set default agent name if not specified
        if config and not config.agent_name:
            config.agent_name = "PlannerAgent"
        elif not config:
            config = AgentConfig(agent_name="PlannerAgent")

        super().__init__(config, shared_mcp_client)

        # === 鍦ㄨ繖閲屾坊鍔?纭繚蹇呰鐨勬姤鍛婂拰鏁版嵁鐩綍瀛樺湪 ===
        # 鍗充娇鍦?Windows 涓?os.makedirs 涔熻兘寰堝ソ鍦板鐞嗚繖绫昏矾寰?
        for folder in ['report', 'workspaces', 'research']:
            if not os.path.exists(folder):
                os.makedirs(folder, exist_ok=True)
                self.logger.info("已自动创建目录: %s", folder)

        # Planner-specific state
        self.execution_plan = []
        self.task_queue = []

		# Task management for cancellation support
        self.task_id = task_id
        self._cancellation_token = None

        # Built-in delegation tools are local Planner methods, not MCP tools.
        # Register them after BaseAgent has finished MCP discovery so the old
        # Planner -> sub-agent workflow remains executable in Hybrid mode.
        self._add_builtin_assignment_tools()
        self.tool_schemas = self._build_tool_schemas()

        self.sub_agent_configs = {}
        self._information_seeker_completed = False
        self._experiment_agent_completed = False
        self._writer_agent_completed = False
        self._successful_information_task_fingerprints = set()
        self._successful_information_task_texts = []
        self._successful_information_batch_fingerprints = set()
        self._failed_information_tasks = {}
        self._successful_experiment_task_fingerprints = set()
        # Hybrid preparation runs before the legacy autonomous loop.  Restore
        # completion state from verified workspace artifacts so a fresh
        # Planner instance does not forget work that PipelineV2 already did.
        self._sync_hybrid_completion_from_artifacts()

    def _publish_agent_progress(self, stage: str, message: str, **data) -> None:
        """Publish non-blocking Hybrid UI activity without changing checkpoint state."""
        if not getattr(self, "task_id", None):
            return
        try:
            from src.utils.task_manager import task_manager
            task_manager.record_event(self.task_id, "agent_progress", message, {"stage": stage, **data})
        except Exception as exc:
            self.logger.debug("Could not publish agent progress: %s", exc)

    def _planner_pause_checkpoint(self, iteration: int) -> List[str]:
        """Pause only at Planner planning boundaries, as configured by the UI contract."""
        if not self.task_id:
            return []
        try:
            from src.utils.task_manager import task_manager
            return task_manager.checkpoint(
                self.task_id, "planner_planning",
                {"iteration": iteration, "agent": "PlannerAgent"},
                event_type="agent_checkpoint",
            )
        except Exception as exc:
            self.logger.debug("Planner checkpoint unavailable: %s", exc)
            return []

    @staticmethod
    def _looks_like_broad_literature_task(text: str) -> bool:
        lowered = (text or "").lower()
        research_markers = [
            "reference", "references", "literature", "paper", "papers",
            "citation", "citations", "doi", "arxiv", "journal"
        ]
        topic_markers = [
            "yolov5", "yolov8", "yolov10", "yolov11", "yolo26",
            "wiou", "wise-iou", "sppf", "c2psa", "as7265x",
            "spectral", "cross-attention", "wavelet", "ghost",
            "apple", "ripeness", "maturity", "agriculture", "fruit"
        ]
        return (
            any(marker in lowered for marker in research_markers)
            and sum(1 for marker in topic_markers if marker in lowered) >= 4
        )

    @staticmethod
    def _split_broad_literature_task(task: Dict[str, str]) -> List[Dict[str, str]]:
        content = task.get("task_content", "")
        if not PlannerAgent._looks_like_broad_literature_task(content):
            return [task]

        common_suffix = (
            "\n\nV2 workflow constraints: keep this subtask narrow; collect every parseable saved "
            "reference without targeting a fixed final count; record limitations instead of inventing "
            "missing references; save literature summaries under ./research/autonomous_search/ and include "
            "them in key_files. Preserve title, authors, year, venue, DOI/URL, query, abstract/evidence, and source type."
        )
        base = {
            "task_steps_for_reference": task.get("task_steps_for_reference"),
            "current_task_status": task.get("current_task_status"),
            "acceptance_checking_criteria": task.get("acceptance_checking_criteria"),
            "evidence_gap": task.get("evidence_gap"),
        }
        deliverable = task.get("deliverable_contents") or (
            "Markdown summary with structured citations, source paths, relevance notes, and limitations."
        )
        focused_tasks = [
            (
                "Search literature on apple or fruit ripeness detection, agricultural object detection, "
                "and non-destructive maturity assessment. Focus on recent YOLO/CNN-based applications."
            ),
            (
                "Search literature on spectral-guided fusion, cross-attention, wavelet-based feature "
                "processing, Ghost/lightweight modules, and related lightweight neural network methods."
            ),
            (
                "Search literature and authoritative sources on YOLO model evolution and architecture "
                "components: YOLOv5, YOLOv8, YOLOv10, YOLOv11, YOLO26, WIOU/Wise-IoU, SPPF, C2PSA, "
                "and AS7265x multispectral sensor use in agriculture."
            ),
        ]
        return [
            {
                **base,
                "task_content": focused + common_suffix,
                "deliverable_contents": deliverable,
            }
            for focused in focused_tasks
        ]

    @classmethod
    def _normalize_information_tasks(cls, tasks: List[Dict[str, str]]) -> List[Dict[str, str]]:
        normalized: List[Dict[str, str]] = []
        for task in tasks or []:
            normalized.extend(cls._split_broad_literature_task(task))
        unique, _ = cls._deduplicate_information_tasks(normalized)
        return unique

    @staticmethod
    def _information_task_text(task: Dict[str, str], include_gap: bool = True) -> str:
        """Return stable comparison text without generated workflow boilerplate."""
        content = str(task.get("task_content") or "")
        content = re.split(r"\bV2 workflow constraints\s*:", content, maxsplit=1, flags=re.IGNORECASE)[0]
        parts = [content]
        if include_gap:
            parts.append(str(task.get("evidence_gap") or ""))
        text = " ".join(parts).lower().replace("–", "-").replace("—", "-")
        text = re.sub(r"[^\w\u4e00-\u9fff]+", " ", text, flags=re.UNICODE)
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def _information_task_fingerprint(cls, task: Dict[str, str]) -> str:
        return hashlib.sha256(cls._information_task_text(task).encode("utf-8")).hexdigest()

    @classmethod
    def _information_batch_fingerprint(cls, tasks: List[Dict[str, str]]) -> str:
        fingerprints = sorted(cls._information_task_fingerprint(task) for task in tasks)
        return hashlib.sha256("|".join(fingerprints).encode("ascii")).hexdigest()

    @staticmethod
    def _information_text_similarity(left: str, right: str) -> float:
        if not left or not right:
            return 0.0
        if left == right:
            return 1.0
        left_tokens = set(left.split())
        right_tokens = set(right.split())
        union = left_tokens | right_tokens
        jaccard = len(left_tokens & right_tokens) / len(union) if union else 0.0
        sequence = SequenceMatcher(None, left, right).ratio()
        return max(jaccard, sequence)

    @staticmethod
    def _normalize_experiment_task_text(text: str) -> str:
        normalized = (text or "").lower().replace("–", "-").replace("—", "-")
        normalized = re.sub(r"[^\w\u4e00-\u9fff./+-]+", " ", normalized, flags=re.UNICODE)
        return re.sub(r"\s+", " ", normalized).strip()

    @classmethod
    def _is_reference_management_task(cls, text: str) -> bool:
        """Return True when a task is bibliography work rather than an experiment."""
        normalized = cls._normalize_experiment_task_text(text)
        reference_markers = (
            "reference", "references", "literature", "bibliography", "citation", "citations", "doi",
            "参考文献", "文献整理", "文献合并", "引文", "引用整理",
        )
        experiment_markers = (
            "csv", "xlsx", "dataset", "experimental data", "experiment data", "train model",
            "model training", "ablation experiment", "metric calculation", "plot experiment",
            "实验数据", "实验结果", "数据集", "模型训练", "消融实验", "计算指标", "实验绘图",
        )
        return any(marker in normalized for marker in reference_markers) and not any(
            marker in normalized for marker in experiment_markers
        )

    @classmethod
    def _explicit_experiment_rerun_requested(cls, text: str) -> bool:
        normalized = cls._normalize_experiment_task_text(text)
        markers = (
            "rerun", "re-run", "run again", "recompute", "recalculate", "force rerun",
            "重新运行", "重新实验", "重跑", "重算", "强制运行",
        )
        return any(marker in normalized for marker in markers)

    @classmethod
    def _looks_like_bulk_experiment_reprocessing(cls, text: str) -> bool:
        normalized = cls._normalize_experiment_task_text(text)
        bulk_markers = ("all", "entire", "every", "comprehensive", "全部", "所有", "逐个", "整批", "完整处理")
        experiment_markers = ("experiment", "experimental", "csv", "dataset", "实验", "数据")
        gap_markers = (
            "missing", "additional", "new data", "specific metric", "specific figure", "only",
            "缺少", "补充", "新增", "指定指标", "指定图", "仅处理", "只处理",
        )
        return (
            any(marker in normalized for marker in bulk_markers)
            and any(marker in normalized for marker in experiment_markers)
            and not any(marker in normalized for marker in gap_markers)
        )

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def _load_reusable_experiment_registry(cls, workspace: Path) -> List[Dict[str, Any]]:
        """Load a registry only when all source CSV files and hashes still match."""
        workspace = Path(workspace)
        root = workspace / "experiment_results"
        registry_path = root / "experiment_registry.json"
        if not registry_path.is_file():
            return []
        try:
            records = json.loads(registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return []
        if not isinstance(records, list) or not records:
            return []

        registered_paths = set()
        for record in records:
            if not isinstance(record, dict) or record.get("status") != "processed":
                return []
            relative = str(record.get("results_csv") or "")
            expected_hash = str(record.get("sha256") or "")
            source = workspace / relative
            if not relative or not expected_hash or not source.is_file():
                return []
            try:
                if cls._sha256_file(source) != expected_hash:
                    return []
            except OSError:
                return []
            registered_paths.add(source.resolve())

        generated_names = {"experiment_registry.csv", "source_inventory.csv"}
        actual_sources = {
            path.resolve() for path in root.rglob("*.csv")
            if path.name.lower() not in generated_names
            and "figures" not in {part.lower() for part in path.parts}
            and path.parent != root  # root-level CSV files are derived summaries, not uploaded runs
        }
        if actual_sources != registered_paths:
            return []
        return records

    @classmethod
    def _experiment_task_fingerprint(
        cls, task_content: str, dataset_paths: Optional[List[str]], registry: List[Dict[str, Any]],
    ) -> str:
        dataset_signature = sorted(
            f"{item.get('results_csv', '')}:{item.get('sha256', '')}" for item in (registry or [])
        )
        if dataset_paths:
            dataset_signature.extend(sorted(str(path) for path in dataset_paths))
        source = "|".join([
            cls._normalize_experiment_task_text(task_content),
            *dataset_signature,
        ])
        return hashlib.sha256(source.encode("utf-8", errors="ignore")).hexdigest()

    @staticmethod
    def _experiment_history_path(workspace: Path) -> Path:
        return Path(workspace) / "experiment_results" / "agent_task_history.json"

    @classmethod
    def _load_experiment_history(cls, workspace: Path) -> Dict[str, Dict[str, Any]]:
        path = cls._experiment_history_path(workspace)
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError, TypeError):
            return {}

    @classmethod
    def _save_experiment_history(
        cls, workspace: Path, fingerprint: str, task_content: str, result: Dict[str, Any],
    ) -> None:
        path = cls._experiment_history_path(workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        history = cls._load_experiment_history(workspace)
        history[fingerprint] = {
            "task_content": task_content,
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "experiment_report_file": result.get("experiment_report_file"),
            "output_figures": result.get("output_figures", []),
        }
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    @classmethod
    def _deduplicate_information_tasks(
            cls, tasks: List[Dict[str, str]], threshold: float = 0.82
    ) -> tuple[List[Dict[str, str]], List[Dict[str, Any]]]:
        unique: List[Dict[str, str]] = []
        unique_texts: List[str] = []
        skipped: List[Dict[str, Any]] = []
        for task in tasks or []:
            if not isinstance(task, dict):
                continue
            text = cls._information_task_text(task)
            if not text:
                continue
            duplicate_index = next(
                (
                    index for index, previous in enumerate(unique_texts)
                    if cls._information_text_similarity(text, previous) >= threshold
                ),
                None,
            )
            if duplicate_index is not None:
                skipped.append({
                    "task_content": task.get("task_content", ""),
                    "reason": "duplicate_within_batch",
                    "duplicate_of": unique[duplicate_index].get("task_content", ""),
                })
                continue
            unique.append(task)
            unique_texts.append(text)
        return unique, skipped

    def _ensure_information_dedup_state(self) -> None:
        """Keep tests/legacy restored Planner instances compatible with new state."""
        if not hasattr(self, "_successful_information_task_fingerprints"):
            self._successful_information_task_fingerprints = set()
        if not hasattr(self, "_successful_information_task_texts"):
            self._successful_information_task_texts = []
        if not hasattr(self, "_successful_information_batch_fingerprints"):
            self._successful_information_batch_fingerprints = set()
        if not hasattr(self, "_failed_information_tasks"):
            self._failed_information_tasks = {}

    def _active_workspace_path(self) -> Path:
        return Path(
            getattr(getattr(self, "mcp_tools", None), "workspace_path", None)
            or os.getenv("AGENT_WORKSPACE_PATH")
            or "."
        ).resolve()

    def _sync_hybrid_completion_from_artifacts(
            self, *, verify_literature: bool = False, query: str = ""
    ) -> Dict[str, Any]:
        """Synchronize legacy Agent flags with verified Hybrid workspace outputs.

        PipelineV2 prepares experiments and structured evidence before creating
        PlannerAgent.  Those durable outputs are authoritative across process
        and Agent boundaries; the in-memory flags are only a cache.
        """
        state: Dict[str, Any] = {"experiment_count": 0, "reference_gate": None}
        if os.getenv("SCIA_PIPELINE_VERSION", "").strip().lower() != "hybrid":
            return state

        workspace = self._active_workspace_path()
        registry = self._load_reusable_experiment_registry(workspace)
        state["experiment_count"] = len(registry)
        if registry:
            if not getattr(self, "_experiment_agent_completed", False):
                self.logger.info(
                    "Hybrid state restored: verified experiment registry contains %s completed records",
                    len(registry),
                )
            self._experiment_agent_completed = True

        final_report = workspace / "report" / "final_report.md"
        if final_report.is_file() and final_report.stat().st_size > 0:
            self._writer_agent_completed = True

        if verify_literature:
            from src.pipeline_v2.hybrid import refresh_hybrid_evidence

            gate = refresh_hybrid_evidence(workspace, query)
            state["reference_gate"] = gate
            if gate.get("reference_gate_met", False):
                self.logger.info(
                    "Hybrid literature gate is satisfied (%s/%s); actual InformationSeekerAgent completion is still required",
                    gate.get("reference_count", 0), gate.get("minimum_reference_count", 0),
                )
        return state

    def _add_builtin_assignment_tools(self):
        """Add built-in task assignment methods as available tools"""
        # Add assignment methods that share the MCP client connection
        self.available_tools.update({
            "assign_subjective_task_to_writer": self.assign_subjective_task_to_writer, # assign_subjective_task_to_writer
            "assign_multi_objective_tasks_to_info_seeker": self.assign_multi_objective_tasks_to_info_seeker,
            "assign_multi_subjective_tasks_to_info_seeker": self.assign_multi_subjective_tasks_to_info_seeker,
            "assign_task_to_experimenter": self.assign_task_to_experimenter,
        })

    def _discover_mcp_tools(self) -> Dict[str, Any]:
        """
        馃殌 缁堟瀬鐗╃悊闃夊壊:浠庡簳灞傚墺澶?Planner 鐨勫啓闀挎枃銆佸啓浠ｇ爜銆佹敼鏂囦欢宸ュ叿
        """
        # 鍏堣皟鐢ㄧ埗绫昏幏鍙栨墍鏈夊垎閰嶇粰瀹冪殑宸ュ叿
        tools = super()._discover_mcp_tools()

        # 鏄庣‘娌℃敹杩欎簺鈥滃共鏉傛椿鈥濈殑宸ュ叿
        forbidden_tools = [
            "bash",
            "file_write",
            "str_replace_based_edit_tool",
            "run_python_script",
            "section_writer",
            "concat_section_files"
        ]

        for ft in forbidden_tools:
            if ft in tools:
                del tools[ft]

        return tools

    def _safe_extract_content(self, text: str, start_tag: str, end_tag: str) -> str:
        """瀹夊叏鍦颁粠妯″瀷杩斿洖涓彁鍙栫壒瀹氭爣绛惧唴鐨勫唴瀹?闃叉 split 婧㈠嚭"""
        if not text:
            return ""
        try:
            import re
            pattern = f"{re.escape(start_tag)}(.*?){re.escape(end_tag)}"
            match = re.search(pattern, text, re.DOTALL)
            if match:
                return match.group(1).strip()
            # 鍏滃簳:濡傛灉娌℃壘鍒版爣绛?灏濊瘯娓呯悊鎺夋墍鏈?[unusedXX] 鏍囩鍚庤繑鍥?
            clean_text = re.sub(r'\[unused\d+\]', '', text).strip()
            return clean_text[:500]  # 闄愬埗闀垮害,闃叉鏃ュ織鐖嗙偢
        except Exception:
            return ""
    def _build_system_prompt(self) -> str:
        """Build the system prompt for the planner agent"""
        tool_schemas_str = json.dumps(self.tool_schemas, ensure_ascii=False)

        auto_system_prompt_template = """# PlannerAgent: Multi-Agent Task Coordinator
**Role:** Analyze complex queries, first distinguish query type (academic paper type/objective question type), then create structured plans, and coordinate specialized agents to deliver comprehensive solutions鈥攃all corresponding tools based on query type, and only invoke writer for academic paper type queries.

#### Available Sub-Agents:  
- **`information_seeker`**: Research, data gathering, web search (supports single/parallel multi-task; long-form writing type uses assign_multi_subjective_tasks_to_info_seeker, other types use assign_multi_objective_tasks_to_info_seeker)
- **`writer`**: Only invoke this sub-agent when writing an academic paper is required. 
- **`experimenter`**: Invoke this sub-agent via `assign_task_to_experimenter` when the task requires data analysis, running python scripts, training models, or processing datasets.
---

## Optimized Workflow
### 1. Query Type Judgment & Analysis & Planning Phase
**Goal:** Use the `think` tool to analyze the problem and determine whether it is a simple task (refers to tasks that do not require calling the information search agent or tool) or a complex task (requires calling info seeker). If it is a complex task, it is necessary to further analyze whether it is a objective question(do not require calling the writer agent)or a long-form writing question (requires long-form expression and need to call the writer agent later).
- **Simple Tasks:** For simple tasks that do not require info seeker invocation, you can directly call the `planner_objective_task_done` tool and write the answer in `final_answer` field without creating a todo.md file.
- **Complex Tasks:**  
  - For objective tasks, must use `assign_multi_objective_tasks_to_info_seeker`
  - For academic paper tasks, must use `assign_multi_subjective_tasks_to_info_seeker`, and call the writer agent to produce a complete academic paper (NOT a report or review)
  - **Task Decomposition Rules:** 
    - Construct a task tree with a tree-like structure, where the root node represents the user's input query. Each subtask is marked with its depth in the task tree, and the entire task tree is executed from shallow to deep. Tasks at the same depth in the task tree must be independent and can be executed in parallel (via `assign_multi_xxx_tasks_to_info_seeker`) without mutual dependencies.
    - At the first level of the task tree, it is essential to thoroughly design subtasks that can be executed in parallel to explore various potential background information, thereby providing more specific clues for the next step of planning.
    - Competitive Redundancy Mechanism:
      - For key subtasks that have a significant impact on subsequent reasoning and planning, a redundancy mechanism should be established. This involves duplicating the task at the same depth level in the task tree, enabling the parallel execution of nearly identical tasks to enhance the completion rate and robustness of the task execution.
  - **Task Parallel Sending Requirements:**
    - When using `assign_multi_xxx_tasks_to_info_seeker`, all parallel-sent subtasks must be independent of each other; the description of each subtask must not contain any mutual references or dependency requirements for other subtasks.
    - There is no sequential execution relationship among all parallel-sent subtasks.

  - **Mandatory Documentation:** Create and write `todo.md` (e.g., `todo_v1.md`) with fields:  
    ```markdown
    # Task Planning Document
    ## task_name: [Clear identifier]
    ## task_desc: [Detailed requirements - focus on WHAT not HOW]
    ## deliverable_contents: [Exact output format specs]
    ## success_criteria: [Measurable 100% completion metrics]
    ## context: [Background, constraints, prior results]
    ## task_steps_for_reference: [Tree-structured preliminary execution plan, tag tasks with the depth in task tree `[DEPTH:xx]`]
    ```  

### 2. Execution & Iteration Phase
#### A. Unified Iteration Triggers (Shared by Both Types)
- Based on upper-layer task results, refine the next layer of planning and document it in a new version of `todo.md` (e.g., `todo_v2.md`).  
- If upper-layer tasks fail/encounter challenges: Invoke the `reflect` tool for introspection (no new information acquired, only saves thoughts), adjust the plan, and re-invoke the corresponding `information_seeker` method only for a concrete unresolved evidence gap. Never resend a completed broad literature batch.
- If current tasks require prior round information: Clearly specify the context of each task and referenced files (e.g., `./data/agent_output_v1.json`) when calling `information_seeker`.  
- Decompose and refine clues from upper-layer results, then execute verification in parallel.  

#### B. Query-Type-Specific Operations
- **Objective tasks**: No additional operations (strictly no writer invocation). Continue iterating until information meets `success_criteria`.  
- **Long-form writing tasks**: Add **information sufficiency check before writer invocation**:  
  1. Evaluate collected information from two dimensions: quantity (e.g., "Enough case studies for 3 chapters") and comprehensiveness (e.g., "Covers both positive and negative impacts of AI on education").  
  2. If information is insufficient: Adjust subtask directions (e.g., "Supplement AI education failure cases") and re-invoke `assign_multi_subjective_tasks_to_info_seeker` only for targeted collection. Each supplemental task must include `evidence_gap`, stating the missing claim, source type, time range, or unresolved citation. Repeating the same broad topics is forbidden.
  3. If information is sufficient: Invoke the writer via `assign_subjective_task_to_writer` (provide all collected materials and `todo.md` as context).  
  4. If the writer returns an incomplete result: Do not assist in completing it; only feed back the current completion status to the user.  

### 3. Completion & Synthesis Phase
#### A. Unified Validation & Integration (Shared by Both Types)
- **Validation**: Cross-check multi-source `information_seeker` outputs for consistency (e.g., "NBS and World Bank GDP data differ by 鈮?%").  
- **Integration**: Combine parallel outputs into a unified deliverable (e.g., "Merge two GDP data sources into a single table" or "Integrate writer鈥檚 report with supplementary case studies").  
- **Delivery**: Output language must match the user鈥檚 query language (e.g., Chinese query 鈫?Chinese deliverable).  

#### B. Query-Type-Specific Task Completion (Critical)
- **Objective tasks**: Call the `planner_objective_task_done` tool **only when** all planned tasks are completed and the final deliverable (e.g., verified data, clear answers) is ready for user delivery.  
- **Long-form writing tasks**: Call the `planner_subjective_task_done` tool **only when** the writer has finished executing and the final long-form content meets the `success_criteria` in `todo.md`.  

---

## Critical Protocols
0. **MANDATORY EXPERIMENT RULE (鏁版嵁涓庡疄楠屽己鍒惰鍒?- 缁濆绂佹浼€犳暟鎹笌浜茶嚜鍐欎唬鐮?**: 
   - 鍙浠诲姟娑夊強"鏁版嵁闆?銆?鏁版嵁鍒嗘瀽"銆?妯″瀷璁粌"銆?浠ｇ爜缂栧啓"鎴?瑙ｅ帇鏂囦欢",蹇呴』**绔嬪嵆**涓?*浠呰兘**璋冪敤 `assign_task_to_experimenter`.
   - 鈿狅笍 **鏁版嵁璇诲彇閾佸緥**:瀵逛簬琛ㄦ牸鏁版嵁(濡?`.xlsx`, `.csv`)鎴栦簩杩涘埗鏂囦欢,Planner **缁濆绂佹**浣跨敤 `file_read` 鎴?`document_qa` 宸ュ叿寮鸿璇诲彇!浣犲繀椤荤洿鎺ュ皢鏂囦欢璺緞浼犵粰 Experimenter,鍛戒护鍏剁紪鍐?Python 浠ｇ爜(濡?`pandas`)杩涜瑙ｆ瀽.
   - 馃洃 **鍙嶉€犲亣閾佸緥**:褰撶敤鎴锋彁渚涗簡瀹為獙鏁版嵁闆嗘椂,**缁濆绂佹浣跨敤 numpy 绛夊伐鍏封€滄ā鎷熲€濇垨鈥滃嚟绌轰吉閫犫€濅换浣曟暟鎹?** 鍝€曚綘鏆傛椂涓嶇煡閬撴暟鎹粨鏋?涔熻鍏堣瀹為獙鍛樺啓涓瘯鎺㈡€х殑鑴氭湰鍘?`print(df.head())` 鎽稿簳,蹇呴』 100% 浣跨敤鐢ㄦ埛鐨勭湡瀹炴暟鎹繘琛屽悗缁湡瀹炲疄楠?
   - 蹇呴』鑾峰緱瀹為獙鍛樿繑鍥炵殑鐪熷疄杩愯缁撴灉鍚?鎵嶈兘寰€涓嬫帹杩涗换鍔?
1. **Dependency Management:**  
    - Prohibit parallel dispatch for sequential dependent tasks unless using competitive redundancy mechanism
    - Convert sequential chains to parallel where possible (e.g., Hypothesis_A vs Hypothesis_B testing)  
2. **File Traceability:**  
    - All output references use relative paths (`./data/agent_output_1.json`)  
    - Version `todo.md` after each iteration (e.g., `todo_v2.md`)
3. **Local File Reading Recommendations:**
    - For files crawled natively, it is not recommended to directly use the `file_read` tool to read the entire content (maybe too long). Instead, the `document_qa` tool should be used to extract and verify the required information.
    - For task deliverables and summary documents from sub-agents, the `file_read` tool can be used to read them.
4. The final deliverable presented to the user should be consistent with the language used in the user's question.
5. **Writer invocation**: Strictly prohibit calling the writer for objective tasks; for academic paper tasks, **never directly answer**鈥攎ust invoke the writer to produce the final academic paper.
6. **OUTPUT CONVENTION(杈撳嚭瑙勮寖)- 閾佸緥,涓嶅緱杩濆弽 **: 
   - 淇℃伅鏀堕泦瀹屾垚鍚?涓嬩竴姝ュ繀椤讳笖鍙兘璋冪敤 `assign_subjective_task_to_writer`.
   - 涓ョ鐩存帴璋冪敤 `writer_subjective_task_done` 鎴?`planner_subjective_task_done`.
   - 璋冪敤 `writer_subjective_task_done` 鍜?`planner_subjective_task_done` 鐨勬潈鍔?
     鍙睘浜?WriterAgent 鎵ц瀹屾瘯鍚庣殑杩斿洖淇″彿,Planner 鏈韩缁濆绂佹涓诲姩璋冪敤杩欎袱涓伐鍏?
   - 杩濆弽姝よ鍒?= 浠诲姟澶辫触.
   - 鍦?task_content 鍙傛暟涓?绂佹浣跨敤"鎶ュ憡"銆?鍒嗘瀽鎶ュ憡"绛夊瓧鐪?蹇呴』浣跨敤"瀛︽湳璁烘枃".
   - task_content 蹇呴』鏄庣‘瑕佹眰 Writer 鎸夌収 Title/Abstract/Introduction/Methodology/Results/Discussion/Conclusion/References 缁撴瀯杈撳嚭.
7. **CRAWLER BLACKLIST (鐖櫕榛戝悕鍗?**:
   - 涓ョ瀵逛互涓嬪煙鍚嶈皟鐢?`url_crawler`: mdpi.com, ieeexplore.ieee.org, sciencedirect.com.
   - 鐞嗙敱:杩欎簺缃戠珯浼氭嫤鎴埇铏鑷寸郴缁熷崱姝?
   - 鏇夸唬鏂规:濡傛灉鏄鏈鏂?蹇呴』浼樺厛浣跨敤 `arxiv_search` 鎴?`search_pubmed_key_words`.
8. IMAGE ANALYSIS PROTOCOL (鍥剧墖鍒嗘瀽鎸囧紩):
    - **IMAGE ANALYSIS (璇诲浘鎸囧紩):** 閬囧埌 .png/.jpg 鍥剧墖,绂佹浣跨敤 `document_qa`.浣?*蹇呴』**璋冪敤 `analyze_image` 宸ュ叿鎻愬彇骞跺垎鏋?灏嗘彁鍙栧埌鐨勬暟鎹紶缁?WriterAgent.
    - 鎮ㄥ彲浠ュ湪 `prompt` 鍙傛暟涓槑纭姹傛彁鍙栨暟鎹?
    - 鎷垮埌鍒嗘瀽缁撴灉鍚?璇峰皢鍏舵暟鍊艰繛鍚屽浘鐗囧師濮嬭矾寰勪竴骞舵彁渚涚粰 WriterAgent,骞跺湪璁烘枃涓紩鐢ㄥ師鍥?
9. IMAGE INTEGRATION (鍥炬枃鏁村悎 - 寮哄埗瑕佹眰):
    - 濡傛灉瀹為獙鐢熸垚浜嗛噸瑕佺殑鍙鍖栧浘琛?璁板綍鍦?`output_figures` 涓?,鍦ㄨ皟鐢?`assign_subjective_task_to_writer` 鏃?**蹇呴』**灏嗘墍鏈夊浘鐗囪矾寰勬坊鍔犲埌 `key_files` 鍙傛暟涓?
    - 姣忎釜鍥剧墖鐨勬牸寮忓簲涓?{"file_path": "鍥剧墖璺緞", "desc": "鍥剧墖鎻忚堪鍜屽疄楠屾暟鎹?}
    - 鍚屾椂鍦?task_content 涓槑纭姹?Writer 浣跨敤 `![鎻忚堪](鍥剧墖璺緞)` 璇硶鎻掑叆杩欎簺鍥剧墖鍒拌鏂囦腑.
    - 杩欐槸寮哄埗瑕佹眰,涓嶆槸寤鸿!閬楁紡鍥剧墖浼氬鑷磋鏂囦笉瀹屾暣.
10. **WRITER CALL PROTOCOL(璋冪敤Writer鍗忚)- 閾佸緥**:
   - assign_subjective_task_to_writer 鏄悓姝ラ樆濉炶皟鐢?璋冪敤鍚庝笉瑕佸仛浠讳綍鍏朵粬鎿嶄綔.
   - 璋冪敤杩斿洖 success 鍚?绔嬪嵆璋冪敤 planner_subjective_task_done,涓嶈鍐嶆鏌ユ枃浠舵槸鍚﹀瓨鍦?
   - 涓ョ鍦?writer 杩斿洖涔嬪墠鎴栦箣鍚庣敤 file_find_by_name銆乴ist_workspace 杞璁烘枃鏂囦欢.
   - key_files 閲屽繀椤诲寘鍚?./experiment_results/experiment_results.md 鍜屾墍鏈?./experiment_results/*.png 鍥剧墖,浠ュ強 ./user_uploads/ 涓嬬殑鎵€鏈夌敤鎴蜂笂浼犲浘鐗?*.png, *.jpg).user_uploads 涓殑鍥剧墖蹇呴』淇濈暀 ./user_uploads/ 璺緞,涓嶈娣峰叆 experiment_results.
   - 鑻ラ仐婕忓疄楠岀粨鏋滄枃浠舵垨鍥剧墖 = 璁烘枃涓嶅畬鏁?= 浠诲姟澶辫触.
Below, within the <tools></tools> tags, are the descriptions of each tool and the required fields for invocation:
<tools>
$tool_schemas
</tools>
For each function call, return a JSON object placed within the [unused11][unused12] tags, which includes the function name and the corresponding function arguments:
[unused11][{\"name\": <function name>, \"arguments\": <args json object>}][unused12]
CRITICAL: `arguments` must be a JSON object, never a quoted/stringified JSON value. Use `"arguments": {"tasks": [...]}`, not `"arguments": "{\"tasks\": [...]}"`."""

        writing_system_prompt_template = """### PlannerAgent: Multi-Agent Task Coordinator  
**Role:** Analyze complex queries, create structured plans, and coordinate specialized agents to deliver comprehensive solutions.  

#### Available Sub-Agents:  
- **`information_seeker`**: Research, data gathering, web search (supports single/parallel multi-task)  
- **`writer`**: Writes academic papers in standard journal format (Title/Abstract/Introduction/Methodology/Results/Discussion/Conclusion/References), NOT reports or reviews.
- **`experimenter`**: Invoke this via `assign_task_to_experimenter` for any dataset processing, unzipping large files, or training models.
---

### Optimized Workflow  
#### 1. Analysis & Planning Phase  
**Goal:** Analyze the problem and determine whether it is a simple task or a complex task. If it is a complex task, it is necessary to further analyze whether it is a subject-driven question or an objective-driven question, so as to decompose the problem into multiple clear and executable subtasks according to the specific problem type. The main characteristic of objective-driven questions is that their answers are clear and verifiable entities, otherwise they are subject-driven questions. 
- **Simple Tasks:** For simple tasks that do not require sub-agent invocation, you can directly answer without creating a todo.md file
- **Complex Tasks:**  
  - For Objective-driven tasks, Adopt *diverge-converge* strategy:  
    1. Use `assign_multi_subjective_tasks_to_info_seeker` call for divergent background research  
    2. Converge findings to define specific sub-problems  
  - For Subject-driven tasks, Adopt *multi-perspective* strategy:
    1. Use assign_multi_subjective_tasks_to_info_seeker call for divergent multi-source exploration (each task targets independent dimensions)
    2. Converge findings to define focused sub-problems addressing distinct knowledge gaps
    3. When the information seeker collects information, start to call the writer agent to integrate the collected information to produce a complete academic paper with real experimental data
  - **Task Decomposition Rules:**  
    - Construct a task tree with a tree-like structure, where the root node represents the user's input query. Each subtask is marked with its depth in the task tree, and the entire task tree is executed from shallow to deep. Tasks at the same depth in the task tree must be independent and can be executed in parallel (via `assign_multi_subjective_tasks_to_info_seeker`) without mutual dependencies.
    - At the first level of the task tree, it is essential to thoroughly design subtasks that can be executed in parallel to explore various potential background information, thereby providing more specific clues for the next step of planning.
    - Competitive Redundancy Mechanism:
      - For key subtasks that have a significant impact on subsequent reasoning and planning, a redundancy mechanism should be established. This involves duplicating the task at the same depth level in the task tree, enabling the parallel execution of nearly identical tasks to enhance the completion rate and robustness of the task execution.
  - **Task Parallel Sending Requirements:**
    - When using `assign_multi_subjective_tasks_to_info_seeker`, all parallel-sent subtasks must be independent of each other; the description of each subtask must not contain any mutual references or dependency requirements for other subtasks.
    - There is no sequential execution relationship among all parallel-sent subtasks.

  - **Mandatory Documentation:** Create and write `todo.md` (e.g., `todo_v1.md`) with fields:  
    ```markdown
    # Task Planning Document
    ## task_name: [Clear identifier]
    ## task_desc: [Detailed requirements - focus on WHAT not HOW]
    ## deliverable_contents: [Exact output format specs]
    ## success_criteria: [Measurable 100% completion metrics]
    ## context: [Background, constraints, prior results]
    ## task_steps_for_reference: [Tree-structured preliminary execution plan, tag tasks with the depth in task tree `[DEPTH:xx]`]
    ```  
#### 2. Execution & Iteration Phase
- **Iteration Triggers:**
  - Based on the execution results of the upper layer, specify and refine the next layer of planning using the `think` tool. **STRICTLY PROHIBITED:** Do NOT attempt to use `bash` or other tools to save physical files like `todo_v2.md` to the disk, as this will cause infinite loops. Keep the plan updates in your memory.
  - Once the information is deemed sufficient according to your virtual plan, you MUST IMMEDIATELY invoke `assign_subjective_task_to_writer` (or the appropriate agent). Do NOT delay the process by re-evaluating the plan repeatedly.
  - If there are tasks in the previous layer that have failed or encountered challenges, it is necessary to invoke `reflect` for introspection, consider more possibilities, and make new task planning and invoke `assign_multi_subjective_tasks_to_info_seeker` again. 
  - If the tasks sent in the current round require reference to task information from previous rounds, it is essential to clearly specify the context of each task and the files that may need to be used or referenced when calling `assign_multi_subjective_tasks_to_info_seeker`.
  - For the multiple clues of the execution results from the previous layer, they should be decomposed and refined, and executed in parallel for verification.
- **Information check required before calling writer:**  
  - Before invoking writer, analyze collected information for sufficiency: evaluate both quantity and comprehensiveness to ensure adequate material for a full academic paper
  - If information is insufficient, adjust subtask direction and initiate additional targeted information collection
- **When information is sufficient, invoke writer agent** via `assign_subjective_task_to_writer`

#### 3. Completion & Synthesis Phase  
- **Validation:** Cross-check multi-source outputs for consistency, and Check whether the information source is sufficient
- **Integration:** Combine parallel outputs into unified deliverable  
- **Delivery:** Output language must match user's query language  
- When the writer agent is finished executing, planner_subjective_task_done tool needs to be called to end the current task

---

### Critical Protocols  
1. **Dependency Management:**  
   - Prohibit parallel dispatch for sequential dependent tasks unless using competitive redundancy mechanism
   - Convert sequential chains to parallel where possible (e.g., Hypothesis_A vs Hypothesis_B testing)
2. **File Traceability:**  
   - All output references use relative paths (`./data/agent_output_1.json`)  
   - Version `todo.md` after each iteration (e.g., `todo_v2.md`)  
3. **Iteration Discipline:**  
   - Minimum 2 parallel agents for critical hypothesis-validation tasks  
   - Terminate only when ALL success criteria are met at 100%  
5. **Usage of Think Tool:**
   - `think` is a systematic tool. After receiving the response from the complex tool or before invoking any other tools, you must **first invoke the `think` tool**: to deeply reflect on the results of previous tool invocations (if any), and to thoroughly consider and plan the user's task. The `think` tool does not acquire new information; it only saves your thoughts into memory.
6. **Usage of Reflect Tool:**
    `reflect` is a systematic tool. When encountering a failure in tool execution, it is necessary to invoke the reflect tool to conduct a review and revise the task plan. It does not acquire new information; it only saves your thoughts into memory.
7. Always prioritize complete solutions over partial delivery. Use parallel redundancy for critical path tasks, and convert agent disagreements into new parallel investigation branches.
8. **CRITICAL:** When you determine that the information_seeker has gathered sufficient information, you must invoke the writer agent to write the final academic paper in response to the user's query. You are not allowed to reply directly based on the collected information!
9.Also note that when the writing agent returns a result that shows it is not completed, you do not need to help it complete it further. You only need to feedback the current completion status to the user.
10. **WRITER CALL PROTOCOL(璋冪敤Writer鍗忚)- 閾佸緥**:
   - assign_subjective_task_to_writer 鏄悓姝ラ樆濉炶皟鐢?璋冪敤鍚庝笉瑕佸仛浠讳綍鍏朵粬鎿嶄綔.
   - 璋冪敤杩斿洖 success 鍚?绔嬪嵆璋冪敤 planner_subjective_task_done,涓嶈鍐嶆鏌ユ枃浠舵槸鍚﹀瓨鍦?
   - 涓ョ鍦?writer 杩斿洖涔嬪墠鎴栦箣鍚庣敤 file_find_by_name銆乴ist_workspace 杞璁烘枃鏂囦欢.
   - key_files 閲屽繀椤诲寘鍚?./experiment_results/experiment_results.md 鍜屾墍鏈?./experiment_results/*.png 鍥剧墖,浠ュ強 ./user_uploads/ 涓嬬殑鎵€鏈夌敤鎴蜂笂浼犲浘鐗?*.png, *.jpg).user_uploads 涓殑鍥剧墖蹇呴』淇濈暀 ./user_uploads/ 璺緞,涓嶈娣峰叆 experiment_results.
   - 鑻ラ仐婕忓疄楠岀粨鏋滄枃浠舵垨鍥剧墖 = 璁烘枃涓嶅畬鏁?= 浠诲姟澶辫触.
Below, within the <tools></tools> tags, are the descriptions of each tool and the required fields for invocation:
<tools>
$tool_schemas
</tools>
For each function call, return a JSON object placed within the [unused11][unused12] tags, which includes the function name and the corresponding function arguments:
[unused11][{\"name\": <function name>, \"arguments\": <args json object>}][unused12]
CRITICAL: `arguments` must be a JSON object, never a quoted/stringified JSON value. Use `"arguments": {"tasks": [...]}`, not `"arguments": "{\"tasks\": [...]}"`."""

        qa_system_prompt_template = """### PlannerAgent: Multi-Agent Task Coordinator  
**Role:** Analyze complex queries, create structured plans, and coordinate specialized agents to deliver comprehensive solutions.  

#### Available Sub-Agents:  
- **`information_seeker`**: Research, data gathering, web search (supports single/parallel multi-task)  
- **`experimenter`**: Invoke this via `assign_task_to_experimenter` for any dataset processing, unzipping large files, or training models.
---

### Optimized Workflow  
#### 1. Analysis & Planning Phase  
**Goal:** Decompose problems into executable units with clear dependencies  
- **Simple Tasks:** For simple tasks that do not require sub-agent invocation, you can directly answer and call `planner_objective_task_done` without creating a todo.md file
- **Complex Tasks:**
  - **Task Decomposition Rules:**  
    - Construct a task tree with a tree-like structure, where the root node represents the user\'s input query. Each subtask is marked with its depth in the task tree, and the entire task tree is executed from shallow to deep. Tasks at the same depth in the task tree must be independent and can be executed in parallel (via `assign_multi_objective_tasks_to_info_seeker`) without mutual dependencies.
    - At the first level of the task tree, it is essential to thoroughly design subtasks that can be executed in parallel to explore various potential background information, thereby providing more specific clues for the next step of planning.
    - Competitive Redundancy Mechanism:
      - For key subtasks that have a significant impact on subsequent reasoning and planning, a redundancy mechanism should be established. This involves duplicating the task at the same depth level in the task tree, enabling the parallel execution of nearly identical tasks to enhance the completion rate and robustness of the task execution.
  - **Task Parallel Sending Requirements:**
    - When using `assign_multi_objective_tasks_to_info_seeker`, all parallel-sent subtasks must be independent of each other; the description of each subtask must not contain any mutual references or dependency requirements for other subtasks.
    - There is no sequential execution relationship among all parallel-sent subtasks.

  - **Mandatory Documentation:** Create and write `todo.md` (e.g., `todo_v1.md`) with fields:  
    ```markdown
    # Task Planning Document
    ## task_name: [Clear identifier]
    ## task_desc: [Detailed requirements - focus on WHAT not HOW]
    ## deliverable_contents: [Exact output format specs]
    ## success_criteria: [Measurable 100% completion metrics]
    ## context: [Background, constraints, prior results]
    ## task_steps_for_reference: [Tree-structured preliminary execution plan, tag tasks with the depth in task tree `[DEPTH:xx]`]
    ```  

#### 2. Execution & Iteration Phase
- **Iteration Triggers:**
  - Based on the execution results of the upper layer of the task tree, specify and refine the next layer and subsequent task planning, and document them in a new `todo.md` file (e.g., `todo_v2.md`).
  - If there are tasks in the previous layer that have failed or encountered challenges, it is necessary to invoke `reflect` for introspection, consider more possibilities, and make new task planning and invoke `assign_multi_objective_tasks_to_info_seeker` again. 
  - If the tasks sent in the current round require reference to task information from previous rounds, it is essential to clearly specify the context of each task and the files that may need to be used or referenced when calling `assign_multi_objective_tasks_to_info_seeker`.
  - For the multiple clues of the execution results from the previous layer, they should be decomposed and refined, and executed in parallel for verification.

#### 3. Completion & Synthesis Phase  
- **Validation:** Cross-check multi-source outputs for consistency
- **Integration:** Combine parallel outputs into unified deliverable  
- **Delivery:** Output language must match user\'s query language  
- **Task Completed:** The `planner_objective_task_done` can only be called when all planned tasks have been completed and the final results are ready to be delivered to the user.

---

### Critical Protocols  
1. **Dependency Management:**  
   - Prohibit parallel dispatch for sequential dependent tasks unless using competitive redundancy mechanism
   - Convert sequential chains to parallel where possible (e.g., Hypothesis_A vs Hypothesis_B testing)  
2. **File Traceability:**  
   - All output references use relative paths (`./data/agent_output_1.json`)  
   - Version `todo.md` after each iteration (e.g., `todo_v2.md`)
3. **Local File Reading Recommendations:**
    - For files crawled natively, it is not recommended to directly use the `file_read` tool to read the entire content (maybe too long). Instead, the `document_qa` tool should be used to extract and verify the required information.
    - For task deliverables and summary documents from sub-agents, the `file_read` tool can be used to read them.
4. The final deliverable presented to the user should be consistent with the language used in the user\'s question.
5. MANDATORY EXPERIMENT RULE: Never use `file_read` on .zip/.tar.gz or large datasets (>10MB). Always delegate dataset unzipping, data analysis, model training, or computational experiments to the `experimenter` agent using `assign_task_to_experimenter`. Get real task-relevant metrics before calling `planner_objective_task_done`.
6. **WRITER CALL PROTOCOL(璋冪敤Writer鍗忚)- 閾佸緥**:
   - assign_subjective_task_to_writer 鏄悓姝ラ樆濉炶皟鐢?璋冪敤鍚庝笉瑕佸仛浠讳綍鍏朵粬鎿嶄綔.
   - 璋冪敤杩斿洖 success 鍚?绔嬪嵆璋冪敤 planner_subjective_task_done,涓嶈鍐嶆鏌ユ枃浠舵槸鍚﹀瓨鍦?
   - 涓ョ鍦?writer 杩斿洖涔嬪墠鎴栦箣鍚庣敤 file_find_by_name銆乴ist_workspace 杞璁烘枃鏂囦欢.
   - key_files 閲屽繀椤诲寘鍚?./experiment_results/experiment_results.md 鍜屾墍鏈?./experiment_results/*.png 鍥剧墖,浠ュ強 ./user_uploads/ 涓嬬殑鎵€鏈夌敤鎴蜂笂浼犲浘鐗?*.png, *.jpg).user_uploads 涓殑鍥剧墖蹇呴』淇濈暀 ./user_uploads/ 璺緞,涓嶈娣峰叆 experiment_results.
   - 鑻ラ仐婕忓疄楠岀粨鏋滄枃浠舵垨鍥剧墖 = 璁烘枃涓嶅畬鏁?= 浠诲姟澶辫触.
Below, within the <tools></tools> tags, are the descriptions of each tool and the required fields for invocation:
<tools>
$tool_schemas
</tools>
For each function call, return a JSON object placed within the [unused11][unused12] tags, which includes the function name and the corresponding function arguments:
[unused11][{\"name\": <function name>, \"arguments\": <args json object>}][unused12]
CRITICAL: `arguments` must be a JSON object, never a quoted/stringified JSON value. Use `"arguments": {"tasks": [...]}`, not `"arguments": "{\"tasks\": [...]}"`."""

        planner_mode_system_prompt_map = {
            "auto": auto_system_prompt_template,
            "writing": writing_system_prompt_template,
            "qa": qa_system_prompt_template
        }

        system_prompt = planner_mode_system_prompt_map[self.config.planner_mode].replace("$tool_schemas", tool_schemas_str)
        anti_involution_prompt = """
                        ### AUTONOMOUS RESEARCH CONTROL
                        You own the research plan. Re-plan whenever evidence, experiments, or reviewer feedback exposes a gap.
                        InformationSeeker and ExperimentAgent may be called repeatedly and in whichever order the evidence requires.
                        Do not target a fixed number of references and do not stop after a predetermined chapter sequence.
                        WriterAgent may start only after literature and experimental evidence have each been examined successfully at least once;
                        after that, further searches or experiments remain allowed before or during revision.

                        ### JSON SYNTAX SURVIVAL RULE
                        1. NO REAL LINE BREAKS: Use `\\n`. DO NOT press Enter inside a string!
                        2. DOUBLE QUOTES ONLY.
                        3. ESCAPE QUOTES: `\\"`.

                        ### EVIDENCE AND SYNCHRONOUS EXECUTION
                        1. NO BACKGROUND TASKS: All sub-agents run SYNCHRONOUSLY.
                        2. Do not poll for sub-agents. Evaluate their returned evidence, re-plan if needed, and delegate WriterAgent only when the evidence gate is satisfied.
                        """
        system_prompt = system_prompt.replace("<tools>", anti_involution_prompt + "\n<tools>")
        return get_skill_loader().inject_agent_skills(
            system_prompt,
            self.config.agent_name,
            compact=True
        )

    def assign_multi_objective_tasks_to_info_seeker(
            self,
            tasks: List[Dict[str, str]],
            max_workers: int = 5
        ) -> Dict[str, Any]:
        """
        Creates multiple TaskInput objects and routes them to info_seeker agents for concurrent execution.
        This tool enables the PlannerAgent to assign multiple research tasks through the MCP tool interface.

        Args:
            tasks: List of task dictionaries with the following keys:
                - task_content (required): The specific task content
                - task_steps_for_reference: Optional reference steps for execution
                - deliverable_contents: Format of expected deliverable
                - acceptance_checking_criteria: Criteria for task completion and quality
                - workspace_id: Workspace ID for stored files and memory
                - current_task_status: Description of current task status

            max_workers: Maximum concurrent threads (default=4)

        Returns:
            MCPToolResult with execution results for all tasks
        """
        try:
            # Validate task count (1-4 tasks)
            if not (1 <= len(tasks) <= 5):
                return {
                    "success": False,
                    "error": f"Invalid task count ({len(tasks)}). Must assign 1~5 tasks. Please re-plan the task execution schedule or re-decompose the task."
                }

            # Import here to avoid circular imports
            try:
                from agents import TaskInput, create_objective_information_seeker
            except ImportError:
                from ..agents import TaskInput, create_objective_information_seeker

            results = []
            import threading
            lock = threading.Lock()

            def process_task(task: Dict[str, str]):
                """Process a single task with thread-safe result collection"""
                try:
                    if self._check_cancellation():
                        response_data = {
                            "task_content": task.get("task_content", "Unknown task"),
                            "success": False,
                            "error": "Task cancelled by user"
                        }
                        with lock:
                            results.append(response_data)
                        return response_data


                    # Create TaskInput object
                    task_input = TaskInput(
                        task_content=task["task_content"],
                        task_steps_for_reference=task.get("task_steps_for_reference"),
                        deliverable_contents=task.get("deliverable_contents"),
                        current_task_status=task.get("current_task_status"),
                        workspace_id=None,  # Session/workspace is managed by the server; no need to set explicitly
                        acceptance_checking_criteria=task.get("acceptance_checking_criteria")
                    )

                    # Create and execute with info seeker agent - use shared MCP client for session consistency
                    info_seeker_config = getattr(self, 'sub_agent_configs', {}).get('information_seeker', {})
                    info_seeker = create_objective_information_seeker(
                        model=info_seeker_config.get('model', self.config.model),
                        max_iterations=60,
                        shared_mcp_client=self.mcp_tools.client if hasattr(self.mcp_tools, 'client') else self.mcp_tools
                    )
                    info_seeker.task_id = self.task_id
                    if hasattr(self, "_cancellation_token") and self._cancellation_token:
                        info_seeker.set_cancellation_token(self._cancellation_token)

                    self.logger.info(f"Assigning task to InformationSeekerAgent: {task['task_content'][:8000]}...")


                    # Execute the task
                    response = info_seeker.execute_task(task_input)

                    if response.success:
                        response_data = {
                            "task_content": task.get("task_content", "Unknown task"),
                            "success": True,
                            "data": response.result,
                            "agent_name": response.agent_name,
                            "iterations": response.iterations,
                            "execution_time": response.execution_time,
                            # "reasoning_trace": response.reasoning_trace
                        }
                    else:
                        response_data = {
                            "task_content": task.get("task_content", "Unknown task"),
                            "success": False,
                            "error": response.error,
                            "agent_name": response.agent_name
                        }

                    # Thread-safe result collection
                    with lock:
                        results.append(response_data)

                    return response_data

                except Exception as e:
                    error_msg = f"Task processing failed: {str(e)}"
                    self.logger.error(error_msg)
                    with lock:
                        results.append({
                            "task_content": task.get("task_content", "Unknown task"),
                            "success": False,
                            "error": error_msg
                        })
                    return None

            # Execute tasks in parallel with thread pool
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(process_task, task) for task in tasks]
                # Wait for all tasks to complete
                for future in futures:
                    if self._check_cancellation():
                        break
                    future.result()  # Raise exceptions if any

            # Check overall success
            all_success = all(task_result.get("success", False) for task_result in results)

            return {
                "success": all_success,
                "data": {"tasks": results},
                "error": None if all_success else "Some tasks failed",
                "metadata": {
                    "tool_name": "assign_multi_objective_tasks_to_info_seeker",
                    "task_count": len(tasks),
                    "success_count": sum(1 for r in results if r.get("success")),
                    "failure_count": sum(1 for r in results if not r.get("success"))
                }
            }

        except Exception as e:
            self.logger.error(f"Multi-task assignment failed: {e}")
            return {
                "success": False,
                "error": f"Multi-task assignment failed: {str(e)}"
            }


    # def assign_multi_subjective_tasks_to_info_seeker(
    #         self,
    #         tasks: List[Dict[str, str]],
    #         max_workers: int = 5
    # ) -> Dict[str, Any]:
    #     """
    #     Creates multiple TaskInput objects and routes them to info_seeker agents for concurrent execution.
    #     This tool enables the PlannerAgent to assign multiple research tasks through the MCP tool interface.
    #
    #     Args:
    #         tasks: List of task dictionaries with the following keys:
    #             - task_content (required): The specific task content
    #             - task_steps_for_reference: Optional reference steps for execution
    #             - deliverable_contents: Format of expected deliverable
    #             - acceptance_checking_criteria: Criteria for task completion and quality
    #             - workspace_id: Workspace ID for stored files and memory
    #             - current_task_status: Description of current task status
    #
    #         max_workers: Maximum concurrent threads (default=4)
    #
    #     Returns:
    #         MCPToolResult with execution results for all tasks
    #     """
    #     try:
    #         # Validate task count (1-4 tasks)
    #         if not (1 <= len(tasks) <= 6):
    #             return {
    #                 "success": False,
    #                 "error": f"Invalid task count ({len(tasks)}). Must assign 1-6 tasks."
    #             }
    #
    #         # Import here to avoid circular imports
    #         try:
    #             from agents import TaskInput, create_subjective_information_seeker
    #         except ImportError:
    #             from ..agents import TaskInput, create_subjective_information_seeker
    #
    #         results = []
    #         import threading
    #         lock = threading.Lock()
    #
    #         def process_task(task: Dict[str, str]):
    #             """Process a single task with thread-safe result collection"""
    #             try:
    #                 # Create TaskInput object
    #                 task_input = TaskInput(
    #                     task_content=task["task_content"],
    #                     task_steps_for_reference=task.get("task_steps_for_reference"),
    #                     deliverable_contents=task.get("deliverable_contents"),
    #                     current_task_status=task.get("current_task_status"),
    #                     workspace_id=self.get_session_info()["session_id"],  # Session/workspace is managed by the server; no need to set explicitly
    #                     acceptance_checking_criteria=task.get("acceptance_checking_criteria")
    #                 )
    #
    #                 # Create and execute with info seeker agent - use shared MCP client for session consistency
    #                 info_seeker_config = getattr(self, 'sub_agent_configs', {}).get('information_seeker', {})
    #                 info_seeker = create_subjective_information_seeker(
    #                     model=info_seeker_config.get('model', self.config.model),
    #                     max_iterations=info_seeker_config.get('max_iterations', 30),
    #                     shared_mcp_client=self.mcp_tools.client if hasattr(self.mcp_tools, 'client') else self.mcp_tools
    #                 )
    #
    #                 self.logger.info(f"Assigning task to InformationSeekerAgent: {task['task_content'][:8000]}...")
    #
    #                 # Execute the task
    #                 response = info_seeker.execute_task(task_input)
    #
    #                 if response.success:
    #                     response_data = {
    #                         "task_content": task.get("task_content", "Unknown task"),
    #                         "success": True,
    #                         "data": response.result,
    #                         "agent_name": response.agent_name,
    #                         "iterations": response.iterations,
    #                         "execution_time": response.execution_time,
    #                         # "reasoning_trace": response.reasoning_trace
    #                     }
    #                 else:
    #                     response_data = {
    #                         "task_content": task.get("task_content", "Unknown task"),
    #                         "success": False,
    #                         "error": response.error,
    #                         "agent_name": response.agent_name
    #                     }
    #
    #                     # Thread-safe result collection
    #                 with lock:
    #                     results.append(response_data)
    #
    #                 return response_data
    #
    #             except Exception as e:
    #                 error_msg = f"Task processing failed: {str(e)}"
    #                 self.logger.error(error_msg)
    #                 with lock:
    #                     results.append({
    #                         "task_content": task.get("task_content", "Unknown task"),
    #                         "success": False,
    #                         "error": error_msg
    #                     })
    #                 return None
    #
    #         # Execute tasks in parallel with thread pool
    #         with ThreadPoolExecutor(max_workers=max_workers) as executor:
    #             futures = [executor.submit(process_task, task) for task in tasks]
    #             # Wait for all tasks to complete
    #             for future in futures:
    #                 future.result()  # Raise exceptions if any
    #
    #         # Check overall success
    #         all_success = all(task_result.get("success", False) for task_result in results)
    #
    #         return {
    #             "success": all_success,
    #             "data": {"tasks": results},
    #             "error": None if all_success else "Some tasks failed",
    #             "metadata": {
    #                 "tool_name": "assign_multi_subjective_tasks_to_info_seeker",
    #                 "task_count": len(tasks),
    #                 "success_count": sum(1 for r in results if r.get("success")),
    #                 "failure_count": sum(1 for r in results if not r.get("success"))
    #             }
    #         }
    #
    #     except Exception as e:
    #         self.logger.error(f"Multi-task assignment failed: {e}")
    #         return {
    #             "success": False,
    #             "error": f"Multi-task assignment failed: {str(e)}"
    #         }
    def assign_multi_subjective_tasks_to_info_seeker(
            self,
            tasks: List[Dict[str, str]] = None,  # 馃殌 蹇呴』鍔犻粯璁ゅ€?= None
            max_workers: int = 3,  # 榛樿鏀逛负 1,寮哄埗椤哄簭鎵ц
            **kwargs  # 馃殌 蹇呴』鍔犺繖涓粦娲炲弬鏁?鍚告敹澶фā鍨嬬殑骞昏鍙傛暟!
    ) -> Dict[str, Any]:
        """
        銆愪慨澶嶇増銆戦『搴忔墽琛屽涓皟鐮斾换鍔?
        瑙ｅ喅澶氫釜 InformationSeekerAgent 瀹炰緥骞跺彂杩愯瀵艰嚧鐨勮凯浠ｆ贩涔卞拰鏂囦欢鍐欏叆鍐茬獊.
        """
        try:
            # ================= 馃殌 鑷姩绾犻敊鏈哄埗寮€濮?=================
            if tasks is None:
                tasks = []

            # 濡傛灉澶фā鍨嬬姱鍌?鎶?task_content 鐩存帴浼犲埌浜嗛《灞?鎴戜滑鎵嬪姩甯畠濉炶繘 tasks 鍒楄〃閲?
            if "task_content" in kwargs:
                tasks.append({
                    "task_content": kwargs.get("task_content", ""),
                    "deliverable_contents": kwargs.get("deliverable_contents", ""),
                    "current_task_status": kwargs.get("current_task_status", ""),
                    "evidence_gap": kwargs.get("evidence_gap", ""),
                })
            expanded_tasks: List[Dict[str, str]] = []
            for task in tasks or []:
                expanded_tasks.extend(self._split_broad_literature_task(task))
            tasks, within_batch_duplicates = self._deduplicate_information_tasks(expanded_tasks)
            if within_batch_duplicates:
                self.logger.info(
                    "检索任务批内去重：原始拆分后 %s 个，移除重复 %s 个，保留 %s 个",
                    len(expanded_tasks), len(within_batch_duplicates), len(tasks),
                )
            # ================= 馃殌 鑷姩绾犻敊鏈哄埗缁撴潫 =================

            # 1. 鍩虹鏍￠獙
            if not tasks:
                return {"success": False, "error": "Task list is empty"}

            if len(tasks) > 6:
                return {
                    "success": False,
                    "error": (
                        f"Too many distinct literature tasks ({len(tasks)}). Max 6 allowed. "
                        "Merge overlapping topics and keep only the six evidence gaps most important to the paper."
                    ),
                    "metadata": {"duplicate_tasks_removed": len(within_batch_duplicates)},
                }

            self._ensure_information_dedup_state()
            requested_stage = None
            if getattr(self, "task_id", None):
                try:
                    from src.utils.task_manager import task_manager
                    requested_stage = task_manager.peek_requested_stage(self.task_id)
                except Exception:
                    requested_stage = None
            if requested_stage and requested_stage.get("stage") != "information_search":
                self._information_seeker_completed = True
                self.logger.info(
                    "用户要求切换到 %s，跳过尚未开始的文献检索批次",
                    requested_stage.get("stage"),
                )
                return {
                    "success": True,
                    "data": {
                        "tasks": [],
                        "completion_status": "stopped_by_user_guidance",
                        "requested_stage": requested_stage.get("stage"),
                        "next_action": f"Call the {requested_stage.get('stage')} stage now; do not continue literature search.",
                    },
                    "metadata": {
                        "tool_name": "assign_multi_subjective_tasks_to_info_seeker",
                        "requested_stage": requested_stage.get("stage"),
                    },
                }
            retry_only = False
            original_requested_count = len(tasks)
            if self._failed_information_tasks:
                max_failed_retries = max(0, int(os.getenv("INFO_SEEKER_FAILED_TASK_MAX_RETRIES", "1")))
                retry_records = [
                    record for record in self._failed_information_tasks.values()
                    if int(record.get("retry_attempts", 0)) < max_failed_retries
                ]
                if retry_records:
                    tasks = [dict(record["task"]) for record in retry_records]
                    retry_only = True
                    self.logger.info(
                        "检测到 %s 个失败调研子任务；本轮只重试失败项，忽略 Planner 新提交的其余 %s 项",
                        len(tasks), max(0, original_requested_count - len(tasks)),
                    )
                    self._publish_agent_progress(
                        "information_retry_started", "只重试上轮失败的文献子任务",
                        retry_count=len(tasks), ignored_new_tasks=max(0, original_requested_count - len(tasks)),
                        summary=f"上轮有 {len(tasks)} 个子任务失败，本轮不会重新执行已经成功的检索任务。",
                    )
                else:
                    exhausted = [
                        {
                            "task_content": record["task"].get("task_content", ""),
                            "error": record.get("last_error", "unknown error"),
                            "retry_attempts": record.get("retry_attempts", 0),
                        }
                        for record in self._failed_information_tasks.values()
                    ]
                    self._failed_information_tasks.clear()
                    self.logger.warning(
                        "失败调研子任务已达到重试上限；不再重新启动已成功的检索任务"
                    )
                    return {
                        "success": True,
                        "data": {
                            "tasks": [], "failed_tasks": exhausted,
                            "completion_status": "failed_retry_exhausted",
                            "next_action": "Proceed with the evidence already collected; do not restart completed literature tasks.",
                        },
                        "metadata": {
                            "tool_name": "assign_multi_subjective_tasks_to_info_seeker",
                            "retry_exhausted": True,
                        },
                    }
            incoming_batch_fingerprint = self._information_batch_fingerprint(tasks)
            if incoming_batch_fingerprint in self._successful_information_batch_fingerprints:
                self.logger.warning(
                    "Blocked repeated InformationSeeker batch: all %s tasks already completed successfully",
                    len(tasks),
                )
                self._publish_agent_progress(
                    "information_search_ready",
                    "已跳过重复文献检索",
                    summary="这批检索任务此前已经成功完成，Planner 将继续实验分析或写作。",
                    skipped_count=len(tasks) + len(within_batch_duplicates),
                )
                return {
                    "success": True,
                    "data": {
                        "tasks": [],
                        "completion_status": "already_completed",
                        "skipped_count": len(tasks) + len(within_batch_duplicates),
                        "next_action": (
                            "Do not dispatch this literature batch again. Continue to ExperimentAgent or WriterAgent. "
                            "If genuinely new evidence is missing, submit only targeted tasks and state evidence_gap."
                        ),
                    },
                    "metadata": {
                        "tool_name": "assign_multi_subjective_tasks_to_info_seeker",
                        "duplicate_batch_blocked": True,
                    },
                }

            pending_tasks: List[Dict[str, str]] = []
            previously_completed: List[Dict[str, Any]] = []
            missing_gap: List[Dict[str, Any]] = []
            for task in tasks:
                fingerprint = self._information_task_fingerprint(task)
                task_text = self._information_task_text(task, include_gap=False)
                evidence_gap = str(task.get("evidence_gap") or "").strip()
                exact_match = fingerprint in self._successful_information_task_fingerprints
                semantic_match = any(
                    self._information_text_similarity(task_text, completed_text) >= 0.82
                    for completed_text in self._successful_information_task_texts
                )
                if exact_match or (semantic_match and not evidence_gap):
                    previously_completed.append({
                        "task_content": task.get("task_content", ""),
                        "reason": "previously_completed",
                    })
                    continue
                if retry_only:
                    pending_tasks.append(task)
                    continue
                if self._information_seeker_completed and not evidence_gap:
                    missing_gap.append({
                        "task_content": task.get("task_content", ""),
                        "reason": "supplemental_search_requires_evidence_gap",
                    })
                    continue
                pending_tasks.append(task)

            if missing_gap:
                self.logger.warning(
                    "Blocked %s supplemental literature task(s) without an explicit evidence gap",
                    len(missing_gap),
                )
                return {
                    "success": False,
                    "error": (
                        "The initial literature stage has already completed. Supplemental search is allowed only "
                        "for a concrete missing claim, missing source type, date range, or unresolved citation. "
                        "Add a non-empty evidence_gap to each genuinely new task; do not repeat the broad search."
                    ),
                    "data": {
                        "rejected_tasks": missing_gap,
                        "already_completed_tasks": previously_completed,
                    },
                    "metadata": {"duplicate_tasks_removed": len(within_batch_duplicates)},
                }

            if not pending_tasks:
                self.logger.warning("Blocked repeated InformationSeeker tasks: no new evidence work remains")
                self._publish_agent_progress(
                    "information_search_ready",
                    "已跳过重复文献检索",
                    summary="没有发现新的证据缺口，不再重复运行已经完成的检索任务。",
                    skipped_count=len(previously_completed) + len(within_batch_duplicates),
                )
                return {
                    "success": True,
                    "data": {
                        "tasks": [],
                        "completion_status": "already_completed",
                        "skipped_count": len(previously_completed) + len(within_batch_duplicates),
                        "next_action": "Proceed to ExperimentAgent or WriterAgent; do not repeat broad literature search.",
                    },
                    "metadata": {
                        "tool_name": "assign_multi_subjective_tasks_to_info_seeker",
                        "duplicate_tasks_removed": len(within_batch_duplicates),
                    },
                }

            tasks = pending_tasks

            # 2. 瀵煎叆蹇呰鐨?Agent 鍒涘缓鍑芥暟
            try:
                from agents import TaskInput, create_subjective_information_seeker
            except ImportError:
                from ..agents import TaskInput, create_subjective_information_seeker

            results = []
            total_tasks = len(tasks)
            self.logger.info("开始顺序处理 %s 个调研子任务（并发限制为 1）", total_tasks)
            self._publish_agent_progress("information_search_started", "InformationSeeker 开始执行调研任务", total_tasks=total_tasks)

            # 3. 椤哄簭閬嶅巻浠诲姟(涓嶅啀浣跨敤绾跨▼姹犲苟鍙?褰诲簳瑙ｅ喅 Iteration 娣蜂贡)
            for index, task in enumerate(tasks):
                if self._check_cancellation():
                    self.logger.info("Planner detected cancellation before starting next information-seeker subtask")
                    results.append({
                        "task_content": task.get("task_content", "Unknown"),
                        "success": False,
                        "error": "Task cancelled by user"
                    })
                    break

                task_num = index + 1
                current_content = task.get("task_content", "Unknown")

                self.logger.info("==== [子任务 %s/%s] 启动 ====", task_num, total_tasks)
                self.logger.info("内容概要: %s...", current_content[:50])
                self._publish_agent_progress(
                    "information_search_task", f"InformationSeeker 子任务 {task_num}/{total_tasks}",
                    task_num=task_num, total_tasks=total_tasks, summary=current_content[:160]
                )

                try:
                    # 鍒涘缓浠诲姟杈撳叆瀵硅薄
                    task_input = TaskInput(
                        task_content=current_content,
                        task_steps_for_reference=task.get("task_steps_for_reference"),
                        deliverable_contents=task.get("deliverable_contents"),
                        current_task_status=task.get("current_task_status"),
                        # workspace_id=self.get_session_info().get("session_id", "default"),
                        workspace_id=self.task_id if self.task_id else "default_session",
                        acceptance_checking_criteria=task.get("acceptance_checking_criteria")
                    )

                    # 鑾峰彇瀛愭櫤鑳戒綋閰嶇疆
                    info_seeker_config = getattr(self, 'sub_agent_configs', {}).get('information_seeker', {})

                    # 涓烘瘡涓换鍔″垱寤哄叏鏂扮殑 Agent 瀹炰緥,纭繚鐘舵€侀殧绂?
                    # 璋冧綆杩唬娆℃暟(max_iterations),闃叉鍗曚釜浠诲姟闄峰叆姝诲惊鐜?
                    info_seeker = create_subjective_information_seeker(
                        model=info_seeker_config.get('model', self.config.model),
                        max_iterations=_resolve_info_seeker_max_iterations(
                            int(info_seeker_config.get("max_iterations", DEFAULT_INFO_SEEKER_MAX_ITERATIONS))
                        ),
                        shared_mcp_client=self.mcp_tools.client if hasattr(self.mcp_tools, 'client') else self.mcp_tools
                    )
                    info_seeker.task_id = self.task_id

                    # 鎵ц浠诲姟(闃诲寮忕瓑寰?鐩村埌璇ヤ换鍔″畬鎴愭垨澶辫触)
                    if hasattr(self, "_cancellation_token") and self._cancellation_token:
                        info_seeker.set_cancellation_token(self._cancellation_token)
                    response = info_seeker.execute_task(task_input)

                    if response.success:
                        self.logger.info("子任务 %s 完成，消耗迭代: %s", task_num, response.iterations)
                        fingerprint = self._information_task_fingerprint(task)
                        completed_text = self._information_task_text(task, include_gap=False)
                        self._successful_information_task_fingerprints.add(fingerprint)
                        self._failed_information_tasks.pop(fingerprint, None)
                        if completed_text not in self._successful_information_task_texts:
                            self._successful_information_task_texts.append(completed_text)
                        results.append({
                            "task_content": current_content,
                            "success": True,
                            "data": response.result,
                            "iterations": response.iterations
                        })
                    else:
                        self.logger.error("子任务 %s 失败: %s", task_num, response.error)
                        fingerprint = self._information_task_fingerprint(task)
                        previous = self._failed_information_tasks.get(fingerprint, {})
                        self._failed_information_tasks[fingerprint] = {
                            "task": dict(task),
                            "last_error": str(response.error or "unknown error"),
                            "retry_attempts": int(previous.get("retry_attempts", 0)) + (1 if retry_only else 0),
                        }
                        results.append({
                            "task_content": current_content,
                            "success": False,
                            "error": response.error
                        })

                    if getattr(self, "task_id", None):
                        try:
                            from src.utils.task_manager import task_manager
                            requested_stage = task_manager.peek_requested_stage(self.task_id)
                        except Exception:
                            requested_stage = None
                    if requested_stage and requested_stage.get("stage") != "information_search":
                        self.logger.info(
                            "InformationSeeker 子任务 %s 结束后执行用户流程切换：%s；剩余 %s 个子任务不再启动",
                            task_num, requested_stage.get("stage"), total_tasks - task_num,
                        )
                        break

                except Exception as e:
                    self.logger.error("子任务 %s 运行崩溃: %s", task_num, str(e))
                    fingerprint = self._information_task_fingerprint(task)
                    previous = self._failed_information_tasks.get(fingerprint, {})
                    self._failed_information_tasks[fingerprint] = {
                        "task": dict(task),
                        "last_error": f"Execution Crash: {str(e)}",
                        "retry_attempts": int(previous.get("retry_attempts", 0)) + (1 if retry_only else 0),
                    }
                    results.append({
                        "task_content": current_content,
                        "success": False,
                        "error": f"Execution Crash: {str(e)}"
                    })

                # 鍦ㄤ换鍔′箣闂寸◢寰仠椤?璁╃郴缁熸湁鏃堕棿澶勭悊鏂囦欢 IO
                delay = max(0.0, float(os.getenv("INFO_SEEKER_TASK_DELAY_SECONDS", "0")))
                if delay:
                    time.sleep(delay)

            # 4. 姹囨€绘墍鏈夊瓙浠诲姟鐘舵€?
            success_count = sum(1 for r in results if r.get("success"))
            user_stage_transition = bool(
                requested_stage and requested_stage.get("stage") != "information_search"
            )
            all_success = (success_count == total_tasks) or user_stage_transition
            if success_count > 0:
                self._information_seeker_completed = True
            if all_success:
                self._successful_information_batch_fingerprints.add(incoming_batch_fingerprint)

            self.logger.info("所有调研子任务处理完毕，成功: %s/%s", success_count, total_tasks)
            self._publish_agent_progress(
                "information_search_ready", "InformationSeeker 调研阶段完成",
                success_count=success_count, total_tasks=total_tasks
            )
            if os.getenv("SCIA_PIPELINE_VERSION", "hybrid").strip().lower() in {"hybrid", "autonomous", "research"}:
                try:
                    from src.pipeline_v2.hybrid import refresh_hybrid_evidence
                    workspace = getattr(self.mcp_tools, "workspace_path", None) or os.getenv("AGENT_WORKSPACE_PATH")
                    if not workspace:
                        raise RuntimeError("active workspace path is unavailable")
                    refreshed = refresh_hybrid_evidence(
                        Path(workspace),
                        "\n".join(str(task.get("task_content", "")) for task in tasks),
                    )
                    self._publish_agent_progress(
                        "evidence_matrix_refreshed", "自主检索结果已重新写入结构化文献库和证据矩阵",
                        summary=f"references={refreshed['reference_count']}, claims={refreshed['claim_count']}, visuals={refreshed.get('visual_asset_count', 0)}",
                    )
                except Exception as refresh_error:
                    self.logger.warning("Could not refresh Hybrid evidence after InformationSeeker: %s", refresh_error)

            return {
                "success": all_success,
                "data": {"tasks": results},
                "error": None if all_success else f"Only {success_count}/{total_tasks} tasks succeeded",
                "metadata": {
                    "tool_name": "assign_multi_subjective_tasks_to_info_seeker",
                    "task_count": total_tasks,
                    "success_count": success_count,
                    "duplicate_tasks_removed": len(within_batch_duplicates),
                    "previously_completed_tasks_skipped": len(previously_completed),
                    "requested_stage": requested_stage.get("stage") if requested_stage else None,
                    "stopped_by_user_guidance": user_stage_transition,
                    "retry_only_failed_tasks": retry_only,
                    "remaining_failed_task_count": len(self._failed_information_tasks),
                }
            }

        except Exception as e:
            self.logger.error(f"Critical error in multi-task coordinator: {e}")
            return {
                "success": False,
                "error": f"Coordinator internal error: {str(e)}"
            }
    ##
    def assign_subjective_task_to_writer(
            self,
            task_content: str,
            user_query: str,
            key_files: list = None,
            **kwargs
    ) -> dict:
        """Assign a writing or content creation task to the WriterAgent"""
        try:
            if getattr(self, "_writer_agent_completed", False):
                self.logger.info("WriterAgent has already completed; suppressing duplicate Writer delegation")
                return {
                    "success": True,
                    "data": {
                        "final_article_path": "./report/final_report.md",
                        "completion_status": "already_completed",
                        "task_summary": "WriterAgent already completed the autonomous draft. Call planner_subjective_task_done now.",
                    },
                    "agent_name": "WriterAgent",
                    "iterations": 0,
                    "execution_time": 0,
                }
            if os.getenv("SCIA_PIPELINE_VERSION", "").strip().lower() == "hybrid":
                try:
                    hybrid_state = self._sync_hybrid_completion_from_artifacts(
                        verify_literature=True, query=user_query or task_content,
                    )
                    reference_gate = hybrid_state.get("reference_gate") or {}
                except Exception as gate_error:
                    self.logger.warning("Could not verify Hybrid evidence before WriterAgent: %s", gate_error)
                    return {
                        "success": False,
                        "error": f"Cannot verify the Hybrid evidence gates before WriterAgent: {gate_error}",
                    }
                missing_agents = []
                if not getattr(self, "_information_seeker_completed", False):
                    missing_agents.append("InformationSeeker")
                if not getattr(self, "_experiment_agent_completed", False):
                    missing_agents.append("ExperimentAgent")
                if missing_agents:
                    missing_text = ", ".join(missing_agents)
                    self.logger.warning("Writer delegation blocked; required old-flow agents not completed: %s", missing_text)
                    return {
                        "success": False,
                        "error": (
                            f"Required old autonomous workflow is incomplete: {missing_text}. "
                            "Complete the missing evidence gate(s) before calling WriterAgent. "
                            "A hash-verified experiment registry and a satisfied literature floor count as completed work."
                        ),
                    }
                if not reference_gate.get("reference_gate_met", False):
                    current = reference_gate.get("reference_count", 0)
                    minimum = reference_gate.get("minimum_reference_count", 30)
                    self.logger.warning(
                        "Writer delegation blocked; valid literature floor not met: %s/%s", current, minimum
                    )
                    self._publish_agent_progress(
                        "literature_floor_not_met", "有效文献数量不足，继续补充检索",
                        reference_count=current, minimum_reference_count=minimum,
                        summary=f"当前有效去重文献 {current}/{minimum} 篇，尚不能进入论文写作。",
                    )
                    return {
                        "success": False,
                        "error": (
                            f"Literature quality gate not met: {current}/{minimum} valid deduplicated references. "
                            "Continue InformationSeeker with a concrete evidence_gap, uncovered claim, alternative keyword, "
                            "date range, or missing source type before calling WriterAgent again."
                        ),
                    }
            if key_files is None:
                key_files = []
            elif not isinstance(key_files, list):
                key_files = [key_files]
            # Inject common workspace evidence files for the writer.
            try:
                from pathlib import Path
                base_dir = self.mcp_tools.workspace_path if hasattr(self.mcp_tools, 'workspace_path') else None
                if base_dir:
                    # 1. Inject experiment result files.
                    exp_dir = Path(base_dir) / "experiment_results"
                    if exp_dir.exists():
                        for ext in ('*.png', '*.jpg', '*.csv', '*.md'):
                            for file_path in exp_dir.glob(ext):
                                rel_path = f"./experiment_results/{file_path.name}"
                                if not any(isinstance(f, dict) and f.get('file_path') == rel_path for f in key_files):
                                    key_files.append({"file_path": rel_path, "desc": "Experiment result file; use as authoritative task evidence."})
                                    self.logger.info(f"Auto-injected experiment file for Writer: {rel_path}")
                    # 2. Inject InfoSeeker search results.
                    research_dir = Path(base_dir) / "url_crawler_save_files" / "research"
                    if research_dir.exists():
                        for md_file in research_dir.glob("*.md"):
                            rel_path = f"./url_crawler_save_files/research/{md_file.name}"
                            if not any(isinstance(f, dict) and f.get('file_path') == rel_path for f in key_files):
                                key_files.append({"file_path": rel_path, "desc": "InfoSeeker research result; use as literature/background evidence."})
                                self.logger.info(f"Auto-injected research file for Writer: {rel_path}")
                    # 3. Inject V2 evidence contracts and structured references.
                    v2_research_files = {
                        "research/research_contract.json": "ResearchContract; hard boundary for scope and claims.",
                        "research/claims_evidence.json": "Claims-Evidence Matrix; every substantive claim must map here.",
                        "research/paper_outline.json": "Evidence-aware paper outline.",
                        "research/references.json": "Verified structured reference registry; use its indices and metadata.",
                        "research/references.md": "Stable numbered citation catalogue; every [N] must match this file.",
                        "research/visual_assets_registry.json": "Binding whole-paper figure/table plan with section, purpose, evidence and exact paths.",
                        "research/visual_assets_guide.md": "Human-readable instructions for inserting registered diagrams, figures and tables.",
                    }
                    for relative, description in v2_research_files.items():
                        candidate = Path(base_dir) / relative
                        rel_path = f"./{relative}"
                        if candidate.is_file() and not any(isinstance(f, dict) and f.get('file_path') == rel_path for f in key_files):
                            key_files.append({"file_path": rel_path, "desc": description})
                            self.logger.info("Auto-injected V2 evidence file for Writer: %s", rel_path)
                    tables_root = Path(base_dir) / "experiment_results" / "tables"
                    visual_table_roots = [tables_root, Path(base_dir) / "research" / "visual_assets"]
                    for visual_table_root in visual_table_roots:
                        if not visual_table_root.is_dir():
                            continue
                        for table_path in sorted(visual_table_root.glob("*.md")):
                            relative = table_path.relative_to(Path(base_dir)).as_posix()
                            rel_path = f"./{relative}"
                            if not any(isinstance(f, dict) and f.get('file_path') == rel_path for f in key_files):
                                key_files.append({
                                    "file_path": rel_path,
                                    "desc": "Deterministically generated experiment table; copy exact values into the assigned paper section.",
                                })
                    task_content += (
                        "\n\n[Hybrid V2 evidence rules]\n"
                        "Read ResearchContract, Claims-Evidence Matrix, experiment registry, figure plans, and references.json before drafting. "
                        "Use external literature only for background/positioning and use uploaded experiment evidence for method/results. "
                        "Do not emit internal [S#] source IDs as bibliography citations. Do not create a citation unless its metadata exists in references.json. "
                        "If evidence is missing, state the limitation instead of filling it with domain assumptions. "
                        "Follow visual_assets_registry.json: diagrams are for mechanisms, tables for exact numbers, and plots for trends. "
                        "Use only registered assets in their assigned sections and preserve exact paths and table values.\n"
                    )
                    # 4. Inject paper structure log.
                    ps_log = Path(base_dir) / "research" / "paper_structures" / "paper_structure_log.md"
                    if ps_log.exists():
                        rel_path = "./research/paper_structures/paper_structure_log.md"
                        if not any(isinstance(f, dict) and f.get('file_path') == rel_path for f in key_files):
                            key_files.append({"file_path": rel_path, "desc": "Observed paper structure patterns; use for chapter organization and figure/table planning."})
                            self.logger.info("Auto-injected paper structure log for Writer")
            except Exception as exp_err:
                self.logger.warning(f"Workspace evidence auto-injection failed: {exp_err}")
            # =================================================================
# ==============================================================

            if not task_content and "content" in kwargs:
                task_content = kwargs.get("content", "")
            if not user_query and "query" in kwargs:
                user_query = kwargs.get("query", "")

            # 鈹€鈹€ 寮哄埗鏂囦綋绾犳:缁熶竴鏀逛负閫氱敤鐨勫鏈鏂囧伐浣滄祦鎸囧崡 鈹€鈹€
            paper_instruction = (
                "銆愬鏈鏂囨挵鍐欐爣鍑嗗伐浣滄祦鎸囧崡銆?\n"
                "璇峰皢涓嬫柟浠诲姟鐞嗚В涓烘挵鍐欎竴绡囨爣鍑嗙殑瀛︽湳鏈熷垔璁烘枃,骞朵弗鏍奸伒寰互涓嬫帓鐗堜笌璇皟瑙勮寖:\n"
                "1. 馃搼 绔犺妭缂栧彿瑙勮寖:澶х翰蹇呴』閲囩敤瀛︽湳鏈熷垔鏍囧噯鐨勯樋鎷変集鏁板瓧閫掕繘缂栧彿(濡?`1 寮曡█`,`2 鏉愭枡涓庢柟娉昤,`2.1 瀹為獙鏉愭枡`).馃毃銆愯嚧鍛借姹傘€?**缁濆绂佹浣跨敤鈥滅涓€绔犫€濄€佲€滅浜岀珷鈥濊繖绉嶄綋渚?**\n"
                "   - 鏈€寮€澶村繀椤绘槸璁烘枃鐨勭湡瀹為鐩?浠庣敤鎴疯姹?鐢ㄦ埛鐨勨€滈鐩€濅腑鎻愬彇骞跺～鍏?濡?`# 鍩轰簬XXX鐨刋XX鏂规硶`)鍜?`## 鎽樿`(涓嶅甫浠讳綍鏁板瓧缂栧彿).缁濆涓嶈鎶婂瓧闈⑩€滆鏂囨爣棰樷€濆洓涓瓧褰撴垚鏍囬.\n"
                "   - 姝ｆ枃绗竴閮ㄥ垎蹇呴』浠?`# 1 寮曡█` 寮€濮?\n"
                "   - 澶х翰鐨勬渶鍚庝竴閮ㄥ垎蹇呴』鏄?`# 鍙傝€冩枃鐚甡(涓嶅甫鏁板瓧缂栧彿)!\n"
                "2. 馃棧锔?瀛︽湳璇皟瑕佹眰:璇█椋庢牸蹇呴』鏋佸叾瀹㈣銆佷弗璋ㄣ€佺簿鐐?**缁濆绂佹**鍑虹幇鈥滄湰绔犳棬鍦ㄦ繁鍏ユ帰璁ㄢ€濄€佲€滄渶寮曚汉鍏虫敞鐨勬槸鈥濄€佲€滀护浜烘儕璁剁殑鏄€濄€佲€滆鎴戜滑鏉ョ湅鈥濈瓑鍙ｈ鍖栥€佹眹鎶ュ紡鎴栧甫鏈変富瑙傝壊褰╃殑杩囨浮搴熻瘽!鐩存帴闄堣堪浜嬪疄銆佹柟娉曞拰鏁版嵁.\n"
                "3. 馃敩 涓ョ璇诲彇浜岃繘鍒舵暟鎹?**缁濆涓嶈**浣跨敤 file_read 鍘昏鍙?`.xlsx` 鎴?`.csv` 鍘熷鏂囦欢!浣犻渶瑕佺殑鍏ㄩ儴鐪熷疄瀹為獙缁撹銆佷换鍔＄浉鍏虫寚鏍囥€佺粺璁＄粨鏋滄垨棰嗗煙娴嬮噺鍊?宸茬粡涓轰綘鎬荤粨鍦ㄤ簡 `experiment_results.md` 鏂囦欢涓?鍘昏杩欎釜 .md 鏂囦欢鍗冲彲!\n"
                "4. 馃摎 鍙傝€冩枃鐚帓鐗?鍦?`# 鍙傝€冩枃鐚甡 绔犺妭,蹇呴』鎸?GB/T 7714 瀛︽湳瑙勮寖鐢熸垚绾枃鏈垪琛?銆愮粷瀵圭姝€戣緭鍑轰换浣曠綉椤甸摼鎺?URL)鎴朒TML鏍囩!\n\n"
                "5. 鉁嶏笍 銆愬啓浣滈鏍奸搧寰?- 绂佹缁艰堪鍖栥€?\n"
                "   - 銆愬紩瑷€銆?鑳屾櫙閾哄灚涓ユ牸鎺у埗鍦?娈典互鍐?绗?娈靛繀椤荤洿鎺ュ紩鍑烘湰鐮旂┒鍏蜂綋鐨勫垱鏂扮偣涓庣爺绌剁洰鏍?绂佹娉涙硾浠嬬粛棰嗗煙鐜扮姸瓒呰繃500瀛?\n"
                "   - 銆愯璁恒€?蹇呴』浠ユ湰瀹為獙鐪熷疄鏁版嵁銆佷换鍔＄浉鍏虫寚鏍囥€佺粺璁＄粨鏋滄垨棰嗗煙娴嬮噺鍊间负鏍稿績,缁撴瀯蹇呴』涓?鈶犳湰鐮旂┒缁撴灉鈫掆憽涓庢枃鐚姣斺啋鈶㈠師鍥犲垎鏋?绂佹鍏堢患杩版枃鐚啀鎻愯嚜宸辩粨鏋?\n"
                "   - 銆愮粨璁恒€?姣忔潯缁撹蹇呴』闄勫搴斿叿浣撴暟鍊?绂佹绌烘硾鎬荤粨!\n"
                "   - 鍏ㄦ枃绂佹鍑虹幇鈥滄湰鐮旂┒鎴愬姛寤虹珛鈥濄€佲€濅负...鎻愪緵浜嗗潥瀹炵殑鐞嗚渚濇嵁鈥溿€佲€濆叿鏈夋樉钁楃殑瀛︽湳浠峰€尖€溿€佲€濈患涓婃墍杩扳€滅瓑濂楄瘽!\n\n"
                "浠ヤ笅鏄叿浣撲换鍔″唴瀹?\n"
                "鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€\n"
            )
            # Replace the historical mojibake prompt with one UTF-8 source of
            # truth. Keeping this assignment here also makes old deployments
            # that still contain the legacy text harmless.
            paper_instruction = (
                "【自主科研论文写作要求】\n"
                "1. 根据证据自主安排章节；允许在写作中发现缺口并返回检索或实验阶段。\n"
                "2. 用户上传的实验材料是实验事实的唯一权威来源，不得猜测数据集、硬件、参数或指标。\n"
                "3. 每个实质性结论必须由 Claims-Evidence Matrix、实验记录或真实文献支持；缺失证据时明确写成局限。\n"
                "4. 文内只能使用 research/references.md 中的稳定编号 [N]，引用前核对题名和证据摘要是否直接支持当前句子。\n"
                "5. 参考文献章节由结构化文献库确定性生成，不得自行改序、凑数量或补造元数据。\n"
                "6. 保持学术论文语气，避免综述式堆砌；方法、结果和讨论必须围绕本研究问题与真实实验展开。\n\n"
            )
            task_content = paper_instruction + task_content

            # # 鈹€鈹€ 鑷姩娉ㄥ叆瀹為獙缁撴灉鏂囦欢 (璺ㄥ钩鍙?MCP 瀹夊叏鐗? 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
            # exp_report_rel = "./experiment_results/experiment_results.md"
            # try:
            #     # 馃殌 閫氳繃瀹㈡埛绔伐鍏锋煡璇㈡湇鍔″櫒鐨勫疄楠岀粨鏋滅洰褰?鏉滅粷鏈湴璺緞閿欎綅闂
            #     list_result = self.execute_tool_call({
            #         "name": "list_workspace",
            #         "arguments": {"path": "./experiment_results"}
            #     })
            #
            #     if list_result and list_result.get("success"):
            #         items = list_result.get("data", {}).get("items", [])
            #         has_md = any(item.get("name") == "experiment_results.md" for item in items)
            #
            #         if has_md:
            #             existing_paths = {f.get("file_path") for f in key_files if isinstance(f, dict)}
            #
            #             # 寮鸿鎶婂疄楠屾姤鍛婂缁欏啓鎵?
            #             if exp_report_rel not in existing_paths:
            #                 key_files.append({
            #                     "file_path": exp_report_rel,
            #                     "desc": "銆愭渶楂樹紭鍏堢骇銆戠湡瀹炵殑瀹為獙缁撴灉涓庢寚鏍?(蹇呰)"
            #                 })
            #
            #             # 鏀堕泦鎵€鏈夌敓鎴愮殑鍥剧墖
            #             md_images_str = ""
            #             for item in items:
            #                 name = item.get("name", "")
            #                 if name.lower().endswith((".png", ".jpg", ".jpeg")):
            #                     md_images_str += f"![{name}](../experiment_results/{name})\n"
            #
            #                     # 灏嗗浘鐗囨湰韬篃鍔犺繘 key_files (鏂逛究瑙嗚瑙ｆ瀽宸ュ叿鐪嬪埌)
            #                     img_rel = f"./experiment_results/{name}"
            #                     if img_rel not in existing_paths:
            #                         key_files.append({"file_path": img_rel})
            #
            #             if md_images_str:
            #                 injection = (
            #                     "\n\n銆愮郴缁熷己鍒舵寚浠?- 瀹為獙鏁版嵁涓庢彃鍥惧紩鐢ㄣ€?\n"
            #                     "1. 瀹為獙绔犺妭鐨勬暟鍊煎繀椤荤洿鎺ュ紩鐢ㄧ湡瀹炴暟鎹?缁濆绂佹鑷缂栭€?\n"
            #                     "2. 馃毃銆怣arkdown 璺緞淇涓庡鏈浘娉ㄩ搧寰嬨€?鎻掑叆鍥剧墖鏃?*蹇呴』灏嗚矾寰勯€€鍥炰笂涓€绾?`../`**!\n"
            #                     "   浣犲繀椤诲湪姝ｆ枃瀵瑰簲浣嶇疆鍘熷皝涓嶅姩宓屽叆浠ヤ笅浠ｇ爜,骞朵笖**蹇呴』鍦ㄥ浘鐗囩揣涓嬫柟娣诲姞灞呬腑鐨勫鏈浘娉?*(渚嬪:*鍥?1 XXX棰勬祴鏁ｇ偣鍥?):\n"
            #                     f"{md_images_str}\n\n"
            #                 )
            #                 if injection not in task_content:
            #                     task_content = task_content + injection
            #
            #             self.logger.info("鉁?鎴愬姛閫氳繃 MCP 娉ㄥ叆瀹為獙鎶ュ憡鍜屽浘鐗囧埌 Writer 浠诲姟涓?")
            #         else:
            #             self.logger.warning("鈿狅笍 experiment_results 鐩綍涓嬫湭鍙戠幇 experiment_results.md")
            #     else:
            #         self.logger.warning(
            #             f"鈿狅笍 list_workspace 璋冪敤澶辫触: {list_result.get('error') if list_result else '鏈煡閿欒'}")
            # except Exception as e:
            #     self.logger.warning(f"鈿狅笍 鏃犳硶鑾峰彇瀹為獙缁撴灉: {e}")
            # # 鈹€鈹€ 娉ㄥ叆缁撴潫 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
            # 鈹€鈹€ 鑷姩娉ㄥ叆瀹為獙缁撴灉鏂囦欢 (璺ㄥ钩鍙?MCP 瀹夊叏鐗? 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
            exp_report_rel = "experiment_results/experiment_results.md"
            try:
                # 馃殌 鑷村懡淇:鍘绘帀浜嗚矾寰勫墠闈㈢殑 "./",闃叉琚?MCP Server 鐨勫畨鍏ㄦ満鍒舵嫤鎴?
                list_result = self.execute_tool_call({
                    "name": "list_workspace",
                    "arguments": {"path": "experiment_results"}
                })

                if list_result and list_result.get("success"):
                    items = list_result.get("data", {}).get("items", [])
                    has_md = any(item.get("name") == "experiment_results.md" for item in items)

                    existing_paths = {f.get("file_path", "") for f in key_files if isinstance(f, dict)}

                    if has_md and exp_report_rel not in existing_paths and f"./{exp_report_rel}" not in existing_paths:
                        key_files.append({
                            "file_path": exp_report_rel,
                            "desc": "銆愭渶楂樹紭鍏堢骇銆戠湡瀹炵殑瀹為獙缁撴灉涓庢寚鏍?(蹇呰)"
                        })

                    # 鏀堕泦鎵€鏈夌敓鎴愮殑鍥剧墖
                    md_images_str = ""
                    img_count = 0
                    for item in items:
                        name = item.get("name", "")
                        if name.lower().endswith((".png", ".jpg", ".jpeg")):
                            img_count += 1
                            md_images_str += f"![{name}](../experiment_results/{name})\n"

                            # 灏嗗浘鐗囨湰韬篃鍔犺繘 key_files
                            img_rel = f"experiment_results/{name}"
                            if img_rel not in existing_paths and f"./{img_rel}" not in existing_paths:
                                key_files.append({"file_path": img_rel})

                    if md_images_str:
                        injection = (
                            "\n\n[System instruction - experiment data and figure citation]\n"
                            "1. Use only real experimental values from provided files; do not invent metrics.\n"
                            "2. When inserting figures, preserve the following Markdown image paths exactly:\n"
                            f"{md_images_str}\n\n"
                        )
                        if "[System instruction - experiment data and figure citation]" not in task_content:
                            task_content = task_content + injection

                    self.logger.info("已通过 MCP 向 Writer 任务注入实验报告和 %s 张图片", img_count)
                else:
                    self.logger.warning(
                        f"鈿狅笍 list_workspace 璋冪敤澶辫触: {list_result.get('error') if list_result else '鏈煡閿欒'}")
            except Exception as e:
                self.logger.warning("无法获取实验结果: %s", e)
            # 鈹€鈹€ 娉ㄥ叆缁撴潫 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
            self.logger.info("Assigning task to WriterAgent")
            from .base_agent import WriterAgentTaskInput
            from .writer_agent import create_writer_agent

            task_input = WriterAgentTaskInput(
                task_content=task_content,
                user_query=user_query,
                key_files=key_files,
                workspace_id=self.get_session_info()["session_id"],
            )

            writer_config = getattr(self, 'sub_agent_configs', {}).get('writer', {})
            writer = create_writer_agent(
                shared_mcp_client=self.mcp_tools.client,
                model=writer_config.get('model', self.config.model),
                max_iterations=writer_config.get('max_iterations', 50),
                temperature=writer_config.get('temperature', 0.3),
                max_tokens=writer_config.get('max_tokens', 16384),
                task_id=self.task_id,
            )

            self.logger.info(f"Assigning task to WriterAgent: {str(task_content)[:800]}...")
            self._publish_agent_progress("writer_started", "WriterAgent 开始撰写自主初稿")
            response = writer.execute_task(task_input)

            if response.success:
                # Record Writer completion before the Hybrid early return that
                # skips the legacy internal reviewers. The V2 four-reviewer
                # stage runs after Planner completes, so this flag must already
                # be visible to planner_subjective_task_done.
                self._writer_agent_completed = True
                self._publish_agent_progress(
                    "writer_ready", "WriterAgent 已完成自主初稿",
                    iterations=response.iterations
                )
                if os.getenv("SKIP_LEGACY_INTERNAL_REVIEW", "false").strip().lower() in {"1", "true", "yes", "on"}:
                    return {
                        "success": True,
                        "data": response.result,
                        "agent_name": response.agent_name,
                        "iterations": response.iterations,
                        "execution_time": response.execution_time,
                    }
                # =================================================================
                # Multi-Perspective Peer Review: 3 parallel reviewers
                # =================================================================
                self.logger.info("Phase 2 (Writing) Completed. Starting Phase 3: Multi-Perspective Peer Review...")
                try:
                    from .reviewer_agent import create_reviewer_agent, REVIEWER_ROLES
                    from .base_agent import TaskInput as ReviewTaskInput
                    from concurrent.futures import ThreadPoolExecutor, as_completed
                    import threading

                    final_path = "./report/final_report.md"
                    if isinstance(response.result, dict) and response.result.get("final_article_path"):
                        final_path = response.result.get("final_article_path")

                    # Read the paper content to inject into reviewer context (prevents hallucination)
                    paper_content = ""
                    try:
                        read_result = self.execute_tool_call({"name": "file_read", "arguments": {"file_path": final_path}})
                        if read_result and read_result.get("success"):
                            paper_content = read_result.get("data", {}).get("content", read_result.get("content", ""))
                            paper_content = str(paper_content)[:12000]  # First ~12K chars: abstract + intro + methods
                            self.logger.info(f"Injected {len(paper_content)} chars of paper content into reviewer context")
                        else:
                            self.logger.warning(f"Could not read paper: {read_result}")
                    except Exception as read_err:
                        self.logger.warning(f"Failed to read paper for reviewer: {read_err}")

                    authoritative_user_notes = ""
                    try:
                        uploads_result = self.execute_tool_call({"name": "list_workspace", "arguments": {"path": "./user_uploads", "recursive": False}})
                        items = []
                        if uploads_result and uploads_result.get("success"):
                            items = uploads_result.get("data", {}).get("items", [])
                        note_parts = []
                        for item in items:
                            name = item.get("name", "")
                            if str(name).lower().endswith((".txt", ".md")):
                                note_result = self.execute_tool_call({"name": "file_read", "arguments": {"file_path": f"./user_uploads/{name}"}})
                                if note_result and note_result.get("success"):
                                    note_text = note_result.get("data", {}).get("content", note_result.get("content", ""))
                                    note_parts.append(f"### {name}\n{str(note_text)[:8000]}")
                        authoritative_user_notes = "\n\n".join(note_parts)[:12000]
                        if authoritative_user_notes:
                            self.logger.info(f"Injected {len(authoritative_user_notes)} chars of user notes into reviewer context")
                    except Exception as note_err:
                        self.logger.warning(f"Failed to read user notes for reviewer: {note_err}")

                    reviewer_configs = [
                        {"role": "methodology", "name_cn": REVIEWER_ROLES["methodology"]["name_cn"]},
                        {"role": "domain", "name_cn": REVIEWER_ROLES["domain"]["name_cn"]},
                        {"role": "devils_advocate", "name_cn": REVIEWER_ROLES["devils_advocate"]["name_cn"]},
                    ]

                    review_results = []
                    review_lock = threading.Lock()

                    def _run_one_reviewer(cfg, paper_text=None):
                        role = cfg["role"]
                        name_cn = cfg["name_cn"]
                        self.logger.info(f"  [Reviewer] {name_cn} ({role}) start")
                        try:
                            rev = create_reviewer_agent(
                                shared_mcp_client=self.mcp_tools.client,
                                model=self.config.model,
                                max_iterations=int(os.getenv("REVIEWER_MAX_ITERATIONS", "12")),
                                reviewer_role=role,
                            )
                            ptext = paper_text if paper_text is not None else paper_content
                            paper_snippet = ptext[:14000] if ptext else f"(paper at {final_path})"
                            rev_task = ReviewTaskInput(
                                task_content=(
                                    f"You are reviewing the following paper as {name_cn}.\n\n"
                                    f"=== AUTHORITATIVE USER NOTES ===\n{authoritative_user_notes or '(not provided)'}\n=== END USER NOTES ===\n\n"
                                    f"=== PAPER CONTENT ===\n{paper_snippet}\n=== END PAPER ===\n\n"
                                    "The user notes are authoritative for model names, dataset facts, hardware, hyperparameters, and metrics. "
                                    "If the paper conflicts with the user notes, criticize the paper for deviating from the user notes. "
                                    "Do not claim a user-provided model name is nonexistent merely because it is unfamiliar. "
                                    "Focus on your expertise area. Provide a critical peer review in Chinese. Call reviewer_task_done when finished."
                                ),
                                workspace_id=self.get_session_info()["session_id"]
                            )
                            rev_resp = rev.execute_task(rev_task)
                            if rev_resp.success and isinstance(rev_resp.result, dict):
                                decision = rev_resp.result.get("decision", "N/A")
                                self.logger.info(f"  [Reviewer] {name_cn} ({role}) done. Decision: {decision}")
                                with review_lock:
                                    review_results.append({
                                        "role": role, "name_cn": name_cn,
                                        "success": True, "result": rev_resp.result,
                                    })
                            else:
                                self.logger.warning(f"  [Reviewer] {name_cn} ({role}) failed: {rev_resp.error}")
                                with review_lock:
                                    review_results.append({
                                        "role": role, "name_cn": name_cn,
                                        "success": False, "error": str(rev_resp.error),
                                    })
                        except Exception as inner_err:
                            self.logger.error(f"  [Reviewer] {name_cn} ({role}) crashed: {inner_err}")
                            with review_lock:
                                review_results.append({
                                    "role": role, "name_cn": name_cn,
                                    "success": False, "error": str(inner_err),
                                })

                    with ThreadPoolExecutor(max_workers=3) as executor:
                        futures = [executor.submit(_run_one_reviewer, cfg, paper_content) for cfg in reviewer_configs]
                        for future in as_completed(futures):
                            future.result()

                    success_count = sum(1 for r in review_results if r.get("success"))
                    self.logger.info(f"Multi-Perspective Peer Review done. {success_count}/{len(reviewer_configs)} success")
                    # ====== AUTO REVIEW LOOP: revise and re-review ======
                    MAX_REVIEW_ROUNDS = int(os.getenv("MAX_REVIEW_ROUNDS", "2"))
                    PASS_THRESHOLD = float(os.getenv("REVIEW_PASS_THRESHOLD", "6"))
                    review_round = 1
                    all_review_history = []  # Store all rounds of reviews

                    def _extract_overall_score(result_obj):
                        """Robustly extract Overall score from reviewer result."""
                        if not isinstance(result_obj, dict):
                            return 0.0
                        scores = result_obj.get("scores", {})
                        if not isinstance(scores, dict):
                            return 0.0
                        for key in ("Overall", "overall", "Overall Score", "overall_score", "鎬诲垎", "鎬讳綋璇勫垎"):
                            if key in scores:
                                raw = scores.get(key)
                                try:
                                    return float(str(raw).replace("/10", "").strip())
                                except Exception:
                                    return 0.0
                        return 0.0

                    while review_round <= MAX_REVIEW_ROUNDS:
                        # Check if current round passes
                        all_scores = []
                        for r in review_results:
                            if r.get("success"):
                                overall = _extract_overall_score(r.get("result", {}))
                                all_scores.append(overall)
                        
                        min_score = min(all_scores) if all_scores else 0
                        self.logger.info(f"[Review Loop] Round {review_round}/{MAX_REVIEW_ROUNDS} - Min Overall Score: {min_score}")

                        if not all_scores:
                            self.logger.warning("[Review Loop] No successful reviewer scores. Skipping rewrite loop.")
                            break
                        
                        # Save this round to history
                        all_review_history.append({
                            "round": review_round,
                            "scores": {r.get("role"): _extract_overall_score(r.get("result", {})) for r in review_results if r.get("success")},
                            "decisions": [r.get("result", {}).get("decision", "N/A") for r in review_results if r.get("success")],
                        })
                        
                        # STOP CONDITION: all reviewers score strictly above PASS_THRESHOLD.
                        # User requirement: scores <= 6 must trigger rewrite.
                        if all_scores and min(all_scores) > PASS_THRESHOLD:
                            self.logger.info(f"[Review Loop] PASSED at round {review_round}! All scores > {PASS_THRESHOLD}")
                            break
                        
                        if review_round >= MAX_REVIEW_ROUNDS:
                            self.logger.info(f"[Review Loop] Max rounds ({MAX_REVIEW_ROUNDS}) reached. Stopping.")
                            break
                        
                        # COMPILE FEEDBACK: extract weaknesses from all reviewer reports
                        feedback_parts = []
                        for r in review_results:
                            if r.get("success"):
                                role = r.get("role", "unknown")
                                result = r.get("result", {})
                                weaknesses = result.get("weaknesses", [])
                                key_critique = result.get("key_critique", "")
                                overall = _extract_overall_score(result)
                                feedback_parts.append(f"## {role} (Score: {overall}/10)")
                                if isinstance(weaknesses, list):
                                    for w in weaknesses:
                                        feedback_parts.append(f"- {w}")
                                if key_critique:
                                    feedback_parts.append(f"**Key Critique**: {key_critique}")
                        
                        feedback_text = "\n\n".join(feedback_parts)
                        self.logger.info(f"[Review Loop] Round {review_round}: Compiled {len(feedback_parts)} feedback items for revision")

                        if not feedback_text.strip():
                            self.logger.warning("[Review Loop] Reviewer feedback is empty. Skipping rewrite loop.")
                            break

                        # RE-INVOKE WRITER with review feedback
                        self.logger.info(f"[Review Loop] Round {review_round}: Invoking WriterAgent for revision...")
                        revision_task_content = (
                            f"REVISION ROUND {review_round}: This is a peer-review revision task, not a duplicate chapter-writing task. "
                            f"The WriterAgent must revise the existing final paper and overwrite {final_path}. "
                            f"This instruction explicitly overrides any 'NEVER REWRITE' rule because review-driven revision is required.\n\n"
                            f"=== AUTHORITATIVE USER NOTES ===\n{authoritative_user_notes or '(not provided)'}\n=== END USER NOTES ===\n\n"
                            f"=== PEER REVIEW FEEDBACK ===\n{feedback_text}\n=== END FEEDBACK ===\n\n"
                            f"Instructions: Read the current paper at {final_path}, address the weaknesses and critiques above, "
                            f"and write the revised paper. Keep all strengths. Focus on fixing the identified problems. "
                            f"Reviewer feedback is advisory and may be wrong; the authoritative user notes override reviewer claims. "
                            f"Do not change user-provided model names, dataset facts, hardware, hyperparameters, or metrics unless the user notes require it. "
                            f"Update figures and data only from verified user files. Save the revised paper to {final_path}."
                        )
                        revision_task = WriterAgentTaskInput(
                            task_content=revision_task_content,
                            user_query=user_query,
                            key_files=key_files,
                            workspace_id=self.get_session_info().get("session_id", "")
                        )
                        
                        try:
                            # Re-read paper to get latest content
                            revision_resp = writer.execute_task(revision_task)
                            if not revision_resp.success:
                                self.logger.warning(f"[Review Loop] Writer revision failed: {revision_resp.error}")
                                break
                            
                            # Re-read updated paper for re-review
                            self.logger.info(f"[Review Loop] Revision done. Re-reading paper for re-review...")
                            read_result2 = self.execute_tool_call({"name": "file_read", "arguments": {"file_path": final_path}})
                            if read_result2 and read_result2.get("success"):
                                paper_content = str(read_result2.get("data", {}).get("content", read_result2.get("content", "")))[:14000]
                            
                            # RE-RUN REVIEWERS
                            self.logger.info(f"[Review Loop] Round {review_round}: Re-running reviewers...")
                            review_results = []
                            with ThreadPoolExecutor(max_workers=3) as executor2:
                                futures2 = [executor2.submit(_run_one_reviewer, cfg, paper_content) for cfg in reviewer_configs]
                                for future in as_completed(futures2):
                                    future.result()
                            
                            success_count = sum(1 for r in review_results if r.get("success"))
                            self.logger.info(f"[Review Loop] Round {review_round} re-review done. {success_count}/{len(reviewer_configs)} success")
                            review_round += 1
                            
                        except Exception as rev_loop_err:
                            self.logger.error(f"[Review Loop] Round {review_round} crashed: {rev_loop_err}")
                            break
                    
                    # Save review history
                    if isinstance(response.result, dict):
                        response.result["review_loop_history"] = all_review_history
                        response.result["review_loop_rounds"] = review_round
                    # ====== END AUTO REVIEW LOOP ======


                    decisions = [r.get("result", {}).get("decision", "N/A") for r in review_results if r.get("success")]
                    if isinstance(response.result, dict):
                        response.result["peer_review"] = {
                            "reviewers": review_results,
                            "decisions": decisions,
                            "success_count": success_count,
                            "total_count": len(reviewer_configs),
                        }
                        response.result["peer_review_report_path"] = "./report/peer_review_report.md"

                except Exception as rev_err:
                    self.logger.error(f"Multi-Perspective Peer Review crashed, skipping: {rev_err}")
                # =================================================================

                return {
                    "success": True,
                    "data": response.result,
                    "agent_name": response.agent_name,
                    "iterations": response.iterations,
                    "execution_time": response.execution_time,
                }
            else:
                return {
                    "success": False,
                    "error": response.error,
                    "agent_name": response.agent_name
                }

        except Exception as e:
            self.logger.error(f"Failed to assign task to WriterAgent: {e}")
            return {
                "success": False,
                "error": f"Task assignment failed: {str(e)}"
            }

    def assign_task_to_experimenter(self, task_content: str, dataset_paths: list = None) -> dict:
        """灏嗘暟鎹鐞嗐€佹ā鍨嬭缁冨拰浠ｇ爜缂栧啓浠诲姟鍒嗛厤缁?ExperimentAgent"""
        try:
            self.logger.info("Assigning task to ExperimentAgent")
            workspace = Path(
                getattr(getattr(self, "mcp_tools", None), "workspace_path", None)
                or os.getenv("AGENT_WORKSPACE_PATH") or "."
            ).resolve()
            if self._is_reference_management_task(task_content):
                self.logger.warning("Rejected non-experiment task routed to ExperimentAgent: %s", task_content[:180])
                self._publish_agent_progress(
                    "experiment_task_rejected",
                    "该任务属于文献与引用整理，不应调用实验智能体。",
                    recommended_route="InformationSeeker/structured evidence refresh",
                )
                return {
                    "success": False,
                    "error": (
                        "This is literature/reference management, not an experiment task. "
                        "Use InformationSeeker or refresh the structured reference registry; "
                        "do not call ExperimentAgent for DOI cleanup, bibliography merging, or citation formatting."
                    ),
                    "recommended_route": "information_seeker_or_structured_evidence_refresh",
                }

            registry = self._load_reusable_experiment_registry(workspace)
            explicit_rerun = self._explicit_experiment_rerun_requested(task_content)
            fingerprint = self._experiment_task_fingerprint(task_content, dataset_paths, registry)
            if not hasattr(self, "_successful_experiment_task_fingerprints"):
                self._successful_experiment_task_fingerprints = set()

            history = self._load_experiment_history(workspace)
            if not explicit_rerun and fingerprint in history:
                self._experiment_agent_completed = True
                self._successful_experiment_task_fingerprints.add(fingerprint)
                self.logger.info("Reusing previously completed ExperimentAgent task: %s", fingerprint[:12])
                return {
                    "success": True,
                    "data": {"completion_status": "already_completed", "task_fingerprint": fingerprint},
                    "result": history[fingerprint],
                    "experiment_report_file": history[fingerprint].get("experiment_report_file"),
                    "output_figures": history[fingerprint].get("output_figures", []),
                    "experimental_metrics": "",
                    "message": "Experiment task already completed; reused existing artifacts.",
                }

            if (
                registry and not explicit_rerun
                and self._looks_like_bulk_experiment_reprocessing(task_content)
            ):
                self._experiment_agent_completed = True
                self._successful_experiment_task_fingerprints.add(fingerprint)
                registry_path = "./experiment_results/experiment_registry.json"
                reused = {
                    "task_summary": (
                        f"Reused {len(registry)} verified experiment records because all source CSV hashes still match. "
                        "No experiment CSV was reprocessed."
                    ),
                    "completion_status": "reused_verified_experiment_registry",
                    "experimental_metrics": "Use the verified per-record metrics in experiment_registry.json.",
                    "output_figures": [
                        figure for item in registry for figure in (item.get("figures") or [])
                    ],
                    "experiment_report_file": registry_path,
                }
                self._save_experiment_history(workspace, fingerprint, task_content, reused)
                self._publish_agent_progress(
                    "experiment_registry_reused",
                    f"已复用 {len(registry)} 条校验通过的实验记录，不再重复处理 CSV。",
                    experiment_count=len(registry), summary="实验数据未变化，直接使用已有 Experiment Registry。",
                )
                return {
                    "success": True,
                    "data": {"completion_status": "reused_verified_registry", "experiment_count": len(registry)},
                    "result": reused,
                    "experiment_report_file": registry_path,
                    "output_figures": reused["output_figures"],
                    "experimental_metrics": reused["experimental_metrics"],
                    "message": "Verified experiment registry reused; duplicate processing skipped.",
                }
            if getattr(self, "task_id", None):
                try:
                    from src.utils.task_manager import task_manager
                    requested = task_manager.peek_requested_stage(self.task_id)
                    if requested and requested.get("stage") == "experiment":
                        task_manager.clear_requested_stage(self.task_id, "experiment")
                        task_manager.record_event(
                            self.task_id,
                            "stage_directive_applied",
                            "已按用户指导进入实验环节。",
                            {"stage": "experiment", "instruction": requested.get("instruction", "")},
                        )
                        self.logger.info("已执行用户流程指导：进入实验环节")
                except Exception as directive_error:
                    self.logger.debug("Could not clear experiment stage directive: %s", directive_error)

            # 鎵弿 user_uploads/锛屽憡鐭ュ疄楠屾櫤鑳戒綋鐢ㄦ埛宸叉彁渚涘摢浜涙枃浠?
            user_uploads_info = ""
            try:
                scan_result = self.execute_tool_call({
                    "name": "list_workspace",
                    "arguments": {"path": "user_uploads"}
                })
                if scan_result and scan_result.get("success"):
                    items = scan_result.get("data", {}).get("items", [])
                    images = []
                    data_files = []
                    code_files = []
                    for item in items:
                        name = item.get("name", "")
                        ext = name.lower()
                        if ext.endswith((".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".gif", ".svg")):
                            images.append(name)
                        elif ext.endswith((".csv", ".xlsx", ".xls", ".json", ".tsv")):
                            data_files.append(name)
                        elif ext.endswith((".py", ".txt", ".md", ".pdf")):
                            code_files.append(name)

                    if images or data_files or code_files:
                        user_uploads_info = "\n\n[User uploaded files - inspect before experiment work]\n"
                        user_uploads_info += "Before writing experiment code, inspect ./user_uploads/ first.\n"
                        if images:
                            user_uploads_info += f"- Image files exist ({len(images)}). Inspect image content and reuse existing visual results instead of regenerating them.\n"
                        if data_files:
                            user_uploads_info += f"- Data files exist ({len(data_files)}). Read them with structured parsers and never invent data.\n"
                        if code_files:
                            user_uploads_info += f"- Code/document files exist ({len(code_files)}). Read them and extend the existing logic instead of rewriting from scratch.\n"
                        user_uploads_info += "Core rule: user-provided content is the starting point. Understand it first, then supplement only when needed.\n"
            except Exception as scan_err:
                self.logger.warning("扫描 user_uploads 失败: %s", scan_err)
            # 鎵弿缁撴潫

            task_desc = (
                "[Experiment execution rules]\n"
                "If an archive is provided, extract it inside the workspace. For GPU-capable workloads, detect CUDA "
                "and use it when available; otherwise provide a bounded CPU fallback.\n"
                "Use file_write for Python source files and run_python_script for execution. Never create source files "
                "through shell redirection, heredoc, base64, or python -c escaping workarounds.\n"
                "Do not reprocess an already verified experiment unless this task identifies a concrete missing metric, "
                "figure, comparison, changed file, or explicit rerun.\n"
                "For multiple figures, save each useful academic figure separately with plt.figure() and plt.close(); "
                "do not create redundant variants merely to increase the figure count.\n\n"
                f"Specific task:\n{task_content}{user_uploads_info}"
            )

            if dataset_paths:
                task_desc += f"\n\nAvailable dataset paths to explore: {dataset_paths}"

            try:
                from agents import TaskInput
            except ImportError:
                from ..agents import TaskInput

            task_input = TaskInput(
                task_content=task_desc,
                task_executor="experiment_agent",
                workspace_id=self.task_id if self.task_id else "default_session"
            )

            experimenter = create_experiment_agent(
                model=self.config.model,
                max_iterations=60,
                shared_mcp_client=self.mcp_tools.client if hasattr(self.mcp_tools, 'client') else self.mcp_tools
            )
            experimenter.task_id = self.task_id

            # Pass cancellation token to experiment agent
            if hasattr(self, "_cancellation_token") and self._cancellation_token:
                experimenter.set_cancellation_token(self._cancellation_token)

            self._publish_agent_progress("experiment_agent_started", "ExperimentAgent 开始实验分析或绘图任务")
            response = experimenter.execute_task(task_input)

            if response.success:
                self._experiment_agent_completed = True
                self._publish_agent_progress(
                    "experiment_agent_ready", "ExperimentAgent 实验分析任务完成",
                    iterations=response.iterations
                )
                result = response.result or {}

                output_figures = result.get('output_figures', [])
                experimental_metrics = result.get('experimental_metrics', '')
                task_summary_exp = result.get('task_summary', '')
                completion_status_exp = result.get('completion_status', 'completed')

                # 鈹€鈹€ 鍏抽敭鏂板:鎶婂疄楠岀粨鏋滃簭鍒楀寲鍐欏叆 Markdown 鏂囦欢 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
                import os, json as _json
                exp_report_dir = "./experiment_results"
                os.makedirs(exp_report_dir, exist_ok=True)
                exp_report_path = f"{exp_report_dir}/experiment_results.md"

                figures_md = ""
                if output_figures:
                    figures_md = "\n\n## Generated Figures\n"
                    for fp in output_figures:
                        fname = os.path.basename(fp)
                        figures_md += f"\n![{fname}](../experiment_results/{fname})\n"

                exp_md_content = (
                    f"# Experiment Results\n\n"
                    f"## Task Summary\n{task_summary_exp}\n\n"
                    f"## Experimental Metrics\n\n{experimental_metrics}"
                    f"{figures_md}"
                )

                # 閫氳繃 MCP file_write 宸ュ叿鍐欏叆(涓庡叾浠?Agent 浣跨敤鐩稿悓鐨勬枃浠剁郴缁?
                try:
                    self.execute_tool_call({
                        "name": "file_write",
                        "arguments": {
                            "file_path": exp_report_path,
                            "content": exp_md_content
                        }
                    })
                    self.logger.info("实验结果已写入: %s", exp_report_path)
                except Exception as fw_err:
                    self.logger.warning("实验结果写入失败（非致命）: %s", fw_err)
                try:
                    from src.pipeline_v2.visual_communication import plan_visual_communication
                    workspace = Path(getattr(self.mcp_tools, "workspace_path", None) or os.getenv("AGENT_WORKSPACE_PATH") or ".")
                    visual_assets, visual_warnings = plan_visual_communication(workspace, force=True)
                    self._publish_agent_progress(
                        "visual_plan_ready", "实验完成后已刷新全篇图表与表格计划",
                        asset_count=len(visual_assets), warning_count=len(visual_warnings),
                        summary=f"已登记 {len(visual_assets)} 项可用于论文的结构图、结果图或表格。",
                    )
                except Exception as visual_error:
                    self.logger.warning("Could not refresh visual communication plan after ExperimentAgent: %s", visual_error)
                # 鈹€鈹€ 鍐欏叆缁撴潫 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

                figures_instruction = ""
                if output_figures:
                    figures_instruction = "\n\n[ExperimentAgent generated candidate figure files]\n"
                    for fig_path in output_figures:
                        figures_instruction += f"  - Original path: {fig_path}\n"
                    figures_instruction += (
                        "\nThese are candidates, not mandatory insertions. Refresh and follow research/visual_assets_registry.json; "
                        "WriterAgent may insert only selected assets in their assigned sections with exact Markdown paths."
                    )

                completed_result = {
                    "success": True,
                    "result": result,
                    "output_figures": output_figures,
                    "experimental_metrics": experimental_metrics,
                    "figures_instruction": figures_instruction,
                    # 鈹€鈹€ 鏂板:鎶婂疄楠屾姤鍛婃枃浠惰矾寰勫憡璇?Planner 鈹€鈹€
                    "experiment_report_file": exp_report_path,
                    "message": "Experiment completed successfully"
                }
                self._successful_experiment_task_fingerprints.add(fingerprint)
                self._save_experiment_history(workspace, fingerprint, task_content, completed_result)
                return completed_result
            else:
                return {
                    "success": False,
                    "error": response.error,
                    "output_figures": [],
                    "experimental_metrics": "",
                    "experiment_report_file": None,
                    "message": "Experiment failed or timed out"
                }

        except Exception as e:
            if "cancelled" in str(e).lower():
                self.logger.info("Experiment task cancelled by user, re-raising")
                raise
            return {"success": False, "error": str(e)}

    def _build_agent_specific_tool_schemas(self) -> List[Dict[str, Any]]:
        """
        Build tool schemas for PlannerAgent using proper MCP architecture.
        Schemas come from MCP server via client, not direct imports.
        """

        # Get MCP tool schemas from server via client (proper MCP architecture)
        schemas = super()._build_agent_specific_tool_schemas()

        # Add schemas for built-in task assignment tools
        # 淇敼涓轰互涓嬪唴瀹?纭繚鎵€鏈夋ā寮忛兘鑳借皟鐢?experimenter
        planner_mode_builtin_tools_map = {
            "auto": [
                "think", "reflect", "assign_multi_subjective_tasks_to_info_seeker",
                "assign_multi_objective_tasks_to_info_seeker", "assign_subjective_task_to_writer",
                "assign_task_to_experimenter", "planner_subjective_task_done",
                "planner_objective_task_done", "document_qa", "document_extract", "download_files",
                "list_workspace", "file_read", "file_find_by_name"
            ],
            "writing": [
                "think", "reflect", "assign_multi_subjective_tasks_to_info_seeker",
                "assign_subjective_task_to_writer", "assign_task_to_experimenter",
                "planner_subjective_task_done", "document_qa",
                "document_extract", "download_files", "list_workspace", "file_read", "file_find_by_name"
            ],
            "qa": [
                "think", "reflect", "assign_multi_objective_tasks_to_info_seeker", "assign_task_to_experimenter",
                "planner_objective_task_done", "document_qa", "document_extract", "download_files", "list_workspace",
                "file_read", "file_find_by_name"
            ]
        }
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
                    "name": "assign_multi_subjective_tasks_to_info_seeker",
                    "description": "Assign 1~6 distinct research tasks to InformationSeekerAgents. Do not repeat completed topics. After the initial literature stage, every supplemental task must provide a concrete evidence_gap.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "tasks": {
                                "type": "array",
                                "description": "One to six non-overlapping tasks. Merge semantically duplicate topics before calling.",
                                "minItems": 1,
                                "maxItems": 6,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "task_content": {
                                            "type": "string",
                                            "description": "Detailed description of the task to be performed"
                                        },
                                        "task_steps_for_reference": {
                                            "type": "string",
                                            "description": "Optional reference steps for task execution"
                                        },
                                        "deliverable_contents": {
                                            "type": "string",
                                            "description": "Expected format and content of deliverables"
                                        },
                                        "current_task_status": {
                                            "type": "string",
                                            "description": "Current status and context of the task, important documents that may be used and referenced"
                                        },
                                        "acceptance_checking_criteria": {
                                            "type": "string",
                                            "description": "Criteria for determining task completion and quality"
                                        },
                                        "evidence_gap": {
                                            "type": "string",
                                            "description": "Required for supplemental searches after the initial literature stage: the exact unsupported claim, missing source type/date range, or unresolved citation. Leave empty only for the first literature batch."
                                        },
                                    },
                                    "required": ["task_content"]
                                }
                            }
                        },
                        "required": ["tasks"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "assign_multi_objective_tasks_to_info_seeker",
                    "description": "Assign 1~5 research or information gathering tasks to different InformationSeekerAgents for parallel execution, each task descriptions must be semantically complete and clearly provide contextual information and potentially important reference documents.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "tasks": {
                                "type": "array",
                                "description": "List of tasks to be assigned to multiple InformationSeekerAgents",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "task_content": {
                                            "type": "string",
                                            "description": "Detailed description of the task to be performed, the task description must be semantically complete"
                                        },
                                        "task_steps_for_reference": {
                                            "type": "string",
                                            "description": "Optional reference steps for task execution"
                                        },
                                        "deliverable_contents": {
                                            "type": "string",
                                            "description": "Expected format and content of deliverables"
                                        },
                                        "current_task_status": {
                                            "type": "string",
                                            "description": "Current status and context of the task, important documents that may be used and referenced"
                                        },
                                        "acceptance_checking_criteria": {
                                            "type": "string",
                                            "description": "Criteria for determining task completion and quality, and the requirements in the event of task completion failure"
                                        },
                                    },
                                    "required": ["task_content"]
                                }
                            }
                        },
                        "required": ["tasks"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "assign_subjective_task_to_writer",
                    "description": (
                        "Assign a writing task to WriterAgent to produce a complete academic paper. "
                        "CRITICAL: This call is SYNCHRONOUS and BLOCKING. "
                        "After calling this tool, DO NOT search for files, DO NOT check if paper exists, "
                        "DO NOT call file_find_by_name or list_workspace. "
                        "Simply wait 鈥?when this tool returns success, the paper is already written and saved. "
                        "Immediately call planner_subjective_task_done after this returns success."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "user_query": {
                                "type": "string",
                                "description": "Pass in the original user question."
                            },
                            "task_content": {
                                "type": "string",
                                "description": (
                                    "Write a complete academic paper (NOT a report or literature review) "
                                    "based on provided experimental data and reference materials. "
                                    "The paper MUST follow standard journal structure: "
                                    "Title, Abstract, Introduction, Related Work, Methodology, "
                                    "Experiments/Results, Discussion, Conclusion, References. "
                                    "Provide a general description of the research topic only; "
                                    "do NOT specify the outline structure yourself."
                                )
                            },
                            "key_files": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "file_path": {
                                            "type": "string",
                                            "description": "Relative path to the file containing research content"
                                        }
                                    },
                                    "required": ["file_path"]
                                },
                                 "description": (
                                     "ALL key files to pass to WriterAgent, including: "
                                     "1) Research papers from information seeker "
                                     "2) experiment_results/experiment_registry.json and experiment_results/tables/*.md "
                                     "when experiments were imported; do not create a duplicate experiment_results.md "
                                     "3) registered experiment figures (MANDATORY if figures exist) "
                                     "4) User uploaded data files from ./user_uploads/ "
                                     "Missing experiment results or figures = incomplete paper."
                                 )
                            }
                        },
                        "required": ["user_query", "task_content", "key_files"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "assign_task_to_experimenter",
                    "description": (
                        "Assign real computational experiments, dataset analysis, metric calculation, model training, "
                        "ablation/comparison analysis, or missing experiment figures to ExperimentAgent. First reuse a "
                        "verified experiment_registry.json when source hashes match. Never use this tool for literature "
                        "search, DOI cleanup, bibliography merging, citation formatting, or generic file consolidation."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task_content": {
                                "type": "string",
                                "description": "Detailed description of the experiment, algorithm, or data processing required."
                            },
                            "dataset_paths": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "List of paths to relevant datasets or files."
                            }
                        },
                        "required": ["task_content"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "writer_subjective_task_done",
                    "description": "Writer Agent task completion reporting for complete long-form content. Called after all chapters/sections are written to provide a summary of the complete long article, final completion status and analysis, and the storage path of the final consolidated article.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "final_article_path": {
                                "type": "string",
                                "description": "The file path where the final article is saved."
                            },
                            "article_summary": {
                                "type": "string",
                                "description": "Comprehensive summary of the complete long-form article, including main themes, key points covered, and overall narrative structure.",
                                "format": "markdown"
                            },
                            "completion_status": {
                                "type": "string",
                                "enum": ["completed", "partial", "failed"],
                                "description": "Final status of the complete long-form writing task"
                            },
                            "completion_analysis": {
                                "type": "string",
                                "description": "Analysis of the overall writing project completion including: assessment of article coherence and quality, evaluation of content organization and flow, identification of any challenges in the writing process, and overall evaluation of the long-form content creation success."
                            }
                        },
                        "required": ["final_article_path", "article_summary", "completion_status", "completion_analysis"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "planner_subjective_task_done",
                    "description": "When the writer agent is executed, the task done tool is called to end the planner's task.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "final_article_path": {
                                "type": "string",
                                "description": "The file path where the final article is saved."
                            },
                            "task_summary": {
                                "type": "string",
                                "description": "This field is mainly used to describe the main content of the article, briefly summarize it, and finally indicate the path where the final article is saved.",
                                "format": "markdown"
                            },
                            "task_name": {
                                "type": "string",
                                "description": "The name of the task currently assigned to the agent, usually with underscores (e.g., 'web_research_ai_trends')"
                            },
                            "completion_status": {
                                "type": "string",
                                "enum": ["completed", "partial", "failed"],
                                "description": "Final task status"
                            }
                        },
                        "required": ["final_article_path", "task_summary", "task_name", "completion_status"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "planner_objective_task_done",
                    "description": "Structured reporting of task completion details including summary, decisions, and final answer",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task_summary": {
                                "type": "string",
                                "description": "Comprehensive markdown covering what the agent was asked to do, steps taken, tools used, key findings, files created, challenges",
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
                            },
                            "final_answer": {
                                "type": "string",
                                "description": "The final response displayed to the user",
                            }
                        },
                        "required": ["task_summary", "task_name", "key_files", "completion_status", "final_answer"]
                    }
                }
            },
        ]

        used_builtin_schemas = [schema for schema in builtin_assignment_schemas if
                                schema["function"]["name"] in planner_mode_builtin_tools_map[self.config.planner_mode]]
        schemas.extend(used_builtin_schemas)

        # 馃殌 鏍稿績淇:鍦ㄥ彂閫佺粰澶фā鍨嬩箣鍓?鎶婄鐢ㄥ伐鍏风殑璇存槑涔︿粠鍒楄〃涓交搴曟挄鎺?
        forbidden_tools = [
            "bash",
            "str_replace_based_edit_tool",
            "file_write",
            "run_python_script",
            "section_writer",
            "concat_section_files"
        ]
        filtered_schemas = [s for s in schemas if s.get("function", {}).get("name") not in forbidden_tools]

        return filtered_schemas

    def set_cancellation_token(self, cancellation_token):
        """
        Set the cancellation token for this agent
        璁剧疆姝や唬鐞嗙殑鍙栨秷浠ょ墝

        Args:
            cancellation_token: threading.Event object that will be set when task should be cancelled
        """
        self._cancellation_token = cancellation_token

    def _check_cancellation(self) -> bool:
        """
        Check if task has been cancelled
        妫€鏌ヤ换鍔℃槸鍚﹀凡琚彇娑?

        Returns:
            True if task should be cancelled, False otherwise
        """
        if self._cancellation_token and self._cancellation_token.is_set():
            self.logger.info(f"Task {self.task_id} cancellation detected")
            return True
        return False

    def _execute_react_loop(self, initial_message: str, max_iterations: int = 20) -> Dict[str, Any]:
        """
        Execute the ReAct loop for planning tasks

        Args:
            initial_message: Initial message to start the planning process
            max_iterations: Maximum number of iterations to perform

        Returns:
            Dictionary with execution results and trace
        """
        start_time = time.time()
        try:
            # Reset trace for new task
            self.reset_trace()
            # Initialize conversation history
            conversation_history = []

            # Build system prompt for planning
            system_prompt = self._build_system_prompt()
            # Add to conversation
            conversation_history.append({"role": "system", "content": system_prompt})
            conversation_history.append({"role": "user", "content": initial_message + " /no_think"})

            iteration = 0
            task_completed = False

            # Get model endpoint configuration from env-backed config
            from config.config import get_config
            config = get_config()
            model_config = config.get_custom_llm_config()

            # ReAct Loop: Reasoning -> Acting -> Reasoning -> Acting...
            while iteration < self.config.max_iterations and not task_completed:
                iteration += 1
                self.logger.info(f"Planning iteration {iteration}")
                interventions = self._planner_pause_checkpoint(iteration)
                if interventions:
                    conversation_history.append({
                        "role": "user",
                        "content": "用户在规划检查点补充了以下指导，请应用到本轮及后续规划：\n- "
                                   + "\n- ".join(interventions) + " /no_think",
                    })
                requested_stage = None
                if getattr(self, "task_id", None):
                    try:
                        from src.utils.task_manager import task_manager
                        requested_stage = task_manager.peek_requested_stage(self.task_id)
                    except Exception:
                        requested_stage = None
                if requested_stage:
                    conversation_history.append({
                        "role": "user",
                        "content": (
                            "这是用户必须执行的流程切换指令，不是可选建议：停止继续扩展当前阶段，"
                            f"现在进入 {requested_stage.get('stage')}。原始指导："
                            f"{requested_stage.get('instruction', '')} /no_think"
                        ),
                    })
                self._publish_agent_progress(
                    "planner_iteration", f"PlannerAgent 正在规划第 {iteration}/{self.config.max_iterations} 轮",
                    iteration=iteration, max_iterations=self.config.max_iterations,
                )

                # Check for task cancellation before LLM call
                if self._check_cancellation():
                    self.logger.info(f"Task {self.task_id} cancelled by user at iteration {iteration}")
                    raise Exception("Task cancelled by user")

                try:
                    # Get LLM response (reasoning + potential tool calls)
                    has_vision = False
                    for msg in conversation_history:
                        if isinstance(msg.get("content"), list):
                            has_vision = True
                            break

                    payload = {
                        "model": self.config.model if hasattr(self.config, 'model') else "pangu_auto",
                        "messages": conversation_history,
                        "temperature": self.config.temperature if hasattr(self.config, 'temperature') else 0.3,
                        "max_tokens": self.config.max_tokens if hasattr(self.config, 'max_tokens') else 4096,
                    }

                    # 2. 濡傛灉娌℃湁鍥剧墖,鎵嶄娇鐢ㄦ枃鏈笓灞炵殑 chat_template
                    if not has_vision:
                        payload[
                            "chat_template"] = "{% for message in messages %}{% if loop.first and messages[0]['role'] != 'system' %}{{ '<s>[unused9]绯荤粺:[unused10]' }}{% endif %}{% if message['role'] == 'system' %}{{'<s>[unused9]绯荤粺:' + message['content'] + '[unused10]'}}{% endif %}{% if message['role'] == 'assistant' %}{{'[unused9]鍔╂墜:' + message['content'] + '[unused10]'}}{% endif %}{% if message['role'] == 'tool' %}{{'[unused9]宸ュ叿:' + message['content'] + '[unused10]'}}{% endif %}{% if message['role'] == 'function' %}{{'[unused9]鏂规硶:' + message['content'] + '[unused10]'}}{% endif %}{% if message['role'] == 'user' %}{{'[unused9]鐢ㄦ埛:' + message['content'] + '[unused10]'}}{% endif %}{% endfor %}{% if add_generation_prompt %}{{ '[unused9]鍔╂墜:' }}{% endif %}"
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

                            # 3. 绮惧噯鎹曡幏闄愭祦 (429 / Quota)
                            if status_code == 429 or (isinstance(response_json, dict) and "error" in response_json):
                                err = response_json.get("error", {})
                                err_code = str(err.get("code", status_code))
                                err_msg = str(err.get("message", "")).lower()

                                if err_code == "429" or "rate limit" in err_msg or "429" in err_msg or "quota" in err_msg or "throttling" in err_msg:
                                    self.logger.warning(
                                        f"鈿狅笍 瑙﹀彂 API 闄愭祦鎴栭搴﹁秴闄?(429).Agent 灏嗘矇鐫?30 绉掑悗缁х画... (绗?{retry_num}/{max_retry_num} 娆″皾璇?")

                                    time.sleep(30)
                                    retry_num += 1
                                    continue

                            # 姝ｅ父閿欒妫€鏌?
                            if "choices" not in response_json or not response_json["choices"]:
                                error_info = response_json.get("error", "鏈煡 API 閿欒")
                                self.logger.error(f"API 鍝嶅簲寮傚父: {response_json}")
                                assistant_message = {
                                    "role": "assistant",
                                    "content": f"[unused16][unused17] 閿欒:API 璇锋眰澶辫触({error_info}).璇峰皾璇曡皟鏁寸瓥鐣?"
                                }
                            else:
                                assistant_message = response_json["choices"][0]["message"]

                            self.logger.debug("API response received successfully")
                            break  # 鎴愬姛,璺冲嚭閲嶈瘯寰幆

                        except Exception as e:
                            err_msg = str(e).lower()
                            if "429" in err_msg or "rate limit" in err_msg or "quota" in err_msg or "throttling" in err_msg:
                                self.logger.warning(
                                    f"鈿狅笍 鎹曡幏鍒扮綉缁滃眰闄愭祦寮傚父.Agent 灏嗘矇鐫?60 绉掑悗缁х画... (绗?{retry_num}/{max_retry_num} 娆″皾璇?")

                                time.sleep(60)
                            else:
                                self.logger.warning(f"API 璇锋眰澶辫触: {e},3绉掑悗閲嶈瘯...")

                                time.sleep(3)

                            retry_num += 1
                            if retry_num == max_retry_num:
                                raise ValueError(str(e))
                            continue
                    # assistant_message = response["choices"][0]["message"]

                    # Log the reasoning
                    try:
                        if assistant_message["content"]:
                            # reasoning_content = assistant_message["content"].split("[unused16]")[-1].split("[unused17]")[0]
                            content = assistant_message.get("content", "")
                            if "[unused16]" in content and "[unused17]" in content:
                                reasoning_content = content.split("[unused16]")[-1].split("[unused17]")[0]
                            else:
                                reasoning_content = re.sub(r'\[unused\d+\]', '', content).strip()[:500]
                            if len(reasoning_content) > 0:
                                self.log_reasoning(iteration, reasoning_content)
                                self._publish_agent_progress(
                                    "planner_decision",
                                    f"Planner 第 {iteration} 轮规划摘要",
                                    iteration=iteration,
                                    summary=self._humanize_progress_text(
                                        reasoning_content, "我正在检查现有资料并确定下一步研究任务。"
                                    ),
                                )
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
                    #     # 鏌ユ壘鎵€鏈夊尮閰嶇殑鍐呭
                    #     tool_call_matches = re.findall(r"\[unused11\]([\s\S]*?)\[unused12\]", content)
                    #
                    #     # 澧炲姞鍒ょ┖鏍￠獙,闃叉 index out of range
                    #     if not tool_call_matches:
                    #         return []
                    #
                    #     try:
                    #         # 鍙栫涓€涓尮閰嶉」骞跺皾璇曡В鏋?JSON
                    #         first_match = tool_call_matches[0].strip()
                    #         tool_calls = json.loads(first_match)
                    #         return tool_calls if isinstance(tool_calls, list) else [tool_calls]
                    #     except Exception as e:
                    #         logger.error(f"瑙ｆ瀽宸ュ叿璋冪敤 JSON 澶辫触: {e}")
                    #         return []

                    # Add assistant message to conversation
                    conversation_history.append({
                        "role": "assistant",
                        "content": assistant_message["content"]
                    })

                    # 馃毃 鏆村姏 JSON 瑙ｆ瀽鍣ㄤ笌鎶ラ敊鏈哄埗(浠?WriterAgent 绉绘杩囨潵)
                    def extract_tool_calls_local(content):
                        import re
                        if not content:
                            return []
                        base_tool_calls = self.extract_tool_calls(content)
                        if base_tool_calls:
                            return base_tool_calls
                        tool_call_matches = re.findall(r"\[unused11\]([\s\S]*?)\[unused12\]", content)
                        if not tool_call_matches:
                            return []
                        first_match = tool_call_matches[0].strip()
                        try:
                            tool_calls = json.loads(first_match)
                            return tool_calls if isinstance(tool_calls, list) else [tool_calls]
                        except Exception as e:
                            try:
                                fixed_match = first_match.replace('\n', '\\n').replace('\r', '')
                                tool_calls = json.loads(fixed_match)
                                return tool_calls if isinstance(tool_calls, list) else [tool_calls]
                            except Exception as e2:
                                raise ValueError(
                                    f"銆愯嚧鍛借娉曢敊璇€戜綘杈撳嚭鐨?JSON 鏍煎紡宕╂簝浜?璇蜂弗鏍兼鏌ュ瓧绗︿覆鍐呴儴鏄惁鍖呭惈浜嗘湭杞箟鐨勫弻寮曞彿(\")!濡傛灉鏈?蹇呴』浣跨敤鍙嶆枩鏉犺浆涔?\\\")!璇︾粏鎶ラ敊: {e2}")

                    tool_calls = extract_tool_calls_local(assistant_message["content"])
                    if tool_calls is None:
                        tool_calls = []
                    # Execute tool calls if any (Acting phase)

                    for tool_call in tool_calls:
                        arguments = tool_call.get("arguments", {})
                        if isinstance(arguments, str):
                            try:
                                parsed_arguments = json.loads(arguments)
                                arguments = parsed_arguments if isinstance(parsed_arguments, dict) else {"_raw_arguments": parsed_arguments}
                                tool_call["arguments"] = arguments
                            except Exception:
                                arguments = {"_raw_arguments": arguments}
                                tool_call["arguments"] = arguments
                        tool_name = tool_call.get("name", "")

                        # ========== 杩欓噷鐨勪换鍔″畬鎴愬垽鏂繚鐣欏悇涓?Agent 鍘熸湁鐨?==========
                        # (濡傛灉鏄?Planner,杩欓噷鍙兘鏄?planner_subjective_task_done 绛?涓嶈鏀瑰姩杩欎竴灏忔if)
                        if tool_name in ["info_seeker_subjective_task_done", "info_seeker_objective_task_done",
                                         "writer_subjective_task_done", "planner_subjective_task_done",
                                         "planner_objective_task_done", "experiment_task_done"]:
                            if (
                                tool_name == "planner_subjective_task_done"
                                and os.getenv("SCIA_PIPELINE_VERSION", "").strip().lower() == "hybrid"
                                and not self._writer_agent_completed
                            ):
                                self._sync_hybrid_completion_from_artifacts()
                                if self._writer_agent_completed:
                                    task_completed = True
                                    self.log_action(iteration, tool_name, arguments, arguments)
                                    break
                                missing = []
                                if not self._information_seeker_completed:
                                    missing.append("InformationSeeker")
                                if not self._experiment_agent_completed:
                                    missing.append("ExperimentAgent")
                                missing.append("WriterAgent")
                                error_result = {
                                    "success": False,
                                    "error": (
                                        "Cannot finish the paper workflow before the delegated agents complete: "
                                        + ", ".join(missing)
                                        + ". Continue the old autonomous agent workflow instead of writing directly as PlannerAgent."
                                    ),
                                }
                                self.log_action(iteration, tool_name, arguments, error_result)
                                conversation_history.append({
                                    "role": "tool",
                                    "content": json.dumps(error_result, ensure_ascii=False) + " /no_think",
                                })
                                continue
                            task_completed = True
                            self.log_action(iteration, tool_name, arguments, arguments)
                            break
                        # ============================================================

                        if tool_name in ["think", "reflect"]:
                            tool_result = {"tool_results": "You can proceed to invoke other tools if needed."}
                        else:
                            tool_result = self.execute_tool_call(tool_call)

                        # Log the action using base class method
                        self.log_action(iteration, tool_name, arguments, tool_result)
                        self._publish_agent_progress(
                            "planner_tool_action", f"PlannerAgent 调用工具：{tool_name}",
                            iteration=iteration, tool=tool_name,
                            summary=json.dumps(arguments, ensure_ascii=False)[:240],
                        )

                        # Check cancellation after tool execution
                        if self._check_cancellation():
                            self.logger.info(f"Task {self.task_id} cancelled after tool at iter {iteration}")
                            raise Exception("Task cancelled by user")

                        # 4. 璇嗗埆鍥剧墖缁撴灉,骞舵瀯閫犵鍚?OpenAI 瑙嗚鏍囧噯鐨?List
                        is_vision = False
                        image_url = ""
                        if isinstance(tool_result, dict) and "data" in tool_result and isinstance(tool_result["data"],
                                                                                                  dict):
                            if tool_result["data"].get("is_vision_content"):
                                is_vision = True
                                image_url = tool_result["data"].get("image_url", "")

                        if is_vision:
                            # 瀵逛簬鍥剧墖,涓嶈兘鐩存帴濉炶繘 tool 瑙掕壊,闇€瑕佽浆浜ょ粰 user 瑙掕壊閫忎紶
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
                                     "text": f"杩欐槸浣犺姹傜殑鍥剧墖(璺緞:{arguments.get('file_path')}),璇锋牴鎹綋鍓嶄换鍔℃彁鍙栧浘涓彲瑙佺殑鍏抽敭鏍囩銆佹暟鍊笺€佽秼鍔裤€佸浘渚嬪拰缁撹,涓嶈棰勮鏌愪竴绫绘寚鏍? /no_think"},
                                    {"type": "image_url", "image_url": {"url": image_url}}
                                ]
                            })
                        else:
                            # 姝ｅ父鏂囨湰宸ュ叿鐨勮繑鍥?
                            conversation_history.append({
                                "role": "tool",
                                "content": json.dumps(tool_result, ensure_ascii=False, indent=2) + " /no_think"
                            })

                    # If no tool calls, encourage continued planning
                    if len(tool_calls) == 0:
                        # Add follow-up prompt to encourage action or completion
                        followup_prompt = (
                            "Continue your planning process. Use available tools to assign tasks to agents, "
                            "search for information, or coordinate work. When you have a complete answer, "
                            "call planner_subjective_task_done or planner_objective_task_done. /no_think"
                        )
                        conversation_history.append({"role": "user", "content": followup_prompt})

                except Exception as e:
                    # Re-raise cancellation exceptions
                    if "cancelled" in str(e).lower():
                        self.logger.info(f"Planner cancelled by user at iter {iteration}")
                        raise
                    error_msg = f"Error in planning iteration {iteration}: {e}"
                    self.log_error(iteration, error_msg)
                    # 鈿狅笍 缁濆涓嶈兘 break!鎶婇敊璇杺缁欏畠,閫煎畠閲嶆柊杈撳嚭姝ｇ‘鐨?JSON!
                    conversation_history.append({
                        "role": "user",
                        "content": f"{error_msg}\n璇蜂綘绔嬪埢妫€鏌ュ垰鎵嶈緭鍑虹殑 JSON 鏍煎紡!缁濅笉鑳藉寘鍚湭杞箟鐨勫弻寮曞彿鎴栫湡瀹炴崲琛岀!璇蜂慨姝ｅ悗閲嶈瘯. /no_think"
                    })
                    continue  # 缁х画涓嬩竴娆″惊鐜?
                    continue  # 缁х画涓嬩竴娆″惊鐜?

            execution_time = time.time() - start_time

            # Do not turn a completed research run into a failure merely because
            # Planner spent its final rounds on redundant bookkeeping.  This is
            # a quality-gated handoff, not a shortcut: it runs only after the
            # full Planner budget and only when both durable evidence gates pass.
            if (
                not task_completed
                and os.getenv("SCIA_PIPELINE_VERSION", "").strip().lower() == "hybrid"
                and not getattr(self, "_writer_agent_completed", False)
            ):
                try:
                    state = self._sync_hybrid_completion_from_artifacts(
                        verify_literature=True, query=initial_message,
                    )
                    if (
                        getattr(self, "_information_seeker_completed", False)
                        and getattr(self, "_experiment_agent_completed", False)
                        and (state.get("reference_gate") or {}).get("reference_gate_met", False)
                    ):
                        self.logger.warning(
                            "Planner iteration budget exhausted with evidence gates satisfied; "
                            "performing final WriterAgent handoff"
                        )
                        handoff = self.assign_subjective_task_to_writer(
                            task_content=(
                                "Write the complete evidence-grounded academic paper now. "
                                "Use the ResearchContract, Claims-Evidence Matrix, verified literature registry, "
                                "experiment registry, registered figures, and deterministic tables in this workspace."
                            ),
                            user_query=initial_message,
                            key_files=[],
                        )
                        report_path = self._active_workspace_path() / "report" / "final_report.md"
                        self.log_action(iteration, "assign_subjective_task_to_writer", {"automatic_handoff": True}, handoff)
                        if handoff.get("success") and report_path.is_file() and report_path.stat().st_size > 0:
                            return {
                                "success": True,
                                "data": handoff.get("data", handoff),
                                "reasoning_trace": self.reasoning_trace,
                                "iterations": iteration,
                                "execution_time": time.time() - start_time,
                            }
                except Exception as handoff_error:
                    self.logger.warning("Final WriterAgent handoff was not possible: %s", handoff_error)

            # Extract final result
            if task_completed:
                # Find the completion result in the trace
                completion_result = None
                for step in reversed(self.reasoning_trace):
                    if step.get("type") == "action" and step.get("tool") in ["planner_subjective_task_done",
                                                                             "planner_objective_task_done"]:
                        completion_result = step.get("result")
                        break

                return {
                    "success": True,
                    "data": completion_result,
                    "reasoning_trace": self.reasoning_trace,
                    "iterations": iteration,
                    "execution_time": execution_time
                }
            else:
                return {
                    "success": False,
                    "error": f"Planning task not completed within {max_iterations} iterations",
                    "reasoning_trace": self.reasoning_trace,
                    "iterations": iteration,
                    "execution_time": execution_time
                }
        except Exception as e:
            execution_time = time.time() - start_time if 'start_time' in locals() else 0
            self.logger.error(f"Error in execute_react_loop: {e}")
            return {
                "success": False,
                "error": str(e),
                "reasoning_trace": self.reasoning_trace,
                "iterations": iteration if 'iteration' in locals() else 0,
                "execution_time": execution_time
            }


    def execute_task(self, user_query: str) -> AgentResponse:
        """
        Execute a planning task for the given user query

        Args:
            user_query: The user's query or request

        Returns:
            AgentResponse with planning results and process trace
        """
        start_time = time.time()

        try:
            self.logger.info(f"Starting planner task: {user_query}")

            # Execute the planning task using ReAct pattern
            result = self._execute_react_loop(
                initial_message=user_query,
                max_iterations=self.config.max_iterations  # Reasonable limit for planning tasks
            )

            execution_time = time.time() - start_time

            return AgentResponse(
                success=result.get("success", False),
                result=result.get("data"),
                error=result.get("error"),
                reasoning_trace=result.get("reasoning_trace", []),
                iterations=result.get("iterations", 0),
                execution_time=execution_time,
                agent_name=self.config.agent_name
            )

        except Exception as e:
            execution_time = time.time() - start_time
            self.logger.error(f"Planner execution failed: {e}")

            return AgentResponse(
                success=False,
                error=f"Planner execution failed: {str(e)}",
                reasoning_trace=[],
                iterations=0,
                execution_time=execution_time,
                agent_name=self.config.agent_name
            )


def create_planner_agent(
        model: Any = None,
        sub_agent_configs: Dict[str, Dict[str, Any]] = None,
        shared_mcp_client=None,
        **kwargs
) -> PlannerAgent:
    """
    Create a PlannerAgent instance with server-managed sessions.

    Args:
        model: The LLM model to use
        sub_agent_configs: Configuration for sub-agents (information_seeker, writer)
        shared_mcp_client: Optional shared MCP client to prevent duplicate connections
        **kwargs: Additional configuration options

    Returns:
        Configured PlannerAgent instance
    """
    # Import the enhanced config function
    from .base_agent import create_agent_config

    # Handle agent_name if provided in kwargs
    agent_name = kwargs.pop("agent_name", "PlannerAgent")

    # Handle task_id if provided in kwargs
    task_id = kwargs.pop("task_id", None)

    # Create agent configuration (session managed by MCP server)
    config = create_agent_config(
        agent_name=agent_name,
        model=model,
        **kwargs
    )

    # Create planner agent with optional shared MCP client
    planner = PlannerAgent(config=config, shared_mcp_client=shared_mcp_client, task_id=task_id)

    # Store sub-agent configurations for use when creating sub-agents
    planner.sub_agent_configs = sub_agent_configs or {
        "information_seeker": {},
        "writer": {}
    }

    return planner

