"""Real curved, multi-region, constrained and spatial profile round trips."""
from copy import deepcopy
import math
import pytest
from app.cad_executor import build_plan_shape, _build_worker_shape, _execute_in_worker
from app.cad_plan import validate_plan, PlanValidationError, cad_plan_schema
from app.cad_history import TopologyHistory, authorize_bindings
from app.cad_topology import export_topology_preview
from app.geometry import get_cadquery

cq = get_cadquery()
pytestmark = pytest.mark.skipif(cq is None, reason="Real OCCT required")


def rectangle(x0=0, y0=0, x1=20, y1=10):
    return {"start":[x0,y0],"segments":[{"type":"line","to":[x1,y0]},{"type":"line","to":[x1,y1]},{"type":"line","to":[x0,y1]},{"type":"line","to":[x0,y0]}]}


def ellipse(rx=10,ry=5,cx=0,cy=0,rotation=0):
    a=math.radians(rotation);start=[cx+rx*math.cos(a),cy+rx*math.sin(a)]
    return {"start":start,"segments":[{"type":"ellipse","center":[cx,cy],"radii":[rx,ry],"rotation":rotation,"startAngle":0,"endAngle":360,"to":start}]}


def plan(feature, parameters=None):
    return {"version":"cad-plan-v1","parameters":parameters or {},"features":[feature],"result":feature["id"]}


def exchanged(tmp_path,p):
    shape,_,_=build_plan_shape(p)
    path=tmp_path/'actual.step';cq.exporters.export(shape.val(),str(path))
    read=cq.importers.importStep(str(path)).val();assert read.isValid()
    assert len(read.Solids())==len(shape.val().Solids())
    assert read.Volume()==pytest.approx(shape.val().Volume(),rel=2e-5,abs=1e-5)
    return read


def helix_sweep_plan(*, lefthand=False, spatial=False):
    sign = -1 if lefthand else 1
    frame = ({'origin':[2,3,4],'xDir':[0,1,0],'normal':[1,0,0]} if spatial
             else {'origin':[0,0,0],'xDir':[1,0,0],'normal':[0,0,1]})
    section = ({'origin':[2,'3+r',4],'xDir':[0,1,0],'normal':['p',0,f'{sign}*2*3.141592653589793*r']} if spatial
               else {'origin':['r',0,0],'xDir':[1,0,0],'normal':[0,f'{sign}*2*3.141592653589793*r','p']})
    p=plan({'id':'body','op':'profile_sweep','plane':'custom','frame':section,**ellipse(.3,.3),
            'path':{'curveReference':'helix1'},'isFrenet':True}, {'r':{'value':5},'p':{'value':4}})
    p['references']=[{'id':'helix1','kind':'helix','radius':'r','pitch':'p','turns':1.5,'frame':frame,'lefthand':lefthand}]
    return p


@pytest.mark.parametrize('lefthand,spatial',[(False,False),(True,False),(False,True),(True,True)])
def test_reference_helix_is_exact_curved_spine_and_rebuilds_parameters(tmp_path,lefthand,spatial):
    from app.cad_profile_operations import reference_curve_wire
    from app.cad_plan import evaluate_expression
    from app.cad_topology import feature_prefix
    p=helix_sweep_plan(lefthand=lefthand,spatial=spatial)
    for radius,pitch in [(5,4),(7,5)]:
        p['parameters']['r']['value']=radius;p['parameters']['p']['value']=pitch
        number=lambda value:evaluate_expression(value,{'r':radius,'p':pitch})
        wire=reference_curve_wire(cq,'helix1',p,number)
        assert wire.isValid() and len(wire.Edges())==1
        assert wire.Edges()[0].geomType() != 'LINE'
        placement=cq.Plane(**p['references'][0]['frame'])
        # Actual curve evaluation on the cylindrical p-curve, not polyline points.
        for u in [0,.137,.419,.731,1]:
            v=placement.toLocalCoords(wire.Edges()[0].positionAt(u))
            assert math.hypot(v.x,v.y)==pytest.approx(radius,abs=2e-6)
            assert v.z==pytest.approx(1.5*pitch*u,abs=2e-6)
            angle=(-1 if lefthand else 1)*3*math.pi*u
            assert (v.x,v.y)==pytest.approx((radius*math.cos(angle),radius*math.sin(angle)),abs=2e-6)
        body=exchanged(tmp_path,p)
        assert len(body.Solids())==1
        assert body.Volume()==pytest.approx(math.pi*.3**2*1.5*math.hypot(2*math.pi*radius,pitch),rel=3e-5)
        prefix=feature_prefix(p,'body')
        assert prefix['references']==p['references'] and set(prefix['parameters'])=={'r','p'}


