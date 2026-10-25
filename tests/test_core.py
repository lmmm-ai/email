#!/usr/bin/env python3
"""fetch_mail.py 的单元测试：全部离线，不连 IMAP、不调模型、不写真实状态文件。

跑法：
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import logging
import sys
import tempfile
import unittest
from email.message import EmailMessage
from email.mime.message import MIMEMessage
from pathlib import Path

logging.disable(logging.CRITICAL)  # 被测代码会故意打 WARNING，别弄脏测试输出

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

spec = importlib.util.spec_from_file_location("fm", ROOT / "scripts" / "fetch_mail.py")
fm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fm)

EXAMPLES = ROOT / "examples" / "emails"


def rec(uid: int, **over) -> dict:
    base = {
        "uid": uid,
        "message_id": f"<m{uid}@x>",
        "subject": f"主题{uid}",
        "sender": f"发件人{uid} <s{uid}@x.com>",
        "sender_addr": f"s{uid}@x.com",
        "date": "2026-10-25 10:00",
        "role": "to",
        "noise": [],
        "kind": "plain",
        "attachments": 0,
        "chars": 4,
        "removed_quote": 0,
        "removed_sig": 0,
        "body": "正文",
    }
    base.update(over)
    return base


class TestDecode(unittest.TestCase):
    def test_rfc2047_gbk(self):
        raw = f"=?GBK?B?{base64.b64encode('中文测试'.encode('gbk')).decode()}?="
        self.assertEqual(fm.decode_mime(raw), "中文测试")

    def test_rfc2047_utf8(self):
        self.assertEqual(fm.decode_mime("=?utf-8?B?5L2g5aW9?="), "你好")

    def test_raw_unicode_escape(self):
        # 少数客户端把 \uXXXX 原样写进头，decode_header 会当乱码处理
        self.assertEqual(fm.decode_mime("会议"), "会议")

    def test_format_date_cst(self):
        self.assertEqual(
            fm.format_date("Sun, 25 Oct 2026 05:00:00 +0000"), "2026-10-25 13:00"
        )

    def test_format_date_garbage(self):
        self.assertNotEqual(fm.format_date("garbage"), "")


class TestHtmlAndClean(unittest.TestCase):
    def test_html_to_text(self):
        html = (
            "<html><head><title>T</title><style>a{}</style></head><body>"
            "<p>你好</p><br><ul><li>甲</li></ul><script>x()</script>&nbsp;&amp;</body></html>"
        )
        text = fm.html_to_text(html)
        self.assertNotIn("x()", text)
        self.assertNotIn("a{}", text)
        self.assertIn("你好", text)
        self.assertIn("&", text)
        self.assertIn("- 甲", text)

    def test_strip_quoted_cn_marker(self):
        body = "我同意。\n\n------------------ 原始邮件 ------------------\n发件人: a\n" + "旧\n" * 30
        clean, n = fm.strip_quoted(body)
        self.assertIn("我同意", clean)
        self.assertNotIn("旧", clean)
        self.assertGreater(n, 20)

    def test_strip_quoted_writes(self):
        clean, _ = fm.strip_quoted("同意。\n\n在 2026年10月24日 10:00，张三 <z@x.com> 写道：\n> 旧\n")
        self.assertIn("同意。", clean)
        self.assertNotIn("旧", clean)

    def test_strip_quoted_outlook_header(self):
        body = "好。\n\n发件人: 李四\n发送时间: 2026-10-24\n收件人: 我\n主题: x\n正文老内容\n"
        clean, _ = fm.strip_quoted(body)
        self.assertIn("好。", clean)
        self.assertNotIn("老内容", clean)

    def test_strip_quoted_angle_lines(self):
        clean, _ = fm.strip_quoted("第一行\n> a\n> b\n第三行\n")
        self.assertNotIn(">", clean)
        self.assertIn("第一行", clean)
        self.assertIn("第三行", clean)

    def test_strip_quoted_keeps_normal(self):
        body = "第一行没有引用。\n第二行 100 > 50 是数学比较\n第三行正常结束。\n"
        clean, n = fm.strip_quoted(body)
        self.assertEqual(n, 0)
        self.assertIn("第一行", clean)
        self.assertIn("第三行", clean)

    def test_strip_signature(self):
        clean, _ = fm.strip_signature("正文一\n正文二\n--\n张三\n公司名\n")
        self.assertIn("正文二", clean)
        self.assertNotIn("公司名", clean)
        clean, _ = fm.strip_signature("正文\n\nSent from my iPhone\n")
        self.assertNotIn("Sent from", clean)


class TestExtractBody(unittest.TestCase):
    def test_prefers_plain(self):
        msg = EmailMessage()
        msg["From"] = "a@x.com"
        msg.set_content("纯文本正文")
        msg.add_alternative("<p>HTML 正文</p>", subtype="html")
        msg.add_attachment(b"x" * 10, maintype="application", subtype="pdf", filename="a.pdf")
        body, kind, att = fm.extract_body(fm.email.message_from_bytes(msg.as_bytes()))
        self.assertEqual(kind, "plain")
        self.assertIn("纯文本正文", body)
        self.assertEqual(att, 1)

    def test_prefers_html_when_plain_is_stub(self):
        msg = EmailMessage()
        msg["From"] = "a@x.com"
        msg.set_content("请用 HTML 客户端查看")
        msg.add_alternative(
            "<p>真正的正文在这里，包含金额 12,800 元和截止时间 2026-10-26 18:00，"
            "以及一段足够长的说明文字用于通过长度判断。</p>",
            subtype="html",
        )
        body, kind, _ = fm.extract_body(fm.email.message_from_bytes(msg.as_bytes()))
        self.assertEqual(kind, "html")
        self.assertIn("12,800", body)

    def test_forwarded_message_not_unwrapped(self):
        outer = EmailMessage()
        outer["From"] = "a@x.com"
        outer.set_content("看下面这封")
        inner = EmailMessage()
        inner["From"] = "b@x.com"
        inner.set_content("内层机密正文")
        outer.make_mixed()
        outer.attach(MIMEMessage(inner))
        body, _, _ = fm.extract_body(fm.email.message_from_bytes(outer.as_bytes()))
        self.assertNotIn("内层机密", body)


class TestNoiseAndRole(unittest.TestCase):
    def test_noise_multi_signal(self):
        m = fm.email.message_from_bytes(
            b"From: noreply@github.com\r\nList-Unsubscribe: <x>\r\nPrecedence: bulk\r\n"
            b"Subject: hi\r\n\r\nbody"
        )
        self.assertGreaterEqual(len(fm.noise_reasons(m, "noreply@github.com")), 3)

    def test_noise_normal(self):
        m = fm.email.message_from_bytes(
            b"From: zhangsan@example.com\r\nSubject: hi\r\n\r\nbody"
        )
        self.assertEqual(fm.noise_reasons(m, "zhangsan@example.com"), [])

    def test_role_cc(self):
        m = fm.email.message_from_bytes(
            b"To: a@x.com\r\nCc: me@qq.com\r\nSubject: x\r\n\r\nb"
        )
        self.assertEqual(fm.recipient_role(m, "me@qq.com"), "cc")

    def test_role_to(self):
        m = fm.email.message_from_bytes(
            b"To: me@qq.com\r\nCc: a@x.com\r\nSubject: x\r\n\r\nb"
        )
        self.assertEqual(fm.recipient_role(m, "me@qq.com"), "to")


class TestMapping(unittest.TestCase):
    def test_mask(self):
        self.assertEqual(fm.mask_addr("zhangsan@client-demo.com"), "z***@client-demo.com")
        self.assertIn("***", fm.mask_sender("王海", "wanghai@x.com"))
        self.assertNotIn("wanghai", fm.mask_sender("王海", "wanghai@x.com"))
        self.assertEqual(fm.mask_addr("不是地址"), "不是地址")

    def test_build_record_message_id(self):
        msg = EmailMessage()
        msg["From"] = "=?utf-8?B?5byg5LiJ?= <z@x.com>"
        msg["To"] = "me@qq.com"
        msg["Subject"] = "合同评审"
        msg["Message-ID"] = "<abc123@x.com>"
        msg.set_content("正文内容")
        raw = msg.as_bytes()
        args = type("A", (), {"mask": False})()
        out = fm.build_record(raw, 42, len(raw), False, args, "me@qq.com")
        self.assertEqual(out["message_id"], "<abc123@x.com>")
        self.assertEqual(out["role"], "to")
        self.assertIn("合同评审", out["subject"])

    def test_build_record_message_id_fallback(self):
        msg = EmailMessage()
        msg["From"] = "a@x.com"
        msg["Subject"] = "没有 Message-ID"
        msg.set_content("正文")
        raw = msg.as_bytes()
        args = type("A", (), {"mask": True})()
        out = fm.build_record(raw, 7, len(raw), False, args, "me@qq.com")
        self.assertEqual(out["message_id"], "uid:7")
        self.assertIn("***", out["sender"])

    def test_parse_uid_list(self):
        self.assertEqual(fm.parse_uid_list("34,36 30-32"), [30, 31, 32, 34, 36])


class TestState(unittest.TestCase):
    def test_roundtrip_and_new_fields(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "s" / "mail_state.json"
            fm.save_state(p, {"version": 3, "uidvalidity": 1, "last_uid": 9,
                              "initialized": True, "last_run": "x"})
            st = fm.load_state(p)
            self.assertEqual(st["last_uid"], 9)
            self.assertIn("delivered", st)
            self.assertIn("stats", st)

    def test_migrate_v1_state(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "mail_state.json"
            p.write_text('{"version": 1, "uidvalidity": 1, "last_uid": 41, "initialized": true}', "utf-8")
            st = fm.load_state(p)
            # 老代码的 bug：合并后 version 被旧值覆盖，永远停在 1
            self.assertEqual(st["version"], fm.STATE_VERSION)
            self.assertEqual(st["last_uid"], 41)
            self.assertEqual(st["delivered"], {})

    def test_broken_and_missing(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "mail_state.json"
            p.write_text("{broken", "utf-8")
            st = fm.load_state(p)
            self.assertEqual(st["last_uid"], 0)
            self.assertFalse(st["initialized"])
            missing = fm.load_state(Path(td) / "nope.json")
            self.assertEqual(missing["version"], fm.STATE_VERSION)

    def test_prune_delivered(self):
        led = {f"<{i}>": {"uid": i} for i in range(10)}
        kept = fm.prune_delivered(led)
        self.assertEqual(len(kept), 10)
        led = {f"<{i}>": {"uid": i} for i in range(fm.DELIVERED_KEEP + 5)}
        kept = fm.prune_delivered(led)
        self.assertEqual(len(kept), fm.DELIVERED_KEEP)
        self.assertIn(f"<{fm.DELIVERED_KEEP + 4}>", kept)


class TestModelParsing(unittest.TestCase):
    def test_parse_variants(self):
        self.assertEqual(len(fm._parse_json_items('{"items":[{"uid":1}]}')), 1)
        self.assertEqual(len(fm._parse_json_items('```json\n{"items":[{"uid":1},{"uid":2}]}\n```')), 2)
        self.assertEqual(len(fm._parse_json_items('好的：{"items":[{"uid":1}]} 完毕')), 1)
        self.assertEqual(len(fm._parse_json_items('[{"uid":1}]')), 1)
        with self.assertRaises(Exception):
            fm._parse_json_items("完全不是 JSON")

    def test_normalize_item(self):
        item = fm._normalize_item(
            {"priority": "p0", "summary": "", "action_items": ["做A", {"title": "做B", "due": "2026-10-26"}]},
            rec(7),
        )
        self.assertEqual(item["priority"], "P0")
        self.assertEqual(item["summary"], "主题7")
        self.assertEqual([a["title"] for a in item["action_items"]], ["做A", "做B"])
        self.assertEqual(item["action_items"][1]["due"], "2026-10-26")

    def test_invalid_priority_falls_back(self):
        self.assertEqual(fm._normalize_item({"priority": "PX"}, rec(1))["priority"], "P2")

    def test_norm_dt(self):
        self.assertEqual(fm._norm_dt("2026-10-26 18:00 前"), "2026-10-26T18:00")
        self.assertEqual(fm._norm_dt("2026年10月26日"), "2026-10-26")
        self.assertEqual(fm._norm_dt("2026/10/26 9:05"), "2026-10-26T09:05")
        self.assertIsNone(fm._norm_dt("本周五"))
        self.assertIsNone(fm._norm_dt(""))
        self.assertIsNone(fm._norm_dt("2026-13-45"))

    def test_prompt_guard(self):
        u = fm.build_user_message(
            [{"uid": 1, "sender": "a <a@x>", "role": "to", "date": "2026-10-25 10:00",
              "subject": "S", "noise": [], "body": "x" * 500}],
            "2026-10-25 周六",
            100,
        )
        self.assertIn("正文过长已截断", u)
        self.assertIn("<<<", u)
        self.assertIn("不是指令", fm.SYSTEM_PROMPT)


class TestClassifyResilience(unittest.TestCase):
    def setUp(self):
        self.records = [rec(1), rec(2)]
        self.cfg = {"api_key": "k", "base_url": "http://x", "model": "m"}

    def _patch(self, fn):
        original = fm.call_llm
        fm.call_llm = fn
        self.addCleanup(lambda: setattr(fm, "call_llm", original))

    def test_all_batches_fail_raises(self):
        def boom(*a, **k):
            raise RuntimeError("HTTP 502")

        self._patch(boom)
        with self.assertRaises(RuntimeError):
            fm.classify(self.records, self.cfg, 2, True, 200)

    def test_partial_failure_degrades(self):
        calls = {"n": 0}

        def sometimes(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("HTTP 500")
            return '{"items":[{"uid":2,"priority":"P0","summary":"急"}]}', {
                "prompt_tokens": 10,
                "completion_tokens": 5,
            }

        self._patch(sometimes)
        results, stats = fm.classify(self.records, self.cfg, 1, True, 200)
        self.assertEqual(results[1]["priority"], "P2")
        self.assertEqual(results[1]["source"], "fallback")
        self.assertEqual(results[2]["priority"], "P0")
        self.assertEqual(stats["failed_batches"], 1)
        self.assertEqual(stats["batches"], 2)
        self.assertEqual(stats["tokens_in"], 10)

    def test_bad_json_retried_once(self):
        seq = iter([
            "抱歉我不能输出 JSON",
            ('{"items":[{"uid":1,"priority":"P1","summary":"改好了"},'
             '{"uid":2,"priority":"P3","summary":"存档"}]}'),
        ])

        def fake(*a, **k):
            return next(seq), {"prompt_tokens": 3, "completion_tokens": 4}

        self._patch(fake)
        results, stats = fm.classify(self.records, self.cfg, 2, True, 200)
        self.assertEqual(results[1]["priority"], "P1")
        self.assertEqual(results[2]["priority"], "P3")
        self.assertEqual(stats["failed_batches"], 0)

    def test_missing_uid_degrades(self):
        self._patch(lambda *a, **k: ('{"items":[{"uid":1,"priority":"P0"}]}',
                                     {"prompt_tokens": 1, "completion_tokens": 1}))
        results, _ = fm.classify(self.records, self.cfg, 5, True, 200)
        self.assertEqual(results[1]["priority"], "P0")
        self.assertEqual(results[2]["priority"], "P2")

    def test_noise_skips_model(self):
        noisy = rec(3, noise=["List-Unsubscribe"])
        called = {"n": 0}

        def counting(*a, **k):
            called["n"] += 1
            return '{"items":[]}', {"prompt_tokens": 0, "completion_tokens": 0}

        self._patch(counting)
        results, _ = fm.classify([noisy], self.cfg, 5, True, 200)
        self.assertEqual(results[3]["priority"], "P3")
        self.assertEqual(called["n"], 0)


class TestItems(unittest.TestCase):
    def _results(self):
        return {
            1: {"uid": 1, "priority": "P2", "summary": "s1", "reason": "", "deadline": None,
                "need_reply": False, "action_items": [], "source": "llm"},
            2: {"uid": 2, "priority": "P0", "summary": "s2", "reason": "", "deadline": "2026-10-25T18:00",
                "need_reply": True, "action_items": [{"title": "回电", "due": None}], "source": "llm"},
            3: {"uid": 3, "priority": "P3", "summary": "s3", "reason": "", "deadline": None,
                "need_reply": False, "action_items": [], "source": "rule"},
        }

    def test_build_items_sorted(self):
        items = fm.build_items([rec(1), rec(2), rec(3)], self._results())
        self.assertEqual([it["priority"] for it in items], ["P0", "P2", "P3"])
        self.assertEqual(items[0]["message_id"], "<m2@x>")
        self.assertTrue(items[0]["need_reply"])

    def test_pushable_threshold_and_dedupe(self):
        items = fm.build_items([rec(1), rec(2), rec(3)], self._results())
        self.assertEqual([it["uid"] for it in fm.pushable_items(items, "P1", {})], [2])
        self.assertEqual(fm.pushable_items(items, "P1", {"<m2@x>": {}}), [])
        self.assertEqual(len(fm.pushable_items(items, "P2", {})), 2)

    def test_build_meta_counts_dict(self):
        from datetime import datetime

        items = fm.build_items([rec(1), rec(2), rec(3)], self._results())
        meta = fm.build_meta(items, datetime.now(fm.CST), model="m")
        self.assertIsInstance(meta["counts"], dict)
        self.assertEqual(meta["counts"]["P0"], 1)
        self.assertEqual(meta["total"], 3)
        self.assertEqual(meta["model"], "m")
        self.assertTrue(meta["stamp"])

    def test_deliver_none_mode(self):
        args = type("A", (), {"deliver": "none", "notify_min": "P1", "webhook_url": "",
                              "digest_dir": "state/digest"})()
        outs, pushed = fm.deliver_all([], args, {}, fm.empty_state())
        self.assertEqual(outs, [])
        self.assertEqual(pushed, [])

    def test_render_digest_counts(self):
        recs = [rec(1), rec(2)]
        results = {
            1: {"uid": 1, "priority": "P0", "summary": "急", "reason": "", "deadline": None,
                "need_reply": True, "action_items": [{"title": "回信", "due": None}]},
            2: {"uid": 2, "priority": "P3", "summary": "存档", "reason": "", "deadline": None,
                "need_reply": False, "action_items": []},
        }
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            n = fm.render_digest(recs, results)
        self.assertEqual(n, 1)
        self.assertIn("□ 回信", buf.getvalue())


class FakeConn:
    def __init__(self, payload):
        self.payload = payload

    def uid(self, *a):
        return "OK", [self.payload]


class TestSearch(unittest.TestCase):
    def test_filter(self):
        self.assertEqual(fm.search_uids(FakeConn(b"38 39 40"), 38), [39, 40])
        self.assertEqual(fm.search_uids(FakeConn(b"3 1 2"), 0), [1, 2, 3])


@unittest.skipUnless(EXAMPLES.exists(), "演示邮件未生成")
class TestDemo(unittest.TestCase):
    def _load(self, name):
        raw = (EXAMPLES / name).read_bytes()
        args = type("A", (), {"mask": False})()
        return fm.build_record(raw, 1, len(raw), False, args, "demo@example.com")

    def test_fixture_priorities(self):
        cases = {
            "01-p0-customer-complaint.eml": "P0",
            "02-p1-boss-confirm.eml": "P1",
            "04-p3-newsletter.eml": "P3",
            "06-p3-system-notify.eml": "P3",
        }
        for name, want in cases.items():
            with self.subTest(name=name):
                self.assertEqual(fm.demo_classify(self._load(name))["priority"], want)

    def test_gbk_subject_decoded(self):
        self.assertIn("方案", self._load("02-p1-boss-confirm.eml")["subject"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
