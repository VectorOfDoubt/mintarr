# Testing

> **Type:** Development / quality
> **Version:** 1.0 — 2026-05-26
> **Status:** Living document. Updates as test patterns evolve.
> **Audience:** Contributors writing new tests or debugging test failures. Maintainers reviewing test coverage on PRs.

---

## 1. Test philosophy

Mintarr's test suite is the load-bearing check on every change. By the time a PR reaches review, the tests have already caught most regressions. The maintainer's review focuses on scope, design, and security — not on whether the change works.

For this to remain true, tests must:

- **Run fast.** The suite runs in ~3 seconds when cached. New tests should preserve this.
- **Run in isolation.** No test depends on another test having run first.
- **Run deterministically.** A test that flakes is broken; remove or fix it.
- **Cover the boundary explicitly.** When you add a feature, add a test that asserts the new behaviour, AND a test that asserts the old behaviour still works.

The suite is currently 300+ tests. New PRs typically add 5-15 tests.

## 2. Test matrix per change type

Not every PR needs every test. This table is the authoritative answer to
"what tests must I add for this change?" Reviewers reject PRs that miss
the required tests for their change type.

| If your change touches... | Required tests | Recommended tests |
|---|---|---|
|  (new source) | pytest unit tests for  +  +  against fixture data | One end-to-end pipeline test with this adapter mocked |
|  | pytest per-phase unit test + at least one full-pipeline integration test | Failure-mode test for the changed phase |
|  | pytest unit tests for new policy branch + sidecar shape regression test | Property-test (hypothesis) if the rule is numeric |
|  Flask route | pytest endpoint test (auth + happy path + at least one error case) | Route appears in  output |
|  (schema change) | Migration test asserting old + new shape coexist | Round-trip property test |
|  (template / JSON) | pytest endpoint test + assert response structure | HTML smoke-render test if template logic changed |
| Dependency bump (Dockerfile / requirements) | Full suite passes; container builds | Container smoke test boots Flask |
|  only | None — link check via  is enough | MkDocs  build locally |
|  | At least one test that imports the script and asserts its public function | If shell: shellcheck clean |
|  | Workflow runs green on PR | Manual  smoke-run |

### 2.1 Test layers we currently run

| Layer | Tooling | Where | Trigger |
|---|---|---|---|
| Unit + integration (Python) | pytest |  | Every push, every PR |
| Static analysis | ruff (with per-file ignores) |  +  | Every push, every PR |
| Type check | mypy 1.10 |  | Disabled in v0.1.0; tracked for v0.2.0 |
| Container build | Docker Buildx | root  | Every push, every tag |
| Docs build | mkdocs Material  |  | Push to  paths |
| Route inventory |  | Flask app | Manual + cutover gate |
| Sidecar format |  | Real sidecars | Manual + cutover gate |

### 2.2 Test layers we do NOT run yet

These do not exist today. The condition under which each gets added is
documented so future maintainers know when to introduce them.

| Layer | Add when... | Likely tooling |
|---|---|---|
| Frontend E2E | Phase 2 dashboard redesign replaces server-rendered HTML/CSS/vanilla JS with a JavaScript framework. Whichever framework wins drives the choice (Playwright for cross-browser, Vitest + Testing Library for component, Storybook for visual). Until then, dashboard tests are server-side pytest only. | Playwright + framework-native runner |
| End-to-end against a real Lidarr | An operator-reported bug shows the mocked Lidarr fixture diverges from real Lidarr behaviour in a way unit tests cannot catch. Requires a Lidarr-in-Docker fixture; cost is CI run time. | pytest + lidarr container service |
| Performance baseline | Worker-queue or pipeline change risks regressing the N=1 single-threaded throughput recorded as the v0.1.0 baseline. Adds one pytest per measured path. | pytest-benchmark |
| Soulseek lane | F3.5a Soulseek adapter implementation lands (post-cutover). Tests cover completed-folder validation, settle window, partial-marker rejection. | pytest with mocked slskd HTTP |
| Container security scan | Operator or maintainer requests, OR repo grows to handle PII / regulated content (will not happen by design). | trivy / grype in CI |
| Dependency licence scan | New AGPL-incompatible dependency proposed in a PR. | pip-licenses + manual review against ADR-0005 |
| Mutation testing | A bug regresses despite passing tests; reveals the suite is too forgiving. | mutmut or cosmic-ray |

If you think a layer is needed but the condition above has not been
met, open an issue rather than adding the layer in your PR — adding
test infrastructure is its own change.

