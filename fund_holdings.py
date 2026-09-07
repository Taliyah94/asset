#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抓取基金季报十大持仓 -> 输出 fund_holdings.json

用途
----
本脚本从东方财富 fundf10 接口抓取各只基金的「季度报告 · 十大重仓股」，
topline=11（前十大；半年报虽有更全明细但不取，见下），
整理成资产看板（asset-v2.html）可导入的 JSON 结构：

    {
      "000043": {
        "report": "2026-06-30",
        "items": [{"c": "AAPL", "n": "苹果", "p": 8.29, "m": "us"}, ...],
        "nav": [[时间戳(ms), 单位净值], ...]   // 净值历史（紧凑二维数组，升序）；proxy 类基金无此字段
      },
      "016532": {"report": "跟踪纳斯达克100", "proxy": true, "items": [...]},

      // 非美股（港/A/日/韩）行情快照：每天存一次，看板按目标日取收盘价与当日涨跌
      "_daily_quotes": {
        "kr005930": [{"date": "20260904", "close": 255500, "prevClose": 250000,
                      "ts": "2026-09-04 14:30:05"}, ...],   // 升序，最多 5 条
        "hk02513":  [{"date": "20260904", "close": 1075, "prevClose": 1108,
                      "ts": "2026/09/04 16:08:05"}, ...]
      },

      // QQQ 日线历史（曲线图「QQQ 涨跌幅(基准)」对比线用）：每天刷新，覆盖组合成立日至今
      "qqq_daily": [{"d": "2026-02-06", "c": 518.20}, ...]   // 升序，{d:日期, c:收盘价}
    }

字段含义
  report : 季报日期，格式 YYYY-MM-30（季末）；代理基金为文字说明
  items  : 十大持仓列表（按占净值比例降序，固定前 10 条）
           c = 证券代码, n = 名称, p = 占净值比例(%), m = 市场(us/hk/sh/sz/jp/kr/tw)
           注：半年报会披露 20-30 条完整明细（含 ISIN 全码/台股），本脚本刻意不取；
           market_of 仍保留 ISIN/台股识别，以备个别前十大里出现这类代码
  proxy  : true 表示用代理（如纳斯达克100ETF联接用 QQQ 代理，不抓东方财富）

_daily_quotes（顶层，非基金键，前端按基金代码遍历时会被自然忽略）
  看板原本靠东方财富 push2his 取港/A/日/韩的历史日K，但该接口对日股(176.)/韩股(177.)
  恒返回空，且每次打开页面都要现抓一遍（慢、且受跨域与限流影响）。
  改为每天用腾讯 qt.gtimg.cn 存一次「现价/昨收/行情时间」，攒出近 5 个交易日的序列，
  看板直接按目标日取 close 当基准、close/prevClose-1 当当日涨跌，无需再请求东方财富。
  同日期覆盖，抓取失败保留旧值。

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
DEFAULT_CODES = ["000043", "270023", "021277", "016532", "016533","539002"]

# 走代理的基金：东方财富无有效季报，用 QQQ 代理，保持不动
DEFAULT_PROXY = {"016532": True, "016533": True}

QQQ_PROXY = {
    "report": "跟踪纳斯达克100",
    "proxy": True,
    "items": [{"c": "QQQ", "m": "us", "n": "纳斯达克100ETF", "p": 100}],
}

USER_AGENT = "Mozilla/5.0"
REFERER = "https://fundf10.eastmoney.com/"
BASE_URL = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc&code=%s&topline=10"


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
# 东方财富行情链接里的市场前缀（//quote.eastmoney.com/unify/r/<前缀>.<代码>）。
# 这是最可靠的市场线索：A股/港股/美股都会被东财收录并带链接；
# 日股/韩股东财【未收录】（无链接、data-texch 为空），只能靠代码格式或名称推断。
EM_MARKET_MAP = {
    "105": "us",   # 纳斯达克（NVDA / MU / AVGO ...）
    "106": "us",   # 纽交所（TSM / LLY / GLW ...）
    "116": "hk",   # 港股（02513 智谱 ...）
    "0": "sz",     # 深圳（300408 三环集团 ...）
    "1": "sh",     # 上海（600519 贵州茅台 ...）
}

# 东财未收录时的兜底名单（韩国6位代码与A股6位代码格式完全相同，无法从格式区分）
KNOWN_JP_CODES = {"285A", "4004"}      # 铠侠 KIOXIA / Resonac（东京证交所）
KNOWN_KR_CODES = {"000660", "005930"}  # SK海力士 / 三星电子（韩国交易所）
JP_NAME_KEYS = ("kioxia", "铠侠")
KR_NAME_KEYS = ("海力士", "三星", "sk hynix", "samsung")
KNOWN_TW_CODES = {"2317", "2383", "3711"}  # 鸿海 / 台光电 / 日月光投控（台湾证交所）
TW_NAME_KEYS = ("鸿海", "日月光", "台光电")

