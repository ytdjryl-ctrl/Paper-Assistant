# Scientific Agent Skills 集成使用指南

## 📋 概述

本指南说明如何在 SciAssistant 项目中配置和使用 scientific-agent-skills 来增强学术论文写作能力。

---

## ✅ 已完成的集成工作

### 1. 修复路径配置
- **文件**: `deepdiver_v2/src/utils/skill_loader.py`
- **修改**: 修正了 skills 目录路径，从 `scientific-skills` 改为 `scientific-agent-skills-main/scientific-skills`

### 2. 启用核心 Skills
- **文件**: `deepdiver_v2/src/agents/writer_agent.py`
- **已启用的 Skills**:
  - ✅ `scientific-writing` - 科学写作核心规范
  - ✅ `citation-management` - 引用管理
  - ✅ `paper-lookup` - 文献检索
  - ✅ `literature-review` - 文献综述

### 3. 创建配置文件
- **文件**: `deepdiver_v2/config/skills_config.yaml`
- **功能**: 集中管理 skills 启用状态和场景配置

### 4. 清理不必要文件
- 删除了 `.github/`, `docs/`, 扫描脚本等与使用无关的文件
- 保留了核心的 `scientific-skills/` 目录（138个 skills）

---

## 🚀 快速开始

### 当前配置（开箱即用）

当前已经配置好了 4 个核心 skills，无需额外操作即可使用：

1. **scientific-writing**: IMRAD结构、两段式写作、学术写作规范
2. **citation-management**: APA/AMA/Vancouver/IEEE/Chicago 引用格式
3. **paper-lookup**: 从 PubMed、arXiv、Semantic Scholar 等 10 个数据库检索文献
4. **literature-review**: 系统性文献综述方法论

### 验证配置

启动项目后，查看日志确认 skills 加载成功：

```
✅ WriterAgent 启用的 skills: ['scientific-writing', 'citation-management', 'paper-lookup', 'literature-review']
✅ 成功加载 skill: scientific-writing (33600 字符)
✅ 成功加载 skill: citation-management (15200 字符)
✅ 成功集成 4 个 skills 到 WriterAgent
```

---

## 🔧 自定义配置

### 方法1：修改 WriterAgent 代码

编辑 `deepdiver_v2/src/agents/writer_agent.py` 第 43-52 行：

```python
self.enabled_skills = [
    "scientific-writing",      # 始终启用
    "citation-management",     # 始终启用
    "paper-lookup",            # 始终启用
    "literature-review",       # 始终启用
    
    # 取消注释以启用更多 skills：
    # "scientific-visualization",  # 科学可视化
    # "scientific-schematics",     # 科学图表生成
    # "venue-templates",           # 期刊模板
    # "peer-review",               # 同行评审
]
```

### 方法2：参考配置文件

查看 `deepdiver_v2/config/skills_config.yaml`，里面有按场景推荐的配置：

#### 场景1：学术论文写作（默认）
```yaml
scenario_academic_paper:
  - scientific-writing
  - citation-management
  - paper-lookup
  - literature-review
  - venue-templates
```

#### 场景2：文献综述
```yaml
scenario_literature_review:
  - scientific-writing
  - literature-review
  - paper-lookup
  - citation-management
```

#### 场景3：数据分析报告
```yaml
scenario_data_report:
  - scientific-writing
  - exploratory-data-analysis
  - statistical-analysis
  - matplotlib
  - seaborn
```

#### 场景4：完整研究 pipeline
```yaml
scenario_full_research:
  - scientific-writing
  - citation-management
  - paper-lookup
  - literature-review
  - exploratory-data-analysis
  - statistical-analysis
  - scientific-visualization
  - venue-templates
```

---

## 📚 可用 Skills 完整列表

### 核心写作 Skills（推荐）

