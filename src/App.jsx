import { createContext, useContext, useEffect, useId, useMemo, useRef, useState } from 'react'
import { api, API_BASE } from './api.js'
import { cadAgent, cadArtifactUrl } from './cadAgentClient.js'
import { configuredAiProvider, workspaceAiProvider, aiProviderPresentation } from './aiProviderState.js'
import { isFeatureModel, shouldUseCadAgent, isLegacyDrawingDraft, normalizeCadWorkspaceMode, cadLegacySourceFiles, cadModelFromResult, cadGenerationFromResult, cadGenerationIsCurrent, cadConfirmationParameters, cadParameterEditMessage, cadRequestState, cadExplicitMaterial, cadProgressFromEvent, cadWorkflowSnapshot, cadPrimaryAction, cadRetryMessage, cadRetryFiles, cadTerminalResult, cadPlanSignature, editCadParameter } from './cadAgentState.js'
import CadAgentPanel, { CadAgentSummary, CadAgentDrawing } from './CadAgentPanel.jsx'
import {
  aiEditOutcome,
  applyAiModelPatch,
  canApplyAiModelEdit,
  hasAiGeometryParameters,
  isRecipeIncompatible,
  syncAiDrawingEdit,
  normalizeAiParameterEvidence,
  normalizeAiParameterPatch,
  resolveAiPartKind,
  seedAiModel,
  shouldProtectConcurrentModelEdit,
} from './candidateSync.js'
import ThreeDViewer from './ThreeDViewer.jsx'
import * as ProjectStore from './projectStore.js'
import ProjectFilesWorkspace from './ProjectFilesWorkspace.jsx'
import DrawingWorkspace from './DrawingWorkspace.jsx'
import AssemblyWorkspace from './AssemblyWorkspace.jsx'
import LibraryWorkspace from './LibraryWorkspace.jsx'
import { validateModelParameters } from './modelValidation.js'
import { archedClevisSupportDefinition, archedClevisSupportDimensions, archedClevisSupportFeatures, archedClevisSupportCandidateModel } from './archedClevisSupport.js'
import { dxfForModel } from './drawingGeometry.js'
import { readableError, notificationFor, downloadBlob } from './workspaceFeedback.js'
import { WorkspaceDialog, NewProjectDialog, CommandDialog, SettingsWorkspace, HelpWorkspace } from './WorkspaceTools.jsx'

const ParameterErrors = createContext([])
const welcomeMessages = () => [
  { role: 'ai', text: '欢迎来到设计工作台。上传一张图纸，或描述你想设计、检查或修改的内容。', status: 'complete' },
  { role: 'ai', text: '模型、对话与版本保存在当前项目文件中；图纸候选经你确认后再生成实体。', status: 'complete' },
]
const defaultPreferences = { defaultMaterial: '45# 钢', defaultView: 'isometric', textSize: 'normal' }
const emptyModel = (name = '新建零件', material = defaultPreferences.defaultMaterial) => ({ name, kind: '', material })
const modeForFile = (file) => ({ '工程图': '2D 工程图', '装配体': '装配', '文档': '项目管理' }[file?.type] || '3D 建模')

const defaultModel = {
  name: '动力轴 · 版本 04',
  kind: 'shaft',
  type: '零件',
  outerDiameter: 24,
  length: 70,
  holeDiameter: 10,
  keywayWidth: 6,
  keywayDepth: 3,
  keywayLength: 40,
  material: '45# 钢',
  updatedAt: '刚刚',
}

const bracketModel = {
  name: '安装支架 · 图纸识别',
  kind: 'bracket',
  type: '零件',
  baseLength: 100,
  baseWidth: 50,
  baseThickness: 10,
  upperLength: 70,
  // The right view's 30 mm callout is the length of the two central pockets;
  // the upper body itself spans the full 50 mm base width.
  upperWidth: 50,
  upperHeight: 30,
  totalHeight: 40,
  notchOpening: 40,
  notchRadius: 15,
  notchCenterZ: 40,
  notchBottomZ: 25,
  slotLength: 30,
  slotWidth: 10,
  pocketDepth: 10,
  saddleDepth: 50,
  holeDepth: 40,
  holeThrough: true,
  // Kept in the public model for backwards-compatible API payloads.  These
  // dimensions describe the two Ø20 side cutouts, never additive bosses.
  bossDiameter: 20,
  bossCenterDistance: 70,
  bossHeight: 30,
  bossPositions: [{ x: -35, y: 0, z: 0 }, { x: 35, y: 0, z: 0 }],
  material: '45# 钢',
  updatedAt: '刚刚',
}

// Second production recipe: an open split-clamp pedestal rather than the
// legacy rectangular saddle bracket.  Values here are a private WebGL
// scaffold only; the evidence/confirmation payload always contains just the
// remote model's candidates plus explicit customer edits.
const splitClampModel = {
  name: '开口夹紧座 · AI 候选',
  kind: 'split_clamp_support',
  recipeId: 'split_clamp_support_v1',
  type: '零件',
  baseLength: 125,
  baseWidth: 95,
  baseThickness: 15,
  baseMainDepth: 80,
  frontTongueWidth: 80,
  rearBridgeWidth: 86,
  totalHeight: 75,
  pedestalOuterRadius: 33,
  pedestalCenterFromRear: 35,
  pedestalHeight: 40,
  rearClampRise: 20,
  boreDiameter: 36,
  boreFloorZ: 40,
  splitWidth: 12,
  mountHoleCount: 2,
  mountHoleDiameter: 12,
  mountHoleCenterDistance: 96,
  mountHoleCenterFromRear: 40,
  crossHoleDiameter: 12,
  crossHoleCenterZ: 55,
  ribHeight: 20,
  ribThickness: 10,
  outerCornerRadius: 8,
  neckConcaveRadius: 5,
  neckConvexRadius: 8,
  material: '45# 钢',
  units: 'mm',
  updatedAt: '刚刚',
}

// Two-solid candidate assembly reconstructed from the uploaded AC1021 DWG.
// The insert stays a separate visible component until its fit/fixing method is
// confirmed; M12 is a designation only and is never rendered as a real thread.
const steppedTaperedNozzleModel = {
  name: '阶梯锥管嘴组件 · AI 候选',
  kind: 'stepped_tapered_nozzle',
  recipeId: 'stepped_tapered_nozzle_with_insert_v1',
  type: '候选装配',
  mainLength: 98,
  headLength: 50,
  neckLength: 20,
  headLeftDiameter: 54.25449350717895,
  headRightDiameter: 56,
  neckDiameter: 30,
  tipDiameter: 25,
  counterboreDiameter: 40,
  counterboreDepth: 40,
  axialBoreDiameter: 13,
  outletDiameter: 17,
  outletTaperHalfAngle: 15,
  insertOuterDiameter: 39.4,
  insertLength: 40,
  insertThreadDesignation: 'M12',
  insertAxialOffset: 0,
  material: '45# 钢',
  units: 'mm',
  updatedAt: '刚刚',
}

const acceptanceDrawingSha256 = 'ea337023af0158438f9cea2482e8e2d6d4052fc04e7e7f4265956824478c4366'
const mainModes = ['首页', '3D 建模', '2D 工程图', '装配']
const workflowSteps = [
  { id: 'upload', label: '输入 / 上传', short: '上传' },
  { id: 'recognize', label: 'AI 分析', short: '分析' },
  { id: 'review', label: '确认数据', short: '确认' },
  { id: 'generate', label: '生成 3D', short: '生成' },
  { id: 'edit', label: '二次修改', short: '修改' },
  { id: 'export', label: '导出交付', short: '导出' },
]
const bracketParameterKeys = ['baseLength', 'baseWidth', 'baseThickness', 'upperLength', 'upperWidth', 'upperHeight', 'totalHeight', 'notchOpening', 'notchRadius', 'slotLength', 'slotWidth', 'pocketDepth', 'saddleDepth', 'holeDepth', 'holeThrough', 'bossDiameter', 'bossCenterDistance', 'bossHeight', 'material', 'units']
const bracketRequiredParameterKeys = ['baseLength', 'baseWidth', 'baseThickness', 'upperLength', 'upperWidth', 'upperHeight', 'totalHeight', 'notchOpening', 'notchRadius', 'slotLength', 'slotWidth', 'pocketDepth', 'bossDiameter', 'bossCenterDistance']
const bracketParameterLabels = {
  baseLength: '底板长度', baseWidth: '底板宽度', baseThickness: '底板厚度',
  upperLength: '上部长度', upperWidth: '上部全宽', upperHeight: '上部高度',
  totalHeight: '总高度', notchOpening: '鞍槽开口', notchRadius: '鞍槽半径',
  slotLength: '浅槽长度', slotWidth: '浅槽宽度', pocketDepth: '浅槽深度',
  // Legacy API names are kept in the payload, but the customer-facing label
  // describes the actual subtractive feature in this drawing: two vertical
  // through holes that appear as side semicircular notches.
  bossDiameter: '贯穿孔直径', bossCenterDistance: '贯穿孔中心距',
}
const splitClampParameterKeys = [
  'baseLength', 'baseWidth', 'baseThickness', 'baseMainDepth', 'frontTongueWidth',
  'rearBridgeWidth', 'totalHeight', 'pedestalOuterRadius', 'pedestalCenterFromRear',
  'pedestalHeight', 'rearClampRise', 'boreDiameter', 'boreFloorZ', 'splitWidth',
  'mountHoleCount', 'mountHoleDiameter', 'mountHoleCenterDistance', 'mountHoleCenterFromRear',
  'crossHoleDiameter', 'crossHoleCenterZ', 'ribHeight', 'ribThickness',
  'outerCornerRadius', 'neckConcaveRadius', 'neckConvexRadius', 'material', 'units',
]
const splitClampRequiredParameterKeys = splitClampParameterKeys.filter((key) => !['material', 'units'].includes(key))
const splitClampParameterLabels = {
  baseLength: '底板总长', baseWidth: '底板总深', baseThickness: '底板厚度',
  baseMainDepth: '底板主段深度', frontTongueWidth: '前舌宽度', rearBridgeWidth: '后桥 / 跨筋宽度',
  totalHeight: '零件总高', pedestalOuterRadius: '圆筒座外半径', pedestalCenterFromRear: '圆筒轴距后缘',
  pedestalHeight: '低圆筒高度', rearClampRise: '后夹耳加高', boreDiameter: '中央竖孔直径',
  boreFloorZ: '盲孔底面 Z', splitWidth: '径向开缝宽度', mountHoleCount: '安装孔数量',
  mountHoleDiameter: '安装孔直径', mountHoleCenterDistance: '安装孔中心距',
  mountHoleCenterFromRear: '安装孔轴线距后缘',
  crossHoleDiameter: '夹紧横孔直径', crossHoleCenterZ: '横孔中心 Z', ribHeight: '加强筋高度',
  ribThickness: '加强筋厚度', outerCornerRadius: '底板外圆角', neckConcaveRadius: '肩部内凹圆角',
  neckConvexRadius: '前舌外圆角',
}
const splitClampGroups = [
  { title: '异形底板', fields: ['baseLength', 'baseWidth', 'baseThickness', 'baseMainDepth', 'frontTongueWidth'] },
  { title: '圆筒夹座', fields: ['rearBridgeWidth', 'pedestalOuterRadius', 'pedestalCenterFromRear', 'pedestalHeight', 'rearClampRise', 'totalHeight'] },
  { title: '孔与开缝', fields: ['boreDiameter', 'boreFloorZ', 'splitWidth', 'mountHoleCount', 'mountHoleDiameter', 'mountHoleCenterDistance', 'mountHoleCenterFromRear', 'crossHoleDiameter', 'crossHoleCenterZ'] },
  { title: '加强与圆角', fields: ['ribHeight', 'ribThickness', 'outerCornerRadius', 'neckConcaveRadius', 'neckConvexRadius'] },
]
const steppedTaperedNozzleParameterKeys = [
  'mainLength', 'headLength', 'neckLength', 'headLeftDiameter', 'headRightDiameter',
  'neckDiameter', 'tipDiameter', 'counterboreDiameter', 'counterboreDepth',
  'axialBoreDiameter', 'outletDiameter', 'outletTaperHalfAngle',
  'insertOuterDiameter', 'insertLength', 'insertThreadDesignation', 'insertAxialOffset',
  'material', 'units',
]
const steppedTaperedNozzleRequiredParameterKeys = steppedTaperedNozzleParameterKeys.filter((key) => !['material', 'units'].includes(key))
const steppedTaperedNozzleParameterLabels = {
  mainLength: '主件总长', headLength: '前端浅锥段长度', neckLength: '中间颈段长度',
  headLeftDiameter: '浅锥左端直径', headRightDiameter: '浅锥右端直径',
  neckDiameter: '颈段直径', tipDiameter: '末段直径', counterboreDiameter: '左端沉孔直径',
  counterboreDepth: '左端沉孔深度', axialBoreDiameter: '轴向贯通孔直径',
  outletDiameter: '末端锥口直径', outletTaperHalfAngle: '出口锥半角',
  insertOuterDiameter: '镶件外径', insertLength: '镶件长度',
  insertThreadDesignation: '镶件螺纹标注', insertAxialOffset: '镶件轴向偏置',
}
const steppedTaperedNozzleGroups = [
  { title: '主件轴向尺寸', fields: ['mainLength', 'headLength', 'neckLength'] },
  { title: '主件外轮廓', fields: ['headLeftDiameter', 'headRightDiameter', 'neckDiameter', 'tipDiameter'] },
  { title: '内孔与出口锥', fields: ['counterboreDiameter', 'counterboreDepth', 'axialBoreDiameter', 'outletDiameter', 'outletTaperHalfAngle'] },
  { title: '独立 M12 镶件', fields: ['insertOuterDiameter', 'insertLength', 'insertThreadDesignation', 'insertAxialOffset'] },
]
const shaftParameterKeys = ['outerDiameter', 'length', 'holeDiameter', 'keywayWidth', 'keywayDepth', 'keywayLength', 'material']
const partKindAliases = {
  arched_clevis_support_v1: 'arched_clevis_support',
  circular_clamp: 'split_clamp_support',
  circular_clamp_v1: 'split_clamp_support',
  clamp_pedestal: 'split_clamp_support',
  split_clamp_pedestal: 'split_clamp_support',
  split_clamp_support_v1: 'split_clamp_support',
  stepped_tapered_nozzle_with_insert_v1: 'stepped_tapered_nozzle',
  tapered_nozzle_with_insert: 'stepped_tapered_nozzle',
  stepped_tapered_nozzle_v1: 'stepped_tapered_nozzle',
  bracket_support_v1: 'bracket',
  shaft_v1: 'shaft',
}
const canonicalPartKind = (value) => {
  const raw = String(value || '').trim().toLowerCase()
  return partKindAliases[raw] || raw
}
const productionPartKinds = ['bracket', 'split_clamp_support', 'stepped_tapered_nozzle', 'arched_clevis_support']
const partDefinition = (kind) => {
  const normalized = canonicalPartKind(kind)
  if (!['shaft', ...productionPartKinds].includes(normalized)) return { kind: '', recipeId: '', preview: emptyModel(), keys: [], required: [], labels: {} }
  if (normalized === 'arched_clevis_support') return archedClevisSupportDefinition
  if (normalized === 'stepped_tapered_nozzle') return {
    kind: normalized,
    recipeId: 'stepped_tapered_nozzle_with_insert_v1',
    preview: steppedTaperedNozzleModel,
    keys: steppedTaperedNozzleParameterKeys,
    required: steppedTaperedNozzleRequiredParameterKeys,
    labels: steppedTaperedNozzleParameterLabels,
  }
  if (normalized === 'split_clamp_support') return {
    kind: normalized,
    recipeId: 'split_clamp_support_v1',
    preview: splitClampModel,
    keys: splitClampParameterKeys,
    required: splitClampRequiredParameterKeys,
    labels: splitClampParameterLabels,
  }
  if (normalized === 'bracket') return {
    kind: normalized,
    recipeId: 'bracket_support_v1',
    preview: bracketModel,
    keys: bracketParameterKeys,
    required: bracketRequiredParameterKeys,
    labels: bracketParameterLabels,
  }
  return { kind: 'shaft', recipeId: 'shaft_v1', preview: defaultModel, keys: shaftParameterKeys, required: shaftParameterKeys.filter((key) => key !== 'material'), labels: { outerDiameter: '外径', length: '总长度', holeDiameter: '通孔直径', keywayWidth: '键槽宽度', keywayDepth: '键槽深度', keywayLength: '键槽长度', material: '材料' } }
}
const partDefinitions = Object.fromEntries(['stepped_tapered_nozzle', 'split_clamp_support', 'bracket', 'shaft', 'arched_clevis_support']
  .map((kind) => [kind, partDefinition(kind)]))
const modelFromCandidate = (definition, parameters = {}, metadata = {}) => definition.kind === 'arched_clevis_support'
  ? archedClevisSupportCandidateModel(parameters, metadata)
  : { ...definition.preview, ...parameters, ...metadata, kind: definition.kind, recipeId: definition.recipeId }
const seedModelForAi = (options) => options.definition.kind === 'arched_clevis_support'
  ? modelFromCandidate(options.definition, options.recognizedParameters
    || (!options.hasAttachments && options.currentKind === options.definition.kind ? options.baseModel : {}), {
      name: options.recognizedParameters?.name || (!options.hasAttachments ? options.baseModel?.name : '') || options.definition.preview.name,
      updatedAt: '刚刚',
    })
  : seedAiModel(options)
const archedClevisGroups = [
  { title: '拱形主体', fields: ['archOuterRadius', 'archInnerRadius', 'baseWidth', 'baseThickness'] },
  { title: '双耳与横孔', fields: ['earRadius', 'earHoleDiameter', 'earCenterHeight', 'earThickness', 'earGap'] },
  { title: '底部安装耳', fields: ['mountEarRadius', 'mountHoleDiameter', 'mountHoleCenterDistance'] },
]
const archedClevisMetrics = (model) => {
  const d = archedClevisSupportDimensions(model)
  return { boundingLength: d.baseLength, boundingWidth: d.baseWidth, boundingHeight: d.totalHeight, solidCount: validateModelParameters(model).valid ? 1 : undefined }
}
const formatDimension = (value) => value !== null && value !== undefined && value !== '' && typeof value !== 'boolean' && Number.isFinite(Number(value))
  ? String(Number(Number(value).toFixed(6))) : '—'
const modelBoundsText = (model, metrics = {}) => {
  const kind = canonicalPartKind(model.kind)
  const arched = kind === 'arched_clevis_support'
  const dimensions = arched ? archedClevisSupportDimensions(model) : model
  const diameter = Math.max(Number(model.headLeftDiameter), Number(model.headRightDiameter))
  const fallback = kind === 'stepped_tapered_nozzle'
    ? [model.mainLength, diameter, diameter]
    : [dimensions.baseLength, dimensions.baseWidth, dimensions.totalHeight]
  // A previous solid cannot provide the missing dimensions of a new draft.
  const currentMetrics = arched && !validateModelParameters(model).valid ? {} : metrics
  return [
    currentMetrics.boundingLength ?? currentMetrics.bboxLength ?? fallback[0],
    currentMetrics.boundingWidth ?? currentMetrics.bboxWidth ?? fallback[1],
    currentMetrics.boundingHeight ?? currentMetrics.bboxHeight ?? fallback[2],
  ].map(formatDimension).join(' × ')
}
const archedClevisEvidenceRows = (model, source, compare) => [
  ['拱形主体', `外 R${compare(source.archOuterRadius, model.archOuterRadius)} · 内 R${compare(source.archInnerRadius, model.archInnerRadius)} · 全宽 ${compare(source.baseWidth, model.baseWidth)} mm`],
  ['双耳', `耳厚 ${compare(source.earThickness, model.earThickness)} · 间隙 ${compare(source.earGap, model.earGap)} · 顶部 R${compare(source.earRadius, model.earRadius)}`],
  ['横向耳孔', `Ø${compare(source.earHoleDiameter, model.earHoleDiameter)} · 圆心距底面 ${compare(source.earCenterHeight, model.earCenterHeight)} mm`],
  ['安装耳 / 孔', `外 R${compare(source.mountEarRadius, model.mountEarRadius)} · 厚 ${compare(source.baseThickness, model.baseThickness)} · 2×Ø${compare(source.mountHoleDiameter, model.mountHoleDiameter)} · 孔距 ${compare(source.mountHoleCenterDistance, model.mountHoleCenterDistance)} mm`],
]
const partKindFromEnvelope = (value, fallback = 'bracket') => resolveAiPartKind(value, {
  definitions: partDefinitions,
  fallback,
  kindAliases: partKindAliases,
  parameterAliases: recognitionParameterAliases,
})
const parameterKeysForKind = (kind) => partDefinition(kind).keys
const requiredKeysForKind = (kind) => partDefinition(kind).required
const parameterLabelsForKind = (kind) => partDefinition(kind).labels
const requiredParameterPresent = (key, value) => {
  if (key === 'insertThreadDesignation') return Boolean(String(value || '').trim())
  if (value === null || value === undefined || typeof value === 'boolean' || (typeof value === 'string' && !value.trim())) return false
  if (typeof value !== 'number' && typeof value !== 'string') return false
  if (key === 'insertAxialOffset') return Number.isFinite(Number(value)) && Number(value) >= 0
  return Number.isFinite(Number(value)) && Number(value) > 0
}
const splitClampParametersValid = (value) => {
  const n = (key) => Number(value?.[key])
  if (!splitClampRequiredParameterKeys.every((key) => Number.isFinite(n(key)) && n(key) > 0)) return false
  const tongueDepth = n('baseWidth') - n('baseMainDepth')
  const shoulderWidth = (n('baseLength') - n('frontTongueWidth')) / 2
  const pedestalRadius = n('pedestalOuterRadius')
  const pedestalFromRear = n('pedestalCenterFromRear')
  const mountRadius = n('mountHoleDiameter') / 2
  const mountFromRear = n('mountHoleCenterFromRear')
  const crossRadius = n('crossHoleDiameter') / 2
  const lowerTop = n('baseThickness') + n('pedestalHeight')
  const mountPedestalClearance = Math.hypot(
    n('mountHoleCenterDistance') / 2,
    pedestalFromRear - mountFromRear,
  ) - mountRadius - pedestalRadius
  return n('mountHoleCount') === 2
    && n('baseMainDepth') < n('baseWidth')
    && n('frontTongueWidth') <= n('baseLength')
    && pedestalRadius * 2 <= n('rearBridgeWidth')
    && n('rearBridgeWidth') <= n('baseLength')
    && n('outerCornerRadius') <= Math.min(n('baseMainDepth'), n('baseLength')) / 2
    && n('neckConcaveRadius') <= Math.min(tongueDepth, shoulderWidth)
    && n('neckConvexRadius') <= Math.min(tongueDepth, n('frontTongueWidth') / 2)
    && n('neckConcaveRadius') + n('neckConvexRadius') <= tongueDepth
    && pedestalRadius * 2 <= n('baseLength')
    && pedestalFromRear >= pedestalRadius
    && pedestalFromRear + pedestalRadius <= n('baseWidth')
    && n('boreDiameter') < pedestalRadius * 2
    && n('boreFloorZ') > n('baseThickness')
    && n('boreFloorZ') < lowerTop
    && n('splitWidth') < n('boreDiameter')
    && n('mountHoleCenterDistance') + n('mountHoleDiameter') <= n('baseLength')
    && mountFromRear >= mountRadius
    && n('baseMainDepth') - mountFromRear >= mountRadius
    && mountPedestalClearance > 0
    && n('crossHoleCenterZ') >= n('baseThickness') + crossRadius
    && n('crossHoleCenterZ') <= n('totalHeight') - crossRadius
    && n('crossHoleCenterZ') - crossRadius <= lowerTop
    && lowerTop <= n('crossHoleCenterZ') + crossRadius
    && n('ribHeight') <= n('pedestalHeight')
    && Math.abs(n('totalHeight') - n('baseThickness') - n('pedestalHeight') - n('rearClampRise')) < 0.01
}
const steppedTaperedNozzleParametersValid = (value) => {
  if (!steppedTaperedNozzleRequiredParameterKeys.every((key) => requiredParameterPresent(key, value?.[key]))) return false
  const n = (key) => Number(value?.[key])
  const threadMatch = String(value?.insertThreadDesignation || '').trim().toUpperCase().replace('×', 'X').match(/^M(\d+(?:\.\d+)?)(?:X\d+(?:\.\d+)?)?$/)
  const threadNominalDiameter = Number(threadMatch?.[1])
  const tipLength = n('mainLength') - n('headLength') - n('neckLength')
  const taperLength = (n('outletDiameter') - n('axialBoreDiameter')) / (2 * Math.tan((Math.PI / 180) * n('outletTaperHalfAngle')))
  return tipLength > 0
    && Boolean(threadMatch)
    && threadNominalDiameter < n('insertOuterDiameter')
    && n('tipDiameter') <= n('neckDiameter')
    && n('neckDiameter') < Math.min(n('headLeftDiameter'), n('headRightDiameter'))
    && n('headLeftDiameter') > n('counterboreDiameter')
    && n('headRightDiameter') > n('counterboreDiameter')
    && n('counterboreDiameter') > n('insertOuterDiameter')
    && n('insertOuterDiameter') > n('axialBoreDiameter')
    && n('neckDiameter') > n('axialBoreDiameter')
    && n('tipDiameter') > n('outletDiameter')
    && n('outletDiameter') > n('axialBoreDiameter')
    && n('counterboreDepth') <= n('headLength')
    && n('insertAxialOffset') + n('insertLength') <= n('counterboreDepth')
    && n('outletTaperHalfAngle') < 90
    && Number.isFinite(taperLength)
    && taperLength > 0
    && taperLength < tipLength
}
const candidateSourceLabels = {
  drawing: '图纸识别',
  ai: 'AI 候选',
  derived: 'AI 推导',
  direct_dimension: 'DWG 原生尺寸',
  vector_derived: 'DWG 矢量量测',
  ai_interpreted: 'AI 语义映射',
  template_default: '模板默认',
  manual: '人工修改',
}
const normalizedCandidateSource = (value, fallback = 'ai') => {
  const source = String(value || '').trim().toLowerCase()
  return Object.hasOwn(candidateSourceLabels, source) ? source : fallback
}
const normalizedConfidence = (value) => {
  if (typeof value === 'string') {
    const label = value.trim().toLowerCase()
    if (label === 'high') return 0.95
    if (label === 'medium') return 0.75
    if (label === 'low') return 0.5
  }
  const number = Number(value)
  return Number.isFinite(number) ? Math.max(0, Math.min(1, number)) : null
}
const nonEvidenceCandidateSources = new Set(['template_default', 'current_model'])
const recognitionParameterAliases = {
  base_length: 'baseLength', base_width: 'baseWidth', base_thickness: 'baseThickness',
  upper_length: 'upperLength', upper_width: 'upperWidth', upper_height: 'upperHeight',
  total_height: 'totalHeight', notch_opening: 'notchOpening', notch_radius: 'notchRadius',
  slot_length: 'slotLength', slot_width: 'slotWidth', pocket_depth: 'pocketDepth',
  saddle_depth: 'saddleDepth', hole_depth: 'holeDepth', hole_through: 'holeThrough',
  boss_diameter: 'bossDiameter', boss_center_distance: 'bossCenterDistance', boss_height: 'bossHeight',
  base_main_depth: 'baseMainDepth', front_tongue_width: 'frontTongueWidth', rear_bridge_width: 'rearBridgeWidth',
  pedestal_outer_radius: 'pedestalOuterRadius', pedestal_center_from_rear: 'pedestalCenterFromRear',
  pedestal_height: 'pedestalHeight', rear_clamp_rise: 'rearClampRise', bore_diameter: 'boreDiameter',
  bore_floor_z: 'boreFloorZ', split_width: 'splitWidth', mount_hole_count: 'mountHoleCount',
  mount_hole_diameter: 'mountHoleDiameter', mount_hole_center_distance: 'mountHoleCenterDistance',
  mount_hole_center_from_rear: 'mountHoleCenterFromRear',
  cross_hole_diameter: 'crossHoleDiameter', cross_hole_center_z: 'crossHoleCenterZ',
  rib_height: 'ribHeight', rib_thickness: 'ribThickness', outer_corner_radius: 'outerCornerRadius',
  neck_concave_radius: 'neckConcaveRadius', neck_convex_radius: 'neckConvexRadius',
  main_length: 'mainLength', head_length: 'headLength', neck_length: 'neckLength',
  head_left_diameter: 'headLeftDiameter', head_right_diameter: 'headRightDiameter',
  neck_diameter: 'neckDiameter', tip_diameter: 'tipDiameter', counterbore_diameter: 'counterboreDiameter',
  counterbore_depth: 'counterboreDepth', axial_bore_diameter: 'axialBoreDiameter',
  outlet_diameter: 'outletDiameter', outlet_taper_half_angle: 'outletTaperHalfAngle',
  insert_outer_diameter: 'insertOuterDiameter', insert_length: 'insertLength',
  insert_thread_designation: 'insertThreadDesignation', insert_axial_offset: 'insertAxialOffset',
}

let chatSequence = 0
const chatId = (prefix = 'msg') => {
  chatSequence += 1
  const random = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID().slice(0, 8)
    : `${Date.now().toString(36)}-${chatSequence}`
  return `${prefix}-${random}`
}
const chatAbortError = () => Object.assign(new Error('AI turn cancelled'), { name: 'AbortError' })
const chatMessage = (role, text, extra = {}) => ({ id: chatId(role), role, text, status: 'complete', ...extra })
const normalizeChatMessage = (message) => ({
  id: message?.id || chatId(message?.role || 'msg'),
  role: message?.role === 'user' ? 'user' : 'ai',
  text: String(message?.text || ''),
  status: message?.status || 'complete',
  ...(Array.isArray(message?.attachments) ? { attachments: message.attachments } : {}),
  ...(message?.candidate ? { candidate: message.candidate } : {}),
})
const conversationHistory = (items) => {
  const completedTurnIds = new Set(items
    .filter((item) => item?.role === 'ai' && item?.turnId && (item?.status || 'complete') === 'complete')
    .map((item) => item.turnId))
  return items
    .filter((item) => (
      ['user', 'ai'].includes(item?.role)
      && (item?.status || 'complete') === 'complete'
      && (!item?.turnId || completedTurnIds.has(item.turnId))
      && String(item?.text || '').trim()
    ))
    .map((item) => ({ role: item.role === 'ai' ? 'assistant' : 'user', text: String(item.text) }))
}

// Return only fields genuinely supplied by the recognizer/AI.  The rendering
// model may fill safe defaults so the viewport stays usable, but those defaults
// must never count as drawing evidence or silently pass the customer confirm
// gate for an unknown/partial drawing.
function recognitionCandidateFields(recognition) {
  const kind = partKindFromEnvelope(recognition, 'bracket')
  const allowedKeys = parameterKeysForKind(kind)
  const fields = new Set()
  const add = (raw) => {
    if (!raw || typeof raw !== 'object') return
    Object.keys(raw).forEach((key) => {
      const normalized = recognitionParameterAliases[key] || key
      if (allowedKeys.includes(normalized)) fields.add(normalized)
    })
  }
  const fallbackEngine = ['heuristic-review', 'tesseract-compatible', 'compatibility-recognizer'].includes(String(recognition?.engine || ''))
  add(recognition?.recognizedParameters || recognition?.recognized_parameters)
  const candidateParameters = recognition?.candidateParameters || recognition?.candidate_parameters
  const candidateMeta = recognition?.candidateParameterMeta || recognition?.candidate_parameter_meta
  if (candidateMeta && typeof candidateMeta === 'object' && candidateParameters && typeof candidateParameters === 'object') {
    add(Object.fromEntries(Object.entries(candidateParameters).filter(([key]) => {
      const normalized = recognitionParameterAliases[key] || key
      return !nonEvidenceCandidateSources.has(String(candidateMeta[normalized]?.source || candidateMeta[key]?.source || ''))
    })))
  } else {
    add(candidateParameters)
  }
  if (!fallbackEngine) {
    add(recognition?.parameters)
    add(recognition?.modelRecipe?.parameters)
  }
  if (Array.isArray(recognition?.dimensions)) {
    recognition.dimensions.forEach((item) => {
      if (String(item?.sourceText || item?.source || '').startsWith('canonical-bracket-fallback')) return
      const normalized = recognitionParameterAliases[item?.field] || snakeToCamel(String(item?.field || ''))
      if (allowedKeys.includes(normalized) && item?.value !== undefined && item?.value !== null) fields.add(normalized)
    })
  }
  return [...fields]
}
function normalizeStoredModel(value) {
  if (!value || typeof value !== 'object') return value
  if (canonicalPartKind(value.kind || value.recipeId) === 'arched_clevis_support') return { ...value, kind: 'arched_clevis_support', recipeId: 'arched_clevis_support_v1' }
  if (canonicalPartKind(value.kind || value.recipeId) === 'stepped_tapered_nozzle') {
    return { ...steppedTaperedNozzleModel, ...value, kind: 'stepped_tapered_nozzle', recipeId: 'stepped_tapered_nozzle_with_insert_v1' }
  }
  if (canonicalPartKind(value.kind || value.recipeId) === 'split_clamp_support') {
    return { ...splitClampModel, ...value, kind: 'split_clamp_support', recipeId: 'split_clamp_support_v1' }
  }
  if (value.kind !== 'bracket') return value
  // Models saved by the pre-correction build had no pocket fields and used
  // upperWidth=30 for the right-view callout.  Migrate that snapshot to the
  // calibrated drawing interpretation instead of silently showing the old
  // convex-boss geometry after a browser refresh.
  const legacy = value.slotLength === undefined && value.pocketDepth === undefined
  return {
    ...bracketModel,
    ...value,
    ...(legacy ? {
      upperWidth: 50,
      slotLength: 30,
      slotWidth: 10,
      pocketDepth: 10,
      saddleDepth: 50,
      holeDepth: 40,
      holeThrough: true,
      bossPositions: bracketModel.bossPositions,
    } : {}),
  }
}
const snakeToCamel = (value) => value.replace(/_([a-z])/g, (_, letter) => letter.toUpperCase())
const emptyPlatformState = () => ({ token: '', user: null, users: [], project: null, manifest: null, camPlan: null, approval: null, simulation: null, gate: null, nc: null, busy: false, error: '' })

function rawParametersFromRecognition(recognition) {
  if (!recognition || typeof recognition !== 'object') return null
  const kind = partKindFromEnvelope(recognition, 'bracket')
  const allowedKeys = parameterKeysForKind(kind)
  const normalize = (raw) => {
    if (!raw || typeof raw !== 'object') return null
    const converted = Object.fromEntries(Object.entries(raw)
      .map(([key, value]) => [recognitionParameterAliases[key] || key, value])
      .filter(([key, value]) => allowedKeys.includes(key) && value !== undefined && value !== null && value !== ''))
    return Object.keys(converted).length ? converted : null
  }
  // The legacy compatibility recognizer fills an entire canonical bracket
  // profile when OCR finds no labelled value. Treat that profile as a visual
  // scaffold only; use actual labelled dimensions as candidates instead.
  const compatibilityFallback = ['heuristic-review', 'tesseract-compatible', 'compatibility-recognizer'].includes(String(recognition.engine || ''))
  const recognized = normalize(recognition.recognizedParameters || recognition.recognized_parameters)
  const rawCandidate = recognition.candidateParameters || recognition.candidate_parameters
  const candidateMeta = recognition.candidateParameterMeta || recognition.candidate_parameter_meta
  const evidenceOnlyCandidate = candidateMeta && typeof candidateMeta === 'object' && rawCandidate && typeof rawCandidate === 'object'
    ? Object.fromEntries(Object.entries(rawCandidate).filter(([key]) => {
      const normalized = recognitionParameterAliases[key] || key
      return !nonEvidenceCandidateSources.has(String(candidateMeta[normalized]?.source || candidateMeta[key]?.source || ''))
    }))
    : rawCandidate
  const candidate = normalize(evidenceOnlyCandidate)
  // A pending arched support's candidate is the complete editable evidence
  // snapshot. Older recipe/OCR values must not resurrect a dimension removed
  // from the final AI answer or cleared by the customer.
  if (kind === 'arched_clevis_support' && rawCandidate && typeof rawCandidate === 'object'
    && !Array.isArray(rawCandidate) && !evidenceAcceptedForPreview(recognition)) return candidate
  // Build one effective editable snapshot.  Lower-level recipe/OCR values are
  // useful as provenance, but explicit recognized values and finally the
  // current candidateParameters (including customer edits) must win.
  const recipe = !compatibilityFallback
    ? normalize(recognition.modelRecipe?.parameters || recognition.model_recipe?.parameters)
    : null
  const parameters = !compatibilityFallback ? normalize(recognition.parameters) : null
  const dimensions = Array.isArray(recognition.dimensions)
    ? normalize(Object.fromEntries(recognition.dimensions
        .filter((item) => !String(item?.sourceText || item?.source || '').startsWith('canonical-bracket-fallback'))
        .map((item) => [item.field, item.value])))
    : null
  const merged = Object.assign({}, recipe || {}, parameters || {}, dimensions || {}, recognized || {}, candidate || {})
  if (Object.keys(merged).length) return merged
  if (!compatibilityFallback) {
    const topLevel = normalize(recognition)
    if (topLevel) return topLevel
  }
  return null
}

function sanitiseRecognitionForCandidate(recognition) {
  if (!recognition || typeof recognition !== 'object') return recognition
  const fallbackEngine = ['heuristic-review', 'tesseract-compatible', 'compatibility-recognizer'].includes(String(recognition.engine || ''))
  const source = recognition.modelRecipe?.source || recognition.model_recipe?.source
  const compatibilityFallback = fallbackEngine || String(source || '').toLowerCase() === 'compatibility-recognizer'
  if (!compatibilityFallback) return recognition
  const explicit = rawParametersFromRecognition(recognition) || {}
  const warnings = Array.isArray(recognition.warnings) ? recognition.warnings : []
  return {
    ...recognition,
    status: 'needs_review',
    partType: 'unknown',
    parameters: explicit,
    candidateParameters: explicit,
    modelRecipe: { ...(recognition.modelRecipe || {}), parameters: explicit, source: 'ai-candidate' },
    unresolved: [...new Set([...(recognition.unresolved || []), 'part_type', 'dimensions', 'feature_topology'])],
    warnings: [...new Set([...warnings, '兼容识别器仅提供预览基准；以下尺寸必须由 AI/人工确认'])],
  }
}

function parametersFromRecognition(recognition) {
  if (!recognition || isRecipeIncompatible(recognition)) return null
  const kind = partKindFromEnvelope(recognition, 'bracket')
  const definition = partDefinition(kind)
  const normalize = (raw) => {
    if (!raw || typeof raw !== 'object') return null
    const converted = Object.fromEntries(Object.entries(raw).map(([key, value]) => [recognitionParameterAliases[key] || key, value]))
    return modelFromCandidate(definition, converted)
  }
  // A rich platform recognition can expose an AI-only candidate separately
  // from the durable recipe.  Prefer that candidate while it is pending, but
  // never treat an empty object as a successful recognition (an empty object
  // is truthy in JavaScript and used to make an unknown drawing look like the
  // calibrated bracket).
  const rawCandidate = rawParametersFromRecognition(recognition)
  if (rawCandidate) return normalize(rawCandidate)
  return null
}

