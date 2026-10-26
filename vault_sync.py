#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Google Drive 生物医药投研库自动同步与中英对照 PDF 翻译引擎 (vault_sync.py)
核心功能：
1. 自动巡检并维护 Google Drive 投研库目录结构：
   - 00_网站与催化剂底稿同步 (Claude_Artifact_Sync)：存放从 Claude Artifact 导出的 .html/.tsx/.md/.json 源码或催化剂汇总表
   - 每只持仓标的专属文件夹（如 ABSI_Absci, ARWR_Arrowhead, KRYS_Krystal, MANE_Veradermics）：
     • 01_公司内部官方口径 (Deck_Transcript_SEC)：公司路演 PPT (Deck)、电话会纪要 (Transcript)、SEC 深度文件
     • 02_外部投行与独立研报 (SellSide_Consensus)：华尔街投行报告、市场预期共识 (Beat/Miss 阈值)、独立第三方分析
     • 03_中英对照译文与解析 (Bilingual_Output)：系统自动生成的中英对照 PDF 与在线文档
2. 支持多 API Key 轮询池（在 Config!B3 中用英文逗号分隔多个不同 GCP 项目的 Key），在长文档分块翻译时自动负载均衡与防限流切换。
3. 自动将提取出的【公司内部官方口径】、【外部投行预期共识】、【自建网站催化剂关注框架】写入 Google Sheet 的 `Vault` 标签页，供实时监控引擎 `monitor.py` 在新公告发布时自动联动比对。
"""

import base64
import datetime
import html
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

SPREADSHEET_ID = os.environ.get(
    "SPREADSHEET_ID", "1pIKfEy9UNPs32W5gBFJu3N5doqU-SKCvIBDjX-TMwGQ"
)

VAULT_MODELS = [
    "gemini-flash-latest",
    "gemini-3-flash-preview",
    "gemini-pro-latest",
]

_KEY_INDEX = 0


def parse_api_keys(raw_key_str):
    """解析单个或多个以逗号、分号或换行分隔的 Gemini API Key"""
    if not raw_key_str:
        return []
    keys = [
        k.strip()
        for k in re.split(r"[,，;\n\r\s]+", str(raw_key_str))
        if k.strip() and not k.strip().startswith("请")
    ]
    return list(dict.fromkeys(keys))


def http_json(url, method="GET", headers=None, payload=None, timeout=60):
    req_headers = headers.copy() if headers else {}
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json; charset=utf-8")
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8", errors="replace"))


def drive_find_or_create_folder(access_token, name, parent_id=None):
    q = f"mimeType='application/vnd.google-apps.folder' and name='{name}' and trashed=false"
    if parent_id:
        q += f" and '{parent_id}' in parents"
    url = "https://www.googleapis.com/drive/v3/files?" + urllib.parse.urlencode(
        {"q": q, "fields": "files(id,name,webViewLink)"}
    )
    res = http_json(url, headers={"Authorization": f"Bearer {access_token}"})
    files = res.get("files", [])
    if files:
        return files[0]
    body = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        body["parents"] = [parent_id]
    create_url = "https://www.googleapis.com/drive/v3/files?fields=id,name,webViewLink"
    return http_json(
        create_url,
        method="POST",
        headers={"Authorization": f"Bearer {access_token}"},
        payload=body,
    )


def ensure_vault_folders(access_token, portfolio):
    """确保 Google Drive 投研库及所有持仓标的子目录完整存在"""
    root = drive_find_or_create_folder(
        access_token, "美股生物医药投研库 (Biotech Research Vault)"
    )
    sync_folder = drive_find_or_create_folder(
        access_token, "00_网站与催化剂底稿同步 (Claude_Artifact_Sync)", root["id"]
    )
    stocks_map = {}
    for stock in portfolio:
        ticker = stock["ticker"]
        clean_comp = re.sub(r"[^A-Za-z0-9]+", "", stock["company_clean"].split()[0])
        folder_name = f"{ticker}_{clean_comp}" if clean_comp else ticker
        # 先查找是否已有以 TICKER_ 开头的文件夹
        q = (
            f"mimeType='application/vnd.google-apps.folder' and "
            f"name contains '{ticker}_' and '{root['id']}' in parents and trashed=false"
        )
        url = "https://www.googleapis.com/drive/v3/files?" + urllib.parse.urlencode(
            {"q": q, "fields": "files(id,name,webViewLink)"}
        )
        existing = http_json(
            url, headers={"Authorization": f"Bearer {access_token}"}
        ).get("files", [])
        sf = existing[0] if existing else drive_find_or_create_folder(access_token, folder_name, root["id"])

        f_internal = drive_find_or_create_folder(
            access_token, "01_公司内部官方口径 (Deck_Transcript_SEC)", sf["id"]
        )
        f_external = drive_find_or_create_folder(
            access_token, "02_外部投行与独立研报 (SellSide_Consensus)", sf["id"]
        )
        f_bilingual = drive_find_or_create_folder(
            access_token, "03_中英对照译文与解析 (Bilingual_Output)", sf["id"]
        )
        stocks_map[ticker] = {
            "stock": stock,
            "folder": sf,
            "internal": f_internal,
            "external": f_external,
            "bilingual": f_bilingual,
        }
    return {"root": root, "sync_folder": sync_folder, "stocks": stocks_map}


def list_folder_files(access_token, folder_id):
    q = (
        f"'{folder_id}' in parents and "
        f"mimeType != 'application/vnd.google-apps.folder' and trashed=false"
    )
    url = "https://www.googleapis.com/drive/v3/files?" + urllib.parse.urlencode(
        {
            "q": q,
            "fields": "files(id,name,mimeType,modifiedTime,size,webViewLink)",
            "orderBy": "modifiedTime desc",
            "pageSize": 25,
        }
    )
    res = http_json(url, headers={"Authorization": f"Bearer {access_token}"})
    return res.get("files", [])


def extract_docx_text(docx_bytes):
    """内置 .docx (ZIP+XML) 解析器：无需第三方库即可提取 Word 中的所有段落与表格文字"""
    try:
        with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
            if "word/document.xml" not in zf.namelist():
                return ""
            xml_bytes = zf.read("word/document.xml")
        root = ET.fromstring(xml_bytes)
        lines = []
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "p":
                texts = [
                    t.text
                    for t in elem.iter()
                    if (t.tag.endswith("}t") or t.tag == "t") and t.text
                ]
                if texts:
                    lines.append("".join(texts))
        return "\n\n".join(lines)
    except Exception:
        return ""


def convert_office_file_to_pdf_bytes(access_token, file_meta):
    """
    利用 Google Drive 官方转换引擎，将用户上传的 Word (.docx/.doc) 或 PPT (.pptx/.ppt)
    临时转换为 Google Doc/Slides 并导出为原生 PDF 二进制流（保留全部表格与图片），用完即焚。
    """
    file_id = file_meta["id"]
    fname = file_meta.get("name", "").lower()
    mime = file_meta.get("mimeType", "").lower()
    target_g_mime = (
        "application/vnd.google-apps.presentation"
        if ("presentation" in mime or fname.endswith((".ppt", ".pptx")))
        else "application/vnd.google-apps.document"
    )
    copy_url = f"https://www.googleapis.com/drive/v3/files/{file_id}/copy?fields=id"
    copy_info = http_json(
        copy_url,
        method="POST",
        headers={"Authorization": f"Bearer {access_token}"},
        payload={"name": f"_temp_office_conv_{file_id}", "mimeType": target_g_mime},
    )
    temp_id = copy_info["id"]
    try:
        export_url = f"https://www.googleapis.com/drive/v3/files/{temp_id}/export?mimeType=application/pdf"
        req_exp = urllib.request.Request(
            export_url, headers={"Authorization": f"Bearer {access_token}"}
        )
        with urllib.request.urlopen(req_exp, timeout=60) as resp:
            return resp.read()
    finally:
        try:
            del_req = urllib.request.Request(
                f"https://www.googleapis.com/drive/v3/files/{temp_id}",
                headers={"Authorization": f"Bearer {access_token}"},
                method="DELETE",
            )
            urllib.request.urlopen(del_req, timeout=20).close()
        except Exception:
            pass


def download_drive_file_bytes(access_token, file_meta):
    file_id = file_meta["id"]
    mime = file_meta.get("mimeType", "")
    if mime == "application/vnd.google-apps.document":
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}/export?mimeType=application/pdf"
    elif mime == "application/vnd.google-apps.presentation":
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}/export?mimeType=application/pdf"
    elif mime == "application/vnd.google-apps.spreadsheet":
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}/export?mimeType=text/csv"
    else:
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {access_token}"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def call_gemini_with_rotation(
    api_keys, prompt, inline_bytes=None, inline_mime=None, expect_json=False, deep_thinking=False
):
    """支持多 Key 自动轮询切换 + 原生 PDF 多模态输入的 Gemini 调用器"""
    global _KEY_INDEX
    keys = parse_api_keys(api_keys) if isinstance(api_keys, str) else list(api_keys)
    if not keys:
        return None, None

    parts = []
    if inline_bytes and inline_mime:
        b64_data = base64.b64encode(inline_bytes).decode("ascii")
        parts.append({"inlineData": {"mimeType": inline_mime, "data": b64_data}})
    parts.append({"text": prompt})

    for model in VAULT_MODELS:
        for attempt in range(len(keys)):
            key = keys[(_KEY_INDEX + attempt) % len(keys)]
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                f"?key={urllib.parse.quote(key)}"
            )
            gen_cfg = {"temperature": 0.15}
            if expect_json:
                gen_cfg["responseMimeType"] = "application/json"
            if deep_thinking:
                gen_cfg["thinkingConfig"] = {"thinkingBudget": 4096}

            payload = {
                "contents": [{"parts": parts}],
                "generationConfig": gen_cfg,
            }
            try:
                resp = http_json(url, method="POST", payload=payload, timeout=120)
                _KEY_INDEX = (_KEY_INDEX + attempt + 1) % len(keys)
                candidates = resp.get("candidates", [])
                if candidates and "content" in candidates[0]:
                    c_parts = candidates[0]["content"].get("parts", [])
                    text = "\n".join(
                        p.get("text", "")
                        for p in c_parts
                        if p.get("text") and not p.get("thought")
                    ).strip()
                    if text:
                        return text, model
            except urllib.error.HTTPError as e:
                print(f"[WARN] Gemini key#{attempt+1} model {model} HTTP {e.code}: {e}")
                if e.code == 429:
                    time.sleep(2)
                    continue
            except Exception as e:
                print(f"[WARN] Gemini key#{attempt+1} model {model} error: {e}")
    return None, None


def split_pdf_chunks(pdf_bytes, pages_per_chunk=6, max_chunks=8):
    """
    如果本地或云端安装了 pypdf，则将长 PDF 切分为每 6 页一个分块（兼顾图表识别精度与输出 Token 上限）；
    如果未安装 pypdf 或文件较小，则直接整份作为单块发送给 Gemini 原生 PDF 引擎。
    """
    try:
        from pypdf import PdfReader, PdfWriter

        reader = PdfReader(io.BytesIO(pdf_bytes))
        total_pages = len(reader.pages)
        if total_pages <= pages_per_chunk:
            return [(1, total_pages, pdf_bytes)]
        chunks = []
        for start in range(0, min(total_pages, pages_per_chunk * max_chunks), pages_per_chunk):
            end = min(start + pages_per_chunk, total_pages)
            writer = PdfWriter()
            for p in range(start, end):
                writer.add_page(reader.pages[p])
            buf = io.BytesIO()
            writer.write(buf)
            chunks.append((start + 1, end, buf.getvalue()))
        return chunks
    except Exception:
        return [(1, "全部", pdf_bytes)]


def translate_and_extract_pdf(api_keys, stock, role_type, file_name, pdf_bytes):
    """
    针对上传到 Drive 的英文 PDF（公司 Deck / 电话会纪要 / 外部投行研报）：
    1. 分块进行【中英对照精译】（保留页码、核心原文句子、中文精准翻译、图表与临床数据表格）
    2. 按内外口径提炼结构化底稿摘要
    """
    chunks = split_pdf_chunks(pdf_bytes, pages_per_chunk=6, max_chunks=8)
    bilingual_sections = []

    for start_p, end_p, chunk_bytes in chunks:
        chunk_prompt = f"""你正在处理生物科技公司 {stock['ticker']} ({stock['company']}) 的【{role_type}】原始英文 PDF 文档（文件名：{file_name}，当前分块：第 {start_p} 至 {end_p} 页）。