# 未收录且名单未命中时，是否用腾讯行情接口反查市场（可识别新增日韩股，需联网）
ENABLE_QT_PROBE = True

# ---------------------------------------------------------------------------
# 非美股（港/A/日/韩）行情快照
# ---------------------------------------------------------------------------
# 东方财富 push2his 对日股(176.) / 韩股(177.) 恒返回空 data，取不到历史日K；
# 港股/A股虽然能取到，但每次打开页面都要现抓一遍，慢且依赖跨域接口。
# 腾讯 qt.gtimg.cn 能给出「现价 / 昨收 / 行情时间」但没有历史K。
# 因此每天跑一次，把当天快照存进 JSON，攒出近 N 个交易日的序列供看板按目标日取值。
# 美股不在此列：盘前/盘后与 Bybit 实时价另有一套逻辑，继续走腾讯实时。
SNAPSHOT_MARKETS = ("hk", "sz", "sh", "jp", "kr")
SNAPSHOT_DAYS = 5                # 每个代码保留最近几个交易日
SNAPSHOT_KEY = "_daily_quotes"   # 顶层键名（非 6 位基金代码，前端遍历时会被忽略）
SNAPSHOT_LEGACY_KEYS = ("_jpkr_quotes",)  # 旧键名，读取时兼容合并后不再写回
QT_BATCH = 20                    # 腾讯行情单次批量查询的代码数


def probe_market(code, timeout=6):
    """用腾讯行情反查代码所属市场：依次试 kr/jp/hk/sh/sz，返回首个有行情的市场。

    看板最终就是用 qt.gtimg.cn 读这些代码，所以以它为准最可靠；失败返回 None。
    """
    for mk in ("kr", "jp", "hk", "sh", "sz"):
        try:
            req = urllib.request.Request(
                "https://qt.gtimg.cn/q=%s%s" % (mk, code),
                headers={"User-Agent": USER_AGENT},
            )
            txt = urllib.request.urlopen(req, timeout=timeout).read().decode("gbk", "ignore")
            if ('v_%s%s="' % (mk, code)) in txt and "pv_none_match" not in txt:
                return mk
        except Exception:  # noqa: BLE001 - 探测失败即换下一个市场
            continue
    return None


def market_of(code, name="", em_prefix=None):
    """判断证券市场，返回 us/hk/sh/sz/jp/kr。

    判定优先级：
      1. 东财行情链接前缀（最可靠，A股/港股/美股均被收录）
      2. 日股/韩股代码格式与已知名单（东财未收录的场景）
      3. 腾讯行情反查（解决韩国6位代码与A股6位代码格式冲突）
      4. 纯代码格式兜底（完全没有东财线索时的旧逻辑）
    """
    code = (code or "").strip()
    nm = (name or "").lower()

    # 1) 东财行情链接前缀
    if em_prefix and str(em_prefix) in EM_MARKET_MAP:
        return EM_MARKET_MAP[str(em_prefix)]

    # 2) 东财未收录：多为日股/韩股。日股代码形如 285A（4位数字+字母）
    if re.match(r"^\d{4}[A-Za-z]$", code):
        return "jp"
    if code in KNOWN_JP_CODES or any(k in nm for k in JP_NAME_KEYS):
        return "jp"
    if code in KNOWN_KR_CODES or any(k in nm for k in KR_NAME_KEYS):
        return "kr"

    # 2.5) ISIN 全码（topline>10 的半年报明细里部分日韩/台股只给 ISIN，
    #      如 JP3914400001 村田制作所 / KR7009150004 三星电机）
    m_isin = re.match(r"^([A-Z]{2})[A-Z0-9]{9}\d$", code)
    if m_isin:
        return {"JP": "jp", "KR": "kr", "TW": "tw"}.get(m_isin.group(1), "us")
    # 台股（腾讯无行情，仅标注市场，前端涨跌显示 -- 不参与加权）
    if code in KNOWN_TW_CODES or any(k in nm for k in TW_NAME_KEYS):
        return "tw"

    # 3) 6位数字：韩股与A股格式完全相同，用腾讯行情反查（失败则回落第 4 步）
    if ENABLE_QT_PROBE and re.match(r"^\d{6}$", code):
        probed = probe_market(code)
        if probed:
            return probed

    # 4) 纯代码格式兜底
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


