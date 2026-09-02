#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
parse_ibkr_asset1day.py — 盈透证券(IBKR)每日资产快照同步脚本

用途
----
通过 IBKR Flex Web Service 接口，用 token + queryId 下载「1day」Flex 报表 XML，
解析出当日的总净值 / 现金 / 持仓，并将其 **追加/覆盖** 写入**脚本同目录**下的
Asset_parsed.json（DEFAULT_JSON = 脚本所在目录，不一定是仓库根）：

  totalNetValueDaily[]   开户至今每日总净值   {date:'YYYY-MM-DD', value:Number}
  cashDaily[]            开户至今每日现金(USD) {date:'YYYY-MM-DD', value:Number,
                         accrued?:Number}     # accrued=该日应计利息（取自 interestAccruals），
                                              # 仅报表覆盖到的日期才有；缺则为无（不记）
  ibkrAccruedInterest   盈透现金应计利息（= 最新报表日的 interestAccruals；
                         若最新报表日该字段为空则沿用旧值，**不会**回退去取更早日期）。
                         前端卡片据此并入「盈透现金」；每日同步自动刷新，免手工改。
  holdings[]             当前持仓（symbol/position/costBasisPrice/markPrice ...）
  trades[]               近一月交易明细（1day 当日 + asset 全量，合并去重，按日期倒序）
  optBase               {A:{date,asOf,runAt,prices}, B:{...}} 期权当日盈亏基准（见文末说明）
  lastSync              'YYYY-MM-DD'（北京时间日期），标记「当日已同步」，供 cron 去重

与 365day 解析脚本(parse_ibkr_asset.py) 的分工（本脚本实际会动哪些字段）：
  - account            会被本脚本**覆盖更新**（取自 1day 报表的 AccountInformation；
                       只有 1day 报表不含该节点时才保留原值，与 IBKR_QUERY_ID_ASSET 无关）
  - forexTrades        仅当设置了 IBKR_QUERY_ID_ASSET 时**整体重写**（保留 _fixed 手动记录）
  - trades / holdings / totalNetValueDaily / cashDaily / optBase   本脚本写入
  - electronicFundTransfers   本脚本**既不读也不写**，沿用 365day 脚本产出的结果

运行方式
--------
  # 标准（GitHub Actions / 定时任务）：用环境变量取密钥，联网下载
  IBKR_TOKEN=xxx IBKR_QUERY_ID_1DAY=yyy IBKR_QUERY_ID_ASSET=zzz python parse_ibkr_asset1day.py

  # 本地调试：直接解析本地 XML（不联网，且跳过 lastSync 判断、不写 lastSync）
  python parse_ibkr_asset1day.py --xml Asset-1day.xml [--json Asset_parsed.json]

  # 只回填近一月交易明细（不动净值/现金/持仓），同样不写 lastSync
  python parse_ibkr_asset1day.py --backfill Asset-365day.xml [--json Asset_parsed.json]

  # 忽略「今日已同步」标记强制重跑
  python parse_ibkr_asset1day.py --force

环境变量
--------
  IBKR_TOKEN              盈透 Flex Web Service token（必填，除非 --xml/--backfill）
  IBKR_QUERY_ID_1DAY      「1day」Flex 报表的 queryId（必填，除非 --xml/--backfill）
  IBKR_QUERY_ID_ASSET     「365day/资产」Flex 报表的 queryId（可选；
                          提供时额外刷新 forexTrades 与 trades，**不影响 account**；
                          不提供则 forexTrades 沿用旧值，trades 只补 1day 当日的成交）
  JSON_PATH              输出 json 路径（默认：脚本同目录 Asset_parsed.json）
  IBKR_BASE_URL          Flex Web Service 基地址（一般无需改）

  ※ 注意：代码里实际读取的发送/取回地址变量是 IBKR_SEND_URL / IBKR_GET_URL，
    并没有读取 IBKR_BASE_URL（该环境变量当前不生效，保留仅为兼容旧配置）。