请逐页/逐节输出高可读性的【中英对照精译版】（使用清晰的 Markdown 格式）：
1. 对每一页（Slide / Page）标明页码与小标题。
2. 采用“**【EN 原文要点/段落】** -> **【CN 中文精译】**”的上下对照结构，完整翻译该页的所有实质文字、临床试验设计、终点数据、p值、剂量、不良反应表格、财务与现金流数据。
3. 如果某页包含关键数据图表或对比表格，请用 Markdown 表格或清晰的要点将其数据完整还原并翻译为中文。
4. 自动忽略纯装饰性封面、空白页或冗长的标准法律免责声明页（仅用一句话注记即可）。
5. 全文严禁使用“炒股、买入、卖出、加仓、暴涨、暴跌”等敏感字眼。"""

        trans_md, _ = call_gemini_with_rotation(
            api_keys,
            chunk_prompt,
            inline_bytes=chunk_bytes,
            inline_mime="application/pdf",
            expect_json=False,
            deep_thinking=False,
        )
        if trans_md:
            bilingual_sections.append(f"## 📄 第 {start_p} - {end_p} 页中英对照\n\n{trans_md}")
        time.sleep(2)

    if not bilingual_sections:
        print(f"[WARN] No translated sections obtained for {file_name}. Aborting PDF creation to avoid blank 1-page document.")
        return None, None
    full_bilingual_md = "\n\n---\n\n".join(bilingual_sections)

    # 第二步：基于翻译后的全文提炼【内部官方口径 vs 外部投行共识】结构化底稿
    if "内部" in role_type:
        summary_instruction = """请将该【公司内部官方资料 (Deck/Transcript/SEC)】提炼为不超过 350 字的结构化底稿，严格包含：
