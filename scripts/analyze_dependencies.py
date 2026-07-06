#!/usr/bin/env python3
"""
Baldur 코드 의존성 분석 스크립트
- 모듈 간 import 관계 분석
- 고아 모듈 식별
- 가장 많이 참조되는 모듈/의존성이 많은 모듈 계산
- re-export 패턴 탐지 및 직접 import 권장
"""

import ast
from pathlib import Path
from collections import defaultdict
import json

# baldur 패키지 경로
PACKAGE_ROOT = Path(__file__).parent.parent / "packages" / "baldur-python" / "src" / "baldur"


def get_module_name(filepath: Path) -> str:
    """파일 경로를 모듈명으로 변환"""
    rel_path = filepath.relative_to(PACKAGE_ROOT)
    parts = list(rel_path.parts)
    # .py 확장자 제거
    if parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    # __init__ 처리
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else "__init__"


def extract_imports(filepath: Path) -> list:
    """AST를 사용하여 import문 추출"""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except:
        return []

    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)

    return imports


def extract_imports_detailed(filepath: Path) -> list:
    """AST를 사용하여 상세 import 정보 추출 (re-export 분석용)"""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
            tree = ast.parse(content)
    except:
        return []

    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module:
                for alias in node.names:
                    imports.append({
                        "module": node.module,
                        "name": alias.name,
                        "alias": alias.asname,
                        "level": node.level,  # 0=absolute, 1=relative (.), 2=(..) etc
                        "line": node.lineno,
                    })
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.append({
                    "module": alias.name,
                    "name": None,
                    "alias": alias.asname,
                    "level": 0,
                    "line": node.lineno,
                })
    return imports


def find_reexports(modules: dict) -> dict:
    """
    __init__.py에서 re-export하는 패턴 찾기
    
    Returns:
        dict: {package_name: [(exported_name, original_module), ...]}
    """
    reexports = defaultdict(list)
    
    for mod_name, filepath in modules.items():
        # __init__.py 파일만 분석
        if not str(filepath).endswith("__init__.py"):
            continue
            
        imports = extract_imports_detailed(filepath)
        
        for imp in imports:
            # 상대 import (from .xxx import YYY)
            if imp["level"] > 0 and imp["name"] and imp["name"] != "*":
                # 패키지 경로 계산
                if mod_name == "__init__":
                    package = ""
                else:
                    package = mod_name
                    
                # 원본 모듈 경로
                if imp["module"]:
                    original_module = f"{package}.{imp['module']}" if package else imp["module"]
                else:
                    original_module = package
                    
                reexports[package].append({
                    "name": imp["name"],
                    "original_module": original_module,
                    "line": imp["line"],
                })
    
    return reexports


def find_reexport_usages(modules: dict, reexports: dict) -> list:
    """
    re-export를 통해 import하는 곳 찾기
    
    Returns:
        list: [(file, line, current_import, suggested_import), ...]
    """
    usages = []
    
    # re-export 맵 생성: {(package, name): original_module}
    reexport_map = {}
    for package, exports in reexports.items():
        for exp in exports:
            key = (package, exp["name"])
            reexport_map[key] = exp["original_module"]
    
    for mod_name, filepath in modules.items():
        # __init__.py는 제외 (re-export 정의하는 곳이니까)
        if str(filepath).endswith("__init__.py"):
            continue
            
        imports = extract_imports_detailed(filepath)
        
        for imp in imports:
            if imp["level"] > 0:
                continue  # 상대 import는 스킵
                
            module = imp["module"]
            name = imp["name"]
            
            if not name or name == "*":
                continue
                
            # baldur. 접두사 제거
            if module and module.startswith("baldur."):
                module = module[12:]
            
            # re-export 사용 여부 확인
            key = (module, name)
            if key in reexport_map:
                original = reexport_map[key]
                usages.append({
                    "file": str(filepath.relative_to(PACKAGE_ROOT)),
                    "module": mod_name,
                    "line": imp["line"],
                    "current": f"from baldur.{module} import {name}",
                    "suggested": f"from baldur.{original} import {name}",
                    "package": module,
                    "name": name,
                    "original_module": original,
                })
    
    return usages