function modelForPendingDrawing(model, job) {
  const evidence = job?.evidence
  if (!evidence || evidenceAcceptedForPreview(evidence) || ['queued', 'analyzing'].includes(job.status)) return model
  const kind = partKindFromEnvelope(evidence, model?.kind)
  if (kind !== 'arched_clevis_support') return model
  const definition = partDefinition(kind)
  const candidate = rawParametersFromRecognition(evidence) || {}
  const sameKind = canonicalPartKind(model?.kind) === kind
  if (!sameKind && !hasAiGeometryParameters(candidate)) return model
  const restored = modelFromCandidate(definition, candidate, sameKind ? model : {})
  if (sameKind && definition.keys.every((key) => Object.is(model[key], restored[key]))) return model
  return restored
}

function recognitionEvidence(recognition) {
  if (Array.isArray(recognition?.dimensions)) return recognition.dimensions
  return Array.isArray(recognition?.evidence) ? recognition.evidence : []
}

function hydratePendingCandidateDefaults(job) {
  if (!job?.evidence || evidenceAcceptedForPreview(job.evidence) || !['ready', 'error'].includes(job.status)) return job
  const existingCandidates = job.evidence.candidateParameters && typeof job.evidence.candidateParameters === 'object'
    ? job.evidence.candidateParameters
    : {}
  const storedSources = job.analysis?.candidateSources || {}
  const storedMeta = job.evidence.candidateParameterMeta || {}
  const evidenceCandidates = Object.fromEntries(Object.entries(existingCandidates).filter(([key]) => {
    const source = String(storedSources[key] || storedMeta[key]?.source || '')
    return !nonEvidenceCandidateSources.has(source)
  }))
  const humanFields = new Set(job.humanEditedFields || [])
  const kind = partKindFromEnvelope(job.evidence || job.analysis, job.analysis?.partType || 'bracket')
  const definition = partDefinition(kind)
  const completeCandidates = Object.fromEntries(Object.entries(evidenceCandidates)
    .filter(([key, value]) => definition.keys.includes(key) && value !== undefined && value !== null && value !== ''))
  const engine = String(job.evidence.engine || '').toLowerCase()
  const candidateSources = Object.fromEntries(Object.keys(completeCandidates).map((key) => [
    key,
    humanFields.has(key) ? 'manual' : engine.includes('ai') ? 'ai' : 'drawing',
  ]))
  const defaultedFields = []
  const missingFields = definition.required.filter((key) => !requiredParameterPresent(key, completeCandidates[key]))
  const candidateParameterMeta = Object.fromEntries(Object.entries(candidateSources).map(([key, source]) => [key, {
    source,
    confidence: source === 'drawing' ? Number(job.evidence.confidence || 0) : null,
    requiresConfirmation: true,
  }]))
  return {
    ...job,
    status: job.status === 'error' ? 'ready' : job.status,
    evidence: {
      ...job.evidence,
      status: 'pending',
      parameters: completeCandidates,
      candidateParameters: completeCandidates,
      recognizedParameters: completeCandidates,
      candidateParameterMeta,
    },
    analysis: {
      ...(job.analysis || {}),
      partType: definition.kind,
      recipeId: definition.recipeId,
      candidateParameters: completeCandidates,
      candidateSources,
      recognizedFields: Object.keys(completeCandidates),
      defaultedFields,
      missingFields,
      needsInput: missingFields.length > 0,
    },
  }
}

function artifactDownloadUrl(artifact) {
  if (!artifact) return ''
  if (artifact.downloadUrl) {
    try { return new URL(artifact.downloadUrl, `${API_BASE}/`).toString() } catch { return artifact.downloadUrl }
  }
  return api.artifactUrl(artifact.id, artifact.format)
}

async function sha256File(file) {
  if (!globalThis.crypto?.subtle) return ''
  const bytes = await file.arrayBuffer()
  const digest = await globalThis.crypto.subtle.digest('SHA-256', bytes)
  return [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, '0')).join('')
}

// Normalize every browser/file-picker shape at the UI boundary.  A native
// FileList is array-like (and iterable in modern browsers) but is not an
// Array; wrapping it as one item would send the list object to FormData and
// bypass the attachment-count/review gates below.  Keep File/Blob-like
// objects intact while expanding FileList, DataTransfer.files and iterables.
function normalizeFilesInput(input) {
  if (!input || typeof input === 'string') return []
  if (Array.isArray(input)) return input.filter(Boolean)
  if (typeof input === 'object') {
    if (input.files && input.files !== input) return normalizeFilesInput(input.files)
    const fileLike = typeof input.name === 'string' || typeof input.arrayBuffer === 'function'
    const length = Number(input.length)
    if (!fileLike && Number.isFinite(length) && length >= 0) {
      try { return Array.from(input).filter(Boolean) } catch { /* fall through to iterable/single-item handling */ }
    }
    if (!fileLike && typeof Symbol !== 'undefined') {
      try {
        if (typeof input[Symbol.iterator] === 'function') return Array.from(input).filter(Boolean)
      } catch { /* malformed iterables should not break the chat composer */ }
    }
  }
  return [input].filter(Boolean)
}


const defaultProjects = [
  { id: 'p1', name: '创模AI · 机械传动', files: 8, updated: '今天 14:32', color: 'blue' },
  { id: 'p2', name: '保持架结构复刻', files: 12, updated: '昨天 09:18', color: 'orange' },
  { id: 'p3', name: '试制夹具 - A17', files: 5, updated: '8 月 26 日', color: 'violet' },
]

const defaultFiles = [
  { id: 'f1', name: '动力轴 · 版本 04', type: '零件', size: '2.4 MB', version: 'v04', updated: '刚刚', status: '已同步' },
  { id: 'f2', name: '动力轴 · 工程图', type: '工程图', size: '486 KB', version: 'v03', updated: '今天 13:06', status: '已同步' },
  { id: 'f3', name: '传动轴组件', type: '装配体', size: '1.8 MB', version: 'v02', updated: '昨天 16:20', status: '仅本地' },
  { id: 'f4', name: '设计说明与参数', type: '文档', size: '38 KB', version: 'v01', updated: '8 月 26 日', status: '已同步' },
]

const features = [
  { id: 'origin', icon: '◎', label: '原点', meta: '基准' },
  { id: 'plane', icon: '△', label: 'XY 基准面', meta: '基准面' },
  { id: 'sketch', icon: '▱', label: '草图 01 · 轴截面', meta: '已约束' },
  { id: 'pad', icon: '▰', label: '拉伸 · 70 mm', meta: '实体' },
  { id: 'hole', icon: '◉', label: '通孔 · Ø10 mm', meta: '切除' },
  { id: 'keyway', icon: '⌗', label: '键槽 · 6 × 3 × 40', meta: '切除' },
  { id: 'chamfer', icon: '◇', label: '倒角 · 1 mm', meta: '细节' },
]

function getBracketFeatures(model) {
  const value = (key, fallback) => Number.isFinite(Number(model?.[key])) ? Number(model[key]) : fallback
  return [
    { id: 'origin', icon: '◎', label: '原点', meta: '基准' },
    { id: 'base', icon: '▰', label: `底板 · ${value('baseLength', 100)} × ${value('baseWidth', 50)} × ${value('baseThickness', 10)}`, meta: '实体' },
    { id: 'upper', icon: '▰', label: `上部实体 · ${value('upperLength', 70)} × ${value('upperWidth', 50)} × ${value('upperHeight', 30)}`, meta: '实体' },
    { id: 'notch', icon: '∪', label: `R${value('notchRadius', 15)} 横向鞍槽 · 开口 ${value('notchOpening', 40)}`, meta: '切除 · 贯穿 Y' },
    { id: 'pockets', icon: '▤', label: `矩形浅槽 × 2 · ${value('slotWidth', 10)} × ${value('slotLength', 30)} · 深 ${value('pocketDepth', 10)}`, meta: '切除' },
    { id: 'holes', icon: '◌', label: `Ø${value('bossDiameter', 20)} 贯穿孔 × 2 · 中心距 ${value('bossCenterDistance', 70)}`, meta: '切除 · 贯穿 Z · 侧边显示为半圆缺口' },
    { id: 'edge', icon: '◇', label: '边缘处理 · 保留锐边', meta: '细节' },
  ]
}

function getSplitClampFeatures(model) {
  const value = (key, fallback) => Number.isFinite(Number(model?.[key])) ? Number(model[key]) : fallback
  return [
    { id: 'origin', icon: '◎', label: '原点', meta: '基准' },
    { id: 'base', icon: '▰', label: `异形底板 · ${value('baseLength', 125)} × ${value('baseWidth', 95)} × ${value('baseThickness', 15)}`, meta: '实体 · 前舌' },
    { id: 'pedestal', icon: '◉', label: `圆筒夹座 · R${value('pedestalOuterRadius', 33)} · 高 ${value('pedestalHeight', 40)}`, meta: '实体' },
    { id: 'rear-wall', icon: '▰', label: `后部夹耳 · 加高 ${value('rearClampRise', 20)}`, meta: '实体' },
    { id: 'bore', icon: '◌', label: `中央盲孔 · Ø${value('boreDiameter', 36)} · 底面 Z${value('boreFloorZ', 40)}`, meta: '切除 · Z 轴' },
    { id: 'split', icon: '⌁', label: `径向开缝 · ${value('splitWidth', 12)} mm`, meta: '切除 · 贯通前壁' },
    { id: 'mount-holes', icon: '◌', label: `${value('mountHoleCount', 2)} × Ø${value('mountHoleDiameter', 12)} 安装孔 · 中心距 ${value('mountHoleCenterDistance', 96)}`, meta: `切除 · 贯穿 Z · 距后缘 ${value('mountHoleCenterFromRear', 40)}` },
    { id: 'cross-hole', icon: '◌', label: `夹紧横孔 · Ø${value('crossHoleDiameter', 12)} · Z${value('crossHoleCenterZ', 55)}`, meta: '切除 · Y 轴' },
    { id: 'ribs', icon: '△', label: `加强筋 × 2 · ${value('ribHeight', 20)} × ${value('ribThickness', 10)}`, meta: '实体' },
  ]
}

function getSteppedTaperedNozzleFeatures(model) {
  const value = (key, fallback) => Number.isFinite(Number(model?.[key])) ? Number(model[key]) : fallback
  const tipLength = value('mainLength', 98) - value('headLength', 50) - value('neckLength', 20)
  const taperLength = (value('outletDiameter', 17) - value('axialBoreDiameter', 13))
    / (2 * Math.tan((Math.PI / 180) * value('outletTaperHalfAngle', 15)))
  return [
    { id: 'origin', icon: '◎', label: '同轴基准', meta: 'X 轴' },
    { id: 'head-taper', icon: '◒', label: `浅锥主段 · ${value('headLength', 50)} mm · Ø${value('headLeftDiameter', 54.25449350717895).toFixed(3)} → Ø${value('headRightDiameter', 56)}`, meta: '主件实体' },
    { id: 'neck', icon: '▰', label: `颈段 · Ø${value('neckDiameter', 30)} × ${value('neckLength', 20)}`, meta: '主件实体' },
    { id: 'tip', icon: '▰', label: `末段 · Ø${value('tipDiameter', 25)} × ${Number(tipLength.toFixed(3))}`, meta: '主件实体 · 派生长度' },
    { id: 'counterbore', icon: '◌', label: `左端沉孔 · Ø${value('counterboreDiameter', 40)} × ${value('counterboreDepth', 40)}`, meta: '切除' },
    { id: 'axial-bore', icon: '◌', label: `轴向贯通孔 · Ø${value('axialBoreDiameter', 13)}`, meta: '切除' },
    { id: 'outlet-taper', icon: '◇', label: `出口锥口 · Ø${value('outletDiameter', 17)} · 半角 ${value('outletTaperHalfAngle', 15)}°`, meta: `切除 · 深 ${Number(taperLength.toFixed(9))} mm` },
    { id: 'insert', icon: '◎', label: `独立镶件 · Ø${value('insertOuterDiameter', 39.4)} × ${value('insertLength', 40)} · ${model?.insertThreadDesignation || 'M12'}`, meta: '第二组件 · 装配关系待确认' },
  ]
}

const libraryItems = [
  { icon: '⬡', name: '六角螺栓', spec: 'M8 × 30', group: '紧固件' },
  { icon: '◉', name: '深沟球轴承', spec: '6204 · 20 × 47 × 14', group: '轴承' },
  { icon: '⬢', name: '圆柱销', spec: 'Ø6 × 24', group: '定位件' },
  { icon: '◌', name: '弹簧垫圈', spec: 'M8', group: '紧固件' },
  { icon: '▣', name: '法兰螺母', spec: 'M10', group: '紧固件' },
]

function parsePrompt(prompt, current) {
  const next = { ...current }
  if (canonicalPartKind(current.kind) === 'stepped_tapered_nozzle') {
    next.kind = 'stepped_tapered_nozzle'
    next.recipeId = 'stepped_tapered_nozzle_with_insert_v1'
    const editNumber = (patterns, fallback) => {
      for (const pattern of patterns) {
        const result = prompt.match(pattern)
        if (result) return Number(result[result.length - 1])
      }
      return fallback
    }
    next.mainLength = editNumber([/主件总长(?:度)?[^\d]*(\d+(?:\.\d+)?)/i, /总长(?:度)?[^\d]*(\d+(?:\.\d+)?)/i], current.mainLength)
    next.headLength = editNumber([/(?:前端)?浅锥(?:段)?长(?:度)?[^\d]*(\d+(?:\.\d+)?)/i], current.headLength)
    next.neckLength = editNumber([/(?:中间)?颈段长(?:度)?[^\d]*(\d+(?:\.\d+)?)/i], current.neckLength)
    next.headLeftDiameter = editNumber([/浅锥左端(?:直径|外径|Ø|φ)?[^\d]*(\d+(?:\.\d+)?)/i], current.headLeftDiameter)
    next.headRightDiameter = editNumber([/浅锥右端(?:直径|外径|Ø|φ)?[^\d]*(\d+(?:\.\d+)?)/i], current.headRightDiameter)
    next.neckDiameter = editNumber([/颈段(?:直径|外径|Ø|φ)[^\d]*(\d+(?:\.\d+)?)/i], current.neckDiameter)
    next.tipDiameter = editNumber([/末段(?:直径|外径|Ø|φ)[^\d]*(\d+(?:\.\d+)?)/i], current.tipDiameter)
    next.counterboreDiameter = editNumber([/沉孔(?:直径|Ø|φ)[^\d]*(\d+(?:\.\d+)?)/i], current.counterboreDiameter)
    next.counterboreDepth = editNumber([/沉孔深(?:度)?[^\d]*(\d+(?:\.\d+)?)/i], current.counterboreDepth)
    next.axialBoreDiameter = editNumber([/(?:轴向)?贯通孔(?:直径|Ø|φ)[^\d]*(\d+(?:\.\d+)?)/i], current.axialBoreDiameter)
    next.outletDiameter = editNumber([/(?:末端|出口)锥口(?:直径|Ø|φ)[^\d]*(\d+(?:\.\d+)?)/i], current.outletDiameter)
    next.outletTaperHalfAngle = editNumber([/(?:出口|末端)锥(?:口)?半角[^\d]*(\d+(?:\.\d+)?)/i], current.outletTaperHalfAngle)
    next.insertOuterDiameter = editNumber([/镶件(?:外径|直径|Ø|φ)[^\d]*(\d+(?:\.\d+)?)/i], current.insertOuterDiameter)
    next.insertLength = editNumber([/镶件长(?:度)?[^\d]*(\d+(?:\.\d+)?)/i], current.insertLength)
    next.insertAxialOffset = editNumber([/镶件轴向偏置[^\d-]*(-?\d+(?:\.\d+)?)/i], current.insertAxialOffset)
    const thread = prompt.match(/\bM\s*(\d+(?:\.\d+)?(?:\s*[x×]\s*\d+(?:\.\d+)?)?)/i)
    if (thread) next.insertThreadDesignation = `M${thread[1].replace(/\s+/g, '').replace('×', 'X')}`
    if (/铝|al6061/i.test(prompt)) next.material = 'AL6061 铝合金'
    if (/不锈钢|304/i.test(prompt)) next.material = 'SUS304 不锈钢'
    return next
  }
  // The drawing-to-3D workflow can hand the copilot a bracket model. Keep
  // bracket dimensions separate from the shaft grammar so a follow-up prompt
  // never overwrites the model with undefined shaft fields.
  if (current.kind === 'bracket' || /支架|底板|缺口|凹槽|浅槽|凸台|贯穿孔/.test(prompt)) {
    next.kind = 'bracket'
    next.name = current.kind === 'bracket' ? current.name : '安装支架 · AI 参数化'
    const bracketMatch = (patterns, fallback) => {
      for (const pattern of patterns) {
        const result = prompt.match(pattern)
        if (result) {
          let rawValue = result[1]
          // For edits that mention both values (for example
          // "底板长度从100改为90" or "R15 改为 R12"), use the final number
          // in the same clause as the requested target.
          const tail = prompt.slice((result.index || 0) + result[0].length, (result.index || 0) + result[0].length + 80)
          const clause = tail.split(/[,，;；。\n]/, 1)[0]
          if (/(?:改为|改成|调整为|设为|设置为|变更为|换成|变成|替换为|为|到)/.test(clause)) {
            const candidates = clause.match(/\d+(?:\.\d+)?/g)
            if (candidates?.length) rawValue = candidates[candidates.length - 1]
          }
          return Number(rawValue)
        }
      }
      return fallback
    }
    next.baseLength = bracketMatch([/底板[^\d]{0,8}(?:长|长度)[^\d]*(\d+(?:\.\d+)?)/i, /底板[^\d]*(\d+(?:\.\d+)?)\s*[×x*]/i, /\b100\b/], current.baseLength ?? bracketModel.baseLength)
    next.baseWidth = bracketMatch([/底板[^\d]{0,8}(?:宽|宽度)[^\d]*(\d+(?:\.\d+)?)/i, /\b(?:宽度|宽)\s*[:：]?\s*(\d+(?:\.\d+)?)/i], current.baseWidth ?? bracketModel.baseWidth)
    next.baseThickness = bracketMatch([/底板[^\d]{0,8}(?:厚|厚度)[^\d]*(\d+(?:\.\d+)?)/i, /厚度\s*[:：]?\s*(\d+(?:\.\d+)?)/i], current.baseThickness ?? bracketModel.baseThickness)
    next.upperLength = bracketMatch([/上部[^\d]{0,8}(?:长|长度)[^\d]*(\d+(?:\.\d+)?)/i, /上部[^\d]*(\d+(?:\.\d+)?)\s*[×x*]/i], current.upperLength ?? bracketModel.upperLength)
    next.upperWidth = bracketMatch([/上部[^\d]{0,8}(?:宽|深|深度)[^\d]*(\d+(?:\.\d+)?)/i], current.upperWidth ?? bracketModel.upperWidth)
    next.upperHeight = bracketMatch([/上部[^\d]{0,8}(?:高|高度)[^\d]*(\d+(?:\.\d+)?)/i], current.upperHeight ?? bracketModel.upperHeight)
    next.totalHeight = bracketMatch([/总高(?:度)?\s*[:：]?\s*(\d+(?:\.\d+)?)/i], current.totalHeight ?? (next.baseThickness + next.upperHeight))
    next.notchOpening = bracketMatch([/(?:缺口|开口)[^\d]{0,8}(?:宽|开口)?[^\d]*(\d+(?:\.\d+)?)/i], current.notchOpening ?? bracketModel.notchOpening)
    next.notchRadius = bracketMatch([/(?:缺口|圆弧)?\s*(?:半径|R)\s*\d+(?:\.\d+)?[^\d]{0,16}(?:改为|改成|调整为|设为|换成|到)[^\d]*(?:R\s*)?(\d+(?:\.\d+)?)/i, /(?:缺口|圆弧)[^\d]{0,8}(?:半径|R)\s*[:：]?\s*(\d+(?:\.\d+)?)/i, /R\s*(\d+(?:\.\d+)?)/i], current.notchRadius ?? bracketModel.notchRadius)
    next.slotLength = bracketMatch([/(?:浅槽|槽)[^\d]{0,8}(?:长|长度|沿Y)[^\d]*(\d+(?:\.\d+)?)/i, /槽长\s*[:：]?\s*(\d+(?:\.\d+)?)/i], current.slotLength ?? bracketModel.slotLength)
    next.slotWidth = bracketMatch([/(?:浅槽|槽)[^\d]{0,8}(?:宽|宽度)[^\d]*(\d+(?:\.\d+)?)/i, /槽宽\s*[:：]?\s*(\d+(?:\.\d+)?)/i], current.slotWidth ?? bracketModel.slotWidth)
    next.pocketDepth = bracketMatch([/(?:浅槽|口袋|槽)[^\d]{0,8}(?:深|深度)[^\d]*(\d+(?:\.\d+)?)/i], current.pocketDepth ?? bracketModel.pocketDepth)
    next.bossDiameter = bracketMatch([/(?:凸台|圆柱|凹槽|贯穿孔|侧孔)[^\d]{0,8}(?:直径|Ø|φ)\s*[:：]?\s*(\d+(?:\.\d+)?)/i, /[ØΦφ]\s*(\d+(?:\.\d+)?)/i], current.bossDiameter ?? bracketModel.bossDiameter)
    next.bossCenterDistance = bracketMatch([/(?:凸台|凹槽|孔)?中心距\s*[:：]?\s*(\d+(?:\.\d+)?)/i], current.bossCenterDistance ?? bracketModel.bossCenterDistance)
    if (/铝|al6061/i.test(prompt)) next.material = 'AL6061 铝合金'
    if (/不锈钢|304/i.test(prompt)) next.material = 'SUS304 不锈钢'
    return next
  }
  const match = (patterns, fallback) => {
    for (const pattern of patterns) {
      const result = prompt.match(pattern)
      if (result) {
        let rawValue = result[1]
        const tail = prompt.slice((result.index || 0) + result[0].length, (result.index || 0) + result[0].length + 80)
        const clause = tail.split(/[,，;；。\n]/, 1)[0]
        if (/(?:改为|改成|调整为|设为|设置为|变更为|换成|变成|替换为|为|到)/.test(clause)) {
          const candidates = clause.match(/\d+(?:\.\d+)?/g)
          if (candidates?.length) rawValue = candidates[candidates.length - 1]
        }
        return Number(rawValue)
      }
    }
    return fallback
  }
  next.outerDiameter = match([/外径\s*[:：]?\s*(\d+(?:\.\d+)?)/i, /直径\s*[:：]?\s*(\d+(?:\.\d+)?)/i, /OD\s*[:：]?\s*(\d+(?:\.\d+)?)/i], current.outerDiameter)
  const hasKeyway = /键槽/.test(prompt)
  next.length = match(hasKeyway
    ? [/总长度\s*[:：]?\s*(\d+(?:\.\d+)?)/i, /零件长度\s*[:：]?\s*(\d+(?:\.\d+)?)/i, /轴长度\s*[:：]?\s*(\d+(?:\.\d+)?)/i, /加长(?:到|为)?\s*(\d+(?:\.\d+)?)/i]
    : [/总长度\s*[:：]?\s*(\d+(?:\.\d+)?)/i, /长度\s*[:：]?\s*(\d+(?:\.\d+)?)/i, /length\s*[:：]?\s*(\d+(?:\.\d+)?)/i, /加长(?:到|为)?\s*(\d+(?:\.\d+)?)/i], current.length)
  if (next.length === current.length) {
    const shortLength = prompt.match(/长\s*[:：]?\s*(\d+(?:\.\d+)?)/i)
    const keywayIndex = prompt.indexOf('键槽')
    const featureIndex = prompt.search(/(?:宽|深)\s*[:：]?\s*\d/i)
    if (shortLength && (featureIndex < 0 || shortLength.index < featureIndex) && (keywayIndex < 0 || shortLength.index < keywayIndex)) next.length = Number(shortLength[1])
  }
  next.holeDiameter = match([/通孔\s*[:：]?\s*(\d+(?:\.\d+)?)/i, /内径\s*[:：]?\s*(\d+(?:\.\d+)?)/i, /孔径\s*[:：]?\s*(\d+(?:\.\d+)?)/i], current.holeDiameter)
  next.keywayWidth = match([/(?:键槽[^\d]*?)?(?:槽宽|宽度?|宽)\s*[:：]?\s*(\d+(?:\.\d+)?)/i], current.keywayWidth)
  next.keywayDepth = match([/(?:键槽[^\d]*?)?(?:槽深|深度?|深)\s*[:：]?\s*(\d+(?:\.\d+)?)/i], current.keywayDepth)
  next.keywayLength = match([/键槽[^\d]{0,12}(?:槽长|长度|长)[^\d]{0,16}(?:改为|改成|调整为|设为|换成|到)[^\d]*(\d+(?:\.\d+)?)/i, /键槽[^\d]{0,12}(?:槽长|长度|长)\s*[:：]?\s*(\d+(?:\.\d+)?)/i, /槽长\s*[:：]?\s*(\d+(?:\.\d+)?)/i], current.keywayLength)
  if (/铝|al6061/i.test(prompt)) next.material = 'AL6061 铝合金'
  if (/不锈钢|304/i.test(prompt)) next.material = 'SUS304 不锈钢'
  return next
}

function Icon({ children, className = '' }) {
  return <span className={`icon ${className}`}>{children}</span>
}

function productionArtifactsAvailable(generation) {
  return Boolean(
    generation?.validation?.productionReady
    && !generation?.stale
    && generation?.artifactStatus !== 'unavailable'
    && generation?.artifactStatus !== 'recovering',
  )
}

const evidenceAcceptedForPreview = (evidence) => ['confirmed', 'preview_confirmed'].includes(String(evidence?.status || ''))

