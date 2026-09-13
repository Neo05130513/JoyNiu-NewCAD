"""Direction and offset tests against non-symmetric, real OCCT solids."""
import math
import pytest

cq=pytest.importorskip('cadquery')
from app.cad_design_workspace import CadDesignWorkspace,DesignError,_section,_projection,_view_frame


@pytest.fixture(scope='module')
def asymmetric(tmp_path_factory):
    root=tmp_path_factory.mktemp('sections-real')
    # Unequal block plus a boss in only the +X/+Y quadrant: mirrors differ,
    # and changing section position materially changes the actual contour.
    solid=cq.Workplane().box(40,30,12).union(cq.Workplane().box(10,10,8).translate((12,8,10))).val()
    source=root/'asymmetric.step';cq.exporters.export(solid,str(source))
    store=CadDesignWorkspace(root/'designs')
    record=store.import_step('owner','asymmetric.step',source.read_bytes(),'section-check')
    return solid,store,record,root


def test_xyz_and_oblique_cut_plane_have_actual_distinct_areas(asymmetric):
    solid,_,_,_=asymmetric
    expected={'X':(360,[30,12]),'Y':(480,[40,12]),'Z':(1200,[40,30])}
    for axis,(area,size) in expected.items():
        section=_section(solid,{'view':'section','axis':axis,'position':0})
        assert section['metrics']['areaMm2']==pytest.approx(area)
        assert section['metrics']['sizeMm']==pytest.approx(size)
        assert any(path.get('hatch') for path in section['paths'])
    offset=_section(solid,{'view':'section','axis':'Z','position':10})
    assert offset['metrics']['areaMm2']==pytest.approx(100)
    assert offset['metrics']['sizeMm']==pytest.approx([10,10])
    zero=_section(solid,{'view':'section','axis':'custom','normal':[2,2,0],'position':0})
    assert zero['metrics']['areaMm2']==pytest.approx(360*math.sqrt(2))
    assert zero['metrics']['sizeMm']==pytest.approx([30*math.sqrt(2),12])
    shifted=_section(solid,{'view':'section','normal':[1,1,0],'position':20/math.sqrt(2)})
    assert shifted['metrics']['areaMm2']==pytest.approx(260*math.sqrt(2))
    assert shifted['frame']['normal']==pytest.approx([1/math.sqrt(2),1/math.sqrt(2),0])
    assert shifted['frame']['origin']==pytest.approx([10,10,0])
    reverse=_section(solid,{'view':'section','normal':[-1,-1,0],'position':-20/math.sqrt(2)})
    assert reverse['metrics']['areaMm2']==pytest.approx(shifted['metrics']['areaMm2'])
    assert reverse['metrics']['retainedVolumeMm3']+shifted['metrics']['retainedVolumeMm3']==pytest.approx(solid.Volume())


def test_six_orthographic_projections_and_custom_orientation(asymmetric):
    solid,_,_,_=asymmetric
    expected={'front':[40,20],'rear':[40,20],'top':[40,30],'bottom':[40,30],'right':[30,20],'left':[30,20]}
    projections={}
    for view,size in expected.items():
        points=[p for path in _projection(solid,view) for p in path['points']]
        assert [max(p[i] for p in points)-min(p[i] for p in points) for i in (0,1)]==pytest.approx(size)
        projections[view]=points
    def contains(view,point):return any(math.dist(p,point)<1e-6 for p in projections[view])
    assert contains('top',[17,13]) and not contains('top',[17,-13])
    assert contains('bottom',[17,-13]) and not contains('bottom',[17,13])
    assert contains('right',[13,14]) and contains('left',[-13,14])
    frame=_view_frame({'view':'custom','normal':[1,2,3],'horizontal':[1,0,0]})
    assert math.isclose(sum(v*v for v in frame['normal']),1)
    assert abs(sum(a*b for a,b in zip(frame['normal'],frame['horizontal'])))<1e-9
    assert _projection(solid,frame)
    assert _projection(solid,_view_frame({'view':'isometric'}))


def test_full_section_projects_actual_retained_half_solid(asymmetric):
    solid,_,_,_=asymmetric
    spec={'view':'section','axis':'Z','position':10}
    cross=_section(solid,{**spec,'sectionMode':'cross_section'})
    cut=_section(solid,{**spec,'sectionMode':'cutaway'})
    assert cross['metrics']['areaMm2']==pytest.approx(100)
    assert cut['metrics']['areaMm2']==pytest.approx(100)
    assert cut['metrics']['retainedVolumeMm3']==pytest.approx(14800)
    assert cut['metrics']['retainedSolidCount']==1
    def size(value):
        points=[p for path in value['paths'] for p in path['points']]
        return [max(p[i] for p in points)-min(p[i] for p in points) for i in (0,1)]
    assert size(cross)==pytest.approx([10,10])
    assert size(cut)==pytest.approx([40,30])  # The wider base behind the cut remains visible.
    assert solid.Volume()==pytest.approx(15200)  # Original shape is immutable.


def test_oblique_section_dimension_matches_plane_frame_and_exports(asymmetric):
    import ezdxf
    import fitz
    _,store,record,_=asymmetric
    vertices=[v for v in record['topology'] if v['kind']=='vertex']
    a=next(v for v in vertices if v['center']==[-20,-15,-6])
    b=next(v for v in vertices if v['center']==[-20,-15,6])
    ref=lambda item:{'kind':item['kind'],'index':item['index']}
    request={'page':'A3','views':[{'view':'section','axis':'custom','normal':[1,1,0],'position':0,'scale':1},
        {'view':'section','axis':'X','position':0},{'view':'section','axis':'Y','position':0},
        {'view':'section','axis':'Z','position':10},{'view':'front'},{'view':'top'},{'view':'right'}],
        'dimensions':[{'a':ref(a),'b':ref(b),'orientation':'vertical','viewIndex':0,'offset':10}]}
    result=store.drawing('owner',record['id'],request)
    sheet=result['lastDrawing']
    assert sheet['dimensions'][0]['value']==pytest.approx(12)
    assert [x['section']['areaMm2'] for x in sheet['layouts'][:4]]==pytest.approx([360*math.sqrt(2),360,480,100])
    assert sheet['settings']['views']==request['views']
    assert store.get('owner',record['id'])['lastDrawing']['settings']['dimensions']==request['dimensions']
    dxf,_=store.artifact('owner',record['id'],next(k for k in sheet['artifacts'] if k.endswith('.dxf')))
    document=ezdxf.readfile(dxf);assert not document.audit().errors
    dimensions=list(document.modelspace().query('DIMENSION'))
    assert len(dimensions)==1 and dimensions[0].get_measurement()==pytest.approx(12)
    pdf,_=store.artifact('owner',record['id'],next(k for k in sheet['artifacts'] if k.endswith('.pdf')))
    with fitz.open(pdf) as document:
        assert '12' in document[0].get_text() and len(document[0].get_drawings())>30


def test_invalid_normal_and_empty_section_preserve_previous_sheet(asymmetric):
    _,store,record,_=asymmetric
    before=store.get('owner',record['id'])
    for view in ({'view':'section','normal':[0,0,0],'position':0},
                 {'view':'section','normal':[1,2,3],'position':9999},
                 {'view':'custom','normal':[1,0,0],'horizontal':[2,0,0]}):
        with pytest.raises(DesignError):store.drawing('owner',record['id'],{'views':[view]})
    assert store.get('owner',record['id'])==before
