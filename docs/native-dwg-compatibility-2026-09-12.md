# 原生二维 DWG 写入验收（2026-09-12）

本轮从“所有 DIMENSION / INSERT / 纸空间一律拒绝”改为真实转换和内容核验。该结论是本机与构建包验证，不代表公网部署或 AutoCAD 跨厂商认证。

## 已验证内容

- LibreDWG 0.14，输出 R2000 / AC1015；所有成功文件都经真实 DWG 读取。
- 线性、对齐、半径、直径、三点角度标注：保持 DIMENSION 类型、句柄、定义点、测量值、公差及文字块，不炸开。
- 普通中文块、非零基点、旋转、X/Y/Z 缩放；层色、线型和线宽；多段线逐段宽度与圆弧凸度。
- 多个纸空间保留中文正文、纸张/边距/视口几何和主视口身份；重新打开后可选择指定布局导出 PDF。
- 应用标注关联 XDATA 跨 DWG 导出/重新导入保持，修改源直线后仍更新同一标注。
- 真实检验图 `docs/evidence/currentcad-ui-2026-09-12/inspection.dxf` 已生成同目录 `inspection.dwg`：测量 150、公差 +0.2/-0.1、气泡 7、源直线 8D、DIMENSION 8E 的关联 XDATA 与气泡圆 B0 的 `JOYNIU_INSPECTION_BALLOON` 均保留。

## 已定位并修复的 LibreDWG 0.14 兼容问题

1. 使用 UTF-8 的较新 DXF 向 R2000 写入时中文可能乱码：传输副本采用 R2000 / ANSI_936，原 DXF 不改写；无法等价编码的内容由回读差异阻断。
2. MTEXT 旋转 code 50 导致解析错误：用等价方向向量 code 11；字高、宽度及行距由原数值恢复。
3. 斜线标注角度、INSERT 的 Z 缩放、块基点/匿名标记、多段线逐段宽度、居中文字对齐点发生丢失：独立 C 适配器恢复确切源字段后重新写入真实 DWG。
4. 三点角度标注的未使用、全零旧版 code 16 会触发转换器 abort：只省略该无效旧版占位字段。实际三顶点 13/14/15 保持。非零旧字段明确拒绝。
5. 多个应用的 XDATA 被合并到首个 APPID：适配器按源应用边界重建 EED 分组；实际数据不改写。
6. 读取 DWG 时，CLI 生成的 DXF 会遗漏零值标注样式、匿名块位、文字对齐的隐式插入点：从同一真实 DWG 的 JSON 结构恢复这些确切字段，完全不使用“期望原图”掩盖回读错误。
7. DWG 不保存 DXF 的视口编号/激活序号，读取器全局重编号使第二张布局主视口身份错乱：将原状态作为 `JOYNIU_NATIVE_VIEWPORT_STATE` XDATA 随真实视口保存，重开恢复。原生布局关系和视口几何保留。

比较涵盖受支持图元的 DXF 属性、顶点/宽度数组、文本与本应用 XDATA、完整图层/文字/标注样式、线型图案、各块和各布局。省略序列化缓存句柄、字体文件可推导的默认字体族缓存，以及仅用于尚不支持的弧长标注的样式字段 DIMARCSYM；实际字体、命名引用、尺寸值、显示文字和几何仍比较。未支持的类型或专有关系明确拒绝，不静默删除。

## 依赖、打包与部署边界

