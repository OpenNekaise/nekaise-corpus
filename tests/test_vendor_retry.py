"""Connection outages must not lose pages or pin every vendor behind one failed host."""
import json
import sys

import pytest
import requests

import find_vendor
import run_round
import vendor_retry
from test_find_vendor import vendor
from test_run_round import fixture_store


def scanner(tmp_path, monkeypatch):
    monkeypatch.setattr(find_vendor, 'CACHE_DIR', tmp_path / 'cache')
    return vendor(mechanism='sitemap_pages', page_pattern='/product/', pages_per_run=20)


def test_outage_is_bounded_persisted_and_recovers_after_cache_loss(tmp_path, monkeypatch):
    cfg = scanner(tmp_path, monkeypatch)
    pages = [f'https://a.example/product/{n}' for n in range(30)]
    clock = [1800000000.0]
    calls = []

    def down(url, *a):
        calls.append(url)
        clock[0] += vendor_retry.CONNECT_TIMEOUT + 1
        raise requests.ConnectTimeout('offline')

    retry = vendor_retry.Connections({}, down, lambda: clock[0])
    assert find_vendor.scan_pages('a', cfg, pages, 20, retry) == []
    assert len(calls) == 3
    assert clock[0] - 1800000000 < 240
    assert retry.hosts['a.example']['pages'] == pages
    assert find_vendor.load_state('a', 'visited') == {}
    report = {'cursor': 0, 'vendor': 'a', 'hosts': retry.hosts}
    entry = vendor_retry.apply_report({'flag': '--cursor', 'next': 0}, report)
    assert entry['next'] == 1  # the next enabled vendor receives its normal turn
    assert entry['vendor_retry']['a'] == retry.hosts

    cooldown = vendor_retry.Connections(entry['vendor_retry']['a'], down, lambda: clock[0])
    find_vendor.scan_pages('a', cfg, [], 20, cooldown)  # no universe/cache required
    assert len(calls) == 3  # no probes before the due time
    assert cooldown.hosts == retry.hosts
    clock[0] = retry.hosts['a.example']['next_retry_at']
    probe = vendor_retry.Connections(cooldown.hosts, down, lambda: clock[0])
    find_vendor.scan_pages('a', cfg, [], 20, probe)
    assert len(calls) == 4  # exactly one due probe
    state = probe.hosts['a.example']
    assert state['failures'] == 2
    assert state['next_retry_at'] - clock[0] == 2 * vendor_retry.INITIAL_BACKOFF
    clock[0] = state['next_retry_at']

    def up(url, *a):
        calls.append(url)
        return b'<a href="https://a.example/hvac-manual.pdf">HVAC manual</a>'

    recovered = vendor_retry.Connections(probe.hosts, up, lambda: clock[0])
    assert find_vendor.scan_pages('a', cfg, [], 20, recovered) == ['https://a.example/hvac-manual.pdf']
    assert recovered.hosts['a.example']['pages'] == pages[20:]
    recovered = vendor_retry.Connections(recovered.hosts, up, lambda: clock[0])
    find_vendor.scan_pages('a', cfg, [], 20, recovered)
    assert recovered.hosts == {}
    assert set(find_vendor.load_state('a', 'visited')) == set(pages)
    assert 'vendor_retry' not in vendor_retry.apply_report(entry, {
        'cursor': 1, 'vendor': 'a', 'hosts': {}})


def test_breaker_is_host_scoped_and_success_resets_consecutive_failures(tmp_path, monkeypatch):
    cfg = scanner(tmp_path, monkeypatch)
    calls = []
    pages = [f'https://a.example/product/{n}' for n in range(8)]
    pages.insert(4, 'https://b.example/product/ok')

    def fetch(url, *args):
        calls.append(url)
        if 'a.example' in url:
            raise requests.ConnectTimeout('down')
        return b'<a href="https://b.example/manual.pdf">HVAC</a>'

    retry = vendor_retry.Connections({}, fetch)
    docs = find_vendor.scan_pages('a', cfg, pages, 20, retry)
    assert len(calls) == 4 and docs == ['https://b.example/manual.pdf']
    assert set(retry.hosts) == {'a.example'}
    assert len(retry.hosts['a.example']['pages']) == 8
    assert vendor_retry.summaries({'hosts': retry.hosts})[0]['deferred_pages'] == 8

    outcomes = iter([False, False, True, False, False])
    def intermittent(*args):
        if not next(outcomes):
            raise requests.ConnectTimeout()
        return b'ok'
    retry = vendor_retry.Connections({}, intermittent)
    for _ in range(5):
        try:
            retry('https://a.example/')
        except requests.ConnectTimeout:
            pass
    assert retry.hosts == {}


@pytest.mark.parametrize('exc', [requests.ReadTimeout(), requests.ConnectionError(),
                                 requests.HTTPError()])
