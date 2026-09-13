"""Real sheet, individual-body and mesh operations, including rejected picks."""
from copy import deepcopy
import math
import pytest
from app.cad_executor import build_plan_shape
from app.cad_plan import PlanValidationError, validate_plan
from app.cad_surface_features import build_surface_feature, body_signature
from app.cad_topology import topology_identity, feature_prefix, resolve_topology_selection
from app.geometry import get_cadquery
cq=get_cadquery()
pytestmark=pytest.mark.skipif(cq is None,reason='Requires real OCCT')


def plan(*features):return {'version':'cad-plan-v1','parameters':{},'features':list(features),'result':features[-1]['id']}


def picks(p,kind='face'):
    shape=build_plan_shape(p)[0];identity=topology_identity(p,p['result'],shape)
    return shape,[{'kind':kind,'sourceFeatureId':p['result'],'geometryVersion':identity['version'],'index':i,'signature':sig} for i,sig in enumerate(identity[kind+'Signatures'])]


def add(p,feature):return {**deepcopy(p),'features':[*deepcopy(p['features']),feature],'result':feature['id']}


def roundtrip(tmp_path,p):
    shape=build_plan_shape(p)[0].val();path=tmp_path/'sheet.step';cq.exporters.export(shape,str(path))
    restored=cq.importers.importStep(str(path)).val();assert restored.isValid()
    assert len(restored.Solids())==len(shape.Solids())
    assert sum(s.Volume() for s in restored.Solids())==pytest.approx(sum(s.Volume() for s in shape.Solids()),rel=2e-5,abs=1e-6)
    assert restored.Area()==pytest.approx(shape.Area(),rel=2e-5,abs=1e-5)
    return restored


BOX={'id':'box','op':'box','size':[10,8,6]}
SHEET={'id':'sheet','op':'surface_style','points':[[[20,0,0],[20,8,0]],[[30,0,0],[30,8,0]]]}


def test_offset_six_faces_uses_surface_limit_without_relaxing_shell_limit(tmp_path):
    p=plan(BOX);shape,faces=picks(p)
    with pytest.raises(PlanValidationError):resolve_topology_selection(p,'box',shape,faces,'face')
    assert len(resolve_topology_selection(p,'box',shape,faces,'face',max_faces=64))==6
    with pytest.raises(PlanValidationError):validate_plan(add(p,{'id':'bad','op':'shell','input':'box','thickness':-1,'faces':faces}))
    output=roundtrip(tmp_path,add(p,{'id':'offset','op':'surface_offset','input':'box','faces':faces,'distance':1,'keepOriginal':False}))
    assert not output.Solids() and len(output.Faces())==6
    assert output.Area()==pytest.approx(376)
    too_many=[deepcopy(faces[0]) for _ in range(65)]
    with pytest.raises(PlanValidationError):validate_plan(add(p,{'id':'bad','op':'surface_offset','input':'box','faces':too_many,'distance':1}))


@pytest.mark.parametrize('continuity',['position','tangent','curvature'])
def test_closed_boundary_constructs_real_sheet_with_requested_continuity(tmp_path,continuity):
    p=plan(SHEET);_,edges=picks(p,'edge')
    output=roundtrip(tmp_path,add(p,{'id':'patch','op':'surface_boundary','input':'sheet','edges':edges,'keepOriginal':False,'continuity':continuity}))
    assert len(output.Faces())==1 and not output.Solids()
    assert output.Area()==pytest.approx(80,rel=1e-5)


def test_incomplete_or_stale_boundary_is_rejected(tmp_path):
    p=plan(SHEET);_,edges=picks(p,'edge')
    f={'id':'patch','op':'surface_boundary','input':'sheet','edges':edges[:2],'keepOriginal':False}
    with pytest.raises(PlanValidationError) as error:build_plan_shape(add(p,f))
    assert error.value.code=='invalid_boundary'
    f['edges']=deepcopy(edges);f['edges'][0]['geometryVersion']='f'*64
    with pytest.raises(PlanValidationError) as error:build_plan_shape(add(p,f))
    assert error.value.code=='stale_topology'


