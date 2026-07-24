# Copyright (c) 2026 South China Sea Institute of Oceanology, Chinese Academy of Sciences (SCSIO, CAS). All rights reserved.
"""
PlannerAgent HTTP Server
鍩轰簬FastAPI瀹炵幇鐨凱lannerAgent鏈嶅姟鍣?紝鎻愪緵RESTful API鎺ュ彛
鏀?寔鍗曟煡璇㈠?鐞嗐€佹壒閲忔煡璇㈠?鐞嗙瓑鍔熻兘
鏈?枃浠堕厤缃?」锛?
	app="a:app",
	host="0.0.0.0",
	port=8000,		# a.py瀵瑰?鎻愪緵鏈嶅姟绔?彛鍙?
	reload=False,
	workers=1
"""
import asyncio
import os
import sys
import time
import json
import uuid
import signal
import multiprocessing as mp
from pathlib import Path
from tempfile import TemporaryDirectory
from concurrent.futures import ProcessPoolExecutor, as_completed, ThreadPoolExecutor

# 銆愰噸瑕併€戝厛璋冩暣Python璺?緞锛屽啀瀵煎叆椤圭洰妯″潡
sys.path.insert(0, str(Path(__file__).parent.parent))  # 娣诲姞 new_deepdiver 鍒拌矾寰?
sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # 娣诲姞椤圭洰鏍圭洰褰曞埌璺?緞

from src.utils.console_encoding import force_utf8_console

force_utf8_console()

# 瀵煎叆鏃ュ織閰嶇疆
from config.logging_config import get_logger, quick_setup

# 瀵煎叆鏍稿績妯″潡
from src.agents.planner_agent import PlannerAgent
from src.agents.base_agent import AgentConfig
from src.tools.mcp_tools import MCPTools
from src.pipeline_v2 import PipelineV2
from src.pipeline_v2.hybrid import build_autonomous_agent_brief, review_revision_loop, seed_hybrid_literature
from src.pipeline_v2.reference_export import write_reference_download_txt
from src.utils.task_manager import task_manager, TaskStatus

# 閰嶇疆鏃ュ織 - 鎹曡幏鎵€鏈夋棩蹇楀埌鏂囦欢
import logging

# 浣跨敤缁濆?璺?緞锛岀‘淇濇棩蹇楀啓鍏ラ」鐩?牴鐩?綍鐨刲ogs鏂囦欢澶?
log_dir = Path(__file__).parent.parent.parent / 'logs'
quick_setup(environment='production', log_dir=str(log_dir))
logger = get_logger(__name__)


def _ensure_dict(value):
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        return {"text": value}
    return {"value": value}


def _pipeline_mode() -> str:
    """Return the sole supported autonomous research workflow.

    The former structured-only and legacy-only branches duplicated state,
    logging, and completion semantics.  Keep the environment variable for
    deployment compatibility, but intentionally converge every request on the
    autonomous ReAct loop plus evidence/review gates.
    """
    value = os.getenv("SCIA_PIPELINE_VERSION", "hybrid").strip().lower()
    if value not in {"", "hybrid", "autonomous", "research"}:
        logger.warning(
            "SCIA_PIPELINE_VERSION=%s is deprecated; using the unified autonomous research workflow",
            value,
        )
    return "hybrid"


def _use_pipeline_v2() -> bool:
    return _pipeline_mode() == "v2"

# 纭?繚绗?笁鏂瑰簱鐨勬棩蹇椾篃鍐欏叆鏂囦欢
logging.getLogger('config.config').setLevel(logging.INFO)
logging.getLogger('faiss.loader').setLevel(logging.INFO)
logging.getLogger('litellm').setLevel(logging.WARNING)

# 瀵煎叆FastAPI鐩稿叧妯″潡
from typing import List, Dict, Optional, Any, cast
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
import uvicorn

# 瀵煎叆鍘熸湁鏍稿績妯″潡
sys.path.insert(0, str(Path(__file__).parent.parent))
from src.agents.planner_agent import PlannerAgent, create_planner_agent
from src.agents.base_agent import AgentConfig
from config.config import get_config
from fastapi.middleware.cors import CORSMiddleware
from typing import cast, Any
# a.py 鏂板?浼氳瘽绠＄悊宸ュ叿绫?
import uuid
from pathlib import Path
from typing import Dict, Optional
# a.py 搴旂敤鍒濆?鍖栨敼閫?
from concurrent.futures import ThreadPoolExecutor
import asyncio
# a.py 瀹氭椂娓呯悊浠诲姟
from fastapi import BackgroundTasks
import time

# 鍏ㄥ眬鍙橀噺
query_history: List[Dict[str, Any]] = []  # 浠呰?褰曟煡璇㈠巻鍙诧紝鏃犱細璇濆叧鑱?
batch_results: Dict[str, Any] = {}
executor = None  # 绾跨▼姹犲皢鍦╨ifespan涓?垵濮嬪寲


# 鏁版嵁妯″瀷锛堣?姹?鍝嶅簲鏍煎紡锛?
class UserFile(BaseModel):
    """Uploaded user file descriptor."""
    file_id: str  # 鏂囦欢ID锛岀敤浜庝粠Flask鍚庣?涓嬭浇鏂囦欢
    filename: str  # 鏂囦欢鍚嶏紝鐢ㄤ簬鏄剧ず鍜屼繚瀛?