说明
----
- 1day 报表的 EquitySummaryByReportDateInBase 只含最近 1~2 天；按 date 去重覆盖写入：
  报表里出现的日期覆盖旧值，未出现的日期原样保留（merge_daily 不做任何"日期先后"判断）。
- 持仓只取 1day 报表最新 reportDate 的快照（覆盖式更新），不保留历史持仓。
- 若当日数据尚未生成（如周末/休市，报表最新日期与已同步日期相同），脚本不会凭空造数据，
  只会把同一天的数据再覆盖写一遍（fetch_validate 允许"相等"，只允许"更新"不允许"更旧"）；
  缺失日期由前一次运行或历史 365day 数据补足。

optBase（期权当日盈亏基准）说明
------------------------------
- A = 上次运行算出的 B；B = 本次运行从持仓里提取的期权 markPrice 快照。
  每次运行把旧 B 降级为 A，实现「滚动两日窗口」，前端无需自己维护历史。
- 脚本每天在 UTC 05:10 跑一次（= 北京时间 13:10），
  此时美股早已收盘 8 小时（美东 16:00 收盘 = 北京夏令时 04:00 / 冬令时 05:00），
  因此 B 是**上一个美股交易日收盘**的期权 mark，A 是**再往前一个交易日**收盘的 mark。
- 每个基准带 3 个日期字段，别混用：
    asOf   持仓快照的报表日 = prices 里 markPrice **真正对应的交易日**；
           **前端做日期判断/展示一律用这个**（报表达常滞后，asOf 一般早于运行日）
    runAt  本次运行的北京时间日期
    date   等于 runAt，仅保留兼容已有前端，不要用它判断交易日
- 前端按北京时间切段取数：盘外 04:00~21:30 用 A，盘中 21:30~次日 04:00 用 B
  （该时段对应美东夏令时 09:30~16:00；冬令时会整体后移一小时，届时需同步调整）。
- 若某次报表滞后（B.asOf 不比 A.asOf 新），脚本会打印 [warn] 提示，但仍照常写入。

调度说明（2026-09-02 统一：脚本与 workflow 口径一致，纯 UTC 不再绑美东）
--------------------------------------------------------------------------
- 脚本内**不含任何调度逻辑**，它只是一次性任务，时间表由 workflow 的 cron 决定：

      cron: "10 5 * * *"   = UTC 05:10
                           = 北京时间 13:10

  每天**只跑一次**，没有兜底重试。固定锚点是「UTC 05:10 / 北京 13:10」：
  距美股收盘（16:00 ET = 北京夏令时 04:00 / 冬令时 05:00）已过去约 8~9 小时，
  IBKR 报表必然就绪，单次命中率极高，没必要再排第二班 ——
  跑第二次只会白白消耗 Actions 时长。

  注：不再按美东时区换算，免去夏令时/冬令时半年一次的手动切换。
  北京 13:10 在美股收盘后、且恰好落在 A/B 基准窗口的「盘外段」（04:00~21:30），
  取数逻辑与调度时间天然解耦，无需随季节调整。

- 单次运行很轻：每个阶段最多 2 次尝试（首次 + 退避 20 秒重试 1 次），
  最坏约 40 秒 + 请求耗时；正常几秒完成。workflow 侧 timeout 给 5 分钟足够。
- 脚本用 JSON 里的 lastSync（北京时间日期）标记「今日已同步」：
  每天只会写一次，重复触发（如手动 workflow_dispatch）会被 lastSync 挡住，
  需要强制重跑时勾选 force 入参（透传为 --force）。
  注意：lastSync 是在**写入前**设置到内存、随 do_parse_and_write 一起落盘的，
  所以解析/写盘失败时不会留下「假同步」标记。
  （--xml / --backfill 两个本地模式不写 lastSync，也不受它影响。）
- **坑**：报表未就绪、或今日已同步而跳过时，脚本走的是「正常 return」，退出码为 0。
  workflow 不能只看退出码，否则会把「什么都没做」也报成成功 ——
  应按日志里的 [ok] / [skip] / [warn] 判断（workflow 已这样处理）。
