"""Unicode 规范化与脚本（script）分析。

- 规范名：NFKC 归一化后再做 casefold，用于大小写/兼容字符无关的相等性比较。
- 脚本分析：基于 unicodedataplus 的 script()，忽略 Common 与 Inherited，
  统计名称中出现的其余脚本及对应码点。
- 混脚本违规：由命名空间级策略（namespace script-policy）中被阻断的脚本对
  （blocked_script_pairs）决定——名称同时出现某一对中的两个脚本即违规。
  未配置策略时使用内建默认策略：Latin / Cyrillic / Greek 三者中任意两种
  混用即违规（与历史行为一致）。

脚本对在存储与传输时统一使用“规范对”：两个脚本名按字典序排列（小者在前），
因此 (Latin, Cyrillic) 与 (Cyrillic, Latin) 完全等价；策略中的脚本对无序
且去重。
"""
from __future__ import annotations

import unicodedataplus as udp

# 标点、数字、空白、组合符等不参与混脚本判定
IGNORED_SCRIPTS = frozenset({"Common", "Inherited"})

# 互似字形最多的三个脚本：未配置命名空间策略时，三者任意两种混用即违规
RESTRICTED_SCRIPTS = frozenset({"Latin", "Cyrillic", "Greek"})

# 内建默认策略（未配置 namespace_script_policies 时生效）：
# Latin/Cyrillic/Greek 两两组合，统一存为按字典序排列的规范对
DEFAULT_BLOCKED_PAIRS: tuple[tuple[str, str], ...] = (
    ("Cyrillic", "Greek"),
    ("Cyrillic", "Latin"),
    ("Greek", "Latin"),
)

# 合法脚本名集合（Unicode 16.0 的 Script 属性值全集，由
# unicodedataplus.script() 枚举码点生成）。Common / Inherited / Unknown
# 不是实际文字脚本，不允许出现在策略对中：与它们组合永远不可能触发违规，
# 配置出来只会造成误导。
VALID_SCRIPTS = frozenset(
    {
        "Adlam", "Ahom", "Anatolian_Hieroglyphs", "Arabic", "Armenian",
        "Avestan", "Balinese", "Bamum", "Bassa_Vah", "Batak", "Bengali",
        "Bhaiksuki", "Bopomofo", "Brahmi", "Braille", "Buginese", "Buhid",
        "Canadian_Aboriginal", "Carian", "Caucasian_Albanian", "Chakma",
        "Cham", "Cherokee", "Chorasmian", "Coptic", "Cuneiform", "Cypriot",
        "Cypro_Minoan", "Cyrillic", "Deseret", "Devanagari", "Dives_Akuru",
        "Dogra", "Duployan", "Egyptian_Hieroglyphs", "Elbasan", "Elymaic",
        "Ethiopic", "Garay", "Georgian", "Glagolitic", "Gothic", "Grantha",
        "Greek", "Gujarati", "Gunjala_Gondi", "Gurmukhi", "Gurung_Khema",
        "Han", "Hangul", "Hanifi_Rohingya", "Hanunoo", "Hatran", "Hebrew",
        "Hiragana", "Imperial_Aramaic", "Inscriptional_Pahlavi",
        "Inscriptional_Parthian", "Javanese", "Kaithi", "Kannada",
        "Katakana", "Kawi", "Kayah_Li", "Kharoshthi", "Khitan_Small_Script",
        "Khmer", "Khojki", "Khudawadi", "Kirat_Rai", "Lao", "Latin",
        "Lepcha", "Limbu", "Linear_A", "Linear_B", "Lisu", "Lycian",
        "Lydian", "Mahajani", "Makasar", "Malayalam", "Mandaic",
        "Manichaean", "Marchen", "Masaram_Gondi", "Medefaidrin",
        "Meetei_Mayek", "Mende_Kikakui", "Meroitic_Cursive",
        "Meroitic_Hieroglyphs", "Miao", "Modi", "Mongolian", "Mro",
        "Multani", "Myanmar", "Nabataean", "Nag_Mundari", "Nandinagari",
        "New_Tai_Lue", "Newa", "Nko", "Nushu", "Nyiakeng_Puachue_Hmong",
        "Ogham", "Ol_Chiki", "Ol_Onal", "Old_Hungarian", "Old_Italic",
        "Old_North_Arabian", "Old_Permic", "Old_Persian", "Old_Sogdian",
        "Old_South_Arabian", "Old_Turkic", "Old_Uyghur", "Oriya", "Osage",
        "Osmanya", "Pahawh_Hmong", "Palmyrene", "Pau_Cin_Hau", "Phags_Pa",
        "Phoenician", "Psalter_Pahlavi", "Rejang", "Runic", "Samaritan",
        "Saurashtra", "Sharada", "Shavian", "Siddham", "SignWriting",
        "Sinhala", "Sogdian", "Sora_Sompeng", "Soyombo", "Sundanese",
        "Sunuwar", "Syloti_Nagri", "Syriac", "Tagalog", "Tagbanwa",
        "Tai_Le", "Tai_Tham", "Tai_Viet", "Takri", "Tamil", "Tangsa",
        "Tangut", "Telugu", "Thaana", "Thai", "Tibetan", "Tifinagh",
        "Tirhuta", "Todhri", "Toto", "Tulu_Tigalari", "Ugaritic", "Vai",
        "Vithkuqi", "Wancho", "Warang_Citi", "Yezidi", "Yi",
        "Zanabazar_Square",
    }
)