class SingleQueryRequest(BaseModel):
    query: str  # 鏌ヨ?鏂囨湰
    taskId: str  # 浠诲姟ID
    user_files: Optional[List[UserFile]] = []  # 寮哄埗浣跨敤鐨勬枃浠跺垪琛?紙鐩存帴涓婁紶锛?
    reference_files: Optional[List[UserFile]] = []  # 鍙?€夊弬鑰冪殑鏂囦欢鍒楄〃锛堜粠鏂囨。搴撻€夋嫨锛?
    use_web_search: bool = True  # 鏄?惁鍚?敤缃戠粶妫€绱?
    prioritize_user_files: bool = True  # 鏄?惁浼樺厛浣跨敤鐢ㄦ埛鏂囦欢
    enable_review: bool = True
    auto_revise: bool = True
    username: Optional[str] = "user"  # 鐢ㄦ埛鍚嶏紝鐢ㄤ簬鐢熸垚鎶ュ憡缃插悕


class BatchQueryRequest(BaseModel):
    queries: List[str]  # 鎵归噺鏌ヨ?鍒楄〃
    max_workers: Optional[int] = None  # 鍙?€夛細鎸囧畾杩涚▼鏁?


class InterventionRequest(BaseModel):
    instruction: str


class AgentSubResponse(BaseModel):
    """Sub-agent response schema."""
    success: bool
    result: Optional[Any] = None
    error: Optional[str] = None
    reasoning_trace: Optional[List[Dict[str, Any]]] = []
    iterations: int
    execution_time: float
    agent_name: str


# 绔犺妭鍐欎綔Agent鐨勫搷搴旓紙鐗规畩澶勭悊锛屽洜鐢盬riter璋冪敤锛?
class SectionWriterSubResponse(BaseModel):
    section_task: Optional[Dict[str, Any]] = None  # 绔犺妭浠诲姟鍙傛暟
    section_result: Optional[Dict[str, Any]] = None  # 绔犺妭鍐欎綔缁撴灉
    execution_time: float = 0.0


# 鏈€缁堟帴鍙ｅ搷搴旀ā鍨?
class QueryResponse(BaseModel):
    # 1. 鍩虹?璇锋眰淇℃伅
    success: bool
    query: str
    timestamp: str
    session_id: str
    task_id: Optional[str] = None  # 鏂板?锛氫换鍔?D鐢ㄤ簬璺熻釜鍜屽彇娑?
    # PlannerAgent淇℃伅
    planner_result: Optional[Dict[str, Any]] = None
    planner_error: Optional[str] = None
    planner_reasoning_trace: List[Dict[str, Any]] = []
    planner_iterations: int = 0
    planner_execution_time: float = 0.0
    planner_agent_name: str = ""

    # 瀛怉gent鍝嶅簲
    section_writer_responses: List[SectionWriterSubResponse] = []
    
    # 鏈€缁堟姤鍛婂唴瀹?
    final_report: Optional[str] = None  # Markdown鏍煎紡鐨勬渶缁堟姤鍛婂唴瀹?
    report_path: Optional[str] = None  # 鎶ュ憡鏂囦欢璺?緞锛堢浉瀵逛簬workspace锛?
    pdf_path: Optional[str] = None
    references_txt_path: Optional[str] = None
    quality_status: Optional[str] = None
    quality_warnings: List[str] = []


class BatchResponse(BaseModel):
    batch_id: str
    status: str  # "processing" 鎴?"completed"
    total_queries: int
    completed_count: int
    results: Optional[List[QueryResponse]] = None


# 鏈嶅姟鍣ㄧ敓鍛藉懆鏈熺?鐞?
@asynccontextmanager
async def lifespan(app: FastAPI):
    """鏈嶅姟鍣ㄥ惎鍔ㄥ拰鍏抽棴鏃剁殑澶勭悊閫昏緫"""
    import random

    # 銆愬?杩涚▼鍏煎?鎬т慨澶嶃€戞坊鍔犻殢鏈哄欢杩燂紝閬垮厤澶氫釜 worker 鍚屾椂鍒濆?鍖?
    # 寤惰繜 0-2 绉掞紝閿欏紑璧勬簮鍒濆?鍖栨椂闂?
    delay = random.uniform(0, 2)
    await asyncio.sleep(delay)

    # 鍚?姩鏃跺垵濮嬪寲鐜??鍙橀噺
    if not os.environ.get('MCP_SERVER_URL'):
        os.environ['MCP_SERVER_URL'] = 'http://localhost:6274/mcp/'
        os.environ['MCP_USE_STDIO'] = 'false'

    # 鍒濆?鍖栧叏灞€绾跨▼姹?
    global executor
    executor = ThreadPoolExecutor(max_workers=8)

    logger.info(f"PlannerAgent server initialized. PID={os.getpid()}, delay={delay:.2f}s")
    yield  # 杩愯?鏈熼棿

    # 鍏抽棴绾跨▼姹?
    logger.info(f"Server shutting down. PID={os.getpid()}")
    if executor:
        executor.shutdown(wait=True)


# 鍒濆?鍖朏astAPI搴旂敤
app = FastAPI(
    title="PlannerAgent Server (Stateless)",
    description="鏃犵姸鎬丳lannerAgent鏈嶅姟鍣?紝鏀?寔骞跺彂鏌ヨ?澶勭悊",
    version="1.0.0",
    lifespan=lifespan
)

# 閰嶇疆璺ㄥ煙
app.add_middleware(
    cast(Any, CORSMiddleware),
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"]
)


