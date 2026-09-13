import { createClientId } from './clientId.js'

export function insertImportedBody(plan, asset, { position = [0, 0, 0], instanceId } = {}) {
  if (!plan || plan.version !== 'cad-plan-v1' || plan.units !== 'mm' || !Array.isArray(plan.features)) throw new Error('当前图档没有有效的可编辑计划。')
  if (!asset || !/^asset_[a-f0-9]{32}$/.test(asset.id) || !/^[a-f0-9]{64}$/.test(asset.sha256) || asset.inspection?.valid !== true || !Number.isInteger(asset.inspection.solidCount) || asset.inspection.solidCount < 1 || asset.inspection.solidCount > 256) throw new Error('导入文件尚未通过真实实体校验。')
  if (!Array.isArray(position) || position.length !== 3 || position.some(value => typeof value !== 'number' || !Number.isFinite(value) || Math.abs(value) > 1e6)) throw new Error('定位需要 ±1000000 mm 内的三个坐标。')
  const next = structuredClone(plan), prefix = `import_${instanceId || createClientId().replace(/[^a-zA-Z0-9]/g, '').slice(0, 20)}`
  if (!/^import_[a-zA-Z0-9_]{1,24}$/.test(prefix) || next.features.some(item => item.id === prefix || item.id.startsWith(prefix + '_')) || Object.keys(next.parameters || {}).some(key => key.startsWith(prefix + '_'))) throw new Error('导入实例编号冲突，请重试。')
  const featureIds = [prefix]
  next.features.push({ id: prefix, op: 'import_step', label: asset.name || asset.originalFilename, assetId: asset.id, sha256: asset.sha256 })
  next.parameters ||= {}
  const vector = position.map((value, index) => { const key = `${prefix}_${['x', 'y', 'z'][index]}`; next.parameters[key] = { value, label: `${asset.name} · ${['X', 'Y', 'Z'][index]} 位移 mm` }; return key })
  const moved = `${prefix}_position`; next.features.push({ id: moved, op: 'translate', input: prefix, vector }); featureIds.push(moved)
  if (plan.features.length) {
    if (!plan.features.some(item => item.id === plan.result)) throw new Error('原图档结果引用已失效。')
    const combined = `${prefix}_bodies`; next.features.push({ id: combined, op: 'compound', inputs: [plan.result, moved] }); featureIds.push(combined); next.result = combined
  } else next.result = moved
  if (next.features.length > 128 || Object.keys(next.parameters).length > 100) throw new Error('导入后超过当前图档的特征或参数上限。')
  return { plan: next, assetId: asset.id, featureIds, label: asset.name || asset.originalFilename }
}