1. **【官方核心卖点与管线定位】**：管理层主打的适应症、靶点优势及核心临床数据结论；
2. **【管理层承诺的催化剂日历】**：官方明确给出的后续读出时间窗口、入组节点或监管申报节点；
3. **【关键指引与话术口径】**：现金流跑道（Cash Runway）、管理层强调的统计口径及潜在回避的细节。"""
    else:
        summary_instruction = """请将该【外部投行研报 / 独立分析 (Sell-Side Consensus)】提炼为不超过 350 字的结构化底稿，严格包含：
1. **【投行核心观点与预期共识 (Consensus Bar)】**：外部机构对该管线临床数据的基准预期（什么数据算超预期 Beat / 符合预期 Base / 不及预期 Miss）；
2. **【核心催化剂关注指标】**：外部分析师最盯紧的主要/次要终点、对照组差值或安全性红线；
3. **【外部质疑点与潜在风险】**：投行指出的竞争对手威胁、临床设计隐患或商业化分歧。"""

    summary_prompt = f"""以下是关于 {stock['ticker']} ({stock['company']}) 的【{role_type}】（{file_name}）的完整内容：

{full_bilingual_md[:28000]}

{summary_instruction}
⚠️ 严禁出现敏感荐股词汇，直接输出精炼要点："""

    structured_summary, _ = call_gemini_with_rotation(
        api_keys,
        summary_prompt,
        expect_json=False,
        deep_thinking=True,
    )
    return structured_summary or full_bilingual_md[:400], full_bilingual_md


def parse_claude_artifact_or_text(api_keys, file_name, raw_text, portfolio):
    """
    解析用户从自建网站（如 Claude Artifact 导出的 .html / .tsx / .jsx / .md / .txt）上传的底稿，
    自动识别涉及的持仓标的，提取网站上整理的【催化剂日历 + 关键数据关注点 + 投行预期共识】
    """
    tickers_str = ", ".join(f"{s['ticker']} ({s['company']})" for s in portfolio)
    prompt = f"""以下是用户自建投研网站/Claude Artifact 的源代码或文档内容（文件名：{file_name}）。
