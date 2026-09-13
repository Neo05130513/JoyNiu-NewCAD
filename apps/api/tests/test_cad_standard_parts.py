"""Exact catalogue structure, internal geometry and STEP preservation."""
import copy
import math
from pathlib import Path

import cadquery as cq
import pytest
from app.cad_plan import PlanValidationError, validate_plan, resolve_parameters
from app.cad_executor import build_plan_shape
from app.cad_standard_parts import CATALOG, build_standard_part


def feature(key):
    return {"id": "part", "op": "standard_part", "catalogId": key, "dimensions": copy.deepcopy(CATALOG[key]["dimensions"])}


def plan(key):
    return {"version": "cad-plan-v1", "units": "mm", "features": [feature(key)], "result": "part"}


@pytest.mark.parametrize("key", CATALOG)
def test_all_selectable_parts_have_complete_structure_and_real_step_roundtrip(key, tmp_path):
    value, _, _ = build_plan_shape(plan(key)); shape = value.val(); d = CATALOG[key]["dimensions"]
    solids = shape.Solids(); expected = int(d["ballCount"])+3 if "ballCount" in d else 1
    assert shape.isValid() and len(solids) == expected
    bounds = shape.BoundingBox()
    assert [bounds.xlen, bounds.ylen, bounds.zlen] == pytest.approx([d.get("headDiameter", d["outerDiameter"])]*2 + [d["length"]+d.get("headLength",0)], abs=1e-4)
    path = tmp_path / f"{key}.step"; cq.exporters.export(shape, str(path))
    reopened = cq.importers.importStep(str(path)).val()
    assert reopened.isValid() and len(reopened.Solids()) == expected
    assert reopened.Volume() == pytest.approx(shape.Volume(), rel=2e-6)
    if "ballCount" in d:
        # 3-dimensional material, no hidden collisions between ring/ball/cage.
        sphere_solids = [solid for solid in solids if any(face.geomType() == "SPHERE" for face in solid.Faces()) and len(solid.Faces()) == 1]
        assert len(sphere_solids) == d["ballCount"]
        assert all(solid.Volume() == pytest.approx(4*math.pi*(d["ballDiameter"]/2)**3/3, rel=1e-6) for solid in sphere_solids)
        assert any(face.geomType() == "TORUS" for face in solids[0].Faces())
        assert any(face.geomType() == "TORUS" for face in solids[1].Faces())
        for i, left in enumerate(solids):
            for right in solids[:i]: assert left.intersect(right).Volume() < 1e-6
    elif CATALOG[key]["kind"] == "socket_screw":
        r, p = d["outerDiameter"]/2,d["pitch"]
        # At the same radius, a half-turn moves the groove by exactly P/2;
        # stacked annular grooves would fail this helicity test.
        assert not shape.isInside((r-.3*p,0,3*p),1e-5)
        assert shape.isInside((r-.3*p,0,3.5*p),1e-5)
        assert shape.isInside((-r+.3*p,0,3*p),1e-5)
        assert not shape.isInside((-r+.3*p,0,3.5*p),1e-5)
        z=d["length"]+d["headLength"]-.5*d["socketDepth"]
        assert not shape.isInside((0,0,z),1e-5)
        assert shape.isInside((d["headDiameter"]/2-d["headChamfer"]-.1,0,z),1e-5)
        assert any(face.geomType() == "BSPLINE" for face in shape.Faces())
    elif CATALOG[key]["kind"] == "pin":
        assert any(face.geomType() == "CONE" for face in shape.Faces())
        assert shape.Volume() < math.pi*(d["outerDiameter"]/2)**2*d["length"]
    else:
        assert not shape.isInside((0,0,d["length"]/2),1e-5)
        assert shape.Volume() == pytest.approx(math.pi*(d["outerDiameter"]**2-d["innerDiameter"]**2)/4*d["length"])


@pytest.mark.parametrize("changes", [
    {"catalogId": []}, {"catalogId": "../../../file"},
    {"dimensions": {"radius": 3}}, {"extra": "ignored"},
])
def test_unknown_catalogue_fields_and_untrusted_inputs_are_rejected(changes):
    value=plan("washer-m8"); value["features"][0].update(changes)
    with pytest.raises(PlanValidationError): validate_plan(value)


@pytest.mark.parametrize("key,field,value", [("bearing-6204","ballCount",10000),("bearing-6204","ballCount",8.5),("bearing-6204","pitchDiameter",20),("socket-m6-20","pitch",.01),("socket-m6-20","socketDepth",10),("washer-m8","innerDiameter",20),("pin-6-24","endChamfer",4)])
def test_invalid_geometry_or_unbounded_work_rejected_before_kernel(key,field,value):
    f=feature(key); f["dimensions"][field]=value
    with pytest.raises(PlanValidationError): build_standard_part(None,f,float)
