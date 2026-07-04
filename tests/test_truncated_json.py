from imj_util.gigachat_client import parse_analysis_response

TRUNCATED = (
    '{"risks": [{"category": "sexual_content", "risk_level": "critical", '
    '"detected_signs": ["обнаженные части тела", "сексуально откровенные позы"], '
    '"legal_area": "13.1", "description": "Изображение содержит обнаженные части тела."}, '
    '{"category": "violence_humiliation", "risk_level": "none", "detected_signs": [], '
    '"legal_area": null, "description": "Признаки насилия не обнаружены."}, '
    '{"category": "minors", "risk_level": "none", "detected_signs": [], "legal_ar'
)


def test_salvage_truncated_json():
    parsed = parse_analysis_response(TRUNCATED)
    assert parsed.parsed_from_truncated_json
    assert parsed.report["overall_risk_level"] == "critical"
    assert len(parsed.report["risks"]) == 6
    assert parsed.report["risks"][0]["risk_level"] == "critical"
    assert parsed.report["manual_review_required"] is True


def test_valid_json_unchanged():
    raw = (
        '{"risks": [], "overall_risk_level": "low", '
        '"manual_review_required": false, "recommendations": [], "disclaimer": "x"}'
    )
    parsed = parse_analysis_response(raw)
    assert not parsed.parsed_from_truncated_json
    assert parsed.report["overall_risk_level"] == "low"
