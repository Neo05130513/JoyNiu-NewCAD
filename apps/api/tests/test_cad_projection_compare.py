"""Generic contour tests, without the user's drawing or any recipe fixture."""
import io
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from app.ai_proxy import AIFile
from app.cad_projection_compare import compare_drawing_projections, _projection_mask, _orientation, _orientation_concern


def png(image, name="drawing.png"):
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return AIFile(name, "image/png", stream.getvalue())


def figure(*, changed=False, width=2):
    image = Image.new("RGB", (400, 330), "white")
    draw = ImageDraw.Draw(image)
    draw.line([(45, 80), (120, 80), (120, 40), (225, 40), (225, 80), (355, 80),
               (355, 275), (45, 275), (45, 80)], fill="black", width=width)
    draw.ellipse((80, 125, 130, 175), outline="black", width=width)
    draw.ellipse((255, 125, 305, 175), outline="black", width=width)
    if changed:
        draw.line((47, 217, 353, 217), fill="black", width=width)
    return image


def sheet(*, scale=1, angle=0, width=2, repeated=False):
    part = figure(width=width)
    if scale != 1:
        part = part.resize((round(part.width*scale), round(part.height*scale)), Image.Resampling.LANCZOS)
    if angle:
        part = part.rotate(angle, Image.Resampling.BICUBIC, expand=True, fillcolor="white")
    page = Image.new("RGB", (part.width+120, (part.height+80)*(2 if repeated else 1)), "white")
    page.paste(part, (60, 35))
    if repeated:
        page.paste(part, (60, part.height+115))
    draw = ImageDraw.Draw(page)
    draw.line((25, 65, 25, min(page.height-30, 290)), fill="black", width=1)
    draw.text((7, 140), "DIM", fill="black")
    return page


def run(tmp_path, source=None, model=None, **options):
    return compare_drawing_projections(source_files=[png(source or sheet())],
        projection_files={"front": png(model or figure(), "generated-front.png")}, output_dir=tmp_path, **options)


@pytest.mark.parametrize("scale,angle,width", [(1, 0, 2), (.75, 0, 2), (1.2, .75, 3), (1, -1.5, 2)])
def test_same_contour_with_annotation_scale_scan_rotation_and_line_width_is_not_mismatch(tmp_path, scale, angle, width):
    result = run(tmp_path, source=sheet(scale=scale, angle=angle, width=width))
    assert result["status"] == "uncertain"  # contour support is not full geometry approval
    assert result["views"][0]["status"] == "supported"
    assert result["views"][0]["metrics"]["unsupportedFraction"] <= .03
    assert result["noDetectedContourDifference"] is True
    assert result["productionReady"] is False and result["scope"] == "original_drawing"


def test_extra_material_boundary_produces_localized_executable_mismatch(tmp_path):
    result = run(tmp_path, model=figure(changed=True))
    assert result["status"] == "mismatch"
    view = result["views"][0]
    assert view["reliability"] == "high" and view["status"] == "mismatch"
    assert view["differenceRegions"]
    for region in view["differenceRegions"]:
        x, y, w, h = region["sourceRegion"]
        assert min(x, y) >= 0 and min(w, h) > 0 and x+w <= 1 and y+h <= 1
    assert any(item["kind"] == "difference" for item in result["artifacts"])
    for item in result["artifacts"]:
        assert Path(item["path"]).is_relative_to(tmp_path.resolve())
        assert Image.open(item["path"]).width > 0


def test_repeated_source_view_is_ambiguous_not_a_hard_mismatch(tmp_path):
    result = run(tmp_path, source=sheet(repeated=True), model=figure(changed=True))
    assert result["status"] == "uncertain"
    assert result["views"][0]["reliability"] == "uncertain"


def test_blank_source_is_uncertain_and_does_not_report_agreement(tmp_path):
    result = run(tmp_path, source=Image.new("RGB", (500, 400), "white"))
    assert result["status"] == "uncertain"
    assert result["noDetectedContourDifference"] is False


def test_missing_source_hole_cannot_be_declared_complete_by_one_way_distance(tmp_path):
    model = figure()
    ImageDraw.Draw(model).rectangle((75, 120, 135, 180), fill="white")
    result = run(tmp_path, model=model)
    assert result["status"] != "consistent"
    assert any("不能证明" in note for note in result["limitations"])


