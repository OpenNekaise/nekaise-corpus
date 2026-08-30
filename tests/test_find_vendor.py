"""find_vendor: config validation, sitemap/listing enumeration, URL filters, entry shaping, cursor."""
import json
import sys

import pytest

import find_vendor
import registry


def vendor(**over):
    cfg = {
        "name": "Acme HVAC", "source": "vendor_acme", "mechanism": "sitemap",
        "sitemaps": ["https://acme.example/sitemap.xml"], "topic": "equipment_systems",
        "rights": {"tos_url": "https://acme.example/terms", "tos_excerpt": "free to download",
                   "robots": "no relevant Disallow", "reviewed_at": "2026-08-28", "decision": "go"},
    }
    cfg.update(over)
    return cfg


def test_validate_vendors_rejects_bad_configs():
    errors = find_vendor.validate_vendors({"vendors": {
        "Bad Key": vendor(),
        "nomech": vendor(mechanism="ftp"),
        "nourls": vendor(sitemaps=[]),
        "badtopic": vendor(topic="cooking"),
        "badre": vendor(pdf_pattern="("),
        "nogo-on": vendor(rights={"tos_url": "https://x", "reviewed_at": "2026-08-28",
                                  "decision": "no-go"}, enabled=True),
        "ok": vendor(),
    }})
    text = "\n".join(errors)
    assert "Bad Key" in text and "mechanism" in text and "sitemaps" in text
    assert "unknown topic" in text and "does not compile" in text
    assert "no-go but vendor is enabled" in text
    assert "vendors.ok" not in text


def test_validate_vendors_accepts_empty_config():
    assert find_vendor.validate_vendors({"vendors": {}}) == []


def test_sitemap_index_and_urlset_are_walked_with_filter():
    pages = {
        "https://acme.example/sitemap.xml": b"""<?xml version="1.0"?>
            <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
              <sitemap><loc>https://acme.example/sitemap-docs.xml</loc></sitemap>
              <sitemap><loc>https://acme.example/sitemap-news.xml</loc></sitemap>
            </sitemapindex>""",
        "https://acme.example/sitemap-docs.xml": b"""<?xml version="1.0"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
              <url><loc>https://acme.example/docs/chiller-30xa-product-data.pdf</loc></url>
              <url><loc>https://acme.example/docs/page.html</loc></url>
            </urlset>""",
    }
    fetched = []

    def fetcher(url, delay=0.0):
        fetched.append(url)
        return pages[url]

    cfg = vendor(sitemap_filter=r"docs")
    urls = find_vendor.enumerate_sitemap(cfg, fetcher)
    assert urls == ["https://acme.example/docs/chiller-30xa-product-data.pdf",
                    "https://acme.example/docs/page.html"]
    assert "https://acme.example/sitemap-news.xml" not in fetched  # filtered child never fetched


def test_html_index_links_are_absolutized_and_unescaped():
    page = b'<a href="/lit/a%20b.pdf?x=1&amp;y=2">A</a><a href="https://cdn.example/c.PDF">C</a>'
    cfg = vendor(mechanism="html_index", index_urls=["https://acme.example/literature/"])
    urls = find_vendor.enumerate_html_index(cfg, lambda url, *a, **k: page)
    assert urls == ["https://acme.example/lit/a%20b.pdf?x=1&y=2", "https://cdn.example/c.PDF"]


def test_select_documents_applies_patterns_and_dedups():
    cfg = vendor(exclude_pattern=r"/sds/|safety-data", lang_pattern=r"/en/")
    universe = [
        "https://acme.example/en/docs/x.pdf", "https://acme.example/en/docs/x.pdf",
        "https://acme.example/de/docs/x.pdf", "https://acme.example/en/sds/x.pdf",
        "https://acme.example/en/docs/y.PDF?dl=1", "https://acme.example/en/docs/page.html",
    ]
    assert find_vendor.select_documents(cfg, universe) == [
        "https://acme.example/en/docs/x.pdf", "https://acme.example/en/docs/y.PDF?dl=1",
    ]


