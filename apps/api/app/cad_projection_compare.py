"""Bounded, deterministic comparison of source ink and executed CAD edges.

The source drawing may contain annotations. Therefore a small one-way contour
distance is supporting evidence, never a proof that all source features exist.
Only well registered, spatially supported contradictions are called mismatch.
No dimensions, recipes, reference solids or model-generated expected values
are used by this module.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
from pathlib import Path
import time
from typing import Any, Iterable, Mapping

from .ai_proxy import AIFile
from .cad_source_reader import _ink_regions

VERSION = "cad-projection-compare-v2"
MAX_PIXELS = 80_000_000
MAX_TOTAL_PIXELS = 160_000_000
MAX_BYTES = 60 * 1024 * 1024
WORKING_SIDE = 600
AXES = {"front": ("X", "Z"), "top": ("X", "Y"), "right": ("Y", "Z")}
VIEW_RAYS = {"front": "+Y", "top": "-Z", "right": "-X"}
SIGNED_AXES = {sign+axis for sign in ("+", "-") for axis in "XYZ"}


def _raster(item: AIFile):
    from PIL import Image
    if not isinstance(item, AIFile) or not 0 < len(item.data) <= 20 * 1024 * 1024:
        raise ValueError("invalid image bytes")
    with Image.open(io.BytesIO(item.data)) as opened:
        if opened.width * opened.height > MAX_PIXELS:
            raise ValueError("image pixel limit")
        rgba = opened.convert("RGBA")
        image = Image.new("RGB", rgba.size, "white")
        image.paste(rgba, mask=rgba.getchannel("A"))
        return image


def _projection_mask(image):
    import cv2
    import numpy as np
    sample = image.copy()
    sample.thumbnail((960, 960))
    ink = (np.asarray(sample.convert("L")) < 120).astype("uint8")
    # cad_inspector._projection_png reserves this header for its caption;
    # it is not geometry. Preserve that renderer contract when resized.
    caption_rows = max(1, round(32 * sample.height / image.height))
    ink[:caption_rows] = 0
    count, labels, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
    # Executor projection titles are small disconnected letters; retain long
    # visible contours and holes. Hidden grey strokes are excluded by tone.
    mask = np.zeros_like(ink)
    minimum_span = max(12, max(ink.shape) * .02)
    for index in range(1, count):
        _, _, width, height, area = stats[index]
        if max(width, height) >= minimum_span and area >= 8:
            mask[labels == index] = 1
    ys, xs = np.where(mask)
    if len(xs) < 40 or xs.max() - xs.min() < 15 or ys.max() - ys.min() < 15:
        raise ValueError("projection has too few resolved edges")
    box = [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1]
    return mask[box[1]:box[3], box[0]:box[2]], box, sample.size


def _orientation(mask, view, hint):
    horizontal, vertical = hint.get("horizontalAxis"), hint.get("verticalAxis")
    if not isinstance(horizontal, str) or not isinstance(vertical, str):
        return mask, False
    if horizontal not in SIGNED_AXES or vertical not in SIGNED_AXES:
        return mask, False
    axes = AXES[view]
    if {horizontal[1:], vertical[1:]} != set(axes):
        return None, False
    # Image y points down, while the reported engineering vertical points up.
    if horizontal[1:] != axes[0]:
        mask = mask.T[::-1, ::-1]
    if horizontal[0] == "-":
        mask = mask[:, ::-1]
    if vertical[0] == "-":
        mask = mask[::-1, :]
    return mask.copy(), True


def _orientation_concern(view, hint):
    """A reflected image cannot reconstruct visibility from the opposite side."""
    if hint.get("nonOrthographicOnly"):
        return "no_orthographic_source_candidate"
    if hint.get("confidence") == "low" or hint.get("confidence") == "uncertain" or hint.get("reportedViewDirection"):
        return "source_view_orientation_uncertain"
    horizontal, vertical = hint.get("horizontalAxis"), hint.get("verticalAxis")
    if not isinstance(horizontal, str) or horizontal not in SIGNED_AXES:
        return None
    if not isinstance(vertical, str) or vertical not in SIGNED_AXES:
        return None
    axes = AXES[view]
    if {horizontal[1:], vertical[1:]} != set(axes):
        return "source_projection_axes_differ"
    determinant = (1 if horizontal[0] == vertical[0] else -1) * (1 if horizontal[1:] == axes[0] else -1)
    expected_ray = VIEW_RAYS[view] if determinant == 1 else ("-" if VIEW_RAYS[view][0] == "+" else "+")+VIEW_RAYS[view][1:]
    if hint.get("viewDirection") and hint["viewDirection"] != expected_ray:
        return "source_view_axis_conflict"
    if determinant == -1:
        return "opposite_view_visibility_not_available"
    return None


def _source_regions(image, index, hints):
    width, height = image.size
    entries = []
    for hint in hints:
        if not isinstance(hint, Mapping) or hint.get("imageId") != f"source-{index}" or hint.get("kind") != "orthographic":
            continue
        box = hint.get("bbox")
        if not isinstance(box, list) or len(box) != 4 or any(type(x) not in (int, float) or not math.isfinite(x) for x in box):
            continue
        x, y, w, h = box
        if min(x, y) < 0 or min(w, h) <= 0 or x + w > 1.000001 or y + h > 1.000001:
            continue
        # A candidate bounding box is not a certified boundary: leave room
        # for scan skew, imperfect model cropping and dimension leaders.
        pad_x, pad_y = w * .08, h * .08
        pixels = (int(max(0, x-pad_x)*width), int(max(0, y-pad_y)*height),
                  int(min(1, x+w+pad_x)*width), int(min(1, y+h+pad_y)*height))
        entries.append((pixels, dict(hint), "source_spatial_candidate"))
    if not entries:
        boxes = _ink_regions(image) or [(0, 0, width, height)]
        named_views = [hint for hint in hints if isinstance(hint, Mapping) and hint.get("imageId") == f"source-{index}"]
        fallback_hint = {"nonOrthographicOnly": True} if named_views and all(hint.get("kind") != "orthographic" for hint in named_views) else {}
        entries = [(box, fallback_hint, "whitespace_region_candidate") for box in boxes]
    result = []
    for box, hint, origin in entries[:4]:
        crop = image.crop(box)
        crop.thumbnail((WORKING_SIDE, WORKING_SIDE))
        result.append({"image": crop, "box": list(box), "sourceSize": [width, height], "hint": hint,
                       "origin": origin, "imageId": f"source-{index}", "sourceIndex": index})
    return result


def _fit(region, mask, deadline, *, scale_hint=None):
    import cv2
    import numpy as np
    gray = np.asarray(region["image"].convert("L"))
    ink = (gray < 145).astype("uint8")
    if ink.sum() < 50 or ink.mean() > .35:
        return None
    distance = cv2.distanceTransform(1-ink, cv2.DIST_L2, 3)
    thickness = cv2.distanceTransform(ink, cv2.DIST_L2, 3)
    line_width = max(1.0, 2 * float(np.quantile(thickness[ink > 0], .7)))
    height, width = ink.shape
    cap = max(5, min(height, width)*.025)
    cost_image = np.minimum(distance, cap).astype("float32")
    size = np.array([mask.shape[1], mask.shape[0]])
    max_scale = min((width-2)/size[0], (height-2)/size[1])
    best = None
    poses = []
    def attempt(scale, angle):
        nonlocal best
        if time.monotonic() >= deadline:
            raise TimeoutError("projection comparison deadline")
        tw, th = np.maximum(2, np.rint(size*scale).astype(int))
        if tw >= width or th >= height or min(tw, th) < 16:
            return
        # Nearest-neighbour downsampling can erase a thin complete CAD edge
        # between sampling rows. Area coverage preserves the entire contour.
        template = (cv2.resize(mask.astype("float32"), (int(tw), int(th)), interpolation=cv2.INTER_AREA) > .04).astype("uint8")
        if angle:
            center = ((tw-1)/2, (th-1)/2)
            rotation = cv2.getRotationMatrix2D(center, angle, 1)
            diagonal = math.hypot(tw, th)
            pad = int(math.ceil(diagonal * abs(math.sin(math.radians(angle)))))+2
            template = cv2.copyMakeBorder(template, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=0)
            rotation = cv2.getRotationMatrix2D(((template.shape[1]-1)/2, (template.shape[0]-1)/2), angle, 1)
            template = cv2.warpAffine(template, rotation, (template.shape[1], template.shape[0]), flags=cv2.INTER_NEAREST)
        if template.shape[0] >= height or template.shape[1] >= width or template.sum() < 40:
            return
        costs = cv2.matchTemplate(cost_image, template.astype("float32"), cv2.TM_CCORR) / template.sum()
        score, _, xy, _ = cv2.minMaxLoc(costs)
        poses.append((float(score), float(scale), xy, template.shape))
        if best is None or score < best["score"]:
            best = {"score": float(score), "scale": float(scale), "rotationDegrees": float(angle),
                    "xy": list(xy), "template": template}
    scales = np.linspace(max_scale*.48, max_scale, 40) if scale_hint is None else np.linspace(scale_hint*.96, scale_hint*1.04, 5)
    for scale in scales:
        attempt(scale, 0)
    if best is None:
        return None
    if scale_hint is None:
        seed = best["scale"]
        for scale in np.linspace(seed*.975, seed*1.025, 7):
            for angle in (-2.0, -.75, 0, .75, 2.0):
                attempt(scale, angle)
    x, y = best["xy"]
    template = best["template"]
    center = (x+template.shape[1]/2, y+template.shape[0]/2)
    alternative = [score for score, scale, xy, shape in poses
                   if abs(scale/best["scale"]-1) > .12
                   or math.dist(center, (xy[0]+shape[1]/2, xy[1]+shape[0]/2)) > .15*math.hypot(*template.shape)]
    ys, xs = np.where(template)
    values = distance[ys+y, xs+x]
    # Source line width and plot resolution, rather than held-out part IDs,
    # define the tolerance. It is pixels, never manufacturing millimetres.
    tolerance = max(2.0, line_width*1.75, math.hypot(template.shape[0], template.shape[1])*.004)
    supported = values <= tolerance
    inlier_fraction = float(supported.mean())
    spread = [float(np.ptp(xs[supported]))/max(1, template.shape[1]-1),
              float(np.ptp(ys[supported]))/max(1, template.shape[0]-1)] if supported.any() else [0, 0]
    # Anchors on multiple sides constrain registration even when a local
    # curve is wrong. Mere overlap around one hole is insufficient.
    occupied = {(min(2, int(px*3/template.shape[1])), min(2, int(py*3/template.shape[0])))
                for px, py in zip(xs[supported], ys[supported])}
    residual = np.zeros_like(template)
    residual[ys[~supported], xs[~supported]] = 1
    count, labels, stats, _ = cv2.connectedComponentsWithStats(residual, 8)
    clusters = sorted((row.tolist() for row in stats[1:]), key=lambda row: row[4], reverse=True)
    largest = float(clusters[0][4])/len(xs) if clusters else 0.0
    margin = min(x+xs.min(), y+ys.min(), width-1-(x+xs.max()), height-1-(y+ys.max()))
    best.update({"region": region, "distance": distance, "residual": residual,
                 "alternativePoseSeparation": (min(alternative)-best["score"])/max(1, tolerance) if alternative else None,
                 "lineWidthPixels": line_width, "tolerancePixels": tolerance,
                 "unsupportedFraction": 1-inlier_fraction, "inlierFraction": inlier_fraction,
                 "supportedSpread": spread, "anchorGridCells": len(occupied), "boundaryMarginPixels": float(margin),
                 "p90DistancePixels": float(np.quantile(values, .9)), "meanDistancePixels": float(values.mean()),
                 "largestResidualFraction": largest, "residualClusters": clusters[:4], "contourPixelCount": len(xs)})
    return best


def _assessment(candidates):
    candidates.sort(key=lambda item: item["score"])
    best = candidates[0]
    second = candidates[1]["score"] if len(candidates) > 1 else None
    # Equal fits in multiple regions are not evidence of which view we saw.
    separation = None if second is None else (second-best["score"])/max(1, best["tolerancePixels"])
    reliable = (best["inlierFraction"] >= .60 and min(best["supportedSpread"]) >= .70
                and best["anchorGridCells"] >= 5 and best["boundaryMarginPixels"] >= 1
                and (best["alternativePoseSeparation"] is None or best["alternativePoseSeparation"] >= .10)
                and (separation is None or separation >= .12)
                and not best.get("orientationConcern"))
    mismatch = (reliable and best["unsupportedFraction"] >= .10
                and best["p90DistancePixels"] > best["tolerancePixels"]*1.5
                and best["largestResidualFraction"] >= .025)
    supported = reliable and best["unsupportedFraction"] <= .03
    return best, separation, reliable, mismatch, supported


def _coordinate_ambiguity(mask, regions, candidates, deadline):
    """Try bounded orthogonal frame alternatives before asserting a mismatch.

    Source axes are independent AI candidates, not a verified transform into
    the executed CAD frame. A substantially supported alternative is evidence
    of ambiguity, not permission to silently rotate/relabel the model or call
    it correct. Only the original mismatch thresholds decide when to run this
    diagnostic; all eight 2-D orthogonal transforms use the same fit criteria.
    """
    import numpy as np
    assessed = _assessment(candidates)
    if not assessed[3]:
        return candidates
    def ambiguous(fitted, *, degrees, mirrored):
        region = fitted["region"]
        fitted["orientationConcern"] = "source_to_cad_coordinate_frame_ambiguous"
        fitted["alternativeFrame"] = {"rotationDegreesCounterclockwise": degrees, "horizontalMirror": mirrored,
            "sourceViewId": region["hint"].get("id"),
            "sourceHorizontalAxis": region["hint"].get("horizontalAxis"),
            "sourceVerticalAxis": region["hint"].get("verticalAxis"), "verified": False}
        return [fitted]
    seen = set()
    for mirrored in (False, True):
        base = mask[:, ::-1] if mirrored else mask
        for turns in range(4):
            transformed = np.rot90(base, turns).copy()
            fingerprint = (transformed.shape, hashlib.sha256(transformed.tobytes()).hexdigest())
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            for region in regions[:12]:
                # Search across candidate source axes as well: a CAD axis
                # permutation can map its named front view to the source top.
                fitted = _fit(region, transformed, deadline)
                if fitted is None or not _assessment([fitted])[4]:
                    continue
                return ambiguous(fitted, degrees=turns*90, mirrored=mirrored)
    # A round or nearly round outline can retain many anchors at any angle,
    # while its eccentric holes rotate. Orthogonal alternatives alone do not
    # resolve this ambiguity. Scan bounded continuous in-plane alternatives
    # near the measured scale, then refine the best angular neighbourhood.
    import cv2
    def rotate(degrees):
        height, width = mask.shape
        side = int(math.ceil(math.hypot(width, height)))+4
        matrix = cv2.getRotationMatrix2D(((width-1)/2, (height-1)/2), degrees, 1)
        matrix[:, 2] += [(side-width)/2, (side-height)/2]
        rotated = cv2.warpAffine(mask, matrix, (side, side), flags=cv2.INTER_NEAREST)
        rows, columns = np.where(rotated)
        return rotated[rows.min():rows.max()+1, columns.min():columns.max()+1]
    best_rotation = None
    for degrees in range(10, 360, 10):
        if degrees % 90 == 0:
            continue
        transformed = rotate(degrees)
        for region in regions[:12]:
            fitted = _fit(region, transformed, deadline, scale_hint=assessed[0]["scale"])
            if fitted is None:
                continue
            if _assessment([fitted])[4]:
                return ambiguous(fitted, degrees=degrees, mirrored=False)
            if best_rotation is None or fitted["score"] < best_rotation[0]["score"]:
                best_rotation = (fitted, degrees)
    if best_rotation is not None:
        fitted, coarse_degrees = best_rotation
        for degrees in range(coarse_degrees-5, coarse_degrees+6):
            refined = _fit(fitted["region"], rotate(degrees), deadline, scale_hint=fitted["scale"])
            if refined is not None and _assessment([refined])[4]:
                return ambiguous(refined, degrees=degrees, mirrored=False)
    return candidates


def _report_view(view, candidates, output_dir):
    import numpy as np
    from PIL import Image, ImageDraw
    best, separation, reliable, mismatch, supported = _assessment(candidates)
    status = "mismatch" if mismatch else "supported" if supported else "uncertain"
    region = best["region"]
    x, y = best["xy"]
    template = best["template"]
    mask_y, mask_x = np.where(template)
    overlay = np.array(region["image"])
    bad = best["residual"][mask_y, mask_x].astype(bool)
    overlay[mask_y+y, mask_x+x] = np.where(bad[:, None], [220, 35, 45], [10, 150, 100])
    canvas = Image.new("RGB", (overlay.shape[1], overlay.shape[0]+32), "white")
    canvas.paste(Image.fromarray(overlay), (0, 32))
    ImageDraw.Draw(canvas).text((8, 8), f"{view}: red = unsupported CAD edges; green = source ink nearby", fill="black")
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{view}-overlay.png"
    canvas.save(path)
    artifacts = [{"view": view, "path": str(path.resolve()), "kind": "overlay"}]
    differences = []
    crop_x, crop_y, crop_right, crop_bottom = region["box"]
    for index, (left, top, width, height, count) in enumerate(best["residualClusters"]):
        if count / best["contourPixelCount"] < .01:
            continue
        source_region = [(crop_x+(left+x)/overlay.shape[1]*(crop_right-crop_x))/region["sourceSize"][0],
                         (crop_y+(top+y)/overlay.shape[0]*(crop_bottom-crop_y))/region["sourceSize"][1],
                         width/overlay.shape[1]*(crop_right-crop_x)/region["sourceSize"][0],
                         height/overlay.shape[0]*(crop_bottom-crop_y)/region["sourceSize"][1]]
        differences.append({"sourceImageId": region["imageId"], "sourceRegion": source_region,
                            "sourceRegionFrame": "prepared_source_image",
                            "projectionRegion": [left/template.shape[1], top/template.shape[0], width/template.shape[1], height/template.shape[0]],
                            "projectionRegionFrame": "registered_visible_contour",
                            "contourFraction": count/best["contourPixelCount"],
                            "finding": "该模型轮廓段在等比配准后仍无对应原图墨迹；请据叠图复查该处外轮廓、开口或圆弧基准。"})
        detail = canvas.crop((max(0, x+left-14), max(32, y+top+32-14),
                              min(canvas.width, x+left+width+14), min(canvas.height, y+top+height+32+14)))
        detail_path = output_dir / f"{view}-difference-{index+1}.png"
        detail.save(detail_path)
        artifacts.append({"view": view, "path": str(detail_path.resolve()), "kind": "difference"})
    return {"view": view, "status": status, "reliability": "high" if reliable else "uncertain",
            "reason": best.get("orientationConcern"),
            "sourceImageId": region["imageId"], "sourceRegion": [crop_x/region["sourceSize"][0], crop_y/region["sourceSize"][1],
                                                                        (crop_right-crop_x)/region["sourceSize"][0], (crop_bottom-crop_y)/region["sourceSize"][1]],
            "regionOrigin": region["origin"], "registration": {"method": "uniform_scale_translation_small_rotation",
                "scale": best["scale"], "rotationDegrees": best["rotationDegrees"], "translationPixels": best["xy"],
                "sourceWorkingSize": list(region["image"].size), "candidateSeparation": separation,
                "alternativeFrame": best.get("alternativeFrame")},
            "metrics": {key: best[key] for key in ("lineWidthPixels", "tolerancePixels", "unsupportedFraction", "inlierFraction",
                        "supportedSpread", "anchorGridCells", "boundaryMarginPixels", "p90DistancePixels", "meanDistancePixels",
                        "largestResidualFraction", "contourPixelCount", "alternativePoseSeparation")}, "differenceRegions": differences}, artifacts


def compare_drawing_projections(*, source_files: Iterable[AIFile], projection_files: Iterable[AIFile] | Mapping[str, AIFile],
                                output_dir: Path, source_views: Iterable[Mapping[str, Any]] | None = None,
                                timeout_seconds: float = 45) -> dict[str, Any]:
    """Return objective pixel discrepancies, never business export approval.

    ``source_views`` accepts source spatial contract candidate views, normalized
    to the prepared image: imageId, kind, bbox [x,y,w,h], horizontalAxis,
    verticalAxis. Paths in returned artifacts are generated in output_dir.
    """
    started = time.monotonic()
    result: dict[str, Any] = {"version": VERSION, "scope": "original_drawing", "status": "uncertain",
                              "views": [], "artifacts": [], "productionReady": False,
                              "limitations": ["一向轮廓距离不能证明源图全部特征均已建模；尺寸线或文字可能让距离低估。",
                                               "候选视图识别、未知投影方式、比例超出搜索范围、缺图或配准歧义必须继续核对。",
                                               "候选源轴不证明 CAD 坐标一致；其它正交朝向可解释差异时保留不确定，反侧可见性不能靠镜像恢复。",
                                               "像素距离不是毫米公差，也不决定用户明确改型是否允许偏离原图。"]}
    try:
        duration = float(timeout_seconds)
        if not math.isfinite(duration) or duration <= 0 or duration > 180:
            raise ValueError("invalid time budget")
        sources = tuple(source_files)
        entries = list(projection_files.items()) if isinstance(projection_files, Mapping) else [
            (Path(item.filename).stem.removeprefix("generated-"), item) for item in projection_files]
        if not 1 <= len(sources) <= 4 or not 1 <= len(entries) <= 3 or len(set(key for key, _ in entries)) != len(entries):
            raise ValueError("invalid image count")
        if any(key not in AXES for key, _ in entries):
            raise ValueError("unknown projection axes")
        if sum(len(item.data) for item in (*sources, *(item for _, item in entries))) > MAX_BYTES:
            raise ValueError("image byte limit")
        hints = list(source_views or ())
        total_pixels = 0
        regions = []
        for index, source in enumerate(sources):
            image = _raster(source)
            total_pixels += image.width*image.height
            if total_pixels > MAX_TOTAL_PIXELS:
                raise ValueError("total pixel limit")
            regions.extend(_source_regions(image, index, hints))
        result["sourceFingerprints"] = [hashlib.sha256(item.data).hexdigest() for item in sources]
        result["projectionFingerprints"] = {view: hashlib.sha256(item.data).hexdigest() for view, item in entries}
        for view, projection in entries:
            mask, _, _ = _projection_mask(_raster(projection))
            candidates = []
            for region in regions[:12]:
                oriented, _ = _orientation(mask, view, region["hint"])
                if oriented is None:
                    continue
                fitted = _fit(region, oriented, started+duration)
                if fitted is not None:
                    fitted["orientationConcern"] = _orientation_concern(view, region["hint"])
                    candidates.append(fitted)
            if not candidates:
                result["views"].append({"view": view, "status": "uncertain", "reliability": "uncertain", "reason": "no_reliable_source_registration"})
                continue
            candidates = _coordinate_ambiguity(mask, regions, candidates, started+duration)
            report, artifacts = _report_view(view, candidates, Path(output_dir))
            result["views"].append(report)
            result["artifacts"].extend(artifacts)
        if any(view["status"] == "mismatch" for view in result["views"]):
            result["status"] = "mismatch"
        result["noDetectedContourDifference"] = bool(result["views"]) and all(view["status"] == "supported" for view in result["views"])
    except (ImportError, ValueError, OSError, TimeoutError) as error:
        result["errorCode"] = "comparison_timeout" if isinstance(error, TimeoutError) else "comparison_unavailable"
        # Preserve already demonstrated contradictions if a later view times out.
        result["status"] = "mismatch" if any(view["status"] == "mismatch" for view in result["views"]) else "uncertain"
    result["elapsedSeconds"] = round(time.monotonic()-started, 3)
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    (Path(output_dir)/"projection-comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return result
