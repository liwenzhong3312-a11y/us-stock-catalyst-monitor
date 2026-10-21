#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美股持仓与生物医药催化剂 24 小时云端自动盯盘引擎
直接联动 Google Sheet (1pIKfEy9UNPs32W5gBFJu3N5doqU-SKCvIBDjX-TMwGQ)：
1. 自动从 Google Sheet 读取最新持仓池 (Portfolio)、系统配置 (Config) 与已推送历史 (Logs)
2. 渠道 A：直接对接美国证监会 SEC EDGAR 官方实时接口 (data.sec.gov)，监控持仓股 8-K (重大事件/临床/并购)、Form 4 (内部人买卖)、SC 13D/13G (机构举牌)
3. 渠道 B：自动巡检三大一手公关发稿社 (GlobeNewswire / PR Newswire / BusinessWire) 与主流医药财经媒体 (STAT News / FierceBiotech / BioPharma Dive / Endpoints / Reuters)
4. 阶段一：调用 Gemini 3.6 / 3.5 Flash 秒级分诊打分 (1-10分) -> 微信 PushPlus 推送【第一阶段：快讯通知】
5. 阶段二：若评分 >= 阈值 (默认 7 分)，调用 Gemini + Google Search 实时联网搜索，结合持仓成本生成【第二阶段：深度研报与交易建议】二次推送到微信
6. 将快讯、研报结论与唯一消息 ID 自动写回 Google Sheet 的 Logs 表归档防重
"""

import base64
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
from email.utils import parsedate_to_datetime

SPREADSHEET_ID = os.environ.get(
    "SPREADSHEET_ID", "1pIKfEy9UNPs32W5gBFJu3N5doqU-SKCvIBDjX-TMwGQ"
)
SEC_USER_AGENT = "AntigravityStockMonitor/1.0 (neo664939@gmail.com)"
GEMINI_TRIAGE_MODELS = [
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite-preview",
]

GEMINI_DEEP_MODELS = [
    "gemini-4-argon",
    "gemini-3.1-pro-preview",
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
    client_id = cred["client_id"]
    client_secret = cred["client_secret"]
    refresh_token = cred["token"]["refresh_token"]

    form_data = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
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
        token_resp = json.loads(resp.read().decode("utf-8"))
    return token_resp["access_token"]


def load_sheet_state(access_token):
    ranges = ["Portfolio!A2:J50", "Config!A2:B20", "Logs!I2:I500"]
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
        enabled = str(row[7]).strip().upper() if len(row) > 7 and row[7] else "YES"
        if enabled == "NO":
            continue
        company = str(row[1]).strip() if len(row) > 1 else ticker
        exchange = str(row[2]).strip() if len(row) > 2 else ""
        raw_kw = str(row[3]) if len(row) > 3 else ""
        kw_list = [k.strip() for k in re.split(r"[,，]", raw_kw) if k.strip()]
        keywords = list(dict.fromkeys([ticker, company] + kw_list))
        catalysts = str(row[4]).strip() if len(row) > 4 else ""
        avg_cost = str(row[5]).strip() if len(row) > 5 and str(row[5]).strip() else "未填写"
        pos_note = str(row[6]).strip() if len(row) > 6 and str(row[6]).strip() else "持仓观察"
        portfolio.append(
            {
                "ticker": ticker,
                "company": company,
                "exchange": exchange,
                "keywords": keywords,
                "catalysts": catalysts,
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
    except Exception as e:
        print(f"[WARN] SEC CIK map error: {e}")
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
                    age_hours = (now_utc - dt).total_seconds() / 3600.0
                    if age_hours > max_age_hours:
                        continue
                except Exception:
                    pass

            cik_int = str(int(cik))
            doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no_hyphen}/{doc}"
            items_desc = str(items_list[i]) if i < len(items_list) else ""
            event_id = f"SEC_{stock['ticker']}_{acc}"

            form_label = {
                "8-K": "SEC 8-K 重大事件公告",
                "8-K/A": "SEC 8-K/A 重大事件补充公告",
                "4": "SEC Form 4 内部人(高管/董事)交易申报",
                "SC 13D": "SEC SC 13D 机构/大股东举牌申报",
                "SC 13G": "SEC SC 13G 机构持股超过5%申报",
            }.get(form, f"SEC {form} 申报")

            events.append(
                {
                    "id": event_id,
                    "stock": stock,
                    "source": f"SEC EDGAR 官方直连 ({form})",
                    "title": f"[{form_label}] {stock['company']} (${stock['ticker']}) 提交 {form} 文件 (日期: {fdate})",
                    "content": (
                        f"股票代码: ${stock['ticker']} ({stock['company']})\n"
                        f"SEC 申报类型: {form_label}\n"
                        f"申报时间: {accept_str or fdate}\n"
                        f"涉及条款 (Items): {items_desc}\n"
                        f"SEC 官方原文链接: {doc_url}\n"
                        f"关注催化剂背景: {stock['catalysts']}"
                    ),
                    "url": doc_url,
                }
            )
    except Exception as e:
        print(f"[WARN] SEC filing check failed for {stock['ticker']}: {e}")
    return events


def fetch_media_rss_events(stock, max_age_hours=24):
    events = []
    company_short = stock["company"].split(",")[0].split("Inc")[0].split("Corp")[0].strip()
    # 将股票代码、公司名称、以及前3个核心管线药名全部纳入一级并列检索词，防止媒体只写公司名或药名而漏网
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
            publisher = source_el.text.strip() if source_el is not None and source_el.text else "主流媒体/发稿社"
            raw_desc = item.findtext("description") or ""
            clean_desc = re.sub(r"<[^>]+>", " ", html.unescape(raw_desc)).strip()

            if pub_date:
                try:
                    dt = parsedate_to_datetime(pub_date)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=datetime.timezone.utc)
                    age_hours = (now_utc - dt).total_seconds() / 3600.0
                    if age_hours > max_age_hours:
                        continue
                except Exception:
                    pass

            # 确保标题或摘要确实包含股票代码或核心关键词
            combined = f"{title} {clean_desc}".lower()
            if not any(kw.lower() in combined for kw in stock["keywords"] if len(kw) >= 3):
                continue

            short_hash = hashlib.sha1(guid.encode("utf-8")).hexdigest()[:16]
            event_id = f"RSS_{stock['ticker']}_{short_hash}"
            events.append(
                {
                    "id": event_id,
                    "stock": stock,
                    "source": f"一手公关稿/主流媒体 ({publisher})",
                    "title": title,
                    "content": (
                        f"发布时间: {pub_date}\n"
                        f"来源媒体: {publisher}\n"
                        f"新闻标题: {title}\n"
                        f"新闻摘要: {clean_desc}"
                    ),
                    "url": link,
                }
            )
    except Exception as e:
        print(f"[WARN] Media RSS failed for {stock['ticker']}: {e}")
    return events


def call_gemini(api_key, prompt, expect_json=False, use_search=False, models=None):
    model_list = models or (GEMINI_TRIAGE_MODELS if expect_json else GEMINI_DEEP_MODELS)
    for model in model_list:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            f"?key={urllib.parse.quote(api_key)}"
        )
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1 if expect_json else 0.2},
        }
        if expect_json:
            payload["generationConfig"]["responseMimeType"] = "application/json"
        if use_search:
            payload["tools"] = [{"google_search": {}}]

        try:
            resp = http_json(url, method="POST", payload=payload, timeout=45)
            candidates = resp.get("candidates", [])
            if candidates and "content" in candidates[0]:
                parts = candidates[0]["content"].get("parts", [])
                text = "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()
                if text:
                    return text, model
        except urllib.error.HTTPError as e:
            print(f"[WARN] Gemini model {model} HTTP {e.code} (use_search={use_search})")
            if use_search:
                try:
                    payload.pop("tools", None)
                    resp = http_json(url, method="POST", payload=payload, timeout=40)
                    candidates = resp.get("candidates", [])
                    if candidates and "content" in candidates[0]:
                        parts = candidates[0]["content"].get("parts", [])
                        text = "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()
                        if text:
                            return text, model
                except Exception:
                    pass
        except Exception as e:
            print(f"[WARN] Gemini model {model} error: {e}")
    return None, None


def gemini_triage(ev, api_key):
    stock = ev["stock"]
    prompt = f"""你是一位资深华尔街美股生物医药（Biotech）与事件驱动对冲基金分析师。