def fetch_nav(code, retries=3, timeout=30):
    """抓取基金净值历史 Data_netWorthTrend，返回 [[t_ms, nav], ...]（紧凑、升序）或 None。

    数据源与浏览器端一致：fund.eastmoney.com/pingzhongdata/{code}.js，
    其中 Data_netWorthTrend = [{"x": 时间戳(ms), "y": 单位净值, ...}, ...]。
    仅取 (x, y) 两列，单位净值 <=0 的脏数据丢弃；输出紧凑二维数组 [[t_ms, nav], ...]。
    """
    url = "https://fund.eastmoney.com/pingzhongdata/%s.js" % code
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Referer": "https://fundf10.eastmoney.com/"}
            )
            raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")
            m = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\])\s*;", raw, re.S)
            if not m:
                return None
            arr = json.loads(m.group(1))
            out = []
            for row in arr:
                if not isinstance(row, dict):
                    continue
                t, nav = row.get("x"), row.get("y")
                if not isinstance(nav, (int, float)) or not isinstance(t, (int, float)):
                    continue
                if nav <= 0:
                    continue
                out.append([int(t), round(float(nav), 4)])
            return out if out else None
        except Exception as e:  # noqa: BLE001 - 网络异常统一重试
            last_err = e
            if attempt < retries:
                time.sleep(2 * attempt)
    sys.stderr.write("  [nav异常] %s: %s\n" % (code, last_err))
    return None


def collect_snapshot_symbols(result):
    """从抓取结果里收集非美股（港/A/日/韩）的腾讯行情代码，如 hk02513 / sz300408 / jp285A。

    跳过 ISIN 全码（如 JP3914400001）：腾讯不认，查了也是空。
    """
    isin = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")
    syms = []
    for entry in (result or {}).values():
        if not isinstance(entry, dict):
            continue
        for it in entry.get("items") or []:
            mk = (it or {}).get("m")
            code = (it or {}).get("c", "")
            if mk in SNAPSHOT_MARKETS and not isin.match(code):
                # 代码保留原始大小写：日股 285A 末位大写，腾讯对大小写敏感（jp285a 查不到）
                s = "%s%s" % (mk, code)
                if s not in syms:
                    syms.append(s)
    return syms


def fetch_qt_quotes(syms, retries=3, timeout=15):
    """批量拉腾讯行情，返回 {sym: {"date","close","prevClose","ts"}}。

    qt.gtimg.cn 字段：p[3] 现价 / p[4] 昨收 / p[30] 行情时间。
    时间格式两种：A股 "20260828161406"、日韩股 "2026-09-04 14:30:29"，
    统一取前 8 位数字当日期（YYYYMMDD），便于字符串比较。
    单个代码失败不影响其他代码。
    """
    out = {}
    if not syms:
        return out
    for i in range(0, len(syms), QT_BATCH):
        chunk = syms[i:i + QT_BATCH]
        url = "https://qt.gtimg.cn/q=" + ",".join(chunk)
        txt = None
        for attempt in range(1, retries + 1):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                txt = urllib.request.urlopen(req, timeout=timeout).read().decode("gbk", "ignore")
                break
            except Exception as e:  # noqa: BLE001 - 网络异常统一重试
                if attempt == retries:
                    sys.stderr.write("  [行情失败] %s: %s\n" % (",".join(chunk), e))
                else:
                    time.sleep(2 * attempt)
        if not txt:
            continue
        for sym in chunk:
            m = re.search('v_%s="([^"]*)"' % re.escape(sym), txt)
            if not m:
                continue
            p = m.group(1).split("~")
            if len(p) < 31:
                continue
            try:
                close, prev = float(p[3]), float(p[4])
            except (ValueError, IndexError):
                continue
            if close <= 0:
                continue
            date = re.sub(r"\D", "", p[30])[:8]
            if len(date) != 8:
                continue
            out[sym] = {
                "date": date,
                "close": round(close, 4),
                "prevClose": round(prev, 4) if prev > 0 else None,
                "ts": p[30],
            }
    return out


def load_existing_snapshots(path):
    """读取已存盘 JSON 里的快照，兼容旧键 _jpkr_quotes（合并后只按新键写回）。"""
    out = {}
    if not (path and os.path.exists(path)):
        return out
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
    except Exception as e:  # noqa: BLE001 - 旧文件损坏则当空，不阻断主流程
        sys.stderr.write("  [快照] 读取旧文件失败，本次不累积：%s\n" % e)
        return out
    for key in (SNAPSHOT_KEY,) + SNAPSHOT_LEGACY_KEYS:
        part = obj.get(key)
        if not isinstance(part, dict):
            continue
        for sym, rows in part.items():
            if not isinstance(rows, list):
                continue
            by_date = {}
            for r in out.get(sym, []) + rows:
                if isinstance(r, dict) and r.get("date"):
                    by_date[r["date"]] = r
            if by_date:
                out[sym] = [by_date[d] for d in sorted(by_date)]
    return out