当前监控的持仓标的列表为：{tickers_str}

【网站源码/文档内容】：
{raw_text[:35000]}

请仔细解析其中的所有生物医药公司信息、管线催化剂时间表、临床数据关注点及投行预期共识，并严格按以下 JSON 格式返回：
{{
  "matched_tickers": ["匹配的股票代码，如 ABSI；若涉及多只则填多个"],
  "title_cn": "该投研底稿的中文标题概述",
  "per_ticker_notes": {{
    "ABSI": "【网站催化剂与预期共识底稿】：1. 近期核心催化剂与时间点：...；2. 关键临床数据评估标准（Beat/Base/Miss 阈值）：...；3. 投行预期共识与核心关注点：..."
  }},
  "bilingual_markdown": "将网站/文档中整理的所有核心信息、催化剂表格、临床指标阈值与投行共识完整整理为一篇结构清晰的中英对照/中文详实投研底稿（Markdown格式，保留所有具体数字、药名、时间节点与投行观点）"
}}"""

    res_raw, _ = call_gemini_with_rotation(
        api_keys, prompt, expect_json=True, deep_thinking=True
    )
    if res_raw:
        try:
            cleaned = re.sub(r"^```json\s*|\s*```$", "", res_raw.strip())
            return json.loads(cleaned)
        except Exception as e:
            print(f"[WARN] Failed to parse Claude artifact JSON: {e}")
    return None


def markdown_to_styled_html(title, subtitle, summary_text, body_md):
    """将中英对照内容转换为高保真【左右严格分栏并列（左侧英文原文 50%，右侧中文精译 50%）】的 HTML 表格"""
    # 将正文按照 Markdown 标题或分段拆分为行
    sections = re.split(r"(?m)^(##+ .+)$", body_md)
    table_rows = []
    current_sec_title = "核心内容中英对照"
    
    i = 0
    while i < len(sections):
        part = sections[i].strip()
        if not part:
            i += 1
            continue
        if re.match(r"^##+ ", part):
            current_sec_title = re.sub(r"^##+\s*", "", part)
            table_rows.append(f'''
            <tr style="background:#e8f0fe;border-top:2px solid #1a73e8;border-bottom:1px solid #d2e3fc;">
              <th colspan="2" style="padding:10px 14px;text-align:left;color:#174ea6;font-size:11pt;">
                📂 {html.escape(current_sec_title)}
              </th>
            </tr>
            ''')
            i += 1
            if i < len(sections):
                part = sections[i].strip()
            else:
                break
        
        # 拆分段落进行左右分栏对照解析
        paras = [p.strip() for p in re.split(r"\n{2,}", part) if p.strip()]
        for p_idx, p in enumerate(paras):
            # 检查是否有显式的 EN / CN 标记
            m_pair = re.search(r"(?is)(?:【?EN(?:原文)?】?|\[EN\])\s*(.*?)\s*(?:【?CN(?:精译|中文)?】?|\[CN\])\s*(.*)", p)
            if m_pair:
                en_col = html.escape(m_pair.group(1).strip()).replace("\n", "<br/>")
                cn_col = html.escape(m_pair.group(2).strip()).replace("\n", "<br/>")
            else:
                # 默认全宽段落或自动加粗处理
                escaped_p = html.escape(p).replace("\n", "<br/>")
                escaped_p = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped_p)
                en_col = escaped_p
                cn_col = escaped_p

            bg = "#fcfcfc" if p_idx % 2 == 0 else "#ffffff"
            table_rows.append(f'''
            <tr style="background:{bg};border-bottom:1px solid #e0e0e0;">
              <td style="width:50%;padding:11px 14px;vertical-align:top;font-size:9.5pt;line-height:1.68;color:#3c4043;border-right:1px solid #e0e0e0;font-family:'Segoe UI',Roboto,sans-serif;word-break:break-word;">
                {en_col}
              </td>
              <td style="width:50%;padding:11px 14px;vertical-align:top;font-size:10pt;line-height:1.75;color:#202124;font-family:'Microsoft YaHei','PingFang SC',sans-serif;word-break:break-word;">
                {cn_col}
              </td>
            </tr>
            ''')
        i += 1

    consensus_escaped = html.escape(summary_text).replace("\n", "<br/>")
    consensus_escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", consensus_escaped)
    table_content = "\n".join(table_rows)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<title>{html.escape(title)}</title>
</head>
<body style="font-family:'Microsoft YaHei','Segoe UI',Arial,sans-serif;line-height:1.6;color:#202124;padding:15px;max-width:1150px;margin:0 auto;">
  <div style="background:#1a73e8;color:#ffffff;padding:16px 20px;border-radius:6px;margin-bottom:16px;">
    <h1 style="margin:0 0 4px 0;font-size:16pt;color:#ffffff;">{html.escape(title)}</h1>
    <p style="margin:0;font-size:10pt;color:#e8f0fe;">{html.escape(subtitle)}</p>
  </div>
  <div style="background:#fef7e0;border-left:5px solid #fbbc04;padding:14px 18px;margin-bottom:20px;border-radius:4px;">
    <h2 style="margin:0 0 6px 0;font-size:12pt;color:#b06000;">🎯 华尔街核心观点与预期共识基准 (Consensus Bar)</h2>
    <div style="font-size:10pt;line-height:1.68;color:#202124;">{consensus_escaped}</div>
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
      {table_content}
    </tbody>
  </table>
  <div style="margin-top:20px;text-align:center;font-size:9pt;color:#888;">
    美股生物医药投研库 (Biotech Research Vault) ｜ 左右分栏高保真中英对照导出版
  </div>
</body>
</html>"""


