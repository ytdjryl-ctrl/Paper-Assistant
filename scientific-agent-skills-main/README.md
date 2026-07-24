# Scientific Agent Skills for SciAssistant

## 简介

本目录包含 138 个科学写作和研究 skills，用于增强 SciAssistant 的学术论文写作能力。

## 核心 Skills

### 写作相关（已启用）
- **scientific-writing**: 科学写作核心规范（IMRAD结构、两段式写作、引用格式）
- **citation-management**: 引用管理（APA/AMA/Vancouver/IEEE等格式）
- **paper-lookup**: 文献检索（PubMed、arXiv等10个学术数据库）
- **literature-review**: 文献综述方法论

### 可选 Skills（按需启用）
- **scientific-visualization**: 科学可视化
- **scientific-schematics**: 科学图表生成
- **venue-templates**: 期刊模板（Nature/Science/Cell等）
- **peer-review**: 同行评审
- **scientific-brainstorming**: 科学头脑风暴
- **hypothesis-generation**: 假设生成

## 配置

在 `deepdiver_v2/config/skills_config.yaml` 中配置启用的 skills。

## 使用

Skills 会自动通过 `skill_loader.py` 加载并注入到 WriterAgent 的系统提示词中。

## 完整文档

原始项目: https://github.com/K-Dense-AI/scientific-agent-skills
