"""AST 기반 하드코딩 timeout/window 탐지 린트 테스트.

services/ 디렉토리에서 timeout=N, window=N 형태의 하드코딩을 AST로 탐지한다.
주석/문자열 내부 코드를 무시하고, settings 속성 참조(ast.Attribute)는 자동 제외.
"""

import ast
from pathlib import Path

# 313 범위 외 사전 존재 하드코딩 — 별도 이슈로 처리 예정
_KNOWN_EXCEPTIONS: set[str] = {
    "canary/cross_cluster.py",
    "circuit_breaker/adaptive_threshold.py",
    "circuit_breaker/models.py",
    "config/propagator.py",
    "correlation_engine/wildcard_observer.py",
    "event_bus/redis_bus.py",
    "metrics/registry.py",
    "postmortem/incident_group.py",
    "postmortem/store.py",
    "throttle/audit.py",
}


class HardcodedTimeoutVisitor(ast.NodeVisitor):
    """AST 기반 하드코딩 timeout/window 탐지."""

    def __init__(self, filepath: Path):
        self.filepath = filepath
        self.violations: list[str] = []

    def visit_Call(self, node: ast.Call) -> None:
        for kw in node.keywords:
            if kw.arg in ("timeout", "window") and isinstance(kw.value, ast.Constant):
                if isinstance(kw.value.value, (int, float)):
                    self.violations.append(
                        f"{self.filepath}:{node.lineno}: "
                        f"hardcoded {kw.arg}={kw.value.value}"
                    )
        self.generic_visit(node)


class TestHardcodedTimeoutComplianceContract:
    """서비스 코드의 하드코딩 timeout/window 부재 계약 검증."""

    def test_no_hardcoded_timeouts_in_services(self):
        """services/ 디렉토리에서 timeout=N, window=N 형태의 하드코딩을 AST로 탐지."""
        services_dir = Path("packages/baldur-python/src/baldur/services")
        violations = []

        for py_file in services_dir.rglob("*.py"):
            rel = py_file.relative_to(services_dir)
            rel_str = str(rel).replace("\\", "/")

            if rel_str in _KNOWN_EXCEPTIONS:
                continue

            source = py_file.read_text(encoding="utf-8")
            try:
                tree = ast.parse(source, filename=str(py_file))
            except SyntaxError:
                continue
            visitor = HardcodedTimeoutVisitor(rel)
            visitor.visit(tree)
            violations.extend(visitor.violations)

        assert not violations, (
            f"Found {len(violations)} hardcoded values in services:\n"
            + "\n".join(violations)
        )