def test_entries_are_shaped_titled_topiced_and_deduped():
    cfg = vendor(topic_rules=[[r"desigo", "controls_bas"]])
    docs = [
        "https://acme.example/lit/chillers/30xa-product-data.pdf",
        "https://acme.example/lit/controls/desigo-cc-datasheet.pdf",
        "https://acme.example/lit/insulation/glass-wool-tds.pdf",
        "https://acme.example/lit/chillers/30xa-product-data.pdf",   # dup url
    ]
    known_urls = {"https://acme.example/lit/insulation/glass-wool-tds.pdf"}
    out = find_vendor.entries_for("acme", cfg, docs, known_urls, set(), cap=10)
    assert [e["id"] for e in out] == ["vnd-acme-30xa-product-data", "vnd-acme-desigo-cc-datasheet"]
    assert out[0]["title"] == "Acme HVAC: chillers — 30xa product data"
    labeled = find_vendor.entries_for(
        "acme", cfg, ["https://acme.example/dms/3cbc9de3-03ce-344b-8649-7a74153fb818/Sika%20PDS.pdf"],
        set(), set(), cap=5, labels={"https://acme.example/dms/3cbc9de3-03ce-344b-8649-7a74153fb818/Sika%20PDS.pdf": "Product data sheet EN"})
    assert labeled[0]["title"] == "Acme HVAC: Product data sheet EN (Sika PDS)"   # uuid + %20 gone
    assert find_vendor.topic_for(cfg, "https://x/wp-content/uploads/836205.pdf", "") == "equipment_systems"  # 'uploads' != load
    assert out[0]["topic"] == "equipment_systems" and out[1]["topic"] == "controls_bas"
    assert out[0]["license"] == "open" and out[0]["format"] == "pdf"
    assert out[0]["license_url"] == "https://acme.example/terms"
    assert out[0]["rights_verified_at"] == "2026-08-28"
    assert out[0]["document_type"] == "product-literature"
    assert registry.shard_path(out[0]["id"]).name == "vendor.yaml"
    assert registry.manifest_shard(out[0]["id"]) == "vendor"
    assert registry.discovered(out[0]["id"])


def test_sitemap_pages_scans_a_budget_remembers_visits_and_accumulates_docs(tmp_path, monkeypatch):
    monkeypatch.setattr(find_vendor, "CACHE_DIR", tmp_path / "cache")
    cfg = vendor(mechanism="sitemap_pages", page_pattern=r"/product/", pages_per_run=2)
    pages = ["https://acme.example/product/p1", "https://acme.example/product/p2",
             "https://acme.example/product/p3", "https://acme.example/news/n1"]
    html = {
        "https://acme.example/product/p1": b'<a href="/files/p1-datasheet.pdf">d</a>',
        "https://acme.example/product/p2": b'<a href="https://cdn.acme.example/p2_EN_low.pdf">d</a>',
        "https://acme.example/product/p3": b'<a href="/files/p3-iom.pdf">d</a>',
    }
    fetched = []

    def fetcher(url, delay=0.0, method="GET", data=None):
        fetched.append(url)
        return html[url]

    first = find_vendor.candidate_documents("acme", cfg, pages, 40, fetcher)
    assert fetched == ["https://acme.example/product/p1", "https://acme.example/product/p2"]
    assert first == ["https://acme.example/files/p1-datasheet.pdf",
                     "https://cdn.acme.example/p2_EN_low.pdf"]
    assert find_vendor.known_titles_for("acme")["https://acme.example/files/p1-datasheet.pdf"] == "d"
    second = find_vendor.candidate_documents("acme", cfg, pages, 40, fetcher)
    assert fetched[2:] == ["https://acme.example/product/p3"]   # p1/p2 remembered, news skipped
    assert second[-1] == "https://acme.example/files/p3-iom.pdf" and len(second) == 3
    third = find_vendor.candidate_documents("acme", cfg, pages, 40, fetcher)
    assert len(fetched) == 3 and len(third) == 3               # nothing left to scan, docs kept


def test_sitemap_pages_fails_only_when_every_page_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(find_vendor, "CACHE_DIR", tmp_path / "cache")
    cfg = vendor(mechanism="sitemap_pages", page_pattern=r"/product/")
    pages = ["https://acme.example/product/p1", "https://acme.example/product/p2"]

    def flaky(url, delay=0.0, method="GET", data=None):
        if url.endswith("p1"):
            raise RuntimeError("503")
        return b'<a href="/files/p2.pdf">d</a>'

    assert find_vendor.candidate_documents("acme", cfg, pages, 40, flaky) == [
        "https://acme.example/files/p2.pdf"]
    with pytest.raises(RuntimeError, match="all 1 page fetches failed"):
        find_vendor.candidate_documents("acme", cfg, ["https://acme.example/product/p9"], 40,
                                        lambda u, *a, **k: (_ for _ in ()).throw(RuntimeError("x")))


def test_pdf_links_reads_anchors_bare_hrefs_and_json_embedded_urls():
    page = ('<a href="/d/a.pdf"><span>Data sheet</span> EN</a>'
            '<link href="/d/b.pdf">'
            '<script>{"fileTypes":[{"url":"https:\\/\\/cdn.example\\/d\\/c.pdf?f=1","isGated":false}]}</script>')
    assert find_vendor.pdf_links("https://acme.example/p", page) == [
        ("https://acme.example/d/a.pdf", "Data sheet EN"),
        ("https://acme.example/d/b.pdf", ""),
        ("https://cdn.example/d/c.pdf?f=1", ""),
    ]


