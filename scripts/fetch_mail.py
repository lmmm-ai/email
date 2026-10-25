#!/usr/bin/env python3
"""QQ 邮箱取信 + 正文清洗 —— 第 2 步。

第 1 步只打印标题；这一步把正文取出来洗干净，为第 3 步的 LLM 分类做准备。

做什么：
  1. UID 游标增量取信（逻辑同第 1 步，没动）
  2. 取正文：多部分 MIME 优先 text/plain，没有就把 HTML 转文本
  3. 清洗：剥掉引用的历史回复、签名档，折叠空白
  4. 打标签：收件人角色（to/cc）、是否噪音（通知/营销/自动回复）、附件数
  5. 统计：原 N 字符 -> 清洗后 M 字符，剥掉多少行

不做什么：不调用 LLM、不写数据库、不改邮件已读状态（EXAMINE + BODY.PEEK）。

只依赖 Python 3.9+ 标准库。

本地试跑：
  set QQ_EMAIL=1032867463@qq.com
  set QQ_AUTH_CODE=16位授权码

  # 预览已有邮件（不动游标）
  python scripts/fetch_mail.py --uid 34,36,38 --preview 300

  # 正常增量
  python scripts/fetch_mail.py --state state/mail_state.json --limit 5
"""

from __future__ import annotations

import argparse
import email
import html.parser
import imaplib
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.parser import BytesHeaderParser
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

DEFAULT_HOST = "imap.qq.com"
DEFAULT_PORT = 993
DEFAULT_MAX_BYTES = 1024 * 1024  # 单封最多取 1MB，超出只取前 1MB
CST = timezone(timedelta(hours=8))

# QQ 邮箱（以及部分国内邮箱）要求客户端登录后用 ID 命令声明身份，
# 否则 SELECT 会返回 "Unsafe Login"。imaplib 默认不认识 ID 命令，
# 必须先把它注册进去，否则 _command() 会直接抛 "command ID illegal"。
imaplib.Commands["ID"] = ("AUTH", "SELECTED")

HEADER_FIELDS = "FROM TO CC DATE SUBJECT MESSAGE-ID"

log = logging.getLogger("mailbot")


# --------------------------------------------------------------------------- #
# 状态文件
# --------------------------------------------------------------------------- #
def empty_state() -> dict:
    return {
        "version": 2,
        "uidvalidity": None,
        "last_uid": 0,
        "initialized": False,
        "last_run": None,
    }


