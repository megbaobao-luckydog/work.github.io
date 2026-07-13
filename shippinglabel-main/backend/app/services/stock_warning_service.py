"""StockWarningService — 库存预警分类（纯函数，可单元测试）

把每个订单按它含的 SKU 状态(in/out/ns)归到**唯一一类**:

    全是 in                     → 不预警
    in + ns  (无 out)           → ③ not_satisfy_all
    in + out (无 ns)            → ② partially_in_stock
    无 in    (全 out 或 out+ns) → ① out_of_stock

判定的唯一地基是「按 SKU 聚合」(§3.2 单一真相源)：库存够不够、谁能发，在聚合层
就全局算死；订单分类只决定这一行**显示在哪个分区**，不参与库存分配。详见 PRD_ShipGuard.md §3。
"""

from collections import defaultdict


# SKU 三态
OUT = "out"            # 连最小那一单都凑不齐 → 谁都发不了
NS = "not_satisfy"     # 部分满足：能发至少一单，发不了全部抢它的订单
IN = "ok"              # 所有订单都能发
NOT_FOUND = "not_found"  # Neto 无该 SKU 记录 —— 数据问题，独立于 out（缺货）


def sku_status(total_stock, total_need: int, min_need: int) -> str:
    """§2.1：给单个 SKU 定状态。total_stock 为 None = Neto 无记录 → not_found（独立于 out）。"""
    if total_stock is None:
        return NOT_FOUND
    if total_stock < min_need:
        return OUT
    if total_stock < total_need:
        return NS
    return IN


def classify_warnings(lines: list, stock: dict, threshold: int = 0) -> dict:
    """
    lines: [{invoice, sku, need, place_time}]   一行 = 某订单的某 SKU
    stock: {sku: available_int}                 每个 SKU 的可用库存(kit 已折算)
    threshold: 全局低库存阈值；某 SKU 的总库存 <= threshold(且 threshold>0)即标 low_stock。
               纯标注，不改变四类分类逻辑。
    stock 里某 sku 的值为 None（或不存在）= Neto 无记录 = not_found（数据问题，独立成桶）。
    返回 (PRD §5 形状，每个 SKU 多带 low_stock 标记):
      {
        "not_found":          [{invoice, sku, need}],   # Neto 查不到的 SKU —— 改数据,不是补货
        "out_of_stock":       [{invoice, sku, need, available, low_stock}],
        "partially_in_stock": [{invoice, layers:[{sku, need, available, status:"in"|"out", low_stock}]}],
        "not_satisfy_all":    [{sku, total_need, total_stock, low_stock, orders:[{invoice, need, place_time}]}],
      }
    """
    # ---- §2.1 统一 SKU 聚合 → sku_status（单一真相源）----
    needs_by_sku = defaultdict(list)
    for ln in lines:
        sku, need = ln["sku"], int(ln.get("need") or 0)
        if sku and need > 0:
            needs_by_sku[sku].append(need)

    status_of = {}
    agg = {}
    for sku, needs in needs_by_sku.items():
        raw = stock.get(sku, None)
        total_stock = None if raw is None else int(raw or 0)   # None = Neto 无记录 = not_found
        total_need = sum(needs)
        status_of[sku] = sku_status(total_stock, total_need, min(needs))
        agg[sku] = {
            "total_stock": total_stock,
            "total_need": total_need,
            "low_stock": total_stock is not None and threshold > 0 and total_stock <= threshold,
        }

    # ---- §3.3 按 invoice 卷积 → 每单归一类 ----
    lines_by_invoice = defaultdict(list)
    for ln in lines:
        if ln["sku"] and int(ln.get("need") or 0) > 0:
            lines_by_invoice[ln["invoice"]].append(ln)

    not_found = []
    out_of_stock = []
    partially_in_stock = []
    ns_orders_by_sku = defaultdict(list)   # ③ 按争抢的 ns SKU 分组

    for invoice, inv_lines in lines_by_invoice.items():
        # not_found 独立成桶（数据问题），且不参与 in/out/ns 的订单分类
        for l in inv_lines:
            if status_of[l["sku"]] == NOT_FOUND:
                not_found.append({
                    "invoice": invoice,
                    "sku": l["sku"],
                    "need": int(l.get("need") or 0),
                })
        clf_lines = [l for l in inv_lines if status_of[l["sku"]] != NOT_FOUND]
        if not clf_lines:
            continue                                   # 整单都是 not_found → 只进 not_found 桶

        statuses = {status_of[l["sku"]] for l in clf_lines}
        has_in, has_ns, has_out = IN in statuses, NS in statuses, OUT in statuses

        if not has_ns and not has_out:
            continue                                   # 全是 in → 不预警

        if has_in and has_ns and not has_out:          # ③ in + ns
            for l in clf_lines:
                if status_of[l["sku"]] == NS:
                    ns_orders_by_sku[l["sku"]].append({
                        "invoice": invoice,
                        "need": int(l.get("need") or 0),
                        "place_time": str(l.get("place_time") or ""),
                    })
        elif has_in and has_out and not has_ns:        # ② in + out
            partially_in_stock.append({
                "invoice": invoice,
                "layers": [{
                    "sku": l["sku"],
                    "need": int(l.get("need") or 0),
                    "available": agg[l["sku"]]["total_stock"],
                    "status": "in" if status_of[l["sku"]] == IN else "out",
                    "low_stock": agg[l["sku"]]["low_stock"],
                } for l in clf_lines],
            })
        else:                                          # ① 无 in：全 out 或 out+ns
            for l in clf_lines:
                if status_of[l["sku"]] == IN:
                    continue
                out_of_stock.append({
                    "invoice": invoice,
                    "sku": l["sku"],
                    "need": int(l.get("need") or 0),
                    "available": agg[l["sku"]]["total_stock"],
                    "low_stock": agg[l["sku"]]["low_stock"],
                })

    # ③ 整理：每个 ns SKU 一组，订单按 place_time 升序（早下单先抢；空时间排末尾）
    not_satisfy_all = [{
        "sku": sku,
        "total_need": agg[sku]["total_need"],
        "total_stock": agg[sku]["total_stock"],
        "low_stock": agg[sku]["low_stock"],
        "orders": sorted(orders, key=lambda o: (o["place_time"] == "", o["place_time"])),
    } for sku, orders in ns_orders_by_sku.items()]

    return {
        "not_found": not_found,
        "out_of_stock": out_of_stock,
        "partially_in_stock": partially_in_stock,
        "not_satisfy_all": not_satisfy_all,
    }