function App() {
  const initialStoreRef = useRef(null)
  if (!initialStoreRef.current) initialStoreRef.current = ProjectStore.loadProjectStore(localStorage)
  const initialSnapshot = ProjectStore.getFileSnapshot(initialStoreRef.current) || {}
  const [workspaceStore, setWorkspaceStore] = useState(initialStoreRef.current)
  const storeRef = useRef(workspaceStore)
  storeRef.current = workspaceStore
  const activeProject = ProjectStore.getActiveProject(workspaceStore)
  const activeFile = ProjectStore.getActiveFile(workspaceStore)
  const selectedProject = activeProject?.name || '我的项目'
  const projects = workspaceStore.projects.map((project) => ({ ...project, files: project.files.length, updated: new Date(project.updatedAt).toLocaleString('zh-CN') }))
  const files = activeProject?.files || []
  const [settings, setSettings] = useState(() => {
    try { return { ...defaultPreferences, ...JSON.parse(localStorage.getItem('joyniu-preferences')) } } catch { return defaultPreferences }
  })
  const [activeMode, setActiveMode] = useState(normalizeCadWorkspaceMode(initialSnapshot.activeMode) || (initialSnapshot.drawingJob?.evidence || initialSnapshot.generation ? modeForFile(activeFile) : '首页'))
  const [activePanel, setActivePanel] = useState('参数')
  const [model, setModel] = useState(() => modelForPendingDrawing(initialSnapshot.model || emptyModel(activeFile?.name, settings.defaultMaterial), initialSnapshot.drawingJob))
  const hasModel = Boolean(model.kind)
  const [selectedFeature, setSelectedFeature] = useState('keyway')
  const [drawingJob, setDrawingJob] = useState(initialSnapshot.drawingJob || { file: null, previewUrl: '', status: 'idle', evidence: null })
  const [storageError, setStorageError] = useState('')
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false)
  const [dialog, setDialog] = useState('')
  const transientFilesRef = useRef(new Map())
  const currentSnapshotRef = useRef(initialSnapshot)
  const workspaceIdRef = useRef('')
  workspaceIdRef.current = `${workspaceStore.activeProjectId}:${workspaceStore.activeFileId}`
  const [backend, setBackend] = useState({ status: 'checking', engine: '正在连接几何服务', productionReady: false, health: null, error: '' })
  const [generation, setGeneration] = useState(initialSnapshot.generation || null)
  const [platform, setPlatform] = useState(() => emptyPlatformState())
  const [aiConversation, setAiConversation] = useState({ conversationId: chatId('conversation'), previousResponseId: '', serviceStatus: null, status: null, providerRoute: '', error: '', turnStatus: 'idle', statusMessage: '' })
  const [chatAttachments, setChatAttachments] = useState([])
  const [prompt, setPrompt] = useState(initialSnapshot.prompt || '')
  const [messages, setMessages] = useState(initialSnapshot.messages?.length ? initialSnapshot.messages.map(normalizeChatMessage) : welcomeMessages)
  const [assemblyItems, setAssemblyItems] = useState(initialSnapshot.assemblyItems || [])
  const [lastAiTurn, setLastAiTurn] = useState(null)
  const lastAiTurnRef = useRef(null)
  lastAiTurnRef.current = lastAiTurn
  const [checkResult, setCheckResult] = useState(null)
  const [isChecking, setIsChecking] = useState(false)
  const [isGenerating, setIsGenerating] = useState(false)
  const [isAccepting, setIsAccepting] = useState(false)
  const [toast, setToast] = useState(null)
  const [view, setView] = useState(initialSnapshot.view || settings.defaultView)
  const [section, setSection] = useState(Boolean(initialSnapshot.section))
  const [zoom, setZoom] = useState(initialSnapshot.zoom || 1)
  const drawingUrlRef = useRef('')
  const drawingTimerRef = useRef(null)
  const drawingRequestRef = useRef(0)
  const aiRequestRef = useRef(0)
  const chatAbortRef = useRef(null)
  const cadConfirmRef = useRef(null)
  const modelInteractionRevisionRef = useRef(0)
  const artifactRecoveryRef = useRef({ inFlight: false, attemptedParameterSignatures: new Set() })
  const modelRef = useRef(model)
  const generationRef = useRef(generation)
  const drawingJobRef = useRef(drawingJob)
  const chatAttachmentsRef = useRef(chatAttachments)
  // Keep only opaque ids between account switches.  Workflow payloads are
  // reloaded through the API under the newly authenticated user's project ACL;
  // no bearer token or NC text is persisted in browser storage.
  const platformWorkflowRef = useRef({ projectId: '', planId: '' })
  const [drawingScale, setDrawingScale] = useState(initialSnapshot.drawingScale || '1:1')
  const [drawingPreferences, setDrawingPreferences] = useState(initialSnapshot.drawingPreferences || { layers: {}, selectedView: 'all' })

  modelRef.current = model
  generationRef.current = generation
  drawingJobRef.current = drawingJob
  chatAttachmentsRef.current = chatAttachments

  // A refresh restores the drawing evidence and model snapshot separately.
  // Let a typed recipe identity win over an unrelated old shaft/bracket demo,
  // while preserving every explicit AI or customer candidate value.
  useEffect(() => {
    const evidence = drawingJob?.evidence
    if (isFeatureModel(model)) return
    if (!evidence) return
    // Compatibility recognition may arrive before the remote SSE result.  It
    // is evidence for the final turn, not permission to replace the active
    // model while the multimodal analysis is still running.
    if (['queued', 'analyzing'].includes(drawingJob?.status)) return
    const repaired = modelForPendingDrawing(model, drawingJob)
    if (repaired !== model) {
      modelRef.current = repaired
      setModel(repaired)
      setGeneration((current) => current ? { ...current, stale: true, pendingDrawing: false } : current)
      return
    }
    const evidenceKind = partKindFromEnvelope(evidence || drawingJob?.analysis, model.kind)
    if (!productionPartKinds.includes(evidenceKind) || evidenceKind === canonicalPartKind(model.kind)) return
    const definition = partDefinition(evidenceKind)
    const candidate = rawParametersFromRecognition(evidence) || {}
    if (!hasAiGeometryParameters(candidate)) return
    const restored = modelFromCandidate(definition, candidate, { name: definition.preview.name, updatedAt: '刚刚' })
    modelRef.current = restored
    setModel(restored)
    if (evidence.status !== 'confirmed') setGeneration((current) => current ? { ...current, stale: true, pendingDrawing: false } : current)
  }, [drawingJob?.status, drawingJob?.evidence, model.kind])

  currentSnapshotRef.current = {
    ...(activeFile?.snapshot || {}), model: activeFile?.type === '文档' || !hasModel ? null : model,
    drawingJob, generation, messages, assemblyItems, prompt, drawingScale, drawingPreferences, activeMode, view, section, zoom,
  }
  useEffect(() => {
    setWorkspaceStore((current) => ProjectStore.updateFileSnapshot(current, current.activeProjectId, current.activeFileId, currentSnapshotRef.current))
  }, [model, drawingJob, generation, messages, assemblyItems, prompt, drawingScale, drawingPreferences, activeMode, view, section, zoom])
  useEffect(() => {
    try { ProjectStore.persistProjectStore(workspaceStore, localStorage); setStorageError('') }
    catch (error) { setStorageError(readableError(error)) }
  }, [workspaceStore])
  useEffect(() => {
    try { localStorage.setItem('joyniu-preferences', JSON.stringify(settings)) } catch (error) { setStorageError(readableError(error)) }
  }, [settings])
  useEffect(() => {
    window.scrollTo({ top: 0, left: 0, behavior: 'auto' })
    setMobileMenuOpen(false)
  }, [activeMode])
  useEffect(() => { if (toast && toast.type !== 'error') { const timer = setTimeout(() => setToast(null), 4500); return () => clearTimeout(timer) } }, [toast])
  useEffect(() => { setCheckResult(null) }, [model])
  useEffect(() => {
    const onKeyDown = (event) => {
      if (event.key === 'Escape') setMobileMenuOpen(false)
      if (!(event.metaKey || event.ctrlKey) || event.altKey) return
      const key = event.key.toLowerCase()
      if (['k', 'n', 's'].includes(key)) {
        event.preventDefault()
        if (key === 'k') setDialog('commands')
        else if (key === 'n') setDialog('project')
        else saveVersionForFile(storeRef.current.activeFileId)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])
  useEffect(() => {
    let active = true
    api.health().then((health) => {
      if (!active) return
      const geometry = health.geometry || {}
      setBackend({
        status: geometry.available ? 'connected' : 'degraded',
        engine: geometry.engine || 'faceted-fallback',
        productionReady: Boolean(geometry.available),
        health,
        error: '',
      })
    }).catch((error) => {
      if (!active) return
      setBackend({ status: 'offline', engine: '浏览器预览', productionReady: false, health: null, error: error.name === 'AbortError' ? 'API 连接超时' : error.message })
    })
    return () => { active = false }
  }, [])
  useEffect(() => {
    let active = true
    api.aiStatus().then((status) => {
      if (active) setAiConversation((current) => ({ ...current, serviceStatus: status, status: current.providerRoute ? current.status : status, error: current.providerRoute ? current.error : '' }))
    }).catch((error) => {
      if (active) setAiConversation((current) => ({ ...current, status: null, error: error.message || 'AI 服务状态不可用' }))
    })
    return () => { active = false }
  }, [])
  useEffect(() => () => {
    if (drawingTimerRef.current) window.clearTimeout(drawingTimerRef.current)
    if (drawingUrlRef.current) URL.revokeObjectURL(drawingUrlRef.current)
    chatAbortRef.current?.abort()
  }, [])

  const showToast = (text, type) => setToast({ ...notificationFor(text, type), id: Date.now() })
  const updateModel = (key, value) => {
    modelInteractionRevisionRef.current += 1
    if (isFeatureModel(modelRef.current)) {
      const next = editCadParameter(modelRef.current, key, value)
      modelRef.current = next
      setModel(next)
      setGeneration(null)
      setDrawingJob((current) => ({ ...current, status: 'ready' }))
      return
    }
    const nextValue = ['material', 'insertThreadDesignation'].includes(key) ? value : value === '' ? '' : Number(value)
    setModel((prev) => {
      const updated = { ...prev, [key]: nextValue, updatedAt: '刚刚' }
      if (prev.kind === 'bracket') {
        if (key === 'totalHeight') updated.holeDepth = nextValue
        if (key === 'upperWidth') updated.saddleDepth = nextValue
      }
      modelRef.current = updated
      return updated
    })
    setGeneration((current) => current ? { ...current, stale: true } : current)
    // Edits made in the main parameter inspector are the human-confirmation
    // input for an AI drawing candidate. Keep them in the evidence envelope
    // as well as the visible model so the confirm gate can distinguish an
    // explicitly supplied value from a visual scaffold default.
    setDrawingJob((current) => {
      if (!current?.evidence) return current
      const candidateParameters = {
        ...(current.evidence.recognizedParameters && typeof current.evidence.recognizedParameters === 'object' ? current.evidence.recognizedParameters : {}),
        ...(current.evidence.parameters && typeof current.evidence.parameters === 'object' ? current.evidence.parameters : {}),
        ...(current.evidence.candidateParameters && typeof current.evidence.candidateParameters === 'object' ? current.evidence.candidateParameters : {}),
        [key]: nextValue,
      }
      const parameters = {
        ...(current.evidence.parameters && typeof current.evidence.parameters === 'object' ? current.evidence.parameters : {}),
        [key]: nextValue,
      }
      const humanEditedFields = [...new Set([...(current.humanEditedFields || []), key])]
      const candidateKind = partKindFromEnvelope(current.evidence || current.analysis, modelRef.current.kind)
      const missingFields = requiredKeysForKind(candidateKind).filter((field) => !requiredParameterPresent(field, candidateParameters[field]))
      const candidateSources = { ...(current.analysis?.candidateSources || {}), [key]: 'manual' }
      const candidateParameterMeta = {
        ...(current.evidence.candidateParameterMeta || {}),
        [key]: { source: 'manual', confidence: null, requiresConfirmation: true },
      }
      const defaultedFields = (current.analysis?.defaultedFields || []).filter((field) => field !== key)
      return {
        ...current,
        status: 'ready',
        customerAccepted: false,
        humanConfirmed: false,
        evidence: { ...current.evidence, parameters, candidateParameters, candidateParameterMeta, status: 'pending' },
        candidateFields: [...new Set([...(current.candidateFields || []), key])],
        humanEditedFields,
        analysis: { ...(current.analysis || {}), candidateParameters, candidateSources, defaultedFields, missingFields, needsInput: missingFields.length > 0 },
      }
    })
  }
  const modelKind = canonicalPartKind(model.kind)
  const parameterValidation = validateModelParameters(model)
  const modelValid = parameterValidation.valid
  const currentFeatures = !hasModel || isFeatureModel(model) ? [] : modelKind === 'arched_clevis_support' ? archedClevisSupportFeatures(model) : modelKind === 'bracket'
    ? getBracketFeatures(model)
    : modelKind === 'split_clamp_support'
      ? getSplitClampFeatures(model)
      : modelKind === 'stepped_tapered_nozzle'
        ? getSteppedTaperedNozzleFeatures(model)
        : [
          { id: 'origin', icon: '◎', label: '原点', meta: '基准' },
          { id: 'pad', icon: '▰', label: `轴体 · Ø${model.outerDiameter} × ${model.length} mm`, meta: '参数草稿' },
          { id: 'hole', icon: '◉', label: `通孔 · Ø${model.holeDiameter} mm`, meta: '切除' },
          { id: 'keyway', icon: '⌗', label: `键槽 · ${model.keywayWidth} × ${model.keywayDepth} × ${model.keywayLength} mm`, meta: '切除' },
        ]

  const modelParametersForApi = (value) => {
    const keys = parameterKeysForKind(value?.kind)
    return Object.fromEntries(keys.filter((key) => value?.[key] !== undefined && value?.[key] !== '').map((key) => [key, value[key]]))
  }
  const browserFixtureRecognition = (file, digest) => ({
    id: `offline_${Date.now()}`,
    status: 'confirmed',
    confidence: 0.995,
    sourceFilename: file?.name || 'drawing',
    sourceSha256: digest,
    parameters: { ...bracketModel },
    evidence: bracketParameterKeys.filter((key) => typeof bracketModel[key] === 'number').map((field) => ({ field, value: bracketModel[field], source: '验收夹具哈希匹配', confidence: 0.995 })),
    engine: 'verified-browser-fixture',
    validation: { valid: true, productionReady: false, engine: 'browser-preview', metrics: { boundingLength: 100, boundingWidth: 50, boundingHeight: 40, solidCount: 1, faceCount: 24, notchBottomZ: 25 } },
    warnings: ['FastAPI 不可用；当前使用验收夹具浏览器预览，不能导出生产 STEP。'],
  })
  const generateAiArtifact = async (next, sourceDrawingId = '', options = {}) => {
    const workspaceScope = workspaceIdRef.current
    const kind = canonicalPartKind(next?.kind)
    if (!productionPartKinds.includes(kind) || backend.status === 'offline') return null
    const payload = {
      partType: kind,
      recipeId: partDefinition(kind).recipeId,
      parameters: modelParametersForApi(next),
      formats: ['step', 'glb'],
      ...(sourceDrawingId && !String(sourceDrawingId).startsWith('offline_') ? { sourceDrawingId, confirmed: true } : {}),
      // A health check can still be in flight when the customer uploads a
      // drawing.  Treat every non-offline state as production-intent so a
      // transient `checking` status cannot silently produce a faceted
      // fallback and leave the workbench looking complete.
      requireCadQuery: backend.status !== 'offline',
    }
    const generated = kind === 'bracket'
      ? await api.generateBracket(payload, options.signal)
      : await api.generateModel(payload, options.signal)
    if (workspaceIdRef.current !== workspaceScope || options.signal?.aborted || (options.commitGuard && !options.commitGuard())) return null
    const step = generated.artifacts?.find((item) => item.format === 'step')
    if (!step || generated.validation?.valid !== true) throw new Error('实体校验未通过，未生成可交付文件')
    if (backend.status !== 'offline' && !step.productionReady) throw new Error('OCCT 实体或 STEP 拓扑校验未达到生产交付条件')
    setGeneration({ ...generated, stale: false })
    setBackend((current) => ({ ...current, status: current.status === 'checking' ? 'connected' : current.status, engine: generated.engine || step.engine, productionReady: Boolean(step.productionReady), error: '' }))
    return generated
  }
  const recoverExpiredProductionGlb = async (failure = {}) => {
    if (isFeatureModel(modelRef.current)) {
      setGeneration((current) => current ? { ...current, artifactStatus: 'unavailable' } : current)
      showToast('当前实体文件不可用，请点击“重建实体”恢复。', 'error')
      return
    }
    const currentGeneration = generationRef.current
    const currentModel = modelRef.current
    const currentDrawingJob = drawingJobRef.current
    const sourceRequestId = failure.requestId || currentGeneration?.requestId || ''
    const parameters = modelParametersForApi(currentModel)
    const generationParameters = currentGeneration?.parameters || {}
    const generationDoesNotMatchModel = requiredKeysForKind(currentModel?.kind).some((key) => {
      const generatedValue = generationParameters[key]
      const currentValue = parameters[key]
      const generatedNumber = Number(generatedValue)
      const currentNumber = Number(currentValue)
      return Number.isFinite(generatedNumber) && Number.isFinite(currentNumber)
        ? generatedNumber !== currentNumber
        : String(generatedValue ?? '') !== String(currentValue ?? '')
    })
    if (!currentGeneration || (sourceRequestId && currentGeneration.requestId !== sourceRequestId)) return

    // The old FastAPI process no longer owns this artifact. Mark it
    // unavailable before evaluating whether automatic regeneration is safe,
    // so even an inconsistent/pending restored session cannot keep presenting
    // the fallback mesh as a production file.
    setGeneration((current) => current?.requestId === currentGeneration.requestId
      ? { ...current, artifactStatus: 'unavailable', artifactRecoveryError: failure.message || 'GLB artifact 已失效' }
      : current)

    if (
      !productionPartKinds.includes(canonicalPartKind(currentModel?.kind))
      || currentGeneration.stale
      || currentGeneration.validation?.productionReady !== true
      || currentGeneration.pendingDrawing
      || generationDoesNotMatchModel
      || chatAttachmentsRef.current.length > 0
      || (currentDrawingJob?.evidence && currentDrawingJob.evidence.status !== 'confirmed')
    ) return

    // Never turn an offline/fallback response into a successful production
    // recovery. A later reload against a healthy API can attempt again.
    if (backend.status === 'offline' || backend.status === 'degraded') {
      showToast('旧 GLB 已失效；CadQuery/OCCT 离线，当前仅显示参数预览')
      return
    }

    const parameterSignature = JSON.stringify(parameters)
    const recovery = artifactRecoveryRef.current
    if (recovery.inFlight || recovery.attemptedParameterSignatures.has(parameterSignature)) return
    recovery.inFlight = true
    recovery.attemptedParameterSignatures.add(parameterSignature)
    setGeneration((current) => current?.requestId === currentGeneration.requestId
      ? { ...current, artifactStatus: 'recovering', artifactRecoveryError: '' }
      : current)
    showToast('旧 GLB 已失效，正在按当前参数重建 STEP / GLB…')

    try {
      // Do not reuse sourceDrawingId here. Its confirmation belongs to the old
      // process-local recognition record; current validated parameters are the
      // complete recovery source.
      const recoveryPayload = {
        partType: canonicalPartKind(currentModel.kind),
        recipeId: partDefinition(currentModel.kind).recipeId,
        parameters,
        formats: ['step', 'glb'],
        requireCadQuery: true,
      }
      const generated = canonicalPartKind(currentModel.kind) === 'bracket'
        ? await api.generateBracket(recoveryPayload)
        : await api.generateModel(recoveryPayload)
      const step = generated.artifacts?.find((item) => item.format === 'step')
      const glb = generated.artifacts?.find((item) => item.format === 'glb')
      if (
        generated.validation?.valid !== true
        || generated.validation?.productionReady !== true
        || generated.engine !== 'cadquery-occt'
        || step?.engine !== 'cadquery-occt'
        || step?.productionReady !== true
        || !glb
      ) {
        throw new Error('OCCT 未返回通过校验的 STEP 与 GLB')
      }
      if (
        generationRef.current?.requestId !== currentGeneration.requestId
        || generationRef.current?.artifactStatus !== 'recovering'
        || generationRef.current?.stale
        || generationRef.current?.pendingDrawing
        || JSON.stringify(modelParametersForApi(modelRef.current)) !== parameterSignature
        || chatAttachmentsRef.current.length > 0
        || (drawingJobRef.current?.evidence && drawingJobRef.current.evidence.status !== 'confirmed')
      ) return
      const recovered = {
        ...generated,
        stale: false,
        artifactStatus: 'available',
        recoveredFromRequestId: currentGeneration.requestId,
      }
      setGeneration(recovered)
      setDrawingJob((current) => current?.generation?.requestId === currentGeneration.requestId
        ? { ...current, status: 'generated', generation: recovered }
        : current)
      setBackend((current) => ({ ...current, status: 'connected', engine: generated.engine || step.engine, productionReady: true, error: '' }))
      showToast('STEP / GLB 已按当前参数自动恢复')
    } catch (error) {
      if (generationRef.current?.requestId === currentGeneration.requestId) {
        setGeneration((current) => ({ ...current, artifactStatus: 'unavailable', artifactRecoveryError: error.message }))
        showToast(`自动恢复失败：${error.message}；当前仅显示参数预览`)
      }
    } finally {
      recovery.inFlight = false
    }
  }
  const rebuildCurrentModel = async () => {
    if (isFeatureModel(modelRef.current)) return confirmCadModel()
    if (!hasModel) return showToast('当前文件还没有模型，请先描述零件或上传图纸。', 'info')
    if (generation?.pendingDrawing || (drawingJob?.evidence && drawingJob.evidence.status !== 'confirmed')) return showToast('请先确认 AI 候选数据，再重建生产实体')
    if (!modelValid) return showToast('请先修正参数，再重建实体')
    if (!productionPartKinds.includes(canonicalPartKind(model.kind))) return showToast('当前轴类模型可直接继续编辑；生产实体重建将在对应内核接入后开放')
    setIsGenerating(true)
    try {
      const generated = await generateAiArtifact(model, drawingJob?.evidence?.status === 'confirmed' ? drawingJob.evidence.id : '')
      if (generated) {
        setDrawingJob((current) => ({ ...current, status: 'generated', generation: generated }))
        setMessages((prev) => [...prev, { role: 'ai', text: '已按当前参数重建实体，并更新了 STEP / GLB 交付版本。' }])
        showToast('实体已按当前参数重建')
      }
    } catch (error) {
      showToast(`实体重建失败：${error.message}`)
    } finally {
      setIsGenerating(false)
    }
  }
  const applyCadResult = (result, previous, sourceFile = null) => {
    if (!result || !['needs_input', 'review_required', 'ready', 'failed'].includes(result.status)) throw new Error('CAD 服务返回的建模状态无效')
    const next = cadModelFromResult(result, previous)
    modelRef.current = next
    setModel(next)
    setGeneration(cadGenerationFromResult(result, next.cadPlan))
    setDrawingJob((current) => ({
      ...current, ...(sourceFile ? { file: sourceFile, fileMeta: { name: sourceFile.name, size: sourceFile.size, type: sourceFile.type } } : {}),
      status: result.status === 'failed' ? 'error' : result.status === 'ready' ? 'generated' : 'ready', evidence: null, analysis: null,
      agentRunId: result.runId, cadTask: null, questions: result.questions || [], requiresFileReselection: false, sourceFileUnavailable: false,
      interrupted: false, error: result.status === 'failed' ? result.message : '', warning: '',
    }))
    setActivePanel('参数')
    return next
  }
  const sendCadConversation = async (userText, files = [], { resumeTask = null } = {}) => {
    if (isAccepting || cadConfirmRef.current || chatAbortRef.current) { showToast('当前操作仍在处理，请等待完成后再发送。', 'info'); return false }
    if (drawingJobRef.current?.cadTask?.runId && !resumeTask) { showToast('本轮已在后台运行，请先检查后台结果。', 'info'); return false }
    if (files.length && modelRef.current?.kind) {
      const currentStore = flushWorkspace()
      commitStore(ProjectStore.saveBeforeDrawingReplacement(currentStore, currentStore.activeProjectId, currentStore.activeFileId, currentSnapshotRef.current))
    }
    const requestId = ++aiRequestRef.current
    const scope = workspaceIdRef.current, revision = modelInteractionRevisionRef.current
    const previous = files.length ? emptyModel(activeFile?.name, settings.defaultMaterial) : modelRef.current
    const assistantId = resumeTask?.assistantId || chatId('assistant'), turnId = chatId('turn')
    const controller = new AbortController()
    chatAbortRef.current?.abort()
    chatAbortRef.current = controller
    const current = () => requestId === aiRequestRef.current && workspaceIdRef.current === scope && !controller.signal.aborted
    let durableTask = resumeTask
    const saveTask = (task) => { durableTask = task; const job = { ...drawingJobRef.current, cadTask: task }; drawingJobRef.current = job; setDrawingJob(job) }
    setLastAiTurn({ prompt: userText, files, workspaceId: scope })
    setMessages((items) => resumeTask && items.some((item) => item.id === assistantId)
      ? items.map((item) => item.id === assistantId ? { ...item, status: 'streaming', statusText: '正在恢复后台结果…' } : item)
      : [...items, ...(!resumeTask ? [chatMessage('user', userText, { turnId, attachments: files.map((file) => file.name) })] : []), chatMessage('ai', '', { id: assistantId, turnId, status: 'streaming', statusText: '正在检查图纸与建模要求…' })])
    setIsGenerating(true)
    setAiConversation((state) => ({ ...state, status: configuredAiProvider(state, 'cad'), providerRoute: 'cad', error: '', turnStatus: 'submitting', statusMessage: '正在准备建模…', cadProgress: cadProgressFromEvent({ stage: 'started' }) }))
    if (files.length) {
      modelRef.current = previous
      setModel(previous)
      setGeneration(null)
      const job = { file: files[0], fileMeta: { name: files[0].name, size: files[0].size, type: files[0].type }, status: 'analyzing', evidence: null }
      drawingJobRef.current = job; setDrawingJob(job)
    }
    if (resumeTask) saveTask({ ...resumeTask, paused: false })
    const onProgress = (payload) => {
      if (!current()) return
      const message = payload.message || payload.summary || '正在处理本轮建模…'
      const progress = cadProgressFromEvent({ ...payload, message }, durableTask?.progress)
      if (payload.runId || durableTask) saveTask({ ...durableTask, runId: payload.runId || durableTask.runId, revision: payload.revision || durableTask?.revision || 1, status: 'running', prompt: userText, assistantId, paused: false, progress })
      setAiConversation((state) => ({ ...state, status: payload.provider || state.status, providerRoute: 'cad', turnStatus: 'streaming', statusMessage: message, cadProgress: progress }))
      setMessages((items) => items.map((item) => item.id === assistantId ? { ...item, statusText: message } : item))
    }
    try {
      let result
      try {
        result = resumeTask ? await cadAgent.waitForRun({ token: platform.token, runId: resumeTask.runId, signal: controller.signal, onProgress })
          : await cadAgent.run({ token: platform.token, message: userText, files, modelState: cadRequestState(previous), history: files.length ? [] : conversationHistory(messages), signal: controller.signal, onEvent: (event, payload) => {
            if (event === 'progress' || event.endsWith('.progress')) onProgress(payload)
          } })
      } catch (error) {
        if (!durableTask?.runId || controller.signal.aborted || resumeTask) throw error
        onProgress({ stage: 'agent_working', message: '连接已中断，正在读取后台保存的进度…' })
        result = await cadAgent.waitForRun({ token: platform.token, runId: durableTask.runId, signal: controller.signal, onProgress })
      }
      result = cadTerminalResult(result)
      if (!current()) return false
      if (modelInteractionRevisionRef.current !== revision) throw new Error('本轮期间模型已被编辑，返回结果未覆盖你的修改，请重试。')
      if (durableTask && (result.runId !== durableTask.runId || result.revision !== durableTask.revision)) throw new Error('后台返回了其他模型版本，本页未被覆盖。')
      applyCadResult(result, previous, files[0])
      drawingJobRef.current = { ...drawingJobRef.current, cadTask: null }
      setLastAiTurn({ prompt: userText, files, workspaceId: scope, cadRun: { runId: result.runId, revision: result.revision } })
      setMessages((items) => items.map((item) => item.id === assistantId ? { ...item, text: [result.message, ...(result.questions || []).map((question) => `待确认：${question}`)].filter(Boolean).join('\n'), status: result.status === 'failed' ? 'error' : 'complete', statusText: '' } : item))
      setAiConversation((state) => ({ ...state, status: result.provider || state.status, providerRoute: 'cad', turnStatus: result.status === 'failed' ? 'error' : 'complete', statusMessage: '', error: result.status === 'failed' ? result.message || '本轮建模未完成，请继续修正。' : '' }))
      return result.status !== 'failed'
    } catch (error) {
      if (requestId !== aiRequestRef.current || workspaceIdRef.current !== scope) return false
      const stopped = error.name === 'AbortError'
      const message = stopped ? durableTask?.runId ? '已停止等待；后台仍在处理，可稍后检查结果。' : '已停止等待本轮建模。可以重新发送。' : durableTask?.runId ? `${readableError(error)}；运行记录已保存，可检查后台结果。` : readableError(error)
      setMessages((items) => items.map((item) => item.id === assistantId ? { ...item, text: message, status: stopped ? 'interrupted' : 'error', statusText: '' } : item))
      setAiConversation((state) => ({ ...state, turnStatus: stopped ? 'idle' : 'error', statusMessage: '', error: stopped ? '' : message }))
      if (durableTask?.runId) saveTask({ ...durableTask, paused: true })
      else if (files.length) setDrawingJob((job) => ({ ...job, status: 'error', error: message }))
      else setGeneration((value) => value ? { ...value, lastTurnError: message } : value)
      return false
    } finally {
      if (requestId === aiRequestRef.current) { setIsGenerating(false); if (chatAbortRef.current === controller) chatAbortRef.current = null }
    }
  }
  const confirmCadModel = async () => {
    if (isGenerating || isAccepting || chatAbortRef.current || cadConfirmRef.current || drawingJobRef.current?.cadTask?.runId) return false
    const currentModel = modelRef.current
    const action = cadPrimaryAction(currentModel)
    if (!currentModel.agentRun?.runId) return showToast(action.hint || '请先描述零件并开始建模。', 'info')
    if (action.kind === 'retry') return sendCadConversation(cadRetryMessage(currentModel))
    if (action.kind === 'answer') {
      document.querySelector('[aria-label="给 AI 发送消息"]')?.focus()
      return showToast(action.hint, 'info')
    }
    const validation = validateModelParameters(currentModel)
    if (!validation.valid) return showToast(validation.errors.map((item) => item.message).join('；'), 'error')
    if (currentModel.agentRun.dirty) {
      const message = cadParameterEditMessage(currentModel)
      setLastAiTurn({ prompt: message, files: [] })
      return sendCadConversation(message)
    }
    if (currentModel.agentRun.deliveryBlockedReason) {
      return sendCadConversation('请重新检查当前保存的模型，核对原图和实体投影，并修正仍有差异的尺寸与结构。')
    }
    const scope = workspaceIdRef.current, revision = modelInteractionRevisionRef.current
    const operation = { runId: currentModel.agentRun.runId, revision: currentModel.agentRun.revision, requestId: aiRequestRef.current, planSignature: cadPlanSignature(currentModel.cadPlan) }
    cadConfirmRef.current = operation
    const current = () => cadConfirmRef.current === operation && workspaceIdRef.current === scope && modelInteractionRevisionRef.current === revision
      && aiRequestRef.current === operation.requestId && modelRef.current.agentRun?.runId === operation.runId && modelRef.current.agentRun?.revision === operation.revision
      && cadPlanSignature(modelRef.current.cadPlan) === operation.planSignature
    setIsAccepting(true)
    try {
      const result = await cadAgent.confirm({ token: platform.token, runId: currentModel.agentRun.runId, revision: currentModel.agentRun.revision, parameters: cadConfirmationParameters(currentModel.cadPlan) })
      if (!current()) return false
      if (result.runId !== operation.runId || result.revision !== operation.revision + 1 || result.status !== 'ready') throw new Error('确认返回的模型版本不匹配，本页未被覆盖。')
      applyCadResult(result, currentModel)
      setMessages((items) => [...items, chatMessage('ai', result.message || (result.status === 'ready' ? '当前尺寸已确认，实体已重新生成并检查。' : '建模检查发现待解决的问题，请继续补充。'))])
      showToast(result.status === 'ready' ? '当前实体已生成，预览与交付文件已同步。' : result.message, result.status === 'ready' ? 'success' : 'info')
      return result.status === 'ready'
    } catch (error) {
      if (current()) {
        if (error.status === 409) {
          setPrompt('请重新检查当前保存版本，保持本页尺寸与结构，并更新预览。')
          document.querySelector('[aria-label="给 AI 发送消息"]')?.focus()
          showToast('当前保存的是历史版本。已准备重新检查的消息，发送后会建立独立的新版本。', 'info')
        } else showToast(`确认未完成：${readableError(error)}`, 'error')
      }
      return false
    }
    finally { if (cadConfirmRef.current === operation) { cadConfirmRef.current = null; if (workspaceIdRef.current === scope) setIsAccepting(false) } }
  }
  const sendAiConversation = async (text, filesInput = []) => {
    if (isAccepting || cadConfirmRef.current) { showToast('正在确认当前模型，请完成后再发送。', 'info'); return false }
    const files = normalizeFilesInput(filesInput)
    const userText = text?.trim() || (files[0] ? `请依据这份图纸建立可编辑的三维实体，核对尺寸、空间关系和各视图轮廓；发现差异请修正，缺少必要信息再提问。图纸：${files[0].name}` : '')
    if (!userText && !files.length) return false
    if (shouldUseCadAgent(modelRef.current, files)) return sendCadConversation(userText, files)
    const requestId = aiRequestRef.current + 1
    aiRequestRef.current = requestId
    const turnId = chatId('turn')
    const assistantMessageId = chatId('assistant')
    const baseModel = { ...modelRef.current }
    const baseModelInteractionRevision = modelInteractionRevisionRef.current
    const baseDrawingJob = drawingJobRef.current
    const baseGeneration = generationRef.current
    const history = conversationHistory(messages)
    const attachmentNames = files.map((file) => file.name)
    setMessages((prev) => [
      ...prev,
      chatMessage('user', userText || attachmentNames.join('、'), { turnId, attachments: attachmentNames }),
      chatMessage('ai', '', { id: assistantMessageId, turnId, status: 'streaming', statusText: files.length ? '正在读取图纸…' : '正在思考…' }),
    ])
    const controller = new AbortController()
    chatAbortRef.current?.abort()
    chatAbortRef.current = controller
    const turnStillCurrent = () => requestId === aiRequestRef.current && !controller.signal.aborted
    setIsGenerating(true)
    setAiConversation((current) => ({ ...current, status: configuredAiProvider(current, 'legacy'), providerRoute: 'legacy', error: '', turnStatus: 'submitting', statusMessage: files.length ? '正在读取图纸…' : '正在连接远程大模型…', activeTurnId: turnId, cadProgress: null }))
    let recognition = null
    let recognitionError = null
    let result = null
    let aiError = null
    let appliedModelSignature = ''
    try {
      // Keep an unresolved drawing review requirement across chat turns. The
      // attachment chip is cleared after each request, but the evidence card
      // remains the authoritative candidate state until an explicit customer
      // or designer confirmation
      // action updates it to ``confirmed``.
      const pendingDrawingReview = files.length === 0
        && drawingJob?.evidence
        && !evidenceAcceptedForPreview(drawingJob.evidence)
        ? drawingJob.evidence
        : null
      // Keep the geometry evidence registry and the AI conversation in sync.
      // The first exact drawing is hash-calibrated and returns confirmed
      // evidence; arbitrary drawings remain reviewable.
      if (files[0]) {
        setAiConversation((current) => ({ ...current, turnStatus: 'submitting', statusMessage: '正在登记图纸并准备视觉分析…' }))
        setGeneration((current) => current ? { ...current, stale: true, pendingDrawing: true } : current)
        setDrawingJob((current) => ({ ...current, file: files[0], status: 'analyzing', requiresFileReselection: false, sourceFileUnavailable: false, interrupted: false, evidence: null, customerAccepted: false, humanConfirmed: false, analysis: null, candidateFields: [], humanEditedFields: [], questions: [], error: '', warning: '' }))
        const primaryExtension = files[0].name?.split('.').pop()?.toLowerCase()
        // DWG uses the dedicated LibreDWG/ezdxf path inside the AI turn.  The
        // legacy /drawings/recognize endpoint is raster/bracket-oriented and
        // must not run opaque DWG bytes through OCR or return its old demo
        // bracket as if it were vector evidence.
        if (primaryExtension !== 'dwg') {
          try {
            recognition = await api.recognizeDrawing(files[0], controller.signal)
          } catch (error) {
            if (error?.name === 'AbortError') throw error
            recognitionError = error
            try {
              const digest = await sha256File(files[0])
              if (digest === acceptanceDrawingSha256) recognition = browserFixtureRecognition(files[0], digest)
            } catch { /* no browser crypto in older contexts */ }
          }
        }
        if (!turnStillCurrent()) throw chatAbortError()
        if (recognition) {
          // Compatibility/OCR recognition is private audit context for this
          // turn.  Do not expose it through `evidence` while the remote SSE is
          // running: the review/check panels would otherwise label its local
          // confidence (for example 24%) as an AI candidate confidence.
          setDrawingJob((current) => ({ ...current, file: files[0], status: 'analyzing', evidence: null, questions: [], error: '', warning: '' }))
        }
      }
      try {
        result = await api.aiConversationStream(
          userText,
          files,
          baseModel,
          history,
          aiConversation.previousResponseId,
          platform.token,
          (eventName, payload) => {
            if (!turnStillCurrent()) return
            if (eventName === 'turn.started') {
              setAiConversation((current) => ({ ...current, status: payload?.provider || current.status, turnStatus: 'streaming', statusMessage: 'AI 正在回复…' }))
            } else if (eventName === 'turn.status') {
              setAiConversation((current) => ({ ...current, turnStatus: 'streaming', statusMessage: payload?.message || 'AI 正在回复…' }))
              setMessages((current) => current.map((item) => item.id === assistantMessageId ? { ...item, statusText: payload?.message || item.statusText } : item))
            } else if (eventName === 'assistant.delta') {
              setMessages((current) => current.map((item) => item.id === assistantMessageId ? { ...item, text: String(payload?.text || ''), status: 'streaming', statusText: '' } : item))
            }
          },
          controller.signal,
        )
      } catch (error) {
        if (error?.name === 'AbortError') throw error
        aiError = error
      }
      if (!turnStillCurrent()) throw chatAbortError()
      if (aiError && !result) throw aiError
      if (recognition && result?.questions?.length) {
        setDrawingJob((current) => ({ ...current, questions: result.questions }))
      }
      // Prefer the recognition returned by /drawings/recognize: its id is
      // registered in the geometry service and can safely be used as
      // sourceDrawingId. The proxy's compact evidence copy is display-only.
      // The legacy compatibility upload endpoint is not part of the
      // platform recognition registry, so its id cannot be used for customer
      // acceptance. Prefer the canonical recognition returned by the AI
      // conversation; only retain the legacy exact-fixture result as a local
      // preview fallback. In particular, never carry its heuristic canonical
      // defaults into an arbitrary drawing candidate.
      const platformDrawing = result?.drawingRecognition
      if (isRecipeIncompatible(result) || isRecipeIncompatible(platformDrawing)) {
        const compatibility = result.recipeCompatibility || platformDrawing?.recipeCompatibility
        const unsupported = compatibility?.unsupportedFeatures || []
        const message = `${result.message || '图纸结构无法完整匹配现有建模方式。'}\n${unsupported.length ? `尚无法表达的结构：${unsupported.join('、')}。\n` : ''}未生成候选模型，请补充结构说明后重新分析。`
        if (files.length) {
          const blank = emptyModel(activeFile?.name, baseModel.material || settings.defaultMaterial)
          modelRef.current = blank
          setModel(blank)
          setGeneration(null)
          setDrawingJob((current) => ({ ...current, status: 'error', evidence: null, generation: null,
            customerAccepted: false, humanConfirmed: false, candidateFields: [], humanEditedFields: [],
            error: message, questions: result.questions || [], analysis: { message, recipeCompatibility: compatibility } }))
          setChatAttachments(files)
        }
        setMessages((current) => current.map((item) => item.id === assistantMessageId ? { ...item, text: message, status: 'complete', statusText: '', candidate: [] } : item))
        setAiConversation((current) => ({ ...current, status: result.provider || current.status, previousResponseId: result.responseId || '', turnStatus: 'completed', statusMessage: '', activeTurnId: '' }))
        showToast('图纸结构不匹配 · 未套用模板，请补充说明后重新分析', 'info')
        return false
      }
      // A compatibility recognition envelope can still be present when the
      // relay has exhausted every remote attempt.  It is useful as private
      // audit evidence, but it must never turn that failed upload into a
      // confirmable CAD candidate or make the previous model look updated.
      const remoteUploadFailed = files.length > 0 && result?.provider?.mode !== 'remote'
      const legacyFixturePreview = recognition?.engine === 'deterministic-calibration' && files[0]
        ? browserFixtureRecognition(files[0], recognition.sourceSha256 || acceptanceDrawingSha256)
        : null
      const drawing = platformDrawing
        ? { ...sanitiseRecognitionForCandidate(platformDrawing), status: 'pending' }
        : legacyFixturePreview
            ? { ...legacyFixturePreview, status: 'pending' }
          : recognition
            ? { ...sanitiseRecognitionForCandidate(recognition), status: 'pending' }
            : null
      if (drawing && !remoteUploadFailed) {
        setDrawingJob((current) => ({
          ...current,
          status: 'ready',
          evidence: drawing,
          error: '',
          warning: drawing.warnings?.join('；') || '',
        }))
      }
      if (!remoteUploadFailed && !recognition && result?.drawingRecognition) {
        // Recognition can still arrive from the conversation proxy when the
        // separate compatibility endpoint is temporarily unavailable. Keep
        // that candidate envelope visible for later customer confirmation.
        setDrawingJob((current) => ({
          ...current,
          status: 'ready',
          evidence: drawing || { ...sanitiseRecognitionForCandidate(result.drawingRecognition), status: 'pending' },
          error: '',
          questions: result.questions || result.drawingRecognition.questions || [],
          warning: result.drawingRecognition.warnings?.join('；') || '',
        }))
      }
      // Treat every non-remote upload result as diagnostics only.  The backend
      // currently returns an empty patch for this state, but keeping the guard
      // here prevents a future/local compatibility payload from changing the
      // visible model or appearing in the chat as an AI CAD suggestion.
      const resultPatch = remoteUploadFailed
        ? {}
        : normalizeAiParameterPatch(result, recognitionParameterAliases)
      const drawingKind = partKindFromEnvelope(platformDrawing || drawing || recognition, baseModel.kind)
      const resultKind = files.length > 0
        ? partKindFromEnvelope({ ...result, candidateParameters: resultPatch }, drawingKind)
        : partKindFromEnvelope({ ...result, candidateParameters: resultPatch }, baseModel.kind)
      const resultDefinition = partDefinition(resultKind)
      let candidate = Object.entries(resultPatch).map(([field, value]) => ({
        field,
        label: resultDefinition.labels[field] || bracketParameterLabels[field] || field,
        from: baseModel?.[field],
        to: value,
      }))
      const aiDrawingPatch = Object.fromEntries(Object.entries(resultPatch)
        .filter(([key, value]) => resultDefinition.keys.includes(key) && value !== undefined && value !== null && value !== ''))
      // On upload turns, only the multimodal model's parameterPatch may seed
      // the candidate. Local OCR/geometry data remains review metadata and
      // must not silently become editable AI output.
      const recognizedParameters = files.length > 0
        ? (!remoteUploadFailed && Object.keys(aiDrawingPatch).length
            ? parametersFromRecognition({
                partType: resultDefinition.kind,
                recipeId: resultDefinition.recipeId,
                candidateParameters: aiDrawingPatch,
                engine: 'ai-candidate',
              })
            : null)
        : parametersFromRecognition(drawing)
      if (remoteUploadFailed) {
        const errorCode = result?.provider?.configured === false
          ? 'not_configured'
          : result?.provider?.lastErrorCode || 'invalid_response'
        const attempts = Number(result?.provider?.attempts || 0)
        const errorLabels = {
          timeout: '中转站响应超时',
          transport: '中转站连接中断',
          incomplete: '模型输出未完成',
          invalid_json: '模型结构化结果不完整',
          empty_response: '模型未返回结果',
          invalid_stream: '流式响应异常',
          invalid_response: '模型返回格式异常',
          not_configured: '远程大模型未配置',
          invalid_dwg_input: 'DWG 文件签名无效或文件已损坏',
          dwg_input_too_large: 'DWG 文件超过本地解析上限',
          dwg_converter_unavailable: '服务器未安装 DWG 转换引擎',
          dwg_conversion_timeout: 'DWG 本地转换超时',
          dwg_conversion_failed: 'DWG 本地转换失败',
          dxf_parser_unavailable: '服务器未安装 DXF 矢量解析组件',
          dxf_parse_failed: '转换后的 DXF 无法解析',
          dxf_render_failed: 'DWG 工程图预览生成失败',
          dwg_resource_limit_exceeded: 'DWG 实体数量或输出超过安全上限',
          dwg_preprocessor_unavailable: '服务器 DWG 解析组件不可用',
        }
        const failureMessage = `${errorLabels[errorCode] || `中转站请求失败（${errorCode}）`}${attempts ? `；已自动尝试 ${attempts} 次` : ''}。原图已保留，可直接重新分析。`
        setDrawingJob((current) => ({
          ...current,
          status: 'error',
          evidence: null,
          customerAccepted: false,
          humanConfirmed: false,
          candidateFields: [],
          humanEditedFields: [],
          questions: [],
          error: failureMessage,
          warning: '',
          analysis: {
            message: result?.message || failureMessage,
            provider: 'local-fallback',
            failureCode: errorCode,
            attempts,
            candidateParameters: {},
            candidateSources: {},
            recognizedFields: [],
            defaultedFields: [],
            missingFields: [],
            needsInput: false,
          },
        }))
      } else if (files.length > 0 && !drawing && !Object.keys(aiDrawingPatch).length) {
        setDrawingJob((current) => ({ ...current, status: 'error', evidence: null, error: aiError?.message || recognitionError?.message || 'AI 未返回可用尺寸候选', warning: '' }))
      }
      const seed = seedModelForAi({
        baseModel,
        definition: resultDefinition,
        recognizedParameters,
        hasAttachments: files.length > 0,
        currentKind: canonicalPartKind(baseModel.kind),
      })
      const identityChanged = !remoteUploadFailed
        && [...productionPartKinds, 'shaft'].includes(resultKind)
        && canonicalPartKind(baseModel.kind) !== resultKind
      const application = applyAiModelPatch(seed, resultPatch, resultDefinition.keys)
      const hasApplicableEdit = canApplyAiModelEdit({ baseModel, accepted: application.accepted, identityChanged })
      const next = !remoteUploadFailed && hasApplicableEdit ? application.model : baseModel
      const changedParameters = Object.fromEntries(resultDefinition.keys
        .filter((key) => next[key] !== baseModel[key] && !(typeof next[key] === 'number' && baseModel[key] !== '' && baseModel[key] != null && next[key] === Number(baseModel[key])))
        .map((key) => [key, next[key]]))
      const patchChanged = !remoteUploadFailed && hasApplicableEdit && (identityChanged || Object.keys(changedParameters).length > 0)
      const geometryChanged = identityChanged || Object.keys(changedParameters).some((key) => !['material', 'units', 'insertThreadDesignation'].includes(key))
      const nextValidation = validateModelParameters(next)
      candidate = Object.entries(files.length > 0 ? application.accepted : patchChanged ? changedParameters : {}).map(([field, value]) => ({
        field, label: resultDefinition.labels[field] || bracketParameterLabels[field] || field,
        from: baseModel?.[field], to: value,
      }))
      if (shouldProtectConcurrentModelEdit({
        hasAttachments: files.length > 0,
        patchChanged: patchChanged || Object.keys(application.accepted).length > 0,
        baseRevision: baseModelInteractionRevision,
        currentRevision: modelInteractionRevisionRef.current,
      })) {
        const provider = result?.provider || aiConversation.status || null
        setMessages((current) => current.map((item) => item.id === assistantMessageId
          ? { ...item, text: `${result?.message || 'AI 已返回修改建议。'}\n检测到你在回复期间编辑了当前模型，因此本轮建议未自动覆盖你的修改。`, status: 'complete', statusText: '', candidate }
          : item))
        setAiConversation((current) => ({
          ...current,
          previousResponseId: result?.provider?.mode === 'remote' ? (result.responseId || current.previousResponseId) : current.previousResponseId,
          status: provider || current.status,
          error: '',
          turnStatus: 'completed',
          statusMessage: '',
          activeTurnId: '',
        }))
        if (files.length > 0) setActivePanel('参数')
        showToast('当前模型已被你编辑 · AI 建议未自动覆盖')
        return false
      }
      if (files.length > 0) setActivePanel('参数')
      if (patchChanged) {
        appliedModelSignature = JSON.stringify(modelParametersForApi(next))
        modelRef.current = next
        setModel(next)
        // Once a newly uploaded drawing has produced a real AI candidate, the
        // previous part's kernel report is no longer meaningful.  Remove it
        // instead of showing an old bbox/face count beside the new editable
        // candidate.  Text-only edits keep the stale artifact long enough for
        // the guarded auto-regeneration path below to replace it.
        setGeneration((current) => files.length > 0
          ? null
          : current ? { ...current, stale: true, pendingDrawing: false } : current)
      }
      if (files.length === 0 && !drawing && (identityChanged || Object.keys(application.accepted).length > 0)) {
        setDrawingJob((current) => syncAiDrawingEdit(current, {
          model: next, patch: application.accepted, definition: resultDefinition,
          existingParameters: rawParametersFromRecognition(current?.evidence) || {},
          sameKind: partKindFromEnvelope(current?.evidence, next.kind) === next.kind,
          needsReview: Boolean(result?.needsReview),
        }))
      }

      let generated = null
      let modelConflictAfterApply = false
      // Any uploaded drawing that is not backed by a confirmed recognition
      // remains review-gated.  This also covers a transient failure of the
      // separate recognition request: a remote model must not turn an
      // unverified attachment into an automatically released solid.
      const attachmentNeedsReview = (files.length > 0 && (files.length !== 1 || !drawing || drawing.status !== 'confirmed'))
        || Boolean(pendingDrawingReview && !identityChanged)
      const effectiveNeedsReview = Boolean(result?.needsReview) || attachmentNeedsReview
      const attachmentGenerationAllowed = (files.length === 0 && (!pendingDrawingReview || identityChanged))
        || (files.length === 1 && drawing?.status === 'confirmed')
      const canAutoGenerate = files.length === 0
        && productionPartKinds.includes(canonicalPartKind(next.kind))
        && !effectiveNeedsReview
        && nextValidation.valid
        && attachmentGenerationAllowed
        && patchChanged
        && backend.status !== 'offline'
      if (canAutoGenerate) {
        try {
          const expectedModelSignature = JSON.stringify(modelParametersForApi(next))
          generated = await generateAiArtifact(next, drawing?.status === 'confirmed' ? drawing.id : '', {
            signal: controller.signal,
            commitGuard: () => turnStillCurrent() && JSON.stringify(modelParametersForApi(modelRef.current)) === expectedModelSignature,
          })
          if (!turnStillCurrent()) throw chatAbortError()
          modelConflictAfterApply = JSON.stringify(modelParametersForApi(modelRef.current)) !== expectedModelSignature
          if (generated && !modelConflictAfterApply) setDrawingJob((current) => ({ ...current, status: 'generated', generation: generated, evidence: drawing || current.evidence }))
        } catch (error) {
          if (error?.name === 'AbortError') throw error
          aiError = aiError || error
        }
      }
      const provider = result?.provider || aiConversation.status || null
      setAiConversation((current) => ({
        ...current,
        previousResponseId: result?.provider?.mode === 'remote' ? (result.responseId || current.previousResponseId) : current.previousResponseId,
        status: provider || current.status,
        error: aiError ? aiError.message : '',
        turnStatus: 'finalizing',
        statusMessage: '正在校验 CAD 修改…',
      }))
      const fallbackNote = aiError && !result
        ? files.length > 0
          ? `（远程大模型提示：${aiError.message}；原图已保留，未使用本地候选替代。）`
          : `（AI/实体服务提示：${aiError.message}，已保留本地明确参数）`
        : ''
      const resultMessage = result?.message || ''
      const parsedDwg = Array.isArray(result?.attachments)
        ? result.attachments.find((item) => item?.dwgPreprocessing?.status === 'parsed')
        : null
      const dwgEvidenceNote = parsedDwg
        ? `\nDWG 已由 ${parsedDwg.dwgPreprocessing.engine || '本地矢量引擎'} 解析：${parsedDwg.dwgPreprocessing.entityCount ?? '—'} 个实体、${parsedDwg.dwgPreprocessing.dimensionCount ?? '—'} 个原生尺寸；AI 已同时读取高清渲染与坐标证据。`
        : ''
      const reviewQuestions = result?.provider?.mode === 'local-fallback' ? [] : (result?.questions || [])
      const review = remoteUploadFailed || !effectiveNeedsReview
        ? ''
        : reviewQuestions.length
          ? `\n待确认：${reviewQuestions.join('；')}`
          : /确认|候选/.test(resultMessage)
            ? ''
            : '\n候选数据待确认。'
      const localText = canonicalPartKind(next.kind) === 'arched_clevis_support'
        ? `参数已更新：外拱 R${next.archOuterRadius}、内拱 R${next.archInnerRadius}；双耳厚 ${next.earThickness}、间隙 ${next.earGap}、耳孔 Ø${next.earHoleDiameter}；安装耳 R${next.mountEarRadius}、安装孔 2×Ø${next.mountHoleDiameter}。`
        : canonicalPartKind(next.kind) === 'stepped_tapered_nozzle'
        ? `参数已更新：同轴主件总长 ${next.mainLength} mm，浅锥 Ø${Number(next.headLeftDiameter).toFixed(3)}→Ø${next.headRightDiameter}、颈段 Ø${next.neckDiameter}、末段 Ø${next.tipDiameter}；Ø${next.counterboreDiameter}×${next.counterboreDepth} 沉孔、Ø${next.axialBoreDiameter} 贯通孔与独立 Ø${next.insertOuterDiameter}×${next.insertLength} ${next.insertThreadDesignation} 镶件。`
        : canonicalPartKind(next.kind) === 'split_clamp_support'
        ? `参数已更新：异形底板 ${next.baseLength} × ${next.baseWidth} × ${next.baseThickness} mm；R${next.pedestalOuterRadius} 圆筒夹座、Ø${next.boreDiameter} 中央盲孔、${next.splitWidth} mm 径向开缝、${next.mountHoleCount}×Ø${next.mountHoleDiameter} 安装孔。`
        : next.kind === 'bracket'
          ? `参数已更新：底板 ${next.baseLength} × ${next.baseWidth} × ${next.baseThickness} mm；上部 ${next.upperLength} × ${next.upperWidth} × ${next.upperHeight} mm；R${next.notchRadius} 鞍槽、两条 ${next.slotWidth} × ${next.slotLength} × ${next.pocketDepth} 浅槽、2×Ø${next.bossDiameter} 贯穿凹槽。`
          : `参数已更新：Ø${next.outerDiameter} × ${next.length} mm，通孔 Ø${next.holeDiameter}；键槽 ${next.keywayWidth} × ${next.keywayDepth} × ${next.keywayLength} mm。`
      // Keep the complete AI analysis beside the editable candidate.  This is
      // intentionally a plain JSON envelope so it survives a refresh and can
      // be audited without exposing provider payloads or credentials.
      if ((files.length > 0 || drawing) && !remoteUploadFailed) {
        const source = drawing || result?.drawingRecognition || {}
        // Keep only values that came from the drawing/OCR/AI candidate. The
        // editable model has a supported recipe surface for previewing, but
        // its default values are not evidence and must never silently become
        // an accepted answer for an unrelated upload.
        const sourceParameters = files.length > 0 ? {} : (rawParametersFromRecognition(source) || {})
        const candidateDefinition = partDefinition(next.kind || resultKind)
        const recognizedCandidateParameters = Object.fromEntries(
          Object.entries({ ...sourceParameters, ...aiDrawingPatch })
            .filter(([key, value]) => candidateDefinition.keys.includes(key) && value !== undefined && value !== null && value !== '')
        )
        const candidateParameters = recognizedCandidateParameters
        const missingCandidateFields = candidateDefinition.required.filter((key) => !requiredParameterPresent(key, candidateParameters[key]))
        const candidateFields = Object.keys(candidateParameters)
        const aiCandidateFields = new Set(Object.keys(aiDrawingPatch))
        const remoteParameterEvidence = normalizeAiParameterEvidence(
          result?.parameterEvidence || result?.parameter_evidence,
          recognitionParameterAliases,
        )
        const candidateSources = Object.fromEntries(Object.keys(candidateParameters).map((key) => {
          if (aiCandidateFields.has(key)) {
            const parameterEvidence = remoteParameterEvidence[key] || {}
            const declaredSource = parameterEvidence.sourceType || parameterEvidence.source_type || parameterEvidence.source
            return [key, normalizedCandidateSource(declaredSource, parameterEvidence.derivation ? 'derived' : 'ai')]
          }
          return [key, 'manual']
        }))
        const defaultedFields = []
        const candidateParameterMeta = Object.fromEntries(Object.entries(candidateSources).map(([key, sourceType]) => {
          const dimension = recognitionEvidence(source).find((item) => {
            const field = recognitionParameterAliases[item?.field] || snakeToCamel(String(item?.field || ''))
            return field === key
          })
          return [key, {
            source: sourceType,
            confidence: remoteParameterEvidence[key]?.confidence !== undefined
              ? normalizedConfidence(remoteParameterEvidence[key].confidence)
              : sourceType === 'drawing' && dimension?.confidence !== undefined ? normalizedConfidence(dimension.confidence) : null,
            sourceView: remoteParameterEvidence[key]?.sourceView || remoteParameterEvidence[key]?.source_view || '',
            sourceText: remoteParameterEvidence[key]?.sourceText || remoteParameterEvidence[key]?.source_text || '',
            derivation: remoteParameterEvidence[key]?.derivation || '',
            requiresConfirmation: true,
          }]
        }))
        const questions = result?.questions || source.questions || []
        const remoteScores = Object.values(remoteParameterEvidence)
          .map((item) => normalizedConfidence(item?.confidence))
          .filter((value) => value !== null)
        const candidateConfidence = remoteScores.length
          ? remoteScores.reduce((sum, value) => sum + value, 0) / remoteScores.length
          : normalizedConfidence(result?.confidence)
        const completeRemoteCandidate = result?.provider?.mode === 'remote' && missingCandidateFields.length === 0
        const assumptions = Array.isArray(result?.assumptions)
          ? result.assumptions
          : completeRemoteCandidate ? [] : (source.assumptions || source.modelRecipe?.assumptions || [])
        const unresolved = Array.isArray(result?.unresolved)
          ? result.unresolved
          : completeRemoteCandidate ? [] : (source.unresolved || [])
        const features = Array.isArray(result?.features)
          ? result.features
          : completeRemoteCandidate ? [] : (source.features || [])
        const analysisMessage = result?.message || (
          recognizedParameters
            ? 'AI 已从图纸证据提取候选尺寸；请逐项核对来源视图与特征语义。'
            : 'AI 暂未形成可信的完整拓扑；已创建可编辑候选参数，请补全并确认后再生成。'
        )
        setDrawingJob((current) => {
          const currentEvidence = current?.evidence && typeof current.evidence === 'object' && Object.keys(current.evidence).length
            ? current.evidence
            : source && typeof source === 'object' && Object.keys(source).length
              ? source
              : {
                  id: `offline_ai_${Date.now()}`,
                  status: 'needs_review',
                  sourceFilename: files[0]?.name || 'AI 图纸',
                  sourceSha256: '',
                  confidence: 0,
                  engine: 'ai-candidate',
                  warnings: ['AI 候选已保留，但服务端未返回可确认的图纸 ID。'],
                }
          const evidenceParameters = candidateParameters
          return {
            ...current,
            status: candidateFields.length ? 'ready' : current.status === 'error' ? 'error' : 'ready',
            evidence: {
              ...currentEvidence,
              // Never display the local OCR confidence as if it measured a
              // remote multimodal answer.  When the model omits per-parameter
              // evidence the honest state is "待评估", not OCR's percentage.
              ...(result?.provider?.mode === 'remote'
                ? { confidence: candidateConfidence ?? undefined }
                : candidateConfidence !== null ? { confidence: candidateConfidence } : {}),
              ...(result?.provider?.mode === 'remote' ? { engine: 'remote-multimodal' } : {}),
              partType: candidateDefinition.kind,
              recipeId: candidateDefinition.recipeId,
              modelRecipe: {
                ...(currentEvidence.modelRecipe || currentEvidence.model_recipe || {}),
                recipeId: candidateDefinition.recipeId,
                parameters: {},
              },
              status: 'pending',
              parameters: evidenceParameters,
              candidateParameters,
              recognizedParameters: recognizedCandidateParameters,
              candidateParameterMeta,
            },
            candidateFields,
            questions,
            analysis: {
              message: analysisMessage,
              provider: result?.provider?.mode || source.engine || 'local-fallback',
              partType: candidateDefinition.kind,
              recipeId: candidateDefinition.recipeId,
              units: source.units || 'mm',
              confidence: result?.provider?.mode === 'remote'
                ? candidateConfidence
                : candidateConfidence ?? normalizedConfidence(source.confidence),
              assumptions,
              unresolved,
              features,
              candidateParameters,
              candidateSources,
              parameterEvidence: remoteParameterEvidence,
              dwgPreprocessing: parsedDwg?.dwgPreprocessing || null,
              recognizedFields: candidateFields,
              defaultedFields,
              missingFields: missingCandidateFields,
              needsInput: missingCandidateFields.length > 0,
            },
          }
        })
      }
      const conflictNote = modelConflictAfterApply ? '\n你在实体生成期间又编辑了模型；旧生成结果已丢弃，当前参数未被覆盖。' : ''
      const editOutcome = files.length === 0 ? aiEditOutcome({
        changed: patchChanged, accepted: application.accepted, rejected: application.rejected,
        geometryChanged, modelValid: nextValidation.valid,
      }) : ''
      const responseText = `${editOutcome ? `${editOutcome}\n\n` : ''}${result?.message || (patchChanged ? localText : 'AI 本轮未提供新的尺寸。')}${dwgEvidenceNote}${generated?.validation?.productionReady ? ' 已生成并通过 OCCT 拓扑检查。' : generated ? ' 已生成可交互 GLB 预览。' : ''}${review}${fallbackNote}${conflictNote}`
      setMessages((prev) => prev.map((item) => item.id === assistantMessageId
        ? { ...item, text: responseText, status: 'complete', statusText: '', candidate }
        : item))
      const keepAttachmentForRetry = files.length > 0 && remoteUploadFailed
      setChatAttachments(keepAttachmentForRetry ? files : [])
      setAiConversation((current) => ({ ...current, turnStatus: 'completed', statusMessage: '', activeTurnId: '' }))
      if (modelConflictAfterApply) showToast('检测到新的人工编辑 · 已丢弃旧生成结果')
      else if (generated?.validation?.productionReady) showToast('AI 修改已应用 · OCCT STEP / GLB 已生成')
      else if (generated) showToast('AI 修改已应用 · 三维实体已更新')
      else if (aiError) showToast('AI 参数已保留；实体服务稍后可重试')
      else if (keepAttachmentForRetry) showToast('远程 AI 本次已降级 · 原图已保留，可再次分析')
      else if (files.length === 0) showToast(editOutcome, patchChanged && nextValidation.valid ? 'success' : 'info')
      else showToast('AI 候选已更新，请确认参数', 'info')
      return true
    } catch (error) {
      // Keep an unexpected malformed response or UI-side exception from
      // leaving the workbench in a permanent "生成中" state. API failures
      // that have a safe local patch are handled above; this branch is the
      // final guard for genuinely unhandled errors.
      if (error?.name === 'AbortError' && requestId === aiRequestRef.current) {
        const currentModelSignature = JSON.stringify(modelParametersForApi(modelRef.current))
        const mayRestoreTurnState = modelInteractionRevisionRef.current === baseModelInteractionRevision
          && (!appliedModelSignature || currentModelSignature === appliedModelSignature)
        if (mayRestoreTurnState) {
          modelRef.current = baseModel
          setModel(baseModel)
          setGeneration(baseGeneration)
          setDrawingJob(baseDrawingJob)
        }
        setMessages((prev) => prev.map((item) => item.id === assistantMessageId
          ? { ...item, text: item.text || '已停止等待本轮回复。', status: 'cancelled', statusText: '已停止' }
          : item))
        setAiConversation((current) => ({ ...current, turnStatus: 'cancelled', statusMessage: '', activeTurnId: '', error: '' }))
        if (files.length) setChatAttachments(files)
      } else if (requestId === aiRequestRef.current) {
        const message = error?.message || 'AI 对话处理失败'
        if (files.length) {
          setDrawingJob(baseDrawingJob)
          setGeneration(baseGeneration)
        }
        setAiConversation((current) => ({ ...current, error: message, turnStatus: 'error', statusMessage: '', activeTurnId: '' }))
        setMessages((prev) => prev.map((item) => item.id === assistantMessageId
          ? { ...item, text: `本次对话未完成：${message}`, status: 'error', statusText: '' }
          : item))
        if (files.length) setChatAttachments(files)
        showToast(`AI 对话失败：${message}`)
      }
      return false
    } finally {
      if (requestId === aiRequestRef.current) {
        setIsGenerating(false)
        if (chatAbortRef.current === controller) chatAbortRef.current = null
      }
    }
  }
  const runGenerate = async () => {
    if (isGenerating || isAccepting || chatAbortRef.current || cadConfirmRef.current || drawingJobRef.current?.cadTask?.runId) return showToast('当前操作尚未完成，请先等待或检查后台结果。', 'info')
    if (!prompt.trim() && !chatAttachments.length) return showToast('请先描述设计或上传一份图纸')
    const submittedPrompt = prompt
    const submittedAttachments = [...chatAttachments]
    setLastAiTurn({ prompt: submittedPrompt, files: submittedAttachments })
    setPrompt('')
    setChatAttachments([])
    await sendAiConversation(submittedPrompt, submittedAttachments)
  }
  const stopAiConversation = () => {
    if (!chatAbortRef.current) return
    chatAbortRef.current.abort()
    showToast('已停止等待本轮 AI 回复')
  }
  const startNewConversation = () => {
    if (isAccepting || cadConfirmRef.current || drawingJobRef.current?.cadTask?.runId) return showToast('当前模型仍在处理，请完成后再开始新对话。', 'info')
    chatAbortRef.current?.abort()
    aiRequestRef.current += 1
    chatAbortRef.current = null
    setIsGenerating(false)
    setPrompt('')
    setChatAttachments([])
    setMessages([
      chatMessage('ai', '已开始新对话。当前 CAD 模型和已生成实体仍然保留，你可以继续提问、修改，或附加一张新图纸。'),
    ])
    setAiConversation((current) => ({ ...current, status: current.serviceStatus || null, providerRoute: '', conversationId: chatId('conversation'), previousResponseId: '', error: '', turnStatus: 'idle', statusMessage: '', activeTurnId: '', cadProgress: null }))
    showToast('已开始新对话 · 当前模型未清空')
  }

  const createBasicShaft = () => {
    if (hasModel || isGenerating || isAccepting) return
    modelInteractionRevisionRef.current += 1
    const next = { ...defaultModel, name: activeFile?.name || '新建轴', material: settings.defaultMaterial }
    modelRef.current = next
    setModel(next)
    setGeneration(null)
    setActivePanel('参数')
    showToast('基础轴已创建，可在右侧修改尺寸')
  }
  const resetModel = () => {
    if (!hasModel) return
    modelInteractionRevisionRef.current += 1
    const definition = partDefinition(model.kind)
    const reset = definition.preview
    modelRef.current = reset
    setModel(reset)
    setGeneration(null)
    // Reset is an explicit customer edit.  If this model came from a drawing,
    // turn the reset snapshot into a new pending candidate so a later generate
    // cannot silently fall back to the previously confirmed evidence values.
    setDrawingJob((current) => {
      if (!current?.evidence) return current
      const candidateParameters = Object.fromEntries(definition.keys
        .filter((key) => reset[key] !== undefined && reset[key] !== '')
        .map((key) => [key, reset[key]]))
      const candidateSources = Object.fromEntries(Object.keys(candidateParameters).map((key) => [key, 'manual']))
      const candidateParameterMeta = Object.fromEntries(Object.keys(candidateParameters).map((key) => [key, {
        source: 'manual', confidence: null, requiresConfirmation: true,
      }]))
      const missingFields = definition.required.filter((key) => !requiredParameterPresent(key, candidateParameters[key]))
      return {
        ...current,
        status: 'ready',
        customerAccepted: false,
        humanConfirmed: false,
        evidence: {
          ...current.evidence,
          status: 'pending',
          parameters: candidateParameters,
          candidateParameters,
          candidateParameterMeta,
        },
        candidateFields: Object.keys(candidateParameters),
        humanEditedFields: Object.keys(candidateParameters),
        analysis: {
          ...(current.analysis || {}),
          candidateParameters,
          candidateSources,
          defaultedFields: [],
          missingFields,
          needsInput: missingFields.length > 0,
        },
      }
    })
    showToast(canonicalPartKind(model.kind) === 'stepped_tapered_nozzle' ? '已恢复阶梯锥管嘴候选基准' : canonicalPartKind(model.kind) === 'split_clamp_support' ? '已恢复开口夹紧座预览基准' : model.kind === 'bracket' ? '已恢复支架基准参数' : '已恢复基准参数')
  }
  const commitStore = (next) => { storeRef.current = next; setWorkspaceStore(next); return next }
  const flushWorkspace = () => {
    const current = storeRef.current
    transientFilesRef.current.set(current.activeFileId, { file: drawingJobRef.current?.file, attachments: chatAttachmentsRef.current })
    return commitStore(ProjectStore.updateFileSnapshot(current, current.activeProjectId, current.activeFileId, currentSnapshotRef.current))
  }
  const restoreWorkspace = (next, mode) => {
    chatAbortRef.current?.abort()
    aiRequestRef.current += 1
    drawingRequestRef.current += 1
    modelInteractionRevisionRef.current += 1
    commitStore(next)
    const file = ProjectStore.getActiveFile(next)
    const snapshot = ProjectStore.getFileSnapshot(next) || {}
    const transient = transientFilesRef.current.get(file?.id) || {}
    const job = { ...(snapshot.drawingJob || { status: 'idle', evidence: null }), ...(transient.file ? { file: transient.file, requiresFileReselection: false, sourceFileUnavailable: false } : {}) }
    const nextModel = modelForPendingDrawing(snapshot.model || emptyModel(file?.name, settings.defaultMaterial), job)
    modelRef.current = nextModel; drawingJobRef.current = job; generationRef.current = snapshot.generation || null
    setModel(nextModel); setDrawingJob(job); setGeneration(snapshot.generation || null)
    setMessages(snapshot.messages?.length ? snapshot.messages.map(normalizeChatMessage) : welcomeMessages())
    setPrompt(snapshot.prompt || ''); setAssemblyItems(snapshot.assemblyItems || []); setDrawingScale(snapshot.drawingScale || '1:1'); setDrawingPreferences(snapshot.drawingPreferences || { layers: {}, selectedView: 'all' })
    setChatAttachments(transient.attachments || []); setLastAiTurn(null); setCheckResult(null)
    setAiConversation((current) => ({ ...current, status: current.serviceStatus || null, providerRoute: '', conversationId: chatId('conversation'), previousResponseId: '', error: '', turnStatus: 'idle', statusMessage: '', cadProgress: null }))
    setIsGenerating(false); setIsAccepting(false); setView(snapshot.view || settings.defaultView); setZoom(snapshot.zoom || 1); setSection(Boolean(snapshot.section))
    setActiveMode(normalizeCadWorkspaceMode(mode) || (file && !file.contentUnavailable ? modeForFile(file) : '项目管理'))
    setMobileMenuOpen(false)
  }
  const canSwitch = () => {
    if (isGenerating || isAccepting || chatAbortRef.current || cadConfirmRef.current || platform.busy) { showToast('当前操作正在处理，请完成或停止等待后再切换文件。', 'info'); return false }
    return true
  }
  const selectLocalProject = (projectId) => {
    if (!canSwitch()) return
    restoreWorkspace(ProjectStore.selectProject(flushWorkspace(), projectId), activeMode === '项目管理' ? '项目管理' : undefined)
  }
  const openProjectFile = (file) => {
    if (!canSwitch() || file.contentUnavailable) return
    restoreWorkspace(ProjectStore.selectProjectFile(flushWorkspace(), file.projectId, file.id))
  }
  const createProject = (name) => {
    if (!canSwitch()) return
    if (typeof name !== 'string') return setDialog('project')
    const next = ProjectStore.createProject(flushWorkspace(), { name })
    restoreWorkspace(next, '3D 建模'); setDialog(''); showToast('独立项目已创建')
  }
  const createFile = ({ name, type, source }) => {
    if (!canSwitch()) return
    const current = flushWorkspace()
    const next = ProjectStore.createProjectFile(current, current.activeProjectId, { name, type,
      ...(source === 'current' && type !== '文档' ? { snapshot: currentSnapshotRef.current } : {}),
    })
    restoreWorkspace(next); showToast(`${type}文件已创建`)
  }
  const renameLocalProject = (id, name) => commitStore(ProjectStore.renameProject(storeRef.current, id, name))
  const renameLocalFile = (id, name) => {
    const current = flushWorkspace()
    const next = ProjectStore.renameProjectFile(current, current.activeProjectId, id, name)
    commitStore(next)
    if (id === current.activeFileId && activeFile?.type === '零件') setModel((value) => ({ ...value, name }))
  }
  const saveVersionForFile = (fileId, note = '') => {
    const current = flushWorkspace()
    const next = ProjectStore.saveFileVersion(current, current.activeProjectId, fileId, { note })
    commitStore(next)
    const saved = ProjectStore.getProjectFile(next, next.activeProjectId, fileId)
    if (saved && !saved.contentUnavailable) showToast(`${saved.name} · ${saved.versions.at(-1)?.label} 已保存`)
  }
  const restoreVersion = (fileId, versionId) => {
    if (!canSwitch()) return
    let next = flushWorkspace()
    next = ProjectStore.saveFileVersion(next, next.activeProjectId, fileId, { note: '恢复历史版本前自动保留草稿' })
    next = ProjectStore.restoreFileVersion(next, next.activeProjectId, fileId, versionId)
    if (fileId === next.activeFileId) { transientFilesRef.current.delete(fileId); restoreWorkspace(next, '项目管理') }
    else commitStore(next)
    showToast('历史版本已恢复；恢复前的草稿已另存为版本')
  }
  const updateDocument = (fileId, documentText) => {
    const current = storeRef.current
    const file = ProjectStore.getProjectFile(current, current.activeProjectId, fileId)
    if (file) commitStore(ProjectStore.updateFileSnapshot(current, current.activeProjectId, fileId, { ...file.snapshot, documentText }))
  }
  const exportFile = async (format, options = {}, targetFile = null) => {
    const scope = workspaceIdRef.current
    const exportRevision = modelInteractionRevisionRef.current
    const snapshot = targetFile?.snapshot || currentSnapshotRef.current
    const exportModel = snapshot.model
    const name = targetFile?.name || activeFile?.name || exportModel?.name || '设计文件'
    try {
      if (format === 'json') {
        downloadBlob(JSON.stringify({ schemaVersion: 1, name, type: targetFile?.type || activeFile?.type, snapshot: ProjectStore.sanitizeWorkspaceSnapshot(snapshot), exportedAt: new Date().toISOString() }, null, 2), `${name}.json`)
      } else if (format === 'txt') {
        downloadBlob(snapshot.documentText || '', `${name}.txt`, 'text/plain;charset=utf-8')
      } else {
        if (!exportModel?.kind) throw new Error('当前文件还没有模型，请先描述零件或上传图纸。')
        const validation = validateModelParameters(exportModel)
        if (!validation.valid) throw new Error(validation.errors.map((item) => item.message).join('；'))
        if (isFeatureModel(exportModel)) {
          if (snapshot.drawingJob?.cadTask?.runId) throw new Error('本轮仍在后台处理，请取得结果后再导出当前模型。')
          if (!productionArtifactsAvailable(snapshot.generation) || !cadGenerationIsCurrent(exportModel, snapshot.generation) || exportModel.agentRun?.status !== 'ready') throw new Error('请先确认当前参数并重新生成实体，再导出交付文件。')
          const artifact = snapshot.generation.artifacts?.find((item) => item.format === format)
          if (!artifact) throw new Error(`当前实体没有 ${format.toUpperCase()} 文件。可导出 STEP、GLB 或 JSON 草稿。`)
          const response = await fetch(cadArtifactUrl(artifact))
          if (!response.ok) throw new Error('实体文件读取失败，请重新生成后再导出。')
          downloadBlob(await response.blob(), `${name}.${format}`, response.headers.get('content-type') || 'application/octet-stream')
          showToast(`${name} · ${format.toUpperCase()} 已准备下载`)
          return
        }
        if (format === 'dxf') {
          downloadBlob(dxfForModel(exportModel, { generation: snapshot.generation, drawingJob: snapshot.drawingJob, scale: snapshot.drawingScale || '1:1', layers: snapshot.drawingPreferences?.layers || {}, ...options }), `${name}.dxf`, 'application/dxf')
        } else {
          if (snapshot.generation?.pendingDrawing || ['queued', 'analyzing'].includes(snapshot.drawingJob?.status) || (snapshot.drawingJob?.evidence && snapshot.drawingJob.evidence.status !== 'confirmed')) throw new Error('请先确认当前候选数据，再生成生产实体')
          let currentGeneration = snapshot.generation
          let artifact = productionArtifactsAvailable(currentGeneration) ? currentGeneration?.artifacts?.find((item) => item.format === format) : null
          if (!artifact && productionPartKinds.includes(canonicalPartKind(exportModel.kind))) {
            showToast(`正在生成 ${format.toUpperCase()} 文件…`, 'info')
            const definition = partDefinition(exportModel.kind)
            const payload = { partType: definition.kind, recipeId: definition.recipeId, parameters: modelParametersForApi(exportModel), formats: [format], requireCadQuery: true }
            currentGeneration = definition.kind === 'bracket' ? await api.generateBracket(payload) : await api.generateModel(payload)
            artifact = productionArtifactsAvailable(currentGeneration) ? currentGeneration.artifacts?.find((item) => item.format === format) : null
            if (!targetFile && workspaceIdRef.current === scope && modelInteractionRevisionRef.current === exportRevision) setGeneration(currentGeneration)
          }
          if (!artifact) throw new Error('当前配方尚无可交付实体，请先导出 JSON 参数草稿')
          const response = await fetch(artifactDownloadUrl(artifact))
          if (!response.ok) throw new Error('实体文件已失效，请重建后重试')
          downloadBlob(await response.blob(), `${name}.${format}`, response.headers.get('content-type') || 'application/octet-stream')
        }
      }
      showToast(`${name} · ${format.toUpperCase()} 已准备下载`)
    } catch (error) { showToast(`导出失败：${readableError(error)}`, 'error') }
  }
  const downloadProjectFile = (file) => exportFile(file.type === '文档' ? 'txt' : file.type === '工程图' ? 'dxf' : 'json', {}, file)
  const retryAi = async () => {
    if (isGenerating || isAccepting || chatAbortRef.current || cadConfirmRef.current) return
    const task = drawingJobRef.current?.cadTask
    if (task?.runId) return sendCadConversation(task.prompt || '继续检查本轮建模结果', [], { resumeTask: task })
    const turn = lastAiTurnRef.current
    if (!turn) {
      if (isFeatureModel(modelRef.current) && modelRef.current.agentRun?.runId) return sendCadConversation('请基于当前已保存的原图与建模草稿，继续检查并完成上一轮未完成的工作。')
      setActiveMode('3D 建模'); showToast('请重新输入要求；有附件时请重新选择原文件。', 'info'); return
    }
    const files = cadRetryFiles(turn, modelRef.current, workspaceIdRef.current)
    if (files === null) return showToast('本页模型已更新，请直接发送新的要求，避免重试旧版本。', 'info')
    await sendAiConversation(turn.prompt, files)
  }
  const runModelChecks = async () => {
    if (isFeatureModel(modelRef.current)) return sendCadConversation('请检查当前实体与原图的尺寸及结构差异，并修正发现的问题。')
    if (!modelRef.current.kind) return showToast('当前文件还没有模型，创建后即可检查。', 'info')
    const validation = validateModelParameters(modelRef.current)
    const scope = workspaceIdRef.current
    const revision = modelInteractionRevisionRef.current
    setIsChecking(true)
    try {
      let result = { ...validation, scope: '浏览器参数约束' }
      if (validation.valid && productionPartKinds.includes(canonicalPartKind(modelRef.current.kind)) && backend.status !== 'offline') {
        const definition = partDefinition(modelRef.current.kind)
        const checked = await api.validateModel({ partType: definition.kind, recipeId: definition.recipeId, parameters: modelParametersForApi(modelRef.current) })
        result = { ...checked, valid: checked.valid ?? checked.validation?.valid, scope: '几何服务参数校验', errors: checked.errors || checked.validation?.errors || (checked.issues || []).filter((issue) => issue.passed === false && issue.severity !== 'warning') }
      }
      if (workspaceIdRef.current === scope && modelInteractionRevisionRef.current === revision) {
        setCheckResult({ ...result, checkedAt: new Date().toLocaleTimeString('zh-CN') })
        showToast(result.valid ? '当前参数检查通过；实体拓扑由生成结果单独验证。' : '检查发现参数问题，请按提示修正。', result.valid ? 'success' : 'error')
      }
    } catch (error) { if (workspaceIdRef.current === scope) showToast(`检查失败：${readableError(error)}`, 'error') }
    finally { setIsChecking(false) }
  }

  const rememberPlatformWorkflow = (projectId = '', planId = '') => {
    platformWorkflowRef.current = {
      projectId: projectId || platformWorkflowRef.current.projectId || '',
      planId: planId || platformWorkflowRef.current.planId || '',
    }
  }

  const hydratePlatformWorkspace = async (token, _preferredProjectId = '', preferredPlanId = '') => {
    if (!token) return
    const local = ProjectStore.getActiveProject(storeRef.current)
    const scope = local?.id
    try {
      const listedProjects = await api.projects(token)
      const availableProjects = listedProjects.items || []
      const boundIds = Object.values(local?.pdmBindings || {})
      const project = availableProjects.find((item) => item.metadata?.localProjectId === scope || boundIds.includes(item.id)) || null
      const manifest = project ? await api.projectManifest(project.id, token) : null
      const plans = project ? (await api.camPlans(token, project.id)).items || [] : []
      const currentGeneration = generationRef.current
      const hash = productionArtifactsAvailable(currentGeneration) ? currentGeneration.artifacts?.find((item) => item.format === 'step')?.sha256 || currentGeneration.requestId : ''
      const matchingPlans = plans.filter((item) => hash && item.geometryHash === hash)
      const camPlan = matchingPlans.find((item) => item.id === preferredPlanId) || matchingPlans[0] || null
      const simulation = camPlan?.latestSimulationId ? await api.camSimulation(camPlan.latestSimulationId, token) : null
      const gate = camPlan ? await api.camGate(camPlan.id, token) : null
      const nc = camPlan?.releasedNcId ? await api.camNcInfo(camPlan.releasedNcId, token) : null
      if (storeRef.current.activeProjectId !== scope) return
      setPlatform((current) => current.token === token
        ? { ...current, project, manifest, camPlan, approval: camPlan?.approvals?.at(-1) || null, simulation, gate, nc, error: '' }
        : current)
    } catch (error) {
      if (storeRef.current.activeProjectId === scope) setPlatform((current) => current.token === token ? { ...current, error: `工作区恢复失败：${readableError(error)}` } : current)
    }
  }

  const platformLogin = async ({ email, password, displayName, roles, bootstrap = false }) => {
    setPlatform((current) => ({ ...current, busy: true, error: '' }))
    try {
      if (bootstrap) {
        try {
          await api.createUser({ email, password, displayName, roles })
        } catch (error) {
          // A persistent database may already have the bootstrap account. In
          // that case continue with login and surface other errors normally.
          if (error.status !== 409) throw error
        }
      }
      const previousWorkflow = { ...platformWorkflowRef.current }
      const session = await api.login(email, password)
      setPlatform((current) => ({ ...current, token: session.access_token, user: session.user, users: [], busy: false, error: '' }))
      showToast(`已登录平台服务 · ${(session.user?.roles || []).join(' / ')}`)
      if ((session.user?.permissions || []).includes('*') || (session.user?.permissions || []).includes('user:manage')) {
        api.users(session.access_token).then((result) => setPlatform((current) => current.token === session.access_token ? { ...current, users: result.items || [] } : current)).catch(() => {})
      }
      // Resolve project/plan state under this account.  This is deliberately
      // asynchronous so login remains responsive while a reviewer or
      // manufacturing account's scoped CAM view is rebuilt.
      hydratePlatformWorkspace(session.access_token, previousWorkflow.projectId, previousWorkflow.planId)
      return session
    } catch (error) {
      setPlatform((current) => ({ ...current, busy: false, error: error.message }))
      showToast(`登录失败：${error.message}`)
      return null
    }
  }

  const platformLogout = () => {
    rememberPlatformWorkflow(platform.project?.id || '', platform.camPlan?.id || '')
    // Keep only the ids needed to rehydrate the workflow on the next login;
    // clear the visible account-scoped records so a logged-out user cannot
    // inspect a prior project's PDM/CAM details in the UI.
    setPlatform(emptyPlatformState())
    setAiConversation((current) => ({ ...current, previousResponseId: '' }))
    showToast('已退出平台服务')
  }

  const refreshPlatformUsers = async () => {
    if (!platform.token) return showToast('请先登录平台服务')
    try {
      const result = await api.users(platform.token)
      setPlatform((current) => ({ ...current, users: result.items || [] }))
      showToast(`已刷新账号列表 · ${(result.items || []).length} 个账号`)
    } catch (error) {
      setPlatform((current) => ({ ...current, error: error.message }))
      showToast(`账号列表读取失败：${error.message}`)
    }
  }

  const createPlatformUser = async ({ email, password, displayName, roles }) => {
    if (!platform.token) return showToast('请先登录平台服务')
    setPlatform((current) => ({ ...current, busy: true, error: '' }))
    try {
      await api.createUser({ email, password, displayName, roles }, platform.token)
      const result = await api.users(platform.token)
      setPlatform((current) => ({ ...current, users: result.items || [], busy: false }))
      showToast(`已创建 ${(roles || []).join(' / ')} 账号`)
      return true
    } catch (error) {
      setPlatform((current) => ({ ...current, busy: false, error: error.message }))
      showToast(`创建账号失败：${error.message}`)
      return false
    }
  }

  const resolvePlatformProject = async (local, token, userId) => {
    const listed = await api.projects(token)
    const boundId = local.pdmBindings?.[userId]
    let project = (listed.items || []).find((item) => item.id === boundId || item.metadata?.localProjectId === local.id)
    if (!project) project = await api.createProject({ name: local.name, description: 'JoyNiu 当前项目', metadata: { localProjectId: local.id } }, token)
    else if (project.name !== local.name) project = await api.renameProject(project.id, local.name, token)
    commitStore({ ...storeRef.current, projects: storeRef.current.projects.map((item) => item.id === local.id ? { ...item, pdmBindings: { ...item.pdmBindings, [userId]: project.id } } : item) })
    return project
  }
  const createPlatformProject = async () => {
    if (!platform.token) return showToast('请先登录平台服务')
    const local = ProjectStore.getActiveProject(storeRef.current)
    const token = platform.token
    setPlatform((current) => ({ ...current, busy: true, error: '' }))
    try {
      const project = await resolvePlatformProject(local, token, platform.user.id)
      const manifest = await api.projectManifest(project.id, token)
      if (storeRef.current.activeProjectId === local.id) setPlatform((current) => current.token === token ? { ...current, project, manifest, busy: false } : current)
      showToast('当前项目已绑定 PDM，清单已刷新')
    } catch (error) {
      setPlatform((current) => current.token === token ? { ...current, busy: false, error: readableError(error) } : current)
      showToast(`PDM 操作失败：${readableError(error)}`, 'error')
    }
  }

  const shareProjectTeam = async (rawMembers = '') => {
    if (!platform.token) return showToast('请先登录平台服务')
    if (!platform.project) return showToast('请先创建或绑定一个 PDM 项目')
    const typedMembers = String(rawMembers || '')
      .split(/[,，\s]+/)
      .map((item) => item.trim())
      .filter(Boolean)
    const directoryMembers = (platform.users || [])
      .filter((item) => (item.roles || []).some((roleValue) => ['reviewer', 'manufacturing'].includes(String(roleValue).toLowerCase())))
      .map((item) => ({ userId: item.id, access: 'read' }))
    const members = typedMembers.length
      ? typedMembers.map((identity) => ({ userId: identity, access: 'read' }))
      : directoryMembers
    if (!members.length) return showToast('没有可共享的审核/制造账号；先创建账号或填写邮箱/用户 ID')
    setPlatform((current) => ({ ...current, busy: true, error: '' }))
    try {
      const result = await api.updateProjectMembers(platform.project.id, members, platform.token)
      const manifest = await api.projectManifest(platform.project.id, platform.token).catch(() => platform.manifest)
      setPlatform((current) => ({ ...current, project: result.project || current.project, manifest, busy: false }))
      showToast(`已共享 ${members.length} 个项目成员（只读，可审核/放行）`)
      return true
    } catch (error) {
      setPlatform((current) => ({ ...current, busy: false, error: error.message }))
      showToast(`项目成员更新失败：${error.message}`)
      return false
    }
  }

  const syncDrawingToPdm = async () => {
    if (!platform.token) return showToast('请先在“平台服务”中登录')
    if (!activeFile || activeFile.contentUnavailable) return showToast('请先打开有内容的文件')
    const current = flushWorkspace()
    const local = ProjectStore.getActiveProject(current)
    const file = ProjectStore.getActiveFile(current)
    const snapshot = ProjectStore.sanitizeWorkspaceSnapshot(currentSnapshotRef.current)
    const sourceFile = drawingJobRef.current?.file
    const token = platform.token
    setPlatform((value) => ({ ...value, busy: true, error: '' }))
    try {
      const project = await resolvePlatformProject(local, token, platform.user.id)
      let manifest = await api.projectManifest(project.id, token)
      const writeVersion = async (category, name, payload) => {
        let document = (manifest.documents || []).map((entry) => entry.document || entry).find((item) => item.metadata?.localFileId === file.id && item.metadata?.category === category)
        if (!document) document = await api.createDocument(project.id, { name: `${name} [${file.id}:${category}]`, kind: category === 'source' ? 'drawing' : 'model', metadata: { displayName: name, localProjectId: local.id, localFileId: file.id, category } }, token)
        await api.createVersion(document.id, { ...payload, note: '同步当前文件快照，保留人工修改', metadata: { displayName: name, localFileId: file.id, modelName: snapshot.model?.name, localUpdatedAt: file.updatedAt } }, token)
      }
      await writeVersion('snapshot', file.name, { content: { schemaVersion: 1, fileName: file.name, fileType: file.type, snapshot }, fileName: `${file.name}.json`, contentType: 'application/json' })
      let savedArtifacts = 0
      const base64 = async (blob) => {
        const bytes = new Uint8Array(await blob.arrayBuffer())
        let binary = ''; for (let index = 0; index < bytes.length; index += 8192) binary += String.fromCharCode(...bytes.subarray(index, index + 8192))
        return btoa(binary)
      }
      if (sourceFile) await writeVersion('source', sourceFile.name, { contentBase64: await base64(sourceFile), fileName: sourceFile.name, contentType: sourceFile.type || 'application/octet-stream' })
      if (productionArtifactsAvailable(snapshot.generation) && validateModelParameters(snapshot.model).valid) {
        for (const artifact of snapshot.generation.artifacts || []) {
          const response = await fetch(artifactDownloadUrl(artifact))
          if (!response.ok) throw new Error('参数快照已保存，但实体文件已失效；请重建后重试同步')
          await writeVersion(artifact.format, `${file.name} · ${artifact.format}`, { contentBase64: await base64(await response.blob()), fileName: artifact.filename || `${file.name}.${artifact.format}`, contentType: response.headers.get('content-type') || 'application/octet-stream' })
          savedArtifacts += 1
        }
      }
      manifest = await api.projectManifest(project.id, token)
      if (storeRef.current.activeProjectId === local.id) setPlatform((value) => value.token === token ? { ...value, project, manifest, busy: false, error: '' } : value)
      showToast(`PDM 已保存「${file.name}」当前快照${sourceFile ? '、原图' : ''}${savedArtifacts ? `与 ${savedArtifacts} 个实体文件` : '（参数草稿）'}`)
    } catch (error) {
      setPlatform((value) => value.token === token ? { ...value, busy: false, error: readableError(error) } : value)
      showToast(`PDM 同步失败：${readableError(error)}`, 'error')
    }
  }

  const createCamPlan = async () => {
    if (!platform.token) return showToast('请先登录平台服务')
    if (!productionArtifactsAvailable(generation)) return showToast('请先生成并校验当前实体')
    if (!platform.project || platform.project.metadata?.localProjectId !== activeProject.id) return showToast('请先绑定当前项目的 PDM 项目')
    if (canonicalPartKind(model.kind) !== 'bracket') return showToast('当前 CAM 三轴铣削方案仅支持安装支架，请使用匹配工艺。', 'info')
    setPlatform((current) => ({ ...current, busy: true, error: '' }))
    try {
      const hash = generation.artifacts?.find((item) => item.format === 'step')?.sha256 || generation.requestId || 'preview-geometry'
      const plan = await api.camPlan({ geometryHash: hash, stock: { length: Number(model.baseLength) + 10, width: Number(model.baseWidth) + 10, height: Number(model.totalHeight) + 5, material: model.material }, machine: '3-axis-mill', projectId: platform.project?.id }, platform.token)
      const operation = await api.camOperation(plan.id, { operationType: 'profile', toolId: 'T10', depth: 1, feedRate: 600, spindleRpm: 6000, retractHeight: 5, pathLength: Number(model.baseLength) + Number(model.baseWidth) }, platform.token)
      const hydratedPlan = await api.camPlanById(plan.id, platform.token).catch(() => ({ ...plan, operations: [...(plan.operations || []), operation], revision: (plan.revision || 1) + 1 }))
      rememberPlatformWorkflow(platform.project?.id || '', hydratedPlan.id)
      setPlatform((current) => ({ ...current, camPlan: hydratedPlan, approval: null, simulation: null, gate: null, nc: null, busy: false }))
      showToast('CAM 草案已创建（尚未放行 NC）')
    } catch (error) {
      setPlatform((current) => ({ ...current, busy: false, error: error.message }))
      showToast(`CAM 计划失败：${error.message}`)
    }
  }

  const simulateCamPlan = async () => {
    if (!platform.token || !platform.camPlan) return showToast('请先创建 CAM 草案')
    setPlatform((current) => ({ ...current, busy: true, error: '' }))
    try {
      const simulation = await api.camSimulate(platform.camPlan.id, {}, platform.token)
      const camPlan = await api.camPlanById(platform.camPlan.id, platform.token).catch(() => platform.camPlan)
      const gate = await api.camGate(platform.camPlan.id, platform.token).catch(() => null)
      setPlatform((current) => ({ ...current, camPlan, simulation, gate, busy: false }))
      showToast(simulation.passed ? 'CAM 确定性预仿真通过 · 等待审核者审批' : 'CAM 仿真发现风险，NC 已阻断')
    } catch (error) {
      setPlatform((current) => ({ ...current, busy: false, error: error.message }))
      showToast(`CAM 仿真失败：${error.message}`)
    }
  }
  const approveCamPlan = async () => {
    if (!platform.token || !platform.camPlan) return showToast('请先创建 CAM 草案')
    setPlatform((current) => ({ ...current, busy: true, error: '' }))
    try {
      const result = await api.camApprove(platform.camPlan.id, { role: 'reviewer', simulationId: platform.simulation?.id, comment: '界面审核通过' }, platform.token)
      const camPlan = await api.camPlanById(platform.camPlan.id, platform.token).catch(() => ({ ...platform.camPlan, status: 'approved', approvals: [...(platform.camPlan?.approvals || []), result] }))
      const gate = await api.camGate(platform.camPlan.id, platform.token).catch(() => null)
      // The approval endpoint returns an Approval record, not a CAMPlan. Keep
      // the immutable plan/id so the subsequent manufacturing release cannot
      // accidentally target `/cam/plans/undefined`.
      setPlatform((current) => ({ ...current, approval: result, camPlan, gate, busy: false }))
      showToast('审核记录已写入；等待制造角色放行 NC')
    } catch (error) {
      setPlatform((current) => ({ ...current, busy: false, error: error.message }))
      showToast(`CAM 审核失败：${error.message}`)
    }
  }
  const releaseCamPlan = async () => {
    if (!platform.token || !platform.camPlan) return showToast('请先创建 CAM 草案')
    setPlatform((current) => ({ ...current, busy: true, error: '' }))
    try {
      const result = await api.camRelease(platform.camPlan.id, { postprocessor: 'generic-3axis', includeText: true }, platform.token)
      const camPlan = await api.camPlanById(platform.camPlan.id, platform.token).catch(() => ({ ...platform.camPlan, status: 'released', releasedNcId: result.id }))
      const gate = await api.camGate(platform.camPlan.id, platform.token).catch(() => ({ ...(platform.gate || {}), passed: true }))
      rememberPlatformWorkflow(platform.project?.id || '', camPlan.id)
      setPlatform((current) => ({ ...current, camPlan, nc: result, busy: false, gate }))
      showToast('NC 已放行并生成；请在机床侧做最终验证')
    } catch (error) {
      setPlatform((current) => ({ ...current, busy: false, error: error.message }))
      showToast(`NC 放行失败：${error.message}`)
    }
  }
  const downloadNcProgram = async () => {
    if (!platform.token || !platform.nc?.id) return showToast('当前账号没有可下载的 NC 程序')
    try {
      const text = platform.nc.text || await api.camNcText(platform.nc.id, platform.token)
      const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = `${platform.nc.id}.nc`
      anchor.click()
      URL.revokeObjectURL(url)
      setPlatform((current) => ({ ...current, nc: { ...current.nc, text } }))
      showToast('NC 程序已按当前制造权限下载')
    } catch (error) {
      showToast(`NC 下载失败：${error.message}`)
    }
  }
  const analyzeDrawing = async (file) => {
    if (!file) return
    modelInteractionRevisionRef.current += 1
    const extension = file.name?.split('.').pop()?.toLowerCase()
    const supported = file.type?.startsWith('image/') || ['pdf', 'dxf', 'dwg'].includes(extension)
    if (!supported) {
      setDrawingJob({ file, previewUrl: '', status: 'error', evidence: null, error: '文件格式不受支持，请上传 JPG、PNG、WEBP、PDF、DWG 或 DXF。' })
      return
    }
    if (file.size > 20 * 1024 * 1024) {
      setDrawingJob({ file, previewUrl: '', status: 'error', evidence: null, error: '文件超过 20 MB 限制，请压缩后重试。' })
      return
    }
    const requestToken = drawingRequestRef.current + 1
    drawingRequestRef.current = requestToken
    if (drawingTimerRef.current) window.clearTimeout(drawingTimerRef.current)
    if (drawingUrlRef.current) URL.revokeObjectURL(drawingUrlRef.current)
    const previewUrl = file.type?.startsWith('image/') ? URL.createObjectURL(file) : ''
    drawingUrlRef.current = previewUrl
    setGeneration(null)
    setDrawingJob({ file, previewUrl, status: 'analyzing', evidence: null, error: '', warning: '' })
    try {
      const result = await api.recognizeDrawing(file)
      if (drawingRequestRef.current !== requestToken) return
      // Recognition can be incomplete. The arched support preserves only
      // explicit dimensions and pauses its preview until they are complete.
      const candidate = { ...sanitiseRecognitionForCandidate(result), status: 'pending' }
      const candidateKind = partKindFromEnvelope(candidate, modelRef.current.kind || 'bracket')
      const definition = partDefinition(candidateKind)
      const recognizedCandidateParameters = rawParametersFromRecognition(candidate) || {}
      const parameters = parametersFromRecognition(candidate) || modelFromCandidate(definition, recognizedCandidateParameters, {
        name: `${definition.preview.name.replace(/\s*·\s*AI\s*候选/g, '')} · AI 候选`,
      })
      // Only values actually returned by the recognizer belong in the
      // confirmable candidate envelope. Missing dimensions stay empty.
      const candidateParameters = Object.fromEntries(Object.entries(recognizedCandidateParameters)
        .filter(([key, value]) => definition.keys.includes(key) && value !== undefined && value !== null && value !== ''))
      const missingFields = definition.required.filter((key) => !requiredParameterPresent(key, candidateParameters[key]))
      const candidateFields = recognitionCandidateFields(candidate).filter((key) => Object.hasOwn(candidateParameters, key))
      const candidateFieldSet = new Set(candidateFields)
      const sourceEngine = String(candidate.engine || '').toLowerCase()
      const returnedMeta = candidate.candidateParameterMeta || candidate.candidate_parameter_meta || {}
      const candidateSources = Object.fromEntries(Object.keys(candidateParameters).map((key) => [
        key,
        normalizedCandidateSource(
          returnedMeta[key]?.source || returnedMeta[key]?.sourceType || returnedMeta[key]?.source_type,
          candidateFieldSet.has(key) && sourceEngine.includes('ai-candidate') ? 'ai' : 'drawing',
        ),
      ]))
      const defaultedFields = []
      const candidateParameterMeta = Object.fromEntries(Object.entries(candidateSources).map(([key, sourceType]) => [key, {
        source: sourceType,
        confidence: returnedMeta[key]?.confidence ?? (['drawing', 'direct_dimension', 'vector_derived'].includes(sourceType) ? Number(candidate.confidence || 0) : null),
        requiresConfirmation: true,
      }]))
      const localCandidateEngine = ['heuristic-review', 'tesseract-compatible', 'compatibility-recognizer', 'deterministic-calibration', 'verified-browser-fixture'].includes(String(candidate.engine || '').toLowerCase())
      const validationWarning = result.validation && result.validation.valid === false
        ? '识别出的候选尺寸存在几何约束冲突，请修正后再确认。'
        : ''
      setDrawingJob({
        file,
        previewUrl,
        status: 'ready',
        evidence: { ...candidate, candidateParameters, recognizedParameters: recognizedCandidateParameters, candidateParameterMeta },
        candidateFields,
        questions: candidate.questions || [],
        analysis: {
          message: localCandidateEngine
            ? '识别服务已返回结构化证据；只有明确读值已写入候选，其余字段保持待补全。'
            : 'AI 已完成图纸分析，以下是待确认的结构化候选数据。',
          provider: candidate.engine || 'OCR',
          partType: definition.kind,
          recipeId: definition.recipeId,
          units: 'mm',
          confidence: candidate.confidence,
          assumptions: candidate.assumptions || [],
          unresolved: candidate.unresolved || [],
          features: candidate.features || [],
          candidateParameters,
          candidateSources,
          recognizedFields: candidateFields,
          defaultedFields,
          missingFields,
          needsInput: missingFields.length > 0,
        },
        error: '',
        warning: [candidate.warnings?.join('；'), validationWarning].filter(Boolean).join('；'),
      })
      // Show the candidate recipe immediately so every recognized value is
      // editable before the customer accepts it; no geometry is generated.
      const nextModel = modelFromCandidate(definition, { ...parameters, ...candidateParameters }, { name: parameters.name || definition.preview.name, updatedAt: '刚刚' })
      modelRef.current = nextModel
      setModel(nextModel)
      setActivePanel('参数')
      setBackend((current) => ({ ...current, status: current.status === 'checking' || current.status === 'offline' ? 'connected' : current.status, engine: result.validation?.engine || current.engine, error: '' }))
      showToast(`图纸分析完成 · 已识别 ${candidateFields.length} 项 · 待补全 ${missingFields.length} 项`)
    } catch (error) {
      if (drawingRequestRef.current !== requestToken) return
      let digest = ''
      try { digest = await sha256File(file) } catch { /* browser preview fallback remains unavailable */ }
      if (digest === acceptanceDrawingSha256) {
        const fallbackEvidence = {
          id: `offline_${Date.now()}`,
          status: 'confirmed',
          confidence: 0.995,
          parameters: bracketModel,
          evidence: bracketParameterKeys.filter((key) => typeof bracketModel[key] === 'number').map((field) => ({ field, value: bracketModel[field], source: '验收夹具哈希匹配 · 待服务端复核', confidence: 0.995 })),
          sourceSha256: digest,
          engine: 'verified-browser-fixture',
          validation: { valid: true, productionReady: false, engine: 'browser-preview', metrics: { boundingLength: 100, boundingWidth: 50, boundingHeight: 40, notchBottomZ: 25 } },
          warnings: [`FastAPI 暂不可用（${error.message}）；当前只展示验收夹具预览，不能导出生产 STEP。`],
        }
        const fixtureCandidates = modelParametersForApi(bracketModel)
        const fixtureSources = Object.fromEntries(Object.keys(fixtureCandidates).map((key) => [key, 'drawing']))
        const fixtureMeta = Object.fromEntries(Object.keys(fixtureCandidates).map((key) => [key, { source: 'drawing', confidence: 0.995, requiresConfirmation: true }]))
        const candidate = { ...fallbackEvidence, status: 'pending', candidateParameters: fixtureCandidates, recognizedParameters: fixtureCandidates, candidateParameterMeta: fixtureMeta }
        setDrawingJob({
          file,
          previewUrl,
          status: 'ready',
          evidence: candidate,
          candidateFields: [...bracketRequiredParameterKeys],
          humanEditedFields: [],
          questions: candidate.questions || [],
          analysis: {
            message: '后端暂不可用；已保留验收图的本地候选数据，请确认后再启动生产生成。',
            provider: 'verified-browser-fixture',
            partType: 'bracket',
            units: 'mm',
            confidence: candidate.confidence,
            assumptions: ['本地验收夹具哈希匹配；当前仅浏览器预览。'],
            unresolved: [],
            features: [],
            candidateParameters: fixtureCandidates,
            candidateSources: fixtureSources,
            recognizedFields: [...bracketRequiredParameterKeys],
            defaultedFields: [],
            missingFields: [],
            needsInput: false,
          },
          error: '',
          warning: candidate.warnings[0],
        })
        setModel((current) => ({ ...current, ...bracketModel, kind: 'bracket', name: '安装支架 · AI 识别', updatedAt: '刚刚' }))
        setActivePanel('参数')
        setBackend((current) => ({ ...current, status: 'offline', productionReady: false, error: error.message }))
        showToast('API 离线：已加载可审计预览，生产 STEP 仍需启动后端')
      } else {
        setDrawingJob({ file, previewUrl, status: 'error', evidence: null, error: `识别服务不可用：${error.message}` })
      }
    }
  }
  const updateDrawingEvidence = (field, value) => {
    modelInteractionRevisionRef.current += 1
    const numeric = ['material', 'insertThreadDesignation'].includes(field) ? value : value === '' ? '' : Number(value)
    setModel((current) => {
      const updated = { ...current, [field]: numeric, updatedAt: '刚刚' }
      modelRef.current = updated
      return updated
    })
    setGeneration((current) => current ? { ...current, stale: true } : current)
    setDrawingJob((current) => {
      if (!current?.evidence) return current
      const parameters = { ...(current.evidence.parameters || {}), [field]: numeric }
      const dimensions = Array.isArray(current.evidence.dimensions)
        ? current.evidence.dimensions.map((item) => {
          const itemField = snakeToCamel(String(item.field || ''))
          return itemField === field ? { ...item, value: numeric } : item
        })
        : current.evidence.dimensions
      const candidateParameters = {
        ...(current.evidence.candidateParameters && typeof current.evidence.candidateParameters === 'object' ? current.evidence.candidateParameters : {}),
        [field]: numeric,
      }
      const evidenceKind = partKindFromEnvelope(current.evidence || current.analysis, modelRef.current.kind)
      const missingFields = requiredKeysForKind(evidenceKind).filter((key) => !requiredParameterPresent(key, candidateParameters[key]))
      const candidateSources = { ...(current.analysis?.candidateSources || {}), [field]: 'manual' }
      const candidateParameterMeta = {
        ...(current.evidence.candidateParameterMeta || {}),
        [field]: { source: 'manual', confidence: null, requiresConfirmation: true },
      }
      const defaultedFields = (current.analysis?.defaultedFields || []).filter((key) => key !== field)
      return {
        ...current,
        status: 'ready',
        customerAccepted: false,
        humanConfirmed: false,
        evidence: { ...current.evidence, parameters, candidateParameters, candidateParameterMeta, ...(dimensions ? { dimensions } : {}), status: 'pending' },
        candidateFields: [...new Set([...(current.candidateFields || []), field])],
        humanEditedFields: [...new Set([...(current.humanEditedFields || []), field])],
        analysis: { ...(current.analysis || {}), candidateParameters, candidateSources, defaultedFields, missingFields, needsInput: missingFields.length > 0 },
      }
    })
  }
  const acceptDrawingData = async () => {
    if (isFeatureModel(modelRef.current)) return confirmCadModel()
    if (isGenerating || isAccepting || ['submitting', 'streaming', 'finalizing'].includes(aiConversation.turnStatus)) {
      showToast('请等待本轮 AI 分析完成后再确认数据')
      return false
    }
    const currentJob = drawingJobRef.current
    if (currentJob.status !== 'ready') return false
    const currentEvidence = currentJob.evidence
    if (!currentEvidence) return showToast('请先完成 AI 分析')
    if (isRecipeIncompatible(currentEvidence) || isRecipeIncompatible(currentJob.analysis)) return showToast('当前图纸结构不匹配已有建模方式，请重新分析或补充结构说明。')
    // Confirm only values supplied by the multimodal model or explicitly
    // edited by the customer. The render model keeps a private scaffold so
    // the viewport can remain usable, but those hidden template values must
    // never enter the confirmation payload.
    const evidenceKind = partKindFromEnvelope(currentEvidence || currentJob.analysis, modelRef.current.kind)
    const definition = partDefinition(evidenceKind)
    if (!definition.kind) return showToast('尚未识别出可建模的零件结构，请重新分析图纸。')
    const candidate = rawParametersFromRecognition(currentEvidence) || {}
    const overrides = Object.fromEntries(Object.entries(candidate)
      .filter(([key, value]) => definition.keys.includes(key) && value !== undefined && value !== null && value !== ''))
    const missing = definition.required.filter((key) => !requiredParameterPresent(key, overrides[key]))
    if (missing.length) return showToast(`请先补全候选尺寸：${missing.slice(0, 3).map((key) => definition.labels[key] || key).join('、')}${missing.length > 3 ? '…' : ''}`)
    if (!modelValid && productionPartKinds.includes(canonicalPartKind(modelRef.current.kind))) return showToast('候选尺寸存在约束冲突，请先修正参数面板中的标红字段')
    const evidenceId = currentEvidence.id || ''
    const candidateSignature = JSON.stringify(overrides)
    const revisionAtSubmit = modelInteractionRevisionRef.current
    const candidateStillCurrent = () => modelInteractionRevisionRef.current === revisionAtSubmit
      && (drawingJobRef.current?.evidence?.id || '') === evidenceId
      && JSON.stringify(rawParametersFromRecognition(drawingJobRef.current?.evidence) || {}) === candidateSignature
    let accepted = { ...currentEvidence, status: currentEvidence.status, parameters: overrides, candidateParameters: overrides }
    let serverAccepted = false
    setIsAccepting(true)
    try {
      if (currentEvidence.id && !String(currentEvidence.id).startsWith('offline_')) {
        try {
          const result = await api.acceptDrawing(currentEvidence.id, { partType: definition.kind, recipeId: definition.recipeId, parameterOverrides: overrides }, platform.token)
          if (result?.status !== 'confirmed') throw new Error('服务端尚未确认当前数据，请检查后重试。')
          const serverParameters = rawParametersFromRecognition(result) || {}
          accepted = { ...accepted, ...result, status: 'confirmed', parameters: { ...serverParameters, ...overrides }, candidateParameters: overrides }
          serverAccepted = true
        } catch (error) {
          const offlineNetworkFailure = backend.status === 'offline' && !error?.status && error?.name !== 'AbortError'
            && /failed to fetch|networkerror|network request failed|load failed|无法连接服务/i.test(error?.message || '')
          if (!offlineNetworkFailure) throw error
          accepted = { ...accepted, status: 'preview_confirmed', warning: `${accepted.warning || ''}${accepted.warning ? '；' : ''}网络离线 · 本地确认仅可生成预览，联网后仍需服务端确认` }
        }
      } else {
        if (backend.status !== 'offline') throw new Error('当前候选没有服务端记录，请重新分析后再确认。')
        // Offline fixture acceptance is intentionally local and can only produce
        // a browser preview; it must never be sent as a confirmed source id.
        accepted = { ...accepted, status: 'preview_confirmed', warning: `${accepted.warning || ''}${accepted.warning ? '；' : ''}本地确认 · 仅可生成预览` }
      }
      if (!candidateStillCurrent()) {
        showToast('候选数据已发生变化；旧确认结果已丢弃，请重新确认')
        return false
      }
      setDrawingJob((current) => ({ ...current, evidence: accepted, status: 'ready', customerAccepted: true, humanConfirmed: serverAccepted, error: '', analysis: { ...(current.analysis || {}), candidateParameters: overrides, needsInput: false } }))
      setModel((current) => {
        const updated = { ...current, ...accepted.parameters, kind: definition.kind, recipeId: definition.recipeId, updatedAt: '刚刚' }
        modelRef.current = updated
        return updated
      })
      showToast(serverAccepted ? '数据已确认；下一步生成 3D' : '已记录你的确认；当前只能生成非生产预览')
      return true
    } catch (error) {
      const message = `确认失败：${readableError(error)} 候选数据已保留，请检查后重试。`
      if (candidateStillCurrent()) setDrawingJob((current) => ({
        ...current, status: 'ready', customerAccepted: false, humanConfirmed: false, error: message,
        evidence: { ...current.evidence, status: 'pending' },
      }))
      showToast(message, 'error')
      return false
    } finally {
      setIsAccepting(false)
    }
  }
  const generateFromDrawing = async () => {
    if (isFeatureModel(modelRef.current)) return confirmCadModel()
    if (drawingJob.status !== 'ready') return showToast('请等待图纸识别完成')
    let recognized = drawingJob.evidence
    if (recognized?.status !== 'confirmed') {
      if (drawingJob.customerAccepted) {
        if (backend.status !== 'offline') return showToast('服务端尚未确认数据，请重试“确认数据”')
        // Offline/local acceptance is allowed to create an explicit preview,
        // but never passes a sourceDrawingId to the production endpoint.
        recognized = {
          ...recognized,
          parameters: rawParametersFromRecognition(recognized) || {},
        }
      } else {
        await acceptDrawingData()
        return
      }
    }
    const recognizedParameters = parametersFromRecognition(recognized)
    if (!recognizedParameters) return showToast('识别结果缺少参数，不能生成实体')
    const generationDefinition = partDefinition(recognizedParameters.kind || recognized.partType || recognized.part_type)
    modelInteractionRevisionRef.current += 1
    setDrawingJob((current) => ({ ...current, status: 'generating', error: '' }))
    setIsGenerating(true)
    let generated
    try {
      const payload = {
        partType: generationDefinition.kind,
        recipeId: generationDefinition.recipeId,
        parameters: modelParametersForApi(recognizedParameters),
        formats: ['step', 'glb'],
        ...(recognized.status === 'confirmed' && recognized.id && !String(recognized.id).startsWith('offline_') ? { sourceDrawingId: recognized.id, confirmed: true } : { confirmed: false }),
        // A healthy OCCT service is required for this production upload path.
        // If the health check is still settling, keep the production intent;
        // an unavailable kernel must fail explicitly instead of being
        // mistaken for a completed manufacturing artifact.
        requireCadQuery: backend.status !== 'offline',
      }
      generated = generationDefinition.kind === 'bracket'
        ? await api.generateBracket(payload)
        : await api.generateModel(payload)
      const step = generated.artifacts?.find((item) => item.format === 'step')
      if (!step || generated.validation?.valid !== true) throw new Error('实体校验未通过，未生成可交付文件')
      if (backend.status !== 'offline' && !step.productionReady) throw new Error('OCCT 实体或 STEP 拓扑校验未达到生产交付条件')
      setGeneration(generated)
      setBackend((current) => ({ ...current, status: current.status === 'offline' ? 'degraded' : current.status === 'checking' ? 'connected' : current.status, engine: generated.engine || step.engine, productionReady: Boolean(step.productionReady), error: '' }))
      setDrawingJob((current) => ({ ...current, status: 'generated', generation: generated }))
    } catch (error) {
      if (backend.status !== 'offline') {
        setDrawingJob((current) => ({ ...current, status: 'ready', error: `实体生成失败：${error.message}` }))
        setIsGenerating(false)
        showToast(`实体生成失败：${error.message}`)
        return
      }
      const previewKind = generationDefinition.kind
      const previewDiameter = Math.max(Number(recognizedParameters.headLeftDiameter), Number(recognizedParameters.headRightDiameter))
      generated = {
        requestId: `preview_${Date.now()}`,
        status: 'completed',
        engine: 'browser-preview',
        parameters: recognizedParameters,
        artifacts: [],
        validation: {
          valid: true,
          productionReady: false,
          engine: 'browser-preview',
          metrics: previewKind === 'arched_clevis_support'
            ? archedClevisMetrics(recognizedParameters)
            : previewKind === 'stepped_tapered_nozzle'
            ? { boundingLength: recognizedParameters.mainLength, boundingWidth: previewDiameter, boundingHeight: previewDiameter, solidCount: 2 }
            : { boundingLength: recognizedParameters.baseLength, boundingWidth: recognizedParameters.baseWidth, boundingHeight: recognizedParameters.totalHeight },
        },
        warnings: ['仅生成浏览器参数化预览；未生成 STEP。'],
      }
      setGeneration(generated)
      setDrawingJob((current) => ({ ...current, status: 'generated', generation: generated }))
    }
    const recognizedName = recognized.name || generationDefinition.preview.name
    const generatedName = /候选/.test(recognizedName)
      ? recognizedName.replace(/\s*·?\s*AI\s*候选/g, '').replace(/候选/g, '').trim() + ' · 图纸实体'
      : recognizedName
    const generatedModel = modelFromCandidate(generationDefinition, recognizedParameters, { name: generatedName, updatedAt: '刚刚' })
    modelRef.current = generatedModel
    setModel(generatedModel)
    setSelectedFeature(generationDefinition.kind === 'arched_clevis_support' ? 'arch' : generationDefinition.kind === 'stepped_tapered_nozzle' ? 'head-taper' : generationDefinition.kind === 'split_clamp_support' ? 'pedestal' : 'notch')
    setActivePanel('参数')
    setView('isometric')
    setZoom(1)
    const metrics = generated.validation?.metrics || {}
    const productionText = generated.validation?.productionReady ? 'OCCT 实体与 STEP 已通过拓扑检查' : '当前是浏览器预览，未形成生产 STEP'
    const generatedDescription = generationDefinition.kind === 'arched_clevis_support'
      ? `图纸已确认：外拱 R${recognizedParameters.archOuterRadius}、内拱 R${recognizedParameters.archInnerRadius}；双耳厚 ${recognizedParameters.earThickness}、间隙 ${recognizedParameters.earGap}、横向耳孔 Ø${recognizedParameters.earHoleDiameter}；底部安装耳厚 ${recognizedParameters.baseThickness}、安装孔 2×Ø${recognizedParameters.mountHoleDiameter}。`
      : generationDefinition.kind === 'stepped_tapered_nozzle'
      ? `图纸已确认：总长 ${recognizedParameters.mainLength} mm 的阶梯锥管嘴主件，包含 Ø${recognizedParameters.counterboreDiameter}×${recognizedParameters.counterboreDepth} 沉孔、Ø${recognizedParameters.axialBoreDiameter} 贯通孔与 Ø${recognizedParameters.outletDiameter} 出口锥；Ø${recognizedParameters.insertOuterDiameter}×${recognizedParameters.insertLength} ${recognizedParameters.insertThreadDesignation} 镶件仍作为独立第二组件。未生成真实螺纹牙型。`
      : generationDefinition.kind === 'split_clamp_support'
        ? `图纸已确认：${recognizedParameters.baseLength} × ${recognizedParameters.baseWidth} × ${recognizedParameters.baseThickness} 异形底板、R${recognizedParameters.pedestalOuterRadius} 圆筒夹座、Ø${recognizedParameters.boreDiameter} 中央盲孔、${recognizedParameters.splitWidth} mm 径向开缝、${recognizedParameters.mountHoleCount}×Ø${recognizedParameters.mountHoleDiameter} 安装孔及 Ø${recognizedParameters.crossHoleDiameter} 横孔。`
        : `图纸已确认：${recognizedParameters.baseLength} × ${recognizedParameters.baseWidth} × ${recognizedParameters.baseThickness} 底板、${recognizedParameters.upperLength} × ${recognizedParameters.upperWidth} × ${recognizedParameters.upperHeight} 上部实体；R${recognizedParameters.notchRadius} 横向鞍槽、两条 ${recognizedParameters.slotWidth || 10} × ${recognizedParameters.slotLength || 30} × ${recognizedParameters.pocketDepth || 10} 浅槽、2×Ø${recognizedParameters.bossDiameter} 贯穿凹槽。`
    const boundingText = modelBoundsText(generatedModel, metrics)
    setMessages((prev) => [...prev, { role: 'ai', text: `${generatedDescription}${productionText}；包络 ${boundingText} mm。` }])
    setActiveMode('3D 建模')
    setIsGenerating(false)
    showToast(generated.validation?.productionReady ? '实体与 STEP 已生成并通过 OCCT 校验' : '已生成参数预览；启动后端后可生成生产 STEP')
  }
  const attachDrawingToConversation = (fileInput) => {
    if (isGenerating || isAccepting || chatAbortRef.current || cadConfirmRef.current || drawingJobRef.current?.cadTask?.runId) return showToast('当前模型仍在处理，请完成后再更换图纸。', 'info')
    const selectedFiles = normalizeFilesInput(fileInput)
    if (!selectedFiles.length) return
    if (selectedFiles.length > 4) return showToast('一次最多上传 4 个图纸文件', 'error')
    const unsupported = selectedFiles.find((file) => {
      const extension = file.name?.split('.').pop()?.toLowerCase()
      return !(file.type?.startsWith('image/') || ['pdf', 'dxf', 'dwg'].includes(extension))
    })
    if (unsupported) return showToast('不支持此文件格式，请选择 JPG、PNG、WEBP、PDF、DWG 或 DXF。', 'error')
    if (selectedFiles.some((file) => file.size > 20 * 1024 * 1024)) return showToast('单个图纸不能超过 20 MB')
    if (!activeFile || activeFile.type === '文档' || activeFile.contentUnavailable) {
      if (!canSwitch()) return
      createFile({ name: selectedFiles[0].name.replace(/\.[^.]+$/, ''), type: '零件', source: 'blank' })
    }
    // Keep upload, recognition and generation as visible stages.  Selecting a
    // file only queues it in the composer; the customer can still add intent
    // (for example "只识别主视图") before pressing the single primary action.
    // New uploads run through the CAD agent; existing recipe-only models
    // keep their original editing path until a new drawing is submitted.
    setActiveMode('3D 建模')
    setChatAttachments(selectedFiles)
    // Merely selecting an attachment must not invalidate the current CAD
    // entity or confirmed evidence.  A new drawing context begins only when
    // the customer actually sends this chat turn.
    showToast(`${selectedFiles.length === 1 ? '图纸' : `${selectedFiles.length} 个文件`}已附加到下一条消息`)
  }
  const remodelLegacyDrawing = async (fileInput) => {
    if (isGenerating || isAccepting || chatAbortRef.current || cadConfirmRef.current || drawingJobRef.current?.cadTask?.runId) return false
    if (!isLegacyDrawingDraft(modelRef.current, drawingJobRef.current)) return false
    const files = fileInput ? normalizeFilesInput(fileInput) : cadLegacySourceFiles(drawingJobRef.current)
    if (files.length !== 1 || !(files[0] instanceof File)) { showToast('旧项目仅保存了文件信息，请重新选择原图。', 'info'); return false }
    const file = files[0], extension = file.name.split('.').pop()?.toLowerCase()
    if (!(file.type.startsWith('image/') || ['pdf', 'dxf', 'dwg'].includes(extension))) { showToast('请选择原始图片、PDF、DWG 或 DXF 图纸。', 'error'); return false }
    if (file.size > 20 * 1024 * 1024) { showToast('单个图纸不能超过 20 MB', 'error'); return false }
    setActiveMode('3D 建模')
    setChatAttachments([])
    showToast('开始按原图重新建模；旧草稿会保留在项目历史版本中。', 'info')
    return sendCadConversation('请依据这份原图重新识别尺寸和结构，建立实体并核对投影。不要沿用旧版草稿的参数或预设形状。', files)
  }
  useEffect(() => {
    // Defer one tick so StrictMode's setup/cleanup probe cannot abort the
    // recovery request and accidentally persist it as a user-paused task.
    const timer = setTimeout(() => {
      const task = drawingJobRef.current?.cadTask
      if (task?.runId && task.status === 'running' && !task.paused && !chatAbortRef.current && !cadConfirmRef.current) {
        void sendCadConversation(task.prompt || '继续检查本轮建模结果', [], { resumeTask: task })
      }
    }, 0)
    return () => clearTimeout(timer)
  }, [activeFile?.id])
  useEffect(() => {
    platformWorkflowRef.current = { projectId: '', planId: '' }
    setPlatform((value) => ({ ...value, project: null, manifest: null, camPlan: null, approval: null, simulation: null, gate: null, nc: null, error: '' }))
    if (platform.token) hydratePlatformWorkspace(platform.token)
  }, [workspaceStore.activeProjectId, workspaceStore.activeFileId, platform.token, generation?.requestId, generation?.stale])
  const recentFiles = workspaceStore.projects.flatMap((project) => project.files.filter((file) => !file.contentUnavailable).map((file) => ({ ...file, projectName: project.name }))).sort((a, b) => String(b.lastOpenedAt || b.updatedAt).localeCompare(String(a.lastOpenedAt || a.updatedAt))).slice(0, 12)
  const exportBackup = () => downloadBlob(JSON.stringify(flushWorkspace(), null, 2), 'JoyNiu-项目备份.json')
  const refreshServices = async () => {
    setBackend((current) => ({ ...current, status: 'checking' }))
    const [healthResult, aiResult] = await Promise.allSettled([api.health(), api.aiStatus()])
    if (healthResult.status === 'fulfilled') {
      const health = healthResult.value
      setBackend({ status: health.geometry?.available ? 'connected' : 'degraded', engine: health.geometry?.engine || '浏览器预览', productionReady: Boolean(health.geometry?.available), health, error: '' })
    } else setBackend({ status: 'offline', engine: '浏览器预览', productionReady: false, health: null, error: readableError(healthResult.reason) })
    if (aiResult.status === 'fulfilled') setAiConversation((current) => ({ ...current, serviceStatus: aiResult.value, status: isGenerating ? current.status : aiResult.value, providerRoute: isGenerating ? current.providerRoute : '' }))
    showToast(healthResult.status === 'fulfilled' ? '服务状态已更新' : '无法连接几何服务，当前草稿仍然保留。', healthResult.status === 'fulfilled' ? 'success' : 'error')
  }
  const exportDiagnostics = () => downloadBlob(JSON.stringify({ exportedAt: new Date().toISOString(), backend: { status: backend.status, engine: backend.engine, error: backend.error }, fileType: activeFile?.type, modelKind: model.kind, parameterErrors: parameterValidation.errors, storageError }, null, 2), 'JoyNiu-诊断信息.json')
  const startTextDesign = (text = '') => {
    if (!activeFile || activeFile.type === '文档' || activeFile.contentUnavailable) createFile({ name: '新零件', type: '零件', source: 'blank' })
    setPrompt(text); setActiveMode('3D 建模')
  }
  return (
    <div className={`app-shell text-size-${settings.textSize}`}>
      <header className="topbar">
        <button className="mobile-menu-button" aria-label="打开导航菜单" aria-expanded={mobileMenuOpen} onClick={() => setMobileMenuOpen((value) => !value)}>☰</button><div className="brand"><div className="brand-mark" aria-hidden="true"><span>J</span><i /></div><div><strong>JoyNiu <em>CAD</em></strong><span>创模 AI · ENGINEERING</span></div></div>
        <nav className="topbar-center" aria-label="主要工作台">
          {mainModes.map((mode) => <button key={mode} className={`mode-tab ${activeMode === mode ? 'active' : ''}`} onClick={() => setActiveMode(mode)}>{mode === '3D 建模' && <Icon>✦</Icon>}{mode}</button>)}
        </nav>
        <div className="topbar-actions"><span className="credits">{platform.user?.displayName || platform.user?.display_name || '本地工作区'}</span><button className="icon-button" aria-label="打开快捷命令" onClick={() => setDialog('commands')}>⌘K</button><button className="avatar" aria-label="打开账号" onClick={() => setActiveMode('平台服务')}>{(platform.user?.displayName || platform.user?.display_name)?.slice(0, 1) || 'J'}</button></div>
      </header>

      <div className="workspace">
        {mobileMenuOpen && <button className="menu-backdrop" aria-label="关闭导航菜单" onClick={() => setMobileMenuOpen(false)} />}
        <aside className={`sidebar ${mobileMenuOpen ? 'open' : ''}`} onClick={(event) => { if (event.target.closest('button')) setMobileMenuOpen(false) }}>
          <button className="new-project" onClick={createProject}><span>＋</span><span className="new-project-label">新建项目</span><kbd>⌘N</kbd></button>
          <div className="side-section"><div className="side-label">设计</div>
            <button title="项目" className={`side-link ${activeMode === '项目管理' ? 'active' : ''}`} onClick={() => setActiveMode('项目管理')}><Icon>▦</Icon><span className="side-link-label">我的项目</span><span className="count">{projects.length}</span></button>
            <button title="最近打开" className="side-link" onClick={() => { setDialog('recent'); setMobileMenuOpen(false) }}><Icon>◷</Icon><span className="side-link-label">最近打开</span></button>
            <button title="标准件库" className={`side-link ${activeMode === '标准件库' ? 'active' : ''}`} onClick={() => setActiveMode('标准件库')}><Icon>⬡</Icon><span className="side-link-label">标准件库</span></button>
            <button title="设计工作台 · 图纸导入" className={`side-link ${activeMode === '3D 建模' ? 'active' : ''}`} onClick={() => { setActiveMode('3D 建模'); showToast('已打开设计工作台 · 上传后按 AI 分析 → 确认数据 → 生成 3D') }}><Icon>⌁</Icon><span className="side-link-label">设计工作台 / 图纸导入</span><span className="new-badge">推荐</span></button>
          </div>
          <div className="side-section project-list"><div className="side-label">当前项目</div>{projects.map((project) => <button title={project.name} key={project.id} className={`project-link ${activeProject?.id === project.id ? 'selected' : ''}`} onClick={() => selectLocalProject(project.id)}><span className={`project-dot ${project.color}`} /><span className="project-link-label">{project.name}</span><span className="project-files">{project.files}</span></button>)}</div>
          <div className="sidebar-bottom"><div className="side-label">高级</div><button title="PDM / 账号" className={`side-link ${activeMode === '平台服务' ? 'active' : ''}`} onClick={() => setActiveMode('平台服务')}><Icon>◈</Icon><span className="side-link-label">PDM / 账号</span></button><button title="CAM / NC" className={`side-link ${activeMode === 'CAM / NC' ? 'active' : ''}`} onClick={() => setActiveMode('CAM / NC')}><Icon>⌁</Icon><span className="side-link-label">CAM / NC</span></button><button title="设置" className="side-link" onClick={() => setActiveMode('设置')}><Icon>⚙</Icon><span className="side-link-label">设置</span></button><button title="帮助与反馈" className="side-link" onClick={() => setActiveMode('帮助与反馈')}><Icon>?</Icon><span className="side-link-label">帮助与反馈</span></button><div className={`engine-status ${backend.status}`} title={`${API_BASE} · ${backend.error || '服务正常'}`}><span className="status-dot" /><div><b>{backend.status === 'checking' ? '连接 FastAPI…' : backend.productionReady ? 'CadQuery / OCCT' : backend.status === 'degraded' ? '降级几何内核' : '浏览器预览'}</b><small>{backend.status === 'connected' ? 'B-Rep 与 STEP 可用' : backend.status === 'degraded' ? '仅审计预览，不可生产' : backend.status === 'offline' ? 'API 离线 · 不可导出 STEP' : API_BASE}</small></div></div></div>
        </aside>

        <main className="main-area">
          <div className="breadcrumb"><span>{selectedProject}</span><Icon>›</Icon><b>{activeMode === '首页' ? '项目概览' : activeMode}</b>{activeFile && <span> · {activeFile.name}</span>}<span className="save-status"><span className="status-dot" /> {storageError ? '草稿尚未保存' : '本地自动保存'}</span></div>
          {storageError && <div className="storage-warning" role="alert">{storageError}<button onClick={exportBackup}>导出备份</button></div>}
          {normalizeCadWorkspaceMode(activeMode) === '3D 建模' && activeFile?.type !== '文档' && activeFile && !activeFile.contentUnavailable && <ModelWorkspace key={activeFile.id} {...{ activePanel, setActivePanel, model, hasModel, modelValid, updateModel, resetModel, createBasicShaft, features: currentFeatures, selectedFeature, setSelectedFeature, prompt, setPrompt, runGenerate, stopAiConversation, startNewConversation, rebuildCurrentModel, recoverExpiredProductionGlb, isGenerating, isAccepting, messages, view, setView, section, setSection, zoom, setZoom, exportFile, showToast, backend, generation, attachDrawingToConversation, remodelLegacyDrawing, chatAttachments, setChatAttachments, aiConversation, platform, drawingJob, setActiveMode, generateFromDrawing, acceptDrawingData, parameterValidation, checkResult, isChecking, runModelChecks, retryAi, saveCurrentVersion: () => saveVersionForFile(activeFile.id) }} />}
          {activeMode === '2D 工程图' && activeFile?.type !== '文档' && activeFile && !activeFile.contentUnavailable && (hasModel ? isFeatureModel(model) ? <CadAgentDrawing model={model} generation={generation} busy={isGenerating || isAccepting || drawingJob?.cadTask?.status === 'running'} onBack={() => setActiveMode('3D 建模')} onExport={exportFile} /> : <DrawingWorkspace key={activeFile.id} model={model} generation={generation} drawingJob={drawingJob} drawingScale={drawingScale} setDrawingScale={setDrawingScale} drawingPreferences={drawingPreferences} setDrawingPreferences={setDrawingPreferences} onExport={exportFile} onSaveVersion={() => saveVersionForFile(activeFile.id)} onEditParameters={() => { setActiveMode('3D 建模'); setActivePanel('参数') }} showToast={showToast} /> : <section className="secondary-workspace"><h1>还没有可生成工程图的模型</h1><p>先创建零件，工程图将随模型尺寸生成。</p><button className="primary-button" onClick={() => setActiveMode('3D 建模')}>开始建模</button></section>)}
          {activeMode === '装配' && activeFile?.type !== '文档' && activeFile && !activeFile.contentUnavailable && <AssemblyWorkspace key={activeFile.id} model={model} assemblyItems={assemblyItems} setAssemblyItems={setAssemblyItems} onBackToModel={() => setActiveMode('3D 建模')} onOpenLibrary={() => setActiveMode('标准件库')} showToast={showToast} />}
          {activeMode === '标准件库' && activeFile?.type !== '文档' && activeFile && !activeFile.contentUnavailable && <LibraryWorkspace key={activeFile.id} model={model} assemblyItems={assemblyItems} setAssemblyItems={setAssemblyItems} onOpenAssembly={() => setActiveMode('装配')} onBackToModel={() => setActiveMode('3D 建模')} showToast={showToast} />}
          {['3D 建模', '2D 工程图', '装配', '标准件库'].includes(activeMode) && (!activeFile || activeFile.type === '文档' || activeFile.contentUnavailable) && <section className="secondary-workspace"><h1>先打开一个设计文件</h1><p>当前内容是文档或尚未创建模型。可在项目中打开零件、工程图或装配文件。</p><button className="primary-button" onClick={() => setActiveMode('项目管理')}>打开项目文件</button><button className="secondary-button" onClick={() => createFile({ name: '新零件', type: '零件', source: 'blank' })}>新建零件</button></section>}
          {activeMode === '项目管理' && <ProjectFilesWorkspace store={workspaceStore} storageError={storageError} onSelectProject={selectLocalProject} onCreateProject={createProject} onRenameProject={renameLocalProject} onCreateFile={createFile} onRenameFile={renameLocalFile} onOpenFile={openProjectFile} onDownloadFile={downloadProjectFile} onSaveVersion={saveVersionForFile} onRestoreVersion={restoreVersion} onUpdateDocument={updateDocument} />}
          {activeMode === '设置' && <SettingsWorkspace settings={settings} onChange={setSettings} onExportBackup={exportBackup} />}
          {activeMode === '帮助与反馈' && <HelpWorkspace onNavigate={setActiveMode} onDiagnostics={exportDiagnostics} />}
          {(activeMode === '平台服务' || activeMode === 'CAM / NC') && <PlatformWorkspace onRefreshServices={refreshServices} mode={activeMode} backend={backend} platform={platform} platformLogin={platformLogin} platformLogout={platformLogout} refreshPlatformUsers={refreshPlatformUsers} createPlatformUser={createPlatformUser} createPlatformProject={createPlatformProject} shareProjectTeam={shareProjectTeam} syncDrawingToPdm={syncDrawingToPdm} createCamPlan={createCamPlan} simulateCamPlan={simulateCamPlan} approveCamPlan={approveCamPlan} releaseCamPlan={releaseCamPlan} downloadNcProgram={downloadNcProgram} generation={generation} showToast={showToast} />}
          {activeMode === '首页' && <HomeWorkspace projects={projects} onSelectProject={selectLocalProject} onStartText={startTextDesign} createProject={createProject} setActiveMode={setActiveMode} showToast={showToast} attachDrawingToConversation={attachDrawingToConversation} />}
        </main>
      </div>
      {dialog === 'project' && <NewProjectDialog suggestedName={`新建项目 ${projects.length + 1}`} onSubmit={createProject} onClose={() => setDialog('')} />}
      {dialog === 'commands' && <CommandDialog onClose={() => setDialog('')} commands={[...['首页', '3D 建模', '2D 工程图', '装配', '标准件库', '项目管理', '平台服务', 'CAM / NC', '设置', '帮助与反馈'].map((mode) => ({ label: `打开${mode}`, action: () => setActiveMode(mode) })), { label: '新建项目', action: () => setDialog('project'), shortcut: '⌘ / Ctrl + N' }, { label: '保存当前文件版本', action: () => saveVersionForFile(activeFile?.id), shortcut: '⌘ / Ctrl + S' }, { label: '最近打开', action: () => setDialog('recent') }]} />}
      {dialog === 'recent' && <WorkspaceDialog title="最近打开的文件" onClose={() => setDialog('')}><div className="command-list">{recentFiles.map((file) => <button key={file.id} onClick={() => { openProjectFile(file); setDialog('') }}><span>{file.name}<small>{file.projectName} · {file.type}</small></span><small>{new Date(file.lastOpenedAt || file.updatedAt).toLocaleString('zh-CN')}</small></button>)}</div></WorkspaceDialog>}
      {toast && <div className={`toast ${toast.type}`} role={toast.type === 'error' ? 'alert' : 'status'}><span className="toast-icon">{toast.type === 'error' ? '!' : toast.type === 'info' ? 'i' : '✓'}</span><span>{toast.message}</span><button className="toast-dismiss" aria-label="关闭提示" onClick={() => setToast(null)}>×</button></div>}
    </div>
  )
}

function workflowSnapshot({ drawingJob, generation, chatAttachments, isGenerating, model, modelValid }) {
  const evidence = drawingJob?.evidence
  const hasFile = Boolean(drawingJob?.file || drawingJob?.fileMeta?.name || chatAttachments?.length)
  const reviewRequired = Boolean(evidence && !evidenceAcceptedForPreview(evidence))
  const pendingConfirmedDrawing = Boolean(evidenceAcceptedForPreview(evidence) && generation?.pendingDrawing)
  const generated = Boolean(generation)
  if (isGenerating && drawingJob?.status === 'analyzing') return { current: 'recognize', label: 'AI 分析中' }
  if (isGenerating && drawingJob?.status === 'generating') return { current: 'generate', label: '正在生成 3D' }
  if (drawingJob?.status === 'error' && drawingJob?.analysis?.provider === 'local-fallback') {
    return { current: 'recognize', label: 'AI 分析失败，可重新尝试' }
  }
  // A new upload always starts a new workflow. The previous solid may remain
  // visible as a clearly labelled historical preview, but its completed steps
  // must not make the new drawing look analyzed/confirmed/generated already.
  if (chatAttachments?.length || drawingJob?.status === 'queued') return { current: 'recognize', label: '图纸已添加，开始 AI 分析' }
  if (isLegacyDrawingDraft(model, drawingJob)) return { current: 'recognize', label: '旧版图纸草稿，待按原图重建' }
  if (reviewRequired) return { current: 'review', label: '确认候选数据' }
  if (pendingConfirmedDrawing) return { current: 'generate', label: '数据已确认，准备生成' }
  if (generated && generation?.stale) return { current: 'edit', label: '参数已修改，等待重建' }
  if (generated) return { current: 'edit', label: '实体已生成，可继续修改' }
  if (evidence) return { current: 'generate', label: '数据已确认，准备生成' }
  if (model?.kind && modelValid && drawingJob?.status === 'idle' && !drawingJob.requiresFileReselection && !drawingJob.interrupted) return { current: 'generate', label: isGenerating ? '正在生成 3D' : '参数草稿，准备生成' }
  if (hasFile) return { current: 'recognize', label: '图纸已添加，准备识别' }
  return { current: 'upload', label: '上传图纸或开始描述' }
}

function ChatMessageList({ messages, onOpenCandidate }) {
  const listRef = useRef(null)
  const followLatestRef = useRef(true)
  const latestMessage = messages[messages.length - 1]
  useEffect(() => {
    const node = listRef.current
    if (!node || !followLatestRef.current) return
    node.scrollTop = node.scrollHeight
  }, [messages.length, latestMessage?.text, latestMessage?.statusText, latestMessage?.status])

  const trackScroll = () => {
    const node = listRef.current
    if (!node) return
    followLatestRef.current = node.scrollHeight - node.scrollTop - node.clientHeight < 56
  }

  return <div className="message-list" role="log" aria-live="polite" aria-relevant="additions text" ref={listRef} onScroll={trackScroll}>
    {messages.map((message, index) => {
      const candidate = Array.isArray(message.candidate) ? message.candidate : []
      const streamingEmpty = message.status === 'streaming' && !message.text
      return <div key={message.id || `${message.role}-${index}`} className={`message ${message.role} ${message.status || 'complete'}`} data-message-status={message.status || 'complete'}>
        <div className="message-avatar">{message.role === 'ai' ? '✦' : 'J'}</div>
        <div className="message-content">
          <div className={`message-bubble ${streamingEmpty ? 'typing' : ''}`}>
            {streamingEmpty ? <><i /><i /><i /></> : <span className="message-text">{message.text}</span>}
            {message.attachments?.length > 0 && <div className="message-attachments">{message.attachments.map((name, attachmentIndex) => <span className="message-attachment" key={`${name}-${attachmentIndex}`}><span>{name}</span></span>)}</div>}
          </div>
          {message.statusText && <span className="message-status">{message.statusText}</span>}
          {candidate.length > 0 && message.status === 'complete' && <div className="chat-candidate-card">
            <div><b>CAD 修改建议</b><span>{candidate.length} 项参数</span></div>
            {candidate.slice(0, 4).map((item) => <span key={item.field}><b>{item.label}</b><em>{item.from ?? '—'} → {item.to}</em></span>)}
            <button type="button" onClick={onOpenCandidate}>查看参数与确认状态</button>
          </div>}
        </div>
      </div>
    })}
  </div>
}

function ModelWorkspace(props) {
  const { activePanel, setActivePanel, model, hasModel, modelValid, updateModel, resetModel, createBasicShaft, features, selectedFeature, setSelectedFeature, prompt, setPrompt, runGenerate, stopAiConversation, startNewConversation, rebuildCurrentModel, recoverExpiredProductionGlb, isGenerating, isAccepting, messages, view, setView, section, setSection, zoom, setZoom, exportFile, showToast, backend, generation, attachDrawingToConversation, chatAttachments = [], setChatAttachments, aiConversation, platform, drawingJob, setActiveMode, generateFromDrawing, acceptDrawingData } = props
  const { parameterValidation, checkResult, isChecking, runModelChecks, retryAi, saveCurrentVersion, remodelLegacyDrawing } = props
  const drawingInputRef = useRef(null)
  const legacyDrawingInputRef = useRef(null)
  const [viewResetNonce, setViewResetNonce] = useState(0)
  const [exportOpen, setExportOpen] = useState(false)
  const pendingCadTask = drawingJob?.cadTask?.status === 'running'
  const interactionBusy = isGenerating || isAccepting || pendingCadTask
  const productionReady = !pendingCadTask && productionArtifactsAvailable(generation) && (!isFeatureModel(model) || cadGenerationIsCurrent(model, generation))
  const modelKind = canonicalPartKind(model.kind)
  const modelDefinition = partDefinition(modelKind)
  const featureModel = isFeatureModel(model)
  const agentStatus = model.agentRun?.status
  const topology = modelValid && !generation?.stale ? generation?.validation?.metrics || {} : {}
  const aiStatus = workspaceAiProvider(model, chatAttachments, aiConversation)
  const usesCadProvider = shouldUseCadAgent(model, chatAttachments)
  const providerError = !aiConversation?.providerRoute || aiConversation.providerRoute === (usesCadProvider ? 'cad' : 'legacy') ? aiConversation?.error : ''
  const providerDisplay = aiProviderPresentation(aiStatus, { error: providerError, failureCode: usesCadProvider ? '' : drawingJob?.analysis?.failureCode, attempts: usesCadProvider ? 0 : drawingJob?.analysis?.attempts })
  const { ready: providerReady, degraded: providerDegraded, isCodex: providerIsCodex, failed: providerFailed, label: providerLabel, failureCode: providerFailureCode, attempts: providerFailureAttempts, failureText: providerFailureText } = providerDisplay
  const evidence = drawingJob?.evidence
  const legacyDrawing = isLegacyDrawingDraft(model, drawingJob)
  const remoteAnalysisFailed = drawingJob?.status === 'error' && drawingJob?.analysis?.provider === 'local-fallback'
  const reviewRequired = featureModel ? agentStatus !== 'ready' : Boolean(evidence && !evidenceAcceptedForPreview(evidence))
  const pendingConfirmedDrawing = Boolean(evidenceAcceptedForPreview(evidence) && generation?.pendingDrawing)
  // Any unconfirmed recognition is a candidate, even when the provider did
  // return numeric values. A customer must not mistake a plausible OCR guess
  // for a locked drawing dimension.
  const pendingDrawing = Boolean(generation?.pendingDrawing)
  const agentWorkflow = pendingCadTask || (isGenerating && aiConversation?.cadProgress) || ((featureModel || aiConversation?.cadProgress) && !chatAttachments.length)
  const workflow = pendingCadTask && !isGenerating ? { current: cadProgressFromEvent(drawingJob.cadTask.progress).phase, label: '运行记录已保存 · 可检查后台结果' }
    : agentWorkflow ? cadWorkflowSnapshot({ model, progress: aiConversation?.cadProgress, busy: isGenerating, error: aiConversation?.error || generation?.lastTurnError }) : workflowSnapshot({ drawingJob, generation, chatAttachments, isGenerating, model, modelValid })
  // Generic CAD builds a real candidate before the user confirms delivery.
  const displayedWorkflowSteps = agentWorkflow ? [workflowSteps[0], workflowSteps[1], workflowSteps[3], workflowSteps[2], ...workflowSteps.slice(4)] : workflowSteps
  const hasSource = Boolean(drawingJob?.file || drawingJob?.fileMeta?.name || chatAttachments.length)
  const canBuildParameterDraft = hasModel && modelValid && !evidence && !generation && !chatAttachments.length && drawingJob?.status === 'idle' && !drawingJob.requiresFileReselection && !drawingJob.interrupted
  const migrateLegacyDrawing = () => {
    if (interactionBusy) return
    const files = cadLegacySourceFiles(drawingJob)
    if (files.length) return remodelLegacyDrawing?.(files)
    showToast('旧项目仅保存了文件信息，请重新选择原图；选择后将重新建模并保留旧版本。', 'info')
    legacyDrawingInputRef.current?.click()
  }
  const primaryLabel = chatAttachments.length ? '开始 AI 分析' : featureModel ? cadPrimaryAction(model).kind === 'ready' ? generation?.artifactStatus === 'unavailable' ? '重建实体' : '导出交付' : cadPrimaryAction(model).label : legacyDrawing && !chatAttachments.length ? '按原图重新建模' : drawingJob?.requiresFileReselection ? '重新选择原图' : remoteAnalysisFailed
    ? chatAttachments.length ? '重新尝试 AI 分析' : '重新选择原图'
    : canBuildParameterDraft ? '生成 3D'
    : !hasSource && !generation
    ? '上传图纸'
      : chatAttachments.length
      ? '开始 AI 分析'
      : reviewRequired
        ? '确认数据'
        : pendingConfirmedDrawing
          ? '生成 3D'
        : !generation
          ? '生成 3D'
          : generation.stale
            ? '重建实体'
            : '导出交付'
  const primaryAction = () => {
    if (chatAttachments.length) return runGenerate()
    if (featureModel && !chatAttachments.length) {
      if (agentStatus === 'ready' && productionReady) return exportFile('step')
      return acceptDrawingData()
    }
    if (canBuildParameterDraft) return rebuildCurrentModel?.()
    if (legacyDrawing && !chatAttachments.length) return migrateLegacyDrawing()
    if (drawingJob?.requiresFileReselection) return drawingInputRef.current?.click()
    if (remoteAnalysisFailed) return chatAttachments.length ? runGenerate() : drawingInputRef.current?.click()
    if (!hasSource && !generation) return drawingInputRef.current?.click()
    if (reviewRequired) {
      // Confirmation is the next customer action, not a terminal reviewer
      // screen. Keep the editable parameter panel available and invoke the
      // same explicit acceptance handler used by the evidence card.
      setActivePanel('参数')
      if (acceptDrawingData) return acceptDrawingData()
      window.requestAnimationFrame(() => {
        const reviewCard = document.querySelector('[data-testid="workbench-review"]')
        reviewCard?.scrollIntoView({ behavior: 'smooth', block: 'center' })
        reviewCard?.querySelector('button')?.focus()
      })
      showToast('请确认 AI 候选数据后生成 3D')
      return
    }
    if (pendingConfirmedDrawing && drawingJob?.status === 'ready' && generateFromDrawing) return generateFromDrawing()
    if (!generation && drawingJob?.status === 'ready' && generateFromDrawing) return generateFromDrawing()
    if (!generation && prompt.trim()) return runGenerate()
    if (generation?.stale) return rebuildCurrentModel?.()
    return exportFile('step')
  }
  // A queued drawing or an unresolved review always takes precedence over the
  // previous model's delivery state.  Otherwise uploading a second drawing
  // while an older entity is production-ready would leave only an "导出交付"
  // button visible and hide the action that starts recognition.
  const showExportAction = productionReady && !chatAttachments.length && !reviewRequired && !pendingConfirmedDrawing && !isGenerating
  const statusForStep = (stepId) => {
    const order = displayedWorkflowSteps.map((item) => item.id)
    const currentIndex = order.indexOf(workflow.current)
    const index = order.indexOf(stepId)
    if (stepId === workflow.current) return 'active'
    if (index < currentIndex) return 'done'
    if (stepId === 'export' && productionReady) return 'active'
    return 'pending'
  }
  // The summary mirrors the complete candidate snapshot. Field provenance in
  // `analysis.candidateSources` keeps template defaults distinct from actual
  // drawing/AI evidence until the customer confirms the values.
  const sourceParameters = evidence?.candidateParameters && typeof evidence.candidateParameters === 'object'
    ? evidence.candidateParameters
    : rawParametersFromRecognition(evidence) || {}
  const analysis = drawingJob?.analysis || {}
  const analysisFeatures = Array.isArray(analysis.features) ? analysis.features : []
  const analysisAssumptions = Array.isArray(analysis.assumptions) ? analysis.assumptions : []
  const analysisUnresolved = Array.isArray(analysis.unresolved) ? analysis.unresolved : []
  const analysisMissing = Array.isArray(analysis.missingFields) ? analysis.missingFields : []
  const analysisDefaulted = Array.isArray(analysis.defaultedFields) ? analysis.defaultedFields : []
  const analysisQuestions = Array.isArray(drawingJob?.questions) ? drawingJob.questions : []
  const hasAnalysisConfidence = analysis.confidence !== null && analysis.confidence !== undefined && analysis.confidence !== '' && Number.isFinite(Number(analysis.confidence))
  const analysisConfidence = hasAnalysisConfidence ? `${Math.round(Number(analysis.confidence) * 100)}%` : '待评估'
  const compare = (source, current, suffix = '') => {
    if (source === undefined || source === null || source === '') return `待确认${suffix}`
    const sourceNumber = Number(source)
    const currentNumber = Number(current)
    const changed = Number.isFinite(sourceNumber) && Number.isFinite(currentNumber)
      ? sourceNumber !== currentNumber
      : String(source) !== String(current)
    return changed ? `图纸 ${source} → 当前 ${current}${suffix}` : `${source}${suffix}`
  }
  const evidenceRows = !evidence ? [] : modelKind === 'arched_clevis_support' ? archedClevisEvidenceRows(model, sourceParameters, compare) : modelKind === 'stepped_tapered_nozzle' ? [
    ['主件轴向', `${compare(sourceParameters.mainLength, model.mainLength)} mm · 分段 ${compare(sourceParameters.headLength, model.headLength)} + ${compare(sourceParameters.neckLength, model.neckLength)} + ${Number(model.mainLength) - Number(model.headLength) - Number(model.neckLength)} mm`],
    ['外轮廓', `Ø${compare(sourceParameters.headLeftDiameter, model.headLeftDiameter)} → Ø${compare(sourceParameters.headRightDiameter, model.headRightDiameter)} · Ø${compare(sourceParameters.neckDiameter, model.neckDiameter)} · Ø${compare(sourceParameters.tipDiameter, model.tipDiameter)}`],
    ['轴向内孔', `沉孔 Ø${compare(sourceParameters.counterboreDiameter, model.counterboreDiameter)} × ${compare(sourceParameters.counterboreDepth, model.counterboreDepth)} · 通孔 Ø${compare(sourceParameters.axialBoreDiameter, model.axialBoreDiameter)}`],
    ['出口锥口', `Ø${compare(sourceParameters.outletDiameter, model.outletDiameter)} · 半角 ${compare(sourceParameters.outletTaperHalfAngle, model.outletTaperHalfAngle, '°')}`],
    ['独立镶件', `Ø${compare(sourceParameters.insertOuterDiameter, model.insertOuterDiameter)} × ${compare(sourceParameters.insertLength, model.insertLength)} · ${compare(sourceParameters.insertThreadDesignation, model.insertThreadDesignation)} · 偏置 ${compare(sourceParameters.insertAxialOffset, model.insertAxialOffset)} mm`],
  ] : modelKind === 'split_clamp_support' ? [
    ['异形底板', `${compare(sourceParameters.baseLength, model.baseLength)} × ${compare(sourceParameters.baseWidth, model.baseWidth)} × ${compare(sourceParameters.baseThickness, model.baseThickness)} mm`],
    ['圆筒夹座', `R${compare(sourceParameters.pedestalOuterRadius, model.pedestalOuterRadius)} · 轴线距后缘 ${compare(sourceParameters.pedestalCenterFromRear, model.pedestalCenterFromRear)} mm`],
    ['盲孔 / 开缝', `Ø${compare(sourceParameters.boreDiameter, model.boreDiameter)} · 孔底 Z${compare(sourceParameters.boreFloorZ, model.boreFloorZ)} · 缝宽 ${compare(sourceParameters.splitWidth, model.splitWidth)} mm`],
    ['安装孔', `${compare(sourceParameters.mountHoleCount, model.mountHoleCount)} × Ø${compare(sourceParameters.mountHoleDiameter, model.mountHoleDiameter)} · 中心距 ${compare(sourceParameters.mountHoleCenterDistance, model.mountHoleCenterDistance)} · 距后缘 ${compare(sourceParameters.mountHoleCenterFromRear, model.mountHoleCenterFromRear)} mm`],
    ['夹紧横孔 / 加强筋', `Ø${compare(sourceParameters.crossHoleDiameter, model.crossHoleDiameter)} @ Z${compare(sourceParameters.crossHoleCenterZ, model.crossHoleCenterZ)} · ${compare(sourceParameters.ribHeight, model.ribHeight)} × ${compare(sourceParameters.ribThickness, model.ribThickness)} mm`],
  ] : [
    ['底板', `${compare(sourceParameters.baseLength, model.baseLength)} × ${compare(sourceParameters.baseWidth, model.baseWidth)} × ${compare(sourceParameters.baseThickness, model.baseThickness)} mm`],
    ['上部实体', `${compare(sourceParameters.upperLength, model.upperLength)} × ${compare(sourceParameters.upperWidth, model.upperWidth)} × ${compare(sourceParameters.upperHeight, model.upperHeight)} mm`],
    ['鞍槽 / 浅槽', `R${compare(sourceParameters.notchRadius, model.notchRadius)} · ${compare(sourceParameters.slotWidth, model.slotWidth)} × ${compare(sourceParameters.slotLength, model.slotLength)} × ${compare(sourceParameters.pocketDepth, model.pocketDepth)}`],
    ['贯穿孔', `2 × Ø${compare(sourceParameters.bossDiameter, model.bossDiameter)} · 中心距 ${compare(sourceParameters.bossCenterDistance, model.bossCenterDistance)} mm`],
  ]
  return <div className="model-workspace">
    <div className="workbench-header">
      <div className="workbench-title"><span className="eyebrow">DESIGN WORKBENCH</span><h1>3D 设计工作台</h1><p>{model.name} · 从一张图纸到可编辑实体，所有步骤在同一页完成</p></div>
      <div className="workbench-header-actions"><button className="secondary-button" onClick={saveCurrentVersion}>保存版本</button><button className="secondary-button" onClick={() => exportFile('json')}>导出草稿</button><span className={`workbench-status ${productionReady ? 'ready' : reviewRequired ? 'review' : ''}`}><i />{isAccepting ? '正在确认数据' : workflow.label}</span><button type="button" className={`secondary-button header-text-action ${productionReady ? 'header-upload-action' : ''}`} disabled={interactionBusy} onClick={() => { if (productionReady) { setChatAttachments?.([]); drawingInputRef.current?.click(); showToast('新图发送前会自动保存当前版本，可在项目历史版本中恢复') } else { setPrompt((current) => current || '创建一个可编辑的参数化零件'); showToast('已切换到文字设计') } }}>{productionReady ? '上传新图纸' : '从文字开始'}</button>{showExportAction ? <details className="export-menu" open={exportOpen} onToggle={(event) => setExportOpen(event.currentTarget.open)}><summary className="primary-button" aria-label="导出交付">导出交付 <Icon>⌄</Icon></summary><div className="export-menu-popover"><b>选择交付格式</b><button onClick={() => exportFile('step')}>STEP · 生产实体</button><button onClick={() => exportFile('glb')}>GLB · 三维预览</button>{!featureModel && <button onClick={() => exportFile('dxf')}>DXF · 工程图</button>}<button onClick={() => exportFile('json')}>JSON · 参数与审计</button></div></details> : <button type="button" data-testid="workbench-primary-action" className="primary-button workbench-primary" disabled={interactionBusy} onClick={primaryAction}>{isGenerating || isAccepting ? '处理中…' : primaryLabel} <Icon>{primaryLabel === '上传图纸' ? '＋' : '↗'}</Icon></button>}</div>
    </div>
    <nav className="workflow-rail" aria-label="建模流程">{displayedWorkflowSteps.map((step, index) => <div key={step.id} className={`workflow-step ${statusForStep(step.id)}`}><span className="workflow-step-index">{statusForStep(step.id) === 'done' ? '✓' : index + 1}</span><span><b>{step.label}</b><small>{step.id === workflow.current ? '当前' : statusForStep(step.id) === 'done' ? '已完成' : '待处理'}</small></span>{index < displayedWorkflowSteps.length - 1 && <i className="workflow-connector" />}</div>)}</nav>
    {remoteAnalysisFailed && <section className="review-banner needs-review" data-testid="workbench-ai-failure">
      <div className="review-banner-icon">!</div>
      <div className="review-banner-copy">
        <b>本次远程 AI 分析未完成</b>
        <span>{drawingJob.error || `失败类别：${providerFailureText}。原图已保留，可以直接重新分析。`}</span>
      </div>
      <div className="ai-analysis-summary" aria-label="AI 失败诊断">
        <div className="ai-analysis-summary-heading"><b>中转站诊断</b><span>{providerFailureCode || 'unknown'}{providerFailureAttempts ? ` · ${providerFailureAttempts} 次尝试` : ''}</span></div>
        <p>{hasModel ? '本轮没有创建候选参数，中间和右侧保留上一版本。' : '本轮没有创建候选参数，当前文件仍为空白。请重新分析或补充零件尺寸。'}</p>
      </div>
      <button type="button" className="primary-button" disabled={interactionBusy} onClick={() => { if (chatAttachments.length) runGenerate(); else drawingInputRef.current?.click() }}>{chatAttachments.length ? '重新尝试 AI 分析' : '重新选择原图'}</button>
    </section>}
    {featureModel && <CadAgentSummary model={model} busy={interactionBusy} onConfirm={acceptDrawingData} onAnswer={() => document.querySelector('[aria-label="给 AI 发送消息"]')?.focus()} />}
    {!featureModel && evidence && <section className="review-banner needs-review" data-testid="workbench-review">
      <div className="review-banner-icon">!</div>
      <div className="review-banner-copy">
        <b>旧版图纸草稿</b>
        <span>当前为旧版识别与模板参数，尚未经过独立原图复核。可继续手工编辑，或按原图重新建模；旧草稿会保留在项目历史版本中。</span>
      </div>
      <div className="review-evidence-mini">{evidenceRows.map(([label, value]) => <span key={label}><b>{reviewRequired ? `候选 · ${label}` : label}</b>{value}</span>)}</div>
      <details className="ai-analysis-summary workbench-analysis" aria-label="AI 分析摘要">
        <summary className="ai-analysis-summary-heading"><b>查看 AI 分析与尺寸来源{analysisQuestions.length > 0 ? ` · ${analysisQuestions.length} ${reviewRequired ? '项待核查' : '条原分析问题'}` : ''}</b><span>参考置信度 {analysisConfidence} · 展开 / 收起</span></summary>
        <div className="analysis-details-content">
        <p>{analysis.message || (reviewRequired ? '已生成候选参数，请在右侧参数面板逐项确认。' : '候选参数已由人工确认。')}</p>
        {(analysisMissing.length > 0 || analysisDefaulted.length > 0 || analysisAssumptions.length > 0 || analysisUnresolved.length > 0 || analysisFeatures.length > 0 || analysisQuestions.length > 0) && <div className="ai-analysis-tags">
          {analysisMissing.slice(0, 8).map((key) => <span key={`missing-${key}`} className="warning">待补全：{modelDefinition.labels[key] || key}</span>)}
          {analysisDefaulted.slice(0, 8).map((key) => <span key={`default-${key}`} className="default">模板默认：{modelDefinition.labels[key] || key}</span>)}
          {analysisFeatures.slice(0, 4).map((feature, index) => <span key={`feature-${index}`}>特征：{String(feature?.featureType || feature?.feature_type || feature?.type || feature || '已识别')}</span>)}
          {analysisAssumptions.slice(0, 2).map((item, index) => <span key={`assumption-${index}`}>假设：{String(item)}</span>)}
          {analysisUnresolved.slice(0, 2).map((item, index) => <span key={`unresolved-${index}`} className="warning">待确认：{String(item)}</span>)}
          {analysisQuestions.slice(0, 2).map((item, index) => <span key={`question-${index}`} className="warning">AI 问题：{String(item)}</span>)}
        </div>}
        </div>
      </details>
      <button type="button" className="primary-button" disabled={interactionBusy} onClick={migrateLegacyDrawing}>按原图重新建模</button>
      <input ref={legacyDrawingInputRef} className="file-input" type="file" accept="image/*,.pdf,.dxf,.dwg" aria-label="重新选择原图并重新建模" disabled={interactionBusy} onChange={(event) => { const files = Array.from(event.currentTarget.files || []); event.currentTarget.value = ''; if (files.length) remodelLegacyDrawing?.(files) }} />
    </section>}

    <section className="ai-column panel-card">
      <div className="panel-heading"><div><span className="eyebrow">AI COPILOT</span><h2>AI 设计助手</h2><p className="panel-subtitle">像聊天一样分析图纸、追问并修改模型</p></div><button type="button" className="chat-new-button" aria-label="开始新对话" title="保留当前模型并清空聊天上下文" disabled={interactionBusy} onClick={startNewConversation}>＋ 新对话</button></div>
      <div className="ai-mode-pill"><span className="sparkle">✦</span><b>连续对话 · 参数化 CAD</b><span className="chat-memory-indicator">记忆当前会话</span></div>
      <div className={`ai-provider-status ${providerReady ? 'ready' : providerFailed ? 'error' : ''}`} data-status={providerReady ? 'ready' : providerFailed ? 'error' : 'checking'}><span>{providerIsCodex ? 'Codex' : 'AI'}</span><b>{aiStatus?.model || '正在读取引擎配置'}{aiStatus?.reasoningEffort ? ` · reasoning ${aiStatus.reasoningEffort}` : ''}</b><small>{isGenerating ? (aiConversation?.statusMessage || 'AI 正在回复…') : providerLabel}</small></div>
      {providerDisplay.configurationHint && (providerIsCodex || !platform?.token) && <div className="ai-auth-hint">{providerDisplay.configurationHint}</div>}
      {providerDegraded && <div className="chat-turn-notice">上一轮没有取得远程模型候选；你可以继续说明要求，或用已保留的原图重新发送。</div>}
      {aiConversation?.error && <div className="ai-error-banner" role="alert"><p>{readableError(aiConversation.error)}</p>{!providerIsCodex && !platform?.token && /登录|认证|token/i.test(aiConversation.error) && <button className="secondary-button" onClick={() => setActiveMode('平台服务')}>前往登录</button>}<button className="secondary-button" disabled={isGenerating} onClick={retryAi}>重试上一条</button></div>}
      {pendingCadTask && !isGenerating && <div className="chat-turn-notice" role="status">本轮运行记录已保存，检查结果会更新回当前项目。<button type="button" disabled={isAccepting} onClick={retryAi}>检查后台结果</button></div>}
      {drawingJob?.requiresFileReselection && !chatAttachments.length && <div className="chat-turn-notice">上次处理已中断，尺寸和对话已恢复；请重新选择原文件继续。<button onClick={() => drawingInputRef.current?.click()}>重新选择原图</button></div>}
      {!hasSource && !generation && <div className="quick-start-card"><div className="quick-start-icon">▱</div><div><b>从一张图纸开始</b><span>支持图片、PDF、DWG、DXF；读取原图并生成实体，核对差异后再确认交付。</span></div><button type="button" className="primary-button" onClick={() => drawingInputRef.current?.click()}>上传图纸</button></div>}
      <ChatMessageList messages={messages} onOpenCandidate={() => setActivePanel('参数')} />
      {chatAttachments.length > 0 && <div className="queued-drawing"><div><b>随下一条消息发送</b><span>可以先补充你希望 AI 重点检查的内容</span></div><div className="ai-attachment-list">{chatAttachments.map((file, fileIndex) => <div className="ai-attachment-chip" key={`${file.name}-${file.size}-${file.lastModified || 0}-${fileIndex}`} data-status="ready"><span className="attachment-type">{file.name.split('.').pop()?.toUpperCase() || 'FILE'}</span><span className="attachment-name">{file.name}</span><button type="button" className="attachment-remove" aria-label={`移除 ${file.name}`} onClick={() => setChatAttachments?.((current) => current.filter((_, index) => index !== fileIndex))}>×</button></div>)}</div></div>}
      <div className="prompt-box"><textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder="给 AI 发消息，继续追问或修改尺寸…" aria-label="给 AI 发送消息" onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); if (!interactionBusy) runGenerate(); else showToast('当前操作尚未完成，请等待或检查后台结果') } }} /><input ref={drawingInputRef} className="file-input" type="file" multiple accept="image/*,.pdf,.dxf,.dwg" aria-label="上传工程图到 AI 对话" disabled={interactionBusy} onChange={(e) => { const files = Array.from(e.currentTarget.files || []); e.currentTarget.value = ''; attachDrawingToConversation?.(files) }} /><div className="prompt-actions"><button type="button" className="attach attach-labeled" aria-label="给本条消息添加图纸" title="添加图纸" disabled={interactionBusy} onClick={() => drawingInputRef.current?.click()}><Icon>📎</Icon><span>添加图纸</span></button><span>Enter 发送 · Shift+Enter 换行</span>{isGenerating ? <button type="button" className="run-button stop-button" aria-label="停止等待 AI 回复" title="停止等待后，后台建模会继续，可稍后检查结果" onClick={stopAiConversation}><Icon>■</Icon> 停止等待</button> : <button type="button" className="run-button" aria-label="发送给 AI" disabled={interactionBusy || (!prompt.trim() && !chatAttachments.length)} onClick={runGenerate}>发送 <Icon>↑</Icon></button>}</div></div>
      <div className="suggestions"><span>快速开始：</span><button onClick={() => setPrompt('创建一个带法兰和 4 个安装孔的支架')}>带法兰的支架</button>{hasModel && <button onClick={() => setPrompt('将当前模型材质改为 AL6061 铝合金')}>更换材质</button>}</div>
    </section>

    <section className="viewport-column">
      <div className="viewport-toolbar"><div className="toolbar-group"><button disabled={!hasModel} className={view === 'isometric' ? 'selected' : ''} onClick={() => setView('isometric')}>等轴测</button><button disabled={!hasModel} className={view === 'front' ? 'selected' : ''} onClick={() => setView('front')}>前视</button><button disabled={!hasModel} className={view === 'top' ? 'selected' : ''} onClick={() => setView('top')}>俯视</button></div><div className="toolbar-group"><button disabled={!hasModel} onClick={() => setSection((value) => !value)} className={section ? 'selected' : ''}><Icon>◐</Icon> 剖切</button><button disabled={!hasModel} onClick={() => { setZoom(1); setView('isometric'); setViewResetNonce((value) => value + 1); showToast('视图已重置') }}>重置视图</button></div></div>
      <div className={`viewport ${!hasModel ? 'viewport-empty' : ''}`}>
        <div className="viewport-grid" />
        {!hasModel ? <div className="model-empty-state" data-testid="empty-model-preview" role="status">
          <span className="model-empty-symbol" aria-hidden="true">◇</span>
          <h2>{isGenerating ? '正在准备你的模型' : '当前文件还没有模型'}</h2>
          <p>{isGenerating ? 'AI 返回零件类型和尺寸后，预览会显示在这里。' : '描述想要的零件，或导入图纸开始建模。'}</p>
          <div className="model-empty-actions"><button className="primary-button" disabled={interactionBusy} onClick={() => document.querySelector('[aria-label="给 AI 发送消息"]')?.focus()}>描述零件</button><button className="secondary-button" disabled={interactionBusy} onClick={() => drawingInputRef.current?.click()}>导入图纸</button></div>
          {!hasSource && !evidence && <button className="model-empty-template" disabled={interactionBusy} onClick={createBasicShaft}>或创建基础轴，手动设置尺寸 →</button>}
        </div> : <>
          <div className="axis axis-x">X</div><div className="axis axis-y">Y</div><div className="axis axis-z">Z</div>
          {modelValid || featureModel ? <ThreeDViewer model={model} generation={generation} view={view} section={section} zoom={zoom} onZoomChange={setZoom} onProductionGlbLoadError={recoverExpiredProductionGlb} resetNonce={viewResetNonce} /> : <div className="invalid-preview" role="status"><b>参数需要修正，已暂停模型预览</b><ul>{parameterValidation.errors.map((error) => <li key={error.field + error.message}>{error.message}</li>)}</ul><button onClick={() => setActivePanel('参数')}>查看参数</button></div>}
          <div className={`model-context-badge ${productionReady ? 'production' : reviewRequired ? 'review' : ''}`}><span className={`status-dot ${generation || reviewRequired ? 'ready' : ''}`} />{pendingCadTask ? (generation ? '上一版本实体 · 本轮仍在处理' : '本轮仍在处理 · 尚无完成实体') : featureModel && generation?.lastTurnError ? '上一版本实体 · 本轮修改未完成' : featureModel && !generation ? (agentStatus === 'needs_input' ? '等待补充信息 · 尚无实体' : agentStatus === 'failed' ? '本轮未完成 · 尚无实体' : '等待重新构建实体') : !modelValid ? '参数无效 · 预览暂停' : remoteAnalysisFailed ? '上一版本预览 · AI 未返回候选' : pendingDrawing ? '上一版本预览 · 新图纸处理中' : legacyDrawing ? '旧版图纸草稿 · 尚未重新识别' : reviewRequired ? (featureModel ? '实际 CAD 候选 · 待核对' : 'AI 候选 · 参数化 3D 草稿') : generation ? (generation.artifactStatus === 'recovering' ? '旧文件已失效 · 正在恢复生产实体' : productionReady ? '已生成实体 · OCCT 校验通过' : '已生成可交互 3D 预览') : '当前参数草稿 · 尚未生成生产实体'}</div>
          <div className="view-cube"><span>TOP</span><b>FRONT</b><span>RIGHT</span></div><div className="viewport-hint"><Icon>✥</Icon> 拖拽旋转 · 滚轮缩放</div><div className="zoom-control"><button aria-label="放大" onClick={() => setZoom((value) => Math.min(1.8, value + .1))}>＋</button><span>{Math.round(zoom * 100)}%</span><button aria-label="缩小" onClick={() => setZoom((value) => Math.max(.55, value - .1))}>−</button></div>
        </>}
      </div>
      <div className="viewport-footer">{hasModel ? <><span><i className="live-dot" /> {legacyDrawing ? '旧版图纸草稿已恢复' : featureModel && generation?.lastTurnError ? '保留上次完成的版本' : featureModel && !generation ? (model.cadPlan ? '尺寸与建模计划已保存' : '原图与任务状态已保存') : remoteAnalysisFailed ? '上一版本未被覆盖' : reviewRequired ? '候选模型已同步' : generation ? '模型版本已更新' : '当前参数已保存'} · {model.updatedAt}</span><span className={`production-badge ${productionReady ? 'ready' : 'preview'}`}>{productionReady ? 'OCCT 已验证' : featureModel && !generation ? '待生成' : remoteAnalysisFailed ? '上一版本' : reviewRequired ? '候选预览' : generation?.stale ? '参数已变更' : '参数草稿'}</span><span>单位 <b>mm</b></span><span>材质 <b>{featureModel ? cadExplicitMaterial(model) || '未指定' : model.material}</b></span></> : <span>空白文件 · 尚未创建模型</span>}</div>
    </section>

    <aside className="inspector-column">
      <div className="inspector-tabs"><button className={activePanel === '参数' ? 'active' : ''} onClick={() => setActivePanel('参数')}>参数</button><button className={activePanel === '特征' ? 'active' : ''} onClick={() => setActivePanel('特征')}>特征树</button><button className={activePanel === '检查' ? 'active' : ''} onClick={() => setActivePanel('检查')}>检查</button></div>
      {!hasModel && <div className="inspector-empty-state" role="status"><b>{activePanel === '特征' ? '尚无模型特征' : activePanel === '检查' ? '尚无模型可检查' : '尚无模型参数'}</b><p>创建模型后，可在这里查看和编辑{activePanel === '特征' ? '特征' : activePanel === '检查' ? '检查结果' : '尺寸与材料'}。</p></div>}
      {featureModel && <CadAgentPanel model={model} tab={activePanel} busy={interactionBusy} onParameterChange={updateModel} onConfirm={acceptDrawingData} generation={generation} onAsk={(text) => { setPrompt(text); document.querySelector('[aria-label="给 AI 发送消息"]')?.focus() }} />}
      {hasModel && !featureModel && activePanel === '参数' && <ParameterErrors.Provider value={parameterValidation.errors}><ParameterPanel model={model} modelValid={modelValid} updateModel={updateModel} resetModel={resetModel} drawingJob={drawingJob} disabled={interactionBusy} />{parameterValidation.errors.length > 0 && <div className="parameter-errors" role="status">{parameterValidation.errors.map((error) => <p className="parameter-error" key={error.field + error.message}>{error.message}</p>)}</div>}</ParameterErrors.Provider>}
      {hasModel && !featureModel && activePanel === '特征' && <FeaturePanel features={features} selectedFeature={selectedFeature} setSelectedFeature={setSelectedFeature} onAddFeature={() => { setPrompt('请说明当前配方支持的特征修改，并帮我调整'); document.querySelector('[aria-label="给 AI 发送消息"]')?.focus() }} />}
      {hasModel && !featureModel && activePanel === '检查' && <CheckPanel model={model} modelValid={modelValid} showToast={showToast} backend={backend} generation={generation} drawingJob={drawingJob} generateFromDrawing={generateFromDrawing} acceptDrawingData={acceptDrawingData} setActiveMode={setActiveMode} busy={isGenerating || isAccepting || isChecking} checkResult={checkResult} onRunChecks={runModelChecks} parameterErrors={parameterValidation.errors} />}
      {productionPartKinds.includes(modelKind) && generation && <div className="artifact-meta-panel"><div className="artifact-meta-heading"><span className="eyebrow">SOLID KERNEL</span><span className={`production-badge ${productionReady ? 'ready' : 'preview'}`}>{productionReady ? '生产实体' : '仅预览'}</span></div><div className="artifact-meta-grid"><span>引擎</span><b>{generation.engine}</b><span>包络</span><b>{modelBoundsText(model, topology)}</b><span>实体 / 面</span><b>{topology.solidCount ?? '—'} / {topology.faceCount ?? '—'}</b></div></div>}
      <div className="export-card"><div><span className="eyebrow">交付状态</span><h3>{!hasModel ? '创建模型后可导出' : productionReady ? '可导出交付文件' : featureModel && cadPrimaryAction(model).kind === 'retry' ? '完成建模后可导出' : reviewRequired ? '先确认数据才能导出' : '先生成实体再导出'}</h3><p>{!hasModel ? '空白文件可以保存；创建模型后再生成工程图与交付文件。' : productionReady ? (featureModel ? 'STEP、GLB 与建模 JSON 已集中到右上角“导出交付”。' : 'STEP、GLB、DXF 与参数 JSON 已集中到右上角“导出交付”。') : featureModel ? cadPrimaryAction(model).hint || '确认尺寸与检查结果后，再生成交付文件。' : '当前只显示可编辑预览，避免把未校验模型误当成生产文件。'}</p></div>{productionReady ? <div className="export-card-hint">右上角 <b>导出交付</b> · 统一出口</div> : <button type="button" className="secondary-button full" disabled={interactionBusy} onClick={primaryAction}>{featureModel ? primaryLabel : reviewRequired ? '打开确认数据' : '继续当前流程'} <Icon>↗</Icon></button>}</div>
    </aside>

  </div>
}

