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
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-flash-lite-latest",
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


def is_translated_or_temp_file(fname):
    """判断文件是否为已生成的翻译稿或临时转换文件，防止重复死循环翻译"""
    if not fname:
        return True
    base = os.path.splitext(fname)[0].strip()
    if base.endswith(("[译]", "【译】", "[中英对照]", "【中英对照】")):
        return True
    if any(
        tag in fname
        for tag in (
            "[译].",
            "【译】.",
            "【中英对照PDF】",
            "_temp_bilingual_",
            "_temp_office_conv_",
        )
    ):
        return True
    return False


VAULT_ROOT_FOLDER_ID = "182XirR1PicM8j0m1cvVAAWDNl0U_2HAg"


def _resolve_stock_subfolders(access_token, stock_folder_id):
    """智能识别标的子目录（兼容用户自定义的 '公司内部官方口径' / '外部投行与独立研报' 或带 '01_'/'02_' 前缀的目录名）"""
    q_sub = (
        f"mimeType='application/vnd.google-apps.folder' and "
        f"'{stock_folder_id}' in parents and trashed=false"
    )
    url_sub = "https://www.googleapis.com/drive/v3/files?" + urllib.parse.urlencode(
        {"q": q_sub, "fields": "files(id,name,webViewLink)", "pageSize": 50}
    )
    subs = http_json(
        url_sub, headers={"Authorization": f"Bearer {access_token}"}
    ).get("files", [])

    f_internal = None
    f_external = None
    for s in subs:
        sname = s.get("name", "")
        if not f_internal and any(k in sname for k in ("内部", "官方", "01_", "Deck", "Transcript")):
            f_internal = s
        elif not f_external and any(k in sname for k in ("外部", "投行", "研报", "02_", "SellSide")):
            f_external = s

    if not f_internal:
        f_internal = drive_find_or_create_folder(
            access_token, "01_公司内部官方口径 (Deck_Transcript_SEC)", stock_folder_id
        )
    if not f_external:
        f_external = drive_find_or_create_folder(
            access_token, "02_外部投行与独立研报 (SellSide_Consensus)", stock_folder_id
        )
    return f_internal, f_external


