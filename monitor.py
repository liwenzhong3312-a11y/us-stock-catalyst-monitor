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
    "gemini-3.5-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-flash-latest",
    "gemini-3-flash-preview",
]

GEMINI_DEEP_MODELS = [
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-flash-latest",
    "gemini-3-flash-preview",
    "gemini-pro-latest",
]


def http_json(url, method="GET", headers=None, payload=None, timeout=30):
    req_headers = headers.copy() if headers else {}
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json; charset=utf-8")
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if not raw:
                    return {}
                return json.loads(raw.decode("utf-8", errors="replace"))
        except urllib.error.HTTPError:
            raise
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))


# 官方新闻通讯社电头与官方信源域名列表
OFFICIAL_WIRES_PAT = r"(?i)\b(BUSINESS WIRE|GLOBE NEWSWIRE|GlobeNewswire|PR Newswire|PRNewswire|ACCESSWIRE)\b"
OFFICIAL_IR_DOMAINS = [
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
    "veradermics.com",
]

# 专业生物医药行业媒体域名（用于提炼【行业速览】）
BIOTECH_MEDIA_DOMAINS = [
    "biospace.com",
    "endpoints",
    "endpts.com",
    "fiercebiotech.com",
    "fiercepharma.com",
    "statnews.com",
    "genengnews.com",
    "bioworld.com",
    "biopharmadive.com",
    "medcitynews.com",
    "clinicaltrialsarena.com",
    "nature.com",
    "cell.com",
    "biocentury.com",
    "fda.gov",
    "nih.gov",
    "medscape.com",
    "biorxiv.org",
    "medrxiv.org",
    "cbinsights.com",
    "science.org",
    "aaas.org",
    "nejm.org",
    "thelancet.com",
]

# 非生科媒体/通用交易与系统通知发件人（过滤无关广告与通用资讯）
NON_MEDIA_DOMAINS = [
    "google.com",
    "youtube.com",
    "claude.com",
    "anthropic.com",
    "github.com",
    "apple.com",
    "microsoft.com",
    "openai.com",
    "substack.com",
]


