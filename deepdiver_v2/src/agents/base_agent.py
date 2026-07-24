# Copyright (c) 2025 Huawei Technologies Co., Ltd. All rights reserved.
# Copyright (c) 2026 South China Sea Institute of Oceanology, Chinese Academy of Sciences (SCSIO, CAS). All rights reserved.
import json
import logging
import time
import os
import sys
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, Any, List, Optional, Union
from dataclasses import dataclass, field
from pathlib import Path
import re
import threading
import hashlib

logger = logging.getLogger(__name__)

# Import MCP client instead of direct tools
try:
    from ..tools import mcp_client as _mcp_client_module  # noqa: F401
    MCP_CLIENT_AVAILABLE = True
except ImportError:
    MCP_CLIENT_AVAILABLE = False


@dataclass
class AgentConfig:
    """Configuration for agents - session management handled entirely by MCP server"""
    agent_name: str = "base_agent"
    planner_mode: str = "auto"
    model: Optional[str] = None
    max_iterations: int = 10
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    # Paths used by writer and other agents
    trajectory_storage_path: Optional[str] = None
    report_output_path: Optional[str] = None
    document_analysis_path: Optional[str] = None


@dataclass
class AgentResponse:
    """Standardized response format for all agents"""
    success: bool
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    iterations: int = 0
    reasoning_trace: List[Dict[str, Any]] = field(default_factory=list)
    agent_name: str = ""
    execution_time: float = 0.0


