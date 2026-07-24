# SciAssistant 与 Scientific-Writing Skill 整合方案

## 📋 项目分析

### 1. SciAssistant 项目现状
**位置**: `F:\scia\SciAssistant-main1-fuben`

**核心架构**:
- **后端**: Flask (app.py) - 提供RESTful API
- **前端**: HTML/JS (chatAi/ai_chat.html)
- **AI Agent系统**: deepdiver_v2/src/agents/
  - `PlannerAgent`: 任务协调器
  - `WriterAgent`: 论文写作代理
  - `InformationSeeker`: 信息检索代理
  - `ExperimentAgent`: 实验数据分析代理
  - `ReviewerAgent`: 论文审查代理

**现有写作能力**:
- WriterAgent 已有基础写作流程(大纲生成→文件分类→分节写作)
- 支持IMRAD结构
- 支持本地文件检索和文献整合
- 但缺少系统化的学术写作规范和最佳实践指导

### 2. Scientific-Writing Skill 核心优势
**位置**: `F:\Apple Dataset\scientific-agent-skills-main\scientific-skills\scientific-writing`

**核心能力**:
1. **完整的IMRAD写作框架** - 详细的章节结构指导
2. **两段式写作流程** - 先大纲(要点)→ 再完整段落(禁止bullet points)
3. **引用管理** - APA/AMA/Vancouver/Chicago/IEEE多种格式
4. **报告规范** - CONSORT/STROBE/PRISMA等研究类型指南
5. **图表规范** - 数据可视化最佳实践
6. **学术写作原则** - 清晰性、简洁性、准确性、客观性
7. **领域特定术语** - 生物医学/化学/物理/神经科学等
8. **期刊格式适配** - Nature/Science/Cell Press等
9. **图形摘要生成** - 强制要求生成visual summary

---

## 🎯 整合目标

将Scientific-Writing Skill的系统化写作规范、最佳实践和工具集成到SciAssistant的WriterAgent中,使其:
1. 遵循国际顶级期刊的写作标准
2. 生成更规范、更专业的学术论文
3. 支持多种引用格式和报告规范
4. 提供可视化的图形摘要和图表
5. 避免常见写作陷阱

---

## 🔧 整合方案(分阶段实施)

### 阶段一:核心写作规范集成(推荐优先实施)

#### 1.1 更新WriterAgent系统提示词

**文件**: `deepdiver_v2/src/agents/writer_agent.py`

**修改位置**: `_build_system_prompt()` 方法(第162行开始)

**需要添加的核心规范**:

```python
# 在_build_system_prompt()方法中,在现有system_prompt_template之前添加:

scientific_writing_guidelines = """
【SCIENTIFIC WRITING MANDATORY RULES (学术写作强制规范)】

## 1. 两段式写作流程(严格遵守)
- **阶段1 - 大纲规划**: 先用bullet points创建结构化大纲(仅作为草稿,不进入最终论文)
- **阶段2 - 完整段落转换**: 将每个bullet point转换为完整的学术段落
  * 使用完整的句子(主语+谓语+宾语)
  * 添加过渡词(however, moreover, in contrast, subsequently)
  * 自然嵌入引用(不要以列表形式呈现)
  * 扩展上下文和解释
  * 确保段落内逻辑流畅
  * 变化句式结构以保持读者 engagement

## 2. 禁止事项(绝对遵守)
❌ 绝不在最终论文中保留bullet points
❌ 绝不在Results或Discussion部分使用编号/项目符号列表
❌ 绝不写不完整的句子或片段
❌ 绝不使用"本章旨在"、"令人惊讶的是"等元话语
❌ 绝不伪造事实、数据或引用

## 3. IMRAD结构标准
- **Introduction**: 
  * 建立研究背景和问题重要性
  * 系统回顾相关文献
  * 识别知识空白
  * 明确研究问题/假设
  * 背景介绍不超过3段,第3段必须直接说明本研究的具体创新点和目标
- **Methods**: 
  * 详细的参与者/样本描述
  * 清晰的程序文档
  * 统计方法及理由
  * 伦理批准声明
- **Results**: 
  * 从主要到次要结果的逻辑流
  * 与图表整合
  * 统计显著性+效应量
  * 客观报告,不做解释
- **Discussion**: 
  * 结果与研究问题的关联
  * 与现有文献对比
  * 诚实承认局限性
  * 提出机制性解释
  * 建议实际意义和未来研究方向

## 4. 引用格式规范
- 优先引用原始文献
- 包含近5-10年的文献(活跃领域)
- 验证所有引用的准确性
- 根据目标期刊选择格式(APA/AMA/Vancouver/IEEE)

## 5. 学术写作原则
- **清晰性**: 使用精确、无歧义的语言;首次使用时定义技术术语和缩写
- **简洁性**: 消除冗余词汇;favor短句子(平均15-20词);严格遵守字数限制
- **准确性**: 报告精确值;使用一致术语;区分观察和解释
- **客观性**: 无偏见呈现结果;避免夸大发现;承认矛盾证据

## 6. 图表规范
- 每个图表必须自解释(完整的标题和说明)
- 所有轴、列、行标注单位
- 包含样本量(n)和统计注释
- 遵循"每1000词一个图表"指南
- 避免在文本、表格、图表间重复信息

## 7. 强制图形生成
⚠️ 每篇科学论文 MUST 包含:
1. **图形摘要(Graphical Abstract)** - 视觉化总结整个论文
2. **方法流程图** - 研究设计的可视化
3. **结果可视化** - 关键数据的图表展示
"""

# 然后在system_prompt_template中引用这个变量:
system_prompt_template = f"""You are an ambitious AI PhD student...

{scientific_writing_guidelines}

【CRITICAL WRITING ORCHESTRATION RULES (论文统筹与学术铁律)】
...
"""
```

