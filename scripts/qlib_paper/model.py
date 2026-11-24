"""Qlib official baseline and separately labelled rolling paper predictions."""
from copy import deepcopy
import json
from pathlib import Path

import pandas as pd
import yaml


def initialize(provider):
    import qlib
    qlib.init(provider_uri=str(Path(provider).resolve()), region='cn', kernels=2)


def configuration():
    config = yaml.safe_load(Path(__file__).with_name('official_alpha158.yaml').read_text())
    config['task']['model']['kwargs']['num_threads'] = 2
    return config


def fit_predict(config):
    from qlib.utils import init_instance_by_config
    model = init_instance_by_config(config['task']['model'])
    dataset = init_instance_by_config(config['task']['dataset'])
    model.fit(dataset)
    return model.predict(dataset)


def benchmark(provider, output):
    from qlib.backtest import backtest
    from qlib.contrib.evaluate import risk_analysis
    initialize(provider)
    config = configuration()
    predictions = fit_predict(config)
    strategy = deepcopy(config['port_analysis_config']['strategy'])
    strategy['kwargs']['signal'] = predictions
    portfolios, _ = backtest(
        executor={'class': 'SimulatorExecutor', 'module_path': 'qlib.backtest.executor',
                  'kwargs': {'time_per_step': 'day', 'generate_portfolio_metrics': True}},
        strategy=strategy, **config['port_analysis_config']['backtest'])
    report, _ = portfolios['1day']
    if report.empty or not report[['return', 'bench', 'cost']].notna().all().all():
        raise ValueError('Invalid benchmark output')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    report.to_csv(output / 'benchmark_daily.csv')
    result = {
        'baseline': 'microsoft/qlib v0.9.7 LightGBM Alpha158',
        'changes': ['local provider path', 'num_threads=2', 'kernels=2'],
        'first_day': str(report.index.min().date()), 'last_day': str(report.index.max().date()),
        'days': len(report),
        'excess_after_cost': risk_analysis(report['return'] - report['bench'] - report['cost'])['risk'].to_dict(),
        'strategy_after_cost': risk_analysis(report['return'] - report['cost'])['risk'].to_dict(),
    }
    (output / 'benchmark.json').write_text(json.dumps(result, indent=2, allow_nan=False))
    return result


def rolling_signal(provider, asof):
    """Three-year train, six-month validation, two-session label embargo."""
    initialize(provider)
    dates = (Path(provider) / 'calendars/day.txt').read_text().splitlines()
    index = dates.index(asof)
    if index < 900:
        raise ValueError('Insufficient training history')
    config = configuration()
    handler = config['task']['dataset']['kwargs']['handler']['kwargs']
    train_end = dates[index - 128]
    valid_start, valid_end = dates[index - 125], dates[index - 3]
    train_start = dates[index - 884]
    handler.update(start_time=dates[index - 944] if index >= 944 else dates[0], end_time=asof,
                   fit_start_time=train_start, fit_end_time=train_end)
    config['task']['dataset']['kwargs']['segments'] = {
        'train': [train_start, train_end], 'valid': [valid_start, valid_end], 'test': [asof, asof]}
    predictions = fit_predict(config)
    if predictions.empty:
        raise ValueError('No current predictions')
    scores = predictions.xs(pd.Timestamp(asof), level='datetime').dropna().sort_values(ascending=False)
    # Restrict the first version to ordinary main-board symbols; ST/limit rules remain conservative.
    candidates = [str(code) for code in scores.index if str(code).startswith(('SH600', 'SH601', 'SH603', 'SH605', 'SZ000', 'SZ001', 'SZ002', 'SZ003'))]
    if len(candidates) < 10:
        raise ValueError('Insufficient prediction coverage')
    return {'asof': asof, 'codes': candidates[:10], 'train_end': train_end, 'validation_end': valid_end}
