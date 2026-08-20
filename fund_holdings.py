#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抓取基金季报十大持仓 -> 输出 fund_holdings.json

用途
----
本脚本从东方财富 fundf10 接口抓取各只基金的「季度报告 · 十大重仓股」，
整理成资产看板（asset-v2.html）可导入的 JSON 结构：

    {
      "000043": {
        "report": "2026-06-30",
        "items": [{"c": "AAPL", "n": "苹果", "p": 8.29, "m": "us"}, ...]
      },
      "016532": {"report": "跟踪纳斯达克100", "proxy": true, "items": [...]}
    }

字段含义
  report : 季报日期，格式 YYYY-MM-30（季末）；代理基金为文字说明
  items  : 十大持仓列表
           c = 证券代码, n = 名称, p = 占净值比例(%), m = 市场(us/hk/sh/sz)
  proxy  : true 表示用代理（如纳斯达克100ETF联接用 QQQ 代理，不抓东方财富）

用法
----
  # 默认输出 fund_holdings.json（基金列表见下方 DEFAULT_CODES）
  python3 fund_holdings.py

  # 指定输出文件
  python3 fund_holdings.py -o holdings.json

  # 用配置文件覆盖基金列表（见 fund_codes.json.example）
  python3 fund_holdings.py -c fund_codes.json

  # 命令行直接指定要抓的基金（逗号分隔，覆盖默认）
  python3 fund_holdings.py --codes 000043,270023

  # CI 中只想看结果、不写文件
  python3 fund_holdings.py --quiet --no-write

说明
----
  - 接口来自东方财富 fundf10，仅用于个人资产记录，请遵守其 robots / 频率限制。
  - 浏览器端直连该接口会被跨域拦截，因此改为「脚本/CI 抓取 -> 提交 JSON -> 看板导入」。
  - 本脚本只负责「抓取并产出 JSON」，不负责提交；提交由 GitHub Actions 完成。
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from html.parser import HTMLParser

# ---------------------------------------------------------------------------
# 默认基金列表（与 asset-v2.html 的 FUND_HOLDINGS 对齐）
# ---------------------------------------------------------------------------
DEFAULT_CODES = ["000043", "270023", "021277", "016532", "016533"]

# 走代理的基金：东方财富无有效季报，用 QQQ 代理，保持不动
DEFAULT_PROXY = {"016532": True, "016533": True}

QQQ_PROXY = {
    "report": "跟踪纳斯达克100",
    "proxy": True,
    "items": [{"c": "QQQ", "m": "us", "n": "纳斯达克100ETF", "p": 100}],
}

USER_AGENT = "Mozilla/5.0"
REFERER = "https://fundf10.eastmoney.com/"
BASE_URL = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc&code=%s&topline=11"


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def market_of(code):
    """根据证券代码判断市场。"""
    if re.match(r"^[A-Za-z]+$", code):
        return "us"
    if re.match(r"^\d{5}$", code) or re.match(r"^\d{4}[A-Za-z]$", code):
        return "hk"
    if re.match(r"^\d{6}$", code):
        return "sh" if code[:2] in ("60", "68", "90") else "sz"
    return "us"


def fetch_content(code, retries=3, timeout=30):
    """抓取单只基金的季报 HTML 片段，返回 (content, report)。失败返回 (None, None)。"""
    url = BASE_URL % code
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Referer": REFERER}
            )
            raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")
            m = re.search(r'content:"(.*?)"\s*,', raw, re.S)
            if not m:
                return None, None
            content = (
                m.group(1)
                .replace('\\"', '"')
                .replace("\\'", "'")
                .replace("\\n", "")
                .replace("\\t", "")
            )
            rep = re.search(r"(\d{4})年(\d)季度", raw)
            report = "%s-%02d-30" % (rep.group(1), int(rep.group(2)) * 3) if rep else ""
            return content, report
        except Exception as e:  # noqa: BLE001 - 网络异常统一重试
            last_err = e
            if attempt < retries:
                time.sleep(2 * attempt)  # 简单退避
    sys.stderr.write("  [异常] %s: %s\n" % (code, last_err))
    return None, None


