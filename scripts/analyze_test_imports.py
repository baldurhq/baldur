"""
Analyze imports from baldur.services in multiple directories
"""
import re
from collections import Counter
from pathlib import Path


def analyze_imports(base_paths: list[str], title: str):
    patterns = []
    
    for base_path in base_paths:
        base = Path(base_path)
        if not base.exists():
            continue
            
        for py_file in base.rglob('*.py'):
            try:
                content = py_file.read_text(encoding='utf-8', errors='ignore')
            except Exception:
                continue
                
            # Match 'from baldur.services import (...)' including multiline
            for match in re.finditer(r'from baldur\.services import \(([^)]+)\)', content, re.DOTALL):
                imports = match.group(1)
                for item in imports.split(','):
                    item = item.strip().split()[0] if item.strip() else ''
                    if item and not item.startswith('#'):
                        patterns.append(item)
            
            # Single line imports: from baldur.services import X, Y, Z
            for match in re.finditer(r'from baldur\.services import ([A-Za-z_][A-Za-z0-9_\s,]+?)(?:\n|$)', content):
                imports = match.group(1)
                if '(' not in imports:
                    for item in imports.split(','):
                        item = item.strip().split()[0] if item.strip() else ''
                        if item and not item.startswith('#') and not item.startswith('('):
                            patterns.append(item)

    counter = Counter(patterns)
    print("=" * 60)
    print(title)
    print("=" * 60)
    print(f"{'Count':>5}  Symbol")
    print("-" * 60)
    for name, count in counter.most_common(100):
        print(f'{count:5d}  {name}')
    print("-" * 60)
    print(f"Total unique symbols: {len(counter)}")
    print(f"Total import occurrences: {sum(counter.values())}")
    print()
    return counter


if __name__ == "__main__":
    # Tests 분석
    test_imports = analyze_imports(['tests/'], "Tests에서 baldur.services import 사용 현황")
    
    # Shopping 분석 (tests 제외)
    shopping_imports = Counter()
    base = Path('shopping/')
    if base.exists():
        patterns = []
        for py_file in base.rglob('*.py'):
            if 'tests' in str(py_file):
                continue  # shopping/tests는 제외
            try:
                content = py_file.read_text(encoding='utf-8', errors='ignore')
            except Exception:
                continue
            for match in re.finditer(r'from baldur\.services import \(([^)]+)\)', content, re.DOTALL):
                imports = match.group(1)
                for item in imports.split(','):
                    item = item.strip().split()[0] if item.strip() else ''
                    if item and not item.startswith('#'):
                        patterns.append(item)
            for match in re.finditer(r'from baldur\.services import ([A-Za-z_][A-Za-z0-9_\s,]+?)(?:\n|$)', content):
                imports = match.group(1)
                if '(' not in imports:
                    for item in imports.split(','):
                        item = item.strip().split()[0] if item.strip() else ''
                        if item and not item.startswith('#') and not item.startswith('('):
                            patterns.append(item)
        shopping_imports = Counter(patterns)
        
        print("=" * 60)
        print("Shopping (tests 제외)에서 baldur.services import 사용 현황")
        print("=" * 60)
        print(f"{'Count':>5}  Symbol")
        print("-" * 60)
        for name, count in shopping_imports.most_common(100):
            print(f'{count:5d}  {name}')
        print("-" * 60)
        print(f"Total unique symbols: {len(shopping_imports)}")
        print(f"Total import occurrences: {sum(shopping_imports.values())}")
        print()

    # 통합 현황
    all_symbols = test_imports + shopping_imports
    print("=" * 60)
    print("전체 외부 사용 현황 (유지 필요)")
    print("=" * 60)
    print(f"{'Count':>5}  Symbol")
    print("-" * 60)
    for name, count in all_symbols.most_common(100):
        print(f'{count:5d}  {name}')
    print("-" * 60)
    print(f"Total unique symbols: {len(all_symbols)}")
    print(f"Total import occurrences: {sum(all_symbols.values())}")
