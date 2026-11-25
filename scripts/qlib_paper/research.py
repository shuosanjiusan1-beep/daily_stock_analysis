"""Evidence-led daily research, independent of simulated execution."""
from html import escape
import math
from pathlib import Path
import struct


def history(provider, code, field, end, count=61):
    path = Path(provider) / 'features' / code.lower() / f'{field}.day.bin'
    if not path.is_file():
        raise ValueError('Missing feature')
    with path.open('rb') as stream:
        raw = stream.read(4)
        if len(raw) != 4:
            raise ValueError('Invalid feature header')
        start = struct.unpack('<f', raw)[0]
        if not math.isfinite(start) or start != int(start) or end - count + 1 < start:
            raise ValueError('Insufficient history')
        stream.seek(4 * (1 + end - count + 1 - int(start)))
        raw = stream.read(count * 4)
    if len(raw) != count * 4:
        raise ValueError('Incomplete history')
    values = list(struct.unpack('<' + 'f' * count, raw))
    if not all(math.isfinite(x) and x > 0 for x in values):
        raise ValueError('Missing or suspended bar')
    return values


def metrics(provider, code, end):
    close = history(provider, code, 'close', end)
    high = history(provider, code, 'high', end)
    low = history(provider, code, 'low', end)
    volume = history(provider, code, 'volume', end)
    factor = history(provider, code, 'factor', end, 1)[0]
    return {'code': code, 'close': close[-1] / factor,
            'day': close[-1] / close[-2] - 1, 'week': close[-1] / close[-6] - 1,
            'month': close[-1] / close[-21] - 1,
            'ma5': sum(close[-5:]) / 5 / factor, 'ma20': sum(close[-20:]) / 20 / factor,
            'support': min(low[-20:]) / factor,
            'resistance': max(high[-21:-1]) / factor,
            'volume_ratio': volume[-1] / (sum(volume[-6:-1]) / 5)}


def build(provider, asof, signal, previous=None):
    dates = (Path(provider) / 'calendars/day.txt').read_text().splitlines()
    index = dates.index(asof)
    benchmark = metrics(provider, 'SH000300', index)
    active = {line.split()[0] for line in (Path(provider) / 'instruments/csi300.txt').read_text().splitlines()
              if line.split()[1] <= asof <= line.split()[2]}
    rows, missing = {}, []
    for code in sorted(active):
        try:
            rows[code] = metrics(provider, code, index)
        except ValueError:
            missing.append(code)
    ranked = (signal or {}).get('ranked', [])
    if signal and signal.get('asof') != asof:
        raise ValueError('Stale research signal')
    old = {r['code']: n for n, r in enumerate((previous or {}).get('ranked', []), 1)}
    strong, weak = [], []
    for rank, item in enumerate(ranked, 1):
        row = rows.get(item['code'])
        if row is None:
            continue
        row = dict(row, rank=rank, score=item['score'], rank_change=old.get(item['code'], rank) - rank)
        row['excess_week'] = row['week'] - benchmark['week']
        row['trend'] = ('偏强' if row['close'] > row['ma5'] > row['ma20'] and row['excess_week'] > 0
                        else '偏弱' if row['close'] < row['ma20'] and row['excess_week'] < 0 else '分歧/观察')
        strong.append(row)
    weak = list(reversed(strong[-3:])) if len(strong) >= 10 else []
    return {'asof': asof, 'benchmark': benchmark, 'coverage': len(rows), 'universe': len(active),
            'missing': missing, 'up': sum(r['day'] > 0 for r in rows.values()),
            'down': sum(r['day'] < 0 for r in rows.values()),
            'above_ma20': sum(r['close'] > r['ma20'] for r in rows.values()),
            'strong': strong[:5], 'weak': weak,
            'signal': signal, 'previous_asof': (previous or {}).get('asof')}


def pct(value):
    return f'{value:+.2%}'