def _download_user_files(user_files_data: List[Dict[str, str]], workspace_path: Path) -> None:
    """Download selected user files into the workspace."""
    if not user_files_data:
        return

    mandatory_files = [f for f in user_files_data if f.get("type") == "mandatory"]
    optional_files = [f for f in user_files_data if f.get("type") == "optional"]
    logger.info(
        "Detected %s uploaded files: %s mandatory, %s optional",
        len(user_files_data), len(mandatory_files), len(optional_files)
    )

    try:
        mcp_tools = MCPTools(workspace_path=workspace_path)

        if mandatory_files:
            file_ids = [f["file_id"] for f in mandatory_files]
            logger.info("Downloading mandatory files to user_uploads/: %s", file_ids)
            download_result = mcp_tools.process_user_uploaded_files(
                file_ids=file_ids,
                backend_url="http://localhost:5000",
                target_subdir="user_uploads"
            )
            if download_result.success:
                downloaded_files = download_result.data.get("files", [])
                logger.info("Downloaded %s mandatory files", len(downloaded_files))
                for f in downloaded_files:
                    logger.debug("  - %s -> %s", f.get("filename"), f.get("local_path"))
            else:
                logger.error("Mandatory file download failed: %s", download_result.error)

        if optional_files:
            file_ids = [f["file_id"] for f in optional_files]
            logger.info("Downloading optional files to library_refs/: %s", file_ids)
            download_result = mcp_tools.process_user_uploaded_files(
                file_ids=file_ids,
                backend_url="http://localhost:5000",
                target_subdir="library_refs"
            )
            if download_result.success:
                downloaded_files = download_result.data.get("files", [])
                logger.info("Downloaded %s optional files", len(downloaded_files))
                for f in downloaded_files:
                    logger.debug("  - %s -> %s", f.get("filename"), f.get("local_path"))
            else:
                logger.error("Optional file download failed: %s", download_result.error)
    except Exception as e:
        logger.error("Failed to pre-download user files: %s", e, exc_info=True)


def _build_enhanced_query(query_text: str, user_files_data: List[Dict[str, str]]) -> str:
    """Append uploaded-file context to the user query."""
    if not user_files_data:
        return query_text

    mandatory_files = [f for f in user_files_data if f.get("type") == "mandatory"]
    optional_files = [f for f in user_files_data if f.get("type") == "optional"]
    lines = ["", "[Uploaded files]"]
    if mandatory_files:
        lines.append("Mandatory files in ./user_uploads/:")
        for i, file_info in enumerate(mandatory_files, 1):
            lines.append(f"{i}. ./user_uploads/{file_info['filename']} (file_id: {file_info['file_id']})")
    if optional_files:
        lines.append("Optional reference files in ./library_refs/:")
        for i, file_info in enumerate(optional_files, 1):
            lines.append(f"{i}. ./library_refs/{file_info['filename']} (file_id: {file_info['file_id']})")
    lines.append("Use uploaded files as primary evidence. Do not invent experimental data.")
    return "\n".join(lines) + "\n\n" + query_text


def _pipeline_checkpoint(task_id: str, workspace_path: Path, stage: str, data: Dict[str, Any]) -> List[str]:
    """Publish macro progress; only revision start is a blocking pause point."""
    if stage in {"writer_writing", "revision_round_started"}:
        return task_manager.checkpoint(task_id, stage, data, event_type="checkpoint")
    stage_messages = {
        "visual_plan_ready": "全篇图表与表格规划完成",
        "visual_audit_ready": "图表与正文对应关系检查完成",
    }
    task_manager.update_task_progress(task_id, {"stage": stage, **(data or {})})
    task_manager.record_event(
        task_id,
        "checkpoint",
        stage_messages.get(stage, f"进入阶段：{stage}"),
        {"stage": stage, **(data or {})},
    )
    return []


def _pipeline_activity(task_id: str, stage: str, data: Dict[str, Any]) -> None:
    """Publish non-blocking fine-grained Agent/reviewer activity to the Web UI."""
    reviewer_labels = {
        "methodology": "审稿人1（方法）", "experiment_evidence": "审稿人2（实验证据）",
        "citation": "审稿人3（引用）", "adversarial": "审稿人4（反方）",
    }
    labels = {
        "reviewer_completed": f"{reviewer_labels.get(data.get('role'), data.get('role', 'Reviewer'))}完成",
    }
    if stage == "reviewer_completed":
        detail = data.get("summary") or data.get("decision") or "审稿完成"
        data = {**data, "summary": f"评分={data.get('score')}，结论={data.get('decision')}。{detail}"}
    task_manager.record_event(task_id, "agent_progress", labels.get(stage, stage), {"stage": stage, **data})