| Skill 名称 | 功能描述 | 推荐场景 |
|-----------|---------|---------|
| `scientific-writing` | IMRAD结构、两段式写作、学术规范 | 所有学术写作 |
| `citation-management` | APA/AMA/Vancouver/IEEE/Chicago 格式 | 需要引用管理 |
| `paper-lookup` | 10个学术数据库检索（PubMed、arXiv等） | 文献检索 |
| `literature-review` | 系统性文献综述方法 | 文献综述 |
| `venue-templates` | Nature/Science/Cell等期刊模板 | 特定期刊投稿 |

### 数据分析 Skills

| Skill 名称 | 功能描述 | 依赖 |
|-----------|---------|------|
| `exploratory-data-analysis` | 探索性数据分析流程 | pandas, numpy |
| `statistical-analysis` | 统计分析方法 | scipy, statsmodels |
| `matplotlib` | 科学绘图 | matplotlib |
| `seaborn` | 统计可视化 | seaborn |
| `scientific-visualization` | 科学可视化最佳实践 | matplotlib |

### 文档处理 Skills

| Skill 名称 | 功能描述 |
|-----------|---------|
| `pdf` | PDF文档处理 |
| `docx` | Word文档处理 |
| `xlsx` | Excel表格处理 |
| `markitdown` | Markdown转换 |

### 生物信息学 Skills（按需启用）

| Skill 名称 | 功能描述 |
|-----------|---------|
| `biopython` | 38个NCBI数据库访问 |
| `bioservices` | 40+生物信息服务 |
| `database-lookup` | 78+科学数据库统一查询 |
| `scanpy` | 单细胞RNA-seq分析 |
| `rdkit` | 化学信息学 |

### 完整列表

所有 138 个 skills 位于 `scientific-agent-skills-main/scientific-skills/` 目录，每个 skill 包含：
- `SKILL.md` - 详细文档
- `references/` - 参考材料（可选）
- `scripts/` - 辅助脚本（可选）
- `assets/` - 资源文件（可选）

---

## ⚙️ Skill Loader 工作原理

### 加载流程

1. **初始化**: WriterAgent 创建时初始化 SkillLoader
2. **扫描**: 自动扫描 `scientific-agent-skills-main/scientific-skills/` 目录
3. **加载**: 根据 `enabled_skills` 列表加载对应的 SKILL.md
4. **注入**: 将 skill 内容注入到系统提示词中

### 路径推导

```
deepdiver_v2/src/utils/skill_loader.py
  ↓ (parent × 3)
SciAssistant-main1/
  ↓
scientific-agent-skills-main/scientific-skills/
```

### 缓存机制

- Skills 加载后会被缓存，避免重复读取
- 单个 reference 文件限制 3000 字符（可配置）
- 可通过 `skills_config.yaml` 调整缓存行为

---

## 🎯 使用示例

### 示例1：标准学术论文写作

**配置**:
```python
self.enabled_skills = [
    "scientific-writing",
    "citation-management",
    "paper-lookup",
    "literature-review",
]
```

**效果**:
- AI 会遵循 IMRAD 结构（引言、方法、结果、讨论）
- 使用两段式写作（先大纲，再完整段落）
- 自动从学术数据库检索相关文献
- 引用格式符合 APA/AMA 等标准

### 示例2：投 Nature/Science

**配置**:
```python
self.enabled_skills = [
    "scientific-writing",
    "citation-management",
    "paper-lookup",
    "venue-templates",  # 启用期刊模板
]
```

**效果**:
- 适配 Nature/Science 的写作风格（通俗易懂、故事驱动）
- 遵循特定期刊的格式要求
- 包含图形摘要（Graphical Abstract）

### 示例3：数据分析报告

**配置**:
```python
self.enabled_skills = [
    "scientific-writing",
    "exploratory-data-analysis",
    "statistical-analysis",
    "matplotlib",
    "seaborn",
]
```

**效果**:
- 生成包含统计分析的报告
- 自动创建 publication-quality 图表
- 遵循科学报告规范

---

## ⚠️ 注意事项

### 1. 提示词长度控制