def clean_boilerplate_text(text):
    """剥离邮件或网页底部的广告推荐、免责声明与退订页脚，并彻底清除隐藏字符与零宽空格"""
    if not text:
        return ""
    # 彻底清除各种零宽字符、图元空格及隐藏换行字符（如 Seeking Alpha 等邮件中插入的大量 \u034f \u200b \u2007 等）
    cleaned = re.sub(r"[\u034f\u200b\u200c\u200d\u2007\ufeff\xad]+", "", text)
    cutoff_patterns = [
        r"(?i)You may also like:",
        r"(?i)You are receiving this email because",
        r"(?i)Past performance is no guarantee of future results",
        r"(?i)Manage Alerts\s*\|\s*Top News",
        r"(?i)Download the Seeking Alpha app",
        r"(?i)To unsubscribe from these emails",
        r"(?i)Forward-Looking Statements\s*This press release contains",
    ]
    for pat in cutoff_patterns:
        m = re.search(pat, cleaned)
        if m and m.start() > 80:
            cleaned = cleaned[: m.start()]
    cleaned = re.sub(r"(?i)Read Now\s*»?", "", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def is_official_event(ev):
    """
    判断事件是否属于一手官方信源：
    1. SEC EDGAR 官方申报
    2. 官方通讯社公关新闻稿 (Business Wire, GlobeNewswire, PR Newswire, Accesswire)
    3. 官方 IR 邮件订阅 (alert.gcs-web.com, notified.com, q4inc.com, equisolve.com, 或公司官方域名)
    """
    if ev.get("is_official") is not None:
        return bool(ev["is_official"])
    source_lower = (ev.get("source") or "").lower()
    source_tier = (ev.get("source_tier") or "").lower()
    body_en = ev.get("body_en", "")[:2500]

    if "sec edgar" in source_lower or "sec 官方" in source_lower:
        return True
    if any(w in source_lower or w in source_tier for w in ["一手官方", "官方新闻稿", "news release"]):
        return True
    if any(domain in source_lower for domain in OFFICIAL_IR_DOMAINS):
        return True
    if re.search(OFFICIAL_WIRES_PAT, body_en):
        return True
    return False


def is_biotech_media_sender(sender_str):
    """判断发件人是否属于生物科技专业媒体 Newsletter"""
    s = (sender_str or "").lower()
    if any(d in s for d in NON_MEDIA_DOMAINS):
        return False
    return any(d in s for d in BIOTECH_MEDIA_DOMAINS)


def _format_html_tables(html_str):
    """
    将 HTML <table> 解析为严格的二维网格（完整还原 colspan 跨列与 rowspan 跨行合并），
    杜绝因忽略空单元格或跨列属性导致表头与数据列错位（如把 304 BID 错对齐到 302 表头下）。
    """
    def repl_table(t_match):
        table_inner = t_match.group(1)
        tr_list = re.findall(r"(?is)<tr\b[^>]*>(.*?)</tr>", table_inner)
        if not tr_list:
            return "\n"
        # 解析每一行的 (文本, colspan, rowspan)
        parsed_rows = []
        for tr in tr_list:
            cell_matches = re.findall(r"(?is)<t[dh]\b([^>]*)>(.*?)</t[dh]>", tr)
            if not cell_matches:
                continue
            row_cells = []
            for attrs, c_body in cell_matches:
                c_txt = re.sub(r"</?[a-zA-Z!][^>]*>", " ", c_body)
                c_txt = re.sub(r"\s+", " ", c_txt).strip()
                cs_m = re.search(r'(?i)colspan\s*=\s*["\']?(\d+)', attrs)
                rs_m = re.search(r'(?i)rowspan\s*=\s*["\']?(\d+)', attrs)
                cs = max(1, min(int(cs_m.group(1)), 40)) if cs_m else 1
                rs = max(1, min(int(rs_m.group(1)), 40)) if rs_m else 1
                row_cells.append((c_txt, cs, rs))
            if any(txt for txt, _, _ in row_cells):
                parsed_rows.append(row_cells)

        if not parsed_rows:
            return "\n"

        # 展开 colspan 与 rowspan 到二维网格 grid[r][c]
        num_rows = len(parsed_rows)
        grid = {}
        max_col = 0
        for r_idx, row_cells in enumerate(parsed_rows):
            c_idx = 0
            for txt, cs, rs in row_cells:
                while (r_idx, c_idx) in grid:
                    c_idx += 1
                for dr in range(rs):
                    for dc in range(cs):
                        if r_idx + dr < num_rows:
                            grid[(r_idx + dr, c_idx + dc)] = txt
                c_idx += cs
                if c_idx > max_col:
                    max_col = c_idx

        # 剔除完全空白的冗余列，并合并 Workiva/SEC 表格中因 colspan=3 产生的完全相同的相邻重复列向量
        non_empty_cols = [
            c for c in range(max_col)
            if any(grid.get((r, c), "").strip() for r in range(num_rows))
        ]
        valid_cols = []
        prev_vec = None
        for c in non_empty_cols:
            vec = tuple(grid.get((r, c), "").strip() for r in range(num_rows))
            if vec != prev_vec:
                valid_cols.append(c)
                prev_vec = vec
        if not valid_cols:
            return "\n"

        out_lines = [""]
        for r in range(num_rows):
            row_vals = [grid.get((r, c), "").strip() for c in valid_cols]
            if any(row_vals):
                out_lines.append("| " + " | ".join(row_vals) + " |")
        out_lines.append("")
        return "\n".join(out_lines)

    return re.sub(r"(?is)<table\b[^>]*>(.*?)</table>", repl_table, html_str)


def html_to_text(raw):
    """
    HTML 转纯文本。
    双重安全保障：
    1. 正则仅匹配真实 HTML 标签 `</?[a-zA-Z!][^>]*>`（绝不把 `<0.0001` 或 `< 0.05` 当作标签）；
    2. 严格在剥离完所有 HTML 标签之后，最后一步才执行一次 `html.unescape()`。
    """
    if not raw:
        return ""
    s = re.sub(r"(?is)<(script|style|head|noscript)[^>]*>.*?</\1>", " ", raw)
    s = re.sub(r"(?is)<!--.*?-->", " ", s)
    # 剥离 SEC EDGAR SGML 头部标签 (如 <TYPE>EX-99.1 <SEQUENCE>2 <FILENAME>...)
    s = re.sub(r"(?im)^<(TYPE|SEQUENCE|FILENAME|DESCRIPTION)>[^\n]*\n?", "", s)
    # 先把表格行 <tr>...</tr> 格式化为单行 | col1 | col2 |
    s = _format_html_tables(s)
    # 块级元素 -> 换行；其余真实 HTML 标签直接去掉
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</?(p|div|li|ul|ol|h[1-6]|table|section|article|blockquote)\b[^>]*>", "\n", s)
    s = re.sub(r"</?[a-zA-Z!][^>]*>", "", s)
    # 所有 HTML 标签清理完毕后，最后一步才执行 unescape
    s = html.unescape(s).replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    # 清理残余的 SEC Exhibit 文件头行
    lines = s.strip().splitlines()
    while lines and re.match(r"(?i)^(ex-?99\.\d+|exhibit\s*99\.\d+|\d+|[\w\-]+\.htm[l]?)$", lines[0].strip()):
        lines.pop(0)
    return "\n".join(lines).strip()


def fetch_webpage_plain_text(url, is_sec=False, max_chars=25000):
    """抓取公告或新闻原文完整正文（默认保留高达 25,000 字符，完整覆盖整篇新闻稿）"""
    if not url or "news.google.com/rss/articles" in url:
        return ""
    try:
        headers = {
            "User-Agent": SEC_USER_AGENT
            if is_sec
            else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        # 对于 Yahoo Finance 等外部媒体网页，优先提取 <article> 主体，剥离顶部大盘指数导航栏噪音
        if not is_sec:
            art_m = re.search(r"(?is)<article\b[^>]*>(.*?)</article>", raw)
            if art_m and len(art_m.group(1)) > 400:
                raw = art_m.group(1)
        raw = re.sub(r"(?is)<(nav|footer|header)[^>]*>.*?</\1>", " ", raw)
        return clean_boilerplate_text(html_to_text(raw))[:max_chars]
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
    ranges = ["Portfolio!A2:H50", "Config!A2:B20", "Logs!H2:I500"]
    q = "&".join(f"ranges={urllib.parse.quote(r)}" for r in ranges)
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values:batchGet?{q}"
    res = http_json(url, headers={"Authorization": f"Bearer {access_token}"})
    value_ranges = res.get("valueRanges", [])

    portfolio_rows = value_ranges[0].get("values", []) if len(value_ranges) > 0 else []
    config_rows = value_ranges[1].get("values", []) if len(value_ranges) > 1 else []
    log_rows = value_ranges[2].get("values", []) if len(value_ranges) > 2 else []

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
        ir_url = str(row[7]).strip() if len(row) > 7 and str(row[7]).strip() else ""
        if ir_url:
            m_dom = re.search(r"https?://(?:www\.|ir\.|investors?\.)?([a-zA-Z0-9\-]+\.[a-zA-Z]{2,})", ir_url)
            if m_dom:
                root_dom = m_dom.group(1).lower()
                if root_dom not in OFFICIAL_IR_DOMAINS:
                    OFFICIAL_IR_DOMAINS.append(root_dom)

        portfolio.append(
            {
                "ticker": ticker,
                "company": company,
                "company_clean": company_clean,
                "exchange": exchange,
                "keywords": keywords,
                "avg_cost": avg_cost,
                "pos_note": pos_note,
                "ir_url": ir_url,
            }
        )

    processed_ids = set()
    processed_titles = set()
    for row in log_rows:
        if len(row) >= 2 and str(row[1]).strip():
            processed_ids.add(str(row[1]).strip())
        if len(row) >= 1 and str(row[0]).strip():
            raw_h = str(row[0]).strip()
            clean_h = re.sub(r"\(https?://[^\)]+\)", "", raw_h).strip().lower()
            if clean_h:
                processed_titles.add(re.sub(r"[^a-z0-9]+", "", clean_h))

    return config, portfolio, processed_ids, processed_titles


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


SEC_8K_ITEMS_CN = {
    "1.01": "重大协议", "1.02": "终止重大协议", "2.02": "经营业绩", "3.02": "未注册股票发行",
    "5.02": "高管/董事变动", "5.07": "股东会表决", "7.01": "Reg FD 披露", "8.01": "其他重大事项",
    "9.01": "财务报表与附件",
}


def fetch_sec_press_exhibits(cik_int, acc_no_hyphen, max_chars=25000):
    """读取 8-K 的 EX-99.x 附件（公司官方新闻稿全文），返回 (标题, 正文, 链接, 真实美东受理时间dt)"""
    base = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no_hyphen}/"
    items_meta = []
    try:
        idx = http_json(base + "index.json", headers={"User-Agent": SEC_USER_AGENT}, timeout=20)
        items_meta = idx.get("directory", {}).get("item", [])
        names = [it.get("name", "") for it in items_meta]
    except Exception:
        return "", "", "", None

    # 从 index.json 提取 SEC 官方真实美东受理时间 (如 '2026-10-19 09:06:14' EDT)
    real_et_dt = None
    for it in items_meta:
        lm = (it.get("last-modified") or "").strip()
        if lm:
            try:
                naive_dt = datetime.datetime.strptime(lm, "%Y-%m-%d %H:%M:%S")
                # 美东夏令时 (3月-11月初) UTC-4，冬令时 UTC-5（不依赖外部 tzdata 库）
                et_offset = -4 if 3 <= naive_dt.month <= 10 else -5
                real_et_dt = naive_dt.replace(
                    tzinfo=datetime.timezone(datetime.timedelta(hours=et_offset), name="EDT" if et_offset == -4 else "EST")
                )
                break
            except Exception:
                pass

    ex_files = sorted(
        n for n in names
        if re.search(r"(?i)(ex[-_]?99|exhibit[-_]?99|ex99|dex99)", n) and n.lower().endswith((".htm", ".html"))
    )
    if not ex_files:
        return "", "", "", real_et_dt
    texts = []
    for n in ex_files[:3]:
        t = fetch_webpage_plain_text(base + n, is_sec=True, max_chars=max_chars)
        if t:
            texts.append(t)
    if not texts:
        return "", "", "", real_et_dt
    full = "\n\n".join(texts)[:max_chars]
    # 标题提取：跳过 EX-99.1 / 文件名 / 页码等表头，合并被 HTML 换行切碎的多行主标题
    headline_parts = []
    for line in texts[0].splitlines():
        ln = line.strip()
        if not ln or re.match(r"(?i)^(ex-?99|exhibit\s*99|\d+$|.*\.htm[l]?$)", ln):
            continue
        if not headline_parts:
            if len(ln) < 20:
                continue
            headline_parts.append(ln)
            # 若首行以介词/连词结尾（如 'in', 'of', 'for', 'to', 'with', 'from', 'and'）或不足 95 字符，继续拼接下一行标题
            if not re.search(r"(?i)\b(in|of|for|to|with|from|at|on|and|by|as|into)$", ln) and len(ln) > 95:
                break
        else:
            if len(ln) < 120 and not ln.endswith("."):
                headline_parts.append(ln)
            break
    headline = " ".join(headline_parts)[:260]
    return headline, full, base + ex_files[0], real_et_dt


def fetch_sec_edgar_events(stock, cik, max_age_hours=36):
    """
    抓取 SEC EDGAR 一手官方申报，优先解析 Form 8-K/6-K 挂载的 EX-99.1 新闻稿全文及原始发布时间戳。
    """
    events = []
    if not cik:
        return events
    cik_str = str(cik).zfill(10)
    cik_int = int(cik)
    submissions_url = f"https://data.sec.gov/submissions/CIK{cik_str}.json"
    headers = {"User-Agent": "CatalystBiotechIntelligence ResearchAdmin/3.0 (research@biotechcatalyst.org)"}
    try:
        data = http_json(submissions_url, headers=headers)
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        filing_dates = recent.get("filingDate", [])
        acc_nums = recent.get("accessionNumber", [])
        primary_docs = recent.get("primaryDocument", [])
        act_descs = recent.get("primaryDocDescription", [])
        items_list = recent.get("items", [])

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        for i in range(min(12, len(forms))):
            form = forms[i]
            if form not in ("8-K", "8-K/A", "6-K", "6-K/A", "4", "SC 13D", "SC 13G", "SC 13D/A", "SC 13G/A"):
                continue
            acc_num = acc_nums[i]
            acc_no_hyphen = acc_num.replace("-", "")
            f_date_str = filing_dates[i]
            prim_doc = primary_docs[i]
            doc_desc = act_descs[i] or ""
            items_desc = items_list[i] if i < len(items_list) else ""

            try:
                dt = datetime.datetime.strptime(f_date_str, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
                if (now_utc - dt).total_seconds() / 3600.0 > max_age_hours:
                    continue
            except Exception:
                continue

            event_id = f"SEC_{stock['ticker']}_{form}_{acc_num}"
            doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no_hyphen}/{prim_doc}"
            title = f"{stock['ticker']} SEC Filing Form {form}"
            if doc_desc:
                title += f" - {doc_desc}"

            body = ""
            url_show = doc_url
            if form.startswith("8-K") or form == "6-K":
                ex_headline, ex_text, ex_url, real_et_dt = fetch_sec_press_exhibits(cik_int, acc_no_hyphen)
                if real_et_dt:
                    dt = real_et_dt
                if ex_text:
                    body = ex_text
                    url_show = ex_url
                    if ex_headline:
                        title = ex_headline
            if not body:
                body = fetch_webpage_plain_text(doc_url, is_sec=True, max_chars=8000)

            item_cn = "、".join(
                SEC_8K_ITEMS_CN.get(x.strip(), x.strip()) for x in items_desc.split(",") if x.strip()
            )
            events.append(
                {
                    "id": event_id,
                    "event_type": "ticker_catalyst",
                    "is_official": True,
                    "has_substantive_body": True,
                    "stock": stock,
                    "source": f"SEC EDGAR 官方文件 (Form {form}{'；' + item_cn if item_cn else ''})",
                    "source_tier": "一手官方",
                    "published_at": dt,
                    "time_label": "SEC 受理时间",
                    "title": title,
                    "body_en": body or f"SEC Form {form} (Items: {items_desc}). Please see original filing at {doc_url}",
                    "url": url_show,
                }
            )
    except Exception as e:
        print(f"[WARN] SEC check failed for {stock['ticker']}: {e}")
    return events


def format_pub_time(dt, time_label="原始发布时间"):
    """将 UTC/带时区时间格式化为：美东时间 + 北京时间，让用户一眼看清信息真实发布时刻"""
    if not dt or not isinstance(dt, datetime.datetime):
        return "未知时间"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        et_dt = dt.astimezone(ZoneInfo("America/New_York"))
        et_str = et_dt.strftime("%Y-%m-%d %H:%M %Z")
    except Exception:
        et_dt = dt.astimezone(datetime.timezone(datetime.timedelta(hours=-4)))
        et_str = et_dt.strftime("%Y-%m-%d %H:%M EDT")
    bj_dt = dt.astimezone(datetime.timezone(datetime.timedelta(hours=8)))
    bj_str = bj_dt.strftime("%Y-%m-%d %H:%M")
    return f"{bj_str} 北京时间（{et_str}）"


def has_substantive_body(title, body_en):
    """判断正文是否包含实质性段落（剔除仅把标题重复一遍的无正文空壳邮件/快讯）"""
    if not body_en:
        return False
    t_norm = re.sub(r"\s+", " ", (title or "")).strip().lower()
    b_norm = re.sub(r"\s+", " ", body_en).strip()
    # 移除正文中与标题完全重合的部分
    if t_norm and t_norm in b_norm.lower():
        idx = b_norm.lower().find(t_norm)
        b_norm = (b_norm[:idx] + " " + b_norm[idx + len(t_norm):]).strip()
    # 移除网址与常见引导点击短语及 IR 邮件空壳套话
    b_norm = re.sub(r"https?://\S+", "", b_norm)
    b_norm = re.sub(
        r"(?i)\b(read now|full article|click here to view all releases|click here|view this release|view all releases|powered by notified|view presentation|slideshow|transcript)\b",
        "",
        b_norm,
    ).strip()
    return len(b_norm) >= 180


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
        for item in channel.findall("item")[:15]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            guid = (item.findtext("guid") or link).strip()
            pub_date = (item.findtext("pubDate") or "").strip()
            source_tag = (item.findtext("source") or "").strip()
            desc = clean_boilerplate_text(html_to_text(item.findtext("description") or ""))

            dt = None
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

            full_text = fetch_webpage_plain_text(link, is_sec=False, max_chars=25000)
            body_en = full_text if len(full_text) > len(desc) else desc
            if not has_substantive_body(title, body_en):
                continue

            # 自动识别是否为公司官方公关通稿 (Business Wire / GlobeNewswire / PR Newswire / Accesswire)
            wire_match = re.search(OFFICIAL_WIRES_PAT, body_en[:2500])
            if wire_match:
                wire_name = wire_match.group(1)
                pub_source = f"公司官方新闻稿 ({wire_name} / Yahoo Finance)"
                tier = "一手官方新闻稿 (News Release)"
                is_off = True
            else:
                pub_name = source_tag or "Yahoo Finance 财经报道"
                pub_source = f"财经媒体报道：{pub_name}"
                tier = "外部财经媒体"
                is_off = False

            short_hash = hashlib.sha1(guid.encode("utf-8")).hexdigest()[:16]
            events.append(
                {
                    "id": f"YF_{stock['ticker']}_{short_hash}",
                    "event_type": "ticker_catalyst",
                    "is_official": is_off,
                    "has_substantive_body": True,
                    "stock": stock,
                    "source": pub_source,
                    "source_tier": tier,
                    "published_at": dt,
                    "time_label": "原始发布时间",
                    "title": title,
                    "body_en": body_en,
                    "url": link,
                }
            )
        # 优先将公司一手官方新闻稿 (News Release) 排在普通媒体报道之前
        events.sort(key=lambda x: 0 if "一手官方新闻稿" in x.get("source_tier", "") else 1)
    except Exception as e:
        print(f"[WARN] Yahoo RSS failed for {stock['ticker']}: {e}")
    return events


def fetch_biocentury_rss_events(portfolio, processed_ids, max_age_hours=28):
    """
    直接主动抓取免邮箱订阅的权威生科官方 XML 订阅源：
    - BioCentury (https://www.biocentury.com/rss/BioCentury.xml)
    - Clinical Trials Arena (https://www.clinicaltrialsarena.com/feed/，完美绕过其官网的企业邮箱注册限制)
    1. 若单篇文章命中持仓标的（代码/公司名/管线药名），实时抓全文生成个股催化剂事件；
    2. 每日汇总过去 24~28 小时内的行业深度报道，各自自动生成 1 期全量【行业速览】。
    """
    events = []
    direct_feeds = [
        {
            "prefix": "BC",
            "name": "BioCentury",
            "rss_url": "https://www.biocentury.com/rss/BioCentury.xml",
            "home_url": "https://www.biocentury.com/rss",
            "fetch_full": False,
        },
        {
            "prefix": "CTA",
            "name": "Clinical Trials Arena",
            "rss_url": "https://www.clinicaltrialsarena.com/feed/",
            "home_url": "https://www.clinicaltrialsarena.com/",
            "fetch_full": True,
        },
        {
            "prefix": "BPD",
            "name": "BioPharma Dive",
            "rss_url": "https://www.biopharmadive.com/feeds/news/",
            "home_url": "https://www.biopharmadive.com/",
            "fetch_full": False,
        },
        {
            "prefix": "FDA",
            "name": "FDA Newsroom",
            "rss_url": "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml",
            "home_url": "https://www.fda.gov/news-events/fda-newsroom/press-announcements",
            "fetch_full": True,
        },
        {
            "prefix": "MCN",
            "name": "MedCity News",
            "rss_url": "https://medcitynews.com/feed/",
            "home_url": "https://medcitynews.com/",
            "fetch_full": True,
        },
    ]

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    bj_now = now_utc + datetime.timedelta(hours=8)

    for feed in direct_feeds:
        try:
            req = urllib.request.Request(
                feed["rss_url"],
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                xml_bytes = resp.read()
            root = ET.fromstring(xml_bytes)
            channel = root.find("channel")
            if channel is None:
                continue

            digest_lines = []
            digest_html_links = []
            latest_dt = None

            for item in channel.findall("item")[:25]:
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                guid = (item.findtext("guid") or link or title).strip()
                pub_date = (item.findtext("pubDate") or "").strip()
                desc = clean_boilerplate_text(html_to_text(item.findtext("description") or ""))

                dt = None
                if pub_date:
                    try:
                        dt = parsedate_to_datetime(pub_date)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=datetime.timezone.utc)
                        if (now_utc - dt).total_seconds() / 3600.0 > max_age_hours:
                            continue
                    except Exception:
                        pass

                if dt and (latest_dt is None or dt > latest_dt):
                    latest_dt = dt

                combined = f"{title}\n{desc}"
                matched_stock = None
                for stock in portfolio:
                    if is_strict_match_for_stock(combined, stock):
                        matched_stock = stock
                        break

                if matched_stock:
                    short_hash = hashlib.sha1(guid.encode("utf-8")).hexdigest()[:16]
                    ev_id = f"{feed['prefix']}_{matched_stock['ticker']}_{short_hash}"
                    if ev_id not in processed_ids:
                        body_en = f"{title}\n\n{desc}"
                        if feed["fetch_full"] and link:
                            full_txt = fetch_webpage_plain_text(link, is_sec=False, max_chars=25000)
                            if len(full_txt) > len(body_en):
                                body_en = full_txt
                        events.append(
                            {
                                "id": ev_id,
                                "event_type": "ticker_catalyst",
                                "is_official": False,
                                "has_substantive_body": len(body_en) >= 150,
                                "stock": matched_stock,
                                "source": f"生科权威媒体：{feed['name']} RSS",
                                "source_tier": "外部生科专业媒体",
                                "published_at": dt or now_utc,
                                "time_label": "原始发布时间",
                                "title": title,
                                "body_en": body_en,
                                "url": link,
                            }
                        )
                else:
                    digest_lines.append(f"• Title: {title}\n  Summary: {desc}\n  URL: {link}")
                    if link:
                        digest_html_links.append(f'<a href="{link}">{html.escape(title)}</a>')

            # 每日汇总推送 1 次行业速览（北京时间 08:00 后且当日未推送过、至少有 3 条新资讯）
            digest_id = f"{feed['prefix']}_DIGEST_{bj_now.strftime('%Y%m%d')}"
            if bj_now.hour >= 8 and len(digest_lines) >= 3 and digest_id not in processed_ids:
                events.append(
                    {
                        "id": digest_id,
                        "event_type": "industry_digest",
                        "is_official": False,
                        "has_substantive_body": True,
                        "stock": None,
                        "source": f"生科行业媒体：{feed['name']} Official RSS",
                        "source_tier": "行业媒体周刊/快讯",
                        "published_at": latest_dt or now_utc,
                        "time_label": "原始发布时间",
                        "title": f"{feed['name']} Daily Intelligence ({bj_now.strftime('%Y-%m-%d')})",
                        "body_en": "\n\n".join(digest_lines)[:28000],
                        "url": feed["home_url"],
                        "raw_html": "\n".join(digest_html_links),
                    }
                )
        except Exception as e:
            print(f"[WARN] {feed['name']} RSS check failed: {e}")
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


def fetch_gmail_imap_events(config, portfolio, processed_ids, max_age_hours=24):
    """
    高频巡检 Gmail 邮箱：
    1. 区分一手官方 IR 邮件 (alert.gcs-web.com / notified / q4) 与第三方财经媒体快讯 (Seeking Alpha / TipRanks)；
    2. 若官方邮件正文过短，自动提取邮件超链接抓取完整公告全文与数据表格；
    3. 第三方突发快讯（如 SA Breaking News 针对 ARWR 等标的之评级/竞品变动）予以保留，推事实但标记无详细长文（跳过解读）；
    4. 针对专业生科媒体（BioSpace, Endpoints 等）中未匹配特定持仓标的的 Newsletter，打包为【行业速览】要闻汇总。
    """
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

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        msg_nums = data[0].split()[-25:]
        for num in reversed(msg_nums):
            st, msg_data = mail.fetch(num, "(X-GM-MSGID RFC822)")
            if st != "OK" or not msg_data or not msg_data[0]:
                continue
            hdr_meta = msg_data[0][0].decode("utf-8", errors="ignore") if isinstance(msg_data[0][0], bytes) else str(msg_data[0][0])
            gm_m = re.search(r"X-GM-MSGID\s+(\d+)", hdr_meta)
            gas_id = f"GAS_{hex(int(gm_m.group(1)))[2:]}" if gm_m else None
            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)
            msg_id_hdr = (msg.get("Message-ID") or str(num)).strip()
            gmail_id = "GMAIL_" + hashlib.sha1(msg_id_hdr.encode("utf-8")).hexdigest()[:16]
            if gmail_id in processed_ids or (gas_id and gas_id in processed_ids):
                continue
            ev_id = gas_id or gmail_id

            subject = decode_mime_str(msg.get("Subject", "(无标题)")).strip()
            sender = decode_mime_str(msg.get("From", "")).strip()
            date_hdr = (msg.get("Date") or "").strip()

            msg_dt = None
            if date_hdr:
                try:
                    msg_dt = parsedate_to_datetime(date_hdr)
                    if msg_dt.tzinfo is None:
                        msg_dt = msg_dt.replace(tzinfo=datetime.timezone.utc)
                    if (now_utc - msg_dt).total_seconds() / 3600.0 > max_age_hours:
                        continue
                except Exception:
                    pass

            # 跳过订阅确认信本身及纯会议售票促销邮件 (如 events@endpointsnews.com)
            if re.search(r"(?i)(confirm your subscription|verify your email|welcome to .* investor)", subject):
                continue
            if "events@endpointsnews.com" in sender.lower() or re.search(r"(?i)^\d+\s*(days?|hours?)\s+to\s+go\b", subject):
                processed_ids.add(ev_id)
                continue

            body_text = ""
            raw_html = ""
            if msg.is_multipart():
                for part in msg.walk():
                    ctype = part.get_content_type()
                    if ctype == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                        payload = part.get_payload(decode=True)
                        if payload:
                            body_text += payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    elif ctype == "text/html":
                        payload = part.get_payload(decode=True)
                        if payload:
                            raw_html += payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            else:
                payload = msg.get_payload(decode=True)
                if payload:
                    body_text = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
                    if "<html" in body_text.lower():
                        raw_html = body_text

            if not body_text and raw_html:
                body_text = html_to_text(raw_html)

            cleaned_body = clean_boilerplate_text(body_text)

            # 判定官方 IR 还是第三方
            sender_lower = sender.lower()
            is_official = any(dom in sender_lower for dom in OFFICIAL_IR_DOMAINS)
            if is_official:
                source_label = f"公司官方投资者关系 (IR 邮件)"
                source_tier = "一手官方 IR 邮件"
            else:
                if "seekingalpha.com" in sender_lower:
                    source_label = f"第三方快讯：Seeking Alpha"
                elif "tipranks.com" in sender_lower:
                    source_label = f"第三方快讯：TipRanks"
                else:
                    source_label = f"第三方资讯：{sender[:35]}"
                source_tier = "第三方媒体快讯"

            # 若为一手官方 IR 邮件且正文偏短或无实质正文，自动提取超链接抓取官网全文或 SEC 申报
            article_url = "https://mail.google.com/mail/u/0/#inbox"
            if is_official and (len(cleaned_body) < 450 or not has_substantive_body(subject, cleaned_body)) and raw_html:
                links = re.findall(r'href=["\'](https?://[^"\']+)["\']', raw_html)
                for l in links:
                    if not any(x in l.lower() for x in ["unsubscribe", "privacy", "manage", "twitter", "linkedin"]):
                        fetched = fetch_webpage_plain_text(l, is_sec=("sec.gov" in l), max_chars=25000)
                        if len(fetched) > len(cleaned_body):
                            cleaned_body = clean_boilerplate_text(fetched)
                            article_url = l
                            break

            combined = f"{subject}\n{cleaned_body}"
            matched_stock = None
            for stock in portfolio:
                if is_strict_match_for_stock(combined, stock):
                    matched_stock = stock
                    break

            if matched_stock:
                # 剔除纯幻灯片重发件（如已推送过顶线数据，后缀为 - Slideshow / - Transcript 的纯图集/录音）
                if re.search(r"(?i)(-\s*slideshow|-\s*transcript)$", subject.strip()):
                    print(f"[INFO] Skipping redundant slideshow/transcript for {matched_stock['ticker']}: {subject}")
                    processed_ids.add(ev_id)
                    continue

                has_sub = has_substantive_body(subject, cleaned_body)
                # 拦截纯引导点击的 IR 链接空壳邮件（如仅含 "View this release -> or click here to view all releases. Powered by Notified"）
                is_ir_link_stub = bool(
                    re.search(
                        r"(?i)(view this release|view all releases|powered by notified|查看本新闻稿|查看所有新闻稿)",
                        cleaned_body,
                    )
                )
                if not has_sub and (is_official or is_ir_link_stub or len(cleaned_body.strip()) < 20):
                    print(f"[INFO] Skipping link-stub or empty shell email for {matched_stock['ticker']}: {subject}")
                    processed_ids.add(ev_id)
                    continue

                events.append(
                    {
                        "id": ev_id,
                        "event_type": "ticker_catalyst",
                        "is_official": is_official,
                        "has_substantive_body": has_sub,
                        "stock": matched_stock,
                        "source": source_label,
                        "source_tier": source_tier,
                        "published_at": msg_dt,
                        "time_label": "邮件发送时间",
                        "title": subject,
                        "body_en": cleaned_body[:28000],
                        "url": article_url,
                        "raw_html": raw_html,
                    }
                )
            else:
                # 未匹配持仓个股：检查是否为专业生科媒体（BioSpace, Endpoints, STAT, Nature 等）的行业 Newsletter
                if is_biotech_media_sender(sender) and len(cleaned_body) >= 150:
                    events.append(
                        {
                            "id": ev_id,
                            "event_type": "industry_digest",
                            "is_official": False,
                            "has_substantive_body": True,
                            "stock": None,
                            "source": f"生科行业媒体：{sender[:35]}",
                            "source_tier": "行业媒体周刊/快讯",
                            "published_at": msg_dt,
                            "time_label": "邮件发送时间",
                            "title": subject,
                            "body_en": cleaned_body[:28000],
                            "url": "https://mail.google.com/mail/u/0/#inbox",
                            "raw_html": raw_html,
                        }
                    )
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
            gen_cfg = {
                "temperature": 0.1 if expect_json else 0.15,
                "maxOutputTokens": 16384,
            }
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
                resp = http_json(url, method="POST", payload=payload, timeout=120)
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
                if e.code in (429, 503):
                    time.sleep(1.5)
                    continue
            except Exception as e:
                print(f"[WARN] Gemini key#{attempt+1} model {model} error: {e}")
    return None, None



