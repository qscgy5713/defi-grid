"""命令列介面。全部是唯讀分析:不需要錢包、私鑰或 API 金鑰,也不會送出任何交易。"""
import argparse
import datetime
import os
import sys
import time
import unicodedata

from . import __version__
from .data import DataError, fetch_candles, suspicious
from . import paper
from .lp import LpConfig, human_liquidity, simulate_lp
from .rpc import Rpc, RpcError
from .scan import hodl_pct, scan, summarize, walk_forward, walk_summary
from .sim import CostModel, GridConfig, Trade, economics, grid_lines, simulate
from .swaps import LOGS_RPC, MAX_HOURS, as_candles, as_ticks, fetch_swaps
from .uniswap import POOL_WETH_USDC_500, WETH_BASE, l1_fee_upper_bound, price_impact, price_token1_per_token0, read_pool

DISCLAIMER = "※ 這是模擬與分析工具,不構成投資建議;模擬結果不代表實際會獲利。"


def _pct(x):
    return f"{x:+.2f}%"


def _ts(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")


def _width(s):
    """終端機顯示寬度:中文等全形字佔兩格。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def _table(head, body, left=()):
    """依顯示寬度對齊印出表格;left 是要靠左的欄位編號,其餘靠右。"""
    widths = [max(_width(row[i]) for row in [head] + body) for i in range(len(head))]
    for row in [head] + body:
        cells = []
        for i, (cell, w) in enumerate(zip(row, widths)):
            pad = " " * (w - _width(cell))
            cells.append(cell + pad if i in left else pad + cell)
        print("  ".join(cells).rstrip())


def _costs(args, pool_state=None):
    impact = (lambda v: price_impact(v, pool_state)) if pool_state else None
    return CostModel(
        fee_rate=args.fee_bps / 10_000,
        slippage_rate=args.slippage_bps / 10_000,
        gas_usd=args.gas_usd,
        impact_fn=impact,
    )


def cmd_pool(args):
    rpc = Rpc(args.rpc)
    s = read_pool(rpc, args.pool)
    price = price_token1_per_token0(s)
    print(f"節點:{rpc.host}    區塊:{s.block}")
    print(f"池子:{s.symbol0}/{s.symbol1}  手續費 {s.fee_rate * 100:.2f}%  ({s.address})")
    print(f"目前價格:1 {s.symbol0} = {price:,.2f} {s.symbol1}    tick {s.tick}")
    print(f"目前區間流動性 L = {s.liquidity:,}")
    print("價格衝擊(只算目前區間內的流動性,大額會更大):")
    for v in (100, 1_000, 10_000, 100_000):
        print(f"  買入 ${v:>9,}  → 價格上漲約 {price_impact(v, s) * 100:.4f}%")
    return 0


def cmd_gas(args):
    rpc = Rpc(args.rpc)
    s = read_pool(rpc, args.pool)
    price = price_token1_per_token0(s)
    gwei = int(rpc.call("eth_gasPrice", []), 16) / 1e9
    usd = gwei * 1e9 * args.gas_units / 1e18 * price
    l1 = l1_fee_upper_bound(rpc, args.tx_bytes) / 1e18 * price
    print(f"Base gas 價格:{gwei:.6f} gwei    ETH ≈ ${price:,.2f}")
    print(f"一次 swap(約 {args.gas_units:,} gas)的執行費:約 ${usd:.4f}")
    print(f"L1 資料費(交易約 {args.tx_bytes} bytes,取上限):約 ${l1:.6f}")
    total = usd + l1
    print(f"合計約 ${total:.4f}。gas 價格會隨壅塞變動,建議 --gas-usd 至少放寬到 ${max(total * 3, 0.01):.2f}。")
    return 0


def cmd_economics(args):
    price = args.price
    if price is None:
        price = price_token1_per_token0(read_pool(Rpc(args.rpc), args.pool))
    cfg = GridConfig(args.lower, args.upper, args.grids, args.capital, args.mode)
    e = economics(cfg, _costs(args), price)
    lines = grid_lines(cfg)
    print(f"參考價格 ${price:,.2f}    區間 ${cfg.lower:,.0f} ~ ${cfg.upper:,.0f}    {cfg.grids} 格({cfg.mode})")
    print(f"格距約 {e['spacing_pct']:.3f}%(${lines[1] - lines[0]:,.2f} 起)    每格資金 ${e['grid_value']:,.2f}")
    print(f"每筆單邊成本 {e['cost_rate_pct']:.3f}%(手續費+滑點),另加每筆 gas ${args.gas_usd}")
    print(f"每個完整買賣循環:淨 ${e['net_per_cycle_usd']:+.4f}({_pct(e['net_per_cycle_pct'])} 每格資金)")
    if e["viable"]:
        print(f"→ 格距高於損益兩平的 {e['breakeven_spacing_pct']:.3f}%,每完成一個循環有賺。")
    else:
        print(f"→ 格距低於損益兩平的 {e['breakeven_spacing_pct']:.3f}%,每完成一個循環是虧的。請減少格數、加大區間或增加本金。")
    print(DISCLAIMER)
    return 0


def cmd_backtest(args):
    if (args.lower is None) != (args.upper is None):
        raise ValueError("--lower 與 --upper 必須一起給(或都不給,改用 --range-pct)")
    GridConfig(1.0, 2.0, args.grids, args.capital, args.mode)  # 先驗證格數、本金等參數,避免白白下載
    candles = fetch_candles(args.pool, args.token, args.days, use_cache=not args.no_cache)
    p0 = candles[0].open
    if args.lower is not None and args.upper is not None:
        lower, upper = args.lower, args.upper
    else:
        lower, upper = p0 * (1 - args.range_pct / 100), p0 * (1 + args.range_pct / 100)
    cfg = GridConfig(lower, upper, args.grids, args.capital, args.mode)
    state = read_pool(Rpc(args.rpc), args.pool) if args.live_impact else None
    costs = _costs(args, state)
    r = simulate(candles, cfg, costs, args.path)
    e = economics(cfg, costs, p0)

    print(f"期間:{_ts(candles[0].ts)} ~ {_ts(candles[-1].ts)} UTC({len(candles)} 根小時 K 線)")
    print(f"價格:${r.start_price:,.2f} → ${r.end_price:,.2f}({_pct(r.hodl_pct)})")
    print(f"網格:${cfg.lower:,.2f} ~ ${cfg.upper:,.2f}  {cfg.grids} 格({cfg.mode})  本金 ${cfg.capital:,.2f}  格距約 {e['spacing_pct']:.3f}%")
    print(f"成本假設:手續費 {args.fee_bps}bp + 滑點 {args.slippage_bps}bp"
          + (" + 池子價格衝擊" if state else "") + f",每筆 gas ${args.gas_usd}    K 線路徑:{args.path}")
    print("-" * 56)
    print(f"網格最終資產:${r.final_equity:,.2f}    報酬 {_pct(r.return_pct)}(持幣以收盤價計)")
    if r.exit_cost:
        print(f"若期末全部賣回 USDC:出場成本 ${r.exit_cost:,.2f},報酬 {_pct(r.exit_return_pct)}")
    print(f"全程持有幣(不含成本):{_pct(r.hodl_pct)}    持有 USDC:+0.00%")
    print(f"最大回撤:{r.max_drawdown * 100:.2f}%    價格在區間內的時間:{r.in_range_share * 100:.0f}%")
    print(f"成交 {len(r.trades)} 筆,完成買賣循環 {r.cycles} 次" + (f",因餘額不足略過 {r.skipped_buys} 次買入" if r.skipped_buys else ""))
    print(f"已實現網格獲利 ${r.realized_profit:+,.2f}    未實現損益 ${r.unrealized_pnl:+,.2f}")
    print(f"交易成本 ${r.trading_cost:,.2f}    gas ${r.gas_paid:,.2f}")
    bad = suspicious(candles)
    if bad:
        print(f"⚠ 有 {len(bad)} 根 K 線單小時振幅超過 15%,可能是池子異常價格,會扭曲結果。")
    if not e["viable"]:
        print("⚠ 以起始價估算,每個買賣循環扣完成本是虧的(見 economics 指令)。")
    if r.in_range_share < 0.5:
        print("⚠ 價格大半時間在網格區間外,網格沒有在運作。")
    print(DISCLAIMER)
    return 0


def _check_grid_lists(args):
    # 先驗證所有參數,避免下載完資料才報錯
    for g in args.grids_list:
        GridConfig(1.0, 2.0, g, args.capital, args.mode)
    if any(not 0 < r < 100 for r in args.ranges):
        raise ValueError("--ranges 的每個值必須介於 0 與 100 之間")


def cmd_scan(args):
    _check_grid_lists(args)
    if not 0 < args.train <= 1:
        raise ValueError("--train 必須介於 0 與 1 之間(1 = 不做樣本外驗證)")
    if args.top < 1:
        raise ValueError("--top 必須至少為 1")
    candles = fetch_candles(args.pool, args.token, args.days, use_cache=not args.no_cache)
    costs = _costs(args)
    rows, train, test = scan(candles, args.grids_list, args.ranges, args.capital, costs, args.mode, args.path, args.train)
    info = summarize(rows)

    print(f"樣本內:{_ts(train[0].ts)} ~ {_ts(train[-1].ts)} UTC({len(train)} 根)  幣價 {_pct(hodl_pct(train))}")
    if test:
        print(f"樣本外:{_ts(test[0].ts)} ~ {_ts(test[-1].ts)} UTC({len(test)} 根)  幣價 {_pct(hodl_pct(test))}")
    print(f"本金 ${args.capital:,.0f}  {args.mode}  成本:手續費 {args.fee_bps}bp + 滑點 {args.slippage_bps}bp,每筆 gas ${args.gas_usd}")
    print(f"共 {len(rows)} 組,依樣本內報酬排序(✗ = 每個買賣循環扣完成本是虧的)")
    head = ["名次", "格數", "區間", "格距", "樣本內報酬", "循環", "回撤"]
    if test:
        head += ["樣本外報酬", "循環"]
    body = []
    for i, r in enumerate(rows[: args.top], 1):
        cells = [str(i), str(r.grids), f"±{r.range_pct:g}%", f"{r.spacing_pct:.2f}%" + ("" if r.viable else "✗"),
                 _pct(r.train.return_pct), str(r.train.cycles), f"{r.train.max_drawdown * 100:.2f}%"]
        if r.test:
            cells += [_pct(r.test.return_pct), str(r.test.cycles)]
        body.append(cells)
    _table(head, body)
    if len(rows) > args.top:
        print(f"(只顯示前 {args.top} 名,可用 --top 調整)")

    print("-" * 56)
    best = info["best_train"]
    if test:
        print(f"樣本內最佳:{best.grids} 格 ±{best.range_pct:g}% → 樣本內 {_pct(best.train.return_pct)},樣本外 {_pct(best.test.return_pct)}")
        bt = info["best_test"]
        print(f"事後看樣本外最佳:{bt.grids} 格 ±{bt.range_pct:g}%({_pct(bt.test.return_pct)})——這是事後諸葛,實際上無法事先知道")
        coin = hodl_pct(test)
        beat_coin = sum(1 for r in rows if r.test.return_pct > coin)
        print(f"樣本外賺錢的組合:{info['test_profitable']} / {len(rows)}    勝過同期持有幣({_pct(coin)}):{beat_coin} / {len(rows)}")
        corr = info["rank_corr"]
        if corr is None:
            print("排名相關性:無法計算(組合太少或報酬沒有差異)")
        else:
            verdict = "排名大致延續" if corr >= 0.5 else "排名只部分延續" if corr >= 0.2 else "排名幾乎沒有延續"
            print(f"排名相關性(樣本內 vs 樣本外):{corr:+.2f} → {verdict}")
            # 排名穩定 ≠ 會賺錢:可能只是「每組都賠、但賠的順序一樣」
            if info["test_profitable"] == 0:
                print("⚠ 樣本外沒有任何組合賺錢。排名穩定只代表相對好壞一致,不代表這些參數能獲利。")
    print("※ 掃描越多組越容易挑到「剛好貼合過去」的參數;請以樣本外結果為主要參考。")
    print(DISCLAIMER)
    return 0


def _day(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%m-%d")


def cmd_walk(args):
    _check_grid_lists(args)
    if not 1 <= args.days <= 120:
        raise ValueError("--days 必須介於 1 到 120")
    if args.train_days + args.test_days > args.days:
        raise ValueError(f"--train-days + --test-days({args.train_days + args.test_days} 天)不能超過 --days({args.days} 天)")
    candles = fetch_candles(args.pool, args.token, args.days, use_cache=not args.no_cache)
    folds = walk_forward(candles, args.grids_list, args.ranges, args.capital, _costs(args), args.mode, args.path,
                         args.train_days * 24, args.test_days * 24, args.anchored)
    s = walk_summary(folds)

    window = "擴張視窗(樣本內從頭累積)" if args.anchored else f"滾動視窗(樣本內固定 {args.train_days} 天)"
    print(f"滾動式驗證:{s['folds']} 輪,每輪用過去資料挑出樣本內最佳參數,再拿接下來 {args.test_days} 天實跑  {window}")
    print(f"樣本外合計:{_ts(folds[0].test_start)} ~ {_ts(folds[-1].test_end)} UTC    每輪從 {s['combos']} 組中挑選")
    print(f"本金 ${args.capital:,.0f}  {args.mode}  成本:手續費 {args.fee_bps}bp + 滑點 {args.slippage_bps}bp,每筆 gas ${args.gas_usd}")
    body = [[str(i), f"{_day(f.test_start)}~{_day(f.test_end)}", f"{f.chosen.grids} 格 ±{f.chosen.range_pct:g}%",
             _pct(f.chosen.train.return_pct), _pct(f.chosen.test.exit_return_pct), _pct(f.hodl_pct), f"{f.chosen_rank}/{s['combos']}"]
            for i, f in enumerate(folds, 1)]
    _table(["輪", "樣本外期間", "選中參數", "樣本內", "樣本外(出場後)", "持有幣", "該輪名次"], body, left=(1, 2))

    print("-" * 56)
    print(f"依滾動挑選實際操作(各輪連乘):{_pct(s['wf_pct'])}    同期持有幣:{_pct(s['hodl_pct'])}")
    print(f"賺錢的輪數:{s['profitable']} / {s['folds']}    勝過同期持有幣:{s['beat_hodl']} / {s['folds']}    最差一輪:{_pct(s['worst_fold_pct'])}")
    print(f"選中參數優於該輪中位數:{s['beat_median']} / {s['folds']}    平均名次 {s['avg_rank']:.1f} / {s['combos']}    換參數 {s['changes']} 次")
    (bg, br), bp = s["fixed_best"]
    print(f"對照:整段固定用同一組參數 → 中位數 {_pct(s['fixed_median_pct'])};事後看最好的是 {bg} 格 ±{br:g}%({_pct(bp)}),無法事先知道")
    edge = s["wf_pct"] - s["fixed_median_pct"]
    if edge >= 1:
        print(f"→ 依過去表現挑參數,比「隨便固定一組」多 {edge:.2f} 個百分點,挑選可能有一點效果。")
    elif edge > -1:
        print(f"→ 依過去表現挑參數,和「隨便固定一組」只差 {edge:+.2f} 個百分點,看不出挑選有效果。")
    else:
        print(f"→ 依過去表現挑參數,比「隨便固定一組」少 {-edge:.2f} 個百分點:過去最好的參數對下一段沒有預測力。")
    if s["wf_pct"] <= 0:
        print("⚠ 滾動挑選的整體結果是虧損。")
    if s["wf_pct"] < s["hodl_pct"]:
        print("⚠ 整體輸給同期持有幣:上漲行情中網格會邊漲邊賣,少賺漲幅。")
    bad = suspicious(candles)
    if bad:
        print(f"⚠ 有 {len(bad)} 根 K 線單小時振幅超過 15%,可能是池子異常價格,會扭曲結果。")
    if s["folds"] < 4:
        print("⚠ 輪數太少,結論很不穩定;請增加 --days 或縮短 --test-days。")
    print("※ 每輪結束都假設全部賣回 USDC(含手續費、滑點與 gas),下一輪再依當時價格重新建倉。")
    print(DISCLAIMER)
    return 0


def cmd_swaps(args):
    GridConfig(1.0, 2.0, args.grids, args.capital, args.mode)  # 先驗證參數
    if not 0 < args.range_pct < 100:
        raise ValueError("--range-pct 必須介於 0 與 100 之間")
    if args.hours > MAX_HOURS:
        raise ValueError(f"--hours 最多 {MAX_HOURS} 小時")
    rpc = Rpc(args.rpc or os.environ.get("BASE_RPC_URL") or LOGS_RPC)
    state = read_pool(rpc, args.pool)

    def progress(x):
        print(f"\r下載 Swap 事件 {x * 100:5.1f}%", end="", file=sys.stderr, flush=True)

    swaps = fetch_swaps(rpc, state, args.hours, progress)
    print(file=sys.stderr)
    if len(swaps) < 2:
        raise ValueError("這段期間的成交太少,無法比較")
    p0 = swaps[0].price
    cfg = GridConfig(p0 * (1 - args.range_pct / 100), p0 * (1 + args.range_pct / 100), args.grids, args.capital, args.mode)
    costs = _costs(args)
    ticks, candles = as_ticks(swaps), as_candles(swaps)
    runs = [("逐筆成交(實際路徑)", len(ticks), simulate(ticks, cfg, costs))]
    for path, name in (("auto", "小時K線・預設路徑"), ("low-first", "小時K線・先低後高"), ("high-first", "小時K線・先高後低")):
        runs.append((name, len(candles), simulate(candles, cfg, costs, path)))

    print(f"節點:{rpc.host}    池子:{state.symbol0}/{state.symbol1}")
    print(f"期間:{_ts(swaps[0].ts)} ~ {_ts(swaps[-1].ts)} UTC(約,依區塊推算)  {len(swaps):,} 筆成交")
    print(f"價格:${p0:,.2f} → ${swaps[-1].price:,.2f}    網格 ±{args.range_pct:g}%  {cfg.grids} 格  本金 ${cfg.capital:,.0f}")
    _table(["資料", "價格點數", "成交", "循環", "報酬(出場後)"],
           [[name, f"{n:,}", str(len(r.trades)), str(r.cycles), _pct(r.exit_return_pct)] for name, n, r in runs], left=(0,))
    print("-" * 56)
    real, hourly = runs[0][2], runs[1][2]
    if real.cycles:
        print(f"小時 K 線(預設路徑)抓到逐筆結果 {hourly.cycles / real.cycles * 100:.0f}% 的買賣循環。")
    else:
        print("這段期間逐筆成交也沒有完成任何買賣循環,請拉長 --hours 或縮小 --range-pct。")
    print("※ 逐筆路徑包含夾擊交易等瞬間價格,真實機器人未必能在那些價格成交,所以是成交次數的「上限」。")
    print(DISCLAIMER)
    return 0


def cmd_lp(args):
    if (args.lower is None) != (args.upper is None):
        raise ValueError("--lower 與 --upper 必須一起給(或都不給,改用 --range-pct)")
    if args.lower is None and not 0 < args.range_pct < 100:
        raise ValueError("--range-pct 必須介於 0 與 100 之間")
    GridConfig(1.0, 2.0, args.grids, args.capital, args.mode)  # 先驗證網格參數
    state = read_pool(Rpc(args.rpc), args.pool)
    # LP 數學以「token0 用 token1 計價」為準;K 線若是另一個代幣的價格,單位會對不上
    if args.token.lower() != state.token0.lower():
        raise ValueError(f"lp 的 --token 必須是池子的 token0({state.symbol0} {state.token0})")
    candles = fetch_candles(args.pool, args.token, args.days, use_cache=not args.no_cache)
    p0 = candles[0].open
    if args.lower is not None:
        lower, upper = args.lower, args.upper
    else:
        lower, upper = p0 * (1 - args.range_pct / 100), p0 * (1 + args.range_pct / 100)
    costs = _costs(args)
    pool_l = human_liquidity(state.liquidity, state.decimals0, state.decimals1)
    lp = simulate_lp(candles, LpConfig(lower, upper, args.capital, state.fee_rate, pool_l, args.lp_gas_mult), costs)
    grid = simulate(candles, GridConfig(lower, upper, args.grids, args.capital, args.mode), costs)

    print(f"期間:{_ts(candles[0].ts)} ~ {_ts(candles[-1].ts)} UTC({len(candles)} 根小時 K 線)")
    print(f"價格:${p0:,.2f} → ${lp.end_price:,.2f}({_pct(hodl_pct(candles))})    區間 ${lower:,.2f} ~ ${upper:,.2f}  本金 ${args.capital:,.0f}")
    print(f"池子手續費 {state.fee_rate * 100:.2f}%    占池子區間流動性約 {lp.share * 100:.5f}%(以目前鏈上流動性估算)")
    print(f"價格在區間內的時間:{lp.in_range_share * 100:.0f}%")
    print("-" * 56)
    _table(["方式", "期末資產", "出場後報酬"],
           [["LP 集中流動性", f"${lp.final_equity:,.2f}", _pct(lp.exit_return_pct)],
            [f"網格 {args.grids} 格(自己下 swap)", f"${grid.final_equity:,.2f}", _pct(grid.exit_return_pct)],
            ["持有幣(不含成本)", "", _pct(hodl_pct(candles))]], left=(0,))
    print("-" * 56)
    print(f"LP 手續費收入 ${lp.fees:,.2f}    無常損失 ${lp.impermanent_loss:+,.2f}(相對於拿著建倉時的幣不動)")
    print(f"LP 建倉成本 ${lp.entry_cost:,.2f}、出場成本 ${lp.exit_cost:,.2f}    網格交易成本 ${grid.trading_cost:,.2f}、gas ${grid.gas_paid:,.2f}")
    if lp.in_range_share < 0.5:
        print("⚠ 價格大半時間在區間外,LP 部位沒有在賺手續費。")
    print("※ 手續費收入是粗估:池子流動性固定用現在的值、成交量依 K 線高低點分攤,誤差可能很大。")
    print(DISCLAIMER)
    return 0


def _paper_rpc(args):
    return Rpc(args.rpc or os.environ.get("BASE_RPC_URL") or LOGS_RPC)


def _paper_summary(p):
    e = p.engine
    in_range = p.cfg.lower <= p.last_price <= p.cfg.upper
    hours = (p.last_ts - p.started_ts) / 3600
    print(f"模擬盤「{p.name}」  {p.symbol0}/{p.symbol1}  已跑 {hours:.1f} 小時(開始 {_ts(p.started_ts)} UTC)")
    print(f"網格:${p.cfg.lower:,.2f} ~ ${p.cfg.upper:,.2f}  {p.cfg.grids} 格({p.cfg.mode})  本金 ${p.cfg.capital:,.2f}")
    print(f"價格:${p.start_price:,.2f} → ${p.last_price:,.2f}({_pct(p.hodl_pct())})"
          + ("" if in_range else "  ⚠ 目前在網格區間外"))
    print(f"資產:${p.equity():,.2f}  報酬 {_pct(p.return_pct())}(持幣以現價計)    全部賣回 USDC:{_pct(p.exit_return_pct())}")
    print(f"持有 {e.base:.6f} {p.symbol0} + {e.quote:,.2f} {p.symbol1}    最大回撤 {p.max_drawdown * 100:.2f}%")
    print(f"成交 {p.buys + p.sells} 筆(買 {p.buys} / 賣 {p.sells}),完成買賣循環 {p.sells} 次"
          + (f",因餘額不足略過 {e.skipped_buys} 次買入" if e.skipped_buys else ""))
    print(f"已實現網格獲利 ${e.realized_profit:+,.2f}    未實現損益 ${e.unrealized_pnl(p.last_price):+,.2f}    "
          f"交易成本 ${e.trading_cost:,.2f}  gas ${e.gas_paid:,.2f}")
    if p.gaps:
        print(f"⚠ 曾有 {p.gaps} 次停機超過 {MAX_HOURS} 小時,中間那段只能當作價格直接走過去,會漏算成交。")


def _print_trade(t, symbol0):
    side = "買入" if t.side == "buy" else "賣出"
    print(f"  {_ts(t.ts)}  {side} {t.qty:.6f} {symbol0} @ ${t.price:,.2f}(格子 {t.cell})")


def cmd_paper_run(args):
    if args.interval < 5:
        raise ValueError("--interval 至少 5 秒(公開節點有速率限制)")
    creating = not paper.exists(args.name)
    given = [k for k in ("grids", "capital", "mode", "range_pct", "lower", "upper", "fee_bps", "slippage_bps", "gas_usd")
             if getattr(args, k) is not None]
    if creating:
        if (args.lower is None) != (args.upper is None):
            raise ValueError("--lower 與 --upper 必須一起給(或都不給,改用 --range-pct)")
        if args.lower is None and not 0 < (args.range_pct or 10.0) < 100:
            raise ValueError("--range-pct 必須介於 0 與 100 之間")
        GridConfig(1.0, 2.0, args.grids or 20, args.capital or 1000.0, args.mode or "arithmetic")  # 先驗證參數
    rpc = _paper_rpc(args)
    if creating:
        state = read_pool(rpc, args.pool)
        costs = CostModel(fee_rate=(5.0 if args.fee_bps is None else args.fee_bps) / 10_000,
                          slippage_rate=(2.0 if args.slippage_bps is None else args.slippage_bps) / 10_000,
                          gas_usd=0.03 if args.gas_usd is None else args.gas_usd)
        cfg_args = {"grids": args.grids or 20, "capital": args.capital or 1000.0, "mode": args.mode or "arithmetic",
                    "range_pct": args.range_pct or 10.0, "lower": args.lower, "upper": args.upper}
        p = paper.create(args.name, state, cfg_args, costs)
        print(f"已建立模擬盤「{p.name}」,以目前價格 ${p.start_price:,.2f} 建倉(區塊 {p.last_block})")
    else:
        p = paper.load(args.name)
        if given:
            print(f"(模擬盤「{p.name}」已存在,沿用原本的設定,忽略 {', '.join('--' + k.replace('_', '-') for k in given)})")
        print(f"接續模擬盤「{p.name}」,從區塊 {p.last_block + 1} 開始補抓")
    _paper_summary(p)
    print(DISCLAIMER)
    if not args.once:
        print(f"每 {args.interval:g} 秒檢查一次新成交,按 Ctrl+C 停止(狀態會保存,之後可接續)")

    polls = 0
    try:
        while True:
            try:
                new, n_swaps, gap = paper.step(p, rpc)
            except RpcError as e:
                print(f"{_ts(time.time())}  ⚠ 讀取失敗,下次再試: {e}", file=sys.stderr)
                new, n_swaps, gap = [], 0, False
            if gap:
                print(f"⚠ 停機超過 {MAX_HOURS} 小時,只補抓最近 {MAX_HOURS} 小時,中間那段會漏算成交。")
            for t in new:
                _print_trade(t, p.symbol0)
            polls += 1
            if new or args.once or polls % args.status_every == 0:
                print(f"{_ts(p.last_ts)}  價格 ${p.last_price:,.2f}  資產 ${p.equity():,.2f}({_pct(p.return_pct())})"
                      f"  持有幣 {_pct(p.hodl_pct())}  循環 {p.sells}  本輪 {n_swaps} 筆鏈上成交")
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n已停止,狀態已保存。")
        _paper_summary(p)
        return 0


def cmd_paper_status(args):
    p = paper.load(args.name)
    _paper_summary(p)
    trades = paper.read_trades(args.name, args.trades)
    if trades:
        print(f"最近 {len(trades)} 筆成交:")
        for t in trades:
            _print_trade(Trade(int(t["ts"]), t["side"], float(t["price"]), float(t["qty"]), int(t["cell"])), p.symbol0)
    print("※ 狀態停在上次執行的時間;要更新請執行 paper run。")
    print(DISCLAIMER)
    return 0


def cmd_paper_list(args):
    names = paper.list_names()
    if not names:
        print("還沒有任何模擬盤;用 paper run --name <名稱> 建立")
        return 0
    rows = []
    for n in names:
        try:
            p = paper.load(n)
        except ValueError as e:
            rows.append([n, "存檔損毀", "", "", "", str(e)[:30]])
            continue
        rows.append([n, f"${p.cfg.lower:,.0f}~${p.cfg.upper:,.0f} {p.cfg.grids} 格", f"${p.cfg.capital:,.0f}",
                     _pct(p.return_pct()), _pct(p.hodl_pct()), _ts(p.last_ts)])
    _table(["名稱", "設定", "本金", "報酬", "持有幣", "最後更新 (UTC)"], rows, left=(0, 1))
    return 0


def _list_of(kind, name):
    def parse(v):
        try:
            items = [kind(x) for x in v.split(",") if x.strip()]
        except ValueError:
            raise argparse.ArgumentTypeError(f"{name} 必須是以逗號分隔的數字,例如 5,10,20")
        if not items or len(items) > 50 or any(x <= 0 for x in items):
            raise argparse.ArgumentTypeError(f"{name} 需要 1~50 個正數")
        return items

    return parse


def _positive(v):
    x = float(v)
    if not x > 0:
        raise argparse.ArgumentTypeError("必須大於 0")
    return x


def _positive_int(v):
    x = int(v)
    if x <= 0:
        raise argparse.ArgumentTypeError("必須是正整數")
    return x


def _nonneg(v):
    x = float(v)
    if x < 0:
        raise argparse.ArgumentTypeError("不能是負數")
    return x


def _add_common(p):
    p.add_argument("--rpc", help="Base RPC 網址(預設讀環境變數 BASE_RPC_URL,否則用公開節點)")
    p.add_argument("--pool", default=POOL_WETH_USDC_500, help="Uniswap v3 池子位址(預設 Base 的 WETH/USDC 0.05%%)")


def _add_costs(p):
    p.add_argument("--fee-bps", type=_nonneg, default=5.0, help="池子手續費,單位 bp(預設 5 = 0.05%%)")
    p.add_argument("--slippage-bps", type=_nonneg, default=2.0, help="額外滑點假設(bp)")
    p.add_argument("--gas-usd", type=_nonneg, default=0.03, help="每筆交易的 gas(美元);用 gas 指令實測後校準")


def _add_grid(p):
    p.add_argument("--grids", type=int, default=20, help="格數")
    p.add_argument("--capital", type=_positive, default=1000.0, help="本金(USDC)")
    p.add_argument("--mode", choices=("arithmetic", "geometric"), default="arithmetic", help="等差或等比網格")


def build_parser():
    ap = argparse.ArgumentParser(prog="defigrid", description="Base 鏈 Uniswap v3 網格交易的唯讀分析與模擬")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pool", help="讀取池子的鏈上狀態與價格衝擊")
    _add_common(p)
    p.set_defaults(fn=cmd_pool)

    p = sub.add_parser("gas", help="估算 Base 上一次 swap 的 gas 成本")
    _add_common(p)
    p.add_argument("--gas-units", type=_positive_int, default=150_000, help="一次 swap 預估的 gas 用量")
    p.add_argument("--tx-bytes", type=_positive_int, default=400, help="一筆 swap 交易的大小(bytes),用來估 L1 資料費")
    p.set_defaults(fn=cmd_gas)

    p = sub.add_parser("economics", help="估算一組網格參數每個買賣循環是賺是虧")
    _add_common(p)
    _add_grid(p)
    _add_costs(p)
    p.add_argument("--lower", type=_positive, required=True, help="區間下界")
    p.add_argument("--upper", type=_positive, required=True, help="區間上界")
    p.add_argument("--price", type=_positive, help="參考價格(省略則讀鏈上)")
    p.set_defaults(fn=cmd_economics)

    p = sub.add_parser("backtest", help="用歷史 K 線回測網格")
    _add_common(p)
    _add_grid(p)
    _add_costs(p)
    p.add_argument("--token", default=WETH_BASE, help="要看價格的代幣位址(預設 WETH)")
    p.add_argument("--days", type=int, default=30, help="回測最近幾天(1~120)")
    p.add_argument("--lower", type=_positive, help="區間下界(與 --upper 一起給)")
    p.add_argument("--upper", type=_positive, help="區間上界")
    p.add_argument("--range-pct", type=_positive, default=10.0, help="沒給上下界時,以起始價為中心 ±百分比")
    p.add_argument("--path", choices=("auto", "low-first", "high-first"), default="auto", help="一根 K 線內假設的價格路徑")
    p.add_argument("--live-impact", action="store_true", help="用鏈上池子流動性估算價格衝擊")
    p.add_argument("--no-cache", action="store_true", help="不使用快取,重新下載 K 線")
    p.set_defaults(fn=cmd_backtest)

    p = sub.add_parser("scan", help="一次回測多組格數 × 區間,並用樣本外資料驗證")
    _add_common(p)
    _add_costs(p)
    p.add_argument("--token", default=WETH_BASE, help="要看價格的代幣位址(預設 WETH)")
    p.add_argument("--days", type=int, default=60, help="使用最近幾天的資料(1~120)")
    p.add_argument("--grids-list", type=_list_of(int, "--grids-list"), default=[5, 10, 15, 20, 30, 40], help="要試的格數,逗號分隔")
    p.add_argument("--ranges", type=_list_of(float, "--ranges"), default=[5.0, 10.0, 15.0, 20.0], help="要試的區間 ±百分比,逗號分隔")
    p.add_argument("--capital", type=_positive, default=1000.0, help="本金(USDC)")
    p.add_argument("--mode", choices=("arithmetic", "geometric"), default="arithmetic", help="等差或等比網格")
    p.add_argument("--path", choices=("auto", "low-first", "high-first"), default="auto", help="一根 K 線內假設的價格路徑")
    p.add_argument("--train", type=float, default=0.7, help="樣本內比例(預設 0.7;1 = 不做樣本外驗證)")
    p.add_argument("--top", type=int, default=15, help="顯示前幾名")
    p.add_argument("--no-cache", action="store_true", help="不使用快取,重新下載 K 線")
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("walk", help="滾動式驗證:反覆用過去挑參數、用接下來的資料驗證")
    _add_common(p)
    _add_costs(p)
    p.add_argument("--token", default=WETH_BASE, help="要看價格的代幣位址(預設 WETH)")
    p.add_argument("--days", type=int, default=120, help="使用最近幾天的資料(1~120)")
    p.add_argument("--train-days", type=_positive_int, default=30, help="每輪用來挑參數的天數")
    p.add_argument("--test-days", type=_positive_int, default=7, help="每輪驗證的天數(也是往後推進的天數)")
    p.add_argument("--anchored", action="store_true", help="樣本內從資料開頭一路累積,而不是固定長度")
    p.add_argument("--grids-list", type=_list_of(int, "--grids-list"), default=[5, 10, 15, 20, 30, 40], help="要試的格數,逗號分隔")
    p.add_argument("--ranges", type=_list_of(float, "--ranges"), default=[5.0, 10.0, 15.0, 20.0], help="要試的區間 ±百分比,逗號分隔")
    p.add_argument("--capital", type=_positive, default=1000.0, help="本金(USDC)")
    p.add_argument("--mode", choices=("arithmetic", "geometric"), default="arithmetic", help="等差或等比網格")
    p.add_argument("--path", choices=("auto", "low-first", "high-first"), default="auto", help="一根 K 線內假設的價格路徑")
    p.add_argument("--no-cache", action="store_true", help="不使用快取,重新下載 K 線")
    p.set_defaults(fn=cmd_walk)

    p = sub.add_parser("swaps", help="用鏈上逐筆成交檢查小時 K 線低估了多少成交")
    p.add_argument("--rpc", help=f"支援 eth_getLogs 的 Base RPC(預設讀 BASE_RPC_URL,否則用 {LOGS_RPC})")
    p.add_argument("--pool", default=POOL_WETH_USDC_500, help="Uniswap v3 池子位址(預設 Base 的 WETH/USDC 0.05%%)")
    _add_grid(p)
    _add_costs(p)
    p.add_argument("--hours", type=_positive_int, default=24, help=f"最近幾小時(1~{MAX_HOURS})")
    p.add_argument("--range-pct", type=_positive, default=3.0, help="以起始價為中心 ±百分比")
    p.set_defaults(fn=cmd_swaps, grids=10)

    p = sub.add_parser("lp", help="模擬 Uniswap v3 集中流動性部位,並和網格比較")
    _add_common(p)
    _add_grid(p)
    _add_costs(p)
    p.add_argument("--token", default=WETH_BASE, help="要看價格的代幣位址(預設 WETH)")
    p.add_argument("--days", type=int, default=30, help="回測最近幾天(1~120)")
    p.add_argument("--lower", type=_positive, help="區間下界(與 --upper 一起給)")
    p.add_argument("--upper", type=_positive, help="區間上界")
    p.add_argument("--range-pct", type=_positive, default=10.0, help="沒給上下界時,以起始價為中心 ±百分比")
    p.add_argument("--lp-gas-mult", type=_nonneg, default=2.0, help="加入 / 移除流動性的 gas 是 swap 的幾倍")
    p.add_argument("--no-cache", action="store_true", help="不使用快取,重新下載 K 線")
    p.set_defaults(fn=cmd_lp)

    pp = sub.add_parser("paper", help="即時模擬盤:用鏈上真實成交持續跑網格(不碰錢包)")
    psub = pp.add_subparsers(dest="paper_cmd", required=True)
    p = psub.add_parser("run", help="建立或接續一個模擬盤,持續追蹤新成交")
    p.add_argument("--name", required=True, help="模擬盤名稱(小寫英數字、- 與 _)")
    p.add_argument("--rpc", help=f"支援 eth_getLogs 的 Base RPC(預設讀 BASE_RPC_URL,否則用 {LOGS_RPC})")
    p.add_argument("--pool", default=POOL_WETH_USDC_500, help="Uniswap v3 池子位址(預設 Base 的 WETH/USDC 0.05%%)")
    p.add_argument("--grids", type=int, help="格數(建立時用,預設 20)")
    p.add_argument("--capital", type=_positive, help="本金 USDC(建立時用,預設 1000)")
    p.add_argument("--mode", choices=("arithmetic", "geometric"), help="等差或等比網格(建立時用)")
    p.add_argument("--range-pct", type=_positive, help="以目前價格為中心 ±百分比(建立時用,預設 10)")
    p.add_argument("--lower", type=_positive, help="區間下界(建立時用,與 --upper 一起給)")
    p.add_argument("--upper", type=_positive, help="區間上界")
    p.add_argument("--fee-bps", type=_nonneg, help="池子手續費 bp(建立時用,預設 5)")
    p.add_argument("--slippage-bps", type=_nonneg, help="額外滑點 bp(建立時用,預設 2)")
    p.add_argument("--gas-usd", type=_nonneg, help="每筆 gas 美元(建立時用,預設 0.03)")
    p.add_argument("--interval", type=float, default=15.0, help="每幾秒檢查一次(至少 5)")
    p.add_argument("--status-every", type=_positive_int, default=20, help="沒有成交時,每幾次檢查印一次狀態")
    p.add_argument("--once", action="store_true", help="只更新一次就結束(適合排程)")
    p.set_defaults(fn=cmd_paper_run)
    p = psub.add_parser("status", help="查看模擬盤狀態與最近成交(不連網)")
    p.add_argument("--name", required=True, help="模擬盤名稱")
    p.add_argument("--trades", type=_positive_int, default=10, help="顯示最近幾筆成交")
    p.set_defaults(fn=cmd_paper_status)
    p = psub.add_parser("list", help="列出所有模擬盤")
    p.set_defaults(fn=cmd_paper_list)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (ValueError, RpcError, DataError) as e:
        print(f"錯誤: {e}", file=sys.stderr)
        return 2
