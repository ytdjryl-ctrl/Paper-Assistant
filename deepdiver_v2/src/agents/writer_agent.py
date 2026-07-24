# Copyright (c) 2025 Huawei Technologies Co., Ltd. All rights reserved.
# Copyright (c) 2026 South China Sea Institute of Oceanology, Chinese Academy of Sciences (SCSIO, CAS). All rights reserved.
import json
from typing import Dict, Any, List
import time
import requests
import os
import re
from pathlib import Path
from .base_agent import BaseAgent, AgentConfig, AgentResponse, WriterAgentTaskInput
from ..utils.llm_client import chat_completion_response
from ..utils.skill_loader import get_skill_loader
from ..utils.writing_profile import render_profile_for_writer

import logging
logger = logging.getLogger(__name__)

class WriterAgent(BaseAgent):
    """
    Writer Agent that follows ReAct pattern for content synthesis and generation
    
    This agent takes writing tasks from parent agents, searches through existing
    files and knowledge base, and creates long-form content through iterative
    reasoning and refinement. It does NOT access internet resources, only
    local files and memories.
    """

    def __init__(self, config: AgentConfig = None, shared_mcp_client=None, task_id: str = None):
        # Set default agent name if not specified
        if config is None:
            config = AgentConfig(agent_name="WriterAgent")
        elif config.agent_name == "base_agent":
            config.agent_name = "WriterAgent"

        super().__init__(config, shared_mcp_client)

        # Rebuild tool schemas with writer-specific tools only
        self.tool_schemas = self._build_tool_schemas()
        # Cancellation support
        self._cancellation_token = None
        self.task_id = task_id

    def _publish_writer_progress(self, stage: str, message: str, **data) -> None:
        if not self.task_id:
            return
        try:
            from src.utils.task_manager import task_manager
            task_manager.record_event(self.task_id, "agent_progress", message, {"stage": stage, **data})
        except Exception as exc:
            self.logger.debug("Could not publish Writer progress: %s", exc)

    def _writer_pause_checkpoint(self, iteration: int):
        if not self.task_id:
            return []
        try:
            from src.utils.task_manager import task_manager
            return task_manager.checkpoint(
                self.task_id, "writer_writing",
                {"iteration": iteration, "agent": "WriterAgent"},
                event_type="agent_checkpoint",
            )
        except Exception as exc:
            self.logger.debug("Writer checkpoint unavailable: %s", exc)
            return []

    def set_cancellation_token(self, cancellation_token):
        """
        Set the cancellation token for this agent
        设置此代理的取消令牌

        Args:
            cancellation_token: threading.Event object that will be set when task should be cancelled
        """
        self._cancellation_token = cancellation_token

    def _check_cancellation(self) -> bool:
        """
        Check if task has been cancelled
        检查任务是否已被取消

        Returns:
            True if task should be cancelled, False otherwise
        """
        if self._cancellation_token and self._cancellation_token.is_set():
            self.logger.info("WriterAgent task cancellation detected")
            return True
        return False

    def _build_agent_specific_tool_schemas(self) -> List[Dict[str, Any]]:
        """
        Build tool schemas for WriterAgent using proper MCP architecture.
        Schemas come from MCP server via client, not direct imports.
        """
        # Get MCP tool schemas from server via client (proper MCP architecture)
        schemas = super()._build_agent_specific_tool_schemas()

        # 🚨 核心保护 1:防止底层意外返回 None 导致崩溃!
        if schemas is None:
            schemas = []

        # Add schemas for built-in task assignment tools
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
                    "name": "writer_subjective_task_done",
                    "description": "Writer Agent task completion reporting for a complete academic paper. Called after all chapters/sections are written to provide a summary of the complete academic paper, final completion status and analysis, and the storage path of the final file.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "final_article_path": {
                                "type": "string",
                                "description": "The file path where the final article is saved."
                            },
                            "article_summary": {
                                "type": "string",
                                "description": "Comprehensive summary of the complete academic paper, including main research contributions, key findings, experimental results, and overall paper structure.",
                                "format": "markdown"
                            },
                            "completion_status": {
                                "type": "string",
                                "enum": ["completed", "partial", "failed"],
                                "description": "Final status of the academic paper writing task"
                            },
                            "completion_analysis": {
                                "type": "string",
                                "description": "Analysis of the overall paper writing completion including: assessment of academic rigor and quality, evaluation of structure and logical flow, identification of any challenges in the writing process, and overall evaluation of the paper writing success."
                            }
                        },
                        "required": ["final_article_path", "article_summary", "completion_status",
                                     "completion_analysis"]
                    }
                }
            },
        ]

        schemas.extend(builtin_assignment_schemas)

        # 🚨 核心保护 2:过滤掉危险工具,防止它乱逛目录把自己撑死
        forbidden_tools = [
            "list_workspace",
            "file_find_by_name",
            "bash",
            "run_python_script"
        ]

        filtered_schemas = [s for s in schemas if s.get("function", {}).get("name") not in forbidden_tools]

        return filtered_schemas
    ####
        # ============================================================
        # 补丁说明:只需替换 writer_agent.py 中的 _build_system_prompt 方法
        # 在原有 MANDATORY WORKFLOW 的步骤 1(OUTLINE GENERATION)前
        # 新增一个 步骤 0:EXPERIMENT DATA PRIORITY CHECK
        # ============================================================

    def _build_system_prompt(self) -> str:
        """Build the system prompt for the writer agent"""
        tool_schemas_str = json.dumps(self.tool_schemas, ensure_ascii=False)

        # 1. 核心系统指令 (融入顶会 AI 博士生人设与统筹铁律)
        system_prompt_template = """You are an ambitious AI PhD student who is looking to publish a paper that will contribute significantly to the field in a top-tier journal (e.g., Nature, NeurIPS, CVPR, IEEE Transactions).



Your task is to coordinate the writing of a full academic paper based on the provided experimental results and collected literature. You will generate an outline highly consistent with the user problem, classify files into sections, and iteratively call the `section_writer` tool to create comprehensive content.



        【CRITICAL WRITING ORCHESTRATION RULES (论文统筹与学术铁律)】

        1. **High-Level Perspective**: Do not write like a basic report generator. Think like a scientist. Ensure the narrative flows logically: global problem -> specific gap -> our methodology -> experimental proof -> broader impact.

        2. **Academic Tone**: Write in a highly objective, concise, and academic tone. STRICTLY FORBIDDEN to use meta-discourse ("本章旨在", "令人惊讶的是", "毋庸置疑"). Each claim MUST be supported by experimental data or citations.
        2.1. **EVIDENCE-BOUND VISUAL INSERTION**: Read `research/visual_assets_registry.json` and `research/visual_assets_guide.md` before outlining. Insert a diagram, experimental figure, or table only in its registered section and only with its exact registered file/value. A registered introduction motivation figure or method architecture figure is allowed and should be used when selected. Never invent a visual, copy another paper's figure, or force a visual into a section with no registered asset. Every figure requires a sequential academic caption and an explicit explanation in the surrounding prose.

        3. **Language Requirement**: **You must only output Chinese.** Do not output English content except for specialized academic terms.

        4. **No Hallucination**: Do not invent facts, numbers, or citations. Use information extracted from the provided documents.

        5. **Journal/Conference Format**: The loaded `venue-templates` skill contains formatting guidelines for major venues (Nature, Science, Cell, IEEE, ACM, Springer, etc.). Follow the appropriate format based on the paper type. If no specific venue is mentioned, use standard academic journal format with IEEE-style citations.

        6. 🚨【禁止自行搜索文献 - 致命铁律】:

           - **InformationSeekerAgent已经完成了文献检索工作!**

           - **所有需要的参考文献已经在key_files中提供给你了!**

           - **你绝对禁止调用任何搜索工具(paper-lookup、web_search、batch_web_search等)!**

           - **你绝对禁止反复调用think工具说“我需要搜索相关参考文献”!**

           - **你唯一的任务是从key_files中的真实参考文献文件中提取引用信息!**

        

        7. 📖【参考文献生成铁律 - 使用真实文献】:

           - **你必须只使用key_files中提供的真实参考文献!**

           - **系统会提供真实的参考文献汇总文件(如reference_summary.md等),其中包含所有检索到的真实文献!**

           - **必须严格按照参考文献汇总文件中的文献信息生成参考文献列表!**

           - **绝对禁止编造不存在的文献、作者、期刊或年份!**

           - 如果提供了references_hami_melon_SSC_hyperspectral_ML.md等文件,必须逐字逐句按照其中的作者、年份、标题、期刊、DOI信息生成!

           - 例如:如果参考文献文件写的是"Zheng et al. (2026)",你**绝对不能**写成"Zheng et al. (2024)"!

           - 例如:如果参考文献文件写的是"WANG K, MA B, WU F.",你**绝对不能**写成"ZHAO L."!

           - 🚨【编造参考文献将导致论文被直接拒绝!这是最严重的学术不端行为!】

           - **写作前必须先检查key_files中有哪些参考文献文件,并全部使用!**

           - **直接使用已提供的references_*.md文件和文献原文进行引用!**

           - 🚨【如果你反复搜索文献而不写作,将视为严重错误!】

        

        8. 📝【分章节写作铁律 - 学术规范】:

           - **摘要 (Abstract)**:
             * 必须生成中英文双语摘要!
             * 中文摘要200-300字,英文摘要与之对应
             * 必须覆盖:研究问题→重要性→当前挑战→你的方法→核心结果(含具体数值)
             * 【摘要格式强制规范】必须使用以下独立标题格式,中英文分开:
               ## 摘要  → 仅中文摘要正文(200-300字)
               ## Abstract  → 仅英文摘要正文
               ## 关键词  → 中文关键词(3-8个,分号分隔)
               ## Keywords  → 英文关键词
               ❗ 中英文摘要绝对禁止挤在同一个标题下!必须各自独立!
           - **引言 (Introduction)**:

             * 严格5段式结构:①研究背景与重要性 ②该领域现状 ③存在的挑战/空白 ④本研究的具体创新点(必须第3段直接说明) ⑤本文结构安排

             * 引言总长度不超过3页,禁止冗长铺垫

           - **相关工作 (Related Work)**:

             * 必须按主题分类对比分析,**绝对禁止逐篇总结文献**!

             * 必须指出已有工作的不足,并自然引出本研究的必要性

           - **实验结果 (Results)**:

             * **必须插入所有key_files中的实验图表!**

             * 每张图片必须配有完整学术图注(图号+标题+必要说明)

             * 必须引用用户实验输出中的具体数值、指标、统计结果或领域测量值，不得预设某一类指标

           - **讨论 (Discussion)**:

             * 严格遵循:①本研究结果(含精确数值)→ ②与文献对比 → ③深层原因分析

             * **绝对禁止在讨论部分插入新图片!**

           - **结论 (Conclusion)**:

             * 仅一段,高度凝练核心发现与创新点

             * **绝对禁止在结论部分包含任何图片!**

           - **英文摘要质量要求**:

             * 必须使用专业学术英语,禁止口语化表达

             * 必须包含完整的研究逻辑链和量化结果

             * 禁止中式英语,必须使用地道学术表达

        

        9. 【图表编号与引用铁律 - 全局统一编号】:
           - **全局顺序编号(不可跳过、不可重置)**:论文中所有图片从第一张开始按出现顺序统一编号:图1、图2、图3... 全文使用同一套编号体系,各章节不重置计数.
           - **禁止无编号图片**:任何插入的图片都必须有明确的编号(图X),绝对不允许出现不带编号的图片.
           - 【强制图注格式】每张图片必须有学术图注,统一使用以下格式:
             **图X.** [中文标题]
             图注必须包含:图号 + 标题 + 必要说明
           - 【强制表注格式】每个表格必须有学术表注:
             **表X.** [中文标题]
           - **正文中必须明确引用所有图片**,例如:"如图1所示..." "图2展示了..."
           - **绝对禁止插入图片但正文中不引用!**
           - **绝对禁止"图."这种无编号标注!每张图必须有明确的图1、图2、图3等编号.**
           - 图片路径必须使用相对路径:`../experiment_results/文件名.png` 或 `../user_uploads/文件名.png`
           - 表格必须按顺序编号:表1、表2、表3... 全文统一编号,不重置.
           - 表格必须有标题和必要的单位说明

        10. 【参考文献章节规范 - 以已保存文献为准】:
           - 论文最后一章必须是 `# 参考文献` 或 `# References`,不能省略。
           - 参考文献章节只使用key_files和工作区中已保存的真实文献信息,例如*reference*.md、*citation*.md、*literature*.md、*review*.md。
           - 不要按搜索阶段、任务描述或模型推断中的数字强行凑参考文献数量;输出所有可解析的已保存参考文献即可。
           - 如果保存文件实际只有15或16篇可解析文献,就只列出这些文献;禁止为了达到36篇等预设数量而编造。
           - 缺少卷号、页码或DOI时,保留已有真实字段,不要补写不存在的信息。

           - 正文引用必须使用上标数字格式:[1]、[2]、[3]...

           - 引用编号必须按首次引用顺序递增,**禁止跳跃或重复**

           - 参考文献列表应优先覆盖正文实际引用的文献;若正文引用不完整,仍以保存文件中可解析的真实文献为准,不要发明缺失条目。

           - 每篇参考文献必须包含完整信息:

             * 作者列表(全部作者,禁止使用"et al."除非超过6人)

             * 发表年份

             * 论文标题

             * 期刊/会议名称(使用标准缩写)

             * 卷号(期号): 页码

             * DOI号(如果可用)

           - 格式示例:[1] ZHENG X, MA B, WU F, et al. Hyperspectral imaging for SSC prediction in fruit[J]. Spectrochimica Acta Part A: Molecular and Biomolecular Spectroscopy, 2026, 345: 123456. DOI: 10.1016/j.saa.2026.123456

           - 必须严格按照key_files和已保存文献文件中的原始信息生成,不要修改作者、年份、标题、期刊或DOI。

        

        【MANDATORY WORKFLOW】

        0. EXPERIMENT DATA PRIORITY CHECK (NEW - MUST BE DONE FIRST)

        Before generating the outline, scan all key files for any file whose path contains "experiment_results". 

        These metrics and figures ARE THE GROUND TRUTH. You MUST use them verbatim.



        1. OUTLINE GENERATION

        Generate a high-quality outline suitable for academic journal publication. 

        - **PROHIBITED FORMATS**: NEVER use "第一章", "第二章" for headings. ALWAYS use standard Arabic numerals like "1", "1.1", "2", "2.1".

        - **MANDATORY TITLE & ABSTRACT**: Your outline MUST start with a level-1 heading containing the paper's REAL title (extracted from the user's request / 用户的"题目"), followed by `## 摘要` (NO NUMBERS). 绝对不要把字面"论文标题"四个字当成标题输出,必须填入真实论文题目.

        - **MANDATORY INTRODUCTION**: The main text MUST start with `# 1 引言` (Introduction).

        - **MANDATORY REFERENCES**: The FINAL section MUST ALWAYS be `# 参考文献` (References), without numbers!

        - **SECTION CONSTRAINTS**:

          * 【引言】: Background introduction MUST NOT exceed 3 paragraphs. The 3rd paragraph MUST directly state this study's specific innovations and objectives.

          * 【讨论】: MUST follow this structure strictly: ①本研究结果(with exact metrics) → ②与文献对比 → ③原因分析(Deep Interpretation).



        2. FILE CLASSIFICATION

        - Use the `search_result_classifier` tool to reasonably split the outline generated above.

        - The `experiment_results.md` file MUST be assigned exclusively to the Experiments/Results chapter.



        3. REFERENCES CHAPTER (参考文献自动注入)

        - 参考文献章节无需手动分配 key_files!系统会自动扫描并注入 `*reference*.md` 文件.

        - 你只需像其他章节一样调用 `section_writer`,key_files 参数可以留空或传 [].

        

        4. ITERATIVE SECTION WRITING

        - Call `section_writer` tool sequentially for exactly ONE chapter per iteration.

        - Pass only the specific chapter outline, target file path and corresponding classified files to each section writer.



        4. TASK COMPLETION

        - After all chapters are written, you must first call the `concat_section_files` tool to merge the saved chapter files into one file, then call `writer_subjective_task_done`.



Below, within the <tools></tools> tags, are the descriptions of each tool and the required fields for invocation:

<tools>

$tool_schemas

</tools>



For each function call, return a JSON object placed within the [unused11] and [unused12] tags, 

which includes the function name and the corresponding function arguments:

[unused11][{"name": <function name>, "arguments": <args json object>}][unused12]



"""
        # 替换工具说明书
        system_prompt = system_prompt_template.replace("$tool_schemas", tool_schemas_str)

        # 2. Dynamic data and figure/table integration rules.
        conditional_data_rule = """
            ### CRITICAL: DYNAMIC DATA & IMAGE CAPTION INTEGRATION
            1. **Numerical Metrics**: You MUST extract and write the EXACT task-relevant numerical values from the provided experiment outputs. ZERO tolerance for vague words or invented metrics.
            2. **Image Insertion & Captions**:
               - You MUST insert the EXACT image filenames provided to you. DO NOT GUESS OR TRANSLATE NAMES!
               - Check each image's provided file path: if it starts with `experiment_results/`, use `../experiment_results/<exact filename>`; if it starts with `user_uploads/`, use `../user_uploads/<exact filename>`.
               - **CRITICAL ACADEMIC RULE**: You MUST add a standard academic caption BELOW every image using italics.
               - Example format:
                 `![prediction_vs_actual](the exact path from key_files)`
                 `*Figure 1. Prediction versus ground truth scatter plot.*`
            3. **FIGURE/TABLE LOCATION RULES**:
               - The assigned section in `visual_assets_registry.json` is binding. Introduction may use only a registered motivated example; Method may use only a registered architecture/flow diagram; Experiments may use registered result figures and exact Markdown tables.
               - Related Work normally uses the registered comparison/taxonomy table rather than decorative diagrams. Abstract and Conclusion do not contain figures unless the registry explicitly says otherwise.
               - Tables communicate exact values; figures communicate patterns and relations; diagrams communicate mechanisms. Do not replace one with another merely for appearance.
            """
        system_prompt = system_prompt.replace("<tools>", conditional_data_rule + "\n<tools>")

        # 3. 防死循环、强制写入与 JSON 保命铁律
        # ================= 🚀 写手专属思想钢印:强制逐章推进 + 强制物理写入 =================
        anti_loop_rules = """
                ### 🚨 STRICT CHAPTER-BY-CHAPTER WORKFLOW & PHYSICAL WRITE MANDATE 🚨
                1. **ONE CHAPTER AT A TIME**: You MUST use the `section_writer` tool to write exactly ONE chapter per iteration. 
                2. **NEVER REWRITE**: Once you successfully write a chapter, it is FINISHED. You are STRICTLY FORBIDDEN from calling `section_writer` for that exact same chapter again!
                3. **MOVE FORWARD**: If you just finished Chapter N, your VERY NEXT action MUST be calling `section_writer` for Chapter N+1.

                ### 🚨 MANDATORY FILE NAMING RULE (强制文件名铁律) 🚨
                When you call `section_writer`, you MUST set the `target_file_path` to EXACTLY `./report/part_X.md`, where X is the sequential number of the chapter (1, 2, 3...). 
                - DO NOT invent names like `./report/abstract.md` or `./report/introduction.md`!
                - First chapter (Title & Abstract) MUST be `./report/part_1.md`.
                - Second chapter MUST be `./report/part_2.md`, and so on.

                ### 🚨 MANDATORY FILE WRITE COMMAND (底层强制保存与 JSON 保命鞭子) 🚨
                You MUST append this EXACT text to the END of EVERY `user_query` you send to `section_writer`:
                "【最高物理执行指令】:你必须完成本章撰写,且绝对必须调用 `file_write` 将内容写入 target_file_path!🚨【致命警告】:调用 file_write 时,content 内容极长,你**绝对禁止**在 JSON 字符串中直接使用真实回车换行!必须全部写在同一行内,并用 `\\n` 替代换行符!所有双引号必须转义为 `\\"`!"

                ### 🚨 JSON SYNTAX SURVIVAL RULE 🚨
                1. **NO REAL LINE BREAKS**: All string values MUST be on a single continuous line. Use `\\n` for newlines. DO NOT press Enter inside a string!
                2. **ESCAPE QUOTES**: Any double quotes inside a string MUST be escaped like this: `\\"`.

                ### 🚨 IMAGE PATH RULE (图片路径规范)
                When inserting images, use the EXACT relative path from the key_files list provided to you.
                - Each image in key_files already has its correct path (e.g. ../user_uploads/xxx.png or ../experiment_results/xxx.png)
                - Do NOT guess or default to experiment_results/ — use the path as provided in key_files
                - All paths must be relative to the report/ directory (start with ../)
                - NEVER use ./ prefix from inside report/

                ### 🚨 RETRY RULE 🚨
                If `section_writer` returns an error for a chapter, you MUST retry that exact same chapter immediately (up to 3 times) before moving to the next chapter. NEVER skip a failed chapter.
                """
        system_prompt = system_prompt.replace("<tools>", anti_loop_rules + "\n<tools>")
        profile_prompt = render_profile_for_writer()
        if profile_prompt:
            system_prompt = system_prompt.replace("<tools>", profile_prompt + "\n<tools>")

        # ==============================================================================

        return get_skill_loader().inject_agent_skills(
            system_prompt,
            self.config.agent_name,
            compact=True
        )
    def _build_initial_message_from_task_input(self, task_input: WriterAgentTaskInput) -> str:
        """Build the initial user message from TaskInput"""
        message = ""

        # 🚀 致命修复:接收 Planner 传过来的学术排版规范和要求!
        task_content = getattr(task_input, 'task_content', '') or ''
        if task_content:
            message += f"【系统核心任务指令与规范 (MANDATORY INSTRUCTIONS & GROUND TRUTH)】:\n{task_content}\n\n"

        # 获取用户原始查询/题目
        user_query = getattr(task_input, 'user_query', '') or ''
        if user_query:
            message += f"【用户要求的论文题目 - 必须严格使用此题目】:\n{user_query}\n\n"

        # Add key files information with reliability dimensions
        def load_json_from_server(file_path):
            """Load JSONL file from MCP server using unlimited internal tool"""
            res = []
            try:
                # Use json read tool directly through raw MCP client
                raw_result = self.mcp_tools.client.call_tool("load_json", {"file_path": file_path})
                
                if not raw_result.success:
                    self.logger.error(f"Failed to read file from server: {raw_result.error}")
                    return res
                
                res = json.loads(raw_result.data["content"][0]["text"])["data"]
                                            
            except Exception as e:
                self.logger.error(f"Error loading file {file_path} from MCP server: {e}")
                import traceback
                self.logger.debug(f"Full traceback: {traceback.format_exc()}")
                
            return res

        key_files_dict = {}

        server_analysis_path = f"doc_analysis/file_analysis.jsonl"
        self.logger.debug(f"Loading analysis from MCP server: {server_analysis_path}")
        file_analysis_list = load_json_from_server(server_analysis_path)
        if not file_analysis_list:
            file_analysis_list = []
        for file_info in file_analysis_list:
            if file_info.get('file_path'):
                key_files_dict[file_info.get('file_path')] = file_info

        file_core_content = ""
        # 🚀 修复 1:增加防御性检查,使用 getattr 确保 key_files 即使为 None 也能安全跳过,防止 NoneType 迭代报错
        key_files = getattr(task_input, 'key_files', []) or []

        if key_files:
            message += "Key Files (Available Materials):\n"
            for i, file_ in enumerate(key_files, 1):
                file_path = file_.get('file_path')
                if not file_path:
                    continue

                message += f"{i}. File: {file_path}\n"

                if file_path in key_files_dict:
                    file_info = key_files_dict[file_path]
                    doc_time = file_info.get('doc_time', 'Not specified')
                    source_authority = file_info.get('source_authority', 'Not assessed')
                    task_relevance = file_info.get('task_relevance', 'Not assessed')
                    information_richness = file_info.get('information_richness', 'Not assessed')
                    summary = file_info.get('core_content', '')

                    # 🚀 修复 2:显式识别图片文件并生成“引导式”内容摘要
                    # 检查后缀名判断是否为实验图表
                    is_image = any(
                        file_path.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.webp', '.bmp'])
                    ##
                    if is_image:
                        img_name = os.path.basename(file_path)

                        # 优先从 key_files_dict 取已分析的内容
                        if not summary:
                            try:
                                analyze_result = self.execute_tool_call({
                                    "name": "analyze_image",
                                    "arguments": {
                                        "file_path": file_path,
                                        "prompt": "请详细描述这张实验结果图的内容,提取所有可见的数值、指标、坐标轴标签和关键结论."
                                    }
                                })
                                if analyze_result.get("success"):
                                    summary = analyze_result.get("data", "")
                                    self.logger.info(f"✅ 自动分析图片成功: {file_path}")
                            except Exception as e:
                                self.logger.warning(f"图片自动分析失败: {e}")

                        if not summary:
                            summary = f"[实验结果图表,路径:{file_path}]"

                        # 根据文件路径类型生成正确的相对路径(相对于report/目录)
                        if 'experiment_results' in file_path:
                            img_relative_path = f"../experiment_results/{img_name}"
                        elif 'user_uploads' in file_path:
                            img_relative_path = f"../user_uploads/{img_name}"
                        elif 'research' in file_path:
                            img_relative_path = f"../research/{img_name}"
                        else:
                            img_relative_path = file_path

                        source_type_label = "IMAGE (User Upload)" if "user_uploads" in file_path else "IMAGE (Experimental Results)"
                        file_core_content += (
                            f"[{i}] SOURCE TYPE: {source_type_label}\n"
                            f"    - Path: {file_path}\n"
                            f"    - Data Found in Image: {summary}\n"
                            f"    - MANDATORY: When discussing this data, you MUST embed the image using: ![{img_name}]({img_relative_path})\n"
                        )
                    else:
                        # 针对文档,保持清晰的分层结构
                        file_core_content += (
                            f"[{i}] SOURCE TYPE: DOCUMENT\n"
                            f"    - Path: {file_path}\n"
                            f"    - Time: {doc_time} | Authority: {source_authority} | Relevance: {task_relevance}\n"
                            f"    - Summary: {summary}\n"
                        )
                    file_core_content += "\n"

            # 循环结束后统一拼接所有文件的结构化摘要(避免逐文件重复拼接,且确保所有 key_files 都被处理)
            if file_core_content:
                message += f"\nfile_core_content (Structured Summaries):\n{file_core_content}\n"

        return message

    def execute_task(self, task_input: WriterAgentTaskInput) -> AgentResponse:
        """
        Execute a writing task using ReAct pattern

        Args:
            task_input: TaskInput object with standardized task information

        Returns:
            AgentResponse with writing results and process trace
        """
        start_time = time.time()

        try:
            self.logger.info(f"Starting writing task: {task_input.task_content}")

            # Reset trace for new task
            self.reset_trace()

            # Initialize conversation history
            conversation_history = []

            # Build system prompt for writing
            system_prompt = self._build_system_prompt()

            # Build initial user message from TaskInput
            user_message = self._build_initial_message_from_task_input(task_input)

            # Add to conversation
            conversation_history.append({"role": "system", "content": system_prompt})
            conversation_history.append({"role": "user", "content": user_message + " /no_think"})

            iteration = 0
            task_completed = False
            completion_tool = None  # 记录实际触发完成的 done 工具名,用于后续精确回溯结果

            self.logger.debug("Checking conversation history before model call")
            self.logger.debug(f"Conversation history: {conversation_history}")
            # ReAct Loop for Writing: Research → Plan → Write → Refine → Complete
            # Get model configuration from config
            from config.config import get_config
            config = get_config()
            model_config = config.get_custom_llm_config()


            while iteration < self.config.max_iterations and not task_completed:
                interventions = self._writer_pause_checkpoint(iteration + 1)
                if interventions:
                    conversation_history.append({
                        "role": "user",
                        "content": "用户在写作检查点补充了以下指导，请从当前章节开始执行：\n- "
                                   + "\n- ".join(interventions) + " /no_think",
                    })
                # Check for cancellation at the start of each iteration
                if self._check_cancellation():
                    self.logger.info(f"WriterAgent task cancelled at iteration {iteration}")
                    execution_time = time.time() - start_time
                    return self.create_response(
                        success=False,
                        result="Task was cancelled by user",
                        iterations=iteration,
                        execution_time=execution_time
                    )

                iteration += 1
                self.logger.info(f"Writing iteration {iteration}")

                try:
                    # Get LLM response (reasoning + potential tool calls) with retry

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

                    # 2. 如果没有图片,才使用文本专属的 chat_template
                    if not has_vision:
                        payload[
                            "chat_template"] = "{% for message in messages %}{% if loop.first and messages[0]['role'] != 'system' %}{{ '<s>[unused9]系统:[unused10]' }}{% endif %}{% if message['role'] == 'system' %}{{'<s>[unused9]系统:' + message['content'] + '[unused10]'}}{% endif %}{% if message['role'] == 'assistant' %}{{'[unused9]助手:' + message['content'] + '[unused10]'}}{% endif %}{% if message['role'] == 'tool' %}{{'[unused9]工具:' + message['content'] + '[unused10]'}}{% endif %}{% if message['role'] == 'function' %}{{'[unused9]方法:' + message['content'] + '[unused10]'}}{% endif %}{% if message['role'] == 'user' %}{{'[unused9]用户:' + message['content'] + '[unused10]'}}{% endif %}{% endfor %}{% if add_generation_prompt %}{{ '[unused9]助手:' }}{% endif %}"
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

                            # 3. 精准捕获限流 (429 / Quota)
                            if status_code in [429, 500] or (isinstance(response_json, dict) and "error" in response_json):
                                err = response_json.get("error", {})
                                err_code = str(err.get("code", status_code))
                                err_msg = str(err.get("message", "")).lower()

                                if err_code in [429, 500] or "rate limit" in err_msg or "429" in err_msg or "quota" in err_msg or "throttling" in err_msg:
                                    self.logger.warning(
                                        f"⚠️ 触发 API 限流或额度超限 (429).Agent 将沉睡 30 秒后继续... (第 {retry_num}/{max_retry_num} 次尝试)")

                                    time.sleep(30)
                                    retry_num += 1
                                    continue

                            # 正常错误检查
                            if "choices" not in response_json or not response_json["choices"]:
                                error_info = response_json.get("error", "未知 API 错误")
                                self.logger.error(f"API 响应异常: {response_json}")
                                assistant_message = {
                                    "role": "assistant",
                                    "content": f"[unused16][unused17] 错误:API 请求失败({error_info}).请尝试调整策略."
                                }
                            else:
                                assistant_message = response_json["choices"][0]["message"]

                            self.logger.debug("API response received successfully")
                            break  # 成功,跳出重试循环

                        except Exception as e:
                            err_msg = str(e).lower()
                            if "429" in err_msg or "rate limit" in err_msg or "quota" in err_msg or "throttling" in err_msg:
                                self.logger.warning(
                                    f"⚠️ 捕获到网络层限流异常.Agent 将沉睡 60 秒后继续... (第 {retry_num}/{max_retry_num} 次尝试)")

                                time.sleep(60)
                            else:
                                self.logger.warning(f"API 请求失败: {e},3秒后重试...")

                                time.sleep(3)

                            retry_num += 1
                            if retry_num == max_retry_num:
                                raise ValueError(str(e))
                            continue


                    try:
                        reasoning_content = self._safe_extract_reasoning(assistant_message.get("content", ""))
                        if reasoning_content:
                            self.log_reasoning(iteration, reasoning_content)
                            self._publish_writer_progress(
                                "writer_decision", f"Writer 第 {iteration} 轮写作摘要",
                                iteration=iteration,
                                summary=self._humanize_progress_text(
                                    reasoning_content, "我正在检查论文进度并确定下一段或下一章的写作内容。"
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
                    #     tool_call_str = re.findall(r"\[unused11\]([\s\S]*?)\[unused12\]", content)
                    #     if len(tool_call_str) > 0:
                    #         try:
                    #             tool_calls = json.loads(tool_call_str[0])
                    #         except:
                    #             return []
                    #     else:
                    #         return []
                    #     return tool_calls
                    def extract_tool_calls(content):
                        import re
                        if not content:
                            return []

                        base_tool_calls = self.extract_tool_calls(content)
                        if base_tool_calls:
                            return base_tool_calls

                        # 查找所有匹配的内容
                        tool_call_matches = re.findall(r"\[unused11\]([\s\S]*?)\[unused12\]", content)

                        if not tool_call_matches:
                            if "writer_subjective_task_done" in content:
                                return [{
                                    "name": "writer_subjective_task_done",
                                    "arguments": {
                                        "final_article_path": "./report/final_report.md",
                                        "article_summary": "Writer reported that the academic paper was completed, but the completion call was not emitted in parseable tool-call tags.",
                                        "completion_status": "completed",
                                        "completion_analysis": "Recovered completion signal from plain assistant text."
                                    }
                                }]
                            return []

                        first_match = tool_call_matches[0].strip()
                        try:
                            # 正常解析 JSON
                            tool_calls = json.loads(first_match)
                            return tool_calls if isinstance(tool_calls, list) else [tool_calls]
                        except Exception as e:
                            # 🚨 暴力修复 JSON 换行导致的解析崩溃!
                            try:
                                # 大模型最爱犯的错就是字符串里直接真实换行,强行把真实换行符替换为合法的 \n
                                fixed_match = first_match.replace('\n', '\\n').replace('\r', '')
                                tool_calls = json.loads(fixed_match)
                                return tool_calls if isinstance(tool_calls, list) else [tool_calls]
                            except Exception as e2:
                                logger.error(
                                    f"解析工具调用 JSON 失败 (已尝试修复依然失败): {e2} \n原始内容片段: {first_match[:100]}")
                                # 🚨 致命修改:绝不能 return [],必须抛出异常,让外层捕获并直接骂醒大模型!
                                raise ValueError(
                                    f"【致命语法错误】你输出的 JSON 格式崩溃了!请严格检查字符串内部是否包含了未转义的双引号(\")!如果有,必须使用反斜杠转义(\\\")!不要输出无效的控制字符!详细报错: {e2}")

                    # Add assistant message to conversation
                    conversation_history.append({
                        "role": "assistant",
                        "content": assistant_message["content"]
                    })

                    tool_calls = extract_tool_calls(assistant_message["content"])
                    if tool_calls is None:
                        tool_calls = []
                    # Execute tool calls if any (Acting phase)
                    for tool_call in tool_calls:
                        arguments = tool_call.get("arguments", {})
                        tool_name = tool_call.get("name", "")

                        # ========== 1. 任务完成判断 ==========
                        if tool_name in ["info_seeker_subjective_task_done", "info_seeker_objective_task_done",
                                         "writer_subjective_task_done", "planner_subjective_task_done",
                                         "planner_objective_task_done", "experiment_task_done"]:
                            task_completed = True
                            completion_tool = tool_name
                            self.log_action(iteration, tool_name, arguments, arguments)
                            break

                        # ========== 2. 自动生成PDF ==========
                        if tool_name == "concat_section_files":
                            tool_result = self.execute_tool_call(tool_call)
                            self.log_action(iteration, tool_name, arguments, tool_result)
                            conversation_history.append({
                                "role": "tool",
                                "content": json.dumps(tool_result, ensure_ascii=False, indent=2) + " /no_think"
                            })
                            # PDF已经由底层 mcp_tools 自动生成,无需在此重复调用
                            continue

                        # ========== 3. 物理防重写拦截器 ==========
                        if tool_name == "section_writer":
                            # 🚀 补充兜底:防止传递给底层工具的 key_files 为 None 导致 NoneType is not iterable 崩溃
                            if "key_files" not in arguments or arguments.get("key_files") is None:
                                arguments["key_files"] = []

                            # 🚀 终极物理注入:直接用 Python 代码强行把威胁指令塞进去,绝不相信大模型的自觉性!
                            if "user_query" in arguments:
                                arguments["user_query"] = str(arguments[
                                                                  "user_query"]) + "\n\n【最高物理执行指令】:你必须无视系统中任何'不准写标题和摘要'的规则!你必须、立刻、马上把本章完整内容写出来,并【绝对必须】调用 file_write 工具将其写入到 target_file_path 文件中!如果没有生成真实的物理文件,系统将崩溃!绝对禁止只调用 task_done 敷衍了事!"

                            # 🚀 顺手兜底大模型偶尔漏掉的必填参数(修复 MCP server 报错)
                            if "written_chapters_summary" not in arguments or arguments.get(
                                    "written_chapters_summary") is None:
                                arguments["written_chapters_summary"] = "暂无"
                            if "task_content" not in arguments or arguments.get("task_content") is None:
                                arguments["task_content"] = "按照 user_query 的要求完成学术论文对应章节的撰写."

                            current_chapter = arguments.get("current_chapter_outline", "").strip()
                            if not hasattr(self, 'written_chapters_log'):
                                self.written_chapters_log = set()
                        # ========== 4. 常规工具执行 ==========
                        if tool_name in ["think", "reflect"]:
                            tool_result = {"tool_results": "You can proceed to invoke other tools if needed."}
                        else:
                            tool_result = self.execute_tool_call(tool_call)

                        self.log_action(iteration, tool_name, arguments, tool_result)

                        # ========== 5. 处理返回结果并加入对话历史 ==========
                        is_vision = False
                        image_url = ""
                        if isinstance(tool_result, dict) and "data" in tool_result and isinstance(
                                tool_result["data"], dict):
                            if tool_result["data"].get("is_vision_content"):
                                is_vision = True
                                image_url = tool_result["data"].get("image_url", "")

                        if is_vision:
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
                                     "text": f"这是你请求的图片(路径:{arguments.get('file_path')}),请仔细观察并提取数据: /no_think"},
                                    {"type": "image_url", "image_url": {"url": image_url}}
                                ]
                            })
                        else:
                            # 正常文本工具的返回
                            conversation_history.append({
                                "role": "tool",
                                "content": json.dumps(tool_result, ensure_ascii=False, indent=2) + " /no_think"
                            })

                        # ========== 6. 登记完成状态 & 催促推进下一章 ==========
                        if tool_name == "section_writer" and isinstance(tool_result, dict) and tool_result.get(
                                "success"):
                            completed_chapter = arguments.get("current_chapter_outline", "").strip()
                            target_path = str(arguments.get("target_file_path", ""))
                            full_chapter_text = ""
                            try:
                                workspace = getattr(self.mcp_tools, "workspace_path", None)
                                relative_target = target_path.replace("./", "", 1)
                                physical = (Path(workspace) / relative_target) if workspace else None
                                if physical and physical.is_file():
                                    full_chapter_text = physical.read_text(encoding="utf-8", errors="ignore")
                            except Exception as preview_error:
                                self.logger.debug("Could not read completed chapter preview: %s", preview_error)
                            # SectionWriter returns a completed chapter in one tool call.
                            # Publish it paragraph-by-paragraph so the central chat can
                            # progressively assemble a readable manuscript card.
                            paragraphs = [part.strip() for part in re.split(r"\n\s*\n", full_chapter_text) if part.strip()]
                            for paragraph_index, paragraph in enumerate(paragraphs, 1):
                                self._publish_writer_progress(
                                    "writer_paragraph_ready",
                                    f"WriterAgent 完成段落 {paragraph_index}/{len(paragraphs)}",
                                    section=target_path or completed_chapter or "section",
                                    section_title=completed_chapter or target_path or "正在撰写",
                                    paragraph_index=paragraph_index,
                                    paragraph_count=len(paragraphs),
                                    paragraph=paragraph[:5000],
                                )
                            self._publish_writer_progress(
                                "writer_section_ready",
                                f"WriterAgent 完成章节：{completed_chapter or target_path}",
                                section=target_path or completed_chapter or "section",
                                section_title=completed_chapter or target_path or "已完成章节",
                                content_preview=full_chapter_text[:12000],
                                content_truncated=len(full_chapter_text) > 12000,
                            )

                            if completed_chapter:
                                self.written_chapters_log.add(completed_chapter)

                            self.logger.info(
                                f"📢 动态注入推进指令: 催促写下一章 (刚完成: {completed_chapter[:20]}...)")
                            conversation_history.append({
                                "role": "user",
                                "content": (
                                    f"✅ 章节 '{completed_chapter[:30]}...' 已成功写完并保存.\n"
                                    f"【最高优先级指令】:请仔细比对 overall_outline,找到紧接着的**下一个未写章节**,"
                                    f"更新 current_chapter_outline 为新章节的完整标题,继续调用 `section_writer`!\n"
                                    f"如果所有章节都已写完,请调用 `concat_section_files` 合并报告."
                                )
                            })

                    if task_completed:
                        break

                    # If no tool calls, encourage continued writing
                    if len(tool_calls) == 0:
                        followup_prompt = (
                            "Continue your writing process. If you need to research more, use available tools. "
                            "If you need to write or edit content, use file operations. "
                            "If your writing is complete and meets requirements, call writer_subjective_task_done. /no_think"
                        )
                        conversation_history.append({"role": "user", "content": followup_prompt})


                except Exception as e:
                    error_msg = f"Error in writing iteration {iteration}: {e}"
                    self.log_error(iteration, error_msg)
                    conversation_history.append({
                        "role": "user",
                        "content": f"{error_msg}\n请你立刻检查刚才输出的 JSON 格式!绝不能包含未转义的双引号或真实换行符!请修正后重试. /no_think"
                    })
                    continue  # <--- 注意这里必须是 continue

            execution_time = time.time() - start_time
            # Extract final result
            if task_completed:
                # Find the completion result in the trace
                # 按实际触发完成的工具名回溯,避免只认 writer_subjective_task_done 而丢失结果
                done_tools = [completion_tool] if completion_tool else [
                    "writer_subjective_task_done", "info_seeker_subjective_task_done",
                    "info_seeker_objective_task_done", "planner_subjective_task_done",
                    "planner_objective_task_done", "experiment_task_done"
                ]
                completion_result = None
                for step in reversed(self.reasoning_trace):
                    if step.get("type") == "action" and step.get("tool") in done_tools:
                        completion_result = step.get("result")
                        break
                if completion_result is None:
                    self.logger.warning(
                        f"任务标记完成(via {completion_tool}),但未能从 trace 中回溯到完成结果,result 为空"
                    )
                return self.create_response(
                    success=True,
                    result=completion_result,
                    iterations=iteration,
                    execution_time=execution_time
                )
            else:

                return self.create_response(
                    success=False,
                    error=f"Writing task not completed within {self.config.max_iterations} iterations",
                    iterations=iteration,
                    execution_time=execution_time
                )

        except Exception as e:
            execution_time = time.time() - start_time if 'start_time' in locals() else 0
            self.logger.error(f"Error in execute_react_loop: {e}")

            return self.create_response(
                success=False,
                error=str(e),
                iterations=iteration if 'iteration' in locals() else 0,
                execution_time=execution_time
            )


# Factory function for creating the writer agent
def create_writer_agent(
        model: Any = None,
        max_iterations: int = 50,  # More iterations for writing tasks
        temperature: Any = None,  # Resolved from env if not provided
        max_tokens: Any = None,
        shared_mcp_client=None,
        task_id: str = None,
) -> WriterAgent:
    """
    Create a WriterAgent instance with server-managed sessions.
    
    Args:
        model: The LLM model to use
        max_iterations: Maximum number of iterations for writing tasks
        temperature: Temperature setting for creativity
        max_tokens: Maximum tokens for the AI response
        shared_mcp_client: Optional shared MCP client from parent agent (prevents extra sessions)

    Returns:
        Configured WriterAgent instance with writing-focused tools
    """
    # Import the enhanced config function
    from .base_agent import create_agent_config

    # Create agent configuration (session managed by MCP server)
    config = create_agent_config(
        agent_name="WriterAgent",
        model=model,
        max_iterations=max_iterations,
        temperature=temperature,
        max_tokens=max_tokens,
    )

    # Create agent instance with shared MCP client (filtered tools for writing)
    agent = WriterAgent(config=config, shared_mcp_client=shared_mcp_client, task_id=task_id)

    return agent
