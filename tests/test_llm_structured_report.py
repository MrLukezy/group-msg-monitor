"""结构化报告规范化与卡片判定。"""

from __future__ import annotations

from app.llm.service import (
    has_structured_key_points,
    normalize_key_point,
    normalize_report_payload,
    structured_report_for_api,
)


def test_normalize_string_key_point():
    p = normalize_key_point("群里在讨论部署", index=0)
    assert p["title"] == "群里在讨论部署"
    assert p["deep_dive"] == {"detail": "", "evidence": ""}
    assert p["nouns"] == []


def test_normalize_merges_top_level_deep_dives():
    report = {
        "headline": "测试",
        "key_points": ["要点 甲", "要点 乙"],
        "deep_dives": [
            {"topic": "仓库分析", "detail": "这是深入内容" * 10, "evidence": "原文片段"},
        ],
        "appendix": {},
    }
    normalize_report_payload(report)
    assert len(report["key_points"]) >= 1
    assert report["key_points"][0]["deep_dive"]["detail"].startswith("这是深入内容")
    assert report["key_points"][0]["title"] == "仓库分析"


def test_has_structured_object_points():
    payload = {
        "key_points": [
            {
                "title": "部署方案",
                "summary": "讨论了灰度发布",
                "deep_dive": {"detail": "详细分析", "evidence": "原文"},
                "nouns": [],
                "links": [],
                "notes": [],
            }
        ]
    }
    assert has_structured_key_points(payload) is True


def test_has_structured_rejects_plain_string_points():
    payload = {"key_points": ["只是一句话", "另一句"]}
    assert has_structured_key_points(payload) is False


def test_has_structured_accepts_merged_deep_dives():
    payload = {
        "key_points": ["GitHub 仓库"],
        "deep_dives": [
            {"topic": "repo", "detail": "深入展开的长文分析内容", "evidence": "链接"},
        ],
    }
    assert has_structured_key_points(payload) is True


def test_structured_report_for_api_camel_case():
    payload = {
        "headline": "标题",
        "sentiment": "neutral",
        "topics": ["A"],
        "key_points": [
            {
                "title": "T",
                "summary": "S",
                "deep_dive": {"detail": "D", "evidence": "E"},
                "nouns": [{"term": "LLM", "meaning": "大模型"}],
                "links": [],
                "notes": [],
            }
        ],
        "risks": [],
        "action_items": [],
        "notable_users": [],
        "appendix": {"nouns": [], "links": [], "notes": []},
        "context_usage": {"used_earlier_context": False},
    }
    out = structured_report_for_api(payload)
    assert out["headline"] == "标题"
    assert out["keyPoints"][0]["title"] == "T"
    assert out["keyPoints"][0]["nouns"][0]["term"] == "LLM"
    assert "actionItems" in out
    assert "notableUsers" in out