#### 1.2 创建写作规范参考文件

**在SciAssistant项目中创建目录结构**:
```
deepdiver_v2/
└── resources/
    └── scientific_writing/
        ├── imrad_structure.md          # IMRAD结构详细指南
        ├── citation_styles.md          # 引用格式指南
        ├── reporting_guidelines.md     # 报告规范(CONSORT/STROBE/PRISMA)
        ├── writing_principles.md       # 写作原则
        └── field_terminology.md        # 领域术语规范
```

**PowerShell命令**:
```powershell
# 创建目录
New-Item -ItemType Directory -Path "F:\scia\SciAssistant-main1-fuben\deepdiver_v2\resources\scientific_writing" -Force

# 复制参考文档
Copy-Item "F:\Apple Dataset\scientific-agent-skills-main\scientific-skills\scientific-writing\references\*" `
          "F:\scia\SciAssistant-main1-fuben\deepdiver_v2\resources\scientific_writing\" -Recurse
```

---

### 阶段二:图形摘要和可视化工具集成

#### 2.1 集成图形生成脚本

**PowerShell命令**:
```powershell
# 复制图形生成脚本到SciAssistant
Copy-Item "F:\Apple Dataset\scientific-agent-skills-main\scientific-skills\scientific-writing\scripts\generate_schematic.py" `
          "F:\scia\SciAssistant-main1-fuben\deepdiver_v2\src\tools\"

Copy-Item "F:\Apple Dataset\scientific-agent-skills-main\scientific-skills\scientific-writing\scripts\generate_image.py" `
          "F:\scia\SciAssistant-main1-fuben\deepdiver_v2\src\tools\"
```

#### 2.2 在MCP Server中注册图形生成工具

**文件**: `deepdiver_v2/src/tools/mcp_server_standard.py`

**添加以下工具函数**:

```python
@mcp.tool()
async def generate_graphical_abstract(
    paper_title: str,
    key_findings: str,
    output_path: str = "figures/graphical_abstract.png"
) -> str:
    """
    生成论文的图形摘要(Graphical Abstract)
    
    Args:
        paper_title: 论文标题
        key_findings: 关键发现(用逗号分隔)
        output_path: 输出路径
    
    Returns:
        生成的图形文件路径
    """
    import subprocess
    prompt = f"Graphical abstract for {paper_title}: {key_findings}"
    
    result = subprocess.run([
        "python", "deepdiver_v2/src/tools/generate_schematic.py",
        prompt, "-o", output_path
    ], capture_output=True, text=True)
    
    if result.returncode == 0:
        return f"图形摘要生成成功: {output_path}"
    else:
        return f"图形摘要生成失败: {result.stderr}"

