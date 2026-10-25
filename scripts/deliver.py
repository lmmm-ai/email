#!/usr/bin/env python3
"""投递层：把分级结果送出机器。

三个渠道，全部只用标准库：
  webhook  企业微信群机器人 / 飞书自定义机器人 / 任意收 {"text": "..."} 的地址
  digest   写一份 Markdown 日报到磁盘（Actions 里会推回 state 分支）
  stdout   只打印（--deliver none 时的行为，也是本地默认）

设计约束：
  - 只推 P0/P1，由调用方过滤后传进来（--notify-min 控制）
  - 单条消息有长度上限（企业微信 markdown 4096 字节），超了按邮件切段，不做截断式丢内容
  - 429/5xx/网络错误退避重试 3 次；渠道自己返回的 errcode/code 也会校验
  - 返回 DeliveryResult，调用方据此写幂等台账、决定退出码
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger("mailbot.deliver")

PRIORITY_ORDER = ["P0", "P1", "P2", "P3"]
PRIORITY_LABEL = {
    "P0": "今天必须处理",
    "P1": "本周处理",
    "P2": "可以不急",
    "P3": "仅存档",
}

WECOM_LIMIT = 3500  # 企业微信 markdown 硬上限 4096 字节，留余量
FEISHU_LIMIT = 4000
GENERIC_LIMIT = 3500
FEISHU_MAX_ELEMENTS = 30  # 单卡片元素过多会渲染失败，超了拆卡


@dataclass
class DeliveryResult:
    ok: bool
    channel: str
    sent: list[str] = field(default_factory=list)
    detail: str = ""
    artifacts: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 渠道识别
# --------------------------------------------------------------------------- #
def detect_channel(url: str) -> str:
    host = (urlsplit(url).netloc or "").lower()
    if "qyapi.weixin.qq.com" in host:
        return "wecom"
    if "feishu.cn" in host or "larksuite.com" in host:
        return "feishu"
    return "generic"


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def _cut_bytes(text: str, limit: int) -> str:
    """按 UTF-8 字节数截断，不切断半个汉字。"""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", "ignore") + "…"


def format_item(it: dict) -> str:
    """一封邮件的通知块。企业微信 / 飞书 / 纯文本都能用同一份。"""
    lines = [f"**【{it['priority']}】{it['sender']}**"]
    lines.append(f"主题：{it.get('subject') or '(无主题)'}")
    summary = (it.get("summary") or "").strip()
    if summary:
        lines.append(f"> {summary}")
    meta_bits: list[str] = []
    if it.get("deadline"):
        meta_bits.append(f"截止 {it['deadline']}")
    if it.get("need_reply"):
        meta_bits.append("需要我回复")
    if it.get("reason"):
        meta_bits.append(str(it["reason"]))
    if meta_bits:
        lines.append(" ｜ ".join(meta_bits))
    for act in it.get("action_items") or []:
        due = f"（{act['due']}）" if act.get("due") else ""
        lines.append(f"- □ {act['title']}{due}")
    return "\n".join(lines)


def format_header(meta: dict, pushed: int) -> str:
    counts = meta.get("counts") or {}
    bits = [f"{p} {counts.get(p, 0)}" for p in PRIORITY_ORDER if counts.get(p, 0)]
    tail = " · ".join(bits) if bits else "无"
    return (
        f"### 邮件助手 · {meta.get('time', '')}\n"
        f"本次 {meta.get('total', 0)} 封（{tail}），本次推送 {pushed} 封"
    )


def _pack(header: str, blocks: list[str], limit: int) -> list[list[str]]:
    """把邮件块塞进若干条消息，每条不超过 limit 字节。"""
    base = len(header.encode("utf-8")) + 4
    batches: list[list[str]] = []
    cur: list[str] = []
    size = base
    for block in blocks:
        need = len(block.encode("utf-8")) + 2
        if cur and size + need > limit:
            batches.append(cur)
            cur, size = [], base
        if not cur and need > limit:  # 单封就超长：单独一条，交给 API 截断
            batches.append([block])
            cur, size = [], base
            continue
        cur.append(block)
        size += need
    if cur:
        batches.append(cur)
    return batches


# --------------------------------------------------------------------------- #
# 渠道 payload
# --------------------------------------------------------------------------- #
def _feishu_block(block: str) -> str:
    """飞书 lark_md 不认 `> ` 引用，去掉行首标记。"""
    return "\n".join(line[2:] if line.startswith("> ") else line for line in block.splitlines())


def _wecom_payload(header: str, blocks: list[str]) -> dict:
    content = header + "\n\n" + "\n\n".join(blocks)
    return {"msgtype": "markdown", "markdown": {"content": _cut_bytes(content, WECOM_LIMIT)}}


def _feishu_payload(header: str, blocks: list[str], template: str) -> dict:
    title = header.splitlines()[0].lstrip("# ").strip()
    subtitle = "\n".join(header.splitlines()[1:]).strip()
    elements = []
    if subtitle:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": subtitle}})
        elements.append({"tag": "hr"})
    for block in blocks:
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": _feishu_block(block)}}
        )
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": template,
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": elements,
        },
    }


def _generic_payload(header: str, blocks: list[str]) -> dict:
    return {"text": _cut_bytes(header + "\n\n" + "\n\n".join(blocks), GENERIC_LIMIT)}


def build_payloads(channel: str, header: str, blocks: list[str], template: str) -> list[dict]:
    if channel == "wecom":
        return [_wecom_payload(header, chunk) for chunk in _pack(header, blocks, WECOM_LIMIT)]
    if channel == "feishu":
        # 飞书卡片是结构化元素，不是一段长文本：按"元素个数"拆卡（超过 30 个会渲染失败），
        # 再顺手压一压体积，避免单卡超过 20KB。
        cards: list[list[str]] = []
        cur: list[str] = []
        size = 0
        for block in blocks:
            need = len(block.encode("utf-8")) + 200
            if cur and (len(cur) >= FEISHU_MAX_ELEMENTS or size + need > FEISHU_LIMIT):
                cards.append(cur)
                cur, size = [], 0
            cur.append(block)
            size += need
        if cur:
            cards.append(cur)
        return [_feishu_payload(header, card, template) for card in cards] or [
            _feishu_payload(header, [], template)
        ]
    return [_generic_payload(header, chunk) for chunk in _pack(header, blocks, GENERIC_LIMIT)]


# --------------------------------------------------------------------------- #
# 发送
# --------------------------------------------------------------------------- #
def _post_json(url: str, payload: dict, timeout: int = 15) -> str:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_err = "未知错误"
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url,
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "mailbot/4.0",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            last_err = f"HTTP {exc.code}: {detail}"
            if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(last_err) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_err = f"网络错误: {exc}"
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(last_err) from exc
    raise RuntimeError(last_err)


def _check_response(channel: str, raw: str) -> None:
    """企业微信/飞书 HTTP 200 也可能是业务错误，必须看 errcode/code。"""
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return
    if not isinstance(data, dict):
        return
    if channel == "wecom":
        code = data.get("errcode", 0)
        if code not in (0, None):
            raise RuntimeError(f"企业微信 errcode={code} errmsg={data.get('errmsg')}")
    elif channel == "feishu":
        code = data.get("code", data.get("StatusCode", 0))
        if code not in (0, None):
            raise RuntimeError(f"飞书 code={code} msg={data.get('msg') or data.get('StatusMessage')}")


def deliver_webhook(
    url: str, items: list[dict], meta: dict, channel: str | None = None
) -> DeliveryResult:
    """把已经过滤好的 items 推到 webhook。成功才把 message_id 记成已投递。"""
    channel = channel or detect_channel(url)
    if not items:
        return DeliveryResult(True, channel, [], "无需推送（没有 P0/P1）")

    header = format_header(meta, len(items))
    blocks = [format_item(it) for it in items]
    template = "red" if any(it["priority"] == "P0" for it in items) else "orange"
    payloads = build_payloads(channel, header, blocks, template)

    log.info("投递 %d 封到 %s（%d 条消息）", len(items), channel, len(payloads))
    for idx, payload in enumerate(payloads, 1):
        try:
            raw = _post_json(url, payload)
            _check_response(channel, raw)
        except RuntimeError as exc:
            log.error("第 %d/%d 条消息发送失败：%s", idx, len(payloads), exc)
            return DeliveryResult(False, channel, [], str(exc))
        log.info("第 %d/%d 条消息已送达", idx, len(payloads))

    return DeliveryResult(
        True, channel, [it["message_id"] for it in items if it.get("message_id")], "已送达"
    )


# --------------------------------------------------------------------------- #
# Markdown 日报
# --------------------------------------------------------------------------- #
def render_markdown(items: list[dict], meta: dict) -> str:
    counts = meta.get("counts") or {}
    lines = [
        f"# 邮件助手日报 · {meta.get('time', '')}",
        "",
        f"本次处理 {meta.get('total', 0)} 封："
        + "，".join(f"{p} {counts[p]} 封" for p in PRIORITY_ORDER if counts.get(p))
        + "",
    ]
    if meta.get("model"):
        lines.append(f"分级模型：`{meta['model']}`")
    lines.append("")
    for p in PRIORITY_ORDER:
        bucket = [it for it in items if it["priority"] == p]
        if not bucket:
            continue
        lines.append(f"## {p} · {PRIORITY_LABEL[p]}（{len(bucket)}）")
        lines.append("")
        for it in bucket:
            lines.append(f"### {it.get('subject') or '(无主题)'}")
            lines.append("")
            lines.append(f"- 发件人：{it['sender']}")
            lines.append(f"- 时间：{it.get('date', '')}")
            if it.get("summary"):
                lines.append(f"- 摘要：{it['summary']}")
            if it.get("reason"):
                lines.append(f"- 理由：{it['reason']}")
            if it.get("deadline"):
                lines.append(f"- 截止：{it['deadline']}")
            if it.get("need_reply"):
                lines.append("- 需要回复：是")
            for act in it.get("action_items") or []:
                due = f"（{act['due']}）" if act.get("due") else ""
                lines.append(f"- [ ] {act['title']}{due}")
            lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "> 由 mailbot 自动生成。紧急度定义：P0 今天必须处理 / P1 本周处理 / "
        "P2 可以不急 / P3 仅存档。"
    )
    return "\n".join(lines) + "\n"


def write_digest(digest_dir: str | Path, items: list[dict], meta: dict) -> DeliveryResult:
    target = Path(digest_dir)
    try:
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{meta.get('stamp', 'digest')}.md"
        path.write_text(render_markdown(items, meta), "utf-8")
    except OSError as exc:
        log.error("写日报失败：%s", exc)
        return DeliveryResult(False, "digest", [], str(exc))
    log.info("日报已写入 %s", path)
    return DeliveryResult(True, "digest", [], "已写入", [str(path)])