def test_json_api_pages_until_short_page_and_keeps_titles(tmp_path, monkeypatch):
    import json as _json
    monkeypatch.setattr(find_vendor, "CACHE_DIR", tmp_path / "cache")
    cfg = vendor(mechanism="json_api", sitemaps=None,
                 api_url_template="https://docs.example/bas/api/khub/documents?per-page=2&page={page}",
                 doc_url_template="https://docs.example/bas/api/khub/documents/{id}/content",
                 pdf_pattern="/documents/", title_field="title",
                 filter_field="mimeType", filter_value="application/pdf")
    pages = {
        1: [{"id": "a", "title": "Metasys IOM", "mimeType": "application/pdf"},
            {"id": "b", "title": "Video", "mimeType": "video/mp4"}],
        2: [{"id": "c", "title": "Chiller catalog", "mimeType": "application/pdf"}],
    }
    calls = []

    def fetcher(url, delay=0.0, method="GET", data=None):
        page = int(url.rsplit("page=", 1)[1]); calls.append(page)
        return _json.dumps(pages.get(page, [])).encode()

    urls = find_vendor.enumerate_json_api(cfg, fetcher, key="jci")
    assert urls == ["https://docs.example/bas/api/khub/documents/a/content",
                    "https://docs.example/bas/api/khub/documents/c/content"]
    assert calls == [1, 2]  # page 2 was short -> stop without a third request
    assert find_vendor.known_titles_for("jci", cfg)[urls[1]] == "Chiller catalog"


def test_html_index_post_and_pagination_template(tmp_path, monkeypatch):
    monkeypatch.setattr(find_vendor, "CACHE_DIR", tmp_path / "cache")
    cfg = vendor(mechanism="html_index", sitemaps=None, method="POST", data={"model": ""},
                 index_url_template="https://acme.example/docs?page={page}", index_pages=2)
    seen = []

    def fetcher(url, delay=0.0, method="GET", data=None):
        seen.append((url, method, data))
        n = url[-1]
        return f'<a href="https://cdn.example/d{n}.pdf">Doc {n}</a>'.encode()

    urls = find_vendor.enumerate_html_index(cfg, fetcher, key="acme")
    assert urls == ["https://cdn.example/d1.pdf", "https://cdn.example/d2.pdf"]
    assert seen[0] == ("https://acme.example/docs?page=1", "POST", {"model": ""})
    assert find_vendor.known_titles_for("acme")["https://cdn.example/d2.pdf"] == "Doc 2"


def test_url_rewrite_turns_document_pages_into_download_urls():
    cfg = vendor(url_rewrite=[[r"^https://www\.se\.com/ww/en/download/document/([^/]+)/?$",
                               r"https://download.se.com/files?p_Doc_Ref=\1"]],
                 pdf_pattern=r"p_Doc_Ref=")
    docs = find_vendor.select_documents(cfg, ["https://www.se.com/ww/en/download/document/SPD_ABC/",
                                              "https://www.se.com/ww/en/download/"])
    assert docs == ["https://download.se.com/files?p_Doc_Ref=SPD_ABC"]
    e = find_vendor.entries_for("se", cfg, docs, set(), set(), cap=5)[0]
    assert e["id"] == "vnd-se-spd-abc" and e["title"].endswith("files — SPD ABC")   # query value, not 'files'
    assert find_vendor.select_documents(cfg, ["https://x/privacy-policy.pdf", "https://x/terms-of-use.pdf"]) == []


def test_json_api_stops_when_the_api_ignores_pagination(tmp_path, monkeypatch):
    import json as _json
    monkeypatch.setattr(find_vendor, "CACHE_DIR", tmp_path / "cache")
    cfg = vendor(mechanism="json_api", sitemaps=None,
                 api_url_template="https://d/api?page={page}", doc_url_template="https://d/{id}",
                 pdf_pattern="https://d/")
    calls = []
    same = [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}]
    urls = find_vendor.enumerate_json_api(
        cfg, lambda url, *a, **k: (calls.append(url), _json.dumps(same).encode())[1], key="k")
    assert urls == ["https://d/a", "https://d/b"] and len(calls) == 2  # page 2 added nothing -> stop


