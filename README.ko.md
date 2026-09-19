# Baldur

[![CI](https://github.com/baldurhq/baldur/actions/workflows/ci-oss-mirror.yml/badge.svg)](https://github.com/baldurhq/baldur/actions/workflows/ci-oss-mirror.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)
[![PyPI](https://img.shields.io/pypi/v/baldur-framework.svg)](https://pypi.org/project/baldur-framework/)
[![Docs](https://img.shields.io/badge/docs-baldur.sh-1f6feb.svg)](https://baldur.sh)
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/13522/badge)](https://www.bestpractices.dev/projects/13522)

[English](README.md) | **한국어**

> **이 프로젝트는 완료되었습니다.** 무엇을 만들었고, 숫자가 무엇을 말했고, 무엇이 남았는지: [회고](POSTMORTEM.ko.md).

**여러분이 기대고 있는 외부 API가 한 시간 동안 죽으면, 여러분의 앱에는 무슨 일이 생기나요?**

요청은 타임아웃까지 매달려 있고, 워커는 전부 차 버리고, 그 한 시간 동안 실패한
작업은 사라집니다. OpenAI든, 결제 제공자든, 이메일 서비스든 — Baldur는 이 세
가지를 데코레이터 하나로 해결합니다. 온콜 담당자가 따로 없는 Python 서비스를
위해서요.

```python
import baldur


@baldur.protected("summarize", dlq=True, timeout=60.0)
def summarize(doc_id: str) -> str:
    return llm_api.summarize(doc_id)
```

Redis도, Docker도, 설정도 없이 시작합니다. 저 데코레이터는 멀티 프로세스로 가기
전까지 인메모리로 동작합니다.

트래픽이 흐르는 중에 제공자가 죽거나 — 그냥 느려지기만 해도:

- **앱은 계속 응답합니다.** 60초 상한에서 매달린 호출이 실패로 바뀌고, 서킷
  브레이커가 열리며, 호출은 즉시 실패합니다 — 느려진 제공자 하나가 워커 전체를
  끌고 내려가지 않습니다. 그 제공자가 필요 없는 엔드포인트는 계속 동작합니다.
- **실패한 작업은 사라지지 않고 보관됩니다.** 끝내 실패한 호출은 전부 인자와
  함께 포착되어 내장 콘솔에 목록으로 남습니다.
- **그리고 돌아옵니다.** 작은 재실행 핸들러로 하나를 어떻게 다시 실행하는지
  알려주면, 보관된 작업을 콘솔에서 클릭 한 번으로 재실행하거나, 제공자가
  복구되는 순간 자동으로 재실행합니다 — 옵트인이며, Celery 워커가 필요합니다.

Django, FastAPI, Flask, Celery 어댑터가 들어 있습니다.

![터미널 데모: 트래픽이 흐르는 중에 결제 게이트웨이가 응답 불능이 됩니다 — 결제 5건이 재시도 끝에 실패하고 브레이커가 열리며, 2건은 그 자리에서 거절되고, 7건이 전부 포착되어 복구 시점에 Baldur가 7건을 전부 재실행합니다. 유실 0건.](https://raw.githubusercontent.com/baldurhq/baldur/main/.github/assets/demo-self-healing.gif)

*함께 배포되는 데모의 의존성은 결제 게이트웨이입니다. 트래픽이 흐르는 중에
게이트웨이가 응답 불능이 되고, 결제 7건이 인자와 함께 포착되며, 복구 시점에 7건이
전부 재실행됩니다. 유실 0건. 어떤 호출이든 같은 루프입니다 — 실제 실행 화면이고,
브레이커 상태와 DLQ 집계는 프레임워크에서 실시간으로 읽어온 값입니다. 데코레이터
자체는 `pip install baldur-framework`가 전부입니다. 데모는 프로세스 안의 대역
워커를 위해 `celery` extra를 추가할 뿐, 여전히 프로세스 하나에 Redis도 브로커도
없습니다. 직접 재현해 보세요:*

```bash
pip install "baldur-framework[celery]"
python -m baldur.scripts.demo_self_healing
```

**SDK의 재시도를 이미 쓰고 있나요?** 그대로 두세요. Baldur는 재시도를 대체하지
않습니다 — 재시도가 못 하는 것을 더합니다. 장애 하나에 모든 요청이 재시도 비용을
치르지 않게 하는 브레이커, 호출자가 기다리는 시간의 단일 상한, 폴백, 그리고 어떤
재시도 라이브러리도 주지 않는 포착·재실행.

## 설치

Python 패키지 이름은 `baldur`이고(`import baldur`), PyPI 배포 이름은
`baldur-framework`입니다.

```bash
pip install baldur-framework                 # 프레임워크 독립 코어
pip install baldur-framework[django]         # Django 연동
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
    # 인자와 함께 보관해 두었다가 게이트웨이가 복구되면 재실행합니다.
    return payment_gateway.charge(order_id, amount_cents)
```

게이트웨이가 죽으면 브레이커가 열리고, 서비스는 타임아웃을 쌓아 올리는 대신 즉시
응답합니다. 나가는 길에 실패한 결제는 데드레터 큐에서 기다리다가 브레이커가
닫히면 돌아옵니다. (재실행은 나가는 길에 실패한 작업을 위한 것이지, 비즈니스
상의 거절이나 고객이 이미 떠나버린 결제를 위한 것이 아닙니다 —
[그 경계가 어디인지](docs/concepts/foundations/dlq-replay.md).)

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
| [서킷 브레이커](docs/concepts/oss/circuit-breaker.md) | 연쇄 장애를 차단하고, 복구 시 제한된 수의 half-open 탐침을 보냅니다 |
| [백오프 재시도](docs/concepts/oss/retry.md) | 지터가 적용된 지수 백오프와 상한이 있는 시도 횟수 |
| [폴백 및 조합](docs/concepts/foundations/composition.md) | 모든 복원력 패턴을 순서가 정해진 하나의 파이프라인으로 |
| [멱등성](docs/concepts/oss/idempotency.md) | 동시에 들어온 중복 호출에서도 부수 효과는 정확히 한 번만 실행됩니다 |
| [벌크헤드 격리](docs/concepts/foundations/bulkhead.md) | 의존성마다 고정된 동시성 몫을 할당해, 느린 의존성 하나가 전체 워커를 고갈시키지 못하게 합니다 |
| [데드레터 큐 + 재실행](docs/concepts/foundations/dlq-replay.md) | 끝내 실패한 호출을 문맥과 함께 포착해 두었다가, 의존성이 복구되면 재실행합니다 |
| [헬스 체크](docs/concepts/oss/health-check.md) | 실제 의존성 상태를 반영하는 liveness/readiness |
| [우아한 종료](docs/concepts/oss/graceful-shutdown.md) | 재시작과 배포 시 처리 중이던 작업을 깔끔하게 비웁니다 |
| [메트릭](docs/concepts/oss/metrics.md) | Prometheus와 OpenTelemetry, 기본으로 방출 |
| [시스템 제어](docs/concepts/oss/system-control.md) | Baldur 자동화에 대한 즉시 킬 스위치와 드라이런 모드 — 재배포 불필요 |
| [웹 콘솔](docs/concepts/foundations/web-console.md) | 내장 운영 콘솔: 실시간 브레이커 상태, 제어, 복구 |
| [사전 계산 캐시](docs/concepts/oss/precomputed-cache.md) | 헬스/상태 엔드포인트가 예열된 캐시에서 응답하므로, 끊임없는 프로빙도 비용이 낮게 유지됩니다 |

읽기 경로도 같은 방식으로 스스로 회복합니다. 아래는 실제 HTTP 트래픽을 받고 있는
Django 앱(데모 하네스가 트래픽을 넣는 상황을 녹화)이 21초 동안 Redis로 가는
네트워크 경로를 잃는 장면입니다. 모든 요청이 인메모리 캐시 계층에서 계속 200을
반환하고, Redis 계층은 복구 시점에 스스로 재동기화합니다.

![터미널 데모: Django 앱이 21초간의 Redis 장애 내내 200 응답을 유지합니다](https://raw.githubusercontent.com/baldurhq/baldur/main/.github/assets/redis-dies-app-survives.gif)

## 문서

전체 문서는 **<https://baldur.sh>** 에 있습니다.

- [What is Baldur?](docs/what-is-baldur.md) — 어떤 문제를 어떻게 푸는지
- 시작하기: [Django](docs/getting-started/django.md) ·
  [FastAPI](docs/getting-started/fastapi.md) ·
  [Flask](docs/getting-started/flask.md) ·
  [Celery](docs/getting-started/celery.md)
- [개념 가이드](https://baldur.sh) — 기능당 한 페이지, 이 README 전반에서 링크
- [API 레퍼런스](https://baldur.sh/reference/)
- [문제 해결](docs/troubleshooting.md)
- [호환성](docs/compatibility.md)

## AI 어시스턴트와 함께 쓰기

AI 코딩 어시스턴트(Claude Code, Cursor, Copilot, Codex)로 개발하고 계신가요?
저장소에서 `baldur init-ai`를 실행하면 `AGENTS.md`(Cursor·Copilot·Codex가 읽습니다)와
그것을 임포트하는 Claude Code용 `CLAUDE.md`가 생성됩니다. 이 둘이 함께 어시스턴트에게
서킷 브레이커를 직접 구현하는 대신 `@baldur.protected("name")`을 쓰도록 가르칩니다.
[AI 어시스턴트와 함께 쓰기](docs/getting-started/ai-assistants.md)를 참고하세요.

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
[호환성](docs/compatibility.md)을 참고하세요.

## 플릿 규모로 운영하시나요?

Baldur PRO는 동일한 API 위에 플릿 단위 운영을 위한 기계 장치를 더합니다 — 코어의
어떤 것도 라이선스가 바뀌거나 대체되지 않습니다.
[대규모 DLQ](docs/concepts/foundations/dlq-replay.md)(콘솔에서의 일괄 재실행,
성공률 기반 속도 조절, 디스크에 지속되는 아웃박스, 아카이브/삭제 보존 정책),
해시 체인 [감사 추적](docs/concepts/pro/audit.md),
[통합 알림](docs/concepts/pro/unified-notification.md),
[비상 모드](docs/concepts/pro/emergency-mode.md),
[벌크헤드 스레드 풀 격리](docs/concepts/foundations/bulkhead.md),
[적응형 스로틀링](docs/concepts/pro/throttle.md),
[카나리 복구](docs/concepts/pro/canary-recovery.md),
[거버넌스 게이트](docs/concepts/pro/governance.md), 그리고 Baldur 자신을 감시하는
[메타 워치독](docs/concepts/pro/meta-watchdog.md). 전체
[OSS vs PRO 기능 비교표](docs/concepts/oss-vs-pro.md)와
[가격](https://baldur.sh/pricing/)을 확인해 보세요.

## 얼리 액세스

Baldur는 얼리 액세스 단계입니다. API는 안정적이고 코어는 Sentinel 페일오버를 포함한
지속 부하 테스트를 거쳤지만, 프로젝트 자체가 아직 어립니다 — 마이너 릴리스에도
호환성이 깨지는 변경이 들어갈 수 있으며, 그럴 때는 언제나 체인지로그 항목이 함께
갑니다. 지금은 이미 Python 서비스를 프로덕션에서 운영 중인 소수의 팀과 직접 협업할
상대를 찾고 있습니다. 해당되신다면 자세한 내용과 연락 방법이
[Discussions](https://github.com/baldurhq/baldur/discussions)에 있습니다.

## 라이선스

Baldur는 Apache License 2.0으로 배포됩니다 — [LICENSE](LICENSE)와
[NOTICE](NOTICE)를 참고하세요.

## 기여하기

Apache License 2.0 아래에서의 기여를 환영합니다. 풀 리퀘스트는 사인오프 기반
[DCO](https://developercertificate.org/) 흐름으로 받습니다 — 전체 모델은
[CONTRIBUTING.md](CONTRIBUTING.md)를 참고하세요.

- **아이디어, 또는 만드신 것 자랑** →
  [Discussions](https://github.com/baldurhq/baldur/discussions).
- **버그 / 기능 요청 / 문서** → 이슈나 풀 리퀘스트를 열어 주세요.
- **보안** → [SECURITY.md](SECURITY.md)를 참고하세요 (취약점은 공개 이슈로 올리지 말아 주세요).
- **사용 문의 / 상용** → `support@baldur.sh`.
