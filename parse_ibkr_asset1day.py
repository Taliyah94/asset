#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
parse_ibkr_asset1day.py — 盈透证券(IBKR)每日资产快照同步脚本

用途
----
通过 IBKR Flex Web Service 接口，用 token + queryId 下载「1day」Flex 报表 XML，
解析出当日的总净值 / 现金 / 持仓，并将其 **追加/覆盖** 写入仓库根的 Asset_parsed.json：

  totalNetValueDaily[]   开户至今每日总净值   {date:'YYYY-MM-DD', value:Number}
  cashDaily[]            开户至今每日现金(USD) {date:'YYYY-MM-DD', value:Number}
  ibkrAccruedInterest   盈透现金应计利息（取自最新报表日 interestAccruals），
                        前端据此并入「盈透现金」；每日同步自动刷新，免手工改。
  holdings[]             当前持仓（symbol/position/costBasisPrice/markPrice ...）

其余字段（account / electronicFundTransfers / forexTrades）与原 365day 解析脚本
(parse_ibkr_asset.py) 保持一致——本脚本只新增/更新「每日」维度，不破坏既有结构。

运行方式
--------
  # 标准（GitHub Actions / 定时任务）：用环境变量取密钥，联网下载
  IBKR_TOKEN=xxx IBKR_QUERY_ID_1DAY=yyy IBKR_QUERY_ID_ASSET=zzz python parse_ibkr_asset1day.py

  # 本地调试：直接解析本地 XML（不联网），便于验证逻辑
  python parse_ibkr_asset1day.py --xml Asset-1day.xml [--json Asset_parsed.json]

环境变量
--------
  IBKR_TOKEN              盈透 Flex Web Service token（必填，除非 --xml）
  IBKR_QUERY_ID_1DAY      「1day」Flex 报表的 queryId（必填，除非 --xml）
  IBKR_QUERY_ID_ASSET     「365day/资产」Flex 报表的 queryId（可选；
                          提供时同时刷新 account/forexTrades，否则沿用旧值）
  JSON_PATH              输出 json 路径（默认：脚本同目录 Asset_parsed.json）
  IBKR_BASE_URL          Flex Web Service 基地址（一般无需改）

说明
----
- 1day 报表的 EquitySummaryByReportDateInBase 只含最近 1~2 天；本脚本按 (date) 去重覆盖，
  历史日保留原值、只更新/新增与 1day 报表重合或在其之后的日期。
- 持仓只取 1day 报表最新 reportDate 的快照（覆盖式更新），不保留历史持仓。
- 若当日数据尚未生成（如周末/休市，报表可能不含今天），脚本不会凭空造数据，
  仅保留已下载到的日期；缺失日期由前一次运行或历史 365day 数据补足。

调度说明（2026-08-10 调整）
--------------------------
- 由 GitHub Actions 每 30 分钟一个独立 cron 触发：
  「02:00 / 02:30 / 03:00 / 03:30 / 04:00 / 04:30 / 05:00 UTC」
  = 北京时间 10:00 ~ 13:00，每个整半点各跑一次。
- 美股收盘后 IBKR 不会立即更新报表，通常约北京时间 10:00 才就绪；
  早到的 cron 会因「报表未刷新」直接退出，等下一个 cron（30 分钟后）再试，
  平时任务只需 1~2 分钟，不空耗 Actions 时长。
- 脚本用 JSON 里的 lastSync（北京时间日期）标记「今日已同步」：
  一旦当日任一次成功，后续 cron 直接跳过；次日自动解除。
  手动在 Actions 页面触发时可勾选 force 强制重跑。
