import json

import ops
import prune_corpus
import pytest
import registry
from runids import rid


def test_prune_ledger_records_reason_and_blocklist_decision(tmp_path, monkeypatch):
    rows = [{
        "id": "ost-bad",
        "url": "https://example.org/bad.pdf",
        "title": "Bad extraction",
        "source": "osti",
        "topic": "construction",
        "license": "public-domain",
        "status": "ok",
        "quality": {"chars": 3},
    }]
    monkeypatch.setenv("NEKAISE_RUN_ID", rid("run-7"))

    got = prune_corpus.prune_ledger_rows(
        rows, {"ost-bad": "thin"}, {"https://example.org/bad.pdf"}, now="2026-09-24T00:00:00Z")

    assert len(got) == 1
    assert got[0]["id"] == "ost-bad" and got[0]["reason"] == "thin"
    assert got[0]["blocklisted"] is True and got[0]["run_id"] == rid("run-7")
    assert set(got[0]) <= set(__import__("store").LEDGER_FIELDS)


def test_prune_ledger_rows_append_the_legacy_bytes(tmp_path, monkeypatch):
    """The store appends ledger rows to registry/pruned-<bucket>.jsonl exactly as the legacy
    ops.append_jsonl writer did."""
    import legacy_pipeline
    import store
    rows = [{"id": f"ost-{n}", "url": f"https://example.org/{n}.pdf", "title": f"T{n}",
             "status": "failed", "error": "404"} for n in range(40)]
    drop = {r["id"]: "failed" for r in rows}
    monkeypatch.setattr(prune_corpus.time, "strftime", lambda *_a: "2026-09-24T00:00:00Z")
    legacy, new = tmp_path / "legacy", tmp_path / "new"
    for root in (legacy, new):
        (root / "registry").mkdir(parents=True)
    monkeypatch.setattr(registry, "ROOT", legacy)  # tests/legacy_registry.py follows it
    monkeypatch.setattr(ops, "WORKSPACE", legacy / "workspace")
    legacy_pipeline.legacy_write_prune_ledger(rows, drop, {"https://example.org/3.pdf"})
    st = store.FileStore(new)
    ledger = prune_corpus.prune_ledger_rows(rows, drop, {"https://example.org/3.pdf"})
    with st.writer() as w:
        with st.transaction("t", expected_version=st.version(), writer=w) as tx:
            tx.ledger_append(ledger)
    names = sorted(p.name for p in (legacy / "registry").glob("pruned-*.jsonl"))
    assert len(names) > 1
    assert names == sorted(p.name for p in (new / "registry").glob("pruned-*.jsonl"))
    for name in names:
        assert (legacy / "registry" / name).read_bytes() == (new / "registry" / name).read_bytes()


def test_legacy_prune_ledger_migrates_without_losing_or_duplicating_rows(
    tmp_path, monkeypatch,
):
    """The retired migration (tests/legacy_registry.py) and the store: the store reads the legacy
    monolith and the sharded layout as the same ledger."""
    import legacy_registry
    import store

    reg = tmp_path / "registry"
    reg.mkdir()
    monkeypatch.setattr(registry, "ROOT", tmp_path)
    rows = [
        {"id": f"vnd-test-{i}", "url": f"https://example.org/{i}.pdf", "reason": "thin"}
        for i in range(64)
    ]
    legacy = reg / "pruned.jsonl"
    legacy.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def ledger():
        with store.FileStore(tmp_path).read() as view:
            return sorted(view.scan(store.Table.LEDGER, limit=store.MAX_PAGE).rows,
                          key=lambda row: row["id"])

    assert ledger() == sorted(rows, key=lambda row: row["id"])  # the monolith, as migration input
    counts = legacy_registry.write_prune_ledger_rows(legacy_registry.load_prune_ledger_rows())

    assert not legacy.exists()
    assert len(counts) > 1
    assert sum(counts.values()) == len(rows)
    assert ledger() == sorted(rows, key=lambda row: row["id"])
    for path in legacy_registry.prune_ledger_files():
        assert all(
            legacy_registry.prune_ledger_path(row["id"]) == path
            for row in map(json.loads, path.read_text().splitlines())
        )


