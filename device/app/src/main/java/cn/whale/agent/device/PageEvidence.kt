package cn.whale.agent.device

/** Extract explicit accessibility page indicators without inferring from pixels or time. */
fun pageEvidence(nodes: List<UiNodeRef>, scrollPositions: Map<String, Pair<Int, Int>> = emptyMap()): Map<String, String> {
    for (node in nodes) {
        if (!node.scrollable) continue
        val match = Regex("第\\s*(\\d+)\\s*[屏页].*?(?:共|总共)\\s*(\\d+)\\s*[屏页]|(?:page|screen)\\s+(\\d+)\\s+of\\s+(\\d+)", RegexOption.IGNORE_CASE).find(node.content_description + " " + node.text) ?: continue
        val current = (match.groupValues[1].ifEmpty { match.groupValues[3] }).toIntOrNull() ?: continue
        val total = (match.groupValues[2].ifEmpty { match.groupValues[4] }).toIntOrNull() ?: continue
        if (current !in 1..total || total > 100) continue
        val position = scrollPositions[node.target_id]
        val horizontalPage = position?.takeIf { it.first >= 0 && it.second == 0 && node.right > node.left }
            ?.let { it.first.toDouble() / (node.right - node.left) }
        val actualPage = horizontalPage?.let { kotlin.math.round(it).toInt() + 1 }?.takeIf { it in 1..total } ?: current
        return mapOf(
            "current_page" to actualPage.toString(), "total_pages" to total.toString(),
            "page_target_id" to node.target_id, "page_selector_hash" to node.selector_hash,
            "page_position_source" to if (horizontalPage != null) "scroll_event" else "node_description",
            "page_settled" to (horizontalPage == null || kotlin.math.abs(horizontalPage - kotlin.math.round(horizontalPage)) < 0.01).toString(),
        )
    }
    return emptyMap()
}