# 单条策略允许配置的脚本对上限：覆盖全部脚本两两组合绰绰有余，
# 同时给请求体一个明确边界，避免误传超大策略
MAX_BLOCKED_PAIRS = 500


def canonical_pair(script_a: str, script_b: str) -> tuple[str, str]:
    """把两个脚本名规范为按字典序排列的无序对（小者在前）。"""
    first, second = sorted((script_a, script_b))
    return first, second


def normalize_blocked_pairs(
    pairs: list[list[str]] | tuple[tuple[str, str], ...],
) -> list[tuple[str, str]]:
    """校验并规范化脚本对列表：逐项校验脚本名、两项相异，再无序去重排序。

    每一项必须是恰好两个脚本名；脚本名须在 VALID_SCRIPTS 中（不接受
    Common / Inherited / Unknown 或其他未知脚本名）；同一对中的两个脚本
    不能相同。返回按字典序排列、去重后的规范对列表。
    """
    if len(pairs) > MAX_BLOCKED_PAIRS:
        raise ValueError(f"blocked_script_pairs 至多包含 {MAX_BLOCKED_PAIRS} 对脚本")
    normalized: set[tuple[str, str]] = set()
    for pair in pairs:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError("每一项必须是恰好两个脚本名，形如 [\"Latin\", \"Cyrillic\"]")
        script_a, script_b = pair
        if not isinstance(script_a, str) or not isinstance(script_b, str):
            raise ValueError("脚本名必须是字符串")
        for script in (script_a, script_b):
            if script not in VALID_SCRIPTS:
                raise ValueError(f"未知或不支持的脚本名：{script!r}")
        if script_a == script_b:
            raise ValueError(f"脚本对必须由两种不同的脚本组成：{script_a!r} 与自身配对无意义")
        normalized.add(canonical_pair(script_a, script_b))
    return sorted(normalized)


def normalize_name(name: str) -> str:
    """计算规范名：NFKC + casefold。"""
    return udp.normalize("NFKC", name).casefold()


def analyze_scripts(
    name: str,
    blocked_pairs: tuple[tuple[str, str], ...] | list[tuple[str, str]] | None = None,
) -> dict:
    """按 Unicode 脚本分组名称中的字符（忽略 Common / Inherited）。

    返回脚本清单、每个脚本包含的字符及其码点，以及按给定阻断脚本对
    （blocked_pairs）做出的混脚本违规判定。blocked_pairs 为 None 时使用
    内建默认策略（Latin/Cyrillic/Greek 任意两种混用即违规）。

    - blocked_script_pairs：本次判定实际生效的策略对（规范对，已排序）
    - violating_script_pairs：名称实际命中的策略对子集（名称中同时出现
      一对中的两个脚本即命中），非空即 mixed_script_violation
    - restricted_scripts_used：名称中出现的 Latin/Cyrillic/Greek
      （历史字段，默认策略下与违规明细对应，自定义策略下仅作信息展示）
    """
    effective_pairs = DEFAULT_BLOCKED_PAIRS if blocked_pairs is None else blocked_pairs

    by_script: dict[str, list[str]] = {}
    for char in name:
        script = udp.script(char)
        if script in IGNORED_SCRIPTS:
            continue
        chars = by_script.setdefault(script, [])
        if char not in chars:
            chars.append(char)

    script_details = [
        {
            "script": script,
            "characters": chars,
            "code_points": [f"U+{ord(char):04X}" for char in chars],
        }
        for script, chars in sorted(by_script.items())
    ]
    present_scripts = set(by_script)
    violating_pairs = sorted(
        canonical_pair(script_a, script_b)
        for script_a, script_b in effective_pairs
        if script_a in present_scripts and script_b in present_scripts
    )
    return {
        "scripts": [detail["script"] for detail in script_details],
        "script_details": script_details,
        "restricted_scripts_used": sorted(present_scripts & RESTRICTED_SCRIPTS),
        "blocked_script_pairs": [list(pair) for pair in effective_pairs],
        "violating_script_pairs": [list(pair) for pair in violating_pairs],
        "mixed_script_violation": bool(violating_pairs),
    }