def merge_quote_snapshots(old, new, keep=SNAPSHOT_DAYS):
    """合并新旧快照：同日期覆盖，按日期升序，只留最近 keep 条。

    old 为已存盘的结构 {"kr005930": [{date, close, prevClose, ts}, ...]}，
    new 为本次抓取结果；抓取失败（new 缺某个 sym）时该 sym 原样保留。
    """
    merged = {}
    if isinstance(old, dict):
        for k, v in old.items():
            if isinstance(v, list):
                rows = [x for x in v if isinstance(x, dict) and x.get("date")]
                if rows:
                    merged[k] = rows
    for sym, rec in (new or {}).items():
        rows = [x for x in merged.get(sym, []) if x.get("date") != rec["date"]]
        rows.append(rec)
        rows.sort(key=lambda x: x["date"])
        merged[sym] = rows[-keep:]
    return merged


class _TableParser(HTMLParser):
    """把季报表格解析成二维单元格列表。

    东方财富 jjcc 内容里通常含两张表：第一张是「十大重仓股」(占净值比例 %)，
    第二张是「持仓变动」(同列名但数值为市值/股数，会污染结果)。
    这里只收集【最外层第一张表】的行。
    """

    def __init__(self):
        super().__init__()
        self.rows = []
        self.row_markets = []       # 每行从行情链接里解析出的东财市场前缀（未收录则为 None）
        self._tds = []
        self._td_markets = []
        self._td_market = None
        self._in_td = False
        self._buf = ""
        self._table_depth = 0       # 当前 <table> 嵌套深度
        self._first_done = False    # 第一张表已结束则不再收集

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._table_depth += 1
        elif tag == "tr" and self._table_depth == 1 and not self._first_done:
            self._tds = []
            self._td_markets = []
        elif tag == "td" and self._table_depth == 1 and not self._first_done:
            self._in_td = True
            self._buf = ""
            self._td_market = None
        elif tag == "a" and self._in_td:
            # 行情链接形如 //quote.eastmoney.com/unify/r/105.MU，前缀即市场
            m = re.search(r"unify/r/(\d+)\.", dict(attrs).get("href", "") or "")
            if m:
                self._td_market = m.group(1)

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
            self._td_markets.append(self._td_market)
            self._in_td = False
        elif tag == "tr" and self._table_depth == 1 and not self._first_done:
            if self._tds:
                self.rows.append(self._tds)
                self.row_markets.append(self._td_markets)


def parse(content):
    """从 content 解析出十大持仓 items 列表。"""
    p = _TableParser()
    p.feed(content)
    items = []
    for i, r in enumerate(p.rows):
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
        # 东财市场前缀：取本行首个行情链接的前缀；为 None 表示东财未收录（多为日股/韩股）
        mkts = p.row_markets[i] if i < len(p.row_markets) else []
        em_prefix = next((x for x in mkts if x), None)
        items.append(
            {"c": code, "n": name, "p": round(ratio, 2),
             "m": market_of(code, name, em_prefix)}
        )
    return items


# ---------------------------------------------------------------------------
# QQQ 日线历史（曲线图 TWR 基准对比用）
# ---------------------------------------------------------------------------
# 看板的「曲线图」会把 QQQ 当日净值归一化为累计涨跌幅，与组合的 TWR 放在同一
# 根收益率轴上对比（谁是基准、谁跑赢一目了然）。这需要 QQQ 的每日收盘价序列。
#
# 数据源：优先腾讯 appstock fqkline（与脚本其余行情同源），但该接口对美股「带起
# 止日期」查询会退化、且「最近 N 条」模式对美股只返回首末两条，拿不到连续历史；
# 故回退新浪美股日K（US_MinKService.getDailyK），实测返回 2001 年至今完整日线。
# 每天跑一次会重新抓取全量并更新，曲线图据此自动刷新。
QQQ_SINA = "https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var%20_/US_MinKService.getDailyK?symbol=QQQ&___qn=3&_=1"


