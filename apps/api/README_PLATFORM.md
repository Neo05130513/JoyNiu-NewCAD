# JoyNiu platform services

`app/platform.py`, `app/ocr.py` and `app/cam.py` are the service layer for the
PDM, account/RBAC, drawing evidence and CAM/NC workflows.  They do not depend
on FastAPI, so the same rules can run in a worker or a test.  The optional
`app/platform_api.py` module exposes a FastAPI router.

## Start the API

From this directory, install the API and development extras declared in
`pyproject.toml`:

```bash
python -m pip install -e '.[dev]'
uvicorn app.main:app --reload --port 8000
```

The geometry entrypoint can mount the platform router as follows:

```python
from app.platform_api import build_platform_services, create_platform_router

services = build_platform_services(
    database="${JOYNIU_DATA_DIR:-.}/joyniu.sqlite3",
    auth_secret="replace-this-with-a-32-byte-secret",
)
app.include_router(create_platform_router(services), prefix="/api/v1")
```

Set `JOYNIU_AUTH_SECRET` in deployments instead of putting a secret in source.
The default `":memory:"` database is intended for tests and demos; use a
filesystem path for PDM/account persistence.  CAM plans and OCR recognition
objects are process-local until their manifests are saved as PDM versions.

## API surface

All routes below are relative to the prefix selected by the host application.

| Area | Endpoints |
| --- | --- |
| Auth/RBAC | `POST /auth/users`, `POST /auth/login`, `GET /auth/me`, role/active administration |
| PDM | project/document CRUD, immutable `POST /pdm/documents/{id}/versions`, content download, status transitions |
| OCR | `GET /ocr/fixtures`, JSON base64 `POST /ocr/analyze`, binary `POST /ocr/analyze-bytes`, human confirmation |
| CAM/NC | plan/tools, operation creation, simulation, gate status, reviewer approval, NC release/download |

The first local account creation is an explicit bootstrap operation.  Further
accounts require `user:manage`.  Roles are intentionally small and auditable:
`viewer`, `designer`, `reviewer`, `manufacturing`, and `admin`.
Passwords require at least eight characters; access tokens are HMAC-signed and
re-check the current account on every request.

## Drawing acceptance fixture

`app/fixtures/bracket_support_v1.json` is a deterministic evidence profile for
the supplied four-view bracket drawing (SHA-256
`ea337023af0158438f9cea2482e8e2d6d4052fc04e7e7f4265956824478c4366`).  It records
the verified dimensions and model recipe:

* base `100 x 50 x 10 mm`;
* upper box `70 x 30 x 30 mm` (total height `40 mm`);
* through saddle opening `40 mm`, radius `R15`;
* two vertical `Ø20` bosses, centre distance `70 mm`.

An exact upload is automatically matched by hash.  For deterministic demos a
caller may send `fixtureId=bracket_support_v1`; the response still includes the
actual uploaded hash and an explicit `deterministic-fixture` engine label.
Unknown drawings are returned as `needs_review`, with unresolved fields rather
than guessed geometry.  To enable local OCR, install the `ocr` extra and the
`tesseract` binary, then set `JOYNIU_OCR_ENGINE=tesseract`.  Live OCR output is
never marked confirmed automatically.

## CAM release policy

NC release requires all of the following:

1. a simulation result matching the current plan revision and geometry hash;
2. zero collision, gouge, envelope or unresolved-operation failures;
3. a reviewer approval tied to that simulation (separate from the release actor);
4. the release actor's `cam:release` permission.

The role boundary is explicit: `reviewer` (or `admin`) records the approval;
`manufacturing` (or `admin`) performs the release.  A manufacturing account
does not receive `cam:approve`, and a reviewer account does not receive
`cam:release`.  If one account carries both roles, the separate-reviewer check
still rejects a release approved by that same account.

The built-in simulator is named `deterministic-precheck` and emits a warning in
every result.  It is a workflow gate, not a production material-removal proof;
connect a machine/OCCT simulator through the `SimulationEngine` protocol before
cutting stock.

## Tests

```bash
pytest
```

The service tests cover PDM immutability and stale writes, password/token/RBAC
behaviour, deterministic drawing evidence, and CAM simulation/release gates.