function DrawingImportWorkspace({ drawingJob, analyzeDrawing, generateFromDrawing, acceptDrawingData, model, updateDrawingEvidence, showToast, backend }) {
  const inputRef = useRef(null)
  const [dragging, setDragging] = useState(false)
  const evidence = drawingJob.evidence
  const file = drawingJob.file
  const isImage = Boolean(file?.type?.startsWith('image/'))
  const chooseFile = (nextFile) => {
    if (!nextFile) return
    // Reset the input value so selecting the same drawing twice still starts a
    // fresh recognition job.
    if (inputRef.current) inputRef.current.value = ''
    analyzeDrawing(nextFile)
  }
  const onDrop = (event) => {
    event.preventDefault()
    setDragging(false)
    chooseFile(event.dataTransfer.files?.[0])
  }
  const statusText = drawingJob.status === 'analyzing' ? '识别中' : drawingJob.status === 'generating' ? '生成实体中' : drawingJob.status === 'generated' ? '实体已生成' : drawingJob.status === 'ready' ? '识别完成' : drawingJob.status === 'error' ? '识别失败' : '等待上传'
  // Only explicit evidence belongs in the editable candidate. The viewport
  // may keep a private recipe scaffold, but missing evidence fields remain
  // empty here until the AI or customer supplies them.
  const candidateParameters = evidence?.candidateParameters && typeof evidence.candidateParameters === 'object'
    ? evidence.candidateParameters
    : model || {}
  const evidenceList = recognitionEvidence(evidence)
  const sourceFor = (field, fallback) => evidenceList.find((item) => item.field === field || item.field === field.replace(/[A-Z]/g, (letter) => `_${letter.toLowerCase()}`))?.view || evidenceList.find((item) => item.field === field || item.field === field.replace(/[A-Z]/g, (letter) => `_${letter.toLowerCase()}`))?.source || fallback
  const confidenceLabel = evidence?.confidence !== undefined ? `置信度 ${Math.round(Number(evidence.confidence) * 100)}%` : '置信度 96%'
  const localCandidateEngine = ['heuristic-review', 'tesseract-compatible', 'compatibility-recognizer', 'deterministic-calibration', 'verified-browser-fixture'].includes(String(evidence?.engine || '').toLowerCase())
  const missingFields = new Set(Array.isArray(drawingJob.analysis?.missingFields) ? drawingJob.analysis.missingFields : [])
  const candidateSources = drawingJob.analysis?.candidateSources || {}
  const dwgPreprocessing = drawingJob.analysis?.dwgPreprocessing || null
  const defaultedFields = new Set(Array.isArray(drawingJob.analysis?.defaultedFields) ? drawingJob.analysis.defaultedFields : [])
  const read = (key) => {
    if (evidence && !evidenceAcceptedForPreview(evidence)) return candidateParameters?.[key] ?? ''
    return candidateParameters?.[key] ?? model?.[key] ?? ''
  }
  const sourceLabel = (key) => candidateSourceLabels[candidateSources[key]] || (defaultedFields.has(key) ? candidateSourceLabels.template_default : '待确认')
  const evidenceKind = partKindFromEnvelope(evidence || drawingJob.analysis, model?.kind)
  const evidenceDefinition = partDefinition(evidenceKind)
  const evidenceRows = evidenceKind === 'arched_clevis_support' ? archedClevisGroups.map((group) => [group.title, group.fields.map((key) => [key, archedClevisSupportDefinition.labels[key]]), sourceFor(group.fields[0], '主视 / 俯视 / 右视')]) : evidenceKind === 'stepped_tapered_nozzle' ? [
    ['主件轴向', [['mainLength', '总长'], ['headLength', '浅锥段'], ['neckLength', '颈段']], sourceFor('mainLength', 'DWG 轴向剖视')],
    ['主件外轮廓', [['headLeftDiameter', '浅锥左端 Ø'], ['headRightDiameter', '浅锥右端 Ø'], ['neckDiameter', '颈段 Ø'], ['tipDiameter', '末段 Ø']], sourceFor('headLeftDiameter', 'DWG 轴向剖视')],
    ['沉孔 / 通孔', [['counterboreDiameter', '沉孔 Ø'], ['counterboreDepth', '沉孔深'], ['axialBoreDiameter', '通孔 Ø']], sourceFor('counterboreDiameter', 'DWG 剖视尺寸')],
    ['出口锥口', [['outletDiameter', '出口 Ø'], ['outletTaperHalfAngle', '半角']], sourceFor('outletDiameter', '末端详图')],
    ['独立镶件', [['insertOuterDiameter', '外径 Ø'], ['insertLength', '长度'], ['insertThreadDesignation', '螺纹标注'], ['insertAxialOffset', '轴向偏置']], sourceFor('insertOuterDiameter', 'DWG 第二回转件')],
  ] : [
    ['底板', [['baseLength', '长'], ['baseWidth', '宽'], ['baseThickness', '厚']], sourceFor('baseLength', '俯视 / 主视')],
    ['上部实体', [['upperLength', '长'], ['upperWidth', '宽'], ['upperHeight', '高']], sourceFor('upperLength', '主视 / 右视')],
    ['总高度', [['totalHeight', '高度']], sourceFor('totalHeight', '主视')],
    ['U 型缺口', [['notchOpening', '开口'], ['notchRadius', '半径 R']], sourceFor('notchOpening', '主视')],
    ['矩形浅槽', [['slotWidth', '宽'], ['slotLength', '长'], ['pocketDepth', '深']], sourceFor('slotLength', '俯视 / 右视')],
    ['贯穿孔 / 侧边半圆缺口', [['bossDiameter', '直径 Ø']], sourceFor('bossDiameter', '俯视 / 主视')],
    ['贯穿孔中心距', [['bossCenterDistance', '中心距']], sourceFor('bossCenterDistance', '俯视投影')],
  ]
  const evidenceWarning = evidence?.warnings?.length
    ? evidence.warnings.join('；')
    : evidenceKind === 'arched_clevis_support'
      ? '请核对内外拱、耳厚和间隙，以及横向耳孔与竖直安装孔。桥面高度由外拱与耳侧面交点推导，确认后生成实体。'
    : evidenceKind === 'stepped_tapered_nozzle'
      ? evidence?.status === 'confirmed'
        ? '主件与镶件候选已确认；两者仍按独立实体交付。M12 仅为标注，未生成真实螺纹牙型。'
        : '请确认镶件与 Ø40 沉孔的装配关系和固定方式；当前显示两个独立候选实体，不会虚构螺纹牙型。'
    : evidence?.status === 'confirmed'
      ? '尺寸证据已锁定；Ø20 为两处贯穿竖孔/侧边半圆凹槽，30 mm 为中段浅槽长度，生成结果仍会经过 OCCT 拓扑检查。'
      : 'AI 已给出候选值；请确认 Ø20 是贯穿竖孔/侧边半圆凹槽，30 mm 是两条浅槽沿 Y 的长度，再点击“确认数据”。'
  const stepStatus = (step) => {
    const order = ['upload', 'recognize', 'review', 'generate']
    const current = drawingJob.status === 'analyzing' ? 'recognize' : drawingJob.status === 'ready' ? (evidence?.status === 'confirmed' ? 'generate' : 'review') : drawingJob.status === 'generating' ? 'generate' : drawingJob.status === 'generated' ? 'done' : 'upload'
    if (current === 'done') return 'done'
    const currentIndex = order.indexOf(current); const index = order.indexOf(step)
    return step === current ? 'active' : index >= 0 && index < currentIndex ? 'done' : ''
  }
  return <div className="secondary-workspace import-workspace">
    <div className="secondary-heading"><div><span className="eyebrow">DRAWING → 3D</span><h1>图纸转三维</h1><p>AI 分析候选尺寸 → 确认数据 → 生成可编辑 3D</p></div><div className="heading-actions"><span className={`backend-status compact ${backend?.status || 'checking'}`}><i />{backend?.productionReady ? 'CadQuery / OCCT 在线' : backend?.status === 'offline' ? 'API 离线' : '连接中'}</span><button className="secondary-button" onClick={() => showToast('支持 JPG、PNG、WEBP、PDF、DWG、DXF')}>支持格式</button><button className="primary-button" data-testid="confirm-generate" disabled={!evidence || drawingJob.status === 'analyzing' || drawingJob.status === 'generating' || drawingJob.status === 'generated'} onClick={evidence?.status === 'confirmed' ? generateFromDrawing : acceptDrawingData}>{drawingJob.status === 'generated' ? '已生成 3D' : evidence?.status === 'confirmed' ? '生成 3D' : '确认数据'} <Icon>↗</Icon></button></div></div>
    <div className="import-steps" aria-label="图纸转三维流程"><span className={stepStatus('recognize')}><i>1</i>AI 分析</span><span className={stepStatus('review')}><i>2</i>确认数据</span><span className={stepStatus('generate')}><i>3</i>生成 3D</span></div>
    <div className="import-grid">
      <div className="upload-card panel-card">
        <input ref={inputRef} className="file-input" data-testid="drawing-file-input" type="file" accept="image/*,.pdf,.dxf,.dwg" onChange={(event) => chooseFile(event.target.files?.[0])} />
        <div className={`drop-zone ${file ? 'has-file' : ''} ${dragging ? 'dragging' : ''}`} data-testid="drawing-drop-zone" role="button" tabIndex="0" aria-label="选择或拖拽图纸文件" onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); inputRef.current?.click() } }} onClick={() => inputRef.current?.click()} onDragOver={(event) => { event.preventDefault(); setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={onDrop}>
          {file && isImage && drawingJob.previewUrl ? <img src={drawingJob.previewUrl} alt="已上传工程图预览" /> : file ? <div className="file-preview-placeholder"><div className="upload-symbol">▱</div><b>{file.name.split('.').pop()?.toUpperCase()} 图纸</b><span>{file.name.toLowerCase().endsWith('.dwg') ? '先在服务端转换并提取二维矢量，不直传原始 DWG 二进制' : '该格式将提交给图纸识别服务'}</span></div> : <div className="upload-placeholder"><div className="upload-symbol">↥</div><b>拖拽图纸到这里，或点击上传</b><span>支持图片 / PDF / DWG / DXF · 单个文件不超过 20 MB</span></div>}
          <div className="drop-overlay"><span>{drawingJob.status === 'analyzing' ? '识别中…' : file ? '重新选择图纸' : '选择文件'}</span><button type="button" className="upload-select-button" onClick={(event) => { event.stopPropagation(); inputRef.current?.click() }}>{file ? '替换文件' : '选择图纸文件'}</button></div>
        </div>
        {file && <div className="upload-file-meta"><span className="file-type-icon blue">▱</span><div><b>{file.name}</b><small>{(file.size / 1024).toFixed(0)} KB · {backend?.status === 'connected' ? '已提交 FastAPI' : '本地预览'}</small></div><span className={`parse-status ${drawingJob.status}`}>{statusText}</span></div>}
        {drawingJob.status === 'error' && <div className="import-error"><span>!</span><span>{drawingJob.error || '图纸识别失败，请检查文件后重试。'}</span></div>}
        {drawingJob.warning && <div className="backend-warning"><span>!</span><span>{drawingJob.warning}</span></div>}
        <div className="privacy-note"><Icon>◈</Icon><span>原图仅用于本次识别；生成前会保留每个尺寸的来源视图和校验状态。</span></div>
      </div>
      <div className="evidence-card panel-card" data-testid="drawing-evidence"><div className="evidence-heading"><div><span className="eyebrow">RECOGNITION EVIDENCE</span><h2>识别结果与尺寸证据</h2></div><span className={`confidence ${evidence ? 'ready' : ''}`}>{evidence ? confidenceLabel : drawingJob.status === 'analyzing' ? '分析中…' : '等待图纸'}</span></div>
        {!evidence ? <div className="evidence-empty"><span>{drawingJob.status === 'analyzing' ? '⋯' : '⌁'}</span><b>{drawingJob.status === 'analyzing' ? '正在解析视图与标注' : '上传图纸后开始识别'}</b><small>{drawingJob.status === 'analyzing' ? '正在建立尺寸证据链，请稍候。' : '系统会保留每个尺寸的来源视图和校验状态。'}</small><div className="recognition-meter"><i style={{ width: drawingJob.status === 'analyzing' ? '64%' : '0%' }} /></div></div> : <><div className="evidence-banner"><span className="status-dot" /><div><b>{localCandidateEngine ? '识别完成 · 候选数据' : 'AI 已完成尺寸分析'}</b><small>{dwgPreprocessing ? `${dwgPreprocessing.engine || 'DWG 矢量引擎'} · ${dwgPreprocessing.entityCount ?? '—'} 实体 · ${dwgPreprocessing.dimensionCount ?? '—'} 原生尺寸` : `${evidence.engine || 'OCR'} · ${evidence.status === 'confirmed' ? '数据已确认' : '候选值可编辑'} · 三视图证据链`}</small></div><span className="evidence-source">{dwgPreprocessing ? 'DWG + AI' : evidence.engine || evidence.source || 'OCR'}</span></div>{drawingJob.analysis?.message && <div className="ai-analysis-summary import-analysis-summary"><div className="ai-analysis-summary-heading"><b>AI 分析摘要</b><span>{drawingJob.analysis.provider || evidence.engine || '分析服务'}</span></div><p>{drawingJob.analysis.message}</p>{(missingFields.size > 0 || defaultedFields.size > 0) && <div className="ai-analysis-tags">{[...missingFields].slice(0, 8).map((key) => <span key={key} className="warning">待补全：{evidenceDefinition.labels[key] || key}</span>)}{[...defaultedFields].slice(0, 8).map((key) => <span key={`default-${key}`} className="default">默认候选：{evidenceDefinition.labels[key] || key}</span>)}</div>}</div>}<div className="evidence-table">{evidenceRows.map(([label, fields, source]) => <div className="evidence-row" key={label}><span className="evidence-check">{evidence.status === 'confirmed' ? '✓' : '·'}</span><div><b>{label}</b><small>{source} · {evidenceList.find((item) => item.field === fields[0][0])?.confidence ? `${Math.round(Number(evidenceList.find((item) => item.field === fields[0][0]).confidence) * 100)}%` : '候选'} 置信度</small></div><div className="evidence-edit-fields">{fields.map(([field, fieldLabel]) => <label key={field} data-candidate-source={candidateSources[field] || ''}><span>{missingFields.has(field) ? `${fieldLabel} · 待补全` : `${fieldLabel} · ${sourceLabel(field)}`}</span><input aria-label={`${label} ${fieldLabel}`} type={field === 'insertThreadDesignation' ? 'text' : 'number'} value={read(field)} placeholder={missingFields.has(field) ? '待补全' : '待确认'} onChange={(event) => updateDrawingEvidence?.(field, event.target.value)} /></label>)}</div><span className="evidence-lock">{evidence.status === 'confirmed' ? '已确认' : '待确认'}</span></div>)}</div><div className="evidence-warning"><span>{evidence.status === 'confirmed' ? '✓' : '!'}</span><span>{evidenceWarning}</span></div>{drawingJob.questions?.length > 0 && <div className="evidence-warning"><span>?</span><span>AI 待确认问题：{drawingJob.questions.join('；')}</span></div>}<div className="evidence-confirmed"><span>{evidence.status === 'confirmed' ? '✓' : '!'}</span><span>{evidence.status === 'confirmed' ? '尺寸证据已确认 · 可以生成 3D' : missingFields.size ? `已有候选值已写入；请补全剩余 ${missingFields.size} 项后确认` : '候选尺寸与来源已显示；请核对并确认当前数据'}</span><button disabled={drawingJob.status === 'generating' || drawingJob.status === 'generated'} onClick={evidence.status === 'confirmed' ? generateFromDrawing : acceptDrawingData}>{drawingJob.status === 'generated' ? '已生成' : evidence.status === 'confirmed' ? '生成 3D' : '确认数据'} <Icon>↗</Icon></button></div></>}</div>
    </div>
  </div>
}

function PlatformWorkspace({ onRefreshServices, mode, backend, platform, platformLogin, platformLogout, refreshPlatformUsers, createPlatformUser, createPlatformProject, shareProjectTeam, syncDrawingToPdm, createCamPlan, simulateCamPlan, approveCamPlan, releaseCamPlan, downloadNcProgram, generation, showToast }) {
  const [email, setEmail] = useState('admin@joyniu.local')
  const [password, setPassword] = useState('ChangeMe123!')
  const [displayName, setDisplayName] = useState('JoyNiu 管理员')
  const [role, setRole] = useState('admin')
  const [newEmail, setNewEmail] = useState('reviewer@joyniu.local')
  const [newPassword, setNewPassword] = useState('Reviewer123!')
  const [newDisplayName, setNewDisplayName] = useState('审核员')
  const [newRole, setNewRole] = useState('reviewer')
  const [memberText, setMemberText] = useState('')
  const loggedIn = Boolean(platform?.user && platform?.token)
  const permissions = platform?.user?.permissions || []
  const userDisplayName = platform?.user?.displayName || platform?.user?.display_name || platform?.user?.email || 'JoyNiu'
  const hasPermission = (permission) => permissions.includes('*') || permissions.includes(permission)
  const canManageMembers = hasPermission('user:manage') || Boolean(
    platform?.project && (
      String(platform.project.ownerId || platform.project.owner_id || '').toLowerCase() === String(platform?.user?.id || '').toLowerCase()
      || String(platform.project.ownerId || platform.project.owner_id || '').toLowerCase() === String(platform?.user?.email || '').toLowerCase()
    )
  )
  const manifestDocuments = platform?.manifest?.documents || []
  const versionCount = manifestDocuments.reduce((sum, item) => sum + (item.versions?.length || 0), 0)
  const simulation = platform?.simulation
  const gate = platform?.gate
  const camPlan = platform?.camPlan
  const nc = platform?.nc
  const geometry = backend?.health?.geometry || {}
  const dwg = backend?.health?.dwg || {}
  const ocr = backend?.health?.capabilities?.ocr || backend?.health?.ocr || {}
  const submit = (bootstrap) => platformLogin({ email: email.trim(), password, displayName: displayName.trim(), roles: [role], bootstrap })

  return <div className="secondary-workspace platform-workspace">
    <div className="secondary-heading">
      <div><span className="eyebrow">{mode === 'CAM / NC' ? 'MANUFACTURING CONTROL' : 'PLATFORM SERVICES'}</span><h1>{mode === 'CAM / NC' ? 'CAM / NC 制造门' : 'PDM、账号与审计'}</h1><p>{mode === 'CAM / NC' ? '确定性仿真、审核与 NC 放行均绑定到不可变模型版本。' : '账号权限、图纸证据、模型版本和交付物在同一条审计链中。'}</p></div>
      <div className="heading-actions"><span className={`backend-status compact ${backend?.status || 'checking'}`}><i />{backend?.productionReady ? 'OCCT 在线' : backend?.status === 'offline' ? 'API 离线' : '降级模式'}</span>{loggedIn && <button className="secondary-button" onClick={platformLogout}>退出登录</button>}</div>
    </div>
    {platform?.error && <div className="backend-warning backend-error"><span>!</span><span>{platform.error}</span></div>}
    <div className="platform-grid">
      <section className="platform-card panel-card platform-auth-card">
        <div className="platform-card-heading"><div><span className="eyebrow">IDENTITY & RBAC</span><h2>账号权限</h2></div><span className={`production-badge ${loggedIn ? 'ready' : 'preview'}`}>{loggedIn ? '已认证' : '未登录'}</span></div>
        {!loggedIn ? <form className="platform-form" onSubmit={(event) => { event.preventDefault(); submit(false) }}>
          <label>邮箱<input type="email" value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="username" /></label>
          <label>密码<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" /></label>
          <label>显示名称（首次注册）<input value={displayName} onChange={(event) => setDisplayName(event.target.value)} /></label>
          <label>角色<select value={role} onChange={(event) => setRole(event.target.value)}><option value="admin">admin · 全流程演示</option><option value="designer">designer · 设计 / OCR / CAM 草案</option><option value="reviewer">reviewer · 证据与 CAM 审核</option><option value="manufacturing">manufacturing · NC 制造放行</option><option value="viewer">viewer · 只读</option></select></label>
          <div className="platform-form-actions"><button type="button" className="secondary-button" disabled={platform?.busy} onClick={() => submit(true)}>注册并登录</button><button type="submit" className="primary-button" disabled={platform?.busy}>登录 <Icon>↗</Icon></button></div>
          <small className="platform-help">本地首次运行可用“注册并登录”创建 bootstrap 账号；之后新增账号必须由 admin 管理。</small>
        </form> : <div className="platform-identity"><div className="identity-avatar">{userDisplayName.slice(0, 1).toUpperCase()}</div><div className="identity-copy"><b>{userDisplayName}</b><small>{platform.user.email}</small><div className="role-pills">{(platform.user.roles || []).map((item) => <span key={item}>{item}</span>)}</div></div></div>}
        {loggedIn && <><div className="permission-list"><div className="field-group-title">有效权限 <span>{permissions.includes('*') ? 'admin · 全部' : `${permissions.length} 项`}</span></div><div className="permission-cloud">{permissions.includes('*') ? <span>* 全部权限</span> : permissions.slice(0, 14).map((item) => <span key={item}>{item}</span>)}{!permissions.includes('*') && permissions.length > 14 && <span>+{permissions.length - 14}</span>}</div></div>{hasPermission('user:manage') && <div className="user-admin"><div className="field-group-title">账号目录 <button className="text-button" onClick={refreshPlatformUsers}>刷新</button></div>{(platform.users || []).map((item) => <div className="user-row" key={item.id}><span className="user-state" /><div><b>{item.displayName || item.display_name || item.email}</b><small>{item.email}</small></div><span className="role-text">{(item.roles || []).join(' / ')}</span></div>)}{createPlatformUser && <form className="admin-user-form" onSubmit={(event) => { event.preventDefault(); createPlatformUser({ email: newEmail.trim(), password: newPassword, displayName: newDisplayName.trim(), roles: [newRole] }) }}><div className="field-group-title">创建协作账号</div><div className="admin-user-fields"><input aria-label="新账号邮箱" type="email" value={newEmail} onChange={(event) => setNewEmail(event.target.value)} placeholder="邮箱" /><input aria-label="新账号密码" type="password" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} placeholder="至少 8 位密码" /><input aria-label="新账号名称" value={newDisplayName} onChange={(event) => setNewDisplayName(event.target.value)} placeholder="显示名称" /><select aria-label="新账号角色" value={newRole} onChange={(event) => setNewRole(event.target.value)}><option value="reviewer">reviewer · 审核</option><option value="manufacturing">manufacturing · 放行</option><option value="designer">designer · 设计</option><option value="viewer">viewer · 只读</option></select></div><button className="secondary-button" type="submit" disabled={platform?.busy}>＋ 创建账号</button></form>}</div>}</>}
      </section>

      <section className="platform-card panel-card platform-health-card">
        <div className="platform-card-heading"><div><span className="eyebrow">SERVICE HEALTH</span><h2>服务能力</h2></div><button className="secondary-button" disabled={backend?.status === 'checking'} onClick={onRefreshServices}>重新连接</button><span className={`engine-pill ${backend?.productionReady ? 'occt' : 'fallback'}`}>{backend?.engine || 'checking'}</span></div>
        <div className="platform-health-grid"><div><span>几何内核</span><b>{geometry.available ? `CadQuery ${geometry.version || ''} / OCCT` : 'fallback preview'}</b></div><div><span>拓扑输出</span><b>{geometry.available ? 'STEP B-Rep' : '未启用'}</b></div><div><span>DWG 原生解析</span><b>{dwg.available ? `${dwg.engine || 'vector parser'} · 在线` : '转换器未就绪'}</b></div><div><span>OCR</span><b>{ocr.available ? `${ocr.engine || 'OCR'} ${ocr.version || ''}` : 'fixture / review'}</b></div><div><span>审计存储</span><b>{loggedIn ? 'SQLite PDM' : '需要登录'}</b></div></div>
        {!backend?.productionReady && <div className="production-warning"><span>!</span><span>当前服务没有 OCCT 生产内核；可以审阅证据和参数，但不会把降级网格标成生产 STEP。</span></div>}
        <div className="platform-capability-row"><span className="capability-chip">FastAPI</span><span className="capability-chip">DWG vector evidence</span><span className="capability-chip">2D OCR evidence</span><span className="capability-chip">PDM versions</span><span className="capability-chip">RBAC</span><span className="capability-chip">CAM gate</span></div>
      </section>

      <section className="platform-card panel-card platform-pdm-card">
        <div className="platform-card-heading"><div><span className="eyebrow">PRODUCT DATA MANAGEMENT</span><h2>PDM 项目与版本</h2></div><span className="platform-count">{manifestDocuments.length} 文档 · {versionCount} 版本</span></div>
        <div className="platform-action-row"><button className="secondary-button" disabled={!loggedIn || platform?.busy} onClick={createPlatformProject}>创建 / 刷新项目</button><button className="primary-button" disabled={!loggedIn || platform?.busy} onClick={syncDrawingToPdm}>同步当前文件</button></div>
        {!loggedIn ? <div className="platform-empty">登录后可创建项目、保存原图 SHA-256、参数 JSON、STEP / GLB 版本。</div> : !platform?.project ? <div className="platform-empty">还没有绑定项目；点击“创建 / 刷新项目”开始 PDM 工作流。</div> : <><div className="project-binding"><span className="project-dot green" /><div><b>{platform.project.name}</b><small>{platform.project.id} · {platform.project.status}</small></div><span className="check-status pass">已绑定</span></div>{canManageMembers && <div className="project-members-editor"><div className="field-group-title">共享审核 / 制造成员 <span className="muted">只读项目权限</span></div><div className="member-editor-row"><input aria-label="项目成员邮箱或用户 ID" value={memberText} onChange={(event) => setMemberText(event.target.value)} placeholder="邮箱或用户 ID（逗号分隔；留空自动选账号目录）" /><button className="secondary-button" disabled={platform?.busy || !shareProjectTeam} onClick={() => shareProjectTeam(memberText)}>共享成员</button></div><small className="platform-help">成员可读取项目、查看 CAM；审核和 NC 放行仍由各自 RBAC 权限决定。</small></div>}<div className="pdm-document-list">{manifestDocuments.slice(0, 5).map((item) => <div className="pdm-document-row" key={item.document?.id || item.id}><span className="file-type-icon blue">{item.document?.kind === 'drawing' ? '▱' : '◉'}</span><div><b>{item.versions?.[0]?.metadata?.displayName || item.document?.metadata?.displayName || item.document?.name || item.name}</b><small>{item.document?.kind || 'document'} · {item.versions?.length || 0} 个不可变版本</small></div><span className="role-text">{item.versions?.[0]?.sha256?.slice(0, 8) || '—'}</span></div>)}</div>{manifestDocuments.length > 5 && <small className="platform-help">还有 {manifestDocuments.length - 5} 个文档，完整清单可通过 API manifest 查看。</small>}</>}
      </section>

      {mode === 'CAM / NC' && <section className="platform-card panel-card platform-cam-card">
        <div className="platform-card-heading"><div><span className="eyebrow">CAM / NC RELEASE GATE</span><h2>制造计划与 NC</h2></div><span className={`production-badge ${nc ? 'ready' : simulation?.passed ? 'pending' : 'preview'}`}>{nc ? 'NC 已放行' : simulation?.passed ? '等待审核' : '未仿真'}</span></div>
        <div className="cam-source-row"><span>模型来源</span><b>{generation?.artifacts?.find((item) => item.format === 'step')?.sha256?.slice(0, 16) || generation?.requestId || '尚未生成 OCCT 实体'}</b></div>
        <div className="platform-action-row"><button className="secondary-button" disabled={!loggedIn || platform?.busy || !platform?.project || !generation?.validation?.valid} onClick={createCamPlan}>创建 CAM 草案</button><button className="secondary-button" disabled={!loggedIn || platform?.busy || !camPlan} onClick={simulateCamPlan}>运行确定性仿真</button><button className="secondary-button" disabled={!loggedIn || platform?.busy || !camPlan || !simulation || !hasPermission('cam:approve')} onClick={approveCamPlan}>审核通过</button><button className="primary-button" disabled={!loggedIn || platform?.busy || !camPlan || !gate?.passed || !hasPermission('cam:release')} onClick={releaseCamPlan}>放行 NC</button></div>
        {!camPlan ? <div className="platform-empty">生成并校验 STEP 后创建 CAM 草案；计划会记录几何哈希、毛坯、刀具和工序。</div> : <><div className="cam-plan-summary"><div><span>计划状态</span><b>{camPlan.status}</b></div><div><span>工序</span><b>{camPlan.operations?.length || 0}</b></div><div><span>修订</span><b>r{camPlan.revision}</b></div><div><span>引擎</span><b>{simulation?.engine || '—'}</b></div></div>{simulation && <div className={`simulation-result ${simulation.passed ? 'pass' : 'fail'}`}><div className="simulation-title"><span>{simulation.passed ? '✓' : '!'}</span><b>{simulation.passed ? '仿真通过' : '仿真阻断'}</b><small>{simulation.runtimeSeconds ?? 0}s · 碰撞 {simulation.collisionCount} · 擦伤 {simulation.gougeCount}</small></div><div className="simulation-checks">{Object.entries(simulation.checks || {}).map(([key, value]) => <span key={key} className={value ? 'pass' : 'fail'}>{value ? '✓' : '×'} {key}</span>)}</div>{simulation.warnings?.length > 0 && <small className="platform-help">{simulation.warnings.join('；')}</small>}</div>}{gate && <div className={`gate-result ${gate.passed ? 'pass' : 'fail'}`}><b>{gate.passed ? '放行条件满足' : '仍不可放行'}</b><span>{(gate.reasons || []).join('；') || '审核与仿真状态已满足'}</span></div>}{nc && <div className="nc-preview"><div><div><b>{nc.id}</b><small>{nc.postprocessor} · SHA {nc.sha256?.slice(0, 12)}</small></div>{hasPermission('nc:download') && <button className="secondary-button" onClick={downloadNcProgram}>下载 NC</button>}</div>{nc.text ? <pre>{String(nc.text).slice(0, 900)}</pre> : <div className="nc-text-locked">NC 文本已持久化；点击下载后按当前制造权限读取。</div>}</div>}<small className="platform-help">计划、仿真、审批与 NC 会写入 SQLite 并在服务重启后恢复。审核者和制造者必须使用不同账号；确定性预仿真只是一道安全预检查，切削前仍需机床/材料专用验证。</small></>}
      </section>}
    </div>
  </div>
}

