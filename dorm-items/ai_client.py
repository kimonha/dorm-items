"""Gemini 이용 기록 요약 요청과 응답 검사."""

import http.client
import json
from urllib.parse import quote
from urllib.request import Request, urlopen


class AIUnavailable(Exception):
    """외부 요약을 제공할 수 없을 때 호출자에게 전달하는 오류."""


SUMMARY_INSTRUCTION = '기숙사 물품 관리자를 위한 한국어 요약을 3개 이내 문단으로 작성하세요. 제공된 집계만 사용하고 물품 번호로 참조하세요. 예약 통계와 실제 사용 통계를 구분하세요. 반납 시각은 실제 물리적 반납을 감지한 시각이 아니라 사용자의 반납 완료 등록 시각입니다. 예약 집중 시간대와 미반납·지연 등록을 요약하고 관리 제안은 사실과 구분하세요. 사용자의 신원이나 원인을 추측하지 마세요. 예약·반납 상태는 변경하지 않습니다.'


def generate_summary(facts, *, api_key, model, timeout):
    if not api_key:
        raise AIUnavailable('외부 AI API 키가 설정되지 않았습니다. 집계와 원본 기록을 확인해 주세요.')

    payload = {
        'systemInstruction': {'parts': [{'text': SUMMARY_INSTRUCTION}]},
        'contents': [{'role': 'user', 'parts': [
            {'text': json.dumps(facts, ensure_ascii=False)},
        ]}],
        'generationConfig': {'maxOutputTokens': 2048},
    }
    request = Request(
        'https://generativelanguage.googleapis.com/v1beta/models/'
        f'{quote(model, safe="")}:generateContent',
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'x-goog-api-key': api_key},
        method='POST',
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(100001)
        if len(raw) > 100000:
            raise ValueError('요약 응답이 너무 큽니다.')
        candidate = json.loads(raw)['candidates'][0]
        if candidate.get('finishReason') != 'STOP':
            raise ValueError('요약이 완료되지 않았습니다.')
        parts = candidate['content']['parts']
        answer = ''.join(part.get('text', '') for part in parts if not part.get('thought'))
        if not answer.strip():
            raise ValueError('요약 결과가 비어 있습니다.')
        return answer.strip()
    except (OSError, http.client.HTTPException, ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        raise AIUnavailable(
            '외부 AI 요청이 실패했거나 시간이 초과되었습니다. 집계와 원본 기록을 확인해 주세요.'
        ) from exc
