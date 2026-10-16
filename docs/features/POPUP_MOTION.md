# 子窗口动效

参考 2026-09-24 查阅的 [Cherry Studio Dialog](https://github.com/CherryHQ/cherry-studio/blob/main/packages/ui/src/components/primitives/dialog.tsx) 与 [设计约定](https://github.com/CherryHQ/cherry-studio/blob/main/DESIGN.md)。使用 Motion 的退出挂载机制，统一处理进入、退出和快速重新打开。

| 层级 | 进入 | 退出 |
| --- | --- | --- |
| 居中子弹窗 | 从下方 16px、99% 缩放、透明开始，260ms | 向下 16px、99% 缩放、淡出，200ms |
| 右侧抽屉 | 从右侧滑入，240ms；保持不透明和原尺寸 | 向右侧滑出，160ms |
| 子弹窗遮罩 | 淡入 220ms | 淡出 200ms |
| 账户、项目操作、模型菜单 | 从下方 8px 淡入，180ms | 向下淡出，140ms |

进入曲线为 `cubic-bezier(.16, 1, .3, 1)`，退出为 `cubic-bezier(.4, 0, 1, 1)`。系统设置 `prefers-reduced-motion: reduce` 时停用动画。

上浮淡入用于模型服务及内部确认、项目创建/编辑/删除、数据集导入选择、独立构图窗口、CSV 组合配置、技能详情，以及各菜单。帮助、记忆、技能和数据集的主面板使用右侧抽屉，不上浮、不缩放、不淡入；抽屉内的小弹窗使用上浮淡入。浏览器原生的删除确认和数据集重命名也改为应用内对话框。整页构图工作区和页面切换不使用弹窗动画。

## 接入规则

- 侧边面板使用 `DrawerShell` 的横向滑动，避免套用 `Popup` 或让遮罩透明度连带影响面板。
- `PopupPresence` 放在条件渲染外侧；居中子窗口本体使用 `Popup.div/section/form/dialog`，遮罩使用 `PopupBackdrop`。
- Portal 放在被保留的窗口组件内部；或者将 `PopupPresence` 放在 Portal 内部。不要将 React Portal 直接作为 Presence 子元素。
- 退出中的元素保留到动画完成，并设置 `inert` 和 `aria-hidden`。关闭回调立即更新业务状态，不使用定时器延迟业务操作。
- 原生 `<dialog>` 在退出卸载时关闭，并恢复触发按钮的焦点；嵌套确认保留浏览器顶层焦点隔离。
- 删除/重命名使用 `useActionDialog`。取消、账号切换、所属面板退出或卸载时，挂起的确认均取消；调用方仍检查账号及重复请求。

## 验证

完整前端测试 512 项通过；焦点修复后另行通过相关 11 项测试和生产构建。

实际 Chromium 验证了窗口从 `opacity: 0 / y: 16` 到 `opacity: 1 / y: 0`、退出清除遮罩、35ms 间隔的连续开关、嵌套确认取消及两层焦点恢复、测试技能删除、390×844 深色布局，以及减少动态效果下无位移。测试使用临时账号，结束后删除其项目和技能。前端已重建并部署到本地 Compose 项目 `igraph`。

侧栏范围修正：37 项相关测试通过。浏览器逐帧确认帮助、记忆和技能面板仅横向滑动，透明度保持 1，纵向位移为 0、无缩放；内部技能详情仍上浮淡入。
