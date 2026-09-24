"""The loader, pruner and cleaner write paths as they were before ADR 0001 stage 3, step 6
(scripts at 8dde58e2ba), kept verbatim in logic as the reference for the store path's
equivalence tests: whole-manifest rewrites through registry.write_manifest_rows, in-place registry
removal through registry.remove_ids, blocklist.add, and prune-ledger appends through
ops.append_jsonl.

The helpers they call are the current modules' (unchanged by step 6). The caller points module
paths at a repository copy (tests/pipeline_repo.point)."""
from __future__ import annotations

import os
import time
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import blocklist
import build_corpus as bc
import clean_corpus as cc
import host_policy
import legacy_registry
import ops
import prune_corpus as pc
import quality
import registry


# --- loader -------------------------------------------------------------------------------------

def legacy_load(*, force=False, workers=16, extract_workers=1, only="", reextract=False,
                source="", fmt="", ids_from="") -> None:
    only = {t.strip() for t in only.split(",") if t.strip()}
    selection = bc.reextract_selector(source, fmt, ids_from)
    restrictions = legacy_registry.load_eligibility()
    all_srcs = legacy_registry.load_entries()
    srcs = [s for s in all_srcs if registry.is_training_eligible(s, restrictions)]
    manifest = {r["id"]: r for r in legacy_registry.load_manifest_rows()}

    def write_manifest(rows):
        legacy_registry.write_manifest_rows(rows.values())

    if reextract:
        bc.reextract(manifest, restrictions, selection, only)
        write_manifest(manifest)
        return
    policy = legacy_registry.load_host_policy()
    todo = []
    for s in srcs:
        if only and s.get("topic") not in only:
            continue
        cur = manifest.get(s["id"])
        if cur and cur.get("status") == "ok" and not force:
            if cur.get("raw_path") and (bc.HERE / cur["raw_path"]).exists():
                continue
        if host_policy.suspended(s["url"], policy):
            continue
        todo.append(s)
    extraction_templates = {
        row["sha256"]: row for row in manifest.values()
        if (row.get("sha256") and row.get("status") == "ok" and row.get("text_path")
            and (bc.HERE / row["text_path"]).exists())
    }
    done = 0

    def record_result(rec: dict) -> None:
        nonlocal done
        done += 1
        bc.note_retry(rec, manifest.get(rec["id"]))
        manifest[rec["id"]] = rec
        if done % 25 == 0:
            write_manifest(manifest)

    todo, deferred_ids = bc.cap_per_host(todo, manifest)
    bc.write_deferred(deferred_ids)
    ordered = bc.fair_sources(todo)
    with (ThreadPoolExecutor(max_workers=max(1, workers)) as downloads,
          bc.ProcessPoolExecutor(max_workers=max(1, extract_workers),
                                 mp_context=bc.EXTRACTION_CONTEXT) as extractors):
        download_futures = {downloads.submit(bc.download_one, src) for src in ordered}
        extract_futures = set()
        extract_sha = {}
        waiting_by_sha: dict[str, list[dict]] = defaultdict(list)
        while download_futures or extract_futures:
            completed, _ = wait(download_futures | extract_futures, return_when=FIRST_COMPLETED)
            for future in completed:
                if future in download_futures:
                    download_futures.remove(future)
                    rec = future.result()
                    if rec.get("raw_path"):
                        digest = rec["sha256"]
                        if digest in extraction_templates:
                            record_result(bc.reuse_extraction(rec, extraction_templates[digest]))
                        elif digest in extract_sha.values():
                            waiting_by_sha[digest].append(rec)
                        else:
                            extract_future = extractors.submit(bc.extract_downloaded, rec)
                            extract_futures.add(extract_future)
                            extract_sha[extract_future] = digest
                    else:
                        record_result(rec)
                else:
                    extract_futures.remove(future)
                    digest = extract_sha.pop(future)
                    extracted = future.result()
                    extraction_templates[digest] = extracted
                    record_result(extracted)
                    for duplicate in waiting_by_sha.pop(digest, []):
                        record_result(bc.reuse_extraction(duplicate, extracted))
    if todo:
        write_manifest(manifest)


# --- pruner -------------------------------------------------------------------------------------

def legacy_write_prune_ledger(manifest, drop, blocklisted_urls) -> int:
    if not drop:
        return 0
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    run_id = os.environ.get("NEKAISE_RUN_ID")
    by_id = {r["id"]: r for r in manifest}
    with ops.named_lock("prune-ledger", timeout=30):
        for sid in sorted(drop):
            r = by_id[sid]
            ops.append_jsonl(legacy_registry.prune_ledger_path(sid), {
                "id": sid, "url": r.get("url"), "title": r.get("title"), "reason": drop[sid],
                "source": r.get("source"), "topic": r.get("topic"), "license": r.get("license"),
                "http_status": r.get("http_status"), "error": r.get("error"),
                "sha256": r.get("sha256"), "quality": r.get("quality"),
                "blocklisted": blocklist.normalize(r.get("url")) in blocklisted_urls,
                "pruned_at": now,
                **({"run_id": run_id} if run_id else {}),
            })
    return len(drop)


