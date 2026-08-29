# JoyNiu NewCAD

基于 CurrentCAD 深度体验结论独立实现的 AI 工程设计工作台原型。

## 已实现

- AI 参数化零件 Agent：中文自然语言解析外径、长度、通孔和键槽。
- 3D 建模视口：等轴测/前视/俯视、剖切、拖拽旋转、缩放、视图重置。
- 参数面板、特征树、模型检查和材料切换。
- 2D 工程图：三视图、尺寸标注、图层、比例和 DXF 导出入口。
- 装配 Agent：实例树、同轴配合、装配检查和干涉状态。
- 标准件库：分类/搜索、规格卡片、插入项目零件库。
- 首页与项目管理：最近项目、新建项目、回收站/设置/帮助入口。
- STEP、DXF、参数 JSON 导出交互；当前文件明确标识为演示内核草案。
- 浏览器 `localStorage` 自动保存模型参数和项目列表。

## 本地运行

```bash
npm install
npm run dev
```

打开 `http://localhost:5173/`。

生产构建：

```bash
npm run build
npm run preview
```

## 文件化开发管理

- [Product-Spec.md](Product-Spec.md)：产品定位、线程证据、需求优先级、验收标准和版本路线。
- [docs/CHANGELOG.md](docs/CHANGELOG.md)：每次版本的 Added / Known limitations。
- 分支：`JoyNiu-NewCAD`。

## 生产化边界

当前版本重点复刻交互和信息架构，三维实体由 SVG 演示内核生成，导出文件用于流程联调和验收演示，不宣称为生产级 STEP/DXF。下一阶段可按产品规格接入 FastAPI + CadQuery/OCCT、真实 OCR/2D AI、PDM、权限和 CAM/NC 放行。

