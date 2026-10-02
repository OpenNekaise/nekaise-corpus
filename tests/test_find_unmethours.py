import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import find_unmethours
import lint_registry
import registry


def _today():
    return datetime.now(timezone.utc).date()


def _footer(statement=("User contributions licensed under the <a href=\"http://creativecommons.org/"
                       "licenses/by-sa/3.0/legalcode\">Creative Commons Attribution Share Alike "
                       "3.0 License</a>."), extra=""):
    return ('<html><body><h1>Questions</h1><div id="ground"><div class="footer-links">About</div>'
            '<div class="copyright">Site design and logo: Copyright 2015 Big Ladder Software LLC. '
            f'All rights reserved.<br>{statement}{extra}</div></div></body></html>')


FOOTER = _footer()


def _sitemap(*urls):
    body = "".join(f"<url><loc>{u}</loc><lastmod>2020-01-01</lastmod></url>" for u in urls)
    return ('<?xml version="1.0" encoding="UTF-8"?><urlset '
            f'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>')


SITEMAP = _sitemap(
    "http://unmethours.com/question/12/how-do-you-model-transfer-air-in-energyplus/",
    "http://unmethours.com/question/5/how-to-interpret-cycling-equipment-outputs/",
    "http://unmethours.com/question/30/modelica-solver-efficiency/",
    "http://unmethours.com/questions/",  # not a question page
)


def _setup(monkeypatch, tmp_path, answers, known=None):
    known = known or (set(), set(), set())
    calls, queue = [], iter(answers)

    def get(url, **kwargs):
        calls.append(url)
        item = next(queue)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, SimpleNamespace):
            return item
        return SimpleNamespace(status_code=200, text=item)

    monkeypatch.setattr(find_unmethours.requests, "get", get)
    monkeypatch.setattr(find_unmethours.dedup, "open_keys",
                        lambda: find_unmethours.dedup.from_sets(*known))
    appended = []
    monkeypatch.setattr(find_unmethours.registry, "append_entries", appended.extend)
    files = {n: tmp_path / n for n in ("next", "exhausted", "hold")}
    monkeypatch.setenv("NEKAISE_ROTATION_NEXT_FILE", str(files["next"]))
    monkeypatch.setenv("NEKAISE_BACKEND_EXHAUSTED_FILE", str(files["exhausted"]))
    monkeypatch.setenv("NEKAISE_ROTATION_HOLD_FILE", str(files["hold"]))
    return calls, appended, files


def test_parse_sitemap_sorts_unique_question_pages():
    assert [q for q, _ in find_unmethours.parse_sitemap(SITEMAP)] == [5, 12, 30]


def test_entry_shape_and_licence():
    e = find_unmethours.entry(12, "how-do-you-model-transfer-air-in-energyplus", "2026-10-02")
    assert e["url"] == ("https://unmethours.com/question/12/"
                        "how-do-you-model-transfer-air-in-energyplus/")
    assert e["title"] == "Unmet Hours Q12: how do you model transfer air in energyplus"
    assert e["license"] == "cc-by-sa" and "by-sa/3.0" in e["license_url"]
    assert e["topic"] == "simulation_modeling" in lint_registry.TOPICS
    assert registry.shard_filename(e["id"]) == "unmethours.yaml"
    assert not lint_registry.entry_errors(e, "unmethours.yaml")


def test_walk_from_cursor_skips_known_and_reports_last_walked(monkeypatch, tmp_path):
    known = ({"https://unmethours.com/question/12/how-do-you-model-transfer-air-in-energyplus"},
             set(), set())
    calls, appended, files = _setup(monkeypatch, tmp_path, [SITEMAP, FOOTER], known)
    monkeypatch.setattr(sys, "argv", ["find_unmethours.py", "--cursor", "5", "--max", "1",
                                      "--append"])
    find_unmethours.main()
    assert calls == [find_unmethours.SITEMAP, find_unmethours.INDEX]
    assert [e["url"].split("/")[4] for e in appended] == ["30"]
    assert files["next"].read_text().startswith("watch:")  # 30 is the last question