def test_thicken_true_sheet_has_expected_wall_volume_and_step(tmp_path):
    p=plan(SHEET);_,faces=picks(p)
    output=roundtrip(tmp_path,add(p,{'id':'thick','op':'surface_thicken','input':'sheet','faces':faces,'thickness':2}))
    assert len(output.Solids())==1 and output.Volume()==pytest.approx(160)


def test_style_invalid_grid_and_degenerate_mesh_are_not_substituted():
    with pytest.raises(PlanValidationError):build_plan_shape(plan({**SHEET,'points':[[[0,0,0],[0,0,0]],[[0,0,0],[0,0,0]]]}))
    with pytest.raises(PlanValidationError):validate_plan(plan({**SHEET,'points':[[[0,0,0],[0,1,0]],[[1,0,0]]]}))


def test_adjacent_surfaces_sew_as_shell_and_reject_open_solid(tmp_path):
    a={**SHEET,'id':'a','points':[[[0,0,0],[0,8,0]],[[10,0,0],[10,8,0]]]}
    b={**SHEET,'id':'b','points':[[[10,0,0],[10,8,0]],[[20,0,0],[20,8,0]]]}
    p=plan(a,b,{'id':'joined','op':'surface_join','inputs':['a','b']})
    output=roundtrip(tmp_path,p);assert not output.Solids() and output.Area()==pytest.approx(160)
    p['features'][-1]['makeSolid']=True
    with pytest.raises(PlanValidationError):build_plan_shape(p)


def test_six_oriented_surface_patches_sew_to_actual_closed_solid(tmp_path):
    patches=[]
    for i,face in enumerate(cq.Solid.makeBox(10,8,6).Faces()):
        plane=cq.Plane(origin=face.Center(),normal=face.normalAt())
        local=[plane.toLocalCoords(vertex.Center()) for vertex in face.Vertices()]
        u0,u1=min(p.x for p in local),max(p.x for p in local);v0,v1=min(p.y for p in local),max(p.y for p in local)
        points=[[list(plane.toWorldCoords((u,v)).toTuple()) for v in (v0,v1)] for u in (u0,u1)]
        patches.append({'id':f'face{i}','op':'surface_style','points':points})
    output=roundtrip(tmp_path,plan(*patches,{'id':'closed','op':'surface_join','inputs':[f['id'] for f in patches],'makeSolid':True}))
    assert len(output.Solids())==1 and output.Volume()==pytest.approx(480)


@pytest.mark.parametrize('action',['translate','copy','rotate','keep','delete'])
def test_individual_body_actions_preserve_unselected_sheets(tmp_path,action):
    p=plan(BOX,SHEET,{'id':'mixed','op':'compound','inputs':['box','sheet']})
    shape=build_plan_shape(p)[0];solid=shape.val().Solids()[0]
    f={'id':'edited','op':'body_edit','input':'mixed','bodies':[{'index':0,'signature':body_signature(solid)}],'action':action}
    if action in {'translate','copy'}:f['vector']=[0,0,10]
    if action=='rotate':f.update(axisStart=[0,0,0],axisEnd=[0,0,1],angle=90)
    output=roundtrip(tmp_path,add(p,f))
    count=0 if action=='delete' else 2 if action=='copy' else 1
    assert len(output.Solids())==count
    assert sum(s.Volume() for s in output.Solids())==pytest.approx(count*480)
    outside=[face for face in output.Faces() if face.Center().x>19]
    assert len(outside)==1 and outside[0].Area()==pytest.approx(80)