def test_candidate_view_axes_route_only_the_corresponding_orthographic_projection(tmp_path):
    hint = {"id": "view-a", "imageId": "source-0", "kind": "orthographic", "bbox": [0, 0, 1, 1],
            "horizontalAxis": "+X", "verticalAxis": "+Z", "confidence": "high"}
    result = run(tmp_path, source_views=[hint])
    assert result["views"][0]["status"] == "supported"
    assert result["views"][0]["regionOrigin"] == "source_spatial_candidate"
    incompatible = {**hint, "horizontalAxis": "+Y"}
    result = run(tmp_path / "incompatible", source_views=[incompatible])
    assert result["views"][0]["status"] == "uncertain" and result["artifacts"] == []


def test_too_tight_candidate_crop_cannot_force_a_mismatch(tmp_path):
    hint = {"id": "partial", "imageId": "source-0", "kind": "orthographic", "bbox": [.35, .28, .15, .2],
            "horizontalAxis": "+X", "verticalAxis": "+Z"}
    result = run(tmp_path, source_views=[hint])
    assert result["status"] == "uncertain"


def test_projection_label_and_hidden_grey_lines_do_not_enter_visible_contour():
    image = figure()
    draw = ImageDraw.Draw(image)
    draw.text((5, 5), "Front | OCCT projection", fill="black")
    draw.line((15, 305, 380, 305), fill=(165, 165, 165), width=1)
    mask, box, _ = _projection_mask(image)
    assert box[1] >= 39 and box[3] <= 278
    assert mask.sum() > 500


def test_thin_contour_survives_large_downsampling(tmp_path):
    # Downsampling must not erase edges at unlucky pixel rows: formerly a
    # partial 250px remnant could win over the complete geometric contour.
    model = figure(width=1).resize((1200, 990), Image.Resampling.NEAREST)
    result = run(tmp_path, source=sheet(scale=.75, width=2), model=model)
    assert result["status"] != "mismatch"
    assert result["views"][0]["metrics"]["contourPixelCount"] > 500


@pytest.mark.parametrize("seconds", [0, float("nan"), 181, 1e-9])
def test_invalid_or_exhausted_budget_is_honest_uncertainty(tmp_path, seconds):
    result = run(tmp_path, timeout_seconds=seconds)
    assert result["status"] == "uncertain" and "errorCode" in result


def test_invalid_image_or_unknown_projection_does_not_create_fake_checks(tmp_path):
    result = compare_drawing_projections(source_files=[AIFile("bad", "image/png", b"broken")],
        projection_files={"front": png(figure())}, output_dir=tmp_path)
    assert result["status"] == "uncertain" and result["views"] == []
    result = compare_drawing_projections(source_files=[png(sheet())],
        projection_files={"unknown": png(figure())}, output_dir=tmp_path / "unknown")
    assert result["status"] == "uncertain" and result["views"] == []


@pytest.mark.parametrize("use_hint", [False, True])
def test_same_part_with_mirrored_coordinate_frame_cannot_be_a_hard_mismatch(tmp_path, use_hint):
    # This used to report high mismatch: 29.5% unsupported, 8 anchor cells.
    # The source frame is an unverified candidate, so a better reflected fit
    # must be exposed as coordinate uncertainty, not a geometry repair order.
    hint = {"id": "front-source", "imageId": "source-0", "kind": "orthographic", "bbox": [0, 0, 1, 1],
            "horizontalAxis": "+X", "verticalAxis": "+Z", "confidence": "high"}
    result = run(tmp_path, model=figure().transpose(Image.Transpose.FLIP_LEFT_RIGHT),
                 source_views=[hint] if use_hint else [])
    view = result["views"][0]
    assert result["status"] == view["status"] == "uncertain"
    assert view["reliability"] == "uncertain"
    assert view["reason"] == "source_to_cad_coordinate_frame_ambiguous"
    assert view["metrics"]["unsupportedFraction"] <= .03
    assert view["registration"]["alternativeFrame"]["verified"] is False
    assert result["noDetectedContourDifference"] is False


@pytest.mark.parametrize("transform", [Image.Transpose.ROTATE_90, Image.Transpose.ROTATE_180, Image.Transpose.ROTATE_270])
def test_same_geometry_with_quarter_turns_is_not_a_hard_mismatch(tmp_path, transform):
    result = run(tmp_path, model=figure().transpose(transform))
    assert result["status"] == "uncertain"
    assert all(view["status"] != "mismatch" for view in result["views"])


