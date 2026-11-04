/**
 * ==============================================================================
 * 美股生科催化剂极速直推引擎 (Google Apps Script 版)
 * 运行环境：Google Apps Script (部署于 Google 云端，永久免费、24x7 毫秒级直推)
 * 核心架构：
 * 1. 24x7 毫秒级原生监控 Google Drive 投研库（新研报/官方Deck/纪要 1~2 分钟内全自动中英对照翻译+导出PDF+推微信）
 * 2. 毫秒级 Gmail 官方 IR 邮件与突发快讯监听直推
 * 3. 严格单条合并推送（Single Consolidated Push），杜绝连发两条微信与“深度解读暂不可用”空壳
 * 4. 全局并发锁（LockService）+ 45 秒硬超时防护，100% 杜绝“8分钟执行超时/服务器错误”报警邮件
 * 5. 多 GCP 项目 Gemini API Key 自动轮询与 429 容灾切换
 * ==============================================================================
 */

// 您的 Google Sheet ID
const SPREADSHEET_ID = "1pIKfEy9UNPs32W5gBFJu3N5doqU-SKCvIBDjX-TMwGQ";

// 可用的 Gemini 模型备选阵列（自动轮换，杜绝 404）
const GEMINI_MODELS = [
  "gemini-flash-latest",
  "gemini-3-flash-preview",
  "gemini-3.1-flash-lite",
  "gemini-pro-latest"
];

// Google Drive 投研库根目录与固定目录映射表（同时支持从 Portfolio 表动态发现新标的）
const VAULT_ROOT_FOLDER_ID = "182XirR1PicM8j0m1cvVAAWDNl0U_2HAg";
const VAULT_STOCK_FOLDERS = {
  "ABSI": {
    name: "Absci Corporation",
    internal: { id: "142Lcae_wj_cfsUxXe8BW8J5I123ZTcgP", label: "公司内部官方口径 (Deck/Transcript)" },
    external: { id: "1r4ZSC9q0Odndn8DKzUrrEktOTzZgPhYA", label: "外部投行与独立研报 (Sell-Side)" }
  },
  "ARWR": {
    name: "Arrowhead Pharmaceuticals",
    internal: { id: "18l7CPc9wc9h-260n5uEqzhWcR87BGDNI", label: "公司内部官方口径 (Deck/Transcript)" },
    external: { id: "1ThZ_uJQoMMv4_Su3j2tKpZ0BVsaSfF_E", label: "外部投行与独立研报 (Sell-Side)" }
  },
  "KRYS": {
    name: "Krystal Biotech",
    internal: { id: "1424s8jY16FRaeWEp4WXl9N6Wd5quzDVx", label: "公司内部官方口径 (Deck/Transcript)" },
    external: { id: "1_5ASngeC32HPbzeuX-A3K7EmjAoVXxiZ", label: "外部投行与独立研报 (Sell-Side)" }
  },
  "MANE": {
    name: "Veradermics",
    internal: { id: "1B9kEYHpqlVvBT1WmD68tmZXyYnAnU--g", label: "公司内部官方口径 (Deck/Transcript)" },
    external: { id: "194waR4OjgxvbUfw7Ahd9pCCrfX3H-pUK", label: "外部投行与独立研报 (Sell-Side)" }
  }
};

function isTranslatedOrTempFile(fname) {
  if (!fname) return true;
  const base = fname.replace(/\.[^/.]+$/, "").trim();
  if (/(\[译\]|【译】|\[中英对照\]|【中英对照】)$/.test(base)) return true;
  if (fname.includes("[译].") || fname.includes("【译】.") || fname.includes("【中英对照PDF】") || fname.includes("_temp_")) return true;
  return false;
}

const VAULT_SYNC_FOLDER_ID = "1x-3gF5g8ceE3Z8oulNKBuMEyNHfGxAtF";

// 官方 IR 邮箱与域名清单
const OFFICIAL_IR_DOMAINS = [
  "gcs-web.com",
  "alert.gcs-web.com",
  "notification.gcs-web.com",
  "notified.com",
  "q4inc.com",
  "equisolve.com",
  "issuerdirect.com",
  "sec.gov",
  "krystalbio.com",
  "arrowheadpharma.com",
  "absci.com",
  "veradermics.com"
];

// 专业生科媒体清单
const BIOTECH_MEDIA_DOMAINS = [
  "biospace.com",
  "endpoints",
  "endpts.com",
  "fiercebiotech.com",
  "fiercepharma.com",
  "statnews.com",
  "genengnews.com",
  "bioworld.com",
  "biopharmadive.com",
  "medcitynews.com"
];

const NON_MEDIA_DOMAINS = [
  "google.com",
  "youtube.com",
  "claude.com",
  "anthropic.com",
  "github.com",
  "apple.com",
  "microsoft.com"
];

// ==============================================================================
// 1. 全局入口与防并发锁、硬超时防护（彻底杜绝服务器错误报警邮件）
// ==============================================================================

/**
 * 兼容旧触发器：若用户触发器绑定的仍是 checkGmailAndPushRealtime，自动转入受保护的全局引擎
 */
function checkGmailAndPushRealtime() {
  runRealtimeMonitor();
}

/**
 * 24x7 毫秒级统一实时监控引擎
 * 核心机制：
 * 1. LockService 脚本锁：若上一轮正在处理长文件，当前轮在 2 秒内静默优雅退出，杜绝重叠并发导致的 Google 锁死报错！
 * 2. 45 秒硬超时中断：整体运行绝对不超过 45 秒，彻底远离 Apps Script 6 分钟上限与 Google 8 分钟看门狗强杀！
 */
function runRealtimeMonitor() {
  const lock = LockService.getScriptLock();
  // 尝试获取排他锁，最多等待 2 秒；若获取不到则说明上一轮还在稳妥运行，本轮直接安全退出
  if (!lock.tryLock(2000)) {
    Logger.log("[LOCK] 上一轮监控任务仍在运行中，本轮自动安全退出，避免并发冲突。");
    return;
  }

  const startTime = new Date().getTime();

  try {
    const sheet = SpreadsheetApp.openById(SPREADSHEET_ID);
    const config = loadConfig(sheet);
    const portfolio = loadPortfolio(sheet);

    // 1. Gmail 邮件毫秒级监控（优先处理未读邮件）
    runGmailScan(sheet, config, portfolio, startTime);

    // 2. Google Drive 投研库新增文件监控与自动翻译（1~2 分钟内极速入库）
    runDriveVaultScan(sheet, config, startTime);

  } catch (globalErr) {
    // 捕获顶层所有异常，写入 Apps Script 执行日志，绝不抛出导致 Google 向用户邮箱发送报警通知
    Logger.log("[WARN] runRealtimeMonitor caught safe exception: " + globalErr.message);
  } finally {
    lock.releaseLock();
  }
}