def test_reviewed_title_drops_fail_closed_on_stale_or_curated_ids(tmp_path):
    rows = [
        {"id": "guk-off-topic"},
        {"id": "hand-curated"},
    ]
    reviewed = tmp_path / "reviewed.txt"
    reviewed.write_text("guk-off-topic\n")

    assert prune_corpus.reviewed_title_drops(str(reviewed), rows) == {
        "guk-off-topic": "off-topic-title",
    }

    reviewed.write_text("missing-id\n")
    with pytest.raises(ValueError, match="unknown ids"):
        prune_corpus.reviewed_title_drops(str(reviewed), rows)

    reviewed.write_text("hand-curated\n")
    with pytest.raises(ValueError, match="hand-curated ids"):
        prune_corpus.reviewed_title_drops(str(reviewed), rows)


@pytest.mark.parametrize(
    ("row", "reason", "expected"),
    [
        ({"url": "https://www.mdpi.com/article.pdf", "http_status": 403}, "failed", False),
        ({"url": "https://files.mdpi.com/article.pdf", "http_status": 403}, "failed", False),
        ({"url": "https://example.org/forbidden.pdf", "http_status": 403}, "failed", True),
        ({"url": "https://example.org/busy.pdf", "http_status": 429}, "failed", False),
        ({"url": "https://example.org/slow.pdf", "error": "connection timed out"}, "failed", False),
        ({"url": "https://example.org/old.pdf", "error": "SSL certificate mismatch"}, "failed", True),
        ({"url": "https://www.mdpi.com/thin.pdf", "http_status": 403}, "thin", True),
    ],
)
def test_blocklist_policy_distinguishes_mdpi_wall_from_durable_failures(
    row, reason, expected,
):
    assert prune_corpus._blocklistable(row, reason) is expected


def test_repeated_dns_failures_become_blocklistable_after_three_runs_and_days():
    url = (
        "https://cdn01.rockwoolgroup.com/siteassets/reference-cases/"
        "j2312_rockwool_opco_case_study_booklet_v8.1_renovation.pdf"
        "?f=20200619063212&dl=1"
    )
    error = (
        "HTTPSConnectionPool(host='cdn01.rockwoolgroup.com', port=443): "
        "Max retries exceeded (Caused by NameResolutionError(\"Failed to resolve "
        "'cdn01.rockwoolgroup.com' ([Errno -2] Name or service not known)\"))"
    )
    ledger = [
        {
            "url": url,
            "reason": "failed",
            "error": error,
            "run_id": f"run-{day}",
            "pruned_at": f"2026-09-0{day}T12:00:00Z",
        }
        for day in range(1, 4)
    ]

    repeated = prune_corpus.repeated_dns_failure_urls(ledger)

    assert repeated == {url}
    assert prune_corpus._blocklistable(
        {"url": url, "error": error}, "failed", repeated,
    ) is True


@pytest.mark.parametrize(
    "ledger",
    [
        [
            {
                "url": "https://outage.example/report.pdf",
                "reason": "failed",
                "error": "Temporary failure in name resolution",
                "run_id": "same-run",
                "pruned_at": f"2026-09-0{day}T12:00:00Z",
            }
            for day in range(1, 4)
        ],
        [
            {
                "url": "https://outage.example/report.pdf",
                "reason": "failed",
                "error": "getaddrinfo failed",
                "run_id": f"run-{run}",
                "pruned_at": "2026-09-01T12:00:00Z",
            }
            for run in range(3)
        ],
    ],
)
def test_dns_outage_in_one_run_or_day_remains_retryable(ledger):
    row = {
        "url": "https://outage.example/report.pdf",
        "error": "NameResolutionError: failed to resolve outage.example",
    }

    repeated = prune_corpus.repeated_dns_failure_urls(ledger)

    assert repeated == set()
    assert prune_corpus._blocklistable(row, "failed", repeated) is False


@pytest.mark.parametrize(
    "row",
    [
        {"url": "https://example.org/pending.pdf", "http_status": 202},
        {"url": "https://example.org/unavailable.pdf", "http_status": 503},
        {"url": "https://example.org/reset.pdf", "error": "Connection reset by peer"},
    ],
)
def test_other_transient_fetch_failures_remain_retryable(row):
    assert prune_corpus._blocklistable(row, "failed") is False
