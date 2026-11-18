# M08 相机、相册与媒体：详细开发文档

## 固定类

```text
device/.../CameraController.kt
device/.../PhotoPickerController.kt
device/.../MediaUploadController.kt
device/.../MediaStoreController.kt
host/astrbot_whale_device/media.py
```

## CameraX

- 使用 `ProcessCameraProvider`。
- 用 `ImageCapture` 保存到 app cache 下随机文件名。
- 默认 `LENS_FACING_BACK`、JPEG、标准质量。
- 只把文件复制到受控 `AssetStore`，不把真实路径发给模型。
- 拍照完成后返回 `asset_id`、mime、size、sha256。

## Photo Picker

API 33+ 使用 `ActivityResultContracts.PickVisualMedia` 或多选合同。API 26-32 使用 `ACTION_OPEN_DOCUMENT`，必须带 `CATEGORY_OPENABLE` 和 `FLAG_GRANT_READ_URI_PERMISSION`。

URI 只保存到当前任务资产表，不能转成全局媒体扫描。

## 上传协议

固定请求：

```text
POST /v1/devices/{device_id}/assets/{asset_id}/chunks/{index}
Content-Range: bytes start-end/total
X-Asset-Sha256: sha256
```

分块固定 1 MiB。服务端必须校验 device identity、asset id、offset、总大小和 hash。上传完成后才返回 `upload_complete`。

## AssetStore

素材 ID 只允许 `[A-Za-z0-9._-]{1,160}`。禁止 `..`、斜杠、反斜杠和用户路径。媒体类型只允许 `image/*`、`audio/*`、`video/*`。

## 失败状态

```text
CAMERA_PERMISSION_REQUIRED
PHOTO_PICKER_CANCELLED
MEDIA_TOO_LARGE
MEDIA_TYPE_NOT_ALLOWED
UPLOAD_RETRYABLE
UPLOAD_HASH_MISMATCH
MEDIA_NOT_FOUND
```

## 禁止项

- 禁止通过 WebSocket 传大文件。
- 禁止扫描完整相册生成索引。
- 禁止接受模型给出的本地文件路径。
- 禁止把媒体上传重试变成重复 OneBot 发送。