- 每个 skill 的 SKILL.md 约 30-40KB
- 启用 4 个核心 skills ≈ 120-160KB 提示词
- 如果启用过多 skills，可能超出模型上下文限制

**建议**: 
- 始终启用核心 4 个 skills
- 按需启用其他 skills
- 监控日志中的提示词长度

### 2. Reference 文件

- Reference 文件会额外增加每个 skill 的内容
- 单个 reference 限制 3000 字符
- 如果不需要，可在 `skill_loader.py` 中设置 `include_references=False`

### 3. 网络访问

- `paper-lookup`、`database-lookup` 等 skill 需要网络访问
- 确保 MCP Server 配置了相应的 API keys
- 本地写作任务不需要这些 skills

### 4. 性能影响

- 首次加载 skills 会稍有延迟（约 1-2 秒）
- 后续会使用缓存，无性能影响
- 可通过 `cache_skills: true` 启用缓存

---

## 🔍 故障排查

### 问题1：Skills 未加载

**症状**: 日志显示 "Skills 目录不存在"

**解决**:
```python
# 检查 skill_loader.py 中的路径
skills_base_path = project_root / "scientific-agent-skills-main" / "scientific-skills"

# 验证目录存在
import os
print(os.path.exists("scientific-agent-skills-main/scientific-skills"))
```

### 问题2：某个 Skill 加载失败

**症状**: 日志显示 "Skill 不存在: xxx"

**解决**:
```python
# 列出所有可用 skills
from deepdiver_v2.src.utils.skill_loader import get_skill_loader
loader = get_skill_loader()
print(loader.get_skill_list())
```

### 问题3：提示词过长

**症状**: 模型返回错误或截断

**解决**:
1. 减少启用的 skills 数量
2. 在 `skills_config.yaml` 中设置 `load_references: false`
3. 减小 `max_reference_chars` 值

---

## 📊 Skills 效果对比

### 未启用 Skills
- ❌ 写作结构不规范
- ❌ 引用格式混乱
- ❌ 缺少文献支持
- ❌ 不符合期刊要求

### 启用核心 4 个 Skills
- ✅ 遵循 IMRAD 标准结构
- ✅ 引用格式准确（APA/AMA等）
- ✅ 自动检索相关文献
- ✅ 系统性文献综述
- ✅ 两段式写作（避免 bullet points）

### 启用完整 Skills（7+）
- ✅ 以上所有功能
- ✅ 适配特定期刊风格
- ✅ 自动生成科学图表
- ✅ 数据分析与可视化
- ✅ 同行评审建议

---

## 🎓 推荐配置

### 学术研究（推荐）
```python
self.enabled_skills = [
    "scientific-writing",      # 核心写作规范
    "citation-management",     # 引用管理
    "paper-lookup",            # 文献检索
    "literature-review",       # 文献综述
    "venue-templates",         # 期刊模板
]
```

### 工业界报告
```python
self.enabled_skills = [
    "scientific-writing",
    "exploratory-data-analysis",
    "statistical-analysis",
    "matplotlib",
    "scientific-visualization",
]
```

### 生物信息学
```python
self.enabled_skills = [
    "scientific-writing",
    "citation-management",
    "biopython",
    "bioservices",
    "database-lookup",
]
```

---

## 📝 总结

### 当前状态
✅ Skills 集成已完成，4 个核心 skills 已启用  
✅ 路径配置已修复  
✅ 不必要的文件已清理  
✅ 配置文件已创建  

### 下一步
1. 启动项目验证 skills 加载
2. 根据需求启用更多 skills
3. 测试写作效果
4. 根据需要调整配置

### 获取帮助
- 查看 `scientific-agent-skills-main/scientific-skills/[skill-name]/SKILL.md` 了解每个 skill 的详细信息
- 查看日志确认 skills 加载状态
- 参考 `deepdiver_v2/config/skills_config.yaml` 进行配置

---

**文档版本**: v1.0  
**更新日期**: 2026-05-25  
**维护者**: SciAssistant Team
