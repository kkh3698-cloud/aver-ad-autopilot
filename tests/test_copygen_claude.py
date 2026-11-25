"""Claude 경로: 요청 형태와 응답 파싱·검증을 실제 API 호출 없이 확인."""
import json
import shutil
import sys
import types
from pathlib import Path

from naver_growth import copygen
from naver_growth.config import load_config

ROOT = Path(__file__).resolve().parent.parent


def test_claude_variants_parsed_and_validated(tmp_path, monkeypatch):
    shutil.copy(ROOT / "config.example.yaml", tmp_path / "config.yaml")
    cfg = load_config(tmp_path / "config.yaml")
    cfg.secrets["ANTHROPIC_API_KEY"] = "test"
    captured = {}

    refs = [{"id": 11, "source": "youtube", "metric_label": "조회수 120만", "title": "퇴근 후 1시간 컷트 브이로그",
             "snippet": "직장인 퇴근 후 방문 루틴을 보여주는 영상입니다"}]
    payload = {"patterns": [{"pattern": "시간 효율", "evidence_ids": [11]}], "variants": [
        {"headline": "강남역 퇴근길 커트", "description": "평일 21시까지, 네이버 예약 후 바로 시술",
         "hypothesis": "시간 편의", "pattern": "시간 효율", "inspired_by": [11, 999]},
        {"headline": "강남 최고 커트", "description": "금지어 포함이라 탈락해야 함", "hypothesis": "x",
         "pattern": "x", "inspired_by": []},
        {"headline": "가" * 20, "description": "제목이 길어서 탈락", "hypothesis": "x", "pattern": "x", "inspired_by": []},
        {"headline": "퇴근 루틴 커트", "description": "직장인 퇴근 후 방문 루틴을 보여주는 영상", "hypothesis": "x",
         "pattern": "x", "inspired_by": [11]},
    ]}

    class FakeMessages:
        def create(self, **kw):
            captured.update(kw)
            block = types.SimpleNamespace(type="text", text=json.dumps(payload, ensure_ascii=False))
            return types.SimpleNamespace(stop_reason="end_turn", content=[block])

    class FakeClient:
        def __init__(self, *a, **k):
            self.beta = types.SimpleNamespace(messages=FakeMessages())

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=FakeClient))
    ok, rejected, patterns = copygen.generate(cfg, "커트", [], [], set(), n=3, refs=refs)

    assert captured["model"] == "claude-opus-5-5"
    assert captured["output_config"]["format"]["type"] == "json_schema"
    assert captured["fallbacks"] == "default"
    assert ok[0].headline == "강남역 퇴근길 커트" and ok[0].source == "claude"
    assert ok[0].inspired_by == [11]                      # 존재하지 않는 id(999)는 버림
    assert patterns[0]["pattern"] == "시간 효율"
    assert any("연속 일치" in r for r in rejected)          # 원문 베낀 문구 탈락
    user_msg = captured["messages"][0]["content"]
    assert "<reference_content>" in user_msg and "퇴근 후 1시간" in user_msg
    assert any("금지어" in r for r in rejected) and any("제목" in r for r in rejected)
    assert len(ok) <= 3
