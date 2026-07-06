"""
Rate Limit 단위 테스트 패키지.

분리된 모듈별 테스트:
- test_coordinator: RateLimitCoordinator (이벤트, 디바운싱, Canary, Cooldown, 데코레이터)
- test_adaptive_throttle_429: AdaptiveThrottle 429 연동 (감소, Conservative, Priority, Recovery)
- test_escalation_handler: RateLimitEscalationHandler (에스컬레이션, 리셋)
- test_distributed_channel: DistributedRateLimitChannel (Kafka broadcast, 핸들러)
- test_integration_settings: RateLimitThrottleIntegrationSettings (설정 검증)
"""
