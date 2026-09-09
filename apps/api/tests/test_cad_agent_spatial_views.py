"""Spatial diagnostics reach visual reasoning without becoming planar evidence."""
import base64
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from app.cad_agent import CadAgentService, _digest, _spatial_files
from tests.test_cad_agent import (
    Executor, Provider, StubDrawingReviewer, StubSourceReader,
    StubSpatialInterpreter, context, edit, execute, finish, raster, record,
)


def spatial_descriptor(path):
    return {"pngPath": str(path), "viewType": "isometric", "scope": "spatial_diagnostic",
            "orthographicEngineeringView": False, "pixelRegistrationEligible": False,
            "productionReady": False}


def image_attachments(body):
    """Read the metadata attached to each actual provider image, not context text."""
    content = body["input"][0]["content"]
    return {json.loads(content[index-1]["text"])["filename"]: item["image_url"]
            for index, item in enumerate(content) if item["type"] == "input_image"}


def png_size(url):
    with Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))) as image:
        return image.size


class SpatialExecutor(Executor):
    def __call__(self, value, output_dir, **kwargs):
        result = super().__call__(value, output_dir, **kwargs)
        path = Path(output_dir) / "isometric.png"
        path.write_bytes(raster(50 + len(self.calls), 37).data)
        return {**result, "spatialViews": {"isometric": spatial_descriptor(path)}}


class SpatialDraft:
    def __init__(self, *, fail_second=False):
        self.calls = []
        self.fail_second = fail_second

    def __call__(self, plan, output_dir, **kwargs):
        self.calls.append(plan)
        if self.fail_second and len(self.calls) == 2:
            return {"status": "failed", "valid": False,
                    "errors": [{"code": "invalid_geometry", "message": "Draft did not build"}]}
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        views = {}
        for view in ("front", "top", "right"):
            path = output_dir / f"{view}.png"
            path.write_bytes(raster(60 + len(self.calls), 43).data)
            views[view] = {"pngPath": str(path)}
        path = output_dir / "isometric.png"
        path.write_bytes(raster(70 + len(self.calls), 47).data)
        return {"status": "succeeded", "valid": True, "errors": [],
                "inspection": {"valid": True, "kernelBacked": True, "engine": "cadquery-occt"},
                "draftViews": views, "spatialViews": {"isometric": spatial_descriptor(path)}}


def service(provider, **kwargs):
    return CadAgentService(provider_call=provider, source_reader=StubSourceReader(),
                           spatial_interpreter=StubSpatialInterpreter(),
                           drawing_reviewer=kwargs.pop("drawing_reviewer", StubDrawingReviewer()),
                           projection_comparer=kwargs.pop("projection_comparer", lambda **kw: {
                               "scope": "original_drawing", "status": "uncertain", "views": []}),
                           **kwargs)


def test_spatial_image_requires_explicit_nonorthographic_flags_and_current_output_path(tmp_path):
    directory = tmp_path / "current"
    directory.mkdir()
    path = directory / "isometric.png"
    path.write_bytes(raster().data)
    images, metadata = _spatial_files({"spatialViews": {"isometric": spatial_descriptor(path)}}, directory)
    assert len(images) == len(metadata) == 1
    assert images[0].filename == "generated-isometric.png"
    assert metadata[0]["scope"] == "spatial_diagnostic"
    assert metadata[0]["pixelRegistrationEligible"] is False


@pytest.mark.parametrize("field,value", [
    ("viewType", None), ("viewType", "front"),
    ("orthographicEngineeringView", None), ("orthographicEngineeringView", True),
    ("pixelRegistrationEligible", None), ("pixelRegistrationEligible", True),
])
def test_unlabelled_or_planar_image_cannot_be_used_as_spatial_diagnostic(tmp_path, field, value):
    path = tmp_path / "isometric.png"
    path.write_bytes(raster().data)
    descriptor = spatial_descriptor(path)
    if value is None:
        descriptor.pop(field)
    else:
        descriptor[field] = value
    assert _spatial_files({"spatialViews": {"isometric": descriptor}}, tmp_path) == ((), [])


@pytest.mark.parametrize("symlink", [False, True])
def test_spatial_diagnostic_cannot_read_external_or_symlinked_file(tmp_path, symlink):
    directory = tmp_path / "current"
    directory.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(raster().data)
    path = outside
    if symlink:
        path = directory / "isometric.png"
        path.symlink_to(outside)
    assert _spatial_files({"spatialViews": {"isometric": spatial_descriptor(path)}}, directory) == ((), [])