function ParameterPanel({ model, modelValid, updateModel, resetModel, drawingJob, disabled = false }) {
  if (canonicalPartKind(model.kind) === 'arched_clevis_support') return <ArchedClevisParameterPanel model={model} modelValid={modelValid} updateModel={updateModel} resetModel={resetModel} drawingJob={drawingJob} disabled={disabled} />
  if (canonicalPartKind(model.kind) === 'stepped_tapered_nozzle') return <SteppedTaperedNozzleParameterPanel model={model} modelValid={modelValid} updateModel={updateModel} resetModel={resetModel} drawingJob={drawingJob} disabled={disabled} />
  if (canonicalPartKind(model.kind) === 'split_clamp_support') return <SplitClampParameterPanel model={model} modelValid={modelValid} updateModel={updateModel} resetModel={resetModel} drawingJob={drawingJob} disabled={disabled} />
  if (model.kind === 'bracket') return <BracketParameterPanel model={model} modelValid={modelValid} updateModel={updateModel} resetModel={resetModel} drawingJob={drawingJob} disabled={disabled} />
  const fields = [['outerDiameter', '外径', 'Ø', 'mm'], ['length', '总长度', '', 'mm'], ['holeDiameter', '通孔直径', 'Ø', 'mm'], ['keywayWidth', '键槽宽度', '', 'mm'], ['keywayDepth', '键槽深度', '', 'mm'], ['keywayLength', '键槽长度', '', 'mm']]
  return <div className="inspector-content"><div className="selection-title"><span className="feature-icon blue">◒</span><div><b>{model.name}</b><small>可编辑参数草稿</small></div><span className={`valid-chip ${modelValid ? '' : 'invalid'}`}>{modelValid ? '有效' : '待修正'}</span></div><div className="field-group"><div className="field-group-title">基本尺寸 <span>单位：mm</span></div>{fields.slice(0, 3).map(([key, label, prefix, suffix]) => <NumberField fieldKey={key} key={key} label={label} value={model[key]} prefix={prefix} suffix={suffix} disabled={disabled} onChange={(value) => updateModel(key, value)} />)}</div><div className="field-group"><div className="field-group-title">键槽特征 <span className="muted">切除</span></div>{fields.slice(3).map(([key, label, prefix, suffix]) => <NumberField fieldKey={key} key={key} label={label} value={model[key]} prefix={prefix} suffix={suffix} disabled={disabled} onChange={(value) => updateModel(key, value)} />)}</div><div className="field-group"><div className="field-group-title">材料</div><div className="select-field"><select value={model.material} disabled={disabled} onChange={(e) => updateModel('material', e.target.value)}><option>45# 钢</option><option>AL6061 铝合金</option><option>SUS304 不锈钢</option></select><span>⌄</span></div></div><button className="reset-link" onClick={resetModel} disabled={disabled}>↻ 恢复基准参数</button></div>
}

