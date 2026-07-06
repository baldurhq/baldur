"""
Hash Chain Graceful Degradation 테스트 패키지.

장애 허용(fault-tolerant) 기능 테스트:
- HashChainFallbackChain: 다단계 폴백 (Redis → Replica → Local → Memory)
- DegradedEntryMarker: 장애 중 기록된 엔트리 마킹
- HashChainWALRecovery: WAL 기반 크래시 복구
- HashChainDegradationManager: 통합 degradation 레벨 관리 (NORMAL→DEGRADED→EMERGENCY)
- HashChainCircuitBreaker: 해시 체인 작업용 서킷 브레이커
- HashChainGracefulDegradationManager: 통합 액세스 인터페이스
"""
