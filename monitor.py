#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生物医药与企业动态 24 小时云端情报核验引擎 (全自动免值守版)
核心特性：
1. 全面脱敏：去除敏感金融/荐股词汇，避免触发微信 PushPlus 通道风控拦截，确保 100% 稳定送达
2. 原文精译：自动抓取公告/报道正文并提供【核心原文完整中文直译】（保留全部原始实验数据、p值、样本量与专有名词）
3. 预设标尺对表 (Pre-set Rubric Audit)：自动读取 Google Sheet 中由 Gemini 4 预设的【达标标尺与竞品基准】及【排雷红线与口径观察点】，杜绝模型自由发挥乱下结论
4. 披露口径排雷：内置药企公关话术 (PR Spin) 审查铁律（ITT vs PP 人群、安慰剂校准差值、3级以上SAE、停药率、Form 4 交易代码定性）
"""

import datetime
import email
from email.header import decode_header
from email.utils import parsedate_to_datetime
import hashlib
import html
import imaplib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

SPREADSHEET_ID = os.environ.get(
    "SPREADSHEET_ID", "1pIKfEy9UNPs32W5gBFJu3N5doqU-SKCvIBDjX-TMwGQ"
)
SEC_USER_AGENT = "AntigravityBiotechMonitor/2.0 (neo664939@gmail.com)"

GEMINI_TRIAGE_MODELS = [
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite-preview",
]

GEMINI_DEEP_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3-flash-preview",
]


def http_json(url, method="GET", headers=None, payload=None, timeout=30):
    req_headers = headers.copy() if headers else {}
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json; charset=utf-8")
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def fetch_webpage_plain_text(url, is_sec=False, max_chars=5500):
    """抓取公告或媒体原文正文，去除 HTML 标签，供中文精译与细节排雷"""
    if not url or "news.google.com/rss/articles" in url:
        return ""
    try:
        headers = {
            "User-Agent": SEC_USER_AGENT if is_sec else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        raw = re.sub(r"(?is)<(script|style|head|nav|footer)[^>]*>.*?</\1>", " ", raw)
        text = re.sub(r"<[^>]+>", " ", html.unescape(raw))
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]
    except Exception:
        return ""


def get_google_access_token():
    raw = os.environ.get("GSHEETS_TOKEN_JSON", "").strip()
    if not raw:
        local_path = os.path.expanduser(
            r"~\.gemini\antigravity\plugin_data\gsheets\token.json"
        )
        if os.path.exists(local_path):
            with open(local_path, "r", encoding="utf-8") as f:
                raw = f.read()
    if not raw:
        raise RuntimeError("Missing GSHEETS_TOKEN_JSON credentials")

    cred = json.loads(raw)
    form_data = urllib.parse.urlencode(
        {
            "client_id": cred["client_id"],
            "client_secret": cred["client_secret"],
            "refresh_token": cred["token"]["refresh_token"],
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=form_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))["access_token"]


def load_sheet_state(access_token):
    ranges = ["Portfolio!A2:K50", "Config!A2:B20", "Logs!I2:I500"]
    q = "&".join(f"ranges={urllib.parse.quote(r)}" for r in ranges)
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values:batchGet?{q}"
    res = http_json(url, headers={"Authorization": f"Bearer {access_token}"})
    value_ranges = res.get("valueRanges", [])

    portfolio_rows = value_ranges[0].get("values", []) if len(value_ranges) > 0 else []
    config_rows = value_ranges[1].get("values", []) if len(value_ranges) > 1 else []
    log_id_rows = value_ranges[2].get("values", []) if len(value_ranges) > 2 else []

    config = {}
    for row in config_rows:
        if len(row) >= 2 and str(row[0]).strip():
            config[str(row[0]).strip()] = str(row[1]).strip()

    portfolio = []
    for row in portfolio_rows:
        if not row or not str(row[0]).strip():
            continue
        ticker = str(row[0]).strip().upper()
        # 兼容新11列结构(启用监控在第10列 index 9)与旧结构(index 7)
        if len(row) >= 10:
            enabled = str(row[9]).strip().upper() or "YES"
            benchmark = str(row[5]).strip()
            red_flags = str(row[6]).strip()
            avg_cost = str(row[7]).strip() or "未设置"
            pos_note = str(row[8]).strip() or "核心跟踪"
        else:
            enabled = str(row[7]).strip().upper() if len(row) > 7 and row[7] else "YES"
            benchmark = "对照同适应症标准疗法(SoC)与主流竞品评估有效性与安全性"
            red_flags = "核查主要终点是否达标、样本量是否充足、是否存在3级以上严重不良反应(SAE)或停药率过高"
            avg_cost = str(row[5]).strip() if len(row) > 5 and str(row[5]).strip() else "未设置"
            pos_note = str(row[6]).strip() if len(row) > 6 and str(row[6]).strip() else "核心跟踪"

        if enabled == "NO":
            continue

        company = str(row[1]).strip() if len(row) > 1 else ticker
        exchange = str(row[2]).strip() if len(row) > 2 else ""
        raw_kw = str(row[3]) if len(row) > 3 else ""
        kw_list = [k.strip() for k in re.split(r"[,，]", raw_kw) if k.strip()]
        keywords = list(dict.fromkeys([ticker, company] + kw_list))
        catalysts = str(row[4]).strip() if len(row) > 4 else ""

        portfolio.append(
            {
                "ticker": ticker,
                "company": company,
                "exchange": exchange,
                "keywords": keywords,
                "catalysts": catalysts,
                "benchmark": benchmark,
                "red_flags": red_flags,
                "avg_cost": avg_cost,
                "pos_note": pos_note,
            }
        )

    processed_ids = set()
    for row in log_id_rows:
        if row and str(row[0]).strip():
            processed_ids.add(str(row[0]).strip())

    return config, portfolio, processed_ids


def append_sheet_log(access_token, row_values):
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}"
        f"/values/Logs!A1:append?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS"
    )
    http_json(
        url,
        method="POST",
        headers={"Authorization": f"Bearer {access_token}"},
        payload={"values": [row_values]},
    )


def fetch_clinical_trials_context(stock):
    """直接调用 ClinicalTrials.gov API v2 获取该公司官方注册的临床试验方案底稿"""
    company_short = stock["company"].split(",")[0].split("Inc")[0].split("Corp")[0].strip()
    url = (
        "https://clinicaltrials.gov/api/v2/studies?query.spons="
        + urllib.parse.quote(company_short)
        + "&pageSize=5&format=json"
    )
    try:
        data = http_json(url, headers={"User-Agent": SEC_USER_AGENT}, timeout=15)
        studies = data.get("studies", [])
        if not studies:
            return "未检索到关联注册临床试验记录"
        lines = []
        for st in studies[:4]:
            proto = st.get("protocolSection", {})
            ident = proto.get("identificationModule", {})
            nct_id = ident.get("nctId", "")
            title = ident.get("briefTitle", "")
            status = proto.get("statusModule", {}).get("overallStatus", "")
            design = proto.get("designModule", {})
            phases = ",".join(design.get("phases", []))
            enrollment = design.get("enrollmentInfo", {}).get("count", "未知")
            outcomes = proto.get("outcomesModule", {}).get("primaryOutcomes", [])
            prim_ep = "; ".join(
                f"{o.get('measure', '')} ({o.get('timeFrame', '')})" for o in outcomes[:2]
            )
            lines.append(
                f"- [{nct_id}] ({phases}, 状态:{status}, 预设样本量:{enrollment}人) {title}\n"
                f"  官方登记主要终点: {prim_ep or '未披露'}"
            )
        return "\n".join(lines)
    except Exception:
        return "ClinicalTrials.gov 接口暂不可用"


def fetch_sec_cik_map():
    try:
        data = http_json(
            "https://www.sec.gov/files/company_tickers.json",
            headers={"User-Agent": SEC_USER_AGENT},
            timeout=20,
        )
        mapping = {}
        for _, item in data.items():
            t = str(item.get("ticker", "")).upper()
            cik = str(item.get("cik_str", "")).zfill(10)
            if t and cik:
                mapping[t] = cik
        return mapping
    except Exception:
        return {}


def fetch_sec_edgar_events(stock, cik, max_age_hours=36):
    events = []
    if not cik:
        return events
    try:
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        sub = http_json(url, headers={"User-Agent": SEC_USER_AGENT}, timeout=20)
        recent = sub.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        filing_dates = recent.get("filingDate", [])
        accept_times = recent.get("acceptanceDateTime", [])
        accessions = recent.get("accessionNumber", [])
        primary_docs = recent.get("primaryDocument", [])
        items_list = recent.get("items", [])

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        target_forms = {"8-K", "8-K/A", "4", "SC 13D", "SC 13D/A", "SC 13G", "6-K"}

        for i in range(min(len(forms), 25)):
            form = str(forms[i]).strip()
            if form not in target_forms:
                continue
            acc = str(accessions[i]).strip()
            acc_no_hyphen = acc.replace("-", "")
            doc = str(primary_docs[i]).strip()
            accept_str = str(accept_times[i]).strip() if i < len(accept_times) else ""
            fdate = str(filing_dates[i]).strip() if i < len(filing_dates) else ""

            if accept_str:
                try:
                    dt = datetime.datetime.fromisoformat(accept_str.replace("Z", "+00:00"))
                    if (now_utc - dt).total_seconds() / 3600.0 > max_age_hours:
                        continue
                except Exception:
                    pass

            cik_int = str(int(cik))
            doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no_hyphen}/{doc}"
            items_desc = str(items_list[i]) if i < len(items_list) else ""
            event_id = f"SEC_{stock['ticker']}_{acc}"

            # 自动抓取 SEC 8-K / Form 4 正文文本供翻译与核验
            doc_text = fetch_webpage_plain_text(doc_url, is_sec=True, max_chars=5000)

            form_label = {
                "8-K": "8-K 重大事项通告",
                "8-K/A": "8-K/A 补充事项通告",
                "4": "Form 4 内部人持股变动表",
                "SC 13D": "SC 13D 重要股东持股变动",
                "SC 13G": "SC 13G 机构持股超过5%备案",
            }.get(form, f"{form} 备案文件")

            events.append(
                {
                    "id": event_id,
                    "stock": stock,
                    "source": f"SEC 官方备案 ({form})",
                    "title": f"[{form_label}] {stock['company']} ({stock['ticker']}) 提交 {form} 文件 ({fdate})",
                    "content": (
                        f"标的: {stock['ticker']} ({stock['company']})\n"
                        f"文件类型: {form_label}\n"
                        f"提交时间: {accept_str or fdate}\n"
                        f"涉及条款 (Items): {items_desc}\n"
                        f"原文链接: {doc_url}\n"
                        f"文件正文提取:\n{doc_text or '(详见原文链接)'}"
                    ),
                    "url": doc_url,
                }
            )
    except Exception as e:
        print(f"[WARN] SEC check failed for {stock['ticker']}: {e}")
    return events


def fetch_media_rss_events(stock, max_age_hours=24):
    events = []
    company_short = stock["company"].split(",")[0].split("Inc")[0].split("Corp")[0].strip()
    search_terms = [stock["ticker"], company_short]
    for kw in stock["keywords"]:
        kw_clean = kw.strip()
        if kw_clean and kw_clean not in search_terms and len(kw_clean) >= 4:
            search_terms.append(kw_clean)
        if len(search_terms) >= 5:
            break
    or_clause = " OR ".join(f'"{t}"' for t in search_terms)
    query = (
        f"({or_clause}) "
        f"(site:globenewswire.com OR site:prnewswire.com OR site:businesswire.com "
        f"OR site:statnews.com OR site:fiercebiotech.com OR site:fiercepharma.com "
        f"OR site:biopharmadive.com OR site:endpointsnews.com OR site:reuters.com "
        f"OR site:seekingalpha.com) when:1d"
    )
    rss_url = (
        "https://news.google.com/rss/search?q="
        + urllib.parse.quote(query)
        + "&hl=en-US&gl=US&ceid=US:en"
    )
    try:
        req = urllib.request.Request(rss_url, headers={"User-Agent": SEC_USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            xml_bytes = resp.read()
        root = ET.fromstring(xml_bytes)
        channel = root.find("channel")
        if channel is None:
            return events

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        for item in channel.findall("item")[:4]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            guid = (item.findtext("guid") or link).strip()
            pub_date = (item.findtext("pubDate") or "").strip()
            source_el = item.find("source")
            publisher = source_el.text.strip() if source_el is not None and source_el.text else "行业媒体"
            raw_desc = item.findtext("description") or ""
            clean_desc = re.sub(r"<[^>]+>", " ", html.unescape(raw_desc)).strip()

            if pub_date:
                try:
                    dt = parsedate_to_datetime(pub_date)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=datetime.timezone.utc)
                    if (now_utc - dt).total_seconds() / 3600.0 > max_age_hours:
                        continue
                except Exception:
                    pass

            combined = f"{title} {clean_desc}".lower()
            if not any(kw.lower() in combined for kw in stock["keywords"] if len(kw) >= 3):
                continue

            short_hash = hashlib.sha1(guid.encode("utf-8")).hexdigest()[:16]
            event_id = f"RSS_{stock['ticker']}_{short_hash}"
            events.append(
                {
                    "id": event_id,
                    "stock": stock,
                    "source": f"公关通稿/权威媒体 ({publisher})",
                    "title": title,
                    "content": (
                        f"发布时间: {pub_date}\n"
                        f"来源媒体: {publisher}\n"
                        f"英文原文标题: {title}\n"
                        f"英文原文内容节选: {clean_desc}"
                    ),
                    "url": link,
                }
            )
    except Exception as e:
        print(f"[WARN] Media RSS failed for {stock['ticker']}: {e}")
    return events


def decode_mime_str(s):
    if not s:
        return ""
    parts = decode_header(s)
    out = []
    for content, enc in parts:
        if isinstance(content, bytes):
            out.append(content.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(str(content))
    return "".join(out)


def fetch_gmail_imap_events(config, portfolio, processed_ids):
    events = []
    app_pwd = config.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    gmail_user = config.get("GMAIL_ADDRESS", "neo664939@gmail.com").strip() or "neo664939@gmail.com"
    if not app_pwd or "可选" in app_pwd or "请" in app_pwd:
        return events

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(gmail_user, app_pwd)
        mail.select("INBOX")
        since_date = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=2)).strftime("%d-%b-%Y")
        status, data = mail.search(None, f'(UNSEEN SINCE "{since_date}")')
        if status != "OK" or not data or not data[0]:
            mail.logout()
            return events

        msg_nums = data[0].split()[-25:]
        for num in reversed(msg_nums):
            st, msg_data = mail.fetch(num, "(RFC822)")
            if st != "OK" or not msg_data or not msg_data[0]:
                continue
            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)
            msg_id_hdr = (msg.get("Message-ID") or str(num)).strip()
            ev_id = "GMAIL_" + hashlib.sha1(msg_id_hdr.encode("utf-8")).hexdigest()[:16]
            if ev_id in processed_ids:
                continue

            subject = decode_mime_str(msg.get("Subject", "(无标题)")).strip()
            sender = decode_mime_str(msg.get("From", "")).strip()
            body_text = ""
            if msg.is_multipart():
                for part in msg.walk():
                    ctype = part.get_content_type()
                    if ctype == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                        payload = part.get_payload(decode=True)
                        if payload:
                            body_text += payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            else:
                payload = msg.get_payload(decode=True)
                if payload:
                    body_text = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")

            if not body_text and msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/html":
                        payload = part.get_payload(decode=True)
                        if payload:
                            raw_html = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                            body_text = re.sub(r"<[^>]+>", " ", html.unescape(raw_html))

            combined = f"{subject}\n{sender}\n{body_text}".lower()
            for stock in portfolio:
                if any(kw.lower() in combined for kw in stock["keywords"] if len(kw) >= 3):
                    events.append(
                        {
                            "id": ev_id,
                            "stock": stock,
                            "source": f"Gmail 订阅邮件 ({sender[:40]})",
                            "title": subject,
                            "content": f"发件人: {sender}\n邮件标题: {subject}\n邮件英文原文内容:\n{body_text[:4500]}",
                            "url": "https://mail.google.com/mail/u/0/#inbox",
                        }
                    )
                    break
        mail.logout()
    except Exception as e:
        print(f"[WARN] Gmail IMAP check error: {e}")
    return events


def call_gemini(api_key, prompt, expect_json=False, use_search=False, deep_thinking=False, models=None):
    model_list = models or (GEMINI_TRIAGE_MODELS if expect_json else GEMINI_DEEP_MODELS)
    for model in model_list:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            f"?key={urllib.parse.quote(api_key)}"
        )
        gen_cfg = {"temperature": 0.1 if expect_json else 0.15}
        if expect_json:
            gen_cfg["responseMimeType"] = "application/json"
        if deep_thinking:
            gen_cfg["thinkingConfig"] = {"thinkingBudget": 8192}

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": gen_cfg,
        }
        if use_search:
            payload["tools"] = [{"google_search": {}}]

        try:
            resp = http_json(url, method="POST", payload=payload, timeout=60)
            candidates = resp.get("candidates", [])
            if candidates and "content" in candidates[0]:
                parts = candidates[0]["content"].get("parts", [])
                text = "\n".join(
                    p.get("text", "") for p in parts if p.get("text") and not p.get("thought")
                ).strip()
                if text:
                    u = resp.get("usageMetadata", {})
                    t_cnt = u.get("thoughtsTokenCount", 0)
                    label = f"{model} (Thinking:{t_cnt}tok)" if t_cnt else model
                    return text, label
        except urllib.error.HTTPError as e:
            print(f"[WARN] Gemini model {model} HTTP {e.code} (use_search={use_search}, thinking={deep_thinking})")
            if use_search:
                try:
                    payload.pop("tools", None)
                    resp = http_json(url, method="POST", payload=payload, timeout=50)
                    candidates = resp.get("candidates", [])
                    if candidates and "content" in candidates[0]:
                        parts = candidates[0]["content"].get("parts", [])
                        text = "\n".join(
                            p.get("text", "") for p in parts if p.get("text") and not p.get("thought")
                        ).strip()
                        if text:
                            u = resp.get("usageMetadata", {})
                            t_cnt = u.get("thoughtsTokenCount", 0)
                            label = f"{model} (Thinking:{t_cnt}tok)" if t_cnt else model
                            return text, label
                except Exception:
                    pass
        except Exception as e:
            print(f"[WARN] Gemini model {model} error: {e}")
    return None, None


def gemini_triage_and_translate(ev, api_key):
    """
    阶段一：极速评分 + 核心要点提炼 + 官方英文原文完整精准中文直译
    严格禁止出现任何股票推荐/金融交易敏感词
    """
    stock = ev["stock"]
    prompt = f"""请对以下关于生物科技公司 {stock['ticker']} ({stock['company']}) 的最新英文资料进行客观分级与精准中文翻译。