@mcp.tool()
async def generate_scientific_figure(
    description: str,
    figure_type: str = "schematic",
    output_path: str = "figures/figure.png"
) -> str:
    """
    生成科学图表
    
    Args:
        description: 图表描述
        figure_type: 图表类型(schematic或image)
        output_path: 输出路径
    
    Returns:
        生成的图表文件路径
    """
    import subprocess
    
    if figure_type == "schematic":
        script = "generate_schematic.py"
    else:
        script = "generate_image.py"
    
    result = subprocess.run([
        "python", f"deepdiver_v2/src/tools/{script}",
        description, "-o", output_path
    ], capture_output=True, text=True)
    
    if result.returncode == 0:
        return f"科学图表生成成功: {output_path}"
    else:
        return f"科学图表生成失败: {result.stderr}"
```

#### 2.3 更新WriterAgent工作流

**在WriterAgent的_system_prompt中的【MANDATORY WORKFLOW】部分添加**:

```markdown
【MANDATORY FIGURE GENERATION (强制图形生成)】

⚠️ 每篇科学论文 MUST 包含以下图形元素:

1. **图形摘要(Graphical Abstract)** - 在论文开头生成
   - 视觉化总结整个论文的关键信息
   - 适合期刊目录展示
   - 横向布局(1200x600px)
   - 包含3-5个关键步骤/概念

2. **方法流程图** - 在Methods部分
   - 研究设计的可视化流程
   - 实验步骤图解

3. **结果可视化** - 在Results部分
   - 关键数据的图表展示
   - 统计结果的可视化

**使用示例**:
在写作过程中调用图形生成工具:
- 完成大纲后,立即生成图形摘要
- 在Methods章节写作时,生成方法流程图
- 在Results章节写作时,生成结果可视化图表
```

---

### 阶段三:引用管理和格式化工具

#### 3.1 创建引用格式化工具

**创建新文件**: `deepdiver_v2/src/tools/citation_formatter.py`

**核心代码**:

```python
"""
引用格式化工具 - 支持多种学术引用格式
"""

class CitationFormatter:
    """引用格式转换器"""
    
    @staticmethod
    def format_citation(citation_data: dict, style: str = "APA") -> str:
        """
        格式化引用
        
        Args:
            citation_data: 引用数据字典
                {
                    "authors": ["Smith, J.", "Jones, M."],
                    "title": "Article Title",
                    "journal": "Journal Name",
                    "year": 2024,
                    "volume": "10",
                    "issue": "2",
                    "pages": "100-110",
                    "doi": "10.1000/xyz"
                }
            style: 引用格式(APA/AMA/Vancouver/IEEE)
        
        Returns:
            格式化后的引用字符串
        """
        if style == "APA":
            return CitationFormatter._apa_format(citation_data)
        elif style == "AMA":
            return CitationFormatter._ama_format(citation_data)
        elif style == "Vancouver":
            return CitationFormatter._vancouver_format(citation_data)
        elif style == "IEEE":
            return CitationFormatter._ieee_format(citation_data)
        else:
            raise ValueError(f"Unsupported citation style: {style}")
    
    @staticmethod
    def _apa_format(data: dict) -> str:
        """APA格式"""
        authors = ", ".join(data.get("authors", []))
        title = data.get("title", "")
        journal = data.get("journal", "")
        year = data.get("year", "")
        volume = data.get("volume", "")
        pages = data.get("pages", "")
        
        return f"{authors} ({year}). {title}. {journal}, {volume}, {pages}."
    
    @staticmethod
    def _ama_format(data: dict) -> str:
        """AMA格式(上标数字)"""
        authors = ", ".join(data.get("authors", []))
        title = data.get("title", "")
        journal = data.get("journal", "")
        year = data.get("year", "")
        
        return f"{authors}. {title}. {journal}. {year}."
    
    @staticmethod
    def _vancouver_format(data: dict) -> str:
        """Vancouver格式(方括号数字)"""
        authors = ", ".join(data.get("authors", []))
        title = data.get("title", "")
        journal = data.get("journal", "")
        year = data.get("year", "")
        volume = data.get("volume", "")
        pages = data.get("pages", "")
        
        return f"{authors}. {title}. {journal}. {year};{volume}:{pages}."
    
    @staticmethod
    def _ieee_format(data: dict) -> str:
        """IEEE格式"""
        authors = ", ".join(data.get("authors", []))
        title = data.get("title", "")
        journal = data.get("journal", "")
        year = data.get("year", "")
        volume = data.get("volume", "")
        pages = data.get("pages", "")
        
        return f"{authors}, \"{title},\" {journal}, vol. {volume}, pp. {pages}, {year}."
```

#### 3.2 在MCP Server中注册

**文件**: `deepdiver_v2/src/tools/mcp_server_standard.py`

**添加**:

```python
from .citation_formatter import CitationFormatter