def create_bilingual_doc_and_pdf_in_drive(access_token, output_folder_id, base_title, html_content):
    """
    利用 Google Drive 官方云端渲染引擎生成单一纯净的【中英对照PDF】：
    1. 临时将排版好的 HTML 导入为云端渲染实例
    2. 调用 Drive Export API 直接导出为高质量中文排版 PDF 并存入 `03_中英对照译文与解析` 文件夹
    3. 立即自动销毁临时在线文档实例，确保文件夹内只保留 1 份纯净的 PDF 文件！
    """
    boundary = "===AntigravityVaultBoundary==="
    doc_meta = {
        "name": f"_temp_render_{base_title}",
        "mimeType": "application/vnd.google-apps.document",
    }
    body_parts = (
        f"--{boundary}\r\n"
        f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(doc_meta, ensure_ascii=False)}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: text/html; charset=UTF-8\r\n\r\n"
        f"{html_content}\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")

    upload_url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,name,webViewLink"
    doc_info = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                upload_url,
                data=body_parts,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": f"multipart/related; boundary={boundary}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                doc_info = json.loads(resp.read().decode("utf-8"))
            if doc_info and "id" in doc_info:
                break
        except urllib.error.HTTPError as e:
            if attempt < 2 and e.code in (500, 502, 503, 504):
                time.sleep(3 * (attempt + 1))
                continue
            raise

    doc_id = doc_info["id"]

    try:
        # 导出为原生 PDF 二进制流
        export_url = f"https://www.googleapis.com/drive/v3/files/{doc_id}/export?mimeType=application/pdf"
        req_exp = urllib.request.Request(
            export_url, headers={"Authorization": f"Bearer {access_token}"}
        )
        with urllib.request.urlopen(req_exp, timeout=60) as resp:
            pdf_bytes = resp.read()
    finally:
        # 立即删除临时渲染用的 Google Doc，确保只留下一份 PDF
        try:
            del_req = urllib.request.Request(
                f"https://www.googleapis.com/drive/v3/files/{doc_id}",
                headers={"Authorization": f"Bearer {access_token}"},
                method="DELETE",
            )
            urllib.request.urlopen(del_req, timeout=20).close()
        except Exception:
            pass

    pdf_meta = {
        "name": f"【中英对照PDF】{base_title}.pdf",
        "mimeType": "application/pdf",
        "parents": [output_folder_id],
    }
    pdf_multipart = (
        f"--{boundary}\r\n"
        f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(pdf_meta, ensure_ascii=False)}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: application/pdf\r\n\r\n"
    ).encode("utf-8") + pdf_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

    pdf_info = None
    for attempt in range(3):
        try:
            req_pdf = urllib.request.Request(
                upload_url,
                data=pdf_multipart,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": f"multipart/related; boundary={boundary}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req_pdf, timeout=60) as resp:
                pdf_info = json.loads(resp.read().decode("utf-8"))
            if pdf_info and "id" in pdf_info:
                break
        except urllib.error.HTTPError as e:
            if attempt < 2 and e.code in (500, 502, 503, 504):
                time.sleep(3 * (attempt + 1))
                continue
            raise

    pdf_link = pdf_info.get("webViewLink", f"https://drive.google.com/file/d/{pdf_info['id']}/view")
    return pdf_link, pdf_link