def translate_title_fallback(title, api_key):
    """当主翻译未提取到中文或大模型限流时，调用超轻量模型秒级单句直译标题，100% 确保标题为中文"""
    if not title:
        return ""
    prompt = f"请将以下美股生物医药通告标题直译为一句精炼中文标题（不要多余解释，只输出中文标题）：\n{title}"
    text, _ = call_gemini(api_key, prompt, expect_json=False, models=["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-flash-lite-latest"])
    if text:
        cleaned = re.sub(r"^[\*\"'“”‘’\s]+|[\*\"'“”‘’\s]+$", "", text.strip())
        if cleaned and re.search(r"[\u4e00-\u9fa5]", cleaned):
            return cleaned
    return title

def gemini_translate_facts(ev, api_key):
    """
    阶段一：100% 完整全文精译（严禁概括总结！）
    严格提供：1. 英文原标题；2. 中文翻译标题；3. 【核心亮点 Highlights】逐条精译 + 【完整新闻稿正文与临床数据表格】逐段逐表完整翻译
    采用纯文本标签分隔（不使用 JSON 字符串包裹长文，彻底杜绝因引号/换行转义导致的 JSON 解析失败或被模型擅自缩写成摘要）
    """
    stock = ev["stock"]
    prompt = f"""请将以下关于生物科技公司 {stock['ticker']} ({stock['company']}) 的最新英文稿件进行 100% 完整、逐条、逐段、逐表的简体中文全文翻译。

【来源】：{ev['source']}
【英文原标题】：{ev['title']}
【英文原始全文】：
{ev['body_en']}

⚠️ 铁律要求（违反任何一条均为不合格）：
1. **严禁总结、严禁缩写、严禁省略任何段落或数据！** 必须对英文原稿进行从头到尾的**完整全文翻译**，绝不允许把几千字的新闻稿概括成几百字的摘要！不要输出“【核心要点 (Highlights)】”或“【新闻稿完整正文与数据表格】”之类多余的分节小标题，直接输出内容！
2. **开头导语要点 Highlights（全部加粗显示）**：新闻稿标题下方、正式电头（如 `CITY, State -- (BUSINESS WIRE / GLOBE NEWSWIRE) -- Date`）之前通常有若干条核心亮点导语（Highlights）。你必须在最开头用 `• **...**` 的格式（**整条要点用粗体 `**` 包裹**），**逐条、完整、一字不漏地翻译每一条 Highlight**（100%保留所有实验数据、终点名称、剂量组、百分比、p值与时间点）。
3. **新闻稿完整正文与临床数据表格**：
   - 从电头（如 `康涅狄格州纽黑文 -- (美国商业资讯) -- 2026年10月19日 -- ...`）开始，严格按照英文原稿的自然段落顺序，**逐段完整直译每一段正文**。
   - **完整还原所有数据表格（如 Table 1、Table 2 等）**：严禁用文字概述表格！英文原稿中的表格已展开为等宽列网格（每一行的 `|` 分隔单元格数量完全相等，跨列表头已按列重复展开，跨行分类已在首列重复展开）。你在翻译表格时，**每一行的 `|` 单元格总数必须与英文原表每一行的单元格总数 100% 严格相等**（例如原表第一行有 3 个重复的 `New Study '304' Topline` 对应下方的 `QD | BID | Placebo`，你也必须在第一行保留对应的空单元格及 3 个重复的 `新“304”研究顶线数据` 单元格；原表首列若有重复的跨行分类名称，也必须在每一行首列保留），以便下游渲染引擎自动合并生成完美的 `colspan` 跨列表头与 `rowspan` 跨行单元格！
   - **完整翻译所有高管与临床专家（KOL）引语**：管理层与外部医学专家的每一段发言必须完整逐句翻译，不得删减。
   - **完整翻译关于药物/临床项目/适应症/公司的背景段落（About ...）**：文末的 `About [Drug]`、`About the Clinical Program`、`About [Condition]`、`About [Company]` 段落也必须完整翻译（仅剔除 `Forward-Looking Statements` 法律免责套话及网站导航广告）。
4. 若该资料实际主语不是 {stock['company']} ({stock['ticker']}) 或其研发管线（例如误抓了其他无关公司），请将 `[RELEVANT]` 设为 `NO`，`[SCORE]` 设为 `1`。
5. 全文严禁出现“美股、炒股、买入、卖出、加仓、减仓、止盈、止损、暴涨、暴跌、基金经理、交易建议”等敏感字眼。

请严格按照以下纯文本标签格式输出（不要输出 Markdown 代码块包裹）：
[RELEVANT]: YES 或 NO
[CATEGORY]: 从以下选一：临床数据 / 合作与并购 / 内部人变动 / 监管与上市 / 事项预告 / 经营业绩 / 行业报道 / 常规行政
[SCORE]: 1到10的整数（1-3=无关或例行行政；4-6=一般报道；7-8=重要管线进展/会议预告/管理层增持/合作里程碑；9-10=关键临床II/III期顶线数据/监管获批或CRL/收购并购）
[TITLE_CN]: 完整精准直译的中文标题
[BODY_CN_START]
（在此处输出包含【核心要点 (Highlights)】+【新闻稿完整正文与数据表格】的 100% 完整全文中文翻译）
[BODY_CN_END]"""

    raw, used_model = call_gemini(
        api_key, prompt, expect_json=False, use_search=False, models=GEMINI_TRIAGE_MODELS
    )
    if raw:
        rel_m = re.search(r"\[?\*?\*?RELEVANT\*?\*?\]?[:：]\s*(YES|NO|TRUE|FALSE)", raw, re.I)
        cat_m = re.search(r"\[?\*?\*?CATEGORY\*?\*?\]?[:：]\s*([^\n\r]+)", raw)
        score_m = re.search(r"\[?\*?\*?SCORE\*?\*?\]?[:：]\s*(\d+)", raw)
        title_m = re.search(r"\[?\*?\*?(?:TITLE_CN|中文标题)\*?\*?\]?[:：]\s*([^\n\r]+)", raw, re.I)
        body_m = re.search(r"\[?\*?\*?(?:BODY_CN_START|正文开始)\*?\*?\]?[:：]?\s*(.*?)\s*(?:\[?\*?\*?(?:BODY_CN_END|正文结束)\*?\*?\]?|$)", raw, re.S | re.I)

        is_rel = True
        if rel_m and rel_m.group(1).upper() in ("NO", "FALSE"):
            is_rel = False
        cat = cat_m.group(1).strip() if cat_m else "临床数据"
        try:
            sc = int(score_m.group(1)) if score_m else 8
        except Exception:
            sc = 8
        t_cn = title_m.group(1).strip() if title_m else ""
        if not t_cn or not re.search(r"[\u4e00-\u9fa5]", t_cn):
            t_cn = translate_title_fallback(ev["title"], api_key)
        b_cn = body_m.group(1).strip() if body_m else raw.strip()
        return {
            "is_relevant": is_rel,
            "category": cat,
            "impact_score": sc,
            "title_cn": t_cn,
            "body_cn": b_cn,
            "_model": used_model,
        }

    # 兜底：即使大模型超时，也绝不允许直接输出全英文标题与正文
    fallback_title = translate_title_fallback(ev["title"], api_key)
    return {
        "is_relevant": True,
        "category": "事项预告" if re.search(r"(?i)financial results|conference call|webcast", ev["title"]) else "最新动态",
        "impact_score": 5 if re.search(r"(?i)financial results|conference call|webcast", ev["title"]) else 6,
        "title_cn": fallback_title,
        "body_cn": ev["body_en"][:3000],
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


def format_body_for_wechat_html(text):
    """
    将含 Markdown 表格与粗体的纯文本安全转换为微信深色/浅色模式自适应的 HTML。
    智能还原表格的 colspan（跨列表头居中合并）与 rowspan（首列跨行分类合并），
    并自动修复列数不足的表头行，彻底杜绝表头与数据列错位。
    """
    if not text:
        return ""
    lines = text.splitlines()
    out_parts = []
    table_rows = []

    def flush_table():
        nonlocal table_rows
        if not table_rows:
            return
        parsed = []
        for row in table_rows:
            cells = [c.strip() for c in row.strip().strip("|").split("|")]
            if all(re.match(r"^[:\-\s]*$", c) for c in cells):
                continue
            parsed.append(cells)
        if not parsed:
            table_rows = []
            return

        max_cols = max(len(r) for r in parsed)
        # 自动对齐修复：若某表头行单元格数少于 max_cols（例如模型未重复跨列表头），自动按组展开对齐到 max_cols
        norm_rows = []
        for r_idx, r in enumerate(parsed):
            if len(r) < max_cols:
                non_empty = [c for c in r if c]
                empty_cnt = len(r) - len(non_empty)
                # 检查下一行开头有几个空单元格作为左侧指标列宽度参考
                label_cols = max(1, empty_cnt)
                if r_idx + 1 < len(parsed):
                    nxt_empty = 0
                    for c in parsed[r_idx + 1]:
                        if not c:
                            nxt_empty += 1
                        else:
                            break
                    if nxt_empty > label_cols:
                        label_cols = nxt_empty
                rem = max_cols - label_cols
                if non_empty and rem > 0 and rem % len(non_empty) == 0:
                    span = rem // len(non_empty)
                    new_r = [""] * label_cols
                    for item in non_empty:
                        new_r.extend([item] * span)
                    r = new_r
                else:
                    r = r + [""] * (max_cols - len(r))
            norm_rows.append(r[:max_cols])

        num_r = len(norm_rows)
        # 计算首列 (col=0) 的垂直跨行合并 rowspan（当总列数 >= 7 且首列相邻非空文本完全相同时合并）
        skip_cell = set()
        rowspan_map = {}
        if max_cols >= 7:
            r = 0
            while r < num_r:
                val = norm_rows[r][0]
                if val and r >= 1:
                    rs = 1
                    while r + rs < num_r and norm_rows[r + rs][0] == val:
                        skip_cell.add((r + rs, 0))
                        rs += 1
                    if rs > 1:
                        rowspan_map[(r, 0)] = rs
                    r += rs
                else:
                    r += 1

        t_html = [
            '<div style="overflow-x:auto;margin:8px 0;"><table style="width:100%;border-collapse:collapse;font-size:12.5px;line-height:1.5;">'
        ]
        for r_idx, r in enumerate(norm_rows):
            t_html.append("<tr>")
            is_header = (r_idx == 0) or (r_idx == 1 and not r[0])
            tag = "th" if is_header else "td"
            c_idx = 0
            while c_idx < max_cols:
                if (r_idx, c_idx) in skip_cell:
                    c_idx += 1
                    continue
                val = r[c_idx]
                # 在表头行（或连续空表头格）自动水平合并相邻同值单元格为 colspan
                cs = 1
                if is_header:
                    while (
                        c_idx + cs < max_cols
                        and (r_idx, c_idx + cs) not in skip_cell
                        and r[c_idx + cs] == val
                        and (val != "" or c_idx + cs < 2)
                    ):
                        cs += 1
                rs = rowspan_map.get((r_idx, c_idx), 1)
                c_esc = html.escape(val)
                c_esc = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", c_esc)
                attrs = []
                if cs > 1:
                    attrs.append(f'colspan="{cs}"')
                if rs > 1:
                    attrs.append(f'rowspan="{rs}"')
                align = "center" if (is_header and val) else "left"
                bg_th = "background:rgba(128,128,128,0.12);" if is_header else ""
                attr_str = (" " + " ".join(attrs)) if attrs else ""
                t_html.append(
                    f'<{tag}{attr_str} style="border:1px solid rgba(128,128,128,0.4);{bg_th}padding:5px 7px;text-align:{align};vertical-align:middle;">{c_esc}</{tag}>'
                )
                c_idx += cs
            t_html.append("</tr>")
        t_html.append("</table></div>")
        out_parts.append("".join(t_html))
        table_rows = []

    # 将可能连在同一行内的多个项目符号（如 " • "）拆分为独立行，保证每个要点独立分段
    normalized_text = re.sub(r"([^\n])\s+•\s+", r"\1\n• ", text)
    lines = normalized_text.splitlines()

    in_opening_highlights = True
    for ln in lines:
        s = ln.strip()
        if not s:
            flush_table()
            continue
        if re.match(r"^【(核心要点|新闻稿完整正文).*】$", s):
            continue
        if s.startswith("|") and s.endswith("|") and s.count("|") >= 2:
            in_opening_highlights = False
            table_rows.append(s)
        else:
            flush_table()
            if re.search(r"(--\s*\(|（美国商业资讯）|（环球电讯社）|（美通社）|BUSINESS WIRE|GLOBE NEWSWIRE)", s):
                in_opening_highlights = False
            # 处理 Markdown 标题行（如 ### 1. 🎯 ...）
            if s.startswith("#"):
                h_clean = re.sub(r"^#+\s*", "", s)
                esc = html.escape(h_clean)
                esc = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
                out_parts.append(f'<div style="font-weight:bold;margin:10px 0 4px 0;">{esc}</div>')
                continue
            # 过滤无有效网址的空“原文链接”行
            if re.match(r"^🔗?\s*(?:\*\*)?原文链接(?:\*\*)?\s*[：:]?\s*(?:无|暂无|N/?A|none|-)?$", s, re.I):
                continue
            esc = html.escape(s)
            # 将 "🔗 **原文链接**：https://..." 或 "[原文链接](https://...)" 统一转换为仅在“原文链接”4个字上挂载超链接，隐藏冗长裸网址
            esc = re.sub(
                r"(?:\*\*)?原文链接(?:\*\*)?\s*[：:]\s*(?:\[[^\]]*\]\()?(https?://[^\s<>\"\)]+)\)?",
                r'<a href="\1" style="color:#2563eb;text-decoration:underline;font-weight:bold;">原文链接</a>',
                esc,
            )
            esc = re.sub(
                r"(?:\*\*)?\[(?:\*\*)?([^\]]+?)(?:\*\*)?\]\((https?://[^\s<>\"\)]+)\)(?:\*\*)?",
                r'<a href="\2" style="color:#2563eb;text-decoration:underline;font-weight:bold;">\1</a>',
                esc,
            )
            esc = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
            # 确保开头导语中的所有 • 要点 (Highlights) 强制以粗体展示
            if in_opening_highlights and s.startswith(("•", "·", "* ")) and "<b>" not in esc:
                esc = f"<b>{esc}</b>"
            out_parts.append(f'<div style="margin:6px 0;">{esc}</div>')
    flush_table()
    return "\n".join(out_parts)


def send_pushplus(token, title, html_body):
    if not token or "请在此粘贴" in token:
        return
    try:
        res = http_json(
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
        print(f"[INFO] PushPlus response for '{title[:32]}': code={res.get('code')} msg={res.get('msg')}")
    except Exception as e:
        print(f"[WARN] PushPlus send failed: {e}")


def gemini_industry_digest(ev, api_key):
    """
    对生物医药专业媒体（STAT, Endpoints, BioSpace, Fierce, Nature Briefing, BioCentury 等）综合 Newsletter 提炼全量高价值行业速览：
    1. 彻底过滤纯招聘、会议买票促销或通用消费品广告；
    2. 取消人为条数上限（应留尽留）：凡涉及个股药企动态/临床数据、M&A并购潜力/BD交易/投融资、宏观医药政策/FDA监管风向/药价医保博弈、以及前沿靶点突破，全部逐条完整保留（哪怕一封邮件有 8~16 条也全部列出，绝不遗漏任何对个股或医药板块有推动作用的资讯）；
    3. 每条输出：【中文简明标题】+ (English Title) + 核心事实与催化逻辑（含公司/政策主体、靶点/适应症、关键数据/金额或板块影响）+ 原文链接。
    """
    links = []
    if ev.get("raw_html"):
        raw_links = re.findall(r'href=["\'](https?://[^"\']+)["\']', ev["raw_html"])
        for l in raw_links:
            if not any(x in l.lower() for x in ["unsubscribe", "privacy", "terms", "facebook", "twitter", "linkedin", "instagram", "youtube", "mailto:", "manage", "apple.com", "google.com"]):
                if l not in links:
                    links.append(l)
    links_str = "\n".join(links[:35])

    prompt = f"""请阅读以下来自生物医药专业媒体的行业 Newsletter，为生物医药投资人提取一份**全面、无遗漏**的高价值生科行业与政策要闻速览。

【邮件标题】：{ev['title']}
【发件信源】：{ev['source']}
【邮件正文完整文本】：
{ev['body_en'][:28000]}

【邮件附带的文章链接清单】：
{links_str}

⚠️ 提炼铁律（核心原则：**应留尽留，严禁人为砍掉有价值的资讯，绝不限制在3条或5条**）：
1. **四大必留核心维度（只要涉及以下任一维度，必须 100% 全部保留并逐条输出，哪怕一封邮件有 8～16 条也必须全部列出，绝不能漏掉）**：
   - **① 个股与药企动态**：任何生物科技/制药公司的临床试验数据读出（无论成功或失败）、管线推进/终止、高管与战略变动、财务与商业化表现；
   - **② 并购潜力与资本交易（M&A / BD / 融资）**：并购传闻、潜在被收购标的探讨、大药企 BD 授权合作（License-in/out）、资产剥离拆分、IPO/PIPE 及大额 VC 融资；
   - **③ 宏观医药政策与监管风向（板块级推动/压制因素）**：FDA 审批决定/CRL/现场突击检查（如中印药企检查）、美国国会/两党选举与医疗立法走向、HHS/FDA/CMS 高层人事变动、IRA 医保药价谈判、PBM（药房福利管理）改革、关税与生物安全法案等一切对医药整体板块或细分赛道有潜在影响的重要政策事件；
   - **④ 前沿科学与靶点突破**：新靶点/新机制验证、头对头竞争格局变化、AI 制药实质性突破、顶刊（Nature/Science/Cell/NEJM）重磅转化成果。
2. **仅过滤纯噪音广告**：仅剔除纯求职招聘广告（Job openings/薪资调查）、纯会议买票促销、CDMO 外包硬广、以及与生命科学/医药健康完全无关的通用广告。
3. **每一条新闻严格采用以下格式**：
   • 📌 **【中文简明标题】** (English Title)
     💡 **核心要点**：清晰说明核心事实与潜在影响（包含相关药企/政策机构、靶点/适应症、临床期别、关键疗效/安全性数据、交易金额或对板块/公司的潜在推动逻辑）。
     🔗 [原文链接](匹配的完整文章URL)（从链接清单中匹配最相关的文章 URL，超链接仅挂在“原文链接”4个字上；若无对应单篇链接则不写此行）。
4. 若整封邮件 100% 全为招聘报告或纯会议买票广告、无任何上述四大维度资讯，请将 [VALID] 设为 NO，无需输出正文。
5. 全文严禁出现“炒股、买入、卖出、加仓、止损”等敏感词。

请严格按以下标签输出：
[VALID]: YES 或 NO
[MEDIA_NAME]: 媒体简短名称（如 STAT News / Endpoints / BioSpace / Fierce Biotech / Nature Briefing 等，12字以内）
[TOPIC_CN]: 本期速览核心主题（如：美大选医疗政策转向与M&A动态 / 肿瘤与罕见病临床进展 等，16字以内）
[DIGEST_START]
（在此处完整输出所有符合四大维度的要闻列表，应留尽留，严禁只挑3条）
[DIGEST_END]"""

    raw, used_model = call_gemini(api_key, prompt, expect_json=False, use_search=False, models=GEMINI_TRIAGE_MODELS)
    if not raw:
        return False, "", "", ""
    valid_m = re.search(r"\[VALID\]:\s*(YES|NO)", raw, re.I)
    if valid_m and valid_m.group(1).upper() == "NO":
        return False, "", "", ""
    media_m = re.search(r"\[MEDIA_NAME\]:\s*([^\n\r]+)", raw)
    topic_m = re.search(r"\[TOPIC_CN\]:\s*([^\n\r]+)", raw)
    digest_m = re.search(r"\[DIGEST_START\]\s*(.*?)\s*(?:\[DIGEST_END\]|$)", raw, re.S)

    media_name = media_m.group(1).strip() if media_m else "生科媒体"
    topic_cn = topic_m.group(1).strip() if topic_m else "行业重磅速览"
    digest_text = digest_m.group(1).strip() if digest_m else raw.strip()
    return True, media_name, topic_cn, digest_text


def process_single_event(ev, config, access_token, vault_knowledge, processed_ids, processed_titles=None):
    """
    统一事件处理器：
    - 区分三种推送标题前缀：
      1. 【官方】{Ticker} | {中文标题} 与 【官方·解读】{Ticker} | {中文标题}
      2. 【第三方】{Ticker} | {中文标题} 与 【第三方·解读】{Ticker} | {中文标题}
      3. 【行业速览】{媒体名称} | {中文主题}
    - 针对第三方简短快讯（如 Seeking Alpha 评级/竞品异动），推事实全貌但智能跳过深度解读，绝不制造假空壳！
    """
    api_key = config.get("GEMINI_API_KEY", "").strip()
    push_token = config.get("PUSHPLUS_TOKEN", "").strip()
    threshold = int(float(config.get("IMPACT_THRESHOLD", "7") or 7))
    ev_type = ev.get("event_type", "ticker_catalyst")

    # 分支 A：生科行业综合速览 Newsletter
    if ev_type == "industry_digest":
        norm_title = re.sub(r"[^a-z0-9]+", "", ev["title"].lower())
        if processed_titles is not None and norm_title and norm_title in processed_titles:
            print(f"[INFO] Skipped duplicate industry newsletter by title: {ev['title']}")
            processed_ids.add(ev["id"])
            return
        is_valid, media_name, topic_cn, digest_text = gemini_industry_digest(ev, api_key)
        if not is_valid or not digest_text.strip():
            print(f"[INFO] Skipped non-actionable industry newsletter: {ev['title']}")
            processed_ids.add(ev["id"])
            return

        push_title = f"【行业速览】{media_name} | {topic_cn}"
        pub_time_str = format_pub_time(ev.get("published_at"), time_label="邮件发送时间")
        html_body = f"""
        <div style="font-family:sans-serif;line-height:1.68;font-size:14px;">
          <p style="margin:0 0 6px 0;font-size:13px;opacity:0.85;">
            <b>{html.escape(media_name)}</b> ｜ 生科行业要闻速览<br/>
            🕒 {html.escape(pub_time_str)}<br/>
            📡 信源：{html.escape(ev['source'])}
          </p>
          <hr style="border:none;border-top:1px solid rgba(128,128,128,0.3);margin:8px 0;"/>
          <div style="margin:8px 0;font-size:14px;">{format_body_for_wechat_html(digest_text)}</div>
          <hr style="border:none;border-top:1px solid rgba(128,128,128,0.3);margin:8px 0;"/>
          <p style="margin:4px 0;font-size:13px;opacity:0.85;">
            <a href="{ev['url']}">🔗 查看原始邮件</a> ｜ <a href="https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit">📊 云端历史归档表</a>
          </p>
        </div>
        """
        send_pushplus(push_token, push_title, html_body)
        print(f"[INFO] Sent Industry Digest: {push_title}")

        bj_time = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
        append_sheet_log(
            access_token,
            [
                bj_time,
                "INDUSTRY",
                f"{ev['source']} | {pub_time_str}",
                "行业速览",
                5,
                f"【信源】{ev['source']}\n【原标题】{ev['title']}\n【行业要闻速览】\n{digest_text}",
                "生科行业综合速览（不针对单一持仓标的，无个股深度解读）",
                f"{ev['title']} ({ev['url']})",
                ev["id"],
            ],
        )
        processed_ids.add(ev["id"])
        if processed_titles is not None and norm_title:
            processed_titles.add(norm_title)
        return

    # 分支 B：持仓标的个股催化剂（【官方】 vs 【第三方】）
    stock = ev["stock"]
    is_official = is_official_event(ev)

    # 标题防重复：在调用大模型前先检查是否已在 24 小时内推送过相同标题事件
    norm_title = re.sub(r"[^a-z0-9]+", "", ev["title"].lower())
    if processed_titles is not None and norm_title and norm_title in processed_titles:
        print(f"[INFO] Skipped duplicate story by title for {stock['ticker']}: {ev['title']}")
        processed_ids.add(ev["id"])
        return

    facts = gemini_translate_facts(ev, api_key)
    if not facts.get("is_relevant", True):
        processed_ids.add(ev["id"])
        return

    score = int(facts.get("impact_score", 6))
    category = facts.get("category", "最新动态")
    title_cn = facts.get("title_cn", ev["title"])
    body_cn = facts.get("body_cn", "")

    # 空壳拦截
    if "仅含标题" in body_cn or len(body_cn.strip()) < 25:
        print(f"[INFO] Blocked empty body translation for {stock['ticker']}: {ev['title']}")
        processed_ids.add(ev["id"])
        return

    pub_time_str = format_pub_time(ev.get("published_at"))
    time_label = ev.get("time_label", "原始发布时间")

    # 智能识别纯日程/电话会预告（避免对没有业绩数据的预告强行深度分析）
    is_schedule_notice = bool(
        re.search(r"(?i)to report .* financial results|conference call on|webcast on|announces date for|financial results on", ev["title"])
    )
    if is_schedule_notice:
        category = "事项预告"
        score = min(score, 6)

    # 准备深度解读（条件：高分、有实质长文、且非纯日程预告）
    deep_text = ""
    deep_model_label = ""
    vault_badge = ""
    can_deep_analyze = (
        score >= threshold
        and ev.get("has_substantive_body", True)
        and len(body_cn.strip()) >= 120
        and "仅含标题" not in body_cn
        and not is_schedule_notice
    )

    if can_deep_analyze:
        time.sleep(1.5)
        stock_vault_notes = list(vault_knowledge.get(stock["ticker"], []))
        if stock["ticker"] == "MANE" and "ABSI" in vault_knowledge:
            stock_vault_notes.extend(vault_knowledge["ABSI"])
        res_deep, used_m = gemini_deep_analysis(
            ev, facts, api_key, vault_notes=stock_vault_notes
        )
        if res_deep and "深度解读暂不可用" not in res_deep and len(res_deep.strip()) >= 50:
            deep_text = res_deep.strip()
            deep_model_label = used_m
            vault_badge = " + 投研库内外口径比对" if stock_vault_notes else ""

    # 构建统一单条推送（深色/浅色模式 100% 自适应高对比度排版，绝不硬编码黑底黑字或白底白字）
    has_deep = bool(deep_text)
    badge = f"【{'官方' if is_official else '第三方'}{'·深度' if has_deep else ''}】"
    push_title = f"{badge}{stock['ticker']} | {title_cn}"

    deep_card_html = ""
    if has_deep:
        deep_card_html = f"""
        <div style="background:rgba(128,128,128,0.12);border-left:4px solid #f59e0b;padding:12px 14px;margin:12px 0;border-radius:4px;">
          <div style="font-weight:bold;font-size:13.5px;margin-bottom:6px;">
            💡 Gemini 深度投研解读 ({deep_model_label} + ClinicalTrials{vault_badge})
          </div>
          <div style="font-size:13.5px;line-height:1.68;">
            {format_body_for_wechat_html(deep_text)}
          </div>
        </div>
        """

    notice_tip = ""
    if is_schedule_notice:
        notice_tip = """
        <div style="background:rgba(128,128,128,0.12);border-left:4px solid #3b82f6;padding:8px 12px;margin:10px 0;border-radius:4px;font-size:13px;">
          📌 <b>【事项预告提示】</b>：本通告为公司例行公布的财报/电话会日程预告，尚未披露具体经营业绩与财务数据。完整财务报告将在预告时间正式公布。
        </div>
        """

    unified_html = f"""
    <div style="font-family:sans-serif;line-height:1.68;font-size:14px;">
      <p style="margin:0 0 6px 0;font-size:13px;opacity:0.85;">
        <b>{stock['ticker']}</b> ({stock['company']}) ｜ {category} ｜ <b>影响评分：{score}/10</b><br/>
        🕒 {html.escape(time_label)}：{html.escape(pub_time_str)}<br/>
        📡 信源：{html.escape(ev['source'])}
      </p>
      <div style="margin:8px 0;line-height:1.5;">
        <div style="font-size:12.5px;opacity:0.78;">【原标题】{html.escape(ev['title'])}</div>
        <div style="font-weight:bold;font-size:15.5px;margin-top:4px;">{html.escape(title_cn)}</div>
      </div>
      {notice_tip}
      {deep_card_html}
      <hr style="border:none;border-top:1px solid rgba(128,128,128,0.3);margin:10px 0;"/>
      <div style="margin:8px 0;font-size:14px;line-height:1.72;">
        <div style="font-weight:bold;margin-bottom:6px;font-size:13.5px;">📄 事实全文直译与临床数据：</div>
        {format_body_for_wechat_html(body_cn)}
      </div>
      <hr style="border:none;border-top:1px solid rgba(128,128,128,0.3);margin:10px 0;"/>
      <p style="margin:4px 0;font-size:12.5px;opacity:0.85;">
        <a href="{ev['url']}">🔗 查看英文原稿</a> ｜ <a href="https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit">📊 打开云端历史归档 (Logs)</a>
      </p>
    </div>
    """

    send_pushplus(push_token, push_title, unified_html)
    print(f"[INFO] Sent Unified Single Push: {push_title} (score={score}, has_deep={has_deep})")
    log_deep_text = deep_text if deep_text else "未达深度解读阈值或为短快讯（已整合推送事实全貌）"

    bj_time = (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(hours=8)
    ).strftime("%Y-%m-%d %H:%M:%S")
    append_sheet_log(
        access_token,
        [
            bj_time,
            stock["ticker"],
            f"{ev['source']} | 发布于 {pub_time_str}",
            category,
            score,
            f"【{time_label}】{pub_time_str}\n【原始信源】{ev['source']}\n【英文原标题】{ev['title']}\n【中文翻译标题】{title_cn}\n【中文翻译正文】{body_cn}",
            log_deep_text,
            f"{ev['title']} ({ev['url']})",
            ev["id"],
        ],
    )
    processed_ids.add(ev["id"])
    if processed_titles is not None and norm_title:
        processed_titles.add(norm_title)


def trigger_next_github_relay(config):
    """在 14 分钟常驻循环结束前自动触发下一棒 GitHub Actions workflow_dispatch，消除 GitHub cron 延迟"""
    gh_pat = (config.get("GITHUB_PAT") or os.environ.get("GITHUB_PAT") or "").strip()
    if not gh_pat:
        return
    headers = {
        "Authorization": f"Bearer {gh_pat}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "AntigravityBiotechMonitor/3.0",
    }
    try:
        # 检查是否已有排队中的任务，避免重复堆积
        runs_url = "https://api.github.com/repos/liwenzhong3312-a11y/us-stock-catalyst-monitor/actions/runs?status=queued&per_page=5"
        res = http_json(runs_url, headers=headers, timeout=10)
        if res.get("total_count", 0) > 0:
            print("[INFO] Next GitHub Actions run is already queued, skipping self-dispatch.")
            return
        disp_url = "https://api.github.com/repos/liwenzhong3312-a11y/us-stock-catalyst-monitor/actions/workflows/monitor.yml/dispatches"
        req = urllib.request.Request(
            disp_url,
            data=json.dumps({"ref": "main"}).encode("utf-8"),
            headers={**headers, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"[INFO] Successfully triggered next 24x7 GitHub Actions relay (HTTP {resp.status}).")
    except Exception as e:
        print(f"[WARN] GitHub relay dispatch skipped: {e}")


def main(duration_minutes=0):
    access_token = get_google_access_token()
    config, portfolio, processed_ids, processed_titles = load_sheet_state(access_token)

    print(f"[INFO] Loaded {len(portfolio)} active targets from Google Sheet: {[s['ticker'] for s in portfolio]}")

    # 启动时先毫秒级读取 Google Sheet 的 Vault 投研知识库，绝不在 SEC/新闻巡检前阻塞
    vault_knowledge = {}
    try:
        from vault_sync import load_vault_sheet_records
        _, vault_knowledge = load_vault_sheet_records(access_token)
    except Exception as e:
        print(f"[WARN] Vault records load skipped or failed: {e}")

    cik_map = fetch_sec_cik_map()

    # 持续常驻近实时流式监听循环模式
    if duration_minutes > 0:
        print(f"[INFO] Starting Continuous Real-Time Monitor Loop for {duration_minutes} minutes...")
        loop_start = time.time()
        end_time = loop_start + duration_minutes * 60 - 30  # 预留 30 秒安全退出
        last_rss_sec_check = 0

        try:
            while time.time() < end_time:
                now_t = time.time()

                # 1. 每 60 秒轮询一次：优先执行 SEC EDGAR + Yahoo Finance RSS 实时监控，随后在剩余时间预算内推进 1 份投研库文献翻译
                if now_t - last_rss_sec_check >= 60:
                    last_rss_sec_check = now_t

                    for stock in portfolio:
                        cik = cik_map.get(stock["ticker"])
                        sec_ev = fetch_sec_edgar_events(stock, cik, max_age_hours=36)
                        for ev in sec_ev:
                            if ev["id"] not in processed_ids:
                                process_single_event(ev, config, access_token, vault_knowledge, processed_ids, processed_titles)

                    for stock in portfolio:
                        yf_ev = fetch_yahoo_direct_news(stock, max_age_hours=36)
                        for ev in yf_ev:
                            if ev["id"] not in processed_ids:
                                process_single_event(ev, config, access_token, vault_knowledge, processed_ids, processed_titles)

                    bc_ev = fetch_biocentury_rss_events(portfolio, processed_ids, max_age_hours=28)
                    for ev in bc_ev:
                        if ev["id"] not in processed_ids:
                            process_single_event(ev, config, access_token, vault_knowledge, processed_ids, processed_titles)

                    if os.environ.get("ENABLE_GMAIL_IMAP", "0") == "1":
                        gmail_ev = fetch_gmail_imap_events(config, portfolio, processed_ids, max_age_hours=24)
                        for ev in gmail_ev:
                            if ev["id"] not in processed_ids:
                                process_single_event(ev, config, access_token, vault_knowledge, processed_ids, processed_titles)

                    if os.environ.get("SYNC_DRIVE_VAULT", "0") == "1" and time.time() < end_time - 60:
                        try:
                            from vault_sync import sync_drive_vault
                            vault_knowledge = sync_drive_vault(
                                access_token,
                                config,
                                portfolio,
                                send_pushplus_fn=send_pushplus,
                                deadline=end_time - 25,
                                max_files_per_pass=3,
                            )
                        except Exception as ve:
                            print(f"[WARN] Periodic vault sync check error: {ve}")

                time.sleep(10)
            print(f"[INFO] Continuous Monitor Loop completed cleanly after {duration_minutes} minutes.")
        finally:
            trigger_next_github_relay(config)
        return

    # 单次巡检模式 (duration_minutes == 0)
    print(f"[INFO] Running single-pass audit for {[s['ticker'] for s in portfolio]}...")
    all_events = []

    # 1. SEC EDGAR
    for stock in portfolio:
        cik = cik_map.get(stock["ticker"])
        sec_ev = fetch_sec_edgar_events(stock, cik, max_age_hours=24)
        for ev in sec_ev:
            if ev["id"] not in processed_ids:
                all_events.append(ev)

    # 2. Yahoo Finance RSS
    for stock in portfolio:
        yf_ev = fetch_yahoo_direct_news(stock, max_age_hours=24)
        for ev in yf_ev:
            if ev["id"] not in processed_ids:
                all_events.append(ev)

    # 3. Gmail IMAP
    gmail_ev = fetch_gmail_imap_events(config, portfolio, processed_ids, max_age_hours=24)
    all_events.extend(gmail_ev)

    for ev in all_events[:5]:
        if ev["id"] not in processed_ids:
            process_single_event(ev, config, access_token, vault_knowledge, processed_ids, processed_titles)


if __name__ == "__main__":
    duration_env = os.environ.get("MONITOR_DURATION_MINUTES", "").strip()
    duration = int(duration_env) if duration_env.isdigit() else 0
    if "--loop" in sys.argv:
        duration = 14
    main(duration_minutes=duration)