- API wheel 包含 `app/native_dwg.py` 与 `app/native_drawing_libredwg.c`；已实际通过 PEP 517/hatchling 构建 wheel 并逐字节核对包内文件。
- macOS 已用系统 `cc`、Homebrew 的头文件和 `libredwg.a` 成功静态链接并运行适配器。运行时优先探测预编译 `joyniu-native-dwg-adapter`；本地开发才使用临时编译后备路径。
- Dockerfile 的 dwg-builder 复用锁定版本与 SHA256 的 LibreDWG，额外安装 `dxf2dwg`，以 libtool 链接静态库编译适配器。runtime 仅复制可执行文件；构建阶段执行版本烟测。`COPY apps/api/app` 同样包含 C 源文件。
- Linux ARM 已实际完成 configure、make 和静态链接。四个工具的 `ldd` 仅依赖 libc/libm 和系统加载器，不依赖 `libredwg.so`；复制进不含 `cc` 的 `python:3.12-slim-bookworm` 后，四工具版本检查与真实转换均通过。
- 商业薄镜像需要基于新工具齐全的基础镜像，不能依赖旧镜像仅更新 Python 文件。本轮没有部署、重启或替换运行容器。

## 具体剩余边界

- 带 `ATTDEF/ATTRIB` 的属性块：本机 converter 的简单英文属性块样本可触发 SIGSEGV；当前明确报告属性实体类型。普通 INSERT 块已通过。
- HATCH、代理图元、实体扩展字典、自定义非图形对象和材质等尚未建立完整写入核验。导出保留原定义的 DXF，或用原 CAD/另行配置的兼容写入器处理。
- 写入版本为 R2000，完整任意版本 DWG/动态块/行业插件对象不在已验证范围。没有伪造 DWG、交付转换失败文件或把转换缓存当作原图真值。

## 本轮验证结果

- `tests/test_native_dwg.py`、`test_native_drawing.py`、`test_native_drawing_api.py`、`test_native_drawing_bubbles.py`、`test_native_parametric_library.py`：合计 **50 项通过**。其中 10 项 DWG 专项使用本机真实转换器，涵盖读回被篡改时阻止交付。
- `nativeDrawingDraft.test.js`、`nativeDrawingModel.safety.test.js`：**12 项通过**；对齐标注正负侧、源关联、布局切换、公式及几何约束边界。
- wheel 构建成功；C 适配器使用本机 `libredwg.a` 静态链接后 `--version` 运行成功。
- 第一次直接执行正式 Dockerfile 时，GNU 下载连续 4 次 TLS 超时（每次 20 秒，exit 28）。随后找到已有官方 `libredwg-0.14.tar.xz` 缓存，SHA256 **62ebb73b984f865960f20ed26619ea5f8789d5e3fd088fa40a2598384da81275** 与 Dockerfile 精确一致。临时构建上下文只把 curl 改为 COPY 缓存归档，保留原 SHA 校验与全部 configure/make/link 命令，最终 **exit 0**。正式 Dockerfile 仍要求能下载锁定的 GNU 源码；未修改代理、网络或库版本。
- Docker 29.5.2 / Linux aarch64：builder 镜像 `sha256:5dbeed10d4a498e4333aa1232c728bba5de2f55fc584b1635c3d75f5e3e9fbd3` 成功。随后构建只复制四工具的 slim runtime，无编译器、无网络运行真实往返，非法句柄 patch 被拒绝且不产生输出文件。
- Linux runtime 生成 **23,442 字节**真实 DWG，SHA256 **3d8b089d8c4f9291d783d1636af48123116f2412bde5096432f4093e8bc9b179**。全图内容比较一致：尺寸 150、公差 +0.2/-0.1、DIMENSION 8E → 直线 8D、气泡 7、中文块与 XYZ 缩放、宽线/凸度、两张纸空间；本机独立重开后指定第二布局 PDF 导出成功。CLI 日志仍有默认材质/匿名块诊断；回读审计无错误，仅清理三个 R2000 默认材质占位对象，自定义材质仍明确拒绝。
- 持久证据位于 `docs/evidence/native-dwg-linux-2026-09-12/`：源 DXF、Linux 输出 DWG、第二布局 PDF、构建/ldd/运行日志及 `verification.json`。本轮已验证 Linux 工具链与薄 runtime；未声称完整 API 镜像构建、公网部署或 AutoCAD 认证通过，未部署或重启服务。
