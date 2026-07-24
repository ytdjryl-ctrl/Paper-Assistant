"""
Agent Memory - 单 Session 论文写作记忆模块

每次写完一章自动存储关键信息，后续章节可通过 recall() 检索。
论文完成后清空，下次写新论文重新开始。
"""
import re
import logging
from typing import List, Dict, Optional, Tuple
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np

logger = logging.getLogger(__name__)


class AgentMemory:
    """单 Session 论文写作记忆系统

    核心能力：
    1. store() - 存储章节内容（自动分块）
    2. recall() - 按查询检索最相关的记忆片段
    3. clear() - 论文完成后清空所有记忆
    """

    def __init__(self):
        self.chunks: List[Dict] = []  # [{id, text, chapter, type, metadata}]
        self.vectorizer: Optional[TfidfVectorizer] = None
        self.vectors = None
        self._dirty = False  # 标记是否需要重建向量索引

    def store(self, chapter_name: str, content: str, content_type: str = "chapter",
              metadata: Dict = None) -> None:
        """存储一章或多段内容到记忆

        Args:
            chapter_name: 章节名，如 "材料与方法"
            content: 内容文本
            content_type: 类型标记，如 chapter / data / method / result
            metadata: 附加元数据
        """
        if not content or not content.strip():
            return

        if content_type in {"chapter", "summary"}:
            old_count = len(self.chunks)
            self.chunks = [c for c in self.chunks if c.get("chapter") != chapter_name]
            if len(self.chunks) != old_count:
                logger.info(
                    f"AgentMemory: replaced existing chunks for '{chapter_name}' "
                    f"({old_count - len(self.chunks)} removed)"
                )

        # 按自然段落分块，每块尽量保留完整语义
        paragraphs = [p.strip() for p in content.split('\n') if p.strip()]
        
        # 如果段落太少，不拆分
        if len(paragraphs) <= 2:
            paragraphs = [content.strip()]
        
        for i, para in enumerate(paragraphs):
            chunk_id = f"{chapter_name}_{i}"
            # 检查是否已存在相同 ID
            if any(c["id"] == chunk_id for c in self.chunks):
                chunk_id = f"{chunk_id}_{len(self.chunks)}"
            
            self.chunks.append({
                "id": chunk_id,
                "text": para,
                "chapter": chapter_name,
                "type": content_type,
                "metadata": metadata or {}
            })

        self._dirty = True
        logger.info(f"AgentMemory: stored {len(paragraphs)} chunks from '{chapter_name}' "
                    f"(total chunks: {len(self.chunks)})")

    def recall(self, query: str, top_k: int = 5) -> List[Dict]:
        """检索与查询最相关的记忆片段

        Args:
            query: 查询文本，如 "LightGBM 的 R² 值是多少"
            top_k: 返回前 k 个结果

        Returns:
            [{id, text, chapter, type, score}, ...]
        """
        if not self.chunks:
            return []

        # 重建向量索引
        if self._dirty or self.vectorizer is None:
            self._rebuild_index()

        # 向量化查询
        query_vec = self.vectorizer.transform([query])
        
        # 计算相似度
        scores = cosine_similarity(query_vec, self.vectors)[0]
        
        # 取 top_k
        top_indices = np.argsort(scores)[::-1][:top_k]
        
        results = []
        for idx in top_indices:
            if scores[idx] > 0:  # 只返回有相关性的
                chunk = self.chunks[idx]
                results.append({
                    "id": chunk["id"],
                    "text": chunk["text"],
                    "chapter": chunk["chapter"],
                    "type": chunk["type"],
                    "score": float(scores[idx])
                })

        logger.info(f"AgentMemory recall('{query[:50]}...') -> {len(results)} results")
        return results

    def get_chapter_summary(self, chapter_name: str) -> Optional[str]:
        """获取某个章节的全部记忆内容"""
        chunks = [c for c in self.chunks if c["chapter"] == chapter_name]
        if not chunks:
            return None
        return '\n'.join(c["text"] for c in chunks)

    def get_all_summaries(self) -> str:
        """获取所有章节的结构化摘要，用于注入 prompt"""
        if not self.chunks:
            return "暂无前文记忆。"
        
        chapters = {}
        for chunk in self.chunks:
            chapter = chunk["chapter"]
            if chapter not in chapters:
                chapters[chapter] = []
            chapters[chapter].append(chunk["text"])
        
        lines = ["【前文记忆 - 已完成的章节数据】"]
        for chapter, texts in chapters.items():
            combined = ' '.join(texts)
            # 截取前500字作为摘要
            summary = combined[:500] + ("..." if len(combined) > 500 else "")
            lines.append(f"\n## {chapter}")
            lines.append(summary)
        
        return '\n'.join(lines)

    def clear(self) -> None:
        """清空所有记忆（论文完成后调用）"""
        count = len(self.chunks)
        self.chunks = []
        self.vectorizer = None
        self.vectors = None
        self._dirty = False
        logger.info(f"AgentMemory: cleared {count} chunks")

    def get_stats(self) -> Dict:
        """获取记忆统计信息"""
        chapters = {}
        for chunk in self.chunks:
            ch = chunk["chapter"]
            chapters[ch] = chapters.get(ch, 0) + 1
        return {
            "total_chunks": len(self.chunks),
            "total_chapters": len(chapters),
            "chapters": chapters
        }

    def _rebuild_index(self):
        """重建 TF-IDF 向量索引"""
        if not self.chunks:
            self.vectorizer = None
            self.vectors = None
            return

        texts = [c["text"] for c in self.chunks]
        self.vectorizer = TfidfVectorizer(
            max_features=1000,
            ngram_range=(1, 2),
            analyzer='char_wb'  # 字符级 n-gram，对中英文混合效果好
        )
        self.vectors = self.vectorizer.fit_transform(texts)
        self._dirty = False
        logger.info(f"AgentMemory: rebuilt index with {len(self.chunks)} chunks")


# 全局单例
_global_memory: Optional[AgentMemory] = None


def get_memory() -> AgentMemory:
    """获取全局 AgentMemory 实例"""
    global _global_memory
    if _global_memory is None:
        _global_memory = AgentMemory()
    return _global_memory


def reset_memory():
    """重置记忆（新论文开始时调用）"""
    global _global_memory
    if _global_memory:
        _global_memory.clear()
    _global_memory = AgentMemory()
    logger.info("AgentMemory: reset for new session")