def test_body_edit_rejects_stale_signature_and_preserves_free_curve():
    solid=cq.Solid.makeBox(2,2,2);wire=cq.Wire.makePolygon([cq.Vector(10,0,0),cq.Vector(12,0,0)],close=False)
    value=cq.Workplane('XY').newObject([cq.Compound.makeCompound([solid,wire])])
    f={'id':'edit','op':'body_edit','input':'mixed','bodies':[{'index':0,'signature':body_signature(solid)}],'action':'translate','vector':[0,0,3]}
    output=build_surface_feature(cq,f,{'mixed':value},float,lambda v:tuple(map(float,v)),{})
    assert any(edge.isSame(wire.Edges()[0]) for edge in output.val().Edges())
    f['bodies'][0]['signature']='f'*64
    with pytest.raises(PlanValidationError) as error:build_surface_feature(cq,f,{'mixed':value},float,lambda v:tuple(map(float,v)),{})
    assert error.value.code=='stale_body_selection'


def test_closed_triangle_mesh_becomes_real_solid_and_open_mesh_rejected(tmp_path):
    f={'id':'tetra','op':'mesh_body','vertices':[[0,0,0],[6,0,0],[0,6,0],[0,0,6]],'triangles':[[0,2,1],[0,1,3],[0,3,2],[1,2,3]]}
    output=roundtrip(tmp_path,plan(f));assert len(output.Solids())==1 and output.Volume()==pytest.approx(36)
    f['triangles'][3]=[0,1,2]
    with pytest.raises(PlanValidationError) as error:build_plan_shape(plan(f))
    assert error.value.code=='open_mesh'


def test_history_prefix_prunes_later_metadata_but_never_hides_missing_dependency():
    from app.cad_topology import digest
    face_ref={'kind':'face','sourceFeatureId':'later','geometryVersion':'a'*64,'signature':'b'*64,'index':0}
    sketch={'id':'laterSketch','plane':'custom','frame':{'origin':[0,0,0],'normal':[0,0,1],'xDir':[1,0,0]},'planeSource':face_ref,'start':[0,0],'segments':[{'type':'line','to':[1,0]},{'type':'line','to':[0,1]}]}
    p=plan(BOX,{'id':'later','op':'box','size':[2,2,2]});p['sketches']=[sketch]
    p['annotations']=[{'id':'laterNote','kind':'note','position':[0,0,0],'references':[face_ref],'text':'later'}]
    prefix=feature_prefix(p,'box');assert prefix['sketches']==[] and prefix['annotations']==[]
    assert build_plan_shape(prefix)[0].val().Volume()==pytest.approx(480)
    p['features'][0]={'id':'box','op':'profile_extrude','sketchId':'laterSketch','plane':'XY','start':[0,0],'segments':[{'type':'line','to':[1,0]},{'type':'line','to':[0,1]}],'distance':1}
    with pytest.raises(PlanValidationError) as error:feature_prefix(p,'box')
    assert error.value.code=='invalid_preview_feature'


def test_curvature_boundary_matches_nonzero_principal_curvatures_after_step(tmp_path):
    from OCP.BRep import BRep_Tool
    from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
    from OCP.GeomLProp import GeomLProp_SLProps
    feature={'id':'curved','op':'surface_style','points':[[[x,y,.02*x*x+.03*y*y] for y in (0,4,8)] for x in (0,5,10)]}
    p=plan(feature);source,edges=picks(p,'edge')
    result=roundtrip(tmp_path,add(p,{'id':'patch','op':'surface_boundary','input':'curved','edges':edges,'continuity':'curvature','keepOriginal':False}))
    def properties(face,point):
        surface=BRep_Tool.Surface_s(face.wrapped);projection=GeomAPI_ProjectPointOnSurf(point.toPnt(),surface)
        u,v=projection.LowerDistanceParameters();props=GeomLProp_SLProps(surface,u,v,2,1e-7)
        return [props.MinCurvature(),props.MaxCurvature()],projection.LowerDistance()
    for edge in source.val().Edges():
        expected,_=properties(source.val().Faces()[0],edge.positionAt(.5))
        actual,distance=properties(result.Faces()[0],edge.positionAt(.5))
        assert min(abs(v) for v in expected)>.02
        assert actual==pytest.approx(expected,abs=2e-4)
        assert distance<1e-4