## 3. Running tests

### 3.1 Full suite

```bash
docker compose -f docker-compose.test.yaml run --rm tests
```

Builds the test image on first run (~3-5 minutes). Runs in 3 seconds after that.

### 3.2 Single file

```bash
docker compose -f docker-compose.test.yaml run --rm \
    --entrypoint pytest tests /tests/test_pipeline_phases.py -v
```

### 3.3 Single test

```bash
docker compose -f docker-compose.test.yaml run --rm \
    --entrypoint pytest tests \
    /tests/test_pipeline_phases.py::test_execute_source_grab_runs_phases_in_order -v
```

### 3.4 With short traceback

```bash
docker compose -f docker-compose.test.yaml run --rm \
    --entrypoint pytest tests /tests/ -v --tb=short
```

### 3.5 With stop-on-first-failure

```bash
docker compose -f docker-compose.test.yaml run --rm \
    --entrypoint pytest tests /tests/ -v -x
```

### 3.6 With stdout captured

```bash
docker compose -f docker-compose.test.yaml run --rm \
    --entrypoint pytest tests /tests/ -v -s
```

`-s` is useful when debugging — `print` statements appear in test output.

## 4. Test structure

Tests live under `tests/`. One file per logical area:

```
tests/
├── conftest.py                    ← shared fixtures
├── test_auth.py                   ← API key + auth tests
├── test_adapter_base.py           ← adapter Protocol contract
├── test_backfill_state.py         ← state_db rebuild from sidecars
├── test_dashboard.py              ← dashboard endpoint tests
├── test_helpers.py                ← server helper functions
├── test_import_cleanup.py         ← cleanup pathways after import
├── test_local_folder_adapter.py   ← LocalFolder adapter
├── test_newznab_routing.py        ← Newznab aggregation + addurl routing
├── test_pipeline_phases.py        ← pipeline.execute_source_grab phases
├── test_sensor_registry.py        ← sensor registry
├── test_state_db.py               ← state_db core
├── test_state_db_source_type.py   ← F3.1 source_type migration
├── test_tidal_grab_port.py        ← addurl + tidal_grab executor
├── test_v2_decision_logging.py    ← V2 decision audit
├── test_v2_import_flow.py         ← end-to-end V2 import flow
├── test_v2_verification_endpoints.py ← V2 promote/discard endpoints
├── test_verification.py           ← V2 policy scoring
└── test_worker_queue.py           ← worker queue mechanics
```

When you add a new file, prefix it `test_<area>.py`. When you add a new test in an existing file, group it near related tests.

## 5. Fixtures

`conftest.py` provides cross-cutting fixtures. The key ones:

### 5.1 `_reset_state_db` (autouse)

Automatically resets state_db between tests:

- Sets `state_db._initialized = False`
- Deletes the test database file
- Re-registers the TIDAL and LocalFolder adapters

This means every test starts with a clean state_db and a fresh adapter registry. No test depends on database state from another test.

### 5.2 `fake_album`

A minimal stand-in for `tidalapi.Album` used by `_classify_quality` and `_release_title`:

```python
@pytest.fixture
def fake_album():
    return SimpleNamespace(
        id=12345,
        name="Random Access Memories",
        duration=4500,
        artist=SimpleNamespace(name="Daft Punk"),
        release_date=SimpleNamespace(year=2013),
        num_tracks=13,
        type="ALBUM",
    )
```

### 5.3 `fresh_db` (per-test)

For tests that need explicit state_db setup beyond the autouse reset:

```python
@pytest.fixture
def fresh_db():
    import state_db
    state_db._initialized = False
    if state_db._db_path.exists():
        state_db._db_path.unlink()
    state_db.init()
    return state_db
```

## 6. Test patterns

### 6.1 Testing an adapter

Use a `FakePipelineContext` to exercise the adapter without the runtime:

```python
class _FakeContext:
    def __init__(self, *, jid, raw_dir, output_dir):
        self.jid = jid
        self.worker_job_id = None
        self.raw_dir = raw_dir
        self.output_dir = output_dir
        self.progress_calls = []

    def check_cancelled(self): pass
    def run_subprocess(self, argv, *, timeout, text=True):
        return subprocess.CompletedProcess(argv, 0, "", "")
    def set_progress(self, **kwargs):
        self.progress_calls.append(kwargs)
    def log(self, *args, **kwargs): pass


def test_my_adapter_download_raw_copies_files(tmp_path):
    from adapters.my_source import MySourceAdapter
    adapter = MySourceAdapter(ingest_root=str(tmp_path / "src"))
    # set up source files
    src = tmp_path / "src" / "Artist" / "Album"
    src.mkdir(parents=True)
    (src / "01 track.flac").write_bytes(b"FAKE-FLAC" + bytes(2048))

    raw_dir = tmp_path / "raw"
    ctx = _FakeContext(jid="test", raw_dir=raw_dir, output_dir=tmp_path / "out")
    result = adapter.download_raw("Artist/Album", ctx)

    assert result.file_count == 1
    assert (raw_dir / "01 track.flac").exists()
```

### 6.2 Testing a pipeline phase

Use a `FakeAdapter` to drive the pipeline:

```python
class _FakeAdapter:
    name = "fake"
    source_type = "fake"

    def is_enabled(self): return True
    def search(self, *a, **kw): return []

    def download_raw(self, candidate_id, ctx):
        from adapters.base import RawDownload
        ctx.raw_dir.mkdir(parents=True, exist_ok=True)
        (ctx.raw_dir / "track.flac").write_bytes(b"FAKE")
        return RawDownload(files_dir=ctx.raw_dir, file_count=1, total_bytes=4)

    def cleanup(self, jid, ctx): pass


def test_execute_source_grab_runs_phases_in_order(tmp_path, monkeypatch):
    import server, pipeline

    monkeypatch.setattr(server, "OUTPUT_BASE", tmp_path / "output")
    monkeypatch.setattr(server, "DOWNLOAD_BASE", tmp_path / "downloads")
    monkeypatch.setattr(server, "_trigger_lidarr_import", lambda *a, **k: None)
    monkeypatch.setattr(server, "_raise_if_job_cancelled", lambda *a, **k: None)
    # ... etc

    adapter = _FakeAdapter()
    ctx = _FakeContext(jid="test", raw_dir=server.DOWNLOAD_BASE / "test", output_dir=...)
    job = {"id": None, "jid": "test", "payload_json": json.dumps({"source_id": "999"})}

    pipeline.execute_source_grab(job, adapter, ctx)

    stages = [c["stage"] for c in ctx.progress_calls]
    assert "preparing" in stages
    assert "ready_for_import" in stages
```

### 6.3 Testing a Flask endpoint

Use Flask's test client:

```python
@pytest.fixture
def client():
    import server
    server.app.config["TESTING"] = True
    return server.app.test_client()


def test_endpoint_requires_api_key(client):
    r = client.get("/dashboard/v1/summary")
    assert r.status_code == 401


def test_endpoint_returns_summary(client):
    r = client.get(f"/dashboard/v1/summary?apikey={os.environ['MINTARR_API_KEY']}")
    assert r.status_code == 200
    assert "total_records" in r.get_json()
```

### 6.4 Testing with mocked Lidarr

Monkeypatch the helpers that call Lidarr:

```python
def test_import_path_calls_lidarr_correctly(monkeypatch):
    import server

    seen = {}
    def _capturing_trigger(jid, output_dir, *, worker_job_id=None, source_type="tidal"):
        seen["jid"] = jid
        seen["source_type"] = source_type

    monkeypatch.setattr(server, "_trigger_lidarr_import", _capturing_trigger)

    # ... drive the test ...
    assert seen["source_type"] == "local"
```

### 6.5 Testing migrations

Build a pre-migration database explicitly:

```python
def test_migration_adds_source_type_column(tmp_path):
    import state_db

    db = tmp_path / "legacy.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute("CREATE TABLE records (jid TEXT PRIMARY KEY)")  # pre-migration shape

    state_db._initialized = False
    state_db.init(db_path=db)  # runs migration

    with sqlite3.connect(str(db)) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(records)").fetchall()}
    assert "source_type" in cols
```