def normalize_import(imp: str) -> str:
    """baldur 패키지 내부 import를 정규화"""
    # baldur.xxx -> xxx
    if imp.startswith("baldur."):
        return imp[12:]
    # 상대 import는 이미 처리됨
    return imp


def is_internal_import(imp: str) -> bool:
    """baldur 내부 import인지 확인"""
    return (
        imp.startswith("baldur.")
        or "." not in imp
        or imp.split(".")[0]
        in [
            "adapters",
            "api",
            "audit",
            "config",
            "context",
            "core",
            "factory",
            "interfaces",
            "metrics",
            "models",
            "resilience",
            "services",
            "tasks",
            "utils",
            "config_tracker",
            "slo",
        ]
    )


def main():
    # 모든 Python 파일 수집
    all_files = list(PACKAGE_ROOT.rglob("*.py"))
    all_files = [f for f in all_files if "__pycache__" not in str(f)]

    print(f"📊 분석 대상: {len(all_files)}개 파일")

    # 모듈 이름 -> 파일 경로 매핑
    modules = {}
    for f in all_files:
        mod_name = get_module_name(f)
        modules[mod_name] = f

    print(f"📦 전체 모듈 수: {len(modules)}개")

    # 의존성 그래프 구축
    dependencies = defaultdict(set)  # module -> [imports...]
    reverse_deps = defaultdict(set)  # module -> [imported by...]

    for mod_name, filepath in modules.items():
        imports = extract_imports(filepath)
        for imp in imports:
            norm_imp = normalize_import(imp)
            # 내부 모듈만 추적
            if is_internal_import(imp):
                # 최상위 모듈로 정규화
                parts = norm_imp.split(".")
                # 다양한 길이로 매칭 시도
                for i in range(len(parts), 0, -1):
                    candidate = ".".join(parts[:i])
                    if candidate in modules and candidate != mod_name:
                        dependencies[mod_name].add(candidate)
                        reverse_deps[candidate].add(mod_name)
                        break

    # 의존성 있는 모듈 수
    has_deps = sum(1 for m in modules if dependencies[m])
    print(f"🔗 의존성이 있는 모듈: {has_deps}개")

    # 고아 모듈 (다른 모듈에서 import되지 않음)
    orphans = [m for m in modules if not reverse_deps[m]]
    print(f"🏝️  고아 모듈: {len(orphans)}개")

    # 가장 많이 참조되는 모듈 (Top 20)
    ref_counts = [(m, len(reverse_deps[m])) for m in modules if reverse_deps[m]]
    ref_counts.sort(key=lambda x: -x[1])

    print("\n🏆 가장 많이 참조되는 모듈 (Top 20):")
    print("-" * 50)
    for i, (mod, count) in enumerate(ref_counts[:20], 1):
        print(f"  {i:2}. {mod:<45} {count}회")

    # 가장 많은 의존성을 가진 모듈 (Top 15)
    dep_counts = [(m, len(dependencies[m])) for m in modules if dependencies[m]]
    dep_counts.sort(key=lambda x: -x[1])

    print("\n📈 가장 많은 의존성을 가진 모듈 (Top 15):")
    print("-" * 50)
    for i, (mod, count) in enumerate(dep_counts[:15], 1):
        print(f"  {i:2}. {mod:<45} {count}개")

    # 고아 모듈 분류
    print("\n⚠️  고아 모듈 분류:")
    print("-" * 50)

    # 카테고리별 분류
    entry_points = []
    migrations = []
    admin_apps = []
    tests = []
    check_needed = []

    for m in sorted(orphans):
        if "migrations" in m:
            migrations.append(m)
        elif "admin" in m or "apps" in m:
            admin_apps.append(m)
        elif "urls" in m or "middleware" in m or "routes" in m:
            entry_points.append(m)
        elif "tasks" in m:
            entry_points.append(m)
        elif any(x in m for x in ["null_adapter", "stdout_adapter", "file_adapter"]):
            check_needed.append(m)
        else:
            check_needed.append(m)

    print(f"\n  📍 엔트리포인트/미들웨어: {len(entry_points)}개")
    for m in entry_points[:10]:
        print(f"      - {m}")
    if len(entry_points) > 10:
        print(f"      ... 외 {len(entry_points) - 10}개")

    print(f"\n  📋 Django Admin/Apps: {len(admin_apps)}개")
    for m in admin_apps:
        print(f"      - {m}")

    print(f"\n  🔄 마이그레이션: {len(migrations)}개")

    print(f"\n  ⚠️  검토 필요: {len(check_needed)}개")
    for m in check_needed:
        print(f"      - {m}")

    # JSON 출력 (문서 생성용)
    result = {
        "total_modules": len(modules),
        "has_dependencies": has_deps,
        "orphan_count": len(orphans),
        "top_referenced": ref_counts[:20],
        "top_dependencies": dep_counts[:15],
        "orphans": sorted(orphans),
        "orphan_categories": {
            "entry_points": entry_points,
            "migrations": migrations,
            "admin_apps": admin_apps,
            "check_needed": check_needed,
        },
    }

    output_path = Path(__file__).parent.parent / "docs" / "baldur" / "dependency_analysis_result.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"\n✅ 결과 저장: {output_path}")

    # === re-export 분석 ===
    print("\n" + "=" * 60)
    print("🔄 RE-EXPORT 패턴 분석")
    print("=" * 60)
    
    reexports = find_reexports(modules)
    total_reexports = sum(len(v) for v in reexports.values())
    print(f"\n📦 re-export 정의: {total_reexports}개 ({len(reexports)}개 패키지)")
    
    # re-export가 많은 패키지 Top 10
    reexport_counts = [(pkg, len(exports)) for pkg, exports in reexports.items()]
    reexport_counts.sort(key=lambda x: -x[1])
    
    print("\n🏆 re-export가 많은 패키지 (Top 10):")
    print("-" * 50)
    for pkg, count in reexport_counts[:10]:
        pkg_display = pkg if pkg else "(root __init__)"
        print(f"    {pkg_display:<40} {count}개")
    
    # re-export 사용처 분석
    reexport_usages = find_reexport_usages(modules, reexports)
    print(f"\n⚠️  re-export를 통한 import: {len(reexport_usages)}개")
    
    if reexport_usages:
        # 패키지별 그룹화
        usage_by_package = defaultdict(list)
        for usage in reexport_usages:
            usage_by_package[usage["package"]].append(usage)
        
        print("\n📋 re-export 사용 현황 (직접 import 권장):")
        print("-" * 70)
        
        for pkg in sorted(usage_by_package.keys()):
            usages = usage_by_package[pkg]
            print(f"\n  📦 {pkg} ({len(usages)}개)")
            for u in usages[:5]:  # 패키지당 최대 5개만 표시
                print(f"      {u['file']}:{u['line']}")
                print(f"        현재: {u['current']}")
                print(f"        권장: {u['suggested']}")
            if len(usages) > 5:
                print(f"      ... 외 {len(usages) - 5}개")
    
    # JSON에 re-export 정보 추가
    result["reexports"] = {
        "total_definitions": total_reexports,
        "packages": {pkg: len(exports) for pkg, exports in reexports.items()},
        "usage_count": len(reexport_usages),
        "usages": reexport_usages,
    }
    
    # ==========================================================================
    # Internal Import Linter - 패키지 내부에서 Public API 사용 금지
    # ==========================================================================
    print("\n" + "=" * 60)
    print("🔍 Internal Import Linter (패키지 내부 Public API 사용 검사)")
    print("=" * 60)
    
    internal_violations = check_internal_import_violations(modules)
    result["internal_import_violations"] = internal_violations
    
    if internal_violations:
        print(f"\n⚠️  {len(internal_violations)}개의 위반 발견!")
        print("   패키지 내부에서는 직접 import를 사용해야 합니다.\n")
        for v in internal_violations[:10]:
            print(f"   {v['file']}:{v['line']}")
            print(f"      ❌ {v['import']}")
            print(f"      ✅ {v['suggested']}")
            print()
        if len(internal_violations) > 10:
            print(f"   ... 외 {len(internal_violations) - 10}개")
    else:
        print("\n✅ 모든 내부 코드가 직접 import를 사용합니다.")
    
    # JSON 다시 저장
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    
    print(f"\n✅ re-export 분석 결과 추가 저장: {output_path}")
    
    # 정리 작업 요약
    if reexport_usages:
        print("\n" + "=" * 60)
        print("📝 권장 조치")
        print("=" * 60)
        print(f"""
1. re-export 사용 import {len(reexport_usages)}개를 직접 import로 변경
   - 유지보수성 향상
   - IDE 지원 개선 (정의로 이동)
   - 순환 import 위험 감소

2. __init__.py의 re-export 제거 검토
   - re-export 정의: {total_reexports}개
   - 사용되지 않는 re-export 정리 가능

3. 직접 import 예시:
   ❌ from baldur.services import CircuitBreakerService
   ✅ from baldur.services.circuit_breaker.service import CircuitBreakerService
""")