def load_vault_sheet_records(access_token):
    """读取 Google Sheet 中 Vault 标签页已处理的记录与按股票聚合的内外口径知识库"""
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/Vault!A2:I500"
    try:
        res = http_json(url, headers={"Authorization": f"Bearer {access_token}"})
        rows = res.get("values", [])
    except Exception:
        rows = []

    processed_vault_ids = set()
    knowledge_by_ticker = {}
    for row in rows:
        if len(row) >= 9 and str(row[8]).strip():
            processed_vault_ids.add(str(row[8]).strip())
        if len(row) >= 5 and str(row[1]).strip() and str(row[4]).strip():
            ticker = str(row[1]).strip().upper()
            role = str(row[2]).strip()
            fname = str(row[3]).strip()
            summary = str(row[4]).strip()
            knowledge_by_ticker.setdefault(ticker, []).append(
                f"🔹【{role} | {fname}】\n{summary}"
            )
    return processed_vault_ids, knowledge_by_ticker


def append_vault_sheet_row(access_token, row_values):
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}"
        f"/values/Vault!A1:append?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS"
    )
    http_json(
        url,
        method="POST",
        headers={"Authorization": f"Bearer {access_token}"},
        payload={"values": [row_values]},
    )


def sync_drive_vault(access_token, config, portfolio, send_pushplus_fn=None):
    """
    巡检 Google Drive 投研库中的所有新文件：
    - 若发现新上传的英文 PDF / 文档 / Claude 网站底稿，自动执行中英对照翻译、生成 PDF 并写入 Vault 知识库
    - 返回最新的 `knowledge_by_ticker` 字典供实时解读直接调用
    """
    api_keys = parse_api_keys(config.get("GEMINI_API_KEY", ""))
    push_token = config.get("PUSHPLUS_TOKEN", "").strip()
    processed_vault_ids, knowledge_by_ticker = load_vault_sheet_records(access_token)
    if not api_keys:
        return knowledge_by_ticker

    vault_tree = ensure_vault_folders(access_token, portfolio)
    processed_count = 0

    # 1. 巡检 00_网站与催化剂底稿同步 (Claude_Artifact_Sync)
    sync_files = list_folder_files(access_token, vault_tree["sync_folder"]["id"])
    for fmeta in sync_files:
        if processed_count >= 2:
            break
        v_id = f"SYNC_{fmeta['id']}_{fmeta.get('modifiedTime', '')[:16]}"
        if v_id in processed_vault_ids:
            continue
        try:
            raw_bytes = download_drive_file_bytes(access_token, fmeta)
            fname_low = fmeta["name"].lower()
            if fname_low.endswith(".docx"):
                raw_text = extract_docx_text(raw_bytes) or raw_bytes.decode("utf-8", errors="replace")
            else:
                raw_text = raw_bytes.decode("utf-8", errors="replace")
            parsed = parse_claude_artifact_or_text(
                api_keys, fmeta["name"], raw_text, portfolio
            )
            if not parsed:
                continue
            matched = parsed.get("matched_tickers") or ["ABSI"]
            per_notes = parsed.get("per_ticker_notes") or {}
            bilingual_md = parsed.get("bilingual_markdown") or raw_text[:4000]
            base_name = os.path.splitext(fmeta["name"])[0]

            for tk in matched:
                tk_up = str(tk).upper()
                tk_note = per_notes.get(tk_up) or next(iter(per_notes.values()), "已同步网站催化剂与共识底稿")
                out_folder_id = (
                    vault_tree["stocks"][tk_up]["bilingual"]["id"]
                    if tk_up in vault_tree["stocks"]
                    else vault_tree["sync_folder"]["id"]
                )
                styled_html = markdown_to_styled_html(
                    f"{tk_up} 网站催化剂与预期共识底稿 ({base_name})",
                    f"来源：00_网站与催化剂底稿同步 ｜ 原始文件：{fmeta['name']}",
                    tk_note,
                    bilingual_md,
                )
                pdf_link, doc_link = create_bilingual_doc_and_pdf_in_drive(
                    access_token, out_folder_id, f"{tk_up}_{base_name}", styled_html
                )
                bj_time = (
                    datetime.datetime.now(datetime.timezone.utc)
                    + datetime.timedelta(hours=8)
                ).strftime("%Y-%m-%d %H:%M:%S")
                append_vault_sheet_row(
                    access_token,
                    [
                        bj_time,
                        tk_up,
                        "网站催化剂与共识底稿",
                        fmeta["name"],
                        tk_note,
                        pdf_link,
                        doc_link,
                        fmeta.get("webViewLink", ""),
                        v_id,
                    ],
                )
                knowledge_by_ticker.setdefault(tk_up, []).append(
                    f"🔹【网站催化剂与共识底稿 | {fmeta['name']}】\n{tk_note}"
                )
                if send_pushplus_fn and push_token:
                    send_pushplus_fn(
                        push_token,
                        f"📚【网站底稿已同步】{tk_up} | {base_name[:22]}",
                        f"<p>已自动解析您上传的网站/催化剂底稿 <b>{html.escape(fmeta['name'])}</b> 并注入实时监控比对库：</p>"
                        f"<div style='border-left:3px solid #fbbc04;padding-left:10px;margin:10px 0;white-space:pre-wrap;'>{html.escape(tk_note)}</div>"
                        f"<p><a href='{pdf_link}'>📄 点击打开/下载中英对照 PDF</a></p>",
                    )
            processed_vault_ids.add(v_id)
            processed_count += 1
        except Exception as e:
            print(f"[WARN] Failed syncing website artifact {fmeta['name']}: {e}")

    # 2. 巡检各持仓标的的 01_公司内部官方口径 与 02_外部投行与独立研报
    for ticker, s_folders in vault_tree["stocks"].items():
        stock = s_folders["stock"]
        for role_key, role_label in [
            ("internal", "公司内部官方口径 (Deck/Transcript)"),
            ("external", "外部投行与独立研报 (Sell-Side)"),
        ]:
            folder_id = s_folders[role_key]["id"]
            files = list_folder_files(access_token, folder_id)
            for fmeta in files:
                if processed_count >= 2:
                    break
                v_id = f"VAULT_{fmeta['id']}_{fmeta.get('modifiedTime', '')[:16]}"
                if v_id in processed_vault_ids:
                    continue
                fname = fmeta["name"]
                mime = fmeta.get("mimeType", "")
                print(f"[INFO] Translating & indexing new vault file for {ticker} ({role_label}): {fname}")
                try:
                    fname_low = fname.lower()
                    mime_low = mime.lower()
                    is_pdf = (
                        "pdf" in mime_low
                        or fname_low.endswith(".pdf")
                        or mime_low in ("application/vnd.google-apps.document", "application/vnd.google-apps.presentation")
                    )
                    is_office = (
                        fname_low.endswith((".docx", ".doc", ".pptx", ".ppt"))
                        or "wordprocessingml" in mime_low
                        or "msword" in mime_low
                        or "presentationml" in mime_low
                        or "powerpoint" in mime_low
                    )

                    if is_pdf:
                        raw_bytes = download_drive_file_bytes(access_token, fmeta)
                        summary, bilingual_md = translate_and_extract_pdf(
                            api_keys, stock, role_label, fname, raw_bytes
                        )
                    elif is_office:
                        try:
                            pdf_bytes = convert_office_file_to_pdf_bytes(access_token, fmeta)
                            summary, bilingual_md = translate_and_extract_pdf(
                                api_keys, stock, role_label, fname, pdf_bytes
                            )
                        except Exception as conv_err:
                            print(f"[WARN] Office-to-PDF conversion fallback for {fname}: {conv_err}")
                            raw_bytes = download_drive_file_bytes(access_token, fmeta)
                            text_content = extract_docx_text(raw_bytes) or raw_bytes.decode("utf-8", errors="replace")
                            prompt = f"""请将以下 {ticker} ({stock['company']}) 的【{role_label}】文档（{fname}）逐段翻译为中英对照格式，并在开头提炼 300 字以内的核心要点（区分内部承诺或外部投行预期阈值）：\n\n{text_content[:25000]}"""
                            bilingual_md, _ = call_gemini_with_rotation(
                                api_keys, prompt, expect_json=False, deep_thinking=True
                            )
                            summary = (bilingual_md or "")[:400]
                    else:
                        raw_bytes = download_drive_file_bytes(access_token, fmeta)
                        text_content = raw_bytes.decode("utf-8", errors="replace")
                        prompt = f"""请将以下 {ticker} ({stock['company']}) 的【{role_label}】文档（{fname}）逐段翻译为中英对照格式，并在开头提炼 300 字以内的核心要点（区分内部承诺或外部投行预期阈值）：\n\n{text_content[:25000]}"""
                        bilingual_md, _ = call_gemini_with_rotation(
                            api_keys, prompt, expect_json=False, deep_thinking=True
                        )
                        summary = (bilingual_md or "")[:400]

                    base_name = os.path.splitext(fname)[0]
                    styled_html = markdown_to_styled_html(
                        f"[{ticker}] {base_name} (中英对照精译版)",
                        f"标的：{ticker} ({stock['company']}) ｜ 口径分类：{role_label} ｜ 原始文件：{fname}",
                        summary,
                        bilingual_md,
                    )
                    pdf_link, doc_link = create_bilingual_doc_and_pdf_in_drive(
                        access_token,
                        s_folders["bilingual"]["id"],
                        f"{ticker}_{base_name}",
                        styled_html,
                    )
                    bj_time = (
                        datetime.datetime.now(datetime.timezone.utc)
                        + datetime.timedelta(hours=8)
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    append_vault_sheet_row(
                        access_token,
                        [
                            bj_time,
                            ticker,
                            role_label,
                            fname,
                            summary,
                            pdf_link,
                            doc_link,
                            fmeta.get("webViewLink", ""),
                            v_id,
                        ],
                    )
                    knowledge_by_ticker.setdefault(ticker, []).append(
                        f"🔹【{role_label} | {fname}】\n{summary}"
                    )
                    if send_pushplus_fn and push_token:
                        send_pushplus_fn(
                            push_token,
                            f"📚【中英对照PDF已生成】{ticker} | {base_name[:20]}",
                            f"<p><b>标的：</b>{ticker} ({stock['company']})<br/><b>口径分类：</b>{role_label}<br/><b>原始文件：</b>{html.escape(fname)}</p>"
                            f"<div style='border-left:3px solid #fbbc04;padding-left:10px;margin:10px 0;white-space:pre-wrap;font-size:14px;'><b>【核心结构化提炼】</b>\n{html.escape(summary)}</div>"
                            f"<p style='margin-top:10px;'><a href='{pdf_link}'>👉 点击打开/下载中英对照 PDF</a></p>",
                        )
                    processed_vault_ids.add(v_id)
                    processed_count += 1
                except Exception as e:
                    print(f"[WARN] Vault file process failed for {fname}: {e}")

    return knowledge_by_ticker
