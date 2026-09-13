from __future__ import annotations

import hashlib
import io
from copy import deepcopy

import pytest
from PIL import Image

from app.cad_agent_store import CadRunStore
from app.cad_file_access import CadFileAccess
from app.cad_source_preview import SourcePreviewError, source_documents, source_preview
from .test_cad_agent_api import api, run_result


def image_bytes(*, exif=False):
    image = Image.new("RGB", (180, 90), "white")
    image.paste("black", (10, 12, 40, 25))
    output = io.BytesIO()
    if exif:
        metadata = Image.Exif()
        metadata[274] = 6
        image.save(output, format="JPEG", exif=metadata)
    else:
        image.save(output, format="PNG")
    return output.getvalue()


def upload(client, payload, filename="drawing.png", mime="image/png"):
    return run_result(client.post("/api/v1/cad-agent/run", data={"message": "build from drawing"},
                                  files={"files": (filename, payload, mime)}))


def source_identity(documents):
    """The saved file identity is stable; each fetch may renew its capability."""
    result = deepcopy(documents)
    for document in result:
        document["downloadUrl"] = document["downloadUrl"].split("?", 1)[0]
        for page in document["pages"]:
            page["url"] = page["url"].split("?", 1)[0]
    return result


def test_transparent_drawing_preview_uses_the_readers_white_background(api):
    client, _, _, _ = api
    image = Image.new("RGBA", (40, 20), (0, 0, 0, 0))
    image.paste((0, 0, 0, 255), (10, 5, 20, 15))
    output = io.BytesIO()
    image.save(output, format="PNG")
    result = upload(client, output.getvalue())
    response = client.get(result["sourceDocuments"][0]["pages"][0]["url"])
    assert response.status_code == 200
    with Image.open(io.BytesIO(response.content)) as preview:
        assert preview.getpixel((0, 0)) == (255, 255, 255)
        assert preview.getpixel((15, 10)) == (0, 0, 0)


def test_source_links_survive_revision_and_restart_with_exact_uploaded_bytes(api):
    client, store, _, _ = api
    payload = image_bytes()
    first = upload(client, payload, "客户图纸.png")
    source = first["sourceDocuments"][0]
    page = source["pages"][0]
    assert source["filename"] == "客户图纸.png"
    assert source["sha256"] == page["preparedSha256"] == hashlib.sha256(payload).hexdigest()
    assert (page["width"], page["height"], page["preparedFileIndex"]) == (180, 90, 0)
    assert page["preparedRegion"] == [0, 0, 1, 1]
    assert client.get(source["downloadUrl"]).content == payload
    assert "attachment" in client.get(source["downloadUrl"]).headers["content-disposition"]
    response = client.get(page["url"])
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(response.content)).size == (180, 90)
    second = client.post("/api/v1/cad-agent/confirm", json={"runId": first["runId"], "revision": 1}).json()
    assert second["revision"] == 2
    assert "/2/sources/" in second["sourceDocuments"][0]["downloadUrl"]
    assert client.get(page["url"]).content == response.content
    old = client.get(f"/api/v1/cad-agent/runs/{first['runId']}?revision=1").json()
    assert source_identity(old["sourceDocuments"]) == source_identity(first["sourceDocuments"])
    restarted = CadRunStore(store.root)
    stored = restarted.load(first["runId"], 1)
    access = CadFileAccess(restarted, allow_anonymous=True)
    restored = access.protect_result(stored, {"sourceDocuments": source_documents(restarted, stored, "/api/v1")}, "/api/v1")["sourceDocuments"]
    assert source_identity(restored) == source_identity(first["sourceDocuments"])
    assert client.get(restored[0]["downloadUrl"]).content == payload
    assert client.get(restored[0]["pages"][0]["url"]).content == response.content
    assert "path" not in str(restored)


def test_source_preview_preserves_reader_orientation_and_is_lazy_cached(api, monkeypatch):
    client, store, _, _ = api
    first = upload(client, image_bytes(exif=True), "phone.jpg", "image/jpeg")
    page = first["sourceDocuments"][0]["pages"][0]
    directory = store.directory(first["runId"]) / "source-previews"
    assert list(directory.rglob("page-1.png")) == []
    response = client.get(page["url"])
    assert Image.open(io.BytesIO(response.content)).size == (180, 90)
    assert len(list(directory.rglob("page-1.png"))) == 1
    monkeypatch.setattr(Image.Image, "save", lambda *args, **kwargs: pytest.fail("cached preview must not be rendered again"))
    assert client.get(page["url"]).content == response.content
    assert client.get(f"/api/v1/cad-agent/runs/{first['runId']}").status_code == 200