请对以下关于美股 ${stock['ticker']} ({stock['company']}) 的最新消息进行极速分诊评估。
该公司核心管线/关键词：{', '.join(stock['keywords'])}
重点关注的催化剂：{stock['catalysts']}

【消息来源】：{ev['source']}
【消息标题】：{ev['title']}
【消息内容】：
{ev['content']}

请严格按以下 JSON 格式返回（只输出纯 JSON）：
{{
  "category": "从以下选一：临床数据 / 并购与BD合作 / 内部人交易 / FDA监管与产品上市 / 催化剂预告 / 财报与商业化 / 主流媒体报道 / 常规噪音",
  "impact_score": 1到10的整数（1-3=无实质影响或例行卖股交税噪音；4-6=一般行业报道/普通会议邀请/常规进展；7-8=重要管线进展/R&D研发日或明确临床读数预告/内部人真金白银主动买入/大行重要评级调整/机构5%举牌；9-10=重磅临床顶线数据Readout/FDA获批或CRL/并购收购/重磅BD交易）,
  "sentiment": "重大利好 / 偏多 / 中性 / 偏空 / 重大利空",
  "flash_summary": "用简体中文写出80字以内的核心快讯摘要，直击关键结论与数字"
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
        "sentiment": "待定",
        "flash_summary": ev["title"],
        "_model": "rule-fallback",
    }


def gemini_deep_analysis(ev, triage, api_key):
    stock = ev["stock"]
    prompt = f"""你是一位管理美股生物医药（Biotech）专户的资深基金经理。
投资人目前持仓/关注标的：${stock['ticker']} ({stock['company']})
- 投资人记录的持仓均价：{stock['avg_cost']}
- 仓位备注：{stock['pos_note']}
- 该股核心看点与催化剂：{stock['catalysts']}

刚刚收到一条影响评分为 {triage.get('impact_score')}/10 的【{triage.get('category')}】情报：
【来源】：{ev['source']}
【标题】：{ev['title']}
【详情】：
{ev['content']}

请你结合实时检索与行业背景，用简体中文输出一份简明犀利、直接指导实战的手机端深度分析报告（控制在 450 字以内，分段清晰）：

1. 🎯 **核心结论与预期差定性**：一句话定性该消息是“超预期利好”、“符合预期（谨防 Sell the news）”还是“不及预期/潜在风险”？
2. 🔬 **基本面与催化剂拆解**：结合公司核心管线（{', '.join(stock['keywords'][:5])}）分析本次事件对股价的核心驱动逻辑或潜在风险点。
3. 💡 **实战交易与仓位建议**：针对小资金个人投资者（当前均价：{stock['avg_cost']}，定位：{stock['pos_note']}），给出明确的盘前/盘中应对参考（如分批止盈、持有观察或风控止损位）。"""
    res, used_model = call_gemini(
        api_key, prompt, expect_json=False, use_search=True, models=GEMINI_DEEP_MODELS
    )
    return (res or "深度分析生成失败，请稍后重试。"), (used_model or "gemini")


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
                            "source": f"Gmail 官方订阅邮件 ({sender[:40]})",
                            "title": subject,
                            "content": f"发件人: {sender}\n邮件标题: {subject}\n邮件内容节选:\n{body_text[:3500]}",
                            "url": "https://mail.google.com/mail/u/0/#inbox",
                        }
                    )
                    break
        mail.logout()
    except Exception as e:
        print(f"[WARN] Gmail IMAP check error: {e}")
    return events