def test_nonconnect_failures_do_not_trip_or_reclassify(tmp_path, monkeypatch, exc):
    cfg = scanner(tmp_path, monkeypatch)
    def bad(*args):
        raise exc
    retry = vendor_retry.Connections({}, bad)
    with pytest.raises(RuntimeError, match='all 4 page fetches failed'):
        find_vendor.scan_pages('a', cfg, [f'https://a.example/product/{n}' for n in range(4)], 20, retry)
    assert retry.hosts == {}
    assert find_vendor.load_state('a', 'visited') == {}


def test_connect_timeout_preserves_slow_read_budgets(monkeypatch):
    calls = []
    class Response:
        content = b'normal catalogue'
        def raise_for_status(self):
            pass
    def request(url, **kw):
        calls.append(kw)
        # A simulated 45-second read is valid under both unchanged read budgets.
        assert kw['timeout'][0] < 45 < kw['timeout'][1]
        assert kw['headers'] == find_vendor.UA
        return Response()
    monkeypatch.setattr(find_vendor.requests, 'get', request)
    monkeypatch.setattr(find_vendor.requests, 'post', request)
    assert find_vendor.fetch('https://a.example/') == b'normal catalogue'
    find_vendor.fetch('https://a.example/', method='POST')
    assert [x['timeout'] for x in calls] == [(15, 60), (15, 90)]


def test_retry_state_and_rotation_are_one_store_transaction(tmp_path, monkeypatch):
    backends = {'find_vendor': {'script': 'find_vendor.py'}}
    state = {'find_vendor': {'flag': '--cursor', 'next': 0}}
    st = fixture_store(tmp_path, backends, state)
    retry = vendor_retry.Connections({}, lambda *a: None, lambda: 1800000000)
    retry.defer('a.example')
    retry.hosts['a.example']['pages'] = ['https://a.example/product/1']
    path = tmp_path / 'retry-output'
    path.write_text(json.dumps({'cursor': 0, 'vendor': 'a', 'hosts': retry.hosts}))
    proposal = tmp_path / 'proposal'
    proposal.write_text('[]')
    result = {'name': 'find_vendor', 'index': 0, 'proposal': proposal,
              'rotation_hold': False, 'backend_exhausted': tmp_path / 'absent',
              'vendor_retry': path}
    with st.writer() as writer:
        with pytest.raises(RuntimeError, match='abort'):
            with st.transaction('vendor-abort', expected_version=st.version(), writer=writer) as tx:
                run_round.apply_discovery(tx, [result], ['find_vendor'], backends)
                raise RuntimeError('abort')
        with st.read(writer=writer) as view:
            assert view.rotation_get('find_vendor') == state['find_vendor']
        with st.transaction('vendor-retry', expected_version=st.version(), writer=writer) as tx:
            _, _, notes = run_round.apply_discovery(tx, [result], ['find_vendor'], backends)
        with st.read(writer=writer) as view:
            entry = view.rotation_get('find_vendor')
            assert entry['next'] == 1
            assert entry['vendor_retry']['a'] == retry.hosts
    assert any(event == 'discovery_degraded' and fields['failures'].get('find_vendor')
               for _, event, fields in notes)
    with pytest.raises(ValueError, match='stale'):
        vendor_retry.apply_report(entry, json.loads(path.read_text()))


def test_aged_backoff_is_visible_and_bounded():
    clock = [1800000000.0]
    retry = vendor_retry.Connections({}, lambda *a: None, lambda: clock[0])
    for _ in range(30):
        retry.defer('a.example')
        assert retry.hosts['a.example']['next_retry_at'] - clock[0] <= 86400
        clock[0] = retry.hosts['a.example']['next_retry_at']
    summary = vendor_retry.summaries({'hosts': retry.hosts}, now=clock[0])
    assert 'operator attention' in summary[0]['reason']