def check_internal_import_violations(modules: dict) -> list:
    """
    패키지 내부에서 Public API(__init__.py re-export) 사용을 검사.
    
    규칙: baldur 패키지 내부 코드는 다음 패턴을 사용하면 안 됨:
    - from baldur.services import X (services/__init__.py 경유)
    - from baldur.services.metrics import X (metrics/__init__.py 경유)
    - from baldur.api.django.tiering import X (tiering/__init__.py 경유)
    등
    
    Returns:
        list: [{"file": str, "line": int, "import": str, "suggested": str}, ...]
    """
    violations = []
    
    # 금지된 패턴 (패키지 __init__.py 경유 import)
    forbidden_patterns = [
        # (pattern, suggested_base)
        ("baldur.services.metrics", "baldur.services.metrics.recorders 또는 .definitions"),
        ("baldur.services.circuit_breaker", "baldur.services.circuit_breaker.service 또는 .config"),
        ("baldur.services.chaos", "baldur.services.chaos.experiments 등"),
        ("baldur.api.django.tiering", "baldur.api.django.tiering.registry 등"),
        ("baldur.api.django.views.error_budget", "baldur.api.django.views.error_budget.status 등"),
        ("baldur.api.django.views.xtest", "baldur.api.django.views.xtest.circuit_breaker 등"),
        ("baldur.tasks", "baldur.tasks.base 또는 개별 태스크 모듈"),
        ("baldur.adapters.audit", "baldur.adapters.audit.file_adapter 등"),
    ]
    
    # services/__init__.py 경유도 금지 (패키지 내부에서만)
    # 단, factory.py는 호환성 레이어이므로 제외
    
    for mod_name, filepath in modules.items():
        # __init__.py 파일은 검사 제외 (자기 자신)
        if str(filepath).endswith("__init__.py"):
            continue
        # factory.py는 호환성 레이어이므로 제외
        if str(filepath).endswith("factory.py"):
            continue
            
        imports = extract_imports_detailed(filepath)
        
        for imp in imports:
            module = imp.get("module", "")
            if not module:
                continue
                
            # 금지된 패턴 검사
            for pattern, suggested in forbidden_patterns:
                # 정확히 패키지 경로와 일치하는 경우만 (하위 모듈 직접 import는 OK)
                if module == pattern:
                    violations.append({
                        "file": str(filepath.relative_to(PACKAGE_ROOT)),
                        "line": imp.get("line", 0),
                        "import": f"from {module} import {imp.get('name', '?')}",
                        "suggested": f"from {suggested} import {imp.get('name', '?')}",
                    })
    
    return violations


if __name__ == "__main__":
    main()
