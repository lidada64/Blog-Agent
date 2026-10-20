"""Durable worker fencing, configuration and conservative maintenance accounting."""
from contextlib import contextmanager
import hashlib
import json
import math
import time
import uuid

from .article_memory_build import PARSER_VERSION


MIGRATION_V2 = (
    "ALTER TABLE nodes ADD COLUMN cache_key TEXT",
    "ALTER TABLE nodes ADD COLUMN coverage_json TEXT NOT NULL DEFAULT '[]'",
    """CREATE TABLE workers (library_id TEXT PRIMARY KEY REFERENCES libraries(library_id),
        token INTEGER NOT NULL DEFAULT 0, owner TEXT, pid INTEGER, heartbeat REAL,
        lease_until REAL NOT NULL DEFAULT 0, paused INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT 'stopped', exit_reason TEXT, log_path TEXT)""",
    """CREATE TABLE summary_cache (library_id TEXT NOT NULL, cache_key TEXT NOT NULL,
        summary TEXT NOT NULL, PRIMARY KEY(library_id, cache_key))""",
    "ALTER TABLE usage ADD COLUMN worker_token INTEGER NOT NULL DEFAULT 0",
)


class LeaseLost(ValueError):
    pass


class BudgetBlocked(ValueError):
    pass


@contextmanager
def transaction(db):
    db.execute('BEGIN IMMEDIATE')
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


def validate_config(config):
    required = {'model', 'base_url', 'prompt_version', 'currency', 'max_requests',
                'max_input_tokens', 'max_output_tokens', 'max_cost_microunits',
                'input_price_per_million', 'output_price_per_million',
                'chunk_chars', 'summary_chars', 'request_output_tokens'}
    if set(config) != required:
        raise ValueError('Maintenance config must contain exactly: ' + ', '.join(sorted(required)))
    for key in ('model', 'base_url', 'prompt_version', 'currency'):
        if not isinstance(config[key], str) or not config[key].strip():
            raise ValueError(f'Invalid {key}')
    if not config['base_url'].startswith(('https://', 'http://localhost:', 'http://127.0.0.1:')):
        raise ValueError('Use HTTPS or a local provider URL')
    for key in required - {'model', 'base_url', 'prompt_version', 'currency'}:
        if type(config[key]) is not int or config[key] <= 0:
            raise ValueError(f'{key} must be a positive integer')
    if not 512 <= config['chunk_chars'] <= 16000 or not 64 <= config['summary_chars'] <= config['chunk_chars'] // 4:
        raise ValueError('Require chunk_chars 512..16000 and summary_chars 64..chunk_chars/4')
    if not 64 <= config['request_output_tokens'] <= 8192:
        raise ValueError('request_output_tokens must be 64..8192')


def generation_version(config):
    keys = ('model', 'base_url', 'prompt_version', 'chunk_chars', 'summary_chars', 'request_output_tokens')
    return hashlib.sha256(json.dumps([PARSER_VERSION, {k: config[k] for k in keys}],
                                    sort_keys=True).encode()).hexdigest()