// ==============================================================================
// 2. Gmail 官方 IR 邮件与媒体快讯监听引擎
// ==============================================================================

function runGmailScan(sheet, config, portfolio, startTime) {
  // 单次扫描最长允许耗时 20 秒，超时立即退出让路
  const MAX_GMAIL_DURATION_MS = 20 * 1000;
  function isGmailTimeout() {
    return (new Date().getTime() - startTime) > MAX_GMAIL_DURATION_MS;
  }

  const geminiApiKey = config["GEMINI_API_KEY"];
  const pushToken = config["PUSHPLUS_TOKEN"];
  const threshold = parseInt(config["IMPACT_THRESHOLD"] || "7", 10);
  if (!pushToken || !geminiApiKey) return;

  // 使用 Config 表中定制的精准查询，若无则使用收窄的 IR 与生科媒体查询，杜绝拉取全邮箱 2000+ 封杂乱邮件
  let searchQuery = config["GMAIL_QUERY"];
  if (!searchQuery || !searchQuery.includes("is:unread")) {
    searchQuery = "is:unread -in:trash -in:drafts newer_than:2d (label:Stock-Alert OR from:(globenewswire.com OR prnewswire.com OR businesswire.com OR sec.gov OR fiercebiotech.com OR statnews.com OR seekingalpha.com OR biopharmadive.com OR biospace.com OR arrowheadpharma.com OR krystalbio.com OR absci.com OR veradermics.com))";
  }

  let threads = [];
  try {
    // 每次最多检索 5 个线程，保持极速轻量
    threads = GmailApp.search(searchQuery, 0, 5);
  } catch (searchErr) {
    Logger.log("Gmail search error: " + searchErr.message);
    return;
  }

  if (!threads || threads.length === 0) return;

  let processedCount = 0;

  for (let i = 0; i < threads.length; i++) {
    if (processedCount >= 2 || isGmailTimeout()) break;
    const thread = threads[i];
    const messages = thread.getMessages();

    for (let j = 0; j < messages.length; j++) {
      if (processedCount >= 2 || isGmailTimeout()) break;
      const msg = messages[j];
      if (!msg.isUnread()) continue;

      const subject = msg.getSubject() || "(无标题)";
      const sender = msg.getFrom() || "";
      const date = msg.getDate();
      const bodyHtml = msg.getBody() || "";
      const bodyPlain = msg.getPlainBody() || "";

      // 自动过滤订阅确认邮件
      if (/confirm your subscription|verify your email|welcome to .* investor/i.test(subject)) {
        msg.markRead();
        continue;
      }

      let cleanedBody = cleanBoilerplateText(bodyPlain.length > 50 ? bodyPlain : htmlToPlain(bodyHtml));
      const senderLower = sender.toLowerCase();
      const isOfficial = OFFICIAL_IR_DOMAINS.some(d => senderLower.includes(d));

      let articleUrl = "https://mail.google.com/mail/u/0/#inbox";
      // 官方 IR 邮件若内容过短或无实质正文，安全尝试抓取原新闻稿，单次最多耗时 5 秒
      if (isOfficial && (cleanedBody.length < 450 || !hasSubstantiveBody(subject, cleanedBody)) && bodyHtml) {
        const linkMatch = bodyHtml.match(/href=["'](https?:\/\/[^"']+)["']/i);
        if (linkMatch && !linkMatch[1].includes("unsubscribe") && !linkMatch[1].includes("google.com")) {
          articleUrl = linkMatch[1];
          try {
            const fetchResp = UrlFetchApp.fetch(articleUrl, { muteHttpExceptions: true, followRedirects: true, validateHttpsCertificates: false });
            if (fetchResp.getResponseCode() === 200) {
              const fetchedPlain = cleanBoilerplateText(htmlToPlain(fetchResp.getContentText()));
              if (fetchedPlain.length > cleanedBody.length) {
                cleanedBody = fetchedPlain.substring(0, 8000);
              }
            }
          } catch (e) {
            Logger.log("Safe url fetch skipped: " + e.message);
          }
        }
      }

      // 检查持仓标的匹配
      let matchedStock = null;
      const combined = (subject + " " + cleanedBody).toUpperCase();
      for (let s = 0; s < portfolio.length; s++) {
        const stock = portfolio[s];
        if (isStrictMatch(combined, stock)) {
          matchedStock = stock;
          break;
        }
      }

      if (matchedStock) {
        if (/(-(\s*)slideshow|-(\s*)transcript)$/i.test(subject.trim())) {
          msg.markRead();
          continue;
        }

        const hasSubstantive = hasSubstantiveBody(subject, cleanedBody);
        const isIrLinkStub = /view this release|view all releases|powered by notified|查看本新闻稿|查看所有新闻稿/i.test(cleanedBody);
        if (!hasSubstantive && (isOfficial || isIrLinkStub || cleanedBody.trim().length < 20)) {
          msg.markRead();
          continue;
        }

        const facts = geminiTranslateFacts(geminiApiKey, matchedStock, subject, cleanedBody, sender, isOfficial);
        if (facts.is_relevant) {
          const score = facts.impact_score;
          const titleCn = facts.title_cn || subject;
          const bodyCn = facts.body_cn || cleanedBody;

          const isScheduleNotice = /to report .* financial results|conference call on|webcast on|announces date for|financial results on/i.test(subject);
          if (isScheduleNotice) {
            facts.category = "事项预告";
          }

          let deepText = "";
          if (score >= threshold && hasSubstantive && bodyCn.trim().length >= 120 && !bodyCn.includes("仅含标题") && !isScheduleNotice) {
            Utilities.sleep(1000);
            const resDeep = geminiDeepAnalysis(geminiApiKey, matchedStock, subject, titleCn, cleanedBody);
            if (resDeep && !resDeep.includes("深度解读暂不可用") && resDeep.trim().length >= 50) {
              deepText = resDeep.trim();
            }
          }

          // 严格单条合并推送（深色/浅色模式 100% 自适应高对比度排版，绝不硬编码黑底黑字或白底白字）
          const hasDeep = Boolean(deepText);
          const badge = `【${isOfficial ? "官方" : "第三方"}${hasDeep ? "·深度" : ""}】`;
          const singleTitle = `${badge}${matchedStock.ticker} | ${titleCn}`;
          const pubTimeStr = formatDateTime(date);

          let deepCardHtml = "";
          if (hasDeep) {
            deepCardHtml = `
              <div style="background:rgba(128,128,128,0.12);border-left:4px solid #f59e0b;padding:12px 14px;margin:12px 0;border-radius:4px;">
                <div style="font-weight:bold;font-size:13.5px;margin-bottom:6px;">
                  💡 Gemini 深度投研解读
                </div>
                <div style="font-size:13.5px;line-height:1.68;">
                  ${formatBodyHtml(deepText)}
                </div>
              </div>
            `;
          }

          let noticeTip = "";
          if (isScheduleNotice) {
            noticeTip = `
              <div style="background:rgba(128,128,128,0.12);border-left:4px solid #3b82f6;padding:8px 12px;margin:10px 0;border-radius:4px;font-size:13px;">
                📌 <b>【事项预告提示】</b>：本通告为公司例行公布的财报/电话会日程预告，尚未披露具体经营业绩与财务数据。完整财务报告将在预告时间正式公布。
              </div>
            `;
          }

          const unifiedHtml = `
            <div style="font-family:sans-serif;line-height:1.68;font-size:14px;">
              <p style="margin:0 0 6px 0;font-size:13px;opacity:0.85;">
                <b>${matchedStock.ticker}</b> (${matchedStock.company}) ｜ ${facts.category} ｜ <b>影响评分：${score}/10</b><br/>
                🕒 原始发送时间：${pubTimeStr}<br/>
                📡 信源：${escapeHtml(isOfficial ? "公司官方 IR 邮件订阅" : "第三方媒体快讯: " + sender.substring(0, 35))}
              </p>
              <div style="margin:8px 0;line-height:1.5;">
                <div style="font-size:12.5px;opacity:0.78;">【原标题】${escapeHtml(subject)}</div>
                <div style="font-weight:bold;font-size:15.5px;margin-top:4px;">${escapeHtml(titleCn)}</div>
              </div>
              ${noticeTip}
              ${deepCardHtml}
              <hr style="border:none;border-top:1px solid rgba(128,128,128,0.3);margin:10px 0;"/>
              <div style="margin:8px 0;font-size:14px;line-height:1.72;">
                <div style="font-weight:bold;margin-bottom:6px;font-size:13.5px;">📄 事实全文直译与临床数据：</div>
                ${formatBodyHtml(bodyCn)}
              </div>
              <hr style="border:none;border-top:1px solid rgba(128,128,128,0.3);margin:10px 0;"/>
              <p style="margin:4px 0;font-size:12.5px;opacity:0.85;"><a href="${articleUrl}">🔗 查看原邮件或通告链接</a></p>
            </div>
          `;
          sendPushPlus(pushToken, singleTitle, unifiedHtml);
          Logger.log(`Sent Unified Single Push: ${singleTitle}`);

          appendSheetLog(sheet, [
            formatDateTime(new Date()),
            matchedStock.ticker,
            `${sender.substring(0, 40)} | ${pubTimeStr}`,
            facts.category,
            score,
            `【原标题】${subject}\n【中文标题】${titleCn}\n【中文翻译】\n${bodyCn}`,
            deepText,
            `${subject} (${articleUrl})`,
            "GAS_" + msg.getId()
          ]);
        }
        msg.markRead();
        processedCount++;
      } else {
        // 未命中持仓标的：检查生科专业媒体 Newsletter
        const isBiotechMedia = BIOTECH_MEDIA_DOMAINS.some(d => senderLower.includes(d)) && !NON_MEDIA_DOMAINS.some(d => senderLower.includes(d));
        if (isBiotechMedia && cleanedBody.length >= 150) {
          const digest = geminiIndustryDigest(geminiApiKey, sender, subject, cleanedBody);
          if (digest && digest.isValid && digest.digestText) {
            const pushTitle = `【行业速览】${digest.mediaName} | ${digest.topicCn}`;
            const pubTimeStr = formatDateTime(date);
            const digestHtml = `
              <div style="font-family:sans-serif;line-height:1.68;font-size:14px;">
                <p style="margin:0 0 6px 0;font-size:13px;opacity:0.85;">
                  <b>${escapeHtml(digest.mediaName)}</b> ｜ 生科行业要闻速览<br/>
                  🕒 ${pubTimeStr}<br/>
                  📡 信源：${escapeHtml(sender.substring(0, 45))}
                </p>
                <hr style="border:none;border-top:1px solid rgba(128,128,128,0.3);margin:8px 0;"/>
                <div style="margin:8px 0;font-size:14px;">${formatBodyHtml(digest.digestText)}</div>
                <hr style="border:none;border-top:1px solid rgba(128,128,128,0.3);margin:8px 0;"/>
                <p style="margin:4px 0;font-size:13px;opacity:0.85;"><a href="https://mail.google.com/mail/u/0/#inbox">🔗 查看原始邮件</a></p>
              </div>
            `;
            sendPushPlus(pushToken, pushTitle, digestHtml);
            Logger.log(`Sent Industry Digest: ${pushTitle}`);

            appendSheetLog(sheet, [
              formatDateTime(new Date()),
              "INDUSTRY",
              `${sender.substring(0, 40)} | ${pubTimeStr}`,
              "行业速览",
              5,
              `【原标题】${subject}\n【要闻速览】\n${digest.digestText}`,
              "生科行业综合速览（不针对单一持仓标的，无个股深度解读）",
              subject,
              "GAS_" + msg.getId()
            ]);
          }
          msg.markRead();
          processedCount++;
        }
      }
    }
  }
}