@pytest.mark.parametrize('path',[{'curveReference':'absent'}, {'curveReference':'helix1','start':[0,0,0]}, {'curveReference':'helix1','sketchId':'sketch1'}, {'curveReference':3}])
def test_helix_path_rejects_missing_reference_and_ambiguous_snapshots(path):
    p=helix_sweep_plan();p['features'][0]['path']=path
    with pytest.raises(PlanValidationError):validate_plan(p)


@pytest.mark.parametrize('key,value',[('radius',0),('pitch',-1),('turns',101),('turns',0)])
def test_helix_rejects_invalid_or_excessive_geometry(key,value):
    p=helix_sweep_plan();p['references'][0][key]=value
    with pytest.raises(PlanValidationError):build_plan_shape(p)


def test_helix_never_repositions_an_incorrect_section_silently():
    p=helix_sweep_plan();p['features'][0]['frame']['normal']=[0,0,1]
    with pytest.raises(PlanValidationError,match='起点平面'):build_plan_shape(p)


def test_helix_is_a_real_transaction_preview_and_failed_edit_preserves_version(tmp_path):
    from app.cad_feature_workspace import CadFeatureWorkspace, FeatureWorkspaceError
    store=CadFeatureWorkspace(tmp_path/'store')
    p=helix_sweep_plan(spatial=True)
    initial=store.commit('alice',{'name':'空间螺旋扫掠','changeNote':'明确截面与参考线','plan':p,'suppressed':[]})
    assert initial['inspection']['stepReadback']['valid'] and initial['revision']==1
    changed=deepcopy(initial['plan']);changed['parameters']['r']['value']=7
    preview=store.preview('alice',{'designId':initial['id'],'expectedRevision':1,'plan':changed})
    assert preview['inspection']['solidCount']==1 and preview['mesh']['positions']
    assert store.get('alice',initial['id'])['revision']==1
    second=store.commit('alice',{'designId':initial['id'],'expectedRevision':1,'name':'空间螺旋扫掠','changeNote':'参数重建','plan':changed,'suppressed':[]})
    assert second['revision']==2 and second['inspection']['stepReadback']['valid']
    bad=deepcopy(second['plan']);bad['features'][0]['frame']['normal']=[0,0,1]
    with pytest.raises(FeatureWorkspaceError):store.commit('alice',{'designId':initial['id'],'expectedRevision':2,'name':'空间螺旋扫掠','changeNote':'无效截面','plan':bad,'suppressed':[]})
    assert store.get('alice',initial['id'])==second and len(store.versions('alice',initial['id']))==2


@pytest.mark.parametrize('rx,ry,rotation',[(10,5,0),(5,10,31),(7,7,65)])
def test_exact_ellipse_with_hole_extrusion_roundtrip(tmp_path,rx,ry,rotation):
    f={'id':'body','op':'profile_extrude','plane':'XY',**ellipse(rx,ry,rotation=rotation),'distance':4,
       'contours':[{'id':'hole','role':'hole','parent':'main',**ellipse(2,2)}]}
    body=exchanged(tmp_path,plan(f));assert body.Volume()==pytest.approx(math.pi*(rx*ry-4)*4,rel=1e-7)
    assert not body.isInside(cq.Vector(0,0,2))


def test_multiple_outer_contours_remain_separate_solids_with_explicit_hole(tmp_path):
    f={'id':'body','op':'profile_extrude','plane':'XY',**rectangle(0,0,20,10),'distance':5,
       'contours':[{'id':'hole','role':'hole','parent':'main',**ellipse(2,2,5,5)}, {'id':'second','role':'outer',**rectangle(30,0,40,10)}]}
    body=exchanged(tmp_path,plan(f));assert len(body.Solids())==2
    assert body.Volume()==pytest.approx((300-4*math.pi)*5,rel=1e-7)


