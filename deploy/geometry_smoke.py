import json,tempfile,os
from pathlib import Path
from app.cad_executor import execute_cad_plan,inspect_cad_draft
plan={'version':'cad-plan-v1','units':'mm','name':'Deployment geometry smoke','parameters':{},'features':[{'id':'body','op':'box','size':[20,15,10],'origin':[0,0,0]}],'result':'body'}
with tempfile.TemporaryDirectory(prefix='joyniu-deploy-smoke-') as folder:
 result=execute_cad_plan(plan,Path(folder)/'entity',timeout_seconds=60,include_isometric=True)
 if result.get('status') != 'succeeded' or not result.get('valid'):
  print(json.dumps(result,ensure_ascii=False));raise SystemExit('Isolated CAD worker failed')
 size=result['inspection']['bbox']['size']
 assert all(abs(a-b)<0.001 for a,b in zip(size,[20,15,10])),size
 artifacts=result['artifacts']
 assert Path(artifacts['step']['path']).stat().st_size>100
 assert Path(artifacts['glb']['path']).read_bytes()[:4]==b'glTF'
 views=artifacts['views']
 assert len(views)==3
 for view in views.values():
  assert Path(view['pngPath']).read_bytes().startswith(b'\x89PNG')
 draft=inspect_cad_draft(plan,Path(folder)/'draft',timeout_seconds=15,include_projections=True)
 assert draft.get('status') == 'succeeded' and draft.get('valid'),draft
 print(json.dumps({'uid':os.getuid(),'geometry':'passed','bboxMm':size,'STEP':True,'GLB':True,'orthographicViews':len(views),'draft15Seconds':True}))