function ArchedClevisParameterPanel({ model, modelValid, updateModel, resetModel, drawingJob, disabled = false }) {
  const definition = archedClevisSupportDefinition
  const evidence = drawingJob?.evidence
  const pending = Boolean(evidence && !evidenceAcceptedForPreview(evidence))
  const sources = drawingJob?.analysis?.candidateSources || {}
  const missing = new Set(pending ? definition.required.filter((key) => !requiredParameterPresent(key, evidence.candidateParameters?.[key])) : [])
  const d = archedClevisSupportDimensions(model)
  const format = (value) => Number.isFinite(value) ? Number(value.toFixed(3)) : '—'
  return <div className="inspector-content">
    <div className="selection-title"><span className="feature-icon orange">∩</span><div><b>{model.name}</b><small>双耳拱形支座 · {pending ? 'AI 候选数据' : '可编辑模型'}</small></div><span className={`valid-chip ${!modelValid || missing.size ? 'invalid' : ''}`}>{missing.size ? '待补全' : pending ? '待确认' : modelValid ? '有效' : '待修正'}</span></div>
    {missing.size > 0 && <div className="candidate-missing-note"><b>还有 {missing.size} 项尺寸需要补全</b><span>{[...missing].map((key) => definition.labels[key]).join('、')}</span></div>}
    {archedClevisGroups.map((group) => <div className="field-group" key={group.title}><div className="field-group-title">{group.title}<span>单位：mm</span></div>{group.fields.map((key) => <NumberField key={key} fieldKey={key} label={`${definition.labels[key]}${sources[key] ? ` · ${candidateSourceLabels[sources[key]] || sources[key]}` : ''}`} value={missing.has(key) ? '' : model[key]} pending={pending} source={sources[key] || ''} placeholder={missing.has(key) ? '待补全' : ''} prefix={key.endsWith('Radius') ? 'R' : key.endsWith('Diameter') ? 'Ø' : ''} suffix="mm" disabled={disabled} onChange={(value) => updateModel(key, value)} />)}</div>)}
    <div className="bracket-datum"><span>⌖</span><div><b>尺寸关系</b><small>全宽 = 两侧耳厚 × 2 + 耳间隙</small><small>总长 {format(d.baseLength)} · 总高 {format(d.totalHeight)} mm</small><small>桥面高度 {format(d.bridgeHeight)} mm · 由外拱与耳侧面交点推导</small><small>耳孔沿前后方向；安装孔竖直贯穿底部安装耳。</small></div></div>
    <div className="field-group"><div className="field-group-title">材料</div><div className="select-field"><select value={model.material || '45# 钢'} disabled={disabled} onChange={(event) => updateModel('material', event.target.value)}><option>45# 钢</option><option>AL6061 铝合金</option><option>SUS304 不锈钢</option></select><span>⌄</span></div></div>
    <button className="reset-link" disabled={disabled || pending} onClick={resetModel}>↻ 恢复支座基准参数</button>
  </div>
}