@pytest.mark.parametrize('hole,code',[(ellipse(2,2,21,5),'invalid_profile'),(ellipse(5,5,5,5),'invalid_profile')])
def test_outside_or_touching_holes_fail_instead_of_silent_boolean(tmp_path,hole,code):
    f={'id':'body','op':'profile_extrude','plane':'XY',**rectangle(),'distance':5,'contours':[{'id':'hole','role':'hole','parent':'main',**hole}]}
    with pytest.raises(PlanValidationError) as error:build_plan_shape(plan(f))
    assert error.value.code==code


def test_revolve_region_with_internal_loop_removes_true_toroidal_cavity(tmp_path):
    f={'id':'body','op':'profile_revolve','plane':'XY',**rectangle(5,0,10,8),'axisStart':[0,0],'axisEnd':[0,1],
       'contours':[{'id':'hole','role':'hole','parent':'main',**rectangle(6,2,8,6)}]}
    body=exchanged(tmp_path,plan(f));assert body.Volume()==pytest.approx(math.pi*(100-25)*8-math.pi*(64-36)*4,rel=1e-7)


def test_hermite_spline_is_exact_curved_profile_and_passes_declared_points(tmp_path):
    from app.cad_profile_geometry import hermite_spline
    edge=hermite_spline(cq,[cq.Vector(0,0),cq.Vector(5,4),cq.Vector(10,0)])
    assert edge.geomType()=='BSPLINE' and edge.distance(cq.Vertex.makeVertex(5,4,0))<1e-8
    f={'id':'body','op':'profile_extrude','plane':'XY','start':[0,0], 'segments':[{'type':'spline','through':[[5,4]],'to':[10,0]},{'type':'line','to':[0,0]}],'distance':3}
    body=exchanged(tmp_path,plan(f));assert 60<body.Volume()<120


@pytest.mark.parametrize('constraint',[{'type':'parallel','edges':[0,2]}, {'type':'perpendicular','edges':[0,1]}, {'type':'equal','edges':[1,3]}, {'type':'angle','edges':[0,1],'value':90}])
def test_line_constraints_are_verified_after_parameter_resolution(tmp_path,constraint):
    f={'id':'body','op':'profile_extrude','plane':'XY',**rectangle(x1='w'),'distance':2,'sketchConstraints':[{'id':'c',**constraint}]}
    assert exchanged(tmp_path,plan(f,{'w':{'value':20}})).Volume()==pytest.approx(400)
    f['segments'][0]['to'][1]=2
    if constraint['type']=='equal':f['segments'][1]['to'][1]=13
    with pytest.raises(PlanValidationError) as error:build_plan_shape(plan(f,{'w':{'value':20}}))
    assert error.value.code=='invalid_sketch_constraint'


def test_radius_concentric_and_cross_contour_coincidence_checked_on_real_curves(tmp_path):
    f={'id':'body','op':'profile_extrude','plane':'XY',**ellipse(10,10),'distance':2,
       'contours':[{'id':'hole','role':'hole','parent':'main',**ellipse(3,3)}],
       'sketchConstraints':[{'id':'r','type':'radius','contourId':'hole','edge':0,'value':3},
                            {'id':'c','type':'concentric','edges':[0,{'contourId':'hole','edge':0}]},
                            {'id':'p','type':'coincident','points':['0:center',{'contourId':'hole','point':'0:center'}]}]}
    assert exchanged(tmp_path,plan(f)).Volume()==pytest.approx(182*math.pi,rel=1e-7)
    f['sketchConstraints'][0]['value']=4
    with pytest.raises(PlanValidationError,match='r'):build_plan_shape(plan(f))


def test_tangent_constraint_checks_curve_derivatives_not_chord(tmp_path):
    f={'id':'body','op':'profile_extrude','plane':'XY','start':[0,0],'segments':[{'type':'line','to':[10,0]},{'type':'arc','through':[15,5],'to':[10,10]},{'type':'line','to':[0,10]}],'distance':2,
       'sketchConstraints':[{'id':'t','type':'tangent','edges':[0,1]}]}
    assert exchanged(tmp_path,plan(f)).Volume()==pytest.approx(200+25*math.pi,rel=1e-7)
    f['segments'][1]['through']=[13,5]
    with pytest.raises(PlanValidationError):build_plan_shape(plan(f))


