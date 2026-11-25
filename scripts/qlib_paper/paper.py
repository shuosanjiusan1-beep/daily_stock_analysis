"""Conservative daily-bar paper ledger. No broker or order API exists here."""
from copy import deepcopy
import math
from pathlib import Path

from data import value_at


class PaperBlocked(ValueError):
    pass


def initial_state():
    return {'version': 1, 'cash': 100000.0, 'positions': {}, 'signal': None,
            'last_settled': None, 'events': [], 'notifications': []}


def validate(state):
    if state.get('version') != 1 or not math.isfinite(state['cash']) or state['cash'] < 0:
        raise PaperBlocked('Invalid state; manual review required')
    for position in state['positions'].values():
        if position['shares'] <= 0 or position['shares'] % 100:
            raise PaperBlocked('Invalid position')
    return state


def bars(provider, codes, day):
    dates = (Path(provider) / 'calendars/day.txt').read_text().splitlines()
    index = dates.index(day)
    result = {}
    for code in codes:
        values = {f: value_at(provider, code, f, index) for f in ('open', 'close', 'factor', 'volume')}
        previous = value_at(provider, code, 'close', index - 1)
        if not all(math.isfinite(v) and v > 0 for v in [*values.values(), previous]):
            raise PaperBlocked(f'Missing or suspended daily bar: {code}')
        result[code] = {'open': values['open'] / values['factor'],
                        'close': values['close'] / values['factor'], 'factor': values['factor'],
                        'gap': values['open'] / previous - 1}
    return result


def settle(state, day, market, next_session, rebalance=True):
    state = deepcopy(validate(state))
    if state['last_settled'] == day:
        return state
    signal = state['signal']
    if signal is None or not rebalance:
        for code, pos in state['positions'].items():
            if code not in market or abs(market[code]['factor'] / pos['factor'] - 1) > 1e-5:
                raise PaperBlocked('Corporate action or missing valuation; reconciliation required')
            pos['mark'] = market[code]['close']
        state['last_settled'] = day
        return state
    if next_session(signal['asof']) != day:
        raise PaperBlocked('Non-consecutive session; reconciliation required')
    for code, pos in state['positions'].items():
        if code not in market or abs(market[code]['factor'] / pos['factor'] - 1) > 1e-5:
            raise PaperBlocked('Corporate action or missing valuation; reconciliation required')
    targets = signal['codes'][:5]
    # Conservative 4.5% open-gap threshold also avoids uncertain ST/price-limit fills.
    for code in list(state['positions']):
        pos = state['positions'][code]
        bar = market[code]
        if code in targets or abs(bar['gap']) >= .045 or pos['bought'] >= day:
            continue
        proceeds = pos['shares'] * bar['open'] * .9995
        fee = max(5.0, proceeds * .0015)
        state['cash'] += proceeds - fee
        state['events'].append({'day': day, 'code': code, 'side': 'paper_sell',
                                'shares': pos['shares'], 'price': bar['open'] * .9995, 'fee': fee})
        del state['positions'][code]
    equity = state['cash'] + sum(p['shares'] * market[c]['open'] for c, p in state['positions'].items())
    for code in targets:
        if code in state['positions'] or len(state['positions']) >= 5:
            continue
        bar = market[code]
        if abs(bar['gap']) >= .045:
            continue
        price = bar['open'] * 1.0005
        # At most 16% initial day equity per new position; retain a cash reserve.
        available = min(equity * .16, max(0, state['cash'] - equity * .2))
        shares = int(max(0, available - 5) / (price * 1.0005) // 100) * 100
        if not shares:
            continue
        notional = shares * price
        fee = max(5.0, notional * .0005)
        if notional + fee > state['cash']:
            raise PaperBlocked('Insufficient paper cash')
        state['cash'] -= notional + fee
        state['positions'][code] = {'shares': shares, 'bought': day, 'factor': bar['factor'], 'cost': price}
        state['events'].append({'day': day, 'code': code, 'side': 'paper_buy', 'shares': shares, 'price': price, 'fee': fee})
    for code, pos in state['positions'].items():
        pos['mark'] = market[code]['close']
    state['last_settled'] = day
    state['events'] = state['events'][-1000:]
    return validate(state)