Also test idempotency (running the migration twice doesn't error):

```python
def test_migration_is_idempotent(tmp_path):
    import state_db

    db = tmp_path / "fresh.db"
    state_db._initialized = False
    state_db.init(db_path=db)

    state_db._initialized = False
    state_db.init(db_path=db)  # second call should be no-op
```

## 7. Writing assertions

### 7.1 Assert specifics, not "it worked"

Bad:
```python
result = some_function()
assert result is not None
```

Good:
```python
result = some_function()
assert result.field_a == "expected"
assert result.field_b == 42
```

The first form catches "did not crash"; the second catches "did the right thing".

### 7.2 Use pytest's assertion introspection

Plain `assert` statements produce useful failure messages because pytest rewrites them:

```python
assert payload["status"] == "ok"
# On failure: "AssertionError: assert 'failed' == 'ok'"
```

Avoid `unittest.TestCase.assertEqual` and friends — pytest's rewriting is better.

### 7.3 Use parametrize for table-driven tests

```python
@pytest.mark.parametrize("score,expected_decision", [
    (95, "ACCEPT"),
    (75, "ACCEPT"),
    (60, "ACCEPT_PROVISIONAL"),
    (30, "REVIEW_REQUIRED"),
    (10, "BLOCK"),
])
def test_v2_decision_by_score(score, expected_decision):
    result = verification.decide(score, [])
    assert result.decision == expected_decision
```

This generates 5 tests, each named distinctly. Failures point to the specific score that failed.

## 8. Coverage expectations

Mintarr does not enforce coverage thresholds in CI. Coverage is meaningless without judgement — 100% coverage of obvious code provides no signal.

The expectation is qualitative:

- **New logic gets tests.** A PR adding `def foo(): ...` without `def test_foo_does_x(): ...` is incomplete.
- **Bug fixes get regression tests.** A PR fixing a bug without a test demonstrating the old broken behaviour and the new fixed behaviour is incomplete.
- **Critical paths get integration tests.** The four pipeline phases, the V2 decision matrix, the worker queue mechanics, the addurl dispatcher — these have end-to-end tests because they are load-bearing.
- **Adapters get contract tests.** Every adapter has tests covering is_enabled, search, download_raw happy path, download_raw failure paths.

## 9. What is hard to test

### 9.1 Live Lidarr interaction

Tests do not call a real Lidarr. They mock `_trigger_lidarr_import` and verify Mintarr's intent. To test against a real Lidarr, run a manual smoke test (see [DEVELOPMENT.md](DEVELOPMENT.md) §"Running Mintarr locally for manual testing").

### 9.2 Subprocess behaviour

Tests do not invoke real `tidal-dl-ng`, `ffprobe`, `ffmpeg`, `flac`. They monkeypatch `subprocess.run` to return predetermined outputs. This means the test suite does not catch breakage in those tools' actual behaviour — only Mintarr's handling of them.

### 9.3 Race conditions

The worker queue has lease + heartbeat mechanics that are hard to test in unit-test scale. Integration tests cover the simple cases (dequeue → run → complete); the complex cases (worker crash mid-job, lease takeover) are covered by hand-crafted scenarios in `test_worker_queue.py`.

### 9.4 Real Soulseek peer behaviour

When F3.5b lands, Soulseek HTTP interaction will be tested against mocked slskd responses. Real peer behaviour cannot be reproduced in tests.

## 10. Test-driven adapter authoring

For new source adapters, write the tests first:

1. Decide what the adapter does
2. Write tests covering is_enabled, search (returns empty for indexer-test, returns hits for real query), download_raw (copies/fetches files, raises on invalid input), cleanup (idempotent)
3. Run the tests — they should all fail
4. Implement the adapter
5. Run the tests — they should pass

This is not enforced; it is just easier. By writing tests first, you confront the contract before writing the code that has to satisfy it.

## 11. Debugging test failures

### 11.1 Read the traceback

The traceback usually points at the failure line. With `--tb=short`, the last frame is what failed.

### 11.2 Add print statements with `-s`

```python
def test_my_thing():
    result = compute()
    print(f"DEBUG: result = {result!r}")
    assert result == "expected"
```

Then run with `-s`:

```bash
docker compose -f docker-compose.test.yaml run --rm \
    --entrypoint pytest tests /tests/test_foo.py::test_my_thing -v -s
```

### 11.3 Use pytest's `--pdb`

Drops into a debugger on first failure:

```bash
docker compose -f docker-compose.test.yaml run --rm \
    --entrypoint pytest tests /tests/test_foo.py::test_my_thing -v --pdb
```

Use `c` to continue, `q` to quit.

### 11.4 Isolate the failing test

A test that passes in isolation but fails in the full suite usually has a state-sharing problem. Find it by running smaller subsets:

```bash
# Does it fail by itself?
pytest tests/test_foo.py::test_failing -v

# Does it fail with one other file?
pytest tests/test_foo.py tests/test_bar.py -v
```

The `_reset_state_db` autouse fixture should prevent most state-sharing problems. If you find a new failure mode, that's a fixture bug — fix the fixture, not the test.

---

> Last updated: 2026-05-26