def test_paginated_listing_is_scanned_with_a_budget_and_raw_links(tmp_path, monkeypatch):
    monkeypatch.setattr(find_vendor, "CACHE_DIR", tmp_path / "cache")
    cfg = vendor(mechanism="html_index", sitemaps=None,
                 index_url_template="https://v/document-search/?page={page}", index_pages=3,
                 pages_per_run=2, raw_link_pattern=r'"downloadLink":"([^"]+\.pdf)"', pdf_pattern=r"\.pdf")
    seen = []

    def fetcher(url, *a, **k):
        seen.append(url); n = url[-1]
        return ('{"documents":[{"downloadLink":"/globalassets/doc%s.pdf"}]}' % n).encode()

    universe = find_vendor.universe_for("v", cfg, refresh=True, fetcher=fetcher)
    assert universe == [f"https://v/document-search/?page={n}" for n in (1, 2, 3)] and seen == []
    docs = find_vendor.candidate_documents("v", cfg, universe, 40, fetcher)
    assert docs == ["https://v/globalassets/doc1.pdf", "https://v/globalassets/doc2.pdf"]
    assert seen == ["https://v/document-search/?page=1", "https://v/document-search/?page=2"]
    docs = find_vendor.candidate_documents("v", cfg, universe, 40, fetcher)
    assert seen[-1] == "https://v/document-search/?page=3" and len(docs) == 3


def test_validate_json_api_and_template_shapes():
    errors = find_vendor.validate_vendors({"vendors": {
        "x": vendor(mechanism="json_api", api_url_template="https://a/x", doc_url_template="https://a/{id}"),
        "y": vendor(mechanism="html_index", sitemaps=None, index_url_template="https://a/?p={page}"),
        "z": vendor(method="PUT"),
    }})
    text = "\n".join(errors)
    assert "'{page}'" in text and "index_pages" in text and "method must be GET or POST" in text


def test_validate_requires_page_pattern_for_sitemap_pages():
    errors = find_vendor.validate_vendors({"vendors": {"x": vendor(mechanism="sitemap_pages")}})
    assert any("page_pattern" in e for e in errors)


def test_cap_limits_entries():
    cfg = vendor()
    docs = [f"https://acme.example/lit/d{i}.pdf" for i in range(5)]
    assert len(find_vendor.entries_for("acme", cfg, docs, set(), set(), cap=2)) == 2


def test_host_delays_cover_sitemap_and_extra_hosts():
    vendors = {
        "a": vendor(crawl_delay=5, hosts=["cdn.acme.example"]),
        "b": vendor(sitemaps=["https://b.example/s.xml"]),
    }
    assert find_vendor.host_delays(vendors) == {"acme.example": 5.0, "cdn.acme.example": 5.0}


def test_cursor_round_robins_enabled_vendors_and_stages_a_proposal(tmp_path, monkeypatch, capsys):
    cfgs = {"off": vendor(enabled=False), "a": vendor(), "b": vendor(name="Bee")}
    path = tmp_path / "vendors.json"
    path.write_text(json.dumps({"vendors": cfgs}))
    monkeypatch.setattr(find_vendor, "VENDORS_PATH", path)
    monkeypatch.setattr(find_vendor, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(registry, "existing_keys", lambda: (set(), set(), set()))
    monkeypatch.setattr(
        find_vendor, "enumerate_sitemap",
        lambda cfg, fetcher=None: [f"https://acme.example/lit/{cfg['name']}-x.pdf"],
    )
    proposal = tmp_path / "proposal.json"
    monkeypatch.setenv("NEKAISE_PROPOSAL_FILE", str(proposal))
    monkeypatch.setattr(sys, "argv", ["find_vendor.py", "--cursor", "3", "--max", "5", "--append"])

    find_vendor.main()  # cursor 3 % 2 enabled -> "b"

    staged = json.loads(proposal.read_text())
    assert [e["id"] for e in staged] == ["vnd-b-bee-x"]
    assert "NEW Bee documents (cursor->b" in capsys.readouterr().out
    # second run hits the cache, not the network
    monkeypatch.setattr(find_vendor, "enumerate_sitemap",
                        lambda cfg, fetcher=None: pytest.fail("must use cached universe"))
    monkeypatch.setattr(sys, "argv", ["find_vendor.py", "--vendor", "b", "--max", "5"])
    find_vendor.main()


def test_enumeration_failure_aborts_without_proposal(tmp_path, monkeypatch):
    path = tmp_path / "vendors.json"
    path.write_text(json.dumps({"vendors": {"a": vendor()}}))
    monkeypatch.setattr(find_vendor, "VENDORS_PATH", path)
    monkeypatch.setattr(find_vendor, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(registry, "existing_keys", lambda: (set(), set(), set()))

    def boom(cfg, fetcher=None):
        raise RuntimeError("503")

    monkeypatch.setattr(find_vendor, "enumerate_sitemap", boom)
    monkeypatch.setattr(sys, "argv", ["find_vendor.py", "--cursor", "0", "--append"])
    with pytest.raises(SystemExit) as exc:
        find_vendor.main()
    assert exc.value.code == 1
