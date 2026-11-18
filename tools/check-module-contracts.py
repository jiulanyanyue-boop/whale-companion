#!/usr/bin/env python3
"""Whale Agent v2 模块契约静态一致性检查。

用途：本机没有 Gradle / Android SDK / JDK 17，无法编译 Kotlin，因此用本脚本把
「文档 ↔ contracts ↔ 设备端源码」之间本可以机器核对的一致性真正核对一遍。

检查项（全部以 docs/modules 为唯一规范来源）：
  C1  contracts/*.schema.json 是合法 JSON，且声明 draft 2020-12。
  C2  device-commands.schema.json 的命令集合 == DeviceCommands.ALL（Kotlin）
      == M06 产品文档「固定能力」清单 == CommandSpec.kt 的 CommandSpec 声明。
  C3  device-mode.schema.json 的 mode 枚举 == AgentMode 枚举，且恰好两个模式。
  C4  M06 开发文档「错误码固定集合」块中出现的错误码，全部在 CommandErrors.kt 中登记为 ALL 成员；
      反过来 CommandErrors.ALL 的每个成员都必须出现在 M06 或 M01 文档里（不得私造错误码）。
  C5  禁止项扫描：不得出现 shell / adb / eval / execute_code / raw_sql / arbitrary_http 命令声明。
  C6  M06 路由表里每一条命令都在 device-commands.schema.json 中有对应定义。
  C7  M01/M06 各有一份 product 与 development 文档（成对性）。

退出码：0 全部通过；1 存在不一致（逐条打印）。
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs" / "modules"
CONTRACTS = ROOT / "contracts"
DEVICE = ROOT / "device" / "app" / "src" / "main" / "java" / "cn" / "whale" / "agent" / "device"

FORBIDDEN_COMMAND_TOKENS = (
    "shell", "adb", "eval", "execute_code", "raw_sql", "arbitrary_http",
)

failures: list[str] = []
checks_run = 0


def fail(check: str, message: str) -> None:
    failures.append(f"[{check}] {message}")


def read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def check_c1() -> None:
    global checks_run
    checks_run += 1
    for path in sorted(CONTRACTS.glob("*.schema.json")):
        try:
            data = json.loads(read(path))
        except json.JSONDecodeError as exc:
            fail("C1", f"{path.name} 不是合法 JSON: {exc}")
            continue
        if data.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            fail("C1", f"{path.name} 未声明 draft 2020-12")
        if not data.get("$id"):
            fail("C1", f"{path.name} 缺少 $id")


def schema_command_set() -> set[str]:
    data = json.loads(read(CONTRACTS / "device-commands.schema.json"))
    commands: set[str] = set()
    for definition in data.get("definitions", {}).values():
        for branch in definition.get("allOf", []):
            const = branch.get("properties", {}).get("command", {})
            if "const" in const:
                commands.add(const["const"])
            enum = branch.get("properties", {}).get("command", {}).get("enum")
            if enum:
                commands.update(enum)
    return commands


def kotlin_command_set() -> set[str]:
    text = read(DEVICE / "DeviceModels.kt")
    block = re.search(r"val ALL: Set<String> = setOf\((.*?)\n    \)", text, re.S)
    if not block:
        fail("C2", "DeviceModels.kt 中找不到 DeviceCommands.ALL 定义")
        return set()
    return set(re.findall(r'"(device\.[a-z0-9_.]+)"', block.group(1)))


def spec_command_set() -> set[str]:
    text = read(DEVICE / "CommandSpec.kt")
    return set(re.findall(r'CommandSpec\(\s*"(device\.[a-z0-9_.]+)"', text))


def spec_empty_payload_set() -> set[str]:
    text = read(DEVICE / "CommandSpec.kt")
    return set(
        re.findall(r'CommandSpec\(\s*"(device\.[a-z0-9_.]+)"\s*,\s*emptyPayload\s*=\s*true', text)
    )


def doc_command_set() -> set[str]:
    text = read(DOCS / "M06-command-runtime-product.md")
    block = re.search(r"## 固定能力\s*```text(.*?)```", text, re.S)
    if not block:
        fail("C2", "M06 产品文档缺少「固定能力」代码块")
        return set()
    return set(re.findall(r"(device\.[a-z0-9_.]+)", block.group(1)))


def check_c2() -> None:
    global checks_run
    checks_run += 1
    schema = schema_command_set()
    kotlin = kotlin_command_set()
    spec = spec_command_set()
    doc = doc_command_set()

    for name, other, ref in (
        ("DeviceCommands.ALL", kotlin, "schema"),
        ("CommandSpecs", spec, "schema"),
        ("M06 产品文档", doc, "schema"),
    ):
        missing = schema - other
        extra = other - schema
        if missing:
            fail("C2", f"{name} 缺少 schema 中已声明的命令: {sorted(missing)}")
        if extra:
            fail("C2", f"{name} 出现 schema 未声明的命令: {sorted(extra)}")
        del ref

    if kotlin != spec:
        fail("C2", f"DeviceCommands.ALL 与 CommandSpecs 不一致: 仅 ALL={sorted(kotlin - spec)} 仅 Spec={sorted(spec - kotlin)}")

    empty_doc_commands = {
        "device.app.list", "device.ui.back", "device.ui.home",
        "device.ui.recents", "device.ui.notifications", "device.device_status",
        "device.screen.capture", "device.system.unlock",
    }
    declared_empty = spec_empty_payload_set()
    if declared_empty != empty_doc_commands:
        fail("C2", f"emptyPayload 命令集合应为 {sorted(empty_doc_commands)}，实际 {sorted(declared_empty)}")


def check_c3() -> None:
    global checks_run
    checks_run += 1
    schema = json.loads(read(CONTRACTS / "device-mode.schema.json"))
    schema_modes = set(schema["properties"]["mode"]["enum"])
    text = read(DEVICE / "AgentMode.kt")
    body = re.search(r"enum class AgentMode\s*\{\s*(.*?)\n\s*;?\s*\n", text, re.S)
    if not body:
        fail("C3", "AgentMode.kt 中找不到 AgentMode 枚举定义")
        return
    # 枚举常量只可能是「大写标识符，位于块首」的那几个；companion object 与属性都在 `;` 之后。
    head = body.group(1).split(";")[0]
    kotlin_modes = set(re.findall(r"[A-Z][A-Z0-9_]{2,}", head))
    if not kotlin_modes:
        fail("C3", "AgentMode.kt 中解析不到任何枚举常量")
        return
    if schema_modes != kotlin_modes:
        fail("C3", f"AgentMode {sorted(kotlin_modes)} 与 schema {sorted(schema_modes)} 不一致")
    if len(kotlin_modes) != 2:
        fail("C3", f"模式必须是恰好两个，实际 {len(kotlin_modes)}: {sorted(kotlin_modes)}")
    if "REMOTE_B" not in kotlin_modes:
        fail("C3", "缺少 REMOTE_B 模式")


def declared_error_codes() -> set[str]:
    text = read(DEVICE / "CommandErrors.kt")
    block = re.search(r"val ALL: Set<String> = setOf\((.*?)\n    \)", text, re.S)
    if not block:
        fail("C4", "CommandErrors.kt 中找不到 ALL 定义")
        return set()
    # ALL 里引用的是常量名（不带引号），因此按标识符提取。
    return set(re.findall(r"[A-Z][A-Z0-9_]+", block.group(1))) - {"ALL"}


def doc_error_codes() -> set[str]:
    text = read(DOCS / "M06-command-runtime-development.md")
    section = re.search(r"## 错误码固定集合\s*(.*?)\n## ", text, re.S)
    if not section:
        fail("C4", "M06 开发文档缺少「错误码固定集合」小节")
        return set()
    codes: set[str] = set()
    for block in re.findall(r"```text(.*?)```", section.group(1), re.S):
        codes |= set(re.findall(r"[A-Z][A-Z0-9_]{3,}", block))
    return codes


def m01_referenced_codes() -> set[str]:
    """M01 文档里以反引号或代码块形式引用的错误码。

    只认可「反引号包裹的大写标识符」与「```text 代码块内的大写标识符」，
    避免把 `REMOTE_B` / `LOCAL_PET` 这类模式名、以及正文里偶然出现的大写词当成错误码。
    """
    text = read(DOCS / "M01-system-modes-development.md")
    codes: set[str] = set()
    for block in re.findall(r"```text(.*?)```", text, re.S):
        codes |= set(re.findall(r"[A-Z][A-Z0-9_]{3,}", block))
    for inline in re.findall(r"`([A-Z][A-Z0-9_]{3,})`", text):
        codes.add(inline)
    # 模式名不是错误码。
    return codes - set(AgentMode_MODE_NAMES)


AgentMode_MODE_NAMES = {"REMOTE_B", "LOCAL_PET"}


def check_c4() -> None:
    global checks_run
    checks_run += 1
    declared = declared_error_codes()
    in_doc = doc_error_codes()

    m01_codes = m01_referenced_codes()

    missing_in_kotlin = in_doc - declared
    if missing_in_kotlin:
        fail("C4", f"文档固定错误码未在 CommandErrors.kt 登记: {sorted(missing_in_kotlin)}")

    undocumented = declared - in_doc - m01_codes
    if undocumented:
        fail("C4", f"CommandErrors.kt 出现文档未声明的错误码（不得私造）: {sorted(undocumented)}")

    # 路由表里要求返回的失败码必须可被解释。
    required_by_routing = {
        "UNKNOWN_COMMAND", "INVALID_PAYLOAD", "PACKAGE_NOT_ALLOWED", "MODE_REJECTED",
        "SIGNATURE_REQUIRED", "REPLAY", "EXPIRED", "NO_ACCESSIBILITY", "STALE_SNAPSHOT",
        "SELECTOR_MISMATCH", "FOREGROUND_PACKAGE_MISMATCH", "PERMISSION_REQUIRED",
        "NEEDS_UNLOCK", "NOT_WIRED",
    }
    lost = required_by_routing - in_doc
    if lost:
        fail("C4", f"M06 文档固定集合遗漏路由表要求的错误码: {sorted(lost)}")

    # M08 的 MediaErrors 与 CommandErrors 必须逐字一致：M08 定义语义，CommandErrors 是唯一登记处。
    media_errors = media_error_codes()
    if media_errors is not None:
        absent = media_errors - declared
        if absent:
            fail("C4", f"MediaErrors 中的码未登记到 CommandErrors.ALL: {sorted(absent)}")
        if media_errors - in_doc:
            fail("C4", f"MediaErrors 中的码未写入 M06 文档固定集合: {sorted(media_errors - in_doc)}")


def media_error_codes() -> set[str] | None:
    """从 AssetStore.kt 的 `object MediaErrors` 里取出 `ALL` 成员。"""
    path = DEVICE / "AssetStore.kt"
    if not path.exists():
        return None
    text = read(path)
    block = re.search(r"object MediaErrors\s*{(.*?)\n}", text, re.S)
    if not block:
        fail("C4", "AssetStore.kt 中找不到 object MediaErrors")
        return None
    all_block = re.search(r"val ALL: Set<String> = linkedSetOf\((.*?)\n    \)", block.group(1), re.S)
    if not all_block:
        all_block = re.search(r"val ALL: Set<String> = setOf\((.*?)\n    \)", block.group(1), re.S)
    if not all_block:
        fail("C4", "MediaErrors 中找不到 ALL 定义")
        return None
    return set(re.findall(r"[A-Z][A-Z0-9_]{3,}", all_block.group(1))) - {"ALL"}


def check_c5() -> None:
    global checks_run
    checks_run += 1
    candidates = [
        DEVICE / "DeviceModels.kt",
        DEVICE / "CommandSpec.kt",
    ]
    declared: set[str] = set()
    for path in candidates:
        if path.exists():
            declared |= set(re.findall(r"(device\.[a-z0-9_.]+)", read(path)))
    for command in sorted(declared):
        for token in FORBIDDEN_COMMAND_TOKENS:
            if token in command:
                fail("C5", f"声明了禁止命令 {command}（命中禁止词 {token}）")


def check_c6() -> None:
    global checks_run
    checks_run += 1
    text = read(DOCS / "M06-command-runtime-development.md")
    block = re.search(r"## 命令路由表\s*(.*?)\n## ", text, re.S)
    if not block:
        fail("C6", "M06 开发文档缺少「命令路由表」")
        return
    rows: list[str] = []
    for line in block.group(1).splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if not cells:
            continue
        first = cells[0]
        if first in {"命令", "---"} or set(first) <= {"-", " "}:
            continue
        # 只认第一列的反引号命令名，忽略「未接入时结果」列里的 `ok` / `queued` 等。
        match = re.fullmatch(r"`([a-z.*]+)`", first)
        if match:
            rows.append(match.group(1))
    if not rows:
        fail("C6", "命令路由表解析不到任何行")
        return
    schema = schema_command_set()
    schema_tails = {c.split(".", 1)[1] for c in schema}
    for row in rows:
        if "*" in row:
            prefix = row.split(".")[0]
            if not any(tail.startswith(prefix + ".") for tail in schema_tails):
                fail("C6", f"路由表前缀 {row} 在 schema 中没有对应命令")
            continue
        if row not in schema_tails:
            fail("C6", f"路由表条目 {row} 在 schema 中没有对应命令定义")


def check_c7() -> None:
    """M13 发布门槛的成对性检查：13 个模块各必须恰好一份 product + 一份 development。"""
    global checks_run
    checks_run += 1
    modules = sorted({p.name.split("-")[0] for p in DOCS.glob("M*.md")})
    expected = [f"M{i:02d}" for i in range(1, 14)]
    missing = [m for m in expected if m not in modules]
    if missing:
        fail("C7", f"缺少模块文档: {missing}")
    for module in expected:
        found = sorted(p.name for p in DOCS.glob(f"{module}-*.md"))
        product = [n for n in found if n.endswith("-product.md")]
        development = [n for n in found if n.endswith("-development.md")]
        if len(found) != 2:
            fail("C7", f"{module} 文档未成对，实际: {found}")
        if len(product) != 1:
            fail("C7", f"{module} 缺少唯一的 product 文档，实际: {product}")
        if len(development) != 1:
            fail("C7", f"{module} 缺少唯一的 development 文档，实际: {development}")


def check_c9() -> None:
    """M13 固定测试目录与真机测试目录结构。

    只检查目录存在性。用例是否真正落地由 C10（验收矩阵 + 覆盖文件存在性）负责，
    两者不重复报同一件事。
    """
    global checks_run
    checks_run += 1

    required_dirs = [
        ROOT / "host" / "tests",
        ROOT / "device" / "app" / "src" / "test",
        ROOT / "device" / "app" / "src" / "androidTest",
        ROOT / "test-fixtures",
        DOCS,
    ]
    for directory in required_dirs:
        if not directory.is_dir():
            fail("C9", f"M13 要求的测试目录不存在: {directory.relative_to(ROOT)}")

    # M13 开发文档必须仍然列出这两份必测清单，否则 C10 的交叉核对失去依据。
    text = read(DOCS / "M13-test-release-development.md")
    for heading in ("## 必测安全集合", "## 必测运行集合"):
        if heading not in text:
            fail("C9", f"M13 开发文档缺少清单小节: {heading}")


def check_c8() -> None:
    """验收夹具里出现的错误码必须已在文档固定集合登记，且夹具引用的命令必须已声明。"""
    global checks_run
    checks_run += 1
    registered = doc_error_codes() | m01_referenced_codes()
    declared_commands = schema_command_set()

    for path in sorted((ROOT / "test-fixtures").glob("m*.json")):
        try:
            data = json.loads(read(path))
        except json.JSONDecodeError as exc:
            fail("C8", f"{path.name} 不是合法 JSON: {exc}")
            continue

        for code in set(re.findall(r'"error_code"\s*:\s*"([A-Z0-9_]+)"', read(path))):
            if code not in registered:
                fail("C8", f"{path.name} 使用文档未登记的错误码: {code}")

        # 正向用例引用的命令必须已声明；负向用例（期望 UNKNOWN_COMMAND）允许故意引用
        # 未声明命令，否则无法验证「命令白名单」这道闸门。
        for case in data.get("cases", []):
            expected = case.get("expected", {})
            negative = expected.get("error_code") == "UNKNOWN_COMMAND"
            referenced: list[str] = []
            envelope = case.get("envelope") or {}
            if isinstance(envelope.get("command"), str):
                referenced.append(envelope["command"])
            if isinstance(case.get("local_command"), dict):
                local = case["local_command"].get("command")
                if isinstance(local, str):
                    referenced.append(local)
            if isinstance(expected.get("command"), str):
                referenced.append(expected["command"])
            referenced.extend(expected.get("forbidden_commands", []))

            for command in referenced:
                if command in declared_commands or negative:
                    continue
                fail("C8", f"{path.name} 用例 {case.get('id')} 引用未声明命令: {command}")


def check_c10() -> None:
    """M13 验收矩阵完整性：每个必测用例都必须有一条带全部 12 个字段的矩阵行。"""
    global checks_run
    checks_run += 1

    matrix_path = ROOT / "test-fixtures" / "m13-acceptance-matrix.json"
    if not matrix_path.is_file():
        fail("C10", "M13 验收矩阵缺失: test-fixtures/m13-acceptance-matrix.json")
        return
    try:
        matrix = json.loads(read(matrix_path))
    except json.JSONDecodeError as exc:
        fail("C10", f"M13 验收矩阵不是合法 JSON: {exc}")
        return

    required_fields = matrix.get("case_format_fields", [])
    if len(required_fields) != 12:
        fail("C10", f"M13 用例格式字段必须是 12 个，实际 {len(required_fields)}: {required_fields}")

    rows = matrix.get("cases", [])
    seen: set[str] = set()
    for row in rows:
        case_id = row.get("case_id")
        if not case_id:
            fail("C10", "验收矩阵存在缺少 case_id 的行")
            continue
        if case_id in seen:
            fail("C10", f"验收矩阵 case_id 重复: {case_id}")
        seen.add(case_id)
        missing = [field for field in required_fields if field not in row]
        if missing:
            fail("C10", f"用例 {case_id} 缺少格式字段: {missing}")

    # 与 M13 文档的必测清单交叉核对。
    text = read(DOCS / "M13-test-release-development.md")
    required_cases: set[str] = set()
    for pattern in (
        r"## 必测安全集合\s*```text(.*?)```",
        r"## 必测运行集合\s*```text(.*?)```",
    ):
        block = re.search(pattern, text, re.S)
        if block:
            required_cases |= set(re.findall(r"[a-z0-9_]{4,}", block.group(1)))

    missing_rows = sorted(required_cases - seen)
    if missing_rows:
        fail("C10", f"M13 必测用例在验收矩阵中缺行: {missing_rows}")
    extra_rows = sorted(seen - required_cases)
    if extra_rows:
        fail("C10", f"验收矩阵出现 M13 文档未列出的用例: {extra_rows}")

    # 覆盖文件必须真实存在，否则矩阵是空话。
    for row in rows:
        covered_by = row.get("covered_by")
        if not covered_by:
            continue
        if not (ROOT / covered_by).exists():
            fail("C10", f"用例 {row.get('case_id')} 声称的覆盖文件不存在: {covered_by}")


def kotlin_brace_depth(text: str) -> int:
    """剥离注释与字符串/字符字面量后的 { } 深度。

    **这条只是廉价的粗略筛查，不是权威判据** —— 权威判据是真编译器
    （`tools/verify-kotlin.ps1` 的 K2JVMCompiler，以及 C12 对它结论的核对）。
    保留它的价值在于：没有 JDK 的环境里也能立刻发现「文件被截断」这类结构性损坏。

    Kotlin 词法里必须被跳过的四类内容：
    1. `//` 行注释；
    2. `/* */` 块注释 —— **可以嵌套**（`/* 外层 /* 内层 */ 外层 */`），必须记嵌套深度；
       否则像 `* xxx -> { /* 等用户真实点击 */ }` 这种在注释里写 `{ }` 的文档行会被误算；
    3. `"..."` 普通字符串（含 `\\` 转义）与 `\"\"\"...\"\"\"` 三引号原始字符串；
    4. `'...'` 字符字面量 —— **`'{'` 和 `'}'` 是合法 Kotlin**，漏掉这一类会直接算错。
    """
    depth = 0
    comment_depth = 0
    in_string = False
    in_raw = False
    in_char = False
    i = 0
    length = len(text)
    while i < length:
        two = text[i:i + 2]
        if comment_depth > 0:
            if two == "/*":
                comment_depth += 1
                i += 2
                continue
            if two == "*/":
                comment_depth -= 1
                i += 2
                continue
            i += 1
            continue
        if in_raw:
            if text.startswith('"""', i):
                in_raw = False
                i += 3
                continue
            i += 1
            continue
        if in_string:
            if text[i] == "\\":
                i += 2
                continue
            if text[i] == '"':
                in_string = False
            i += 1
            continue
        if in_char:
            if text[i] == "\\":
                i += 2
                continue
            if text[i] == "'":
                in_char = False
            i += 1
            continue
        if two == "//":
            newline = text.find("\n", i)
            i = length if newline < 0 else newline
            continue
        if two == "/*":
            comment_depth = 1
            i += 2
            continue
        if text.startswith('"""', i):
            in_raw = True
            i += 3
            continue
        ch = text[i]
        if ch == '"':
            in_string = True
        elif ch == "'":
            in_char = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return depth