- 05:00 UTC（13:00 北京时间）是当日最后一次尝试；仍失败则放弃，次日 02:00 再开始。
"""
import xml.etree.ElementTree as ET
import json
import os
import sys
import time
import urllib.request
import urllib.error
import argparse
import datetime
import calendar
import re

# ----------------------------- 配置 -----------------------------
IBKR_SEND_URL = os.environ.get(
    "IBKR_SEND_URL",
    "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.SendRequest")
IBKR_GET_URL = os.environ.get(
    "IBKR_GET_URL",
    "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.GetStatement")
DEFAULT_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "Asset_parsed.json")


# ----------------------------- 工具 -----------------------------
def ymd(s):
    """'20260206' -> '2026-02-06'；非 8 位数字原样返回"""
    s = (s or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return s


def fnum(s):
    s = (s or "").strip()
    if s == "":
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def http_get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "ibkr-asset-sync/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def beijing_now():
    """当前北京时间（UTC+8）。GitHub Actions runner 默认 UTC，统一用此函数换算。"""
    return datetime.datetime.now(datetime.timezone.utc).astimezone(
        datetime.timezone(datetime.timedelta(hours=8)))


# ----------------------- Flex Web Service -----------------------
def request_flex_xml(token, query_id, max_wait=540, interval=20, send_retries=5):
    """
    调用 IBKR Flex Web Service（两步式）：
      1) SendRequest -> 拿到 ReferenceCode
      2) GetStatement 轮询 -> 取到完整报表 XML
    返回报表 XML 字符串；失败抛异常。

    关键经验：
    - IBKR Flex 接口经常返回「Statement could not be generated at this time.
      Please try again shortly.」这类**瞬时**错误（报表尚未生成/批量时段），
      需做指数退避重试——但**单轮只试几次即可**，真正的重试交给「每 30 分钟
      一个 cron 窗口」自然触发，不必一轮内猛锤。
    - 单轮 SendRequest 过于频繁会触发 IBKR 频率限制「Too many failed
      attempts…」，该限制会把当天后续所有 cron 窗口都堵死。因此命中频率限制
      后本轮立即放弃，等下一个 cron 窗口（30 分钟后）再试。
    """
    TRANSIENT = ("could not be generated", "try again", "please wait",
                 "in progress", "in queue", "1018", "1009", "temporarily")
    # 频率限制：命中后本轮立即放弃，等下一个 cron 窗口，避免越限越多把当天堵死
    RATELIMIT = ("too many failed attempts", "review your configuration",
                 "rate limit", "too many requests", "exceeded")

    # 1) 提交报表请求（瞬时错误需多次退避重试）
    submit = f"{IBKR_SEND_URL}?t={token}&q={query_id}&v=3"
    code = None
    last_err = ""
    wait = interval
    for attempt in range(1, send_retries + 1):
        try:
            txt = http_get(submit)
        except Exception as e:  # 网络抖动也计入重试
            last_err = f"网络异常: {e}"
            print(f"[flex] SendRequest 第{attempt}次网络异常，{wait}s 后重试")
            time.sleep(wait)
            wait = min(wait * 2, 60)
            continue
        rc = txt.split("<ReferenceCode>")[1].split("</ReferenceCode>")[0] \
            if "<ReferenceCode>" in txt else None
        err = txt.split("<ErrorMessage>")[1].split("</ErrorMessage>")[0] \
            if "<ErrorMessage>" in txt else None
        if rc:
            code = rc
            break
        last_err = err or txt[:300]
        # 命中频率限制：本轮放弃（不要继续空耗请求，否则会锁死当天后续窗口）
        if any(k in last_err.lower() for k in RATELIMIT):
            raise RuntimeError(f"Flex 触发频率限制（{last_err[:80]}），"
                               f"本窗口放弃，等下一个 cron")
        is_transient = any(k in last_err.lower() for k in TRANSIENT)
        if is_transient and attempt < send_retries:
            print(f"[flex] SendRequest 第{attempt}次瞬时错误（{last_err[:80]}），"
                  f"{wait}s 后重试")
            time.sleep(wait)
            wait = min(wait * 2, 60)
            continue
        # 非瞬时错误（如 Invalid request）：立即失败
        raise RuntimeError(f"Flex SendRequest 失败: {last_err}")
    if not code:
        raise RuntimeError(f"Flex SendRequest 重试{send_retries}次仍失败: {last_err}")

    # 2) 取回报表（可能还在生成，需轮询 + 退避）
    deadline = time.time() + max_wait
    get_url = f"{IBKR_GET_URL}?t={token}&q={code}&v=3"
    gwait = interval
    while time.time() < deadline:
        try:
            txt = http_get(get_url)
        except Exception as e:
            print(f"[flex] GetStatement 网络异常，{gwait}s 后重试: {e}")
            time.sleep(gwait)
            gwait = min(gwait * 2, 60)
            continue
        if "<ErrorMessage>" in txt:
            msg = txt.split("<ErrorMessage>")[1].split("</ErrorMessage>")[0]
            last_err = msg
            is_transient = any(k in msg.lower() for k in TRANSIENT)
            if is_transient and time.time() + gwait < deadline:
                print(f"[flex] 报表仍在生成（{msg[:60]}），{gwait}s 后轮询")
                time.sleep(gwait)
                gwait = min(gwait * 2, 60)
                continue
            if not is_transient:  # 硬错误
                raise RuntimeError(f"Flex GetStatement 失败: {msg}")
        # 成功返回包含报表数据（FlexStatementResponse / FlexQueryResponse）
        if "<EquitySummaryByReportDateInBase" in txt or "<FlexQueryResponse" in txt \
                or "<FlexStatementResponse" in txt:
            return txt
        time.sleep(gwait)
    raise RuntimeError(f"Flex GetStatement 超时等待报表（最后错误: {last_err}）")


# ----------------------------- 解析 -----------------------------
def parse_equity(equity_root):
    """从 EquitySummaryInBase 解析每日总净值/现金/应计利息。
    返回 dict: date -> {total, cash, accruedInterest}
      accruedInterest 取自 interestAccruals（盈透现金应计利息，如持仓过夜利息）。"""
    out = {}
    for e in equity_root.findall(".//EquitySummaryInBase/"
                                 "EquitySummaryByReportDateInBase"):
        rd = e.get("reportDate", "")
        if not rd:
            continue
        out[ymd(rd)] = {
            "total": fnum(e.get("total")),
            "cash": fnum(e.get("cash")),
            "accruedInterest": fnum(e.get("interestAccruals")),
        }
    return out


def parse_holdings(root):
    """从最新 OpenPosition 报表日解析当前持仓快照"""
    latest = None
    pos_by_date = {}
    for p in root.findall(".//OpenPositions/OpenPosition"):
        rd = ymd(p.get("reportDate", ""))
        pos_by_date.setdefault(rd, []).append(p)
        if latest is None or rd > latest:
            latest = rd
    holdings = []
    for p in pos_by_date.get(latest, []):
        holdings.append({
            "symbol": p.get("symbol"),
            "side": p.get("side"),
            "position": fnum(p.get("position")),
            "costBasisPrice": fnum(p.get("costBasisPrice")),
            "markPrice": fnum(p.get("markPrice")),
            "positionValue": fnum(p.get("positionValue")),
            "costBasisMoney": fnum(p.get("costBasisMoney")),
            "unrealizedPnl": fnum(p.get("fifoPnlUnrealized")),
        })
    return holdings, latest


# ----------------------------- 交易明细 -----------------------------
def parse_trades(root):
    """
    从 Flex 报表解析全部 <Trade>，返回标准化交易明细列表。
    字段：date(YYYY-MM-DD) / symbol / underlying / cat / buySell /
          quantity(原始带符号) / price / realized(fifoPnlRealized) / tradeId
    仅做基础清洗，时间窗口裁剪（近一月）与去重交给 merge_trades。
    """
    out = []
    for t in root.findall(".//Trade"):
        td = ymd(t.get("tradeDate") or "")
        if not td:
            continue
        sym = (t.get("symbol") or "").strip()
        underlying = (t.get("underlyingSymbol") or "").strip() or sym
        cat = (t.get("assetCategory") or "").strip()
        bs = (t.get("buySell") or "").strip().upper()
        qty = fnum(t.get("quantity"))
        price = fnum(t.get("tradePrice"))
        realized = fnum(t.get("fifoPnlRealized")) or 0.0
        tid = t.get("tradeID") or None
        out.append({
            "date": td,
            "symbol": sym,
            "underlying": underlying,
            "cat": cat,
            "buySell": bs,
            "quantity": qty,
            "price": price,
            "realized": round(realized, 2),
            "tradeId": tid,
        })
    return out


def one_month_ago(ref):
    """ref: 'YYYY-MM-DD' -> 该日期往前推 1 个月的 'YYYY-MM-DD'（自动处理跨年/月末）。"""
    y, m, d = map(int, ref.split("-"))
    m -= 1
    if m <= 0:
        m += 12
        y -= 1
    last = calendar.monthrange(y, m)[1]
    d = min(d, last)
    return f"{y:04d}-{m:02d}-{d:02d}"


def merge_trades(existing, new, cutoff):
    """
    合并交易明细：按 tradeId 去重（无 tradeId 则用整行指纹），丢弃 cutoff 之前的记录。
    existing/new: list[dict]。返回按日期倒序的 list。
    """
    by_key = {}
    order = []

    def add(tr):
        key = tr.get("tradeId") or json.dumps(tr, sort_keys=True, ensure_ascii=False)
        if key in by_key:
            return
        by_key[key] = tr
        order.append(tr)

    for tr in (existing or []):
        add(tr)
    for tr in (new or []):
        add(tr)
    kept = [tr for tr in order if (tr.get("date") or "") >= cutoff]
    kept.sort(key=lambda x: (x.get("date") or ""), reverse=True)
    return kept


# ----------------------------- 合并 -----------------------------
def merge_daily(existing, new_map):
    """
    existing: list[{date,value}]  ->  按 date 去重覆盖 new_map(date->{total,cash})
    返回 (totalNetValueDaily, cashDaily) 两个 list，按 date 升序。
    """
    tot_map, cash_map = {}, {}
    for row in (existing.get("totalNetValueDaily") or []):
        tot_map[row["date"]] = row["value"]
    for row in (existing.get("cashDaily") or []):
        cash_map[row["date"]] = row["value"]
    for d, v in new_map.items():
        if v.get("total") is not None:
            tot_map[d] = v["total"]
        if v.get("cash") is not None:
            cash_map[d] = v["cash"]
    total = [{"date": d, "value": tot_map[d]} for d in sorted(tot_map)]
    cash = [{"date": d, "value": cash_map[d]} for d in sorted(cash_map)]
    return total, cash


def fetch_validate(token, query_id, result):
    """
    下载并校验 1day 报表是否已「刷新到当日」。
    成功返回报表 XML 字符串；若 IBKR 未就绪（瞬时错误 / 报表陈旧）则抛异常，
    由调用方决定延时重试。
    """
    xml_txt = request_flex_xml(token, query_id)
    root = ET.fromstring(xml_txt)
    eq_map = parse_equity(root)
    if not eq_map:
        raise RuntimeError("报表无 EquitySummary 数据（IBKR 可能尚未生成）")
    latest_date = max(eq_map.keys())
    existing_latest = max(
        (row["date"] for row in (result.get("totalNetValueDaily") or [])),
        default=None)
    # 仅当 IBKR 返回的报表比已同步的还“旧”时才跳过（避免用陈旧数据覆盖）；
    # 相等（如北京时间 08-11 拉到的正是美东 08-10）也要允许覆盖写入。
    if existing_latest and latest_date < existing_latest:
        raise RuntimeError(
            f"报表最新日期 {latest_date} 早于已同步 {existing_latest}，"
            f"IBKR 拉到了陈旧数据，跳过本次写入")
    return xml_txt


def do_parse_and_write(xml_txt, result, json_path):
    """解析 1day 报表并写回 Asset_parsed.json（账户/每日净值/现金/持仓/换汇）。"""
    root = ET.fromstring(xml_txt)

    # ---- 账户信息（仅当含 AccountInformation 时更新）----
    ai = root.find(".//AccountInformation")
    if ai is not None:
        a = ai.attrib
        result["account"] = {
            "accountId": a.get("accountId"),
            "accountName": a.get("name"),
            "currency": a.get("currency"),
            "openDate": ymd(a.get("dateOpened")),
            "lastTradeDate": ymd(a.get("lastTradedDate")),
        }

    # ---- 每日总净值 / 现金：追加覆盖 ----
    eq_map = parse_equity(root)
    total, cash = merge_daily(result, eq_map)
    result["totalNetValueDaily"] = total
    result["cashDaily"] = cash
    print(f"[merge] 每日总净值 {len(total)} 天 / 现金 {len(cash)} 天；"
          f"本次新增/更新 {len(eq_map)} 天")

    # ---- 应计利息：取最新报表日的 interestAccruals，写入顶层 ibkrAccruedInterest ----
    # 前端 loadAssetJson 会把该值并入「盈透现金」（calcCashAmount），每日同步即自动更新，
    # 免去了每次导出 XML 后手工改 JSON 的维护。若当日报表未含该字段则沿用旧值。
    if eq_map:
        eq_latest = max(eq_map.keys())
        ai_val = eq_map[eq_latest].get("accruedInterest")
        if ai_val is not None:
            result["ibkrAccruedInterest"] = ai_val
            print(f"[accrued] 应计利息 {ai_val}（报表日 {eq_latest}）"
                  f"-> 写入 ibkrAccruedInterest")
        else:
            print(f"[accrued] 报表日 {eq_latest} 无 interestAccruals，"
                  f"沿用旧值 {result.get('ibkrAccruedInterest')}")

    # ---- 当前持仓：覆盖式更新 ----
    holdings, latest = parse_holdings(root)
    result["holdings"] = holdings
    print(f"[holdings] 报表日 {latest}，当前持仓 {len(holdings)} 项")

    # ---- 期权 A/B 基准：缓存 2 天，每日循环 ----
    # 数据口径（与前端「期权当日盈亏」对齐）：
    #   A = 上一次运行算出的 B（即「昨天」的期权 markPrice）
    #   B = 本次运行算出的期权 markPrice（即「今天」）
    # 前端取数：盘外(北京 4:00~21:30) 用 A；盘中(21:30~次日4:00) 用 B。
    # 脚本每天北京 ~17:00 跑一次（12:10 cron 排队后实际完成），配合 lastSync
    # 每日只更新一次；A/B 循环完全由上次 B 降级为 A 实现，无需前端维护。
    _opt_prev_b = (result.get("optBase") or {}).get("B")
    _opt_today = beijing_now().strftime("%Y-%m-%d")
    _opt_prices = {}
    for _h in holdings:
        _sym = (_h.get("symbol") or "")
        # 期权符号形如 ORCL  260814P00140000（含空格、6位日期+C/P+8位行权价）
        if not re.match(r"^\s*[A-Za-z]{1,6}\s*\d{6}[CP]\d{8}\s*$", _sym):
            continue
        _mp = _h.get("markPrice")
        if isinstance(_mp, (int, float)) and _mp > 0:
            _opt_prices[_sym] = _mp
    _opt_a = {"date": (_opt_prev_b or {}).get("date"),
              "prices": dict((_opt_prev_b or {}).get("prices", {}))}
    _opt_b = {"date": _opt_today, "prices": _opt_prices}
    result["optBase"] = {"A": _opt_a, "B": _opt_b}
    print(f"[optBase] A(date={_opt_a['date']}) {len(_opt_a['prices'])} 项, "
          f"B(date={_opt_today}) {len(_opt_b['prices'])} 项")

    # ---- 可选：用 asset(365day) 报表刷新 forexTrades / efts / 交易明细 ----
    asset_trades = []
    q_asset = os.environ.get("IBKR_QUERY_ID_ASSET")
    if q_asset:
        try:
            print("[flex] 下载 asset(365day) 报表以刷新换汇/转账 ...")
            xml_a = request_flex_xml(os.environ.get("IBKR_TOKEN"), q_asset)
            ra = ET.fromstring(xml_a)
            asset_trades = parse_trades(ra)     # 365 全量交易（含近一月）
            # 换汇（仅 USD.CNH）
            forex = []
            for t in ra.findall(".//StmtFunds/StatementOfFundsLine"):
                if t.get("activityCode") != "FOREX":
                    continue
                if t.get("symbol") != "USD.CNH":
                    continue
                q = fnum(t.get("tradeQuantity"))
                r = fnum(t.get("tradePrice"))
                cnh = (round(q * r, 2) if q is not None and r is not None else None)
                forex.append({
                    "date": ymd(t.get("date")),
                    "symbol": t.get("symbol"),
                    "description": t.get("activityDescription"),
                    "buySell": t.get("buySell") or None,
                    "usd": q, "rate": r, "cnh": cnh,
                    "debit": fnum(t.get("debit")),
                    "credit": fnum(t.get("credit")),
                    "amount": fnum(t.get("amount")),
                    "balance": fnum(t.get("balance")),
                })
            # 保留手动补充记录（_fixed 标记）在最前
            manual = [f for f in (result.get("forexTrades") or [])
                      if f.get("_fixed")]
            result["forexTrades"] = manual + forex
            print(f"[forex] 刷新 USD.CNH 换汇 {len(forex)} 笔")
        except Exception as e:
            print(f"[warn] asset 报表刷新失败，沿用旧值: {e}")

    # ---- 交易明细：合并「1day 当日」与「365 全量」，去重后裁剪到近一月 ----
    new_trades = parse_trades(root) + asset_trades
    today_bj = beijing_now().strftime("%Y-%m-%d")
    ref = max([t["date"] for t in new_trades] + [today_bj]) if new_trades else today_bj
    cutoff = one_month_ago(ref)
    merged_trades = merge_trades(result.get("trades") or [], new_trades, cutoff)
    result["trades"] = merged_trades
    print(f"[trades] 近一月交易 {len(merged_trades)} 条（cutoff>={cutoff}）；"
          f"本次报表提供 {len(new_trades)} 条")

    # ---- 写回 ----
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[done] 已写入 {json_path}")
    print(f"       账户: {result['account'].get('accountId')}  "
          f"总净值天数: {len(total)}  现金天数: {len(cash)}  "
          f"持仓: {len(holdings)}  交易: {len(result.get('trades') or [])}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xml", help="本地 XML 路径（调试用，跳过联网下载与跳过判断）")
    ap.add_argument("--backfill", help="仅回填交易明细：从指定 XML 解析近一月交易写入 JSON 的 trades 字段（不动持仓/现金）")
    ap.add_argument("--json", help="输出 JSON 路径（默认脚本同目录 Asset_parsed.json）")
    ap.add_argument("--force", action="store_true",
                    help="忽略「今日已同步」标记，强制重新拉取（手动补跑用）")
    args = ap.parse_args()

    json_path = args.json or os.environ.get("JSON_PATH", DEFAULT_JSON)

    # ---- 读取已有 json ----
    if os.path.exists(json_path):
        with open(json_path, encoding="utf-8") as f:
            result = json.load(f)
        print(f"[load] 已有 Asset_parsed.json: {json_path}")
    else:
        result = {
            "account": {},
            "totalNetValueDaily": [],
            "cashDaily": [],
            "holdings": [],
            "electronicFundTransfers": [],
            "forexTrades": [],
        }
        print(f"[init] 未找到 json，新建于 {json_path}")

    # ---- 本地调试：直接解析一次，不判断今日是否已同步 ----
    if args.xml:
        print(f"[xml] 使用本地文件: {args.xml}")
        do_parse_and_write(open(args.xml, encoding="utf-8").read(), result, json_path)
        return

    # ---- 仅回填交易明细（不动持仓/现金）----
    if args.backfill:
        print(f"[backfill] 从 {args.backfill} 提取近一月交易明细")
        try:
            bxml = ET.parse(args.backfill).getroot()
        except Exception as e:
            sys.exit(f"ERROR: 解析 {args.backfill} 失败: {e}")
        new_trades = parse_trades(bxml)
        ref = max([t["date"] for t in new_trades],
                  default=beijing_now().strftime("%Y-%m-%d"))
        cutoff = one_month_ago(ref)
        merged = merge_trades(result.get("trades") or [], new_trades, cutoff)
        result["trades"] = merged
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[backfill] 近一月交易 {len(merged)} 条（cutoff>={cutoff}）；"
              f"已写入 {json_path}")
        return

    token = os.environ.get("IBKR_TOKEN")
    q1 = os.environ.get("IBKR_QUERY_ID_1DAY")
    if not token or not q1:
        sys.exit("ERROR: 需设置 IBKR_TOKEN 与 IBKR_QUERY_ID_1DAY，或使用 --xml")

    # ---- 今日已同步过则跳过（由 GitHub 每 30 分钟一个 cron 负责重试）----
    today_bj = beijing_now().strftime("%Y-%m-%d")
    if (not args.force) and result.get("lastSync") == today_bj:
        print(f"[skip] 今日（{today_bj}）已同步过，跳过本次运行；"
              f"如需重跑可加 --force")
        return

    # ---- 单次尝试：失败直接退出，等下一个 cron（30 分钟后）再试 ----
    print(f"[sync] 尝试拉取 @ 北京时间 {beijing_now():%Y-%m-%d %H:%M}")
    try:
        xml_txt = fetch_validate(token, q1, result)
    except Exception as e:
        print(f"[warn] 拉取失败：{e}；本窗口内下一个 cron 将自动重试")
        return
    result["lastSync"] = today_bj
    try:
        do_parse_and_write(xml_txt, result, json_path)
    except Exception:
        # 把完整堆栈打到 Actions 日志，便于定位失败根因（不再只显示 step failed）
        import traceback as _tb
        _tb.print_exc()
        raise
    print(f"[ok] 同步成功（标记为 {today_bj}）")


if __name__ == "__main__":
    main()