- 当天失败则放弃，次日 UTC 05:10（北京 13:10）再开始。
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
def request_flex_xml(token, query_id, max_polls=2, interval=20, send_retries=2):
    """
    调用 IBKR Flex Web Service（两步式）：
      1) SendRequest   -> 拿到 ReferenceCode
      2) GetStatement  -> 取到完整报表 XML
    返回报表 XML 字符串；失败抛异常。

    重试策略（2026-09-02 简化，配合「每天只在 UTC 05:10 跑 1 次」的调度）：
    - 每个阶段最多 **2 次尝试** = 首次 + 退避 20s 后重试 1 次（send_retries / max_polls）。
    - 退避固定 20 秒（interval），不做指数放大。
    - 最坏耗时约 40 秒 + 请求本身，单轮轻量；失败即放弃，不拖长 Actions 时长。
    - 之所以敢把重试压到这么低：UTC 05:10（北京 13:10）距美股收盘（16:00 ET）已过去约 8~9 小时，
      IBKR 报表几乎不可能还没就绪，命中率很高；真遇上一次瞬时错误，一次退避重试足以覆盖。

    关键经验：
    - IBKR Flex 接口会返回「Statement could not be generated at this time.
      Please try again shortly.」这类**瞬时**错误（报表尚未生成/批量时段），按上述策略重试。
    - SendRequest 过于频繁会触发 IBKR 频率限制「Too many failed attempts…」，
      该限制会封相当长一段时间。因此命中频率限制后**立即放弃**，绝不继续重试。
    """
    TRANSIENT = ("could not be generated", "try again", "please wait",
                 "in progress", "in queue", "1018", "1009", "temporarily")
    # 说明：末尾两个是 IBKR Flex 的错误码（1018/1009），同样按瞬时错误处理。
    # 频率限制：命中后立即放弃，避免越限越多把后续几天都堵死
    RATELIMIT = ("too many failed attempts", "review your configuration",
                 "rate limit", "too many requests", "exceeded")

    # 1) 提交报表请求（瞬时错误退避 20s 重试 1 次）
    submit = f"{IBKR_SEND_URL}?t={token}&q={query_id}&v=3"
    code = None
    last_err = ""
    for attempt in range(1, send_retries + 1):
        try:
            txt = http_get(submit)
        except Exception as e:  # 网络抖动也计入重试
            last_err = f"网络异常: {e}"
            if attempt < send_retries:
                print(f"[flex] SendRequest 第{attempt}次网络异常，{interval}s 后重试")
                time.sleep(interval)
                continue
            break
        rc = txt.split("<ReferenceCode>")[1].split("</ReferenceCode>")[0] \
            if "<ReferenceCode>" in txt else None
        err = txt.split("<ErrorMessage>")[1].split("</ErrorMessage>")[0] \
            if "<ErrorMessage>" in txt else None
        if rc:
            code = rc
            break
        last_err = err or txt[:300]
        # 命中频率限制：立即放弃（不要继续空耗请求，否则会被封更久）
        if any(k in last_err.lower() for k in RATELIMIT):
            raise RuntimeError(f"Flex 触发频率限制（{last_err[:80]}），本次放弃")
        is_transient = any(k in last_err.lower() for k in TRANSIENT)
        if is_transient and attempt < send_retries:
            print(f"[flex] SendRequest 第{attempt}次瞬时错误（{last_err[:80]}），"
                  f"{interval}s 后重试")
            time.sleep(interval)
            continue
        # 网络异常走到这里说明已是最后一次尝试（前面的 except 已 break）
        # 非瞬时错误（如 Invalid request）：立即失败
        raise RuntimeError(f"Flex SendRequest 失败: {last_err}")
    if not code:
        raise RuntimeError(f"Flex SendRequest 尝试{send_retries}次仍失败: {last_err}")

    # 2) 取回报表（最多 max_polls 次，每次间隔 interval 秒）
    get_url = f"{IBKR_GET_URL}?t={token}&q={code}&v=3"
    for attempt in range(1, max_polls + 1):
        try:
            txt = http_get(get_url)
        except Exception as e:
            last_err = f"网络异常: {e}"
            if attempt < max_polls:
                print(f"[flex] GetStatement 网络异常，{interval}s 后重试: {e}")
                time.sleep(interval)
                continue
            break
        if "<ErrorMessage>" in txt:
            msg = txt.split("<ErrorMessage>")[1].split("</ErrorMessage>")[0]
            last_err = msg
            is_transient = any(k in msg.lower() for k in TRANSIENT)
            if is_transient and attempt < max_polls:
                print(f"[flex] 报表仍在生成（{msg[:60]}），{interval}s 后重试")
                time.sleep(interval)
                continue
            if not is_transient:  # 硬错误
                raise RuntimeError(f"Flex GetStatement 失败: {msg}")
        # 成功返回包含报表数据（FlexStatementResponse / FlexQueryResponse）
        if "<EquitySummaryByReportDateInBase" in txt or "<FlexQueryResponse" in txt \
                or "<FlexStatementResponse" in txt:
            return txt
        last_err = "返回内容不含报表数据"
        if attempt < max_polls:
            print(f"[flex] {last_err}，{interval}s 后重试")
            time.sleep(interval)
    raise RuntimeError(f"Flex GetStatement 轮询{max_polls}次未取到报表"
                       f"（最后错误: {last_err}）")
    raise RuntimeError(f"Flex GetStatement 轮询{max_polls}次未取到报表"
                       f"（最后错误: {last_err}）")


