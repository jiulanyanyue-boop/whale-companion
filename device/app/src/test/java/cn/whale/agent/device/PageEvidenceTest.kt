package cn.whale.agent.device

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class PageEvidenceTest {
    private fun node(description: String, scrollable: Boolean = true) = UiNodeRef("target", "hash", "ScrollView", "", description, 0, 0, 1080, 2166, false, false, scrollable)

    @Test fun explicitSecondPageIsReportedWithVerifiedTarget() {
        val evidence = pageEvidence(listOf(node("第 2 屏，共 3 屏")))
        assertEquals("2", evidence["current_page"])
        assertEquals("3", evidence["total_pages"])
        assertEquals("target", evidence["page_target_id"])
    }
    @Test fun englishPageAndInvalidIndicators() {
        assertEquals("2", pageEvidence(listOf(node("Page 2 of 4")))["current_page"])
        assertTrue(pageEvidence(listOf(node("第 5 屏，共 3 屏"))).isEmpty())
        assertTrue(pageEvidence(listOf(node("第 2 屏，共 3 屏", false))).isEmpty())
        assertTrue(pageEvidence(listOf(node("Home screen"))).isEmpty())
    }
    @Test fun actualScrollPositionOverridesStaleDescriptionAndDetectsAnimation() {
        val stale = node("第 1 屏，共 3 屏")
        val second = pageEvidence(listOf(stale), mapOf("target" to (1080 to 0)))
        assertEquals("2", second["current_page"])
        assertEquals("true", second["page_settled"])
        assertEquals("scroll_event", second["page_position_source"])
        assertEquals("false", pageEvidence(listOf(stale), mapOf("target" to (800 to 0)))["page_settled"])
        assertEquals("3", pageEvidence(listOf(stale), mapOf("target" to (2160 to 0)))["current_page"])
    }
}