def legacy_prune(*, apply=True, drop_ids_from=None) -> dict[str, str]:
    """Returns the drop decisions (id -> reason)."""
    HERE = pc.HERE
    manifest = legacy_registry.load_manifest_rows()
    reviewed_drop = pc.reviewed_title_drops(drop_ids_from, manifest)
    policy = legacy_registry.load_host_policy()
    deferred = pc.deferred_ids()
    protected = pc.protected_ids(manifest, policy, deferred)
    seen_titles = {registry.norm(r.get("title")) for r in manifest
                   if not registry.discovered(r["id"]) and r.get("status") == "ok"
                   and not registry.suspended_unavailable(r, policy, HERE)}
    drop: dict[str, str] = dict(reviewed_drop)
    for r in manifest:
        if not registry.discovered(r["id"]):
            continue
        if r["id"] in reviewed_drop:
            continue
        if r["id"] in protected:
            if pc._usable_text(r):
                seen_titles.add(registry.norm(r.get("title")))
            continue
        if r["status"] != "ok":
            if pc.retry_pending(r):
                continue
            drop[r["id"]] = "failed"
            continue
        if r["id"].startswith("pat-") and quality.off_domain_title(r.get("title", "")):
            drop[r["id"]] = "off-topic-title"
            continue
        tp = r.get("text_path")
        if not tp or not (HERE / tp).exists():
            drop[r["id"]] = "no-text"
            continue
        m = r.get("quality")
        if not m:
            m = r["quality"] = quality.metrics(quality.body((HERE / tp).read_text()))
        q = quality.verdict(m, quality.is_booklike(r["id"], r.get("format", "pdf")))
        if q != "ok":
            drop[r["id"]] = q
            continue
        tk = registry.norm(r.get("title"))
        if tk and tk in seen_titles:
            drop[r["id"]] = "dup-title"
        else:
            seen_titles.add(tk)
    by_sha: dict[str, list] = {}
    for r in manifest:
        if (r.get("status") == "ok" and r.get("sha256") and r["id"] not in drop
                and (r["id"] not in protected or pc._usable_text(r))):
            by_sha.setdefault(r["sha256"], []).append(r)
    for twins in by_sha.values():
        if len(twins) < 2:
            continue
        twins.sort(key=lambda r: (registry.discovered(r["id"]) and r["id"] not in protected,
                                  r["id"]))
        for r in twins[1:]:
            if r["id"] in protected:
                continue
            if registry.discovered(r["id"]):
                drop[r["id"]] = "dup-bytes"
    if not apply:
        return drop
    repeated_dns_urls = pc.repeated_dns_failure_urls(legacy_registry.load_prune_ledger_rows())
    block_urls = {
        blocklist.normalize(r.get("url")) for r in manifest
        if r["id"] in drop and pc._blocklistable(r, drop[r["id"]], repeated_dns_urls)
        and r.get("url")
    }
    legacy_registry.blocklist_add(block_urls)
    legacy_write_prune_ledger(manifest, drop, block_urls)
    legacy_registry.remove_ids(set(drop))
    for r in manifest:
        if r["id"] in drop:
            for p in (r.get("raw_path"), r.get("text_path"), r.get("corpus_path")):
                if p and (HERE / p).exists():
                    (HERE / p).unlink()
    keep = [r for r in manifest if r["id"] not in drop]
    legacy_registry.write_manifest_rows(keep)
    return drop


# --- cleaner ------------------------------------------------------------------------------------

def legacy_clean(*, rules_spec="stamp", force=False, workers=1) -> None:
    rules = cc.parse_rules(cc.stamped_ruleset() if rules_spec == "stamp" else rules_spec)
    restrictions = legacy_registry.load_eligibility()
    rows = legacy_registry.load_manifest_rows()
    todo, restricted = cc.partition_training_rows(rows, restrictions)
    CORPUS, STAMP = cc.CORPUS, cc.STAMP
    CORPUS.mkdir(parents=True, exist_ok=True)
    stamp_now = ",".join(rules) if rules else "none"
    stamp_was = STAMP.read_text().strip() if STAMP.exists() else None
    rebuild = force or stamp_was != stamp_now
    ops.atomic_write_text(STAMP, f"IN-PROGRESS {stamp_now}\n")
    attribution: Counter = Counter()
    cc.clear_corpus_metadata(restricted)
    by_id = {r["id"]: r for r in todo}
    tasks = [(r["id"], r["text_path"], rules, rebuild) for r in todo]
    with cc.ProcessPoolExecutor(max_workers=max(1, workers)) as pool:
        for sid, chars, attr, status, digest in pool.map(cc._clean_one, tasks, chunksize=64):
            if status == "missing-text":
                continue
            row = by_id[sid]
            row["corpus_path"] = f"corpus/{sid}.md"
            row["corpus_chars"] = chars
            if digest:
                row["corpus_sha256"] = digest
                row["cleaner_version"] = f"clean_corpus/2;rules={stamp_now}"
            attribution.update(attr)
    restricted_names = {f"{r['id']}.md" for r in restricted}
    cc.quarantine_policy_files(restricted)
    live = {f"{r['id']}.md" for r in todo}
    for p in [p for p in CORPUS.glob("*.md")
              if p.name not in live and p.name not in restricted_names]:
        p.unlink()
    legacy_registry.write_manifest_rows(rows)
    ops.atomic_write_text(STAMP, stamp_now + "\n")