@pytest.mark.parametrize('constraint',[{'id':'x','type':'radius','edge':0,'value':5,'surprise':1}, {'id':'x','type':'parallel','edges':[0,0]}, {'id':'x','type':'equal','edges':[0,{'contourId':'missing','edge':0}]}, {'id':'x','type':'fixed','points':['0:center'],'position':[0,0]}])
def test_constraint_unknown_fields_bad_refs_and_duplicates_rejected(constraint):
    f={'id':'body','op':'profile_extrude','plane':'XY',**rectangle(),'distance':2,'sketchConstraints':[constraint]}
    with pytest.raises(PlanValidationError):validate_plan(plan(f))


def test_spatial_sweep_section_and_hole_follow_true_curved_path(tmp_path):
    f={'id':'body','op':'profile_sweep','plane':'XY',**ellipse(2,1),'contours':[{'id':'hole','role':'hole','parent':'main',**ellipse(.5,.5)}],
       'path':{'start':[0,0,0],'segments':[{'type':'arc','through':[0,10-10/math.sqrt(2),10/math.sqrt(2)],'to':[0,10,10]}]}}
    body=exchanged(tmp_path,plan(f));assert body.Volume()==pytest.approx((2-.25)*math.pi*5*math.pi,rel=3e-4)
    assert body.BoundingBox().zlen>10


def test_spatial_spline_path_sweeps_actual_custom_profile(tmp_path):
    f={'id':'body','op':'profile_sweep','plane':'XY',**rectangle(-1,-.5,1,.5),
       'path':{'start':[0,0,0],'segments':[{'type':'spline','through':[[0,0,5],[0,4,10]],'to':[0,8,15]}]}}
    body=exchanged(tmp_path,plan(f));assert body.Volume()>30
    assert body.BoundingBox().ylen>8


def test_invalid_sweep_frame_is_not_silently_rotated(tmp_path):
    f={'id':'body','op':'profile_sweep','plane':'XY',**rectangle(-1,-1,1,1),'path':{'start':[0,0,0],'segments':[{'type':'line','to':[10,0,0]}]}}
    with pytest.raises(PlanValidationError,match='起点'):build_plan_shape(plan(f))


def test_arbitrary_frame_loft_with_corresponding_holes_roundtrip(tmp_path):
    section={**rectangle(-5,-3,5,3),'contours':[{'id':'hole','role':'hole','parent':'main',**ellipse(1,1)}]}
    f={'id':'body','op':'profile_loft','ruled':True,'sections':[{'id':'a','frame':{'origin':[0,0,0],'normal':[1,0,0],'xDir':[0,1,0]},**section},{'id':'b','frame':{'origin':[10,2,0],'normal':[1,0,0],'xDir':[0,1,0]},**section}]}
    body=exchanged(tmp_path,plan(f));assert body.Volume()==pytest.approx((60-math.pi)*10,rel=2e-5)
    assert body.BoundingBox().xlen==pytest.approx(10,abs=1e-5)


def test_new_profile_schema_is_strict_and_preserves_legacy_sweep_and_loft():
    variants=cad_plan_schema()['properties']['features']['items']['oneOf'];ops={v['properties']['op']['const']:v for v in variants}
    assert {'sweep','loft','profile_sweep','profile_loft'}<=set(ops)
    assert ops['sweep']['properties']['path']['type']=='array'
    assert ops['profile_sweep']['properties']['path']['type']=='object'
    assert ops['fillet']['properties']['edges']['oneOf'][0]['enum']


def test_empty_reference_document_is_saveable_but_not_exportable():
    p={'version':'cad-plan-v1','parameters':{},'features':[],'result':'','references':[{'id':'p','kind':'point','mode':'coordinates','point':[0,0,0]}]}
    validate_plan(p)
    with pytest.raises(PlanValidationError) as error:build_plan_shape(p)
    assert error.value.code=='empty_geometry'
    p.pop('references')
    with pytest.raises(PlanValidationError):validate_plan(p)


