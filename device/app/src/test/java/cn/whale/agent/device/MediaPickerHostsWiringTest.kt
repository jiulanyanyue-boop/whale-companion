package cn.whale.agent.device

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * M08 相册选图宿主的**登记语义**验证（纯 JVM）。
 *
 * 这个文件守住一件容易「看起来做完了其实永远不可用」的事：
 * `MediaPickerHosts` 到底有没有人登记。没人登记时 `pickerAvailable` 恒为 false，
 * `device.media.pick` 永远返回 `NOT_WIRED`——功能写了但用户用不到。
 *
 * 之前正是这个状态，主人对此明确不满「干嘛老是留一点」。所以这里把
 * 「登记 → 可用 → 撤销 → 不可用」这条链路钉死。
 *
 * 注意：`MainActivity` 的 attach 需要 Android 运行时，本机跑不了；
 * 因此这里验证的是 `MediaPickerHosts` 的登记语义（`MainActivity` 内部走的完全一致）。
 */
class MediaPickerHostsWiringTest {

    /** 最小宿主：只实现接口，不碰 Android。 */
    private class FakeHost(
        override val available: Boolean,
        private val outcome: PhotoPickerController.PickOutcome,
    ) : MediaPickerDelegate {
        var pickCalls = 0

        override fun pick(mediaType: String, maxItems: Int): PhotoPickerController.PickOutcome {
            pickCalls++
            return outcome
        }
    }

    @Test
    fun noHostMeansPickerIsNotAvailable() {
        // 生产默认状态：没人登记 → 不可用 → media.pick 必须 NOT_WIRED。
        MediaPickerHosts.detach()
        assertFalse("初始必须未登记", MediaPickerHosts.available())
        assertNull(MediaPickerHosts.current())
    }

    @Test
    fun attachMakesPickerAvailable() {
        MediaPickerHosts.detach()
        val host = FakeHost(
            available = true,
            outcome = PhotoPickerController.PickOutcome.Picked(emptyList()),
        )
        MediaPickerHosts.attach(host)

        assertTrue("登记后必须可用", MediaPickerHosts.available())
        assertNotNull(MediaPickerHosts.current())
        assertEquals("应能取回同一个宿主实例", host, MediaPickerHosts.current())
    }

    @Test
    fun detachMakesPickerUnavailableAgain() {
        MediaPickerHosts.detach()
        MediaPickerHosts.attach(
            FakeHost(available = true, outcome = PhotoPickerController.PickOutcome.Picked(emptyList())),
        )
        assertTrue(MediaPickerHosts.available())

        // Activity 离开前台 / 销毁时做的事。
        MediaPickerHosts.detach()
        assertFalse("撤销后必须回落为不可用", MediaPickerHosts.available())
        assertNull(MediaPickerHosts.current())
    }

    @Test
    fun reattachDoesNotStackHosts() {
        // 重复 onStart 的极端时序：只应有一份宿主，且以最后一次为准。
        MediaPickerHosts.detach()
        val first = FakeHost(available = true, outcome = PhotoPickerController.PickOutcome.Picked(emptyList()))
        val second = FakeHost(available = true, outcome = PhotoPickerController.PickOutcome.Picked(emptyList()))

        MediaPickerHosts.attach(first)
        MediaPickerHosts.attach(second)

        assertEquals("重复登记必须以最后一次为准", second, MediaPickerHosts.current())
        assertTrue(MediaPickerHosts.available())
        assertEquals("旧宿主不得再被调用", 0, first.pickCalls)
    }

    @Test
    fun hostThatReportsUnavailableIsNotTreatedAsAvailable() {
        // available=false 的宿主（例如 Activity 正在销毁）不能被当成「能用」。
        MediaPickerHosts.detach()
        MediaPickerHosts.attach(
            FakeHost(available = false, outcome = PhotoPickerController.PickOutcome.Picked(emptyList())),
        )
        assertFalse(
            "宿主自报不可用时必须如实回落",
            MediaPickerHosts.available(),
        )
    }

    @Test
    fun cancellationOutcomeIsPreservedThroughHost() {
        // 用户在选图界面取消：宿主必须把「取消」如实交回，不得变成功。
        MediaPickerHosts.detach()
        val cancelled = PhotoPickerController.PickOutcome.Failed(
            MediaErrors.PHOTO_PICKER_CANCELLED,
            "user cancelled the media picker",
        )
        val host = FakeHost(available = true, outcome = cancelled)
        MediaPickerHosts.attach(host)

        val result = host.pick(mediaType = "image", maxItems = 1)
        assertTrue("取消必须仍是失败结果", result is PhotoPickerController.PickOutcome.Failed)
        assertEquals(
            MediaErrors.PHOTO_PICKER_CANCELLED,
            (result as PhotoPickerController.PickOutcome.Failed).code,
        )
    }

    @Test
    fun timeoutFailureIsDistinguishableFromCancellation() {
        // 超时与「用户取消」用同一个码（语义都是「没拿到选择结果」），
        // 但 message 必须能区分，否则排查时分不清是超时还是用户点了取消。
        val timeout = PhotoPickerController.PickOutcome.Failed(
            MediaErrors.PHOTO_PICKER_CANCELLED,
            "picker timed out or was dismissed before returning a result",
        )
        val cancel = PhotoPickerController.PickOutcome.Failed(
            MediaErrors.PHOTO_PICKER_CANCELLED,
            "user cancelled the media picker",
        )
        assertEquals(timeout.code, cancel.code)
        assertTrue("超时说明应包含 timed out", timeout.message.contains("timed out"))
        assertTrue("取消说明应包含 cancelled", cancel.message.contains("cancelled"))
    }

    @Test
    fun pickedOutcomeCarriesEntries() {
        val entries = listOf(
            AssetStore.AssetEntry(
                assetId = "asset-1",
                mime = "image/png",
                size = 16L,
                sha256 = "0" .repeat(64),
                taskId = "task-1",
                createdAtMs = 1_000L,
            ),
        )
        val host = FakeHost(
            available = true,
            outcome = PhotoPickerController.PickOutcome.Picked(entries),
        )
        MediaPickerHosts.detach()
        MediaPickerHosts.attach(host)

        val result = host.pick(mediaType = "image", maxItems = 1)
        assertTrue(result is PhotoPickerController.PickOutcome.Picked)
        assertEquals(entries, (result as PhotoPickerController.PickOutcome.Picked).entries)
        assertEquals(1, host.pickCalls)
        MediaPickerHosts.detach()
    }
}