def main():
    force_demo = "--force-demo" in sys.argv
    access_token = get_google_access_token()
    config, portfolio, processed_ids = load_sheet_state(access_token)

    api_key = config.get("GEMINI_API_KEY", "").strip()
    push_token = config.get("PUSHPLUS_TOKEN", "").strip()
    threshold = int(float(config.get("IMPACT_THRESHOLD", "7") or 7))

    print(f"[INFO] Loaded {len(portfolio)} active stocks from Google Sheet: {[s['ticker'] for s in portfolio]}")
    print(f"[INFO] Loaded {len(processed_ids)} historical log IDs (threshold={threshold})")

    cik_map = fetch_sec_cik_map()
    all_events = []

    # 渠道 0：若配置了 Gmail 应用专用密码，直接读取 neo664939@gmail.com 收到的公司官方 IR 邮件 (SEC Filing / Event / News)
    gmail_ev = fetch_gmail_imap_events(config, portfolio, processed_ids)
    all_events.extend(gmail_ev)

    for stock in portfolio:
        cik = cik_map.get(stock["ticker"])
        sec_ev = fetch_sec_edgar_events(stock, cik, max_age_hours=48)
        rss_ev = fetch_media_rss_events(stock, max_age_hours=36)
        for ev in sec_ev + rss_ev:
            if ev["id"] not in processed_ids:
                all_events.append(ev)

    print(f"[INFO] Found {len(all_events)} new unprocessed events across SEC EDGAR & Mainstream Media.")

    # 如果是首次部署验证 (--force-demo) 且当前无最新突发未读事件，抓取最新一条真实新闻或生成一条巡检报告以验证端到端全链路
    if force_demo and not all_events and portfolio:
        for stock in portfolio:
            rss_any = fetch_media_rss_events(stock, max_age_hours=24 * 14)
            if rss_any:
                sample = rss_any[0]
                sample["id"] = sample["id"] + "_CLOUD_LIVE"
                if sample["id"] not in processed_ids:
                    all_events.append(sample)
                    break

    for ev in all_events[:5]:
        stock = ev["stock"]
        triage = gemini_triage(ev, api_key)
        score = int(triage.get("impact_score", 6))
        category = triage.get("category", "最新动态")
        sentiment = triage.get("sentiment", "中性")
        flash_summary = triage.get("flash_summary", ev["title"])

        if score <= 3:
            # 1-3分属纯行政噪音，仅静默归档到表格 Logs，不弹窗打扰手机
            deep_text = "常规低影响噪音（评分≤3，已静默归档不弹窗）"
        else:
            badge = "🚨【重大催化剂快讯】" if score >= threshold else "📩【持仓动态快讯】"
            flash_title = f"{badge} ${stock['ticker']} | {category} (评分:{score}/10)"
            flash_html = f"""
            <div style="font-family:sans-serif;line-height:1.6;">
              <h3 style="margin:0 0 8px 0;color:{'#d93025' if score >= threshold else '#1a73e8'};">{flash_title}</h3>
              <p style="margin:4px 0;"><b>标的：</b>${stock['ticker']} ({stock['company']})</p>
              <p style="margin:4px 0;"><b>情绪倾向：</b>{sentiment} ｜ <b>影响评分：</b><b>{score} / 10</b></p>
              <p style="margin:4px 0;"><b>信源：</b>{ev['source']}</p>
              <hr style="border:none;border-top:1px solid #eee;margin:10px 0;"/>
              <p style="margin:6px 0;font-size:15px;"><b>⚡ Gemini 快讯摘要：</b><br/>{flash_summary}</p>
              <p style="margin:8px 0;font-size:12px;color:#666;">原文标题：{ev['title']}<br/><a href="{ev['url']}">👉 点击查看原始文件/报道链接</a></p>
              {'<p style="color:#d93025;font-size:13px;">⏳ <b>达到重大催化剂阈值（≥' + str(threshold) + '分），正在调用 Gemini 旗舰模型生成深度投研与交易建议，约10秒后二次推送...</b></p>' if score >= threshold else ''}
            </div>
            """
            send_pushplus(push_token, flash_title, flash_html)
            print(f"[INFO] Sent Stage 1 Flash Alert for {stock['ticker']} (score={score})")

            deep_text = "未达深度分析阈值（4-6分仅推送第一阶段快讯）"
            if score >= threshold:
                time.sleep(2)
                deep_text, deep_model = gemini_deep_analysis(ev, triage, api_key)
                deep_title = f"🧠【Gemini 深度研报】${stock['ticker']} 结论与交易建议"
                deep_html = f"""
                <div style="font-family:sans-serif;line-height:1.65;">
                  <h3 style="margin:0 0 8px 0;color:#188038;">{deep_title}</h3>
                  <p style="margin:4px 0;font-size:13px;color:#555;">
                    <b>关联持仓：</b>${stock['ticker']} ｜ <b>记录均价：</b>{stock['avg_cost']} ｜ <b>定位：</b>{stock['pos_note']} ｜ <b>模型：</b>{deep_model}
                  </p>
                  <hr style="border:none;border-top:1px solid #ddd;margin:10px 0;"/>
                  <div style="white-space:pre-wrap;font-size:14px;">{deep_text}</div>
                  <hr style="border:none;border-top:1px solid #eee;margin:10px 0;"/>
                  <p style="font-size:12px;color:#666;"><a href="{ev['url']}">🔗 查看一手消息原文</a> ｜ <a href="https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit">📊 打开云端持仓监控表</a></p>
                </div>
                """
                send_pushplus(push_token, deep_title, deep_html)
                print(f"[INFO] Sent Stage 2 Deep Analysis Alert for {stock['ticker']} using {deep_model}")

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
                flash_summary,
                deep_text,
                f"{ev['title']} ({ev['url']})",
                ev["id"],
            ],
        )
        processed_ids.add(ev["id"])


if __name__ == "__main__":
    main()
