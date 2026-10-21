#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生物医药与持仓标的 24 小时云端情报引擎 (纯净事实翻译 + 深度解读双阶段版)
核心工作流：
1. 第一阶段【基础事实推送】：
   - 英文原标题
   - 中文翻译标题
   - 中文翻译正文（自动剥离邮件广告、推荐阅读与免责声明页脚，100%还原真实正文与数据）
2. 第二阶段【深度解读推送】（当事件重要度 >= 7 分时触发）：
   - 开启 8192 Token 深度思考推理 + 对照 ClinicalTrials.gov 官方临床试验注册底稿
   - 严格从公告原文提取【核心数据与官方预告的下一阶段时间节点】
   - 结合【竞争格局横向对比】、【官方披露用词口径审视】与【隐晦风险挖掘】输出专业解读
3. 所有历史抓取的公告、邮件、原文翻译与解读自动永久累积在 Google Sheet 的 Logs 标签页中
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
SEC_USER_AGENT = "AntigravityBiotechMonitor/3.0 (neo664939@gmail.com)"

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


def clean_boilerplate_text(text):
    """剥离邮件或网页底部的广告推荐、免责声明与退订页脚"""
    if not text:
        return ""
    cutoff_patterns = [
        r"(?i)You may also like:",
        r"(?i)You are receiving this email because",
        r"(?i)Past performance is no guarantee of future results",
        r"(?i)Manage Alerts\s*\|\s*Top News",
        r"(?i)Download the Seeking Alpha app",
        r"(?i)To unsubscribe from these emails",
        r"(?i)Forward-Looking Statements\s*This press release contains",
    ]
    cleaned = text
    for pat in cutoff_patterns:
        m = re.search(pat, cleaned)
        if m and m.start() > 80:
            cleaned = cleaned[: m.start()]
    cleaned = re.sub(r"(?i)Read Now\s*»?", "", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def fetch_webpage_plain_text(url, is_sec=False, max_chars=6000):
    """抓取公告或新闻原文正文"""
    if not url or "news.google.com/rss/articles" in url:
        return ""
    try:
        headers = {
            "User-Agent": SEC_USER_AGENT
            if is_sec
            else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        raw = re.sub(r"(?is)<(script|style|head|nav|footer)[^>]*>.*?</\1>", " ", raw)
        text = re.sub(r"<[^>]+>", "\n", html.unescape(raw))
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
        return clean_boilerplate_text(text)[:max_chars]
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
    ranges = ["Portfolio!A2:H50", "Config!A2:B20", "Logs!I2:I500"]
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
        enabled = str(row[6]).strip().upper() if len(row) > 6 and row[6] else "YES"
        if enabled == "NO":
            continue

        company = str(row[1]).strip() if len(row) > 1 else ticker
        exchange = str(row[2]).strip() if len(row) > 2 else ""
        raw_kw = str(row[3]) if len(row) > 3 else ""
        kw_list = [k.strip() for k in re.split(r"[,，]", raw_kw) if k.strip()]
        company_clean = company.split(",")[0].strip()
        keywords = list(dict.fromkeys([company_clean] + kw_list))
        avg_cost = str(row[4]).strip() if len(row) > 4 and str(row[4]).strip() else "未设置"
        pos_note = str(row[5]).strip() if len(row) > 5 and str(row[5]).strip() else "核心跟踪"

        portfolio.append(
            {
                "ticker": ticker,
                "company": company,
                "company_clean": company_clean,
                "exchange": exchange,
                "keywords": keywords,
                "avg_cost": avg_cost,
                "pos_note": pos_note,
            }
        )

    processed_ids = set()
    for row in log_id_rows:
        if row and str(row[0]).strip():
            processed_ids.add(str(row[0]).strip())

    return config, portfolio, processed_ids


def is_strict_match_for_stock(text, stock):
    """严格匹配：必须命中公司名称、专属管线药名、或带边界的股票代码，杜绝同名公司误判"""
    if not text:
        return False
    # 1. 匹配 ($TICKER) 或 (NASDAQ: TICKER) 或 $TICKER
    ticker_pat = rf"(\$\b{re.escape(stock['ticker'])}\b|\(\s*{re.escape(stock['ticker'])}\s*\)|\b(NASDAQ|NYSE)\s*:\s*{re.escape(stock['ticker'])}\b)"
    if re.search(ticker_pat, text, re.IGNORECASE):
        return True
    # 2. 匹配公司名或专属管线药名（单词边界）
    for kw in stock["keywords"]:
        if len(kw) >= 4 and re.search(rf"\b{re.escape(kw)}\b", text, re.IGNORECASE):
            return True
    return False


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
    company_short = stock["company_clean"].split("Inc")[0].split("Corp")[0].strip()
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
                f"- [{nct_id}] ({phases}, 状态:{status}, 样本量:{enrollment}人) {title}\n"
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

            doc_text = fetch_webpage_plain_text(doc_url, is_sec=True, max_chars=5500)

            form_label = {
                "8-K": "8-K 重大事项公告",
                "8-K/A": "8-K/A 补充事项公告",
                "4": "Form 4 内部人持股变动表",
                "SC 13D": "SC 13D 重要股东持股变动",
                "SC 13G": "SC 13G 机构持股超5%备案",
            }.get(form, f"{form} 备案文件")

            events.append(
                {
                    "id": event_id,
                    "stock": stock,
                    "source": f"SEC 官方文件 ({form})",
                    "title": f"{stock['company']} ({stock['ticker']}) - SEC Form {form} ({fdate})",
                    "body_en": doc_text or f"SEC Form {form} (Items: {items_desc}). Please see original filing at {doc_url}",
                    "url": doc_url,
                }
            )
    except Exception as e:
        print(f"[WARN] SEC check failed for {stock['ticker']}: {e}")
    return events


def fetch_yahoo_direct_news(stock, max_age_hours=24):
    """直接按精确股票代码抓取 Yahoo Finance 官方聚合的一手公关通稿 (GlobeNewswire/PRNewswire/BusinessWire 等)"""
    events = []
    rss_url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={urllib.parse.quote(stock['ticker'])}&region=US&lang=en-US"
    try:
        req = urllib.request.Request(
            rss_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            xml_bytes = resp.read()
        root = ET.fromstring(xml_bytes)
        channel = root.find("channel")
        if channel is None:
            return events

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        for item in channel.findall("item")[:5]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            guid = (item.findtext("guid") or link).strip()
            pub_date = (item.findtext("pubDate") or "").strip()
            desc = clean_boilerplate_text(re.sub(r"<[^>]+>", " ", html.unescape(item.findtext("description") or "")).strip())

            if pub_date:
                try:
                    dt = parsedate_to_datetime(pub_date)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=datetime.timezone.utc)
                    if (now_utc - dt).total_seconds() / 3600.0 > max_age_hours:
                        continue
                except Exception:
                    pass

            if not is_strict_match_for_stock(f"{title} {desc}", stock):
                continue

            full_text = fetch_webpage_plain_text(link, is_sec=False, max_chars=5500)
            body_en = full_text if len(full_text) > len(desc) else desc

            short_hash = hashlib.sha1(guid.encode("utf-8")).hexdigest()[:16]
            events.append(
                {
                    "id": f"YF_{stock['ticker']}_{short_hash}",
                    "stock": stock,
                    "source": "一手新闻通稿/财经媒体",
                    "title": title,
                    "body_en": body_en or title,
                    "url": link,
                }
            )
    except Exception as e:
        print(f"[WARN] Yahoo RSS failed for {stock['ticker']}: {e}")
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

            # 跳过订阅确认信本身
            if re.search(r"(?i)(confirm your subscription|verify your email|welcome to .* investor)", subject):
                continue

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
                            body_text = re.sub(r"<[^>]+>", "\n", html.unescape(raw_html))

            cleaned_body = clean_boilerplate_text(body_text)
            combined = f"{subject}\n{cleaned_body}"

            for stock in portfolio:
                if is_strict_match_for_stock(combined, stock):
                    events.append(
                        {
                            "id": ev_id,
                            "stock": stock,
                            "source": f"Gmail 订阅邮件 ({sender[:35]})",
                            "title": subject,
                            "body_en": cleaned_body[:5000] if len(cleaned_body) > 30 else subject,
                            "url": "https://mail.google.com/mail/u/0/#inbox",
                        }
                    )
                    break
        mail.logout()
    except Exception as e:
        print(f"[WARN] Gmail IMAP check error: {e}")
    return events


_GEMINI_KEY_IDX = 0


def parse_api_keys(raw_key_str):
    if not raw_key_str:
        return []
    keys = [
        k.strip()
        for k in re.split(r"[,，;\n\r\s]+", str(raw_key_str))
        if k.strip() and not k.strip().startswith("请")
    ]
    return list(dict.fromkeys(keys))


def call_gemini(api_key, prompt, expect_json=False, use_search=False, deep_thinking=False, models=None):
    global _GEMINI_KEY_IDX
    keys = parse_api_keys(api_key) if isinstance(api_key, str) else list(api_key)
    if not keys:
        return None, None
    model_list = models or (GEMINI_TRIAGE_MODELS if expect_json else GEMINI_DEEP_MODELS)
    for model in model_list:
        for attempt in range(len(keys)):
            cur_key = keys[(_GEMINI_KEY_IDX + attempt) % len(keys)]
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                f"?key={urllib.parse.quote(cur_key)}"
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
                _GEMINI_KEY_IDX = (_GEMINI_KEY_IDX + attempt + 1) % len(keys)
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
                print(f"[WARN] Gemini key#{attempt+1} model {model} HTTP {e.code}: {e}")
                if e.code == 429:
                    time.sleep(1.5)
                    continue
            except Exception as e:
                print(f"[WARN] Gemini key#{attempt+1} model {model} error: {e}")
    return None, None


def gemini_translate_facts(ev, api_key):
    """
    阶段一：纯粹的【基础事实提取与中译】
    严格提供：1. 英文原标题；2. 中文翻译标题；3. 中文翻译正文（剔除一切广告/页脚）
    """
    stock = ev["stock"]
    prompt = f"""请对以下关于生物科技公司 {stock['ticker']} ({stock['company']}) 的最新英文资料进行关联核实与精准中文翻译。

【来源】：{ev['source']}
【英文原标题】：{ev['title']}
【英文原始正文】：
{ev['body_en']}

⚠️ 严格要求：
1. 若该资料实际主语不是 {stock['company']} ({stock['ticker']}) 或其研发管线（例如误抓了其他同名公司），请将 `is_relevant` 设为 false，`impact_score` 设为 1。
2. 全文严禁出现“美股、炒股、买入、卖出、加仓、减仓、止盈、止损、暴涨、暴跌、基金经理、交易建议”等敏感字眼。
3. `title_cn`：将【英文原标题】精准直译为简体中文。
4. `body_cn`：将【英文原始正文】中的实质内容完整、准确、逐段翻译为简体中文（100%保留所有实验数据、p值、百分比、剂量、样本量N、金额与日期；彻底忽略并剔除任何残余的网站广告、其他文章推荐、免责声明或取消订阅提示；如果原始正文仅仅是把标题重复了一遍而没有实质正文段落，请直接写“（该快讯仅含标题，尚无详细正文段落，请点击下方链接查看）”）。

请严格按以下 JSON 格式返回（只输出纯 JSON）：
{{
  "is_relevant": true,
  "category": "从以下选一：临床数据 / 合作与并购 / 内部人变动 / 监管与上市 / 事项预告 / 经营业绩 / 行业报道 / 常规行政",
  "impact_score": 1到10的整数（1-3=无关或例行行政事项；4-6=一般行业报道或常规动态；7-8=重要管线进展/会议数据预告/管理层主动增持/合作里程碑；9-10=关键临床II/III期顶线数据/监管获批或CRL/收购并购）,
  "title_cn": "中文翻译标题",
  "body_cn": "中文翻译正文（分段清晰，保留全部数据与关键事实）"
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
            print(f"[WARN] Parse JSON failed: {e}")
    return {
        "is_relevant": True,
        "category": "最新动态",
        "impact_score": 7,
        "title_cn": ev["title"],
        "body_cn": ev["body_en"][:1500],
        "_model": "fallback",
    }


def gemini_deep_analysis(ev, facts, api_key, vault_notes=None):
    """
    阶段二：基于真实公告原文、ClinicalTrials.gov 官方底稿、以及 Google Drive 投研库（内部 Deck/Transcript 口径 vs 外部投行研报预期共识 vs 自建网站催化剂底稿）的深度解读
    """
    stock = ev["stock"]
    ct_context = fetch_clinical_trials_context(stock)
    vault_block = ""
    if vault_notes:
        joined_notes = "\n\n".join(vault_notes[-6:])
        vault_block = f"""
【Google Drive 投研库已归档的内外口径与网站催化剂底稿（内部官方 Deck/Transcript vs 外部投行预期共识）】：
{joined_notes}
"""
    prompt = f"""请开启深度思考推理，基于以下 {stock['ticker']} ({stock['company']}) 刚刚发布的真实通告内容、ClinicalTrials.gov 官方临床试验注册底稿{ '以及投研库归档的内外口径底稿' if vault_block else '' }，进行客观、深度的生物医药专业拆解。

【ClinicalTrials.gov 官方注册试验底稿】：
{ct_context}
{vault_block}
【本次通告英文原标题】：{ev['title']}
【本次通告中文翻译标题】：{facts.get('title_cn')}
【本次通告原文内容】：
{ev['body_en']}

⚠️ 严格遵守以下专业分析准则与输出铁律：
- 严禁寒暄、严禁自称“专家/分析师/经理”，严禁出现“美股、炒股、买入、卖出、加仓、止盈、止损、暴涨、暴跌”等敏感词。
- 必须严格依据通告原文事实进行分析，原文未披露的数据请明确指出“原文未披露”，绝不臆测。
- 总字数控制在 350~480 字以内，只输出以下三个模块：

### 1. 🎯 核心事实与官方披露的后续催化剂节点
- **核心结论**：提炼本次通告最关键的数据指标（如主要/次要终点结果、p值、样本量、交易金额或持股变动性质）。
- **官方预告的下一催化剂**：严格从通告原文中提取公司官方明确提到的下一步计划与时间窗口（例如：何时在医学大会公布完整数据、何时向 FDA 提交 NDA/IND、何时完成入组或启动下一阶段试验；若原文未提及具体时间点，则写“通告原文未披露具体下一时间节点”）。

### 2. 🔍 内外口径比对、用词审视与隐晦风险挖掘
- **内外口径对照与预期差**：若上方【投研库底稿】中包含公司历史 Deck/电话会承诺或外部投行预期共识（Beat/Miss 标准），请明确对比本次结果是否兑现了管理层此前承诺、是否达到外部投行预期基准。
- **措辞与细节审视**：
  • 若为临床进展：审视其统计口径（全人群 ITT 还是仅完成者 Completers？是否只给定性描述如“positive trend/well-tolerated”却隐瞒安慰剂对照组绝对差值、p值或3级以上不良反应/停药率？对照 ClinicalTrials.gov 登记终点看有无偏离？）。
  • 若为内部人变动/财务通告：核查是公开市场主动增持(Code P)还是期权行权/例行扣税(Code M/F/S)；评估临床利好发布后短期内伴随增发融资(S-3/ATM)的潜在稀释风险。

### 3. ⚔️ 竞争格局比对与综合信号定性
- 横向对比同靶点/同适应症主流在研竞品或现行标准疗法（SoC），评估本次结果在行业中的真实身位。
- **综合信号定性**：【强正向信号 / 符合预期中性信号 / 披露不充分待观察 / 负向风险预警】"""

    res, used_model = call_gemini(
        api_key,
        prompt,
        expect_json=False,
        use_search=False,
        deep_thinking=True,
        models=GEMINI_DEEP_MODELS,
    )
    return (res or "深度解读暂不可用。"), (used_model or "gemini")


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
    access_token = get_google_access_token()
    config, portfolio, processed_ids = load_sheet_state(access_token)

    api_key = config.get("GEMINI_API_KEY", "").strip()
    push_token = config.get("PUSHPLUS_TOKEN", "").strip()
    threshold = int(float(config.get("IMPACT_THRESHOLD", "7") or 7))

    print(f"[INFO] Loaded {len(portfolio)} active targets from Google Sheet: {[s['ticker'] for s in portfolio]}")

    # 同步 Google Drive 投研库（内部 Deck/Transcript vs 外部投行研报 vs 网站催化剂底稿）并生成中英对照 PDF
    vault_knowledge = {}
    try:
        from vault_sync import sync_drive_vault
        vault_knowledge = sync_drive_vault(
            access_token, config, portfolio, send_pushplus_fn=send_pushplus
        )
    except Exception as e:
        print(f"[WARN] Drive vault sync skipped or failed: {e}")

    cik_map = fetch_sec_cik_map()
    all_events = []

    gmail_ev = fetch_gmail_imap_events(config, portfolio, processed_ids)
    all_events.extend(gmail_ev)

    for stock in portfolio:
        cik = cik_map.get(stock["ticker"])
        sec_ev = fetch_sec_edgar_events(stock, cik, max_age_hours=48)
        yf_ev = fetch_yahoo_direct_news(stock, max_age_hours=36)
        for ev in sec_ev + yf_ev:
            if ev["id"] not in processed_ids:
                all_events.append(ev)

    for ev in all_events[:5]:
        stock = ev["stock"]
        facts = gemini_translate_facts(ev, api_key)
        if not facts.get("is_relevant", True):
            processed_ids.add(ev["id"])
            continue

        score = int(facts.get("impact_score", 6))
        category = facts.get("category", "最新动态")
        title_cn = facts.get("title_cn", ev["title"])
        body_cn = facts.get("body_cn", "")

        if score <= 3:
            deep_text = "常规低相关事项（重要度≤3，已静默归档）"
        else:
            # 第一条推送：严格只发【基础事实：英文原标题 + 中文翻译标题 + 中文翻译正文】
            badge = "📋【基础事实】"
            fact_push_title = f"{badge} {stock['ticker']} | { title_cn[:28] }"
            fact_html = f"""
            <div style="font-family:sans-serif;line-height:1.65;">
              <p style="margin:0 0 6px 0;font-size:13px;color:#555;">
                <b>标的：</b>{stock['ticker']} ({stock['company']}) ｜ <b>分类：</b>{category} ｜ <b>重要度：</b><b>{score}/10</b>
              </p>
              <div style="background:#f1f3f4;padding:8px 10px;border-radius:4px;margin:6px 0;font-size:13px;">
                <b>【英文原标题】</b><br/>{html.escape(ev['title'])}
              </div>
              <div style="background:#e8f0fe;padding:8px 10px;border-radius:4px;margin:6px 0;font-size:14px;color:#174ea6;">
                <b>【中文翻译标题】</b><br/><b>{html.escape(title_cn)}</b>
              </div>
              <div style="background:#ffffff;padding:10px;border:1px solid #e0e0e0;border-radius:4px;margin:8px 0;font-size:13.5px;white-space:pre-wrap;"><b>【中文翻译正文】</b>\n{html.escape(body_cn)}</div>
              <p style="margin:6px 0;font-size:12px;color:#666;">
                来源：{html.escape(ev['source'])} ｜ <a href="{ev['url']}">👉 点击查看英文原文出处</a>
              </p>
            </div>
            """
            send_pushplus(push_token, fact_push_title, fact_html)
            print(f"[INFO] Sent Step 1 Facts Push for {stock['ticker']} (score={score})")

            deep_text = "未达深度解读阈值（仅推送基础事实与原文翻译）"
            if score >= threshold:
                time.sleep(2)
                stock_vault_notes = vault_knowledge.get(stock["ticker"], [])
                deep_text, deep_model = gemini_deep_analysis(
                    ev, facts, api_key, vault_notes=stock_vault_notes
                )
                vault_badge = " + 投研库内外口径比对" if stock_vault_notes else ""
                deep_title = f"🔬【深度解读】{stock['ticker']} | {category} ({score}/10)"
                deep_html = f"""
                <div style="font-family:sans-serif;line-height:1.65;">
                  <p style="margin:0 0 6px 0;font-size:12px;color:#555;">
                    <b>关联事件：</b>{html.escape(title_cn)}<br/>
                    <b>推理引擎：</b>{deep_model} + ClinicalTrials.gov 官方试验库{vault_badge}
                  </p>
                  <hr style="border:none;border-top:1px solid #ddd;margin:8px 0;"/>
                  <div style="white-space:pre-wrap;font-size:13.5px;">{deep_text}</div>
                  <hr style="border:none;border-top:1px solid #eee;margin:8px 0;"/>
                  <p style="font-size:12px;color:#666;"><a href="{ev['url']}">🔗 查看英文原文</a> ｜ <a href="https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit">📊 打开云端历史归档表 (Logs)</a></p>
                </div>
                """
                send_pushplus(push_token, deep_title, deep_html)
                print(f"[INFO] Sent Step 2 Deep Analysis for {stock['ticker']} using {deep_model}")

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
                f"【英文原标题】{ev['title']}\n【中文翻译标题】{title_cn}\n【中文翻译正文】{body_cn}",
                deep_text,
                f"{ev['title']} ({ev['url']})",
                ev["id"],
            ],
        )
        processed_ids.add(ev["id"])


if __name__ == "__main__":
    main()