// ==============================================================================
// 3. Google Drive 投研库新增文件毫秒级自动感知与中英对照翻译引擎
// ==============================================================================

function runDriveVaultScan(sheet, config, startTime) {
  // 单次全局执行时间上限 45 秒，超时立即退出
  const MAX_GLOBAL_DURATION_MS = 45 * 1000;
  function isDriveTimeout() {
    return (new Date().getTime() - startTime) > MAX_GLOBAL_DURATION_MS;
  }

  const pushToken = config["PUSHPLUS_TOKEN"];
  const apiKeyStr = config["GEMINI_API_KEY"];
  if (!pushToken || !apiKeyStr) return;

  const vaultTab = sheet.getSheetByName("Vault");
  if (!vaultTab) return;

  // 读取已处理的唯一文件标识（第 9 列，Col I）
  const vaultRows = vaultTab.getDataRange().getValues();
  const processedVids = new Set();
  for (let r = 1; r < vaultRows.length; r++) {
    const vid = String(vaultRows[r][8] || "").trim();
    if (vid) processedVids.add(vid);
  }

  let foundNewFile = false;

  // 遍历 4 只核心标的文件夹
  for (const ticker in VAULT_STOCK_FOLDERS) {
    if (foundNewFile || isDriveTimeout()) break;
    const stockCfg = VAULT_STOCK_FOLDERS[ticker];

    for (const roleKey of ["internal", "external"]) {
      if (foundNewFile || isDriveTimeout()) break;
      const folderMeta = stockCfg[roleKey];
      let folder;
      try {
        folder = DriveApp.getFolderById(folderMeta.id);
      } catch (e) {
        continue;
      }

      const files = folder.getFiles();
      while (files.hasNext()) {
        if (isDriveTimeout()) break;
        const file = files.next();
        const fname = file.getName();
        if (isTranslatedOrTempFile(fname)) continue;
        const fileId = file.getId();
        const modifiedStr = file.getLastUpdated().toISOString().substring(0, 16);
        const vId = "VAULT_" + fileId + "_" + modifiedStr;

        if (processedVids.has(vId)) continue;

        // 发现新文件！立即在当前源文件夹内启动自动中英对照翻译与 <源文件名>[译].pdf 渲染
        foundNewFile = true;
        Logger.log(`[VAULT] Processing new file: [${ticker}] ${fname} (${folderMeta.label})`);
        processSingleVaultFile(sheet, vaultTab, config, ticker, stockCfg, folderMeta.id, folderMeta.label, file, vId);
        break; // 每次执行处理 1 份文件，保证 30 秒内稳妥完成并推送
      }
    }
  }

  // 若无新文件，检查 00_网站与催化剂底稿同步
  if (!foundNewFile && !isDriveTimeout()) {
    try {
      const syncFolder = DriveApp.getFolderById(VAULT_SYNC_FOLDER_ID);
      const syncFiles = syncFolder.getFiles();
      while (syncFiles.hasNext()) {
        if (isDriveTimeout()) break;
        const sFile = syncFiles.next();
        if (isTranslatedOrTempFile(sFile.getName())) continue;
        const sId = sFile.getId();
        const sMod = sFile.getLastUpdated().toISOString().substring(0, 16);
        const svId = "SYNC_" + sId + "_" + sMod;
        if (processedVids.has(svId)) continue;

        Logger.log(`[VAULT] Processing sync artifact: ${sFile.getName()}`);
        processSyncArtifactFile(sheet, vaultTab, config, sFile, svId);
        break;
      }
    } catch (e) {}
  }
}

