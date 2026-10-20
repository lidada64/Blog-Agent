"""Explicit project usage report; missing usage/prices stay unknown, never zero."""
from decimal import Decimal
import json
from pathlib import Path

from agent_core.costs import price_usage


def project_usage(store, foreground_paths=(), prices=None):
    background = {row[0]: store.budget_status(row[0]) for row in store.db.execute('SELECT library_id FROM libraries')}
    foreground, seen = [], set()
    for path in foreground_paths:
        path = Path(path).resolve(strict=True)
        if path in seen:
            continue
        seen.add(path)
        state = json.loads(path.read_text(encoding='utf-8'))
        records = state.get('model_requests', [])
        missing = sum(any(type(r.get(k)) is not int for k in ('input_tokens','output_tokens')) for r in records)
        costs = []
        for record in records:
            try:
                costs.append(price_usage(record, prices, record['model']))
            except (ValueError, KeyError, TypeError):
                costs.append(None)
        foreground.append({'path':str(path), 'requests':len(records), 'missing_usage_requests':missing,
                           'input_tokens_observed':sum(r.get('input_tokens') or 0 for r in records),
                           'output_tokens_observed':sum(r.get('output_tokens') or 0 for r in records),
                           'cost':str(sum((Decimal(c['total']) for c in costs),Decimal(0)))
                           if all(c is not None and c['exact'] for c in costs) else None})
    currencies = {b['currency'] for b in background.values() if b['configured']}
    if prices and foreground:
        currencies.add(prices['currency'])
    known_background = sum(b['used']['cost_microunits'] for b in background.values())
    reserved = sum(b['reserved_or_unknown']['cost_microunits'] for b in background.values())
    complete = len(currencies)<=1 and reserved==0 and all(f['cost'] is not None for f in foreground)
    return {'scope':'all libraries in this database plus explicitly selected foreground checkpoints',
            'background':background,'foreground':foreground,
            'requests':sum(b['used']['requests']+b['reserved_or_unknown']['requests'] for b in background.values()) + sum(f['requests'] for f in foreground),
            'input_tokens_observed':sum(b['used']['input_tokens'] for b in background.values())+sum(f['input_tokens_observed'] for f in foreground),
            'output_tokens_observed':sum(b['used']['output_tokens'] for b in background.values())+sum(f['output_tokens_observed'] for f in foreground),
            'currency':next(iter(currencies)) if len(currencies)==1 else None,
            'total_cost':str(Decimal(known_background)/1_000_000+sum((Decimal(f['cost']) for f in foreground),Decimal(0))) if complete else None,
            'cost_complete':complete, 'background_reserved_or_unknown_microunits':reserved,
            'prices_are_configured_estimates_not_provider_invoices':True}
