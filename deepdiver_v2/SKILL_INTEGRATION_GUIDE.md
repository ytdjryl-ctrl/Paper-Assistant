# 🎓 SciAssistant + Scientific Agent Skills 集成使用指南

## ✅ 集成完成！

您现在成功将 **scientific-agent-skills-main** 中的 skills 集成到了 **SciAssistant-main1** 项目中！

---

## 📋 已完成的修改

### 1. 新增文件

- **`deepdiver_v2/src/utils/skill_loader.py`**  
  Skill 加载器，负责扫描、加载和管理 scientific-skills

- **`deepdiver_v2/test_skill_integration.py`**  
  测试脚本，验证 skill 集成是否成功

### 2. 修改文件

- **`deepdiver_v2/src/agents/writer_agent.py`**  
  - 导入了 `SkillLoader`
  - 在 `__init__` 中初始化 skill_loader 和启用的 skills
  - 在 `_build_system_prompt` 中自动注入 skills 到系统提示词

---

## 🚀 如何使用

### 方式一：直接使用（推荐）

**无需任何额外操作！** WriterAgent 已经自动集成了 scientific-writing skill。

当您运行 SciAssistant 并让 WriterAgent 写论文时，它会自动：
1. 加载 scientific-writing skill 的规范
2. 将 skill 内容注入到系统提示词中
3. 按照 skill 的规范生成高质量论文

### 方式二：测试集成是否成功

运行测试脚本：

```bash
cd F:\Apple Dataset\scientific-agent-skills-main\SciAssistant-main1
python deepdiver_v2/test_skill_integration.py
```

如果看到以下输出，说明集成成功：
```
✅ 所有测试通过！Skill 集成成功！
🎉 现在 WriterAgent 将使用 scientific-writing skill 来生成更高质量的论文
```

---

## 🔧 如何启用更多 Skills

编辑 `deepdiver_v2/src/agents/writer_agent.py` 第 42-46 行：

```python
# 配置要加载的 skills（可以根据需要添加更多）
self.enabled_skills = [
    "scientific-writing",      # ✅ 已启用：科学写作核心规范
    "citation-management",     # 🔲 可选：引用管理
    "scientific-visualization",# 🔲 可选：科学可视化
    # 添加更多 skills...
]
```

### 推荐的 Skills 组合

#### 📝 论文写作场景
```python
self.enabled_skills = [
    "scientific-writing",      # 科学写作规范
    "citation-management",     # 引用管理
    "scientific-visualization",# 科学可视化
]
```

#### 🔬 数据分析场景
```python
self.enabled_skills = [
    "exploratory-data-analysis", # 探索性数据分析
    "statistical-analysis",      # 统计分析
    "matplotlib",                # 绘图
]
```

#### 🧬 生物信息学场景
```python
self.enabled_skills = [
    "biopython",        # 生物Python
    "scanpy",           # 单细胞分析
    "anndata",          # 注释数据
]
```

---

## 📊 集成效果

### 集成前
- ❌ 论文质量不高
- ❌ 缺少系统化写作规范
- ❌ 可能使用 bullet points
- ❌ 引用格式不统一
- ❌ 图表不规范

### 集成后
- ✅ 遵循国际顶级期刊标准
- ✅ 两段式写作流程（大纲 → 完整段落）
- ✅ 禁止 bullet points，强制完整段落
- ✅ 多种引用格式支持（APA/AMA/Vancouver/IEEE）
- ✅ 图表规范（自解释、完整标题、单位标注）
- ✅ IMRAD 结构标准
- ✅ 学术写作四大原则（清晰、简洁、准确、客观）

---

## 🎯 Skill 注入机制

### 工作流程

```
用户请求写论文
    ↓
PlannerAgent 分配任务给 WriterAgent
    ↓
WriterAgent 初始化
    ↓
SkillLoader 加载 scientific-writing skill
    ↓
_build_system_prompt() 构建系统提示词
    ↓
inject_skills_to_prompt() 注入 skills
    ↓
完整的系统提示词（原始 + skills）
    ↓
发送给 LLM
    ↓
LLM 按照 skill 规范生成高质量论文
```

### 提示词结构

```
[原始系统提示词]
  ↓
[Scientific Agent Skills 集成部分]
  ├─ scientific-writing SKILL.md
  ├─ references/imrad_structure.md
  ├─ references/citation_styles.md
  ├─ references/writing_principles.md
  └─ ... 其他参考文件
  ↓
[动态图表规范]
  ↓
[防死循环规则]
```

---

## 🔍 如何查看加载的 Skills

