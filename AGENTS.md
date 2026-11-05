# 美股生物医药 24×7 云端情报与投研库自动翻译系统 (US Stock Catalyst Monitor)

本项目是全天候运行在 **GitHub Actions 云端服务器** 与 **Google Sheets / Google Drive** 上的生物医药个股催化剂监控、行业全量速览及 PDF 原位排版中英对照翻译系统。

---

## 1. 核心云端资源与固定 ID
- **本地项目工作区路径**：`C:\Users\gensci\.gemini\antigravity\scratch\us-stock-catalyst-monitor`
- **GitHub 远端仓库**：`https://github.com/liwenzhong3312-a11y/us-stock-catalyst-monitor.git` (`main` 分支)
- **Google Sheet 主控表 ID (`SPREADSHEET_ID`)**：`1pIKfEy9UNPs32W5gBFJu3N5doqU-SKCvIBDjX-TMwGQ`
  - `Portfolio` 标签页：管理持仓标的（A列股票代码、B列公司全称、C列交易所、D列专属匹配词[代码+公司名+核心管线药名]、E列持仓均价、F列仓位备注、G列启用状态、H列公司官网 IR 邮件订阅链接）。
  - `Config` 标签页：保存 `PUSHPLUS_TOKEN`、`GEMINI_API_KEY`（当前已配置 5 个独立密钥，以英文逗号分隔）、`IMPACT_THRESHOLD`、`GMAIL_APP_PASSWORD`、`GITHUB_PAT`。
  - `Logs` 标签页：永久归档所有已推送的个股催化剂与行业速览记录（用于防重复推送去重）。
  - `Vault` 标签页：永久归档所有已翻译的 Google Drive 投研文献（记录源文件 File ID、双语对照 PDF 链接、以及提取出的内部官方口径/外部投行预期共识要点）。
- **Google Drive 投研资料库根目录 ID (`VAULT_ROOT_FOLDER_ID`)**：`182XirR1PicM8j0m1cvVAAWDNl0U_2HAg`（文件夹名称：`生物医药资料库 (Biotech Research Vault)`）

---

## 2. 核心架构与最新运行准则（严禁使用过时说法）

### (1) 谷歌云盘 (Google Drive) 投研库双向同步与 2 子目录结构 (`vault_sync.py`)
- **云盘新建标的自动同步至 Sheet**：当用户在云盘根目录下新建任何股票代码文件夹（如 `PBLS`、`MSLE`）时，云端工作流会自动：
  1. 在该股票文件夹下仅创建/识别 **2 个子文件夹**（**绝不创建第 3 个文件夹**）：
     - `01_公司内部官方口径 (Deck_Transcript_SEC)`（兼容自建的 `公司内部官方口径`）
     - `02_外部投行与独立研报 (SellSide_Consensus)`（兼容自建的 `外部投行与独立研报`）
  2. 调用 `_auto_enrich_new_stock_metadata`（结合 SEC 官方代码库、Yahoo 实时新闻流、云盘内文件名及 Gemini），自动补齐该股票的**公司英文全称、交易所、核心管线药物代号及官网 IR 链接**，并自动追加写入 Google Sheet 的 `Portfolio` 表中。
- **PDF / Word / PPT 原位双语对照翻译**：
  - 支持直接放在股票主文件夹下或上述 2 个子文件夹下的 `.pdf`、`.docx/.doc`、`.pptx/.ppt` 文件。
  - 利用 PyMuPDF (`fitz`) 生成 **2× 宽度左英右中同屏对照 PDF**（完整保留原版矢量图表、临床曲线、彩色表格与配色），生成的 **`原文件名 [译].pdf` 直接原位保存在源文件所在的同一个文件夹内**。
  - 待处理文件按文件体积从小到大排序优先处理（小文件优先秒翻，不被 70 页长篇行业大书阻塞），动态字符批处理（每批 `<= 110` 文本块或 `10,000` 字符）。