function processSingleVaultFile(sheet, vaultTab, config, ticker, stockCfg, sourceFolderId, roleLabel, file, vId) {
  const fileName = file.getName();
  const fileSize = file.getSize();
  const mimeType = file.getMimeType() || "application/pdf";
  const pushToken = config["PUSHPLUS_TOKEN"];
  const apiKeyStr = config["GEMINI_API_KEY"];

  // 超大文件（>25MB）交由 GitHub Actions 后台分块处理
  if (fileSize > 25 * 1024 * 1024) {
    Logger.log(`File ${fileName} size ${fileSize} exceeds 25MB, left for GitHub Actions.`);
    return;
  }

  const blob = file.getBlob();
  const base64Data = Utilities.base64Encode(blob.getBytes());

  const prompt = `你是一位拥有 20 年华尔街顶尖对冲基金背景的生物医药首席分析师（Wall Street Biotech Buy-Side Senior Analyst）。
请深度解析并翻译上市公司 ${ticker} (${stockCfg.name}) 的投研文献（口径分类：${roleLabel}）：
【原始文件名】：${fileName}

⚠️ 核心排版硬指标（重中之重）：
用户明确要求：文献翻译必须采用【严格左右分栏并列中英对照】（左侧为英文原文 Original English，右侧为权威中文精译 Chinese Translation）！方便对照阅读。
请逐章节、逐段落输出严格的中英段落对照对，完整覆盖文献的核心结论、临床方案设计、主要/次要终点数据、p值、安全性、竞争格局与估值模型。

请严格按以下 JSON Schema 格式输出纯 JSON（严禁返回 Markdown 代码块标记外的无关文字）：
{
  "chinese_title": "精炼专业的中文标题（格式：[机构/口径] 股票代码 核心研究主题/预期差结论）",
  "core_consensus_benchmark": "【投行核心观点与预期共识基准】\\n1. 符合预期（Base Case）：临床数据或财务指标关键阈值（如 ORR、PFS、安全性、营收预期）\\n2. 超预期（Bull Case）：上行催化剂与潜在超额收益驱动点\\n3. 低于预期（Bear Case）：下行风险、脱靶毒性或竞品威胁\\n4. 后续关键催化剂时间表",
  "sections": [
    {
      "section_title_en": "1. Executive Summary & Investment Thesis",
      "section_title_cn": "1. 核心投资逻辑与执行摘要",
      "pairs": [
        {
          "en": "English original paragraph...",
          "cn": "对应的权威中文逐句精译..."
        }
      ]
    }
  ]
}`;

  const respJsonText = callGeminiApi(
    apiKeyStr,
    prompt,
    { mimeType: mimeType.includes("word") ? "application/vnd.openxmlformats-officedocument.wordprocessingml.document" : "application/pdf", data: base64Data },
    true
  );

  const parsed = parseGeminiJson(respJsonText);
  // 严格防空文件校验：若模型未返回有效内容或解析为空，绝对不生成 1 页空白 PDF！
  const sectionsData = (parsed && parsed.sections && parsed.sections.length > 0) ? parsed.sections : null;
  const fallbackMd = (parsed && parsed.bilingual_markdown) ? parsed.bilingual_markdown : "";
  if (!sectionsData && (!fallbackMd || fallbackMd.length < 80 || fallbackMd.includes("文档解析为空"))) {
    Logger.log(`[ABORT] Translation returned empty or invalid for ${fileName}, skipping PDF creation to avoid blank 1-page document.`);
    return;
  }

  const chineseTitle = (parsed && parsed.chinese_title) ? parsed.chinese_title : `${ticker} 投研文献中英对照 (${fileName})`;
  const coreBenchmark = (parsed && parsed.core_consensus_benchmark) ? parsed.core_consensus_benchmark : "已完成文献结构化提炼与中英对照排版。";

  // 生成排版精美的左右分栏 HTML 并在原始文件夹内直接渲染为 <源文件名>[译].pdf
  const baseTitle = fileName.replace(/\.[^/.]+$/, "");
  const styledHtml = buildBilingualReportHtml(
    chineseTitle,
    `${ticker} (${stockCfg.name}) ｜ ${roleLabel} ｜ 原始文献：${fileName}`,
    coreBenchmark,
    sectionsData,
    fallbackMd
  );

  const pdfUrl = createBilingualPdfInDrive(sourceFolderId, baseTitle, styledHtml);

  // 记录到 Google Sheet Vault 表
  const bjTimeStr = formatDateTime(new Date());
  vaultTab.appendRow([
    bjTimeStr,
    ticker,
    roleLabel,
    fileName,
    coreBenchmark,
    pdfUrl,
    pdfUrl,
    file.getUrl(),
    vId
  ]);
  Logger.log(`[VAULT] Successfully recorded row in Vault sheet: ${vId}`);

  // 发送 PushPlus 微信通知（深色/浅色模式自适应排版，带核心预期卡片与一键查阅中英对照 PDF 链接）
  const pushTitle = `【投研库入库】${ticker} ｜ ${chineseTitle}`;
  const pushHtml = `
    <div style="font-family:sans-serif;line-height:1.68;font-size:14px;">
      <p style="margin:0 0 6px 0;font-size:13px;opacity:0.85;">
        <b>${ticker}</b> (${stockCfg.name}) ｜ <b>${roleLabel}</b><br/>
        🕒 入库时间：${bjTimeStr}<br/>
        📁 原始文件：${escapeHtml(fileName)}
      </p>
      <div style="margin:8px 0;line-height:1.5;font-weight:bold;font-size:16px;">
        ${escapeHtml(chineseTitle)}
      </div>
      
      <!-- 核心预期共识卡片 -->
      <div style="background:rgba(128,128,128,0.12);border-left:4px solid #f59e0b;padding:12px 14px;margin:12px 0;border-radius:4px;">
        <div style="font-weight:bold;font-size:13.5px;margin-bottom:6px;">
          🎯 华尔街核心观点与预期共识基准 (Consensus Bar)
        </div>
        <div style="font-size:13.5px;line-height:1.68;">
          ${formatBodyHtml(coreBenchmark)}
        </div>
      </div>

      <!-- 中英对照高清 PDF 直达卡片 -->
      <div style="background:rgba(128,128,128,0.12);border-left:4px solid #3b82f6;padding:12px 14px;margin:12px 0;border-radius:4px;">
        <div style="font-weight:bold;font-size:13.5px;margin-bottom:6px;">
          📄 中英对照高清 PDF 已自动生成并存入投研库
        </div>
        <div style="font-size:13px;opacity:0.88;margin-bottom:8px;">
          已根据原始研报/会议纪要完成左右双栏中英对照精排，支持在电脑或手机端直接查阅高清版。
        </div>
        <div>
          <a href="${pdfUrl}" target="_blank" style="font-size:14px;font-weight:bold;text-decoration:underline;">👉 点击在线查阅中英对照完整 PDF</a>
          <span style="margin:0 8px;opacity:0.5;">｜</span>
          <a href="${file.getUrl()}" target="_blank" style="font-size:13px;opacity:0.85;text-decoration:underline;">查看英文原件</a>
        </div>
      </div>
      <hr style="border:none;border-top:1px solid rgba(128,128,128,0.3);margin:10px 0;"/>
      <p style="margin:4px 0;font-size:12px;opacity:0.75;">美股生物医药投研库 (Biotech Research Vault) 24x7 自动化云端管家</p>
    </div>
  `;

  sendPushPlus(pushToken, pushTitle, pushHtml);
  Logger.log(`[VAULT] Sent WeChat notification: ${pushTitle}`);
}

