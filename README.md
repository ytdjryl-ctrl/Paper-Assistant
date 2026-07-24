# SciAssistant

面向科研写作的多智能体协作系统。它在保留自主 Agent 循环的基础上，将任务规划、文献检索、实验数据分析、论文写作、可视化规划、四角色审稿、综合修订和成果交付连接为一个可暂停、可干预、可追踪的工作流。

> 本项目是科研辅助工具，不代替研究者完成学术判断。生成的事实、实验结论、引用和参考文献必须由作者最终核验。

## 主要功能

- **Planner Agent**：理解研究目标、拆分子任务，并根据中间结果持续重新规划，而不是只执行固定流水线。
- **InformationSeeker Agent**：通过 MCP 工具和学术接口检索、读取、去重并整理文献；支持 Crossref、OpenAlex、arXiv、PubMed，以及可选的 ScienceDirect。
- **Experiment Agent**：读取用户上传的实验压缩包和说明文件，逐层建立实验记录，分析 CSV、Excel、图片和文档等材料。
- **Figure Agent**：根据数据字段、实验目的和文献表达习惯，选择趋势图、柱状图、箱线图、散点图、热力图或消融对比图。
- **Writer Agent**：先建立 ResearchContract 和 Claims–Evidence Matrix，再按证据撰写各章节，降低“只像综述”或结论脱离实验数据的问题。
- **Visual Communication Planner**：从整篇论文角度判断引言、相关工作、方法和实验部分需要哪些结构图、架构图、分类表和结果表。
- **四角色审稿**：分别检查方法、实验证据、引用和反方问题；未配置独立审稿模型时会自动复用主模型。
- **综合修订**：汇总审稿意见，围绕证据不足、引用错配、方法不清和过度结论进行定向修改。
- **实时 Web 工作区**：通过 SSE 显示可读的工作进度、章节增量预览和审稿意见，并支持在安全检查点暂停、提交指导和继续执行。
- **成果下载**：任务结束后可下载 Markdown、PDF 和参考文献清单。

## 工作流程

```text
研究问题与上传材料
        ↓
预处理与实验档案导入
        ↓
Planner 自主规划与分解
        ↓
InformationSeeker ↔ Experiment ↔ Planner 多轮协作
        ↓
ResearchContract + Claims–Evidence Matrix
        ↓
Writer 分章节写作 + Visual Communication Planner
        ↓
方法 / 实验证据 / 引用 / 反方 四角色审稿
        ↓
综合修订与复审
        ↓
Markdown + PDF + 参考文献清单
```

预处理阶段只整理问题、文件和已有资料，不会用不完整关键词提前进行网络检索。正式文献搜索由 Planner 分配给 InformationSeeker Agent。

## 项目结构

```text
SciAssistant/
├─ app.py                         # Web 页面、用户与文件相关 API
├─ chatAi/
│  ├─ research.html              # 科研工作区页面
│  └─ chatai.sql                 # MySQL 表结构（不含用户数据）
├─ deepdiver_v2/
│  ├─ cli/a.py                   # 异步科研任务服务，默认端口 8000
│  ├─ config/.env.example        # 无密钥配置模板
│  ├─ src/agents/                # Planner、检索、实验、写作等 Agent
│  ├─ src/pipeline_v2/           # 证据、审稿、修订和交付能力
│  ├─ src/tools/                 # MCP 服务与科研工具
│  ├─ tests/                     # 自动化测试
│  └─ requirements.txt
├─ scientific-agent-skills-main/ # 科研 Skills
├─ litellm_config.yaml           # 可选的 LiteLLM 配置模板
├─ SECURITY.md
└─ LICENSE
```

用户上传内容、任务工作区、日志、实验中间文件和生成论文均为本地运行数据，不包含在公开仓库中。

## 环境要求

- Windows、Linux 或 macOS
- Python 3.10 或更高版本
- MySQL 8.x
- 一个兼容 OpenAI Chat Completions 格式的大模型接口
- 可选：Elsevier API、搜索引擎 API、OCR 和 LaTeX/PDF 相关工具

## 快速开始

### 1. 获取项目并安装依赖

```bash
git clone https://github.com/your-name/SciAssistant.git
cd SciAssistant
python -m venv .venv
```

Windows PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r .\deepdiver_v2\requirements.txt
```

Linux/macOS：

```bash
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r deepdiver_v2/requirements.txt
```

### 2. 配置环境变量

Windows PowerShell：

```powershell
Copy-Item .\deepdiver_v2\config\.env.example .\deepdiver_v2\config\.env
```

Linux/macOS：

```bash
cp deepdiver_v2/config/.env.example deepdiver_v2/config/.env
```

编辑 `deepdiver_v2/config/.env`，至少填写：

```dotenv
MODEL_REQUEST_URL=https://your-provider.example/v1/chat/completions
MODEL_REQUEST_TOKEN=your-api-key
MODEL_NAME=your-model-name

MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=sciassistant
MYSQL_PASSWORD=your-database-password
MYSQL_DATABASE=sciassistant
SECRET_KEY=replace-with-a-long-random-string
```

四个审稿人的 URL、API Key 和模型名都是可选项：

- 全部留空：四个角色均复用主模型。
- 只配置部分角色：已配置的角色使用独立模型，其余角色复用主模型。
- `ELSEVIER_API_KEY` 留空：自动跳过 ScienceDirect，不影响其他来源。

不要把真实 `.env`、API Key、数据库密码或上传数据提交到 Git。

### 3. 初始化 MySQL

先创建数据库：

```sql
CREATE DATABASE sciassistant
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_general_ci;
```

然后将 `chatAi/chatai.sql` 导入该数据库。可以使用 MySQL Workbench、Navicat、phpMyAdmin 等图形工具，也可以在命令行执行：

```bash
mysql -u your_user -p sciassistant < chatAi/chatai.sql
```

生产环境建议为本项目创建独立数据库用户，不要直接使用 MySQL `root` 用户。

### 4. 启动三个服务

打开三个终端，并激活同一个 Python 虚拟环境。

终端 1：启动 MCP 工具服务（默认 `6274` 端口）

```powershell
cd .\deepdiver_v2
python .\src\tools\mcp_server_standard.py --config .\src\tools\server_config.yaml
```

终端 2：在项目根目录启动 Web 服务（默认 `5000` 端口）

```powershell
python .\app.py
```

终端 3：启动科研任务服务（默认 `8000` 端口）

```powershell
cd .\deepdiver_v2
python .\cli\a.py
```

浏览器访问：

```text
http://127.0.0.1:5000/research.html
```

## 上传实验资料

面向不熟悉计算机的用户，推荐直接上传一个 ZIP 压缩包。压缩包可以包含多个按实验名称命名的文件夹，也可以在每个文件夹中放置一个 `说明.txt`：

```text
实验资料.zip
├─ 基线模型/
│  ├─ 说明.txt
│  ├─ results.csv
│  └─ confusion_matrix.png
├─ 注意力模块消融/
│  ├─ 说明.txt
│  └─ results.csv
└─ 不同模型对比/
   ├─ 说明.txt
   └─ comparison.xlsx
```

系统会自动解压、逐层读取文件夹名称和说明文件，再按需处理数据，而不是一次将全部原始数据发送给大模型。

## 运行中的暂停与指导

Web 端的指导不是简单追加到对话末尾。系统会在可安全恢复的小节点检查新指导：

- Planner 开始或重新规划前；
- Writer 开始章节写作前；
- 综合修订开始前；
- InformationSeeker 完成当前文献或工具调用后。

例如，检索第三篇文献时提交新的关键词，当前处理完成后即可将指导应用到后续检索；如果要求切换到实验阶段，Planner 会在下一个检查点重新评估剩余任务。

## 输出内容

完成的任务通常会生成：

- 完整论文 Markdown；
- 渲染后的 PDF；
- 按编号逐条换行的参考文献 TXT；
- 图表与实验记录；
- 四位审稿人的意见及综合修订记录；
- 供断点恢复使用的结构化任务状态。

## 测试

先进入科研引擎目录再执行：

```powershell
cd .\deepdiver_v2
python -m compileall .\src .\cli
python -m pytest .\tests
```

部分测试或端到端任务需要已配置的模型、MCP 服务、数据库或外部学术接口。

## 安全与隐私

公开版本不包含任何真实 API Key、密码、用户上传文件、工作区、日志或生成论文。部署前请阅读 [SECURITY.md](SECURITY.md)，并特别注意：

- 将服务放在反向代理和访问控制之后；
- 对上传文件设置大小、类型、解压路径和生命周期限制；
- 定期清理任务工作区；
- 不在日志和前端输出模型请求头、工具原始参数或密钥；
- 一旦密钥进入 Git 历史，立即吊销并重新生成。

## 当前限制

- 学术网站的访问策略和接口配额可能导致部分全文无法自动获取。
- 文献数量阈值不能代替相关性与真实性审核。
- PDF 生成效果依赖本机字体和渲染环境。
- 大模型可能产生错误理解或不恰当表述，最终稿必须由研究者复核。
- 本项目不能保证生成内容满足任何具体期刊的录用要求。

## 参与贡献

欢迎提交 Issue 和 Pull Request。提交代码前请：

1. 确认没有上传真实密钥、私人论文、实验数据或日志；
2. 保持对现有三服务启动方式的兼容；
3. 为新增行为补充测试或提供可复现说明；
4. 明确标注依赖外部 API 的功能与降级策略。

## License

本项目按照 [LICENSE](LICENSE) 中的条款发布。第三方组件及科研 Skills 的版权与许可信息见 [NOTICE](NOTICE) 及其各自目录中的许可证文件。