def load_state(path: Path) -> dict:
    if not path.exists():
        log.info("状态文件不存在，按首次运行处理：%s", path)
        return empty_state()
    try:
        data = json.loads(path.read_text("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state 不是 JSON 对象")
        return {**empty_state(), **data}
    except Exception as exc:  # 状态坏了就重新初始化，不要让整条链挂掉
        log.warning("状态文件无法解析（%s），按首次运行处理", exc)
        return empty_state()


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", "utf-8")
    tmp.replace(path)  # 原子替换，避免半截文件


# --------------------------------------------------------------------------- #
# IMAP 操作
# --------------------------------------------------------------------------- #
def send_client_id(conn: imaplib.IMAP4_SSL, address: str) -> None:
    """QQ 邮箱必需的 ID 声明。失败只警告，让后面的 SELECT 决定是否真报错。"""
    args = (
        '("name" "mailbot" "version" "1.0.0" '
        f'"vendor" "mailbot" "support-email" "{address}")'
    )
    try:
        conn._simple_command("ID", args)
        conn._untagged_response("OK", [None], "ID")
        log.info("已发送 IMAP ID 声明")
    except Exception as exc:
        log.warning("ID 命令失败，后续 SELECT 可能被拒: %s", exc)


def select_readonly(conn: imaplib.IMAP4_SSL, folder: str) -> int | None:
    """以只读方式打开信箱，返回 UIDVALIDITY。"""
    typ, data = conn.select(folder, readonly=True)
    if typ != "OK":
        raise RuntimeError(f"SELECT {folder} 失败: {data!r}")
    typ, raw = conn.response("UIDVALIDITY")
    try:
        return int(raw[0])
    except Exception:
        return None


def search_uids(conn: imaplib.IMAP4_SSL, last_uid: int) -> list[int]:
    """返回 last_uid 之后的所有 UID（升序）。"""
    if last_uid <= 0:
        typ, data = conn.uid("SEARCH", "ALL")
    else:
        typ, data = conn.uid("SEARCH", "UID", f"{last_uid + 1}:*")
    if typ != "OK":
        raise RuntimeError(f"UID SEARCH 失败: {data!r}")

    raw = (data[0] or b"") if data else b""
    uids = [int(x) for x in raw.split()]
    # 关键坑：IMAP 的 "n:*" 在没有匹配时会返回最后一封邮件，
    # 所以必须按 last_uid 再过滤一次，否则每次都会重复看到同一封。
    return sorted(u for u in uids if u > last_uid)


def fetch_sizes(conn: imaplib.IMAP4_SSL, uids: list[int]) -> dict[int, int]:
    """一次 FETCH 拿到整批的字节大小，用来决定要不要截断。"""
    if not uids:
        return {}
    typ, data = conn.uid("FETCH", ",".join(str(u) for u in uids), "(UID RFC822.SIZE)")
    sizes: dict[int, int] = {}
    for item in data or []:
        if not isinstance(item, bytes):
            continue
        um = re.search(rb"UID (\d+)", item)
        sm = re.search(rb"RFC822\.SIZE (\d+)", item)
        if um and sm:
            sizes[int(um.group(1))] = int(sm.group(1))
    return sizes


def fetch_body(
    conn: imaplib.IMAP4_SSL, uid: int, size: int, max_bytes: int
) -> tuple[bytes | None, bool]:
    """取整封邮件。BODY.PEEK 保证不会把邮件标成已读。

    超过 max_bytes 时用 "<0.N>" 只取前 N 字节；头部在邮件最前面，
    所以标题/发件人仍然完整，只是正文可能被切断。
    """
    truncated = size > max_bytes
    spec = f"(BODY.PEEK[]<0.{max_bytes}>)" if truncated else "(BODY.PEEK[])"
    typ, data = conn.uid("FETCH", str(uid), spec)
    if typ != "OK" or not data or not isinstance(data[0], tuple) or len(data[0]) < 2:
        return None, truncated
    return data[0][1], truncated


# --------------------------------------------------------------------------- #
# 解码
# --------------------------------------------------------------------------- #
def _decode_bytes(data: bytes, charset: str | None) -> str:
    """按声明编码解，失败依次退回 UTF-8 / GB18030。"""
    declared = (charset or "").lower().strip()
    if declared in ("", "unknown-8bit", "x-unknown", "unknown", "8bit", "none"):
        # "unknown-8bit" 意味着客户端没声明编码，UTF-8 和 GBK 都可能，
        # 先试 UTF-8（能成功就基本不会误判），再退 GB18030。
        declared = None
    for enc in (declared, "utf-8", "gb18030"):
        if not enc:
            continue
        try:
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", "replace")


def _fix_surrogates(text: str) -> str:
    """还原未按 RFC2047 编码的原始 8bit 头。

    compat32 策略会把裸字节变成 U+DC80–U+DCFF 之间的代理字符，
    必须拿回原始字节再按 GB18030/UTF-8 解，否则中文就是一堆方块。
    """
    if not any(0xDC80 <= ord(ch) <= 0xDCFF for ch in text):
        return text
    try:
        raw = text.encode("utf-8", "surrogateescape")
    except Exception:
        return text
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def decode_mime(raw: str | bytes | None) -> str:
    """解码 RFC2047 头（=?GBK?B?...?= 这类），国内邮件大量使用。"""
    if not raw:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "surrogateescape")
    parts: list[str] = []
    for chunk, charset in decode_header(raw):
        if isinstance(chunk, str):
            parts.append(_fix_surrogates(chunk))
        elif charset is None:
            # 坑：对"没有编码的普通段落"，标准库会做 bytes(word, 'raw-unicode-escape')，
            # 于是中文变成 ASCII 的 "\u4f1a" 字面量。必须按原样反解回来，
            # 否则日志里打印的就是一堆 \uXXXX。
            try:
                parts.append(_fix_surrogates(chunk.decode("raw-unicode-escape")))
            except Exception:
                parts.append(chunk.decode("latin-1"))
        else:
            parts.append(_decode_bytes(chunk, charset))
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def format_date(raw: str | None) -> str:
    if not raw:
        return "?"
    try:
        dt = parsedate_to_datetime(raw)
        if dt is None:
            return raw.strip()[:25]
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(CST).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return raw.strip()[:25]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# HTML -> 纯文本（标准库够用，不引第三方）
# --------------------------------------------------------------------------- #
_SKIP_TAGS = {"script", "style", "head", "title", "noscript", "iframe", "svg", "template"}
_BLOCK_TAGS = {
    "p", "div", "br", "tr", "li", "ul", "ol", "table", "tbody", "thead",
    "section", "article", "header", "footer", "blockquote", "pre",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr", "td", "th", "dl", "dt", "dd",
}