def test_saved_independent_sketch_face_binding_follows_history_and_rejects_forgery(tmp_path):
    p=plan({'id':'body','op':'box','size':['width',16,10]}, {'width':{'value':20}})
    shape,_,_=build_plan_shape(p);(tmp_path/'pick').mkdir();preview,_=export_topology_preview(p,shape,tmp_path/'pick')
    face=next(f for f in preview['faces'] if f.get('normal',[0,0,0])[2]>.9)
    sketch={'id':'topSketch','plane':'custom','frame':{'origin':face['origin'],'xDir':face['xAxis'],'normal':face['normal']}, 'planeSource':face['selector'],**rectangle(0,0,4,3)}
    p['sketches']=[sketch]
    p['features'].append({'id':'boss','op':'profile_extrude','sketchId':'topSketch',**{k:v for k,v in sketch.items() if k!='id'},'distance':4});p['result']='boss'
    request={'plan':p,'persistTopology':True};before,_,_=_build_worker_shape(request,None);bound=request['builtPlan']
    assert bound['sketches'][0]['planeSource']['binding'] and bound['sketches'][0]['planeAttachment']
    changed=deepcopy(bound);changed['parameters']['width']['value']=22
    request={'plan':changed,'persistTopology':True,'bindingBasePlan':bound};after,_,_=_build_worker_shape(request,None)
    assert after.val().Volume()==pytest.approx(48)
    assert after.val().Center().x-before.val().Center().x==pytest.approx(1)
    cq.exporters.export(after.val(),str(tmp_path/'attached.step'));assert cq.importers.importStep(str(tmp_path/'attached.step')).val().isValid()
    forged=deepcopy(changed);forged['sketches'][0]['planeSource']['binding']['key']='f'*64
    with pytest.raises(PlanValidationError) as error:authorize_bindings(forged,bound)
    assert error.value.code=='untrusted_topology_binding'
    moved=deepcopy(changed);moved['sketches'][0]['id']='other';moved['features'][1]['sketchId']='other'
    with pytest.raises(PlanValidationError):authorize_bindings(moved,bound)
    cyclic=deepcopy(p);cyclic['features'][0]['id']='later';cyclic['sketches'][0]['planeSource']['sourceFeatureId']='boss'
    with pytest.raises(PlanValidationError):validate_plan(cyclic)


@pytest.mark.parametrize('op',['profile_sweep','profile_loft'])
def test_spatial_profile_saved_edge_rounding_survives_length_edit(tmp_path,op):
    rect=rectangle(-2,-1,2,1)
    if op=='profile_sweep':f={'id':'body','op':op,'plane':'XY',**rect,'path':{'start':[0,0,0],'segments':[{'type':'line','to':[0,0,'length']}]}}
    else:f={'id':'body','op':op,'sections':[{'id':'a','frame':{'origin':[0,0,0],'normal':[0,0,1],'xDir':[1,0,0]},**rect},{'id':'b','frame':{'origin':[0,0,'length'],'normal':[0,0,1],'xDir':[1,0,0]},**rect}]}
    p=plan(f,{'length':{'value':10}});shape,_,_=build_plan_shape(p);(tmp_path/'pick').mkdir();preview,_=export_topology_preview(p,shape,tmp_path/'pick')
    edge=next(e for e in preview['edges'] if all(abs(z-10)<1e-5 for z in e['points'][2::3]))
    p['features'].append({'id':'round','op':'fillet','input':'body','edges':[edge['selector']],'radius':.2});p['result']='round'
    request={'plan':p,'persistTopology':True};before,_,_=_build_worker_shape(request,None);bound=request['builtPlan']
    assert bound['features'][-1]['edges'][0]['binding']
    changed=deepcopy(bound);changed['parameters']['length']['value']=12
    request={'plan':changed,'persistTopology':True,'bindingBasePlan':bound};after,_,_=_build_worker_shape(request,None)
    assert after.val().Volume()-before.val().Volume()==pytest.approx(16,abs=1e-5)
    assert after.val().isValid()


def test_loft_between_different_explicit_section_types_is_true_solid(tmp_path):
    f={'id':'body','op':'profile_loft','sections':[{'id':'circle','frame':{'origin':[0,0,0],'normal':[0,0,1],'xDir':[1,0,0]},**ellipse(2,2)}, {'id':'rect','frame':{'origin':[0,0,10],'normal':[0,0,1],'xDir':[1,0,0]},**rectangle(-3,-2,3,2)}]}
    body=exchanged(tmp_path,plan(f));assert 120<body.Volume()<240
    assert body.BoundingBox().zlen==pytest.approx(10,abs=1e-5)


