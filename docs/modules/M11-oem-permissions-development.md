# M11 OEM 与权限：详细开发文档

## 固定适配层

```text
device/.../PermissionState.kt
device/.../OemProfile.kt
device/.../PermissionGuideActivity.kt
device/.../BackgroundKeepAliveGuide.kt
```

## OemProfile

识别顺序固定：`Build.MANUFACTURER` → `Build.BRAND` → `Build.VERSION.SDK_INT`。只允许映射到：`STANDARD/HUAWEI/XIAOMI/OPPO/VIVO/MEIZU/UNKNOWN`。

未知品牌使用标准 Android 路径，不猜测厂商设置 Intent。

## 权限状态

统一返回：

```text
GRANTED
DENIED
NOT_APPLICABLE
BLOCKED_BY_OEM
REQUIRES_USER_ACTION
```

系统设置页 Intent 失败时返回 `BLOCKED_BY_OEM`，并显示手动路径。

## 后台策略

- 不使用 Root。
- 不要求 Shizuku/ADB 作为首版依赖。
- 后台服务只能使用 Android 合法前台服务类型。
- 电池优化仅作为用户可选引导，不能静默修改系统设置。

## 测试矩阵

每个 OEM 记录：型号、系统版本、API、权限前后状态、服务存活、WSS 重连、悬浮窗、相机、Photo Picker、语音和短信结果。

## 禁止项

- 禁止为单个 OEM 引入未经验收的常驻插件。
- 禁止通过无障碍点击系统设置页来“自动授权”。
- 禁止把 OEM 特例散落在命令执行器；全部进入 `OemProfile`。
