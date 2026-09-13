"""Actual OCCT round trips; no fake mesh or envelope passes these tests."""
import io
import math
from pathlib import Path

import pytest

cq = pytest.importorskip("cadquery")
from app.cad_design_workspace import CadDesignWorkspace, DesignError, DesignNotFound, _assembly, _load


@pytest.fixture(scope="module")
def designs(tmp_path_factory):
    root = tmp_path_factory.mktemp("engineering-real")
    part = root / "plate.step"
    cq.exporters.export(cq.Workplane().box(40, 30, 12).faces(">Z").workplane().hole(8), str(part))
    cylinder = root / "cylinder.step"
    cq.exporters.export(cq.Workplane().cylinder(20, 5), str(cylinder))
    store = CadDesignWorkspace(root / "owned")
    plate = store.import_step("owner", "plate.step", part.read_bytes(), "file-a")
    shaft = store.import_step("owner", "shaft.stp", cylinder.read_bytes(), "file-b")
    return store, plate, shaft, root


def ref(item):
    return {"kind": item["kind"], "index": item["index"]}


def test_step_import_roundtrip_and_identity_isolation(designs):
    store, plate, _, _ = designs
    assert plate["metrics"]["solidCount"] == 1
    assert plate["metrics"]["size"] == pytest.approx([40, 30, 12], abs=1e-6)
    assert plate["metrics"]["volume"] == pytest.approx(40*30*12 - math.pi*4**2*12, rel=1e-8)
    path, _ = store.artifact("owner", plate["id"], "step")
    assert _load(path).isValid()
    glb, _ = store.artifact("owner", plate["id"], "glb")
    assert glb.read_bytes()[:4] == b"glTF"
    assert len(store.list("owner", "file-a")) == 1
    assert store.list("other") == []
    for action in (lambda: store.get("other", plate["id"]), lambda: store.artifact("other", plate["id"], "step"),
                   lambda: store.get("owner", "../../file"), lambda: store.artifact("owner", plate["id"], "../source.step")):
        with pytest.raises(DesignNotFound): action()


def test_real_topology_distance_area_radius_and_bad_indices(designs):
    store, plate, _, _ = designs
    points = [item for item in plate["topology"] if item["kind"] == "vertex"]
    first = points[0]
    second = next(item for item in points if item["center"][:2] == first["center"][:2] and item["center"][2] != first["center"][2])
    result = store.measure("owner", plate["id"], {"a": ref(first), "b": ref(second)})
    assert result["minimumDistance"] == pytest.approx(12)
    hole = next(item for item in plate["topology"] if item.get("type") == "CYLINDER")
    result = store.measure("owner", plate["id"], {"a": ref(hole)})
    assert result["a"]["diameter"] == pytest.approx(8)
    assert result["a"]["area"] == pytest.approx(2*math.pi*4*12)
    with pytest.raises(DesignError, match="拓扑编号"):
        store.measure("owner", plate["id"], {"a": {"kind":"face", "index":999999}})


def test_real_assembly_interference_step_and_bom(designs):
    store, plate, _, _ = designs
    record = store.assemble("owner", {"fileId":"assembly-file", "name":"Two plates", "instances":[
        {"id":"left", "designId":plate["id"]}, {"id":"right", "designId":plate["id"], "position":[50,0,0]}]})
    assert record["metrics"]["solidCount"] == 2
    assert record["metrics"]["volume"] == pytest.approx(plate["metrics"]["volume"] * 2)
    assert record["interference"][0]["volumeMm3"] == pytest.approx(0)
    assert record["bom"][0]["quantity"] == 2
    path, _ = store.artifact("owner", record["id"], "step")
    assert len(_load(path).Solids()) == 2
    overlap = store.assemble("owner", {"fileId":"assembly-file", "instances":[
        {"id":"left", "designId":plate["id"]}, {"id":"right", "designId":plate["id"]}]})
    assert overlap["interference"][0]["interferes"]
    assert overlap["interference"][0]["volumeMm3"] == pytest.approx(plate["metrics"]["volume"])
    with pytest.raises(DesignNotFound):
        store.assemble("other", {"fileId":"file", "instances":[{"id":"p", "designId":plate["id"]}]})


def test_concentric_coincident_distance_and_failed_constraints(designs):
    store, plate, shaft, _ = designs
    cylindrical = ref(next(item for item in shaft["topology"] if item.get("type") == "CYLINDER"))
    instances = [{"id":"fixed", "designId":shaft["id"], "fixed":True},
                 {"id":"moving", "designId":shaft["id"], "position":[25,15,30], "rotation":[15,20,0]}]
    result = store.assemble("owner", {"fileId":"fit", "instances":instances, "constraints":[
        {"kind":"concentric", "aInstance":"fixed", "bInstance":"moving", "a":cylindrical,"b":cylindrical}]})
    assert result["constraintResults"][0]["residualMm"] < .01
    planar = [item for item in plate["topology"] if item.get("type") == "PLANE"]
    left = next(item for item in planar if item["normal"][0] < -.9)
    right = next(item for item in planar if item["normal"][0] > .9)
    instances = [{"id":"fixed", "designId":plate["id"], "fixed":True}, {"id":"moving", "designId":plate["id"], "position":[50,0,0]}]
    constraint = {"kind":"coincident", "aInstance":"fixed", "bInstance":"moving", "a":ref(right), "b":ref(left)}
    result = store.assemble("owner", {"fileId":"fit", "instances":instances,"constraints":[constraint]})
    assert result["instances"][1]["position"] == pytest.approx([40,0,0], abs=.01)
    point = ref(next(item for item in plate["topology"] if item["kind"] == "vertex"))
    constraint = {"kind":"distance", "aInstance":"fixed", "bInstance":"moving", "a":point,"b":point,"value":60}
    result = store.assemble("owner", {"fileId":"fit", "instances":instances,"constraints":[constraint]})
    assert result["constraintResults"][0]["residualMm"] < .01
    instances[1]["fixed"] = True
    with pytest.raises(DesignError, match="配合"):
        store.assemble("owner", {"fileId":"fit", "instances":instances,"constraints":[constraint]})