def process_single_query(query_data, task_id: Optional[str] = None, username: str = "user",
                         skip_task_creation: bool = False, use_web_search: bool = True,
                         enable_review: bool = True, auto_revise: bool = True):
    """Process one query in an isolated workspace."""
    query_text, query_index, user_files_data = query_data
    process_id = os.getpid()
    if not task_id:
        task_id = f"req_{int(time.time() * 1000)}_{query_index}"  # 生成唯一请求 ID

    # 鍒涘缓骞舵敞鍐屼换鍔★紙濡傛灉灏氭湭鍦ㄨ皟鐢ㄦ柟鍒涘缓锛?
    if not skip_task_creation:
        task_manager.create_task(task_id, query_text)
        task_manager.update_task_status(task_id, TaskStatus.RUNNING)

    # 浣跨敤鎸佷箙鍖栧伐浣滃尯锛堣€岄潪涓存椂鐩?綍锛?
    # 缁熶竴浣跨敤椤圭洰鏍圭洰褰曠殑 workspaces
    current_file = Path(__file__).resolve()  # cli/a.py
    project_root = None

    # 鍚戜笂鏌ユ壘鍖呭惈 app.py 鐨勭洰褰曪紙椤圭洰鏍圭洰褰曪級
    for parent in [current_file.parent] + list(current_file.parents):
        if (parent / "app.py").exists():
            project_root = parent
            break

    # 濡傛灉鎵句笉鍒?app.py锛屼娇鐢ㄥ綋鍓嶅伐浣滅洰褰?
    if project_root is None:
        project_root = Path.cwd()

    # 缁熶竴浣跨敤椤圭洰鏍圭洰褰曚笅鐨?workspaces
    base_workspaces = project_root / "workspaces"
    base_workspaces.mkdir(exist_ok=True, parents=True)

    # 鐢熸垚 session_id锛堜娇鐢?UUID锛?
    session_id = str(uuid.uuid4())
    workspace_path = base_workspaces / session_id
    workspace_path.mkdir(parents=True, exist_ok=True)
    task_manager.set_event_log_path(task_id, str(workspace_path / "workflow_events.jsonl"))

    logger.info(f"[WORKSPACE] session_id: {session_id}")
    logger.info(f"[WORKSPACE] workspace initialized at: {workspace_path.resolve()}")

    # These values must exist before any pipeline stage can fail. Quality-gate
    # failures happen before the normal response-building block, but the Web
    # response should still expose an already generated draft and references.
    failed_report_content = None
    failed_report_path = None
    failed_reference_path = None

    try:
        app_config = get_config()
        sub_agent_configs = {
            "information_seeker": {"model": app_config.model_name},
            "writer": {"model": app_config.model_name}
        }

        # 璁剧疆鐜??鍙橀噺锛岃? Agent 浣跨敤宸插垱寤虹殑 workspace
        os.environ['AGENT_SESSION_ID'] = session_id
        os.environ['AGENT_WORKSPACE_PATH'] = str(workspace_path)

        # Download user files into the workspace before selecting the execution pipeline.
        _download_user_files(user_files_data, workspace_path)

        # Write username into the workspace to avoid cross-request environment conflicts.
        username_file = workspace_path / '.username'
        with open(username_file, 'w', encoding='utf-8') as f:
            f.write(username)

        enhanced_query = _build_enhanced_query(query_text, user_files_data)

        start_time = time.time()
        mode = _pipeline_mode()
        os.environ["SCIA_PIPELINE_VERSION"] = "hybrid"
        # Four independent reviewers replace the obsolete internal reviewer
        # pass, while Planner/InformationSeeker/ExperimentAgent/Writer retain
        # their autonomous iterative behavior.
        os.environ["SKIP_LEGACY_INTERNAL_REVIEW"] = "true"
        logger.info("[PIPELINE] Starting unified autonomous research workflow")
        preparation = PipelineV2(workspace_path).run(
            enhanced_query,
            make_pdf=False,
            plan_only=True,
            use_web_search=False,
            enable_review=False,
            auto_revise=False,
            cancel_check=lambda: task_manager.is_task_cancelled(task_id),
            checkpoint_callback=lambda stage, data: _pipeline_checkpoint(task_id, workspace_path, stage, data),
        )
        if not preparation.success:
            raise RuntimeError("Autonomous research preparation failed: " + str(preparation.error or preparation.warnings))
        # Hybrid preparation may inventory user-provided/local references, but
        # online retrieval belongs exclusively to InformationSeekerAgent after
        # Planner has read the research request and evidence gaps.
        reference_count, literature_warnings = seed_hybrid_literature(
            workspace_path,
            enhanced_query,
            enabled=False,
            checkpoint=lambda stage, data: _pipeline_checkpoint(task_id, workspace_path, stage, data),
        )
        if literature_warnings:
            logger.warning("[RESEARCH] Local evidence preparation warnings: %s", " | ".join(literature_warnings))
        agent_query = build_autonomous_agent_brief(workspace_path, enhanced_query)
        _pipeline_checkpoint(task_id, workspace_path, "agent_loop_started", {
            "structured_reference_count": reference_count,
            "planner_max_iterations": getattr(app_config, "planner_max_iterations", None),
            "information_seeker_max_iterations": getattr(app_config, "information_seeker_max_iterations", None),
            "writer_max_iterations": getattr(app_config, "writer_max_iterations", None),
        })
        agent = create_planner_agent(
            agent_name="PlannerAgent",
            model=app_config.model_name,
            max_iterations=None,
            sub_agent_configs=sub_agent_configs,
            task_id=task_id,
        )
        cancellation_token = task_manager.get_cancellation_token(task_id)
        if cancellation_token:
            agent.set_cancellation_token(cancellation_token)
        response = agent.execute_task(agent_query + " /no_think")
        workflow_warnings: List[str] = []
        if response.success:
            _pipeline_checkpoint(task_id, workspace_path, "agent_loop_ready", {
                "iterations": getattr(response, "iterations", 0),
                "trace_steps": len(getattr(response, "reasoning_trace", []) or []),
            })
            if enable_review:
                _, workflow_warnings = review_revision_loop(
                    workspace_path,
                    enhanced_query,
                    checkpoint=lambda stage, data: _pipeline_checkpoint(task_id, workspace_path, stage, data),
                    auto_revise=auto_revise,
                    activity=lambda stage, data: _pipeline_activity(task_id, stage, data),
                )
                if workflow_warnings:
                    logger.warning("[RESEARCH] %s", " | ".join(workflow_warnings))
        execution_time = time.time() - start_time

        # 妫€鏌ユ槸鍚﹁?鍙栨秷
        if hasattr(response, 'error') and response.error and "cancelled" in str(response.error).lower():
            task_manager.update_task_status(task_id, TaskStatus.CANCELLED, error=response.error)
            raise HTTPException(status_code=499, detail="Task was cancelled by user")

        reference_download_relative_path = None
        if response.success:
            try:
                reference_download_path = write_reference_download_txt(workspace_path)
                reference_download_relative_path = "report/reference_download_list.txt"
                response_result = _ensure_dict(response.result) or {}
                response_result["reference_download_path"] = str(reference_download_path)
                response.result = response_result
                task_manager.record_event(
                    task_id,
                    "artifact_ready",
                    "参考文献下载清单已生成。",
                    {
                        "stage": "reference_download_ready",
                        "path": reference_download_relative_path,
                        "download_url": f"http://127.0.0.1:5000/api/download_references_txt?session_id={session_id}",
                    },
                )
            except Exception as exc:
                logger.warning("Reference TXT export skipped: %s", exc)

        # 璇诲彇鏈€缁堟姤鍛婂唴瀹?
        final_report_content = None
        report_relative_path = None
        pdf_relative_path = None
        try:
            # 灏濊瘯璇诲彇 final_report.md
            final_report_path = workspace_path / "report" / "final_report.md"
            if final_report_path.exists():
                with open(final_report_path, 'r', encoding='utf-8') as f:
                    final_report_content = f.read()
                report_relative_path = "report/final_report.md"
                logger.info("Final report loaded: %s (%s characters)", final_report_path, len(final_report_content))
            else:
                logger.warning("Final report does not exist: %s", final_report_path)
        except Exception as e:
            logger.error("Failed to read final report: %s", e)

        final_pdf_path = workspace_path / "report" / "final_report.pdf"
        if final_pdf_path.is_file():
            pdf_relative_path = "report/final_report.pdf"

        # Quality gates may fail after a complete draft was generated. Keep
        # that failure visible while still returning the inspectable artifacts.
        candidate_report = workspace_path / "report" / "final_report.md"
        if candidate_report.is_file():
            failed_report_content = candidate_report.read_text(encoding="utf-8", errors="ignore")
            failed_report_path = "report/final_report.md"
            try:
                write_reference_download_txt(workspace_path, candidate_report)
                failed_reference_path = "report/reference_download_list.txt"
            except Exception as export_error:
                logger.warning("Could not export references for failed quality gate: %s", export_error)
        quality_status = "not_reviewed" if not enable_review else "passed"
        if workflow_warnings:
            quality_status = "revision_required"
        result_payload = {
            'success': response.success,
            'query': query_text,
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            'session_id': session_id,
            'task_id': task_id,
            'planner_result': _ensure_dict(response.result) if response.success else None,
            'planner_error': response.error if not response.success else None,
            # Detailed traces remain in debug logs.  The Web UI receives only
            # the safe, plain-language SSE activity stream.
            'planner_reasoning_trace': [],
            'planner_iterations': getattr(response, 'iterations', 0),
            'planner_execution_time': execution_time,
            'planner_agent_name': 'AutonomousResearchAgent',
            'section_writer_responses': [],
            'final_report': final_report_content,
            'report_path': report_relative_path,
            'pdf_path': pdf_relative_path,
            'references_txt_path': reference_download_relative_path,
            'quality_status': quality_status,
            'quality_warnings': workflow_warnings,
        }
        if response.success:
            task_manager.update_task_status(task_id, TaskStatus.COMPLETED, result=result_payload)
        else:
            task_manager.update_task_status(
                task_id,
                TaskStatus.FAILED,
                result=result_payload,
                error=str(response.error or "Agent workflow failed"),
            )
        return result_payload
    except Exception as e:
        import traceback
        logger.error(f"Task failed with exception: {e}\n{traceback.format_exc()}")
        candidate_report = workspace_path / "report" / "final_report.md"
        if candidate_report.is_file():
            try:
                failed_report_content = candidate_report.read_text(encoding="utf-8", errors="ignore")
                failed_report_path = "report/final_report.md"
                write_reference_download_txt(workspace_path, candidate_report)
                failed_reference_path = "report/reference_download_list.txt"
            except Exception as artifact_error:
                logger.warning("Could not preserve failed quality-gate artifacts: %s", artifact_error)
        # 杩斿洖绗﹀悎 QueryResponse 妯″瀷鐨勫瓧鍏哥粨鏋?
        failed_pdf_path = "report/final_report.pdf" if (workspace_path / "report" / "final_report.pdf").is_file() else None
        result_payload = {
            'success': False,
            'query': query_text,
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            'session_id': session_id,
            'task_id': task_id,
            'planner_result': None,
            'planner_error': str(e),
            'planner_reasoning_trace': [],
            'planner_iterations': 0,
            'planner_execution_time': 0,
            'planner_agent_name': 'PlannerAgent',
            'section_writer_responses': [],
            'final_report': failed_report_content,
            'report_path': failed_report_path,
            'pdf_path': failed_pdf_path,
            'references_txt_path': failed_reference_path,
            'quality_status': 'failed',
            'quality_warnings': [str(e)],
        }
        if task_manager.is_task_cancelled(task_id):
            task_manager.update_task_status(task_id, TaskStatus.CANCELLED, result=result_payload, error=str(e))
        else:
            task_manager.update_task_status(task_id, TaskStatus.FAILED, result=result_payload, error=str(e))
        return result_payload


