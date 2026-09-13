import os,sys,time,json,hashlib,shutil,threading
from pathlib import Path
os.environ['JOYNIU_CAD_PROVIDER']='codex'
sys.path.insert(0,str(Path.cwd()/'apps/api'))
from app import ai_proxy,cad_codex_provider
from app.ai_proxy import AIFile
from app.cad_source_reader import CadSourceReader
from app.cad_agent import CadAgentService,_parse_action,_protocol_instructions
from app.cad_executor import execute_cad_plan
from app.cad_plan import validate_plan
root=Path('/tmp/joyniu-image-live-20260912'); start=time.monotonic()
source=Path('/tmp/joyniu-currentcad-acceptance-20260912/cad-feature-workspace/feature_18462ba5bcce42a29adaa5ea0c9105b8/build-4db78869e274489e9bad0d89ac56aef1/views/top.png')
shutil.copyfile(source,root/'synthetic-gear-top.png')
inputfile=AIFile('synthetic-exterior-gear-top.png','image/png',source.read_bytes())
evidence={'startedAt':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),'scope':'real source reader + CAD agent protocol inference + isolated OCCT, bounded stage integration, not full multi-turn run','syntheticImage':True,'imageSha256':hashlib.sha256(inputfile.data).hexdigest(),'calls':[],'argvImages':[],'localBilling':'not instantiated, no billing or account database routes','productionReady':False}
real=cad_codex_provider.CodexCadProvider(); lock=threading.Lock()
oldargv=cad_codex_provider._argv
def auditargv(binary,model,directory,images,effort,schema):
    args=oldargv(binary,model,directory,images,effort,schema)
    evidence['argvImages'].append({'imageCount':len(images),'images':[{'filename':p.name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'bytes':p.stat().st_size} for p in images],'model':model,'effort':effort,'usesImageFlag':'-i' in args or '--image' in args})
    return args
cad_codex_provider._argv=auditargv
class BoundedProvider:
    supports_diagnostics=True
    provider_info=real.provider_info
    def __call__(self,body,timeout,on_diagnostics=None):
        with lock:
            if len(evidence['calls'])>=2: raise ValueError('actual provider call budget exhausted')
            images=[p for message in body.get('input',[]) for p in message.get('content',[]) if p.get('type')=='input_image']
            item={'number':len(evidence['calls'])+1,'inputImageCount':len(images),'timeoutSeconds':min(timeout,65),'startedSeconds':round(time.monotonic()-start,3)}
            evidence['calls'].append(item)
        print('REAL_CALL',item,flush=True)
        result=real(body,min(timeout,65),on_diagnostics=on_diagnostics)
        item.update(elapsedSeconds=round(time.monotonic()-start-item['startedSeconds'],3),usage=result.get('usage'))
        (root/f"response-{item['number']}.json").write_text(json.dumps(result,ensure_ascii=False,indent=2))
        return result
provider=BoundedProvider()
try:
    prepared,contexts,sources=ai_proxy._prepare_provider_attachments((inputfile,))
    reader=CadSourceReader(provider_call=provider)
    reading=reader.read(files=prepared,source_files=sources,output_dir=root/'source-reader',timeout_seconds=65)
    evidence['sourceReading']={k:reading.get(k) for k in ['status','annotations','questions','provider','errorCode']}
    if reading['status']!='succeeded':raise RuntimeError('source reader did not succeed')
    prompt='附件是合成齿轮外形俯视图片，用于验证照片输入流程，不是工程图或实拍准确率样本。按图判断它是否与圆柱直齿轮、中心通孔相符。用户给定精确建模尺寸：模数 module=2 mm；齿数 teeth=24；压力角 pressureAngle=20度；齿宽 width=15 mm；中心通孔 boreDiameter=12 mm；无变位 profileShift=0；侧隙 backlash=0。参考平面标定点(100,100)至(300,100)，参考长度20mm；参考标定不得推翻上述用户给定精确尺寸，不从渲染图像推算厚度或隐藏细节。不附加轴承、键槽、倒角、轮毂或其他孔。采用已有 gear 特征生成完整单实体。所有精确数值 source.type=user，source.text引用本段；图片仅外形候选证据，未知照片字段保留不确定，不伪造标注或核验通过。此次是受预算限制的规划阶段调用，请只返回一个 execute_plan action，包括完整CAD plan，供隔离内核执行；不要返回已执行或已确认声明。'
    (root/'user-request.txt').write_text(prompt)
    content=[{'type':'input_text','text':json.dumps({'userRequest':prompt,'sourceTranscription':reading,'knownDimensions':{'module':2,'teeth':24,'pressureAngle':20,'width':15,'boreDiameter':12}},ensure_ascii=False)}]
    for file in prepared:
        metadata,attachment=ai_proxy._attachment_content(file,image_detail='high');content.extend([{'type':'input_text','text':json.dumps(metadata)},attachment])
    body={'model':provider.provider_info['model'],'instructions':_protocol_instructions()+'\nThis bounded stage has already recorded explicit user dimensions. Return execute_plan with the complete minimal gear plan; do not claim execution or source agreement.','reasoning':{'effort':'low'},'input':[{'role':'user','content':content}],'text':{'format':{'type':'json_object'}},'store':False,'stream':False}
    service=CadAgentService(provider_call=provider)
    response=service._call(body,65)
    action=_parse_action(response)
    (root/'action.json').write_text(json.dumps(action,ensure_ascii=False,indent=2))
    if action.get('action')!='execute_plan' or not action.get('plan'):raise RuntimeError('AI did not submit executable plan: '+action.get('action','unknown'))
    plan=action['plan'];validate_plan(plan)
    execution=execute_cad_plan(plan,root/'kernel',timeout_seconds=min(40,160-(time.monotonic()-start)))
    (root/'execution.json').write_text(json.dumps(execution,ensure_ascii=False,indent=2))
    evidence['execution']={k:execution.get(k) for k in ['status','valid','resolvedParameters','inspection','artifacts','isolation']}
    evidence['plan']=plan
    if execution.get('status')!='succeeded':raise RuntimeError('kernel failed')
    from app.cad_plan import evaluate_expression
    parameters=execution['resolvedParameters'];gear=next(f for f in plan['features'] if f['op']=='gear')
    actual={key:evaluate_expression(gear[key],parameters) for key in ['module','teeth','pressureAngle','width','boreDiameter']}
    evidence['knownDimensionsActual']=actual
    evidence['checks']={'twoActualCalls':len(evidence['calls'])==2,'bothCallsIncludeImages':all(c['inputImageCount']>0 for c in evidence['calls']),'knownDimensionsExact':actual=={'module':2,'teeth':24,'pressureAngle':20,'width':15,'boreDiameter':12},'kernelValid':execution['valid'] is True,'sourceOnlyCandidate':reading.get('verified') is False}
    evidence['status']='passed' if all(evidence['checks'].values()) else 'failed'
except Exception as error:
    evidence.update(status='failed',error=type(error).__name__+': '+str(error))
finally:
    evidence['elapsedSeconds']=round(time.monotonic()-start,3)
    (root/'evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
    print('FINAL',json.dumps({'status':evidence['status'],'error':evidence.get('error'),'elapsed':evidence['elapsedSeconds'],'calls':len(evidence['calls'])},ensure_ascii=False),flush=True)
