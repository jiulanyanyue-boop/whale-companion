package cn.whale.agent.device

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * M04 固定测试：四级确认级别 + 不可降低清单（纯 JVM，JUnit4）。
 *
 * 对应 `docs/modules/M04-owner-security-product.md`「确认级别」：
 *
 * ```text
 * DENY
 * CONFIRM_EVERY_STEP
 * CONFIRM_FIRST_STEP
 * ALLOW_NORMAL
 * ```
 *
 * 系统设置、安装器、短信发送、删除、发布、支付、凭据输入固定至少 `CONFIRM_EVERY_STEP`，
 * **不能被人设或模型降低**。
 */
class ConfirmLevelPolicyTest {

    private fun payload(vararg pairs: Pair<String, String>): JsonObject = buildJsonObject {
        pairs.forEach { (key, value) -> put(key, value) }
    }

    // ---------- 1. 四个级别与严格度顺序 ----------

    @Test
    fun fourFixedLevelsInDocumentOrder() {
        assertEquals(
            listOf("DENY", "CONFIRM_EVERY_STEP", "CONFIRM_FIRST_STEP", "ALLOW_NORMAL"),
            ConfirmLevel.entries.map { it.name },
        )
    }

    @Test
    fun strictnessOrderAndAtLeastPickTheStricterOne() {
        assertTrue(ConfirmLevel.DENY.strictness > ConfirmLevel.CONFIRM_EVERY_STEP.strictness)
        assertTrue(ConfirmLevel.CONFIRM_EVERY_STEP.strictness > ConfirmLevel.CONFIRM_FIRST_STEP.strictness)
        assertTrue(ConfirmLevel.CONFIRM_FIRST_STEP.strictness > ConfirmLevel.ALLOW_NORMAL.strictness)

        assertEquals(
            ConfirmLevel.CONFIRM_EVERY_STEP,
            ConfirmLevel.ALLOW_NORMAL.atLeast(ConfirmLevel.CONFIRM_EVERY_STEP),
        )
        assertEquals(
            ConfirmLevel.CONFIRM_EVERY_STEP,
            ConfirmLevel.CONFIRM_EVERY_STEP.atLeast(ConfirmLevel.CONFIRM_FIRST_STEP),
        )
        assertEquals(
            ConfirmLevel.DENY,
            ConfirmLevel.CONFIRM_EVERY_STEP.atLeast(ConfirmLevel.DENY),
        )
    }

    // ---------- 2. parseOrDeny fail-closed ----------

    @Test
    fun parseOrDenyRejectsIllegalValues() {
        for (raw in listOf(null, "", "   ", "allow_normal", "ALLOW-ALL", "ALLOW_ALL", "yolo", "confirm")) {
            assertEquals("非法值必须 fail-closed: $raw", ConfirmLevel.DENY, ConfirmLevel.parseOrDeny(raw))
        }
    }

    @Test
    fun parseOrDenyAcceptsExactNames() {
        assertEquals(ConfirmLevel.DENY, ConfirmLevel.parseOrDeny("DENY"))
        assertEquals(ConfirmLevel.CONFIRM_EVERY_STEP, ConfirmLevel.parseOrDeny("CONFIRM_EVERY_STEP"))
        assertEquals(ConfirmLevel.CONFIRM_FIRST_STEP, ConfirmLevel.parseOrDeny("CONFIRM_FIRST_STEP"))
        assertEquals(ConfirmLevel.ALLOW_NORMAL, ConfirmLevel.parseOrDeny("ALLOW_NORMAL"))
        // 允许首尾空白，但大小写必须完全一致。
        assertEquals(ConfirmLevel.ALLOW_NORMAL, ConfirmLevel.parseOrDeny("  ALLOW_NORMAL "))
    }

    // ---------- 3. 高风险命令权威清单 ----------

    @Test
    fun highRiskCommandSetMatchesDocumentExactly() {
        assertEquals(
            setOf(
                "device.sms.send",
                "device.system.settings",
                "device.media.share",
                "device.ui.input",
                "device.ui.tap",
            ),
            ConfirmLevelPolicy.HIGH_RISK_COMMANDS,
        )
        for (command in ConfirmLevelPolicy.HIGH_RISK_COMMANDS) {
            assertTrue("$command 必须是高风险", ConfirmLevelPolicy.isHighRisk(command))
        }
        for (command in listOf("device.app.list", "device.app.launch", "device.ui.observe", "device.ui.back")) {
            assertFalse("$command 不是 M04 高风险命令", ConfirmLevelPolicy.isHighRisk(command))
        }
    }