def test_surface_expression_parameters_survive_early_prefix():
    p=plan({'id':'surface','op':'surface_style','points':[[[0,0,0],[0,'height',0]],[[10,0,0],[10,'height',0]]]})
    p['parameters']={'height':{'value':8},'unused':{'value':999}}
    prefix=feature_prefix(p,'surface')
    assert set(prefix['parameters'])=={'height'}
    assert build_plan_shape(prefix)[0].val().Area()==pytest.approx(80)


@pytest.mark.parametrize('op',['translate','rotate','mirror','linear_pattern','circular_pattern'])
def test_existing_transforms_operate_on_true_sheet_geometry(tmp_path,op):
    f={'id':'changed','op':op,'input':'sheet'}
    if op=='translate':f['vector']=[0,0,5]
    if op=='rotate':f.update(axisStart=[0,0,0],axisEnd=[0,1,0],angle=90)
    if op=='mirror':f.update(plane='YZ')
    if op=='linear_pattern':f.update(vector=[20,0,0],count=3)
    if op=='circular_pattern':f.update(axisStart=[0,0,0],axisEnd=[0,0,1],angle=360,count=4)
    output=roundtrip(tmp_path,plan(SHEET,f))
    count=3 if op=='linear_pattern' else 4 if op=='circular_pattern' else 1
    assert not output.Solids() and output.Area()==pytest.approx(80*count)


@pytest.mark.parametrize('op',['rotate','circular_pattern'])
def test_named_axis_parameter_changes_actual_rotation_and_pattern(tmp_path,op):
    source={'id':'source','op':'box','size':[2,2,2],'origin':[5,0,0]}
    feature={'id':'changed','op':op,'input':'source','axisReference':'turnAxis','axisStart':[99,0,0],'axisEnd':[99,0,1],'angle':90 if op=='rotate' else 180}
    if op=='circular_pattern':feature['count']=2
    p=plan(source,feature);p['parameters']={'axisX':{'value':0}}
    p['references']=[{'id':'turnAxis','kind':'axis','mode':'two_points','start':['axisX',0,0],'end':['axisX',0,1]}]
    before=roundtrip(tmp_path,p);p['parameters']['axisX']['value']=1;after=roundtrip(tmp_path,p)
    assert after.Volume()==pytest.approx(before.Volume())
    assert after.Center().x-before.Center().x==pytest.approx(1)
    prefix=feature_prefix(p,'changed');assert prefix['references'][0]['id']=='turnAxis'


def test_named_plane_offset_changes_mirror_actual_location(tmp_path):
    p=plan(BOX,{'id':'reflected','op':'mirror','input':'box','plane':'XY','planeReference':'datum'})
    p['parameters']={'offset':{'value':5}};p['references']=[{'id':'datum','kind':'plane','mode':'offset','parent':'XY','offset':'offset'}]
    before=roundtrip(tmp_path,p);p['parameters']['offset']['value']=7;after=roundtrip(tmp_path,p)
    assert after.Center().z-before.Center().z==pytest.approx(4)
    p['features'][-1]['planeReference']='missing'
    with pytest.raises(PlanValidationError):validate_plan(p)


def test_pmi_anchor_cannot_be_laundered_through_a_saved_draft(tmp_path):
    from app.cad_feature_workspace import CadFeatureWorkspace
    p=plan(BOX);shape,faces=picks(p)
    face=shape.val().Faces()[0]
    p['annotations']=[{'id':'note','kind':'note','text':'位置','position':list(face.Center().toTuple()),'points':[list(face.Center().toTuple())],'references':[faces[0]],'anchors':[{'version':1,'kind':'face','coordinates':[99,99]}]}]
    store=CadFeatureWorkspace(tmp_path/'workspace')
    with pytest.raises(PlanValidationError) as error:store.save('alice',{'name':'伪造锚点','changeNote':'不能信任客户端锚点','plan':p,'suppressed':[]})
    assert error.value.code=='untrusted_topology_binding'
    assert store.list('alice')==[]