def test_real_execute_sends_spatial_diagnostic_to_next_turn_and_reviewer_but_not_pixel_comparer(tmp_path):
    from app.geometry import cadquery_status
    if not cadquery_status()["available"]:
        pytest.skip("CadQuery unavailable")
    compared, reviewed, seen = [], [], []

    def compare(**kwargs):
        assert {item.filename for item in kwargs["projection_files"]} == {
            "generated-front.png", "generated-top.png", "generated-right.png"}
        assert "spatial_files" not in kwargs
        compared.append(kwargs)
        return {"scope": "original_drawing", "status": "uncertain", "views": []}

    def review(**kwargs):
        assert len(kwargs["projection_files"]) == 3
        assert len(kwargs["spatial_files"]) == 1
        assert kwargs["spatial_files"][0].filename == "generated-isometric.png"
        assert kwargs["spatial_files"][0].data.startswith(b"\x89PNG")
        assert not {"plan", "observations", "transcription"}.intersection(kwargs)
        reviewed.append(kwargs)
        return {"status": "consistent", "observations": ["Synthetic fixture reviewed."],
                "differences": [], "questions": []}

    def next_action(body):
        images = image_attachments(body)
        assert set(images) == {"source.png", "generated-front.png", "generated-top.png",
                               "generated-right.png", "generated-isometric.png"}
        assert png_size(images["generated-isometric.png"])[0] > 100
        assert any("Do not treat this as an orthographic dimension projection" in item.get("text", "")
                   for item in body["input"][0]["content"])
        seen.append(True)
        return finish()

    result = service(Provider([record(), execute(), next_action]),
                     drawing_reviewer=review, projection_comparer=compare).run(
        message="Build a 10 by 20 by 30 mm block", files=[raster()], output_dir=tmp_path)
    assert result["status"] == "review_required", result
    assert len(compared) == len(reviewed) == len(seen) == 1
    assert set(result["artifacts"]["views"]) == {"front", "top", "right"}


@pytest.mark.parametrize("failed_recheck", [False, True])
def test_parameter_edit_replaces_actual_and_old_draft_spatial_images(tmp_path, failed_recheck):
    draft = SpatialDraft(fail_second=failed_recheck)
    executor, seen = SpatialExecutor(), []

    def change_parameter(body):
        images = image_attachments(body)
        assert "draft-isometric.png" not in images
        assert png_size(images["generated-isometric.png"]) == (51, 37)
        seen.append("executed")
        return edit(31)

    def check_fresh_draft(body):
        images = image_attachments(body)
        current = context(body)
        assert "generated-isometric.png" not in images
        assert current["currentExecutionSummary"]["hasFreshValidGeometry"] is False
        assert current["currentDraftInspection"]["planHash"] == _digest(current["currentPlan"])
        if failed_recheck:
            assert "draft-isometric.png" not in images
            assert current["currentDraftInspection"]["spatialImages"] == []
        else:
            assert png_size(images["draft-isometric.png"]) == (72, 47)
            assert len(current["currentDraftInspection"]["spatialImages"]) == 1
        seen.append("rechecked")
        return finish()

    result = service(Provider([record(), edit(), {"action": "execute_plan", "message": "Execute saved draft"},
                               change_parameter, check_fresh_draft]),
                     executor=executor, draft_inspector=draft).run(
        message="Build a block, then change height", output_dir=tmp_path, max_turns=5)
    assert seen == ["executed", "rechecked"]
    assert result["status"] == "failed"  # A draft check cannot authorize completion.
    assert result["inspection"] is None and result["artifacts"] == {}
    assert len(draft.calls) == 2 and len(executor.calls) == 1


def test_changed_measurement_ledger_cannot_resurrect_previous_partial_spatial_images(tmp_path):
    seen = []

    def verify_invalidated(body):
        current = context(body)
        images = image_attachments(body)
        assert current["sourceObservations"][0]["expected"] == 31
        assert current["currentExecutionSummary"]["hasFreshValidGeometry"] is False
        assert current["currentDraftInspection"] is None
        assert "generated-isometric.png" not in images
        assert "draft-isometric.png" not in images
        assert not any(name.startswith(("generated-", "draft-")) for name in images)
        seen.append(True)
        return finish()

    provider = Provider([record(), edit(), {"action": "execute_plan", "message": "Execute saved draft"},
                         {"action": "inspect_source", "message": "Recheck source dimension",
                          "source": {"fileIndex": 0, "crop": [0, 0, 0.5, 1], "rotation": 0}},
                         record(31), verify_invalidated])
    result = service(provider, executor=SpatialExecutor(), draft_inspector=SpatialDraft()).run(
        message="Build a block from the drawing", files=[raster()], output_dir=tmp_path, max_turns=6)
    assert seen == [True]
    assert result["status"] == "failed"
    assert result["inspection"] is None and result["artifacts"] == {}