# 鎵归噺澶勭悊浠诲姟锛堢敤浜庡悗鍙版墽琛岋級
def process_batch_task(
        queries: List[str],
        max_workers: Optional[int],
        batch_id: str,
        results_store: Dict[str, BatchResponse]
):
    """Process batch queries and store results."""
    # query_data 涓?3 鍏冪粍 (query_text, query_index, user_files_data),涓?process_single_query 鐨勮В鍖呬竴鑷?
    query_data = [(q, idx, []) for idx, q in enumerate(queries)]
    max_workers = max_workers or min(mp.cpu_count(), len(queries), 4)
    results = []

    # 浣跨敤 ThreadPoolExecutor(鑰岄潪 ProcessPoolExecutor):
    # task_manager 鏄?瘡杩涚▼鐙?珛鐨勫崟渚?璺ㄨ繘绋嬫棤娉曞叡浜?换鍔＄姸鎬佷笌鍙栨秷浠ょ墝;
    # 鐢ㄧ嚎绋嬫睜鍙??鎵归噺瀛愪换鍔′笌鍗曟煡璇㈣矾寰勫?浜庡悓涓€杩涚▼,鐘舵€佸彲鏌ヨ?銆佸彲鍙栨秷.
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {
            executor.submit(process_single_query, qd): qd[1] for qd in query_data
        }
        for future in as_completed(future_to_index):
            idx = future_to_index[future]
            try:
                result = future.result()
            except Exception as e:
                result = {
                    'success': False,
                    'query': queries[idx],
                    'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
                    'session_id': '',
                    'task_id': None,
                    'planner_result': None,
                    'planner_error': str(e),
                    'planner_reasoning_trace': [],
                    'planner_iterations': 0,
                    'planner_execution_time': 0,
                    'planner_agent_name': 'PlannerAgent',
                    'section_writer_responses': [],
                    'final_report': None,
                    'report_path': None
                }
            # 璁板綍鍘熷?绱㈠紩鐢ㄤ簬鎺掑簭(缁撴灉 dict 鏈?韩涓嶅惈 query_index)
            results.append((idx, result))

    # 鎸夊師濮嬫煡璇㈤『搴忔帓搴?鍐嶅墺绂荤储寮?
    results.sort(key=lambda pair: pair[0])
    ordered_results = [r for _, r in results]
    results_store[batch_id] = BatchResponse(
        batch_id=batch_id,
        status="completed",
        total_queries=len(queries),
        completed_count=len(ordered_results),
        results=[QueryResponse(**r) for r in ordered_results]
    )