def test_new_constraint_failures_leave_prior_committed_hole_and_artifacts_unchanged(tmp_path):
    from app.cad_feature_workspace import CadFeatureWorkspace, FeatureWorkspaceError
    store=CadFeatureWorkspace(tmp_path/'workspace')
    f={'id':'body','op':'profile_extrude','plane':'XY',**ellipse(10,5),'distance':3,'contours':[{'id':'hole','role':'hole','parent':'main',**ellipse(2,2)}], 'sketchConstraints':[{'id':'radius','type':'radius','contourId':'hole','edge':0,'value':2}]}
    original=store.commit('alice',{'name':'有孔椭圆板','changeNote':'明确尺寸建模','suppressed':[],'plan':plan(f)})
    before_step=store.artifact('alice',original['id'],1,'step')[0].read_bytes()
    candidate=deepcopy(original['plan']);candidate['features'][0]['sketchConstraints'][0]['value']=3
    with pytest.raises(FeatureWorkspaceError):store.commit('alice',{'designId':original['id'],'expectedRevision':1,'name':original['name'],'changeNote':'故意冲突半径','suppressed':[],'plan':candidate})
    assert store.get('alice',original['id'])==original
    assert len(store.versions('alice',original['id']))==1
    assert store.artifact('alice',original['id'],1,'step')[0].read_bytes()==before_step


def test_hermite_c1_span_mass_properties_match_exact_integral_after_step(tmp_path):
    f={'id':'body','op':'profile_extrude','plane':'XY','start':[0,0],'segments':[{'type':'spline','through':[[5,8]],'to':[10,0]},{'type':'line','to':[10,-5]},{'type':'line','to':[0,-5]}],'distance':5}
    body=exchanged(tmp_path,plan(f));assert body.Volume()==pytest.approx(1450/3,abs=1e-7)
    assert body.Volume(1e-10)==pytest.approx(1450/3,abs=1e-7)


def test_independent_sketch_loft_and_sweep_path_rebuild_from_latest_definition(tmp_path):
    section={'id':'section','plane':'XY',**rectangle(-2,-1,2,1)}
    path={'id':'path','plane':'YZ','origin':[0,0,0],'start':[0,0],'segments':[{'type':'line','to':[0,'length']}]}
    f={'id':'body','op':'profile_sweep','plane':'XY','sketchId':'section',**rectangle(-2,-1,2,1),'path':{'sketchId':'path','start':[0,0,0],'segments':[{'type':'line','to':[0,0,1]}]}}
    p={**plan(f,{'length':{'value':10}}),'sketches':[section,path]}
    first=exchanged(tmp_path,p);assert first.Volume()==pytest.approx(80)
    p['parameters']['length']['value']=15
    second=exchanged(tmp_path,p);assert second.Volume()==pytest.approx(120)
    p['features']=[{'id':'body','op':'profile_loft','sections':[{'id':'a','sketchId':'section','frame':{'origin':[0,0,0],'normal':[0,0,1],'xDir':[1,0,0]},**rectangle(-2,-1,2,1)}, {'id':'b','sketchId':'upper','frame':{'origin':[0,0,1],'normal':[0,0,1],'xDir':[1,0,0]},**rectangle(-2,-1,2,1)}]}]
    p['sketches']=[section,{'id':'upper','plane':'XY','origin':[0,0,'length'],**rectangle(-2,-1,2,1)}]
    assert exchanged(tmp_path,p).Volume()==pytest.approx(120)
    p['parameters']['length']['value']=20
    assert exchanged(tmp_path,p).Volume()==pytest.approx(160)