class _HTMLToText(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            return
        if tag == "br":
            self.parts.append("\n")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS and tag != "br":
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip == 0:
            self.parts.append(data)


def html_to_text(raw: str) -> str:
    """HTML 转纯文本。转义字符由 convert_charrefs 处理。"""
    parser = _HTMLToText()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:  # 残缺 HTML 不该让整封邮件失败
        pass
    return "".join(parser.parts)


# --------------------------------------------------------------------------- #
# 引用 / 签名剥离
# --------------------------------------------------------------------------- #
# 整段引用的"起点"标记：命中就从这里切断，后面全是历史回复
_QUOTE_CUT = [
    re.compile(r"^-{2,}\s*(原始邮件|original message|forwarded message|转发的邮件)\s*-{2,}", re.I),
    re.compile(r"^在\s*.{2,60}写道\s*[:：]\s*$"),
    re.compile(r"^on\s.{4,80}\s+wrote:?\s*$", re.I),
    re.compile(r"^-{8,}\s*$"),
    re.compile(r"^_{15,}\s*$"),
]
# 邮件头块的开头（Outlook / QQ 网页版回复时会把原信头贴上来）
_HEADER_BLOCK_START = re.compile(r"^(发件人|from)\s*[:：]\s*\S", re.I)
_HEADER_BLOCK_HINTS = ("发送时间", "收件人", "主题", "date:", "subject:", "to:", "cc:", "sent:")
# 签名起点
_SIGNATURE_MARKS = [
    re.compile(r"^--\s*$"),
    re.compile(r"^(sent from my|发自我的|发送自|from my iphone)", re.I),
]


def _is_quote_marker(lines: list[str], idx: int) -> bool:
    line = lines[idx].strip()
    for pat in _QUOTE_CUT:
        if pat.match(line):
            return True
    if _HEADER_BLOCK_START.match(line):
        # 单看 "From:" 太容易误杀，要求后面几行确实是邮件头
        window = " ".join(x.strip().lower() for x in lines[idx : idx + 4])
        return any(hint in window for hint in _HEADER_BLOCK_HINTS)
    return False


def strip_quoted(text: str) -> tuple[str, int]:
    """剥掉引用的历史回复，返回 (干净文本, 丢掉的行数)。"""
    lines = text.splitlines()
    cut_at = None
    for idx, line in enumerate(lines):
        if idx == 0 or not line.strip():
            continue
        if _is_quote_marker(lines, idx):
            cut_at = idx
            break

    removed = 0
    if cut_at is not None:
        removed += len(lines) - cut_at
        lines = lines[:cut_at]

    kept: list[str] = []
    for line in lines:
        # 没有整段标记时，逐行去掉 ">" 引用（行内回复的场景）
        if line.lstrip().startswith(">"):
            removed += 1
            continue
        kept.append(line)
    return "\n".join(kept), removed


def strip_signature(text: str) -> tuple[str, int]:
    """剥掉签名档，返回 (干净文本, 丢掉的行数)。"""
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if idx < 2:  # 太靠前的不当签名，避免误杀正文
            continue
        stripped = line.strip()
        if any(pat.match(stripped) for pat in _SIGNATURE_MARKS):
            return "\n".join(lines[:idx]), len(lines) - idx
    return text, 0


def normalize_ws(text: str) -> str:
    text = text.replace("\u200b", "").replace("\ufeff", "").replace("\xa0", " ")
    lines = [line.rstrip() for line in text.splitlines()]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# --------------------------------------------------------------------------- #
# 正文提取 + 噪音判定
# --------------------------------------------------------------------------- #
def _iter_leaf_parts(part: email.message.Message):
    """遍历"真正的正文叶子节点"。

    不能直接用 walk()：message/rfc822（被转发的整封邮件）是 multipart，
    walk() 会钻进去，把别人历史邮件的正文算成这一封的正文。
    """
    if part.get_content_maintype() == "message":
        return
    if part.is_multipart():
        for sub in part.get_payload():
            yield from _iter_leaf_parts(sub)
        return
    yield part


def extract_body(msg: email.message.Message) -> tuple[str, str, int]:
    """返回 (原始正文, 类型 plain/html/none, 附件数)。"""
    plain: list[str] = []
    html_parts: list[str] = []
    attachments = 0

    for part in _iter_leaf_parts(msg):
        disp = str(part.get("Content-Disposition") or "").lower()
        if "attachment" in disp:
            attachments += 1
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        text = _decode_bytes(payload, part.get_content_charset())
        if ctype == "text/plain":
            plain.append(text)
        else:
            html_parts.append(text)

    if plain:
        return "\n".join(plain), "plain", attachments
    if html_parts:
        return html_to_text("\n".join(html_parts)), "html", attachments
    return "", "none", attachments


_BULK_SENDER = re.compile(
    r"^(no-?reply|do-?not-?reply|noreply|notification|notifications|notify|"
    r"mailer-daemon|postmaster|bounce|bounces|newsletter|marketing)",
    re.I,
)
# QQ 官方通知地址（收件箱里最常见的"不用管"）
_SYSTEM_SENDERS = {
    "10000@qq.com",
    "10000@vip.qq.com",
    "admin@qq.com",
    "no-reply@qq.com",
    "service@mail.qq.com",
}


def noise_reasons(msg: email.message.Message, sender_addr: str) -> list[str]:
    """命中的越多越像"不用管"的邮件。第 3 步的 LLM 看到这个能少犯错。"""
    reasons: list[str] = []
    if msg.get("List-Unsubscribe"):
        reasons.append("List-Unsubscribe")
    prec = str(msg.get("Precedence") or "").strip().lower()
    if prec in ("bulk", "list", "junk"):
        reasons.append(f"Precedence={prec}")
    auto = str(msg.get("Auto-Submitted") or "").strip().lower()
    if auto and auto != "no":
        reasons.append(f"Auto-Submitted={auto}")
    if msg.get("X-Auto-Response-Suppress"):
        reasons.append("Auto-Response-Suppress")
    local = sender_addr.split("@")[0] if "@" in sender_addr else sender_addr
    if _BULK_SENDER.match(local):
        reasons.append(f"发件人={local}")
    if sender_addr.strip().lower() in _SYSTEM_SENDERS:
        reasons.append("QQ官方通知")
    return reasons


def recipient_role(msg: email.message.Message, me: str) -> str:
    """我在 To 还是 Cc——这是最便宜也最强的紧急度信号。"""
    target = me.lower()
    to = str(msg.get("To") or "").lower()
    cc = str(msg.get("Cc") or "").lower()
    if target and target in to:
        return "to"
    if target and target in cc:
        return "cc"
    return "?"


# --------------------------------------------------------------------------- #
# 单封处理
# --------------------------------------------------------------------------- #
def process(conn: imaplib.IMAP4_SSL, uid: int, size: int, args, me: str) -> dict | None:
    raw, truncated = fetch_body(conn, uid, size, args.max_bytes)
    if raw is None:
        return None
    msg = email.message_from_bytes(raw)

    sender_name, sender_addr = parseaddr(decode_mime(msg.get("From")))
    sender = f"{sender_name} <{sender_addr}>" if sender_name else sender_addr or "(未知发件人)"

    body_raw, kind, attachments = extract_body(msg)
    body, removed_quote = strip_quoted(body_raw)
    body, removed_sig = strip_signature(body)
    body = normalize_ws(body)

    return {
        "uid": uid,
        "size": size,
        "truncated": truncated,
        "subject": decode_mime(msg.get("Subject")) or "(无主题)",
        "sender": sender,
        "sender_addr": sender_addr,
        "date": format_date(msg.get("Date")),
        "role": recipient_role(msg, me),
        "noise": noise_reasons(msg, sender_addr),
        "kind": kind,
        "attachments": attachments,
        "chars": len(body),
        "removed_quote": removed_quote,
        "removed_sig": removed_sig,
        "body": body,
    }


def report(rec: dict, index: int, total: int, preview: int, max_bytes: int) -> None:
    print(
        f"[{index}/{total}] UID={rec['uid']}  {rec['date']}  {rec['role']:<2}  "
        f"{rec['sender']}  |  {rec['subject']}",
        flush=True,
    )
    bits: list[str] = []
    if rec["kind"] == "none":
        bits.append("无正文")
    else:
        bits.append(f"正文 {rec['chars']} 字（{rec['kind']}）")
    if rec["removed_quote"]:
        bits.append(f"剥引用 {rec['removed_quote']} 行")
    if rec["removed_sig"]:
        bits.append(f"剥签名 {rec['removed_sig']} 行")
    if rec["attachments"]:
        bits.append(f"附件 {rec['attachments']}")
    if rec["truncated"]:
        bits.append(f"超 {max_bytes // 1024}KB 已截断")
    if rec["noise"]:
        bits.append("噪声=" + "/".join(rec["noise"]))
    print("      " + "，".join(bits), flush=True)

    if preview > 0 and rec["body"]:
        flat = rec["body"].replace("\n", " ")
        flat = re.sub(r"\s{2,}", " ", flat).strip()
        suffix = "…" if len(flat) > preview else ""
        print("      " + flat[:preview] + suffix, flush=True)
    print("", flush=True)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_uid_list(raw: str) -> list[int]:
    out: list[int] = []
    for piece in re.split(r"[,\s]+", raw.strip()):
        if not piece:
            continue
        if "-" in piece:
            lo, hi = piece.split("-", 1)
            out.extend(range(int(lo), int(hi) + 1))
        else:
            out.append(int(piece))
    return sorted(set(out))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="QQ 邮箱取信 + 正文清洗")
    p.add_argument("--state", default="state/mail_state.json", help="游标文件路径")
    p.add_argument("--limit", type=int, default=20, help="本次最多处理多少封")
    p.add_argument("--folder", default="INBOX", help="信箱名，默认 INBOX")
    p.add_argument(
        "--uid",
        default="",
        help="调试用：强制处理指定 UID（如 34,36 或 30-38），不读写游标",
    )
    p.add_argument("--preview", type=int, default=200, help="正文预览字数，0 为关闭")
    p.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help="单封最多取多少字节，超出截断",
    )
    p.add_argument(
        "--host", default=os.environ.get("IMAP_HOST", DEFAULT_HOST), help="IMAP 服务器"
    )
    p.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("IMAP_PORT", DEFAULT_PORT)),
        help="IMAP 端口",
    )
    return p.parse_args()