def test_finder_round_trip_gives_next_vendor_a_turn(tmp_path, monkeypatch):
    cfg = scanner(tmp_path, monkeypatch)
    configs = {'a': cfg, 'b': vendor(name='Bee')}
    monkeypatch.setattr(find_vendor, 'load_vendors', lambda: configs)
    monkeypatch.setattr(find_vendor.dedup, 'open_keys',
                        lambda: find_vendor.dedup.from_sets(set(), set(), set()))
    pages = [f'https://a.example/product/{n}' for n in range(20)]
    find_vendor.store_universe('a', pages)
    find_vendor.store_universe('b', ['https://b.example/hvac-manual.pdf'])
    retry_input, retry_output = tmp_path / 'input', tmp_path / 'output'
    proposal = tmp_path / 'proposal'
    monkeypatch.setenv(vendor_retry.INPUT_ENV, str(retry_input))
    monkeypatch.setenv(vendor_retry.OUTPUT_ENV, str(retry_output))
    monkeypatch.setenv('NEKAISE_PROPOSAL_FILE', str(proposal))
    calls = []
    def down(url, *a):
        calls.append(url)
        raise requests.ConnectTimeout('down')
    monkeypatch.setattr(find_vendor, 'fetch', down)
    entry = {'flag': '--cursor', 'next': 0}
    retry_input.write_text(json.dumps(entry))
    monkeypatch.setattr(sys, 'argv', ['find_vendor.py', '--cursor', '0', '--append'])
    find_vendor.main()
    assert len(calls) == 3
    entry = vendor_retry.apply_report(entry, json.loads(retry_output.read_text()))
    assert entry['vendor_retry']['a']['a.example']['pages'] == pages
    retry_input.write_text(json.dumps(entry))
    monkeypatch.setattr(sys, 'argv', ['find_vendor.py', '--cursor', '1', '--append'])
    find_vendor.main()
    entry = vendor_retry.apply_report(entry, json.loads(retry_output.read_text()))
    assert entry['next'] == 2
    assert json.loads(proposal.read_text())[0]['source'] == 'vendor_acme'
    assert json.loads(proposal.read_text())[0]['id'].startswith('vnd-b-')
    assert 'a' in entry['vendor_retry']  # another vendor cannot erase its pending pages


@pytest.mark.parametrize('hosts', [[], {'a.example': {}},
    {'a.example': {'failures': 1, 'first_failure_at': 1, 'next_retry_at': float('nan'), 'pages': []}},
    {'a.example': {'failures': 1, 'first_failure_at': 1, 'next_retry_at': 2,
                   'pages': ['https://wrong.example/product/1']}}])
def test_malformed_retry_state_fails_control_plane_contracts(hosts):
    import rotation
    entry = {'flag': '--cursor', 'next': 1, 'vendor_retry': {'a': hosts}}
    assert rotation.validate_entry('find_vendor', entry)


def test_no_enabled_vendor_holds_without_erasing_retry_state(tmp_path, monkeypatch):
    monkeypatch.setattr(find_vendor, 'load_vendors', lambda: {'a': vendor(enabled=False)})
    hold = tmp_path / 'hold'
    monkeypatch.setenv('NEKAISE_ROTATION_HOLD_FILE', str(hold))
    monkeypatch.setattr(sys, 'argv', ['find_vendor.py', '--cursor', '0', '--append'])
    find_vendor.main()
    assert 'retry state preserved' in hold.read_text()


@pytest.mark.parametrize('mode', ['success', 'failed', 'missing', 'stale'])
def test_round_subprocess_retry_protocol_is_fail_closed(tmp_path, monkeypatch, mode):
    import os
    from test_run_round import finder_env, discovery_transaction

    events = finder_env(tmp_path, monkeypatch)
    monkeypatch.setattr(run_round, 'SCRIPTS', tmp_path)
    script = tmp_path / 'fake_vendor.py'
    script.write_text('''import os, json, sys
from pathlib import Path
state = json.loads(Path(os.environ['NEKAISE_VENDOR_RETRY_INPUT']).read_text())
report = {'cursor': state['next'], 'vendor': 'a', 'hosts': {'a.example': {
    'failures': 1, 'first_failure_at': 1800000000, 'next_retry_at': 1800003600,
    'pages': ['https://a.example/product/1']}}}
mode = sys.argv[1]
if mode == 'stale': report['cursor'] -= 1
if mode != 'missing':
    Path(os.environ['NEKAISE_VENDOR_RETRY_OUTPUT']).write_text(json.dumps(report))
Path(os.environ['NEKAISE_PROPOSAL_FILE']).write_text('[]')
sys.exit(1 if mode == 'failed' else 0)
''')
    backends = {'find_vendor': {'script': script.name, 'args': [mode], 'required': False}}
    state = {'find_vendor': {'flag': '--cursor', 'next': 0}}
    st = fixture_store(tmp_path, backends, state)
    with discovery_transaction(st) as transaction:
        def run():
            run_round.run_finders_parallel(['find_vendor'], backends, state,
                                          os.environ.copy(), 'fixture-run', 1, transaction)
        if mode in ('missing', 'stale'):
            with pytest.raises((FileNotFoundError, ValueError)):
                run()
        else:
            run()
    with st.read() as view:
        entry = view.rotation_get('find_vendor')
    if mode == 'success':
        assert entry['next'] == 1
        assert entry['vendor_retry']['a']['a.example']['pages'] == ['https://a.example/product/1']
        degraded = [fields for _, event, fields in events if event == 'discovery_degraded']
        assert degraded[0]['hosts'][0]['deferred_pages'] == 1
    else:
        assert entry == state['find_vendor']  # even a written report is ignored on finder failure