公司核心管线词库：{', '.join(stock['keywords'])}
重点跟踪事项：{stock['catalysts']}

【来源】：{ev['source']}
【标题】：{ev['title']}
【原始英文内容】：
{ev['content']}

⚠️ 严格要求：
1. 全文严禁出现“美股、炒股、买入、卖出、加仓、减仓、止盈、止损、暴涨、暴跌、基金经理、交易建议”等敏感字眼，一律使用客观科研与企业经营术语。
2. `chinese_translation` 字段必须将该新闻/通告的标题与核心原始段落完整、精准、逐句翻译为简体中文，100%保留原文中的所有实验数据、百分比、p值、剂量、样本量N、患者分组和关键英文专有名词缩写，不得删减数字或自行发挥。

请严格按以下 JSON 格式返回（只输出纯 JSON）：
{{
  "category": "从以下选一：临床数据 / 合作与并购 / 内部人变动 / 监管与上市 / 事项预告 / 经营业绩 / 行业报道 / 常规行政",
  "impact_score": 1到10的整数（1-3=常规行政/例行扣税变动等低相关事项；4-6=一般行业报道或常规推进；7-8=重要管线进展/明确数据读数时间窗预告/管理层主动增持(Code P)/重要合作里程碑；9-10=关键临床II/III期顶线数据/监管获批或CRL/收购并购）,
  "direction": "强正向 / 偏正向 / 中性 / 偏负向 / 强负向",
  "flash_summary": "50字以内的一句话核心事实摘要（只写客观事实与关键数字，不写废话）",
  "chinese_translation": "【标题及正文核心内容完整中文精译（保留全部原始数据与措辞）】"
}}"""
    raw, used_model = call_gemini(
        api_key, prompt, expect_json=True, use_search=False, models=GEMINI_TRIAGE_MODELS
    )
    if raw:
        try:
            cleaned = re.sub(r"^```json\s*|\s*```$", "", raw.strip())
            res = json.loads(cleaned)
            res["_model"] = used_model
            return res
        except Exception as e:
            print(f"[WARN] Parse triage JSON failed: {e}")
    return {
        "category": "最新动态",
        "impact_score": 7,
        "direction": "待核验",
        "flash_summary": ev["title"],
        "chinese_translation": ev["content"][:1000],
        "_model": "fallback",
    }


def gemini_deep_rubric_audit(ev, triage, api_key):
    """
    阶段二：严格按照表格预设的【达标标尺与竞品基准】+【排雷红线与披露口径】进行结构化对表核验
    开启 8192 Token 深度思考推理 + Google 实时检索 + ClinicalTrials.gov 官方底稿比对
    """
    stock = ev["stock"]
    ct_context = fetch_clinical_trials_context(stock)
    prompt = f"""请开启深度思考推理，严格对照下方预设的【达标标尺与竞品基准】、【排雷红线与披露口径观察点】以及【ClinicalTrials.gov 官方注册底稿】，对 {stock['ticker']} ({stock['company']}) 的最新消息进行极度严谨、紧凑的对表核验。