### 方法一：查看日志

运行 SciAssistant 时，日志中会显示：

```
WriterAgent 启用的 skills: ['scientific-writing']
成功加载 skill: scientific-writing (33556 字符)
✅ 成功集成 1 个 skills 到 WriterAgent
提示词长度: 2500 -> 36056 字符
```

### 方法二：运行时检查

在代码中添加调试输出：

```python
# 在 writer_agent.py 的 _build_system_prompt 方法中
print(f"启用的 skills: {self.enabled_skills}")
print(f"系统提示词长度: {len(system_prompt)}")
print(f"包含 scientific-writing: {'scientific-writing' in system_prompt}")
```

---

## 📁 可用的 Skills 列表

您的项目现在有 **138 个 skills** 可用！

部分常用 skills：

### 📝 写作与报告
- `scientific-writing` - 科学写作核心规范 ⭐
- `citation-management` - 引用管理
- `scientific-slides` - 科学幻灯片
- `scientific-visualization` - 科学可视化
- `literature-review` - 文献综述
- `market-research-reports` - 市场研究报告

### 🔬 数据分析
- `exploratory-data-analysis` - 探索性数据分析
- `statistical-analysis` - 统计分析
- `matplotlib` - 绘图
- `seaborn` - 统计绘图
- `pandas` (polars) - 数据处理

### 🧬 生物信息学
- `biopython` - 生物Python
- `scanpy` - 单细胞分析
- `anndata` - 注释数据
- `scvi-tools` - 单细胞深度学习

### 🧪 化学与药物
- `rdkit` - 化学信息学
- `datamol` - 分子数据处理
- `deepchem` - 深度学习化学

---

## ⚙️ 高级配置

### 自定义 Skills 路径

如果 scientific-skills 目录不在默认位置，可以在初始化时指定：

```python
from deepdiver_v2.src.utils.skill_loader import get_skill_loader

# 指定自定义路径
loader = get_skill_loader(
    skills_base_path="F:\\path\\to\\your\\scientific-skills"
)
```

### 动态启用/禁用 Skills

可以在运行时修改启用的 skills：

```python
# 在 WriterAgent 初始化后
agent.enabled_skills = [
    "scientific-writing",
    "citation-management"
]

# 重新构建系统提示词
agent._build_system_prompt()
```

### 包含/排除 References

默认情况下，SkillLoader 会包含 references 目录下的参考文件。

如果只需要 SKILL.md 核心内容：

```python
skill_content = loader.load_skill(
    "scientific-writing",
    include_references=False  # 不包含参考文件
)
```

---

## 🐛 故障排查

### 问题 1：Skills 未加载

**症状**：日志显示 "Skills 目录不存在"

**解决方案**：
```python
# 检查路径是否正确
from pathlib import Path
skills_path = Path("F:\\Apple Dataset\\scientific-agent-skills-main\\scientific-skills")
print(f"目录存在: {skills_path.exists()}")
```

### 问题 2：提示词过长

**症状**：LLM 返回错误或超时

**解决方案**：
1. 减少启用的 skills 数量
2. 设置 `include_references=False`
3. 在 `skill_loader.py` 中调整参考文件长度限制（默认 3000 字符）

### 问题 3：Skill 内容未生效

**症状**：论文质量没有提升

**解决方案**：
1. 检查日志确认 skill 已加载
2. 查看系统提示词是否包含 skill 内容
3. 确认 LLM 模型支持长上下文（建议至少 32K tokens）

---

## 📈 性能影响

### 提示词长度

- **原始提示词**: ~2,500 字符
- **集成 scientific-writing 后**: ~36,000 字符
- **增加**: ~33,500 字符

### 建议

- 使用支持长上下文的模型（GPT-4, Claude, Qwen 等）
- 如果模型上下文有限，可以只加载核心 SKILL.md，不包含 references

---

## 🎉 总结

您现在拥有了一个**强大的科学写作系统**：

1. ✅ **SciAssistant** 提供完整的 Agent 架构和工作流
2. ✅ **Scientific Agent Skills** 提供顶级的写作规范
3. ✅ **SkillLoader** 自动管理和注入 skills
4. ✅ **WriterAgent** 按照规范生成高质量论文

**祝您科研顺利，发表顶会！🎓📝✨**

---

## 📞 技术支持

如有问题，请检查：
1. 日志输出
2. 运行测试脚本
3. 查看本文档的故障排查部分

**文档版本**: v1.0  
**创建日期**: 2026-05-25  
**最后更新**: 2026-05-25