@mcp.tool()
async def format_citation(
    citation_data: dict,
    style: str = "APA"
) -> str:
    """
    格式化学术引用
    
    Args:
        citation_data: 引用数据
            {
                "authors": ["作者1", "作者2"],
                "title": "文章标题",
                "journal": "期刊名",
                "year": 2024,
                "volume": "卷号",
                "pages": "页码"
            }
        style: 引用格式(APA/AMA/Vancouver/IEEE)
    
    Returns:
        格式化后的引用
    """
    try:
        formatted = CitationFormatter.format_citation(citation_data, style)
        return formatted
    except Exception as e:
        return f"引用格式化失败: {str(e)}"
```

---

### 阶段四:期刊模板和写作风格适配

#### 4.1 创建期刊风格指南

**位置**: `deepdiver_v2/resources/journal_styles/`

**PowerShell命令**:
```powershell
# 创建期刊风格目录
New-Item -ItemType Directory -Path "F:\scia\SciAssistant-main1-fuben\deepdiver_v2\resources\journal_styles" -Force

# 如果scientific-agent-skills中有venue-templates skill,复制过来
# Copy-Item "F:\Apple Dataset\scientific-agent-skills-main\scientific-skills\venue-templates\*" `
#           "F:\scia\SciAssistant-main1-fuben\deepdiver_v2\resources\journal_styles\" -Recurse
```

#### 4.2 更新PlannerAgent支持期刊选择

**文件**: `deepdiver_v2/src/agents/planner_agent.py`

**在系统提示的【Critical Protocols】部分添加**:

```markdown
【JOURNAL-SPECIFIC FORMATTING (期刊特定格式)】

在开始写作前,确认目标期刊并适配其风格:

| 期刊类型 | 写作风格特点 |
|---------|------------|
| **Nature/Science** | 通俗易懂,故事驱动,强调广泛意义 |
| **Cell Press** | 机制深度,图形摘要,Highlights |
| **医学期刊(NEJM, Lancet)** | 结构化摘要,证据语言 |
| **ML会议(NeurIPS, ICML)** | 贡献要点,消融实验 |
| **CS会议(CHI, ACL)** | 领域特定惯例 |

**工作流程**:
1. 确认用户目标期刊(如未指定,默认使用通用IMRAD格式)
2. 应用该期刊的特定写作风格
3. 遵循期刊的作者指南(字数限制、格式要求、图表规范)
```

---

## 📁 文件结构变更总结

整合后的SciAssistant项目结构:

```
SciAssistant-main1-fuben/
├── deepdiver_v2/
│   ├── src/
│   │   ├── agents/
│   │   │   ├── writer_agent.py           # ✏️ 更新:集成科学写作规范
│   │   │   ├── planner_agent.py          # ✏️ 更新:支持期刊选择
│   │   │   └── ...
│   │   ├── tools/
│   │   │   ├── mcp_server_standard.py    # ✏️ 更新:注册新工具
│   │   │   ├── generate_schematic.py     # ➕ 新增:图形生成
│   │   │   ├── generate_image.py         # ➕ 新增:图像生成
│   │   │   └── citation_formatter.py     # ➕ 新增:引用格式化
│   │   └── ...
│   ├── resources/                        # ➕ 新增:资源目录
│   │   ├── scientific_writing/
│   │   │   ├── imrad_structure.md
│   │   │   ├── citation_styles.md
│   │   │   ├── reporting_guidelines.md
│   │   │   ├── writing_principles.md
│   │   │   └── field_terminology.md
│   │   └── journal_styles/
│   │       └── ...
│   └── ...
└── ...
```

---

## 🚀 实施步骤(推荐顺序)

### 第1步:备份现有代码(必须)
```powershell
# 备份writer_agent.py
Copy-Item "F:\scia\SciAssistant-main1-fuben\deepdiver_v2\src\agents\writer_agent.py" `
          "F:\scia\SciAssistant-main1-fuben\deepdiver_v2\src\agents\writer_agent.py.backup"

# 备份planner_agent.py
Copy-Item "F:\scia\SciAssistant-main1-fuben\deepdiver_v2\src\agents\planner_agent.py" `
          "F:\scia\SciAssistant-main1-fuben\deepdiver_v2\src\agents\planner_agent.py.backup"