================ 事前预设标尺卡 (Pre-set Rubric) ================
1. 【预设达标标尺与竞品基准 (Benchmark)】：
{stock['benchmark']}

2. 【重点排雷红线与披露口径观察点 (Red Flags & Wording Audit)】：
{stock['red_flags']}

3. 【ClinicalTrials.gov 官方注册试验底稿】：
{ct_context}
=================================================================

【本次最新事件 ({triage.get('category')}, 重要度 {triage.get('impact_score')}/10)】：
标题：{ev['title']}
内容详情：
{ev['content']}

⚠️ 严格遵守以下输出铁律：
- 严禁寒暄、严禁自称“我是专家/分析师/经理”，直接输出干货！
- 严禁出现“美股、炒股、买入、卖出、加仓、止盈、止损、暴涨、暴跌”等敏感词，一律采用客观科研与商业评估措辞（如“显著优于基准”、“符合预期但需留意短期分歧与融资窗口”、“核心指标未达标/触发红线”）。
- 总字数严格控制在 350~450 字以内，言简意赅，只包含以下三个固定模块：

### 1. ⚖️ 预设标尺对表结论：【超越标尺 / 符合标尺 / 低于标尺】
- 直接拿本次披露的具体数字（如疗效幅度、p值、半衰期、ADA比例、处方收入或里程碑金额）与上方【预设达标标尺与竞品基准】逐一比对，明确指出哪项指标赢了竞品、哪项指标落后。