    @Test
    fun highRiskPolicyCoversOnlyDeclaredCommands() {
        // M04 清单里的每条都必须是 M06 已声明命令，避免「对不存在的命令做确认」。
        for (command in ConfirmLevelPolicy.HIGH_RISK_COMMANDS) {
            assertTrue("$command 未在 DeviceCommands 声明", command in DeviceCommands.ALL)
        }
    }

    // ---------- 4. 系统设置 / 短信发送：命令级锁死每步 ----------

    @Test
    fun systemSettingsAndSmsSendArePinnedToEveryStep() {
        for (command in listOf("device.sms.send", "device.system.settings")) {
            assertEquals(
                "$command 必须固定每步确认",
                ConfirmLevel.CONFIRM_EVERY_STEP,
                ConfirmLevelPolicy.floorFor(command),
            )
            assertTrue(ConfirmLevelPolicy.isPinnedToEveryStep(command))
            // 无论请求级别多宽（包括 ALLOW_NORMAL），都不能降低。
            for (requested in ConfirmLevel.entries) {
                val effective = ConfirmLevelPolicy.effectiveLevel(requested, command)
                assertTrue(
                    "$command 生效级别不得低于每步确认（请求=$requested）",
                    effective.strictness >= ConfirmLevel.CONFIRM_EVERY_STEP.strictness,
                )
            }
        }
    }

    // ---------- 5. 安装器 ----------

    @Test
    fun installerLaunchIsAtLeastEveryStep() {
        val installerPayload = payload("package_name" to "com.android.packageinstaller")
        assertEquals(
            ConfirmLevel.CONFIRM_EVERY_STEP,
            ConfirmLevelPolicy.floorFor("device.app.launch", installerPayload),
        )
        assertEquals(
            ConfirmLevel.CONFIRM_EVERY_STEP,
            ConfirmLevelPolicy.effectiveLevel(ConfirmLevel.ALLOW_NORMAL, "device.app.launch", installerPayload),
        )
        // 普通应用启动不受影响。
        assertEquals(
            ConfirmLevel.ALLOW_NORMAL,
            ConfirmLevelPolicy.effectiveLevel(
                ConfirmLevel.ALLOW_NORMAL,
                "device.app.launch",
                payload("package_name" to "com.example.app"),
            ),
        )
    }

    // ---------- 6. 删除 / 发布 / 付款按钮 ----------

    @Test
    fun destructivePublishPaymentButtonsRequireEveryStep() {
        for (label in listOf("delete_button", "publish_dialog", "payment_confirm", "uninstall_confirm", "submit_order")) {
            val target = payload("expected_package" to "com.example.app", "target_id" to label)
            assertEquals(
                "按钮 $label 必须每步确认",
                ConfirmLevel.CONFIRM_EVERY_STEP,
                ConfirmLevelPolicy.floorFor("device.ui.tap", target),
            )
            assertEquals(
                ConfirmLevel.CONFIRM_EVERY_STEP,
                ConfirmLevelPolicy.effectiveLevel(ConfirmLevel.ALLOW_NORMAL, "device.ui.tap", target),
            )
        }
    }

    // ---------- 7. 凭据输入 ----------

    @Test
    fun credentialFieldsRequireEveryStep() {
        for (field in listOf("password_field", "otp_input", "verification_code", "cvv_box", "card_number_input")) {
            val target = payload("expected_package" to "com.example.app", "target_id" to field)
            assertEquals(
                "凭据字段 $field 必须每步确认",
                ConfirmLevel.CONFIRM_EVERY_STEP,
                ConfirmLevelPolicy.floorFor("device.ui.input", target),
            )
        }
    }