# API绔?偣瀹炵幇
@app.post("/api/query", response_model=QueryResponse, summary="澶勭悊鍗曚釜鏌ヨ?")
async def handle_single_query(request: SingleQueryRequest, fastapi_request: Request):
    """Handle a single query with uploaded files."""

    # 銆愬苟鍙戞帶鍒躲€戞?鏌ュ綋鍓嶈繍琛屼腑鐨剄uery鏁伴噺
    running_count = task_manager.get_running_tasks_count()

    if running_count >= 4:
        # 宸叉湁4涓猶uery杩愯?锛岀?5涓??姹傛嫆缁濇湇鍔?
        raise HTTPException(
            status_code=503,
            detail={
                "code": "SERVICE_BUSY",
                "message": "鎶辨瓑锛屾湇鍔℃殏鏃舵嫢鎸わ紝寤鸿?10鍒嗛挓鍚庡啀灏濊瘯",
                "running_queries": running_count
            }
        )

    # 鐢熸垚鍞?竴浠诲姟ID
    task_id = request.taskId
    loop = asyncio.get_event_loop()

    # 銆愬叧閿?慨澶嶃€戝湪鎻愪氦鍒扮嚎绋嬫睜涔嬪墠灏卞垱寤轰换鍔★紝纭?繚骞跺彂璁℃暟鍙婃椂鐢熸晥
    task_manager.create_task(task_id, request.query)
    task_manager.update_task_status(task_id, TaskStatus.RUNNING)

    # 鍑嗗?寮哄埗浣跨敤鐨勬枃浠舵暟鎹?紙鐩存帴涓婁紶锛?
    user_files_data = []
    if request.user_files and len(request.user_files) > 0:
        for file in request.user_files:
            user_files_data.append({
                'file_id': file.file_id,
                'filename': file.filename,
                'type': 'mandatory'  # 鏍囪?涓哄己鍒朵娇鐢?
            })

    # 鍑嗗?鍙?€夊弬鑰冪殑鏂囦欢鏁版嵁锛堜粠鏂囨。搴撻€夋嫨锛?
    reference_files_data = []
    if request.reference_files and len(request.reference_files) > 0:
        for file in request.reference_files:
            reference_files_data.append({
                'file_id': file.file_id,
                'filename': file.filename,
                'type': 'optional'  # 鏍囪?涓哄彲閫夊弬鑰?
            })

    # 鍚堝苟鎵€鏈夋枃浠舵暟鎹?紝浼犻€掔粰澶勭悊鍑芥暟
    all_files_data = user_files_data + reference_files_data

    # 浣跨敤绾跨▼姹犳墽琛岋紝閬垮厤闃诲?浜嬩欢寰?幆
    if executor is None:
        raise HTTPException(status_code=500, detail="Server executor not initialized")

    # 瀹㈡埛绔?柇寮€妫€娴?
    async def monitor_disconnect():
        """Cancel the task if the client disconnects."""
        try:
            while True:
                await asyncio.sleep(5)
                if await fastapi_request.is_disconnected():
                    logger.warning(f"[DISCONNECT] Client disconnected, cancelling task {task_id}")
                    task_manager.cancel_task(task_id)
                    break
        except asyncio.CancelledError:
            pass  # 浠诲姟姝ｅ父瀹屾垚锛屽彇娑堢洃鎺?
    
    disconnect_monitor = asyncio.create_task(monitor_disconnect())

    try:
        result = await loop.run_in_executor(
            executor,
            lambda: process_single_query((request.query, 0, all_files_data), task_id=task_id, username=request.username or "user",
                                         skip_task_creation=True, use_web_search=request.use_web_search,
                                         enable_review=request.enable_review, auto_revise=request.auto_revise)
        )
        # 璁板綍鍘嗗彶
        query_history.append({
            "task_id": task_id,
            "request_id": result['session_id'],
            "query": request.query,
            "timestamp": result['timestamp'],
            "success": result['success'],
            "user_files_count": len(user_files_data),
            "reference_files_count": len(reference_files_data)
        })
        return result
    finally:
        disconnect_monitor.cancel()  # 浠诲姟缁撴潫锛屽仠姝㈢洃鎺?


