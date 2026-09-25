"""TEST-ONLY process hook for end-to-end staged rounds (tests/test_staged_rounds.py).

A real `run_round.py` and every child it starts (finders, fetch/prune/clean, gates) consult the
host authority record, which production code locates through the passwd database only
(scripts/store_authority.py) — deliberately, so no environment can hide it. A test round over a
throwaway repository therefore cannot bind its PostgreSQL authority through the real host record
without touching the operator's configuration. Tests put THIS directory on PYTHONPATH instead:
Python imports `sitecustomize` at start-up in every process, and this one

* points store_authority.HOST_RECORD at NEKAISE_TEST_AUTHORITY_RECORD (the test's private record)
  as soon as the module is imported, and
* applies NEKAISE_TEST_PATCHES ({"module": {"ATTR": value}}) to modules as they are imported
  (e.g. a smaller loader checkpoint interval),

and does nothing when neither variable is set. Nothing in scripts/ imports or knows about it.
"""
import importlib.abc
import importlib.machinery
import json
import os
import sys

_RECORD = os.environ.get("NEKAISE_TEST_AUTHORITY_RECORD")
_PATCHES = json.loads(os.environ.get("NEKAISE_TEST_PATCHES") or "{}")
if _RECORD:
    _PATCHES.setdefault("store_authority", {})["HOST_RECORD"] = {"path": _RECORD}
# "module:hook:point": the module's crash-injection hook (e.g. materialize._crash) kills the
# process (SIGKILL-like, no cleanup) when it is called with `point`
_CRASH = os.environ.get("NEKAISE_TEST_CRASH")
if _CRASH:
    _module, _hook, _point = _CRASH.split(":")

    def _die(point, *_args, **_kwargs):
        if point == _point:
            os._exit(137)
    _PATCHES.setdefault(_module, {})[_hook] = _die


class _PatchOnImport(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name not in _PATCHES:
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or spec.loader is None:
            return None
        run = spec.loader.exec_module

        def exec_module(module, _run=run, _attrs=_PATCHES[name]):
            _run(module)
            from pathlib import Path
            for attr, value in _attrs.items():
                if isinstance(value, dict) and set(value) == {"path"}:
                    value = Path(value["path"])
                setattr(module, attr, value)
        spec.loader.exec_module = exec_module
        return spec


if _PATCHES:
    sys.meta_path.insert(0, _PatchOnImport())
