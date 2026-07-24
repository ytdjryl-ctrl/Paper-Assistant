# Copyright (c) 2026 South China Sea Institute of Oceanology, Chinese Academy of Sciences (SCSIO, CAS). All rights reserved.
"""
Task Manager for handling concurrent agent tasks with cancellation support
支持取消功能的并发agent任务管理器
"""
import os
import threading
import time
from typing import Dict, Optional, Any
from dataclasses import dataclass, field
from enum import Enum
import logging
import json
import re
from pathlib import Path

logger = logging.getLogger(__name__)


class TaskStatus(Enum):
    """Task execution status"""
    PENDING = "pending"
    RUNNING = "running"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED = "paused"
    RESUMING = "resuming"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class TaskInfo:
    """Information about a running task"""
    task_id: str
    query: str
    status: TaskStatus
    created_at: float
    updated_at: float
    thread_id: Optional[int] = None
    process_id: Optional[int] = None
    cancellation_token: threading.Event = field(default_factory=threading.Event)
    resume_token: threading.Event = field(default_factory=threading.Event)
    result: Optional[Any] = None
    error: Optional[str] = None
    progress: Dict[str, Any] = field(default_factory=dict)
    interventions: list = field(default_factory=list)
    requested_stage: Optional[str] = None
    requested_stage_instruction: Optional[str] = None
    events: list = field(default_factory=list)
    event_sequence: int = 0
    event_log_path: Optional[str] = None

    def __post_init__(self):
        self.resume_token.set()
    
    def is_cancelled(self) -> bool:
        """Check if task has been cancelled"""
        return self.cancellation_token.is_set()
    
    def cancel(self):
        """Request task cancellation"""
        self.cancellation_token.set()
        self.resume_token.set()
        self.status = TaskStatus.CANCELLED
        self.updated_at = time.time()