### 2. 🔍 披露口径与隐晦风险排雷
- 严格排查官方用词与数据口径：
  ① 人群口径：是全分析集(ITT/mITT)还是仅统计完成者(Completers/Per-protocol)？有无事后亚组数据挖掘(Post-hoc subgroup spin)？
  ② 对照组与绝对值：是否披露安慰剂校准后绝对差值(Placebo-adjusted delta)及基线水平？
  ③ 安全性与耐受性红线：对照上方【重点排雷红线】，核查是否有 3 级以上 SAE、肝酶升高、心血管不良反应或停药率异常？
  ④ 商业与股本条款：若是 Form 4，核查是公开市场主动增持(Code P)还是期权行权/扣税(Code M/F/S)；若是临床利好，评估短期内借机增发稀释(S-3/ATM)的概率。

### 3. 🧭 综合信号评级与观察要点
- **信号定性**：【强正向催化 / 兑现型中性催化 / 存疑待确认 / 负向风险预警】
- **后续关键验证点**：1~2句话点明接下来最需紧盯的后续细节（如完整会议数据披露、监管沟通节点或现金流变化）。"""

    res, used_model = call_gemini(
        api_key,
        prompt,
        expect_json=False,
        use_search=False,
        deep_thinking=True,
        models=GEMINI_DEEP_MODELS,
    )
    return (res or "深度核验暂不可用。"), (used_model or "gemini")


def send_pushplus(token, title, html_body):
    if not token or "请在此粘贴" in token:
        return
    try:
        http_json(
            "https://www.pushplus.plus/send",
            method="POST",
            payload={
                "token": token,
                "title": title,
                "content": html_body,
                "template": "html",
            },
            timeout=20,
        )
    except Exception as e:
        print(f"[WARN] PushPlus send failed: {e}")


def main():
    force_demo = "--force-demo" in sys.argv
    access_token = get_google_access_token()
    config, portfolio, processed_ids = load_sheet_state(access_token)

    api_key = config.get("GEMINI_API_KEY", "").strip()
    push_token = config.get("PUSHPLUS_TOKEN", "").strip()
    threshold = int(float(config.get("IMPACT_THRESHOLD", "7") or 7))

    print(f"[INFO] Loaded {len(portfolio)} active targets from Google Sheet: {[s['ticker'] for s in portfolio]}")
    cik_map = fetch_sec_cik_map()
    all_events = []

    gmail_ev = fetch_gmail_imap_events(config, portfolio, processed_ids)
    all_events.extend(gmail_ev)

    for stock in portfolio:
        cik = cik_map.get(stock["ticker"])
        sec_ev = fetch_sec_edgar_events(stock, cik, max_age_hours=48)
        rss_ev = fetch_media_rss_events(stock, max_age_hours=36)
        for ev in sec_ev + rss_ev:
            if ev["id"] not in processed_ids:
                all_events.append(ev)

    if force_demo and not all_events and portfolio:
        for stock in portfolio:
            rss_any = fetch_media_rss_events(stock, max_age_hours=24 * 14)
            if rss_any:
                sample = rss_any[0]
                sample["id"] = sample["id"] + "_RUBRIC_V2"
                if sample["id"] not in processed_ids:
                    all_events.append(sample)
                    break

    for ev in all_events[:5]:
        stock = ev["stock"]
        triage = gemini_triage_and_translate(ev, api_key)
        score = int(triage.get("impact_score", 6))
        category = triage.get("category", "最新动态")
        direction = triage.get("direction", "中性")
        flash_summary = triage.get("flash_summary", ev["title"])
        cn_translation = triage.get("chinese_translation", "")

        if score <= 3:
            deep_text = "常规行政事项（重要度≤3，已静默归档）"
        else:
            badge = "📋【重点情报】" if score >= threshold else "📩【动态快讯】"
            flash_title = f"{badge} {stock['ticker']} | {category} ({score}/10)"
            flash_html = f"""
            <div style="font-family:sans-serif;line-height:1.6;">
              <h3 style="margin:0 0 8px 0;color:{'#d93025' if score >= threshold else '#1a73e8'};">{flash_title}</h3>
              <p style="margin:3px 0;font-size:13px;"><b>主体：</b>{stock['ticker']} ({stock['company']}) ｜ <b>方向：</b>{direction} ｜ <b>来源：</b>{ev['source']}</p>
              <hr style="border:none;border-top:1px solid #eee;margin:8px 0;"/>
              <p style="margin:6px 0;font-size:14px;"><b>⚡ 一句话核心要点：</b><br/>{flash_summary}</p>
              <div style="background:#f8f9fa;padding:10px;border-left:3px solid #1a73e8;margin:8px 0;font-size:13px;white-space:pre-wrap;"><b>📄 官方通告/报道原文精准中译：</b>\n{cn_translation}</div>
              <p style="margin:6px 0;font-size:12px;color:#666;">英文原标题：{ev['title']}<br/><a href="{ev['url']}">👉 查看原始英文出处</a></p>
              {'<p style="color:#d93025;font-size:12px;">⏳ <b>重要度 ≥ ' + str(threshold) + '，正在对照预设【竞品标尺与排雷红线】生成深度核验报告...</b></p>' if score >= threshold else ''}
            </div>
            """
            send_pushplus(push_token, flash_title, flash_html)
            print(f"[INFO] Sent Stage 1 Flash + CN Translation for {stock['ticker']} (score={score})")

            deep_text = "未达深度核验阈值（仅推送快讯与原文中译）"
            if score >= threshold:
                time.sleep(2)
                deep_text, deep_model = gemini_deep_rubric_audit(ev, triage, api_key)
                deep_title = f"🔬【标尺核验】{stock['ticker']} 基准比对与口径排雷"
                deep_html = f"""
                <div style="font-family:sans-serif;line-height:1.6;">
                  <h3 style="margin:0 0 6px 0;color:#188038;">{deep_title}</h3>
                  <p style="margin:3px 0;font-size:12px;color:#555;">
                    <b>主体：</b>{stock['ticker']} ｜ <b>引擎：</b>{deep_model} + 预设标尺库
                  </p>
                  <hr style="border:none;border-top:1px solid #ddd;margin:8px 0;"/>
                  <div style="white-space:pre-wrap;font-size:13.5px;">{deep_text}</div>
                  <hr style="border:none;border-top:1px solid #eee;margin:8px 0;"/>
                  <p style="font-size:12px;color:#666;"><a href="{ev['url']}">🔗 查看英文原文出处</a> ｜ <a href="https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit">📊 打开云端预设标尺表</a></p>
                </div>
                """
                send_pushplus(push_token, deep_title, deep_html)
                print(f"[INFO] Sent Stage 2 Rubric Audit for {stock['ticker']} using {deep_model}")

        bj_time = (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=8)
        ).strftime("%Y-%m-%d %H:%M:%S")
        append_sheet_log(
            access_token,
            [
                bj_time,
                stock["ticker"],
                ev["source"],
                category,
                score,
                f"【要点】{flash_summary}\n【原文中译】{cn_translation}",
                deep_text,
                f"{ev['title']} ({ev['url']})",
                ev["id"],
            ],
        )
        processed_ids.add(ev["id"])


if __name__ == "__main__":
    main()
