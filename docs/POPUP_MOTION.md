# 子窗口动效

参考 2026-09-24 查阅的 [Cherry Studio Dialog](https://github.com/CherryHQ/cherry-studio/blob/main/packages/ui/src/components/primitives/dialog.tsx) 与 [设计约定](https://github.com/CherryHQ/cherry-studio/blob/main/DESIGN.md)。使用 Motion 的退出挂载机制，统一处理进入、退出和快速重新打开。

| 层级 | 进入 | 退出 |
| --- | --- | --- |
| 弹窗、抽屉 | 从下方 16px、99% 缩放、透明开始，260ms | 向下 16px、99% 缩放、淡出，200ms |
| 遮罩 | 淡入 220ms | 淡出 200ms |
| 账户、项目操作、模型菜单 | 从下方 8px 淡入，180ms | 向下淡出，140ms |

进入曲线为 `cubic-bezier(.16, 1, .3, 1)`，退出为 `cubic-bezier(.4, 0, 1, 1)`。系统设置 `prefers-reduced-motion: reduce` 时停用动画。

覆盖模型服务及内部确认、项目创建/编辑/删除、数据集抽屉和导入选择、独立构图窗口、CSV 组合配置、记忆与技能、技能详情、帮助，以及各菜单。浏览器原生的删除确认和数据集重命名也改为应用内对话框。整页构图工作区和页面切换不使用弹窗动画。

## 接入规则

- `PopupPresence` 放在条件渲染外侧；窗口本体使用 `Popup.div/section/form/dialog`，遮罩使用 `PopupBackdrop`。
- Portal 放在被保留的窗口组件内部；或者将 `PopupPresence` 放在 Portal 内部。不要将 React Portal 直接作为 Presence 子元素。
- 退出中的元素保留到动画完成，并设置 `inert` 和 `aria-hidden`。关闭回调立即更新业务状态，不使用定时器延迟业务操作。
- 原生 `<dialog>` 在退出卸载时关闭，并恢复触发按钮的焦点；嵌套确认保留浏览器顶层焦点隔离。
- 删除/重命名使用 `useActionDialog`。取消、账号切换、所属面板退出或卸载时，挂起的确认均取消；调用方仍检查账号及重复请求。

## 验证

完整前端测试 512 项通过；焦点修复后另行通过相关 11 项测试和生产构建。

实际 Chromium 验证了窗口从 `opacity: 0 / y: 16` 到 `opacity: 1 / y: 0`、退出清除遮罩、35ms 间隔的连续开关、嵌套确认取消及两层焦点恢复、测试技能删除、390×844 深色布局，以及减少动态效果下无位移。测试使用临时账号，结束后删除其项目和技能。前端已重建并部署到本地 Compose 项目 `igraph`。