def test_kernel_sections_dimension_dxf_pdf_and_layout_validation(designs):
    import ezdxf
    import fitz
    store, plate, _, _ = designs
    points = [item for item in plate["topology"] if item["kind"] == "vertex"]
    first = points[0]
    second = next(item for item in points if item["center"][:2] == first["center"][:2] and item["center"][2] != first["center"][2])
    result = store.drawing("owner", plate["id"], {"page":"A3","views":[{"view":"front"},{"view":"top"},{"view":"right"},{"view":"section","z":0}],
        "dimensions":[{"a":ref(first),"b":ref(second),"orientation":"vertical","viewIndex":0,"offset":10}]})
    assert result["lastDrawing"]["dimensions"][0]["value"] == pytest.approx(12)
    artifacts = result["lastDrawing"]["artifacts"]
    dxf_path, _ = store.artifact("owner",plate["id"],next(k for k in artifacts if k.endswith('.dxf')))
    doc = ezdxf.readfile(dxf_path)
    assert not doc.audit().errors
    dims = list(doc.modelspace().query("DIMENSION")); assert len(dims) == 1
    assert abs(dims[0].get_measurement()) == pytest.approx(12)
    assert len(doc.modelspace().query('LWPOLYLINE[layer=="SECTION"]')) > 0
    pdf_path, _ = store.artifact("owner",plate["id"],next(k for k in artifacts if k.endswith('.pdf')))
    with fitz.open(pdf_path) as pdf:
        assert len(pdf) == 1
        assert pdf[0].rect.width == pytest.approx(420*72/25.4, abs=.01)
        assert len(pdf[0].get_drawings()) > 20
        assert '12' in pdf[0].get_text()
        assert '工程图' in pdf[0].get_text()
        assert '单位 mm · OCCT 实体投影 · 曲线离散 0.05 mm' in pdf[0].get_text()
        fonts = pdf.get_page_fonts(0)
        assert fonts and all(font[5] == 'Identity-H' for font in fonts)
        assert all(pdf.extract_font(font[0])[-1] for font in fonts)
        # The exported document carries the real Chinese glyph outlines,
        # rather than depending on the viewer's system fonts/CMap packages.
        glyphs = {chr(char[0]): char for span in pdf[0].get_texttrace() for char in span['chars']}
        for char in '工程图单位实体投影曲线离散':
            assert glyphs[char][1] > 0
            rendered = pdf[0].get_pixmap(clip=fitz.Rect(glyphs[char][3]), colorspace=fitz.csGRAY)
            assert min(rendered.samples) < 128
        assert pdf[0].get_pixmap().width > 1000
    with pytest.raises(DesignError, match="图框"):
        store.drawing("owner",plate["id"],{"views":[{"view":"front","x":-100}]})
    with pytest.raises(DesignError, match="截面|剖切"):
        store.drawing("owner",plate["id"],{"views":[{"view":"section","z":9999}]})


def test_invalid_upload_not_saved(designs):
    store, _, _, _ = designs
    before = len(store.list("owner"))
    for name, payload in (("file.dwg",b"bad"),("file.step",b"bad"),("file.step",b"ISO-10303-21;broken;")):
        with pytest.raises(DesignError): store.import_step("owner",name,payload,"file")
    assert len(store.list("owner")) == before


def test_circle_dimensions_and_non_unit_paper_scale(designs):
    import ezdxf
    import fitz
    store,plate,_,_=designs
    circle=next(item for item in plate['topology'] if item.get('type')=='CIRCLE')
    result=store.drawing('owner',plate['id'],{'views':[{'view':'top','scale':.5}],
        'dimensions':[{'a':ref(circle),'orientation':kind,'viewIndex':0,'offset':offset} for kind,offset in [('radius',12),('diameter',20)]]})
    assert [d['value'] for d in result['lastDrawing']['dimensions']]==pytest.approx([4,8])
    artifacts=result['lastDrawing']['artifacts']
    dxf,_=store.artifact('owner',plate['id'],next(k for k in artifacts if k.endswith('.dxf')))
    doc=ezdxf.readfile(dxf);assert not doc.audit().errors
    dimensions=list(doc.modelspace().query('DIMENSION'))
    assert len(dimensions)==2
    assert [abs(dim.get_measurement()) for dim in dimensions]==pytest.approx([2,4])
    assert [dim.override().get('dimlfac') for dim in dimensions]==pytest.approx([2,2])
    pdf,_=store.artifact('owner',plate['id'],next(k for k in artifacts if k.endswith('.pdf')))
    with fitz.open(pdf) as document:
        assert 'R4' in document[0].get_text()
        assert '8' in document[0].get_text()
    with pytest.raises(DesignError,match='圆|轴'):
        store.drawing('owner',plate['id'],{'views':[{'view':'front'}],'dimensions':[{'a':ref(circle),'orientation':'diameter','viewIndex':0}]})
