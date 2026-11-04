"""keywords.has_geimori 品牌/机型判定测试（RGI-016：Mikuni T38 误报回归）。"""
from __future__ import annotations

from app import keywords


def test_brand_words_boundary():
    assert keywords.has_geimori("Just got my Geimori T38 grinder")
    assert keywords.has_geimori("mywirsh makes great grinders")
    assert keywords.has_geimori("check out Wirsh burr set")
    # 品牌词边界：不是子串即可命中
    assert not keywords.has_geimori("geimorisucks")
    assert not keywords.has_geimori("mywirshs xyz")


def test_model_words_match_in_grinder_context():
    assert keywords.has_geimori("Thinking about T38 vs T38 plus")
    assert keywords.has_geimori("my GU63 burrs are holding up great")
    assert keywords.has_geimori("GU64 Gen 2 arrived")
    assert keywords.has_geimori("T38 battery gen2 question")
    assert keywords.has_geimori("does gu38 fit the base?")


def test_mechanical_context_exclusion():
    """Mikuni T38 化油器 / Yamaha Virago 帖不得误判为 Geimori 提及。"""
    mikuni = ("The carburetor is Mikuni T38 and it pops when the throttle is "
              "opened off idle and bogging down at upper speeds. I have changed "
              "the pilot jet with a #15, moved the needle valve...")
    assert not keywords.has_geimori(mikuni)
    virago = ("Help with a Yamaha Virago 750 that a 2 carburetor engine to 1 "
              "carburetor. It pops under acceleration.")
    assert not keywords.has_geimori(virago)
    # 机械语境只挡机型词：同帖明确提品牌仍算提及
    mixed = "my virago is fine, but my geimori gu63 broke"
    assert keywords.has_geimori(mixed)


def test_napier_t38_automobile_not_geimori():
    """RGI-017：Napier T38 英国古董汽车/摩托车数据库帖不得误判为 Geimori 提及。
    2026-11-04 Daily Pulse 误报：r/GlobalAutoDatabase 自动帖标题含 T38，
    无化油器词但属于汽车/自行车语境，此前被 t38 机型词命中。"""
    napier = ("Napier T38 Colonial Deluxe British Rugged Edwardian Tourer")
    assert not keywords.has_geimori(napier)
    bicycle = "Vintage Napier T38 bicycle restoration project"
    assert not keywords.has_geimori(bicycle)
    # 正常磨豆机 T38 帖不受语境词影响
    assert keywords.has_geimori("T38 grinder retention is low after RDT")
    assert keywords.has_geimori("Geimori T38 battery gen2 lasts long")


def test_word_boundary():
    assert not keywords.has_geimori("t380 display error")
    assert not keywords.has_geimori("notgu63x here")
    assert not keywords.has_geimori("t38plusnomatch")  # 无边界


def test_has_any_unaffected():
    """has_any 保持原通用子串语义（供竞品/话题统计）。"""
    assert keywords.has_any("I love my GRINDER", keywords.GRINDER_KEYWORDS)
    assert not keywords.has_any("cat kettle", keywords.GRINDER_KEYWORDS)