def test_pdf_pages_map_to_the_same_prepared_contact_sheet_without_eager_render(api, monkeypatch):
    fitz = pytest.importorskip("fitz")
    from app import pdf_preprocessor
    document = fitz.open()
    document.new_page(width=300, height=200).insert_text((20, 30), "Dimension 40")
    document.new_page(width=200, height=300).insert_text((20, 30), "Dimension 25")
    payload = document.tobytes()
    document.close()
    client, store, _, _ = api
    original = pdf_preprocessor.preprocess_pdf
    calls = []
    def counted(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(pdf_preprocessor, "preprocess_pdf", counted)
    first = upload(client, payload, "drawing.pdf", "application/pdf")
    pages = first["sourceDocuments"][0]["pages"]
    assert len(pages) == 2 and calls == []
    prepared = original(payload, "drawing.pdf")
    image = Image.open(io.BytesIO(prepared.png_bytes))
    # The contact sheet has a 24 pixel gap; each region excludes that gap.
    assert pages[0]["preparedRegion"][1] == 0
    assert pages[1]["preparedRegion"][1] > pages[0]["preparedRegion"][3]
    assert sum(page["preparedRegion"][3] for page in pages) == pytest.approx(1 - 24 / image.height)
    for index, page in enumerate(pages):
        response = client.get(page["url"])
        assert response.status_code == 200
        preview = Image.open(io.BytesIO(response.content))
        assert preview.size == (page["width"], page["height"])
        assert calls[-1]["config"].selected_pages == (index,)
    assert len(calls) == 2


def test_sources_require_same_access_as_artifacts_and_reject_path_escape(api, tmp_path, monkeypatch):
    client, store, _, services = api
    first = upload(client, image_bytes())
    source = first["sourceDocuments"][0]
    monkeypatch.setenv("JOYNIU_AI_ALLOW_ANONYMOUS", "0")
    assert client.get(source["pages"][0]["url"].split("?", 1)[0]).status_code == 403
    assert client.get(source["downloadUrl"].split("?", 1)[0] + "?access=wrong").status_code == 403
    assert client.get(source["downloadUrl"]).status_code == 200
    assert client.get(source["pages"][0]["url"].replace("/pages/1?", "/pages/999?")).status_code == 403
    assert client.get(source["downloadUrl"].replace("/sources/0/", "/sources/99/")).status_code == 403
    record = store.load(first["runId"])
    outside = tmp_path / "outside.png"
    outside.write_bytes(image_bytes())
    record["files"][0]["path"] = str(outside)
    record["revision"] = 2
    store.save(record, previous_revision=1)
    assert client.get(source["downloadUrl"].replace("/1/sources/", "/2/sources/")).status_code == 403
    token = CadFileAccess(store, allow_anonymous=True).issue(record, "sources/0/download")
    assert client.get(f"/api/v1/cad-agent/runs/{first['runId']}/2/sources/0/download?access={token}").status_code == 404
    assert source_documents(store, record, "/api/v1")[0]["pages"] == []


def test_unreadable_source_keeps_download_without_fabricating_preview(api):
    client, _, _, _ = api
    source = upload(client, b"invalid image bytes")["sourceDocuments"][0]
    assert source["pages"] == []
    assert source["error"] == "source_preview_unavailable"
    assert client.get(source["downloadUrl"]).content == b"invalid image bytes"


def test_dxf_preview_reuses_layout_renderer_and_dwg_failure_preserves_original(api, monkeypatch):
    from types import SimpleNamespace
    import ezdxf
    from app import dwg_preprocessor
    client, _, _, _ = api
    drawing = ezdxf.new()
    drawing.modelspace().add_line((0, 0), (10, 10))
    stream = io.StringIO()
    drawing.write(stream)
    payload = stream.getvalue().encode()
    calls = []
    def render(document, output_path, config):
        calls.append(len(document.modelspace()))
        return image_bytes(), 1, 0
    monkeypatch.setattr(dwg_preprocessor, "_render_document", render)
    result = upload(client, payload, "drawing.dxf", "application/dxf")
    source = result["sourceDocuments"][0]
    assert source["pages"][0]["preparedFileIndex"] is None
    assert source["pages"][0]["preparedSha256"] is None
    assert source["pages"][0]["preparedRegion"] is None
    assert calls == []
    assert client.get(source["pages"][0]["url"]).status_code == 200
    assert client.get(source["pages"][0]["url"]).status_code == 200
    assert calls == [1]
    refreshed = client.get(f"/api/v1/cad-agent/runs/{result['runId']}").json()
    assert refreshed["sourceDocuments"][0]["pages"][0]["width"] == 180
    def unavailable(*args, **kwargs):
        raise ValueError("converter unavailable at /private/local/path")
    monkeypatch.setattr(dwg_preprocessor, "preprocess_dwg", unavailable)
    result = upload(client, b"AC1027invalid", "drawing.dwg", "application/acad")
    source = result["sourceDocuments"][0]
    failed = client.get(source["pages"][0]["url"])
    assert failed.status_code == 404 and "/private" not in failed.text
    refreshed = client.get(f"/api/v1/cad-agent/runs/{result['runId']}").json()
    assert refreshed["sourceDocuments"][0]["pages"][0]["url"] == source["pages"][0]["url"]
    assert refreshed["sourceDocuments"][0]["error"] == "source_preview_unavailable"
    assert client.get(source["downloadUrl"]).content == b"AC1027invalid"
    monkeypatch.setattr(dwg_preprocessor, "preprocess_dwg", lambda *args, **kwargs: SimpleNamespace(png_bytes=image_bytes()))
    assert client.get(source["pages"][0]["url"]).status_code == 200
    refreshed = client.get(f"/api/v1/cad-agent/runs/{result['runId']}").json()
    assert "error" not in refreshed["sourceDocuments"][0]


def test_metadata_retry_recovers_and_dxf_original_hash_is_not_an_image_identity(api, monkeypatch):
    from app import cad_source_preview
    client, store, _, _ = api
    actual_metadata = cad_source_preview._metadata
    def missing_parser(*args, **kwargs):
        raise ImportError("parser unavailable")
    monkeypatch.setattr(cad_source_preview, "_metadata", missing_parser)
    first = upload(client, image_bytes())
    assert first["sourceDocuments"][0]["pages"] == []
    monkeypatch.setattr(cad_source_preview, "_metadata", actual_metadata)
    recovered = client.get(f"/api/v1/cad-agent/runs/{first['runId']}").json()
    assert len(recovered["sourceDocuments"][0]["pages"]) == 1
    assert "error" not in recovered["sourceDocuments"][0]
    dxf = upload(client, b"DXF bytes", "drawing.dxf", "application/dxf")
    record = store.load(dxf["runId"])
    digest = record["files"][0]["sha256"]
    record["sourceTranscription"] = {"preparedFiles": [{"fileIndex": 0, "sha256": digest}]}
    page = source_documents(store, record, "/api/v1")[0]["pages"][0]
    assert page["preparedSha256"] is None and page["preparedFileIndex"] is None and page["preparedRegion"] is None


def test_customer_parameter_source_retains_drawing_reference_across_api_round_trip(api, monkeypatch):
    import copy
    client, store, agent, _ = api
    base_run = agent.run
    source = {"type": "user", "confirmedAt": "2026-09-09T00:00:00Z",
              "drawingSource": {"type": "drawing", "annotationIds": ["annotation-7"], "fileIndex": 0, "text": "40"}}
    def with_source(**kwargs):
        result = base_run(**kwargs)
        result["plan"]["parameters"]["length"]["source"] = copy.deepcopy(source)
        # The fixture reviewer binds its check to the exact current plan.
        from .test_cad_agent_api import independent_review
        result["drawingReview"]["independentReview"] = independent_review(result["plan"])
        return result
    monkeypatch.setattr(agent, "run", with_source)
    first = upload(client, image_bytes())
    assert first["plan"]["parameters"]["length"]["source"] == source
    rejected = client.post("/api/v1/cad-agent/confirm", json={"runId": first["runId"], "revision": 1, "parameters": {"length": 42}})
    assert rejected.status_code == 422
    assert store.load(first["runId"])["plan"]["parameters"]["length"]["source"] == source
    confirmed = client.post("/api/v1/cad-agent/confirm", json={"runId": first["runId"], "revision": 1}).json()
    assert confirmed["plan"]["parameters"]["length"]["source"] == source
