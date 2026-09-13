# 二维 CAD 功能区与交互验收

状态：2026-09-13 已发布至 http://122.51.168.205/，公网浏览器、重新登录重开、DXF 回读及服务健康验证完成。

旧界面嵌在业务工作台卡片内，画布被导航和常驻属性面板挤占。现在进入“二维绘图”即打开独立全屏 CAD；顶部常用、插入、注释、参数化、批注、工具分组；底部命令行、模型/布局页签、捕捉/网格/正交/极轴开关，属性按需展开。

## 实测

- 前端完整回归 788 项通过；最终新增键盘及布局修复后另跑原生工作区与交互专项。后端原生图纸、API、权限、恢复、气泡、参数图库、DWG 相关 56 项通过。
- 真实浏览器登录、创建图纸、L 与相对坐标、椭圆、选择、移动复制、撤销重做、图元组、Ctrl+C/V、新布局、出图、重新登录重开全部通过。另验证鼠标两点移动、真实圆角、数值旋转和缩放、字母唤起命令、未完成输入保护。
- 下载 DXF 经独立 ezdxf 回读：8 个实体、2 个原生椭圆、1 个图元组、Model/Layout1/Layout2、Layout2 中两个原生 VIEWPORT。桌面1440×960和手机390×844检查通过，JavaScript页面异常0。

[浏览器结果](evidence/native-cad-ribbon-2026-09-13/browser-verification.json) · [图形操作结果](evidence/native-cad-ribbon-2026-09-13/graphical-verification.json) · [桌面](evidence/native-cad-ribbon-2026-09-13/desktop.png) · [手机](evidence/native-cad-ribbon-2026-09-13/mobile.png) · [布局](evidence/native-cad-ribbon-2026-09-13/layout.png)

## 使用与边界

选择对象后点修改工具，按命令行提示指定基点及目标点；未预选时先选对象按 Enter。旋转可在基点后输入角度，缩放可输入比例。圆角/倒角选两条相交直线，输入半径或距离并应用。Esc 取消当前本地绘制，已成功保存的图元不受影响。

纸空间目前用于布局查看、新建视口及出图；模型编辑工具禁用。直线修剪使用其他相交直线为边界；端点拉伸支持直线/多段线；多边形当前为正六边形。图元组支持完整 DXF 持久化，含 GROUP 对象的 DWG 输出仍受既有完整性核验约束，不能保证保留时明确拒绝。此次交付不是对所有 CurrentCAD 命令、所有 DWG 类型完全一致的声明。

## 生产发布

- 应用提交 `8a5a87bffc7d0611e1f64b94631ff5dd5b01146d`，已推送 `origin/V2-detail`。本页和公网证据是发布后追加文档，不更改冻结候选。
- 发布 `native-cad-ribbon-20260913`，冻结源码/构建 SHA256 `0f88d8bc5b8a58903dae3d1028fba904a37665cae955482387f4902d57b404e7`。API/Web 实际镜像和运行时间见 [运行证据](evidence/native-cad-ribbon-public-2026-09-13/runtime.json)。所有部署应用源码、首页与静态资源逐哈希匹配；cad-egress 容器和启动时间未改变。
- Linux 候选通过真实二进制 DWG 写回、原生尺寸/块/纸空间保留、STEP 实体回读、中文 PDF、认证及交付归档；新增椭圆/组/视口/DXF/SVG/重开/撤销重做7项通过。既有发布保护相关163项测试通过；新增环境更新检查拒绝重复键及未知 overlay，保留其他配置。
- 激活沿用维护门禁、无运行任务检查、离线备份、逐哈希部署、镜像验证与失败回滚；备份 `/opt/joyniu-backups/scheduled/currentcad-9ed4b6427c344386b91598a2ecc36c3d`，最终 `completed`、维护关闭、API healthy。
- 公网实际浏览器验证11项流程及5项图形交互通过；390 px 页面无横向溢出，JS错误0；下载DXF包含8个图元、2个原生椭圆、1个组及真实模型视口。公网HTTP刷新后需重新登录，验收经过重新登录重开同一ID/修订。
- [公网浏览器](evidence/native-cad-ribbon-public-2026-09-13/browser-verification.json) · [鼠标/圆角/旋转缩放](evidence/native-cad-ribbon-public-2026-09-13/graphical-verification.json) · [公网桌面](evidence/native-cad-ribbon-public-2026-09-13/desktop.png) · [手机](evidence/native-cad-ribbon-public-2026-09-13/mobile.png) · [DXF独立回读](evidence/native-cad-ribbon-public-2026-09-13/dxf-readback.json)

专用公网测试账号已停用并撤销所有会话；临时密码文件已删除。仅使用合成图纸，未修改客户图纸。