def test_max_stops_mid_sitemap_with_numeric_cursor(monkeypatch, tmp_path):
    _, appended, files = _setup(monkeypatch, tmp_path, [SITEMAP, FOOTER])
    monkeypatch.setattr(sys, "argv", ["find_unmethours.py", "--cursor", "0", "--max", "2",
                                      "--append"])
    find_unmethours.main()
    assert len(appended) == 2 and files["next"].read_text() == "12\n"


def test_recent_watch_cursor_makes_no_request(monkeypatch, tmp_path):
    calls, _, files = _setup(monkeypatch, tmp_path, [])
    cursor = f"watch:{_today().isoformat()}:30"
    monkeypatch.setattr(sys, "argv", ["find_unmethours.py", "--cursor", cursor])
    find_unmethours.main()
    assert calls == [] and files["next"].read_text() == cursor + "\n"


def test_old_watch_cursor_reads_the_sitemap_again(monkeypatch, tmp_path):
    old = (_today() - timedelta(days=find_unmethours.WATCH_DAYS + 1)).isoformat()
    sitemap = _sitemap("http://unmethours.com/question/31/new-question/")
    _, appended, files = _setup(monkeypatch, tmp_path, [sitemap, FOOTER])
    monkeypatch.setattr(sys, "argv", ["find_unmethours.py", "--cursor", f"watch:{old}:30",
                                      "--append"])
    find_unmethours.main()
    assert [e["url"].split("/")[4] for e in appended] == ["31"]
    assert files["next"].read_text().startswith(f"watch:{_today().isoformat()}:31")


@pytest.mark.parametrize("answers", [
    [SimpleNamespace(status_code=503, text="")],
    ["<html>maintenance</html>"],
    [_sitemap("http://unmethours.com/questions/")],
    [SITEMAP, "<html>footer without a licence</html>"],
    [SITEMAP, _footer(statement="<!-- User contributions licensed under the Creative Commons "
                                "Attribution Share Alike 3.0 License -->")],
    [SITEMAP, "<html><h2>User contributions licensed under the Creative Commons Attribution "
              "Share Alike 3.0 License</h2></html>"],  # question text, not the footer
    [SITEMAP, _footer(statement="User contributions licensed under the <a href=\"https://"
                                "creativecommons.org/licenses/by-nc-sa/3.0/\">Creative Commons "
                                "Attribution Share Alike 3.0 License</a>")],
    [SITEMAP, _footer(extra='<a href="https://creativecommons.org/licenses/by-nc/4.0/">x</a>')],
    [SITEMAP, _footer(statement="User contributions licensed under the Creative Commons "
                                "Attribution Share Alike 3.0 License")],  # no licence link
])
def test_unexpected_answers_hold_and_propose_nothing(monkeypatch, tmp_path, answers):
    _, appended, files = _setup(monkeypatch, tmp_path, answers)
    monkeypatch.setattr(sys, "argv", ["find_unmethours.py", "--append"])
    find_unmethours.main()
    assert appended == [] and files["hold"].read_text().strip()
    assert not files["next"].exists()


def test_network_failure_exits_nonzero(monkeypatch, tmp_path):
    _, appended, files = _setup(monkeypatch, tmp_path, [TimeoutError("offline")])
    monkeypatch.setattr(sys, "argv", ["find_unmethours.py", "--append"])
    with pytest.raises(SystemExit) as exc:
        find_unmethours.main()
    assert exc.value.code == 1 and appended == [] and not files["next"].exists()


@pytest.mark.parametrize("cursor", ["", "x", "watch:2026-10-02", "watch:20261002:5", "-1"])
def test_malformed_cursor_is_rejected(cursor):
    with pytest.raises(ValueError):
        find_unmethours.parse_cursor(cursor)