class MemoryRuntime:
    """Methods mixed into the article store; one connection per thread/process."""

    def configure(self, library_id, config):
        validate_config(config)
        generation = generation_version(config)
        with transaction(self.db):
            library = self._library(library_id)
            previous = json.loads(library['maintenance_json'])
            # Existing accounting must retain its denomination and conservative rates.
            for key in ('currency', 'input_price_per_million', 'output_price_per_million'):
                if previous and previous[key] != config[key]:
                    raise ValueError('Changing accounting denomination/rates requires a new scope')
            self.db.execute('UPDATE libraries SET maintenance_json=?, generation_version=? WHERE library_id=?',
                            (json.dumps(config), generation, library_id))
            if library['generation_version'] != generation:
                self.db.execute("UPDATE documents SET status='stale', active_version=NULL WHERE library_id=? AND status<>'deleted'", (library_id,))
                self.db.execute("UPDATE jobs SET state='superseded', reason='configuration_changed' WHERE library_id=?", (library_id,))
            self.db.execute("UPDATE jobs SET state='pending', reason=NULL, next_run_at=NULL WHERE library_id=? AND generation_version=? AND state IN ('blocked_config','blocked_budget')",
                            (library_id, generation))
            # Reconfiguration fences in-flight generators even if only limits changed.
            self.db.execute("UPDATE workers SET token=token+1, lease_until=0, state='stopped', exit_reason='reconfigured' WHERE library_id=?", (library_id,))

    def worker(self, library_id):
        row = self.db.execute('SELECT * FROM workers WHERE library_id=?', (library_id,)).fetchone()
        data = dict(row) if row else {'token': 0, 'paused': 0, 'state': 'stopped', 'lease_until': 0}
        if not data['paused'] and data['lease_until'] <= time.time() and data['state'] in {'starting', 'running'}:
            data['state'] = 'expired'
        return data

    def claim_worker(self, library_id, owner, ttl=30):
        with transaction(self.db):
            self._library(library_id)
            self.db.execute('INSERT OR IGNORE INTO workers(library_id) VALUES (?)', (library_id,))
            row = self.worker(library_id)
            if row['paused'] or row['lease_until'] > time.time():
                return None
            token = row['token'] + 1
            self.db.execute("UPDATE workers SET token=?, owner=?, pid=NULL, lease_until=?, state='starting', exit_reason=NULL WHERE library_id=?",
                            (token, owner, time.time() + ttl, library_id))
            self.db.execute("UPDATE usage SET state='unknown' WHERE library_id=? AND state='reserved'", (library_id,))
            self.db.execute("UPDATE jobs SET state=CASE WHEN attempts>=3 THEN 'failed' ELSE 'retry_wait' END, next_run_at=?, reason='worker_recovered' WHERE library_id=? AND state='running'",
                            (time.time() + 2, library_id))
            return token

    def require_worker(self, library_id, token):
        worker = self.worker(library_id)
        if worker['paused'] or worker['token'] != token or worker['lease_until'] <= time.time():
            raise LeaseLost('Worker lease expired, paused or replaced')

    def heartbeat(self, library_id, token, pid, ttl=30, log_path=None):
        with transaction(self.db):
            self.require_worker(library_id, token)
            self.db.execute("UPDATE workers SET heartbeat=?, lease_until=?, pid=?, state='running', log_path=COALESCE(?,log_path) WHERE library_id=?",
                            (time.time(), time.time() + ttl, pid, log_path, library_id))

    def pause(self, library_id):
        with transaction(self.db):
            self._library(library_id)
            self.db.execute('INSERT OR IGNORE INTO workers(library_id) VALUES (?)', (library_id,))
            self.db.execute("UPDATE workers SET paused=1, token=token+1, lease_until=0, state='paused', exit_reason='user_pause' WHERE library_id=?", (library_id,))

    def resume_worker(self, library_id):
        self.db.execute("UPDATE workers SET paused=0, state='stopped' WHERE library_id=? AND paused=1", (library_id,))

    def release_worker(self, library_id, token, reason):
        self.db.execute("UPDATE workers SET lease_until=0, state='stopped', exit_reason=? WHERE library_id=? AND token=? AND paused=0",
                        (reason, library_id, token))

    def budget_status(self, library_id):
        config = json.loads(self._library(library_id)['maintenance_json'])
        totals = {'requests': 0, 'input_tokens': 0, 'output_tokens': 0, 'cost_microunits': 0}
        reserved = dict(totals)
        for row in self.db.execute('SELECT * FROM usage WHERE library_id=?', (library_id,)):
            target = totals if row['state'] == 'settled' else reserved
            target['requests'] += row['reserved_requests']
            for field in ('input_tokens', 'output_tokens', 'cost_microunits'):
                actual = row['actual_' + field]
                target[field] += actual if row['state'] == 'settled' and actual is not None else row['reserved_' + field]
        return {'used': totals, 'reserved_or_unknown': reserved,
                'remaining': {k: max(0, config.get('max_' + k, 0) - totals[k] - reserved[k]) for k in totals},
                'currency': config.get('currency'), 'configured': bool(config)}

    def reserve(self, library_id, token, input_upper, output_upper):
        if any(type(value) is not int or value <= 0 for value in (input_upper, output_upper)):
            raise ValueError('Reservation token bounds must be positive integers')
        with transaction(self.db):
            self.require_worker(library_id, token)
            config = json.loads(self._library(library_id)['maintenance_json'])
            if not config:
                raise BudgetBlocked('Missing maintenance configuration')
            cost = math.ceil((input_upper * config['input_price_per_million'] + output_upper * config['output_price_per_million']) / 1_000_000)
            wanted = dict(requests=1, input_tokens=input_upper, output_tokens=output_upper, cost_microunits=cost)
            remaining = self.budget_status(library_id)['remaining']
            if any(wanted[k] > remaining[k] for k in wanted):
                raise BudgetBlocked('Maintenance total budget exhausted')
            request_id = str(uuid.uuid4())
            self.db.execute("""INSERT INTO usage (library_id, request_id, state, reserved_requests,
                reserved_input_tokens, reserved_output_tokens, reserved_cost_microunits, worker_token)
                VALUES (?, ?, 'reserved', 1, ?, ?, ?, ?)""", (library_id, request_id, input_upper, output_upper, cost, token))
            return request_id

    def settle(self, library_id, result):
        # A late result may settle its unique reservation, but never publish a node.
        with transaction(self.db):
            row = self.db.execute('SELECT * FROM usage WHERE library_id=? AND request_id=?', (library_id, result.request_id)).fetchone()
            if not row or row['state'] == 'settled':
                raise ValueError('Unknown or already settled request')
            values = (result.input_tokens, result.output_tokens, result.cost_microunits)
            if any(type(v) is not int or v < 0 for v in values):
                self.db.execute("UPDATE usage SET state='unknown' WHERE library_id=? AND request_id=?", (library_id, result.request_id))
                return False
            exceeded = any(v > row['reserved_' + k] for v, k in zip(values, ('input_tokens', 'output_tokens', 'cost_microunits')))
            self.db.execute("UPDATE usage SET state='settled', actual_input_tokens=?, actual_output_tokens=?, actual_cost_microunits=? WHERE library_id=? AND request_id=?",
                            (*values, library_id, result.request_id))
            if exceeded:
                self.db.execute("UPDATE workers SET paused=1, token=token+1, lease_until=0, state='paused', exit_reason='provider_exceeded_reservation' WHERE library_id=?", (library_id,))
            return not exceeded

    def mark_unknown(self, library_id, request_id):
        self.db.execute("UPDATE usage SET state='unknown' WHERE library_id=? AND request_id=? AND state='reserved'", (library_id, request_id))