@app.post("/api/batch", response_model=BatchResponse, summary="澶勭悊鎵归噺鏌ヨ?")
def handle_batch_query(request: BatchQueryRequest, background_tasks: BackgroundTasks):
    """Handle batch queries asynchronously."""
    if not request.queries:
        raise HTTPException(status_code=400, detail="鎵归噺鏌ヨ?鍒楄〃涓嶈兘涓虹┖")

    # 鐢熸垚鍞?竴鎵规?ID
    batch_id = f"batch_{int(time.time())}"
    # 鍒濆?鍖栨壒娆＄姸鎬?
    batch_results[batch_id] = BatchResponse(
        batch_id=batch_id,
        status="processing",
        total_queries=len(request.queries),
        completed_count=0
    )

    # 灏嗘壒閲忓?鐞嗕换鍔℃坊鍔犲埌鍚庡彴
    background_tasks.add_task(
        process_batch_task,
        queries=request.queries,
        max_workers=request.max_workers,
        batch_id=batch_id,
        results_store=batch_results
    )

    return batch_results[batch_id]


@app.get("/api/batch/{batch_id}", response_model=BatchResponse, summary="Get batch task result")
def get_batch_result(batch_id: str):
    """Return a batch result by ID."""
    if batch_id not in batch_results:
        raise HTTPException(status_code=404, detail="Batch ID not found")
    return batch_results[batch_id]


@app.get("/api/concurrency", summary="Get concurrency status")
async def get_concurrency_status():
    """Return current query concurrency status."""
    running_count = task_manager.get_running_tasks_count()

    if running_count >= 4:
        status = "busy"
    elif running_count >= 3:
        status = "queuing"
    else:
        status = "available"

    return {
        "running_queries": running_count,
        "max_concurrent": 4,
        "status": status
    }


@app.get("/api/status", summary="Get server status")
def get_server_status():
    """Return server status and statistics."""
    running_count = task_manager.get_running_tasks_count()
    return {
        "status": "running",
        "concurrent_workers": executor._max_workers if executor and hasattr(executor, '_max_workers') else 8,
        "query_history_count": len(query_history),
        "active_batch_tasks": sum(1 for res in batch_results.values() if res.status == "processing"),
        "running_queries": running_count,
        "concurrency_status": "busy" if running_count >= 4 else ("queuing" if running_count >= 3 else "available")
    }


@app.get("/api/history", summary="Get query history")
def get_query_history(limit: int = 10):
    """Return recent query history."""
    return {
        "total": len(query_history),
        "history": query_history[-limit:]
    }


@app.post("/api/task/{task_id}/cancel", summary="Cancel task")
async def cancel_task(task_id: str):
    """Cancel a running task."""
    success = task_manager.cancel_task(task_id)

    if not success:
        task_info = task_manager.get_task(task_id)
        if task_info is None:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        else:
            return {
                "success": False,
                "message": "Task is already stopped",
                "task_id": task_id,
                "status": task_info.status.value
            }

    return {
        "success": True,
        "message": "Task cancelled successfully",
        "task_id": task_id
    }


@app.post("/api/task/{task_id}/pause", summary="Pause at the next safe checkpoint")
async def pause_task(task_id: str):
    if not task_manager.request_pause(task_id):
        task = task_manager.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        return {"success": False, "task_id": task_id, "status": task.status.value}
    task_manager.record_event(task_id, "pause_requested", "用户请求暂停，任务将在下一个安全检查点暂停。")
    return {"success": True, "task_id": task_id, "status": "pause_requested"}


@app.post("/api/task/{task_id}/intervention", summary="Add user guidance for remaining stages")
async def add_task_intervention(task_id: str, request: InterventionRequest):
    if not task_manager.add_intervention(task_id, request.instruction):
        raise HTTPException(status_code=400, detail="Task not found or instruction is empty")
    target_stage = task_manager.classify_stage_directive(request.instruction)
    stage_label = {"experiment": "实验", "writing": "写作", "review": "审稿"}.get(target_stage, target_stage)
    message = (
        f"已收到流程切换指导，将在当前安全步骤完成后进入{stage_label}阶段。"
        if target_stage else
        "已收到用户指导，将在当前工具完成后的安全检查点立即交给正在工作的智能体。"
    )
    task_manager.record_event(
        task_id, "intervention", message,
        {"instruction": request.instruction, "target_stage": target_stage},
    )
    return {"success": True, "task_id": task_id, "target_stage": target_stage}