def fetch_qqq_daily(retries=3, timeout=30):
    """抓取 QQQ 日线历史，返回 [{"d": "YYYY-MM-DD", "c": 收盘价}, ...]（升序）。

    先试腾讯，失败回退新浪。两者都失败返回 None（不阻断主流程）。
    """
    last_err = None
    # 1) 腾讯 appstock fqkline（同源；仅作为首选项，美股历史可能不全）
    for attempt in range(1, retries + 1):
        try:
            url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=usQQQ,day,,,800,qfq"
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Referer": "https://gu.qq.com/"}
            )
            raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")
            obj = json.loads(raw)
            day = obj.get("data", {}).get("usQQQ", {}).get("day", [])
            out = []
            for row in day:
                if not isinstance(row, (list, dict)):
                    continue
                d = row[0] if isinstance(row, list) else row.get("date")
                c = row[2] if isinstance(row, list) else row.get("close")
                try:
                    close = float(c)
                except (ValueError, TypeError):
                    continue
                if close <= 0 or not d:
                    continue
                out.append({"d": d, "c": round(close, 2)})
            if len(out) >= 5:  # 腾讯对美股常只给首末两条，不足以做连续对比
                out.sort(key=lambda x: x["d"])
                return out
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < retries:
                time.sleep(2 * attempt)
    # 2) 新浪美股日K（完整历史）
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                QQQ_SINA, headers={"User-Agent": USER_AGENT, "Referer": "https://stock.finance.sina.com.cn/"}
            )
            raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")
            i, j = raw.find("["), raw.rfind("]")
            if i < 0 or j < 0:
                continue
            arr = json.loads(raw[i:j + 1])
            out = []
            for x in arr:
                d = x.get("d")
                c = x.get("c")
                if not d or c is None:
                    continue
                try:
                    close = float(c)
                except (ValueError, TypeError):
                    continue
                if close <= 0:
                    continue
                out.append({"d": d, "c": round(close, 2)})
            if out:
                out.sort(key=lambda x: x["d"])
                return out
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < retries:
                time.sleep(2 * attempt)
    sys.stderr.write("  [QQQ] 抓取失败: %s\n" % last_err)
    return None


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
                    nav = fetch_nav(code)
                    entry = {"report": report, "items": items}
                    if nav:
                        entry["nav"] = nav
                    result[code] = entry
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
    ap.add_argument("--no-snapshots", action="store_true",
                    help="跳过日股/韩股行情快照抓取")
    ap.add_argument("--no-qqq", action="store_true",
                    help="跳过 QQQ 日线抓取（曲线图基准对比用）")
    ap.add_argument("--snapshot-days", type=int, default=SNAPSHOT_DAYS,
                    help="行情快照保留天数（默认 %d）" % SNAPSHOT_DAYS)
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

    # 非美股（港/A/日/韩）行情快照：每天存一条，攒出近 N 个交易日序列供看板直接读取
    if not args.no_snapshots:
        syms = collect_snapshot_symbols(result)
        if syms:
            quotes = fetch_qt_quotes(syms)
            if not args.quiet:
                sys.stderr.write("  [快照] 港/A/日/韩 %d 个代码，取到 %d 个\n" % (len(syms), len(quotes)))
            # 读取已存盘文件里的旧快照，合并后回写（--no-write 时也能累积）
            old = load_existing_snapshots(args.output)
            merged = merge_quote_snapshots(old, quotes, keep=max(1, args.snapshot_days))
            if merged:
                result[SNAPSHOT_KEY] = merged
        elif not args.quiet:
            sys.stderr.write("  [快照] 无非美股持仓，跳过\n")

    # QQQ 日线历史（曲线图 TWR 基准对比）
    if not args.no_qqq:
        qqq = fetch_qqq_daily()
        if qqq:
            result["qqq_daily"] = qqq
            if not args.quiet:
                sys.stderr.write("  [QQQ] 日线 %d 条（%s ~ %s）\n" % (
                    len(qqq), qqq[0]["d"], qqq[-1]["d"]))
        elif not args.quiet:
            sys.stderr.write("  [QQQ] 未取得日线，跳过\n")

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

    # 统计：成功（含代理）与失败（无 items）。跳过顶层非基金键（qqq_daily / _daily_quotes）
    NON_FUND_KEYS = (SNAPSHOT_KEY,) + SNAPSHOT_LEGACY_KEYS + ("qqq_daily",)
    ok = sum(1 for k, v in result.items()
             if k not in NON_FUND_KEYS and isinstance(v, dict) and v.get("items"))
    failed = len([c for c in codes if c not in result or not result[c].get("items")])
    if not args.quiet:
        sys.stderr.write("成功 %d 只，失败 %d 只\n" % (ok, failed))

    # 全部失败则非零退出，方便 CI 判定
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
