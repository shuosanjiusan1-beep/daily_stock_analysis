"""Independent Qlib paper workflow entry point."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

from data import check, download
from model import benchmark, rolling_signal
from notify import context, send
from paper import bars, settle, validate
import state as storage

LABELS = {'daily': '每日收盘总结', 'weekly': '周度回顾', 'weekend': '周末/下周开市前预览',
          'pre': '盘前', 'intra': '盘中', 'post': '盘后'}


def session_context(now, phase):
    calendar = xcals.get_calendar('XSHG')
    today = now.date().isoformat()
    if not calendar.is_session(today):
        if phase in ('weekly', 'weekend'):
            expected = calendar.date_to_session(today, direction='previous').date().isoformat()
            return calendar, expected
        return calendar, None
    # Never claim an unclosed session is available, even on a manual post run.
    if phase == 'post' and now.hour >= 16:
        expected = today
    else:
        expected = calendar.previous_session(today).date().isoformat()
    return calendar, expected


def render(state, phase, expected, status, news=''):
    signal = state.get('signal')
    positions = state['positions']
    nav = state['cash'] + sum(p['shares'] * p.get('mark', p['cost']) for p in positions.values())
    lines = [f'Qlib A股模拟辅助 · {LABELS[phase]}', '仅为模拟研究，不连接券商，不下单。', '', status,
             f'本次要求行情日期：{expected}', f"账本结算至：{state['last_settled'] or '尚未开始'}",
             '行情类型：已发布的收盘日线；不提供实时盘中价格。',
             f"模拟现金：{state['cash']:.2f} 元；按最后结算日估值的模拟总资产：{nav:.2f} 元。",
             '', '模拟持仓：']
    lines += [f"{c}：{p['shares']} 股，最后估值 {p.get('mark', p['cost']):.3f} 元" for c, p in positions.items()] or ['空仓']
    lines += ['', '下一交易日的模型候选（不是实盘指令）：']
    if signal:
        lines += [f"信号日期：{signal['asof']}；" + '、'.join(signal['codes'][:5]),
                  ('每周只在下一周首个交易日按开盘价模拟调仓；当前候选不代表成交。'
                   if signal.get('cadence') == 'weekly' else
                   '首次过渡观察信号；当前不据此模拟成交。')]
    else:
        lines += ['暂无通过校验的信号。']
    lines += ['', '假设：初始10万元、最多5只、100股整手、T+1；费用和滑点为模拟假设。',
              '停牌/缺价/复权因子变化暂停结算并要求核查；开盘跳空超过4.5%不假设成交。', '', news]
    if phase == 'weekly':
        week = datetime.fromisoformat(expected).date().isocalendar()[:2]
        events = [e for e in state['events'] if datetime.fromisoformat(e['day']).date().isocalendar()[:2] == week]
        lines[-1:-1] = ['', '本周模拟账本变动：'] + ([
            f"{e['day']} {e['side']} {e['code']} {e['shares']}股 @ {e['price']:.3f}元"
            for e in events] or ['本周没有模拟成交。'])
    return '
'.join(lines)


def daily(phase, output, provider=None):
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    calendar, expected = session_context(now, phase)
    if expected is None:
        print('Non-trading day; no notification or ledger update')
        return
    state, sha = storage.load()
    validate(state)
    key = f'{now.date()}:{phase}'
    if key in state['notifications']:
        print('Notification already completed')
        return
    provider = Path(provider) if provider else download(output / 'data')
    try:
        freshness = check(provider, expected)
    except ValueError:
        # Data lag is expected with the community feed; do not mutate the ledger.
        body = render(state, phase, expected, '数据未齐或校验未通过：暂停生成新信号和模拟结算。次日盘前重试。')
    else:
        (output / 'freshness.json').write_text(json.dumps(freshness, indent=2))
        if phase in ('daily', 'pre', 'post') and state['last_settled'] != expected:
            codes = set(state['positions']) | set((state['signal'] or {}).get('codes', [])[:5])
            market = bars(provider, codes, expected) if codes else {}
            next_day = calendar.next_session(expected).date().isoformat()
            signal = state.get('signal') or {}
            signal_open = (calendar.next_session(signal['asof']).date().isoformat()
                           if signal.get('cadence') == 'weekly' else None)
            if signal_open and signal_open < expected and state.get('last_settled') < signal_open:
                raise ValueError('Missed weekly paper rebalance; reconciliation required')
            rebalance = bool(signal_open and signal_open == expected)
            updated = settle(state, expected, market,
                             lambda d: calendar.next_session(d).date().isoformat(), rebalance=rebalance)
            if (phase in ('daily', 'pre', 'post') and
                    datetime.fromisoformat(next_day).date().isocalendar()[:2] !=
                    datetime.fromisoformat(expected).date().isocalendar()[:2]):
                updated['signal'] = rolling_signal(provider, expected)
                updated['signal']['cadence'] = 'weekly'
            sha = storage.save(updated, sha)
            state = updated
        if phase == 'daily' and now.weekday() == 0:
            status = f'周一开市前概览；最近完整行情截至{expected}。'
        elif phase == 'weekly':
            status = f'周度回顾；最近完整行情截至{expected}。'
        elif phase == 'weekend':
            status = f'周末预览；最近完整行情截至{expected}，并附周末资讯。'
        else:
            status = '日线时效校验通过。' if state.get('last_settled') == expected else '账本尚未完成本次结算；仅报告状态，不补记成交。'
        body = render(state, phase, expected, status, context(str(now.date()), weekend=phase == 'weekend'))
    (output / 'report.txt').write_text(body)
    send(f'[模拟] Qlib {LABELS[phase]} {now.date()}', body)
    state['notifications'] = (state['notifications'] + [key])[-120:]
    storage.save(state, sha)
    print('Paper report delivered')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['validate', 'initialize', 'test-mail', 'daily', 'weekly', 'weekend', 'pre', 'intra', 'post'])
    parser.add_argument('--provider')
    parser.add_argument('--output', default='reports/qlib-paper')
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    if args.mode == 'initialize':
        storage.initialize()
    elif args.mode == 'test-mail':
        send('[模拟] Qlib 邮件连接测试', '这是独立 Qlib 模拟系统的连接测试。未修改持仓，未连接券商，未下单。\n' + context(str(datetime.now(ZoneInfo('Asia/Shanghai')).date())))
        print('Test mail delivered')
    elif args.mode == 'validate':
        provider = Path(args.provider) if args.provider else download(output / 'data')
        now = datetime.now(ZoneInfo('Asia/Shanghai'))
        calendar = xcals.get_calendar('XSHG')
        expected = calendar.date_to_session(str(now.date()), direction='previous')
        if expected.date() == now.date():
            expected = calendar.previous_session(expected)
        freshness = check(provider, expected.date().isoformat())
        (output / 'freshness.json').write_text(json.dumps(freshness, indent=2))
        benchmark(provider, output)
        print('Data and official baseline validated')
    else:
        daily(args.mode, output, args.provider)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Never print HTTP/SMTP exception payloads, request URLs or environment.
        print(f'Qlib paper run failed ({type(exc).__name__}); ledger requires review', file=sys.stderr)
        if len(sys.argv) > 1 and sys.argv[1] in LABELS:
            try:
                send('[模拟] Qlib 运行异常', '本次运行未能完整完成，请检查 GitHub Actions 状态。禁止据此推断新成交；已保存的模拟账本不会自动重置。')
            except Exception:
                print('Failure notification unavailable', file=sys.stderr)
        sys.exit(1)