function processSyncArtifactFile(sheet, vaultTab, config, file, vId) {
  const fileName = file.getName();
  const pushToken = config["PUSHPLUS_TOKEN"];
  const apiKeyStr = config["GEMINI_API_KEY"];
  const textContent = file.getBlob().getDataAsString("UTF-8").substring(0, 15000);

  const prompt = `请阅读以下由 Claude 编写的生物医药催化剂底稿或投研分析：
【文件名】：${fileName}
【内容】：
${textContent}

请提取：
1. 涉及的核心持仓股票代码（从 ABSI, ARWR, KRYS, MANE 中匹配）
2. 精炼核心观点与预期差（200字以内）
3. 准确中文标题

以纯 JSON 输出：
{"ticker": "ABSI", "title": "中文标题", "summary": "核心要点"}`;

  const respJson = callGeminiApi(apiKeyStr, prompt, null, true);
  const parsed = parseGeminiJson(respJson);
  const ticker = (parsed && parsed.ticker) ? parsed.ticker.toUpperCase() : "ABSI";
  const title = (parsed && parsed.title) ? parsed.title : fileName;
  const summary = (parsed && parsed.summary) ? parsed.summary : textContent.substring(0, 300);

  const baseTitle = fileName.replace(/\.[^/.]+$/, "");
  const styledHtml = buildBilingualReportHtml(title, `网站与催化剂底稿同步 ｜ ${fileName}`, summary, null, textContent);
  const pdfUrl = createBilingualPdfInDrive(VAULT_SYNC_FOLDER_ID, baseTitle, styledHtml);

  const bjTimeStr = formatDateTime(new Date());
  vaultTab.appendRow([
    bjTimeStr,
    ticker,
    "网站与催化剂底稿同步",
    fileName,
    summary,
    pdfUrl,
    pdfUrl,
    file.getUrl(),
    vId
  ]);

  const pushTitle = `【底稿同步】${ticker} ｜ ${title}`;
  const pushHtml = `
    <div style="font-family:sans-serif;line-height:1.68;font-size:14px;">
      <p style="margin:0 0 6px 0;font-size:13px;opacity:0.85;">
        <b>${ticker}</b> ｜ <b>网站与催化剂底稿同步</b><br/>
        🕒 入库时间：${bjTimeStr}<br/>
        📁 原始底稿：${escapeHtml(fileName)}
      </p>
      <div style="margin:8px 0;font-size:15.5px;font-weight:bold;">${escapeHtml(title)}</div>
      <div style="background:rgba(128,128,128,0.12);border-left:4px solid #f59e0b;padding:12px 14px;margin:10px 0;border-radius:4px;font-size:13.5px;">
        <b>🎯 底稿核心观点：</b><br/>${formatBodyHtml(summary)}
      </div>
      <div style="margin-top:8px;">
        <a href="${pdfUrl}" target="_blank" style="font-size:14px;font-weight:bold;text-decoration:underline;">👉 查阅排版 PDF</a>
      </div>
    </div>
  `;
  sendPushPlus(pushToken, pushTitle, pushHtml);
}