# 备份mcp_server_standard.py
Copy-Item "F:\scia\SciAssistant-main1-fuben\deepdiver_v2\src\tools\mcp_server_standard.py" `
          "F:\scia\SciAssistant-main1-fuben\deepdiver_v2\src\tools\mcp_server_standard.py.backup"
```

### 第2步:创建资源目录并复制参考文档
```powershell
# 创建资源目录
New-Item -ItemType Directory -Path "F:\scia\SciAssistant-main1-fuben\deepdiver_v2\resources\scientific_writing" -Force

# 复制参考文档
Copy-Item "F:\Apple Dataset\scientific-agent-skills-main\scientific-skills\scientific-writing\references\*" `
          "F:\scia\SciAssistant-main1-fuben\deepdiver_v2\resources\scientific_writing\" -Recurse
```

### 第3步:更新WriterAgent系统提示词
- 按照阶段一1.1节的说明修改`writer_agent.py`
- 在`_build_system_prompt()`方法中添加scientific_writing_guidelines
- 测试写作流程是否正常

### 第4步:集成图形生成工具(可选)
```powershell
# 复制图形生成脚本
Copy-Item "F:\Apple Dataset\scientific-agent-skills-main\scientific-skills\scientific-writing\scripts\generate_schematic.py" `
          "F:\scia\SciAssistant-main1-fuben\deepdiver_v2\src\tools\"

Copy-Item "F:\Apple Dataset\scientific-agent-skills-main\scientific-skills\scientific-writing\scripts\generate_image.py" `
          "F:\scia\SciAssistant-main1-fuben\deepdiver_v2\src\tools\"
```
- 注册MCP工具(在mcp_server_standard.py中添加)
- 测试图形生成功能

### 第5步:集成引用格式化工具(可选)
- 创建`citation_formatter.py`
- 在mcp_server_standard.py中注册工具
- 测试引用格式化功能

### 第6步:全面测试
- 使用真实论文任务测试
- 验证IMRAD结构是否正确
- 检查引用格式是否准确
- 确认图表生成是否正常
- 测试不同期刊风格的适配

---

## ⚠️ 注意事项

### 1. 兼容性
- 确保Python版本兼容(建议3.9+)
- 检查新工具的依赖包(如matplotlib, PIL等)
- 在Windows环境下测试所有路径

### 2. 性能影响
- 图形生成可能增加处理时间
- 建议在后台异步执行
- 考虑添加进度反馈

### 3. 用户体验
- 在前端添加进度指示器
- 提供写作质量反馈机制
- 允许用户选择引用格式和目标期刊

### 4. 维护性
- 所有新增功能添加详细注释
- 创建单元测试
- 记录已知问题和解决方案

### 5. 关键保护
- ⚠️ **修改前必须备份**所有文件
- ⚠️ **逐步测试**,不要一次性修改所有内容
- ⚠️ **保留原有功能**,新增功能是增强而不是替换
- ⚠️ **检查路径兼容性**,Windows使用反斜杠

---

## 📊 预期效果

### 写作质量提升
- ✅ 遵循国际顶级期刊标准
- ✅ 消除bullet points等不规范格式
- ✅ 增强段落逻辑性和流畅度
- ✅ 提高引用准确性

### 功能增强
- ✅ 自动生成图形摘要
- ✅ 支持多种引用格式
- ✅ 适配不同期刊风格
- ✅ 遵循报告规范(CONSORT等)

### 用户满意度
- ✅ 生成更专业的学术论文
- ✅ 减少后期编辑工作量
- ✅ 提高论文接受率
- ✅ 增强系统可信度

---

## 🔍 后续优化方向

1. **AI辅助审查**: 集成ReviewerAgent进行写作质量自动审查
2. **实时反馈**: 在写作过程中提供实时格式检查
3. **模板库**: 创建常用论文模板库
4. **协作写作**: 支持多Agent协作写作不同章节
5. **版本控制**: 集成论文版本管理和对比功能
6. **前端优化**: 在ai_chat.html中添加期刊选择和引用格式选择UI

---

## 📞 技术支持

如在整合过程中遇到问题,可以参考:
- Scientific-Writing Skill完整文档: `F:\Apple Dataset\scientific-agent-skills-main\scientific-skills\scientific-writing\SKILL.md`
- SciAssistant项目文档: `F:\scia\SciAssistant-main1-fuben\README.md`
- 相关参考文件: `deepdiver_v2/resources/scientific_writing/`

---

**文档版本**: v1.0  
**创建日期**: 2026-05-25  
**作者**: AI Assistant  
**状态**: 待实施
