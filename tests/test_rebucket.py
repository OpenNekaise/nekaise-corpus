"""Physical shard migration must preserve rows and roll back partial replacement."""
import json

import pytest

import state_codec as codec
import store
from test_store_contract import entry, mrow


def legacy(tmp_path):
    st = store.FileStore(tmp_path)
    st.reg.mkdir()
    st.man.mkdir()
    entries = [entry('ojs-a', title='熱伝達 — ΔT'), entry('ojs-b')]
    rows = [mrow(e['id'], note='A\u2028B') for e in entries]
    (st.reg / 'ojs.yaml').write_text(codec.shard_header('ojs') + ''.join(
        codec.emit_entry(e) for e in entries))
    (st.man / 'ojs.jsonl').write_text(codec.manifest_shard_text(rows))
    return st, entries, rows


def migrate(st, writer):
    return st.rebucket_family('ojs', run_id='rebucket-test',
                              expected_version=st.version(), writer=writer)


def test_ojs_routing_is_fixed_and_scoped():
    assert codec.manifest_shard('ojs-a') == 'ojs-6'
    assert codec.shard_filename('ojs-b') == 'ojs-4.yaml'
    assert codec.manifest_shard('ost-a') == 'reports'
    assert codec.manifest_shard('hand-one') == 'curated'


def test_migration_preserves_store_rows_and_rejects_legacy_duplicates(tmp_path):
    st, entries, rows = legacy(tmp_path)
    with st.writer() as w:
        assert migrate(st, w) == {'entries': 2, 'manifest': 2}
        with st.read(writer=w) as v:
            assert v.scan(store.Table.ENTRIES).rows == entries
            assert v.scan(store.Table.MANIFEST).rows == rows
        assert st.validate_layout()[0] == []
        assert not (st.man / 'ojs.jsonl').exists()
        (st.man / 'ojs.jsonl').write_text(codec.manifest_shard_text(rows))
        errors, _ = st.validate_layout()
        assert any('prefix routing' in e for e in errors)
        assert any('duplicate manifest id' in e for e in errors)
        with pytest.raises(store.StoreError, match='missing legacy|coexist'):
            migrate(st, w)


@pytest.mark.parametrize('bad', [{'title': 'missing'}, {'id': ''},
                                {'id': 'hand-one'}, {'id': 'ojs-a'}])
def test_bad_rows_fail_before_any_write(tmp_path, bad):
    st, _, rows = legacy(tmp_path)
    path = st.man / 'ojs.jsonl'
    path.write_text(path.read_text() + json.dumps(bad) + '\n')
    before = path.read_bytes()
    with st.writer() as w, pytest.raises(store.StoreError):
        migrate(st, w)
    assert path.read_bytes() == before
    assert not list(st.man.glob('ojs-*.jsonl'))


def test_partial_apply_rolls_back_both_tables(tmp_path, monkeypatch):
    st, _, _ = legacy(tmp_path)
    before = {p: p.read_bytes() for d in (st.reg, st.man) for p in d.iterdir()}
    real = store._write_durable
    failed = False

    def fail_once(path, data):
        nonlocal failed
        if path == st.reg / 'ojs-6.yaml' and not failed:
            failed = True
            raise OSError('injected disk failure')
        return real(path, data)

    monkeypatch.setattr(store, '_write_durable', fail_once)
    with st.writer() as w, pytest.raises(OSError, match='injected'):
        migrate(st, w)
    assert failed
    assert {p: p.read_bytes() for d in (st.reg, st.man) for p in d.iterdir()} == before
    assert st.pending_transactions() == []


def test_migration_requires_lock_and_fresh_version(tmp_path):
    st, _, _ = legacy(tmp_path)
    with pytest.raises(store.WriterError):
        st.rebucket_family('ojs', run_id='no-lock', expected_version=st.version())
    old = st.version()
    (st.man / 'ojs.jsonl').write_text((st.man / 'ojs.jsonl').read_text() + '\n')
    with st.writer() as w, pytest.raises(store.StaleView):
        st.rebucket_family('ojs', run_id='stale', expected_version=old, writer=w)
