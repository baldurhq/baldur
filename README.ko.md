# Baldur

[![CI](https://github.com/baldurhq/baldur/actions/workflows/ci-oss-mirror.yml/badge.svg)](https://github.com/baldurhq/baldur/actions/workflows/ci-oss-mirror.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)
[![PyPI](https://img.shields.io/pypi/v/baldur-framework.svg)](https://pypi.org/project/baldur-framework/)
[![Docs](https://img.shields.io/badge/docs-baldur.sh-1f6feb.svg)](https://baldur.sh)
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/13522/badge)](https://www.bestpractices.dev/projects/13522)

[English](README.md) | **한국어**

**Baldur**는 Python 애플리케이션을 위한 자가 복구(self-healing) 신뢰성 계층입니다.
서킷 브레이커, 재시도, 폴백을 데코레이터 하나 뒤에 묶어 불안정한 하위 의존성이
내 서비스로 장애를 전파하지 못하게 막습니다. 그리고 그것을 실제 프로덕션에서
운영하는 데 필요한 표면까지 함께 제공합니다 — 헬스 체크, Prometheus 및
OpenTelemetry 메트릭, 우아한 종료(graceful shutdown), 내장 웹 콘솔. 코어는
프레임워크에 독립적이며 Django, FastAPI, Flask, Celery용 어댑터를 정식으로
지원합니다.

![터미널 데모: 트래픽이 흐르는 중에 결제 게이트웨이가 응답 불능이 됩니다 — 나가던 결제 5건이 실패해 그대로 포착되고, 브레이커가 열리고, 복구 시점에 Baldur가 5건을 전부 재실행합니다. 유실 0건.](https://raw.githubusercontent.com/baldurhq/baldur/main/.github/assets/demo-self-healing.gif)

*함께 배포되는 데모의 실제 실행 화면입니다. 트래픽이 흐르는 중에 게이트웨이가
응답 불능이 됩니다 — 카드 거절이 아니라 인프라 장애입니다. 결제 5건이 실패하면서
호출 인자와 함께 포착되고, 서킷 브레이커가 열려 죽어가는 의존성을 막아주며,
브레이커가 다시 닫히는 순간 Baldur가 5건을 실제로 재실행합니다. 유실 0건.
(재실행은 나가는 길에 실패한 작업을 위한 것이지, 비즈니스 상의 거절이나 고객이
이미 떠나버린 결제를 위한 것이 아닙니다 —
[그 경계가 어디인지](docs/concepts/foundations/dlq-replay.md).) 직접 재현해 보세요:*

```bash
pip install "baldur-framework[celery]"
python -m baldur.scripts.demo_self_healing
```

*(녹화 화면의 브레이커 상태와 DLQ 집계는 실행 중인 프레임워크에서 실시간으로
읽어온 값입니다. 여러분의 서비스에서는 같은 내용이 Baldur의 구조화 로그 이벤트,
내장 웹 콘솔의 실시간 브레이커 상태, 그리고 Prometheus/OpenTelemetry 메트릭으로
드러납니다.)*

## 왜 Baldur인가?

- **데코레이터 하나에 파이프라인 전체.** `@baldur.protected("name")`이 서킷
  브레이커, 전체 대기 시간 예산, 폴백, 멱등성, 데드레터 포착을 순서가 정해진
  하나의 파이프라인으로 조합합니다 — HTTP 클라이언트나 벤더 SDK가 여러분 몫으로
  남겨두는 부분들입니다. 스스로 재시도하지 않는 호출을 위해 재시도도 함께 조합할
  수 있고, SDK가 이미 재시도한다면 그건 그대로 두고 Baldur가 그 바깥을 감쌉니다.
- **설정 없이 시작, 프로덕션 경로는 내장.** 기본 상태에서는 모든 것이 인메모리
  백엔드 위에서 동작합니다 — Redis도, 환경 변수도, Docker도 필요 없습니다. 워커가
  여러 개로 늘어나면 Redis를 추가하기만 하면 같은 코드가 전체 플릿에서 상태를
  공유합니다. 호출부는 전혀 바뀌지 않습니다.
- **가져다 쓰는 데서 끝나지 않고, 운영합니다.** 내장 웹 콘솔이 모든 브레이커의
  실시간 상태를 보여주고 런타임 on/off 제어를 제공합니다. 헬스 체크는 로드
  밸런서에 사실을 알려주고, 메트릭은 기본으로 나옵니다.
- **프레임워크에 자연스럽게.** Django, FastAPI, Flask, Celery 어댑터가 시작
  시점에 캐시·메트릭·라이프사이클 훅을 배선하므로, 보호 기능이 프레임워크의
  관용구를 우회하지 않고 그 안에서 동작합니다.

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

## 간단한 예제

```python
import baldur


@baldur.protected("llm-summarize")
def summarize(doc_id: str) -> str:
    # 기본적으로 서킷 브레이커로 감싸집니다. 설정을 전혀 하지 않아도
    # 인메모리 백엔드 위에서 동작합니다 — Redis도, 환경 변수도, Docker도 필요 없습니다.
    return llm_api.summarize(doc_id)
```

의존성이 실패하기 시작하면 — 결제 게이트웨이, 데이터베이스, 장애 중인 모델
제공자 — 브레이커가 열리고, 서비스는 타임아웃을 쌓아 올리는 대신 즉시 응답합니다.
기본값 이상이 필요하다면 파이프라인을 선언적으로 조합하면 됩니다.

```python
@baldur.protected(
    "llm-summarize",
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

## Baldur PRO

PRO는 동일한 API 위에 지속성과 플릿 단위 운영을 위한 기계 장치를 더합니다 —
코어의 어떤 것도 라이선스가 바뀌거나 대체되지 않습니다. 주요 항목:
[대규모 DLQ](docs/concepts/foundations/dlq-replay.md)(콘솔에서의 일괄 재실행,
성공률 기반 속도 조절, 디스크에 지속되는 아웃박스, 아카이브/삭제 보존 정책),
해시 체인 [감사 추적](docs/concepts/pro/audit.md),
[통합 알림](docs/concepts/pro/unified-notification.md),
[비상 모드](docs/concepts/pro/emergency-mode.md),
[벌크헤드 스레드 풀 격리](docs/concepts/foundations/bulkhead.md),
[적응형 스로틀링](docs/concepts/pro/throttle.md),
[카나리 복구](docs/concepts/pro/canary-recovery.md),
[거버넌스 게이트](docs/concepts/pro/governance.md), 그리고 Baldur 자신을 감시하는
[메타 워치독](docs/concepts/pro/meta-watchdog.md).

전체 [OSS vs PRO 기능 비교표](docs/concepts/oss-vs-pro.md)와
[가격](https://baldur.sh/pricing/)을 확인해 보세요.

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