@dataclass
class TaskInput:
    """Standardized task input format for all agents"""
    task_content: str                                    # The specific task content
    task_steps_for_reference: Optional[str] = None       # Reference steps for execution
    deliverable_contents: Optional[str] = None           # Format of final deliverable
    current_task_status: Optional[str] = None            # Description of current task status
    task_executor: str = "info_seeker"                  # Name of task executor (info_seeker, writer)
    workspace_id: Optional[str] = None                   # Workspace ID for stored files and memory
    acceptance_checking_criteria: Optional[str] = None   # Criteria for determining task completion and quality
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert TaskInput to dictionary format"""
        return {
            "task_content": self.task_content,
            "task_steps_for_reference": self.task_steps_for_reference,
            "deliverable_contents": self.deliverable_contents,
            "current_task_status": self.current_task_status,
            "task_executor": self.task_executor,
            "workspace_id": self.workspace_id,
            "acceptance_checking_criteria": self.acceptance_checking_criteria
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TaskInput':
        """Create TaskInput from dictionary"""
        return cls(
            task_content=data.get("task_content", ""),
            task_steps_for_reference=data.get("task_steps_for_reference"),
            deliverable_contents=data.get("deliverable_contents"),
            current_task_status=data.get("current_task_status"),
            task_executor=data.get("task_executor", "info_seeker"),
            workspace_id=data.get("workspace_id"),
            acceptance_checking_criteria=data.get("acceptance_checking_criteria")
        )
    
    def format_for_prompt(self) -> str:
        """Format the task input for use in prompts"""
        prompt = f"Task Content:\n{self.task_content}\n\n"
        
        if self.task_steps_for_reference:
            prompt += f"Task Steps for Reference:\n{self.task_steps_for_reference}\n\n"
        
        if self.deliverable_contents:
            prompt += f"Deliverable Contents:\n{self.deliverable_contents}\n\n"
        
        if self.current_task_status:
            prompt += f"Current Task Status:\n{self.current_task_status}\n\n"
        
        if self.acceptance_checking_criteria:
            prompt += f"Acceptance Checking Criteria:\n{self.acceptance_checking_criteria}\n\n"
        
        prompt += f"Task Executor: {self.task_executor}\n"
        
        if self.workspace_id:
            prompt += f"Workspace ID: {self.workspace_id}\n"
        
        return prompt


class SectionWriterTaskInput(TaskInput):
    """
    Specialized TaskInput for section writing tasks

    Only stores the essential parameters. The section_writer agent
    will handle prompt assembly internally.
    """

    def __init__(
        self,
        task_content: str,
        user_query: str,
        write_file_path: str,
        overall_outline: str,
        current_chapter_outline: str,
        key_files: List[Dict[str, Any]],
        written_chapters: str = "",
        workspace_id: Optional[str] = None
    ):
        # Store the section writer specific parameters
        self.write_file_path = write_file_path
        self.user_query = user_query
        self.current_chapter_outline = current_chapter_outline
        self.key_files = key_files
        self.written_chapters = written_chapters
        self.overall_outline = overall_outline

        # Initialize parent TaskInput with minimal required fields
        super().__init__(
            task_content=task_content,
            task_executor="section_writer",
            workspace_id=workspace_id,
        )


class WriterAgentTaskInput(TaskInput):
    """
    Specialized TaskInput for section writing tasks

    Only stores the 4 essential parameters. The section_writer agent
    will handle prompt assembly internally.
    """

    def __init__(
        self,
        task_content: str,
        user_query: str,
        key_files: List[Dict[str, Any]],
        workspace_id: Optional[str] = None
    ):
        # Store the section writer specific parameters
        self.user_query = user_query
        self.key_files = key_files

        # Initialize parent TaskInput with minimal required fields
        super().__init__(
            task_content=task_content,
            task_executor="writer_agent",
            workspace_id=workspace_id,
        )


class BaseAgent(ABC):
    """
    Base class for all agents with MCP server-managed sessions.
    
    Session management is now entirely handled by the MCP server:
    - Server assigns session IDs on connection
    - Server creates workspace folders with UUID names
    - All tool operations are performed in server-managed workspaces
    """
    
    _tool_cache: Dict[str, Dict[str, Any]] = {}
    _tool_cache_lock = threading.Lock()
    _cacheable_research_tools = {
        "academic_search", "arxiv_search", "search_pubmed_key_words",
        "search_pubmed_advanced", "get_pubmed_article", "medrxiv_search",
        "batch_web_search",
    }

    @staticmethod
    def _humanize_progress_text(text: str, fallback: str = "我正在确定下一步工作。") -> str:
        """Convert model/tool protocol text into a short user-facing work summary."""
        value = str(text or "").strip()
        if not value:
            return fallback
        lowered = value.lower()
        if ('"name"' in value and '"arguments"' in value) or "[unused11]" in lowered or "<tool_call>" in lowered:
            if "file_read" in lowered:
                labels = []
                file_labels = {
                    "workspace_digest": "工作区文件概要", "research_contract": "研究目标",
                    "claims_evidence": "论点与证据关系", "paper_outline": "论文提纲",
                    "experiment_registry": "实验记录", "figure_plans": "图表规划",
                    "references": "文献记录",
                }
                for marker, label in file_labels.items():
                    if marker in lowered and label not in labels:
                        labels.append(label)
                return "我正在读取" + ("、".join(labels) if labels else "相关资料") + "，为下一步规划做准备。"
            if "assign_multi" in lowered and "info_seeker" in lowered:
                return "我正在安排文献检索任务，准备补充论文需要的依据。"
            if "section_writer" in lowered:
                return "我正在安排下一部分论文内容的撰写。"
            if "experiment" in lowered:
                return "我正在安排实验数据分析和图表整理。"
            return fallback
        value = re.sub(r"\[unused\d+\]", " ", value)
        value = re.sub(r"```(?:json)?[\s\S]*?```", " ", value, flags=re.IGNORECASE)
        value = re.sub(r"\s+", " ", value).strip()
        return value[:500] if value else fallback

    def __init__(self, config: AgentConfig, shared_mcp_client=None):
        self.execution_stats = None
        self.reasoning_trace = None
        self.config = config
        self.logger = logging.getLogger(f"{__name__}.{config.agent_name}")
        self._cancellation_token = None
        
        # Session info is populated by the MCP server
        self.session_info = None
        
        # Tool management
        self.mcp_tools = None
        self.available_tools = {}
        self._tool_failure_streaks: Dict[str, int] = {}

        self.reset_trace()
        
        # Initialize MCP tools (server will handle session creation or use shared client)
        self._initialize(shared_mcp_client)
    
    def _initialize(self, shared_mcp_client=None):
        """Initialize agent with MCP server connection or shared client"""
        try:
            self.logger.info(f"Initializing agent {self.config.agent_name}")
            
            if shared_mcp_client:
                # Use shared MCP client with agent-specific tool filtering
                agent_type = self._get_agent_type()
                self.mcp_tools = self._create_filtered_mcp_tools(shared_mcp_client, agent_type)
                self.logger.info(f"Agent {self.config.agent_name} using shared MCP client with {agent_type} tools")
            else:
                # Create MCP tools with agent-specific filtering (no more unfiltered access)
                self.mcp_tools = self._create_filtered_mcp_tools_standalone()
            
            # Discover available tools
            self.available_tools = self._discover_mcp_tools()
            
            # Build tool schemas for function calling
            self.tool_schemas = self._build_tool_schemas()
            
            self.logger.info(f"Agent {self.config.agent_name} initialized successfully")
            self.logger.info(f"Available tools: {list(self.available_tools.keys())}")
            
        except Exception as e:
            self.logger.error(f"Failed to initialize agent {self.config.agent_name}: {e}")
            raise

    def set_cancellation_token(self, cancellation_token):
        """Set the cancellation token for this agent.
        All sub-agents (Experiment, Writer, InfoSeeker) inherit this.
        Args:
            cancellation_token: threading.Event that is set when task should cancel
        """
        self._cancellation_token = cancellation_token

    def _check_cancellation(self) -> bool:
        """Check if task has been cancelled.
        Returns:
            True if task should be cancelled, False otherwise
        """
        if self._cancellation_token and self._cancellation_token.is_set():
            self.logger.info(f"Task cancellation detected in {self.config.agent_name}")
            return True
        return False

    def _agent_intervention_checkpoint(
            self, stage: str, iteration: int = 0, tool_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Apply pause/guidance at a small safe unit, never in the middle of an external call."""
        task_id = getattr(self, "task_id", None)
        if not task_id:
            return {"instructions": [], "requested_stage": None}
        try:
            from src.utils.task_manager import task_manager
            data = {
                "agent": self.config.agent_name,
                "iteration": iteration,
            }
            if tool_name:
                data["tool"] = tool_name
            instructions = task_manager.checkpoint(
                task_id, stage, data, event_type="agent_checkpoint"
            )
            requested = task_manager.peek_requested_stage(task_id)
            return {"instructions": instructions, "requested_stage": requested}
        except Exception as exc:
            self.logger.debug("Agent checkpoint unavailable at %s: %s", stage, exc)
            return {"instructions": [], "requested_stage": None}

    @staticmethod
    def _intervention_message(instructions: List[str], requested_stage: Optional[Dict[str, str]] = None) -> str:
        parts = []
        if instructions:
            parts.append("用户在当前安全步骤补充了指导，立即应用到本轮剩余工作：\n- " + "\n- ".join(instructions))
        if requested_stage:
            parts.append(
                "用户发出了必须执行的流程切换指令：完成当前安全步骤后停止本阶段，"
                f"下一阶段转到 {requested_stage.get('stage')}。不要继续扩展当前阶段。"
            )
        return "\n\n".join(parts) + (" /no_think" if parts else "")

    def _discover_mcp_tools(self) -> Dict[str, Any]:
        """Discover available tools from MCP server or fallback tools"""
        available_tools = {}
        
        # Try to get tools from MCP client first
        if hasattr(self.mcp_tools, 'get_available_tools'):
            try:
                mcp_tools_dict = self.mcp_tools.get_available_tools()
                for tool_name, tool_info in mcp_tools_dict.items():
                    # For proper MCP architecture, store tool info for direct client calls
                    # instead of creating wrapper lambda functions
                    available_tools[tool_name] = tool_info
                
                if available_tools:
                    self.logger.info(f"Discovered {len(available_tools)} tools from MCP server")
                    return available_tools
            except Exception as e:
                self.logger.warning(f"Failed to discover MCP tools: {e}")
        
        # Fallback: if MCP client not available, use direct method access
        # This should rarely be needed with proper MCP setup
        if hasattr(self.mcp_tools, '__dict__'):
            for attr_name in dir(self.mcp_tools):
                if not attr_name.startswith('_') and callable(getattr(self.mcp_tools, attr_name)):
                    available_tools[attr_name] = getattr(self.mcp_tools, attr_name)
        
        return available_tools
    
    def _get_agent_type(self) -> str:
        """Get agent type for tool filtering"""
        agent_name = self.config.agent_name.lower()
        if "planner" in agent_name:
            return "planner"
        elif "information" in agent_name or "seeker" in agent_name:
            return "information_seeker"
        elif "writer" in agent_name:
            return "writer"
        elif "experiment" in agent_name:
            return "experimenter"  # 🚀 新增:识别实验智能体,以便后续精确分配代码执行工具
        else:
            # Default to planner tools for unknown agent types
            return "planner"
    
    def _create_filtered_mcp_tools(self, shared_client, agent_type: str):
        """Create filtered MCP tools adapter using shared client"""
        try:
            from src.tools.mcp_client import create_filtered_mcp_tools_adapter
            return create_filtered_mcp_tools_adapter(shared_client, agent_type)
        except ImportError:
            # Fallback if FilteredMCPToolsAdapter not available
            self.logger.warning("FilteredMCPToolsAdapter not available, using regular adapter")
            from src.tools.mcp_client import MCPToolsAdapter
            adapter = MCPToolsAdapter.__new__(MCPToolsAdapter)
            adapter.client = shared_client
            return adapter
    
    def _create_filtered_mcp_tools_standalone(self):
        """Create filtered MCP tools adapter with its own client connection"""
        try:
            # Get agent type for filtering
            agent_type = self._get_agent_type()
            
            # Create a new MCP client
            client = self._create_new_mcp_client()
            
            # Apply filtering based on agent type
            from src.tools.mcp_client import create_filtered_mcp_tools_adapter
            filtered_adapter = create_filtered_mcp_tools_adapter(client, agent_type)
            
            self.logger.info(f"Agent {self.config.agent_name} created filtered MCP adapter with {agent_type} tools")
            return filtered_adapter
            
        except Exception as e:
            self.logger.error(f"Failed to create filtered MCP tools: {e}")
            raise RuntimeError(f"Failed to create filtered MCP client for {self.config.agent_name}: {e}")
    
    def _create_new_mcp_client(self):
        """Create a new MCP client connection"""
        try:
            # Get MCP configuration
            from config.config import get_mcp_config
            mcp_config = get_mcp_config()
            
            # Create MCP client
            from src.tools.mcp_client import MCPClient
            
            if mcp_config.get("server_url") and not mcp_config.get("use_stdio", True):
                # HTTP-based MCP server
                client = MCPClient(server_url=mcp_config["server_url"])
                self.logger.info(
                    f"Agent {self.config.agent_name} connected to HTTP MCP server: {mcp_config['server_url']}")
            else:
                # Default to the expected HTTP MCP server on port 6274
                client = MCPClient(server_url="http://localhost:6274/mcp")
                self.logger.info(
                    f"Agent {self.config.agent_name} connected to default HTTP MCP server: http://localhost:6274/mcp")
                
            return client
                
        except Exception as e:
            self.logger.error(f"Failed to create MCP client: {e}")
            raise RuntimeError(f"MCP client creation failed for {self.config.agent_name}: {e}")
        
    # NOTE: _create_mcp_tools() method removed to prevent unfiltered tool access.
    # All agents now use _create_filtered_mcp_tools_standalone() or _create_filtered_mcp_tools() 
    # to ensure proper tool isolation and security.
    
    def get_session_info(self) -> Optional[Dict[str, Any]]:
        """Get information about the current server-managed session"""
        try:
            # First check environment variables (set by cli/a.py)
            env_session_id = os.environ.get('AGENT_SESSION_ID')
            env_workspace_path = os.environ.get('AGENT_WORKSPACE_PATH')
            
            if env_session_id and env_workspace_path:
                return {
                    "session_id": env_session_id,
                    "workspace_path": env_workspace_path,
                    "server_managed": True,
                    "agent_name": self.config.agent_name,
                    "source": "environment"
                }
            
            # Then try the adapter's get_session_info method if available
            if hasattr(self.mcp_tools, 'get_session_info'):
                session_info = self.mcp_tools.get_session_info()
                if session_info:
                    # Add agent-specific information
                    session_info.update({
                        "server_managed": True,
                        "agent_name": self.config.agent_name
                    })
                    return session_info
            
            # Fallback: Check if we have an MCP tools adapter with a client
            if hasattr(self.mcp_tools, 'client'):
                client = self.mcp_tools.client
                
                # Check if client has session ID and connection status
                if hasattr(client, '_session_id') and hasattr(client, 'is_connected'):
                    return {
                        "session_id": client._session_id,
                        "server_managed": True,
                        "agent_name": self.config.agent_name,
                        "connected": client.is_connected()
                    }
            
            # Fallback: check if mcp_tools has session info directly
            if hasattr(self.mcp_tools, '_session_id'):
                return {
                    "session_id": self.mcp_tools._session_id,
                    "server_managed": True,
                    "agent_name": self.config.agent_name,
                    "connected": getattr(self.mcp_tools, 'is_connected', lambda: True)()
                }
            
            # If no session info available, return basic info
            return {
                "session_id": None,
                "server_managed": True,
                "agent_name": self.config.agent_name,
                "connected": hasattr(self.mcp_tools, 'client') and getattr(self.mcp_tools.client, 'is_connected',
                                                                           lambda: False)()
            }
            
        except Exception as e:
            self.logger.warning(f"Failed to get session info: {e}")
            return {
                "session_id": None,
                "server_managed": True,
                "agent_name": self.config.agent_name,
                "connected": False,
                "error": str(e)
            }
    
    def _build_tool_schemas(self) -> List[Dict[str, Any]]:
        """Build tool schemas for function calling"""
        schemas = []
        
        # Get agent-specific tool schemas
        agent_schemas = self._build_agent_specific_tool_schemas()
        schemas.extend(agent_schemas)
        
        return schemas
    
    def _build_agent_specific_tool_schemas(self) -> List[Dict[str, Any]]:
        """
        Build agent-specific tool schemas using proper MCP architecture.
        Schemas come from MCP server via client, not direct imports.
        """
        schemas = []
        
        # Proper MCP way: Get schemas from MCP client (which got them from server)
        try:
            if hasattr(self.mcp_tools, 'get_tool_schemas'):
                # Use the MCP client to get schemas (proper MCP architecture)
                schemas = self.mcp_tools.get_tool_schemas()
                self.logger.info(f"Retrieved {len(schemas)} tool schemas from MCP server")
            else:
                # Fallback for adapters that don't have the new method yet
                self.logger.warning("MCP adapter doesn't support get_tool_schemas, using fallback")
                schemas = self._build_fallback_schemas()
        except Exception as e:
            self.logger.warning(f"Failed to get schemas from MCP client: {e}, using fallback")
            schemas = self._build_fallback_schemas()
        
        return schemas
    
    def _build_fallback_schemas(self) -> List[Dict[str, Any]]:
        """Fallback schema building if MCP client method fails"""
        schemas = []
        
        # Try to get tool info from MCP client
        if hasattr(self.mcp_tools, 'get_available_tools'):
            try:
                available_tools = self.mcp_tools.get_available_tools()
                for tool_name, tool_info in available_tools.items():
                    schema = {
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "description": getattr(tool_info, 'description', f"Tool: {tool_name}"),
                            "parameters": getattr(tool_info, 'input_schema', {"type": "object", "properties": {}, "required": []})
                        }
                    }
                    schemas.append(schema)
                self.logger.info(f"Built {len(schemas)} schemas using fallback method")
            except Exception as e:
                self.logger.warning(f"Fallback schema building failed: {e}")
        
        return schemas

    def _safe_extract_reasoning(self, content: str) -> str:
        """安全提取思考内容,防止 split 溢出"""
        if not content:
            return ""
        try:
            import re
            if "[unused16]" in content and "[unused17]" in content:
                return content.split("[unused16]")[-1].split("[unused17]")[0].strip()
            # 兜底:如果标签缺失,正则清理所有控制标签并返回前300字
            clean_text = re.sub(r'\[unused\d+\]', '', content).strip()
            return clean_text[:300]
        except Exception:
            return ""

    def _get_llm_response_content(self, response: Any) -> str:
        """
        安全地从大模型响应中提取内容,防止 KeyError: 'choices'
        """
        if response is None:
            return "[unused16][unused17] 错误:模型响应为空,请检查网络或配置."

        # 1. 处理字典格式的响应 (requests 直接返回的情况)
        if isinstance(response, dict):
            if 'choices' in response and len(response['choices']) > 0:
                message = response['choices'][0].get('message', {})
                # 兼容不同模型的字段名
                content = message.get('content') or message.get('reasoning_content')
                return content if content else ""

            # 如果没有 choices 键,提取错误信息
            error_info = response.get('error', '未知 API 错误')
            self.logger.error(f"API 返回异常: {response}")
            return f"[unused16][unused17] 我的上一次请求失败了,API 报错:{error_info}.可能是因为我读取的文件(如PDF)导致内容超限,我应该换个方式尝试."

        # 2. 处理 litellm 对象格式的响应
        try:
            if hasattr(response, 'choices') and len(response.choices) > 0:
                return response.choices[0].message.content
        except Exception as e:
            self.logger.error(f"解析响应对象失败: {e}")

        return "[unused16][unused17] 错误:无法识别的响应格式."

    def extract_tool_calls(self, content: str) -> List[Dict[str, Any]]:
        """【深度加固版】从模型回复中提取并解析工具调用 JSON,具备极强的容错和暴力提取能力"""
        if not content or not isinstance(content, str):
            return []

        # 1. 提取所有被 [unused11] 和 [unused12] 包裹的内容
        # matches = re.findall(r"\[unused11\]([\s\S]*?)\[unused12\]", content)
        matches = re.findall(r"\[unused11\]([\s\S]*?)\[unused12\]", content)
        matches += re.findall(r"<tool_call>\s*([\s\S]*?)\s*</tool_call>", content, flags=re.IGNORECASE)
        loose_tool_calls = self._extract_loose_tool_calls(content)
        if not matches:
            code_blocks = re.findall(r"```(?:json)?\s*([\s\S]*?)\s*```", content, flags=re.IGNORECASE)
            for block in code_blocks:
                candidate = block.strip()
                if '"name"' in candidate and '"arguments"' in candidate:
                    matches.append(candidate)
        if not matches:
            stripped = content.strip()
            if (
                (stripped.startswith("[") or stripped.startswith("{"))
                and '"name"' in stripped
                and '"arguments"' in stripped
            ):
                matches.append(stripped)
        if not matches and not loose_tool_calls:
            return []

        all_tool_calls = list(loose_tool_calls)
        for raw_json in matches:
            raw_json = raw_json.strip()
            if not raw_json: continue

            try:
                # 尝试标准解析
                parsed = json.loads(raw_json)
                all_tool_calls.extend(parsed if isinstance(parsed, list) else [parsed])
            except json.JSONDecodeError:
                # Some providers concatenate a valid tool-call JSON value with
                # a second reasoning/tool block.  json.loads rejects the whole
                # string as "Extra data" and the old regex fallback could then
                # combine the first tool name with arguments from the later
                # block.  Decode only the first complete JSON value instead.
                try:
                    parsed, end = json.JSONDecoder().raw_decode(raw_json)
                    if isinstance(parsed, (dict, list)):
                        all_tool_calls.extend(parsed if isinstance(parsed, list) else [parsed])
                        trailing = raw_json[end:].strip()
                        if trailing:
                            self.logger.warning(
                                "Recovered the first complete tool-call JSON and ignored %s trailing characters",
                                len(trailing),
                            )
                        continue
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass

                # 🛠️ 策略 A: 软修复(处理换行、Tab、未转义引号)
                try:
                    fixed_json = raw_json.replace('\n', '\\n').replace('\r', '').replace('\t', '\\t')
                    # 处理最常见的引号嵌套错误:把字段值内部的引号转义
                    # 仅针对常见的大型文本字段:thought, task_content, reflect, current_chapter_outline
                    parsed = json.loads(fixed_json)
                    all_tool_calls.extend(parsed if isinstance(parsed, list) else [parsed])
                except Exception:
                    # Some OpenAI-compatible providers occasionally double
                    # every structural quote: [{""name"": ...}]. Repair only
                    # when that unmistakable signature is present, so normal
                    # quoted prose is never rewritten.
                    if re.search(r'[\[{]\s*""name""\s*:', raw_json):
                        try:
                            dedoubled_json = raw_json.replace('""', '"')
                            parsed = json.loads(dedoubled_json)
                            all_tool_calls.extend(parsed if isinstance(parsed, list) else [parsed])
                            self.logger.info("Recovered a tool call with doubled structural JSON quotes")
                            continue
                        except Exception:
                            pass
                    # Another common provider defect is an unescaped JSON
                    # object placed inside a quoted `arguments` value. Recover
                    # the inner object before falling back to model retry.
                    quoted_name = re.search(r'"name"\s*:\s*"([^"\r\n]+)"', raw_json)
                    quoted_arguments = re.search(
                        r'"arguments"\s*:\s*"(\{[\s\S]*\})"\s*\}?\s*\]?\s*$',
                        raw_json,
                    )
                    if quoted_name and quoted_arguments:
                        candidate_arguments = quoted_arguments.group(1).replace('\\"', '"')
                        try:
                            parsed_arguments = json.loads(candidate_arguments)
                            if isinstance(parsed_arguments, dict):
                                all_tool_calls.append({
                                    "name": quoted_name.group(1),
                                    "arguments": parsed_arguments,
                                })
                                self.logger.info("Recovered a tool call with a quoted arguments object")
                                continue
                        except Exception:
                            pass
                    # 🛠️ 策略 B: 🚀 物理暴力拆解(即便 JSON 格式全烂了,只要能看到工具名和内容就救回来)
                    name_match = re.search(r'"name"\s*:\s*"([^"]+)"', raw_json)
                    if name_match:
                        tool_name = name_match.group(1)
                        if tool_name in {
                            "assign_multi_subjective_tasks_to_info_seeker",
                            "assign_multi_objective_tasks_to_info_seeker",
                        }:
                            recovered_tasks = self._recover_task_list_from_broken_json(raw_json)
                            if recovered_tasks:
                                all_tool_calls.append({"name": tool_name, "arguments": {"tasks": recovered_tasks}})
                                self.logger.info(f"🛡️ 触发【多任务参数恢复】,成功救回工具调用: {tool_name}")
                                continue

                        # 识别可能包含长文本的字段键名
                        long_text_keys = [
                            "thought", "task_content", "reflect", "current_chapter_outline", "tasks",
                            "task_summary", "completion_status", "key_findings", "sources_used",
                            "key_files", "final_article_path", "experimental_metrics", "validation_notes"
                        ]

                        args_dict = {}
                        for key in long_text_keys:
                            # 暴力正则:匹配 "key": " 到 最后的 " 之前的内容
                            val_match = re.search(rf'"{key}"\s*:\s*"(.*)', raw_json, re.DOTALL)
                            if val_match:
                                # 截断逻辑:找到最后可能的闭合标记
                                val = val_match.group(1)
                                if val.endswith('"}'):
                                    val = val[:-2]
                                elif val.endswith('"}]'):
                                    val = val[:-3]
                                elif val.endswith('"'):
                                    val = val[:-1]
                                args_dict[key] = val

                        done_tools = {
                            "info_seeker_subjective_task_done",
                            "info_seeker_objective_task_done",
                            "writer_subjective_task_done",
                            "planner_subjective_task_done",
                            "planner_objective_task_done",
                            "experiment_task_done",
                            "section_writer_task_done",
                        }
                        if tool_name in done_tools and not args_dict:
                            text = re.sub(r"\s+", " ", raw_json).strip()
                            args_dict = {
                                "task_summary": text[:2000],
                                "completion_status": "completed_with_recovered_arguments",
                            }

                        if (
                            tool_name in {
                                "assign_multi_subjective_tasks_to_info_seeker",
                                "assign_multi_objective_tasks_to_info_seeker",
                            }
                            and not args_dict
                        ):
                            recovered_tasks = self._recover_task_list_from_broken_json(raw_json)
                            if recovered_tasks:
                                args_dict = {"tasks": recovered_tasks}

                        if args_dict:
                            all_tool_calls.append({"name": tool_name, "arguments": args_dict})
                            self.logger.info(f"🛡️ 触发【暴力物理装甲】,成功救回崩溃的工具调用: {tool_name}")
                            continue

                    # 如果彻底没救了,返回错误反馈引导模型重写
                    return [{"name": "system_error_feedback", "arguments": {
                        "error": "Invalid tool-call JSON. The arguments field must be a JSON object, not a quoted JSON string. Use {\"name\":\"tool\",\"arguments\":{\"key\":\"value\"}}, not {\"name\":\"tool\",\"arguments\":\"{...}\"}."}}]
        normalized_tool_calls = []
        for call in all_tool_calls:
            if not isinstance(call, dict):
                continue
            args = call.get("arguments", {})
            if isinstance(args, str):
                try:
                    parsed_args = json.loads(args)
                    args = parsed_args if isinstance(parsed_args, dict) else {"_raw_arguments": parsed_args}
                except Exception:
                    args = {"_raw_arguments": args}
            elif args is None:
                args = {}
            elif not isinstance(args, dict):
                args = {"_raw_arguments": args}
            call["arguments"] = args
            normalized_tool_calls.append(call)
        all_tool_calls = normalized_tool_calls

        # Validate recovered calls against the advertised tool schema.  This
        # prevents false successes such as list_workspace receiving
        # {"thought": "..."} after a greedy recovery mixed two JSON blocks.
        schema_contracts: Dict[str, Dict[str, Any]] = {}
        for schema in getattr(self, "tool_schemas", []) or []:
            function = schema.get("function", {}) if isinstance(schema, dict) else {}
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            parameters = function.get("parameters", {})
            if name and isinstance(parameters, dict):
                schema_contracts[name] = parameters

        all_tool_calls = self._adapt_tool_calls_to_schema(all_tool_calls, schema_contracts)

        validated_tool_calls = []
        for call in all_tool_calls:
            tool_name = call.get("name", "")
            if tool_name == "system_error_feedback":
                validated_tool_calls.append(call)
                continue
            contract = schema_contracts.get(tool_name)
            if not contract:
                validated_tool_calls.append(call)
                continue
            properties = contract.get("properties", {})
            required = set(contract.get("required", []) or [])
            args = call.get("arguments", {})
            if not isinstance(properties, dict) or not properties:
                validated_tool_calls.append(call)
                continue
            known_args = {key: value for key, value in args.items() if key in properties}
            unknown_args = sorted(set(args) - set(properties))
            missing_args = sorted(required - set(known_args))
            if missing_args or (args and not known_args):
                details = []
                if missing_args:
                    details.append(f"missing required arguments: {missing_args}")
                if unknown_args:
                    details.append(f"unexpected arguments: {unknown_args}")
                self.logger.warning(
                    "Rejected malformed recovered tool call %s (%s)",
                    tool_name,
                    "; ".join(details),
                )
                return [{"name": "system_error_feedback", "arguments": {
                    "error": (
                        f"Invalid arguments for {tool_name}: {'; '.join(details)}. "
                        "Regenerate exactly one tool call using the advertised schema."
                    )
                }}]
            if unknown_args:
                self.logger.warning(
                    "Removed unexpected arguments from tool call %s: %s",
                    tool_name,
                    unknown_args,
                )
                call["arguments"] = known_args
            validated_tool_calls.append(call)
        all_tool_calls = validated_tool_calls

        if not all_tool_calls and "file_write" in content:
            content_match = re.search(r'"content"\s*:\s*"(.*?)"\s*\}', content, re.DOTALL)
            path_match = re.search(r'"file_path"\s*:\s*"(.*?)"', content)
            if content_match and path_match:
                return [{"name": "file_write",
                         "arguments": {"file_path": path_match.group(1), "content": content_match.group(1)}}]
        return all_tool_calls

    def _adapt_tool_calls_to_schema(
            self,
            tool_calls: List[Dict[str, Any]],
            schema_contracts: Dict[str, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Normalize common model aliases before strict schema validation.

        MCP servers legitimately use different names for the same concept
        (`query`/`keywords`/`queries`, `path`/`file_path`).  Models should not
        lose an otherwise valid research subtask merely for choosing another
        conventional spelling.  Only unambiguous conversions are performed;
        everything else still goes through strict validation below.
        """
        name_aliases = {
            "read_file": "file_read",
            "write_file": "file_write",
        }
        adapted: List[Dict[str, Any]] = []
        for original in tool_calls:
            if not isinstance(original, dict):
                continue
            call = dict(original)
            name = str(call.get("name") or "")
            alias = name_aliases.get(name)
            if alias and alias in schema_contracts and name not in schema_contracts:
                self.logger.info("Normalized tool alias %s -> %s", name, alias)
                name = alias
                call["name"] = alias

            contract = schema_contracts.get(name) or {}
            properties = contract.get("properties", {}) if isinstance(contract, dict) else {}
            args = dict(call.get("arguments") or {})
            if not isinstance(properties, dict) or not properties:
                call["arguments"] = args
                adapted.append(call)
                continue

            simple_aliases = (
                ("path", "file_path"), ("file_path", "path"),
                ("limit", "max_results"), ("max_results_per_query", "max_results"),
                ("max_results", "max_results_per_query"),
            )
            for source, target in simple_aliases:
                if source in args and target in properties and source not in properties and target not in args:
                    args[target] = args.pop(source)
                    self.logger.info("Normalized %s argument %s -> %s", name, source, target)

            search_keys = ("keywords", "query", "queries")
            expected_search_key = next((key for key in search_keys if key in properties), None)
            supplied_search_key = next((key for key in search_keys if key in args), None)
            if expected_search_key and supplied_search_key and expected_search_key != supplied_search_key:
                value = args.pop(supplied_search_key)
                if expected_search_key == "queries" and not isinstance(value, list):
                    value = [value]
                args[expected_search_key] = value
                self.logger.info(
                    "Normalized %s argument %s -> %s", name, supplied_search_key, expected_search_key,
                )

            # url_crawler requires save metadata, while models commonly emit a
            # plain `urls` list.  Create deterministic workspace-relative paths.
            if "documents" in properties and "documents" not in args:
                raw_urls = args.pop("urls", args.pop("url", None))
                if raw_urls is not None:
                    raw_urls = raw_urls if isinstance(raw_urls, list) else [raw_urls]
                    documents = []
                    for item in raw_urls:
                        if isinstance(item, dict):
                            document = dict(item)
                        else:
                            url = str(item).strip()
                            digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
                            document = {
                                "url": url,
                                "file_path": f"research/retrieved/crawled_{digest}.md",
                            }
                        if document.get("url") and document.get("file_path"):
                            documents.append(document)
                    if documents:
                        args["documents"] = documents
                        self.logger.info("Normalized url_crawler urls -> %s document(s)", len(documents))

            # A singular search tool cannot accept a list.  Split it into
            # independent calls so caching, tracking and failures remain clear.
            if expected_search_key in {"query", "keywords"} and isinstance(args.get(expected_search_key), list):
                values = [value for value in args[expected_search_key] if str(value).strip()]
                if values:
                    for value in values:
                        split_call = dict(call)
                        split_args = dict(args)
                        split_args[expected_search_key] = value
                        split_call["arguments"] = split_args
                        adapted.append(split_call)
                    self.logger.info("Split %s into %s singular search calls", name, len(values))
                    continue

            call["arguments"] = args
            adapted.append(call)
        return adapted

    @staticmethod
    def tool_call_signature(tool_name: str, arguments: Dict[str, Any]) -> str:
        """Return a stable signature used to stop exact consecutive repeats."""
        try:
            normalized = json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            normalized = repr(arguments)
        return f"{tool_name}:{normalized}"

    @staticmethod
    def _extract_loose_tool_calls(content: str) -> List[Dict[str, Any]]:
        """Parse loose forms like '<tool_call>tool_name\n{"arg": 1}'."""
        tool_calls: List[Dict[str, Any]] = []
        pattern = re.compile(
            r"<tool_call>\s*([A-Za-z_][A-Za-z0-9_]*)\s*(\{[\s\S]*?\})(?=(?:\s*<tool_call>|\s*$))",
            re.IGNORECASE,
        )
        for match in pattern.finditer(content):
            tool_name = match.group(1).strip()
            raw_args = match.group(2).strip()
            try:
                arguments = json.loads(raw_args)
            except Exception:
                arguments = {"_raw_arguments": raw_args}
            if not isinstance(arguments, dict):
                arguments = {"_raw_arguments": arguments}
            tool_calls.append({"name": tool_name, "arguments": arguments})
        return tool_calls

    @staticmethod
    def _recover_task_list_from_broken_json(raw_json: str) -> List[Dict[str, str]]:
        """Recover task lists from malformed tool calls produced by some models."""
        tasks: List[Dict[str, str]] = []
        if not raw_json or '"task_content"' not in raw_json:
            return tasks

        task_starts = [m.start() for m in re.finditer(r'"task_content"\s*:\s*"', raw_json)]
        field_names = [
            "task_steps_for_reference",
            "deliverable_contents",
            "acceptance_checking_criteria",
            "current_task_status",
        ]
        field_pattern = "|".join(re.escape(name) for name in field_names)

        for index, start in enumerate(task_starts):
            end = task_starts[index + 1] if index + 1 < len(task_starts) else len(raw_json)
            chunk = raw_json[start:end]
            task: Dict[str, str] = {}

            content_match = re.search(
                r'"task_content"\s*:\s*"(.*?)(?:"\s*,\s*"(?:' + field_pattern + r')"|"\s*\}\s*,|"\s*\}\s*\]|\Z)',
                chunk,
                re.DOTALL,
            )
            if content_match:
                task["task_content"] = content_match.group(1).strip()

            for field_name in field_names:
                match = re.search(
                    rf'"{re.escape(field_name)}"\s*:\s*"(.*?)(?:"\s*,\s*"[^"]+"\s*:|"\s*\}}\s*,|"\s*\}}\s*\]|\Z)',
                    chunk,
                    re.DOTALL,
                )
                if match:
                    task[field_name] = match.group(1).strip()

            if task.get("task_content"):
                tasks.append(task)

        return tasks

    def execute_tool_call(self, tool_call) -> Dict[str, Any]:
        """Execute a tool call and return results using proper MCP architecture"""
        tool_name = tool_call["name"]

        # 🚀 必须加上这段拦截:正面拦截 JSON 错误,大声报错给大模型
        if tool_name == "system_error_feedback":
            return {
                "success": False,
                "error": "工具调用 JSON 无法解析。请只输出一个工具调用，arguments 必须是 JSON 对象。"
            }

        try:
            # Parse arguments
            arguments = tool_call["arguments"]
            if isinstance(arguments, str):
                try:
                    parsed_arguments = json.loads(arguments)
                    if isinstance(parsed_arguments, dict):
                        arguments = parsed_arguments
                    else:
                        return {
                            "success": False,
                            "error": f"Tool '{tool_name}' arguments JSON must decode to an object, got {type(parsed_arguments).__name__}"
                        }
                except Exception as parse_err:
                    return {
                        "success": False,
                        "error": f"Tool '{tool_name}' arguments must be a JSON object, not a raw string: {parse_err}"
                    }
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                return {
                    "success": False,
                    "error": f"Tool '{tool_name}' arguments must be a mapping, got {type(arguments).__name__}"
                }

            cache_key = ""
            if tool_name in self._cacheable_research_tools:
                normalized = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
                cache_key = f"{tool_name}:{normalized}"
                with self._tool_cache_lock:
                    cached = self._tool_cache.get(cache_key)
                if cached is not None:
                    cached_result = dict(cached)
                    cached_result["metadata"] = dict(cached_result.get("metadata") or {})
                    cached_result["metadata"]["cache_hit"] = True
                    self.logger.info("Reusing cached research result: %s", tool_name)
                    return cached_result
            
            # Check if tool is available
            if tool_name not in self.available_tools:
                return {
                    "success": False,
                    "error": f"Tool '{tool_name}' not available for this agent"
                }
            
            # Route tool execution based on tool type
            # Built-in tools (like assign_task_to_*) are callable methods, not MCP server tools
            if callable(self.available_tools[tool_name]):
                # Built-in tool: execute locally
                tool_function = self.available_tools[tool_name]
                result = tool_function(**arguments)
                
                # Convert result to standard format
                if hasattr(result, 'to_dict'):
                    normalized_result = result.to_dict()
                elif isinstance(result, dict):
                    normalized_result = result
                else:
                    normalized_result = {
                        "success": True,
                        "data": result,
                        "error": None,
                        "metadata": {}
                    }
                normalized_result = self._validate_research_result(tool_name, arguments, normalized_result)
                if cache_key and normalized_result.get("success"):
                    with self._tool_cache_lock:
                        self._tool_cache[cache_key] = dict(normalized_result)
                return normalized_result
                    
            elif hasattr(self.mcp_tools, 'client') and hasattr(self.mcp_tools.client, 'call_tool'):
                # MCP server tool: execute via client
                result = self.mcp_tools.client.call_tool(tool_name, arguments)
                
                # Convert MCPClientResult to standard format
                if hasattr(result, 'success'):
                    normalized_result = self._normalize_mcp_client_result(result)
                    normalized_result = self._validate_research_result(tool_name, arguments, normalized_result)
                    if cache_key and normalized_result.get("success"):
                        with self._tool_cache_lock:
                            self._tool_cache[cache_key] = dict(normalized_result)
                    return normalized_result
                else:
                    return result
            else:
                return {
                    "success": False,
                    "error": f"Tool '{tool_name}' is not executable (neither built-in nor MCP)"
                }
            
        except Exception as e:
            self.logger.error(f"Error executing tool {tool_name}: {e}")
            return {
                "success": False,
                "error": f"Tool execution failed: {str(e)}"
                }

    @staticmethod
    def _normalize_mcp_client_result(result: Any) -> Dict[str, Any]:
        """Flatten the JSON-RPC envelope around a tool's standard result.

        MCPClient.success describes the HTTP/JSON-RPC request. The MCP server
        result may itself be ``{success, data, error, metadata}``. Keeping both
        layers caused successful PubMed payloads to look as if they had no PMID
        and also hid real tool-level failures.
        """
        transport_metadata = dict(getattr(result, "metadata", {}) or {})
        payload = getattr(result, "data", None)
        if isinstance(payload, dict) and "success" in payload and (
            "data" in payload or "error" in payload
        ):
            tool_metadata = payload.get("metadata")
            if isinstance(tool_metadata, dict):
                transport_metadata.update(tool_metadata)
            return {
                "success": bool(payload.get("success")),
                "data": payload.get("data"),
                "error": payload.get("error"),
                "metadata": transport_metadata,
            }
        return {
            "success": bool(getattr(result, "success", False)),
            "data": payload,
            "error": getattr(result, "error", None),
            "metadata": transport_metadata,
        }

    @staticmethod
    def _validate_research_result(tool_name: str, arguments: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
        """Reject identifier mismatches before evidence is persisted or cited."""
        if not result.get("success") or tool_name != "get_pubmed_article":
            return result
        requested = re.sub(r"\D", "", str(arguments.get("pmid") or ""))
        data = result.get("data")
        returned = re.sub(r"\D", "", str(data.get("pmid") or "")) if isinstance(data, dict) else ""
        if requested and returned != requested:
            return {
                "success": False,
                "data": None,
                "error": f"PubMed identifier mismatch: requested PMID {requested}, returned {returned or 'missing PMID'}",
                "metadata": {"evidence_rejected": True},
            }
        return result

    
    def log_reasoning(self, iteration: int, reasoning: str):
        """Log reasoning step in the trace"""
        self.reasoning_trace.append({
            "type": "reasoning",
            "iteration": iteration,
            "content": reasoning,
            "timestamp": time.time()
        })
        self.execution_stats["reasoning_steps"] += 1
        self.execution_stats["total_steps"] += 1
        self.logger.info(f"Reasoning (Iter {iteration}): {reasoning[:100]}...")
    
    def log_action(self, iteration: int, tool: str, arguments: Dict[str, Any], result: Dict[str, Any]):
        """Log action step in the trace"""
        self.reasoning_trace.append({
            "type": "action", 
            "iteration": iteration,
            "tool": tool,
            "arguments": arguments,
            "result": result,
            "timestamp": time.time()
        })
        self.execution_stats["action_steps"] += 1
        self.execution_stats["total_steps"] += 1
        
        # Log success/failure
        success = result.get("success", True)
        status = "Success" if success else "Failed"
        self.logger.info(
            "[AGENT_TRACE] task=%s agent=%s iteration=%s tool=%s status=%s args=%s",
            getattr(self, "task_id", "") or "unbound", self.config.agent_name,
            iteration, tool, status, str(arguments)[:400],
        )
        self._publish_tool_activity(iteration, tool, arguments, result, success)

    def _publish_tool_activity(self, iteration: int, tool: str, arguments: Dict[str, Any],
                               result: Dict[str, Any], success: bool) -> None:
        """Expose a safe action/result summary, never prompts or hidden chain-of-thought."""
        task_id = getattr(self, "task_id", None)
        if not task_id or tool in {"think", "reflect", "system_error_feedback"}:
            return
        research_tools = {
            "academic_search", "arxiv_search", "arxiv_read_paper", "search_pubmed_key_words",
            "search_pubmed_advanced", "get_pubmed_article", "medrxiv_search", "batch_web_search",
            "url_crawler", "document_extract", "document_qa", "download_files", "jina_reader",
        }
        experiment_tools = {"run_python_script", "bash", "analyze_image", "file_write"}
        if tool not in research_tools | experiment_tools:
            return
        try:
            from src.utils.task_manager import task_manager
            category = "literature" if tool in research_tools else "experiment"
            query = arguments.get("query") or arguments.get("keywords") or arguments.get("queries") or ""
            if isinstance(query, list):
                query = "; ".join(str(item) for item in query[:3])
            data = result.get("data") if isinstance(result, dict) else None
            items: List[Dict[str, str]] = []

            def visit(value: Any) -> None:
                if len(items) >= 3:
                    return
                if isinstance(value, dict):
                    title = value.get("title") or value.get("name")
                    if title:
                        abstract = value.get("abstract") or value.get("snippet") or value.get("description") or ""
                        items.append({"title": str(title)[:240], "summary": re.sub(r"\s+", " ", str(abstract))[:420]})
                    for nested in value.values():
                        visit(nested)
                elif isinstance(value, list):
                    for nested in value:
                        visit(nested)
            visit(data)
            summary = "调用成功" if success else f"调用失败：{str(result.get('error') or '')[:300]}"
            if items:
                summary = f"获得 {len(items)} 篇代表性结果：" + "；".join(item["title"] for item in items)
            elif query:
                summary = f"正在处理：{str(query)[:260]}"
            task_manager.record_event(task_id, "research_activity", f"{self.config.agent_name}：{tool}", {
                "stage": f"{category}_activity", "agent": self.config.agent_name,
                "iteration": iteration, "tool": tool, "category": category,
                "success": success, "query": str(query)[:500], "summary": summary, "items": items,
            })
        except Exception as exc:
            self.logger.debug("Could not publish tool activity: %s", exc)
    
    def log_error(self, iteration: int, error: str):
        """Log error in the trace"""
        self.reasoning_trace.append({
            "type": "error",
            "iteration": iteration,
            "error": error,
            "timestamp": time.time()
        })
        self.execution_stats["error_steps"] += 1
        self.execution_stats["total_steps"] += 1
        self.logger.error(f"Error (Iter {iteration}): {error}")
    
    def reset_trace(self):
        """Reset the reasoning trace for a new task"""
        self.reasoning_trace = []
        self.execution_stats = {
            "total_steps": 0,
            "reasoning_steps": 0,
            "action_steps": 0, 
            "error_steps": 0,
            "tool_usage": {},
            "success_rate": 1.0
        }
    
    def get_execution_stats(self) -> Dict[str, Any]:
        """Get execution statistics"""
        # Calculate success rate
        if self.execution_stats["action_steps"] > 0:
            failed_actions = sum(1 for step in self.reasoning_trace 
                               if step.get("type") == "action" 
                               and not step.get("result", {}).get("success", True))
            self.execution_stats["success_rate"] = (
                (self.execution_stats["action_steps"] - failed_actions) / 
                self.execution_stats["action_steps"]
            )
        
        return self.execution_stats.copy()
    
    def create_response(self, success: bool, result: Dict[str, Any] = None, 
                       error: str = None, iterations: int = 0, 
                       execution_time: float = 0.0) -> AgentResponse:
        """Create a standardized agent response"""
        return AgentResponse(
            success=success,
            result=result,
            error=error,
            iterations=iterations,
            reasoning_trace=self.reasoning_trace.copy(),
            agent_name=self.config.agent_name,
            execution_time=execution_time
        )
    
    def validate_config(self) -> bool:
        """Validate agent configuration"""
        try:
            # Check required fields
            if not self.config.agent_name:
                return False
            if not self.config.model:
                return False
            if self.config.max_iterations <= 0:
                return False
            if not (0.0 <= self.config.temperature <= 2.0):
                return False
            if self.config.max_tokens <= 0:
                return False
            
            return True
        except Exception:
            return False
    
    @abstractmethod
    def execute_task(self, task_input: TaskInput) -> AgentResponse:
        """
        Execute a task using the standardized TaskInput format
        
        Args:
            task_input: TaskInput object with standardized task information
            
        Returns:
            AgentResponse with results and process trace
        """
        pass
    
    @abstractmethod
    def _build_system_prompt(self) -> str:
        """Build the system prompt for this agent"""
        pass


# Simple factory function for creating agent configurations

def create_agent_config(
    agent_name: str,
    model: Optional[str] = None,
    max_iterations: Optional[int] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None
) -> AgentConfig:
    """
    Create an AgentConfig instance for server-managed sessions.
    
    Args:
        agent_name: Name of the agent
        model: LLM model to use
        max_iterations: Maximum number of iterations
        temperature: LLM temperature setting
        max_tokens: Maximum tokens for LLM response
        
    Returns:
        Configured AgentConfig instance
    """
    # Load env-backed defaults
    try:
        from config.config import get_config
        api_cfg = get_config()
    except Exception as e:
        raise ValueError(f"Failed to load global configuration: {e}")
    
    planner_mode = getattr(api_cfg, "planner_mode", "auto")

    resolved_model = model if model is not None else getattr(api_cfg, "model_name", None)
    if not resolved_model:
        raise ValueError("Model is not specified and MODEL_NAME is not set in environment")

    resolved_temperature = temperature if temperature is not None else getattr(api_cfg, "model_temperature", None)
    if resolved_temperature is None:
        raise ValueError("Temperature is not specified and MODEL_TEMPERATURE is not set in environment")

    resolved_max_tokens = max_tokens if max_tokens is not None else getattr(api_cfg, "model_max_tokens", None)
    if resolved_max_tokens is None:
        raise ValueError("Max tokens is not specified and MODEL_MAX_TOKENS is not set in environment")

    # Optional paths used by writer and others
    trajectory_storage_path = getattr(api_cfg, "trajectory_storage_path", None)
    report_output_path = getattr(api_cfg, "report_output_path", None)
    document_analysis_path = getattr(api_cfg, "document_analysis_path", None)

    # Resolve max_iterations per agent type
    if max_iterations is None:
        agent_lower = (agent_name or "").lower()
        resolved_max_iterations = None
        if "planner" in agent_lower:
            resolved_max_iterations = getattr(api_cfg, "planner_max_iterations", None)
        elif "writer" in agent_lower:
            resolved_max_iterations = getattr(api_cfg, "writer_max_iterations", None)
        elif "information" in agent_lower or "seeker" in agent_lower:
            resolved_max_iterations = getattr(api_cfg, "information_seeker_max_iterations", 60)
        elif "experiment" in agent_lower:
            # 🚀 新增:实验智能体默认给予更高的迭代次数(30次),因为它经常需要自我Debug
            resolved_max_iterations = getattr(api_cfg, "experiment_max_iterations", 60)

        # 🚀 优化:不直接 raise 崩溃,给一个全局兜底的 15 次,提高系统鲁棒性
        if resolved_max_iterations is None:
            logger.warning("Max iterations not specified and no env override. Defaulting to 15.")
            resolved_max_iterations = 60

        max_iterations = resolved_max_iterations

    return AgentConfig(
        agent_name=agent_name,
        planner_mode=planner_mode,
        model=resolved_model,
        max_iterations=int(max_iterations),
        temperature=resolved_temperature,
        max_tokens=resolved_max_tokens,
        trajectory_storage_path=trajectory_storage_path,
        report_output_path=report_output_path,
        document_analysis_path=document_analysis_path
    )