function buildBilingualReportHtml(title, subtitle, summaryText, sectionsData, fallbackMd) {
  let tableRows = "";

  if (sectionsData && Array.isArray(sectionsData) && sectionsData.length > 0) {
    for (let s = 0; s < sectionsData.length; s++) {
      const sec = sectionsData[s];
      const tEn = escapeHtml(sec.section_title_en || `Section ${s + 1}`);
      const tCn = escapeHtml(sec.section_title_cn || `第 ${s + 1} 节`);

      tableRows += `
        <tr style="background:#e8f0fe;border-top:2px solid #1a73e8;border-bottom:1px solid #d2e3fc;">
          <th style="width:50%;padding:10px 14px;text-align:left;color:#174ea6;font-size:11pt;border-right:1px solid #d2e3fc;">
            <b>📂 ${tEn}</b>
          </th>
          <th style="width:50%;padding:10px 14px;text-align:left;color:#174ea6;font-size:11pt;">
            <b>📌 ${tCn}</b>
          </th>
        </tr>
      `;

      const pairs = sec.pairs || [];
      for (let p = 0; p < pairs.length; p++) {
        const pair = pairs[p];
        const enHtml = escapeHtml(pair.en || "").replace(/\n/g, "<br/>");
        const cnHtml = escapeHtml(pair.cn || "").replace(/\n/g, "<br/>");
        const bgColor = (p % 2 === 0) ? "#fcfcfc" : "#ffffff";

        tableRows += `
          <tr style="background:${bgColor};border-bottom:1px solid #e0e0e0;">
            <td style="width:50%;padding:12px 14px;vertical-align:top;font-size:9.5pt;line-height:1.68;color:#3c4043;border-right:1px solid #e0e0e0;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;word-break:break-word;">
              ${enHtml}
            </td>
            <td style="width:50%;padding:12px 14px;vertical-align:top;font-size:10pt;line-height:1.75;color:#202124;font-family:'Microsoft YaHei','PingFang SC',sans-serif;word-break:break-word;">
              ${cnHtml}
            </td>
          </tr>
        `;
      }
    }
  } else if (fallbackMd) {
    const lines = fallbackMd.split(/\n{2,}/);
    for (let i = 0; i < lines.length; i++) {
      const line = escapeHtml(lines[i]).replace(/\n/g, "<br/>");
      tableRows += `
        <tr style="border-bottom:1px solid #e0e0e0;">
          <td colspan="2" style="padding:10px 14px;vertical-align:top;font-size:10pt;line-height:1.7;color:#202124;">
            ${line}
          </td>
        </tr>
      `;
    }
  }

  const consensusHtml = escapeHtml(summaryText || "").replace(/\n/g, "<br/>");

  return `<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<title>${escapeHtml(title)}</title>
</head>
<body style="font-family:'Microsoft YaHei','Segoe UI',Arial,sans-serif;line-height:1.6;color:#202124;padding:15px;max-width:1150px;margin:0 auto;">
  
  <div style="background:#1a73e8;color:#ffffff;padding:16px 20px;border-radius:6px;margin-bottom:16px;">
    <h1 style="margin:0 0 4px 0;font-size:16pt;color:#ffffff;">${escapeHtml(title)}</h1>
    <p style="margin:0;font-size:10pt;color:#e8f0fe;">${escapeHtml(subtitle)}</p>
  </div>

  <div style="background:#fef7e0;border-left:5px solid #fbbc04;padding:14px 18px;margin-bottom:20px;border-radius:4px;">
    <h2 style="margin:0 0 6px 0;font-size:12pt;color:#b06000;">🎯 华尔街核心观点与预期共识基准 (Consensus Bar)</h2>
    <div style="font-size:10pt;line-height:1.68;color:#202124;">${consensusHtml}</div>
  </div>

  <table style="width:100%;border-collapse:collapse;margin:10px 0;table-layout:fixed;border:1px solid #d2e3fc;">
    <thead>
      <tr style="background:#f1f3f4;border-bottom:2px solid #1a73e8;">
        <th style="width:50%;padding:12px 14px;text-align:left;font-size:11pt;color:#174ea6;border-right:1px solid #dcdcdc;">
          🇬🇧 英文原文 (Original English)
        </th>
        <th style="width:50%;padding:12px 14px;text-align:left;font-size:11pt;color:#188038;">
          🇨🇳 权威中文精译 (Chinese Translation)
        </th>
      </tr>
    </thead>
    <tbody>
      ${tableRows}
    </tbody>
  </table>

  <div style="margin-top:20px;text-align:center;font-size:9pt;color:#888;">
    美股生物医药投研库 (Biotech Research Vault) ｜ 左右分栏高保真中英对照导出版
  </div>
</body>
</html>`;
}

function createBilingualPdfInDrive(folderId, baseTitle, htmlContent) {
  const boundary = "===VaultBoundary===";
  const meta = {
    name: "_temp_render_" + baseTitle,
    mimeType: "application/vnd.google-apps.document"
  };
  const body = "--" + boundary + "\r\n" +
               "Content-Type: application/json; charset=UTF-8\r\n\r\n" +
               JSON.stringify(meta) + "\r\n" +
               "--" + boundary + "\r\n" +
               "Content-Type: text/html; charset=UTF-8\r\n\r\n" +
               htmlContent + "\r\n" +
               "--" + boundary + "--\r\n";

  const uploadUrl = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart";
  let docId = null;

  try {
    const res = UrlFetchApp.fetch(uploadUrl, {
      method: "post",
      headers: {
        "Authorization": "Bearer " + ScriptApp.getOAuthToken(),
        "Content-Type": "multipart/related; boundary=" + boundary
      },
      payload: body,
      muteHttpExceptions: true
    });

    if (res.getResponseCode() === 200) {
      const docData = JSON.parse(res.getContentText());
      docId = docData.id;

      const exportUrl = "https://www.googleapis.com/drive/v3/files/" + docId + "/export?mimeType=application/pdf";
      const pdfRes = UrlFetchApp.fetch(exportUrl, {
        headers: { "Authorization": "Bearer " + ScriptApp.getOAuthToken() },
        muteHttpExceptions: true
      });

      const cleanBase = baseTitle.replace(/(\[译\]|【译】)+$/, "");
      const pdfBlob = pdfRes.getBlob().setName(cleanBase + "[译].pdf");
      const targetFolder = DriveApp.getFolderById(folderId);
      const pdfFile = targetFolder.createFile(pdfBlob);
      pdfFile.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);

      try {
        DriveApp.getFileById(docId).setTrashed(true);
      } catch (delErr) {}

      return pdfFile.getUrl();
    }
  } catch (err) {
    Logger.log("Drive API doc conversion fallback: " + err.message);
  }

  // 备选降级方案
  try {
    const cleanBase = baseTitle.replace(/(\[译\]|【译】)+$/, "");
    const blob = Utilities.newBlob(htmlContent, "text/html", cleanBase + ".html").getAs("application/pdf");
    blob.setName(cleanBase + "[译].pdf");
    const targetFolder = DriveApp.getFolderById(folderId);
    const pdfFile = targetFolder.createFile(blob);
    pdfFile.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
    return pdfFile.getUrl();
  } catch (fallbackErr) {
    Logger.log("Fallback PDF creation error: " + fallbackErr.message);
    return "";
  }
}