def test_committed_independent_sketch_attachment_is_persisted_and_reused(tmp_path):
    from app.cad_feature_workspace import CadFeatureWorkspace
    store=CadFeatureWorkspace(tmp_path/'workspace')
    p=plan({'id':'body','op':'box','size':['width',16,10]}, {'width':{'value':20}})
    shape,_,_=build_plan_shape(p);(tmp_path/'pick').mkdir();preview,_=export_topology_preview(p,shape,tmp_path/'pick')
    face=next(f for f in preview['faces'] if f.get('normal',[0,0,0])[2]>.9)
    sketch={'id':'topSketch','plane':'custom','frame':{'origin':face['origin'],'xDir':face['xAxis'],'normal':face['normal']},'planeSource':face['selector'],**rectangle(0,0,4,3)}
    p['sketches']=[sketch];p['features'].append({'id':'boss','op':'profile_extrude','sketchId':'topSketch',**{k:v for k,v in sketch.items() if k!='id'},'distance':4});p['result']='boss'
    first=store.commit('alice',{'name':'关联草图','changeNote':'面上创建凸台','suppressed':[],'plan':p})
    assert first['plan']['sketches'][0]['planeSource']['binding']
    attachment=deepcopy(first['plan']['sketches'][0]['planeAttachment'])
    changed=deepcopy(first['plan']);changed['parameters']['width']['value']=22
    second=store.commit('alice',{'designId':first['id'],'expectedRevision':1,'name':first['name'],'changeNote':'修改上游宽度','suppressed':[],'plan':changed})
    assert second['revision']==2 and second['plan']['sketches'][0]['planeAttachment']==attachment
    assert second['inspection']['stepReadback']['valid']
    assert store.get('alice',first['id'],1)==first


def test_sheet_worker_exports_true_face_and_zero_solid_volume(tmp_path):
    from app.cad_executor import execute_cad_plan
    f={'id':'surface','op':'surface_style','points':[[[0,0,0],[0,10,0]],[[10,0,0],[10,10,1]]]}
    result=execute_cad_plan(plan(f),tmp_path/'output',include_projections=False)
    assert result['status']=='succeeded',result
    assert result['inspection']['solidCount']==0 and result['inspection']['volumeMm3']==0
    assert result['inspection']['surfaceAreaMm2']>100 and result['inspection']['stepReadback']['valid']
    assert result['artifacts']['views']=={}
    restored=cq.importers.importStep(result['artifacts']['step']['path']).val()
    assert len(restored.Faces())==1 and not restored.Solids()


def test_true_spatial_ellipse_path_produces_closed_sweep_and_step(tmp_path):
    f={'id':'body','op':'profile_sweep','plane':'custom','frame':{'origin':[10,0,0],'normal':[0,1,0],'xDir':[1,0,0]},**rectangle(-.5,-.5,.5,.5),
       'path':{'start':[10,0,0],'segments':[{'type':'ellipse','center':[0,0,0],'xDir':[1,0,0],'normal':[0,0,1],'radii':[10,6],'rotation':0,'startAngle':0,'endAngle':360,'to':[10,0,0]}]}}
    body=exchanged(tmp_path,plan(f))
    from app.cad_profile_geometry import curve_edge
    edge=curve_edge(cq,f['path']['start'],f['path']['segments'][0],float)
    from OCP.GCPnts import GCPnts_AbscissaPoint
    # Default Edge.Length() quadrature is coarse for a complete ellipse.
    exact_length=GCPnts_AbscissaPoint.Length_s(edge._geomAdaptor(),1e-9)
    assert body.Volume()==pytest.approx(exact_length,rel=3e-4)
    assert not body.isInside(cq.Vector(0,0,0))


def test_inline_profile_revolve_uses_named_axis_and_rejects_axis_outside_plane(tmp_path):
    f={'id':'body','op':'profile_revolve','plane':'XY',**rectangle(3,0,5,4),'axisStart':[99,0],'axisEnd':[99,1],'axisReference':'rotationAxis'}
    p=plan(f,{'axisX':{'value':0},'axisZ':{'value':0}})
    p['references']=[{'id':'rotationAxis','kind':'axis','mode':'two_points','start':['axisX',0,'axisZ'],'end':['axisX',1,'axisZ']}]
    assert exchanged(tmp_path,p).Volume()==pytest.approx(64*math.pi)
    p['parameters']['axisX']['value']=1
    assert exchanged(tmp_path,p).Volume()==pytest.approx(48*math.pi)
    p['parameters']['axisZ']['value']=1
    with pytest.raises(PlanValidationError,match='平面'):build_plan_shape(p)