def run(args: argparse.Namespace) -> int:
    if args.limit < 1:
        log.error("--limit 必须 >= 1，当前是 %s", args.limit)
        return 2

    email_addr = (os.environ.get("QQ_EMAIL") or "").strip()
    auth_code = (os.environ.get("QQ_AUTH_CODE") or "").strip()
    if not email_addr or not auth_code:
        log.error("缺少环境变量 QQ_EMAIL 或 QQ_AUTH_CODE")
        return 2

    debug_uids = parse_uid_list(args.uid) if args.uid.strip() else []
    state_path = Path(args.state)
    state = load_state(state_path)
    conn: imaplib.IMAP4_SSL | None = None

    try:
        log.info("连接 %s:%s（账号 %s）", args.host, args.port, email_addr)
        conn = imaplib.IMAP4_SSL(args.host, args.port, timeout=30)
        conn.login(email_addr, auth_code)
        log.info("登录成功")
        send_client_id(conn, email_addr)

        uidvalidity = select_readonly(conn, args.folder)

        # ---- 决定这一轮处理哪些 UID ----
        if debug_uids:
            batch = debug_uids[: args.limit]
            log.info("调试模式：强制处理 UID %s（不改游标）", batch)
            first_run = False
        else:
            if state["uidvalidity"] not in (None, uidvalidity):
                log.warning(
                    "UIDVALIDITY 变化 %s -> %s，信箱可能被重建，游标重新初始化",
                    state["uidvalidity"],
                    uidvalidity,
                )
                state["last_uid"] = 0
                state["initialized"] = False

            first_run = not state["initialized"]
            last_uid = int(state["last_uid"] or 0)
            uids = search_uids(conn, last_uid)
            log.info("当前游标 last_uid=%d，发现 %d 封未处理邮件", last_uid, len(uids))

            if not uids:
                log.info("没有新邮件，游标不变")
                log.info("想预览已有邮件怎么洗，用：--uid 34,36,38 --preview 300")
                state.update(uidvalidity=uidvalidity, last_run=now_iso(), initialized=True)
                save_state(state_path, state)
                return 0

            if first_run:
                # 首次运行只做冒烟测试：看最新 N 封，并把游标推到信箱最大 UID，
                # 历史邮件不回溯，避免第一次就吐几千封。
                uids = uids[-args.limit :]
                log.info("首次运行：只处理最新 %d 封，更早的历史邮件跳过", len(uids))

            batch = uids[: args.limit]
            if len(uids) > len(batch):
                log.info(
                    "本次配额 %d 封，剩余 %d 封留给下次运行", args.limit, len(uids) - len(batch)
                )

        if not batch:
            log.info("没有要处理的邮件")
            return 0

        sizes = fetch_sizes(conn, batch)
        print("", flush=True)

        records: list[dict] = []
        for idx, uid in enumerate(batch, 1):
            size = sizes.get(uid, 0)
            rec = process(conn, uid, size, args, email_addr)
            if rec is None:
                log.warning("[%d/%d] UID=%s 取信失败，跳过", idx, len(batch), uid)
                continue
            records.append(rec)
            report(rec, idx, len(batch), args.preview, args.max_bytes)

        # ---- 汇总 ----
        noise = sum(1 for r in records if r["noise"])
        with_body = sum(1 for r in records if r["kind"] != "none" and r["chars"] > 0)
        empty = sum(1 for r in records if r["kind"] == "none" or r["chars"] == 0)
        total_chars = sum(r["chars"] for r in records)
        total_raw = sum(r["size"] for r in records)
        log.info(
            "本次 %d 封：噪音 %d，有正文 %d，空正文 %d；正文合计 %d 字，原始体积 %d KB",
            len(records),
            noise,
            with_body,
            empty,
            total_chars,
            total_raw // 1024,
        )

        if debug_uids:
            log.info("调试模式结束，游标保持 last_uid=%s 未改动", state["last_uid"])
            return 0

        state.update(
            uidvalidity=uidvalidity,
            last_uid=max(batch),
            last_run=now_iso(),
            initialized=True,
        )
        save_state(state_path, state)
        log.info("游标推进到 last_uid=%d，状态已写入 %s", max(batch), state_path)
        return 0

    except imaplib.IMAP4.error as exc:
        text = str(exc)
        if "Unsafe Login" in text or "LIMIT" in text:
            log.error(
                "QQ 拒绝登录（%s）。常见原因：未发送 IMAP ID 声明、"
                "用了 QQ 密码而不是授权码、或登录过于频繁被临时限流。",
                text,
            )
        else:
            log.error("IMAP 错误: %s", text)
        return 3
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        # 本地 Windows 控制台默认 GBK，强制 UTF-8 才打得中文标题
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    return run(parse_args())


if __name__ == "__main__":
    sys.exit(main())