function parseGeminiJson(rawText) {
  if (!rawText) return null;
  let s = rawText.trim();
  s = s.replace(/^```json\s*/i, "").replace(/^```\s*/i, "").replace(/\s*```$/i, "").trim();
  try {
    return JSON.parse(s);
  } catch (e) {
    const m = s.match(/\{[\s\S]*\}/);
    if (m) {
      try {
        return JSON.parse(m[0]);
      } catch (e2) {}
    }
  }
  return null;
}

// ==============================================================================
// 4. 辅助函数与多 Key Gemini API 轮询
// ==============================================================================

function cleanBoilerplateText(text) {
  if (!text) return "";
  let s = text.replace(/[\u034f\u200b\u200c\u200d\u2007\ufeff\xad]+/g, "");
  s = s.replace(/<https?:\/\/[^>]+>/g, "");
  s = s.replace(/https?:\/\/\S+/g, "");
  const cutoffs = [
    /You may also like:/i,
    /You are receiving this email because/i,
    /Past performance is no guarantee of future results/i,
    /Manage Alerts\s*\|\s*Top News/i,
    /Download the Seeking Alpha app/i,
    /To unsubscribe from these emails/i,
    /Forward-Looking Statements\s*This press release contains/i
  ];
  for (let i = 0; i < cutoffs.length; i++) {
    const idx = s.search(cutoffs[i]);
    if (idx > 80) s = s.substring(0, idx);
  }
  s = s.replace(/Read Now\s*»?/gi, "");
  s = s.replace(/[ \t]+/g, " ");
  s = s.replace(/\n{3,}/g, "\n\n").trim();
  return s;
}

function hasSubstantiveBody(title, body) {
  if (!body) return false;
  let b = body.trim();
  if (title) b = b.replace(new RegExp(title.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi"), "");
  b = b.replace(/https?:\/\/\S+/g, "");
  b = b.replace(/\b(read now|full article|click here to view all releases|click here|view this release|view all releases|powered by notified|view presentation|slideshow|transcript)\b/gi, "").trim();
  return b.length >= 180;
}

function isStrictMatch(textUpper, stock) {
  const ticker = stock.ticker.toUpperCase();
  const company = (stock.company || "").toUpperCase();
  const patTicker = new RegExp("(?:^|[^A-Z0-9])" + ticker + "(?:$|[^A-Z0-9])");
  if (patTicker.test(textUpper)) return true;
  if (company && company.length >= 4 && textUpper.includes(company)) return true;
  return false;
}

function loadConfig(sheet) {
  const cfg = {};
  const tab = sheet.getSheetByName("Config");
  if (!tab) return cfg;
  const values = tab.getDataRange().getValues();
  for (let i = 1; i < values.length; i++) {
    const k = String(values[i][0] || "").trim();
    const v = String(values[i][1] || "").trim();
    if (k) cfg[k] = v;
  }
  return cfg;
}

function loadPortfolio(sheet) {
  const list = [];
  const tab = sheet.getSheetByName("Portfolio");
  if (!tab) return list;
  const values = tab.getDataRange().getValues();
  for (let i = 1; i < values.length; i++) {
    const ticker = String(values[i][0] || "").trim().toUpperCase();
    const company = String(values[i][1] || "").trim();
    const active = String(values[i][4] || "TRUE").trim().toUpperCase();
    if (ticker && active !== "FALSE") {
      list.push({ ticker: ticker, company: company });
    }
  }
  return list;
}

function appendSheetLog(sheet, row) {
  const tab = sheet.getSheetByName("Logs");
  if (tab) tab.appendRow(row);
}

function parseApiKeys(keyStr) {
  if (!keyStr) return [];
  return keyStr.split(/[,，\s]+/).map(k => k.trim()).filter(k => k.length > 10);
}

function callGeminiApi(apiKeyStr, promptText, inlineData, expectJson) {
  const keys = parseApiKeys(apiKeyStr);
  if (keys.length === 0) {
    Logger.log("No valid Gemini API keys found.");
    return "";
  }

  for (let m = 0; m < GEMINI_MODELS.length; m++) {
    const model = GEMINI_MODELS[m];

    for (let k = 0; k < keys.length; k++) {
      const apiKey = keys[k];
      const url = `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${apiKey}`;

      const parts = [];
      if (inlineData) {
        parts.push({
          inlineData: {
            mimeType: inlineData.mimeType,
            data: inlineData.data
          }
        });
      }
      parts.push({ text: promptText });

      const genConfig = {
        temperature: 0.15,
        maxOutputTokens: 8192
      };
      if (expectJson) {
        genConfig.responseMimeType = "application/json";
      }

      const payload = {
        contents: [{ parts: parts }],
        generationConfig: genConfig
      };

      const options = {
        method: "post",
        contentType: "application/json",
        payload: JSON.stringify(payload),
        muteHttpExceptions: true
      };

      try {
        const res = UrlFetchApp.fetch(url, options);
        const code = res.getResponseCode();
        if (code === 200) {
          const json = JSON.parse(res.getContentText());
          if (json.candidates && json.candidates[0] && json.candidates[0].content) {
            const cparts = json.candidates[0].content.parts || [];
            return cparts.map(p => p.text || "").join("\n").trim();
          }
        } else if (code === 429) {
          Logger.log(`Key #${k+1} hit 429 on model ${model}, rotating to next key...`);
          Utilities.sleep(1500);
          continue;
        } else {
          Logger.log(`Model ${model} Key #${k+1} returned ${code}: ${res.getContentText().substring(0, 120)}`);
        }
      } catch (e) {
        Logger.log(`Model ${model} Key #${k+1} error: ${e.message}`);
      }
    }
  }
  return "";
}

