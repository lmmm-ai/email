#!/usr/bin/env python3
"""deliver.py 的单元测试：用本地 HTTP 服务当假机器人，不联网、不打扰真群。"""
from __future__ import annotations

import json
import logging
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

logging.disable(logging.CRITICAL)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import deliver  # noqa: E402


def item(uid: int, priority: str = "P0", **over) -> dict:
    base = {
        "uid": uid,
        "message_id": f"<m{uid}@x>",
        "priority": priority,
        "subject": f"主题{uid}",
        "sender": f"张三 <z{uid}@x.com>",
        "date": "2026-10-25 10:00",
        "summary": "摘要内容",
        "reason": "命中紧急关键词",
        "deadline": "2026-10-25T18:00",
        "need_reply": True,
        "action_items": [{"title": "回电", "due": "2026-10-25"}],
    }
    base.update(over)
    return base


META = {
    "date": "2026-10-25",
    "time": "2026-10-25 18:00",
    "stamp": "20261025-180000",
    "total": 3,
    "counts": {"P0": 1, "P1": 1, "P2": 1, "P3": 0},
    "model": "test-model",
}


class _Bot(BaseHTTPRequestHandler):
    bodies: list[dict] = []
    status = 200
    reply: dict = {"errcode": 0, "errmsg": "ok"}

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8")
        type(self).bodies.append(json.loads(raw))
        data = json.dumps(type(self).reply).encode("utf-8")
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class TestChannel(unittest.TestCase):
    def test_detect(self):
        self.assertEqual(deliver.detect_channel("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x"), "wecom")
        self.assertEqual(deliver.detect_channel("https://open.feishu.cn/open-apis/bot/v2/hook/x"), "feishu")
        self.assertEqual(deliver.detect_channel("https://open.larksuite.com/x"), "feishu")
        self.assertEqual(deliver.detect_channel("http://127.0.0.1:9/hook"), "generic")


class TestFormat(unittest.TestCase):
    def test_format_item(self):
        text = deliver.format_item(item(1))
        self.assertIn("P0", text)
        self.assertIn("主题1", text)
        self.assertIn("摘要内容", text)
        self.assertIn("回电", text)
        self.assertIn("需要我回复", text)

    def test_header(self):
        text = deliver.format_header(META, 2)
        self.assertIn("P0 1", text)
        self.assertIn("本次推送 2 封", text)


class TestPayload(unittest.TestCase):
    def test_wecom_respects_byte_limit(self):
        blocks = [deliver.format_item(item(i)) for i in range(120)]
        header = deliver.format_header(META, len(blocks))
        payloads = deliver.build_payloads("wecom", header, blocks, "red")
        self.assertGreater(len(payloads), 1)
        for p in payloads:
            self.assertEqual(p["msgtype"], "markdown")
            self.assertLessEqual(len(p["markdown"]["content"].encode("utf-8")), deliver.WECOM_LIMIT)

    def test_wecom_does_not_cut_hanzi(self):
        blocks = ["很长的中文段落" * 400]
        payloads = deliver.build_payloads("wecom", "标题", blocks, "red")
        for p in payloads:
            p["markdown"]["content"].encode("utf-8").decode("utf-8")  # 不抛 UnicodeDecodeError

    def test_feishu_card(self):
        blocks = [deliver.format_item(item(1)), deliver.format_item(item(2, "P1"))]
        payloads = deliver.build_payloads("feishu", deliver.format_header(META, 2), blocks, "red")
        self.assertEqual(payloads[0]["msg_type"], "interactive")
        self.assertEqual(payloads[0]["card"]["header"]["template"], "red")
        joined = json.dumps(payloads, ensure_ascii=False)
        self.assertIn("主题1", joined)

    def test_feishu_template_by_priority(self):
        payloads = deliver.build_payloads(
            "feishu", "h", [deliver.format_item(item(2, "P1"))], "orange"
        )
        self.assertEqual(payloads[0]["card"]["header"]["template"], "orange")

    def test_generic_payload(self):
        payloads = deliver.build_payloads("generic", "h", [deliver.format_item(item(1))], "red")
        self.assertIn("text", payloads[0])


class TestDigest(unittest.TestCase):
    def test_write_digest(self):
        items = [item(1, "P0"), item(2, "P2")]
        with tempfile.TemporaryDirectory() as td:
            res = deliver.write_digest(td, items, META)
            self.assertTrue(res.ok)
            self.assertEqual(res.channel, "digest")
            path = Path(res.artifacts[0])
            self.assertTrue(path.exists())
            text = path.read_text("utf-8")
            self.assertIn("邮件助手日报", text)
            self.assertIn("P0", text)
            self.assertIn("- [ ] 回电", text)

    def test_render_markdown_groups(self):
        text = deliver.render_markdown([item(1, "P0"), item(2, "P3")], META)
        self.assertIn("## P0", text)
        self.assertIn("## P3", text)
        self.assertIn("test-model", text)


class TestWebhook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Bot)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Bot.bodies = []
        _Bot.status = 200
        _Bot.reply = {"errcode": 0, "errmsg": "ok"}

    def url(self):
        return f"http://127.0.0.1:{self.port}/hook"

    def test_empty_items_is_noop(self):
        res = deliver.deliver_webhook(self.url(), [], META)
        self.assertTrue(res.ok)
        self.assertEqual(_Bot.bodies, [])

    def test_success_marks_message_ids(self):
        res = deliver.deliver_webhook(self.url(), [item(1)], META)
        self.assertTrue(res.ok, res.detail)
        self.assertEqual(res.sent, ["<m1@x>"])
        self.assertEqual(len(_Bot.bodies), 1)
        self.assertIn("主题1", json.dumps(_Bot.bodies[0], ensure_ascii=False))

    def test_business_error_detected(self):
        # HTTP 200 但 errcode 非 0：企业微信常见"key 失效/被限流"
        _Bot.reply = {"errcode": 93000, "errmsg": "invalid webhook url"}
        res = deliver.deliver_webhook(self.url(), [item(1)], META, channel="wecom")
        self.assertFalse(res.ok)
        self.assertIn("93000", res.detail)

    def test_http_error_no_retry_on_400(self):
        _Bot.status = 400
        _Bot.reply = {"error": "bad"}
        res = deliver.deliver_webhook(self.url(), [item(1)], META)
        self.assertFalse(res.ok)
        self.assertIn("400", res.detail)

    def test_wecom_payload_sent(self):
        deliver.deliver_webhook(self.url(), [item(1), item(2, "P1")], META, channel="wecom")
        self.assertEqual(_Bot.bodies[0]["msgtype"], "markdown")

    def test_feishu_payload_sent(self):
        deliver.deliver_webhook(self.url(), [item(1)], META, channel="feishu")
        self.assertEqual(_Bot.bodies[0]["msg_type"], "interactive")


if __name__ == "__main__":
    unittest.main(verbosity=2)