class TaskManager:
    """
    Global task manager for tracking and managing all running agent tasks
    用于跟踪和管理所有运行中agent任务的全局任务管理器
    
    注意：在多进程环境下（如 uvicorn workers > 1），每个进程会有独立的 TaskManager 实例
    """
    
    def __init__(self):
        """Initialize task manager"""
        # 每次创建新实例时都初始化（多进程安全）
        self._tasks: Dict[str, TaskInfo] = {}
        self._tasks_lock = threading.Lock()
        logger.info(f"TaskManager initialized in process {os.getpid()}")

    @staticmethod
    def classify_stage_directive(instruction: str) -> Optional[str]:
        """Recognize explicit workflow transitions; ordinary guidance remains local context."""
        text = re.sub(r"\s+", "", (instruction or "").lower())
        stage_patterns = {
            "experiment": [
                r"(?:直接|立即|马上|现在)?(?:进入|转到|开始|进行)(?:到)?实验(?:环节|阶段|分析)?",
                r"(?:停止|结束|跳过|不要|不用)(?:继续)?(?:文献)?(?:搜索|检索).*(?:进入|开始|做)实验",
                r"(?:go|move|switch|proceed)(?:directly)?to(?:the)?experiment",
            ],
            "writing": [
                r"(?:直接|立即|马上|现在)?(?:进入|转到|开始)(?:论文)?写作(?:环节|阶段)?",
                r"(?:go|move|switch|proceed)(?:directly)?to(?:the)?writ(?:e|ing)",
            ],
            "review": [
                r"(?:直接|立即|马上|现在)?(?:进入|转到|开始)(?:四模型)?审稿(?:环节|阶段)?",
                r"(?:go|move|switch|proceed)(?:directly)?to(?:the)?review",
            ],
        }
        for stage, patterns in stage_patterns.items():
            if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns):
                return stage
        return None
    
    def create_task(self, task_id: str, query: str) -> TaskInfo:
        """
        Create a new task and register it
        
        Args:
            task_id: Unique task identifier
            query: User query for this task
            
        Returns:
            TaskInfo object for the new task
        """
        with self._tasks_lock:
            if task_id in self._tasks:
                logger.warning(f"Task {task_id} already exists, returning existing task")
                return self._tasks[task_id]
            
            task_info = TaskInfo(
                task_id=task_id,
                query=query,
                status=TaskStatus.PENDING,
                created_at=time.time(),
                updated_at=time.time()
            )
            self._tasks[task_id] = task_info
            logger.info(f"Created task {task_id}: {query[:100]}...")
            return task_info
    
    def get_task(self, task_id: str) -> Optional[TaskInfo]:
        """
        Get task information by task ID
        
        Args:
            task_id: Task identifier
            
        Returns:
            TaskInfo object or None if not found
        """
        with self._tasks_lock:
            return self._tasks.get(task_id)
    
    def update_task_status(self, task_id: str, status: TaskStatus, 
                          result: Optional[Any] = None, 
                          error: Optional[str] = None):
        """
        Update task status
        
        Args:
            task_id: Task identifier
            status: New status
            result: Task result (if completed)
            error: Error message (if failed)
        """
        with self._tasks_lock:
            if task_id not in self._tasks:
                logger.warning(f"Task {task_id} not found for status update")
                return
            
            task = self._tasks[task_id]
            task.status = status
            task.updated_at = time.time()
            
            if result is not None:
                task.result = result
            if error is not None:
                task.error = error
            
            logger.info(f"Task {task_id} status updated to {status.value}")
    
    def update_task_progress(self, task_id: str, progress_info: Dict[str, Any]):
        """
        Update task progress information
        
        Args:
            task_id: Task identifier
            progress_info: Progress information dict
        """
        with self._tasks_lock:
            if task_id not in self._tasks:
                return
            
            task = self._tasks[task_id]
            task.progress.update(progress_info)
            task.updated_at = time.time()

    def record_event(self, task_id: str, event_type: str, message: str, data: Optional[Dict[str, Any]] = None):
        event_to_persist = None
        log_path = None
        with self._tasks_lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            task.event_sequence += 1
            event = {
                "id": task.event_sequence,
                "timestamp": time.time(),
                "type": event_type,
                "message": message,
                "data": data or {},
            }
            task.events.append(event)
            if len(task.events) > 2000:
                task.events = task.events[-2000:]
            task.updated_at = time.time()
            event_to_persist = dict(event)
            log_path = task.event_log_path
        if log_path and event_to_persist:
            try:
                path = Path(log_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event_to_persist, ensure_ascii=False, default=str) + "\n")
            except Exception as exc:
                logger.debug("Could not persist workflow event for %s: %s", task_id, exc)

    def set_event_log_path(self, task_id: str, path: str) -> bool:
        """Persist all subsequent SSE events for reload/debug recovery."""
        with self._tasks_lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            task.event_log_path = str(path)
            return True

    def get_events(self, task_id: str, after_id: int = 0):
        with self._tasks_lock:
            task = self._tasks.get(task_id)
            return [dict(event) for event in task.events if event["id"] > after_id] if task else []

    def request_pause(self, task_id: str) -> bool:
        with self._tasks_lock:
            task = self._tasks.get(task_id)
            if not task or task.status not in {TaskStatus.RUNNING, TaskStatus.RESUMING}:
                return False
            task.status = TaskStatus.PAUSE_REQUESTED
            task.resume_token.clear()
            task.updated_at = time.time()
            return True

    def resume_task(self, task_id: str) -> bool:
        with self._tasks_lock:
            task = self._tasks.get(task_id)
            if not task or task.status not in {TaskStatus.PAUSE_REQUESTED, TaskStatus.PAUSED}:
                return False
            task.status = TaskStatus.RESUMING
            task.resume_token.set()
            task.updated_at = time.time()
            return True

    def add_intervention(self, task_id: str, instruction: str) -> bool:
        instruction = (instruction or "").strip()
        if not instruction:
            return False
        with self._tasks_lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            target_stage = self.classify_stage_directive(instruction)
            task.interventions.append({
                "timestamp": time.time(), "instruction": instruction, "consumed": False,
                "target_stage": target_stage,
            })
            if target_stage:
                task.requested_stage = target_stage
                task.requested_stage_instruction = instruction
            task.updated_at = time.time()
            return True

    def peek_requested_stage(self, task_id: str) -> Optional[Dict[str, str]]:
        with self._tasks_lock:
            task = self._tasks.get(task_id)
            if not task or not task.requested_stage:
                return None
            return {
                "stage": task.requested_stage,
                "instruction": task.requested_stage_instruction or "",
            }

    def clear_requested_stage(self, task_id: str, stage: Optional[str] = None) -> bool:
        with self._tasks_lock:
            task = self._tasks.get(task_id)
            if not task or not task.requested_stage:
                return False
            if stage and task.requested_stage != stage:
                return False
            task.requested_stage = None
            task.requested_stage_instruction = None
            task.updated_at = time.time()
            return True

    def consume_interventions(self, task_id: str):
        with self._tasks_lock:
            task = self._tasks.get(task_id)
            if not task:
                return []
            values = []
            for item in task.interventions:
                if not item.get("consumed"):
                    item["consumed"] = True
                    values.append(item["instruction"])
            return values

    def checkpoint(self, task_id: str, stage: str, data: Optional[Dict[str, Any]] = None,
                   event_type: str = "checkpoint"):
        self.update_task_progress(task_id, {"stage": stage, **(data or {})})
        self.record_event(task_id, event_type, f"进入检查点：{stage}", {"stage": stage, **(data or {})})
        while True:
            with self._tasks_lock:
                task = self._tasks.get(task_id)
                if not task:
                    return []
                if task.is_cancelled():
                    return []
                should_pause = task.status in {TaskStatus.PAUSE_REQUESTED, TaskStatus.PAUSED}
                if should_pause:
                    task.status = TaskStatus.PAUSED
                    token = task.resume_token
                else:
                    if task.status == TaskStatus.RESUMING:
                        task.status = TaskStatus.RUNNING
                    values = self.consume_interventions_unlocked(task)
            if not should_pause:
                for instruction in values:
                    self.record_event(
                        task_id,
                        "guidance_applied",
                        "用户指导已在安全检查点交给当前智能体。",
                        {"stage": stage, "instruction": instruction},
                    )
                return values
            token.wait(timeout=0.5)

    @staticmethod
    def consume_interventions_unlocked(task: TaskInfo):
        values = []
        for item in task.interventions:
            if not item.get("consumed"):
                item["consumed"] = True
                values.append(item["instruction"])
        return values
    
    def cancel_task(self, task_id: str) -> bool:
        """
        Request task cancellation
        
        Args:
            task_id: Task identifier
            
        Returns:
            True if task was found and cancellation requested, False otherwise
        """
        with self._tasks_lock:
            if task_id not in self._tasks:
                logger.warning(f"Task {task_id} not found for cancellation")
                return False
            
            task = self._tasks[task_id]
            if task.status in [TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED]:
                logger.info(f"Task {task_id} already in terminal state: {task.status.value}")
                return False
            
            task.cancel()
            logger.info(f"Task {task_id} cancellation requested")
            return True
    
    def get_cancellation_token(self, task_id: str) -> Optional[threading.Event]:
        """
        Get cancellation token for a task
        
        Args:
            task_id: Task identifier
            
        Returns:
            threading.Event object that will be set when task should be cancelled
        """
        with self._tasks_lock:
            task = self._tasks.get(task_id)
            return task.cancellation_token if task else None
    
    def is_task_cancelled(self, task_id: str) -> bool:
        """
        Check if task has been cancelled
        
        Args:
            task_id: Task identifier
            
        Returns:
            True if task is cancelled, False otherwise
        """
        with self._tasks_lock:
            task = self._tasks.get(task_id)
            return task.is_cancelled() if task else False
    
    def cleanup_completed_tasks(self, max_age_seconds: int = 3600):
        """
        Remove completed/cancelled/failed tasks older than max_age_seconds
        
        Args:
            max_age_seconds: Maximum age for completed tasks in seconds
        """
        current_time = time.time()
        with self._tasks_lock:
            tasks_to_remove = []
            for task_id, task in self._tasks.items():
                if task.status in [TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED]:
                    age = current_time - task.updated_at
                    if age > max_age_seconds:
                        tasks_to_remove.append(task_id)
            
            for task_id in tasks_to_remove:
                del self._tasks[task_id]
                logger.info(f"Cleaned up old task {task_id}")
    
    def get_all_tasks(self) -> Dict[str, Dict[str, Any]]:
        """
        Get information about all tasks
        
        Returns:
            Dictionary mapping task_id to task info
        """
        with self._tasks_lock:
            return {
                task_id: {
                    "task_id": task.task_id,
                    "query": task.query,
                    "status": task.status.value,
                    "created_at": task.created_at,
                    "updated_at": task.updated_at,
                    "thread_id": task.thread_id,
                    "progress": task.progress,
                    "has_error": task.error is not None
                }
                for task_id, task in self._tasks.items()
            }
    
    def get_running_tasks_count(self) -> int:
        """Get count of currently running tasks"""
        with self._tasks_lock:
            return sum(1 for task in self._tasks.values()
                      if task.status in {TaskStatus.RUNNING, TaskStatus.PAUSE_REQUESTED, TaskStatus.PAUSED, TaskStatus.RESUMING})
    
    def remove_task(self, task_id: str):
        """
        Remove a task from the manager
        
        Args:
            task_id: Task identifier
        """
        with self._tasks_lock:
            if task_id in self._tasks:
                del self._tasks[task_id]
                logger.info(f"Removed task {task_id}")


# Global singleton instance - 使用延迟初始化避免多进程问题
_task_manager_instance = None

def get_task_manager() -> TaskManager:
    """
    获取 TaskManager 单例（延迟初始化）
    
    在多进程环境下（如 uvicorn workers > 1），每个进程会在首次调用时
    创建自己的 TaskManager 实例，避免 fork 时继承父进程的锁状态
    """
    global _task_manager_instance
    if _task_manager_instance is None:
        _task_manager_instance = TaskManager()
    return _task_manager_instance

# 向后兼容：保留旧的全局变量名，但改为属性访问
class _TaskManagerProxy:
    """代理对象，延迟初始化真实的 TaskManager"""
    def __getattr__(self, name):
        return getattr(get_task_manager(), name)
    
    def __setattr__(self, name, value):
        setattr(get_task_manager(), name, value)

task_manager = _TaskManagerProxy()