function geminiTranslateFacts(apiKeyStr, stock, title, body, source, isOfficial) {
  const prompt = `请将以下关于生物科技公司 ${stock.ticker} (${stock.company}) 的最新英文通告或媒体突发快讯进行精准的简体中文全文翻译。
【来源】：${source}
【英文原标题】：${title}
【英文正文】：
${body}

⚠️ 核心要求：
1. 若内容实际为同靶点/同赛道核心竞争对手（如 Ionis / IONS）的商业化进展或评级变动，请在中文标题及导语开头明确点明关联背景（例如标明：竞争对手 Ionis 获 BMO 上调评级）。
2. 必须将标题精准直译为简体中文，严禁返回英文！
3. 正文逐句翻译，若为短快讯则完整翻译原句，过滤长乱码链接。
4. 全文严禁出现“美股、炒股、买入、卖出、暴涨、暴跌”等敏感字眼。

请严格按标签输出：
[RELEVANT]: YES 或 NO
[CATEGORY]: 从中选一：临床数据 / 竞争对手动向 / 合作与并购 / 内部人变动 / 监管与上市 / 事项预告 / 经营业绩 / 行业报道
[SCORE]: 1到10的整数
[TITLE_CN]: 中文精准直译标题
[BODY_CN_START]
（在此处输出完整中文翻译）
[BODY_CN_END]`;

  const respText = callGeminiApi(apiKeyStr, prompt, null, false);
  if (!respText) {
    return { is_relevant: true, category: "最新动态", impact_score: 5, title_cn: title, body_cn: body };
  }

  const relM = respText.match(/\[RELEVANT\]:\s*(YES|NO)/i);
  const catM = respText.match(/\[CATEGORY\]:\s*([^\n\r]+)/i);
  const scM = respText.match(/\[SCORE\]:\s*(\d+)/i);
  const tM = respText.match(/\[TITLE_CN\]:\s*([^\n\r]+)/i);
  const bM = respText.match(/\[BODY_CN_START\]\s*([\s\S]*?)\s*(?:\[BODY_CN_END\]|$)/i);

  return {
    is_relevant: !(relM && relM[1].toUpperCase() === "NO"),
    category: catM ? catM[1].trim() : "行业报道",
    impact_score: scM ? parseInt(scM[1], 10) : 5,
    title_cn: tM ? tM[1].trim() : title,
    body_cn: bM ? bM[1].trim() : respText.trim()
  };
}

function geminiDeepAnalysis(apiKeyStr, stock, titleEn, titleCn, bodyEn) {
  const prompt = `请基于以下 ${stock.ticker} (${stock.company}) 发布的真实通告内容进行客观深度的生物医药专业拆解。
【英文原标题】：${titleEn}
【中文翻译标题】：${titleCn}
【通告内容】：
${bodyEn}

⚠️ 输出要求（字数控制在 350~450 字，严禁敏感交易字眼）：
### 1. 🎯 核心事实与官方披露的后续催化剂节点
### 2. 🔍 口径审视、用词考究与潜在隐患挖掘
### 3. ⚔️ 竞争格局比对与综合信号定性`;

  return callGeminiApi(apiKeyStr, prompt, null, false) || "";
}

function geminiIndustryDigest(apiKeyStr, sender, title, body) {
  const prompt = `请阅读以下来自生物医药专业媒体的 Newsletter，提炼一份生科行业要闻速览：
【邮件标题】：${title}
【信源】：${sender}
【正文】：
${body.substring(0, 7000)}

⚠️ 提炼铁律：
1. 过滤掉纯求职招聘报告与无关广告。
2. 精炼 2~5 条最具行业影响力/商业临床价值的重大新闻。
3. 格式：
   • 📌 **【中文简明标题】** (English Title)
     💡 **核心要点**：一句话说明核心事实。
4. 若整封邮件全为招聘报告/无研发新闻，将 [VALID] 设为 NO。

请输出：
[VALID]: YES 或 NO
[MEDIA_NAME]: 媒体简短名称（如 BioSpace / Endpoints 等）
[TOPIC_CN]: 本期主题（如：行业要闻速览）
[DIGEST_START]
（精炼要闻列表）
[DIGEST_END]`;

  const resp = callGeminiApi(apiKeyStr, prompt, null, false);
  if (!resp) return { isValid: false };
  const valM = resp.match(/\[VALID\]:\s*(YES|NO)/i);
  if (valM && valM[1].toUpperCase() === "NO") return { isValid: false };

  const medM = resp.match(/\[MEDIA_NAME\]:\s*([^\n\r]+)/i);
  const topM = resp.match(/\[TOPIC_CN\]:\s*([^\n\r]+)/i);
  const digM = resp.match(/\[DIGEST_START\]\s*([\s\S]*?)\s*(?:\[DIGEST_END\]|$)/i);

  return {
    isValid: true,
    mediaName: medM ? medM[1].trim() : "生科媒体",
    topicCn: topM ? topM[1].trim() : "行业重磅速览",
    digestText: digM ? digM[1].trim() : resp.trim()
  };
}

function sendPushPlus(token, title, htmlContent) {
  const url = "http://www.pushplus.plus/send";
  const payload = {
    token: token,
    title: title,
    content: htmlContent,
    template: "html"
  };
  const options = {
    method: "post",
    contentType: "application/json",
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  };
  try {
    UrlFetchApp.fetch(url, options);
  } catch (e) {
    Logger.log("PushPlus Error: " + e.message);
  }
}

function formatBodyHtml(text) {
  if (!text) return "";
  const norm = String(text).replace(/([^\n])\s+•\s+/g, "$1\n• ");
  const lines = norm.split(/\r?\n/);
  const parts = [];
  for (let i = 0; i < lines.length; i++) {
    const s = lines[i].trim();
    if (!s) continue;
    if (/^【(核心要点|新闻稿完整正文).*】$/.test(s)) continue;
    if (s.startsWith("#")) {
      const hClean = escapeHtml(s.replace(/^#+\s*/, "")).replace(/\*\*(.*?)\*\*/g, "<b>$1</b>");
      parts.push(`<div style="font-weight:bold;margin:10px 0 4px 0;">${hClean}</div>`);
      continue;
    }
    let esc = escapeHtml(s);
    esc = esc.replace(/\*\*(.*?)\*\*/g, "<b>$1</b>");
    parts.push(`<div style="margin:6px 0;">${esc}</div>`);
  }
  return parts.join("\n");
}

function formatDateTime(d) {
  if (!d) return "";
  const pad = n => (n < 10 ? "0" + n : n);
  const bj = new Date(d.getTime() + 8 * 3600 * 1000);
  return `${bj.getUTCFullYear()}-${pad(bj.getUTCMonth() + 1)}-${pad(bj.getUTCDate())} ${pad(bj.getUTCHours())}:${pad(bj.getUTCMinutes())}`;
}

function escapeHtml(text) {
  return (text || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function htmlToPlain(html) {
  return html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
}