def ensure_vault_folders(access_token, portfolio):
    """确保 Google Drive 投研库及所有持仓标的子目录完整存在（同时支持识别用户在云盘手动新建的标的文件夹并双向同步至 Portfolio 表）"""
    root = None
    try:
        r_meta = http_json(
            f"https://www.googleapis.com/drive/v3/files/{VAULT_ROOT_FOLDER_ID}?fields=id,name,webViewLink,trashed",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if r_meta and not r_meta.get("trashed"):
            root = r_meta
    except Exception:
        pass
    if not root:
        root = drive_find_or_create_folder(
            access_token, "生物医药资料库 (Biotech Research Vault)"
        )

    sync_folder = drive_find_or_create_folder(
        access_token, "00_网站与催化剂底稿同步 (Claude_Artifact_Sync)", root["id"]
    )
    stocks_map = {}

    # 先拉取根目录下现有的全部子文件夹，避免重复查询并支持自动发现用户手动新建的标的文件夹
    q_all = (
        f"mimeType='application/vnd.google-apps.folder' and "
        f"'{root['id']}' in parents and trashed=false"
    )
    url_all = "https://www.googleapis.com/drive/v3/files?" + urllib.parse.urlencode(
        {"q": q_all, "fields": "files(id,name,webViewLink)", "pageSize": 200}
    )
    existing_root_folders = http_json(
        url_all, headers={"Authorization": f"Bearer {access_token}"}
    ).get("files", [])

    for stock in portfolio:
        ticker = stock["ticker"].strip().upper()
        clean_comp = re.sub(r"[^A-Za-z0-9]+", "", stock["company_clean"].split()[0])
        folder_name = f"{ticker}_{clean_comp}" if clean_comp else ticker
        matched_folder = None
        for ef in existing_root_folders:
            ef_name = ef.get("name", "").strip()
            if (
                ef_name.upper() == ticker
                or ef_name.upper().startswith(f"{ticker}_")
                or ef_name.upper().startswith(f"{ticker}-")
                or ef_name.upper().startswith(f"{ticker} ")
            ):
                matched_folder = ef
                break
        sf = (
            matched_folder
            if matched_folder
            else drive_find_or_create_folder(access_token, folder_name, root["id"])
        )

        f_internal, f_external = _resolve_stock_subfolders(access_token, sf["id"])
        stocks_map[ticker] = {
            "stock": stock,
            "folder": sf,
            "internal": f_internal,
            "external": f_external,
        }

    # 自动发现用户在 Google Drive 根目录下手动新建的标的文件夹（如 PBLS 等），自动补齐子目录并双向同步至 Portfolio 表
    for ef in existing_root_folders:
        ef_name = ef.get("name", "").strip()
        if ef_name.startswith("00_") or ef_name.startswith("."):
            continue
        m_tk = re.match(r"^([A-Za-z]{2,6})(?:[_\-\s]+(.*))?$", ef_name)
        if not m_tk:
            continue
        tk_up = m_tk.group(1).upper()
        if tk_up in stocks_map:
            continue
        comp_label = (m_tk.group(2) or tk_up).strip()
        f_internal, f_external = _resolve_stock_subfolders(access_token, ef["id"])
        new_stock_obj = {
            "ticker": tk_up,
            "company": comp_label,
            "company_clean": comp_label,
            "exchange": "NASDAQ",
            "keywords": [tk_up, comp_label],
            "avg_cost": "未设置",
            "pos_note": "云盘新建自动同步",
        }
        stocks_map[tk_up] = {
            "stock": new_stock_obj,
            "folder": ef,
            "internal": f_internal,
            "external": f_external,
        }
        portfolio.append(new_stock_obj)
        # 自动将该新标的追加到 Google Sheet Portfolio 表，实现双向全自动同步
        try:
            append_url = (
                f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}"
                f"/values/{urllib.parse.quote('Portfolio!A:G')}:append?valueInputOption=USER_ENTERED"
            )
            http_json(
                append_url,
                method="POST",
                headers={"Authorization": f"Bearer {access_token}"},
                payload={
                    "values": [
                        [tk_up, comp_label, "NASDAQ", comp_label, "", "云盘手动新建自动纳管", "YES"]
                    ]
                },
            )
            print(f"[INFO] Auto-registered new Drive stock folder '{ef_name}' ({tk_up}) into Portfolio sheet.")
        except Exception as e:
            print(f"[WARN] Auto-registering {tk_up} to Portfolio failed: {e}")

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
            if deep_thinking and ("gemini-3-flash" in model or "pro" in model):
                gen_cfg["thinkingConfig"] = {"thinkingBudget": 4096}

            payload = {
                "contents": [{"parts": parts}],
                "generationConfig": gen_cfg,
            }
            try:
                resp = http_json(url, method="POST", payload=payload, timeout=90)
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
                if e.code in (429, 503):
                    time.sleep(0.5)
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

请逐页/逐节输出严格对应的【左右双栏中英对照精译版】：
1. 对每一页（Slide / Page）或章节，先用三级标题标明页码与小标题：`### Page {start_p} - 小标题`
2. 必须将每一段或每一个核心要点的【英文原文】与【中文精译】严格使用 `[EN]` 和 `[CN]` 标签成对输出（系统将据此自动渲染为左侧英文原文、右侧中文精译的并列双栏表格）：
[EN]
（此处完整摘录该段/该要点的英文原文 Original English，保留所有临床试验数据、p值、终点指标与药名）
[CN]
（此处输出对应的权威简体中文逐句精译 Chinese Translation，准确翻译所有医学术语、临床设计与财务数据）
3. 如果某页包含关键数据图表或对比表格，请在 `[EN]` 中还原英文指标与数值，在 `[CN]` 中输出对应的中文指标与数值。
4. 自动忽略纯装饰性封面、空白页或冗长的标准法律免责声明页。
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
            bilingual_sections.append(f"## 📄 第 {start_p} - {end_p} 页中英对照 (Pages {start_p}-{end_p})\n\n{trans_md}")
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


def _format_cell_md(raw_str):
    escaped = html.escape((raw_str or "").strip()).replace("\n", "<br/>")
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    return escaped


