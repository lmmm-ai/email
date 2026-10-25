#!/usr/bin/env python3
"""生成 examples/emails/*.eml 演示数据。

为什么要在仓库里放 .eml：作业/演示时别人没有你的邮箱账号、也不想给你邮箱授权，
但只要 `python scripts/fetch_mail.py --demo` 就能跑通「取信 → 清洗 → 分级 → 投递」
整条链路。这些文件都是伪造的，覆盖了真实场景里最容易翻车的几种邮件形态：

  01 客户投诉（P0）+ 中文整段引用历史 + 签名档
  02 上级要求确认（P1）+ GBK 编码主题（国内邮箱最常见的乱码源）
  03 周报征集（P2，我在抄送）
  04 营销广告（P3）+ List-Unsubscribe / Precedence: bulk
  05 只有 HTML 正文（P1）+ 表格 + 链接
  06 QQ 官方系统通知（P3）+ Auto-Submitted

生成方式：python examples/make_fixtures.py
"""

from __future__ import annotations

import base64
from email.message import EmailMessage
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "emails"


def save(name: str, msg: EmailMessage) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    path.write_bytes(msg.as_bytes())
    print(f"写出 {path.relative_to(HERE.parent)}  ({path.stat().st_size} 字节)")


def base(frm: str, to: str = "me@example.com") -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = frm
    msg["To"] = to
    return msg


def f01_customer_complaint() -> None:
    msg = base("王海 <wanghai@client-demo.com>")
    msg["Subject"] = "【投诉】订单 20261024 的发票金额错了，今天必须给个说法"
    msg["Date"] = "Wed, 21 Oct 2026 09:12:00 +0800"
    msg["Message-ID"] = "<demo-001@client-demo.com>"
    body = """李工你好，

我们财务刚核完，订单 20261024 这张发票的金额是 12,800，但实际付款和合同都是 10,800，
差了两千块。这个月 25 号就要关账，今天下班前必须把这个发票处理掉，不然我们这边没法入账。

麻烦你：
1. 今天 18:00 前确认是重开还是作废
2. 把正确的发票号发我

--
王海
采购部 ｜ 演示客户有限公司
电话 138-0000-0000


------------------ 原始邮件 ------------------
发件人: 李工 <me@example.com>
发送时间: 2026年10月20日 10:30
收件人: 王海 <wanghai@client-demo.com>
主题: 回复：订单 20261024 发票

好的，我今天处理。
"""
    msg.set_content(body)
    save("01-p0-customer-complaint.eml", msg)


def f02_boss_confirm() -> None:
    msg = base("Zhang Lei <zhanglei@company-demo.com>")
    msg["Cc"] = "team@company-demo.com"
    # 故意用 GBK + base64 的裸 RFC2047 头：国内邮件系统最常见的形式，
    # 也是解码链最容易出错的地方（见 fetch_mail.decode_mime）。
    raw_subject = "关于周五评审的方案，请确认这版是否可用"
    msg["Subject"] = "=?GBK?B?" + base64.b64encode(raw_subject.encode("gbk")).decode() + "?="
    msg["Date"] = "Wed, 21 Oct 2026 14:05:00 +0800"
    msg["Message-ID"] = "<demo-002@company-demo.com>"
    body = """方案 v3 已经放到共享盘（评审材料/2026Q4/方案v3.pdf）。

周五评审要定的是三件事：范围、排期、人力。你把范围和排期这两块先过一遍，
有问题明天上午 10 点前回我，没问题就按 v3 走。

评审材料请在周四下班前发到会签组邮箱。

--
Zhang Lei ｜ 产品部
"""
    msg.set_content(body)
    save("02-p1-boss-confirm.eml", msg)


def f03_weekly_report() -> None:
    msg = base("行政-周报 <weekly@company-demo.com>", to="all@company-demo.com")
    msg["Cc"] = "me@example.com"
    msg["Subject"] = "本周周报提交提醒（周五 18:00 截止）"
    msg["Date"] = "Thu, 22 Oct 2026 09:00:00 +0800"
    msg["Message-ID"] = "<demo-003@company-demo.com>"
    body = """各位同事：

本周周报请于周五 18:00 前提交到 OA 系统，内容包含：本周完成、下周计划、风险项。

逾期未提交会在周会上点名，谢谢配合。

行政部
"""
    msg.set_content(body)
    save("03-p2-weekly-report.eml", msg)


def f04_newsletter() -> None:
    msg = base("云服务推广 <noreply@promo-demo.com>")
    msg["Subject"] = "限时 5 折！企业云盘年度套餐最后 3 天"
    msg["Date"] = "Thu, 22 Oct 2026 10:30:00 +0800"
    msg["Message-ID"] = "<demo-004@promo-demo.com>"
    msg["List-Unsubscribe"] = "<https://promo-demo.com/unsub?id=1>"
    msg["Precedence"] = "bulk"
    body = """尊敬的用户：

企业云盘年度套餐限时 5 折，仅剩 3 天。点击链接立即抢购。

如果不希望再收到此类邮件，请点击退订。
"""
    msg.set_content(body)
    save("04-p3-newsletter.eml", msg)


def f05_html_only() -> None:
    msg = base("陈晨 <chenchen@partner-demo.com>")
    msg["Subject"] = "接口联调结果与下周上线安排"
    msg["Date"] = "Thu, 22 Oct 2026 16:40:00 +0800"
    msg["Message-ID"] = "<demo-005@partner-demo.com>"
    html = """<html><head><style>td{padding:4px}</style></head><body>
<p>联调结果如下，请核对：</p>
<table border="1">
  <tr><td>接口</td><td>状态</td><td>备注</td></tr>
  <tr><td>登录</td><td>通过</td><td>-</td></tr>
  <tr><td>下单</td><td>失败</td><td>签名错误，需你们修</td></tr>
</table>
<p>下单接口的签名错误请在明天 18:00 前修掉，否则下周二的上线要延期。</p>
<p>文档：<a href="https://example.com/api">接口文档</a></p>
<script>console.log('这段不该出现在正文里')</script>
</body></html>"""
    msg.set_content("（本邮件为 HTML 格式，请用支持 HTML 的客户端查看）")
    msg.add_alternative(html, subtype="html")
    save("05-p1-html-only.eml", msg)


def f06_system_notify() -> None:
    msg = base("QQ邮箱团队 <10000@qq.com>")
    msg["Subject"] = "你的邮箱已开启 IMAP/SMTP 服务"
    msg["Date"] = "Fri, 23 Oct 2026 08:00:00 +0800"
    msg["Message-ID"] = "<demo-006@qq.com>"
    msg["Auto-Submitted"] = "auto-generated"
    body = """你好：

你的邮箱已于 2026-10-23 08:00 开启 IMAP/SMTP 服务。
如非本人操作，请立即修改密码并删除授权码。

QQ邮箱团队
"""
    msg.set_content(body)
    save("06-p3-system-notify.eml", msg)


def main() -> None:
    for fn in (
        f01_customer_complaint,
        f02_boss_confirm,
        f03_weekly_report,
        f04_newsletter,
        f05_html_only,
        f06_system_notify,
    ):
        fn()
    print(f"\n共 {len(list(OUT.glob('*.eml')))} 封演示邮件，目录：{OUT}")


if __name__ == "__main__":
    main()