# ----------------------------- 解析 -----------------------------
def parse_equity(root):
    """从报表根节点解析每日总净值/现金/应计利息。

    入参是报表根节点（FlexQueryResponse / FlexStatementResponse），
    内部用 .//EquitySummaryInBase/EquitySummaryByReportDateInBase 递归定位。
    返回 dict: date -> {total, cash, accruedInterest}
      accruedInterest 取自 interestAccruals —— 盈透按日计提、按月实际收付的利息
      应计余额（存款利息与融资利息都算在内，结息入账后该余额会被冲回）；
      并非「持仓过夜盈亏」，别和 unrealizedPnl 混淆。"""
    out = {}
    for e in root.findall(".//EquitySummaryInBase/"
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
    """从最新 OpenPosition 报表日解析当前持仓快照。
    返回 (holdings:list[dict], latest:str|None)；无持仓时 latest 为 None。
    注意 reportDate 可能为空字符串（部分报表不带该属性），此时会归到 '' 分组。"""
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
    existing/new: list[dict]。返回按日期降序（新→旧）的 list。
    去重是「先到先得」：existing 先入库，因此同 key 的旧记录会胜出、
    后加入的 new 记录被丢弃（即不会用新拉取的数据覆盖已有字段）。
    cutoff 的比较是字符串字典序，要求 date 一律为 'YYYY-MM-DD' 格式。
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
    existing: 整个 result dict（只读其中的 totalNetValueDaily / cashDaily）
    new_map:  dict date -> {total, cash, accruedInterest}
    返回 (totalNetValueDaily, cashDaily) 两个 list，均按 date 升序。
    cashDaily 每项额外带 accrued（该日应计利息），仅报表覆盖到的日期才有；
    历史无 accrued 的日期保留原值（缺省不记）。
    细节：新报表给了 cash 但没给 interestAccruals 时，该日旧的 accrued 会被保留；
    value/total 同理为 None 时不覆盖（保持旧值）。
    """
    tot_map, cash_map = {}, {}
    for row in (existing.get("totalNetValueDaily") or []):
        tot_map[row["date"]] = row["value"]
    for row in (existing.get("cashDaily") or []):
        cash_map[row["date"]] = {
            "value": row["value"],
            "accrued": row.get("accrued"),
        }
    for d, v in new_map.items():
        if v.get("total") is not None:
            tot_map[d] = v["total"]
        if v.get("cash") is not None:
            cur = cash_map.get(d) or {"value": None, "accrued": None}
            cur["value"] = v["cash"]
            if v.get("accruedInterest") is not None:
                cur["accrued"] = v["accruedInterest"]
            cash_map[d] = cur
    total = [{"date": d, "value": tot_map[d]} for d in sorted(tot_map)]
    cash = []
    for d in sorted(cash_map):
        c = cash_map[d]
        e = {"date": d, "value": c["value"]}
        if c["accrued"] is not None:
            e["accrued"] = c["accrued"]
        cash.append(e)
    return total, cash


def fetch_validate(token, query_id, result):
    """
    下载并校验 1day 报表，成功返回报表 XML 字符串；失败（瞬时错误 / 无数据 / 报表陈旧）
    则抛异常，由调用方决定重试策略。

    注意命名与行为的落差：它只做「防倒退」校验——报表最新日期**早于**已同步日期才报错，
    **等于**已同步日期是允许的（会再覆盖写一遍当天数据，用于刷新当日修正值）。
    也就是说这里并不真的校验「已刷新到当日」，周末/休市时同样会通过。
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
    """解析 1day 报表并写回 Asset_parsed.json
    （账户 / 每日净值 / 现金 / 应计利息 / 持仓 / 期权基准 / 换汇 / 交易明细）。"""
    root = ET.fromstring(xml_txt)

    # ---- 账户信息（仅当含 AccountInformation 时更新；否则沿用 JSON 里旧值）----
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
    #   A = 上一次运行算出的 B（即上一次运行时的期权 markPrice）
    #   B = 本次运行算出的期权 markPrice
    #   每次运行把旧 B 整块降级为 A，实现滚动两日窗口。
    # 前端取数：盘外(北京 04:00~21:30) 用 A；盘中(21:30~次日 04:00) 用 B。
    #   ※ 该时段是**美东夏令时** 09:30~16:00 的换算；冬令时要整体后移 1 小时。
    # 脚本每天在 UTC 05:10 跑一次（见文件头 cron 说明，= 北京 13:10，不再绑美东时区），
    # 此时美股早已收盘（美东 16:00 = 北京夏令时 04:00 / 冬令时 05:00），
    # 所以 B 实际是「上一个美股交易日收盘」的期权 mark，A 是「再往前一个交易日」的 mark。
    #
    # 三个日期字段的分工（别混用）：
    #   asOf   持仓快照的报表日，即 prices 里 markPrice **真正对应的交易日**。
    #          前端做日期判断/展示一律用这个。报表达常滞后一天，故 asOf 通常早于运行日。
    #   runAt  本次运行的北京时间日期（= 老字段 date 的语义）。
    #   date   保留原语义（= runAt），仅为兼容已有前端，不要用它做交易日判断。
    # 配合 lastSync 每日只更新一次；A/B 循环完全由上次 B 降级为 A 实现，无需前端维护。
    _opt_prev_b = (result.get("optBase") or {}).get("B") or {}
    _opt_today = beijing_now().strftime("%Y-%m-%d")
    _opt_asof = latest or None          # 无持仓时 latest 为 None/空串
    _opt_prices = {}
    for _h in holdings:
        _sym = (_h.get("symbol") or "")
        # 期权符号形如 ORCL  260814P00140000（含空格、6位日期+C/P+8位行权价）
        if not re.match(r"^\s*[A-Za-z]{1,6}\s*\d{6}[CP]\d{8}\s*$", _sym):
            continue
        _mp = _h.get("markPrice")
        if isinstance(_mp, (int, float)) and _mp > 0:
            _opt_prices[_sym] = _mp
    _opt_a = {"date": _opt_prev_b.get("date"),
              "asOf": _opt_prev_b.get("asOf"),
              "runAt": _opt_prev_b.get("runAt"),
              "prices": dict(_opt_prev_b.get("prices") or {})}
    _opt_b = {"date": _opt_today,
              "asOf": _opt_asof,
              "runAt": _opt_today,
              "prices": _opt_prices}
    # 报表达时（如 IBKR 迟迟没出新一天的报表），本次 B 会比 A 还旧，
    # 前端据此算出的「当日盈亏」方向会是反的，这里打日志提醒。
    if _opt_a.get("asOf") and _opt_asof and _opt_asof <= _opt_a["asOf"]:
        print(f"[warn] optBase 基准未前进：A(asOf={_opt_a['asOf']}) -> "
              f"B(asOf={_opt_asof})，报表可能滞后，当日盈亏会失真")
    result["optBase"] = {"A": _opt_a, "B": _opt_b}
    print(f"[optBase] A(asOf={_opt_a['asOf']}, run={_opt_a['runAt']}) "
          f"{len(_opt_a['prices'])} 项, "
          f"B(asOf={_opt_asof}, run={_opt_today}) {len(_opt_b['prices'])} 项")

    # ---- 可选：用 asset(365day) 报表刷新 forexTrades 与交易明细 ----
    # 注意：这里**不会**刷新 electronicFundTransfers(efts)，该字段本脚本全程不碰。
    asset_trades = []
    q_asset = os.environ.get("IBKR_QUERY_ID_ASSET")
    if q_asset:
        try:
            print("[flex] 下载 asset(365day) 报表以刷新换汇/转账 ...")
            xml_a = request_flex_xml(os.environ.get("IBKR_TOKEN"), q_asset)
            ra = ET.fromstring(xml_a)
            asset_trades = parse_trades(ra)     # asset 报表的全量交易（含近一月，故可单独 --backfill）
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

    # ---- 交易明细：合并「1day 报表里的成交」与「asset 报表全量」，去重后裁剪到近一月 ----
    # 未设置 IBKR_QUERY_ID_ASSET 时 asset_trades 为空，只补 1day 报表那一批。
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
    # 用 .get 兜底：account 只在报表含 AccountInformation 时才写入，
    # 若历史 JSON 里恰好没这个键，不能让「最后一步打印」把整个任务拖成失败。
    print(f"       账户: {(result.get('account') or {}).get('accountId')}  "
          f"总净值天数: {len(total)}  现金天数: {len(cash)}  "
          f"持仓: {len(holdings)}  交易: {len(result.get('trades') or [])}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xml", help="本地 XML 路径（调试用：跳过联网下载，也跳过「今日已同步」判断）")
    ap.add_argument("--backfill", help="仅回填交易明细：从指定 XML 解析近一月交易写入 JSON 的 trades 字段（不动净值/现金/持仓）")
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

    # ---- 本地调试：直接解析一次，跳过「今日已同步」判断，也不写 lastSync ----
    if args.xml:
        print(f"[xml] 使用本地文件: {args.xml}")
        do_parse_and_write(open(args.xml, encoding="utf-8").read(), result, json_path)
        return

    # ---- 仅回填交易明细（只写 trades，不动净值/现金/持仓，同样不写 lastSync）----
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
        sys.exit("ERROR: 需设置 IBKR_TOKEN 与 IBKR_QUERY_ID_1DAY，"
                 "或使用 --xml / --backfill 走本地文件")

    # ---- 今日已同步过则跳过（由 GitHub 每 30 分钟一个 cron 负责重试）----
    today_bj = beijing_now().strftime("%Y-%m-%d")
    if (not args.force) and result.get("lastSync") == today_bj:
        print(f"[skip] 今日（{today_bj}）已同步过，跳过本次运行；"
              f"如需重跑可加 --force")
        return

    # ---- 本窗口只跑一轮（request_flex_xml 内部本身含有限重试：SendRequest 最多
    #      send_retries 次 + GetStatement 最长 max_wait 秒轮询）。整轮失败就退出，
    #      不做外层循环，交给下一个 cron（30 分钟后）再试。----
    print(f"[sync] 尝试拉取 @ 北京时间 {beijing_now():%Y-%m-%d %H:%M}")
    try:
        xml_txt = fetch_validate(token, q1, result)
    except Exception as e:
        print(f"[warn] 拉取失败：{e}；下一个 cron（30 分钟后）将自动重试")
        return
    # 先打标记再写盘：do_parse_and_write 失败时不会落盘，就不会留下「假同步」
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