def markdown_to_styled_html(title, subtitle, summary_text, body_md):
    """将中英对照内容转换为高保真【左右严格分栏并列（左侧英文原文 50%，右侧中文精译 50%）】的 HTML 表格"""
    if not body_md:
        body_md = ""
    # 将正文按照 Markdown 二级/三级标题拆分为章节
    sections = re.split(r"(?m)^(#{2,3}\s+.+)$", body_md)
    table_rows = []
    current_sec_title = "核心内容中英对照"

    pair_pattern = re.compile(
        r"(?:\*\*)*\s*(?:\[EN(?:_START)?\]|【EN[^】]*】)\s*(?:\*\*)*\s*:?\s*(.*?)\s*"
        r"(?:->\s*)?(?:\*\*)*\s*(?:\[CN(?:_START)?\]|【CN[^】]*】)\s*(?:\*\*)*\s*:?\s*(.*?)"
        r"(?=(?:\*\*)*\s*(?:\[EN(?:_START)?\]|【EN[^】]*】)|\Z)",
        re.DOTALL | re.IGNORECASE,
    )

    i = 0
    while i < len(sections):
        part = sections[i].strip()
        if not part:
            i += 1
            continue
        if re.match(r"^#{2,3}\s+", part):
            current_sec_title = re.sub(r"^#{2,3}\s*", "", part)
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

        # 优先在章节全文中提取所有 [EN] ... [CN] ... 对照块（不受段落间空行影响）
        matches = list(pair_pattern.finditer(part))
        if matches:
            for p_idx, m in enumerate(matches):
                en_raw = re.sub(r"\[EN_END\]|\[CN_END\]", "", m.group(1)).strip()
                cn_raw = re.sub(r"\[EN_END\]|\[CN_END\]", "", m.group(2)).strip()
                en_col = _format_cell_md(en_raw)
                cn_col = _format_cell_md(cn_raw)
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
        else:
            # 若无显式 [EN]/[CN] 标签，按段落智能分离英文行与中文行，或跨两列展示，绝不在左右两栏重复同一段文字
            paras = [p.strip() for p in re.split(r"\n{2,}", part) if p.strip() and p.strip() != "---"]
            for p_idx, p in enumerate(paras):
                lines = [ln.strip() for ln in p.splitlines() if ln.strip()]
                en_lines = [ln for ln in lines if not re.search(r"[\u4e00-\u9fa5]", ln)]
                cn_lines = [ln for ln in lines if re.search(r"[\u4e00-\u9fa5]", ln)]
                bg = "#fcfcfc" if p_idx % 2 == 0 else "#ffffff"
                if en_lines and cn_lines:
                    en_col = _format_cell_md("\n".join(en_lines))
                    cn_col = _format_cell_md("\n".join(cn_lines))
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
                else:
                    full_col = _format_cell_md(p)
                    table_rows.append(f'''
                    <tr style="background:{bg};border-bottom:1px solid #e0e0e0;">
                      <td colspan="2" style="padding:11px 14px;vertical-align:top;font-size:10pt;line-height:1.72;color:#202124;font-family:'Microsoft YaHei','Segoe UI',sans-serif;word-break:break-word;">
                        {full_col}
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


def _should_translate_pdf_text(txt, font_size):
    if font_size < 4.5:
        return False
    t = txt.strip()
    if not t:
        return False
    without_emails_urls = re.sub(r"https?://\S+|\S+@\S+", "", t)
    if not re.search(r"[A-Za-z]{2,}", without_emails_urls):
        return False
    if re.match(r"^[A-Z]{2,5}(\s+(US|LN|equity))?$", t) and t not in (
        "BUY",
        "HOLD",
        "SELL",
        "NOTE",
        "DATA",
        "NEWS",
        "RISK",
    ):
        return False
    return True


def _split_line_into_column_segments(ln):
    spans = [s for s in ln.get("spans", []) if s.get("text", "").strip()]
    if not spans:
        return []
    spans.sort(key=lambda s: s["bbox"][0])
    segs = []
    cur = [spans[0]]
    for s in spans[1:]:
        prev_x1 = cur[-1]["bbox"][2]
        cur_x0 = s["bbox"][0]
        if cur_x0 - prev_x1 > 12.0:
            segs.append(cur)
            cur = [s]
        else:
            cur.append(s)
    segs.append(cur)
    out = []
    for seg in segs:
        txt = " ".join(s["text"].strip() for s in seg).strip()
        x0 = min(s["bbox"][0] for s in seg)
        y0 = min(s["bbox"][1] for s in seg)
        x1 = max(s["bbox"][2] for s in seg)
        y1 = max(s["bbox"][3] for s in seg)
        max_sz = max(float(s.get("size", 9.0)) for s in seg)
        dom_col = int(max(seg, key=lambda s: len(s.get("text", "")))["color"])
        bold = any(
            "bold" in (s.get("font") or "").lower()
            or "black" in (s.get("font") or "").lower()
            or (int(s.get("flags", 0)) & 16)
            for s in seg
        )
        out.append(
            {
                "bbox": [x0, y0, x1, y1],
                "text": txt,
                "size": max_sz,
                "color": f"#{dom_col:06x}",
                "bold": bold,
            }
        )
    return out


def _extract_page_translatable_blocks(page):
    d = page.get_text("dict")
    items = []
    for b in d.get("blocks", []):
        if b.get("type") != 0:
            continue
        lines = b.get("lines", [])
        if not lines:
            continue
        bbox = list(b["bbox"])
        bw = max(1.0, bbox[2] - bbox[0])

        rows = []
        for ln in sorted(lines, key=lambda l: (round(l["bbox"][1], 1), l["bbox"][0])):
            ly_mid = 0.5 * (ln["bbox"][1] + ln["bbox"][3])
            if rows and abs(ly_mid - rows[-1]["y_mid"]) <= 3.5:
                rows[-1]["lines"].append(ln)
            else:
                rows.append({"y_mid": ly_mid, "lines": [ln]})

        row_segments = []
        has_multicol_row = False
        for r in rows:
            merged_spans = []
            for ln in r["lines"]:
                merged_spans.extend(ln.get("spans", []))
            segs = _split_line_into_column_segments({"spans": merged_spans})
            if len(segs) > 1:
                has_multicol_row = True
            row_segments.append(segs)

        all_segs = [s for segs in row_segments for s in segs]
        if not all_segs:
            continue
        sizes = [s["size"] for s in all_segs]
        colors = {s["color"] for s in all_segs}
        same_style = (max(sizes) - min(sizes) <= 1.5) and len(colors) == 1
        avg_line_chars = sum(len(s["text"]) for s in all_segs) / max(1, len(rows))
        has_contact_info = any(
            re.search(r"@|\b\d{3}[\s.\-]\d{3}[\s.\-]\d{4}\b", s["text"])
            for s in all_segs
        )
        is_unified_block = (
            (not has_multicol_row)
            and (not has_contact_info)
            and len(rows) >= 2
            and bw >= 125
            and (avg_line_chars >= 26 or (same_style and bw >= 220))
        )

        if is_unified_block:
            full_txt = " ".join(s["text"] for s in all_segs).strip()
            max_sz = max(sizes, default=9.0)
            if not _should_translate_pdf_text(full_txt, max_sz):
                continue
            first_seg = all_segs[0]
            items.append(
                {
                    "id": len(items),
                    "bbox": bbox,
                    "max_x1": bbox[2] + 4.0,
                    "size": max_sz,
                    "color": first_seg["color"],
                    "bold": first_seg["bold"],
                    "text": full_txt,
                    "align": "left",
                    "redact_rects": [list(ln["bbox"]) for ln in lines],
                }
            )
        else:
            for segs in row_segments:
                for idx_s, seg in enumerate(segs):
                    if not _should_translate_pdf_text(seg["text"], seg["size"]):
                        continue
                    if idx_s + 1 < len(segs):
                        next_x0 = segs[idx_s + 1]["bbox"][0]
                        max_x1 = max(seg["bbox"][2] + 2.0, next_x0 - 3.0)
                    else:
                        max_x1 = min(
                            page.rect.width - 10.0,
                            max(seg["bbox"][2] + 25.0, bbox[2] + 4.0),
                        )
                    align = (
                        "right"
                        if (
                            len(segs) > 1
                            and idx_s == len(segs) - 1
                            and seg["bbox"][0] > page.rect.width * 0.55
                        )
                        else "left"
                    )
                    items.append(
                        {
                            "id": len(items),
                            "bbox": seg["bbox"],
                            "max_x1": max_x1,
                            "size": seg["size"],
                            "color": seg["color"],
                            "bold": seg["bold"],
                            "text": seg["text"],
                            "align": align,
                            "redact_rects": [seg["bbox"]],
                        }
                    )
    return items


def translate_pdf_preserve_layout_side_by_side(
    api_keys, stock, role_type, file_name, raw_pdf_bytes, deadline=None
):
    """
    原版排版 100% 保留的【左英文原版页面 + 右同版式中文原位替换页面】双拼 PDF 翻译引擎：
    - 保留原 PDF 的全部图片、临床图表、公司 Logo、配色、表格框线与坐标布局；
    - 页面上所有可翻译文本在原坐标框内原位替换为简体中文（不浓缩、不删减）；
    - 每一页左半侧为 100% 原始英文页面，右半侧为同版式中文页面，严格左右对照。
    """
    import pymupdf

    src_doc = pymupdf.open(stream=raw_pdf_bytes, filetype="pdf")
    trans_doc = pymupdf.open(stream=raw_pdf_bytes, filetype="pdf")

    all_page_items = {}
    payload_blocks = []
    for pno in range(len(trans_doc)):
        items = _extract_page_translatable_blocks(trans_doc[pno])
        all_page_items[pno] = items
        for it in items:
            payload_blocks.append((f"P{pno}_{it['id']}", it["text"]))

    if not payload_blocks:
        # 若为纯扫描图片 PDF（无可提取文本块），回退到多模态拆解并仅对右页叠加说明
        summary, bilingual_md = translate_and_extract_pdf(
            api_keys, stock, role_type, file_name, raw_pdf_bytes
        )
        return None, summary, bilingual_md

    # 按字符上限 (<= 10000 字符) 与块数上限 (<= 110 块) 智能动态分批，大幅减少 API 请求次数并加快长文档处理速度
    batches = []
    cur_batch = []
    cur_chars = 0
    for k, txt in payload_blocks:
        item_len = len(txt) + 16
        if cur_batch and (len(cur_batch) >= 110 or cur_chars + item_len > 10000):
            batches.append(cur_batch)
            cur_batch = []
            cur_chars = 0
        cur_batch.append((k, txt))
        cur_chars += item_len
    if cur_batch:
        batches.append(cur_batch)

    trans_map = {}
    for b_idx, batch in enumerate(batches):
        if deadline and time.time() > deadline:
            raise TimeoutError(
                f"Approaching job time limit on batch {b_idx+1}/{len(batches)} for {file_name}"
            )
        input_str = "\n".join(f"<<<{k}>>>\n{txt}" for k, txt in batch)
        prompt = (
            f"你是一位华尔街顶级生物医药分析师与专业同声传译。当前正在原位翻译 {stock['ticker']} ({stock['company']}) 的【{role_type}】文献（{file_name}）。\n"
            "请将以下带有 <<<ID>>> 标记的 PDF 英文文本块逐一完整精准地翻译为简体中文。\n"
            "核心要求：\n"
            "1. 必须保留原有的每个 <<<ID>>> 标记（格式严格为 <<<ID>>> 独占一行，紧跟对应的中文译文），严禁漏掉任何一个 ID，严禁输出任何开场白或额外解释！\n"
            "2. 严禁浓缩、严禁删减任何句子或临床数据，保留所有临床试验名称、药物名称、靶点、数字、货币符号。\n"
            "3. 纯人名与邮箱地址保持英文原样，职位、部门、表头、图表标题、免责声明等全部译为简明精准的中文。\n\n"
            f"{input_str}"
        )
        resp_text, used_model = call_gemini_with_rotation(
            api_keys, prompt, expect_json=False, deep_thinking=False
        )
        if not resp_text:
            raise RuntimeError(
                f"Gemini API returned empty response on batch {b_idx+1}/{len(batches)} for {file_name}; aborting to prevent untranslated PDF"
            )
        matches = re.findall(
            r"<<<(P\d+_\d+)>>>\s*([\s\S]*?)(?=(?:<<<P\d+_\d+>>>)|\Z)", resp_text
        )
        for k, v in matches:
            trans_map[k.strip()] = v.strip()
        print(
            f"[INFO] Translated batch {b_idx+1}/{len(batches)} ({len(matches)}/{len(batch)} blocks) via {used_model} for {file_name}"
        )

    min_required = max(1, int(len(payload_blocks) * 0.55))
    if len(trans_map) < min_required:
        raise RuntimeError(
            f"Insufficient translated blocks ({len(trans_map)}/{len(payload_blocks)}) for {file_name}; aborting upload"
        )

    for pno, items in all_page_items.items():
        page = trans_doc[pno]
        for it in items:
            key = f"P{pno}_{it['id']}"
            cn = trans_map.get(key, "").strip()
            if not cn or cn == it["text"].strip():
                continue
            for rrect in it["redact_rects"]:
                rx0, ry0, rx1, ry1 = rrect
                rh = max(1.0, ry1 - ry0)
                pad_y = min(1.5, rh * 0.18)
                page.add_redact_annot(
                    pymupdf.Rect(rx0, ry0 + pad_y, rx1, ry1 - pad_y), fill=False
                )
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
        )
        for it in items:
            key = f"P{pno}_{it['id']}"
            cn = trans_map.get(key, "").strip()
            if not cn or cn == it["text"].strip():
                continue
            x0, y0, x1, y1 = it["bbox"]
            if it["align"] == "right":
                rect = pymupdf.Rect(
                    max(10.0, x0 - 30.0),
                    max(0, y0 - 1.5),
                    x1 + 2.0,
                    min(page.rect.height - 2, y1 + 3.0),
                )
            else:
                rect = pymupdf.Rect(
                    x0,
                    max(0, y0 - 1.5),
                    it["max_x1"],
                    min(page.rect.height - 2, y1 + 3.0),
                )
            esc_cn = html.escape(cn).replace("\n", "<br/>")
            weight = "bold" if it["bold"] else "normal"
            font_sz = max(5.5, min(16.0, it["size"] * 0.94))
            css = (
                f"* {{ font-family: sans-serif; font-size: {font_sz:.1f}pt; "
                f"color: {it['color']}; font-weight: {weight}; text-align: {it['align']}; line-height: 1.24; margin: 0; padding: 0; }}"
            )
            page.insert_htmlbox(rect, f"<div>{esc_cn}</div>", css=css, scale_low=0.35)

    dual_doc = pymupdf.open()
    for pno in range(len(src_doc)):
        r = src_doc[pno].rect
        w, h = r.width, r.height
        out_page = dual_doc.new_page(width=w * 2, height=h)
        out_page.show_pdf_page(pymupdf.Rect(0, 0, w, h), src_doc, pno)
        out_page.show_pdf_page(pymupdf.Rect(w, 0, w * 2, h), trans_doc, pno)
        out_page.draw_line(
            pymupdf.Point(w, 0),
            pymupdf.Point(w, h),
            color=(0.75, 0.78, 0.82),
            width=1.0,
        )

    try:
        dual_doc.subset_fonts()
    except Exception:
        pass
    dual_pdf_bytes = dual_doc.tobytes(garbage=4, deflate=True, deflate_fonts=True)

    # 提炼核心摘要供 Vault 表格与微信推送展示
    sample_cn = "\n".join(
        trans_map.get(k, "") for k, _ in payload_blocks[:45] if trans_map.get(k)
    )[:12000]
    summary_prompt = (
        f"请根据以下 {stock['ticker']} ({stock['company']}) 的【{role_type}】（{file_name}）译文内容，"
        f"用 250 字以内精炼概括其核心临床数据/里程碑节点与投行核心预期基准（严禁敏感交易荐股词汇）：\n\n{sample_cn}"
    )
    summary_text, _ = call_gemini_with_rotation(
        api_keys, summary_prompt, expect_json=False, deep_thinking=False
    )
    return dual_pdf_bytes, (summary_text or sample_cn[:350]), ""


def upload_pdf_bytes_to_drive(
    access_token, output_folder_id, base_title, pdf_bytes, existing_file_id=None
):
    """直接将二进制 PDF 上传（或覆盖更新）到指定文件夹，命名为 <源文件名>[译].pdf"""
    boundary = "===AntigravityVaultBoundary==="
    clean_base = re.sub(r"(\[译\]|【译】)+$", "", base_title.strip())
    pdf_meta = {
        "name": f"{clean_base}[译].pdf",
        "mimeType": "application/pdf",
    }
    if not existing_file_id:
        pdf_meta["parents"] = [output_folder_id]

    pdf_multipart = (
        (
            f"--{boundary}\r\n"
            f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(pdf_meta, ensure_ascii=False)}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: application/pdf\r\n\r\n"
        ).encode("utf-8")
        + pdf_bytes
        + f"\r\n--{boundary}--\r\n".encode("utf-8")
    )

    if existing_file_id:
        upload_url = f"https://www.googleapis.com/upload/drive/v3/files/{existing_file_id}?uploadType=multipart&fields=id,name,webViewLink"
        method = "PATCH"
    else:
        upload_url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,name,webViewLink"
        method = "POST"

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
                method=method,
            )
            with urllib.request.urlopen(req_pdf, timeout=90) as resp:
                pdf_info = json.loads(resp.read().decode("utf-8"))
            if pdf_info and "id" in pdf_info:
                break
        except urllib.error.HTTPError as e:
            if attempt < 2 and e.code in (500, 502, 503, 504):
                time.sleep(3 * (attempt + 1))
                continue
            raise

    pdf_link = pdf_info.get(
        "webViewLink", f"https://drive.google.com/file/d/{pdf_info['id']}/view"
    )
    return pdf_link, pdf_link


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

    clean_base = re.sub(r"(\[译\]|【译】)+$", "", base_title.strip())
    pdf_meta = {
        "name": f"{clean_base}[译].pdf",
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


def sync_drive_vault(
    access_token,
    config,
    portfolio,
    send_pushplus_fn=None,
    deadline=None,
    max_files_per_pass=1,
):
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
    existing_sync_names = {f["name"] for f in sync_files}
    for fmeta in sync_files:
        if processed_count >= max_files_per_pass:
            break
        if deadline and time.time() > deadline:
            break
        if is_translated_or_temp_file(fmeta["name"]):
            continue
        base_name = os.path.splitext(fmeta["name"])[0]
        if f"{base_name}[译].pdf" in existing_sync_names:
            continue
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

            # 直接在原始目录生成 <源文件名>[译].pdf
            combined_notes = "\n\n".join(
                f"【{k}】{v}" for k, v in per_notes.items()
            ) or "已同步网站催化剂与共识底稿"
            styled_html = markdown_to_styled_html(
                f"网站催化剂与预期共识底稿 ({base_name})",
                f"来源：00_网站与催化剂底稿同步 ｜ 原始文件：{fmeta['name']}",
                combined_notes,
                bilingual_md,
            )
            pdf_link, doc_link = create_bilingual_doc_and_pdf_in_drive(
                access_token, vault_tree["sync_folder"]["id"], base_name, styled_html
            )
            existing_sync_names.add(f"{base_name}[译].pdf")

            for tk in matched:
                tk_up = str(tk).upper()
                tk_note = per_notes.get(tk_up) or next(iter(per_notes.values()), "已同步网站催化剂与共识底稿")
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
                        f"<div style='font-family:sans-serif;line-height:1.68;font-size:14px;'>"
                        f"<p>已自动解析您上传的网站/催化剂底稿 <b>{html.escape(fmeta['name'])}</b> 并于同目录生成 <b>{html.escape(base_name)}[译].pdf</b>：</p>"
                        f"<div style='background:rgba(128,128,128,0.12);border-left:4px solid #f59e0b;padding:10px 12px;margin:10px 0;border-radius:4px;white-space:pre-wrap;'>{html.escape(tk_note)}</div>"
                        f"<p><a href='{pdf_link}'>📄 点击打开/下载 {html.escape(base_name)}[译].pdf</a></p>"
                        f"</div>",
                    )
            processed_vault_ids.add(v_id)
            processed_count += 1
        except Exception as e:
            print(f"[WARN] Failed syncing website artifact {fmeta['name']}: {e}")

    # 2. 巡检各持仓标的的 01_公司内部官方口径、02_外部投行与独立研报（以及标的根目录直接放入的文件）
    for ticker, s_folders in vault_tree["stocks"].items():
        stock = s_folders["stock"]
        for role_key, role_label in [
            ("internal", "公司内部官方口径 (Deck/Transcript)"),
            ("external", "外部投行与独立研报 (Sell-Side)"),
            ("folder", "标的主目录直接上传文档"),
        ]:
            folder_id = s_folders[role_key]["id"]
            files = list_folder_files(access_token, folder_id)
            existing_names = {f["name"] for f in files}
            for fmeta in files:
                if processed_count >= max_files_per_pass:
                    break
                if deadline and time.time() > deadline:
                    break
                fname = fmeta["name"]
                if is_translated_or_temp_file(fname):
                    continue
                base_name = os.path.splitext(fname)[0]
                if f"{base_name}[译].pdf" in existing_names:
                    continue
                v_id = f"VAULT_{fmeta['id']}_{fmeta.get('modifiedTime', '')[:16]}"
                if v_id in processed_vault_ids:
                    continue
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

                    dual_pdf_bytes = None
                    summary = ""
                    bilingual_md = ""
                    if is_pdf:
                        raw_bytes = download_drive_file_bytes(access_token, fmeta)
                        dual_pdf_bytes, summary, bilingual_md = translate_pdf_preserve_layout_side_by_side(
                            api_keys, stock, role_label, fname, raw_bytes, deadline=deadline
                        )
                    elif is_office:
                        pdf_bytes = convert_office_file_to_pdf_bytes(access_token, fmeta)
                        dual_pdf_bytes, summary, bilingual_md = translate_pdf_preserve_layout_side_by_side(
                            api_keys, stock, role_label, fname, pdf_bytes, deadline=deadline
                        )
                    else:
                        raw_bytes = download_drive_file_bytes(access_token, fmeta)
                        text_content = raw_bytes.decode("utf-8", errors="replace")
                        prompt = f"""请将以下 {ticker} ({stock['company']}) 的【{role_label}】文档（{fname}）逐段翻译为严格左右双栏中英对照格式（每段严格使用 [EN] 英文原文 与 [CN] 中文精译 标签成对输出），并在开头提炼 300 字以内的核心要点（区分内部承诺或外部投行预期阈值）：\n\n{text_content[:25000]}"""
                        bilingual_md, _ = call_gemini_with_rotation(
                            api_keys, prompt, expect_json=False, deep_thinking=True
                        )
                        summary = (bilingual_md or "")[:400]

                    if dual_pdf_bytes:
                        pdf_link, doc_link = upload_pdf_bytes_to_drive(
                            access_token,
                            folder_id,
                            base_name,
                            dual_pdf_bytes,
                        )
                    else:
                        if not bilingual_md or len(bilingual_md.strip()) < 80 or "文档解析为空" in bilingual_md:
                            print(f"[WARN] Skipping PDF creation for {fname}: translation empty or invalid.")
                            continue
                        styled_html = markdown_to_styled_html(
                            f"[{ticker}] {base_name} (中英对照精译版)",
                            f"标的：{ticker} ({stock['company']}) ｜ 口径分类：{role_label} ｜ 原始文件：{fname}",
                            summary,
                            bilingual_md,
                        )
                        pdf_link, doc_link = create_bilingual_doc_and_pdf_in_drive(
                            access_token,
                            folder_id,
                            base_name,
                            styled_html,
                        )
                    existing_names.add(f"{base_name}[译].pdf")
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
                            f"<div style='font-family:sans-serif;line-height:1.68;font-size:14px;'>"
                            f"<p><b>标的：</b>{ticker} ({stock['company']})<br/><b>口径分类：</b>{role_label}<br/><b>原始文件：</b>{html.escape(fname)}<br/><b>生成译稿：</b>{html.escape(base_name)}[译].pdf（已保存在同文件夹内）</p>"
                            f"<div style='background:rgba(128,128,128,0.12);border-left:4px solid #f59e0b;padding:10px 12px;margin:10px 0;border-radius:4px;white-space:pre-wrap;font-size:14px;'><b>【核心结构化提炼】</b>\n{html.escape(summary)}</div>"
                            f"<p style='margin-top:10px;'><a href='{pdf_link}'>👉 点击打开/下载 {html.escape(base_name)}[译].pdf</a></p>"
                            f"</div>",
                        )
                    processed_vault_ids.add(v_id)
                    processed_count += 1
                except Exception as e:
                    print(f"[WARN] Vault file process failed for {fname}: {e}")

    return knowledge_by_ticker
