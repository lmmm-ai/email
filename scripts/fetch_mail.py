#!/usr/bin/env python3
"""QQ 邮箱 IMAP 增量取信 —— 第 1 步：只打印新邮件标题。

刻意只做三件事：
  1. 用 UID 游标判断"哪些是新的"（不靠时间窗口）
  2. 只取邮件头（不取正文，更不落盘）
  3. 只读方式打开信箱（EXAMINE + BODY.PEEK，绝不把邮件标成已读）

依赖：仅 Python 3.9+ 标准库，无需 pip install。

本地试跑：
  set QQ_EMAIL=你的QQ号@qq.com
  set QQ_AUTH_CODE=16位授权码
  python scripts/fetch_mail.py --state state/mail_state.json --limit 5
"""

from __future__ import annotations

import argparse
import imaplib
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.parser import BytesHeaderParser
from email.utils import parsedate_to_datetime
from pathlib import Path

DEFAULT_HOST = "imap.qq.com"
DEFAULT_PORT = 993
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
        "version": 1,
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


def fetch_header(conn: imaplib.IMAP4_SSL, uid: int) -> bytes | None:
    """只取邮件头。BODY.PEEK 保证不会把邮件标成已读。"""
    typ, data = conn.uid(
        "FETCH", str(uid), f"(UID BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])"
    )
    if typ != "OK" or not data:
        return None
    if not isinstance(data[0], tuple) or len(data[0]) < 2:
        return None
    return data[0][1]


# --------------------------------------------------------------------------- #
# 解码
# --------------------------------------------------------------------------- #
def _decode_bytes(data: bytes, charset: str | None) -> str:
    """按声明编码解，失败依次退回 UTF-8 / GB18030。"""
    declared = (charset or "").lower().strip()
    if declared in ("", "unknown-8bit", "x-unknown", "unknown", "8bit", "none"):
        # "unknown-8bit" 意味着 mail 客户端没声明编码，UTF-8 和 GBK 都可能，
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
# 主流程
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="QQ 邮箱增量取信（只打印标题）")
    p.add_argument("--state", default="state/mail_state.json", help="游标文件路径")
    p.add_argument("--limit", type=int, default=20, help="本次最多处理多少封")
    p.add_argument("--folder", default="INBOX", help="信箱名，默认 INBOX")
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

    email = (os.environ.get("QQ_EMAIL") or "").strip()
    auth_code = (os.environ.get("QQ_AUTH_CODE") or "").strip()
    if not email or not auth_code:
        log.error("缺少环境变量 QQ_EMAIL 或 QQ_AUTH_CODE")
        return 2

    state_path = Path(args.state)
    state = load_state(state_path)
    conn: imaplib.IMAP4_SSL | None = None

    try:
        log.info("连接 %s:%s（账号 %s）", args.host, args.port, email)
        conn = imaplib.IMAP4_SSL(args.host, args.port, timeout=30)
        conn.login(email, auth_code)
        log.info("登录成功")
        send_client_id(conn, email)

        uidvalidity = select_readonly(conn, args.folder)
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
            log.info("本次配额 %d 封，剩余 %d 封留给下次运行", args.limit, len(uids) - len(batch))

        print("", flush=True)
        for idx, uid in enumerate(batch, 1):
            header = fetch_header(conn, uid)
            if header is None:
                log.warning("[%d/%d] UID=%s 取邮件头失败，跳过", idx, len(batch), uid)
                continue
            msg = BytesHeaderParser().parsebytes(header)
            subject = decode_mime(msg.get("Subject")) or "(无主题)"
            sender = decode_mime(msg.get("From")) or "(未知发件人)"
            when = format_date(msg.get("Date"))
            print(
                f"[{idx}/{len(batch)}] UID={uid}  {when}  {sender}  |  {subject}",
                flush=True,
            )
        print("", flush=True)

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
