# Checklist

- [ ] 冻结态 Velopack 布局：exe 位于 `<安装根>\current\` 时，`DATA_ROOT` = 安装根（models/tools/third_party/.venv-ml/.venv-tts 与 current 同级），`APP_ROOT` = current（资源、runtime/python 路径正确）
- [ ] 冻结态裸 onedir（-SkipInstaller）：行为与旧版一致（DATA_ROOT = exe 目录），本地测试不受影响
- [ ] 开发态：`DATA_ROOT` = 项目根上一级；启动 `python app/main.py` 正常运行
- [ ] `velopack.App().run()` 是 main.py 中先于 QApplication 的首个启动逻辑
- [ ] 启动后台自动检查不阻塞 UI；发现新版本弹窗（版本号 + release notes）→ 确认 → 下载有进度反馈 → 自动重启完成升级
- [ ] 设置窗口主题下拉右侧有「检查更新」按钮；已是最新有提示；非 Velopack 环境优雅降级不抛异常
- [ ] 两个任务设置面板输出目录默认为空（placeholder 提示），不再预填 `config.OUTPUT_DIR`
- [ ] 任务开始时输出目录为空 → 提示 + 目录选择 → 回填并持久化；取消选择则任务不开始
- [ ] 输出目录设置重启后保留（预填上次值）
- [ ] 新增 UI 文案均有 zh/en 双语（i18n）
- [ ] `requirements.txt` 与 `build/requirements-freeze.txt` 含 `velopack`，冻结构建后 velopack 原生库随包可用
- [ ] `build_windows.ps1` 完整构建产出：onedir + `update-feed`（releases.win.json、全量 nupkg、Setup.exe）+ Inno 首装包；feed 中 Setup.exe 已被 Inno 首装包替换
- [ ] `installer_velopack.iss` 保留磁盘空间检测/分区选择；内嵌 Setup.exe 经 `--installto "{app}" --silent` 装入用户所选目录
- [ ] 旧 `build/installer.iss` 已删除
- [ ] release.yml：tag 与 APP_VERSION 不一致时构建失败；`vpk download github` 拉取上一版生成增量包；`vpk upload github --publish` 发布 Release；softprops 步骤已移除
- [ ] 更新链路实测：旧版 → 检查 → 升级到新版后 current 被替换、数据目录完整保留、venv 仍可用、快捷方式（指向启动桩）正常启动
- [ ] README 已更新：.NET SDK + vpk 前置、新目录布局、开发机存量数据迁移指引（models/tools/third_party/.venv-ml/.venv-tts 移至项目根上一级，output 可选，venv 失效重建）