def check_c11() -> None:
    """Kotlin 源码结构完整性：每个 .kt 文件剥离注释/字符串后大括号必须配平。

    本机无编译器，这条只能抓「文件被截断、括号漏写」这类结构性损坏，
    不能替代 `kotlinc` 的类型检查。

    注意：Kotlin 文件可以由**多个顶层声明**组成，合法的文件不一定以 `}` 结尾
    （例如最后一个声明是 `class Foo(...) : Bar(...)` 这样的单行类、或是
    `data class(...)` 的右括号 `)`）。因此这里只校验大括号配平与「文件非空」，
    不校验末字符——那条判据会误伤合法代码。
    """
    global checks_run
    checks_run += 1

    kt_files = sorted(DEVICE.glob("*.kt")) + sorted(
        (ROOT / "device" / "app" / "src" / "test" / "java" / "cn" / "whale" / "agent" / "device").glob("*.kt")
    )
    if not kt_files:
        fail("C11", "找不到任何 Kotlin 源文件")
        return

    for path in kt_files:
        text = read(path)
        if not text.strip():
            fail("C11", f"{path.name} 是空文件")
            continue
        depth = kotlin_brace_depth(text)
        if depth != 0:
            fail("C11", f"{path.name} 大括号不配平，最终深度 {depth}")


