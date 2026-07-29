import json

import find_books


def test_oapen_license_cache_ignores_transient_negative_entries(tmp_path, monkeypatch):
    cache = tmp_path / "licenses.json"
    cache.write_text(json.dumps({"good": "cc-by", "transient-or-negative": None}))
    monkeypatch.setattr(find_books, "LICENSE_CACHE", cache)

    assert find_books.load_license_cache() == {"good": "cc-by"}