    @Test
    fun plainTextInputIsFirstStepOnly() {
        val search = payload("expected_package" to "com.example.app", "target_id" to "search_box")
        assertEquals(ConfirmLevel.CONFIRM_FIRST_STEP, ConfirmLevelPolicy.floorFor("device.ui.input", search))
        assertEquals(
            ConfirmLevel.CONFIRM_FIRST_STEP,
            ConfirmLevelPolicy.effectiveLevel(ConfirmLevel.ALLOW_NORMAL, "device.ui.input", search),
        )
    }

    // ---------- 8. 媒体外发 ----------

    @Test
    fun mediaShareToExternalTargetRequiresEveryStep() {
        assertEquals(
            ConfirmLevel.CONFIRM_EVERY_STEP,
            ConfirmLevelPolicy.floorFor("device.media.share", payload("asset_id" to "a1", "target" to "com.tencent.mm")),
        )
        assertEquals(
            ConfirmLevel.CONFIRM_FIRST_STEP,
            ConfirmLevelPolicy.floorFor("device.media.share", payload("asset_id" to "a1")),
        )
    }

    // ---------- 9. 拿不到 payload → fail-closed ----------

    @Test
    fun unknownPayloadFailsClosedForHighRiskCommands() {
        for (command in listOf("device.ui.input", "device.ui.tap", "device.media.share")) {
            assertEquals(
                "$command 拿不到 payload 时必须按每步确认",
                ConfirmLevel.CONFIRM_EVERY_STEP,
                ConfirmLevelPolicy.floorFor(command, null),
            )
            assertEquals(
                ConfirmLevel.CONFIRM_EVERY_STEP,
                ConfirmLevelPolicy.effectiveLevel(ConfirmLevel.ALLOW_NORMAL, command, null),
            )
        }
    }

    // ---------- 10. only-tighten-never-widen ----------

    @Test
    fun effectiveLevelOnlyTightensNeverWidens() {
        val search = payload("target_id" to "search_box")
        // 请求更宽 → 被下限顶住。
        assertEquals(
            ConfirmLevel.CONFIRM_FIRST_STEP,
            ConfirmLevelPolicy.effectiveLevel(ConfirmLevel.ALLOW_NORMAL, "device.ui.input", search),
        )
        // 请求更严 → 保持用户的选择，不被放宽。
        assertEquals(
            ConfirmLevel.CONFIRM_EVERY_STEP,
            ConfirmLevelPolicy.effectiveLevel(
                ConfirmLevel.CONFIRM_EVERY_STEP,
                "device.app.launch",
                payload("package_name" to "com.example.app"),
            ),
        )
        // 普通命令 + 请求 DENY → 仍然是 DENY。
        assertEquals(
            ConfirmLevel.DENY,
            ConfirmLevelPolicy.effectiveLevel(ConfirmLevel.DENY, "device.app.launch"),
        )
    }

    @Test
    fun denyIsNeverRelaxed() {
        for (command in ConfirmLevelPolicy.HIGH_RISK_COMMANDS) {
            assertEquals(
                "$command 的 DENY 不得被放宽",
                ConfirmLevel.DENY,
                ConfirmLevelPolicy.effectiveLevel(ConfirmLevel.DENY, command, payload("target" to "x")),
            )
        }
    }

    @Test
    fun highRiskCommandsNeverBecomeAllowNormal() {
        for (command in ConfirmLevelPolicy.HIGH_RISK_COMMANDS) {
            val effective = ConfirmLevelPolicy.effectiveLevel(
                ConfirmLevel.ALLOW_NORMAL,
                command,
                payload("asset_id" to "a", "target" to "com.example.social"),
            )
            assertFalse("$command 不得落到 ALLOW_NORMAL", effective == ConfirmLevel.ALLOW_NORMAL)
        }
    }

    // ---------- 11. 正文类字段不参与判定 ----------

    @Test
    fun payloadBodyTextDoesNotParticipateInJudgement() {
        // 短信正文里出现 "password" 不改变「短信发送固定每步确认」这一事实。
        assertEquals(
            ConfirmLevel.CONFIRM_EVERY_STEP,
            ConfirmLevelPolicy.floorFor("device.sms.send", payload("number" to "10086", "text" to "my password is 1234")),
        )
        // 普通输入框的正文里出现凭据词，也不该被当成凭据字段（只看目标指纹）。
        assertEquals(
            ConfirmLevel.CONFIRM_FIRST_STEP,
            ConfirmLevelPolicy.floorFor(
                "device.ui.input",
                payload("expected_package" to "com.example.app", "target_id" to "search_box", "text" to "password"),
            ),
        )
    }