### (2) 个股催化剂与生科行业媒体双通道监控 (`monitor.py`)
- **个股催化剂主动检索 + 邮箱联动**：
  1. **SEC EDGAR 官方监管库 (`fetch_sec_edgar_events`)**：每 60 秒按 `Ticker -> CIK` 直连美国证监会抓 `8-K`、`10-K`、`10-Q`、`Form 4` 等申报文件（仅靠 `Ticker` 即可 100% 精准锁定）。
  2. **Yahoo Finance 个股聚合新闻流 (`fetch_yahoo_direct_news`)**：每 60 秒抓取各大公关通讯社（GlobeNewswire / BusinessWire / PR Newswire）一手官方新闻稿（微信标题显示为 `【官方】`）及外媒个股分析（微信标题显示为 `【第三方】`），并通过 `is_strict_match_for_stock`（匹配代码、公司名、专属管线药名）做防噪二次校验。
  3. **深度解读联动 (`score >= 7`)**：自动拉取 `ClinicalTrials.gov` 临床登记底稿 + Google Drive `Vault` 投研库中该股的历史内部承诺与投行预期共识，进行预期差比对。
- **生科行业媒体监控（云端直连官方 RSS + Gmail 邮箱订阅双轨制）**：
  - **云端代码已直接接管（免邮箱订阅）的 5 大官方源 (`fetch_biocentury_rss_events`)**：
    1. `BioCentury` (`biocentury.com/rss/BioCentury.xml`)
    2. `Clinical Trials Arena` (`clinicaltrialsarena.com/feed/`)
    3. `FDA Newsroom` (`fda.gov/.../press-releases/rss.xml`)
    4. `BioPharma Dive` (`biopharmadive.com/feeds/news/`)
    5. `MedCity News` (`medcitynews.com/feed/`)
  - **通过 Gmail (`neo664939@gmail.com`) 订阅接收的防爬/付费墙核心媒体**：
    - `STAT News` (`STAT+ Today` / `Pharmalittle`)、`Endpoints News`、`Fierce Biotech`、`Fierce Pharma`、`GEN`、`BioSpace`、`Nature Briefing`、`BioWorld` 等。
  - **行业媒体提炼铁律（`gemini_industry_digest`，严禁限制为 3 条）**：
    - 邮件正文读取上限为 `28,000` 字符（超链接提取 35 条），**取消“只挑 3 条或 2~5 条”的人为数量限制，实行【应留尽留、全量提取】**！
    - 凡涉及 **① 个股与药企临床/商业化动态、② M&A 并购潜力/BD交易/融资、③ 宏观医药政策/FDA监管/医保药价博弈、④ 前沿靶点与顶刊转化突破** 四大维度的内容，**哪怕一封邮件有 8～16 条也必须 100% 全部保留并逐条翻译总结推送**，仅剔除纯求职招聘广告和纯会议买票促销。

### (3) Gemini API 密钥轮询池与 24×7 自接力保活
- **35 个独立配额桶**：`Config!B3` 中的 5 个 API 密钥 × 7 个 Gemini 模型（`gemini-3.5-flash`、`gemini-3.6-flash`、`gemini-3.5-flash-lite`、`gemini-flash-lite-latest`、`gemini-flash-latest`、`gemini-3-flash-preview`、`gemini-pro-latest`）自动轮询与 `HTTP 429` 无缝降级切换。
- **GitHub Actions 24×7 保活 (`.github/workflows/monitor.yml`)**：
  - 每轮常驻运行 14 分钟（每 60 秒优先执行 SEC + Yahoo + 直连 RSS + Gmail 巡检，随后在剩余时间预算内推进云盘 PDF 翻译），结束时通过 `if: always()` 兜底步驟自动触发下一棒 `workflow_dispatch`，配合 `*/15 * * * *` Cron 构成永不间断的云端自动化工作流。
- **代码修改同步规范**：每次修改 `monitor.py`、`vault_sync.py` 或 `.github/workflows/monitor.yml` 后，必须执行 `git commit` 与 `git push origin main`，确保 GitHub 云端服务器立即运行最新代码。
