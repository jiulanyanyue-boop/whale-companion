# M07 UI 感知与无障碍：详细开发文档

## 固定类

```text
device/.../WhaleAccessibilityService.kt
device/.../AccessibilityController.kt
device/.../UiSnapshotStore.kt       # 可从当前 Controller 拆出
```

## Snapshot

每次 observe 生成：

```text
snapshot_id
package_name
window_id
node_count
nodes[target_id,selector_hash,class,text,contentDescription,bounds,clickable,editable,scrollable]
```

`snapshot_id` 只在当前进程和短期缓存内有效，最多保留 8 个。节点最多 3000 个，树深度最多 40。

## Target fingerprint

`selector_hash` 固定由以下字段 canonical 拼接后 SHA-256 截取 24 字符：

```text
className | text | contentDescription | bounds
```

`target_id` 固定由 `treePath | selector_hash` 生成。动作执行前重新取得当前节点并校验：snapshot、target、selector、expected_package、当前前台包。

## 动作顺序

1. `AccessibilityNodeInfo.performAction`。
2. 点击时尝试可点击祖先节点。
3. 只有目标有有效 bounds 才允许 Gesture fallback。
4. 长按使用固定 300 到 30000 ms。
5. 滚动优先 `ACTION_SCROLL_FORWARD/BACKWARD`，失败才使用受限手势。
6. 动作后重新 observe，不能只返回“已点击”。

## 安全防注入

注入期间：

- 确认卡和停止按钮不允许被自动手势命中。
- 目标窗口必须仍属于 expected package。
- 屏幕熄灭、锁屏、无障碍服务解绑立即停止任务。

## 视觉 fallback

设备只在以下条件同时满足时启用：树节点少于 5、应用能力档案声明需要视觉、当前页面非敏感页面。视觉模式必须返回 `evidence.mode=vision`，并限制图片尺寸、数量和保存时间。

## 禁止项

- 禁止把 node index 当成稳定 ID。
- 禁止默认每一步截图。
- 禁止对 FLAG_SECURE 页面截图。
- 禁止向模型发送密码字段、验证码字段或完整剪贴板。