    // ---------- 12. 普通命令与未声明命令 ----------

    @Test
    fun ordinaryCommandsAreAllowNormal() {
        for (command in listOf("device.app.list", "device.ui.back", "device.ui.home", "device.device_status")) {
            assertEquals(
                "$command 不在 M04 固定清单内",
                ConfirmLevel.ALLOW_NORMAL,
                ConfirmLevelPolicy.floorFor(command),
            )
        }
    }

    @Test
    fun undeclaredCommandIsAllowNormalButStillUnknownToPolicy() {
        // 未声明命令由 M06 路由返回 UNKNOWN_COMMAND；M04 不为它私造确认级别。
        val command = "device.unknown.command"
        assertEquals(ConfirmLevel.ALLOW_NORMAL, ConfirmLevelPolicy.floorFor(command))
        assertFalse(ConfirmLevelPolicy.isHighRisk(command))
    }

    // ---------- 13. OwnerRiskPolicy 可直接替换 M01 的过渡实现 ----------

    @Test
    fun ownerRiskPolicyReplacesInterimFallbackWithoutChangingBehaviour() {
        // 对 M06 已声明的每条命令逐一比对：两个实现必须给出相同结论。
        for (command in DeviceCommands.ALL) {
            assertEquals(
                "M04 与 M01 过渡清单在 $command 上不一致",
                RiskPolicies.INTERIM_FALLBACK.isHighRisk(command),
                OwnerRiskPolicy.isHighRisk(command),
            )
        }
    }

    @Test
    fun ownerRiskPolicyIsAFunInterfaceRiskPolicy() {
        val asRiskPolicy: RiskPolicy = OwnerRiskPolicy
        assertTrue(asRiskPolicy.isHighRisk("device.sms.send"))
        assertFalse(asRiskPolicy.isHighRisk("device.app.launch"))
    }

    @Test
    fun noGlobalAllowAllEscapeHatch() {
        // 文档禁止「用一个全局 ALLOW_ALL 绕过应用和命令级规则」：
        // 高风险命令在任何请求级别下都至少要确认，不存在无条件放行的入口。
        for (command in ConfirmLevelPolicy.HIGH_RISK_COMMANDS) {
            assertTrue(
                "$command 不得存在绕过确认的放行级别",
                ConfirmLevelPolicy.floorFor(command).strictness > ConfirmLevel.ALLOW_NORMAL.strictness,
            )
            assertFalse(
                "$command 的生效级别不得等于 ALLOW_NORMAL（请求=ALLOW_NORMAL）",
                ConfirmLevelPolicy.effectiveLevel(ConfirmLevel.ALLOW_NORMAL, command) == ConfirmLevel.ALLOW_NORMAL,
            )
        }
    }

    @Test
    fun pinnedAndFloorAgreeOnEveryStep() {
        for (command in ConfirmLevelPolicy.PINNED_EVERY_STEP_COMMANDS) {
            assertTrue("$command 必须被锁在每步确认", ConfirmLevelPolicy.isPinnedToEveryStep(command))
        }
        assertFalse(ConfirmLevelPolicy.isPinnedToEveryStep("device.app.launch"))
    }

    @Test
    fun payloadArgumentIsOptionalForEveryEntryPoint() {
        // 所有判定入口都必须能不带 payload 调用（M01 的 RiskConfirmation 拿不到 payload）。
        assertEquals(ConfirmLevel.CONFIRM_EVERY_STEP, ConfirmLevelPolicy.floorFor("device.sms.send"))
        assertEquals(ConfirmLevel.ALLOW_NORMAL, ConfirmLevelPolicy.floorFor("device.app.launch"))
        assertEquals(
            ConfirmLevel.CONFIRM_EVERY_STEP,
            ConfirmLevelPolicy.effectiveLevel(ConfirmLevel.ALLOW_NORMAL, "device.sms.send"),
        )
        assertFalse(ConfirmLevelPolicy.isPinnedToEveryStep("device.app.launch", null))
    }
}