class _TableParser(HTMLParser):
    """把季报表格解析成二维单元格列表。

    东方财富 jjcc 内容里通常含两张表：第一张是「十大重仓股」(占净值比例 %)，
    第二张是「持仓变动」(同列名但数值为市值/股数，会污染结果)。
    这里只收集【最外层第一张表】的行。
    """

    def __init__(self):
        super().__init__()
        self.rows = []
        self._tds = []
        self._in_td = False
        self._buf = ""
        self._table_depth = 0       # 当前 <table> 嵌套深度
        self._first_done = False    # 第一张表已结束则不再收集

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._table_depth += 1
        elif tag == "tr" and self._table_depth == 1 and not self._first_done:
            self._tds = []
        elif tag == "td" and self._table_depth == 1 and not self._first_done:
            self._in_td = True
            self._buf = ""

    def handle_data(self, data):
        if self._in_td:
            self._buf += data

    def handle_endtag(self, tag):
        if tag == "table":
            if self._table_depth == 1:
                self._first_done = True  # 第一张表结束
            self._table_depth = max(0, self._table_depth - 1)
        elif tag == "td" and self._table_depth == 1 and not self._first_done:
            self._tds.append(self._buf.strip())
            self._in_td = False
        elif tag == "tr" and self._table_depth == 1 and not self._first_done:
            if self._tds:
                self.rows.append(self._tds)


def parse(content):
    """从 content 解析出十大持仓 items 列表。"""
    p = _TableParser()
    p.feed(content)
    items = []
    for r in p.rows:
        if len(r) < 7:
            continue
        seq, code, name, pct = r[0], r[1], r[2], r[6]
        if not re.match(r"^\d+$", seq):
            continue
        if not code or code in ("--", ""):
            continue
        try:
            ratio = float(pct.replace("%", "").replace(",", ""))
        except ValueError:
            continue
        items.append(
            {"c": code, "n": name, "p": round(ratio, 2), "m": market_of(code)}
        )
    return items


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def load_config(path):
    """读取可选的 fund_codes.json 配置。返回 (codes, proxy) 或 (None, None)。"""
    if not path or not os.path.exists(path):
        return None, None
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    codes = cfg.get("codes")
    proxy = cfg.get("proxy", {})
    if not isinstance(codes, list) or not codes:
        raise ValueError("配置文件中 codes 必须为非空数组")
    return codes, {str(k): bool(v) for k, v in proxy.items()}


def scrape(codes, proxy, quiet=False):
    """抓取所有基金，返回结果 dict。"""
    result = {}
    for code in codes:
        code = str(code).strip()
        if not code:
            continue
        if proxy.get(code):
            result[code] = dict(QQQ_PROXY)
            if not quiet:
                sys.stderr.write("  [代理] %s 使用 QQQ 代理\n" % code)
            continue
        if not quiet:
            sys.stderr.write("  [抓取] %s ...\n" % code)
        try:
            content, report = fetch_content(code)
            if content:
                items = parse(content)
                if items:
                    result[code] = {"report": report, "items": items}
                else:
                    sys.stderr.write("  [空] %s 未解析到持仓\n" % code)
            else:
                sys.stderr.write("  [失败] %s 未取到内容\n" % code)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write("  [异常] %s: %s\n" % (code, e))
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="抓取基金季报十大持仓 -> fund_holdings.json"
    )
    ap.add_argument("-o", "--output", default="fund_holdings.json",
                    help="输出 JSON 路径（默认 fund_holdings.json）")
    ap.add_argument("-c", "--config", default=None,
                    help="基金列表配置文件 fund_codes.json")
    ap.add_argument("--codes", default=None,
                    help="逗号分隔的基金代码，覆盖默认列表")
    ap.add_argument("--no-write", action="store_true",
                    help="只打印 JSON，不写文件")
    ap.add_argument("--quiet", action="store_true",
                    help="减少 stderr 输出")
    ap.add_argument("--retries", type=int, default=3,
                    help="单只基金抓取失败重试次数（默认 3）")
    args = ap.parse_args(argv)

    fetch_content.__defaults__ = (args.retries, 30)

    # 确定基金列表：命令行 > 配置文件 > 默认
    codes, proxy = load_config(args.config)
    if codes is None:
        codes, proxy = list(DEFAULT_CODES), dict(DEFAULT_PROXY)
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        # 命令行指定时，代理仅保留命中者
        proxy = {c: proxy.get(c, False) for c in codes}

    codes = [c for c in codes if c not in (None, "")]
    if not codes:
        sys.stderr.write("错误：没有任何要抓取的基金代码\n")
        return 2

    result = scrape(codes, proxy, quiet=args.quiet)

    js = json.dumps(result, ensure_ascii=False, indent=2)

    if args.no_write:
        print(js)
    else:
        try:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(js)
            if not args.quiet:
                sys.stderr.write("\n已写入 %s\n" % args.output)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write("写文件失败：%s\n上方 JSON 仍可直接复制使用\n" % e)

    # 统计：成功（含代理）与失败（无 items）
    ok = sum(1 for v in result.values() if v.get("items"))
    failed = len([c for c in codes if c not in result or not result[c].get("items")])
    if not args.quiet:
        sys.stderr.write("成功 %d 只，失败 %d 只\n" % (ok, failed))

    # 全部失败则非零退出，方便 CI 判定
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
