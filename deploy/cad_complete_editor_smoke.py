"""Actual candidate-runtime checks for the expanded CAD editor.

Only synthetic records in a newly created private subdirectory are used. No AI,
network, application configuration, customer database or live API is consulted.
Every reported geometry is exchanged through STEP and retained as evidence.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import math
from pathlib import Path
import tempfile
import shutil
import time
from uuid import uuid4
import zipfile

FORMAT = 'joyniu-complete-editor-smoke-v1'
SCRIPT = 'deploy/cad_complete_editor_smoke.py'
CHECKS = (
    'multiContourHole', 'ellipseHole', 'hermiteSpline', 'spatialSweepHole',
    'arbitraryLoftHole', 'referenceHelix', 'helixParameterRebuild', 'helixInteractivePreview', 'styledSurface',
    'curvatureBoundaryG2', 'surfaceJoin', 'movedFace', 'pmiAnchorsRebuilt',
    'importWithoutHistory', 'importedBodyEdited', 'pdmImmutableRecovery', 'standardPart',
    'communityPublishedSnapshot', 'communityReaderIsolation', 'communityIndependentEdit', 'communityWithdrawal',
)
# Required actual STEP evidence and its expected number of solids. Sheets must
# remain genuine zero-solid shapes; a default box is never a valid substitute.
GEOMETRIES = {
    'multi-contour': 2, 'ellipse': 1, 'spline': 1, 'sweep': 1, 'loft': 1,
    'helix': 1, 'helix-rebuilt': 1, 'style': 0, 'boundary-g2': 0,
    'joined-sheets': 0, 'pmi-rebuilt': 1, 'import-edited': 1, 'pdm-reopened': 1,
    'standard-part': 1,
    'community-download': 1, 'community-edited-copy': 1,
}


def require(condition, message):
    if not condition:
        raise RuntimeError('Complete editor candidate check failed: ' + message)


def close(actual, expected, tolerance=1e-5):
    require(math.isfinite(actual) and abs(actual-expected) <= tolerance,
            'numeric geometry agreement')


def rectangle(x0=0, y0=0, x1=20, y1=10):
    return {'start':[x0,y0], 'segments':[{'type':'line','to':p} for p in ([x1,y0],[x1,y1],[x0,y1],[x0,y0])]}


def ellipse(rx=10, ry=5, cx=0, cy=0):
    start=[cx+rx,cy]
    return {'start':start, 'segments':[{'type':'ellipse','center':[cx,cy], 'radii':[rx,ry],
            'rotation':0,'startAngle':0,'endAngle':360,'to':start}]}


def plan(*features, **extra):
    return {'version':'cad-plan-v1','units':'mm','parameters':{},'features':list(features),
            'result':features[-1]['id'], **extra}


def complete_editor_smoke(root, owner, output):
    """Return only after all independent native-kernel checks have succeeded."""
    from app.cad_feature_workspace import CadFeatureWorkspace, FeatureWorkspaceError, FeatureNotFound
    from app.cad_plan import PlanValidationError, validate_plan, evaluate_expression, resolve_parameters
    from app.cad_executor import build_plan_shape
    from app.cad_editor_entities import annotation_measurement
    from app.cad_profile_operations import reference_curve_wire
    from app.cad_topology import topology_identity
    from app.cad_import_assets import CadImportAssets
    from app.cad_editor_pdm import CadEditorPdm
    from app.cad_design_workspace import CadDesignWorkspace
    from app.cad_standard_parts import CATALOG
    from app.cad_community import CadCommunity
    from app.platform import PDMRepository, NotFoundError, AuthorizationError
    from app.geometry import get_cadquery
    cq=get_cadquery()
    require(cq is not None, 'real OCCT unavailable')
    output=Path(output)
    require(output.is_dir(), 'evidence directory missing')
    deadline=time.monotonic()+480
    geometries={}; details={}

    def bounded():
        require(time.monotonic() < deadline, '480 second bounded runtime exhausted')

    def evidence(name, shape, raw=None):
        bounded()
        destination=output/f'complete-editor-{name}.step'
        if raw is None:
            cq.exporters.export(shape, str(destination), exportType='STEP')
        else:
            destination.write_bytes(raw)
        restored=cq.importers.importStep(str(destination)).val()
        require(restored.isValid() and all(s.isValid() for s in restored.Solids()), name+' invalid STEP')
        require(len(restored.Solids())==GEOMETRIES[name], name+' STEP solid count')
        volume=sum(s.Volume() for s in restored.Solids())
        close(volume, sum(s.Volume() for s in shape.Solids()), max(1e-5,abs(volume)*3e-5))
        close(restored.Area(), shape.Area(), max(1e-5,shape.Area()*3e-5))
        box=restored.BoundingBox(); payload=destination.read_bytes()
        geometries[name]={'valid':True,'solidCount':len(restored.Solids()),'faceCount':len(restored.Faces()),
            'volumeMm3':volume,'areaMm2':restored.Area(),'sizeMm':[box.xlen,box.ylen,box.zlen],
            'step':{'name':destination.name,'bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest()}}
        return restored

    with tempfile.TemporaryDirectory(prefix='complete-editor-',dir=Path(root)) as temporary:
        private=Path(temporary); store=CadFeatureWorkspace(private/'features')
        def commit(p, previous=None):
            bounded()
            return store.commit(owner, {'name':'完整编辑器合成验收','fileId':'synthetic-complete-editor',
                'plan':copy.deepcopy(p),'suppressed':[],'changeNote':'候选运行时实证；不作制造放行',
                'requestId':str(uuid4()), **({'designId':previous['id'],'expectedRevision':previous['revision']} if previous else {})})
        def saved_shape(record):
            path,_=store.artifact(owner,record['id'],record['revision'],'step')
            require(record['inspection']['stepReadback']['valid'], 'workspace STEP readback')
            return cq.importers.importStep(str(path)).val()
        def native(p):
            bounded(); return build_plan_shape(validate_plan(p))[0].val()
        def saved_case(name,p,expected):
            record=commit(p); actual=evidence(name,saved_shape(record))
            close(sum(s.Volume() for s in actual.Solids()),expected,max(1e-5,expected*3e-5))
            return record,actual

        multi=plan({'id':'body','op':'profile_extrude','plane':'XY',**rectangle(),'distance':5,
            'contours':[{'id':'hole','role':'hole','parent':'main',**ellipse(2,2,5,5)},
                        {'id':'second','role':'outer',**rectangle(30,0,40,10)}]})
        _,multi_shape=saved_case('multi-contour',multi,(300-4*math.pi)*5)
        require(not any(s.isInside(cq.Vector(5,5,2)) for s in multi_shape.Solids()),'declared hole filled')
        elliptical=plan({'id':'body','op':'profile_extrude','plane':'XY',**ellipse(10,5),'distance':4,
            'contours':[{'id':'hole','role':'hole','parent':'main',**ellipse(2,2)}]})
        saved_case('ellipse',elliptical,math.pi*46*4)
        spline=plan({'id':'body','op':'profile_extrude','plane':'XY','start':[0,0],
            'segments':[{'type':'spline','through':[[5,8]],'to':[10,0]},
                        {'type':'line','to':[10,-5]},{'type':'line','to':[0,-5]},{'type':'line','to':[0,0]}],'distance':5})
        saved_case('spline',spline,1450/3)
        sweep=plan({'id':'body','op':'profile_sweep','plane':'XY',**ellipse(2,1),
            'contours':[{'id':'hole','role':'hole','parent':'main',**ellipse(.5,.5)}],
            'path':{'start':[0,0,0],'segments':[{'type':'arc','through':[0,10-10/math.sqrt(2),10/math.sqrt(2)],'to':[0,10,10]}]}})
        _,swept=saved_case('sweep',sweep,1.75*math.pi*5*math.pi)
        require(swept.BoundingBox().zlen>10,'sweep did not follow actual curved path')
        section={**rectangle(-5,-3,5,3),'contours':[{'id':'hole','role':'hole','parent':'main',**ellipse(1,1)}]}
        loft=plan({'id':'body','op':'profile_loft','ruled':True,'sections':[
            {'id':'a','frame':{'origin':[0,0,0],'normal':[1,0,0],'xDir':[0,1,0]},**section},
            {'id':'b','frame':{'origin':[10,2,0],'normal':[1,0,0],'xDir':[0,1,0]},**section}]})
        _,lofted=saved_case('loft',loft,(60-math.pi)*10)
        close(lofted.BoundingBox().xlen,10)

        helix=plan({'id':'body','op':'profile_sweep','plane':'custom',
            'frame':{'origin':['r',0,0],'xDir':[1,0,0],'normal':[0,'2*3.141592653589793*r','p']},
            **ellipse(.3,.3),'path':{'curveReference':'helix1'},'isFrenet':True},
            parameters={'r':{'value':5},'p':{'value':4}},
            references=[{'id':'helix1','kind':'helix','radius':'r','pitch':'p','turns':1.5,
                'frame':{'origin':[0,0,0],'xDir':[1,0,0],'normal':[0,0,1]}}])
        h1,_=saved_case('helix',helix,math.pi*.3**2*1.5*math.hypot(2*math.pi*5,4))
        changed=copy.deepcopy(h1['plan']);changed['parameters']['r']['value']=7;changed['parameters']['p']['value']=5
        interactive=store.preview(owner,{'designId':h1['id'],'expectedRevision':1,'plan':changed,'suppressed':[]})
        mesh=interactive.get('mesh',{});positions=mesh.get('positions',[]);indices=mesh.get('indices',[])
        require(interactive.get('valid') is True and interactive.get('inspection',{}).get('solidCount')==1,
                'reference helix interactive preview failed')
        require(positions and len(positions)%3==0 and indices and len(indices)%3==0
                and len(mesh.get('normals',[]))==len(positions)
                and len(mesh.get('faceIds',[]))==len(indices)//3
                and all(type(index) is int and 0<=index<len(positions)//3 for index in indices),
                'helix preview triangle/face/normal indexing mismatch')
        require(store.get(owner,h1['id'])==h1,'helix preview changed saved head')
        h2=commit(changed,h1);actual=evidence('helix-rebuilt',saved_shape(h2))
        close(actual.Volume(),math.pi*.3**2*1.5*math.hypot(2*math.pi*7,5),actual.Volume()*3e-5)
        number=lambda value:evaluate_expression(value,resolve_parameters(changed))
        wire=reference_curve_wire(cq,'helix1',changed,number)
        require(len(wire.Edges())==1 and wire.Edges()[0].geomType()!='LINE','helix became sampled polyline')
        for u in (0,.137,.731,1):
            point=wire.Edges()[0].positionAt(u);close(math.hypot(point.x,point.y),7,2e-6);close(point.z,7.5*u,2e-6)
        require(store.get(owner,h1['id'],1)==h1,'helix parameter edit changed old version')
        details['helix']={'savedRevisions':2,'radiusBefore':5,'radiusAfter':7,'pitchAfter':5,'exactCurvedEdges':1,
            'previewVertices':len(positions)//3,'previewTriangles':len(indices)//3,'previewPreservedHead':True}

        curved={'id':'curved','op':'surface_style','points':[[[x,y,.02*x*x+.03*y*y] for y in (0,4,8)] for x in (0,5,10)]}
        surface_plan=plan(curved);styled=commit(surface_plan);source=native(surface_plan);evidence('style',saved_shape(styled))
        identity=topology_identity(surface_plan,'curved',cq.Workplane('XY').newObject([source]))
        edges=[{'kind':'edge','sourceFeatureId':'curved','geometryVersion':identity['version'],'index':i,'signature':sig}
               for i,sig in enumerate(identity['edgeSignatures'])]
        patch_plan=plan(curved,{'id':'patch','op':'surface_boundary','input':'curved','edges':edges,'continuity':'curvature','keepOriginal':False})
        patch=evidence('boundary-g2',native(patch_plan))
        from OCP.BRep import BRep_Tool
        from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
        from OCP.GeomLProp import GeomLProp_SLProps
        def curvature(face,point):
            surface=BRep_Tool.Surface_s(face.wrapped);projection=GeomAPI_ProjectPointOnSurf(point.toPnt(),surface)
            u,v=projection.LowerDistanceParameters();props=GeomLProp_SLProps(surface,u,v,2,1e-7)
            return [props.MinCurvature(),props.MaxCurvature()],projection.LowerDistance()
        max_error=0
        for edge in source.Edges():
            expected,_=curvature(source.Faces()[0],edge.positionAt(.5));values,distance=curvature(patch.Faces()[0],edge.positionAt(.5))
            require(min(abs(v) for v in expected)>.02,'G2 source must have nonzero curvature')
            require(distance<1e-4,'G2 boundary lost positional continuity')
            for a,b in zip(values,expected):close(a,b,2e-4);max_error=max(max_error,abs(a-b))
        details['g2']={'edgeSamples':len(source.Edges()),'maxCurvatureError':max_error,'nonzeroSourceCurvature':True}
        joined=plan({'id':'a','op':'surface_style','points':[[[0,0,0],[0,8,0]],[[10,0,0],[10,8,0]]]},
            {'id':'b','op':'surface_style','points':[[[10,0,0],[10,8,0]],[[20,0,0],[20,8,0]]]},
            {'id':'joined','op':'surface_join','inputs':['a','b']})
        sewn=evidence('joined-sheets',native(joined));close(sewn.Area(),160);require(len(sewn.Shells())==1,'adjacent surfaces were not sewn')

        box=plan({'id':'body','op':'box','size':['width',16,10]},parameters={'width':{'value':20}})
        preview=store.preview(owner,{'plan':box,'suppressed':[]})
        face=next(f for f in preview['faces'] if f.get('normal',[0,0,0])[2]>.9)
        box['annotations']=[{'id':'pmi1','kind':'distance','text':'合成检验尺寸','points':[[2,8,10],[18,8,10]],
            'position':[20,20,15],'references':[copy.deepcopy(face['selector']),copy.deepcopy(face['selector'])]}]
        p1=commit(box);p2plan=copy.deepcopy(p1['plan']);p2plan['parameters']['width']['value']=30;p2=commit(p2plan,p1)
        require(len(p2['plan']['annotations'][0]['anchors'])==2,'saved PMI lost geometry anchors')
        close(annotation_measurement(p1['plan']['annotations'][0],float),16)
        close(annotation_measurement(p2['plan']['annotations'][0],float),24)
        require(store.get(owner,p1['id'],1)==p1 and store.get(owner,p1['id'])['revision']==2,'PMI version persistence')
        evidence('pmi-rebuilt',saved_shape(p2));details['pmi']={'measurementBeforeMm':16,'measurementAfterMm':24,'savedRevisions':2,'oldVersionPreserved':True}
        wrong=copy.deepcopy(p2plan);wrong['annotations'][0]['anchors'][0]['coordinates'][0]=.99
        try:commit(wrong,p2)
        except (FeatureWorkspaceError,PlanValidationError):pass
        else:require(False,'forged PMI anchor accepted')
        require(store.get(owner,p1['id'])==p2,'failed PMI changed saved head')

        # Start from an exchanged box, with no CAD plan/feature history in the
        # upload; imported-body metadata must stay explicit through PDM reopen.
        original=private/'synthetic-no-history.step'
        cq.exporters.export(cq.Workplane('XY').box(20,16,10,centered=False),str(original))
        assets=CadImportAssets(store.root/'imports')
        asset=assets.upload(owner,'synthetic-no-history.step',original.read_bytes(),'无历史合成体','mm',str(uuid4()))
        require(asset['history']=='imported_body' and asset['feature']['op']=='import_step','import fabricated feature history')
        imported=plan({'id':'source',**asset['feature']});i1=commit(imported)
        visible=store.preview(owner,{'designId':i1['id'],'expectedRevision':1,'plan':i1['plan']})
        top=next(f for f in visible['faces'] if f.get('normal',[0,0,0])[2]>.9)
        moved=copy.deepcopy(i1['plan']);moved['features'].append({'id':'moved','op':'move_face','input':'source','faces':[top['selector']],'distance':2});moved['result']='moved'
        i2=commit(moved,i1);imported_shape=evidence('import-edited',saved_shape(i2))
        close(imported_shape.Volume(),20*16*12);close(imported_shape.BoundingBox().zlen,12)
        foreign_owner='synthetic-other-'+uuid4().hex
        try:assets.path(foreign_owner,asset['id'],asset['sha256'])
        except FeatureNotFound:pass
        else:require(False,'foreign imported asset was visible')
        repo=PDMRepository(private/'pdm.sqlite3')
        try:
            pdm=CadEditorPdm(repo,store,CadDesignWorkspace(private/'designs'))
            project=repo.create_project('候选完整编辑器验收',owner)
            data={'projectId':project.id,'name':'候选导入体','featureId':i2['id'],'revision':2,'requestId':str(uuid4()),'expectedCurrentRevision':0}
            archived=pdm.save(owner,data);again=pdm.save(owner,data)
            require(again['version']['id']==archived['version']['id'],'PDM retry duplicated immutable version')
            archive=repo.get_version_content(archived['version']['id'])
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                raw=bundle.read('assets/'+asset['id']+'.step')
                require(hashlib.sha256(raw).hexdigest()==asset['sha256'],'PDM omitted imported source dependency')
            saved_archive=output/'complete-editor-pdm.zip';saved_archive.write_bytes(archive)
            # Delete only this test's own temporary upload to prove the archive
            # restores a real independent body instead of relying on a link.
            shutil.rmtree(assets.path(owner,asset['id'],asset['sha256']).parent)
            restored=pdm.open(owner,archived['version']['id'],{'fileId':'synthetic-restored','requestId':str(uuid4())})['record']
            require(restored['plan']['features'][0]['op']=='import_step' and restored['id']!=i2['id'],'PDM lost imported source/history boundary')
            require(restored['productionReady'] is False and restored['drawingAgreement']=='not_checked','PDM forged manufacturing confirmation')
            rebuilt=commit(restored['plan'],restored);evidence('pdm-reopened',saved_shape(rebuilt))
            close(rebuilt['inspection']['volumeMm3'],20*16*12)
            require(repo.get_version_content(archived['version']['id'])==archive,'PDM archive changed after reopen')
            details['pdm']={'idempotent':True,'missingSourceRestored':True,'independentRebuild':True,'manufacturingConfirmed':False,
                'archive':{'name':saved_archive.name,'bytes':len(archive),'sha256':hashlib.sha256(archive).hexdigest()}}
        finally:repo.close()
        washer=copy.deepcopy(CATALOG['washer-m8'])
        nominal=plan({'id':'standard','op':'standard_part','catalogId':washer['catalogId'],'dimensions':washer['dimensions']})
        d=washer['dimensions'];saved_case('standard-part',nominal,math.pi*(d['outerDiameter']**2-d['innerDiameter']**2)*d['length']/4)
        details['standardPart']={'catalogId':washer['catalogId'],'nominalDimensions':d}

        # Real explicit publication, independent foreign-account copy, and
        # withdrawal. This registry has no access to application/live data.
        community=CadCommunity(private/'community',store)
        secret_note='synthetic-private-source-evidence-'+uuid4().hex
        public_plan=plan({'id':'body','op':'box','size':['width',10,5]},
            parameters={'width':{'value':12,'source':secret_note,'question':secret_note}},
            notes=[secret_note],questions=[secret_note])
        source=commit(public_plan)
        publish_request={'featureId':source['id'],'revision':1,'name':'公开合成参数件',
            'description':'仅供候选验收的合成模型','requestId':str(uuid4()),'consent':'public-copy-download'}
        resource=community.publish(owner,publish_request)
        require(community.publish(owner,publish_request)['id']==resource['id'],'community publication retry duplicated snapshot')
        public_fields={'id','name','description','publishedAt','status','inspection','canWithdraw','sharing','drawingAgreement','productionReady','artifacts'}
        listing=community.list(foreign_owner);public=community.get(resource['id'],foreign_owner)
        require([item['id'] for item in listing['items']]==[resource['id']],'foreign reader cannot find published synthetic resource')
        require(set(public)==public_fields and all(set(item)==public_fields for item in listing['items']), 'community public field whitelist')
        for text in (json.dumps(public),json.dumps(listing)):
            require(all(value not in text for value in (owner,source['id'],source['fileId'],secret_note)),'community leaked private source fields')
        require(public['canWithdraw'] is False and public['productionReady'] is False
                and public['drawingAgreement']=='not_checked','community forged owner/manufacturing state')
        try:community.withdraw(foreign_owner,resource['id'])
        except AuthorizationError:pass
        else:require(False,'foreign reader withdrew owner publication')
        original_step,_=store.artifact(owner,source['id'],1,'step')
        raw,mime=community.artifact(resource['id'],'step')
        require(mime=='application/step' and raw==original_step.read_bytes(),'community download differs from selected built revision')
        downloaded=evidence('community-download',saved_shape(source),raw)
        close(downloaded.Volume(),600)
        glb,glb_mime=community.artifact(resource['id'],'glb')
        require(glb_mime=='model/gltf-binary' and glb[:4]==b'glTF','community GLB is not actual model bytes')
        glb_path=output/'complete-editor-community.glb';glb_path.write_bytes(glb)
        changed=copy.deepcopy(source['plan']);changed['parameters']['width']['value']=18;source2=commit(changed,source)
        require(community.artifact(resource['id'],'step')[0]==raw,'published exact revision drifted with source head')
        close(source2['inspection']['volumeMm3'],900)
        open_request={'fileId':'synthetic-community-copy','requestId':str(uuid4())}
        copied=community.open(foreign_owner,resource['id'],open_request)['record']
        require(community.open(foreign_owner,resource['id'],open_request)['record']['id']==copied['id'],'community copy retry duplicated document')
        require(copied['id']!=source['id'] and copied.get('sourceRun') is None
                and copied['communitySource']=={'resourceId':resource['id']},'community copy guessed private source relation')
        copied_text=json.dumps(copied)
        require(all(value not in copied_text for value in (owner,source['id'],source['fileId'],secret_note)),'community copy exposed private source metadata')
        try:store.get(owner,copied['id'])
        except FeatureNotFound:pass
        else:require(False,'publisher can read another account independent copy')
        copy_plan=copy.deepcopy(copied['plan']);copy_plan['parameters']['width']['value']=20
        edited=store.commit(foreign_owner,{'designId':copied['id'],'expectedRevision':1,'name':copied['name'],
            'fileId':copied['fileId'],'plan':copy_plan,'suppressed':copied['suppressed'],'changeNote':'合成公开副本独立修改',
            'requestId':str(uuid4())})
        edited_path,_=store.artifact(foreign_owner,edited['id'],2,'step')
        edited_shape=cq.importers.importStep(str(edited_path)).val()
        evidence('community-edited-copy',edited_shape,edited_path.read_bytes());close(edited_shape.Volume(),1000)
        require(store.get(owner,source['id'])==source2 and community.artifact(resource['id'],'step')[0]==raw,
                'community copy edit changed original or publication')
        require(edited['productionReady'] is False and edited['drawingAgreement']=='not_checked','community copy forged manufacturing confirmation')
        withdrawn=community.withdraw(owner,resource['id'])
        require(withdrawn['status']=='withdrawn' and not community.list(foreign_owner)['items'],'withdrawn resource remains discoverable')
        for kind in ('step','glb'):
            try:community.artifact(resource['id'],kind)
            except NotFoundError:pass
            else:require(False,'withdrawn resource still downloads')
        try:community.get(resource['id'],foreign_owner)
        except NotFoundError:pass
        else:require(False,'withdrawn resource public detail remains visible')
        for request in (open_request,{'fileId':'synthetic-after-withdrawal','requestId':str(uuid4())}):
            try:community.open(foreign_owner,resource['id'],request)
            except NotFoundError:pass
            else:require(False,'withdrawn resource still creates/replays open')
        require(store.get(foreign_owner,copied['id'])==edited,'withdrawal damaged previously created independent copy')
        # This is an evidence archive of actual public output, not a claimed
        # product ZIP endpoint. No private registry or login data is included.
        snapshot={'public.json':json.dumps(public,ensure_ascii=False,sort_keys=True).encode(),'model.step':raw,'model.glb':glb}
        manifest={'format':'joyniu-community-smoke-snapshot-v1','files':{name:{'bytes':len(value),'sha256':hashlib.sha256(value).hexdigest()} for name,value in snapshot.items()}}
        archive_path=output/'complete-editor-community.zip'
        with zipfile.ZipFile(archive_path,'w',compression=zipfile.ZIP_DEFLATED) as bundle:
            for name,value in snapshot.items():bundle.writestr(name,value)
            bundle.writestr('manifest.json',json.dumps(manifest,sort_keys=True))
        def file_metadata(path):
            value=path.read_bytes();return {'name':path.name,'bytes':len(value),'sha256':hashlib.sha256(value).hexdigest()}
        details['community']={'publicFieldsOnly':True,'publicationIdempotent':True,'copyIdempotent':True,
            'immutablePublishedRevision':True,'independentCopy':True,'parameterEdit':True,'foreignWithdrawRejected':True,
            'withdrawalRejectedDownloads':True,'withdrawalRejectedOpen':True,'withdrawalRejectedRetry':True,
            'existingCopyPreserved':True,'manufacturingConfirmed':False,'sourceRevision':1,'sourceHeadRevision':2,
            'copyRevision':2,'publishedVolumeMm3':600,'editedCopyVolumeMm3':1000,
            'glb':file_metadata(glb_path),'archive':file_metadata(archive_path)}

    require(set(geometries)==set(GEOMETRIES),'incomplete geometry evidence')
    return {'format':FORMAT,'scriptSha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'isolatedSyntheticData':True,'checks':dict.fromkeys(CHECKS,True),'geometries':geometries,'details':details}