def check_c12() -> None:
    """Kotlin 真编译基线：`.verify/last-run.json` 必须存在且达标。

    本机有 JDK 17 + Kotlin 2.0.21 编译器 + android-34 平台（见 `tools/verify-kotlin.ps1`），
    因此「Android 代码没编译过」不再是可接受的免责理由。这条检查确保：

    - 编译产物记录在案（否则说明没有真跑过，或者跑了没留证据）；
    - 36 个 main 源文件、13 个 test 源文件全部编译通过；
    - 13 个测试套件的 251 个 Kotlin 单元用例全部通过；
    - main .kt 与 test .kt 的实际数量与记录一致（有人新增文件却没重跑就会失败）。

    这条不能替代编译器本身——它只核对「最近一次真编译」的结论是否仍然成立。
    重跑方式：`pwsh -File tools/verify-kotlin.ps1`（会重写 `.verify/last-run.json`）。
    """
    global checks_run
    checks_run += 1

    record_path = ROOT / ".verify" / "last-run.json"
    if not record_path.is_file():
        fail(
            "C12",
            "缺少 Kotlin 编译验证记录 .verify/last-run.json —— "
            "请先运行 `pwsh -File tools/verify-kotlin.ps1`，"
            "不要在没有编译证据的情况下声称 Android 代码可用",
        )
        return
    try:
        record = json.loads(read(record_path))
    except json.JSONDecodeError as exc:
        fail("C12", f".verify/last-run.json 不是合法 JSON: {exc}")
        return

    main_sources = len(list(DEVICE.glob("*.kt")))
    test_dir = ROOT / "device" / "app" / "src" / "test" / "java" / "cn" / "whale" / "agent" / "device"
    test_sources = len(list(test_dir.glob("*.kt")))

    if record.get("main_sources") != main_sources:
        fail(
            "C12",
            f"编译记录里 main 源文件是 {record.get('main_sources')} 个，当前实际 {main_sources} 个；"
            "请重跑 tools/verify-kotlin.ps1",
        )
    if record.get("test_sources") != test_sources:
        fail(
            "C12",
            f"编译记录里 test 源文件是 {record.get('test_sources')} 个，当前实际 {test_sources} 个；"
            "请重跑 tools/verify-kotlin.ps1",
        )
    if record.get("test_suites") != test_sources:
        fail(
            "C12",
            f"编译记录里测试套件数是 {record.get('test_suites')}，但 test 源文件有 {test_sources} 个；"
            "说明有测试文件没有被实际运行",
        )
    passed = record.get("unit_tests_passed")
    if not isinstance(passed, int) or passed < 251:
        fail("C12", f"Kotlin 单元用例通过数应为 251，记录为 {passed}")
    if record.get("main_classes", 0) <= 0:
        fail("C12", "编译记录里 main_class 数为 0，说明编译没有真正产出")


def main() -> int:
    if not DOCS.is_dir():
        print(f"无法定位模块文档目录: {DOCS}")
        return 1
    for check in (
        check_c1, check_c2, check_c3, check_c4, check_c5,
        check_c6, check_c7, check_c8, check_c9, check_c10, check_c11, check_c12,
    ):
        check()

    if failures:
        print(f"模块契约一致性检查：{len(failures)} 处不一致（共 {checks_run} 项检查）")
        for line in failures:
            print(f"  - {line}")
        return 1

    print(f"契约一致性检查全部通过（{checks_run} 项检查）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