def test_source_axis_permutation_is_reported_as_frame_ambiguity_not_wrong_geometry(tmp_path):
    mirrored = figure().transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    source = Image.new("RGB", (1080, 420), "white")
    source.paste(figure(), (35, 40))
    source.paste(mirrored, (610, 40))
    hints = [{"id": "candidate-front", "imageId": "source-0", "kind": "orthographic", "bbox": [0, 0, .46, 1],
              "horizontalAxis": "+X", "verticalAxis": "+Z", "confidence": "high"},
             {"id": "candidate-top", "imageId": "source-0", "kind": "orthographic", "bbox": [.53, 0, .47, 1],
              "horizontalAxis": "+X", "verticalAxis": "+Y", "confidence": "high"}]
    result = run(tmp_path, source=source, model=mirrored, source_views=hints)
    view = result["views"][0]
    assert result["status"] == "uncertain"
    assert view["reason"] == "source_to_cad_coordinate_frame_ambiguous"
    assert view["registration"]["alternativeFrame"]["sourceViewId"] == "candidate-top"


@pytest.mark.parametrize("angle", [25, 40])
def test_round_outline_anchors_do_not_make_rotated_eccentric_holes_a_shape_mismatch(tmp_path, angle):
    # A generic circular plate: its contour anchors remain convincing at an
    # arbitrary angle, while two asymmetric holes rotate with the same part.
    source = Image.new("RGB", (440, 420), "white")
    draw = ImageDraw.Draw(source)
    draw.ellipse((70, 65, 370, 365), outline="black", width=2)
    draw.ellipse((250, 180, 300, 230), outline="black", width=2)
    draw.ellipse((140, 270, 165, 295), outline="black", width=2)
    model = source.rotate(angle, Image.Resampling.BICUBIC, fillcolor="white")
    result = run(tmp_path, source=source, model=model)
    assert result["status"] == "uncertain"
    assert result["views"][0]["reason"] == "source_to_cad_coordinate_frame_ambiguous"
    assert result["views"][0]["metrics"]["unsupportedFraction"] <= .03


def test_opposite_observation_side_does_not_infer_backside_visibility_from_a_mirror(tmp_path):
    hint = {"id": "rear", "imageId": "source-0", "kind": "orthographic", "bbox": [0, 0, 1, 1],
            "horizontalAxis": "-X", "verticalAxis": "+Z", "viewDirection": "-Y", "confidence": "high"}
    result = run(tmp_path, model=figure(changed=True).transpose(Image.Transpose.FLIP_LEFT_RIGHT), source_views=[hint])
    assert result["status"] == "uncertain"
    assert result["views"][0]["reason"] == "opposite_view_visibility_not_available"
    assert result["views"][0]["reliability"] == "uncertain"


@pytest.mark.parametrize("hint", [
    {"horizontalAxis": "+X", "verticalAxis": "+Z", "viewDirection": "-Y", "confidence": "high"},
    {"horizontalAxis": "+X", "verticalAxis": "+Z", "viewDirection": None, "reportedViewDirection": "-Y", "confidence": "uncertain"},
])
def test_conflicting_or_uncertain_source_axes_cannot_be_a_hard_mismatch(tmp_path, hint):
    hint = {"id": "uncertain-source", "imageId": "source-0", "kind": "orthographic", "bbox": [0, 0, 1, 1], **hint}
    result = run(tmp_path, model=figure(changed=True), source_views=[hint])
    assert result["status"] == "uncertain"
    assert result["views"][0]["reliability"] == "uncertain"
    assert result["views"][0]["reason"] in {"source_view_axis_conflict", "source_view_orientation_uncertain"}


def test_isometric_or_section_only_hint_is_not_silently_relabelled_as_orthographic(tmp_path):
    hint = {"id": "section", "imageId": "source-0", "kind": "section", "bbox": [0, 0, 1, 1]}
    result = run(tmp_path, model=figure(changed=True), source_views=[hint])
    assert result["status"] == "uncertain"
    assert result["views"][0]["reason"] == "no_orthographic_source_candidate"


def test_axis_sign_and_swap_math_matches_fixed_executor_camera_frames():
    import numpy as np
    mask = np.array([[1, 2, 3], [4, 5, 6]], dtype="uint8")
    cases = [("+X", "+Z", [[1, 2, 3], [4, 5, 6]], None),
             ("-X", "-Z", [[6, 5, 4], [3, 2, 1]], None),
             ("+Z", "-X", [[4, 1], [5, 2], [6, 3]], None),
             ("+Z", "+X", [[6, 3], [5, 2], [4, 1]], "opposite_view_visibility_not_available")]
    for horizontal, vertical, expected, concern in cases:
        hint = {"horizontalAxis": horizontal, "verticalAxis": vertical}
        transformed, accepted = _orientation(mask, "front", hint)
        assert accepted and transformed.tolist() == expected
        assert _orientation_concern("front", hint) == concern