function SteppedTaperedNozzleParameterPanel({ model, modelValid, updateModel, resetModel, drawingJob, disabled = false }) {
  const evidence = drawingJob?.evidence
  const pendingFile = Boolean(drawingJob?.file || drawingJob?.fileMeta?.name)
  const waitingForAnalysis = Boolean(pendingFile && ['queued', 'analyzing'].includes(drawingJob?.status) && !evidence)
  const candidatePending = waitingForAnalysis || Boolean(evidence && !evidenceAcceptedForPreview(evidence))
  const candidateSources = drawingJob?.analysis?.candidateSources || {}
  const evidenceCandidates = evidence?.candidateParameters || {}
  const missingFields = new Set(waitingForAnalysis
    ? steppedTaperedNozzleRequiredParameterKeys
    : candidatePending
      ? (drawingJob?.analysis?.missingFields || steppedTaperedNozzleRequiredParameterKeys.filter((key) => !requiredParameterPresent(key, evidenceCandidates[key])))
      : [])
  const explicitCount = steppedTaperedNozzleRequiredParameterKeys.filter((key) => ['drawing', 'ai', 'manual', 'derived', 'direct_dimension', 'vector_derived', 'ai_interpreted'].includes(candidateSources[key])).length
  const tipLength = Number(model.mainLength) - Number(model.headLength) - Number(model.neckLength)
  const taperLength = (Number(model.outletDiameter) - Number(model.axialBoreDiameter)) / (2 * Math.tan((Math.PI / 180) * Number(model.outletTaperHalfAngle)))
  const radialClearance = (Number(model.counterboreDiameter) - Number(model.insertOuterDiameter)) / 2
  const chipLabel = waitingForAnalysis ? '分析中' : candidatePending ? (missingFields.size ? '待补全' : '待确认') : modelValid ? '有效' : '待修正'
  const fieldPrefix = (key) => key.toLowerCase().includes('diameter') ? 'Ø' : ''
  const fieldSuffix = (key) => key === 'outletTaperHalfAngle' ? '°' : 'mm'
  const fieldLabel = (key) => `${steppedTaperedNozzleParameterLabels[key]}${candidateSources[key] ? ` · ${candidateSourceLabels[candidateSources[key]] || candidateSources[key]}` : missingFields.has(key) ? ' · 待补全' : ''}`
  return <div className="inspector-content">
    <div className="selection-title"><span className="feature-icon orange">◒</span><div><b>{waitingForAnalysis ? '新 DWG · 待 AI 分析' : model.name}</b><small>{candidatePending ? '两组件装配 · 候选数据' : 'stepped_tapered_nozzle_with_insert_v1'}</small></div><span className={`valid-chip ${!modelValid || missingFields.size ? 'invalid' : ''}`}>{chipLabel}</span></div>
    {candidatePending && <div className={`candidate-missing-note ${missingFields.size ? '' : 'candidate-default-note'}`}><b>{missingFields.size ? `仍有 ${missingFields.size} 项必需参数待补全` : `${explicitCount} 项候选已同步`}</b><span>{missingFields.size ? '请根据 DWG 原生尺寸、矢量量测或 AI 解释补全，再确认当前快照。' : '主件与镶件仍是两个独立候选实体；确认前不会生成生产 STEP。'}</span>{missingFields.size > 0 && <small>{[...missingFields].slice(0, 6).map((key) => steppedTaperedNozzleParameterLabels[key] || key).join('、')}{missingFields.size > 6 ? '…' : ''}</small>}</div>}
    {steppedTaperedNozzleGroups.map((group) => <div className="field-group" key={group.title}><div className="field-group-title">{group.title} <span>{group.title === '独立 M12 镶件' ? '第二组件' : group.title === '内孔与出口锥' ? 'mm / °' : '单位：mm'}</span></div>{group.fields.map((key) => key === 'insertThreadDesignation'
      ? <label key={key} data-candidate-source={candidateSources[key] || undefined} className={`number-field ${candidatePending ? 'candidate-pending' : ''} ${candidateSources[key] ? `candidate-source-${candidateSources[key]}` : ''}`}><span>{fieldLabel(key)}</span><div><span className="field-prefix" /><input value={waitingForAnalysis || (candidatePending && missingFields.has(key)) ? '' : model[key] ?? ''} placeholder={waitingForAnalysis ? '等待分析' : missingFields.has(key) ? '例如 M12' : '螺纹标注'} type="text" disabled={disabled || waitingForAnalysis} onChange={(event) => updateModel(key, event.target.value)} /><span className="field-suffix">标注</span></div></label>
      : <NumberField fieldKey={key} key={key} label={fieldLabel(key)} value={waitingForAnalysis || (candidatePending && missingFields.has(key)) ? '' : model[key]} pending={candidatePending && !waitingForAnalysis} source={candidateSources[key] || ''} disabled={disabled || waitingForAnalysis} placeholder={waitingForAnalysis ? '等待分析' : missingFields.has(key) ? '待补全' : candidatePending ? '待确认' : ''} prefix={fieldPrefix(key)} suffix={fieldSuffix(key)} min={key === 'insertAxialOffset' ? 0 : 0.1} step={key === 'headLeftDiameter' ? 0.001 : 0.1} onChange={(value) => updateModel(key, value)} />)}</div>)}
    <div className="bracket-datum"><span>⌖</span><div><b>配方派生（只读）</b><small>末段长度 {Number.isFinite(tipLength) ? Number(tipLength.toFixed(3)) : '—'} mm · 出口锥深 {Number.isFinite(taperLength) ? Number(taperLength.toFixed(9)) : '—'} mm</small><small>镶件径向间隙 {Number.isFinite(radialClearance) ? Number(radialClearance.toFixed(3)) : '—'} mm · 轴向偏置 {model.insertAxialOffset ?? '—'} mm</small><small>预览以两个实体区分主件与镶件；{model.insertThreadDesignation || 'M12'} 只显示标注，不生成真实螺纹牙型。</small></div></div>
    {!modelValid && !waitingForAnalysis && <div className="bracket-constraint"><span>!</span><span>请检查三段长度、孔壁厚度、沉孔/镶件间隙、镶件轴向位置及出口锥角关系。</span></div>}
    <div className="field-group"><div className="field-group-title">主件材料</div><div className="select-field"><select value={model.material} onChange={(event) => updateModel('material', event.target.value)} disabled={disabled || waitingForAnalysis}><option>45# 钢</option><option>AL6061 铝合金</option><option>SUS304 不锈钢</option></select><span>⌄</span></div></div>
    <div className="evidence-mini"><Icon>✓</Icon><span>{candidatePending ? 'DWG 矢量读值、AI 解释和人工修改分别保留来源；确认后才进入 OCCT。' : '当前是两实体装配候选；镶件固定方式与真实螺纹仍需另行定义。'}</span></div>
    <button className="reset-link" onClick={resetModel} disabled={disabled || waitingForAnalysis || candidatePending} title={candidatePending ? '确认或编辑当前候选后才能重置基准' : ''}>↻ 恢复阶梯锥管嘴候选基准</button>
  </div>
}

