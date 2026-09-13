# 二维绘图点击无响应修复 · 2026-09-13

## 原因与改动

新建图纸、编辑图元、导入、撤销/重做和添加约束直接使用 `crypto.randomUUID()`。公网 HTTP 环境不提供这个接口，而 localhost 开发环境可用。新建在请求处理与异常提示之前生成编号，异常导致两个新建入口均没有页面反馈。参数图库保存和 AI 装配提交也有相同依赖，后者还会跳过 finally，使提交按钮一直忙碌。

统一复用已有 `createClientId()`，在没有 randomUUID 的环境使用 getRandomValues 生成 UUID。重试继续复用原请求编号，不会因网络响应丢失再次创建图纸。二维操作入口增加同步错误提示；装配请求编号初始化进入 try/finally 范围，异常后释放提交状态。此次不修改后端或客户图纸。

## 验证

- 保留上一版生产构建，在本地非 localhost 的 HTTP 地址加载；通过同源代理访问验收 API，保留真实 Host/Origin，账号正常登录。两个新建入口均复现点击后无变化。
- 同一 HTTP 页面加载修复后脚本，点击“创建第一张图纸”创建修订 1。绘制 100 mm 直线后修订 2、1 个图元；撤销后修订 3、0 个图元；重做后修订 4、1 个图元。
- 顶部“新建”另建独立图纸，原图可从云图纸列表重新打开。仅操作本地验收账号的两张测试图纸。
- 新增 7 项回归覆盖没有 randomUUID 的真实组件 handlers、失败重试编号复用、导入/约束/图库及装配提交异常恢复。完整前端测试 538 项通过，0 失败。
- 生产构建与 git diff --check 通过。没有真实付费 AI 调用，装配测试的网络被拦截。

## 发布

已发布至 http://122.51.168.205/ ，公网浏览器实际加载 `/assets/index-B41d91hL.js`，与本地完成 HTTP 流程验收的构建相同。

- Web：`joyniu-cad-web:native-http-fix-20260913`
- 镜像：`sha256:c8eea7deb67ceff2d122bdb32f98ff4d9f436c5b693a8c4f780294faf837234b`
- 前端入口与资产 SHA256 同冻结构建一致，API/egress 容器身份和启动时间不变。
- 公网 14 项页面、资源、健康与未登录权限检查通过。公网本轮未登录客户账号创建测试图纸，实际创建/绘图使用本地独立验收账号及非 localhost HTTP 页面。
- 旧 Web 镜像和静态资源保留，可单独回退。源码与产物保留在 `/opt/joyniu-candidates/native-http-fix-20260913`；未创建新 Git 提交或推送。

证据：[测试](evidence/native-http-fix-20260913/frontend-tests.log)、[构建](evidence/native-http-fix-20260913/frontend-build.log)、[发布](evidence/native-http-fix-20260913/web-switch.json)、[公网检查](evidence/native-http-fix-20260913/public-verify.json)、[文件校验](evidence/native-http-fix-20260913/manifest.json)。