def render(report, phase, next_session):
    b = report['benchmark']
    label = {'weekly': '本周复盘与下周候选', 'weekend': '周末复盘与周一观察清单'}.get(phase, '每日选股研究与观察计划')
    lines = [f'【{label}】', f"行情截至 {report['asof']} 收盘；计划观察日 {next_session}。",
             '数据源：chenditc/investment_data 社区日线；不是实时行情。', '', '一、市场发生了什么',
             f"沪深300：收盘 {b['close']:.2f}；当日 {pct(b['day'])}；近5交易日 {pct(b['week'])}；近20交易日 {pct(b['month'])}。",
             f"沪深300样本内：上涨 {report['up']}、下跌 {report['down']}；有效连续历史 {report['coverage']}/{report['universe']}；"
             f"站上20日均线 {report['above_ma20']} 只。该样本不代表全部A股。"]
    healthy = b['close'] > b['ma20'] and report['above_ma20'] > report['coverage'] / 2
    lines += ['市场观察：指数位于20日均线上方且多数有效样本站上均线，趋势广度较好。' if healthy else
              '市场观察：指数趋势与个股广度尚未同时确认，优先等待信号，不因模型排名靠前就追买。', '', '二、重点候选：模型排名与价格证据']
    signal = report.get('signal')
    if not signal or not report['strong']:
        lines += ['本次模型预测不可用：不生成候选或买入价位。市场统计与新闻仍可独立阅读。']
    else:
        lines += [f"Microsoft Qlib / Alpha158 / LightGBM；预测截面 {signal['asof']}；训练截止 {signal['train_end']}；"
                  f"验证截止 {signal['validation_end']}。",
                  f"研究目标为下一交易日收盘起的未来{signal.get('horizon', 1)}个交易日相对表现排序；分数不是上涨概率或收益承诺。",
                  '下列技术指标是独立交叉检查，不是对模型内部选股原因的因果解释。']
        for r in report['strong']:
            lines += ['', f"{r['code']}｜模型第 {r['rank']} 名｜技术状态：{r['trend']}",
                      f"收盘 {r['close']:.2f} 元；日涨跌 {pct(r['day'])}；5日 {pct(r['week'])}；"
                      f"相对沪深300的5日表现 {pct(r['excess_week'])}。",
                      f"5日/20日均线 {r['ma5']:.2f}/{r['ma20']:.2f} 元；当日量/前5日均量 {r['volume_ratio']:.2f} 倍。"]
            if report['previous_asof']:
                lines += [f"相对 {report['previous_asof']} 模型排名变化 {r['rank_change']:+d} 位（正数为上升；新入样本记0）。"]
            risk = 1 - r['support'] / r['resistance']
            if r['trend'] != '偏强' or not healthy or risk > .08 or risk <= 0:
                reasons = []
                if r['trend'] != '偏强':
                    reasons.append('个股趋势尚未确认')
                if not healthy:
                    reasons.append('市场广度偏弱')
                if risk > .08 or risk <= 0:
                    reasons.append('结构风险区间不适合当前规则')
                lines += ['当前行动：等待；原因：' + '、'.join(reasons) + '。',
                          f"重新评估条件：收复并站稳20日均线 {r['ma20']:.2f} 元、近5日强于指数，再观察前20日高点 {r['resistance']:.2f} 元的放量突破。"]
            else:
                lines += [f"条件式观察：后续完整日线收盘突破前20日高点 {r['resistance']:.2f} 元、量比≥1.2，且仍站上20日均线时，才进入下一交易日复核清单。",
                          f"次日若高开超过触发位2%，不按该计划追入；参考失效线为20日低点 {r['support']:.2f} 元。",
                          '这些是未校准的技术观察规则，不代表可成交报价；实际买入前还需核对公告、ST/停牌与涨跌停状态。']
            if r['close'] < r['ma20']:
                lines += [f"风险状态：已在20日均线下方；若已有持仓需优先复核弱势原因。下一结构观察线 {r['support']:.2f} 元。"]
            else:
                lines += [f"退出复核线：20日均线 {r['ma20']:.2f} 元；结构低点 {r['support']:.2f} 元。收盘失守时重新评估原逻辑。"]
    lines += ['', '三、模型低排名观察（可能与当前价格强势相反）']
    lines += [f"{r['code']}｜模型第{r['rank']}名｜{r['trend']}；5日 {pct(r['week'])}，相对沪深300 {pct(r['excess_week'])}，收盘/20日均线 {r['close']:.2f}/{r['ma20']:.2f} 元。"
              for r in report['weak']] or ['模型结果不足，暂无弱势名单。']
    lines += ['', '四、观察多久、什么时候重新判断',
              '研究观察周期：先看5个交易日，每天复核、周末重新评估；这是研究窗口，不是固定卖出期限。',
              '若价格趋势、相对强弱和模型排序继续支持，可以继续观察；若失效条件出现，提前复核，不等满一周。',
              '观察价位仅对下一交易日的复核有效；下一份报告更新后使用新价位。每日分析不等于每日交易。',
              '风险线不是保证成交价格，次日可能跳空。模型低排名也不代表价格已经走弱，更不是做空指令。',
              '当前未录入你的真实持仓和成交成本，因此不能计算你的实际盈亏，也不把模拟空仓当成你的账户状态。',
              '风险线与量比阈值为透明启发式，尚未完成针对本版5日研究规则的样本外回测。官方历史基线不能替它证明收益。']
    if phase in ('weekly', 'weekend'):
        lines += ['', '五、周度复盘重点',
                  f"近5交易日指数 {pct(b['week'])}；本次以最新有效日线重排候选。",
                  '本版尚未积累完整一周的预测档案，不报告预测胜率；模拟成交复盘另附在账本部分。' if not report['previous_asof'] else
                  '排名变化基于上次保存的研究截面，不等同于完整周度预测胜率。',
                  '周一开盘前重点复核周末公告、政策与海外市场变化；周末新闻不会被当作周末成交价格。']
    return '\n'.join(lines)


def html_report(body):
    blocks = []
    for block in body.split('\n\n'):
        lines = block.splitlines()
        if not lines:
            continue
        lead = lines[0]
        heading = lead.startswith(('【', '一、', '二、', '三、', '四、', '五、'))
        stock = '｜模型第' in lead
        title = '<h2 style="font-size:18px;margin:0 0 12px;color:#163a64">' + escape(lead) + '</h2>' if heading or stock else escape(lead)
        rest = '<br>'.join(escape(line) for line in lines[1:])
        style = 'background:#f4f7fc;border-left:4px solid #315f94;padding:18px;' if stock else 'padding:12px 0;'
        blocks.append('<section style="line-height:1.8;margin:16px 0;' + style + '">' + title + ('<br>' + rest if rest else '') + '</section>')
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            '<body style="margin:0;background:#edf1f6;color:#172238;font-family:Arial,sans-serif">'
            '<main style="max-width:860px;margin:24px auto;padding:24px;background:white;border-radius:12px">'
            '<h1 style="font-size:26px;color:#163a64">A股研究 · 每日决策参考</h1>'
            + ''.join(blocks) + '</main></body></html>')
