# Baldur

[![CI](https://github.com/baldurhq/baldur/actions/workflows/ci-oss-mirror.yml/badge.svg)](https://github.com/baldurhq/baldur/actions/workflows/ci-oss-mirror.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)
[![PyPI](https://img.shields.io/pypi/v/baldur-framework.svg)](https://pypi.org/project/baldur-framework/)
[![Docs](https://img.shields.io/badge/docs-baldur.sh-1f6feb.svg)](https://baldur.sh)
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/13522/badge)](https://www.bestpractices.dev/projects/13522)

[English](https://github.com/baldurhq/baldur/blob/main/README.md) | **한국어**

> **초기 사용자 피드백을 받고 있습니다.** 실제 서비스에 붙여 보다가 설치·문서·예상과 다른 동작 등 막히는 곳이 있으면 [Discussions](https://github.com/baldurhq/baldur/discussions)나 [이슈](https://github.com/baldurhq/baldur/issues/new/choose)로 알려 주세요.

**여러분이 기대고 있는 외부 API가 한 시간 동안 죽으면, 여러분의 앱에는 무슨 일이 생기나요?**

요청은 타임아웃까지 매달려 있고, 워커는 전부 차 버리고, 그 한 시간 동안 실패한
작업은 사라집니다. OpenAI든, 결제 제공자든, 이메일 서비스든 — Baldur는 이 세
가지를 데코레이터 하나로 해결합니다. 온콜 담당자가 따로 없는 Python 서비스를
위해서요.

```python
import baldur
from openai import OpenAI

llm = baldur.llm.wrap(OpenAI(), timeout=60.0)


@baldur.protected("summarize", replay=True)
def summarize(doc_id: str) -> str:
    response = llm.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": load_document(doc_id)}],
    )
    return response.choices[0].message.content
```

Redis도, Docker도, 설정도 없이 시작합니다. 저 두 줄은 멀티 프로세스로 가기
전까지 인메모리로 동작합니다. LLM을 부르지 않나요? 데코레이터만으로도 [어떤
의존성이든](#같은-데코레이터-어떤-의존성이든) 똑같이 됩니다.

트래픽이 흐르는 중에 제공자가 호출 한도를 걸거나, 죽거나, 그냥 느려지기만 해도:

- **워커들이 함께 물러섭니다.** 429나 "과부하" 응답이 오면 모든 워커가 공유하는
  대기가 하나 걸리고, 그 길이는 제공자가 요청한 시간 이상입니다. 첫 워커만 거절당하고
  나머지는 기다립니다 — 워커마다 따로 한도를 부딪쳐 알아내지 않습니다. 제공자가
  거절한 요청(400, 422)은 재시도하지 않습니다.
- **앱은 계속 응답합니다.** 매달린 요청은 60초 상한에서 실패로 바뀌고, 서킷
  브레이커가 열리며, 호출은 즉시 실패합니다 — 느려진 제공자 하나가 워커 전체를
  끌고 내려가지 않습니다. wrap에 `fallbacks=[...]`를 주면 호출이 다음 엔드포인트로
  넘어갑니다(OpenAI SDK, Anthropic SDK, google-genai).
- **실패한 작업은 사라지지 않고 보관됩니다.** 끝내 실패한 호출은 전부 인자와
  함께 포착되어 내장 콘솔(`http://127.0.0.1:9090/`)에 목록으로 남습니다.
  프레임워크 없이 쓰는 파이썬 프로세스에서는 시작할 때 `baldur.init()`을 한 번
  호출해야 콘솔이 뜹니다. Django·FastAPI·Flask 연동은 이 호출을 알아서 해 줍니다.
- **그리고 돌아옵니다.** `replay=True`면 Baldur가 보관된 작업을 저장된 인자로
  다시 실행합니다. 콘솔에서 클릭 한 번으로, 또는 제공자가 복구되어 그 작업의
  브레이커가 닫히는 순간 자동으로 — 재실행은 Celery 워커가 돌립니다.

실제 `openai` SDK와 로컬 가짜 제공자로 직접 보세요 — 호출 한도, 이어서 장애,
그리고 보관된 작업 전부의 재실행:

![터미널 데모: 워커 8개가 LLM 제공자의 429(retry-after 5)를 만납니다 — SDK 자체 재시도로는 대기 시간 안에 요청 8건이 제공자에 도착하고, baldur.llm.wrap을 거치면 1건입니다. 이어서 제공자가 503을 돌려주자 작업 12건이 인자와 함께 보관되고, 제공자가 복구되어 브레이커가 닫히면 12건 모두 자동으로 다시 실행됩니다. 유실 0건.](https://raw.githubusercontent.com/baldurhq/baldur/main/.github/assets/demo-llm-outage.gif)

*실제 실행을 실제 시간 그대로 녹화했습니다. 직접 재현해 보세요:*

```bash
pip install "baldur-framework[celery]" openai
python -m baldur.scripts.demo_llm_outage
```

**Baldur가 고장 나면 내 호출도 실패하나요?** 아닙니다. 실패한 작업을 보관하다가
오류가 나는 식으로 Baldur 자체 기록 작업이 실패하면 로그로만 남고, 호출은 원래
결과를 돌려주거나 원래 오류를 던집니다. Redis에 연결할 수 없으면 호출은 각
프로세스의 자체 상태로 계속 돌고, Redis가 돌아올 때까지 워커들이 429 대기와
브레이커를 공유하지 못할 뿐입니다. 예외가 하나 있고 의도된 동작입니다.
`idempotency_key=`를 단 호출은 두 번 실행을 막아 주는 공유 기록 없이 실행하는
대신, 몇 초 기다린 뒤 `IdempotencyUnavailableError`로 거절됩니다.

**재시도나 재실행 때문에 요금이 두 번 나갈 수 있나요?** 네, 그러니 대비해야
합니다. 재실행은 작업 전체를 처음부터 다시 돌립니다. 작업 안에서 이미 성공했던
모델 호출도 다시 나가고 다시 과금됩니다. 시간 초과로 끝난 요청도 제공자 쪽에서는
끝까지 처리됐을 수 있어서, 재시도하면 두 번 과금될 수 있습니다. `replay=True`는
두 번 돌아도 괜찮은 작업에만 쓰거나, 단계마다 결과를 직접 저장해서 이미 끝난
단계는 건너뛰게 하세요.

Django, FastAPI, Flask, Celery 어댑터가 들어 있습니다.

**SDK의 재시도를 이미 쓰고 있나요?** 데코레이터를 씌운 호출이라면 그대로 두세요.
Baldur는 재시도를 대체하지 않습니다 — 재시도가 못 하는 것을 더합니다. 장애 하나에
모든 요청이 재시도 비용을 치르지 않게 하는 브레이커, 호출자가 기다리는 시간의
단일 상한, 폴백, 그리고 어떤 재시도 라이브러리도 주지 않는 포착·재실행.
`baldur.llm.wrap` 클라이언트만은 예외입니다. 거기서는 조율된 재시도 하나가 SDK의
재시도를 대신하므로, 클라이언트 사본에서 SDK 자체 재시도를 끕니다.

## 설치

Python 패키지 이름은 `baldur`이고(`import baldur`), PyPI 배포 이름은
`baldur-framework`입니다.

```bash
pip install baldur-framework                 # 프레임워크 독립 코어
pip install baldur-framework[django]         # Django 연동
pip install baldur-framework[django-api]     # Baldur의 Django REST API (baldur.api.django.urls)
pip install baldur-framework[fastapi]        # FastAPI 연동
pip install baldur-framework[flask]          # Flask 연동
pip install baldur-framework[celery]         # Celery 태스크 보호
pip install baldur-framework[redis]          # Redis 기반 공유 상태
pip install baldur-framework[prometheus]     # Prometheus 메트릭
```

## 같은 데코레이터, 어떤 의존성이든

결제 게이트웨이, 데이터베이스, 이메일 제공자 — 호출부는 전혀 바뀌지 않습니다.

```python
@baldur.protected("charge-customer", dlq=True)
def charge(order_id: str, amount_cents: int) -> dict:
    # 기본적으로 서킷 브레이커로 감싸집니다. dlq=True는 끝내 실패한 호출을
    # 인자와 함께 보관해서, 게이트웨이가 돌아오면 다시 실행할 수 있게 합니다.
    return payment_gateway.charge(order_id, amount_cents)
```

게이트웨이가 죽으면 브레이커가 열리고, 서비스는 타임아웃을 쌓아 올리는 대신 즉시
응답합니다. 나가는 길에 실패한 결제는 데드레터 큐에서 기다리며 내장 콘솔에
표시됩니다. 결제를 두 번 실행하면 두 번 청구될 수 있으므로, 보관된 결제는 사용자가
정한 방식으로만 돌아옵니다. `charge-customer`에 어떤 결제가 다시 실행해도
안전한지 판단하는 재실행 핸들러를 등록하면, 콘솔에서 클릭 한 번으로, 또는 허용한
실패 유형에 한해 브레이커가 닫힐 때 자동으로 재실행됩니다 — 아래 데모가 바로
그렇게 합니다. 두 번 실행해도 괜찮은 작업이라면 `replay=True`만으로 핸들러 없이
됩니다. (재실행은 나가는 길에 실패한 작업을 위한 것이지, 비즈니스 상의 거절이나
고객이 이미 떠나버린 결제를 위한 것이 아닙니다 —
[그 경계가 어디인지](https://baldur.sh/concepts/foundations/dlq-replay/).)

![터미널 데모: 트래픽이 흐르는 중에 결제 게이트웨이가 응답 불능이 되고 결제 1,000건이 들어옵니다 — 5건은 게이트웨이까지 가서 실패하고, 브레이커가 열려 나머지 995건을 그 자리에서 거절하며, 1,000건이 전부 포착되어 복구 시점에 데모의 재실행 핸들러를 통해 10번에 나눠 1,000건이 전부 재실행됩니다. 유실 0건.](https://raw.githubusercontent.com/baldurhq/baldur/main/.github/assets/demo-payment-outage.gif)

*결제 데모입니다. 트래픽이 흐르는 중에 게이트웨이가 응답 불능이 되고 결제
1,000건이 들어옵니다. 5건은 게이트웨이까지 가서 실패하고, 열린 브레이커가 나머지
995건을 게이트웨이를 부르지 않고 거절하며, 1,000건이 전부 인자와 함께 포착되어
데모가 등록한 재실행 핸들러를 통해 복구 시점에 전부 재실행됩니다. 유실 0건. 실제 실행을 실제 시간 그대로 녹화한
화면이고, 브레이커 상태와 DLQ 집계는 프레임워크에서 실시간으로 읽어온 값입니다.
데코레이터 자체는 `pip install baldur-framework`가 전부입니다. 데모는 프로세스
안의 대역 워커를 위해 `celery` extra를 추가할 뿐, 여전히 프로세스 하나에 Redis도
브로커도 없습니다. 직접 재현해 보세요:*

```bash
pip install "baldur-framework[celery]"
python -m baldur.scripts.demo_self_healing --outage-charges 1000
```

기본값 이상이 필요하다면 파이프라인을 선언적으로 조합하면 됩니다.

```python
@baldur.protected(
    "summarize",
    timeout=30.0,                            # 호출자가 기다리는 시간의 단일 상한
    fallback=lambda: last_good_summary(),    # OPEN 상태일 때의 우아한 응답
    idempotency_key="doc_id",                # 재전달된 작업도 한 번만 처리
)
def summarize(doc_id: str) -> str:
    return llm_api.summarize(doc_id)
```

**여기 없는 것에 주목하세요: `retry=`.** 여러분의 SDK는 이미 재시도하고 있을
가능성이 큽니다 — `anthropic`과 `openai`는 백오프와 함께 2회 시도가 기본이고,
boto3에는 적응형 모드가 있습니다 — 그리고 범용 래퍼보다 더 잘 재시도합니다.
어떤 상태 코드가 한 번 더 시도할 가치가 있는지 알고 `retry-after`를 존중하기
때문입니다. 그건 그대로 두세요. 어떤 SDK도 주지 않는 건 나머지입니다. 브레이커 —
제공자 장애가 났을 때 *모든* 요청이 재시도 비용을 다 치르고 나서야 실패하는 일을
막아줍니다. 재시도까지 포함해 호출자가 기다리는 시간에 대한 단일 상한 — SDK 자체의
최악 경우는 `timeout × (max_retries + 1)`이고, `anthropic` 기본값이면 30분입니다.
그리고 폴백, 그리고 SDK가 볼 수 없는 작업 재전달에서도 살아남는 중복 제거 키.
`retry=True`는 스스로 재시도하지 않는 호출을 위해 준비되어 있습니다.

동기·비동기 호출 가능 객체를 모두 지원합니다 — 데코레이터가 코루틴 함수를
자동으로 감지합니다.

## 기본 제공 기능 (OSS, Apache-2.0)

| 기능 | 무엇을 해주는가 |
|------|-----------------|
| [서킷 브레이커](https://baldur.sh/concepts/oss/circuit-breaker/) | 연쇄 장애를 차단하고, 복구 시 제한된 수의 half-open 탐침을 보냅니다 |
| [백오프 재시도](https://baldur.sh/concepts/oss/retry/) | 지터가 적용된 지수 백오프와 상한이 있는 시도 횟수 |
| [폴백 및 조합](https://baldur.sh/concepts/foundations/composition/) | 모든 복원력 패턴을 순서가 정해진 하나의 파이프라인으로 |
| [멱등성](https://baldur.sh/concepts/oss/idempotency/) | 동시에 들어온 중복 호출에서도 부수 효과는 정확히 한 번만 실행됩니다 |
| [벌크헤드 격리](https://baldur.sh/concepts/foundations/bulkhead/) | 의존성마다 고정된 동시성 몫을 할당해, 느린 의존성 하나가 전체 워커를 고갈시키지 못하게 합니다 |
| [데드레터 큐 + 재실행](https://baldur.sh/concepts/foundations/dlq-replay/) | 끝내 실패한 호출을 문맥과 함께 포착해 두었다가, 의존성이 복구되면 재실행합니다 |
| [헬스 체크](https://baldur.sh/concepts/oss/health-check/) | 실제 의존성 상태를 반영하는 liveness/readiness |
| [우아한 종료](https://baldur.sh/concepts/oss/graceful-shutdown/) | 재시작과 배포 시 처리 중이던 작업을 깔끔하게 비웁니다 |
| [메트릭](https://baldur.sh/concepts/oss/metrics/) | Prometheus와 OpenTelemetry, 기본으로 방출 |
| [시스템 제어](https://baldur.sh/concepts/oss/system-control/) | Baldur 자동화에 대한 즉시 킬 스위치와 드라이런 모드 — 재배포 불필요 |
| [웹 콘솔](https://baldur.sh/concepts/foundations/web-console/) | 내장 운영 콘솔: 실시간 브레이커 상태, 제어, 복구 |
| [사전 계산 캐시](https://baldur.sh/concepts/oss/precomputed-cache/) | 헬스/상태 엔드포인트가 예열된 캐시에서 응답하므로, 끊임없는 프로빙도 비용이 낮게 유지됩니다 |

읽기 경로도 같은 방식으로 스스로 회복합니다. 아래는 실제 HTTP 트래픽을 받고 있는
Django 앱(데모 하네스가 트래픽을 넣는 상황을 녹화)이 21초 동안 Redis로 가는
네트워크 경로를 잃는 장면입니다. 모든 요청이 인메모리 캐시 계층에서 계속 200을
반환하고, Redis 계층은 복구 시점에 스스로 재동기화합니다.

![터미널 데모: Django 앱이 21초간의 Redis 장애 내내 200 응답을 유지합니다](https://raw.githubusercontent.com/baldurhq/baldur/main/.github/assets/redis-dies-app-survives.gif)

## 문서

전체 문서는 **<https://baldur.sh>** 에 있습니다.

- [What is Baldur?](https://baldur.sh/what-is-baldur/) — 어떤 문제를 어떻게 푸는지
- 시작하기: [Django](https://baldur.sh/getting-started/django/) ·
  [FastAPI](https://baldur.sh/getting-started/fastapi/) ·
  [Flask](https://baldur.sh/getting-started/flask/) ·
  [Celery](https://baldur.sh/getting-started/celery/)
- [개념 가이드](https://baldur.sh) — 기능당 한 페이지, 이 README 전반에서 링크
- [API 레퍼런스](https://baldur.sh/reference/)
- [문제 해결](https://baldur.sh/troubleshooting/)
- [호환성](https://baldur.sh/compatibility/)

## AI 어시스턴트와 함께 쓰기

AI 코딩 어시스턴트(Claude Code, Cursor, Copilot, Codex)로 개발하고 계신가요?
저장소에서 `baldur init-ai`를 실행하면 `AGENTS.md`(Cursor·Copilot·Codex가 읽습니다)와
그것을 임포트하는 Claude Code용 `CLAUDE.md`가 생성됩니다. 이 둘이 함께 어시스턴트에게
서킷 브레이커를 직접 구현하는 대신 `@baldur.protected("name")`을 쓰도록 가르칩니다.
[AI 어시스턴트와 함께 쓰기](https://baldur.sh/getting-started/ai-assistants/)를 참고하세요.

## 호환성

| 구성 요소 | 최소 버전 | CI 테스트 대상 |
|-----------|-----------|----------------|
| Python | 3.11 | 3.11 · 3.12 · 3.13 |
| Django | 4.2 | 4.2 LTS · 5.2 LTS · 6.0 |
| FastAPI | 0.100 | 최소 버전 이상 최신 (스모크) |
| Flask | 2.3 | 최소 버전 이상 최신 (스모크) |
| Celery | 5.3 | 5.4 |
| Redis 서버 | — | 7.x |

전체 매트릭스와 Python × Django 테스트 그리드, 버전 지원 정책은
[호환성](https://baldur.sh/compatibility/)을 참고하세요.

## 플릿 규모로 운영하시나요?

Baldur PRO는 동일한 API 위에 플릿 단위 운영을 위한 기계 장치를 더합니다 — 코어의
어떤 것도 라이선스가 바뀌거나 대체되지 않습니다.
[대규모 DLQ](https://baldur.sh/concepts/foundations/dlq-replay/)(콘솔에서의 일괄 재실행,
성공률 기반 속도 조절, 아카이브/삭제 보존 정책),
해시 체인 [감사 추적](https://baldur.sh/concepts/pro/audit/),
[통합 알림](https://baldur.sh/concepts/pro/unified-notification/),
[비상 모드](https://baldur.sh/concepts/pro/emergency-mode/),
[벌크헤드 스레드 풀 격리](https://baldur.sh/concepts/foundations/bulkhead/),
[적응형 스로틀링](https://baldur.sh/concepts/pro/throttle/),
[카나리 복구](https://baldur.sh/concepts/pro/canary-recovery/),
[거버넌스 게이트](https://baldur.sh/concepts/pro/governance/), 그리고 Baldur 자신을 감시하는
[메타 워치독](https://baldur.sh/concepts/pro/meta-watchdog/). 전체
[OSS vs PRO 기능 비교표](https://baldur.sh/concepts/oss-vs-pro/)와
[가격](https://baldur.sh/pricing/)을 확인해 보세요.

## 얼리 액세스

Baldur는 얼리 액세스 단계입니다. API는 안정적이고 코어는 Sentinel 페일오버를 포함한
지속 부하 테스트를 거쳤지만, 프로젝트 자체가 아직 어립니다 — 마이너 릴리스에도
호환성이 깨지는 변경이 들어갈 수 있으며, 그럴 때는 언제나 체인지로그 항목이 함께
갑니다. 지금은 이미 Python 서비스를 프로덕션에서 운영 중인 소수의 팀과 직접 협업할
상대를 찾고 있습니다. 해당되신다면 자세한 내용과 연락 방법이
[Discussions](https://github.com/baldurhq/baldur/discussions)에 있습니다.

여기까지 온 과정과 2026년 9월에 접을 뻔했던 이유는 [회고](https://github.com/baldurhq/baldur/blob/main/POSTMORTEM.ko.md)에
있습니다.

## 라이선스

Baldur는 Apache License 2.0으로 배포됩니다 — [LICENSE](https://github.com/baldurhq/baldur/blob/main/LICENSE)와
[NOTICE](https://github.com/baldurhq/baldur/blob/main/NOTICE)를 참고하세요.

## 기여하기

Apache License 2.0 아래에서의 기여를 환영합니다. 풀 리퀘스트는 사인오프 기반
[DCO](https://developercertificate.org/) 흐름으로 받습니다 — 전체 모델은
[CONTRIBUTING.md](https://github.com/baldurhq/baldur/blob/main/CONTRIBUTING.md)를 참고하세요.

- **아이디어, 또는 만드신 것 자랑** →
  [Discussions](https://github.com/baldurhq/baldur/discussions).
- **버그 / 기능 요청 / 문서** → 이슈나 풀 리퀘스트를 열어 주세요.
- **보안** → [SECURITY.md](https://github.com/baldurhq/baldur/blob/main/SECURITY.md)를 참고하세요 (취약점은 공개 이슈로 올리지 말아 주세요).
- **사용 문의 / 상용** → `support@baldur.sh`.
