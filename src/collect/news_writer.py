"""AI 헤드라인/캡션 초안 작성 — Gemini API.

왜 초안이지 최종본이 아닌가
  뉴스 감지는 매체 수/키워드 같은 규칙 기반이라 결과를 항상 재현하고 검증할 수
  있었다. 문구 생성은 다르다 — 모델이 사실을 잘못 옮기거나 과장할 수 있고,
  이건 규칙으로 100% 못 막는다. 그래서 결과를 텔레그램 알림에 미리 보여주고,
  사람이 사진 답장의 캡션에 직접 쓰면 그 문구가 AI 초안을 덮어쓴다(항상 사람이
  최종 승인권을 가짐 — 렌더된 카드도 승인 버튼을 누르기 전엔 발행되지 않는다).

왜 그대로 베끼면 안 되는가
  사실 자체는 저작권 보호 대상이 아니지만 표현(문장)은 보호된다. 그래서 프롬프트
  에서 여러 매체의 제목·요약을 '참고 자료'로만 주고, 사실만 추려 완전히 새로운
  문장으로 쓰라고 명시적으로 지시한다. 서로 다른 매체 표현을 여러 개 섞어 주는
  것 자체가 모델이 특정 매체 문장을 그대로 베끼는 걸 억제하는 효과도 있다.

실패해도 파이프라인은 안 죽는다
  API 키가 없거나, 네트워크 오류거나, 응답 형식이 이상하면 WriterError 를
  던진다. 호출부(scripts/news_watch.py)는 이걸 잡아서 초안 없이 진행한다 —
  이 경우 사람이 사진 답장 캡션에 헤드라인을 직접 써야 하는 기존 경로로
  조용히 돌아간다.
"""

from __future__ import annotations

import json
import os
import re

import requests

from .news_trend import Topic

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
# flash 계열 — 카드 한 장당 비용이 작아야 하는 용도라 pro 계열은 안 씀.
# 모델명은 자주 갱신되니 .env 의 GEMINI_MODEL 로 언제든 바꿀 수 있게 한다.
# 2026-08: gemini-2.5-flash 가 "신규 사용자에게 더 이상 제공 안 함"으로 막혀
# gemini-3.6-flash 로 교체 (현재 GA 최신 flash 모델).
DEFAULT_MODEL = "gemini-3.6-flash"


class WriterError(RuntimeError):
    pass


def _api_key() -> str:
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise WriterError(
            "GEMINI_API_KEY 가 없습니다. https://aistudio.google.com/apikey 에서 "
            "발급 후 .env 에 추가하세요."
        )
    return key


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n?|```\s*$")


def _extract_json(text: str) -> dict:
    text = _FENCE_RE.sub("", text.strip()).strip()
    m = _JSON_RE.search(text)
    if not m:
        raise WriterError(f"AI 응답에서 JSON을 못 찾음: {text[:200]!r}")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise WriterError(f"AI 응답 JSON 파싱 실패: {e}") from e


_PROMPT = """당신은 KBO(한국프로야구) 스포츠 뉴스 인스타그램 계정의 카드뉴스 작가입니다.
아래는 서로 다른 언론사가 같은 사건을 보도한 제목과 요약입니다. 이걸 참고해서
카드뉴스에 쓸 문구를 새로 작성하세요.

절대 규칙
- 아래 문장을 그대로 베끼거나 몇 단어만 바꿔 쓰지 마세요. 사실만 파악해서
  당신의 표현으로 완전히 새로 쓰세요(저작권 때문에 원문 표현을 그대로 옮기면
  안 됩니다).
- 아래 자료에 없는 사실을 지어내지 마세요.
- 과장하거나 낚시성으로 왜곡하지 마세요. 사실을 정확히, 다만 흥미롭게 쓰세요.

자료:
{sources}

다음 JSON 형식으로만 답하세요(다른 설명 없이 JSON 하나만):
{{
  "line1": "카드 상단 헤드라인 1행 (12~18자 내외, 핵심 사실 하나만)",
  "line2": "헤드라인 2행 (선택, 부연 설명 한 마디, 필요 없으면 빈 문자열)",
  "hook": "카드 우상단에 붙는 짧은 강조어 (선택, 예: 단독/속보/충격, 필요 없으면 빈 문자열)",
  "caption_body": "인스타그램 캡션에 들어갈 기사체 2~4문장. 신문 기사체(-다 체)로, 자료에 있는 사실만."
}}"""


def draft(topic: Topic, *, model: str | None = None) -> dict[str, str]:
    """주제 하나로 헤드라인/캡션 초안을 만든다. 실패하면 WriterError."""
    material = topic.source_material(5)
    if not material:
        raise WriterError("소재로 쓸 기사가 없음")

    key = _api_key()
    # os.getenv(key, default) 는 환경변수가 "존재하되 빈 문자열"이면 default 로
    # 안 넘어간다 (.env 의 GEMINI_MODEL= 처럼) — 그래서 빈 문자열도 명시적으로 걸러낸다.
    model = model or os.getenv("GEMINI_MODEL") or DEFAULT_MODEL

    sources = "\n".join(
        f"- [{m['outlet']}] {m['title']}" + (f" — {m['description']}" if m["description"] else "")
        for m in material
    )
    prompt = _PROMPT.format(sources=sources)

    try:
        r = requests.post(
            f"{API_BASE}/{model}:generateContent",
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.4, "maxOutputTokens": 500},
            },
            timeout=30,
        )
    except requests.RequestException as e:
        raise WriterError(f"Gemini 호출 네트워크 오류: {e}") from e

    if r.status_code != 200:
        raise WriterError(f"Gemini 호출 실패 {r.status_code}: {r.text[:300]}")

    body = r.json()
    try:
        text = body["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as e:
        raise WriterError(f"Gemini 응답 형식이 예상과 다름: {str(body)[:300]}") from e

    data = _extract_json(text)
    line1 = (data.get("line1") or "").strip()
    if not line1:
        raise WriterError("AI가 line1(헤드라인)을 안 만들어줌")

    return {
        "line1": line1,
        "line2": (data.get("line2") or "").strip(),
        "hook": (data.get("hook") or "").strip(),
        "caption_body": (data.get("caption_body") or "").strip(),
    }
