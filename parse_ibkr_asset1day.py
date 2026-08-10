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


# ----------------------- Flex Web Service -----------------------
def request_flex_xml(token, query_id, max_wait=300, interval=10, send_retries=8):
    """
    调用 IBKR Flex Web Service（两步式）：
      1) SendRequest -> 拿到 ReferenceCode
      2) GetStatement 轮询 -> 取到完整报表 XML
    返回报表 XML 字符串；失败抛异常。

    关键经验：IBKR Flex 接口经常返回「Statement could not be generated at
    this time. Please try again shortly.」这类**瞬时**错误（尤其在整点/批量
    生成时段）。因此 SendRequest 与 GetStatement 都必须做**指数退避重试**，
    而不是一遇到 ErrorMessage 就放弃。
    """
    TRANSIENT = ("could not be generated", "try again", "please wait",
                 "in progress", "in queue", "1018", "1009", "temporarily")

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
    """从 EquitySummaryInBase 解析每日总净值/现金。返回 dict: date -> {total, cash}"""
    out = {}
    for e in equity_root.findall(".//EquitySummaryInBase/"
                                 "EquitySummaryByReportDateInBase"):
        rd = e.get("reportDate", "")
        if not rd:
            continue
        out[ymd(rd)] = {
            "total": fnum(e.get("total")),
            "cash": fnum(e.get("cash")),
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xml", help="本地 XML 路径（调试用，跳过联网下载）")
    ap.add_argument("--json", help="输出 JSON 路径（默认脚本同目录 Asset_parsed.json）")
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

    # ---- 获取 1day XML ----
    if args.xml:
        print(f"[xml] 使用本地文件: {args.xml}")
        xml_txt = open(args.xml, encoding="utf-8").read()
    else:
        token = os.environ.get("IBKR_TOKEN")
        q1 = os.environ.get("IBKR_QUERY_ID_1DAY")
        if not token or not q1:
            sys.exit("ERROR: 需设置 IBKR_TOKEN 与 IBKR_QUERY_ID_1DAY，或使用 --xml")
        print("[flex] 下载 1day 报表 ...")
        xml_txt = request_flex_xml(token, q1)
        print("[flex] 1day 报表下载完成")

    root = ET.fromstring(xml_txt)

    # ---- 账户信息（仅当 1day 含 AccountInformation 时更新）----
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

    # ---- 当前持仓：覆盖式更新 ----
    holdings, latest = parse_holdings(root)
    result["holdings"] = holdings
    print(f"[holdings] 报表日 {latest}，当前持仓 {len(holdings)} 项")

    # ---- 可选：用 asset(365day) 报表刷新 forexTrades / efts ----
    q_asset = os.environ.get("IBKR_QUERY_ID_ASSET")
    if (not args.xml) and q_asset:
        try:
            print("[flex] 下载 asset(365day) 报表以刷新换汇/转账 ...")
            xml_a = request_flex_xml(os.environ.get("IBKR_TOKEN"), q_asset)
            ra = ET.fromstring(xml_a)
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

    # ---- 写回 ----
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[done] 已写入 {json_path}")
    print(f"       账户: {result['account'].get('accountId')}  "
          f"总净值天数: {len(total)}  现金天数: {len(cash)}  "
          f"持仓: {len(holdings)}")


if __name__ == "__main__":
    main()
