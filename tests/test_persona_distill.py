"""M3-E 聊天记录蒸馏（「一键赋角」入口 C）的验收判据。

单独成文件（不追加到既有文件）：既有文件末尾无换行，追加会制造与功能无关的
EOL 差异。

这个入口的核心判断是**确定性指纹不许被模型覆盖**，所以文件结构按三层分：

    1. 解析层：脏数据 / 四种角色标记 / 连续段落
    2. 指纹层：每一项算法单独钉死（判据 6、7、9）
    3. 生成层：voice 硬约束（判据 8）、不落库（判据 2）、合规红线（判据 3、10、11）
    4. 前端契约：面板与授权闸门（判据 12）

判据 6/7 的期望值是**手算**出来的，不是从实现里抄的：
    · 5 条长度 4/6/8/10/12 → 中位数 8 → 下界 max(3, int(5.6))=5，上界 min(40, max(8, int(12.8)))=12
    · 5 条里 2 条含 emoji → 0.4
    · 同一条消息里重复 5 次的片段 → 只算 1 条消息 → 进不了口癖（判据 9）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _fakes import FakeEmbedder

from linger.distill import (
    PROVENANCE_CLONED,
    TranscriptError,
    distill_persona,
    extract_fingerprint,
    parse_transcript,
)

STATIC = Path(__file__).resolve().parent.parent / "src" / "linger" / "server" / "static"

_TRANSCRIPT = """林汐: 行吧，你先忙
我: 那你呢
林汐: 我没事啊…
我: 真的吗
林汐: 真的。
"""


# --------------------------------------------------------------------------- #
# 剧本化假模型
# --------------------------------------------------------------------------- #
_PREVIEW = "行吧。\n你先忙。\n我没事啊…\n真的。\n嗯。\n那你呢。"


class _DistillLLM:
    """蒸馏请求吐 `payload`，样例请求吐台词。

    默认 payload 不含 voice —— 这也顺带断言了「模型给不给 voice 都不影响结果」。
    """

    name = "scripted"

    def __init__(self, payload: dict | None = None, raw: str | None = None) -> None:
        self.payload = payload if payload is not None else {
            "meta": {"name": "林汐", "identity": "书店店员", "archetype": "安静的人", "age": 24},
            "traits": {
                "openness": 0.5,
                "conscientiousness": 0.5,
                "extraversion": 0.35,
                "agreeableness": 0.7,
                "neuroticism": 0.4,
            },
            "values": {"stance_advice": "不劝人"},
        }
        self.raw = raw
        self.distill_calls = 0

    def generate(self, system, messages, ctx=None):
        from linger.distill.compose import DISTILL_MARKER
        from linger.persona.generator import PREVIEW_MARKER

        if DISTILL_MARKER in system:
            self.distill_calls += 1
            yield self.raw if self.raw is not None else json.dumps(self.payload, ensure_ascii=False)
            return
        if PREVIEW_MARKER in system:
            yield self.preview_override() if hasattr(self, "preview_text") else _PREVIEW
            return
        yield "嗯。"

    def preview_override(self):
        return self.preview_text


@pytest.fixture
def make_client(repo, persona):
    """工厂：给剧本 → TestClient。与 test_persona_generate.py 同构。"""
    from fastapi.testclient import TestClient

    from linger.server.app import create_app

    clients = []

    def _make(llm=None) -> TestClient:
        app = create_app(repo=repo, llm=llm or _DistillLLM(), embedder=FakeEmbedder())
        c = TestClient(app)
        c.llm = llm
        clients.append(c)
        return c

    yield _make
    for c in clients:
        c.close()


def _distill(client, **over):
    body = {"transcript": _TRANSCRIPT, "target_name": "林汐", "authorized": True}
    body.update(over)
    return client.post("/v1/personas/distill", json=body)


# --------------------------------------------------------------------------- #
# 判据 1：200 + 四个键
# --------------------------------------------------------------------------- #
def test_distill_returns_four_keys(make_client):
    with make_client() as c:
        resp = _distill(c)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"persona", "fingerprint", "warnings", "preview"}
    assert body["persona"]["meta"]["name"] == "林汐"
    assert len(body["preview"]) == 6
    assert isinstance(body["warnings"], list)


def test_distill_preview_not_blocked_by_warnings(make_client):
    """警告不阻断：有修正也照出 6 条台词。"""
    llm = _DistillLLM(payload={
        "meta": {"name": "林汐"},
        "traits": {"extraversion": 0.95},  # 会触发 R1
    })
    with make_client(llm) as c:
        body = _distill(c).json()
    assert body["warnings"]
    assert len(body["preview"]) == 6


# --------------------------------------------------------------------------- #
# 判据 2：不落库
# --------------------------------------------------------------------------- #
def test_distill_does_not_persist(make_client, repo):
    before = len(repo.list_personas())
    with make_client() as c:
        assert _distill(c).status_code == 200
        assert len(c.get("/v1/personas").json()["personas"]) == before
    assert len(repo.list_personas()) == before, "蒸馏阶段不许写库"


# --------------------------------------------------------------------------- #
# 判据 3：授权是红线
# --------------------------------------------------------------------------- #
def test_missing_authorized_422(make_client):
    with make_client() as c:
        resp = c.post(
            "/v1/personas/distill", json={"transcript": _TRANSCRIPT, "target_name": "林汐"}
        )
    assert resp.status_code == 422


@pytest.mark.parametrize("flag", [False, 0, "false", "no"])
def test_authorized_false_422(make_client, flag):
    with make_client() as c:
        assert _distill(c, authorized=flag).status_code == 422


def test_authorized_false_does_not_call_llm(make_client):
    """校验必须在**到达模型之前**。否则「没授权也读了聊天记录」。"""
    llm = _DistillLLM()
    with make_client(llm) as c:
        _distill(c, authorized=False)
    assert llm.distill_calls == 0


# --------------------------------------------------------------------------- #
# 判据 4：transcript 至少 3 行
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", ["", "   ", "\n\n\n", "林汐: 行吧\n我: 那你呢\n"])
def test_short_transcript_422(make_client, text):
    with make_client() as c:
        assert _distill(c, transcript=text).status_code == 422


def test_three_rows_is_enough(make_client):
    """边界：3 行正好够。"""
    with make_client() as c:
        resp = _distill(c, transcript="林汐: 行吧\n我: 那你呢\n林汐: 我没事啊")
    assert resp.status_code == 200


def test_blank_lines_do_not_count_as_rows(make_client):
    """空行不算行——否则「1 行内容 + 两行空行」会混过 3 行门槛。"""
    with make_client() as c:
        resp = _distill(c, transcript="林汐: 行吧\n\n\n")
    assert resp.status_code == 422


def test_target_not_in_transcript_422(make_client):
    """点了一个记录里没出现的人 → 422，不是静默降级成「发言最多的人」。"""
    with make_client() as c:
        resp = _distill(c, target_name="王五")
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# 判据 5：四种角色标记都能拆
# --------------------------------------------------------------------------- #
def test_parse_halfwidth_colon():
    assert parse_transcript("林汐: 行吧，你先忙\n我: 那你呢") == [
        ("林汐", "行吧，你先忙"),
        ("我", "那你呢"),
    ]


def test_parse_fullwidth_colon():
    assert parse_transcript("林汐：我没事啊…\n我：真的吗") == [
        ("林汐", "我没事啊…"),
        ("我", "真的吗"),
    ]


def test_parse_bracket():
    assert parse_transcript("[林汐] 真的。\n[我] 嗯") == [("林汐", "真的。"), ("我", "嗯")]


def test_parse_cjk_bracket():
    assert parse_transcript("【林汐】真的。\n【我】嗯") == [("林汐", "真的。"), ("我", "嗯")]


def test_parse_space_fallback():
    """没有冒号/方括号时才用空格规则。"""
    assert parse_transcript("林汐 行吧，你先忙\n我 那你呢") == [
        ("林汐", "行吧，你先忙"),
        ("我", "那你呢"),
    ]


def test_space_fallback_does_not_split_sentences():
    """有冒号标记时，没冒号的行是**正文**，不能被空格切成「说话人 + 内容」。"""
    lines = parse_transcript("林汐: 行吧，你先忙\n其实我觉得还行\n我: 那你呢")
    assert lines == [("林汐", "行吧，你先忙"), ("林汐", "其实我觉得还行"), ("我", "那你呢")]


def test_unmarked_line_inherits_last_speaker():
    lines = parse_transcript("林汐: 第一句\n续了一段\n再接一段\n我: 我说")
    assert lines == [("林汐", "第一句"), ("林汐", "续了一段"), ("林汐", "再接一段"), ("我", "我说")]


def test_dirty_data_tolerated():
    """空行跳过、首尾空白裁掉、只有名字没内容的行跳过。"""
    text = "\n  林汐:   行吧  \n\n我:\n我: 那你呢\n  \n林汐: 我没事啊\n"
    assert parse_transcript(text) == [
        ("林汐", "行吧"),
        ("我", "那你呢"),
        ("林汐", "我没事啊"),
    ]


def test_parse_crlf():
    assert parse_transcript("林汐: 行吧\r\n我: 那你呢\r\n") == [("林汐", "行吧"), ("我", "那你呢")]


def test_parse_leading_unmarked_line_dropped():
    """开头就没有说话人 → 认不出是谁，跳过而不是硬猜。"""
    assert parse_transcript("不知道谁说的\n林汐: 行吧") == [("林汐", "行吧")]


def test_parse_empty_raises():
    with pytest.raises(TranscriptError):
        parse_transcript("   \n\n ")


def test_colon_inside_sentence_is_not_a_speaker():
    """「我觉得：这事不对」不是「我觉得」在说话。"""
    lines = parse_transcript("林汐: 好\n我觉得：这事不对")
    assert ("林汐", "我觉得：这事不对") in lines


# --------------------------------------------------------------------------- #
# 判据 6：句长区间
# --------------------------------------------------------------------------- #
def test_fingerprint_sentence_len_from_five_messages():
    """5 条长度 4/6/8/10/12 → 中位数 8 → [5, 12]。手算，不抄实现。"""
    lines = [("他", "一" * 4), ("他", "二" * 6), ("他", "三" * 8), ("他", "四" * 10), ("他", "五" * 12)]
    fp = extract_fingerprint(lines, "他")
    assert fp["avg_sentence_len"] == [5, 12]


def test_fingerprint_sentence_len_even_median_is_averaged():
    """偶数条取中间两条的均值：[4,6] → 5 → 下界 3，上界 8。"""
    lines = [("他", "一" * 4), ("他", "二" * 6)]
    fp = extract_fingerprint(lines, "他")
    assert fp["avg_sentence_len"] == [3, 8]


def test_fingerprint_sentence_len_clamped():
    """超长消息：上界夹到 40，下界夹回上界以内——指纹自己就该给出合法区间。

    不依赖 R3（倒挂交换）来救：那是下游规则的副作用，不是这一层的契约。
    """
    lines = [("他", "字" * 200)] * 3
    lo, hi = extract_fingerprint(lines, "他")["avg_sentence_len"]
    assert hi == 40
    assert lo <= hi, "下界不得越过上界（否则要等 R3 交换才合法）"


def test_fingerprint_sentence_len_floor():
    """极短消息下界不低于 3。"""
    lines = [("他", "嗯")] * 3
    lo, hi = extract_fingerprint(lines, "他")["avg_sentence_len"]
    assert lo == 3 and hi == 8


# --------------------------------------------------------------------------- #
# 判据 7：emoji 率
# --------------------------------------------------------------------------- #
def test_fingerprint_emoji_rate_two_of_five():
    lines = [("他", "好啊🌿"), ("他", "嗯"), ("他", "开心☕"), ("他", "哦"), ("他", "行")]
    assert extract_fingerprint(lines, "他")["emoji_rate"] == 0.4


def test_fingerprint_emoji_rate_same_emoji_twice_counts_messages():
    """一条消息里 3 个 emoji 也只算「1 条含 emoji」。"""
    lines = [("他", "🌿🌿🌿"), ("他", "嗯"), ("他", "哦")]
    assert extract_fingerprint(lines, "他")["emoji_rate"] == pytest.approx(0.33, abs=0.01)


def test_fingerprint_emoji_set_dedup_and_order():
    lines = [("他", "a🌿"), ("他", "b🌿"), ("他", "c☕"), ("他", "d🌿 ☕")]
    assert extract_fingerprint(lines, "他")["emoji_set"] == ["🌿", "☕"]


def test_fingerprint_emoji_set_capped_at_five():
    lines = [("他", "".join(chr(0x1F600 + i) for i in range(8)))] * 3
    assert len(extract_fingerprint(lines, "他")["emoji_set"]) == 5


def test_arrow_is_not_emoji():
    """0x2190~0x21FF 是箭头，不是表情——不然「→」会被算成 emoji。"""
    from linger.distill.fingerprint import is_emoji

    assert not is_emoji("→")
    assert is_emoji("🌿")
    assert is_emoji("☕")


# --------------------------------------------------------------------------- #
# 判据 9：口癖必须跨消息
# --------------------------------------------------------------------------- #
def test_tic_requires_distinct_messages():
    """同一条消息里重复 5 次 → 只算 1 条消息 → 不进口癖。"""
    lines = [("他", "行吧行吧行吧行吧行吧"), ("他", "嗯"), ("他", "哦")]
    assert "行吧" not in extract_fingerprint(lines, "他")["verbal_tics"]


def test_tic_appears_in_three_messages():
    """3 条不同消息各出现 1 次 → 是口癖。"""
    lines = [("他", "行吧，你先忙"), ("他", "行吧我知道了"), ("他", "行吧")]
    assert "行吧" in extract_fingerprint(lines, "他")["verbal_tics"]


def test_tic_two_messages_not_enough():
    """2 条不同消息还差一条，不算口癖。"""
    lines = [("他", "行吧你先忙"), ("他", "行吧我知道了"), ("他", "嗯")]
    assert "行吧" not in extract_fingerprint(lines, "他")["verbal_tics"]


def test_tic_not_duplicated():
    lines = [("他", "行吧行吧"), ("他", "行吧行吧"), ("他", "行吧行吧")]
    tics = extract_fingerprint(lines, "他")["verbal_tics"]
    assert len(tics) == len(set(tics))
    assert "行吧" in tics


def test_tic_capped_at_five():
    lines = [("他", f"词语{i}号再说一遍") for i in range(8)]
    assert len(extract_fingerprint(lines, "他")["verbal_tics"]) <= 5


def test_tic_ignores_pure_punctuation():
    lines = [("他", "……"), ("他", "……"), ("他", "……")]
    assert extract_fingerprint(lines, "他")["verbal_tics"] == []


# --------------------------------------------------------------------------- #
# 指纹其余项
# --------------------------------------------------------------------------- #
def test_punctuation_policy_ellipsis():
    lines = [("他", "我没事啊…"), ("他", "怎么了…"), ("他", "嗯…"), ("他", "好。")]
    assert extract_fingerprint(lines, "他")["punctuation_policy"] == "常以省略号收尾"


def test_punctuation_policy_period():
    lines = [("他", "好的。"), ("他", "我知道了。"), ("他", "嗯。"), ("他", "行。"), ("他", "随你")]
    assert extract_fingerprint(lines, "他")["punctuation_policy"] == "句子收得干脆"


def test_punctuation_policy_default():
    lines = [("他", "好"), ("他", "嗯"), ("他", "行")]
    assert extract_fingerprint(lines, "他")["punctuation_policy"] == "标点随意"


@pytest.mark.parametrize("text,expected", [("我没事啊", "口语"), ("好的呢", "口语"), ("风雨将至", "书面")])
def test_register(text, expected):
    assert extract_fingerprint([("他", text)] * 3, "他")["register"] == expected


def test_high_freq_words_requires_three_occurrences():
    lines = [("他", "加班"), ("他", "又加班"), ("他", "加班到很晚")]
    assert "加班" in extract_fingerprint(lines, "他")["high_freq_words"]


def test_high_freq_words_capped_at_ten():
    lines = [("他", " ".join(f"word{i}" for i in range(30)))] * 4
    assert len(extract_fingerprint(lines, "他")["high_freq_words"]) <= 10


def test_dominant_speaker_default():
    """不给 target_name 时取发言最多的一方。"""
    text = "我: 嗯\n林汐: 第一句\n林汐: 第二句\n林汐: 第三句\n"
    with_target_gone = parse_transcript(text)
    fp = extract_fingerprint(with_target_gone, None)
    assert fp["register"] in ("口语", "书面")  # 能算出东西 = 选到了人
    from linger.distill.fingerprint import dominant_speaker

    assert dominant_speaker(with_target_gone) == "林汐"


def test_fingerprint_ignores_other_speakers():
    lines = [("他", "好的呢"), ("我", "字" * 40), ("我", "字" * 40), ("他", "嗯。")]
    fp = extract_fingerprint(lines, "他")
    assert fp["avg_sentence_len"][1] <= 8  # 没有被「我」的长句拉高


# --------------------------------------------------------------------------- #
# 判据 8：LLM 不许覆盖 voice
# --------------------------------------------------------------------------- #
def test_llm_cannot_override_voice(make_client):
    """模型返回 voice.register = "书面语" → 最终仍是指纹算的「口语」，且有 warning。"""
    llm = _DistillLLM(payload={
        "meta": {"name": "林汐"},
        "traits": {"extraversion": 0.4},
        "voice": {"register": "书面语"},
    })
    with make_client(llm) as c:
        body = _distill(c).json()
    assert body["persona"]["voice"]["register"] == "口语"
    assert body["persona"]["voice"]["register"] != "书面语"
    assert any("语体" in w and "统计" in w for w in body["warnings"]), body["warnings"]


def test_llm_cannot_override_voice_numbers(make_client):
    """模型改句长/emoji 率也不行——那是统计事实，不是文风偏好。"""
    llm = _DistillLLM(payload={
        "meta": {"name": "林汐"},
        "voice": {"avg_sentence_len": [30, 40], "emoji_rate": 0.9, "verbal_tics": ["假的"]},
    })
    with make_client(llm) as c:
        body = _distill(c).json()
    fp = body["fingerprint"]
    assert body["persona"]["voice"]["avg_sentence_len"] == fp["avg_sentence_len"]
    assert body["persona"]["voice"]["emoji_rate"] == fp["emoji_rate"]
    assert body["persona"]["voice"]["verbal_tics"] == fp["verbal_tics"]


def test_same_voice_in_fingerprint_and_persona(make_client):
    """返回体里的 fingerprint 与 persona.voice 必须一致——不一致用户就没法核对。"""
    with make_client() as c:
        body = _distill(c).json()
    fp, voice = body["fingerprint"], body["persona"]["voice"]
    for key in ("register", "avg_sentence_len", "emoji_rate", "emoji_set", "punctuation_policy"):
        assert voice[key] == fp[key], key


def test_no_override_warning_when_llm_obeys(make_client):
    """模型没给 voice → 不该弹「模型想改语体」。警告多了等于没警告。"""
    with make_client() as c:
        body = _distill(c).json()
    assert not any("语体" in w for w in body["warnings"]), body["warnings"]


def test_resolve_conflicts_reused(make_client):
    """复用 M3-A 的冲突消解：外向 0.95 + 指纹算出极短句 [3, 8] → R1 把上界抬到 14。

    注意这里 R1 的改动**被保留**了（14 > 指纹的 8）：R1 是往上放宽，
    不违背统计事实——8 仍然落在 [3, 14] 里。
    """
    llm = _DistillLLM(payload={"meta": {"name": "林汐"}, "traits": {"extraversion": 0.95}})
    with make_client(llm) as c:
        body = _distill(c).json()
    assert body["persona"]["traits"]["extraversion"] == pytest.approx(0.95)
    assert body["fingerprint"]["avg_sentence_len"] == [3, 8]
    assert body["persona"]["voice"]["avg_sentence_len"] == [3, 14]
    assert any("外向" in w for w in body["warnings"])


def test_resolve_conflicts_never_narrows_below_statistics(make_client):
    """冲突消解只许**放宽**区间，不许把统计出来的句长改小。

    R1 是唯一会动 voice 的规则，它把上界抬到 14——指纹上界恒 ≥8，
    所以命中时只会放宽。这里用一组长度扫一遍，断言不变量：
    `persona.voice.avg_sentence_len` 的上界 ≥ `fingerprint` 的上界。
    """
    llm = _DistillLLM(payload={"meta": {"name": "林汐"}, "traits": {"extraversion": 0.95}})
    with make_client(llm) as c:
        for n in range(1, 16):
            body = _distill(c, transcript="\n".join([f"林汐: {'字' * n}"] * 3)).json()
            fp_hi = body["fingerprint"]["avg_sentence_len"][1]
            card_hi = body["persona"]["voice"]["avg_sentence_len"][1]
            assert card_hi >= fp_hi, f"句长 {n} 时冲突消解把上界从 {fp_hi} 收窄到 {card_hi}"


def test_bad_json_422(make_client):
    with make_client(_DistillLLM(raw="我觉得这个人挺好的")) as c:
        assert _distill(c).status_code == 422


def test_non_object_json_422(make_client):
    with make_client(_DistillLLM(raw="[1, 2, 3]")) as c:
        assert _distill(c).status_code == 422


def test_fenced_json_ok(make_client):
    raw = "```json\n" + json.dumps({"meta": {"name": "林汐"}}, ensure_ascii=False) + "\n```"
    with make_client(_DistillLLM(raw=raw)) as c:
        assert _distill(c).status_code == 200


def test_distill_marks_provenance_cloned(make_client):
    """蒸馏产物在**落库前**就带 cloned：落库那一步只是把它存下来。"""
    with make_client() as c:
        body = _distill(c).json()
    assert body["persona"]["provenance"]["type"] == PROVENANCE_CLONED
    assert body["persona"]["provenance"]["cloned_from"] == "林汐"


def test_client_provenance_is_dropped_by_model_side(make_client):
    """模型自己塞 provenance 不采信——合规标记只能由服务端写。"""
    llm = _DistillLLM(payload={
        "meta": {"name": "林汐"},
        "provenance": {"type": "authored", "template": "linxi"},
    })
    with make_client(llm) as c:
        body = _distill(c).json()
    assert body["persona"]["provenance"]["type"] == PROVENANCE_CLONED
    assert "template" not in body["persona"]["provenance"]


def test_meta_name_falls_back_to_target(make_client):
    """模型忘了给名字 → 用用户指定的名字，不要造一个「（无名）」。"""
    llm = _DistillLLM(payload={"meta": {}, "traits": {"extraversion": 0.4}})
    with make_client(llm) as c:
        body = _distill(c).json()
    assert body["persona"]["meta"]["name"] == "林汐"


# --------------------------------------------------------------------------- #
# 判据 10 & 11：落库时的 provenance.type
# --------------------------------------------------------------------------- #
def test_confirm_keeps_cloned(make_client, repo):
    """判据 10：蒸馏 → 确认落库 → provenance.type 仍是 cloned，不是 generated。"""
    with make_client() as c:
        draft = _distill(c).json()
        created = c.post("/v1/personas", json={"persona": draft["persona"]}).json()
    card = repo.get_persona(created["id"])
    assert card.provenance.get("type") == "cloned"


def test_create_respects_incoming_type(make_client, repo):
    """判据 11：传 cloned 就存 cloned。"""
    payload = {"meta": {"name": "张三"}, "provenance": {"type": "cloned", "cloned_from": "张三"}}
    with make_client() as c:
        created = c.post("/v1/personas", json={"persona": payload}).json()
    assert repo.get_persona(created["id"]).provenance["type"] == "cloned"


def test_create_defaults_to_generated(make_client, repo):
    """不传才默认 generated——M3-A 的一句话生成不能被这次改动带坏。"""
    with make_client() as c:
        created = c.post("/v1/personas", json={"persona": {"meta": {"name": "李四"}}}).json()
    assert repo.get_persona(created["id"]).provenance["type"] == "generated"


def test_create_blank_type_falls_back_to_generated(make_client, repo):
    with make_client() as c:
        created = c.post(
            "/v1/personas",
            json={"persona": {"meta": {"name": "王五"}, "provenance": {"type": "  "}}},
        ).json()
    assert repo.get_persona(created["id"]).provenance["type"] == "generated"


def test_create_keeps_other_provenance_keys(make_client, repo):
    """只补 type 的缺省，不吞掉别的键（cloned_from 是审计线索）。"""
    payload = {"meta": {"name": "赵六"}, "provenance": {"type": "cloned", "cloned_from": "赵六"}}
    with make_client() as c:
        created = c.post("/v1/personas", json={"persona": payload}).json()
    assert repo.get_persona(created["id"]).provenance["cloned_from"] == "赵六"


def test_full_flow_clone_then_list(make_client, repo):
    """蒸馏 → 确认 → 出现在「我的人格」里。"""
    before = len(repo.list_personas())
    with make_client() as c:
        draft = _distill(c).json()
        created = c.post("/v1/personas", json={"persona": draft["persona"]}).json()
        names = [p["name"] for p in c.get("/v1/personas").json()["personas"]]
    assert len(repo.list_personas()) == before + 1
    assert created["name"] in names


# --------------------------------------------------------------------------- #
# mock provider：免密钥可演示
# --------------------------------------------------------------------------- #
def test_mock_provider_distill_has_no_voice():
    from linger.llm.mock import MockProvider

    result = distill_persona(parse_transcript(_TRANSCRIPT), "林汐", MockProvider())
    assert result.persona.voice.register == "口语"  # 来自指纹，不是 mock
    assert result.persona.provenance["type"] == "cloned"
    assert len(result.preview) == 6
    assert result.persona.meta.identity  # 推断字段由 mock 补上了


def test_mock_distill_json_has_no_voice_key():
    """mock 样本本身不含 voice——否则演示会看起来像「模型在跟统计打架」。"""
    from linger.llm import mock

    data = json.loads(mock._MOCK_DISTILL_JSON)
    assert "voice" not in data
    assert {"meta", "traits", "values"} <= set(data)


# --------------------------------------------------------------------------- #
# 判据 12：前端契约
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def html() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def js() -> str:
    return (STATIC / "app.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def css() -> str:
    return (STATIC / "style.css").read_text(encoding="utf-8")


def test_html_has_distill_panel(html):
    for el_id in (
        "btn-show-distill",
        "distill-panel",
        "distill-transcript",
        "distill-target",
        "distill-authorized",
        "btn-distill",
        "distill-result",
    ):
        assert f'id="{el_id}"' in html, f"缺元素: #{el_id}"


def test_html_button_starts_disabled(html):
    """首屏（还没勾选）时按钮就是 disabled——不能等 JS 跑完才禁用。"""
    idx = html.index('id="btn-distill"')
    assert "disabled" in html[idx: idx + 120]


def test_html_checkbox_starts_unchecked(html):
    """授权默认不勾：不能替用户做这个承诺。"""
    idx = html.index('id="distill-authorized"')
    assert "checked" not in html[max(0, idx - 80): idx + 80]


def test_html_consent_text(html):
    assert "授权" in html and "同意" in html


def test_html_placeholder_shows_format(html):
    """placeholder 要给出格式示例，否则用户不知道「一行一句」是什么意思。"""
    idx = html.index('id="distill-transcript"')
    block = html[idx: idx + 400]
    assert "林汐" in block or "：" in block


def test_js_calls_distill_endpoint(js):
    assert "/v1/personas/distill" in js, "JS 没调蒸馏端点"
    assert "distillDraft" in js
    assert "authorized: true" in js or "authorized:true" in js, "请求体应带 authorized"


def test_js_gate_disables_button(js):
    """checkbox → 按钮 disabled 的联动必须存在。"""
    assert "syncDistillGate" in js
    assert "btnDistill.disabled = !ok" in js
    assert "distillAuthorized.checked" in js


def test_js_confirms_via_create(js):
    """确认后落库：调 POST /v1/personas 且 body 带 persona，再开会话。"""
    assert "confirmDistill" in js
    assert "btn-confirm-distill" in js
    assert "openSession(" in js


def test_js_renders_fingerprint(js):
    """指纹要能在界面上被看见——这是「可核对」的前提。"""
    assert "renderFingerprint" in js
    for key in ("avg_sentence_len", "emoji_rate", "verbal_tics", "high_freq_words"):
        assert key in js, f"前端未展示指纹字段: {key}"


def test_js_resets_consent_after_clone(js):
    """授权是一次性的：复刻完就重置，下一次必须重新确认。"""
    idx = js.index("async function confirmDistill")
    assert "distillAuthorized.checked = false" in js[idx: idx + 1200]


def test_distill_styles_defined(js, css):
    for cls in (
        "distill-transcript",
        "distill-consent",
        "distill-target",
        "fp-block",
        "fp-chips",
        "fp-chip",
        "fp-row",
        "fp-key",
        "fp-tag",
        "cloned-mark",
    ):
        assert cls in js, f"JS 未使用样式类: {cls}"
        assert f".{cls}" in css, f"CSS 缺样式: .{cls}"


def test_distill_route_registered():
    from linger.server.app import app

    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/v1/personas/distill" in paths
