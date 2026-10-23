/**
 * ==============================================================================
 * 美股生科催化剂极速直推引擎 (Google Apps Script 版)
 * 运行环境：Google Apps Script (部署于 Google 云端，永久免费、24x7 毫秒级直推)
 * ==============================================================================
 */

// 您的 Google Sheet ID（已自动绑定）
const SPREADSHEET_ID = "1pIKfEy9UNPs32W5gBFJu3N5doqU-SKCvIBDjX-TMwGQ";

// 可用的 Gemini 模型备选阵列（自动重试，杜绝 404 导致翻译回退英文）
const GEMINI_MODELS = [
  "gemini-3-flash-preview",
  "gemini-3.1-flash-lite",
  "gemini-3.5-flash",
  "gemini-3.6-flash"
];

// 官方 IR 邮箱与域名清单
const OFFICIAL_IR_DOMAINS = [
  "alert.gcs-web.com",
  "notified.com",
  "q4inc.com",
  "equisolve.com",
  "issuerdirect.com",
  "sec.gov"
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
  "microsoft.com",
  "openai.com",
  "substack.com"
];

/**
 * 主入口函数：由 Google Apps Script 定时触发器每 1 分钟自动调用
 */
function checkGmailAndPushRealtime() {
  const sheet = SpreadsheetApp.openById(SPREADSHEET_ID);
  const config = loadConfig(sheet);
  const portfolio = loadPortfolio(sheet);
  const geminiApiKey = config["GEMINI_API_KEY"];
  const pushToken = config["PUSHPLUS_TOKEN"];
  const threshold = parseInt(config["IMPACT_THRESHOLD"] || "7", 10);

  if (!pushToken || !geminiApiKey) {
    Logger.log("Missing PUSHPLUS_TOKEN or GEMINI_API_KEY in Sheet Config.");
    return;
  }

  // 查询最近 2 天内到达的未读邮件（排除垃圾箱和草稿）
  const threads = GmailApp.search("is:unread -in:trash -in:drafts newer_than:2d", 0, 15);
  if (!threads || threads.length === 0) {
    return;
  }

  for (let i = 0; i < threads.length; i++) {
    const thread = threads[i];
    const messages = thread.getMessages();
    for (let j = 0; j < messages.length; j++) {
      const msg = messages[j];
      if (!msg.isUnread()) continue;

      const subject = msg.getSubject() || "(无标题)";
      const sender = msg.getFrom() || "";
      const date = msg.getDate();
      const bodyHtml = msg.getBody() || "";
      const bodyPlain = msg.getPlainBody() || "";

      // 跳过订阅确认邮件本身
      if (/confirm your subscription|verify your email|welcome to .* investor/i.test(subject)) {
        msg.markRead();
        continue;
      }

      // 提取纯文本正文并清洗隐藏字符与长追踪链接
      let cleanedBody = cleanBoilerplateText(bodyPlain.length > 50 ? bodyPlain : htmlToPlain(bodyHtml));

      const senderLower = sender.toLowerCase();
      const isOfficial = OFFICIAL_IR_DOMAINS.some(d => senderLower.includes(d));

      // 若为官方 IR 邮件但正文过短，尝试提取邮件超链接并抓取完整正文
      let articleUrl = "https://mail.google.com/mail/u/0/#inbox";
      if (isOfficial && cleanedBody.length < 250 && bodyHtml) {
        const linkMatch = bodyHtml.match(/href=["'](https?:\/\/[^"']+)["']/i);
        if (linkMatch && !linkMatch[1].includes("unsubscribe")) {
          articleUrl = linkMatch[1];
          try {
            const fetchResp = UrlFetchApp.fetch(articleUrl, { muteHttpExceptions: true, followRedirects: true });
            if (fetchResp.getResponseCode() === 200) {
              const fetchedPlain = cleanBoilerplateText(htmlToPlain(fetchResp.getContentText()));
              if (fetchedPlain.length > cleanedBody.length) {
                cleanedBody = fetchedPlain;
              }
            }
          } catch (e) {
            Logger.log("Failed fetching full article from URL: " + e.message);
          }
        }
      }

      // 检查是否命中持仓标的
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
        // 过滤纯重复的幻灯片/录音重发件
        if (/(-(\s*)slideshow|-(\s*)transcript)$/i.test(subject.trim())) {
          msg.markRead();
          continue;
        }

        const hasSubstantive = hasSubstantiveBody(subject, cleanedBody);
        if (cleanedBody.trim().length < 20 && !hasSubstantive) {
          msg.markRead();
          continue;
        }

        // 调用 Gemini 进行 100% 逐字直译
        const facts = geminiTranslateFacts(geminiApiKey, matchedStock, subject, cleanedBody, sender, isOfficial);
        if (facts.is_relevant) {
          const score = facts.impact_score;
          const titlePrefix = isOfficial ? "【官方】" : "【第三方】";
          const titleCn = facts.title_cn || subject;
          const bodyCn = facts.body_cn || cleanedBody;

          // 纯日程/电话会预告判定
          const isScheduleNotice = /to report .* financial results|conference call on|webcast on|announces date for|financial results on/i.test(subject);
          if (isScheduleNotice) {
            facts.category = "事项预告";
          }

          // 准备深度解读（条件：高分、有实质长文、且非纯日程预告）
          let deepText = "";
          if (score >= threshold && hasSubstantive && bodyCn.trim().length >= 120 && !bodyCn.includes("仅含标题") && !isScheduleNotice) {
            Utilities.sleep(1500);
            const resDeep = geminiDeepAnalysis(geminiApiKey, matchedStock, subject, titleCn, cleanedBody);
            if (resDeep && !resDeep.includes("深度解读暂不可用") && resDeep.trim().length >= 50) {
              deepText = resDeep.trim();
            }
          }

          // 统一单条推送（彻底杜绝连发两条微信，绝不发送第二条“深度解读暂不可用”空壳消息）
          const hasDeep = Boolean(deepText);
          const badge = `【${isOfficial ? "官方" : "第三方"}${hasDeep ? "·深度" : ""}】`;
          const singleTitle = `${badge}${matchedStock.ticker} | ${titleCn}`;
          const pubTimeStr = formatDateTime(date);

          let deepCardHtml = "";
          if (hasDeep) {
            deepCardHtml = `
              <div style="background:#fef7e0;border-left:4px solid #fbbc04;padding:12px 14px;margin:12px 0;border-radius:4px;">
                <div style="font-weight:bold;color:#b06000;font-size:13px;margin-bottom:6px;">
                  💡 Gemini 深度投研解读
                </div>
                <div style="font-size:13.5px;line-height:1.65;color:#202124;">
                  ${formatBodyHtml(deepText)}
                </div>
              </div>
            `;
          }

          let noticeTip = "";
          if (isScheduleNotice) {
            noticeTip = `
              <div style="background:#e8f0fe;border-left:4px solid #1a73e8;padding:8px 12px;margin:10px 0;border-radius:4px;font-size:13px;color:#174ea6;">
                📌 <b>【事项预告提示】</b>：本通告为公司例行公布的财报/电话会日程预告，尚未披露具体经营业绩与财务数据。完整财务报告将在预告时间正式公布。
              </div>
            `;
          }

          const unifiedHtml = `
            <div style="font-family:sans-serif;line-height:1.68;font-size:14px;color:#202124;">
              <p style="margin:0 0 6px 0;font-size:13px;color:#555;">
                <b>${matchedStock.ticker}</b> (${matchedStock.company}) ｜ ${facts.category} ｜ <b>影响评分：${score}/10</b><br/>
                🕒 原始发送时间：${pubTimeStr}<br/>
                📡 信源：${escapeHtml(isOfficial ? "公司官方 IR 邮件订阅" : "第三方媒体快讯: " + sender.substring(0, 35))}
              </p>
              <div style="margin:8px 0;line-height:1.45;">
                <span style="color:#666;font-size:12px;">【原标题】</span> <span style="font-size:12.5px;color:#444;">${escapeHtml(subject)}</span><br/>
                <span style="color:#1a73e8;font-weight:bold;font-size:15px;">${escapeHtml(titleCn)}</span>
              </div>
              ${noticeTip}
              ${deepCardHtml}
              <hr style="border:none;border-top:1px solid #ddd;margin:10px 0;"/>
              <div style="margin:8px 0;font-size:14px;line-height:1.7;">
                <div style="font-weight:bold;color:#188038;margin-bottom:6px;font-size:13px;">📄 事实全文直译与临床数据：</div>
                ${formatBodyHtml(bodyCn)}
              </div>
              <hr style="border:none;border-top:1px solid #ddd;margin:10px 0;"/>
              <p style="margin:4px 0;font-size:12.5px;color:#666;"><a href="${articleUrl}">🔗 查看原邮件或通告链接</a></p>
            </div>
          `;
          sendPushPlus(pushToken, singleTitle, unifiedHtml);
          Logger.log(`Sent Unified Single Push: ${singleTitle}`);

          // 归档到 Google Sheet Logs
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
      } else {
        // 未匹配持仓个股：检查是否为生科专业媒体 Newsletter
        const isBiotechMedia = BIOTECH_MEDIA_DOMAINS.some(d => senderLower.includes(d)) && !NON_MEDIA_DOMAINS.some(d => senderLower.includes(d));
        if (isBiotechMedia && cleanedBody.length >= 150) {
          const digest = geminiIndustryDigest(geminiApiKey, sender, subject, cleanedBody);
          if (digest && digest.isValid && digest.digestText) {
            const pushTitle = `【行业速览】${digest.mediaName} | ${digest.topicCn}`;
            const pubTimeStr = formatDateTime(date);
            const digestHtml = `
              <div style="font-family:sans-serif;line-height:1.68;font-size:14px;">
                <p style="margin:0 0 6px 0;font-size:13px;">
                  <b>${escapeHtml(digest.mediaName)}</b> ｜ 生科行业要闻速览<br/>
                  🕒 ${pubTimeStr}<br/>
                  📡 信源：${escapeHtml(sender.substring(0, 45))}
                </p>
                <hr style="border:none;border-top:1px solid #555;margin:8px 0;"/>
                <div style="margin:8px 0;font-size:14px;white-space:pre-wrap;">${formatBodyHtml(digest.digestText)}</div>
                <hr style="border:none;border-top:1px solid #555;margin:8px 0;"/>
                <p style="margin:4px 0;font-size:13px;"><a href="https://mail.google.com/mail/u/0/#inbox">🔗 查看原始邮件</a></p>
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
        }
      }
    }
  }
}

// ----------------- 辅助函数与 API 调用 -----------------

function cleanBoilerplateText(text) {
  if (!text) return "";
  let s = text.replace(/[\u034f\u200b\u200c\u200d\u2007\ufeff\xad]+/g, "");
  // 清洗长追踪 URL，避免正文被长链接刷屏
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
  return b.length >= 160;
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

function geminiTranslateFacts(apiKey, stock, title, body, source, isOfficial) {
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

  const respText = callGeminiApi(apiKey, prompt);
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

function geminiDeepAnalysis(apiKey, stock, titleEn, titleCn, bodyEn) {
  const prompt = `请基于以下 ${stock.ticker} (${stock.company}) 发布的真实通告内容进行客观深度的生物医药专业拆解。
【英文原标题】：${titleEn}
【中文翻译标题】：${titleCn}
【通告内容】：
${bodyEn}

⚠️ 输出要求（字数控制在 350~450 字，严禁敏感交易字眼）：
### 1. 🎯 核心事实与官方披露的后续催化剂节点
### 2. 🔍 口径审视、用词考究与潜在隐患挖掘
### 3. ⚔️ 竞争格局比对与综合信号定性`;

  return callGeminiApi(apiKey, prompt) || "深度解读暂不可用。";
}

function geminiIndustryDigest(apiKey, sender, title, body) {
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

  const resp = callGeminiApi(apiKey, prompt);
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

function callGeminiApi(apiKey, promptText) {
  // 遍历可用模型阵列，确保模型不会因 Google 下架旧模型而 404
  for (let i = 0; i < GEMINI_MODELS.length; i++) {
    const model = GEMINI_MODELS[i];
    const url = `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${apiKey}`;
    const payload = {
      contents: [{ parts: [{ text: promptText }] }],
      generationConfig: { temperature: 0.2, maxOutputTokens: 8192 }
    };
    const options = {
      method: "post",
      contentType: "application/json",
      payload: JSON.stringify(payload),
      muteHttpExceptions: true
    };
    try {
      const res = UrlFetchApp.fetch(url, options);
      if (res.getResponseCode() === 200) {
        const json = JSON.parse(res.getContentText());
        return json.candidates[0].content.parts[0].text;
      } else {
        Logger.log(`Model ${model} returned ${res.getResponseCode()}: ${res.getContentText()}`);
      }
    } catch (e) {
      Logger.log(`Model ${model} fetch failed: ` + e.message);
    }
  }
  return "";
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
  let s = text.replace(/</g, "&lt;").replace(/>/g, "&gt;");
  // Highlights 加粗
  s = s.replace(/•\s*\*\*(.*?)\*\*/g, '• <b style="color:#ffffff;">$1</b>');
  s = s.replace(/\*\*(.*?)\*\*/g, '<b style="color:#ffffff;">$1</b>');
  return s;
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