@app.post("/api/task/{task_id}/resume", summary="Resume a paused task")
async def resume_task(task_id: str):
    if not task_manager.resume_task(task_id):
        task = task_manager.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        return {"success": False, "task_id": task_id, "status": task.status.value}
    task_manager.record_event(task_id, "resumed", "用户已继续任务。")
    return {"success": True, "task_id": task_id, "status": "resuming"}


@app.get("/api/task/{task_id}/events", summary="Stream structured workflow events")
async def stream_task_events(task_id: str, request: Request, after_id: int = 0):
    if task_manager.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    async def event_stream():
        header_id = request.headers.get("last-event-id", "")
        last_id = max(after_id, int(header_id) if header_id.isdigit() else 0)
        yield "retry: 1500\n\n"
        while True:
            events = task_manager.get_events(task_id, last_id)
            for event in events:
                last_id = event["id"]
                yield f"id: {last_id}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            task = task_manager.get_task(task_id)
            if task is None or task.status in {TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED}:
                yield f"event: done\ndata: {json.dumps({'status': task.status.value if task else 'missing'})}\n\n"
                break
            yield ": keepalive\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/task/{task_id}", summary="Get task status")
async def get_task_status(task_id: str):
    """Return task status and progress."""
    task_info = task_manager.get_task(task_id)

    if task_info is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    result = task_info.result if isinstance(task_info.result, dict) else {}
    artifacts = {
        "md": bool(result.get("report_path")),
        "pdf": bool(result.get("pdf_path")),
        "references": bool(result.get("references_txt_path")),
    }
    return {
        "task_id": task_info.task_id,
        "query": task_info.query,
        "status": task_info.status.value,
        "created_at": task_info.created_at,
        "updated_at": task_info.updated_at,
        "progress": task_info.progress,
        "error": task_info.error,
        "has_result": task_info.result is not None,
        "session_id": result.get("session_id"),
        "quality_status": result.get("quality_status"),
        "quality_warnings": result.get("quality_warnings", []),
        "artifacts": artifacts,
    }


@app.get("/api/task/{task_id}/artifact/{artifact_kind}", summary="Download a task artifact")
async def download_task_artifact(task_id: str, artifact_kind: str):
    task_info = task_manager.get_task(task_id)
    if task_info is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    result = task_info.result if isinstance(task_info.result, dict) else {}
    session_id = str(result.get("session_id") or "").strip()
    artifact_specs = {
        "md": ("report_path", "final_report.md", "text/markdown; charset=utf-8"),
        "pdf": ("pdf_path", "final_report.pdf", "application/pdf"),
        "references": ("references_txt_path", "reference_download_list.txt", "text/plain; charset=utf-8"),
    }
    if artifact_kind not in artifact_specs:
        raise HTTPException(status_code=404, detail="Unknown artifact type")
    result_key, download_name, media_type = artifact_specs[artifact_kind]
    relative_path = str(result.get(result_key) or "").strip()
    if not session_id or not relative_path:
        raise HTTPException(status_code=404, detail="Artifact is not ready")

    project_root = Path(__file__).resolve().parents[2]
    workspace = (project_root / "workspaces" / session_id).resolve()
    artifact_path = (workspace / relative_path).resolve()
    try:
        artifact_path.relative_to(workspace)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid artifact path") from exc
    if not artifact_path.is_file():
        raise HTTPException(status_code=404, detail="Artifact file does not exist")
    return FileResponse(artifact_path, media_type=media_type, filename=download_name)


@app.get("/api/tasks", summary="Get all tasks")
async def get_all_tasks():
    """Return all tasks."""
    tasks = task_manager.get_all_tasks()
    running_count = task_manager.get_running_tasks_count()

    return {
        "total_tasks": len(tasks),
        "running_tasks": running_count,
        "tasks": list(tasks.values())
    }


@app.delete("/api/tasks/cleanup", summary="Clean completed tasks")
async def cleanup_old_tasks(max_age_seconds: int = 3600):
    """Clean completed, cancelled, or failed tasks."""
    task_manager.cleanup_completed_tasks(max_age_seconds)

    return {
        "success": True,
        "message": f"Cleaned up tasks older than {max_age_seconds} seconds"
    }


if __name__ == "__main__":
    # 銆愪慨澶?SIGHUP 瀵艰嚧鐨勮繘绋嬮噸鍚?棶棰樸€?
    # 蹇界暐 SIGHUP 淇″彿锛岄槻姝?SSH 鏂?紑鎴栫粓绔?叧闂?椂瑙﹀彂 uvicorn 閲嶅惎
    # SIGHUP 浠呭湪 Unix 绯荤粺涓婂彲鐢?
    if hasattr(signal, 'SIGHUP'):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

    # 鍚?姩UVicorn鏈嶅姟鍣?紙浣跨敤褰撳墠鏂囦欢鍚嶄綔涓烘ā鍧楋級
    uvicorn.run(
        app="a:app",  # 鍥犱负鏂囦欢鍚嶄负a.py锛屾墍浠ユā鍧楀悕涓篴
        host="0.0.0.0",
        port=8000,
        reload=False,
        workers=1,  # 寤鸿?鍚?姩1涓獁orker杩涚▼锛岄伩鍏嶅?杩涚▼涓嬪嚭鐜版暟鎹?笉涓€鑷撮棶棰橈紝宸蹭娇鐢ㄧ嚎绋嬫睜鎶€鏈?疄鐜板?骞跺彂
        access_log=False
    )