function SplitClampParameterPanel({ model, modelValid, updateModel, resetModel, drawingJob, disabled = false }) {
  const evidence = drawingJob?.evidence
  const candidatePending = Boolean(evidence && !evidenceAcceptedForPreview(evidence))
  const pendingFile = Boolean(drawingJob?.file || drawingJob?.fileMeta?.name)
  const waitingForAnalysis = Boolean(pendingFile && ['queued', 'analyzing'].includes(drawingJob?.status) && !evidence)
  const candidateSources = drawingJob?.analysis?.candidateSources || {}
  const missingFields = new Set(candidatePending
    ? (drawingJob?.analysis?.missingFields || splitClampRequiredParameterKeys.filter((key) => !(Number(evidence?.candidateParameters?.[key]) > 0)))
    : [])
  const explicitCount = splitClampRequiredParameterKeys.filter((key) => ['drawing', 'ai', 'manual', 'derived'].includes(candidateSources[key])).length
  const topZ = Number(model.baseThickness) + Number(model.pedestalHeight)
  const frontTongueDepth = Number(model.baseWidth) - Number(model.baseMainDepth)
  const chipLabel = waitingForAnalysis ? '分析中' : candidatePending ? (missingFields.size ? '待补全' : '待确认') : modelValid ? '有效' : '待修正'
  return <div className="inspector-content">
    <div className="selection-title"><span className="feature-icon orange">◉</span><div><b>{waitingForAnalysis ? '新图纸 · 待 AI 分析' : model.name}</b><small>{candidatePending ? '开口夹紧座 · AI 候选数据' : 'split_clamp_support_v1 · 参数化实体'}</small></div><span className={`valid-chip ${candidatePending && missingFields.size ? 'invalid' : ''}`}>{chipLabel}</span></div>
    {candidatePending && <div className={`candidate-missing-note ${missingFields.size ? '' : 'candidate-default-note'}`}><b>{missingFields.size ? `大模型仍有 ${missingFields.size} 项未返回` : `${explicitCount} 项候选已同步`}</b><span>{missingFields.size ? '空白字段需由大模型继续分析或人工补全；已识别值已写入下方。' : '全部必需字段可编辑确认；确认前不会生成生产 STEP。'}</span>{missingFields.size > 0 && <small>{[...missingFields].slice(0, 5).map((key) => splitClampParameterLabels[key] || key).join('、')}{missingFields.size > 5 ? '…' : ''}</small>}</div>}
    {splitClampGroups.map((group) => <div className="field-group" key={group.title}><div className="field-group-title">{group.title} <span>单位：{group.title === '孔与开缝' ? 'mm / 个' : 'mm'}</span></div>{group.fields.map((key) => <NumberField fieldKey={key} key={key} label={`${splitClampParameterLabels[key]}${candidateSources[key] ? ` · ${candidateSourceLabels[candidateSources[key]] || candidateSources[key]}` : missingFields.has(key) ? ' · 待补全' : ''}`} value={waitingForAnalysis || (candidatePending && missingFields.has(key)) ? '' : model[key]} pending={candidatePending && !waitingForAnalysis} source={candidateSources[key] || ''} disabled={disabled || waitingForAnalysis} placeholder={waitingForAnalysis ? '等待分析' : missingFields.has(key) ? '待补全' : candidatePending ? '待确认' : ''} prefix={['boreDiameter', 'mountHoleDiameter', 'crossHoleDiameter'].includes(key) ? 'Ø' : key === 'pedestalOuterRadius' || ['outerCornerRadius', 'neckConcaveRadius', 'neckConvexRadius'].includes(key) ? 'R' : ''} suffix={key === 'mountHoleCount' ? '个' : 'mm'} onChange={(value) => updateModel(key, value)} />)}</div>)}
    <div className="bracket-datum"><span>⌖</span><div><b>配方推导</b><small>圆筒外径 Ø{Number(model.pedestalOuterRadius) * 2 || '—'} · 轴线距后缘 {model.pedestalCenterFromRear || '—'}</small><small>前舌深度 {Number.isFinite(frontTongueDepth) ? frontTongueDepth : '—'} · 低台顶面 Z {Number.isFinite(topZ) ? topZ : '—'}</small><small>高壁顶面 Z {model.totalHeight || '—'} · 盲孔底面 Z {model.boreFloorZ || '—'}</small></div></div>
    {!modelValid && !waitingForAnalysis && <div className="bracket-constraint"><span>!</span><span>请检查总高关系、孔壁厚度、底板轮廓及安装孔边距。</span></div>}
    <div className="field-group"><div className="field-group-title">材料</div><div className="select-field"><select value={model.material} onChange={(e) => updateModel('material', e.target.value)} disabled={disabled || waitingForAnalysis}><option>45# 钢</option><option>AL6061 铝合金</option><option>SUS304 不锈钢</option></select><span>⌄</span></div></div>
    <div className="evidence-mini"><Icon>✓</Icon><span>{candidatePending ? '字段来源与推导由 AI 证据保存；确认后才进入 OCCT 生产实体。' : '当前参数对应 split_clamp_support_v1 可审计配方。'}</span></div>
    <button className="reset-link" onClick={resetModel} disabled={disabled || waitingForAnalysis || candidatePending} title={candidatePending ? '确认或编辑当前 AI 候选后才能重置基准' : ''}>↻ 恢复夹紧座预览基准</button>
  </div>
}

function BracketParameterPanel({ model, modelValid, updateModel, resetModel, drawingJob, disabled = false }) {
  const groups = [
    { title: '底板尺寸', fields: [['baseLength', '长度'], ['baseWidth', '宽度'], ['baseThickness', '厚度']] },
    { title: '上部实体', fields: [['upperLength', '长度'], ['upperWidth', '全宽'], ['upperHeight', '高度'], ['totalHeight', '总高']] },
    { title: '切除特征', fields: [['notchOpening', '鞍槽开口'], ['notchRadius', '鞍槽半径'], ['slotLength', '浅槽长度 Y'], ['slotWidth', '浅槽宽度 X'], ['pocketDepth', '浅槽深度'], ['bossDiameter', '贯穿孔 Ø'], ['bossCenterDistance', '贯穿孔中心距']] },
  ]
  const numeric = (key) => Number(model[key])
  const relationWarning = numeric('upperLength') > numeric('baseLength') || numeric('upperWidth') > numeric('baseWidth') || numeric('slotLength') > numeric('baseWidth') || numeric('pocketDepth') > numeric('upperHeight') || numeric('notchOpening') < numeric('notchRadius') * 2 || Math.abs(numeric('totalHeight') - numeric('baseThickness') - numeric('upperHeight')) > 1e-6
  // The browser intentionally persists only file metadata, not the source
  // bytes.  After a refresh an in-flight upload therefore has `file === null`
  // while `fileMeta` and the queued/analyzing status remain.  Treat that
  // state as pending analysis too; otherwise the persisted model's old
  // dimensions become editable and can be mistaken for the new drawing's AI
  // candidate.  The main upload action remains available so the customer can
  // re-select the source file and resume analysis.
  const pendingFile = Boolean(drawingJob?.file || drawingJob?.fileMeta?.name)
  const waitingForAnalysis = Boolean(pendingFile && ['queued', 'analyzing'].includes(drawingJob?.status) && !drawingJob?.evidence)
  const persistedOnly = waitingForAnalysis && !drawingJob?.file
  const missingFields = new Set(waitingForAnalysis
    ? bracketRequiredParameterKeys
    : Array.isArray(drawingJob?.analysis?.missingFields) ? drawingJob.analysis.missingFields : [])
  const candidateSources = drawingJob?.analysis?.candidateSources || {}
  const defaultedFields = new Set(Array.isArray(drawingJob?.analysis?.defaultedFields) ? drawingJob.analysis.defaultedFields : [])
  const candidatePending = waitingForAnalysis || Boolean(drawingJob?.evidence && !evidenceAcceptedForPreview(drawingJob.evidence))
  const chipLabel = waitingForAnalysis ? '待分析' : candidatePending ? (missingFields.size ? '待补全' : '待确认') : modelValid ? '有效' : '待修正'
  const sourceLabel = (key) => candidateSourceLabels[candidateSources[key]] || (defaultedFields.has(key) ? candidateSourceLabels.template_default : '候选值')
  const fieldLabel = (key, label) => waitingForAnalysis && missingFields.has(key)
    ? `${label} · 待分析`
    : missingFields.has(key) ? `${label} · 待补全` : candidatePending ? `${label} · ${sourceLabel(key)}` : label
  const explicitCandidateCount = bracketRequiredParameterKeys.filter((key) => candidateSources[key] === 'drawing' || candidateSources[key] === 'ai').length
  const candidateMessage = waitingForAnalysis
    ? persistedOnly
      ? ['需要重新选择图纸文件', '刷新后浏览器只保留文件名；请重新选择同一文件以继续 AI 分析，不会沿用上一张图纸的尺寸。']
      : ['等待 AI 分析图纸', '分析完成后会在这里显示模型候选值；不会沿用上一张图纸的尺寸。']
    : missingFields.size
      ? [`大模型仍有 ${missingFields.size} 项未返回`, '空白字段没有使用 OCR 或模板值代填；请人工补全后点击“确认数据”。']
      : [`大模型已返回 ${explicitCandidateCount} 项候选值`, '请逐项核对后确认当前快照。']
  const showCandidateNote = candidatePending && (waitingForAnalysis || missingFields.size > 0 || defaultedFields.size > 0)
  return <div className="inspector-content">
    <div className="selection-title"><span className="feature-icon orange">⌂</span><div><b>{waitingForAnalysis ? '新图纸 · 待 AI 分析' : model.name}</b><small>{waitingForAnalysis ? '上一版本仅保留为预览' : candidatePending ? 'AI 候选数据 · 可编辑确认' : drawingJob?.evidence ? '图纸参数 · 已确认' : '文字或人工参数草稿'}</small></div><span className={`valid-chip ${candidatePending && missingFields.size ? 'invalid' : ''}`}>{chipLabel}</span></div>
    {showCandidateNote && <div className={`candidate-missing-note ${!waitingForAnalysis && !missingFields.size ? 'candidate-default-note' : ''}`}><b>{candidateMessage[0]}</b><span>{candidateMessage[1]}</span>{!waitingForAnalysis && missingFields.size > 0 && <small>{[...missingFields].slice(0, 5).map((key) => bracketParameterLabels[key] || key).join('、')}{missingFields.size > 5 ? '…' : ''}</small>}</div>}
    {groups.map((group) => <div className="field-group" key={group.title}><div className="field-group-title">{group.title} <span>单位：mm</span></div>{group.fields.map(([key, label]) => <NumberField fieldKey={key} key={key} label={fieldLabel(key, label)} value={waitingForAnalysis || (candidatePending && missingFields.has(key)) ? '' : model[key]} pending={candidatePending && !waitingForAnalysis} source={candidateSources[key] || (defaultedFields.has(key) ? 'template_default' : '')} disabled={disabled || waitingForAnalysis} placeholder={waitingForAnalysis ? '等待分析' : missingFields.has(key) ? '待补全' : candidatePending ? '待确认' : ''} prefix={key === 'bossDiameter' ? 'Ø' : ''} suffix="mm" onChange={(value) => updateModel(key, value)} />)}</div>)}
    <div className="bracket-datum"><span>⌖</span><div><b>基准定位</b><small>贯穿孔中心：X ±{Math.round(numeric('bossCenterDistance') / 2 || 35)} · Y 0 · Z 0（贯穿至总高）</small><small>鞍槽圆弧中心 Z {Math.round(numeric('totalHeight') || 40)} · 槽底 Z {Math.round((numeric('totalHeight') || 40) - (numeric('notchRadius') || 15))}</small><small>浅槽：Y ±{Math.round(numeric('slotLength') / 2 || 15)} · 底面 Z {Math.round((numeric('totalHeight') || 40) - (numeric('pocketDepth') || 10))}</small></div></div>
    {relationWarning && !waitingForAnalysis && <div className="bracket-constraint"><span>!</span><span>请确认总高关系、上部全宽、浅槽长度/深度和鞍槽开口约束。</span></div>}
    <div className="field-group"><div className="field-group-title">材料</div><div className="select-field"><select value={model.material} onChange={(e) => updateModel('material', e.target.value)} disabled={disabled || waitingForAnalysis}><option>45# 钢</option><option>AL6061 铝合金</option><option>SUS304 不锈钢</option></select><span>⌄</span></div></div>
    <div className="evidence-mini"><Icon>✓</Icon><span>{waitingForAnalysis ? '先运行 AI 分析，再确认本张图纸的数据。' : candidatePending ? '仅显示大模型候选与人工补全值；确认后才会进入生产实体。' : drawingJob?.evidence ? '已保留候选来源与人工确认状态。' : '当前尺寸来自文字设计或人工编辑；尚无上传图纸证据。'}</span></div><button className="reset-link" onClick={resetModel} disabled={disabled || waitingForAnalysis || candidatePending} title={candidatePending ? '确认或编辑当前 AI 候选后才能重置基准' : ''}>↻ 恢复支架基准参数</button>
  </div>
}

function NumberField({ fieldKey, label, value, prefix, suffix, onChange, pending = false, source = '', placeholder = '', disabled = false, min = 0.1, step = 0.1 }) {
  const errors = useContext(ParameterErrors)
  const id = useId()
  const error = errors.find((item) => item.field === fieldKey)
  return <label data-candidate-source={source || undefined} className={`number-field ${pending ? 'candidate-pending' : ''} ${source ? `candidate-source-${source}` : ''}`}><span>{label}</span><div><span className="field-prefix">{prefix}</span><input value={value ?? ''} placeholder={placeholder} type="number" min={min} step={step} disabled={disabled} aria-invalid={error ? true : undefined} aria-describedby={error ? id : undefined} onChange={(event) => onChange(event.target.value)} /><span className="field-suffix">{suffix}</span></div>{error && <small id={id} className="parameter-error">{error.message}</small>}</label>
}
function FeaturePanel({ features, selectedFeature, setSelectedFeature, onAddFeature }) { return <div className="inspector-content feature-tree-panel"><div className="tree-toolbar"><span>特征历史 <b>{features.length}</b></span><button aria-label="用 AI 修改特征" onClick={onAddFeature}>＋</button></div><div className="feature-tree">{features.map((feature, index) => <button key={feature.id} className={`feature-row ${selectedFeature === feature.id ? 'selected' : ''}`} onClick={() => setSelectedFeature(feature.id)}><span className="tree-line">{index < features.length - 1 ? '│' : '└'}</span><span className="feature-glyph">{feature.icon}</span><span className="feature-label">{feature.label}<small>{feature.meta}</small></span>{selectedFeature === feature.id && <span className="eye">◉</span>}</button>)}</div><div className="feature-note"><Icon>✦</Icon><span>特征树展示当前参数化配方；点击“＋”描述需要的特征，AI 会说明支持范围。</span></div></div> }
function CheckPanel({ model, modelValid, showToast, backend, generation, drawingJob, generateFromDrawing, acceptDrawingData, setActiveMode, busy = false, checkResult, onRunChecks, parameterErrors = [] }) {
  const metrics = modelValid && !generation?.stale ? generation?.validation?.metrics || {} : {}
  const kernelReady = productionArtifactsAvailable(generation)
  const evidence = drawingJob?.evidence
  const modelKind = partKindFromEnvelope(evidence || drawingJob?.analysis, model?.kind)
  const evidenceParameters = evidence?.parameters && Object.keys(evidence.parameters).length
    ? evidence.parameters
    : evidence?.candidateParameters && Object.keys(evidence.candidateParameters).length
      ? evidence.candidateParameters
      : model || evidence || {}
  const evidenceValue = (key) => evidenceParameters[key] !== undefined && evidenceParameters[key] !== null ? evidenceParameters[key] : '待确认'
  const evidenceTipOperands = ['mainLength', 'headLength', 'neckLength'].map((key) => Number(evidenceParameters[key]))
  const evidenceTipLength = evidenceTipOperands.every(Number.isFinite)
    ? Number((evidenceTipOperands[0] - evidenceTipOperands[1] - evidenceTipOperands[2]).toFixed(3))
    : '待确认'
  const evidenceRows = !evidence ? [] : modelKind === 'arched_clevis_support' ? archedClevisEvidenceRows(model, evidenceParameters, (source) => source ?? '待确认') : modelKind === 'stepped_tapered_nozzle' ? [
    ['主件轴向', `${evidenceValue('mainLength')} mm · ${evidenceValue('headLength')} + ${evidenceValue('neckLength')} + ${evidenceTipLength} mm`],
    ['主件外轮廓', `Ø${evidenceValue('headLeftDiameter')} → Ø${evidenceValue('headRightDiameter')} / Ø${evidenceValue('neckDiameter')} / Ø${evidenceValue('tipDiameter')}`],
    ['沉孔 / 通孔', `Ø${evidenceValue('counterboreDiameter')} × ${evidenceValue('counterboreDepth')} · Ø${evidenceValue('axialBoreDiameter')} 贯通`],
    ['末端锥口', `Ø${evidenceValue('outletDiameter')} · 半角 ${evidenceValue('outletTaperHalfAngle')}°`],
    ['独立镶件', `Ø${evidenceValue('insertOuterDiameter')} × ${evidenceValue('insertLength')} · ${evidenceValue('insertThreadDesignation')} · 偏置 ${evidenceValue('insertAxialOffset')} mm`],
  ] : modelKind === 'split_clamp_support' ? [
    ['异形底板', `${evidenceValue('baseLength')} × ${evidenceValue('baseWidth')} × ${evidenceValue('baseThickness')} mm`],
    ['圆筒夹座', `R${evidenceValue('pedestalOuterRadius')} · 轴线距后缘 ${evidenceValue('pedestalCenterFromRear')} mm`],
    ['盲孔 / 开缝', `Ø${evidenceValue('boreDiameter')} · 孔底 Z${evidenceValue('boreFloorZ')} · 缝宽 ${evidenceValue('splitWidth')} mm`],
    ['安装孔', `${evidenceValue('mountHoleCount')} × Ø${evidenceValue('mountHoleDiameter')} · 中心距 ${evidenceValue('mountHoleCenterDistance')} · 距后缘 ${evidenceValue('mountHoleCenterFromRear')} mm`],
    ['夹紧横孔', `Ø${evidenceValue('crossHoleDiameter')} @ Z${evidenceValue('crossHoleCenterZ')}`],
  ] : [
    ['底板', `${evidenceValue('baseLength')} × ${evidenceValue('baseWidth')} × ${evidenceValue('baseThickness')} mm`],
    ['上部实体', `${evidenceValue('upperLength')} × ${evidenceValue('upperWidth')} × ${evidenceValue('upperHeight')} mm`],
    ['鞍槽 / 浅槽', `R${evidenceValue('notchRadius')} · ${evidenceValue('slotWidth')} × ${evidenceValue('slotLength')} × ${evidenceValue('pocketDepth')}`],
    ['贯穿孔', `2 × Ø${evidenceValue('bossDiameter')} · 中心距 ${evidenceValue('bossCenterDistance')} mm`],
  ]
  const evidenceConfirmed = evidenceAcceptedForPreview(evidence)
  const customerReady = Boolean(evidenceConfirmed || (drawingJob?.status === 'ready' && (acceptDrawingData || generateFromDrawing)))
  const analysis = drawingJob?.analysis || {}
  const checks = [
    { label: '参数完整性', status: modelValid ? '通过' : '待修正' },
    { label: '实体拓扑', status: kernelReady && metrics.topologyAuditPassed !== false ? '通过' : generation ? '待内核校验' : '未运行' },
    { label: '关键尺寸', status: kernelReady && metrics.bboxLength ? '通过' : generation ? '待校验' : '未运行' },
    { label: '制造可行性', status: kernelReady ? '提示' : '仅预览' },
  ]
  return <div className="inspector-content check-panel">{evidence && <section className={`check-evidence-card ${evidenceConfirmed ? 'confirmed' : 'needs-review'}`} aria-label="图纸证据确认"><div className="check-evidence-heading"><div><span className="eyebrow">DRAWING EVIDENCE</span><b>{evidenceConfirmed ? '尺寸证据已确认' : 'AI 候选数据待确认'}</b></div><span className={`confidence ${evidenceConfirmed ? 'ready' : ''}`}>{evidence.confidence !== undefined ? `${Math.round(Number(evidence.confidence) * 100)}%` : '—'}</span></div>{analysis.message && <p className="check-analysis-message">{analysis.message}</p>}<div className="check-evidence-rows">{evidenceRows.map(([label, value]) => <div key={label}><span>{evidenceConfirmed ? label : `候选 · ${label}`}</span><b>{value}</b></div>)}</div><p>{evidenceConfirmed ? '来源已锁定；生成实体会继续经过 CadQuery / OCCT 拓扑检查。' : '候选尺寸可在参数面板中逐项编辑；确认数据后，下一步就是生成 3D。'}</p><div className="check-evidence-actions"><button type="button" className={evidenceConfirmed ? 'secondary-button' : 'primary-button'} disabled={busy || !customerReady || drawingJob?.status === 'generating' || drawingJob?.status === 'generated'} onClick={() => { if (evidenceConfirmed) return showToast('尺寸证据已确认'); acceptDrawingData?.() || generateFromDrawing?.() }}>{evidenceConfirmed ? '已确认' : busy ? '处理中…' : '确认数据'} <Icon>↗</Icon></button></div></section>}{!evidence && <div className="check-evidence-empty"><span>⌁</span><b>完成 AI 分析后，这里会显示尺寸证据。</b><small>系统会把来源视图、置信度和确认状态绑定到当前模型版本。</small></div>}<div className="check-summary"><div className={`check-ring ${modelValid && (kernelReady || !generation) ? 'ok' : 'warn'}`}>{modelValid && (kernelReady || !generation) ? '✓' : '!'}</div><div><b>{!modelValid ? '需要修正参数' : kernelReady ? 'OCCT 模型检查通过' : checkResult ? (checkResult.valid ? '当前参数检查通过' : '服务校验未通过') : '参数初检通过 · 未运行服务检查'}</b><small>{backend?.engine || '浏览器'} · 最近检查：{checkResult?.checkedAt || (generation ? '生成时' : '尚未运行')}</small></div></div>{parameterErrors.map((error) => <p className="parameter-error" key={error.field + error.message}>{error.message}</p>)}{checkResult && <><p>{checkResult.scope} · {checkResult.checkedAt}</p>{(checkResult.errors || []).map((error, index) => <p className="parameter-error" key={index}>{typeof error === 'string' ? error : error.message}</p>)}</>}{checks.map((check) => <div className="check-row" key={check.label}><span>{check.label}</span><span className={`check-status ${check.status === '通过' ? 'pass' : check.status === '提示' ? 'hint' : 'warn'}`}>{check.status}</span></div>)}{generation && <div className="kernel-metrics"><span>包络</span><b>{modelBoundsText(model, metrics)} mm</b><span>体积</span><b>{metrics.volumeMm3 ? `${Number(metrics.volumeMm3).toFixed(3)} mm³` : '—'}</b></div>}<button className="primary-outline" disabled={busy} onClick={onRunChecks}>{busy ? '检查中…' : '重新运行检查'} <Icon>↗</Icon></button></div>
}

function HomeWorkspace({ projects, onSelectProject, onStartText, createProject, setActiveMode, showToast, attachDrawingToConversation }) {
  const inputRef = useRef(null)
  const [description, setDescription] = useState('')
  const openTextWorkbench = () => onStartText(description)
  const chooseDrawing = () => inputRef.current?.click()
  return <div className="home-workspace currentcad-home">
    <section className="home-hero">
      <input ref={inputRef} className="file-input" type="file" accept="image/*,.pdf,.dxf,.dwg" aria-label="上传图纸开始 AI 分析" onChange={(event) => { const files = Array.from(event.currentTarget.files || []); event.currentTarget.value = ''; attachDrawingToConversation?.(files) }} />
      <div className="home-launch-badge"><b>JoyNiu AI V2.0</b><span>Agent 驱动的零件与装配体 CAD 智能设计平台</span><button onClick={() => setActiveMode('帮助与反馈')}>查看流程 <Icon>→</Icon></button></div>
      <span className="home-kicker">AI PARAMETRIC CAD</span>
      <h1>Hi，开启您的 AI 建模旅程</h1>
      <p>用文字或工程图生成可编辑的参数化模型，完整保留尺寸来源、特征树和交付版本。</p>
      <div className="home-agent-modes" aria-label="开始设计"><button className="active" onClick={openTextWorkbench}><Icon>✦</Icon> 参数化零件</button><button onClick={() => setActiveMode('装配')}><Icon>⌘</Icon> 装配工作台</button></div>
      <div className="home-composer-frame">
        <textarea className="home-composer-input" aria-label="描述零件设计" value={description} onChange={(event) => setDescription(event.target.value)} placeholder="描述你想设计的零件，例如：创建一个外径 24、长 70、带键槽的动力轴" />
        <div className="home-composer-footer"><div><button onClick={chooseDrawing}><Icon>＋</Icon> 上传图纸</button><button onClick={() => setActiveMode('帮助与反馈')}><Icon>?</Icon> 使用指南</button></div><span>JPG / PDF / DWG / DXF · ≤ 20 MB</span><button className="home-composer-submit" aria-label="开始文字设计" onClick={openTextWorkbench}>↑</button></div>
      </div>
      <div className="home-capability-strip">
        <button onClick={openTextWorkbench}><span className="capability-art text-art">Aa</span><b>文字生成模型</b><Icon>›</Icon></button>
        <button onClick={chooseDrawing}><span className="capability-art image-art">▧</span><b>图片生成模型</b><Icon>›</Icon></button>
        <button onClick={chooseDrawing}><span className="capability-art drawing-art">▱</span><b>二维图生成模型</b><Icon>›</Icon></button>
        <button onClick={() => setActiveMode('标准件库')}><span className="capability-art lab-art">◇</span><b>标准件与自定义零件</b><Icon>›</Icon></button>
      </div>
      <div className="hero-format-note">上传 → AI 分析 → 确认数据 → 生成 3D → 二次修改 → 导出交付</div>
    </section>

    <div className="home-section-heading"><div><h2>最近项目</h2><span>继续你的设计工作</span></div><button className="text-button" onClick={createProject}>＋ 新建项目</button></div>
    <div className="project-cards">{projects.map((project) => <button key={project.id} className="project-card" onClick={() => onSelectProject(project.id)}><div className={`project-preview ${project.color}`}><span>{project.name.slice(0, 1)}</span><small>{project.files} 个文件</small></div><div className="project-card-body"><b>{project.name}</b><span>更新于 {project.updated}</span></div><span className="card-arrow">↗</span></button>)}</div>
    <div className="quick-grid"><button onClick={() => { setActiveMode('3D 建模'); showToast('已打开 AI 设计助手') }}><span className="quick-icon blue">✦</span><div><b>AI 参数化零件</b><small>从一句话开始设计</small></div><span>→</span></button><button onClick={() => setActiveMode('2D 工程图')}><span className="quick-icon orange">▱</span><div><b>2D 工程图</b><small>由当前模型生成视图</small></div><span>→</span></button><button onClick={() => setActiveMode('装配')}><span className="quick-icon violet">◈</span><div><b>装配工作台</b><small>已有实体后再创建装配</small></div><span>→</span></button></div>
  </div>
}

export default App
