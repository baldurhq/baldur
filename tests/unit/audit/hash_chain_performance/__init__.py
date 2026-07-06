"""
Hash Chain Performance 최적화 테스트 패키지.

해시 체인 성능 최적화 컴포넌트 테스트:
- LuaAtomicHashChain: Redis Lua Script로 5 RTT → 1 RTT 최적화
- PipelineBatchQuery: 다중 체인 상태 일괄 조회
- BatchFlushWriter: n×fsync → 1×fsync 배치 저장
- AsyncAuditWriter: 응답 블로킹 제거 비동기 저장
- SamplingVerifier: O(n) → O(k) 확률적 검증
- PendingSequenceWatchdog: Self-Cleanup 워치독
"""
